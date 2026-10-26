"""Windowed check of the Sculpt page's dyntopo rows (native engine), flat-on. Saves tests/dyntopo_panels.png.

blender --factory-startup test_scene.blend --python tests/dyntopo_preview.py

Three panels, Sculpt > More open: dyntopo off, on with Brush detail, on with Constant detail.
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PNG = os.path.join(ROOT, "tests", "dyntopo_panels.png")
LOG = os.path.join(ROOT, "tests", "dyntopo_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Unregister first (see tests/panel_preview.py), after this script's own draw handlers."""
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
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, location=(0, 5, 0))  # With UVs: Show texture, Auto
        s.engine = 'NATIVE'
        menus.reset()
        menus.set_mode(ctx, 'SCULPT')
        menus.set_more('SCULPT', True)

        def add(x, hover=None):
            widgets, height = menus.layout(ctx)
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.15)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height, hover))

        add(-0.25, "dyntopo")
        native.toggle_dyntopo(ctx)
        sculpt = ctx.tool_settings.sculpt
        sculpt.detail_type_method = 'BRUSH'
        add(0.0, "detail_size")
        sculpt.detail_type_method = 'CONSTANT'
        sculpt.constant_detail_resolution = 200.0
        add(0.25, "detail_fill")
        log("dyntopo:", ctx.active_object.use_dynamic_topology_sculpting,
            "| heights:", [round(p[2], 3) for p in panels])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, -0.02)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.75
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
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
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
