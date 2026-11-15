"""Pose mode: pose an armature's bones with the controllers.

Trigger on a bone (FK): the bone turns about its head so the grabbed point follows the tip, and
twisting the controller rolls it. A free bone (no parent, or not connected to it) grabbed near
its head moves with the controller instead. IK: the grabbed bone follows the controller rigidly
and up to pose_chain parent bones bend so its head gets there (CCD solved here, so it works on
any rig without adding constraints). Without an armature in Pose Mode, the trigger moves the
objects it touches, so props can be puppeteered too. The off-hand trigger away from the posed
rig's bones (SWITCH_GAP), or with none posed, on another character (its mesh or bones) poses that
rig instead (rig_at).

The same Follow class drives puppet bindings (puppet.py): a binding is a grab that never ends,
driven by a controller's grip or the headset instead of the trigger.

Writes go through Bone.convert_local_to_pose(invert=True), which turns a wanted armature-space
matrix into the bone's local (basis) matrix given its parent's, so a whole IK chain is written
in one pass without waiting for Blender to re-evaluate the pose.

Limits (limited_basis): every write keeps the bone's rotation and location locks and its Limit
Rotation / Limit Location constraints (Local Space, full influence: the ones that act on the
bone's own channels). IK also keeps the bones' IK limits and IK locks (Bone > Inverse Kinematics),
like Blender's own IK; FK doesn't, like Blender. In IK they're applied inside every CCD step, so
the rest of the chain makes up for a joint that can't bend further. Other constraints still act
on top, as Blender evaluates them.

Production rigs (Rigify): only the bones the animator sees are drawn and grabbed (bone
collections, Bone.hide and PoseBone.hide), as their custom shapes when they have one. A chain
bends visible bones only, passing through hidden helper bones that just follow their parent (no
constraints but scale ones), and stops at one that is driven. A bone steering the rig's own IK
(limbs.ik_handles: hand_ik, foot_ik, poles) is moved, never solved for. Grabbing a control of a
switched limb's inactive side switches the limb first (limbs.py); until then those controls are
drawn where the switch will put them, on the limb.

Curl: with pose_curl on, pulling or pushing a grabbed bone along itself also scales it along its
length (a fraction of the motion, past a dead zone), for bones whose length scale a driver reads:
Rigify's finger, toe, ear and tail masters curl their chain by it. Such a bone always turns (a
grab near its head doesn't move it), and a long one counts CURL_REACH of hand travel as its length.

Turning a swung bone with the wrist: the grabbed point follows the hand, and the controller's turn
is added on top as far as it's more than the hand's move explains (turning in place turns the bone;
swinging the arm, which turns the wrist with it, doesn't count twice). Twist about the grab always
counts.

Rig quirks handled here: a bone whose own Copy Rotation (Local Space, Before / After Original:
Rigify's tail segments) adds a turn gets that turn taken out of what's written, so it still lands
under the hand. A bone drawn on another bone that it moves through constraints (Rigify's tail
master, drawn at the tail's tip) is turned by a small solve (Drive) so its drawn shape follows the
hand. A limb's settings gear (Rigify's thigh_parent: locked, nothing follows it) turns the limb's
root control instead (thigh_ik / thigh_fk), following the controller's turn.
"""

import math
import re
import time

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Euler, Matrix, Quaternion, Vector

from . import anim, limbs, primitives

REACH = 0.02  # hand-space meters around a bone that still grab it
SWITCH_GAP = 0.1  # hand-space meters from the posed rig's bones past which the off hand picks another rig
MOVE_ZONE = 0.3  # share of a free bone's length, from its head, that moves it instead of turning it
CCD_ITERATIONS = 12
CCD_TOLERANCE = 1e-5
# Curl (pose_curl): pulling / pushing a grabbed bone along itself scales its length by CURL_RATE
# per bone length moved, past CURL_DEAD_ZONE bone lengths (so a swing's wobble doesn't curl).
# Rigify's finger master turns each joint by (1 - scale) * 180 deg.
CURL_RATE = 0.6
CURL_DEAD_ZONE = 0.1
CURL_MIN, CURL_MAX = 0.05, 1.5
CURL_REACH = 0.1  # hand-space meters of travel that count as a bone length, for longer bones
# Turning the controller (SWING): its turn per update, less TURN_DEAD (tracking jitter, so a held
# grab doesn't wander), counts in full up to TURN_REACH (hand-space meters) from the bone's head
# to the grab; past it, falling off with the square, as a wrist's tremor would swing the far end.
TURN_DEAD = math.radians(0.1)
TURN_REACH = 0.25
# Drive (a bone drawn on another bone it moves): a damped Gauss-Newton step per headset frame on
# the drawn point, its Jacobian measured at the grab (DRIVE_EPS radians per axis) and kept current
# from what each frame's evaluation shows (Broyden).
DRIVE_EPS = 0.05
DRIVE_GAIN = 0.7
DRIVE_MAX_STEP = 0.25  # radians per frame
DRIVE_LEARN = 0.002  # radians: the smallest step the Jacobian is refined from

BONE_COLOR = (0.75, 0.78, 0.85, 0.45)
SELECTED_COLOR = (0.35, 0.6, 1.0, 0.85)
ACTIVE_COLOR = (0.55, 0.85, 1.0, 1.0)
HOVER_COLOR = (1.0, 0.85, 0.3, 1.0)

# Object the panel's Rig picker or Pose mode put in Pose Mode, and what was active before it.
_state = {"previous": None}


# --- which rig -------------------------------------------------------------

def find_rig(obj):
    """The armature that poses obj: obj itself, its Armature modifier's rig, or its armature parent."""
    if obj is None:
        return None
    if obj.type == 'ARMATURE':
        return obj
    for m in getattr(obj, "modifiers", ()):
        if m.type == 'ARMATURE' and m.object is not None:
            return m.object
    if obj.parent is not None and obj.parent.type == 'ARMATURE':
        return obj.parent
    return None


def rig(context):
    """The armature in Pose Mode (the active object), or None."""
    obj = context.view_layer.objects.active
    return obj if anim.posing(obj) else None


def is_metarig(obj):
    """Whether the armature is a Rigify metarig (a template to generate a rig from, not posed). Read
    from Rigify's own properties, which are there even when Rigify isn't enabled."""
    get = getattr(obj.data, "bl_system_properties_get", None)
    props = get() if get is not None else None
    if props is not None and "rigify_target_rig" in props:
        return True
    if obj.data.get("rig_id") is not None:
        return False  # A generated rig (its bones may keep the metarig's rigify_type).
    for pb in obj.pose.bones:
        get = getattr(pb, "bl_system_properties_get", None)
        props = get() if get is not None else None
        if props is not None and props.get("rigify_type"):
            return True
    return False


def rigs(context):
    """The armatures the rig picker offers (not Rigify metarigs, unless one is being posed)."""
    current = rig(context)
    return [o for o in context.scene.objects
            if o.type == 'ARMATURE' and (o == current or not is_metarig(o))]


def enter(context):
    """Pose mode: put the rig of the active object in Pose Mode. Returns it, or None (objects only)."""
    obj = find_rig(context.view_layer.objects.active)
    if obj is not None:
        use_rig(context, obj)
    return obj


