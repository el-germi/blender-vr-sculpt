"""Background tests for Pose mode on a production rig: a Rigify human generated here. Only the
controls are drawn and picked (as their custom shapes), the metarig stays out of the rig picker,
grabbing a control of a limb's inactive side switches the limb (IK <-> FK) without moving it,
bones steering the rig's IK move instead of being solved for, and the IK tool bends the FK
controls through Rigify's helper bones.

blender --background --factory-startup --python tests/test_rigify.py
"""

import math
import os
import sys
import time
import traceback

import addon_utils
import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import anim, limbs, pose  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def at(point, rot=None):
    return Matrix.LocRotScale(Vector(point), rot or Quaternion(), None)


def world(rig, name):
    return rig.matrix_world @ rig.pose.bones[name].matrix


def matrices(rig, names):
    return {n: world(rig, n) for n in names}


def same_pose(a, b, tol=2e-3, angle=0.02):
    """Worst (distance, angle) between matched matrices, and whether within tolerance."""
    dist = max((a[n].translation - b[n].translation).length for n in a)
    turn = max(_angle(a[n], b[n]) for n in a)
    return dist, turn, dist < tol and turn < angle


def _angle(a, b):
    t = a.to_quaternion().rotation_difference(b.to_quaternion()).angle
    return min(t, 2.0 * math.pi - t)  # q and -q are the same turn.


def worst(a, b):
    """Per-bone (mm, deg) errors, for messages."""
    return {n: (round((a[n].translation - b[n].translation).length * 1000, 2), round(math.degrees(_angle(a[n], b[n])), 2))
            for n in a}


def set_prop(rig, bone, prop, value):
    """Set a rig property as the UI would (a plain assignment doesn't re-run the drivers)."""
    rig.pose.bones[bone][prop] = value
    rig.update_tag()


def ik_fk(rig, limb_bone):
    return rig.pose.bones[limb_bone]["IK_FK"]


def make_rig(ctx):
    addon_utils.enable("rigify", default_set=True)
    bpy.ops.object.armature_human_metarig_add()
    meta = ctx.active_object
    add_tail(meta)
    ctx.view_layer.objects.active = meta
    bpy.ops.pose.rigify_generate()
    rig = next(o for o in bpy.data.objects if o.type == 'ARMATURE' and o != meta)
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in ctx.view_layer.objects:
        o.select_set(False)
    ctx.view_layer.objects.active = rig
    pose.use_rig(ctx, rig)
    return meta, rig


def grab(ctx, rig, name, tool='FK', fraction=0.5, point=None):
    """A Grab of bone name, pressed at point (default: the bone's middle)."""
    settings = ctx.scene.vr_sculpt
    settings.pose_tool = tool
    pb = rig.pose.bones[name]
    if point is None:
        point = rig.matrix_world @ (pb.head + (pb.tail - pb.head) * fraction)
    return pose.Grab(ctx, settings, rig, pb, fraction, at(point))


def drag(ctx, h, point):
    h.update(ctx, at(point), at(point), 1.0)
    ctx.view_layer.update()


def test_visible(meta, rig):
    print("== what is shown")
    ctx = bpy.context
    names = {o.name for o in pose.rigs(ctx)}
    check(rig.name in names and meta.name not in names, f"rig picker: the rig, not its metarig ({sorted(names)})")
    check(pose.is_metarig(meta) and not pose.is_metarig(rig),
          f"metarig told apart (Rigify's own properties) ({pose.is_metarig(meta)}, {pose.is_metarig(rig)})")
    t = time.perf_counter()
    geo = pose.geometry(rig, fresh=True)
    ms_fresh = (time.perf_counter() - t) * 1000
    time.sleep(pose.CACHE_SECONDS)
    t = time.perf_counter()
    for _ in range(10):
        pose.geometry(rig)
        pose._cache["time"] = 0.0  # Rebuild each frame's world lines, not the layout.
    ms = (time.perf_counter() - t) * 100
    shown = {pb.name for pb in geo.bones}
    check(not any(n.startswith(("ORG-", "MCH-", "DEF-")) for n in shown), "no ORG / MCH / DEF bones")
    check("upper_arm_ik_target.L" not in shown and "VIS_upper_arm_ik_pole.L" not in shown,
          "bones Rigify hides (PoseBone.hide: the unused poles) aren't shown")
    check({"hand_ik.L", "hand_fk.L", "upper_arm_fk.L", "torso", "root"} <= shown, "the controls are")
    check(all(pose.has_shape(rig, pb) for pb in geo.bones), "every control is drawn as its custom shape")
    print(f"       ({len(shown)} controls, {len(geo.coords) // 2} lines; layout + lines {ms_fresh:.1f} ms, "
          f"lines per frame {ms:.2f} ms)")
    check(ms < 2.0, f"per-frame lines cheap enough for a 90 Hz headset ({ms:.2f} ms)")
    # Shapes are placed as Blender draws them: hand_fk's widget, on the hand bone.
    pb = rig.pose.bones["hand_fk.L"]
    coords = geo.lines(pb)
    m = world(rig, "hand_fk.L") @ pose._shape_offset(pb)
    local = [Vector(v[:3]) for v in pose._shape_lines(pb.custom_shape)]
    check(len(coords) == len(local) and all((Vector(c) - m @ v).length < 1e-4 for c, v in zip(coords, local)),
          "a widget is where Blender draws it (bone matrix, shape offset, bone length)")
    # A point on root's widget (a ring on the floor) picks root, away from the root bone itself.
    root = rig.pose.bones["root"]
    coords = geo.lines(root)
    far = max((Vector(c) for c in coords), key=lambda v: (v - world(rig, "root").translation).length)
    hit = pose.nearest_bone(ctx, far, 1.0, rig)
    check(hit is not None and hit[0].name == "root", f"a point on root's ring picks root ({hit and hit[0].name})")


