"""Pose mode: pose an armature's bones with the controllers.

Trigger on a bone (FK): the bone turns about its head so the grabbed point follows the tip, and
twisting the controller rolls it. A free bone (no parent, or not connected to it) grabbed near
its head moves with the controller instead. IK: the grabbed bone follows the controller rigidly
and up to pose_chain parent bones bend so its head gets there (CCD solved here, so it works on
any rig without adding constraints). Without an armature in Pose Mode, the trigger moves the
objects it touches, so props can be puppeteered too.

The same Follow class drives puppet bindings (puppet.py): a binding is a grab that never ends,
driven by a controller's grip or the headset instead of the trigger.

Writes go through Bone.convert_local_to_pose(invert=True), which turns a wanted armature-space
matrix into the bone's local (basis) matrix given its parent's, so a whole IK chain is written
in one pass without waiting for Blender to re-evaluate the pose.
"""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import anim, primitives

REACH = 0.02  # hand-space meters around a bone that still grab it
MOVE_ZONE = 0.3  # share of a free bone's length, from its head, that moves it instead of turning it
CCD_ITERATIONS = 12
CCD_TOLERANCE = 1e-5

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


def rigs(context):
    return [o for o in context.scene.objects if o.type == 'ARMATURE']


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
        menus.set_object_mode(context, active, 'OBJECT')
        _state["previous"] = active.name
    if obj.visible_get():
        primitives.make_active(context, obj)
        menus.set_object_mode(context, obj, 'POSE')


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
    bone = pb.bone
    if bone.hide:
        return False
    colls = bone.collections
    return not len(colls) or any(getattr(c, "is_visible_effectively", c.is_visible) for c in colls)


def segments(obj):
    """[(pose bone, world head, world tail)] for the rig's visible bones."""
    mw = obj.matrix_world
    return [(pb, mw @ pb.head, mw @ pb.tail) for pb in obj.pose.bones if visible(pb)]


def _closest(p, a, b):
    """(distance, fraction along a-b) of the point on segment a-b closest to p."""
    ab = b - a
    n = ab.length_squared
    f = 0.0 if n < 1e-12 else min(max((p - a).dot(ab) / n, 0.0), 1.0)
    return (p - (a + ab * f)).length, f


def nearest_bone(context, point, scale, obj=None):
    """(pose bone, fraction along it) of the bone within reach of point, or None."""
    obj = obj or rig(context)
    if obj is None:
        return None
    point = Vector(point)
    best, best_d = None, REACH * scale
    for pb, head, tail in segments(obj):
        d, f = _closest(point, head, tail)
        if d <= best_d:
            best, best_d = (pb, f), d
    return best


def select_bone(obj, pb):
    for other in obj.pose.bones:
        other.select = other == pb
    obj.data.bones.active = pb.bone


def active_bone(obj):
    bone = obj.data.bones.active
    return obj.pose.bones.get(bone.name) if bone is not None else None


def free(pb):
    """Whether the bone can move (not only turn): no parent, or not connected to it."""
    return pb.parent is None or not pb.bone.use_connect


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


def apply_basis(pb, basis, location):
    loc, rot, _scale = basis.decompose()
    set_rotation(pb, rot)
    if location:
        set_location(pb, loc)


def mirror_bone(obj, pb, location):
    """Copy pb's local transform, mirrored across X, to its .L/.R counterpart. Returns it or None."""
    other = obj.pose.bones.get(bpy.utils.flip_name(pb.name))
    if other is None or other == pb:
        return None
    q = rotation(pb)
    set_rotation(other, Quaternion((q.w, q.x, -q.y, -q.z)))
    if location:
        loc = pb.location
        set_location(other, (-loc.x, loc.y, loc.z))
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


