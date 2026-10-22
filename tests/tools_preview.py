"""Windowed check of the Tools mode visuals with simulated controllers: grid, tape measurements,
the camera viewfinder and pinned preview, and the floating desktop window.

blender --factory-startup test_scene.blend --python tests/tools_preview.py
Saves tests/tools_<name>.png for each scenario.
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "tools_preview_result.txt")
lines = []


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


class FakeSessionState:
    """Viewer at (0, -0.6, 1.45) looking along +Y, controllers in front, no navigation offset."""
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.viewer_pose_location = Vector((0.0, -0.6, 1.45))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        down_forward = Quaternion((1, 0, 0), 0.9)
        self.aim_loc = [Vector((-0.18, -0.35, 1.2)), Vector((0.18, -0.35, 1.2))]
        self.aim_rot = [down_forward, down_forward.copy()]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return Vector((-0.18 if i == 0 else 0.18, -0.38, 1.17))

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


def render_camera():
    from vr_sculpt import camera
    try:
        camera.render(bpy.context, force=True)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def common(ctx):
    from vr_sculpt import desktop, measure, menus
    s = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'TOOLS')
    s.grid_show, s.grid_snap, s.grid_size = True, True, 0.04
    measure.clear(ctx.scene)
    measure.add(ctx.scene, (-0.25, 0.0, 1.0), (0.25, 0.0, 1.0))
    measure.add(ctx.scene, (0.25, 0.0, 1.0), (0.25, 0.0, 1.5))


def scenario_measure(ctx):
    from vr_sculpt import desktop
    common(ctx)
    s = ctx.scene.vr_sculpt
    s.tool = 'MEASURE'
    s.camera_preview = True
    s.show_desktop = True
    s.desktop_size = 0.6
    s.desktop_keyboard = True
    desktop.state["force"] = True
    desktop.state["pose"] = None
    # Behind the scene, so it doesn't cover everything.
    world = Matrix.LocRotScale(Vector((0.0, 0.35, 1.3)), Quaternion((1, 0, 0), math.pi / 2), None)
    desktop.set_world_matrix(fake, world)
    desktop.request_full_capture(ctx)


def scenario_camera(ctx):
    from vr_sculpt import desktop
    common(ctx)
    s = ctx.scene.vr_sculpt
    s.tool = 'CAMERA'
    s.show_desktop = False


SCENARIOS = [("measure", scenario_measure), ("camera", scenario_camera)]


def setup():
    import vr_sculpt
    from vr_sculpt import native
    try:
        vr_sculpt.register()
        win = bpy.context.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, -0.05, 1.25)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
        rv3d.view_distance = 1.1
        cams = [o for o in bpy.context.scene.objects if o.type == 'CAMERA']
        log("scene camera:", bpy.context.scene.camera.name if bpy.context.scene.camera else None,
            "cameras:", [c.name for c in cams])
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(render_camera, (), 'WINDOW', 'POST_PIXEL'))
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        step(win, area, region, 0)
    except Exception:
        log(traceback.format_exc())
        done()


def step(win, area, region, i):
    if i >= len(SCENARIOS):
        done()
        return
    name, fn = SCENARIOS[i]
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            fn(bpy.context)
    except Exception:
        log(f"{name} setup failed:\n" + traceback.format_exc())
    for a in win.screen.areas:
        a.tag_redraw()
    bpy.app.timers.register(lambda: (shoot(win, area, region, i), None)[1], first_interval=1.2)


def shoot(win, area, region, i):
    from vr_sculpt import desktop
    name = SCENARIOS[i][0]
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=3)
        log(f"{name}: desktop captures {len(desktop._captures)}")
        path = os.path.join(ROOT, "tests", f"tools_{name}.png")
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=path)
        log("saved", path, "draw errors so far:", len(errors))
    except Exception:
        log(traceback.format_exc())
    step(win, area, region, i + 1)


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
