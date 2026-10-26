"""Background tests for Physics mode: making objects rigid, static, cloth or soft (and back), their settings
(cloth presets, pinning the top edge, pressure, soft stiffness, a Soft Body remade), the world (gravity, speed, length, the
caches' range), a run (a box falling onto a static floor), the hands (colliders that follow the tips and
push a box, grabbing and throwing a body, push / pull fields), spring bones (lag, settling, gravity, a
hand pushing and holding a tail, the pose under the spring, rest, bake), the trigger's choices, keeping
and clearing a run, the hand panel pages, hints and status, mode switching and the trigger operator.

blender --background --factory-startup --python tests/test_physics.py
"""

import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import anim, controllers, gfx, hud, interactions, menus, operators, physics, vr_ui  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_model as tm  # noqa: E402  (fakes and the trigger operator harness)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def at(point):
    return Matrix.LocRotScale(Vector(point), Quaternion(), None)


def run(scene, frames, each=None):
    for f in range(scene.frame_current + 1, scene.frame_current + 1 + frames):
        if each is not None:
            each(f)
        scene.frame_set(f)


def ids():
    return {w.id for w in menus.layout(bpy.context)[0] if w.id}


def widget(wid):
    return vr_ui.find(menus.layout(bpy.context)[0], wid)


def cube(name, loc, size=0.2):
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    return obj


class Playing:
    """anim.is_playing answers True inside (background Blender can't play the timeline)."""

    def __enter__(self):
        self.was = anim.is_playing
        anim.is_playing = lambda context: True

    def __exit__(self, *exc):
        anim.is_playing = self.was


class InVR:
    """operators.session_running answers True inside."""

    def __enter__(self):
        self.was = operators.session_running
        operators.session_running = lambda context: True

    def __exit__(self, *exc):
        operators.session_running = self.was


def fresh(ctx):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    scene = ctx.scene
    if scene.rigidbody_world is not None:
        with ctx.temp_override(scene=scene):
            bpy.ops.rigidbody.world_remove()
    scene.frame_start, scene.frame_end = 1, 250
    scene.gravity = (0.0, 0.0, -9.81)
    scene.frame_set(1)
    physics.forget_caches()
    return scene


# --- objects -----------------------------------------------------------------------------

def test_kinds():
    print("== what an object is")
    ctx = bpy.context
    fresh(ctx)
    box = cube("Box", (0, 0, 1))
    check(physics.kind(box) == 'NONE', "a new cube is Off")
    physics.set_kind(ctx, box, 'RIGID')
    check(physics.kind(box) == 'RIGID' and box.rigid_body.type == 'ACTIVE', "Rigid: an active rigid body")
    check(any(m.type == 'COLLISION' for m in box.modifiers), "with a Collision modifier (cloth lands on it)")
    check(ctx.scene.rigidbody_world is not None and box.name in ctx.scene.rigidbody_world.collection.objects,
          "in the rigid body world (made for it)")
    physics.set_kind(ctx, box, 'STATIC')
    check(physics.kind(box) == 'STATIC' and box.rigid_body.type == 'PASSIVE', "Static: a passive body")
    physics.set_kind(ctx, box, 'CLOTH')
    check(physics.kind(box) == 'CLOTH' and box.rigid_body is None and
          not any(m.type == 'COLLISION' for m in box.modifiers), "Cloth: no rigid body or collision left")
    physics.set_kind(ctx, box, 'SOFT')
    mods = [m.type for m in box.modifiers]
    check(physics.kind(box) == 'SOFT' and mods == ['CLOTH'] and physics.cloth(box).use_internal_springs and
          physics.cloth_preset(box) == "Custom", "Soft: a new cloth with internal springs")
    physics.set_kind(ctx, box, 'CLOTH')
    check(physics.kind(box) == 'CLOTH' and not physics.cloth(box).use_internal_springs, "and back: plain cloth")
    physics.set_kind(ctx, box, 'NONE')
    check(physics.kind(box) == 'NONE' and not box.modifiers and box.rigid_body is None, "Off: all of it gone")
    box.modifiers.new("Collision", 'COLLISION')
    check(physics.kind(box) == 'STATIC', "a Collision modifier alone reads as Static")
    physics.set_kind(ctx, box, 'NONE')
    bpy.ops.object.armature_add()
    check(physics.kind(ctx.active_object) == 'NONE', "an armature is no body")
    bpy.data.objects.remove(ctx.active_object)
    return box


