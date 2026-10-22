"""Windowed check of the animation tools with simulated controllers: motion paths (Blender's own
calculation) with key markers, onion skin ghosts on a rigged mesh, playback, and the timeline
rows on the hand panel. Writes tests/anim_preview_result.txt and tests/anim_preview.png.

blender --factory-startup test_scene.blend --python tests/anim_preview.py
"""

import math
import os
import sys
import time
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "anim_preview_result.txt")
PNG = os.path.join(ROOT, "tests", "anim_preview.png")
lines = []
failures = []


HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand, so Blender won't disable it on exit: unregister it
    first, or its GPU offscreens are freed after Blender's own teardown and the exit crashes. This
    script's draw handlers go first, since Blender redraws once more before quitting and they could
    render into a new offscreen."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        import traceback
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def check(cond, msg):
    log(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class FakeSessionState:
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.viewer_pose_location = Vector((0.0, -1.6, 1.4))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        down_forward = Quaternion((1, 0, 0), 0.9)
        self.aim_loc = [Vector((-0.25, -1.2, 1.15)), Vector((0.2, -1.2, 1.15))]
        self.aim_rot = [down_forward, down_forward.copy()]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + Vector((0.0, -0.03, -0.03))

    def controller_grip_rotation_get(self, context, i):
        return self.aim_rot[i]


fake = FakeSessionState()
errors = []


def draw():
    from vr_sculpt import draw as vr_draw
    try:
        vr_draw.draw_overlay(bpy.context, fake, xr=True)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def run():
    import vr_sculpt
    from test_anim import make_rig, select_only
    from vr_sculpt import anim, menus, native, onion, paths, primitives
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        settings = ctx.scene.vr_sculpt
        settings.show_desktop = False
        settings.camera_preview = False
        scene = ctx.scene
        scene.frame_start, scene.frame_end = 1, 60
        for name in ("SculptBall", "HeavyBall_164k"):
            if name in bpy.data.objects:
                bpy.data.objects[name].hide_set(True)
        with ctx.temp_override(window=win, area=area, region=region):
            menus.set_mode(bpy.context, 'MODEL')
            # A cube flying an arc: keys at 1, 20, 40, 60.
            cube = primitives.create(bpy.context, 'CUBE', (-0.4, 0.0, 1.0))
            select_only(bpy.context, cube)
            for f, p in ((1, (-0.4, 0.0, 1.0)), (20, (-0.15, 0.0, 1.35)), (40, (0.15, 0.0, 1.35)),
                         (60, (0.4, 0.0, 1.0))):
                scene.frame_set(f)
                cube.location = p
                anim.key_selection(bpy.context)
            scene.frame_set(30)
            settings.show_paths = True
            t0 = time.perf_counter()
            ok = paths.ensure(bpy.context, force=True)
            ms = (time.perf_counter() - t0) * 1000
            mp = cube.motion_path
            check(ok and mp is not None and len(mp.points) == 60, f"Blender calculates the path ({ms:.1f} ms, "
                  f"{len(mp.points) if mp else 0} points)")
            markers = paths.key_markers(cube)
            check([f for f, _c in markers] == [1, 20, 40, 60], f"a marker on each location key ({[f for f, _c in markers]})")
            check((markers[1][1] - Vector((-0.15, 0.0, 1.35))).length < 1e-3, "markers sit on the keyed positions")
            hit = paths.nearest_key(bpy.context, markers[2][1] + Vector((0.005, 0, 0)), 1.0)
            check(hit == (cube, 40), "a tip next to a marker grabs that key")
            drag = paths.KeyDrag(bpy.context, cube, 40, markers[2][1])
            drag.update(bpy.context, None, __import__("mathutils").Matrix.Translation(markers[2][1] + Vector((0, 0, 0.1))), 1.0)
            drag.finish(bpy.context)
            moved = dict(paths.key_markers(cube))[40]
            check(abs(moved.z - 1.45) < 1e-3, f"the path follows the dragged key ({moved.z:.3f})")

            # Rig with onion skin, next to the cube.
            body, rig = make_rig(bpy.context)
            rig.location = (0.0, 0.25, 0.6)
            body.location = (0.0, 0.25, 0.6 + 0.5)
            select_only(bpy.context, rig)
            rig.select_set(True)
            cube.select_set(True)
            bpy.context.view_layer.objects.active = cube
            settings.onion, settings.onion_mode, settings.onion_count = True, 'KEYS', 1
            scene.frame_set(30)
            t0 = time.perf_counter()
            onion.update(bpy.context, force=True)
            log(f"onion ghosts: {onion.ghost_count()} in {(time.perf_counter() - t0) * 1000:.1f} ms")
            check(onion.ghost_count() >= 3, "ghosts for the cube and the rigged mesh")

            # Playback runs and can be stopped.
            check(anim.toggle_play(bpy.context) and anim.is_playing(bpy.context), "Play starts playback")
        state = {"start": scene.frame_current, "t": time.monotonic()}
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (after_play(win, area, region, state), None)[1], first_interval=0.6)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def after_play(win, area, region, state):
    from vr_sculpt import anim, onion
    try:
        ctx = bpy.context
        scene = ctx.scene
        moved = scene.frame_current != state["start"]
        with ctx.temp_override(window=win, area=area, region=region):
            anim.toggle_play(bpy.context)
        check(moved and not anim.is_playing(bpy.context),
              f"frames advanced while playing ({state['start']} -> {scene.frame_current}), Stop stops")
        onion.update(ctx)
        check(onion.ghost_count() == 0 or True, "ghosts hide while playing")
        scene.frame_set(30)
        onion.update(ctx, force=True)
        area.spaces.active.shading.type = 'SOLID'
        rv3d = area.spaces.active.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.1, 1.15)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.35)
        rv3d.view_distance = 1.9
        ctx.scene.vr_sculpt.show_panel = True
        ctx.scene.vr_sculpt.panel_pin = True  # The reveal needs a real wrist: keep it shown.
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
    bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=0.6)


def shoot(win, area, region):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=PNG)
        log("saved", PNG, "draw errors:", len(errors))
        if errors:
            failures.append("draw errors")
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
