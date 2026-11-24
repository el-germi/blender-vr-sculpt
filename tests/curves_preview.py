"""Windowed check of Bezier curves: the Draw panel with the Curve and Edit tools (tests/curves_panels.png),
then curves in the scene as the headset draws them: one being built with the next segment to the tip
(tests/curves_build.png) and the Edit tool's points and handles, with a handle under the tip
(tests/curves_edit.png).

blender --factory-startup test_scene.blend --python tests/curves_preview.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PANELS_PNG = os.path.join(ROOT, "tests", "curves_panels.png")
BUILD_PNG = os.path.join(ROOT, "tests", "curves_build.png")
EDIT_PNG = os.path.join(ROOT, "tests", "curves_edit.png")
LOG = os.path.join(ROOT, "tests", "curves_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []
state = {"overlay": False}


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
    print(s, flush=True)
    lines.append(s)


class FakeSessionState:
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.viewer_pose_location = Vector((0.0, -0.9, 1.45))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        down_forward = Quaternion((1, 0, 0), 0.9)
        self.aim_loc = [Vector((-0.45, -0.3, 1.0)), Vector((0.16, -0.3, 1.22))]
        self.aim_rot = [down_forward, down_forward.copy()]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + Vector((0.0, -0.03, -0.03))

    def controller_grip_rotation_get(self, context, i):
        return self.aim_rot[i]


fake = FakeSessionState()


def draw():
    from vr_sculpt import draw as vr_draw, vr_ui
    try:
        for m, widgets, height, hover in panels:
            vr_ui.draw_panel(m, widgets, height, hover)
        if state["overlay"]:
            vr_draw.draw_overlay(bpy.context, fake, xr=True)
    except Exception:
        log(traceback.format_exc())


def view():
    from vr_sculpt import native
    win = bpy.context.window_manager.windows[0]
    area, region = native.largest_view3d(win)
    return win, area, region, area.spaces.active


def add(ctx, x, hover=None, y=-0.15):
    from vr_sculpt import menus, vr_ui
    widgets, height = menus.layout(ctx)
    m = vr_ui.panel_matrix(Vector((x, 0.0, y)), Vector((x, -0.6, y + 0.15)), 1.0)
    panels.append((m, widgets, height, hover))
    return height


def place(ctx, s, points, drag=None):
    """Points placed with the Curve tool as the trigger would (drag: {index: tip offset while held})."""
    from vr_sculpt import curves
    drag = drag or {}
    for i, p in enumerate(points):
        h = curves.PlacePoint(ctx, s, Matrix.Translation(p), 1.0)
        if i in drag:
            h.update(ctx, None, Matrix.Translation(Vector(p) + Vector(drag[i])), 1.0)
        h.finish(ctx)


def setup():
    import vr_sculpt
    from vr_sculpt import curves, menus, vr_ui
    try:
        vr_sculpt.register()
        vr_ui.ANIMATE = False
        ctx = bpy.context
        win, area, region, space = view()
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.show_region_ui = space.show_region_toolbar = False
        space.overlay.show_text = False
        s = ctx.scene.vr_sculpt
        menus.set_mode(ctx, 'DRAW')
        s.draw_tool, s.draw_color, s.draw_width = 'CURVE', 'ORANGE', 0.01
        heights = [add(ctx, -0.33, "tool_CURVE")]
        place(ctx, s, [(5, 5, 5), (5.1, 5, 5)])  # An open curve: the Finish icon shows.
        heights.append(add(ctx, -0.11, "end_curve"))
        curves.end(ctx)
        for o in [o for o in bpy.data.objects if curves.is_ours(o)]:
            curves.delete(o)
        s.draw_tool = 'EDIT'
        heights.append(add(ctx, 0.11, "tool_EDIT"))
        s.draw_tool = 'ERASE'
        heights.append(add(ctx, 0.33, "tool_ERASE"))
        log("panel heights:", [round(h, 3) for h in heights])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, -0.04)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.8
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
        bpy.app.timers.register(lambda: (shoot_build(), None)[1], first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def build_scene():
    from vr_sculpt import curves
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.show_panel = False
    for o in list(bpy.data.objects):
        if o.type == 'MESH' and o.name not in {"Ball", "Sphere"}:
            o.hide_set(True)
    # A finished, closed swirl (smooth points, one dragged out) in blue...
    s.draw_color, s.draw_width = 'BLUE', 0.012
    ring = [(0.35 * math.cos(a), 0.25 + 0.35 * math.sin(a), 1.05 + 0.08 * math.sin(2 * a))
            for a in [k * 2 * math.pi / 6 for k in range(6)]]
    place(ctx, s, ring + [ring[0]], drag={2: (-0.06, 0.06, 0.05)})
    # ...and an open orange one being built, its next segment following the tip.
    s.draw_color, s.draw_width = 'ORANGE', 0.008
    place(ctx, s, [(-0.5, -0.1, 1.35), (-0.3, 0.0, 1.5), (-0.1, -0.05, 1.42)], drag={1: (0.07, 0.0, 0.04)})
    s.draw_tool = 'CURVE'
    fake.aim_loc[1] = Vector((0.12, -0.12, 1.55))
    log("curves:", [(o.name, len(o.data.splines[0].bezier_points), o.data.splines[0].use_cyclic_u)
                    for o in bpy.data.objects if curves.is_ours(o)], "open:", s.draw_tool,
        curves.active(ctx).name if curves.active(ctx) else None)
    _win, _area, _region, space = view()
    space.shading.type = 'SOLID'
    space.overlay.show_floor = True
    rv3d = space.region_3d
    rv3d.view_location = (-0.05, 0.15, 1.25)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.2)
    rv3d.view_distance = 1.6
    state["overlay"] = True


def shoot_build():
    from vr_sculpt import curves
    try:
        shoot(BUILD_PNG)
        ctx = bpy.context
        s = ctx.scene.vr_sculpt
        curves.end(ctx)
        s.draw_tool = 'EDIT'
        # The main tip on the dragged point's outgoing handle; the off hand on another point (red: delete).
        blue = next(o for o in bpy.data.objects if curves.is_ours(o) and o.data.splines[0].use_cyclic_u)
        sp = blue.data.splines[0]
        fake.aim_loc[1] = blue.matrix_world @ sp.bezier_points[2].handle_right
        fake.aim_loc[0] = blue.matrix_world @ sp.bezier_points[4].co
        log("hit main:", curves.nearest_point(ctx, fake.aim_loc[1], curves.POINT_REACH)[2:],
            "hit off:", curves.nearest_point(ctx, fake.aim_loc[0], curves.POINT_REACH, handles=False)[2:])
        bpy.app.timers.register(lambda: (shoot_edit(), None)[1], first_interval=0.6)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_edit():
    try:
        shoot(EDIT_PNG)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