def test_settings(box):
    print("== object and world settings")
    ctx = bpy.context
    scene = ctx.scene
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=10, y_subdivisions=10, size=1.0, location=(2, 0, 1))
    sheet = ctx.active_object
    sheet.rotation_euler = (1.5708, 0.0, 0.0)  # Standing up, like a curtain.
    physics.set_kind(ctx, sheet, 'CLOTH')
    check(physics.cloth_preset(sheet) == "Custom", "a new cloth is Custom (Blender's defaults are no preset)")
    physics.set_cloth_preset(sheet, "Leather")
    check(physics.cloth_preset(sheet) == "Leather" and physics.cloth(sheet).bending_stiffness == 150.0,
          "the Leather preset")
    physics.set_cloth_stiffness(sheet, 33.0)
    s = physics.cloth(sheet)
    check(physics.cloth_preset(sheet) == "Custom" and s.shear_stiffness == 33.0, "stiffness: custom now")
    physics.set_cloth_pressure(sheet, 5.0)
    check(s.use_pressure and physics.cloth_pressure(sheet) == 5.0, "pressure on")
    physics.set_cloth_pressure(sheet, 0.0)
    check(not s.use_pressure and physics.cloth_pressure(sheet) == 0.0, "and off at 0")
    check([v for v, _l in physics.pin_options(sheet)][:2] == ["", physics.PIN_TOP], "pin: None, Top edge, groups")
    ctx.view_layer.update()
    physics.set_pin(sheet, physics.PIN_TOP)
    group = sheet.vertex_groups.get(physics.PIN_GROUP)
    mw = sheet.matrix_world
    pinned = [v for v in sheet.data.vertices if any(g.group == group.index for g in v.groups)]
    top = max((mw @ v.co).z for v in sheet.data.vertices)
    check(group is not None and len(pinned) == 11 and all(abs((mw @ v.co).z - top) < 1e-4 for v in pinned) and
          physics.pin(sheet) == physics.PIN_GROUP, f"Top edge pins the top row ({len(pinned)} vertices)")
    physics.set_pin(sheet, "")
    check(physics.pin(sheet) == "", "unpinned")

    blob = cube("Blob", (4, 0, 1))
    physics.set_kind(ctx, blob, 'SOFT')
    check(abs(physics.soft_stiffness(blob) - physics.SOFT_STIFFNESS) < 1e-5, "a new Soft's stiffness")
    physics.set_soft_stiffness(blob, 0.4)
    b = physics.cloth(blob)
    check(abs(physics.soft_stiffness(blob) - 0.4) < 1e-5 and b.internal_tension_stiffness ==
          b.internal_compression_stiffness and abs(b.tension_stiffness - 3 * b.internal_tension_stiffness) < 1e-4,
          f"soft stiffness: the internal springs ({b.internal_tension_stiffness:.3f}) and the skin")
    physics.set_soft_stiffness(blob, 1.0)
    stiff = b.internal_tension_stiffness
    physics.set_soft_stiffness(blob, 0.0)
    check(stiff > 10 * b.internal_tension_stiffness, "stiffer at 100% than at 0")
    old = cube("Old soft", (6, 0, 1))
    old.modifiers.new("Softbody", 'SOFT_BODY')
    physics.set_soft_stiffness(old, 0.7)
    check(physics.kind(old) == 'SOFT' and physics.old_soft(old) and abs(old.soft_body.pull - 0.7) < 1e-6,
          "a Soft Body modifier reads as Soft (its pull)")
    physics.set_kind(ctx, old, 'SOFT')
    check(physics.kind(old) == 'SOFT' and [m.type for m in old.modifiers] == ['CLOTH'], "Soft remakes it as a cloth")
    bpy.data.objects.remove(old)

    physics.set_gravity(ctx, 0.5)
    check(abs(scene.gravity.z + 4.905) < 1e-4 and abs(physics.gravity(ctx) - 0.5) < 1e-6, "gravity in g")
    physics.set_gravity(ctx, 1.0)
    physics.set_kind(ctx, box, 'RIGID')
    physics.set_speed(ctx, 0.5)
    check(scene.rigidbody_world.time_scale == 0.5 and s.time_scale == 0.5 and b.time_scale == 0.5 and
          physics.speed(ctx) == 0.5, "speed: rigid world, cloth and soft")
    physics.set_speed(ctx, 1.0)
    physics.set_length(ctx, 4.0)
    fps = physics.fps(scene)
    check(scene.frame_end == scene.frame_start + int(4 * fps) - 1 and abs(physics.length(ctx) - 4.0) < 1e-6,
          f"length: {scene.frame_end} frames at {fps:.0f} fps")
    check(len(physics.caches(ctx)) == 3 and all(c.frame_end == scene.frame_end for c in physics.caches(ctx)),
          "every cache covers the range")
    physics.set_length(ctx, 250 / fps)
    for o in (sheet, blob):
        bpy.data.objects.remove(o)
    physics.set_kind(ctx, box, 'NONE')