def use_rig(context, obj):
    from . import menus
    active = context.view_layer.objects.active
    if active is not None and active is not obj:
        if _state["previous"] is None or not anim.posing(active):
            _state["previous"] = active.name  # From one rig to the next, leaving still goes back to the first.
        menus.set_object_mode(context, active, 'OBJECT')
    if obj.visible_get():
        primitives.make_active(context, obj)
        menus.set_object_mode(context, obj, 'POSE')


def rig_at(context, tip, scale):
    """The rig other than the posed one that the tip is on, or None: the rig of the mesh it touches,
    else one whose bone is within reach. None too while the tip is within SWITCH_GAP of the posed rig's
    bones, or touches its mesh, so a near miss on a bone doesn't swap characters."""
    from . import interactions
    current = rig(context)
    tip = Vector(tip)
    others = [o for o in rigs(context) if o is not current and o.visible_get() and not o.hide_select]
    found = find_rig(primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale))
    if found is not None and found == current:
        return None
    if found not in others:
        found, best = None, REACH * scale
        for o in others:
            for _pb, a, b in segments(o):
                d = _closest(tip, a, b)[0]
                if d < best:
                    found, best = o, d
    if found is None:
        return None
    if current is not None:
        gap = SWITCH_GAP * scale
        if any(_closest(tip, a, b)[0] < gap for _pb, a, b in segments(current)):
            return None
    return found


class SelectRig:
    """Off-hand trigger on another character in Pose mode: pose its rig."""

    def __init__(self, context, obj):
        use_rig(context, obj)
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


def leave(context):
    """Back to Object Mode; the object that was active before Pose mode is active again."""
    from . import menus
    obj = rig(context)
    if obj is not None:
        menus.set_object_mode(context, obj, 'OBJECT')
    previous = bpy.data.objects.get(_state["previous"] or "")
    _state["previous"] = None
    if obj is not None and previous is not None and previous.name in context.view_layer.objects:
        primitives.make_active(context, previous)


def choose_rig(context, name):
    """Rig picker: pose this armature, or none ("") to move objects."""
    obj = bpy.data.objects.get(name)
    if obj is None:
        leave(context)
    else:
        use_rig(context, obj)


# --- bones -----------------------------------------------------------------

def visible(pb):
    """Whether the bone shows in Pose Mode: not hidden (Bone.hide, or PoseBone.hide in 5.x, which
    Rigify drives) and in a visible bone collection (or none)."""
    bone = pb.bone
    if bone.hide or getattr(pb, "hide", False):
        return False
    colls = bone.collections
    return not len(colls) or any(getattr(c, "is_visible_effectively", c.is_visible) for c in colls)


def segments(obj):
    """[(pose bone, world head, world tail)] for the rig's visible bones (Physics picks springs by
    them)."""
    mw = obj.matrix_world
    return [(pb, mw @ pb.head, mw @ pb.tail) for pb in obj.pose.bones if visible(pb)]


def _closest(p, a, b):
    """(distance, fraction along a-b) of the point on segment a-b closest to p."""
    ab = b - a
    n = ab.length_squared
    f = 0.0 if n < 1e-12 else min(max((p - a).dot(ab) / n, 0.0), 1.0)
    return (p - (a + ab * f)).length, f


# --- what the rig shows: bones and custom shapes ---------------------------------

_shapes = {}  # mesh name -> (vertex count, edge count, (2E, 4) local line coords or None)


def _shape_lines(ob):
    """A custom shape's edges as (2E, 4) homogeneous local coords, or None (not a mesh, no edges)."""
    if ob is None or ob.type != 'MESH':
        return None
    me = ob.data
    nv, ne = len(me.vertices), len(me.edges)
    hit = _shapes.get(me.name)
    if hit is not None and hit[0] == nv and hit[1] == ne:
        return hit[2]
    lines = None
    if ne:
        co = np.empty(nv * 3, np.float32)
        me.vertices.foreach_get("co", co)
        ed = np.empty(ne * 2, np.int32)
        me.edges.foreach_get("vertices", ed)
        pts = co.reshape(-1, 3)[ed]
        lines = np.hstack((pts, np.ones((len(pts), 1), np.float32)))
    _shapes[me.name] = (nv, ne, lines)
    return lines


def has_shape(obj, pb):
    """Whether the bone is drawn (and grabbed) as its custom shape."""
    return obj.data.show_bone_custom_shapes and _shape_lines(pb.custom_shape) is not None


def _shape_offset(pb):
    """The shape's placement on its bone, as Blender draws it: translation, XYZ rotation, then
    scale (times the bone's rest length with Scale to Bone Length)."""
    s = pb.bone.length if pb.use_custom_shape_bone_size else 1.0
    return Matrix.LocRotScale(pb.custom_shape_translation, Euler(pb.custom_shape_rotation_euler, 'XYZ'),
                              Vector(pb.custom_shape_scale_xyz) * s)


class Layout:
    """What a rig shows that doesn't change frame to frame: bones, the visible pose bones in order;
    for the ones drawn as custom shapes, their lines placed on the bone they follow (local: (V, 4),
    on bone index local_bone, of bone local_owner); the others are octahedra (oct_bones: their pose
    bone indices, oct_owner)."""

    def __init__(self, obj):
        pbs = obj.pose.bones
        shapes_on = obj.data.show_bone_custom_shapes
        self.signature = _signature(obj)
        self.count = len(pbs)
        self.bones = []
        index = {name: i for i, name in enumerate(pbs.keys())}
        local, local_bone, local_owner, sizes = [], [], [], []
        oct_bones, oct_owner = [], []
        for i, pb in enumerate(pbs):
            if not visible(pb):
                continue
            item = len(self.bones)
            self.bones.append(pb)
            lines = _shape_lines(pb.custom_shape) if shapes_on else None
            if lines is None:
                oct_bones.append(i)
                oct_owner.append(item)
                continue
            tx = pb.custom_shape_transform
            local.append(lines @ np.array(_shape_offset(pb), np.float32).T)
            local_bone.append(index.get(tx.name, i) if tx is not None else i)
            local_owner.append(item)
            sizes.append(len(lines))
        self.local = np.concatenate(local) if local else None
        self.local_bone = np.repeat(np.array(local_bone, np.int64), sizes) if local else None
        self.local_owner = np.repeat(np.array(local_owner, np.int64), sizes) if local else None
        self.oct_bones = np.array(oct_bones, np.int64)
        self.oct_owner = np.array(oct_owner, np.int64)
        self.index = index
        self.stand_ins = [limbs.StandIn(obj, limb) for limb in limbs.find(obj)]


def _signature(obj):
    """What decides which bones show, read cheaply: the hide flags and the collections' visibility."""
    pbs, bones = obj.pose.bones, obj.data.bones
    hidden = np.zeros(len(bones), bool)
    bones.foreach_get("hide", hidden)
    posed = np.zeros(len(pbs), bool)
    try:
        pbs.foreach_get("hide", posed)
    except (AttributeError, TypeError):
        pass  # No PoseBone.hide before 5.x.
    colls = tuple(getattr(c, "is_visible_effectively", c.is_visible) for c in obj.data.collections_all)
    return len(pbs), hidden.tobytes(), posed.tobytes(), colls, obj.data.show_bone_custom_shapes


