"""Rig-owned IK/FK switches and IK handles: Rigify limbs, and any rig built the same way.

A switched limb is a chain of bones (Rigify's ORG bones) that each copy the transforms of an FK
control and, on top of it, a bone of an IK chain (two Copy Transforms), with the second
constraint's influence driven by one custom property (Rigify's IK_FK on upper_arm_parent.L /
thigh_parent.L). Limbs are found from the rig's drivers and constraints, so no bone names are
assumed and the generated rig_ui.py doesn't have to be registered.

Grabbing a control of the side that isn't active switches the limb first, keeping its pose: the
other side is snapped to where the limb is, then the property is set. To FK, each FK control gets
its limb bone's matrix (exact). To IK: the chain root control (Rigify's upper_arm_ik) and the pole
get the limb's pose; the IK handle (the topmost visible bone the IK target hangs from: hand_ik,
foot_ik) goes where it sits on the limb's end in the rest pose, then is moved by the end's error;
the controls riding between them (foot_spin_ik, a rear paw's foot_heel_ik) get the same; then the
bend plane is turned (pole, or the root's twist) until the IK elbow is where the limb's is. Each
fit keeps the closest placement found, so a pose the IK can't make exactly (a rear paw's heel)
lands as close as it can. Rigify's own snap operators do the same per rig type.

The inactive side's controls are drawn (and picked) where a switch would put them (StandIn), so an
IK arm's FK rings sit on the arm instead of hanging at rest where they were last posed.

Nothing here holds Blender data between calls (only names), so undo can't leave it dangling.
"""

import math
import re

import bpy
from mathutils import Quaternion

_BONE_CON = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.constraints\["((?:[^"\\]|\\.)*)"\]\.influence$')
_BONE_PROP = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\["((?:[^"\\]|\\.)*)"\]$')

SNAP_STEPS = 4  # iterations of the handle and bend-plane fits (each one re-evaluates the rig)
SNAP_TOLERANCE = 1e-5  # meters (armature space)


def unescape(s):
    return s.replace('\\"', '"').replace('\\\\', '\\')


def _enabled(con):
    return getattr(con, "enabled", not con.mute)


def _visible(pb):
    from . import pose
    return pose.visible(pb)


class Limb:
    """One switched limb, by bone names. bones: the switched bones, parent first; fk / ik: the bone
    each one copies on either side; prop_bone, prop: the switch property; ik_driven: whether the
    driven constraint is the IK side; con: (bone, constraint) whose influence tells the mode;
    handle: the IK handle or None; run: visible bones from the IK target up to the handle; poles;
    end: index of the bone whose IK side is the IK target (or None)."""

    def __init__(self, path, prop_bone, prop):
        self.path, self.prop_bone, self.prop = path, prop_bone, prop
        self.bones, self.fk, self.ik = [], [], []
        self.ik_driven = True
        self.con = None
        self.handle = None
        self.run = []
        self.poles = []
        self.end = None

    def side(self, obj, pb):
        """'FK' or 'IK' if pb is one of this limb's controls, else None."""
        name = pb.name
        if name in self.fk:
            return 'FK'
        if name in self.ik or name in self.run or name in self.poles:
            return 'IK'
        if self.handle is not None:
            p = pb.parent
            while p is not None:
                if p.name == self.handle:
                    return 'IK'
                p = p.parent
        return None


def _ik_members(obj):
    """({bone name} of the bones in an IK constraint's chain, [(IK constraint, {its chain's bone
    names})])."""
    members, cons = set(), []
    for pb in obj.pose.bones:
        for con in pb.constraints:
            if con.type != 'IK':
                continue
            chain = set()
            n = con.chain_count or 64
            b = pb
            while b is not None and n > 0:
                chain.add(b.name)
                b, n = b.parent, n - 1
            members |= chain
            cons.append((con, chain))
    return members, cons


def _solver(pb):
    """pb's enabled IK constraint on the same rig with a target bone, or None."""
    for con in pb.constraints:
        if con.type == 'IK' and _enabled(con) and con.influence > 1e-6 and con.target == pb.id_data and con.subtarget:
            return con
    return None