def test_run():
    print("== a run: a box falls onto a static floor")
    ctx = bpy.context
    scene = fresh(ctx)
    floor = cube("Floor", (0, 0, -0.05), 2.0)
    floor.scale.z = 0.05
    physics.set_kind(ctx, floor, 'STATIC')
    box = cube("Box", (0, 0, 1.0))
    physics.set_kind(ctx, box, 'RIGID')
    run(scene, 40)
    z = box.matrix_world.translation.z
    check(abs(z - 0.1) < 0.05, f"it rests on the floor (z {z:.3f}, rigid body margins included)")
    check(box.location.z == 1.0, "its own transform is untouched")
    physics.reset(ctx)
    check(scene.frame_current == 1 and abs(box.matrix_world.translation.z - 1.0) < 1e-4, "Reset: back to the start")
    return floor, box


def hand_context(ss):
    return tm.FakeContext(tm.FakeWM(ss))


def test_hands(floor, box):
    print("== hands: colliders that push")
    ctx = bpy.context
    scene = ctx.scene
    s = scene.vr_sculpt
    s.mode, s.physics_hands, s.tip_offset = 'PHYSICS', True, 0.0
    ss = tm.FakeSessionState()
    fctx = hand_context(ss)
    ss.aim_loc = [Vector((-3.0, 0.0, 0.1)), Vector((-0.5, 0.0, 0.1))]
    with InVR():
        physics.sync_hands(fctx, ss)
        hand = bpy.data.objects.get(physics.HAND_NAMES[1])
        check(hand is not None and bpy.data.objects.get(physics.FIELD_NAMES[0]) is not None,
              "Hands on: two colliders and two fields")
        check(hand.rigid_body is not None and hand.rigid_body.kinematic and hand.rigid_body.type == 'PASSIVE' and
              any(m.type == 'COLLISION' for m in hand.modifiers), "a collider is an animated passive body + Collision")
        check((hand.matrix_world.translation - Vector((-0.5, 0, 0.1))).length < 1e-5 and
              abs(hand.dimensions.y / 2 - physics.HAND_RADIUS) < 1e-3, "at the tip, hand radius")
        check(hand.hide_select and hand.display_type == 'WIRE', "a wire sphere you can't select")
        run(scene, 10)
        x0 = box.matrix_world.translation.x

        def sweep(f):
            ss.aim_loc[1] = Vector((-0.5 + 0.05 * (f - 11), 0.0, 0.1))
            physics.sync_hands(fctx, ss)
        run(scene, 20, sweep)
        x1 = box.matrix_world.translation.x
        check(0.2 < x1 - x0 < 1.5, f"sweeping the right hand through the box pushes it ({x0:.2f} -> {x1:.2f})")
        check(physics.hand_tips()[1][1].length > 0.1, "the hand's velocity is tracked")
        ss.viewer_scale = 2.0
        physics.sync_hands(fctx, ss)
        hand = bpy.data.objects.get(physics.HAND_NAMES[1])
        check(abs(hand.dimensions.y / 2 - 2 * physics.HAND_RADIUS) < 2e-3 and hand.rigid_body is not None,
              "a bigger you: bigger colliders")
        ss.viewer_scale = 1.0
        s.physics_hands = False
        physics.sync_hands(fctx, ss)
        check(all(bpy.data.objects.get(n) is None for n in physics.HAND_NAMES + physics.FIELD_NAMES) and
              bpy.data.collections.get(physics.COLLECTION) is None, "Hands off: colliders, fields and collection gone")
    s.physics_hands = True
    physics.sync_hands(ctx, ss)  # Not in VR: nothing.
    check(bpy.data.objects.get(physics.HAND_NAMES[0]) is None, "outside VR there are no hand colliders")
    physics.reset(ctx)


def test_grab_throw(box):
    print("== grab and throw")
    ctx = bpy.context
    scene = ctx.scene
    s = scene.vr_sculpt
    s.mode, s.physics_hands, s.physics_tool = 'PHYSICS', True, 'GRAB'
    box.location = (0.0, 0.0, 1.0)
    physics.reset(ctx)
    run(scene, 3)
    start = box.matrix_world.translation.copy()
    tip = start + Vector((0.0, 0.0, 0.05))
    handler = physics.pick(ctx, s, 1, at(tip), 1.0, 1.0)
    check(isinstance(handler, interactions.MoveObject), "not playing: the trigger moves the body (setting up)")
    handler.finish(ctx)
    with Playing():
        handler = physics.pick(ctx, s, 1, at(tip), 1.0, 1.0)
        check(isinstance(handler, physics.GrabBody) and box.rigid_body.kinematic, "playing: a grab, kinematic")
        first = scene.frame_current

        def carry(f):
            p = tip + Vector((0.04 * (f - first), 0.0, 0.0))
            handler.update(ctx, at(p), at(p), 1.0)
        run(scene, 8, carry)
        held = box.matrix_world.translation.x
        handler.finish(ctx)
    check(not box.rigid_body.kinematic and not handler.changed, "let go: dynamic again, no undo step")
    check(tuple(box.location) == (0.0, 0.0, 1.0), "its own transform is put back (the run starts the same)")
    run(scene, 6)
    flown = box.matrix_world.translation.x
    check(flown - held > 0.12, f"it flies on with the hand's speed ({held:.2f} -> {flown:.2f})")
    physics.reset(ctx)
    check((box.matrix_world.translation - Vector((0, 0, 1))).length < 1e-4, "and the run starts where it did")


