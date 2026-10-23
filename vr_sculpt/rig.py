"""Rig mode: build an armature with the controllers, bind a mesh to it with automatic weights, and
paint its bones' weights.

Bones tool (the rig in Edit Mode): each trigger press places the next joint. The first press of a
chain sets its root; every press after that ends a bone at the tip and the next press carries on
from there, connected (hold and move to place the joint precisely). Between presses a rubber band
runs from the last joint to the tip. A press on a joint carries on from it: on a bone's tail the next
bone is its child, so pressing an inner joint branches; on a root's head, a new root starts beside
it. Dragging a joint moves it, and the bones connected to it follow. The left trigger deletes the
bone it touches, or ends the chain in empty space; End chain (B) ends it too. Mirror X builds and
edits .L / .R pairs across the rig's X = 0 plane (a new rig's origin is its first joint).

Bind: an Armature modifier and automatic weights (Blender's bone heat, object.parent_set with
type 'ARMATURE_AUTO').

Weights tool (the rig in Object Mode): a numpy brush, like brushes.py, writes the active bone's
vertex group: Add / Subtract / Smooth in a sphere at the tip, pressure-sensitive, the left hand doing
the opposite (Smooth smooths with both). Vertex groups aren't mesh attributes in 5.4, but
VertexGroup.add() for one vertex costs under a microsecond, so only the touched vertices are
written. A press near a bone, and nearer to it than to the surface, picks that bone instead. The
weights show as a heat map drawn in the headset (Blender's weight overlay isn't drawn in VR).

Nothing here keeps Blender data from one frame or press to the next: rigs, bones, meshes and groups
are found by name, and the caches holding mesh data are cleared on undo (forget_caches).
"""

import time

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import brushes, grid, primitives
from . import pose as pose_mode
from .pose import find_rig

REACH = 0.02  # hand-space meters around a joint or bone that still count as touching it
MIN_LENGTH = 0.01  # hand-space meters: closer than this to the last joint, a press makes no bone
CENTER = 1e-4  # rig-space units: a bone whose middle is this close to X = 0 is on the mirror plane
WEIGHT_RATE = 4.0  # weight added per second at full strength and pressure (Add / Subtract)
SMOOTH_RATE = 10.0  # how fast Smooth pulls a weight to its neighbors' mean
EPS = 1e-4  # weights below this leave the vertex group
HISTORY = 20  # weight strokes B can take back
HEAT_ALPHA = 0.7
HEAT_FPS = 30.0  # the heat map's colors are rebuilt at most this often while painting
HEAT_LIFT = 0.0005  # share of the mesh's size the heat map sits out along the normals
# The heat map lies on the mesh, so the two z-fight wherever the lift along the normals is too small
# (flat faces whose vertex normals lean away, far-off faces). Its shader pulls every vertex toward the
# eye by this share of its distance: the same pixel, a bit nearer (1 mm at 1 m), whatever the view's
# clip range.
HEAT_PULL = 0.001

BRUSH_COLORS = {'ADD': (1.0, 0.45, 0.3), 'SUBTRACT': (0.35, 0.6, 1.0), 'SMOOTH': (0.45, 0.9, 0.6)}
BONES_COLOR = (0.75, 0.6, 1.0)
DELETE_COLOR = (1.0, 0.35, 0.35)
JOINT_SIZE = 0.004  # hand-space meters

# previous: the object that was active before Rig mode. new: the next joint starts a new rig.
# anchor: (rig name or "", point, parent bone name or None, connect) the next bone starts from; the
# point is in the rig's space, or world space while there's no rig yet. made: (rig, bone) names in
# the order they were made, for the panel's undo. message: (text, time) after Bind.
_state = {"previous": None, "new": False, "anchor": None, "made": [], "message": ("", 0.0)}
MESSAGE_SECONDS = 6.0


# --- which rig and mesh ---------------------------------------------------------

def target(context):
    """The armature being rigged: the active armature, or the active mesh's; None for a new rig."""
    if _state["new"]:
        return None
    return find_rig(context.view_layer.objects.active)


def rigs(context):
    return [o for o in context.scene.objects if o.type == 'ARMATURE']


def meshes(context):
    return [o for o in context.scene.objects if o.type == 'MESH']


def bound(obj, arm):
    """Whether arm deforms mesh obj (an Armature modifier pointing at it)."""
    return obj is not None and arm is not None and any(
        m.type == 'ARMATURE' and m.object == arm for m in obj.modifiers)


def mesh(context):
    """The mesh to bind and paint: the one chosen on the panel, else one the rig already deforms."""
    settings = context.scene.vr_sculpt
    obj = settings.rig_mesh
    if obj is not None and obj.type == 'MESH' and obj.name in context.scene.objects:
        return obj
    arm = target(context)
    if arm is not None:
        for o in context.scene.objects:
            if o.type == 'MESH' and bound(o, arm):
                return o
    return None


def wanted_mode(settings):
    return 'EDIT' if settings.rig_tool == 'BONES' else 'OBJECT'


def sync_mode(context):
    """The rig in Edit Mode for the Bones tool, in Object Mode for Weights."""
    from . import menus
    arm = target(context)
    if arm is not None and arm.visible_get():
        menus.set_object_mode(context, arm, wanted_mode(context.scene.vr_sculpt))