def _handle_run(obj, target_name):
    """The visible bones the IK target hangs from, bottom up: up its parents, through hidden bones
    until visible ones, and up those until the next hidden bone. A hidden bone placed by an IK of
    its own is passed by following that IK's target instead (Rigify's rear paw: the heel rides an
    IK aimed at a target under foot_ik). The last one is the handle (hand_ik; foot_spin_ik then
    foot_ik for a Rigify leg)."""
    pbs = obj.pose.bones
    pb = pbs.get(target_name)
    run, seen, climbing = [], set(), False
    while pb is not None and pb.name not in seen:
        seen.add(pb.name)
        if _visible(pb):
            run.append(pb.name)
            climbing = True
        else:
            con = _solver(pb)
            if con is not None:
                pb, climbing = pbs.get(con.subtarget), False
                continue
            if climbing:
                break
        pb = pb.parent
    return run


def ik_handles(obj):
    """{bone name} of the visible bones that steer the rig's own IK constraints: the bone each IK
    target hangs from (passing hidden helper bones), and the poles. Grabbing one moves it."""
    out = set()
    for pb in obj.pose.bones:
        for con in pb.constraints:
            if con.type != 'IK' or con.target != obj:
                continue
            if con.subtarget:
                run = _handle_run(obj, con.subtarget)
                if run:
                    out.add(run[-1])
            if con.pole_target == obj and con.pole_subtarget:
                pole = obj.pose.bones.get(con.pole_subtarget)
                if pole is not None and _visible(pole):
                    out.add(pole.name)
    return out


def find(obj):
    """[Limb] of the armature."""
    ad = obj.animation_data
    if ad is None or obj.type != 'ARMATURE':
        return []
    pbs = obj.pose.bones
    by_path = {}  # prop path -> [(pose bone, base constraint, driven constraint)]
    for fc in ad.drivers:
        m = _BONE_CON.match(fc.data_path)
        if m is None:
            continue
        variables = fc.driver.variables
        if len(variables) != 1 or variables[0].type != 'SINGLE_PROP':
            continue
        target = variables[0].targets[0]
        if target.id != obj or _BONE_PROP.match(target.data_path) is None:
            continue
        pb = pbs.get(unescape(m.group(1)))
        con = pb.constraints.get(unescape(m.group(2))) if pb is not None else None
        if con is None or con.type != 'COPY_TRANSFORMS' or con.target != obj or not con.subtarget:
            continue
        base = [c for c in pb.constraints if c.type == 'COPY_TRANSFORMS' and c != con
                and c.target == obj and c.subtarget]
        if len(base) == 1:
            by_path.setdefault(target.data_path, []).append((pb, base[0], con))
    if not by_path:
        return []
    members, ik_cons = _ik_members(obj)
    limbs = []
    for path, rows in by_path.items():
        m = _BONE_PROP.match(path)
        limb = Limb(path, unescape(m.group(1)), unescape(m.group(2)))
        if limb.prop_bone not in pbs:
            continue
        rows.sort(key=lambda r: len(r[0].parent_recursive))  # Parent first.
        base = [r[1].subtarget for r in rows]
        driven = [r[2].subtarget for r in rows]
        in_base = sum(n in members for n in base)
        in_driven = sum(n in members for n in driven)
        if in_base == in_driven:
            continue  # Not an IK/FK switch.
        limb.ik_driven = in_driven > in_base
        limb.bones = [r[0].name for r in rows]
        limb.fk, limb.ik = (base, driven) if limb.ik_driven else (driven, base)
        limb.con = (rows[0][0].name, rows[0][2].name)
        ik_set = set(limb.ik)
        for con, chain in ik_cons:
            if con.target != obj or con.subtarget not in pbs or not (chain & ik_set):
                continue
            if limb.handle is None:
                run = _handle_run(obj, con.subtarget)
                if run:
                    limb.run, limb.handle = run, run[-1]
                if con.subtarget in ik_set:
                    limb.end = limb.ik.index(con.subtarget)
            if con.pole_target == obj and con.pole_subtarget and con.pole_subtarget not in limb.poles:
                limb.poles.append(con.pole_subtarget)
        limbs.append(limb)
    return limbs


