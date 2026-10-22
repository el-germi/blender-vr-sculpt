"""Windowed check of the full VR overlay (rings, card, hand panel) with simulated controllers.

blender --factory-startup test_scene.blend --python tests/overlay_preview.py
Saves tests/overlay_<name>.png for each scenario.
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "overlay_preview_result.txt")
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
    print(s)
    lines.append(s)


class FakeSessionState:
    """Two controllers held in front of a viewer at the scene camera."""
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -0.6, 1.45))
    viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)  # looking along +Y
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
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
    from vr_sculpt import menus, primitives
    menus.set_mode(ctx, 'MODEL')
    cube = primitives.create(ctx, 'CUBE', (-0.05, -0.1, 1.3))
    cube.vr_prim.size = (0.12, 0.08, 0.1)
    ctx.view_layer.update()
    # Left tip inside the cube (bounds highlight), right tip on its +X face handle (its chips show).
    offset = Vector((0, 0, -ctx.scene.vr_sculpt.tip_offset))
    fake.aim_loc[0] = Vector((-0.05, -0.1, 1.3)) - fake.aim_rot[0] @ offset
    fake.aim_loc[1] = Vector((0.01, -0.1, 1.3)) - fake.aim_rot[1] @ offset


def scenario_draw(ctx):
    """A red tube spiral (mirrored on X around the cursor), a blue ribbon and a yellow note."""
    import numpy as np
    from vr_sculpt import menus, strokes
    menus.set_mode(ctx, 'DRAW')
    s = ctx.scene.vr_sculpt
    s.mirror_x, s.draw_tool, s.draw_style, s.draw_color = True, 'DRAW', 'TUBE', 'RED'
    ctx.scene.cursor.location = (0.0, -0.1, 1.25)
    for o in [o for o in bpy.data.objects if o.name.startswith(("Cube", "Cylinder"))]:
        bpy.data.objects.remove(o)
    for name in ("SculptBall", "HeavyBall_164k"):
        if name in bpy.data.objects:
            bpy.data.objects[name].hide_set(True)
    # Look from a bit to the side so the X mirror plane shows as a grid, not a line.
    from vr_sculpt import native
    area, _region = native.largest_view3d(ctx.window_manager.windows[0])
    rv3d = area.spaces.active.region_3d
    rv3d.view_rotation = Quaternion((0, 0, 1), -0.6) @ Quaternion((1, 0, 0), 1.35)
    rv3d.view_location = (0.0, -0.1, 1.25)
    rv3d.view_distance = 0.7

    def make(style, color, pts, widths, sides, sym=0):
        origin = pts[0]
        obj = strokes.new_object(ctx, style, color, origin)
        samples = {"style": style, "sym": sym, "c": np.array(ctx.scene.cursor.location[:]) - origin,
                   "p": pts - origin, "w": widths, "s": sides}
        strokes.rebuild(obj, samples)
        strokes.save(obj, samples)

    t = np.linspace(0, 3 * np.pi, 120)
    spiral = np.stack([0.05 + 0.04 * np.cos(t) + t * 0.01, -0.12 + 0.04 * np.sin(t), 1.18 + t * 0.012], axis=1)
    make('TUBE', 'RED', spiral, 0.004 + 0.008 * np.sin(np.linspace(0, np.pi, 120)), np.tile([0, 0, 1.0], (120, 1)), 1)
    u = np.linspace(0, 1, 60)
    band = np.stack([-0.12 + 0.24 * u, -0.05 + 0 * u, 1.36 + 0.03 * np.sin(u * 6)], axis=1)
    sides = np.stack([0 * u, np.cos(u * 3), np.sin(u * 3)], axis=1)
    make('RIBBON', 'BLUE', band, np.full(60, 0.03), sides)
    layer, frame = strokes.notes_frame(ctx, 'YELLOW')
    note = frame.strokes.new()
    note.display_mode = '3DSPACE'
    pts = [(0.1 + 0.08 * np.cos(a), -0.15, 1.08 + 0.03 * np.sin(a)) for a in np.linspace(0, 2 * np.pi, 40)]
    note.points.add(len(pts))
    for p, c in zip(note.points, pts):
        p.co = c
    s.show_panel = True


SCENARIOS = [("python", scenario_python), ("native", scenario_native), ("model", scenario_model),
             ("draw", scenario_draw)]


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
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
