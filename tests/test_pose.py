"""Background tests for Pose mode and puppeteering on a small humanoid rig (hips, spine, two
three-bone arms): bone picking, FK swing and twist, moving free bones, IK reach, X mirror,
rotation modes and locks, keying selected bones, bone motion paths, onion skin keeping an
unkeyed pose, mode switching, the hand panel rows, moving objects without a rig, and puppet
bindings with a recorded take.

blender --background --factory-startup --python tests/test_pose.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import anim, menus, onion, paths, pose, puppet, vr_ui  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=1e-3):
    return (Vector(a) - Vector(b)).length < tol


def at(point, rot=None):
    """A tip pose at point."""
    return Matrix.LocRotScale(Vector(point), rot or Quaternion(), None)


def world_head(rig, name):
    return rig.matrix_world @ rig.pose.bones[name].head


def world_tail(rig, name):
    return rig.matrix_world @ rig.pose.bones[name].tail


class FakeSS:
    """Grip poses for the left and right controller, and the headset."""
    viewer_scale = 1.0

    def __init__(self):
        self.grips = [at((-0.3, -0.3, 1.0)), at((0.3, -0.3, 1.0))]
        self.viewer_pose_location = Vector((0.0, -0.6, 1.6))
        self.viewer_pose_rotation = Quaternion()

    def controller_grip_location_get(self, context, i):
        return self.grips[i].translation

    def controller_grip_rotation_get(self, context, i):
        return self.grips[i].to_quaternion()


def make_rig(ctx):
    """Hips (free root), Spine, and UpperArm / Forearm / Hand .L along +X (symmetrized to .R), at the
    origin; plus a mesh deformed by it, the active object."""
    data = bpy.data.armatures.new("Puppet")
    rig = bpy.data.objects.new("Puppet", data)
    ctx.scene.collection.objects.link(rig)
    ctx.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    eb = data.edit_bones
    hips = eb.new("Hips")
    hips.head, hips.tail = (0, 0, 1.0), (0, 0, 1.1)
    spine = eb.new("Spine")
    spine.head, spine.tail = (0, 0, 1.1), (0, 0, 1.4)
    spine.parent, spine.use_connect = hips, True
    prev = spine
    for name, a, b in (("UpperArm.L", 0.1, 0.4), ("Forearm.L", 0.4, 0.7), ("Hand.L", 0.7, 0.8)):
        bone = eb.new(name)
        bone.head, bone.tail = (a, 0, 1.4), (b, 0, 1.4)
        bone.parent = prev
        bone.use_connect = name != "UpperArm.L"
        prev = bone
    for bone in eb:
        bone.select = bone.select_head = bone.select_tail = bone.name.endswith(".L")
    bpy.ops.armature.symmetrize()
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0.0, 0.0, 1.2))
    body = ctx.active_object
    body.name = "Body"
    mod = body.modifiers.new("Armature", 'ARMATURE')
    mod.object = rig
    return rig, body


def grab(ctx, rig, point, tool='FK'):
    settings = ctx.scene.vr_sculpt
    settings.pose_tool = tool
    handler = pose.pick(ctx, settings, at(point), 1.0)
    return handler


def drag(ctx, handler, tip_pose):
    handler.update(ctx, tip_pose, tip_pose, 1.0)
    ctx.view_layer.update()


def test_enter(rig, body):
    print("== entering and leaving Pose mode")
    ctx = bpy.context
    check(sorted(b.name for b in rig.pose.bones) == sorted(
        ["Hips", "Spine", "UpperArm.L", "Forearm.L", "Hand.L", "UpperArm.R", "Forearm.R", "Hand.R"]),
        "test rig symmetrized")
    ctx.view_layer.objects.active = body
    menus.set_mode(ctx, 'POSE')
    check(ctx.view_layer.objects.active == rig and rig.mode == 'POSE', "Pose mode poses the active mesh's rig")
    menus.set_mode(ctx, 'MODEL')
    check(rig.mode == 'OBJECT' and ctx.view_layer.objects.active == body,
          "leaving Pose mode: rig back in Object Mode, the mesh active again")
    menus.set_mode(ctx, 'POSE')
    check(pose.rig(ctx) == rig, "and back in")


def test_pick(rig):
    print("== picking bones")
    ctx = bpy.context
    hit = pose.nearest_bone(ctx, (0.55, 0.01, 1.4), 1.0)
    check(hit is not None and hit[0].name == "Forearm.L" and abs(hit[1] - 0.5) < 0.05,
          f"tip on the forearm picks it, halfway ({hit and (hit[0].name, round(hit[1], 2))})")
    check(pose.nearest_bone(ctx, (0.55, 0.2, 1.4), 1.0) is None, "nothing out of reach")
    hit = pose.nearest_bone(ctx, (-0.55, 0.0, 1.4), 1.0)
    check(hit is not None and hit[0].name == "Forearm.R", "the other side")
    check(pose.engaged(ctx, Vector((0.0, 0.0, 1.3)), 1.0), "touching the spine engages the tip")


def test_fk(rig):
    print("== FK")
    ctx = bpy.context
    h = grab(ctx, rig, (0.67, 0.0, 1.4))
    check(h is not None and h.follow.kind == 'SWING' and h.follow.bone_name == "Forearm.L",
          "grabbing near the forearm's tail swings it")
    check(rig.pose.bones["Forearm.L"].select and rig.data.bones.active.name == "Forearm.L", "and selects it")
    drag(ctx, h, at((0.4, 0.0, 1.13)))
    check(near(world_tail(rig, "Forearm.L"), (0.4, 0.0, 1.1)), f"forearm points at the tip ({world_tail(rig, 'Forearm.L')})")
    check(near(world_head(rig, "Forearm.L"), (0.4, 0.0, 1.4)), "its head stays at the elbow")
    check(near(world_tail(rig, "Hand.L"), (0.4, 0.0, 1.0)), "the hand comes along")
    h.finish(ctx)
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()

    # Twist: roll the controller 30 degrees about the bone's axis without moving it.
    h = grab(ctx, rig, (0.67, 0.0, 1.4))
    z0 = (rig.matrix_world @ rig.pose.bones["Forearm.L"].matrix).col[2].xyz.normalized()
    drag(ctx, h, at((0.67, 0.0, 1.4), Quaternion((1, 0, 0), math.radians(30))))
    m = rig.matrix_world @ rig.pose.bones["Forearm.L"].matrix
    angle = math.degrees(z0.angle(m.col[2].xyz.normalized()))
    check(abs(angle - 30) < 0.5, f"twisting the controller rolls the bone ({angle:.1f} deg)")
    check(near(world_tail(rig, "Forearm.L"), (0.7, 0.0, 1.4)), "without swinging it")
    h.finish(ctx)
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()

    # A free bone grabbed near its head moves.
    h = grab(ctx, rig, (0.0, 0.0, 1.01))
    check(h.follow.kind == 'RIGID' and h.follow.bone_name == "Hips", "the hips near their head move rigidly")
    drag(ctx, h, at((0.1, 0.0, 1.01)))
    check(near(world_head(rig, "Hips"), (0.1, 0.0, 1.0)), "hips moved")
    check(near(world_tail(rig, "Hand.L"), (0.9, 0.0, 1.4)), "the whole body with them")
    h.finish(ctx)
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()


def test_ik(rig):
    print("== IK")
    ctx = bpy.context
    ctx.scene.vr_sculpt.pose_chain = 2
    h = grab(ctx, rig, (0.75, 0.0, 1.4), 'IK')
    check(h.follow.kind == 'IK' and h.follow.parents == ["Forearm.L", "UpperArm.L"], "IK chain: forearm, upper arm")
    target = Vector((0.6, 0.1, 1.2))
    drag(ctx, h, at(Vector((0.75, 0.0, 1.4)) + (target - Vector((0.7, 0.0, 1.4)))))
    err = (world_head(rig, "Hand.L") - target).length
    check(err < 2e-3, f"the hand reaches the target ({err * 1000:.2f} mm)")
    check(near(world_head(rig, "UpperArm.L"), (0.1, 0.0, 1.4)), "the shoulder stays put")
    hand_y = (rig.matrix_world @ rig.pose.bones["Hand.L"].matrix).col[1].xyz.normalized()
    check(hand_y.angle(Vector((1, 0, 0))) < 0.01, "the hand keeps its orientation (controller didn't turn)")
    check(near(world_tail(rig, "Hand.R"), (-0.8, 0.0, 1.4)), "the other arm is untouched")
    # Out of reach: the arm points straight at it.
    far = Vector((0.1, 0.0, 0.2))
    drag(ctx, h, at(Vector((0.75, 0.0, 1.4)) + (far - Vector((0.7, 0.0, 1.4)))))
    d = (world_head(rig, "Hand.L") - world_head(rig, "UpperArm.L")).normalized()
    check(d.angle(Vector((0, 0, -1))) < 0.02, "out of reach: the chain stretches toward the target")
    h.finish(ctx)
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()


def test_mirror_and_locks(rig):
    print("== mirror, rotation modes and locks")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.pose_mirror = True
    h = grab(ctx, rig, (0.67, 0.0, 1.4))
    drag(ctx, h, at((0.5, 0.2, 1.2)))
    a, b = world_tail(rig, "Forearm.L"), world_tail(rig, "Forearm.R")
    check(near(b, (-a.x, a.y, a.z)), f"Mirror X poses the other forearm mirrored ({a} / {b})")
    h.finish(ctx)
    check(set(h.written_names()) >= {"Forearm.L", "Forearm.R"}, "both are reported for keying")
    settings.pose_mirror = False
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()

    pb = rig.pose.bones["Forearm.L"]
    pb.rotation_mode = 'XYZ'
    pb.lock_rotation[2] = True
    h = grab(ctx, rig, (0.67, 0.0, 1.4))
    drag(ctx, h, at((0.5, 0.2, 1.2)))
    check(abs(pb.rotation_euler.z) < 1e-6 and abs(pb.rotation_euler.x) + abs(pb.rotation_euler.y) > 1e-3,
          f"Euler bone posed, locked Z kept ({tuple(round(v, 3) for v in pb.rotation_euler)})")
    h.finish(ctx)
    pb.lock_rotation[2] = False
    pb.rotation_mode = 'QUATERNION'
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()


def test_keys(rig):
    print("== keying bones")
    ctx = bpy.context
    scene = ctx.scene
    tool = scene.tool_settings
    tool.use_keyframe_insert_auto = True
    scene.frame_set(10)
    h = grab(ctx, rig, (0.67, 0.0, 1.4))
    drag(ctx, h, at((0.5, 0.0, 1.2)))
    h.finish(ctx)
    paths_keyed = {fc.data_path for fc in anim.fcurves(rig)}
    check('pose.bones["Forearm.L"].rotation_quaternion' in paths_keyed, "auto key keys the posed bone")
    check(not any(p.startswith('pose.bones["Hand.L"]') for p in paths_keyed), "and only it")
    check(anim.key_frames([rig]) == [10.0], "timeline ticks: the selected bone's keys")
    pose.select_bone(rig, rig.pose.bones["Spine"])
    check(anim.key_frames([rig]) == [], "another bone selected: its (no) keys only")
    scene.frame_set(20)
    n = anim.key_selection(ctx)
    paths_keyed = {fc.data_path for fc in anim.fcurves(rig)}
    check(n == 1 and {'pose.bones["Spine"].location', 'pose.bones["Spine"].rotation_quaternion',
                      'pose.bones["Spine"].scale'} <= paths_keyed, "Key keys the selected bones' transforms")
    check(anim.delete_selection_keys(ctx) == 1 and anim.key_frames([rig]) == [], "Del key removes them")
    tool.use_keyframe_insert_auto = False

    # Motion path of the keyed forearm.
    scene.vr_sculpt.show_paths = True
    pose.select_bone(rig, rig.pose.bones["Forearm.L"])
    paths.ensure(ctx, force=True)
    pb = rig.pose.bones["Forearm.L"]
    check(pb.motion_path is not None and len(pb.motion_path.points) > 1, "the selected keyed bone gets a path")
    markers = paths.bone_markers(rig, pb)
    check([f for f, _co in markers] == [10], f"with a marker on its key ({[f for f, _ in markers]})")
    check(rig.motion_path is None, "the posed rig itself gets none")
    scene.vr_sculpt.show_paths = False

    # Onion skin must not undo an unkeyed pose.
    scene.vr_sculpt.onion = True
    scene.vr_sculpt.onion_mode = 'FRAMES'
    scene.frame_set(10)
    pb.rotation_quaternion = Quaternion((0, 0, 1), 0.7)
    ctx.view_layer.update()
    onion.update(ctx, force=True)
    check(abs(pb.rotation_quaternion.angle - 0.7) < 1e-4, "computing ghosts keeps the unkeyed pose")
    check(onion.ghost_count() > 0, f"and makes ghosts of the deformed mesh ({onion.ghost_count()})")
    scene.vr_sculpt.onion = False
    onion.update(ctx)


def test_rows(rig):
    print("== hand panel")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.pose_tool = 'IK'
    widgets, _h = menus.layout(ctx)
    ids = {w.id for w in widgets}
    first = {"mode_sculpt", "mode_pose", "mode_tools", "pose_rig", "pose_fk", "pose_ik", "pose_mirror", "pose_bones",
             "pose_chain"}
    later = {"pose_reset", "pose_reset_all", "bind_left", "bind_right", "bind_head", "puppet_rec"}
    check(first <= ids and not (later & ids), f"Pose: the first level (missing {sorted(first - ids)})")
    vr_ui.find(widgets, "more").on_press()
    widgets, _h = menus.layout(ctx)
    ids = {w.id for w in widgets}
    check(first | later <= ids, f"Pose with More (missing {sorted((first | later) - ids)})")
    tabs = [w for w in widgets if w.id.startswith("mode_")]
    check(len(tabs) == 7 and len({round(w.rect[0], 5) for w in tabs}) == 1 and
          len({round(w.rect[1], 5) for w in tabs}) == 7, "seven mode icons in one column (the rail)")
    vr_ui.find(widgets, "pose_fk").on_press()
    check(settings.pose_tool == 'FK', "FK button")
    pb = rig.pose.bones["Spine"]
    pb.rotation_quaternion = Quaternion((1, 0, 0), 0.3)
    pose.select_bone(rig, pb)
    vr_ui.find(widgets, "pose_reset").on_press()
    check(pb.rotation_quaternion.angle < 1e-6, "Reset bone")
    pose.choose_rig(ctx, "")
    widgets, _h = menus.layout(ctx)
    check(rig.mode == 'OBJECT' and vr_ui.find(widgets, "pose_fk") is None and vr_ui.find(widgets, "bind_left"),
          "rig picker: Objects (no rig) leaves Pose Mode, Bind stays")
    menus.reset()


def test_objects(rig, body):
    print("== moving objects without a rig")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    bpy.ops.mesh.primitive_cube_add(size=0.2, location=(1.0, 0.0, 0.5))
    prop = ctx.active_object
    prop.select_set(False)
    ctx.view_layer.objects.active = body
    h = pose.pick(ctx, settings, at((1.0, 0.0, 0.55)), 1.0)
    check(h is not None and h.follow.bone_name is None, "the trigger grabs the touched object")
    drag(ctx, h, at((1.2, 0.0, 0.55)))
    check(near(prop.matrix_world.translation, (1.2, 0.0, 0.5)), "and moves it")
    h.finish(ctx)
    return prop


def test_drag_gate(rig, prop):
    print("== grab threshold (a press selects without nudging)")
    import time
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.grab_deadzone, settings.grab_delay = 0.015, 0.0
    from vr_sculpt import interactions
    start = prop.matrix_world.translation.copy()
    tip = start + Vector((0.0, 0.0, 0.05))
    h = interactions.MoveObject(ctx, prop, at(tip))
    check(ctx.view_layer.objects.active == prop, "the press selects the object")
    for d in (0.004, -0.006, 0.008):  # Jitter of a trigger press.
        drag(ctx, h, at(tip + Vector((d, 0.0, 0.0)), Quaternion((0, 0, 1), math.radians(3))))
    check(near(prop.matrix_world.translation, start, 1e-6) and not h.changed, "jitter inside the threshold: it stays")
    drag(ctx, h, at(tip + Vector((0.012, 0.0, 0.0))))
    drag(ctx, h, at(tip + Vector((0.020, 0.0, 0.0))))
    moved = prop.matrix_world.translation - start
    check(h.changed and abs(moved.x - 0.008) < 1e-4,
          f"past it, it moves from the last pose inside, without a jump ({moved.x * 1000:.1f} mm for 20)")
    drag(ctx, h, at(tip + Vector((0.120, 0.0, 0.0))))
    moved = prop.matrix_world.translation - start
    check(abs(moved.x - 0.108) < 1e-4, "then follows the hand 1:1")
    prop.matrix_world.translation = start

    h = interactions.MoveObject(ctx, prop, at(tip))
    drag(ctx, h, at(tip + Vector((0.3, 0.0, 0.0))))
    check(near(prop.matrix_world.translation, start + Vector((0.3, 0.0, 0.0))), "a fast move isn't lost")
    prop.matrix_world.translation = start

    h = interactions.MoveObject(ctx, prop, at(tip))
    drag(ctx, h, at(tip, Quaternion((0, 0, 1), math.radians(20))))
    check(h.changed, "turning the controller past a few degrees starts it too")
    prop.matrix_world = Matrix.Translation(start)

    settings.grab_delay = 0.05
    h = interactions.MoveObject(ctx, prop, at(tip))
    drag(ctx, h, at(tip + Vector((0.003, 0.0, 0.0))))
    check(not h.changed, "a short hold doesn't")
    time.sleep(0.06)
    drag(ctx, h, at(tip + Vector((0.004, 0.0, 0.0))))
    drag(ctx, h, at(tip + Vector((0.009, 0.0, 0.0))))
    moved = prop.matrix_world.translation - start
    check(h.changed and abs(moved.x - 0.005) < 1e-4, f"held for the Grab Delay: small precise moves follow ({moved.x * 1000:.1f} mm)")
    prop.matrix_world.translation = start

    # Bones: the same gate.
    settings.grab_delay = 0.0
    pose.use_rig(ctx, rig)
    rig.animation_data_clear()
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()
    h = grab(ctx, rig, (0.67, 0.0, 1.4))
    drag(ctx, h, at((0.675, 0.004, 1.395)))
    check(not h.changed and rig.pose.bones["Forearm.L"].rotation_quaternion.angle < 1e-6,
          "a bone press with jitter selects it without turning it")
    drag(ctx, h, at((0.4, 0.0, 1.13)))
    check(near(world_tail(rig, "Forearm.L"), (0.4, 0.0, 1.1), 0.02), "then it poses as before")
    h.finish(ctx)
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()
    pose.choose_rig(ctx, "")


def test_puppet(rig, prop):
    print("== puppeteering")
    ctx = bpy.context
    scene = ctx.scene
    settings = scene.vr_sculpt
    ss = FakeSS()
    pose.use_rig(ctx, rig)
    check(rig.mode == 'POSE', "posing again")
    rig.animation_data_clear()  # Start from the rest pose, with no keys from the tests above.
    pose.reset(rig.pose.bones)
    ctx.view_layer.update()
    pose.select_bone(rig, rig.pose.bones["Hand.L"])
    settings.pose_tool = 'IK'
    check(puppet.bind(ctx, ss, 'RIGHT') == "Hand.L", "Bind R binds the active bone")
    move = Vector((-0.1, 0.1, -0.2))
    ss.grips[1] = Matrix.Translation(move) @ ss.grips[1]
    puppet.tick(ctx, ss)
    ctx.view_layer.update()
    err = (world_head(rig, "Hand.L") - (Vector((0.7, 0.0, 1.4)) + move)).length
    check(err < 2e-3, f"the bound hand follows the right grip by IK ({err * 1000:.2f} mm)")

    settings.pose_tool = 'FK'
    pose.select_bone(rig, rig.pose.bones["Spine"])
    puppet.bind(ctx, ss, 'HEAD')
    q0 = (rig.matrix_world @ rig.pose.bones["Spine"].matrix).to_quaternion()
    ss.viewer_pose_rotation = Quaternion((0, 0, 1), 0.5)
    puppet.tick(ctx, ss)
    ctx.view_layer.update()
    q1 = (rig.matrix_world @ rig.pose.bones["Spine"].matrix).to_quaternion()
    turn = q1 @ q0.inverted()
    check(abs(turn.angle - 0.5) < 1e-3 and abs(abs(turn.axis.z) - 1) < 1e-3,
          f"a bound spine turns with the headset (FK) ({turn.angle:.3f} rad)")
    check(puppet.binding_count() == 2, "two bindings")

    # A take: countdown, then frames keyed while the timeline runs (stepped by hand here).
    puppet.toggle_record(ctx)
    check(puppet.state() == 'COUNTDOWN' and puppet.status().startswith("Rec in"), "Rec counts down")
    puppet._take["start"] -= puppet.COUNTDOWN + 0.1
    puppet.tick(ctx, ss)
    check(puppet.state() == 'RECORDING' and scene.frame_current == scene.frame_start,
          "after the countdown the take starts at the first frame")
    for f in range(1, 6):
        scene.frame_set(f)
        ss.grips[1] = Matrix.Translation((0.0, 0.0, 0.01)) @ ss.grips[1]
        puppet.record_frame(ctx, ss)
    hand_curve = [fc for fc in anim.fcurves(rig) if fc.data_path == 'pose.bones["Hand.L"].rotation_quaternion']
    check(hand_curve and [round(k.co[0]) for k in hand_curve[0].keyframe_points] == [1, 2, 3, 4, 5],
          "the bound hand is keyed on every recorded frame")
    fore = [fc for fc in anim.fcurves(rig) if fc.data_path == 'pose.bones["Forearm.L"].rotation_quaternion']
    check(fore and len(fore[0].keyframe_points) == 5, "and so is its IK chain")
    scene.frame_set(1)
    puppet.record_frame(ctx, ss)
    check(puppet.state() == 'ENDING', "playback wrapping around ends the take")
    puppet.tick(ctx, ss)
    check(puppet.state() is None, "the timer stops it")
    check(len(hand_curve[0].keyframe_points) == 5, "nothing keyed on the wrapped frame")

    check(puppet.unbind_all() == 2 and not puppet.active(), "Unbind")
    # Objects can be bound too.
    pose.choose_rig(ctx, "")
    ctx.view_layer.objects.active = prop
    check(puppet.bind(ctx, ss, 'LEFT') == prop.name, "Bind L binds the active object without a rig")
    start = prop.matrix_world.translation.copy()
    ss.grips[0] = Matrix.Translation((0.0, 0.3, 0.0)) @ ss.grips[0]
    puppet.tick(ctx, ss)
    check(near(prop.matrix_world.translation, start + Vector((0.0, 0.3, 0.0))), "and it follows the left grip")
    bpy.data.objects.remove(prop)
    puppet.tick(ctx, ss)
    check(puppet.binding_count() == 0, "a deleted target drops its binding")
    puppet.end_session(ctx)


def main():
    vr_sculpt.register()
    try:
        ctx = bpy.context
        # The geometry tests drag in single steps; the threshold has its own test.
        ctx.scene.vr_sculpt.grab_delay = 0.0
        rig, body = make_rig(ctx)
        test_enter(rig, body)
        test_pick(rig)
        test_fk(rig)
        test_ik(rig)
        test_mirror_and_locks(rig)
        test_keys(rig)
        test_rows(rig)
        prop = test_objects(rig, body)
        test_drag_gate(rig, prop)
        test_puppet(rig, prop)
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


if __name__ == "__main__":
    main()
