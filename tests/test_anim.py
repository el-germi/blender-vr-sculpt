"""Background tests for animation: keys, interpolation, frame stepping and jumping, the timeline
rows, auto-key after a VR grab, key dragging on motion paths, and onion skin ghosts on a small
rigged character.

blender --background --factory-startup --python tests/test_anim.py
"""

import math
import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import anim, interactions, menus, onion, paths, primitives, vr_ui  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def select_only(ctx, obj):
    for o in ctx.view_layer.objects:
        if o is not None:
            o.select_set(False)
    obj.select_set(True)
    ctx.view_layer.objects.active = obj


def make_rig(ctx):
    """A subdivided cylinder bent by a 3-bone armature: B1 rotates 0 / 0.8 / -0.8 at frames 1 / 20 / 40."""
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.1, depth=1.0, location=(3.0, 0.0, 0.5))
    body = ctx.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=12)
    bpy.ops.object.mode_set(mode='OBJECT')
    data = bpy.data.armatures.new("TestRig")
    rig = bpy.data.objects.new("TestRig", data)
    rig.location = (3.0, 0.0, 0.0)
    ctx.scene.collection.objects.link(rig)
    ctx.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    prev = None
    for i in range(3):
        b = data.edit_bones.new(f"B{i}")
        b.head, b.tail = (0, 0, i / 3), (0, 0, (i + 1) / 3)
        if prev:
            b.parent, b.use_connect = prev, True
        prev = b
    bpy.ops.object.mode_set(mode='OBJECT')
    select_only(ctx, body)
    rig.select_set(True)
    ctx.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type='ARMATURE_AUTO')
    pb = rig.pose.bones["B1"]
    pb.rotation_mode = 'XYZ'
    for f, a in ((1, 0.0), (20, 0.8), (40, -0.8)):
        pb.rotation_euler.x = a
        pb.keyframe_insert("rotation_euler", frame=f)
    return body, rig


def test_keys():
    print("== keys and time")
    ctx = bpy.context
    scene = ctx.scene
    scene.frame_start, scene.frame_end = 1, 60
    cube = primitives.create(ctx, 'CUBE', (0.0, 0.0, 0.0))
    select_only(ctx, cube)
    scene.frame_set(1)
    check(anim.key_selection(ctx) == 1, "Key keys the selection")
    check(anim.key_frames([cube]) == [1.0], f"one key at frame 1 ({anim.key_frames([cube])})")
    paths_keyed = {fc.data_path for fc in anim.fcurves(cube)}
    check(paths_keyed == {"location", "rotation_euler", "scale"}, f"location, rotation and scale ({paths_keyed})")
    scene.frame_set(30)
    cube.location = (1.0, 0.0, 0.0)
    anim.key_selection(ctx)
    scene.frame_set(50)
    cube.location = (1.0, 1.0, 0.0)
    anim.key_selection(ctx)
    check(anim.key_frames([cube]) == [1.0, 30.0, 50.0], "three keys")
    scene.frame_set(15)
    check(abs(cube.location.x - 0.5) < 0.2, f"in between it moves ({cube.location.x:.2f})")
    check(anim.jump_key(ctx, 1) == 30 and scene.frame_current == 30, "next key")
    check(anim.jump_key(ctx, 1) == 50, "next key again")
    check(anim.jump_key(ctx, 1) is None and scene.frame_current == 50, "no key after the last")
    check(anim.jump_key(ctx, -1) == 30, "previous key")
    anim.step(ctx, 1)
    check(scene.frame_current == 31, "step forward")
    anim.set_frame(ctx, 500)
    check(scene.frame_current == 60, "frames stay inside the range")
    anim.set_frame(ctx, 30)
    check(anim.interpolation_at(ctx) == 'BEZIER', "keys start as Bezier")
    check(anim.set_interpolation(ctx, 'CONSTANT') > 0 and anim.interpolation_at(ctx) == 'CONSTANT',
          "interpolation of the keys at this frame")
    scene.frame_set(40)
    check(close(cube.location, (1.0, 0.0, 0.0)), "constant holds the pose until the next key")
    scene.frame_set(50)
    check(anim.delete_selection_keys(ctx) == 1 and anim.key_frames([cube]) == [1.0, 30.0], "Del key")
    scene.frame_set(20)
    anim.set_start(ctx)
    scene.frame_set(45)
    anim.set_end(ctx)
    check((scene.frame_start, scene.frame_end) == (20, 45), "Start / End set the range here")
    scene.frame_start, scene.frame_end = 1, 60
    return cube