def limb_of(obj, pb, limbs=None):
    """(Limb, side) for a control of a switched limb, else (None, None)."""
    for limb in (find(obj) if limbs is None else limbs):
        side = limb.side(obj, pb)
        if side is not None:
            return limb, side
    return None, None


def gear_root(context, obj, pb):
    """For a limb's settings bone (Rigify's thigh_parent / upper_arm_parent gear: the switch
    property's bone, fully locked, nothing follows it), the limb's root control on its active
    side (thigh_ik or thigh_fk), which turns the whole limb; else None."""
    if not (all(pb.lock_location) and all(pb.lock_rotation)):
        return None
    pbs = obj.pose.bones
    for limb in find(obj):
        if limb.prop_bone != pb.name:
            continue
        side = limb.ik if ik_active(context, obj, limb) else limb.fk
        root = pbs.get(side[0]) if side else None
        while root is not None and not _visible(root):
            root = root.parent  # A hidden IK root (MCH-thigh_ik) turns with thigh_ik.
        if root is not None and root != pb and not _descends(pb, root):  # Never the torso above.
            return root
    return None


def ik_active(context, obj, limb):
    """Whether the limb follows its IK side (the driven constraint's evaluated influence)."""
    ev = obj.evaluated_get(context.evaluated_depsgraph_get())
    pb = ev.pose.bones.get(limb.con[0])
    con = pb.constraints.get(limb.con[1]) if pb is not None else None
    if con is None:
        return False
    return (con.influence >= 0.5) == limb.ik_driven


def mode(evaluated, limb):
    """'IK' or 'FK' when the limb is fully on that side (from the evaluated rig), None when mixed."""
    pb = evaluated.pose.bones.get(limb.con[0])
    con = pb.constraints.get(limb.con[1]) if pb is not None else None
    if con is None:
        return None
    if con.influence > 0.999:
        return 'IK' if limb.ik_driven else 'FK'
    if con.influence < 0.001:
        return 'FK' if limb.ik_driven else 'IK'
    return None


# --- the inactive side, shown on the limb ---------------------------------------------

class StandIn:
    """Where the controls of a limb's inactive side would be after a switch, cheap enough to work
    out every headset frame. Rigify leaves them where they were posed last (an FK arm hanging at
    rest beside an IK arm), which reads as a broken rig; drawn here, they sit on the limb, and
    grabbing one switches the limb, which puts them there for real. Built from bone names only."""

    def __init__(self, obj, limb):
        from . import pose
        pbs = obj.pose.bones
        self.limb = limb
        self.fk = [(n, b) for n, b in zip(limb.fk, limb.bones) if n in pbs and pose.visible(pbs[n])]
        handle = pbs.get(limb.handle) if limb.handle else None
        self.direct, self.riders, self.handle, self.pole = [], [], None, None
        for name, bone in zip(limb.ik, limb.bones):
            pb = pbs.get(name)
            if pb is not None and pb != handle and pose.visible(pb):
                self.direct.append((name, bone))  # The chain root (upper_arm_ik), toe_ik: the limb's pose.
        if handle is not None and limb.end is not None:
            end = pbs.get(limb.ik[limb.end])
            if end is not None:
                rest = end.bone.matrix_local.inverted_safe() @ handle.bone.matrix_local
                self.handle = (handle.name, limb.bones[limb.end], rest)
                placed = {n for n, _b in self.direct}
                self.riders = [pb.name for pb in pbs if pb.name not in placed and pb != handle
                               and pose.visible(pb) and _descends(pb, handle)]
        pole = next((n for n in limb.poles if n in pbs and pose.visible(pbs[n])), None)
        if pole is not None and len(limb.bones) >= 3:
            self.pole = (pole, limb.bones[0], limb.bones[1], limb.bones[limb.end if limb.end is not None else -1])

    def matrices(self, pbs, side):
        """{bone name: armature-space matrix} for the controls of side ('FK' or 'IK') if it is the
        inactive one; their own pose for the rest."""
        out = {}
        if side == 'FK':
            for name, bone in self.fk:
                out[name] = pbs[bone].matrix
            return out
        for name, bone in self.direct:
            out[name] = pbs[bone].matrix
        if self.pole is not None:
            pole, a, b, c = self.pole
            shoulder, elbow, end = pbs[a].matrix.translation, pbs[b].matrix.translation, pbs[c].matrix.translation
            axis = end - shoulder
            if axis.length > 1e-9:
                bend = _perp(elbow - shoulder, axis.normalized())
                if bend.length > 1e-6:
                    m = pbs[pole].matrix.copy()
                    m.translation = elbow + bend.normalized() * (elbow - shoulder).length
                    out[pole] = m
        if self.handle is not None:
            name, end, rest = self.handle
            m = pbs[end].matrix @ rest
            delta = m @ pbs[name].matrix.inverted_safe()
            out[name] = m
            for rider in self.riders:
                out[rider] = delta @ pbs[rider].matrix  # foot_spin_ik, foot_heel_ik: carried along.
        return out


