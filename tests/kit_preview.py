"""Windowed check of kit parts: the Kit sheet on the hand panel (tests/kit_panels.png) and parts
dropped and stamped on a hull (tests/kit_scene.png).

blender --factory-startup test_scene.blend --python tests/kit_preview.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PANELS_PNG = os.path.join(ROOT, "tests", "kit_panels.png")
SCENE_PNG = os.path.join(ROOT, "tests", "kit_scene.png")
LOG = os.path.join(ROOT, "tests", "kit_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """See tests/panel_preview.py: our draw handlers go, then the add-on, then Blender."""
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


def view():
    from vr_sculpt import native
    win = bpy.context.window_manager.windows[0]
    area, region = native.largest_view3d(win)
    return win, area, region, area.spaces.active


def setup():
    import vr_sculpt
    from vr_sculpt import kit, menus, primitives, vr_ui
    try:
        vr_sculpt.register()
        vr_ui.ANIMATE = False
        kit.THUMBS_PER_FRAME = 100  # All of them in the one frame shot.
        ctx = bpy.context
        win, area, region, space = view()
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.show_region_ui = space.show_region_toolbar = False
        space.overlay.show_text = False
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        s = ctx.scene.vr_sculpt
        s.mode = 'MODEL'

        def add(x, hover=None):
            widgets, height = menus.layout(ctx)
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.15)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height, hover))
            return height

        kit.scan(ctx)
        bolt = next(a for a in kit.library(kit.STARTER).assets if a.name == "Hex Bolt")
        menus.choose_part(ctx, bolt.key)
        heights = [add(-0.33, "kit")]  # The Model page: the part as Add, the Kit button.
        menus.open_kit(ctx)
        menus._kit.update(library=kit.STARTER, catalog=None, page=0)
        heights.append(add(-0.11, "part_" + bolt.key))
        menus._kit.update(catalog="Vents")
        vent = next(a for a in kit.library(kit.STARTER).assets if a.name == "Vent Slots")
        menus.choose_part(ctx, vent.key)
        s.kit_stamp = True
        menus.set_more('KIT', True)
        heights.append(add(0.11, "kit_place_stamp"))
        menus.set_more('KIT', False)
        s.kit_stamp = False
        menus.open_catalog_picker(ctx)
        menus._set_category(kit.STARTER)
        heights.append(add(0.33, None))
        menus.close_picker()
        log("panel heights:", [round(h, 3) for h in heights])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, -0.04)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.62
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (shoot_panels(), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(path):
    win, area, region, _space = view()
    area.tag_redraw()
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    log("saved", path)


def shoot_panels():
    try:
        shoot(PANELS_PNG)
        panels.clear()
        build_scene()
        bpy.app.timers.register(lambda: (shoot_scene(), None)[1], first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def build_scene():
    """A hull with parts placed the way the trigger places them: dropped, stamped, cut."""
    from vr_sculpt import hardsurface, kit, primitives
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    hull = primitives.create(ctx, 'CUBE', (0, 0, 0))
    hull.name = "Hull"
    hull.vr_prim.size = (0.6, 0.4, 0.2)
    ctx.view_layer.update()
    parts = {a.name: a for a in kit.library(kit.STARTER).assets}

    def surface_matrix(obj, point, cutter=False):
        lo, hi = primitives.local_bounds(obj)
        anchor = (lo + hi) / 2
        if not cutter:
            anchor.z = lo.z
        rs = obj.matrix_world.to_3x3()
        return Matrix.Translation(Vector(point) - rs @ anchor) @ rs.to_4x4()

    def drop(name, point, size=1.0, turn=0.0):
        obj = kit.place(ctx, parts[name], size)
        ctx.view_layer.update()
        obj.matrix_world = surface_matrix(obj, point) @ Matrix.Rotation(turn, 4, 'Z')
        return obj

    def cut(name, point, size=1.0):
        primitives.make_active(ctx, hull)
        obj = kit.place(ctx, parts[name], size)
        hardsurface.make_cutter(ctx, obj, hull)
        ctx.view_layer.update()
        obj.matrix_world = surface_matrix(obj, point, cutter=True)
        hardsurface.attach(obj, hull)
        return obj

    drop("Hatch", (-0.15, 0.05, 0.1), 1.5)
    drop("Box Cluster", (0.17, -0.08, 0.1), 1.2)
    drop("Ribbed Block", (0.18, 0.1, 0.1), 1.0)
    drop("Pipe Straight", (-0.05, -0.15, 0.1), 1.0)
    drop("Pipe Run", (0.0, 0.17, 0.1), 1.2)
    cut("Vent Slots", (0.0, 0.0, 0.1), 1.5)
    cut("Grille", (-0.15, -0.1, 0.1), 1.0)
    # A row of stamped bolts along the front edge of the top, as the Stamp sweep places them.
    stamp_parts = [(-0.27 + 0.045 * i, -0.18, 0.1) for i in range(13)]
    for p in stamp_parts:
        obj = kit.place(ctx, parts["Hex Bolt"], 1.0, select=False)
        ctx.view_layer.update()
        obj.matrix_world = surface_matrix(obj, p)
    for p in [(-0.27 + 0.045 * i, 0.18, 0.1) for i in range(5)]:
        obj = kit.place(ctx, parts["Rivet"], 1.4, select=False)
        ctx.view_layer.update()
        obj.matrix_world = surface_matrix(obj, p)
    primitives.make_active(ctx, None)
    for cutter in hardsurface.cutters(hull):
        cutter.hide_set(True)  # So the cuts show.
    _win, _area, _region, space = view()
    space.shading.type = 'SOLID'
    space.shading.light = 'STUDIO'
    space.overlay.show_floor = True
    rv3d = space.region_3d
    rv3d.view_location = (0.0, 0.0, 0.05)
    rv3d.view_rotation = Quaternion((0.82, 0.45, 0.17, 0.31)).normalized()
    rv3d.view_distance = 1.0
    log("scene objects:", len(bpy.data.objects), "booleans on hull:", len(hardsurface.booleans(hull)))


def shoot_scene():
    try:
        shoot(SCENE_PNG)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