def test_timeline_rows(cube):
    print("== timeline rows")
    ctx = bpy.context
    settings = ctx.scene.vr_sculpt
    settings.show_timeline = True
    menus.set_mode(ctx, 'MODEL')
    select_only(ctx, cube)
    class SS:
        viewer_scale = 1.0
        viewer_pose_location = Vector((0.0, -0.6, 1.5))

        def controller_grip_location_get(self, context, i):
            return Vector((-0.2, -0.3, 1.1))
    frames = interactions.panel_frames(ctx, SS())
    check(len(frames) == 2 and frames[1][3] > vr_ui.WIDTH, "the timeline is a wider strip beside the hand panel")
    main_height = frames[0][2]
    check((frames[1][0].translation - frames[0][0].translation).length > vr_ui.WIDTH / 2,
          "next to it, not on top of it")
    widgets, height = frames[1][1], frames[1][2]
    ids = {w.id for w in widgets}
    need = {"frame", "key_prev", "frame_prev", "play", "frame_next", "key_next", "key_insert", "key_delete",
            "autokey", "interp", "range_start", "range_end", "show_paths", "onion"}
    check(need <= ids, f"timeline rows (missing {sorted(need - ids)})")
    frame = vr_ui.find(widgets, "frame")
    check(len(frame.ticks) == 2 and all(0 <= t <= 1 for t in frame.ticks), f"key ticks on the scrubber ({frame.ticks})")
    frame.on_change(40)
    check(ctx.scene.frame_current == 40, "scrubbing sets the frame")
    settings.show_timeline = False
    frames2 = interactions.panel_frames(ctx, SS())
    check(len(frames2) == 1 and abs(frames2[0][2] - main_height) < 1e-9, "Timeline off hides it (panel unchanged)")
    settings.show_timeline = True
    tool = ctx.scene.tool_settings
    tool.use_keyframe_insert_auto = False
    vr_ui.find(widgets, "autokey").on_press()
    check(tool.use_keyframe_insert_auto, "Auto turns on Blender's Auto Keying")


def test_autokey(cube):
    print("== auto-key after a VR grab")
    ctx = bpy.context
    scene = ctx.scene
    scene.tool_settings.use_keyframe_insert_auto = True
    scene.frame_set(10)
    mv = interactions.MoveObject(ctx, cube, Matrix.Translation(cube.location))
    mv.update(ctx, Matrix(), Matrix.Translation(cube.location + Vector((0, 0, 0.5))), 1.0)
    mv.finish(ctx)
    check(10.0 in anim.key_frames([cube]), "letting go keys the object at the current frame")
    scene.frame_set(9)
    scene.frame_set(10)
    check(abs(cube.location.z - 0.5) < 1e-4 or cube.location.z > 0.4, "and the key holds the new pose")
    scene.tool_settings.use_keyframe_insert_auto = False
    scene.frame_set(12)
    before = anim.key_frames([cube])
    mv = interactions.MoveObject(ctx, cube, Matrix.Translation(cube.location))
    mv.update(ctx, Matrix(), Matrix.Translation(cube.location + Vector((0, 0.2, 0))), 1.0)
    mv.finish(ctx)
    check(anim.key_frames([cube]) == before, "with Auto off, no key")


