"""Background tests: the VR IK and FK keep IK limits, IK locks, rotation locks and Limit Rotation /
Limit Location constraints (tests/add_ik_limits_rig.py builds the rig).

blender --background --factory-startup --python tests/test_ik_limits.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import pose  # noqa: E402
import add_ik_limits_rig  # noqa: E402

failures = []
TOL = math.radians(0.5)


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def reset(rig):
    pose.reset(rig.pose.bones)
    bpy.context.view_layer.update()


def head(rig, name):
    return rig.matrix_world @ rig.pose.bones[name].head


def swing_twist(pb):
    """(swing x, twist y, swing z) of a bone's local rotation, as the IK limits measure them."""
    q = pose.rotation(pb)
    t = pose._twist(q, Vector((0, 1, 0)))
    twist = 2.0 * math.atan2(t.y, t.w)
    twist = (twist + math.pi) % (2 * math.pi) - math.pi
    axis, angle = (q @ t.inverted()).to_axis_angle()
    return axis.x * angle, twist, axis.z * angle


def within_ik_limits(pb):
    x, y, z = swing_twist(pb)
    ok = True
    for i, (a, v) in enumerate(zip("xyz", (x, y, z))):
        if getattr(pb, "lock_ik_" + a):
            ok = ok and abs(v) < TOL
        elif getattr(pb, "use_ik_limit_" + a):
            ok = ok and getattr(pb, "ik_min_" + a) - TOL <= v <= getattr(pb, "ik_max_" + a) + TOL
    return ok, tuple(round(math.degrees(v), 1) for v in (x, y, z))


def drag(rig, name, kind, start, end, chain=2, steps=8):
    """Grab bone name at world point start (a controller with no rotation) and move it to end."""
    pb = rig.pose.bones[name]
    follow = pose.Follow(rig, pb, Matrix.Translation(start), kind, grab=start, chain=chain)
    for i in range(1, steps + 1):
        p = Vector(start).lerp(Vector(end), i / steps)
        follow.apply(Matrix.Translation(p))
        bpy.context.view_layer.update()
    return follow


def fk_pose(rig, upper_x, fore_x):
    """Where the hand's head ends up with the upper arm and elbow bent by these angles (degrees)."""
    reset(rig)
    rig.pose.bones["Upper"].rotation_euler = (math.radians(upper_x), 0.0, 0.0)
    rig.pose.bones["Fore"].rotation_quaternion = Quaternion((1, 0, 0), math.radians(fore_x))
    bpy.context.view_layer.update()
    p = head(rig, "Hand")
    reset(rig)
    return p


def test_unit():
    print("== limit_rotation / limit_location")
    rig = bpy.data.objects[add_ik_limits_rig.NAME]
    fore = rig.pose.bones["Fore"]
    q = pose.limit_rotation(fore, Quaternion((1, 0, 0), math.radians(170)), ik=True)
    check(abs(swing_twist_q(q)[0] - math.radians(140)) < TOL, "hinge: 170 degrees clamps to its 140 max")
    q = pose.limit_rotation(fore, Quaternion((0, 0, 1), math.radians(30)), ik=True)
    check(q.angle < TOL, "hinge: a turn about its locked Z axis is dropped")
    q = pose.limit_rotation(fore, Quaternion((0, 0, 1), math.radians(30)))
    check(abs(q.angle - math.radians(30)) < TOL, "without ik, IK limits don't apply (like Blender's FK)")
    hand = rig.pose.bones["Hand"]
    e = pose.limit_rotation(hand, Quaternion((1, 0, 0), math.radians(80))).to_euler()
    check(abs(e.x - math.radians(40)) < TOL, f"Limit Rotation: 80 degrees on X clamps to 40 ({math.degrees(e.x):.1f})")
    slider = rig.pose.bones["Slider"]
    loc = pose.limit_location(slider, (0.1, 0.4, -0.2))
    check((loc - Vector((0.0, 0.15, 0.0))).length < 1e-6, f"Limit Location: slides 0..15 cm on Y only ({tuple(loc)})")


def swing_twist_q(q):
    t = pose._twist(q, Vector((0, 1, 0)))
    twist = 2.0 * math.atan2(t.y, t.w)
    axis, angle = (q @ t.inverted()).to_axis_angle()
    return axis.x * angle, twist, axis.z * angle


