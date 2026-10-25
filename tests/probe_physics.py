"""Probe: what Blender's physics does when driven the way VR would drive it (background).

blender --background --factory-startup --python tests/probe_physics.py

Checks: rigid body setup from Python with no area, whether the original object's matrix_world
follows the simulation, kinematic bodies moved between frames (a hand pushing, a grab and a throw),
hidden kinematic bodies, a force field at a hand, cloth against a moving collider, and what the
point cache does when the run is replayed with different input. Writes tests/probe_physics_result.txt.
"""

import os
import sys
import time

import bpy
from mathutils import Vector

OUT = []


def say(*args):
    line = " ".join(str(a) for a in args)
    OUT.append(line)
    print(line, flush=True)


def fresh():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 250
    scene.frame_set(1)
    return scene


def cube(name, loc, size=0.2):
    bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def sphere(name, loc, r=0.05):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=loc, segments=12, ring_count=6)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def rigid(obj, kind='ACTIVE'):
    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
        bpy.ops.rigidbody.object_add(type=kind)
    return obj.rigid_body


def run(scene, frames, each=None):
    for f in range(scene.frame_current + 1, scene.frame_current + 1 + frames):
        if each is not None:
            each(f)
        scene.frame_set(f)


def probe_fall():
    say("== fall")
    scene = fresh()
    ground = cube("Ground", (0, 0, -0.1), 2.0)
    ground.scale.z = 0.05
    rigid(ground, 'PASSIVE')
    box = cube("Box", (0, 0, 1.0))
    rb = rigid(box)
    say("world:", scene.rigidbody_world is not None, "collection:", scene.rigidbody_world.collection.name,
        "objects:", [o.name for o in scene.rigidbody_world.collection.objects])
    say("rb props:", rb.type, rb.mass, rb.friction, rb.restitution, rb.collision_shape, rb.kinematic, rb.enabled)
    t0 = time.perf_counter()
    run(scene, 30)
    dt = (time.perf_counter() - t0) / 30 * 1000
    say(f"after 30 frames: location {tuple(round(v, 3) for v in box.location)}, matrix_world z "
        f"{box.matrix_world.translation.z:.3f}, evaluated z "
        f"{box.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.z:.3f} "
        f"({dt:.2f} ms per frame)")
    cache = scene.rigidbody_world.point_cache
    say("cache:", cache.frame_start, cache.frame_end, "baked", cache.is_baked, "outdated", cache.is_outdated,
        "info", cache.info)
    # Back to frame 1 and replay: reads the cache?
    scene.frame_set(1)
    say("back at 1: z", round(box.matrix_world.translation.z, 3))
    scene.frame_set(2)
    scene.frame_set(20)
    say("jump 2 -> 20 (cached):", round(box.matrix_world.translation.z, 3))


def probe_push(hidden=False):
    say(f"== push with a kinematic sphere (hidden={hidden})")
    scene = fresh()
    ground = cube("Ground", (0, 0, -0.1), 4.0)
    ground.scale.z = 0.05
    rigid(ground, 'PASSIVE')
    box = cube("Box", (0, 0, 0.1))
    rigid(box)
    hand = sphere("Hand", (-0.5, 0, 0.1), 0.06)
    rb = rigid(hand, 'PASSIVE')
    rb.kinematic = True
    if hidden:
        hand.hide_set(True)
    run(scene, 10)  # Let the box settle.
    x0 = box.matrix_world.translation.x

    def move(f):
        hand.location.x = -0.5 + 0.05 * (f - 11)  # 5 cm per frame, 1.2 m/s at 24 fps
    run(scene, 20, move)
    say(f"hand at x {hand.location.x:.2f}; box x {x0:.3f} -> {box.matrix_world.translation.x:.3f}")


def probe_throw():
    say("== grab and throw: kinematic while held, then dynamic")
    scene = fresh()
    box = cube("Box", (0, 0, 1.0))
    rb = rigid(box)
    run(scene, 2)
    rb.kinematic = True
    start = scene.frame_current
    box.location = (0, 0, 1.0)

    def carry(f):
        box.location = (0.04 * (f - start), 0, 1.0)  # 4 cm per frame along X
    run(scene, 10, carry)
    x_release = box.matrix_world.translation.x
    say("held: x", round(x_release, 3), "location", tuple(round(v, 3) for v in box.location))
    rb.kinematic = False
    run(scene, 1)
    say("1 frame after release: x", round(box.matrix_world.translation.x, 3), "loc", tuple(round(v, 3) for v in box.location))
    run(scene, 9)
    say("10 frames after release: x", round(box.matrix_world.translation.x, 3), "z",
        round(box.matrix_world.translation.z, 3), "(a throw keeps going along X; a drop stays near",
        round(x_release, 3), ")")