def stand_ins(context, obj, plans):
    """{bone name: armature-space matrix} of the inactive side's controls of every limb fully on
    one side (plans: [StandIn])."""
    if not plans:
        return {}
    ev = obj.evaluated_get(context.evaluated_depsgraph_get())
    pbs = obj.pose.bones
    out = {}
    for plan in plans:
        m = mode(ev, plan.limb)
        if m is not None:
            out.update(plan.matrices(pbs, 'FK' if m == 'IK' else 'IK'))
    return out


# --- snapping ----------------------------------------------------------------

def set_matrix(pb, matrix):
    """Put pb at armature-space matrix (given its parent's current pose), keeping locked channels
    (and a connected bone's location)."""
    loc, scale = pb.location.copy(), pb.scale.copy()
    quat, euler = pb.rotation_quaternion.copy(), pb.rotation_euler.copy()
    pb.matrix = matrix
    connected = pb.bone.use_connect
    for i in range(3):
        if connected or pb.lock_location[i]:
            pb.location[i] = loc[i]
        if pb.lock_scale[i]:
            pb.scale[i] = scale[i]
    mode = pb.rotation_mode
    locks = tuple(pb.lock_rotation)
    if mode == 'QUATERNION':
        lock_w = pb.lock_rotation_w if pb.lock_rotations_4d else False
        for i, locked in enumerate((lock_w,) + locks):
            if locked:
                pb.rotation_quaternion[i] = quat[i]
    elif mode != 'AXIS_ANGLE':
        for i in range(3):
            if locks[i]:
                pb.rotation_euler[i] = euler[i]


def _channels(pb):
    return (pb.location.copy(), pb.rotation_quaternion.copy(), pb.rotation_euler.copy(),
            tuple(pb.rotation_axis_angle), pb.scale.copy())


def _restore(pb, channels):
    pb.location, pb.rotation_quaternion, pb.rotation_euler, pb.rotation_axis_angle, pb.scale = channels


def _set_prop(obj, limb, value):
    pb = obj.pose.bones[limb.prop_bone]
    old = pb.get(limb.prop)
    pb[limb.prop] = int(value) if isinstance(old, int) and not isinstance(old, bool) else float(value)
    obj.update_tag()  # Setting a custom property doesn't, and the drivers reading it must run.


def _set_mode(context, obj, limb, to_ik):
    """Set the switch property so the limb is fully on one side. Returns whether it is."""
    pb = obj.pose.bones[limb.prop_bone]
    current = float(pb.get(limb.prop, 0.0))
    for value in sorted((0.0, 1.0), key=lambda v: -abs(v - current)):  # The far end first.
        _set_prop(obj, limb, value)
        context.view_layer.update()
        if ik_active(context, obj, limb) == to_ik:
            return True
    return False