def test_force(box):
    print("== push / pull")
    ctx = bpy.context
    scene = ctx.scene
    s = scene.vr_sculpt
    s.mode, s.physics_hands, s.physics_tool, s.physics_force = 'PHYSICS', True, 'FORCE', 300.0
    ss = tm.FakeSessionState()
    fctx = hand_context(ss)
    physics.set_gravity(ctx, 0.0)
    box.location = (0.0, 0.0, 1.0)
    physics.reset(ctx)
    ss.aim_loc = [Vector((0.0, -0.4, 1.0)), Vector((0.4, 0.0, 1.0))]
    with InVR():
        physics.sync_hands(fctx, ss)
        pull = physics.pick(ctx, s, 1, at(ss.aim_loc[1]), 1.0, 1.0)
        check(isinstance(pull, physics.HandForce), "Push / Pull: a force at the hand")
        physics.sync_hands(fctx, ss)
        field = bpy.data.objects[physics.FIELD_NAMES[1]].field
        check(field.strength < 0 and abs(field.strength + 300.0) < 1e-3, "the right hand pulls (negative strength)")
        check(abs(field.distance_max - physics.FORCE_REACH) < 1e-6, "within reach")
        run(scene, 10, lambda f: physics.sync_hands(fctx, ss))
        x = box.matrix_world.translation.x
        check(x > 0.05, f"the box comes toward the right hand (x {x:.3f})")
        pull.update(ctx, None, None, 0.5)
        physics.sync_hands(fctx, ss)
        check(abs(field.strength + 150.0) < 1e-3, "half the trigger, half the force")
        pull.finish(ctx)
        check(field.strength == 0.0, "let go: no force")
        push = physics.pick(ctx, s, 0, at(ss.aim_loc[0]), 1.0, 1.0)
        physics.sync_hands(fctx, ss)
        check(bpy.data.objects[physics.FIELD_NAMES[0]].field.strength > 0, "the left hand pushes")
        push.finish(ctx)
        s.physics_hands = False
        physics.sync_hands(fctx, ss)
    physics.set_gravity(ctx, 1.0)
    physics.reset(ctx)


# --- spring bones --------------------------------------------------------------------

def make_rig(ctx):
    """A two-bone chain up the Z axis from the origin, 0.5 m each: Root, then Tail."""
    bpy.ops.object.armature_add(location=(0, 0, 0))
    arm = ctx.active_object
    arm.name = "Springy"
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.data.edit_bones
    root = eb[0]
    root.name = "Root"
    root.head, root.tail = (0, 0, 0), (0, 0, 0.5)
    tail = eb.new("Tail")
    tail.head, tail.tail = (0, 0, 0.5), (0, 0, 1.0)
    tail.parent, tail.use_connect = root, True
    bpy.ops.object.mode_set(mode='OBJECT')
    return arm


def tail_world(arm, name="Tail"):
    bpy.context.view_layer.update()
    return arm.matrix_world @ arm.pose.bones[name].tail


def steps(ctx, arm, seconds, dt=1.0 / 90.0, hands=(), radius=0.0):
    names = [pb.name for pb in physics.springs(arm)]
    for _i in range(int(round(seconds / dt))):
        physics.write(arm, physics.step(ctx, arm, physics.snapshot(arm, names), dt, hands, radius))
    ctx.view_layer.update()


