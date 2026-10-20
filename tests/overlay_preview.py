"""Windowed check of the full VR overlay (rings, card, hand panel) with simulated controllers.

blender --factory-startup test_scene.blend --python tests/overlay_preview.py
Saves tests/overlay_<name>.png for each scenario.
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "overlay_preview_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


class FakeSessionState:
    """Two controllers held in front of a viewer at the scene camera."""
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -0.6, 1.45))

    def __init__(self):
        down_forward = Quaternion((1, 0, 0), 0.9)  # tilt local -Z from straight down toward +Y
        self.aim_loc = [Vector((-0.18, -0.35, 1.2)), Vector((0.18, -0.35, 1.2))]
        self.aim_rot = [down_forward, down_forward]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return Vector((-0.18 if i == 0 else 0.18, -0.38, 1.17))


fake = FakeSessionState()
errors = []


def draw():
    from vr_sculpt import draw as vr_draw
    try:
        vr_draw.draw_overlay(bpy.context, fake)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def scenario_python(ctx):
    s = ctx.scene.vr_sculpt
    s.mode, s.engine, s.brush = 'SCULPT', 'PYTHON', 'SMOOTH'


def scenario_native(ctx):
    from vr_sculpt import menus, native
    obj = bpy.data.objects["SculptBall"]
    ctx.view_layer.objects.active = obj
    menus.set_engine(ctx, 'NATIVE')
    native.activate_brush(ctx, "Inflate/Deflate")


def scenario_model(ctx):
    from vr_sculpt import interactions, menus, primitives, vr_ui
    menus.set_mode(ctx, 'MODEL')
    cube = primitives.create(ctx, 'CUBE', (-0.05, -0.1, 1.3))
    cube.vr_prim.size = (0.12, 0.08, 0.1)
    ctx.view_layer.update()
    # Left tip inside the cube (bounds highlight), right ray on the "Size Y" slider.
    fake.aim_loc[0] = Vector((-0.05, -0.1, 1.3)) - fake.aim_rot[0] @ Vector((0, 0, -ctx.scene.vr_sculpt.tip_offset))
    matrix, widgets, height = interactions.panel_frame(ctx, fake)
    w = vr_ui.find(widgets, "size_Y")
    target = matrix @ Vector(((w.rect[0] + w.rect[2]) / 2, (w.rect[1] + w.rect[3]) / 2, 0.0))
    normal = (matrix.to_3x3() @ Vector((0, 0, 1))).normalized()
    fake.aim_loc[1] = target + normal * 0.25 + Vector((0.12, 0, -0.08))
    fake.aim_rot[1] = (target - fake.aim_loc[1]).to_track_quat('-Z', 'Y')


SCENARIOS = [("python", scenario_python), ("native", scenario_native), ("model", scenario_model)]


def setup():
    import vr_sculpt
    from vr_sculpt import native
    try:
        vr_sculpt.register()
        win = bpy.context.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, -0.15, 1.25)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
        rv3d.view_distance = 0.55
        bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')
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
    bpy.app.timers.register(lambda: (shoot(win, area, region, i), None)[1], first_interval=0.8)


def shoot(win, area, region, i):
    name = SCENARIOS[i][0]
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        path = os.path.join(ROOT, "tests", f"overlay_{name}.png")
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=path)
        log("saved", path, "draw errors so far:", len(errors))
    except Exception:
        log(traceback.format_exc())
    step(win, area, region, i + 1)


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
