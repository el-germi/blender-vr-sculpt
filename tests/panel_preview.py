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
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.12)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height, hover))

        import addon_utils
        from vr_sculpt import catalog
        addon_utils.enable("bl_ext.blender_org.extra_mesh_objects", default_set=True)
        catalog.entries(refresh=True)

        # Row 1: model panel with the Add select, the category picker, the Gears page.
        s.mode = 'MODEL'
        add(-0.4, "add")
        menus.open_add_picker(ctx)
        add(-0.2, "cat_Gears")
        menus._set_category("Gears")
        add(0.0, None)
        menus._set_category("Mesh")
        add(0.2, "opt_mesh.primitive_torus_add")
        menus.close_picker()
        # Gear settings page 1, torus with its Dimensions Mode select.
        gear_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_gear")
        primitives.create_from_entry(ctx, gear_key, (0, 5, 0))
        add(0.4, "param_number_of_teeth")
        torus_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_torus_add")
        primitives.create_from_entry(ctx, torus_key, (0, 5, 0))
        add(0.6, "param_mode")
        log("panels:", len(panels), "heights:", [round(p[2], 3) for p in panels])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.1, 0.0, 0.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.95
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