def enter(context):
    """Rig mode: the active mesh becomes the one to bind and paint, and its rig (or the active
    armature) the one to build; with neither, the first joint starts a new rig."""
    settings = context.scene.vr_sculpt
    active = context.view_layer.objects.active
    _state.update(new=False, anchor=None, message=("", 0.0))
    forget_caches()
    if active is not None and active.type == 'MESH':
        settings.rig_mesh = active
    arm = find_rig(active) or find_rig(settings.rig_mesh)
    if arm is not None:
        use_rig(context, arm)


def use_rig(context, arm):
    from . import menus
    active = context.view_layer.objects.active
    if active is not None and active is not arm:
        menus.set_object_mode(context, active, 'OBJECT')
        if _state["previous"] is None:
            _state["previous"] = active.name
    _state["new"] = False
    if arm.visible_get():
        primitives.make_active(context, arm)
        sync_mode(context)


def leave(context):
    """Back to Object Mode (bones made in Edit Mode are kept); the object active before is active again."""
    from . import menus
    active = context.view_layer.objects.active
    if active is not None and active.type == 'ARMATURE':
        menus.set_object_mode(context, active, 'OBJECT')
    previous = bpy.data.objects.get(_state["previous"] or "")
    _state.update(previous=None, new=False, anchor=None)
    if previous is not None and previous is not active and previous.name in context.view_layer.objects:
        primitives.make_active(context, previous)


def choose_rig(context, name):
    """Rig picker: build on this armature, or "" for a new rig (started by the next joint)."""
    from . import menus
    obj = bpy.data.objects.get(name)
    _state["anchor"] = None
    if obj is None or obj.type != 'ARMATURE':
        active = context.view_layer.objects.active
        if active is not None and active.type == 'ARMATURE':
            menus.set_object_mode(context, active, 'OBJECT')
        _state["new"] = True
    else:
        use_rig(context, obj)


def choose_mesh(context, name):
    obj = bpy.data.objects.get(name)
    context.scene.vr_sculpt.rig_mesh = obj if obj is not None and obj.type == 'MESH' else None


def set_tool(context, tool):
    context.scene.vr_sculpt.rig_tool = tool
    _state["anchor"] = None
    sync_mode(context)


def message():
    text, when = _state["message"]
    return text if text and time.monotonic() - when < MESSAGE_SECONDS else ""


def _say(text):
    _state["message"] = (text, time.monotonic())
    print("VR Rig:", text, flush=True)
    return text


# --- bones ------------------------------------------------------------------------

def active_bone(arm):
    """The rig's active bone's name, or None."""
    if arm is None:
        return None
    bone = arm.data.edit_bones.active if arm.mode == 'EDIT' else arm.data.bones.active
    return bone.name if bone is not None else None


def set_active_bone(arm, name):
    if arm.mode == 'EDIT':
        eb = arm.data.edit_bones
        for b in eb:
            b.select = b.select_head = b.select_tail = b.name == name
        eb.active = eb.get(name)
    else:
        bone = arm.data.bones.get(name)
        if bone is not None:
            arm.data.bones.active = bone


def bone_names(arm, deform=False):
    bones = arm.data.edit_bones if arm.mode == 'EDIT' else arm.data.bones
    return [b.name for b in bones if not deform or b.use_deform]


def _frames(arm):
    """[(bone name, world matrix at the head with Y along the bone, world tail)] of the visible bones:
    the edit bones in Edit Mode, else the posed bones."""
    mw = arm.matrix_world
    if arm.mode == 'EDIT':
        return [(b.name, mw @ b.matrix, mw @ b.tail) for b in arm.data.edit_bones if not b.hide]
    return [(pb.name, mw @ pb.matrix, mw @ pb.tail) for pb in arm.pose.bones if pose_mode.visible(pb)]


def joints(arm):
    """[(bone name, 'HEAD' or 'TAIL', world point)]: every bone's tail, and the heads that aren't
    their parent's tail (roots and loose bones). Edit Mode only."""
    if arm is None or arm.mode != 'EDIT':
        return []
    mw = arm.matrix_world
    out = []
    for b in arm.data.edit_bones:
        if b.hide:
            continue
        if b.parent is None or not b.use_connect:
            out.append((b.name, 'HEAD', mw @ b.head))
        out.append((b.name, 'TAIL', mw @ b.tail))
    return out


def nearest_joint(arm, point, scale):
    """(bone name, end, world point) of the joint within reach of point, or None."""
    point = Vector(point)
    best, best_d = None, REACH * scale
    for name, end, p in joints(arm):
        d = (p - point).length
        if d <= best_d:
            best, best_d = (name, end, p), d
    return best


def nearest_bone(arm, point, scale):
    """(bone name, distance) of the bone within reach of point, or None."""
    if arm is None:
        return None
    point = Vector(point)
    best, best_d = None, REACH * scale
    for name, m, tail in _frames(arm):
        d, _f = pose_mode._closest(point, m.translation, tail)
        if d <= best_d:
            best, best_d = name, d
    return (best, best_d) if best is not None else None


def anchor(arm):
    """(point, parent bone name or None, connect) the next bone starts from, or None. The point is in
    arm's space (world space with no rig)."""
    a = _state["anchor"]
    if a is None:
        return None
    rig_name, point, parent, connect = a
    if arm is None:
        return (point.copy(), None, False) if rig_name == "" else None
    if rig_name != arm.name:
        return None
    if parent is not None and (arm.mode != 'EDIT' or parent not in arm.data.edit_bones):
        return None
    return point.copy(), parent, connect