def test_springs():
    print("== spring bones")
    ctx = bpy.context
    fresh(ctx)
    arm = make_rig(ctx)
    root, tail = arm.pose.bones["Root"], arm.pose.bones["Tail"]
    check(not physics.has_spring(tail) and tail.bl_system_properties_get() is None,
          "has_spring doesn't give a bone settings it never had")
    physics.spring_rigs(ctx, refresh=True)
    check(root.bl_system_properties_get() is None, "nor does looking for springs")
    tail.vr_spring.gravity = 0.0
    physics.set_spring(arm, tail, True)
    check(physics.has_spring(tail) and physics.spring_rigs(ctx, refresh=True) == {arm.name: (["Tail"], ["Tail"])},
          "Spring on: the rig is listed with it")
    steps(ctx, arm, 0.2)
    check((tail_world(arm) - Vector((0, 0, 1))).length < 1e-4, "at rest it stays on its pose")
    arm.update_tag()
    ctx.view_layer.update()
    names = [pb.name for pb in physics.springs(arm)]
    physics.write(arm, physics.step(ctx, arm, physics.snapshot(arm, names), 1.0 / 90.0))
    check(not ctx.evaluated_depsgraph_get().id_type_updated('OBJECT'), "a settled spring isn't written again")
    # Swing the parent: the tail lags behind, then catches up.
    root.rotation_mode = 'QUATERNION'
    root.rotation_quaternion = Quaternion((1, 0, 0), 0.6)  # About the bone's X: a bend (Y is its length).
    ctx.view_layer.update()
    target_tail = root.matrix.translation + (root.tail - root.head) * 2.0
    steps(ctx, arm, 1.0 / 30.0)
    lag = (tail_world(arm) - Vector(target_tail)).length
    check(lag > 0.05, f"right after the parent turns, the tail lags ({lag:.3f} m off)")
    steps(ctx, arm, 3.0)
    settled = (tail_world(arm) - Vector(target_tail)).length
    check(settled < 0.01, f"and settles on the pose ({settled:.4f} m off)")
    check(abs(tail.length - 0.5) < 1e-5, "the bone keeps its length")
    check(physics.base_basis(arm, tail) == Matrix.Identity(4) or
          all(abs(a - b) < 1e-5 for r1, r2 in zip(physics.base_basis(arm, tail), Matrix.Identity(4))
              for a, b in zip(r1, r2)), "its own pose (base) is still the rest pose")
    # Gravity sags a sideways bone.
    root.rotation_quaternion = Quaternion((1, 0, 0), 1.5708)
    tail.vr_spring.gravity, tail.vr_spring.stiffness = 1.0, 0.2
    steps(ctx, arm, 3.0)
    sag = tail_world(arm).z - (arm.matrix_world @ tail.head).z
    check(sag < -0.01, f"gravity pulls a sideways tail down ({sag:.3f} m)")
    tail.vr_spring.gravity, tail.vr_spring.stiffness = 0.0, 0.5
    root.rotation_quaternion = Quaternion()
    steps(ctx, arm, 3.0)
    # A hand in the way pushes the tail aside.
    hand = Vector((0.05, 0.0, 0.98))
    steps(ctx, arm, 0.5, hands=[(hand, Vector())], radius=0.1)
    pushed = (tail_world(arm) - hand).length
    check(pushed >= 0.099 and tail_world(arm).x < -0.02,
          f"a hand at the tail pushes it out of its sphere ({pushed:.3f} m from it)")
    steps(ctx, arm, 3.0)
    # A held tail follows the hand; let go, it swings back.
    grab = physics.GrabSpring(ctx, arm, tail, at((0, 0, 1.0)))
    grab.update(ctx, None, at((0.3, 0, 0.9)), 1.0)
    steps(ctx, arm, 0.1)
    held = tail_world(arm)
    want = Vector((0, 0, 0.5)) + (Vector((0.3, 0, 0.9)) - Vector((0, 0, 0.5))).normalized() * 0.5
    check((held - want).length < 0.01, "a grabbed tail points at the hand")
    grab.finish(ctx)
    steps(ctx, arm, 3.0)
    check((tail_world(arm) - Vector((0, 0, 1))).length < 0.01, "let go, it springs back")
    # A new pose under the spring (a key, Pose mode) becomes what it springs to.
    tail.rotation_mode = 'QUATERNION'
    tail.rotation_quaternion = Quaternion((1, 0, 0), 0.5)
    steps(ctx, arm, 3.0)
    base = physics.base_basis(arm, tail).to_quaternion()
    check(abs(base.angle - 0.5) < 1e-3, "a pose set under the spring is its new base")
    tip = tail_world(arm)
    check(tip.y < -0.2, f"and the bone springs to it ({tip.y:.3f})")
    # Rest: back on the pose, nothing remembered.
    steps(ctx, arm, 1.0 / 30.0)
    physics.reset_springs(ctx)
    check(abs(tail.rotation_quaternion.angle - 0.5) < 1e-4 and not physics._sim and not physics._written,
          "Rest puts the bone on its own pose")
    physics.set_spring(arm, tail, False)
    check(physics.spring_rigs(ctx, refresh=True) == {}, "Spring off: no rigs with springs")
    tail.rotation_quaternion = Quaternion()
    # Whole chain: the active bone's settings for it and every bone below.
    root.vr_spring.stiffness = 0.8
    physics.spring_chain(arm, root)
    check(physics.has_spring(root) and physics.has_spring(tail) and abs(tail.vr_spring.stiffness - 0.8) < 1e-6,
          "Whole chain copies the spring down the chain")
    order = physics.spring_rigs(ctx, refresh=True)[arm.name][1]
    check(order == ["Root", "Tail"], "parents first")
    physics.set_spring(arm, root, False)
    return arm


