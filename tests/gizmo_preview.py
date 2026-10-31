"""Windowed check of the transform gizmo: the panel rows (Model, Light, Refs with a selection;
tests/gizmo_panels.png), a selected box with its gizmo and the main tip on its X arrow (tests/gizmo_scene.png),
and a turn handle held mid-turn with its readout (tests/gizmo_drag.png), as the headset draws them.

blender --factory-startup test_scene.blend --python tests/gizmo_preview.py
"""

import math
import os
import shutil
import sys
import tempfile
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PANELS_PNG = os.path.join(ROOT, "tests", "gizmo_panels.png")
SCENE_PNG = os.path.join(ROOT, "tests", "gizmo_scene.png")
DRAG_PNG = os.path.join(ROOT, "tests", "gizmo_drag.png")
LOG = os.path.join(ROOT, "tests", "gizmo_preview_result.txt")
FOLDER = tempfile.mkdtemp(prefix="vrs_gizmo_preview_")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []
state = {"overlay": False, "box": None}


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
    shutil.rmtree(FOLDER, ignore_errors=True)
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
        self.viewer_pose_location = Vector((0.15, -0.75, 1.55))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        down_forward = Quaternion((1, 0, 0), 0.9)
        self.aim_loc = [Vector((-0.35, -0.45, 1.05)), Vector((0.3, -0.4, 1.1))]
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


def add(ctx, x, hover=None, y=-0.17):
    from vr_sculpt import menus, vr_ui
    widgets, height = menus.layout(ctx)
    m = vr_ui.panel_matrix(Vector((x, 0.0, y)), Vector((x, -0.6, y + 0.15)), 1.0)
    panels.append((m, widgets, height, hover))
    return height


def box(ctx):
    import bmesh
    mesh = bpy.data.meshes.new("Panel Box")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("Panel Box", mesh)
    ctx.scene.collection.objects.link(obj)
    obj.matrix_world = Matrix.LocRotScale(Vector((0.05, 0.05, 1.05)), Quaternion((0, 0, 1), 0.35), (0.24, 0.14, 0.08))
    return obj


def setup():
    import vr_sculpt
    from vr_sculpt import lookdev, menus, primitives, refs, vr_ui
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
        s.grab_deadzone = 0.0

        obj = box(ctx)
        state["box"] = obj.name
        menus.set_mode(ctx, 'MODEL')
        primitives.make_active(ctx, obj)
        heights = [add(ctx, -0.33, "gizmo_rotate")]
        menus.set_mode(ctx, 'TOOLS')
        s.tool = 'LIGHT'
        light = lookdev.new_light(ctx, 'SPOT', Matrix.Translation((0.6, 0.4, 1.6)))
        heights.append(add(ctx, -0.11, "gizmo_local"))
        s.tool = 'REFS'
        img = bpy.data.images.new("Front", 64, 40)
        img.filepath_raw = os.path.join(FOLDER, "Front.png")
        img.file_format = 'PNG'
        img.save()
        s.ref_image = img.filepath_raw
        ref = refs.new_ref(ctx, img.filepath_raw, Matrix.Translation((0.5, 0.2, 1.4)), 0.4)
        heights.append(add(ctx, 0.11, "gizmo_scale"))
        bpy.data.objects.remove(ref)
        bpy.data.objects.remove(light)
        log("panel heights:", [round(h, 3) for h in heights])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (-0.11, 0.0, -0.04)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.75
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


def aim_tip_at(i, point):
    """Put controller i's aim so its tip is at point (the tip is tip_offset down its -Z)."""
    from vr_sculpt import operators
    s = bpy.context.scene.vr_sculpt
    pose = operators.controller_pose(fake.aim_loc[i], fake.aim_rot[i])
    tip = operators.brush_tip(pose, s.tip_offset)
    fake.aim_loc[i] = fake.aim_loc[i] + (Vector(point) - tip)


def shoot_panels():
    from vr_sculpt import gizmo, menus, primitives
    try:
        shoot(PANELS_PNG)
        panels.clear()
        ctx = bpy.context
        menus.reset()
        menus.set_mode(ctx, 'MODEL')
        obj = bpy.data.objects[state["box"]]
        primitives.make_active(ctx, obj)
        s = ctx.scene.vr_sculpt
        s.show_panel = False
        _win, _area, _region, space = view()
        space.shading.type = 'SOLID'
        space.overlay.show_floor = True
        rv3d = space.region_3d
        rv3d.view_location = (0.05, 0.05, 1.08)
        rv3d.view_rotation = Quaternion((0, 0, 1), 0.2) @ Quaternion((1, 0, 0), 1.15)
        rv3d.view_distance = 0.55
        f = gizmo.frame_for(ctx, obj, 1.0)
        start, tip = f.arrow(0)
        aim_tip_at(1, start.lerp(tip, 0.4))
        aim_tip_at(0, f.center + Vector((-0.25, -0.2, -0.1)))
        state["overlay"] = True
        log("hover:", gizmo.hint(ctx, start.lerp(tip, 0.4), 1.0))
        bpy.app.timers.register(lambda: (shoot_scene(), None)[1], first_interval=0.6)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_scene():
    from vr_sculpt import actions, gizmo
    try:
        shoot(SCENE_PNG)
        ctx = bpy.context
        obj = bpy.data.objects[state["box"]]
        f = gizmo.frame_for(ctx, obj, 1.0)
        r = gizmo.RING * f.unit
        c = f.center
        h = gizmo.pick(ctx, actions.HANDS[1], Matrix.Translation(c + Vector((r, 0.0, 0.0))), 1.0)
        for i in range(1, 9):
            a = math.radians(40) * i / 8
            p = c + Vector((math.cos(a), math.sin(a), 0.0)) * r
            h.update(ctx, None, Matrix.Translation(p), 1.0)
        aim_tip_at(1, h.tip)
        log("held:", h.kind, h.axis, h.readout())
        state["held"] = h
        bpy.app.timers.register(lambda: (shoot_drag(), None)[1], first_interval=0.6)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_drag():
    try:
        shoot(DRAG_PNG)
        state["held"].finish(bpy.context)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