def solve_ccd(mats, effector, target, iterations=CCD_ITERATIONS):
    """Turn the chain so the effector reaches target (Cyclic Coordinate Descent), in place.

    mats: armature-space matrices, the effector's bone first, then its parents in order;
    effector: a point fixed in mats[0]'s bone space. Returns the effector's final position.
    """
    for _ in range(iterations):
        for j in range(len(mats)):
            eff = mats[0] @ effector
            head = mats[j].translation
            a, b = eff - head, target - head
            if a.length < 1e-7 or b.length < 1e-7:
                continue
            r = _about(head, a.rotation_difference(b))
            for k in range(j + 1):
                mats[k] = r @ mats[k]
        if (mats[0] @ effector - target).length < CCD_TOLERANCE:
            break
    return mats[0] @ effector


class Follow:
    """Make a bone (or an object) follow a moving pose, from the pose it has at creation.

    kind: 'SWING' (turn about the head so `grab` follows, plus the pose's twist), 'RIGID'
    (move and turn with the pose; only turn for a connected bone) or 'IK' (rigid, with the
    parent chain bent to reach). apply(pose) writes the transforms and returns
    {pose bone: location changed} (or {object: True}) for keying.
    """

    def __init__(self, obj, pb, pose, kind='RIGID', grab=None, chain=2, mirror=False):
        self.obj_name = obj.name
        self.bone_name = pb.name if pb is not None else None
        self.kind = kind if pb is not None else 'RIGID'
        self.chain = chain
        self.mirror = mirror
        self.pose0_inv = pose.inverted_safe()
        self.rot0_inv = pose.to_quaternion().inverted()
        mw = obj.matrix_world
        self.start = mw @ pb.matrix if pb is not None else mw.copy()  # world matrix
        if pb is not None:
            self.head0 = mw @ pb.head
            self.grab = Vector(grab) if grab is not None else mw @ pb.tail
            self.parents = []
            p = pb.parent
            while p is not None and len(self.parents) < chain:
                self.parents.append(p.name)
                p = p.parent
            if self.kind == 'IK' and not self.parents:
                self.kind = 'RIGID'
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
        if self.kind == 'SWING':
            head = mw @ pb.head  # Follows the parent, if it moved since.
            d0, d1 = self.grab - self.head0, carry @ self.grab - head
            swing = d0.rotation_difference(d1) if d0.length > 1e-7 and d1.length > 1e-7 else Quaternion()
            delta = pose.to_quaternion() @ self.rot0_inv
            twist = _twist(delta, d1.normalized()) if d1.length > 1e-7 else Quaternion()
            world = Matrix.Translation(head - self.head0) @ _about(self.head0, twist @ swing) @ self.start
            basis = basis_for(pb, mwi @ world, pb.parent.matrix if pb.parent else None)
            apply_basis(pb, basis, False)
            written[pb] = False
        elif self.kind == 'RIGID':
            world = carry @ self.start
            basis = basis_for(pb, mwi @ world, pb.parent.matrix if pb.parent else None)
            moves = free(pb)
            apply_basis(pb, basis, moves)
            written[pb] = moves
        else:
            chain = [obj.pose.bones.get(n) for n in self.parents]
            chain = [c for c in chain if c is not None]
            mats = [c.matrix.copy() for c in chain]
            effector = mats[0].inverted_safe() @ pb.head
            wanted = carry @ self.start
            solve_ccd(mats, effector, mwi @ wanted.translation)
            # Root of the chain first: each bone's basis is relative to its parent's new matrix.
            for i in range(len(chain) - 1, -1, -1):
                c = chain[i]
                parent = mats[i + 1] if i + 1 < len(chain) else (c.parent.matrix if c.parent else None)
                apply_basis(c, basis_for(c, mats[i], parent), False)
                written[c] = False
            apply_basis(pb, basis_for(pb, mwi @ wanted, mats[0]), False)
            written[pb] = False
        if self.mirror:
            for b, location in list(written.items()):
                other = mirror_bone(obj, b, location)
                if other is not None and other not in written:
                    written[other] = location
        self.last = written
        return written