def anchor_world(arm):
    a = anchor(arm)
    if a is None:
        return None
    return arm.matrix_world @ a[0] if arm is not None else a[0]


def end_chain():
    _state["anchor"] = None


def _mirrored(p):
    return Vector((-p.x, p.y, p.z))


def _side(head, tail):
    mid = (head.x + tail.x) / 2
    return 'L' if mid > CENTER else 'R' if mid < -CENTER else ''


def _pair_names(eb, side):
    """(name for this side, name for the other): the first BoneNNN.L / .R pair with both names free."""
    n = 0
    while True:
        base = "Bone" if n == 0 else f"Bone.{n:03d}"
        left, right = base + ".L", base + ".R"
        if left not in eb and right not in eb:
            return (left, right) if side == 'L' else (right, left)
        n += 1


def add_bone(arm, head, tail, parent=None, connect=False, mirror=False):
    """A new edit bone from head to tail (rig space), a child of bone `parent`. With mirror, a bone off
    the mirror plane gets a .L / .R counterpart too. Returns the new bone's name."""
    eb = arm.data.edit_bones
    side = _side(head, tail) if mirror else ''
    names = _pair_names(eb, side) if side else ("Bone", None)
    p = eb.get(parent) if parent else None
    bone = eb.new(names[0])
    bone.head, bone.tail = head, tail
    if p is not None:
        bone.parent = p
        bone.use_connect = connect and (p.tail - bone.head).length < 1e-6
    name = bone.name
    if names[1]:
        other = eb.new(names[1])
        other.head, other.tail = _mirrored(head), _mirrored(tail)
        op = (eb.get(bpy.utils.flip_name(parent)) if parent else None) or p
        if op is not None:
            other.parent = op
            other.use_connect = connect and (op.tail - other.head).length < 1e-6
        other.roll = -bone.roll
    return name


def _move(bone, end, point):
    if end == 'TAIL':
        bone.tail = point
        for c in bone.children:
            if c.use_connect:
                c.head = point
    else:
        bone.head = point
        parent = bone.parent
        if parent is not None and bone.use_connect:
            parent.tail = point
            for c in parent.children:
                if c.use_connect:
                    c.head = point


def move_joint(arm, name, end, point, mirror=False):
    """Move bone `name`'s head or tail (end) to point (rig space), with the bones connected there; with
    mirror, its .L / .R counterpart's the mirrored way."""
    eb = arm.data.edit_bones
    bone = eb.get(name)
    if bone is None:
        return
    _move(bone, end, point)
    if mirror:
        other = eb.get(bpy.utils.flip_name(name))
        if other is not None and other != bone:
            _move(other, end, _mirrored(point))


def delete_bone(arm, name, mirror=False):
    """Delete a bone (and, with mirror, its counterpart); its children go to its parent."""
    eb = arm.data.edit_bones
    names = [name]
    other = bpy.utils.flip_name(name)
    if mirror and other != name and other in eb:
        names.append(other)
    for n in names:
        bone = eb.get(n)
        if bone is None:
            continue
        parent = bone.parent
        for c in list(bone.children):
            keep = parent is not None and c.use_connect and (parent.tail - c.head).length < 1e-6
            c.use_connect = False
            c.parent = parent
            c.use_connect = keep
        eb.remove(bone)
    a = _state["anchor"]
    if a is not None and a[2] in names:
        _state["anchor"] = None
    _state["made"] = [m for m in _state["made"] if not (m[0] == arm.name and m[1] in names)]
    return names


def undo_bone(context):
    """The panel's undo in the Bones tool: delete the last bone made (the chain carries on from its
    parent's tail). Returns its name, or None."""
    arm = target(context)
    if arm is None or arm.mode != 'EDIT':
        return None
    eb = arm.data.edit_bones
    while _state["made"]:
        rig_name, name = _state["made"].pop()
        if rig_name != arm.name or name not in eb:
            continue
        parent = eb[name].parent
        back = (parent.name, parent.tail.copy()) if parent is not None else None
        delete_bone(arm, name, context.scene.vr_sculpt.rig_mirror)
        if back is not None and back[0] in eb:
            _state["anchor"] = (arm.name, back[1], back[0], True)
        return name
    return None


def delete_active(context):
    """The panel's Delete: the active bone (and its counterpart with Mirror X)."""
    arm = target(context)
    name = active_bone(arm)
    if arm is None or arm.mode != 'EDIT' or name is None:
        return None
    return delete_bone(arm, name, context.scene.vr_sculpt.rig_mirror)


def new_rig(context, point):
    """An empty armature with its origin at point (world), active and in Edit Mode."""
    data = bpy.data.armatures.new("Rig")
    arm = bpy.data.objects.new("Rig", data)
    context.collection.objects.link(arm)
    arm.location = point
    arm.show_in_front = True  # The desktop shows the bones through the mesh, as VR does.
    context.view_layer.update()
    use_rig(context, arm)
    return arm


def _rig_named(name):
    obj = bpy.data.objects.get(name) if name else None
    return obj if obj is not None and obj.type == 'ARMATURE' else None