def test_bake(arm):
    print("== baking springs")
    ctx = bpy.context
    scene = ctx.scene
    root, tail = arm.pose.bones["Root"], arm.pose.bones["Tail"]
    scene.frame_start, scene.frame_end = 1, 24
    root.rotation_mode = 'QUATERNION'
    root.rotation_quaternion = Quaternion()
    root.keyframe_insert("rotation_quaternion", frame=1)
    root.rotation_quaternion = Quaternion((1, 0, 0), 0.8)
    root.keyframe_insert("rotation_quaternion", frame=6)
    tail.vr_spring.stiffness, tail.vr_spring.damping, tail.vr_spring.gravity = 0.3, 0.1, 0.0
    physics.set_spring(arm, tail, True)
    n = physics.bake(ctx, arm)
    check(n == 24 and not physics.has_spring(tail), "Bake keys 24 frames and turns the spring off")
    fc = [c for c in anim.fcurves(arm) if c.data_path == 'pose.bones["Tail"].rotation_quaternion']
    check(len(fc) == 4 and len(fc[0].keyframe_points) == 24, "rotation keys on every frame")
    angles = []
    for f in range(1, 25):
        scene.frame_set(f)
        angles.append(tail.rotation_quaternion.angle)
    check(max(angles) > 0.05 and angles[-1] < max(angles), f"the keys swing and settle (max {max(angles):.2f} rad)")
    check(len([c for c in anim.fcurves(arm) if '"Root"' in c.data_path]) == 4 and
          root.rotation_quaternion.angle > 0.79, "the parent's own keys are untouched")
    scene.frame_end = 250
    scene.frame_set(1)
    arm.animation_data_clear()


def test_pick(arm, box):
    print("== what the trigger does")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode, s.physics_hands = 'PHYSICS', False
    from vr_sculpt import primitives
    primitives.make_active(ctx, arm)
    h = physics.pick(ctx, s, 1, at((0, 0, 0.75)), 1.0, 1.0)
    check(isinstance(h, physics.SelectBone) and arm.data.bones.active.name == "Tail",
          "setting up: touching a bone of the selected rig picks it")
    box.location = (2.0, 0.0, 1.0)
    ctx.view_layer.update()
    h = physics.pick(ctx, s, 0, at((2.0, 0.0, 1.0)), 1.0, 1.0)
    check(isinstance(h, interactions.MoveObject) and physics.selected(ctx) == box, "touching an object selects it")
    h.finish(ctx)
    h = physics.pick(ctx, s, 1, at((5.0, 5.0, 5.0)), 1.0, 1.0)
    check(isinstance(h, interactions.Deselect) and physics.selected(ctx) is None, "empty space: nothing selected")
    s.physics_hands, s.physics_tool = True, 'GRAB'
    physics.set_spring(arm, arm.pose.bones["Tail"], True)
    h = physics.pick(ctx, s, 1, at((0, 0, 0.9)), 1.0, 1.0)
    check(isinstance(h, physics.GrabSpring), "Hands, Grab: a spring bone's tail is grabbed (any rig)")
    h.finish(ctx)
    check(physics.pick(ctx, s, 1, at((0, 0, 0.25)), 1.0, 1.0) is None, "a bone without a spring isn't")
    check(physics.engaged(ctx, s, Vector((0, 0, 0.9)), 1.0) and not physics.engaged(ctx, s, Vector((5, 5, 5)), 1.0),
          "engaged near springs, not in empty space")
    s.physics_tool = 'FORCE'
    h = physics.pick(ctx, s, 0, at((5.0, 5.0, 5.0)), 1.0, 1.0)
    check(isinstance(h, physics.HandForce) and h.sign > 0, "Push / Pull anywhere; the left hand pushes")
    h.finish(ctx)
    check(not physics.engaged(ctx, s, Vector((0, 0, 0.9)), 1.0), "Push / Pull never gives way to the desktop")
    physics.set_spring(arm, arm.pose.bones["Tail"], False)
    s.physics_hands, s.physics_tool = False, 'GRAB'


def test_keep():
    print("== keeping a run")
    ctx = bpy.context
    scene = ctx.scene
    physics.reset(ctx)
    run(scene, 10)
    check(not physics.baked(ctx), "a played run is only cached")
    kept = physics.keep(ctx)
    check(kept >= 1 and physics.baked(ctx), f"Keep bakes the caches ({kept})")
    physics.free(ctx)
    check(not physics.baked(ctx), "Clear kept frees them")
    physics.reset(ctx)