class Grab:
    """Trigger handler: pose the touched bone (FK or IK), or move the touched object, while held.
    The press selects it; it only starts to move once the DragGate opens."""

    def __init__(self, context, settings, obj, pb, fraction, tip_pose):
        from . import interactions
        if pb is not None:
            select_bone(obj, pb)
            if settings.pose_tool == 'IK':
                kind = 'IK'
            else:
                kind = 'RIGID' if free(pb) and fraction < MOVE_ZONE else 'SWING'
        else:
            primitives.make_active(context, obj)
            kind = 'RIGID'
        self._follow_args = (obj, pb, kind, settings.pose_chain, settings.pose_mirror)
        self.follow = None
        self._start_follow(tip_pose)
        self.gate = interactions.DragGate(context, tip_pose)
        self.written = {}
        self.changed = False

    def _start_follow(self, pose):
        from . import puppet
        obj, pb, kind, chain, mirror = self._follow_args
        if self.follow is not None:
            puppet.grab_ended(self.follow)
        self.follow = Follow(obj, pb, pose, kind, grab=pose.translation, chain=chain, mirror=mirror)
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
        keyed = anim.auto_key_bones(context, bones)
        for obj in objs:
            keyed = anim.auto_key(context, obj) or keyed
        if not keyed:
            from . import onion
            onion.mark_dirty()


def pick(context, settings, tip_pose, scale):
    """Trigger press in Pose mode: a Grab handler for the bone (or object) at the tip, or None."""
    from . import interactions
    tip = tip_pose.translation
    obj = rig(context)
    if obj is not None:
        hit = nearest_bone(context, tip, scale, obj)
        return Grab(context, settings, obj, hit[0], hit[1], tip_pose) if hit is not None else None
    touched = primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale)
    return Grab(context, settings, touched, None, 0.0, tip_pose) if touched is not None else None


def engaged(context, tip, scale):
    from . import interactions
    if rig(context) is not None:
        return nearest_bone(context, tip, scale) is not None
    return primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale) is not None


# --- drawing ---------------------------------------------------------------

_cache = {"time": 0.0, "key": None, "bones": []}
CACHE_SECONDS = 0.006  # both eyes of one headset frame reuse the bone shapes


def forget_caches():
    """The cache holds pose bones, which an undo frees."""
    _cache.update(time=0.0, key=None, bones=[])


def _bone_lines(obj):
    """[(pose bone, [line coords])]: an octahedral wire per visible bone, like Blender's."""
    now = time.monotonic()
    key = (obj.name, obj.mode)
    if key == _cache["key"] and now - _cache["time"] < CACHE_SECONDS:
        return _cache["bones"]
    mw = obj.matrix_world
    out = []
    for pb in obj.pose.bones:
        if not visible(pb):
            continue
        m = mw @ pb.matrix
        head, tail = m.translation, mw @ pb.tail
        length = (tail - head).length
        if length < 1e-7:
            continue
        x = m.col[0].xyz.normalized() * (0.1 * length)
        z = m.col[2].xyz.normalized() * (0.1 * length)
        mid = head + (tail - head) * 0.1
        ring = [mid + x, mid + z, mid - x, mid - z]
        coords = []
        for i in range(4):
            coords += [head, ring[i], ring[i], tail, ring[i], ring[(i + 1) % 4]]
        out.append((pb, coords))
    _cache.update(time=now, key=key, bones=out)
    return out


def draw(context, tips, scale):
    """Draw the posed rig's bones on top of everything; the bones in reach of a tip are highlighted."""
    obj = rig(context)
    if obj is None:
        return
    hover = set()
    for tip in tips:
        hit = nearest_bone(context, tip, scale, obj)
        if hit is not None:
            hover.add(hit[0].name)
    active = obj.data.bones.active
    groups = {}
    for pb, coords in _bone_lines(obj):
        if pb.name in hover:
            color = HOVER_COLOR
        elif active is not None and pb.name == active.name:
            color = ACTIVE_COLOR
        elif pb.select:
            color = SELECTED_COLOR
        else:
            color = BONE_COLOR
        groups.setdefault(color, []).extend(coords)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for color, coords in groups.items():
        batch = batch_for_shader(shader, 'LINES', {"pos": coords})
        shader.uniform_float("color", color)
        batch.draw(shader)
    gpu.state.blend_set('NONE')