def test_limbs(rig):
    print("== switched limbs found")
    found = {l.prop_bone: l for l in limbs.find(rig)}
    check({"upper_arm_parent.L", "upper_arm_parent.R", "thigh_parent.L", "thigh_parent.R"} <= set(found),
          f"arms and legs ({sorted(found)})")
    arm = found["upper_arm_parent.L"]
    check(arm.fk == ["upper_arm_fk.L", "forearm_fk.L", "hand_fk.L"], f"arm FK side ({arm.fk})")
    check(arm.handle == "hand_ik.L" and arm.ik[arm.end] == "MCH-upper_arm_ik_target.L",
          f"arm IK handle and end ({arm.handle}, {arm.end})")
    leg = found["thigh_parent.L"]
    check(leg.handle == "foot_ik.L" and "foot_spin_ik.L" in leg.run, f"leg IK handle ({leg.handle}, {leg.run})")
    check(leg.side(rig, rig.pose.bones["foot_heel_ik.L"]) == 'IK' and leg.side(rig, rig.pose.bones["toe_fk.L"]) == 'FK',
          "heel is an IK control, toe_fk an FK one")
    handles = limbs.ik_handles(rig)
    check({"hand_ik.L", "foot_ik.L"} <= handles and "upper_arm_fk.L" not in handles, f"IK handles ({sorted(handles)})")


def bend_ik_arm(ctx, rig):
    """Put the left arm's IK somewhere bent and turned, so FK and IK disagree."""
    hand = rig.pose.bones["hand_ik.L"]
    hand.location = (-0.12, 0.15, -0.25)
    hand.rotation_quaternion = Quaternion((0, 0, 1), 0.4) @ Quaternion((1, 0, 0), 0.3)
    ctx.view_layer.update()


ARM_ORG = ["ORG-upper_arm.L", "ORG-forearm.L", "ORG-hand.L"]
LEG_ORG = ["ORG-thigh.L", "ORG-shin.L", "ORG-foot.L", "ORG-toe.L"]


def test_to_fk(rig):
    print("== grabbing an FK control of an IK arm")
    ctx = bpy.context
    bend_ik_arm(ctx, rig)
    check(ik_fk(rig, "upper_arm_parent.L") < 0.5, "the arm starts in IK")
    before = matrices(rig, ARM_ORG)
    h = grab(ctx, rig, "forearm_fk.L")
    check(ik_fk(rig, "upper_arm_parent.L") > 0.5, "the press switches it to FK")
    d, a, ok = same_pose(before, matrices(rig, ARM_ORG))
    check(ok, f"without moving it ({d * 1000:.2f} mm, {math.degrees(a):.2f} deg)")
    d, a, ok = same_pose({n: world(rig, n) for n in ("forearm_fk.L",)}, {"forearm_fk.L": world(rig, "ORG-forearm.L")})
    check(ok, "the FK controls are snapped onto it")
    check(h.changed and h.follow.kind == 'SWING', "the switch is a change (undo step); the forearm swings")
    elbow = world(rig, "ORG-forearm.L").translation
    tip = Vector(h.follow.grab)
    drag(ctx, h, tip + Vector((0.0, -0.1, 0.05)))
    check((world(rig, "ORG-forearm.L").translation - elbow).length < 1e-4 and
          (world(rig, "ORG-hand.L").translation - before["ORG-hand.L"].translation).length > 0.02,
          "dragging bends the arm at the elbow (FK drives it now)")
    h.finish(ctx)