def test_key_drag(cube):
    print("== dragging a key on the motion path")
    ctx = bpy.context
    scene = ctx.scene
    scene.frame_set(1)
    def loc_keys():
        return {fc.array_index: k for fc in anim.fcurves(cube) if fc.data_path == "location"
                for k in fc.keyframe_points if abs(k.co[0] - 30) < 0.5}
    before = {i: (k.co[1], k.handle_left[1], k.handle_right[1]) for i, k in loc_keys().items()}
    drag = paths.KeyDrag(ctx, cube, 30, Vector((1.0, 0.0, 0.0)))
    drag.update(ctx, Matrix(), Matrix.Translation((1.2, 0.0, 0.3)), 1.0)
    drag.finish(ctx)
    after = loc_keys()
    check(abs(after[0].co[1] - (before[0][0] + 0.2)) < 1e-5 and abs(after[2].co[1] - (before[2][0] + 0.3)) < 1e-5,
          "the key moved with the hand")
    check(abs(after[0].handle_left[1] - (before[0][1] + 0.2)) < 1e-5, "its handles came along")
    scene.frame_set(30)
    check(abs(cube.matrix_world.translation.x - (before[0][0] + 0.2)) < 1e-4,
          "the object is there at that frame")
    # Parented: a world move becomes a parent-space key change.
    parent = bpy.data.objects.new("ScaledParent", None)
    ctx.scene.collection.objects.link(parent)
    parent.scale = (2.0, 2.0, 2.0)
    ctx.view_layer.update()
    child = primitives.create(ctx, 'CUBE', (0.0, 0.0, 0.0))
    child.parent = parent
    child.keyframe_insert("location", frame=5)
    drag = paths.KeyDrag(ctx, child, 5, Vector((0.0, 0.0, 0.0)))
    drag.update(ctx, Matrix(), Matrix.Translation((0.4, 0.0, 0.0)), 1.0)
    k = next(k for fc in anim.fcurves(child) if fc.data_path == "location" and fc.array_index == 0
             for k in fc.keyframe_points)
    check(abs(k.co[1] - 0.2) < 1e-5, f"under a 2x parent, 40 cm in the world is 20 cm of key ({k.co[1]:.3f})")


def test_onion():
    print("== onion skin")
    ctx = bpy.context
    scene = ctx.scene
    settings = scene.vr_sculpt
    body, rig = make_rig(ctx)
    select_only(ctx, rig)
    scene.frame_set(10)
    settings.onion, settings.onion_mode, settings.onion_count = True, 'KEYS', 1
    check(onion.meshes(ctx) == [body], "an armature's deformed mesh is ghosted")
    check(onion.ghost_frames(ctx) == [(1, -1), (20, 1)], f"ghosts at the keys around frame 10 ({onion.ghost_frames(ctx)})")
    check(onion.update(ctx), "ghosts are computed")
    check(scene.frame_current == 10, "and the frame is put back")
    check(onion.ghost_count() == 2, f"two ghosts ({onion.ghost_count()})")
    before = onion._ghosts[[i for i, g in enumerate(onion._ghosts) if g[0] == onion.BEFORE_COLOR][0]][2]
    after = onion._ghosts[[i for i, g in enumerate(onion._ghosts) if g[0] == onion.AFTER_COLOR][0]][2]
    check(np.abs(before[:, 1]).max() < 0.12 and np.abs(after[:, 1]).max() > 0.3,
          f"frame 1 is straight, frame 20 is bent ({np.abs(before[:, 1]).max():.2f} / {np.abs(after[:, 1]).max():.2f})")
    v = onion._state["version"]
    check(not onion.update(ctx) and onion._state["version"] == v, "nothing changed: the cache is reused")
    scene.frame_set(25)
    check(onion.update(ctx) and onion.ghost_frames(ctx) == [(20, -1), (40, 1)], "a new frame: new ghosts")
    pb = rig.pose.bones["B1"]
    pb.rotation_euler.x = 0.3
    pb.keyframe_insert("rotation_euler", frame=30)
    onion.mark_dirty()
    check(onion.update(ctx) and (30, 1) in onion.ghost_frames(ctx), "new keys: ghosts move to them")
    settings.onion_mode, settings.onion_count, settings.onion_step = 'FRAMES', 2, 5
    check(onion.ghost_frames(ctx) == [(20, -1), (15, -1), (30, 1), (35, 1)], f"frame step mode ({onion.ghost_frames(ctx)})")
    onion.update(ctx)
    alphas = sorted({round(g[1], 3) for g in onion._ghosts})
    check(len(alphas) == 2 and alphas[0] < alphas[1], "further ghosts are fainter")
    settings.onion = False
    onion.update(ctx)
    check(onion.ghost_count() == 0, "Onion off clears them")


def main():
    vr_sculpt.register()
    try:
        cube = test_keys()
        test_timeline_rows(cube)
        test_autokey(cube)
        test_key_drag(cube)
        test_onion()
    except Exception:
        traceback.print_exc()
        failures.append("raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
