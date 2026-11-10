"""Windowed check of the native brush buttons and the brush list, flat-on. Saves tests/brushes_preview.png.

blender --factory-startup test_scene.blend --python tests/brushes_preview.py

Left to right: the Sculpt page (native engine), with More hovered; the list's Transform page (Snake Hook
hovered); its Paint page; the Sculpt page after picking Pose.
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PNG = os.path.join(ROOT, "tests", "brushes_preview.png")
LOG = os.path.join(ROOT, "tests", "brushes_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []


def quit_cleanly():
    """Unregister first (and drop this script's draw handler), or Blender crashes on exit freeing GPU offscreens."""
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
    from vr_sculpt import vr_ui
    try:
        for m, widgets, height, hover in panels:
            vr_ui.draw_panel(m, widgets, height, hover)
    except Exception:
        log(traceback.format_exc())


def setup():
    import vr_sculpt
    from vr_sculpt import menus, native, vr_ui
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
        s.mode = 'SCULPT'
        s.engine = 'NATIVE'
        s.native_picked = ""
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3)
            bpy.ops.object.mode_set(mode='SCULPT')
            native.activate_brush(bpy.context, "Draw")

        def add(x, hover=None):
            widgets, height = menus.layout(ctx)
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.12)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height, hover))

        add(-0.23, "brush_list")
        menus.open_brush_picker(ctx)
        menus._set_category("Transform")
        add(0.0, "opt_Snake Hook")
        menus._set_category("Paint")
        add(0.23, "opt_Airbrush")
        menus.close_picker()
        with ctx.temp_override(window=win, area=area, region=region):
            native.pick_brush(bpy.context, "Pose")
        add(0.46, "brush_picked")
        log("panels:", len(panels), "heights:", [round(p[2], 3) for p in panels])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.115, 0.0, -0.05)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.7
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(win, area, region):
    try:
        for o in list(bpy.data.objects):
            o.hide_viewport = True  # just the panels
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
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
