"""Windowed check of the Model mode Stack page, flat-on: the list (two pages), a modifier's settings
(ours, someone else's with Up / Down, the shading's group inputs). Saves tests/stack_preview.png.

blender --factory-startup test_scene.blend --python tests/stack_preview.py
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PNG = os.path.join(ROOT, "tests", "stack_preview.png")
LOG = os.path.join(ROOT, "tests", "stack_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)

HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand: unregister it first (and drop this script's draw
    handlers), or its GPU offscreens are freed after Blender's own teardown and the exit crashes."""
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
    from vr_sculpt import hardsurface, menus, native, primitives, shading, symmetry, vr_ui
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
        vr_ui.ANIMATE = False

        def add(x, hover=None):
            widgets, height = menus.layout(ctx)
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.12)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height, hover))
            log(f"panel at {x}: {len(widgets)} widgets, height {height:.3f}")

        s.mode = 'MODEL'
        obj = primitives.create(ctx, 'CUBE', Vector((0, 5, 0)))
        obj.vr_prim.size = (0.2, 0.2, 0.1)
        hardsurface.set_solidify(obj, True)
        for loc in ((0.05, 5.05, 0.05), (-0.05, 4.95, 0.05)):
            cutter = primitives.create(ctx, 'CYLINDER', Vector(loc))
            cutter.vr_prim.radius = 0.02
            hardsurface.make_cutter(ctx, cutter, obj)
        hardsurface.set_array_count(ctx, obj, 3)
        symmetry.model_toggle(ctx, obj, 0)
        hardsurface.set_bevel(obj, True)
        obj.modifiers.new("Subdivision", 'SUBSURF')
        hardsurface.solidify(obj).show_viewport = False
        primitives.make_active(ctx, obj)
        s.model_page = 'STACK'
        menus.open_sheet('OBJECT')
        add(-0.4, "mod_apply_1")
        menus._stack_pages[obj.name] = 1
        add(-0.16, None)
        menus._stack_open[obj.name] = hardsurface.BEVEL
        add(0.08, "modp_segments")
        menus._stack_open[obj.name] = "Subdivision"
        add(0.32, "mod_up")
        menus._stack_open[obj.name] = shading.MODIFIER
        add(0.56, None)
        log("modifiers:", [m.name for m in obj.modifiers])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.1, 0.0, 0.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 1.15
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