# --- Bones tool handlers -------------------------------------------------------------

class PlaceBone:
    """Trigger in empty space: the next bone, from the last joint (starting a chain: from the press)
    to the tip, its tail following the tip until release. Without a rig, the first bone makes one."""

    def __init__(self, context, settings, arm, tip, scale):
        self.mirror = settings.rig_mirror
        self.min = MIN_LENGTH * scale
        self.rig_name = arm.name if arm is not None else ""
        a = anchor(arm)
        self.starting = a is None
        if a is None:
            self.head = grid.snap_if(settings, tip)  # world
            self.parent, self.connect = None, False
        else:
            self.head = arm.matrix_world @ a[0] if arm is not None else a[0]
            self.parent, self.connect = a[1], a[2]
        self.bone = None
        self.changed = False
        self._reach(context, grid.snap_if(settings, tip))

    def _reach(self, context, tip):
        arm = _rig_named(self.rig_name)
        if self.bone is None:
            if (tip - self.head).length < self.min:
                return
            if arm is None:
                arm = new_rig(context, self.head)
                self.rig_name = arm.name
            if arm.mode != 'EDIT':
                return
            mwi = arm.matrix_world.inverted_safe()
            self.bone = add_bone(arm, mwi @ self.head, mwi @ tip, self.parent, self.connect, self.mirror)
            set_active_bone(arm, self.bone)
            self.changed = True
        elif arm is not None and arm.mode == 'EDIT':
            move_joint(arm, self.bone, 'TAIL', arm.matrix_world.inverted_safe() @ tip, self.mirror)

    def update(self, context, pose, tip_pose, value):
        self._reach(context, grid.snap_if(context.scene.vr_sculpt, tip_pose.translation))

    def finish(self, context):
        arm = _rig_named(self.rig_name)
        if self.bone is not None and arm is not None and self.bone in arm.data.edit_bones:
            _state["anchor"] = (arm.name, arm.data.edit_bones[self.bone].tail.copy(), self.bone, True)
            _state["made"].append((arm.name, self.bone))
        elif self.starting:
            # A click in empty space: the chain's root joint, waiting for the next press.
            point = arm.matrix_world.inverted_safe() @ self.head if arm is not None else self.head
            _state["anchor"] = (self.rig_name, point, None, False)


class JointPress:
    """Trigger on a joint: drag to move it (with the bones connected there); a click carries the chain
    on from it instead (from a tail: a child bone, i.e. a branch; from a root's head: a new root)."""

    def __init__(self, context, settings, arm, name, end, joint, tip_pose):
        from . import interactions
        self.rig_name, self.name, self.end = arm.name, name, end
        self.mirror = settings.rig_mirror
        self.joint = joint.copy()
        self.offset = joint - tip_pose.translation
        self.gate = interactions.DragGate(context, tip_pose)
        set_active_bone(arm, name)
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.offset = self.joint - base.translation  # From where the hand was when the move began.
        arm = _rig_named(self.rig_name)
        if arm is None or arm.mode != 'EDIT':
            return
        point = grid.snap_if(context.scene.vr_sculpt, tip_pose.translation + self.offset)
        move_joint(arm, self.name, self.end, arm.matrix_world.inverted_safe() @ point, self.mirror)
        self.changed = True

    def finish(self, context):
        arm = _rig_named(self.rig_name)
        if self.changed or arm is None or arm.mode != 'EDIT':
            return
        bone = arm.data.edit_bones.get(self.name)
        if bone is None:
            return
        if self.end == 'TAIL':
            _state["anchor"] = (arm.name, bone.tail.copy(), bone.name, True)
        else:
            _state["anchor"] = (arm.name, bone.head.copy(), None, False)


class SelectBone:
    """Trigger on a bone (Bones tool), or near one and away from the surface (Weights tool): it becomes
    the active bone (the one Delete deletes and Weights paints)."""

    def __init__(self, context, arm, name):
        set_active_bone(arm, name)
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


class DeleteBone:
    """Left trigger on a bone or joint: delete that bone (with Mirror X, its counterpart too)."""

    def __init__(self, context, settings, arm, name):
        self.deleted = delete_bone(arm, name, settings.rig_mirror)
        self.changed = bool(self.deleted)

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


# --- bind --------------------------------------------------------------------------

def bind(context):
    """Bind the mesh to the rig: an Armature modifier and automatic weights. Returns a message."""
    from . import menus
    arm, obj = target(context), mesh(context)
    if arm is None or obj is None:
        return _say("Pick a rig and a mesh to bind")
    menus.set_object_mode(context, arm, 'OBJECT')
    menus.set_object_mode(context, obj, 'OBJECT')
    deform = [b.name for b in arm.data.bones if b.use_deform]
    if not deform:
        sync_mode(context)
        return _say(f"{arm.name} has no bones yet")
    for m in [m for m in obj.modifiers if m.type == 'ARMATURE' and m.object == arm]:
        obj.modifiers.remove(m)  # parent_set adds its own.
    primitives.make_active(context, arm)
    obj.select_set(True)
    override = dict(active_object=arm, object=arm, selected_objects=[obj, arm],
                    selected_editable_objects=[obj, arm])
    window, view = menus._desktop_view(context)
    if view is not None:
        override.update(window=window, area=view[0], region=view[1])
    t0 = time.perf_counter()
    try:
        with context.temp_override(**override):
            bpy.ops.object.parent_set(type='ARMATURE_AUTO')
    except RuntimeError as ex:
        primitives.make_active(context, arm)
        sync_mode(context)
        return _say(f"Bind failed: {ex}")
    ms = (time.perf_counter() - t0) * 1000.0
    primitives.make_active(context, arm)
    context.scene.vr_sculpt.rig_mesh = obj
    forget_layers()
    weighted = _weighted_groups(obj)
    empty = [n for n in deform if n not in weighted]
    sync_mode(context)
    if empty:
        return _say(f"Bound {obj.name} in {ms:.0f} ms; no weights for {len(empty)} bone(s): {', '.join(empty[:3])}")
    return _say(f"Bound {obj.name} to {len(deform)} bone(s) in {ms:.0f} ms")


