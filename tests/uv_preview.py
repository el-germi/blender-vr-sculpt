"""Windowed check of the UV page: the hand panel's UV page, and a seamed sphere with the checker view, then
the stretch view (the custom shader compiles and draws). Saves tests/uv_preview_checker.png and
tests/uv_preview_stretch.png.

blender --factory-startup some.blend --python tests/uv_preview.py
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "uv_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Our draw handlers first, then the add-on (its GPU offscreens), then quit (see panel_preview.py)."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def draw():
    import gpu
    from vr_sculpt import uv, vr_ui
    try:
        for m, widgets, height, hover in panels:
            vr_ui.draw_panel(m, widgets, height, hover)
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        uv.draw(shader, bpy.context, [Vector((0.0, -0.1, 0.0))], 1.0)
    except Exception:
        log(traceback.format_exc())


def setup():
    import vr_sculpt
    from vr_sculpt import menus, native, primitives, uv, vr_ui
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
        s.mode = 'MODEL'

        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.12, location=(0.18, 0.0, 0.0))
        ball = ctx.active_object
        primitives.make_active(ctx, ball)
        from vr_sculpt import shading
        segs = shading._edge_segments(ball)
        seam = (np.abs(segs[:, :, 1]) < 1e-6).all(1) & (segs[:, :, 0] > 0.18 - 1e-6).all(1)
        uv.set_seams(ball.data, seam)
        log("unwrap:", uv.unwrap(ctx, ball), uv.message())

        s.model_page = 'UV'
        menus.open_sheet('OBJECT')
        menus.set_more('OBJECT_UV', True)
        s.uv_tool = 'SEAM'
        widgets, height = menus.layout(ctx)
        m = vr_ui.panel_matrix(Vector((-0.15, 0.0, -0.12)), Vector((-0.15, -0.6, 0.0)), 1.0)
        panels.append((m, widgets, height, "uv_tool_seam"))
        log("panel height:", round(height, 3), "widgets:", len(widgets))
        page_row = [w for w in widgets if w.id and w.id.startswith("page_")]
        log("page tabs:", [(w.id, w.text, round(w.rect[2], 4) if len(w.rect) > 2 else None) for w in page_row])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.03, 0.0, -0.04)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.75
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        s.uv_view = 'CHECKER'
        uv.tick(ctx)
        log("checker arrays:", uv._view["arrays"] is not None)
        bpy.app.timers.register(lambda: (shoot(win, area, region, "checker"), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(win, area, region, name):
    from vr_sculpt import uv
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        png = os.path.join(ROOT, "tests", f"uv_preview_{name}.png")
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=png)
        log("saved", png)
        if name == "checker":
            bpy.context.scene.vr_sculpt.uv_view = 'STRETCH'
            uv.tick(bpy.context)
            log("stretch arrays:", uv._view["arrays"] is not None)
            bpy.app.timers.register(lambda: (shoot(win, area, region, "stretch"), None)[1], first_interval=0.5)
            return
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