class Geometry:
    """The rig's visible bones in world space, from a Layout and the current pose. bones: the pose
    bones; coords: (V, 3) line vertices, vowner: (V,) their bone (index into bones); a, b: (M, 3)
    pick segments (a custom shape's edges, else the bone from head to tail), owner: (M,).
    moved: {bone name: armature-space matrix} of the inactive limb controls drawn where a switch
    would put them (limbs.StandIn)."""

    def __init__(self, obj, layout):
        pbs = obj.pose.bones
        n = len(pbs)
        mats = np.empty(n * 16, np.float32)
        pbs.foreach_get("matrix", mats)
        mats = mats.reshape(n, 4, 4).transpose(0, 2, 1)  # Stored column by column.
        self.moved = limbs.stand_ins(bpy.context, obj, layout.stand_ins)
        for name, m in self.moved.items():
            mats[layout.index[name]] = np.array(m, np.float32)
        mw = np.array(obj.matrix_world, np.float32)
        world = mw @ mats
        self.bones = layout.bones
        coords, vowner, seg_a, seg_b, owner = [], [], [], [], []
        if layout.local is not None:
            c = np.einsum('vij,vj->vi', world[layout.local_bone], layout.local)[:, :3]
            coords.append(c)
            vowner.append(layout.local_owner)
            seg_a.append(c[0::2])
            seg_b.append(c[1::2])
            owner.append(layout.local_owner[0::2])
        if len(layout.oct_bones):
            tails = np.empty(n * 3, np.float32)
            pbs.foreach_get("tail", tails)
            tails = tails.reshape(n, 3)
            for name, mm in self.moved.items():
                tails[layout.index[name]] = mm @ Vector((0.0, pbs[name].bone.length, 0.0))
            m = world[layout.oct_bones]
            head = m[:, :3, 3]
            tail = tails[layout.oct_bones] @ mw[:3, :3].T + mw[:3, 3]
            length = np.linalg.norm(tail - head, axis=1)
            keep = length > 1e-7
            m, head, tail, length = m[keep], head[keep], tail[keep], length[keep, None]

            def side(col):
                v = m[:, :3, col]
                return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12) * (0.1 * length)
            x, z = side(0), side(2)
            mid = head + (tail - head) * 0.1
            ring = (mid + x, mid + z, mid - x, mid - z)
            verts = []
            for i in range(4):  # Blender's octahedral bone: 6 lines per side.
                verts += [head, ring[i], ring[i], tail, ring[i], ring[(i + 1) % 4]]
            c = np.stack(verts, axis=1).reshape(-1, 3)
            own = layout.oct_owner[keep]
            coords.append(c)
            vowner.append(np.repeat(own, len(verts)))
            seg_a.append(head)
            seg_b.append(tail)
            owner.append(own)
        empty3 = np.zeros((0, 3), np.float32)
        self.coords = np.concatenate(coords).astype(np.float32) if coords else empty3
        self.vowner = np.concatenate(vowner) if vowner else np.zeros(0, np.int64)
        self.a = np.concatenate(seg_a) if seg_a else empty3
        self.b = np.concatenate(seg_b) if seg_b else empty3
        self.owner = np.concatenate(owner) if owner else np.zeros(0, np.int64)

    def lines(self, pb):
        """(2N, 3) line coords of one bone."""
        return self.coords[self.vowner == self.bones.index(pb)]

    def nearest(self, point, reach):
        """(pose bone, distance) of the bone whose lines pass closest to point within reach, or None."""
        if not len(self.a):
            return None
        p = np.asarray(point, np.float32)
        ab = self.b - self.a
        ap = p - self.a
        t = np.clip((ap * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        d = np.linalg.norm(ap - ab * t[:, None], axis=1)
        k = int(np.argmin(d))
        if d[k] > reach:
            return None
        return self.bones[self.owner[k]], float(d[k])


_cache = {"time": 0.0, "key": None, "geometry": None, "layout_time": 0.0, "layout_key": None, "layout": None}
CACHE_SECONDS = 0.006  # both eyes of one headset frame reuse the bone shapes
LAYOUT_SECONDS = 0.25  # which bones show: rechecked (cheaply) a few times a second


def geometry(obj, fresh=False):
    """The rig's Geometry; drawing and hover reuse it within one headset frame. The Layout is kept
    until a few-times-a-second check sees the visible bones change. fresh: all rebuilt now (picks;
    also catches shape edits)."""
    now = time.monotonic()
    key = (obj.name, obj.mode)
    if not fresh and key == _cache["key"] and now - _cache["time"] < CACHE_SECONDS:
        return _cache["geometry"]
    layout = _cache["layout"]
    stale = fresh or layout is None or _cache["layout_key"] != key or layout.count != len(obj.pose.bones)
    if not stale and now - _cache["layout_time"] > LAYOUT_SECONDS:
        stale = _signature(obj) != layout.signature
        _cache["layout_time"] = now
    if stale:
        layout = Layout(obj)
        _cache.update(layout_time=now, layout_key=key, layout=layout)
    geo = Geometry(obj, layout)
    _cache.update(time=now, key=key, geometry=geo)
    return geo


def nearest_bone(context, point, scale, obj=None, fresh=True):
    """(pose bone, fraction along it) of the bone (or bone shape) within reach of point, or None.
    The fraction is where point projects onto the bone itself, wherever its shape is."""
    obj = obj or rig(context)
    if obj is None:
        return None
    geo = geometry(obj, fresh)
    hit = geo.nearest(point, REACH * scale)
    if hit is None:
        return None
    pb = hit[0]
    mw = obj.matrix_world
    m = geo.moved.get(pb.name)
    head, tail = (pb.head, pb.tail) if m is None else (m.translation, m @ Vector((0.0, pb.bone.length, 0.0)))
    return pb, _closest(Vector(point), mw @ head, mw @ tail)[1]


def select_bone(obj, pb):
    for other in obj.pose.bones:
        other.select = other == pb
    obj.data.bones.active = pb.bone


def active_bone(obj):
    bone = obj.data.bones.active
    return obj.pose.bones.get(bone.name) if bone is not None else None


def free(pb):
    """Whether the bone can move (not only turn): no parent, or not connected to it, and its
    location isn't fully locked."""
    return (pb.parent is None or not pb.bone.use_connect) and not all(pb.lock_location)


def _passable(pb):
    """Whether a hidden bone just follows its parent (no constraints acting on it but scale ones),
    so a chain can bend the visible bones above it."""
    for con in pb.constraints:
        if (getattr(con, "enabled", not con.mute) and con.influence > 1e-6 and
                con.type not in {'COPY_SCALE', 'LIMIT_SCALE', 'MAINTAIN_VOLUME'}):
            return False
    return True


def chain_parents(pb, count):
    """Up to count visible ancestors of pb an IK chain bends, nearest first. Hidden helper bones
    in between are passed through when they just follow their parent; one that is driven
    (constraints) ends the chain."""
    out = []
    p = pb.parent
    while p is not None and len(out) < count:
        if visible(p):
            out.append(p)
        elif not _passable(p):
            break
        p = p.parent
    return out


def parent_matrix(pb, above=None, above_matrix=None):
    """pb's parent's armature-space matrix once `above` (pb's parent, or an ancestor reached
    through passable bones) is at above_matrix; without it, the parent's current one."""
    parent = pb.parent
    if parent is None:
        return None
    if above is None:
        return parent.matrix
    if parent == above:
        return above_matrix
    return above_matrix @ above.matrix.inverted_safe() @ parent.matrix


# --- writing transforms ----------------------------------------------------

def basis_for(pb, matrix, parent_matrix):
    """The local (basis) matrix that puts pb at armature-space matrix, given its parent's matrix."""
    bone = pb.bone
    if pb.parent is None:
        return bone.convert_local_to_pose(matrix, bone.matrix_local, invert=True)
    return bone.convert_local_to_pose(matrix, bone.matrix_local, parent_matrix=parent_matrix,
                                      parent_matrix_local=pb.parent.bone.matrix_local, invert=True)


def rotation(pb):
    """The bone's local rotation as a quaternion, whatever its rotation mode."""
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        return pb.rotation_quaternion.copy()
    if mode == 'AXIS_ANGLE':
        angle, *axis = pb.rotation_axis_angle
        return Quaternion(axis, angle)
    return pb.rotation_euler.to_quaternion()


def set_rotation(pb, q):
    """Set the local rotation in the bone's own rotation mode, keeping its rotation locks."""
    locks = tuple(pb.lock_rotation)
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        q = q.normalized()
        q.make_compatible(pb.rotation_quaternion)  # No sign flips between frames.
        old = pb.rotation_quaternion
        new = list(q)
        lock_w = pb.lock_rotation_w if pb.lock_rotations_4d else False
        for i, locked in enumerate((lock_w,) + locks):
            if locked:
                new[i] = old[i]
        pb.rotation_quaternion = new
    elif mode == 'AXIS_ANGLE':
        axis, angle = q.to_axis_angle()
        pb.rotation_axis_angle = (angle, *axis)
    else:
        e = q.to_euler(mode, pb.rotation_euler)  # The closest euler to the current one.
        old = pb.rotation_euler
        pb.rotation_euler = [old[i] if locks[i] else e[i] for i in range(3)]


def set_location(pb, loc):
    old = pb.location
    pb.location = [old[i] if pb.lock_location[i] else loc[i] for i in range(3)]


# --- limits ------------------------------------------------------------------

Y_AXIS = Vector((0.0, 1.0, 0.0))


def _acts(con, kind):
    """Whether constraint con is a kind we pre-apply: on, full influence, Local Space (the bone's own
    channels, which is what we write)."""
    enabled = getattr(con, "enabled", not con.mute)
    return con.type == kind and enabled and con.influence > 0.999 and con.owner_space == 'LOCAL'


def _euler_order(pb, con):
    order = con.euler_order
    if order == 'AUTO':
        order = pb.rotation_mode if pb.rotation_mode not in {'QUATERNION', 'AXIS_ANGLE'} else 'XYZ'
    return order


def _clamp(v, lo, hi):
    return min(max(v, lo), hi)


def _ik_limits(pb, q):
    """q kept within pb's IK limits and locks. Like Blender's IK: twist about the bone (Y) and swing
    (X / Z, as the swing's rotation vector) are limited separately."""
    locks = (pb.lock_ik_x, pb.lock_ik_y, pb.lock_ik_z)
    uses = (pb.use_ik_limit_x, pb.use_ik_limit_y, pb.use_ik_limit_z)
    if not any(locks) and not any(uses):
        return q
    t = _twist(q, Y_AXIS)
    twist = 2.0 * math.atan2(t.y, t.w)
    twist = (twist + math.pi) % (2.0 * math.pi) - math.pi
    swing = q @ t.inverted()
    axis, angle = swing.to_axis_angle()
    values = [axis.x * angle, twist, axis.z * angle]
    for i, a in enumerate("xyz"):
        if locks[i]:
            values[i] = 0.0
        elif uses[i]:
            values[i] = _clamp(values[i], getattr(pb, "ik_min_" + a), getattr(pb, "ik_max_" + a))
    sv = Vector((values[0], 0.0, values[2]))
    swing = Quaternion(sv.normalized(), sv.length) if sv.length > 1e-9 else Quaternion()
    return swing @ Quaternion(Y_AXIS, values[1])


def _rotation_locks(pb, q):
    """q with pb's locked rotation channels kept at their current values (as set_rotation writes)."""
    locks = tuple(pb.lock_rotation)
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        lock_w = pb.lock_rotation_w if pb.lock_rotations_4d else False
        if not (lock_w or any(locks)):
            return q
        q = q.copy()
        q.make_compatible(pb.rotation_quaternion)
        old = pb.rotation_quaternion
        new = [old[i] if locked else q[i] for i, locked in enumerate((lock_w,) + locks)]
        return Quaternion(new).normalized()
    if mode == 'AXIS_ANGLE' or not any(locks):
        return q
    e = q.to_euler(mode, pb.rotation_euler)
    old = pb.rotation_euler
    for i in range(3):
        if locks[i]:
            e[i] = old[i]
    return e.to_quaternion()


def limit_rotation(pb, q, ik=False):
    """The local rotation q kept within pb's limits: IK limits (with ik), Limit Rotation constraints
    and rotation locks."""
    q = q.normalized()
    if ik:
        q = _ik_limits(pb, q)
    for con in pb.constraints:
        if _acts(con, 'LIMIT_ROTATION'):
            order = _euler_order(pb, con)
            e = q.to_euler(order)
            for i, a in enumerate("xyz"):
                if getattr(con, "use_limit_" + a):
                    e[i] = _clamp(e[i], getattr(con, "min_" + a), getattr(con, "max_" + a))
            q = e.to_quaternion()
    return _rotation_locks(pb, q)


def limit_location(pb, loc):
    """The local location loc kept within pb's Limit Location constraints and location locks."""
    loc = Vector(loc)
    for con in pb.constraints:
        if _acts(con, 'LIMIT_LOCATION'):
            for i, a in enumerate("xyz"):
                if getattr(con, "use_min_" + a):
                    loc[i] = max(loc[i], getattr(con, "min_" + a))
                if getattr(con, "use_max_" + a):
                    loc[i] = min(loc[i], getattr(con, "max_" + a))
    old = pb.location
    return Vector([old[i] if pb.lock_location[i] else loc[i] for i in range(3)])


def limited_basis(pb, basis, location=False, ik=False):
    """The basis pb actually gets for a wanted one: limited rotation, and the wanted location
    (limited) only if it moves (location), else its current one. Scale is left alone."""
    loc, rot, _scale = basis.decompose()
    rot = limit_rotation(pb, rot, ik)
    loc = limit_location(pb, loc) if location else pb.location.copy()
    return Matrix.LocRotScale(loc, rot, pb.scale)


def pose_for(pb, basis, parent_matrix):
    """The armature-space matrix a basis puts pb at, given its parent's (basis_for's inverse)."""
    bone = pb.bone
    if pb.parent is None:
        return bone.convert_local_to_pose(basis, bone.matrix_local)
    return bone.convert_local_to_pose(basis, bone.matrix_local, parent_matrix=parent_matrix,
                                      parent_matrix_local=pb.parent.bone.matrix_local)


def apply_basis(pb, basis, location, ik=False):
    b = limited_basis(pb, basis, location, ik)
    set_rotation(pb, b.to_quaternion())
    if location:
        set_location(pb, b.translation)


def mirror_bone(obj, pb, location):
    """Copy pb's local transform, mirrored across X, to its .L/.R counterpart (within its own
    limits). Returns it or None."""
    other = obj.pose.bones.get(bpy.utils.flip_name(pb.name))
    if other is None or other == pb:
        return None
    q = rotation(pb)
    set_rotation(other, limit_rotation(other, Quaternion((q.w, q.x, -q.y, -q.z))))
    if location:
        loc = pb.location
        set_location(other, limit_location(other, (-loc.x, loc.y, loc.z)))
        other.scale = [other.scale[i] if other.lock_scale[i] else pb.scale[i] for i in range(3)]
    return other


def reset(pbs):
    for pb in pbs:
        pb.location = (0.0, 0.0, 0.0)
        pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        pb.rotation_euler = (0.0, 0.0, 0.0)
        pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        pb.scale = (1.0, 1.0, 1.0)


def reset_selected(context, everything=False):
    obj = rig(context)
    if obj is None:
        return 0
    pbs = list(obj.pose.bones) if everything else anim.selected_bones(obj)
    reset(pbs)
    if pbs and not anim.auto_key_bones(context, {pb: True for pb in pbs}):
        context.view_layer.update()
    return len(pbs)


def _twist(q, axis):
    """The part of rotation q about axis (swing-twist decomposition)."""
    v = Vector((q.x, q.y, q.z))
    p = axis * v.dot(axis)
    t = Quaternion((q.w, p.x, p.y, p.z))
    return t.normalized() if t.magnitude > 1e-9 else Quaternion()


def _about(point, q):
    """4x4 rotation by q about a point."""
    return Matrix.Translation(point) @ q.to_matrix().to_4x4() @ Matrix.Translation(-point)


def solve_ccd(mats, effector, target, iterations=CCD_ITERATIONS, constrain=None):
    """Turn the chain so the effector reaches target (Cyclic Coordinate Descent), in place.

    mats: armature-space matrices, the effector's bone first, then its parents in order;
    effector: a point fixed in mats[0]'s bone space. constrain(j, matrix, mats), if given, returns
    the matrix bone j can actually take instead of matrix (its limits); the bones below it follow.
    Returns the effector's final position.
    """
    for _ in range(iterations):
        for j in range(len(mats)):
            eff = mats[0] @ effector
            head = mats[j].translation
            a, b = eff - head, target - head
            if a.length < 1e-7 or b.length < 1e-7:
                continue
            new = _about(head, a.rotation_difference(b)) @ mats[j]
            if constrain is not None:
                new = constrain(j, new, mats)
            delta = new @ mats[j].inverted_safe()
            for k in range(j + 1):
                mats[k] = delta @ mats[k]
        if (mats[0] @ effector - target).length < CCD_TOLERANCE:
            break
    return mats[0] @ effector


def chain_limits(chain):
    """A solve_ccd constrain callback for pose bones chain (effector's bone first): each step keeps
    the bone within its IK limits, Limit constraints and locks."""
    def constrain(j, matrix, mats):
        pb = chain[j]
        parent = parent_matrix(pb, chain[j + 1], mats[j + 1]) if j + 1 < len(chain) else parent_matrix(pb)
        return pose_for(pb, limited_basis(pb, basis_for(pb, matrix, parent), ik=True), parent)
    return constrain


def _angle(q):
    """A rotation's angle, 0..pi."""
    a = q.angle
    return min(a, 2.0 * math.pi - a)


def _between(u, v):
    """The shortest rotation taking direction u to v (none if either is ~zero)."""
    if u.length < 1e-9 or v.length < 1e-9:
        return Quaternion()
    return u.rotation_difference(v)


def _viewer_scale():
    ss = bpy.context.window_manager.xr_session_state
    return ss.viewer_scale if ss is not None else 1.0


def constraint_offset(pb):
    """('BEFORE' | 'AFTER', rotation) the bone's own constraints add to its local rotation, measured
    from its evaluated pose, when they're Copy Rotation / Copy Transforms in Local Space mixed
    before or after the bone's own (Rigify's tail segments copy their parent control's turn
    before theirs); None otherwise. Constant while the bones they copy don't move."""
    side = None
    for con in pb.constraints:
        if not getattr(con, "enabled", not con.mute) or con.influence < 1e-6:
            continue
        if con.type not in {'COPY_ROTATION', 'COPY_TRANSFORMS'}:
            continue
        if con.owner_space != 'LOCAL':
            return None
        mix = getattr(con, "mix_mode", 'REPLACE')
        if mix in {'BEFORE', 'ADD', 'BEFORE_FULL', 'BEFORE_SPLIT'}:
            s = 'BEFORE'
        elif mix in {'AFTER', 'AFTER_FULL', 'AFTER_SPLIT'}:
            s = 'AFTER'
        else:
            return None
        if side not in (None, s):
            return None
        side = s
    if side is None:
        return None
    local = basis_for(pb, pb.matrix, pb.parent.matrix if pb.parent else None).to_quaternion()
    own = pb.matrix_basis.to_quaternion()
    x = local @ own.inverted() if side == 'BEFORE' else own.inverted() @ local
    return (side, x) if _angle(x) > 1e-5 else None


def _uncopy(offset, basis):
    """The basis to write so that, with the copied turn (constraint_offset) added, it's basis."""
    if offset is None:
        return basis
    side, x = offset
    loc, rot, scale = basis.decompose()
    rot = x.inverted() @ rot if side == 'BEFORE' else rot @ x.inverted()
    return Matrix.LocRotScale(loc, rot, scale)


def _same4(a, b, tol=1e-7):
    return all(abs(a[i][j] - b[i][j]) < tol for i in range(4) for j in range(4))


class Drive:
    """Turn a bone so a point of its shape, drawn on another bone it moves through constraints
    (Rigify's tail master, drawn at the tail's tip), follows a target. The Jacobian of the point
    over the bone's free local axes is measured once (a few evaluations); each frame then takes a
    damped Gauss-Newton step from where the last evaluation put the point, and refines the
    Jacobian from it (Broyden). A frame whose evaluation hasn't caught up is skipped."""

    def __init__(self, obj, pb, shape_bone, point, columns, axes):
        mw = obj.matrix_world
        self.shape_bone = shape_bone
        self.bone = pb.name
        self.local = (mw @ obj.pose.bones[shape_bone].matrix).inverted_safe() @ Vector(point)
        self.q0 = rotation(pb)
        self.axes = axes
        self.J = np.array([c[:] for c in columns], np.float64).T  # (3, N)
        self.theta = np.zeros(len(axes))
        self.last = None  # (point, theta) seen last fresh frame
        self.pending = None  # bone matrix before the last write that changed the basis
        self.stale = 0
        self.lead = np.zeros(3)  # how far the controller's own turns moved the point from the hand

    def point(self, obj):
        sb = obj.pose.bones.get(self.shape_bone)
        return obj.matrix_world @ sb.matrix @ self.local if sb is not None else None

    def grab_dir(self, obj):
        """From the bone's head to the drawn point (world)."""
        p = self.point(obj)
        pb = obj.pose.bones.get(self.bone)
        return p - obj.matrix_world @ pb.head if p is not None and pb is not None else Vector()

    @staticmethod
    def make(obj, pb, point):
        """A Drive for pb grabbed at point (world) on its shape, or None when the shape is on pb
        itself or rides it rigidly (a plain swing does then)."""
        tb = pb.custom_shape_transform
        if tb is None or tb == pb or tb.id_data != obj or not has_shape(obj, pb):
            return None
        free_axes = [i for i in range(3) if pb.rotation_mode == 'AXIS_ANGLE' or not pb.lock_rotation[i]]
        if not free_axes:
            return None
        ctx = bpy.context
        mw = obj.matrix_world
        name, bone = tb.name, pb.name
        p0 = Vector(point)
        local = (mw @ tb.matrix).inverted_safe() @ p0
        m0 = (mw @ pb.matrix).copy()
        q0 = rotation(pb)
        columns, rigid = [], True
        reach = max((p0 - mw @ pb.head).length, 1e-6)
        try:
            for i in free_axes:
                axis = Vector((0.0, 0.0, 0.0))
                axis[i] = 1.0
                set_rotation(obj.pose.bones[bone], q0 @ Quaternion(axis, DRIVE_EPS))
                ctx.view_layer.update()
                pbs = obj.pose.bones
                p = mw @ pbs[name].matrix @ local
                columns.append((p - p0) / DRIVE_EPS)
                riding = (mw @ pbs[bone].matrix) @ m0.inverted_safe() @ p0
                if (riding - p).length > 0.02 * reach * DRIVE_EPS:
                    rigid = False
        finally:
            set_rotation(obj.pose.bones[bone], q0)
            ctx.view_layer.update()
        if rigid:
            return None
        # Axes that don't move the drawn point (a Rigify tail copies only some of the master's
        # turn) are left alone: turning about them would show nowhere.
        top = max(c.length for c in columns)
        keep = [k for k, c in enumerate(columns) if c.length > 0.05 * top]
        return Drive(obj, obj.pose.bones[bone], name, p0, [columns[k] for k in keep], [free_axes[k] for k in keep])

    def step(self, obj, pb, target, turn=None):
        """Write pb's rotation for this frame. target: where the point should be (world); turn: a
        world rotation the controller adds on top (it moves the point away from the hand)."""
        if turn is not None and _angle(turn) > 1e-7:
            m = (obj.matrix_world @ pb.matrix).to_quaternion()
            local = m.inverted() @ turn @ m
            axis, angle = local.to_axis_angle()
            if angle > math.pi:
                angle -= 2.0 * math.pi
            add = np.array([axis[i] * angle for i in self.axes])
            self.theta = self.theta + add
            self.lead = self.lead + self.J @ add
        p = self.point(obj)
        if p is None:
            return
        if self.pending is not None and _same4(pb.matrix, self.pending) and self.stale < 3:
            self.stale += 1  # Not evaluated since the last write: wait for it (a turn waits too).
            return
        self.stale = 0
        target = Vector(target) + Vector(self.lead)
        pv = np.array(p[:], np.float64)
        if self.last is not None:
            dp, dt = pv - self.last[0], self.theta - self.last[1]
            n = float(dt @ dt)
            if n > DRIVE_LEARN ** 2:  # Smaller steps are mostly the hand's jitter.
                self.J += np.outer(dp - self.J @ dt, dt) / n
        self.last = (pv, self.theta.copy())
        e = np.array(target[:], np.float64) - pv
        jtj = self.J.T @ self.J
        lam = 1e-9 + 0.01 * float(np.trace(jtj)) / len(self.axes)
        d = np.linalg.solve(jtj + lam * np.eye(len(self.axes)), self.J.T @ e) * DRIVE_GAIN
        size = float(np.linalg.norm(d))
        if size > DRIVE_MAX_STEP:
            d *= DRIVE_MAX_STEP / size
        self.theta = self.theta + d
        self._write(pb)

    def _write(self, pb):
        v = Vector((0.0, 0.0, 0.0))
        for k, i in enumerate(self.axes):
            v[i] = self.theta[k]
        q = self.q0 @ (Quaternion(v.normalized(), v.length) if v.length > 1e-12 else Quaternion())
        before = pb.matrix_basis.copy()
        apply_basis(pb, Matrix.LocRotScale(pb.location, q, pb.scale), False)
        self.pending = pb.matrix.copy() if not _same4(pb.matrix_basis, before) else None


class Follow:
    """Make a bone (or an object) follow a moving pose, from the pose it has at creation.

    kind: 'SWING' (turn about the head so `grab` follows the hand, plus the controller's twist and
    whatever turn it adds beyond the hand's move), 'TURN' (turn about the head as the controller
    turns, wherever the hand goes), 'RIGID' (move and turn with the pose; only turn for a connected
    bone) or 'IK' (rigid, with the parent chain bent to reach). apply(pose) writes the transforms
    and returns {pose bone: location changed} (or {object: True}) for keying.
    """

    def __init__(self, obj, pb, pose, kind='RIGID', grab=None, chain=2, mirror=False, curl=False, drive=None):
        """drive: the grabbed point (world) on the bone's drawn shape, to follow with a Drive if
        the shape is on another bone the bone moves (SWING only)."""
        self.obj_name = obj.name
        self.bone_name = pb.name if pb is not None else None
        self.kind = kind if pb is not None else 'RIGID'
        self.chain = chain
        self.mirror = mirror
        self.curl = None  # (start Y scale, length of hand travel that counts as the bone's)
        self.curled = False
        self.pose0_inv = pose.inverted_safe()
        self.rot0_inv = pose.to_quaternion().inverted()
        self.offset = None
        self.drive = None
        mw = obj.matrix_world
        self.start = mw @ pb.matrix if pb is not None else mw.copy()  # world matrix
        if pb is not None:
            self.head0 = mw @ pb.head
            self.grab = Vector(grab) if grab is not None else mw @ pb.tail
            self.parents = [p.name for p in chain_parents(pb, chain)]
            if self.kind == 'IK' and not self.parents:
                self.kind = 'RIGID'
            if self.kind in {'SWING', 'TURN', 'RIGID'}:
                self.offset = constraint_offset(pb)
            length = (mw @ pb.tail - self.head0).length
            if curl and self.kind == 'SWING' and not pb.lock_scale[1] and length > 1e-7:
                self.curl = (pb.scale[1], min(length, CURL_REACH * _viewer_scale()))
            if drive is not None and self.kind == 'SWING' and self.curl is None:
                self.drive = Drive.make(obj, pb, drive)
                if self.drive is not None:
                    self.grab = Vector(drive)  # The point on the drawn shape, not on the bone.
            self.turned = Quaternion()  # SWING: the turn about the head so far (world)
            self.prev = (self.grab.copy(), pose.to_quaternion(), self.head0.copy())
            reach = (self.grab - self.head0).length / _viewer_scale()
            self.turn_weight = 1.0 if reach <= TURN_REACH else (TURN_REACH / reach) ** 2
        self.last_pose = pose.copy()
        self.last = {}

    def target(self):
        """(object, pose bone or None), or None once either is gone (deleted, undone)."""
        obj = bpy.data.objects.get(self.obj_name)
        if obj is None:
            return None
        if self.bone_name is None:
            return obj, None
        if obj.type != 'ARMATURE':
            return None
        pb = obj.pose.bones.get(self.bone_name)
        return (obj, pb) if pb is not None else None

    def _step(self, head, hand, rot, grab_dir, fade_twist=False):
        """This update's (aim, turn), world: aim, the hand-held point's move about the head; turn,
        the controller's twist about the grab (grab_dir, after the aim) and its turn beyond what
        the move explains (none when the arm swings the wrist along, all of it when it turns in
        place), the latter faded by turn_weight. The controller's turn goes through a backlash of
        TURN_DEAD first: jitter smaller than that never turns the bone, a real turn follows 1:1."""
        p_hand, ref, p_head = self.prev
        rot = rot.copy()
        rot.make_compatible(ref)
        gap = _angle(ref.rotation_difference(rot))
        new_ref = ref.slerp(rot, 1.0 - TURN_DEAD / gap) if gap > TURN_DEAD else ref.copy()
        self.prev = (hand.copy(), new_ref, head.copy())
        aim = _between(p_hand - p_head, hand - head)
        r = new_ref @ ref.inverted()
        axis = aim @ grab_dir
        twist = _twist(r, axis.normalized()) if axis.length > 1e-9 else Quaternion()
        r_swing = r @ twist.inverted()
        a_aim, a_turn = _angle(aim), _angle(r_swing)
        extra = Quaternion()
        if a_turn > 1e-9 and a_turn > a_aim:
            excess = r_swing @ aim.inverted()
            if excess.w < 0.0:
                excess = Quaternion((-excess.w, -excess.x, -excess.y, -excess.z))
            extra = Quaternion().slerp(excess, (1.0 - a_aim / a_turn) * self.turn_weight)
        if fade_twist:  # The grab isn't on the bone: its twist swings the far end like a turn.
            if twist.w < 0.0:
                twist = Quaternion((-twist.w, -twist.x, -twist.y, -twist.z))
            twist = Quaternion().slerp(twist, self.turn_weight)
        return aim, extra @ twist

    def _swing(self, head, hand, rot):
        """Advance self.turned by this update."""
        aim, turn = self._step(head, hand, rot, self.turned @ (self.grab - self.head0))
        self.turned = (turn @ aim @ self.turned).normalized()

    def apply(self, pose=None):
        pose = pose if pose is not None else self.last_pose
        self.last_pose = pose.copy()
        found = self.target()
        if found is None:
            self.last = {}
            return self.last
        obj, pb = found
        carry = pose @ self.pose0_inv  # world-space motion since the start
        if pb is None:
            obj.matrix_world = carry @ self.start
            self.last = {obj: True}
            return self.last
        mw = obj.matrix_world
        mwi = mw.inverted_safe()
        written = {}
        parent = pb.parent.matrix if pb.parent else None
        if self.kind == 'SWING' and self.drive is not None:
            hand = carry @ self.grab
            head = mw @ pb.head
            _aim, turn = self._step(head, hand, pose.to_quaternion(), self.drive.grab_dir(obj), fade_twist=True)
            self.drive.step(obj, pb, hand, turn)
            written[pb] = False
        elif self.kind in {'SWING', 'TURN'}:
            head = mw @ pb.head  # Follows the parent, if it moved since.
            hand = carry @ self.grab
            if self.kind == 'SWING':
                self._swing(head, hand, pose.to_quaternion())
                turn = self.turned
            else:
                turn = pose.to_quaternion() @ self.rot0_inv
            world = Matrix.Translation(head - self.head0) @ _about(self.head0, turn) @ self.start
            apply_basis(pb, _uncopy(self.offset, basis_for(pb, mwi @ world, parent)), False)
            if self.curl is not None:
                s0, reach = self.curl
                pull = ((hand - head).length - (self.grab - self.head0).length) / reach  # + = out
                pull = math.copysign(max(abs(pull) - CURL_DEAD_ZONE, 0.0), pull)
                sy = _clamp(s0 * (1.0 + CURL_RATE * pull), CURL_MIN, CURL_MAX)
                if abs(sy - pb.scale[1]) > 1e-6:
                    pb.scale[1] = sy
                self.curled = self.curled or abs(sy - s0) > 1e-6
            written[pb] = self.curled  # Keys location and scale too (the scale curls).
        elif self.kind == 'RIGID':
            world = carry @ self.start
            moves = free(pb)
            apply_basis(pb, _uncopy(self.offset, basis_for(pb, mwi @ world, parent)), moves)
            written[pb] = moves
        else:
            chain = [obj.pose.bones.get(n) for n in self.parents]
            chain = [c for c in chain if c is not None]
            mats = [c.matrix.copy() for c in chain]
            effector = mats[0].inverted_safe() @ pb.head
            wanted = carry @ self.start
            solve_ccd(mats, effector, mwi @ wanted.translation, constrain=chain_limits(chain))
            # Root of the chain first: each bone's basis is relative to its parent's new matrix.
            for i in range(len(chain) - 1, -1, -1):
                c = chain[i]
                parent = parent_matrix(c, chain[i + 1], mats[i + 1]) if i + 1 < len(chain) else parent_matrix(c)
                apply_basis(c, basis_for(c, mats[i], parent), False, ik=True)
                written[c] = False
            apply_basis(pb, basis_for(pb, mwi @ wanted, parent_matrix(pb, chain[0], mats[0])), False, ik=True)
            written[pb] = False
        if self.mirror:
            for b, location in list(written.items()):
                other = mirror_bone(obj, b, location)
                if other is not None and other not in written:
                    written[other] = location
        self.last = written
        return written


def grab_kind(obj, pb, tool, fraction=None, curl=False):
    """How a grabbed (or, with fraction None, bound) bone follows: 'RIGID' for a bone steering the
    rig's own IK, 'IK' with the IK tool, else 'RIGID' (a free bone near its head, or a binding) or
    'SWING'. A free bone that can't swing (X and Z rotation locked: Rigify's tweak controls, which
    only twist) is moved wherever it's grabbed; one that curls (curl) always turns."""
    if pb.name in limbs.ik_handles(obj):
        return 'RIGID'
    if tool == 'IK':
        return 'IK'
    if fraction is None:
        return 'RIGID'
    if curl:
        return 'SWING'
    if free(pb) and (fraction < MOVE_ZONE or not can_swing(pb)):
        return 'RIGID'
    return 'SWING'


def can_swing(pb):
    """Whether the bone can turn off its own axis (X or Z rotation not locked). Euler and quaternion
    locks both name the local axes; axis-angle bones are taken as free."""
    if pb.rotation_mode == 'AXIS_ANGLE':
        return True
    return not (pb.lock_rotation[0] and pb.lock_rotation[2])


_SCALE_Y = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.scale(?:\.y|\[1\])$')


def curl_bones(obj):
    """{bone name} of the bones whose length scale (Y) a driver of the rig reads: Rigify's finger,
    toe, ear and tail masters, which curl their chain by it."""
    out = set()
    ad = obj.animation_data
    if ad is None:
        return out
    for fc in ad.drivers:
        for var in fc.driver.variables:
            for t in var.targets:
                if t.id != obj:
                    continue
                if var.type == 'SINGLE_PROP':
                    m = _SCALE_Y.match(t.data_path or "")
                    if m is not None:
                        out.add(limbs.unescape(m.group(1)))
                elif var.type == 'TRANSFORMS' and t.bone_target and t.transform_type in {'SCALE_Y', 'SCALE_AVG'}:
                    out.add(t.bone_target)
    return out


def swing_grab(obj, pb, point, curl=False):
    """The point a swing turns the bone by. A shape grabbed near the bone's head (Rigify's FK hand
    ring) is moved out along the bone to half its length, so the bone has a lever to turn by; with
    curl, to its whole length, so pushing it in to curl doesn't pass the head."""
    point = Vector(point)
    if not (curl or has_shape(obj, pb)):
        return point
    mw = obj.matrix_world
    head, tail = mw @ pb.head, mw @ pb.tail
    axis = tail - head
    length = axis.length
    if length < 1e-7:
        return point
    axis /= length
    along = (point - head).dot(axis)
    lever = (1.0 if curl else 0.5) * length
    return point + axis * (lever - along) if along < lever else point


class Grab:
    """Trigger handler: pose the touched bone (FK or IK), or move the touched object, while held.
    The press selects it (and switches its limb to the side it belongs to); it only starts to
    move once the DragGate opens."""

    def __init__(self, context, settings, obj, pb, fraction, tip_pose):
        from . import interactions
        self.written = {}
        self.switched = []
        curl = False
        if pb is not None:
            root = limbs.gear_root(context, obj, pb)
            if root is not None:  # A limb's settings gear: turn the limb from its root.
                pb, kind = root, 'TURN'
                select_bone(obj, pb)
            else:
                select_bone(obj, pb)
                written, self.switched = limbs.prepare(context, obj, pb, settings.pose_mirror)
                self.written.update(written)
                curl = settings.pose_curl and pb.name in curl_bones(obj)
                kind = grab_kind(obj, pb, settings.pose_tool, fraction, curl)
        else:
            primitives.make_active(context, obj)
            kind = 'RIGID'
        self._follow_args = (obj, pb, kind, settings.pose_chain, settings.pose_mirror, curl)
        self.follow = None
        self._start_follow(tip_pose, measure=False)
        self.gate = interactions.DragGate(context, tip_pose)
        self.changed = bool(self.switched)

    def _start_follow(self, pose, measure=True):
        """measure: let a swing of a bone drawn elsewhere set up its Drive (a few rig evaluations:
        done once the move begins, not on the press)."""
        from . import puppet
        obj, pb, kind, chain, mirror, curl = self._follow_args
        if self.follow is not None:
            puppet.grab_ended(self.follow)
        point = pose.translation
        grab = swing_grab(obj, pb, point, curl) if kind == 'SWING' else point
        drive = point if measure and pb is not None and kind == 'SWING' else None
        self.follow = Follow(obj, pb, pose, kind, grab=grab, chain=chain, mirror=mirror, curl=curl, drive=drive)
        puppet.grab_started(self.follow)

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            if self.follow.target() is None:
                return
            self._start_follow(base)  # From where the hand was when the move began.
        written = self.follow.apply(tip_pose)
        if written:
            for k, location in written.items():
                self.written[k] = self.written.get(k, False) or location
            self.changed = True

    def written_names(self):
        return [k.name for k in self.written]

    def abandon(self):
        from . import puppet
        puppet.grab_ended(self.follow)

    def finish(self, context):
        from . import puppet
        puppet.grab_ended(self.follow)
        if not self.changed:
            return
        objs = [k for k in self.written if isinstance(k, bpy.types.Object)]
        bones = {k: v for k, v in self.written.items() if not isinstance(k, bpy.types.Object)}
        rig_obj = bpy.data.objects.get(self.follow.obj_name)
        keyed = rig_obj is not None and limbs.key_switches(context, rig_obj, self.switched)
        keyed = anim.auto_key_bones(context, bones) or keyed
        for obj in objs:
            keyed = anim.auto_key(context, obj) or keyed
        if not keyed:
            from . import onion
            onion.mark_dirty()


def pick(context, settings, tip_pose, scale, off=False):
    """Trigger press in Pose mode: a Grab handler for the bone (or object) at the tip, a SelectRig for
    another character (off hand), or None."""
    from . import interactions
    tip = tip_pose.translation
    obj = rig(context)
    if obj is not None:
        hit = nearest_bone(context, tip, scale, obj)
        if hit is not None:
            return Grab(context, settings, obj, hit[0], hit[1], tip_pose)
    other = rig_at(context, tip, scale) if off else None
    if other is not None:
        return SelectRig(context, other)
    if obj is not None:
        return None
    touched = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
    return Grab(context, settings, touched, None, 0.0, tip_pose) if touched is not None else None


def engaged(context, tip, scale):
    from . import interactions
    if rig(context) is not None:
        return nearest_bone(context, tip, scale, fresh=False) is not None
    return primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale) is not None


# --- drawing ---------------------------------------------------------------

def forget_caches():
    """The cache holds pose bones, which an undo frees (and shapes, which it may change)."""
    _cache.update(time=0.0, key=None, geometry=None, layout_time=0.0, layout_key=None, layout=None)
    _shapes.clear()


def draw(context, tips, scale):
    """Draw the posed rig's bones (as their custom shapes, if any) on top of everything; the bones
    in reach of a tip are highlighted."""
    obj = rig(context)
    if obj is None:
        return
    geo = geometry(obj)
    hover = set()
    for tip in tips:
        hit = geo.nearest(tip, REACH * scale)
        if hit is not None:
            hover.add(hit[0].name)
    active = obj.data.bones.active
    colors = []
    for pb in geo.bones:
        if pb.name in hover:
            colors.append(3)
        elif active is not None and pb.name == active.name:
            colors.append(2)
        elif pb.select:
            colors.append(1)
        else:
            colors.append(0)
    vcolor = np.array(colors, np.int8)[geo.vowner] if len(geo.vowner) else np.zeros(0, np.int8)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for k, color in enumerate((BONE_COLOR, SELECTED_COLOR, ACTIVE_COLOR, HOVER_COLOR)):
        coords = geo.coords[vcolor == k]
        if not len(coords):
            continue
        batch = batch_for_shader(shader, 'LINES', {"pos": np.ascontiguousarray(coords)})
        shader.uniform_float("color", color)
        batch.draw(shader)
    gpu.state.blend_set('NONE')