def _weighted_groups(obj):
    """Names of obj's vertex groups that have some weight on some vertex."""
    used = set()
    for v in obj.data.vertices:
        for e in v.groups:
            if e.weight > EPS:
                used.add(e.group)
    return {g.name for g in obj.vertex_groups if g.index in used}


def normalize(context):
    """Make each vertex's deform weights add up to 1 (Blender's Normalize All, bone groups only)."""
    obj = mesh(context)
    if obj is None or not obj.vertex_groups:
        return False
    with context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.vertex_group_normalize_all(group_select_mode='BONE_DEFORM', lock_active=False)
    forget_layers()
    return True


# --- weights -----------------------------------------------------------------------

def read_weights(obj, name):
    """float32 weights of vertex group `name` for every vertex (0 where it isn't in the group)."""
    me = obj.data
    w = np.zeros(len(me.vertices), dtype=np.float32)
    g = obj.vertex_groups.get(name)
    if g is None:
        return w
    gi = g.index
    for v in me.vertices:
        for e in v.groups:
            if e.group == gi:
                w[v.index] = e.weight
                break
    return w


# (mesh object name, group name) -> float32 weights, kept up to date by the brush.
_layers = {}
_version = [0]  # bumped on every weight change, so the heat map knows to recolor


def layer(obj, name):
    key = (obj.name, name)
    w = _layers.get(key)
    if w is None or len(w) != len(obj.data.vertices):
        w = _layers[key] = read_weights(obj, name)
    return w


def forget_layers():
    _layers.clear()
    _version[0] += 1