def probe_field():
    say("== force field at a hand")
    scene = fresh()
    box = cube("Box", (0, 0, 0.0))
    rigid(box)
    scene.gravity = (0, 0, 0)
    bpy.ops.object.empty_add(location=(1.0, 0, 0))
    hand = bpy.context.active_object
    hand.name = "HandField"
    say("empty field before:", hand.field)
    with bpy.context.temp_override(object=hand, active_object=hand):
        bpy.ops.object.forcefield_toggle()
    say("after forcefield_toggle:", hand.field, hand.field.type if hand.field else None)
    hand.field.type = 'FORCE'
    hand.field.strength = -50.0  # negative attracts
    hand.field.falloff_type = 'SPHERE'
    say("field:", hand.field.type, hand.field.strength, hand.field.shape, hand.field.flow)
    run(scene, 20)
    say("box x after 20 frames toward the field at x=1:", round(box.matrix_world.translation.x, 3))
    # Move the field to the other side mid-run.
    hand.location.x = -1.0
    run(scene, 20)
    say("after moving the field to x=-1, 20 more frames:", round(box.matrix_world.translation.x, 3))


def probe_cache_replay():
    say("== replay with different input")
    scene = fresh()
    ground = cube("Ground", (0, 0, -0.1), 4.0)
    ground.scale.z = 0.05
    rigid(ground, 'PASSIVE')
    box = cube("Box", (0, 0, 0.1))
    rigid(box)
    hand = sphere("Hand", (-0.5, 0, 0.1), 0.06)
    rigid(hand, 'PASSIVE').kinematic = True
    run(scene, 30)
    x_still = box.matrix_world.translation.x
    scene.frame_set(1)
    hand.location.x = -0.5

    def move(f):
        hand.location.x = -0.5 + 0.05 * f
    run(scene, 29, move)
    say(f"first run (hand still): box x {x_still:.3f}; second run (hand pushes): {box.matrix_world.translation.x:.3f}"
        " (same = cache read back instead of simulated)")
    cache = scene.rigidbody_world.point_cache
    say("cache outdated:", cache.is_outdated, "info:", cache.info)
    # Force a fresh run: reset by touching a world setting.
    scene.frame_set(1)
    hand.location.x = -0.5
    w = scene.rigidbody_world
    w.time_scale = w.time_scale
    run(scene, 29, move)
    say(f"after time_scale reassign: box x {box.matrix_world.translation.x:.3f}")
    scene.frame_set(1)
    hand.location.x = -0.5
    w.point_cache.frame_end = w.point_cache.frame_end
    run(scene, 29, move)
    say(f"after point_cache.frame_end reassign: box x {box.matrix_world.translation.x:.3f}")


def probe_cloth():
    say("== cloth against a moving collider")
    scene = fresh()
    ball = sphere("Ball", (0, 0, 0.0), 0.2)
    ball.modifiers.new("Collision", 'COLLISION')
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=20, y_subdivisions=20, size=1.0, location=(0, 0, 0.5))
    cloth = bpy.context.active_object
    cloth.name = "Cloth"
    mod = cloth.modifiers.new("Cloth", 'CLOTH')
    bpy.context.view_layer.update()
    say("cloth settings:", mod.settings.quality, mod.settings.mass, mod.settings.tension_stiffness,
        mod.settings.bending_stiffness, "collision:", mod.collision_settings.use_collision,
        mod.collision_settings.use_self_collision, "cache:", mod.point_cache.frame_end)
    say("collision settings:", ball.collision.use, ball.collision.thickness_outer, ball.collision.cloth_friction,
        ball.collision.damping)
    t0 = time.perf_counter()
    run(scene, 20)
    dt = (time.perf_counter() - t0) / 20 * 1000
    dg = bpy.context.evaluated_depsgraph_get()
    zs = [v.co.z for v in cloth.evaluated_get(dg).data.vertices]
    say(f"cloth after 20 frames: min z {min(zs):.3f} max z {max(zs):.3f} ({dt:.1f} ms per frame, 441 verts)")

    def lift(f):
        ball.location.z = 0.0 + 0.04 * (f - 21)
    run(scene, 15, lift)
    dg = bpy.context.evaluated_depsgraph_get()
    zs = [v.co.z for v in cloth.evaluated_get(dg).data.vertices]
    say(f"after the ball rose to z {ball.location.z:.2f}: cloth max z {max(zs):.3f} (pushed up if > 0.5ish)")
    ball.hide_set(True)
    run(scene, 10, lambda f: setattr(ball.location, "z", ball.location.z + 0.04))
    dg = bpy.context.evaluated_depsgraph_get()
    zs = [v.co.z for v in cloth.evaluated_get(dg).data.vertices]
    say(f"hidden ball rose to z {ball.location.z:.2f}: cloth max z {max(zs):.3f}")
    say("presets:", sorted(os.listdir(os.path.join(bpy.utils.resource_path('LOCAL'), "scripts", "presets", "cloth")))
        if os.path.isdir(os.path.join(bpy.utils.resource_path('LOCAL'), "scripts", "presets", "cloth")) else "none")
    # Cloth also feels rigid bodies? (no: needs a Collision modifier). Pressure:
    say("pressure props:", hasattr(mod.settings, "use_pressure"), getattr(mod.settings, "uniform_pressure_force", None))
    say("pin group prop:", mod.settings.vertex_group_mass, "shrink:", mod.settings.shrink_min)