def test_rows(arm, box):
    print("== hand panel pages")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    from vr_sculpt import primitives
    menus.reset()
    menus.set_mode(ctx, 'PHYSICS')
    used = set()
    pages = []
    cases = [('none', None, False), ('box NONE', box, False), ('RIGID', box, False), ('STATIC', box, False),
             ('CLOTH', box, False), ('SOFT', box, False), ('RIGID hands', box, True), ('rig', arm, False),
             ('rig hands', arm, True)]
    for name, obj, hands in cases:
        s.physics_hands, s.physics_tool = hands, 'FORCE' if hands else 'GRAB'
        primitives.make_active(ctx, obj)
        kind = name.split()[0]
        if obj is box:
            physics.set_kind(ctx, box, kind if kind in dict(physics.KINDS) else 'NONE')
        if obj is arm:
            arm.data.bones.active = arm.data.bones["Tail"]
            physics.set_spring(arm, arm.pose.bones["Tail"], True)
        for expanded in (False, True):
            menus.set_more('PHYSICS', expanded)
            widgets, height = menus.layout(ctx)
            overlap = any(a is not b and a.rect[0] < b.rect[2] - 1e-9 and b.rect[0] < a.rect[2] - 1e-9 and
                          a.rect[1] < b.rect[3] - 1e-9 and b.rect[1] < a.rect[3] - 1e-9
                          for a in widgets for b in widgets)
            check(height < 0.34 and not overlap, f"{name}{' + More' if expanded else ''}: fits ({height:.3f} m)")
            used |= {w.icon for w in widgets if w.icon}
            pages.append({w.id for w in widgets if w.id})
        menus.set_more('PHYSICS', False)
        primary = len(menus.panel(ctx).rows) - 2
        check(2 <= primary <= 5, f"{name}: {primary} controls up front")
    check(not (used - set(gfx.ICONS)), f"every icon is drawn (missing {sorted(used - set(gfx.ICONS))})")
    check({"sim_play", "sim_reset", "physics_hands", "kind_rigid", "kind_cloth"} <= pages[2] and
          "physics_sim" not in pages[2] and "physics_sim" in pages[3], "the sim row, the kinds; More: Simulation")
    check({"rb_mass", "rb_bounce"} <= pages[4] and {"rb_shape_box", "rb_friction", "rb_air"} <= pages[5],
          "Rigid: mass, bounce; More: shape, friction, air drag")
    check({"cloth_preset", "cloth_pin", "cloth_stiffness"} <= pages[8] and
          {"cloth_pressure", "cloth_mass", "cloth_self"} <= pages[9], "Cloth: preset, pin, stiffness")
    check({"soft_stiffness", "soft_pressure"} <= pages[10] and {"soft_mass", "soft_self"} <= pages[11],
          "Soft: stiffness, pressure; More: mass, self collision")
    check({"ptool_grab", "ptool_force"} <= pages[12] and "physics_force" in pages[13], "Hands: the tool; More: force")
    check({"spring_on", "spring_chain", "spring_stiffness", "spring_damping"} <= pages[14] and
          {"spring_gravity", "spring_bake", "spring_rest"} <= pages[15], "a rig: the active bone's spring")
    # Presses.
    s.physics_hands = False
    primitives.make_active(ctx, box)
    physics.set_kind(ctx, box, 'NONE')
    widget("kind_rigid").on_press()
    check(physics.kind(box) == 'RIGID', "the Rigid segment")
    widget("kind_cloth").on_press()
    check(physics.kind(box) == 'CLOTH', "the Cloth segment")
    widget("cloth_preset").on_press()
    check(menus.picker_open(), "the Cloth select opens a picker")
    vr_ui.find(menus.layout(ctx)[0], "opt_Silk").on_press()
    check(physics.cloth_preset(box) == "Silk", "choosing Silk")
    widget("cloth_pin").on_press()
    vr_ui.find(menus.layout(ctx)[0], "opt_" + physics.PIN_TOP).on_press()
    check(physics.pin(box) == physics.PIN_GROUP, "pinning the top edge from the picker")
    menus.set_more('PHYSICS', True)
    widget("physics_sim").on_press()
    check(menus.sheet() == 'SIMULATION' and {"physics_gravity", "physics_speed", "physics_length",
                                             "physics_keep", "back"} <= ids(), "the Simulation sheet")
    widgets, height = menus.layout(ctx)
    check(height < 0.34, f"it fits ({height:.3f} m)")
    widget("physics_gravity").on_change(0.5)
    check(abs(physics.gravity(ctx) - 0.5) < 1e-6, "its gravity slider")
    physics.set_gravity(ctx, 1.0)
    widget("back").on_press()
    check(menus.sheet() is None, "Back")
    menus.open_sheet('SIMULATION')
    menus.set_mode(ctx, 'TOOLS')
    menus.panel(ctx)
    check(menus.sheet() is None, "another mode closes it")
    menus.set_mode(ctx, 'PHYSICS')
    menus.set_more('PHYSICS', False)
    widget("physics_hands").on_press()
    check(s.physics_hands, "the Hands toggle")
    widget("ptool_force").on_press()
    check(s.physics_tool == 'FORCE', "the hand tool")
    s.physics_hands, s.physics_tool = False, 'GRAB'
    physics.set_kind(ctx, box, 'NONE')
    primitives.make_active(ctx, arm)
    widget("spring_on").on_press()
    check(not physics.has_spring(arm.pose.bones["Tail"]), "the Spring toggle")
    menus.reset()