def world_positions(context, obj):
    """((n, 3) float64 world positions, (n, 3) world normals) of obj's vertices: deformed by its
    modifiers when they keep the vertex count (a posed rig), else the mesh's own."""
    me = obj.data
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    no = np.empty(n * 3, dtype=np.float32)
    ev = obj.evaluated_get(context.evaluated_depsgraph_get())
    em = ev.to_mesh()
    try:
        src = em if em is not None and len(em.vertices) == n else me
        src.attributes["position"].data.foreach_get("vector", co)
        src.vertex_normals.foreach_get("vector", no)
    finally:
        ev.to_mesh_clear()
    mw = np.array(obj.matrix_world, dtype=np.float64)
    co = co.reshape(n, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
    no = no.reshape(n, 3).astype(np.float64) @ np.linalg.inv(mw[:3, :3])
    no /= np.maximum(np.linalg.norm(no, axis=1), 1e-12)[:, None]
    return co, no


class Geometry:
    """A mesh's world-space vertex positions at the start of a stroke, with brushes.py's hash grid and
    edge adjacency (it has the attributes brushes._adjacency reads)."""

    def __init__(self, context, obj):
        self.mesh = obj.data
        self.co, self.normals = world_positions(context, obj)
        self.num_edges = len(self.mesh.edges)
        self._grid = None

    @property
    def edges(self):
        return brushes._edges(self)

    def select(self, center, radius):
        """Indices and falloff of the vertices within radius of center (world)."""
        center = np.asarray(center, dtype=np.float64)
        if len(self.co) >= brushes.GRID_MIN_VERTS:
            if self._grid is None or self._grid.stale(radius):
                self._grid = brushes._Grid(self.co, radius)
            cand = self._grid.candidates(center, radius)
        else:
            cand = np.arange(len(self.co))
        d = self.co[cand] - center
        dist2 = np.einsum("ij,ij->i", d, d)
        keep = dist2 < radius * radius
        return cand[keep], brushes.falloff(np.sqrt(dist2[keep]), radius)

    def nearest(self, center, radius):
        """Distance from center to the nearest vertex within radius, or infinity."""
        idx, _f = self.select(center, radius)
        if not len(idx):
            return float("inf")
        d = self.co[idx] - np.asarray(center, dtype=np.float64)
        return float(np.sqrt(np.einsum("ij,ij->i", d, d).min()))

    def neighbor_mean(self, w, idx):
        """Mean weight of each vertex's edge neighbors (its own weight if it has none)."""
        indptr, nbr = brushes._adjacency(self)
        starts = indptr[idx]
        counts = indptr[idx + 1] - starts
        mean = w[idx].astype(np.float64)
        has = counts > 0
        if not has.any():
            return mean
        starts, counts = starts[has], counts[has]
        offsets = np.cumsum(counts) - counts
        flat = np.repeat(starts - offsets, counts) + np.arange(counts.sum())
        mean[has] = np.add.reduceat(w[nbr[flat]].astype(np.float64), offsets) / counts
        return mean


_history = []  # [{"mesh": name, "groups": {group name: {vertex: weight before}}}], latest last


def flip(brush):
    return {'ADD': 'SUBTRACT', 'SUBTRACT': 'ADD'}.get(brush, brush)


class PaintWeights:
    """Trigger near the mesh (Weights tool): paint the active bone's vertex group while held. With
    Mirror X, the mirrored side of the mesh gets the same on the .L / .R counterpart's group (a bone
    on the mirror plane: its own group)."""

    def __init__(self, context, settings, obj, bone, left, scale, geometry=None):
        self.obj_name, self.bone = obj.name, bone
        self.brush = flip(settings.weight_brush) if left else settings.weight_brush
        self.radius = settings.weight_radius * scale
        self.strength = settings.weight_strength
        self.mirror = bpy.utils.flip_name(bone) if settings.weight_mirror else None
        for name in {bone, self.mirror} - {None}:
            if obj.vertex_groups.get(name) is None:
                obj.vertex_groups.new(name=name)
        obj.vertex_groups.active_index = obj.vertex_groups[bone].index
        self.geo = geometry or Geometry(context, obj)
        self.mw = obj.matrix_world.copy()
        self.mwi = self.mw.inverted_safe()
        self.before = {}
        self.t = None
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        now = time.monotonic()
        dt = 1.0 / 90.0 if self.t is None else min(now - self.t, 0.05)
        self.t = now
        self.dab(tip_pose.translation, max(value, 0.05), dt)

    def dab(self, center, pressure, dt):
        """One brush step at center (world) for dt seconds. Returns how many weights changed."""
        obj = bpy.data.objects.get(self.obj_name)
        if obj is None or obj.type != 'MESH' or len(obj.data.vertices) != len(self.geo.co):
            return 0
        n = self._apply(obj, self.bone, Vector(center), pressure, dt)
        if self.mirror is not None:
            local = self.mwi @ Vector(center)
            n += self._apply(obj, self.mirror, self.mw @ Vector((-local.x, local.y, local.z)), pressure, dt)
        if n:
            obj.data.update_tag()
            _version[0] += 1
            self.changed = True
        return n

    def _apply(self, obj, name, center, pressure, dt):
        idx, f = self.geo.select(center, self.radius)
        if not len(idx):
            return 0
        w = layer(obj, name)
        old = w[idx].astype(np.float64)
        amount = f * pressure * self.strength
        if self.brush == 'SMOOTH':
            new = old + (self.geo.neighbor_mean(w, idx) - old) * np.minimum(amount * SMOOTH_RATE * dt, 1.0)
        else:
            sign = 1.0 if self.brush == 'ADD' else -1.0
            new = old + sign * amount * WEIGHT_RATE * dt
        new = np.clip(new, 0.0, 1.0)
        moved = np.abs(new - old) > 1e-6
        idx, old, new = idx[moved], old[moved], new[moved]
        if not len(idx):
            return 0
        before = self.before.setdefault(name, {})
        for i, o in zip(idx.tolist(), old.tolist()):
            before.setdefault(i, o)
        w[idx] = new
        _write(obj.vertex_groups[name], idx.tolist(), new.tolist(), old.tolist())
        return len(idx)

    def finish(self, context):
        if self.changed:
            _history.append({"mesh": self.obj_name, "groups": self.before})
            del _history[:-HISTORY]


def _write(group, idx, new, old):
    for i, v, o in zip(idx, new, old):
        if v > EPS:
            group.add([i], v, 'REPLACE')
        elif o > EPS:
            group.remove([i])


def undo_stroke(context):
    """Put back the weights the last VR weight stroke changed (one regular undo step)."""
    while _history:
        entry = _history.pop()
        obj = bpy.data.objects.get(entry["mesh"])
        if obj is None or obj.type != 'MESH':
            continue
        for name, before in entry["groups"].items():
            g = obj.vertex_groups.get(name)
            if g is None:
                continue
            w = layer(obj, name)
            idx = list(before)
            old = [float(w[i]) if i < len(w) else 0.0 for i in idx]
            new = [before[i] for i in idx]
            _write(g, idx, new, old)
            for i, v in zip(idx, new):
                if i < len(w):
                    w[i] = v
        obj.data.update_tag()
        _version[0] += 1
        try:
            bpy.ops.ed.undo_push(message="Undo VR weight stroke")
        except RuntimeError:
            pass  # No undo stack (background); the weights are back either way.
        return True
    return False


# --- picking -----------------------------------------------------------------------

def _weights_target(context):
    """(rig, mesh) the Weights tool paints, or None: a rig in Object Mode deforming the mesh."""
    arm, obj = target(context), mesh(context)
    if arm is None or obj is None or not bound(obj, arm):
        return None
    return arm, obj


def pick(context, settings, left, tip_pose, scale):
    """Trigger press in Rig mode: the handler for what the tip is at, or None."""
    tip = tip_pose.translation
    arm = target(context)
    if settings.rig_tool == 'WEIGHTS':
        found = _weights_target(context)
        if found is None:
            return None
        arm, obj = found
        if arm.mode != 'OBJECT':
            sync_mode(context)
        geo = Geometry(context, obj)
        hit = nearest_bone(arm, tip, scale)
        if hit is not None and hit[1] < geo.nearest(tip, settings.weight_radius * scale):
            return SelectBone(context, arm, hit[0])
        bone = active_bone(arm)
        if bone is None:
            return None
        return PaintWeights(context, settings, obj, bone, left, scale, geo)
    if arm is not None and arm.mode != 'EDIT':
        sync_mode(context)
        if arm.mode != 'EDIT':
            return None  # Hidden, say.
    joint = nearest_joint(arm, tip, scale)
    bone = nearest_bone(arm, tip, scale) if joint is None else None
    if left:
        name = joint[0] if joint is not None else bone[0] if bone is not None else None
        if name is None:
            end_chain()
            return None
        return DeleteBone(context, settings, arm, name)
    if joint is not None:
        return JointPress(context, settings, arm, joint[0], joint[1], joint[2], tip_pose)
    if bone is not None:
        return SelectBone(context, arm, bone[0])
    return PlaceBone(context, settings, arm, tip, scale)


def engaged(context, settings, tip, scale):
    """Whether the tip is at work on the rig or the mesh (so a desktop screen behind doesn't take the trigger)."""
    arm = target(context)
    if settings.rig_tool == 'BONES':
        return nearest_joint(arm, tip, scale) is not None or nearest_bone(arm, tip, scale) is not None
    found = _weights_target(context)
    if found is None:
        return False
    return nearest_bone(found[0], tip, scale) is not None or primitives.touching(
        context, tip, settings.weight_radius * scale) is found[1]


# --- caches --------------------------------------------------------------------------

# The heat map: world positions (pushed out along the normals) and triangles of the mesh, computed in
# the app timer (tick); the batch is rebuilt when the weights change.
_heat = {"key": None, "co": None, "tris": None, "batch": None, "batch_key": None, "built": 0.0}


def forget_caches():
    """Undo / redo / load: the weights, positions and batches are of data that's gone."""
    forget_layers()
    _history.clear()
    _heat.update(key=None, co=None, tris=None, batch=None, batch_key=None)
    _hover.update(time=0.0, key=None, value=None)


def heat_wanted(context):
    settings = context.scene.vr_sculpt
    return settings.mode == 'RIG' and settings.rig_tool == 'WEIGHTS' and settings.weight_show


def _pose_signature(obj, arm):
    """Changes when what the heat map shows the mesh at changes: its transform, its vertex count, the pose."""
    sig = [obj.name, len(obj.data.vertices), tuple(round(v, 5) for row in obj.matrix_world for v in row)]
    if arm is not None:
        sig.append(round(sum(sum(v for row in pb.matrix for v in row) for pb in arm.pose.bones), 5))
        sig.append(tuple(round(v, 5) for row in arm.matrix_world for v in row))
    return tuple(sig)


def tick(context):
    """App timer (live.py): keep the heat map's geometry and the active bone's weights loaded."""
    from . import operators
    if not heat_wanted(context) or operators.active_hands:
        return
    found = _weights_target(context)
    if found is None:
        return
    arm, obj = found
    key = _pose_signature(obj, arm)
    if key != _heat["key"]:
        co, no = world_positions(context, obj)
        me = obj.data
        tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tris)
        size = float(np.linalg.norm(co.max(axis=0) - co.min(axis=0))) if len(co) else 0.0
        _heat.update(key=key, co=(co + no * (HEAT_LIFT * size)).astype(np.float32), tris=tris.reshape(-1, 3),
                     batch=None)
    bone = active_bone(arm)
    if bone is not None:
        layer(obj, bone)


