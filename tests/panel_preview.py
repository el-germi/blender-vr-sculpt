"""Windowed check of the hand panel layout, flat-on. Saves tests/panel_preview.png.

blender --factory-startup test_scene.blend --python tests/panel_preview.py
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PNG = os.path.join(ROOT, "tests", "panel_preview.png")
LOG = os.path.join(ROOT, "tests", "panel_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
VIEWER = Vector((0.0, -0.6, 0.0))


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def draw():
    from vr_sculpt import vr_ui
    try:
        for m, widgets, height, hover in panels:
            vr_ui.draw_panel(m, widgets, height, hover)
    except Exception:
        log(traceback.format_exc())


def setup():
    import vr_sculpt
    from vr_sculpt import menus, native, primitives, vr_ui
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        s = ctx.scene.vr_sculpt

        def add(x, hover=None):
            widgets, height = vr_ui.layout(menus.rows(ctx))
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.09)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height, hover))

        s.mode, s.engine = 'SCULPT', 'PYTHON'
        add(-0.3, "brush_SMOOTH")
        s.mode = 'MODEL'
        add(-0.1)
        cube = primitives.create(ctx, 'CUBE', (0, 5, 0))
        add(0.1, "size_Y")
        cyl = primitives.create(ctx, 'CYLINDER', (0, 5, 0))
        add(0.3, "delete")
        log("panels:", len(panels), "heights:", [round(p[2], 3) for p in panels])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, 0.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.6
        bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')
        bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(win, area, region):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=PNG)
        log("saved", PNG)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