def _perp(v, axis):
    return v - axis * v.dot(axis)


def _snap_fk(context, obj, limb, wanted):
    written = {}
    for name, matrix in zip(limb.fk, wanted):
        pb = obj.pose.bones.get(name)
        if pb is None:
            continue
        set_matrix(pb, matrix)
        written[pb] = True
        context.view_layer.update()
    return written


def _descends(pb, ancestor):
    p = pb.parent
    while p is not None:
        if p == ancestor:
            return True
        p = p.parent
    return False


def _snap_ik(context, obj, limb, wanted):
    from . import pose
    pbs = obj.pose.bones
    written = {}
    handle = pbs.get(limb.handle) if limb.handle else None

    def put(pb, matrix):
        set_matrix(pb, matrix)
        written[pb] = True
        context.view_layer.update()

    # The chain's root controls (upper_arm_ik): the limb's pose, which seeds the IK's bend.
    for i, name in enumerate(limb.ik):
        pb = pbs.get(name)
        if pb is not None and pb != handle and pose.visible(pb) and not (handle and _descends(pb, handle)):
            put(pb, wanted[i])
    pole = next((pbs[n] for n in limb.poles if n in pbs and pose.visible(pbs[n])), None)
    shoulder = wanted[0].translation
    if pole is not None and len(wanted) >= 3:
        elbow, end = wanted[1].translation, wanted[limb.end if limb.end is not None else -1].translation
        axis = (end - shoulder).normalized()
        out = _perp(elbow - shoulder, axis)
        if out.length > 1e-6:
            m = pole.matrix.copy()
            m.translation = elbow + out.normalized() * (elbow - shoulder).length
            put(pole, m)
    # The handle: placed so the IK chain's end lands on the limb's end. Then the controls between
    # them (Rigify's foot_spin_ik / heel), and the handle again: a rear paw's heel turns on its
    # own control.
    if handle is not None and limb.end is not None:
        end_pb = pbs.get(limb.ik[limb.end])

        def miss():
            delta = wanted[limb.end] @ end_pb.matrix.inverted_safe()
            q = delta.to_quaternion()
            return delta, delta.translation.length + 0.05 * min(q.angle, 2.0 * math.pi - q.angle)

        def fit(pb):
            """Where pb sits on the end in the rest pose (exact when the end rides it rigidly),
            then moved by the end's error; the closest placement found is kept."""
            delta, error = miss()
            if error < SNAP_TOLERANCE:
                return error
            best = (error, _channels(pb))
            rest = end_pb.bone.matrix_local.inverted_safe() @ pb.bone.matrix_local
            put(pb, wanted[limb.end] @ rest)
            delta, error = miss()
            if error < best[0]:
                best = (error, _channels(pb))
            for _ in range(SNAP_STEPS):
                if error < SNAP_TOLERANCE:
                    break
                put(pb, delta @ pb.matrix)
                delta, error = miss()
                if error >= best[0]:
                    break
                best = (error, _channels(pb))
            if error > best[0]:
                _restore(pb, best[1])
                context.view_layer.update()
            return best[0]

        if fit(handle) > SNAP_TOLERANCE:
            riders = [pbs[n] for n in reversed(limb.run) if n in pbs and n != limb.handle]
            for pb in riders:
                fit(pb)
            if riders:
                fit(handle)
        # Controls riding on the handle (Rigify's toe_ik): the limb's pose too.
        for i, name in enumerate(limb.ik):
            pb = pbs.get(name)
            if pb is not None and pb != handle and pose.visible(pb) and _descends(pb, handle):
                put(pb, wanted[i])
    # The bend plane: turn the pole about shoulder-end, or twist the chain's root about its own
    # axis, until the IK elbow is where the limb's is.
    if len(wanted) >= 3 and limb.end is not None and limb.end >= 2:
        root = pbs.get(limb.ik[0])
        while root is not None and not pose.visible(root):
            root = root.parent  # A hidden IK root (a rear paw's MCH-thigh_ik) turns with thigh_ik.
        mid = pbs.get(limb.ik[1])
        axis = wanted[limb.end].translation - shoulder
        if mid is not None and axis.length > 1e-6:
            axis.normalize()
            if pole is not None:
                p0 = pole.matrix.copy()

                def turn(angle):
                    m = p0.copy()
                    m.translation = shoulder + Quaternion(axis, angle) @ (p0.translation - shoulder)
                    put(pole, m)
            elif root is not None and pose.visible(root) and not root.lock_rotation[1]:
                q0 = pose.rotation(root)

                def turn(angle):
                    pose.set_rotation(root, q0 @ Quaternion((0.0, 1.0, 0.0), angle))
                    written[root] = True
                    context.view_layer.update()
            else:
                turn = None

            def error():
                """Signed angle about the axis from the IK elbow to the limb's."""
                a = _perp(mid.matrix.translation - shoulder, axis)
                b = _perp(wanted[1].translation - shoulder, axis)
                if a.length < 1e-6 or b.length < 1e-6:
                    return 0.0
                angle = a.angle(b)
                return -angle if axis.dot(a.cross(b)) < 0.0 else angle

            if turn is not None:
                _secant(turn, error)
    if handle is not None and limb.end is not None:
        fit(handle)  # The end first: turning the plane can move a rear paw's heel.
    return written