_RAMP_X = (0.0, 0.25, 0.5, 0.75, 1.0)
_RAMP = ((0.0, 0.0, 1.0), (0.0, 1.0, 1.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (1.0, 0.0, 0.0))


def heat_colors(w, alpha=HEAT_ALPHA):
    """(n, 4) float32 colors of weights w: Blender's weight ramp, blue (0) to red (1)."""
    out = np.empty((len(w), 4), dtype=np.float32)
    for c in range(3):
        out[:, c] = np.interp(w, _RAMP_X, [r[c] for r in _RAMP])
    out[:, 3] = alpha
    return out


_shader = {}


def heat_shader():
    """SMOOTH_COLOR, with each vertex pulled toward the eye (in view space) by HEAT_PULL of its
    distance, so the heat map wins over the mesh it lies on."""
    shader = _shader.get("heat")
    if shader is None:
        iface = gpu.types.GPUStageInterfaceInfo("vrs_heat_iface")
        iface.smooth('VEC4', "finalColor")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "modelViewMatrix")
        info.push_constant('MAT4', "projectionMatrix")
        info.push_constant('FLOAT', "pull")
        info.vertex_in(0, 'VEC3', "pos")
        info.vertex_in(1, 'VEC4', "color")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "fragColor")
        info.vertex_source(
            "void main() {"
            "  vec4 v = modelViewMatrix * vec4(pos, 1.0);"
            "  v.xyz *= 1.0 - pull;"
            "  gl_Position = projectionMatrix * v;"
            "  finalColor = color;"
            "}")
        info.fragment_source("void main() { fragColor = finalColor; }")
        shader = _shader["heat"] = gpu.shader.create_from_info(info)
        _shader["iface"] = iface  # The interface has to outlive the shader's creation info.
    return shader


