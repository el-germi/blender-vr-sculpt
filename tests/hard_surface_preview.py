"""Windowed check of the hard surface tools: a beveled plate with a drilled hole, a slot, a union boss
and a radial ring of bolts, with the VR overlay (cutter outlines, snap ring, hand panel on the Cuts
page), then the Model panel's pages side by side.

blender --factory-startup test_scene.blend --python tests/hard_surface_preview.py
(any .blend will do: it's cleared; loading one just keeps the splash screen away)
Saves tests/hard_surface_scene.png, _model.png, _panels.png and _marks.png (the Mark tool at work).
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "hard_surface_preview_result.txt")
lines = []
HANDLES = []  # this script's own draw handlers
panels = []  # (matrix, widgets, height) drawn for the second shot
PANEL_X = 5.0  # the static panels are drawn out here, away from the model


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def quit_cleanly():
    """Unregister by hand (Blender won't, the add-on was registered by this script) after removing
    this script's draw handlers, then quit."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    try:
        import vr_sculpt
        if hasattr(bpy.types.Scene, "vr_sculpt"):
            vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


class FakeSessionState:
    """Controllers held over the plate, the viewer in front of it."""
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -0.75, 0.45))
    viewer_pose_rotation = Quaternion((1, 0, 0), 1.1)
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        down = Quaternion((1, 0, 0), 0.6)
        self.aim_loc = [Vector((-0.42, -0.3, 0.12)), Vector((0.16, -0.12, 0.12))]
        self.aim_rot = [down, down]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + Vector((0.0, -0.03, -0.03))


fake = FakeSessionState()
errors = []
state = {"overlay": True}


def draw():
    from vr_sculpt import draw as vr_draw, vr_ui
    try:
        if state["overlay"]:
            vr_draw.draw_overlay(bpy.context, fake)
        for m, widgets, height in panels:
            vr_ui.draw_panel(m, widgets, height)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def build(ctx):
    from vr_sculpt import hardsurface, menus, primitives, snapping
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    s = ctx.scene.vr_sculpt
    s.show_desktop = s.show_timeline = s.grid_show = False
    menus.set_mode(ctx, 'MODEL')

    plate = primitives.create(ctx, 'CUBE', Vector((0.0, 0.0, 0.0)))
    plate.name = "Plate"
    plate.vr_prim.size = (0.3, 0.2, 0.06)
    ctx.view_layer.update()
    # A hole (its center on the top face, like Snap to Surface leaves a cutter), a slot, a boss.
    hole = primitives.create(ctx, 'CYLINDER', Vector((-0.08, 0.0, 0.03)))
    hole.name = "Hole"
    hole.vr_prim.radius, hole.vr_prim.height, hole.vr_prim.segments = 0.03, 0.1, 48
    hardsurface.make_cutter(ctx, hole, plate)
    slot = primitives.create(ctx, 'CUBE', Vector((0.07, 0.05, 0.03)))
    slot.name = "Slot"
    slot.vr_prim.size = (0.12, 0.02, 0.03)
    hardsurface.make_cutter(ctx, slot, plate)
    boss = primitives.create(ctx, 'CYLINDER', Vector((0.08, -0.04, 0.045)))
    boss.name = "Boss"
    boss.vr_prim.radius, boss.vr_prim.height, boss.vr_prim.segments = 0.03, 0.03, 48
    hardsurface.make_cutter(ctx, boss, plate, 'UNION')
    ctx.view_layer.update()
    hardsurface.set_bevel(plate, True)
    hardsurface.set_bevel_width(plate, 0.003)

    # Bolts around the hole: one small finished cube, a radial array of 8 about the hole's axis.
    ctx.scene.cursor.location = (-0.08, 0.0, 0.04)
    bolt = primitives.create(ctx, 'CUBE', Vector((-0.08, 0.05, 0.035)))
    bolt.name = "Bolt"
    bolt.vr_prim.size = (0.012, 0.012, 0.01)
    ctx.view_layer.update()
    hardsurface.set_bevel(bolt, True)
    hardsurface.set_array_axis(ctx, bolt, 2)
    hardsurface.set_array_count(ctx, bolt, 8)
    hardsurface.set_array_mode(ctx, bolt, 'RADIAL')
    ctx.view_layer.update()
    log("plate modifiers:", [m.name for m in plate.modifiers],
        "solvers:", [m.solver for m in hardsurface.booleans(plate)])
    log("bolt modifiers:", [m.name for m in bolt.modifiers])
    bevel = hardsurface.bevel(plate)
    for on in (False, True):
        bevel.show_viewport = on
        ctx.view_layer.update()
        ev = plate.evaluated_get(ctx.evaluated_depsgraph_get())
        log(f"plate evaluated, bevel {'on' if on else 'off'}: {len(ev.data.vertices)} verts, "
            f"{len(ev.data.polygons)} faces, custom normals {ev.data.has_custom_normals}")
    return plate, hole, slot, boss, bolt