def probe_soft():
    say("== soft body")
    scene = fresh()
    ground = cube("Ground", (0, 0, -0.1), 4.0)
    ground.scale.z = 0.05
    ground.modifiers.new("Collision", 'COLLISION')
    obj = sphere("Jelly", (0, 0, 0.5), 0.2)
    mod = obj.modifiers.new("Softbody", 'SOFT_BODY')
    bpy.context.view_layer.update()
    s = obj.soft_body
    say("soft:", s.use_goal, s.goal_default, s.goal_spring, s.goal_friction, s.use_edges, s.pull, s.push, s.bend,
        s.mass, s.friction, s.use_self_collision, "cache", mod.point_cache.frame_end)
    s.use_goal = False
    run(scene, 30)
    dg = bpy.context.evaluated_depsgraph_get()
    zs = [v.co.z for v in obj.evaluated_get(dg).data.vertices]
    say(f"jelly after 30 frames (no goal): min z {min(zs):.3f} (ground top at -0.05)")


def probe_effectors_on_rigid():
    say("== effector weights")
    scene = fresh()
    w = None
    box = cube("Box", (0, 0, 0))
    rigid(box)
    w = scene.rigidbody_world
    say("world props:", w.enabled, w.time_scale, w.substeps_per_frame, w.solver_iterations,
        "effector all:", w.effector_weights.all, "gravity:", w.effector_weights.gravity, tuple(scene.gravity),
        scene.use_gravity)


def probe_bake():
    say("== bake from cache")
    scene = fresh()
    box = cube("Box", (0, 0, 1.0))
    rigid(box)
    run(scene, 20)
    cache = scene.rigidbody_world.point_cache
    try:
        with bpy.context.temp_override(scene=scene, point_cache=cache):
            bpy.ops.ptcache.bake_from_cache()
        say("bake_from_cache: baked", cache.is_baked, cache.info)
    except Exception as ex:
        say("bake_from_cache failed:", ex)
    try:
        with bpy.context.temp_override(scene=scene, point_cache=cache):
            bpy.ops.ptcache.free_bake()
        say("free_bake: baked", cache.is_baked)
    except Exception as ex:
        say("free_bake failed:", ex)
    try:
        bpy.ops.ptcache.free_bake_all()
        say("free_bake_all ok")
    except Exception as ex:
        say("free_bake_all failed:", ex)
    scene.frame_set(1)
    try:
        with bpy.context.temp_override(selected_objects=[box], active_object=box, object=box):
            bpy.ops.rigidbody.bake_to_keyframes(frame_start=1, frame_end=20)
        say("bake_to_keyframes: action", box.animation_data.action.name if box.animation_data else None,
            "rigid body left:", box.rigid_body is not None)
    except Exception as ex:
        say("bake_to_keyframes failed:", ex)


def probe_remove():
    say("== remove")
    scene = fresh()
    box = cube("Box", (0, 0, 1.0))
    rigid(box)
    with bpy.context.temp_override(object=box, active_object=box, selected_objects=[box]):
        bpy.ops.rigidbody.object_remove()
    say("after object_remove:", box.rigid_body, [o.name for o in scene.rigidbody_world.collection.objects])
    # Without the operator: unlinking from the collection.
    rigid(box)
    scene.rigidbody_world.collection.objects.unlink(box)
    say("after unlink:", box.rigid_body)
    # Rigid body via collection link only?
    other = cube("Other", (1, 0, 1))
    scene.rigidbody_world.collection.objects.link(other)
    say("linked into collection:", other.rigid_body)


def main():
    for p in (probe_fall, probe_push, lambda: probe_push(True), probe_throw, probe_field, probe_cache_replay,
              probe_cloth, probe_soft, probe_effectors_on_rigid, probe_bake, probe_remove):
        try:
            p()
        except Exception:
            import traceback
            say(traceback.format_exc())
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_physics_result.txt")
    with open(path, "w", encoding="ascii", errors="replace") as f:
        f.write("\n".join(OUT) + "\n")


main()