def test_modes(arm):
    print("== switching modes")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    from vr_sculpt import primitives
    primitives.make_active(ctx, arm)
    menus.set_mode(ctx, 'POSE')
    check(arm.mode == 'POSE', "Pose poses the rig")
    bpy.ops.vrsculpt.cycle_brush(off_hand=True)
    check(s.mode == 'PHYSICS' and arm.mode == 'OBJECT', "Y after Pose: Physics, the rig back in Object Mode")
    bpy.ops.vrsculpt.cycle_brush(off_hand=True)
    check(s.mode == 'TOOLS', "then Tools")
    menus.set_mode(ctx, 'PHYSICS')
    s.physics_hands = False
    bpy.ops.vrsculpt.cycle_radius()
    check(s.physics_hands, "right stick click: Hands on")
    bpy.ops.vrsculpt.cycle_radius()
    check(not s.physics_hands, "and off")


def test_hints_and_status():
    print("== hints and tip status")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    Info = controllers.Info

    def label(hand, button, info=Info()):
        h = controllers.hints(s, info)[hand][button]
        return h[1] if h else None

    s.mode, s.physics_hands = 'PHYSICS', False
    check(label('right', 'trigger') == "Select, move" and label('left', 'trigger') == "Select, move",
          "setting up: both triggers select and move")
    check(label('right', 'top') == "Simulate" and label('right', 'top', Info(playing=True)) == "Stop",
          "B plays and stops")
    check(label('right', 'stick_click') == "Hands on" and label('left', 'top') == "Next: Tools",
          "stick click: Hands on; Y: Tools next")
    s.physics_hands, s.physics_tool = True, 'GRAB'
    check(label('right', 'trigger') == "Grab, throw" and label('left', 'trigger') == "Grab, throw" and
          label('right', 'stick_click') == "Hands off", "Grab: both hands grab")
    s.physics_tool = 'FORCE'
    check(label('right', 'trigger') == "Pull" and label('left', 'trigger') == "Push", "Push / Pull: right pulls")
    icons = {h[0] for hand in controllers.hints(s, Info()).values() for h in hand.values() if h}
    check(not (icons - set(gfx.ICONS)), "every hint icon is drawn")
    for hands, tool, title in ((False, 'GRAB', "Physics"), (True, 'GRAB', "Grab & Throw"),
                               (True, 'FORCE', "Push & Pull")):
        s.physics_hands, s.physics_tool = hands, tool
        content = hud.card_content(s, None)
        check(content[0] == title and content[3] in gfx.ICONS, f"tip status: {title}")
    s.physics_force = 250.0
    check(hud.status_size(ctx, s) == "force 250", "Push / Pull shows the force")
    s.physics_hands, s.physics_tool = False, 'GRAB'
    check(hud.status_size(ctx, s) == "", "stopped: nothing")


def test_operator(box):
    print("== the trigger operator")
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    from vr_sculpt import primitives
    s.show_panel = False
    s.tip_offset = 0.0
    menus.set_mode(ctx, 'PHYSICS')
    box.location = (0.9, 0.0, 1.0)
    ctx.view_layer.update()
    primitives.make_active(ctx, None)
    ss = tm.FakeSessionState()
    fctx = tm.FakeContext(tm.FakeWM(ss))
    with InVR():
        _op, r = tm.press(fctx, ss, 1, [(0.9, 0.0, 1.0)])
        check(r == {'CANCELLED'} and physics.selected(ctx) == box, "touching selects (a click: no undo step)")
        _op, r = tm.press(fctx, ss, 1, [(0.9, 0.0, 1.0), (0.9, 0.0, 1.2), (0.9, 0.0, 1.3)])
        check(r == {'FINISHED'} and box.location.z > 1.2, "dragging moves it: one undo step")
        s.physics_hands, s.physics_tool = True, 'FORCE'
        _op, r = tm.press(fctx, ss, 1, [(0.9, 0.0, 1.3), (0.9, 0.0, 1.3)])
        check(r == {'CANCELLED'}, "a push / pull is no undo step")
        s.physics_hands = False
    s.show_panel = True
    physics.remove_hands()


def test_undo_guard(arm):
    print("== undo guard")
    tail = arm.pose.bones["Tail"]
    physics.set_spring(arm, tail, True)
    steps(bpy.context, arm, 0.1)
    physics._held[(arm.name, "Tail")] = [Vector(), Vector(), Vector()]
    operators._on_data_swap()
    check(not physics._sim and not physics._written and not physics._held, "an undo forgets the springs' state")
    physics.set_spring(arm, tail, False)


def main():
    vr_sculpt.register()
    try:
        box = test_kinds()
        test_settings(box)
        bpy.data.objects.remove(box)
        floor, box = test_run()
        test_hands(floor, box)
        test_grab_throw(box)
        test_force(box)
        test_keep()
        arm = test_springs()
        test_bake(arm)
        box = cube("Box", (2.0, 0.0, 1.0))
        test_pick(arm, box)
        test_rows(arm, box)
        test_modes(arm)
        test_hints_and_status()
        test_operator(box)
        test_undo_guard(arm)
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


if __name__ == "__main__":
    main()