def setup():
    import vr_sculpt
    from vr_sculpt import hardsurface, menus, native, primitives, snapping, vr_ui
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.shading.type = 'SOLID'
        plate, hole, slot, boss, bolt = build(ctx)
        s = ctx.scene.vr_sculpt

        # The static panels: Shape (plate), Cuts (plate), Finish (bolt), Array (bolt, radial).
        def add(i, obj, page):
            primitives.make_active(ctx, obj)
            s.model_page = page
            menus.open_sheet('OBJECT')
            widgets, height = menus.layout(ctx)
            x = PANEL_X + (i - 1.5) * 0.2
            m = vr_ui.panel_matrix(Vector((x, 0.0, 0.0)), Vector((x, -0.6, 0.0)), 1.0)
            panels.append((m, widgets, height))
            log(f"panel {page} for {obj.name}: {len(widgets)} widgets, height {height:.3f}")
        add(0, plate, 'SHAPE')
        add(1, plate, 'BOOLEAN')
        add(2, bolt, 'FINISH')
        add(3, bolt, 'ARRAY')

        # The live overlay: making cuts, the slot cutter selected (its page), a snap ring by the boss.
        s.model_add_as = 'DIFFERENCE'
        s.model_page = 'BOOLEAN'
        s.show_panel = True
        s.panel_pin = True  # The reveal needs a real wrist: keep it shown.
        primitives.make_active(ctx, slot)
        snapping.last_hit = (Vector((0.0, -0.05, 0.03)), Vector((0.0, 0.0, 1.0)))

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (-0.14, -0.12, 0.12)
        rv3d.view_rotation = Quaternion((0, 0, 1), -0.25) @ Quaternion((1, 0, 0), 1.05)
        rv3d.view_distance = 1.15
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (shoot_scene(win, area, region), None)[1], first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def screenshot(win, area, region, name):
    area.tag_redraw()
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
    path = os.path.join(ROOT, "tests", name)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    log("saved", path, "draw errors so far:", len(errors))


def shoot_scene(win, area, region):
    try:
        screenshot(win, area, region, "hard_surface_scene.png")
        state["overlay"] = False
        rv3d = area.spaces.active.region_3d
        from vr_sculpt import hardsurface
        hardsurface.set_cutters_visible(bpy.context, False)
        rv3d.view_location = (-0.06, -0.02, 0.02)
        rv3d.view_rotation = Quaternion((0, 0, 1), -0.5) @ Quaternion((1, 0, 0), 0.95)
        rv3d.view_distance = 0.32
        bpy.app.timers.register(lambda: (shoot_model(win, area, region), None)[1], first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_model(win, area, region):
    try:
        screenshot(win, area, region, "hard_surface_model.png")
        rv3d = area.spaces.active.region_3d
        rv3d.view_location = (PANEL_X, 0.0, -0.1)
        rv3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
        rv3d.view_distance = 1.0
        bpy.app.timers.register(lambda: (shoot_panels(win, area, region), None)[1], first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_panels(win, area, region):
    try:
        screenshot(win, area, region, "hard_surface_panels.png")
        set_up_marks(bpy.context, area)
        bpy.app.timers.register(lambda: (shoot_marks(win, area, region), None)[1], first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def set_up_marks(ctx, area):
    """The Mark tool on the plate: its front top edge marked soft, a side edge of the boss (a Union
    cutter: its marks carry into the result) marked hard, the right tip over the back edge."""
    import numpy as np
    from vr_sculpt import hardsurface, primitives, shading
    plate, boss = bpy.data.objects["Plate"], bpy.data.objects["Boss"]
    hardsurface.set_cutters_visible(ctx, True)
    primitives.make_active(ctx, plate)
    s = ctx.scene.vr_sculpt
    s.model_add_as = 'OBJECT'
    s.model_page = 'FINISH'
    s.model_mark = 'SOFT'
    for obj, point, how in ((plate, (0.0, -0.1, 0.03), 'SOFT'), (boss, (0.11, -0.04, 0.045), 'HARD')):
        near = shading._near(shading._edge_segments(obj), np.array(point), 0.01)
        hard, soft = shading.marks(obj.data)
        (soft if how == 'SOFT' else hard)[near] = True
        shading._write(obj.data, shading.SHARP, hard)
        shading._write(obj.data, shading.SOFT, soft)
        obj.data.update()
        log(f"marked {near.sum()} {how.lower()} edge(s) on {obj.name}")
    fake.aim_loc[1] = Vector((0.02, 0.1, 0.03)) - fake.aim_rot[1] @ Vector((0.0, 0.0, -s.tip_offset))
    state["overlay"] = True
    rv3d = area.spaces.active.region_3d
    rv3d.view_location = (0.0, -0.02, 0.04)
    rv3d.view_rotation = Quaternion((0, 0, 1), -0.3) @ Quaternion((1, 0, 0), 1.0)
    rv3d.view_distance = 0.55


def shoot_marks(win, area, region):
    try:
        screenshot(win, area, region, "hard_surface_marks.png")
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