def _secant(turn, error, steps=8, tolerance=1e-4):
    """Find the angle for turn(angle) that zeroes error() (signed, about as the turn), from 0."""
    x0, e0 = 0.0, error()
    if abs(e0) < tolerance:
        return
    best = (abs(e0), x0)
    x1 = e0
    turn(x1)
    e1 = error()
    for _ in range(steps):
        best = min(best, (abs(e1), x1))
        if abs(e1) < tolerance or abs(e1 - e0) < 1e-9:
            break
        x0, e0, x1 = x1, e1, x1 - e1 * (x1 - x0) / (e1 - e0)
        turn(x1)
        e1 = error()
    if abs(e1) > best[0]:
        turn(best[1])


def switch(context, obj, limb, to_ik):
    """Snap the other side onto the limb's current pose and switch it. Returns {pose bone: True}
    written (for keying), or {} if it didn't switch."""
    pbs = obj.pose.bones
    wanted = [pbs[n].matrix.copy() for n in limb.bones]
    written = _snap_ik(context, obj, limb, wanted) if to_ik else _snap_fk(context, obj, limb, wanted)
    if not _set_mode(context, obj, limb, to_ik):
        return {}
    return written


def prepare(context, obj, pb, mirror=False):
    """Before pb is grabbed (or bound): if it's a control of a limb on the other side, switch that
    limb (and, with mirror, its counterpart's). Returns ({pose bone: True} written, [Limb])."""
    limbs = find(obj)
    if not limbs:
        return {}, []
    written, switched = {}, []
    names = [pb.name]
    if mirror:
        other = bpy.utils.flip_name(pb.name)
        if other != pb.name and other in obj.pose.bones:
            names.append(other)
    for name in names:
        limb, side = limb_of(obj, obj.pose.bones[name], limbs)
        if limb is None or limb in switched:
            continue
        to_ik = side == 'IK'
        if ik_active(context, obj, limb) == to_ik:
            continue
        w = switch(context, obj, limb, to_ik)
        if w or ik_active(context, obj, limb) == to_ik:
            written.update(w)
            switched.append(limb)
    return written, switched


def key_switches(context, obj, limbs):
    """Key the switch properties of limbs if Auto Keying is on (before the bones, so re-evaluating
    the frame doesn't put an animated switch back)."""
    if not limbs or not context.scene.tool_settings.use_keyframe_insert_auto:
        return False
    frame = context.scene.frame_current
    for limb in limbs:
        obj.keyframe_insert(data_path=limb.path, frame=frame, group=limb.prop_bone)
    return True