def ik_stretch(rig, value):
    """Rigify's IK chains stretch a little (ik_stretch 0.1), so Blender's IK lands the elbow up to
    ~2 cm off even on a reachable target; without it the snap is exact."""
    old = {}
    for pb in rig.pose.bones:
        if isinstance(value, dict):
            if pb.name in value:
                pb.ik_stretch = value[pb.name]
        elif pb.ik_stretch:
            old[pb.name] = pb.ik_stretch
            pb.ik_stretch = value
    return old


def test_to_ik(rig, pole):
    print(f"== grabbing the IK hand of an FK arm ({'pole' if pole else 'no pole'}, no IK stretch)")
    ctx = bpy.context
    stretch = ik_stretch(rig, 0.0)
    set_prop(rig, "upper_arm_parent.L", "pole_vector", pole)
    set_prop(rig, "upper_arm_parent.L", "IK_FK", 1.0)
    fk = rig.pose.bones
    for n in ("upper_arm_fk.L", "forearm_fk.L", "hand_fk.L"):
        fk[n].scale = (1.0, 1.0, 1.0)  # The FK snap above copied the IK arm's stretch.
    fk["upper_arm_fk.L"].rotation_quaternion = Quaternion((0, 1, 0), 0.5) @ Quaternion((1, 0, 0), -0.6)
    fk["forearm_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), 0.9)
    fk["hand_fk.L"].rotation_quaternion = Quaternion((0, 0, 1), 0.3)
    ctx.view_layer.update()
    check(("upper_arm_ik_target.L" in {pb.name for pb in pose.geometry(rig, fresh=True).bones}) == pole,
          "the pole shows only when it's used")
    before = matrices(rig, ARM_ORG)
    h = grab(ctx, rig, "hand_ik.L", tool='IK')
    check(ik_fk(rig, "upper_arm_parent.L") < 0.5, "the press switches the arm to IK")
    after = matrices(rig, ARM_ORG)
    d, a, ok = same_pose(before, after)
    check(ok, f"without moving it ({worst(before, after)})")
    check(h.follow.kind == 'RIGID', "the IK hand is moved even with the IK tool (the rig solves the arm)")
    start = Vector(h.follow.grab)
    target = start + Vector((0.05, -0.05, 0.08))
    drag(ctx, h, target)
    moved = world(rig, "ORG-hand.L").translation - before["ORG-hand.L"].translation
    check((moved - Vector((0.05, -0.05, 0.08))).length < 2e-3, f"the hand follows ({moved})")
    h.finish(ctx)
    set_prop(rig, "upper_arm_parent.L", "pole_vector", False)
    ik_stretch(rig, stretch)


def test_leg(rig):
    print("== leg: FK -> IK -> FK (Rigify's IK stretch on)")
    ctx = bpy.context
    pbs = rig.pose.bones
    set_prop(rig, "thigh_parent.L", "IK_FK", 1.0)
    pbs["thigh_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), -0.5)
    pbs["shin_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), 0.8)
    pbs["foot_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), 0.3)
    pbs["toe_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), 0.4)
    ctx.view_layer.update()
    before = matrices(rig, LEG_ORG)
    h = grab(ctx, rig, "foot_ik.L")
    after = matrices(rig, LEG_ORG)
    ends = ["ORG-foot.L", "ORG-toe.L"]
    d, a, ok = same_pose({n: before[n] for n in ends}, {n: after[n] for n in ends})
    check(ik_fk(rig, "thigh_parent.L") < 0.5 and ok,
          f"grabbing the IK foot switches to IK, foot and toe in place ({d * 1000:.2f} mm, {math.degrees(a):.2f} deg)")
    d, a, ok = same_pose(before, after, tol=0.03, angle=math.radians(4))
    check(ok, f"the knee close, as close as the rig's stretchy IK lands it ({worst(before, after)})")
    h.finish(ctx)
    pbs["foot_ik.L"].location.z += 0.1
    ctx.view_layer.update()
    before = matrices(rig, LEG_ORG)
    h = grab(ctx, rig, "toe_fk.L")
    d, a, ok = same_pose(before, matrices(rig, LEG_ORG))
    check(ik_fk(rig, "thigh_parent.L") > 0.5 and ok,
          f"grabbing the FK toe switches back in place ({d * 1000:.2f} mm, {math.degrees(a):.2f} deg)")
    h.finish(ctx)


def test_ik_tool_on_fk(rig):
    print("== IK tool on the FK hand: bends the FK arm through the helper bones")
    ctx = bpy.context
    ctx.scene.vr_sculpt.pose_chain = 2
    pbs = rig.pose.bones
    set_prop(rig, "upper_arm_parent.L", "IK_FK", 1.0)
    for n in ("upper_arm_fk.L", "forearm_fk.L", "hand_fk.L"):
        pbs[n].rotation_quaternion = (1, 0, 0, 0)
        pbs[n].scale = (1.0, 1.0, 1.0)
    pbs["forearm_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), 0.4)
    ctx.view_layer.update()
    h = grab(ctx, rig, "hand_fk.L", tool='IK', fraction=0.0)
    check(h.follow.kind == 'IK' and h.follow.parents == ["forearm_fk.L", "upper_arm_fk.L"],
          f"chain: forearm_fk, upper_arm_fk (past MCH-hand_fk) ({h.follow.parents})")
    wrist = world(rig, "hand_fk.L").translation
    shoulder = world(rig, "upper_arm_fk.L").translation
    target = wrist + (shoulder - wrist) * 0.25 + Vector((0.0, -0.05, 0.0))
    start = Vector(h.follow.grab)
    for k in range(1, 4):  # A few frames, as in the headset.
        drag(ctx, h, start + (target - wrist) * (k / 3))
    err = (world(rig, "ORG-hand.L").translation - target).length
    check(err < 3e-3, f"the hand reaches the target ({err * 1000:.2f} mm)")
    check((world(rig, "upper_arm_fk.L").translation - shoulder).length < 1e-4, "the shoulder stays")
    h.finish(ctx)


def test_stand_ins(rig):
    print("== the inactive side is drawn on the limb")
    ctx = bpy.context
    set_prop(rig, "upper_arm_parent.L", "IK_FK", 0.0)
    bend_ik_arm(ctx, rig)
    ctx.view_layer.update()
    geo = pose.geometry(rig, fresh=True)
    fk_names = ("upper_arm_fk.L", "forearm_fk.L", "hand_fk.L")
    check(all(n in geo.moved for n in fk_names) and "hand_ik.L" not in geo.moved,
          f"an IK arm: its FK controls stand in, its IK ones don't ({sorted(n for n in geo.moved if n.endswith('.L'))})")
    pb = rig.pose.bones["forearm_fk.L"]
    far = (world(rig, "forearm_fk.L").translation - world(rig, "ORG-forearm.L").translation).length
    coords = geo.lines(pb)
    m = world(rig, "ORG-forearm.L") @ pose._shape_offset(pb)
    local = [Vector(v[:3]) for v in pose._shape_lines(pb.custom_shape)]
    check(far > 0.02 and all((Vector(c) - m @ v).length < 1e-4 for c, v in zip(coords, local)),
          f"the FK forearm's ring is drawn on the arm ({far * 1000:.0f} mm from where the bone is)")
    hit = pose.nearest_bone(ctx, Vector(coords[0]), 1.0, rig)
    check(hit is not None and hit[0].name == "forearm_fk.L", f"and picked there ({hit and hit[0].name})")
    drawn = Matrix(m)
    h = grab(ctx, rig, "forearm_fk.L", point=Vector(coords[0]))
    d, a, ok = same_pose({"x": drawn}, {"x": world(rig, "forearm_fk.L") @ pose._shape_offset(pb)})
    check(ik_fk(rig, "upper_arm_parent.L") > 0.5 and ok, f"grabbing it switches with the ring staying put ({d * 1000:.2f} mm)")
    h.finish(ctx)
    # An FK arm: hand_ik stands in on the hand.
    rig.pose.bones["forearm_fk.L"].rotation_quaternion = Quaternion((1, 0, 0), 0.6)
    ctx.view_layer.update()
    geo = pose.geometry(rig, fresh=True)
    check("hand_ik.L" in geo.moved and not any(n in geo.moved for n in fk_names), "an FK arm: its IK hand stands in")
    m = geo.moved.get("hand_ik.L")
    hand = world(rig, "ORG-hand.L").translation
    check(m is not None and (rig.matrix_world @ m.translation - hand).length < 1e-4, "on the hand")
    h = grab(ctx, rig, "hand_ik.L")
    err = (world(rig, "hand_ik.L").translation - rig.matrix_world @ m.translation).length
    check(ik_fk(rig, "upper_arm_parent.L") < 0.5 and err < 2e-3, f"grabbing it switches to IK, the hand staying ({err * 1000:.2f} mm)")
    h.finish(ctx)
    set_prop(rig, "upper_arm_parent.L", "IK_FK", 0.5)
    ctx.view_layer.update()
    check(not any(n.endswith(".L") and n.startswith(("upper_arm", "forearm", "hand")) for n in pose.geometry(rig, fresh=True).moved),
          "a limb half way between: drawn where it is")
    set_prop(rig, "upper_arm_parent.L", "IK_FK", 0.0)
    ctx.view_layer.update()


def test_curl(rig):
    print("== curl: pushing a finger master closes the finger")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    name = "f_index.01_master.L"
    curls = pose.curl_bones(rig)
    check(name in curls and "forearm_fk.L" not in curls, f"finger masters found ({sorted(c for c in curls if c.endswith('.L'))})")
    pbs = rig.pose.bones
    pb, drv = pbs[name], pbs["MCH-f_index.02_drv.L"]

    def push(amount, on=True):
        """Grab the master, push it in along itself by amount bone lengths; (Y scale, joint angle)."""
        settings.pose_curl = on
        pb.scale = (1.0, 1.0, 1.0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        ctx.view_layer.update()
        head, tail = rig.matrix_world @ pb.head, rig.matrix_world @ pb.tail
        hand = head + (tail - head) * 0.5
        push.length = (tail - head).length
        h = grab(ctx, rig, name, point=hand)
        drag(ctx, h, hand - (tail - head) * amount)
        h.finish(ctx)
        return pb.scale.y, drv.matrix_basis.to_euler().x, h

    sy, turn, h = push(0.85)
    length = push.length  # unscaled
    moved = 0.85 * length / min(length, pose.CURL_REACH)  # a long master: CURL_REACH counts as its length
    want = max(1.0 - pose.CURL_RATE * (moved - pose.CURL_DEAD_ZONE), pose.CURL_MIN)
    check(h.follow.curl is not None and abs(sy - want) < 0.01, f"pushed in, it shortens ({sy:.3f}, want {want:.3f})")
    check(abs(turn - (1.0 - sy) * math.pi) < 0.02 and turn > 1.0, f"and Rigify curls the finger ({math.degrees(turn):.0f} deg)")
    check(pb.rotation_quaternion.angle < 0.02, "without turning the master")
    sy, turn, h = push(-0.5)
    check(sy > 1.1, f"pulled out, it opens ({sy:.3f})")
    sy, turn, h = push(0.05)
    check(abs(sy - 1.0) < 1e-6, "a small wobble doesn't curl (dead zone)")
    sy, turn, h = push(0.85, on=False)
    check(abs(sy - 1.0) < 1e-6 and h.follow.curl is None, "Curl off: only turns")
    settings.pose_curl = True
    check(pose.free(pb) and pose.grab_kind(rig, pb, 'FK', 0.05, curl=True) == 'SWING',
          "grabbed near its head, a curling master still turns (doesn't move)")
    h = grab(ctx, rig, "forearm_fk.L")
    check(h.follow.curl is None, "a bone nothing curls by isn't scaled")
    h.finish(ctx)
    pb.scale = (1.0, 1.0, 1.0)
    ctx.view_layer.update()


def test_tweaks(rig):
    print("== tweak controls (twist only) move wherever they're grabbed")
    ctx = bpy.context
    pbs = rig.pose.bones
    names = [n for n in ("tweak_tongue.003", "tweak_tongue", "forearm_tweak.L", "tweak_spine.001") if n in pbs]
    check(bool(names), f"the rig has tweak controls ({names})")
    for name in names:
        pb = pbs[name]
        if not pose.visible(pb):
            continue
        check(not pose.can_swing(pb) and pose.free(pb), f"{name}: can't swing, can move ({tuple(pb.lock_rotation)})")
        before = world(rig, name).translation.copy()
        h = grab(ctx, rig, name, fraction=0.9)
        check(h.follow.kind == 'RIGID', f"{name}: grabbed at its far end, it's moved ({h.follow.kind})")
        start = Vector(h.follow.grab)
        drag(ctx, h, start + Vector((0.0, -0.03, 0.01)))
        moved = world(rig, name).translation - before
        check((moved - Vector((0.0, -0.03, 0.01))).length < 1e-3, f"{name}: follows the hand ({moved})")
        h.finish(ctx)
        pb.location = (0.0, 0.0, 0.0)
        ctx.view_layer.update()
    check(pose.grab_kind(rig, pbs["forearm_fk.L"], 'FK', 0.9) == 'SWING', "an FK control still swings")


def turned(a, b):
    return math.degrees(_angle(a, b))


def open_gate(ctx, h, point):
    """A hair's move: the grab starts following (a turn alone opens it only past GATE_ANGLE)."""
    h.update(ctx, at(point + Vector((1e-6, 0.0, 0.0))), at(point + Vector((1e-6, 0.0, 0.0))), 1.0)
    ctx.view_layer.update()


def test_wrist(rig):
    print("== turning the controller in place turns a swung bone")
    ctx = bpy.context
    set_prop(rig, "upper_arm_parent.L", "IK_FK", 1.0)
    ctx.view_layer.update()
    name = "forearm_fk.L"
    rig.pose.bones[name].rotation_quaternion = (1, 0, 0, 0)
    ctx.view_layer.update()
    before = world(rig, name)
    h = grab(ctx, rig, name)
    check(h.follow.kind == 'SWING', f"the FK forearm swings ({h.follow.kind})")
    point = Vector(h.follow.grab)
    open_gate(ctx, h, point)
    m = before.to_3x3().normalized()
    for i in range(10):  # 0.4 rad about the bone's X, the hand staying put
        q = Quaternion(m.col[0], 0.04 * (i + 1))
        h.update(ctx, at(point, q), at(point, q), 1.0)
        ctx.view_layer.update()
    after = world(rig, name)
    h.finish(ctx)
    check(abs(turned(before, after) - math.degrees(0.4)) < 1.0 and (after.translation - before.translation).length < 1e-4,
          f"it turns with the controller about its head ({turned(before, after):.1f} deg, want {math.degrees(0.4):.1f})")
    rig.pose.bones[name].rotation_quaternion = (1, 0, 0, 0)
    ctx.view_layer.update()
    before = world(rig, name)
    h = grab(ctx, rig, name)
    point = Vector(h.follow.grab)
    head = before.translation
    axis = before.to_3x3().normalized().col[2]
    for i in range(10):  # the arm swings the wrist along: hand and controller turn together
        q = Quaternion(axis, 0.03 * (i + 1))
        p = head + q @ (point - head)
        h.update(ctx, at(p, q), at(p, q), 1.0)
        ctx.view_layer.update()
    after = world(rig, name)
    h.finish(ctx)
    check(abs(turned(before, after) - math.degrees(0.3)) < 1.0,
          f"swinging the arm (the wrist turning along) isn't counted twice ({turned(before, after):.1f} deg, want 17.2)")
    rig.pose.bones[name].rotation_quaternion = (1, 0, 0, 0)
    ctx.view_layer.update()


def test_gear(rig):
    print("== a limb's settings gear turns the limb from its root")
    ctx = bpy.context
    pbs = rig.pose.bones
    set_prop(rig, "upper_arm_parent.L", "pole_vector", False)  # the IK root's twist sets the elbow
    for value, root in ((1.0, "upper_arm_fk.L"), (0.0, "upper_arm_ik.L")):
        set_prop(rig, "upper_arm_parent.L", "IK_FK", value)
        pose.reset([pbs[root]])  # its locked channels at rest: a turn about its axis is all Y
        ctx.view_layer.update()
        before = world(rig, root)
        q_before = pose.rotation(pbs[root])
        forearm = world(rig, "ORG-forearm.L")
        h = grab(ctx, rig, "upper_arm_parent.L", fraction=0.2)
        check(h.follow.kind == 'TURN' and h.follow.bone_name == root,
              f"IK_FK {value}: the gear grabs {root} ({h.follow.bone_name}, {h.follow.kind})")
        point = Vector(h.follow.grab)
        open_gate(ctx, h, point)
        axis = before.to_3x3().normalized().col[1 if value == 0.0 else 0]  # the IK root only twists
        for i in range(8):
            q = Quaternion(axis, 0.05 * (i + 1))
            p = point + Vector((0.05, 0.0, 0.0)) * i
            h.update(ctx, at(p, q), at(p, q), 1.0)
            ctx.view_layer.update()
        after = world(rig, root)
        moved = turned(forearm, world(rig, "ORG-forearm.L"))
        h.finish(ctx)
        angle = math.degrees(_angle(q_before.to_matrix().to_4x4(), pose.rotation(pbs[root]).to_matrix().to_4x4()))
        check(abs(angle - math.degrees(0.4)) < 0.5 and (after.translation - before.translation).length < 1e-4,
              f"IK_FK {value}: it turns with the controller, the hand's moves ignored ({angle:.1f} deg)")
        check(moved > 5.0, f"IK_FK {value}: the arm goes along (the forearm turns {moved:.1f} deg)")
        pose.reset([pbs[root]])
        ctx.view_layer.update()
    check(limbs.gear_root(ctx, rig, pbs["forearm_fk.L"]) is None, "an ordinary control isn't a gear")


def add_tail(meta):
    """Three tail bones on the metarig's hips, a Rigify basic tail curling about X only."""
    ctx = bpy.context
    ctx.view_layer.objects.active = meta
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = meta.data.edit_bones
    parent, head = ebs["spine"], Vector((0.0, 0.12, 1.0))
    for i, name in enumerate(("tail", "tail.001", "tail.002")):
        eb = ebs.new(name)
        eb.head, eb.tail = head, head + Vector((0.0, 0.18, -0.04))
        eb.roll = 0.0
        eb.parent = parent
        eb.use_connect = i > 0
        parent, head = eb, eb.tail
    bpy.ops.object.mode_set(mode='POSE')
    pb = meta.pose.bones["tail"]
    pb.rigify_type = "spines.basic_tail"
    if hasattr(pb.rigify_parameters, "copy_rotation_axes"):
        pb.rigify_parameters.copy_rotation_axes = (True, False, False)
    bpy.ops.object.mode_set(mode='OBJECT')


def shape_point(rig, name, frac=0.75):
    """A point of the bone's drawn shape (world), near frac along the bone."""
    pb = rig.pose.bones[name]
    lines = pose.geometry(rig, fresh=True).lines(pb)
    target = rig.matrix_world @ (pb.head + (pb.tail - pb.head) * frac)
    return min((Vector(c) for c in lines), key=lambda c: (c - target).length)


def test_tail(rig):
    print("== Rigify tail: segments land under the hand, the master's ring (drawn at the tip) follows it")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.pose_tool = 'FK'
    pbs = rig.pose.bones
    check(all(n in pbs for n in ("tail_master", "tail", "tail.001")), "the tail is generated")
    if "tail_master" not in pbs:
        return
    master = pbs["tail_master"]
    shape_on = master.custom_shape_transform
    check(shape_on is not None and shape_on != master, f"the master is drawn on another bone ({shape_on and shape_on.name})")
    master.rotation_quaternion = Quaternion((1, 0, 0), 0.4)  # the segments copy this turn
    ctx.view_layer.update()
    # A segment: what its constraint copies is taken out, so the grabbed point stays under the hand.
    name = "tail.001"
    p = shape_point(rig, name)
    hit = pose.nearest_bone(ctx, p, 1.0, rig)
    h = pose.Grab(ctx, settings, rig, hit[0], hit[1], at(p))
    check(hit[0].name == name and h.follow.offset is not None,
          f"{name}: its copied turn is known ({hit[0].name}, {h.follow.offset and h.follow.offset[0]})")
    local = world(rig, name).inverted() @ p
    hand = p + Vector((0.06, 0.0, 0.03))
    for i in range(10):
        drag(ctx, h, p + (hand - p) * (i + 1) / 10)
    head = world(rig, name).translation
    now = world(rig, name) @ local
    err = math.degrees((now - head).angle(hand - head))
    h.finish(ctx)
    check(err < 0.5, f"{name}: the grabbed point points at the hand ({err:.2f} deg off)")
    for n in ("tail", "tail.001", "tail.002"):
        pbs[n].rotation_quaternion = (1, 0, 0, 0)
    ctx.view_layer.update()
    # The master: where its ring would be with the master turned further, reached by dragging.
    p = shape_point(rig, "tail_master", 0.5)
    hit = pose.nearest_bone(ctx, p, 1.0, rig)
    check(hit is not None and hit[0] == master, f"the ring at the tip picks the master ({hit and hit[0].name})")
    ring = shape_on.matrix.inverted() @ (rig.matrix_world.inverted() @ p)
    q0 = master.rotation_quaternion.copy()
    master.rotation_quaternion = Quaternion((1, 0, 0), 0.75)
    ctx.view_layer.update()
    goal = rig.matrix_world @ shape_on.matrix @ ring
    master.rotation_quaternion = q0
    ctx.view_layer.update()
    h = pose.Grab(ctx, settings, rig, master, hit[1], at(p))
    path = []
    for i in range(30):
        drag(ctx, h, p + (goal - p) * min((i + 1) / 10, 1.0))
        path.append(rig.matrix_world @ shape_on.matrix @ ring)
    h.finish(ctx)
    check(h.follow.drive is not None, "the master is driven by its drawn ring")
    end = (path[-1] - goal).length
    wobble = max((path[i] - path[i - 1]).length for i in range(20, 30))
    jump = max((path[i] - path[i - 1]).length for i in range(1, 30))
    span = (goal - p).length
    check(end < 0.03 * span, f"the ring reaches the hand ({end * 1000:.1f} mm off, {span * 1000:.0f} mm dragged)")
    check(wobble < 1e-3 * span + 1e-5 and jump < 0.35 * span,
          f"without overshooting or shaking (settled {wobble * 1000:.3f} mm, largest step {jump * 1000:.0f} mm)")
    angle = master.rotation_quaternion.rotation_difference(Quaternion((1, 0, 0), 0.75)).angle
    check(min(angle, 2 * math.pi - angle) < 0.05,
          f"the master turned as far as the ring shows ({math.degrees(master.rotation_quaternion.angle):.1f} deg)")
    # Turning the controller in place turns it too.
    master.rotation_quaternion = (1, 0, 0, 0)
    ctx.view_layer.update()
    p = shape_point(rig, "tail_master", 0.5)
    h = pose.Grab(ctx, settings, rig, master, 0.5, at(p))
    open_gate(ctx, h, p)
    axis = (rig.matrix_world @ master.matrix).to_3x3().normalized().col[0]
    q = Quaternion()
    for i in range(15):
        q = Quaternion(axis, 0.03 * min(i + 1, 10))
        h.update(ctx, at(p, q), at(p, q), 1.0)
        ctx.view_layer.update()
    weight = h.follow.turn_weight
    h.finish(ctx)
    a = master.rotation_quaternion.angle
    want = 0.3 * weight  # the ring is far from the master's head: the wrist's turn is faded
    check(weight < 1.0 and abs(a - want) < 0.5 * want,
          f"turning the controller in place curls it, faded far from the joint ({math.degrees(a):.1f} deg, want {math.degrees(want):.1f})")
    master.rotation_quaternion = (1, 0, 0, 0)
    ctx.view_layer.update()


def test_keys(rig):
    print("== auto key keys the switch")
    ctx = bpy.context
    scene = ctx.scene
    scene.tool_settings.use_keyframe_insert_auto = True
    scene.frame_set(5)
    pbs = rig.pose.bones
    set_prop(rig, "upper_arm_parent.R", "IK_FK", 0.0)
    pbs["upper_arm_parent.R"].keyframe_insert('["IK_FK"]', frame=1)  # Animated: frame changes re-apply it.
    ctx.view_layer.update()
    h = grab(ctx, rig, "forearm_fk.R")
    tip = Vector(h.follow.grab)
    drag(ctx, h, tip + Vector((0.0, -0.1, 0.0)))
    h.finish(ctx)
    check(ik_fk(rig, "upper_arm_parent.R") > 0.5, "still FK after the frame is re-evaluated")
    curves = {fc.data_path: fc for fc in anim.fcurves(rig)}
    fc = curves.get('pose.bones["upper_arm_parent.R"]["IK_FK"]')
    check(fc is not None and any(abs(k.co[0] - 5) < 0.5 and k.co[1] > 0.5 for k in fc.keyframe_points),
          "IK_FK keyed at FK on this frame")
    check('pose.bones["upper_arm_fk.R"].rotation_quaternion' in curves, "and the snapped FK controls")
    scene.tool_settings.use_keyframe_insert_auto = False


def main():
    vr_sculpt.register()
    try:
        ctx = bpy.context
        ctx.scene.vr_sculpt.grab_delay = 0.0
        ctx.scene.vr_sculpt.grab_deadzone = 0.0
        meta, rig = make_rig(ctx)
        test_visible(meta, rig)
        test_limbs(rig)
        test_to_fk(rig)
        test_to_ik(rig, pole=False)
        test_to_ik(rig, pole=True)
        test_leg(rig)
        test_ik_tool_on_fk(rig)
        test_stand_ins(rig)
        test_curl(rig)
        test_tweaks(rig)
        test_wrist(rig)
        test_gear(rig)
        test_tail(rig)
        test_keys(rig)
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


if __name__ == "__main__":
    main()