def test_ik():
    print("== IK keeps the chain's limits")
    rig = bpy.data.objects[add_ik_limits_rig.NAME]
    pbs = rig.pose.bones
    # A pose inside the limits: the solver should find one that gets there.
    target = fk_pose(rig, 30, 90)
    start = head(rig, "Hand")
    drag(rig, "Hand", 'IK', start, target)
    miss = (head(rig, "Hand") - target).length
    check(miss < 0.002, f"a reachable target is reached ({miss * 1000:.1f} mm)")
    for name in ("Upper", "Fore", "Hand"):
        ok, angles = within_ik_limits(pbs[name])
        check(ok, f"{name} within its IK limits {angles}")
    check(abs(pbs["Upper"].rotation_euler.y) < 1e-6, "Upper's locked Y rotation stays 0")

    # The mirror image needs the elbow bent backwards, which the hinge can't.
    target = fk_pose(rig, -30, -90)
    start = head(rig, "Hand")
    drag(rig, "Hand", 'IK', start, target)
    ok, angles = within_ik_limits(pbs["Fore"])
    check(ok and angles[0] >= -0.5, f"the elbow never bends past its hinge {angles}")
    limited_miss = (head(rig, "Hand") - target).length
    # Same drag with the limits off: the plain solver bends the elbow backwards.
    pbs["Fore"].use_ik_limit_x = pbs["Fore"].lock_ik_z = False
    reset(rig)
    drag(rig, "Hand", 'IK', start, target)
    free_angles = within_ik_limits(pbs["Fore"])[1]
    check(free_angles[0] < -10.0, f"(without its limits the elbow would bend backwards: {free_angles})")
    pbs["Fore"].use_ik_limit_x = pbs["Fore"].lock_ik_z = True
    print(f"    limited reach misses by {limited_miss * 100:.1f} cm")
    reset(rig)


def test_fk_and_moves():
    print("== FK and moves keep Limit constraints and locks")
    rig = bpy.data.objects[add_ik_limits_rig.NAME]
    pbs = rig.pose.bones
    reset(rig)
    # Swing the hand 90 degrees forward by its tip: the constraint stops it at its limit.
    tip = rig.matrix_world @ pbs["Hand"].tail
    hand_head = head(rig, "Hand")
    drag(rig, "Hand", 'SWING', tip, hand_head + Vector((0.0, -0.08, 0.0)))
    e = pbs["Hand"].rotation_quaternion.to_euler()
    check(all(abs(e[i]) <= lim + TOL for i, lim in enumerate(map(math.radians, (40, 60, 30)))),
          f"FK: Hand stays within its Limit Rotation ({tuple(round(math.degrees(v), 1) for v in e)})")
    check(abs(abs(e.x) - math.radians(40)) < TOL, "and stops at the 40 degree limit")
    # Muted constraints don't count.
    con = pbs["Hand"].constraints[0]
    con.mute = True
    reset(rig)
    drag(rig, "Hand", 'SWING', tip, hand_head + Vector((0.0, -0.08, 0.0)))
    e = pbs["Hand"].rotation_quaternion.to_euler()
    check(abs(e.x) > math.radians(60), f"a muted constraint is ignored ({math.degrees(e.x):.1f})")
    con.mute = False
    # FK on the elbow ignores its IK limits, as in Blender.
    reset(rig)
    fore_tip = rig.matrix_world @ pbs["Fore"].tail
    drag(rig, "Fore", 'SWING', fore_tip, head(rig, "Fore") + Vector((0.1, 0.0, 0.05)))
    check(abs(swing_twist(pbs["Fore"])[2]) > math.radians(20), "FK doesn't apply IK limits (Blender doesn't either)")

    # The slider moves only along itself, 0..15 cm, however the hand moves.
    reset(rig)
    s = head(rig, "Slider")
    drag(rig, "Slider", 'RIGID', s, s + Vector((0.2, 0.1, 0.5)))
    loc = pbs["Slider"].location
    check(abs(loc.x) < 1e-6 and abs(loc.z) < 1e-6 and abs(loc.y - 0.15) < 1e-6,
          f"Limit Location: the slider stops at 15 cm along itself ({tuple(round(v, 3) for v in loc)})")
    reset(rig)


def main():
    vr_sculpt.register()
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        add_ik_limits_rig.build(bpy.context, (0.0, 0.0, 0.0))
        test_unit()
        test_ik()
        test_fk_and_moves()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