def _draw_heat(context, obj, bone):
    co, tris = _heat["co"], _heat["tris"]
    w = _layers.get((obj.name, bone))
    if co is None or w is None or len(w) != len(co) or _heat["key"][0] != obj.name:
        return
    shader = heat_shader()
    key = (id(co), bone, _version[0])
    now = time.monotonic()
    if _heat["batch"] is None or (_heat["batch_key"] != key and now - _heat["built"] >= 1.0 / HEAT_FPS):
        _heat["batch"] = batch_for_shader(shader, 'TRIS', {"pos": co, "color": heat_colors(w)}, indices=tris)
        _heat.update(batch_key=key, built=now)
    shader.bind()
    shader.uniform_float("modelViewMatrix", gpu.matrix.get_model_view_matrix())
    shader.uniform_float("projectionMatrix", gpu.matrix.get_projection_matrix())
    shader.uniform_float("pull", HEAT_PULL)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    _heat["batch"].draw(shader)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


# --- drawing -----------------------------------------------------------------------

_hover = {"time": 0.0, "key": None, "value": None}
HOVER_SECONDS = 0.006  # both eyes of one headset frame share the hover tests


def hover(context, settings, arm, tips, scale):
    """{bone name: color} and the highlighted joint (name, end) for what each tip would act on."""
    now = time.monotonic()
    key = (arm.name, arm.mode, settings.rig_tool, tuple(tuple(t) for t in tips))
    if key == _hover["key"] and now - _hover["time"] < HOVER_SECONDS:
        return _hover["value"]
    bones, joint = {}, None
    if settings.rig_tool == 'BONES':
        for i, tip in enumerate(tips):
            j = nearest_joint(arm, tip, scale)
            b = (j[0],) if j is not None else nearest_bone(arm, tip, scale)
            if i == 0 and b is not None:
                bones[b[0]] = DELETE_COLOR
            elif i == 1:
                if j is not None:
                    joint = (j[0], j[1])
                elif b is not None:
                    bones.setdefault(b[0], pose_mode.HOVER_COLOR)
    else:
        b = nearest_bone(arm, tips[1], scale)
        co = _heat["co"]
        if b is not None and co is not None:
            d = co - np.asarray(tips[1], dtype=np.float32)
            if b[1] < float(np.sqrt(np.einsum("ij,ij->i", d, d).min())):
                bones[b[0]] = pose_mode.HOVER_COLOR
        elif b is not None:
            bones[b[0]] = pose_mode.HOVER_COLOR
    value = (bones, joint)
    _hover.update(time=now, key=key, value=value)
    return value


def _octahedron(m, tail):
    head = m.translation
    length = (tail - head).length
    if length < 1e-7:
        return []
    x = m.col[0].xyz.normalized() * (0.1 * length)
    z = m.col[2].xyz.normalized() * (0.1 * length)
    mid = head + (tail - head) * 0.1
    ring = [mid + x, mid + z, mid - x, mid - z]
    coords = []
    for i in range(4):
        coords += [head, ring[i], ring[i], tail, ring[i], ring[(i + 1) % 4]]
    return coords


def _cross(p, r):
    return [p + Vector((r, 0, 0)), p - Vector((r, 0, 0)), p + Vector((0, r, 0)), p - Vector((0, r, 0)),
            p + Vector((0, 0, r)), p - Vector((0, 0, r))]


def draw(context, tips, scale):
    """Rig mode in the headset: the heat map (Weights), the bones, joints and the rubber band."""
    settings = context.scene.vr_sculpt
    arm = target(context)
    if settings.rig_tool == 'WEIGHTS':
        found = _weights_target(context)
        bone = active_bone(found[0]) if found is not None else None
        if found is not None and bone is not None and settings.weight_show:
            _draw_heat(context, found[1], bone)
    groups = {}
    if arm is not None and arm.visible_get():
        bones, joint = hover(context, settings, arm, tips, scale)
        active = active_bone(arm)
        for name, m, tail in _frames(arm):
            color = bones.get(name) or (pose_mode.ACTIVE_COLOR if name == active else pose_mode.BONE_COLOR)
            groups.setdefault(color, []).extend(_octahedron(m, tail))
        for name, end, p in joints(arm):
            hot = joint == (name, end)
            groups.setdefault(pose_mode.HOVER_COLOR if hot else (1.0, 1.0, 1.0, 0.7), []).extend(
                _cross(p, JOINT_SIZE * scale * (1.6 if hot else 1.0)))
    if settings.rig_tool == 'BONES':
        start = anchor_world(arm)
        if start is not None:
            groups.setdefault((*BONES_COLOR, 0.9), []).extend([start, tips[1]] + _cross(start, JOINT_SIZE * scale * 1.4))
            if settings.rig_mirror and arm is not None:
                mw, mwi = arm.matrix_world, arm.matrix_world.inverted_safe()
                a, b = mwi @ start, mwi @ tips[1]
                groups.setdefault((*BONES_COLOR, 0.4), []).extend([mw @ _mirrored(a), mw @ _mirrored(b)])
    if not groups:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for color, coords in groups.items():
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", color if len(color) == 4 else (*color, 1.0))
        batch.draw(shader)
    gpu.state.blend_set('NONE')
