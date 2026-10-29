"""Windowed check of look dev: the Light page, the Look sheet and its preview tiles, the Material page
(tests/lookdev_panels.png, tests/lookdev_panels_more.png), and lights with their gizmos around the sculpt ball
in Material Preview, with the hand panel (tests/lookdev_scene.png).

blender --factory-startup test_scene.blend --python tests/lookdev_preview.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PANELS_PNG = os.path.join(ROOT, "tests", "lookdev_panels.png")
MORE_PNG = os.path.join(ROOT, "tests", "lookdev_panels_more.png")
SCENE_PNG = os.path.join(ROOT, "tests", "lookdev_scene.png")
LOG = os.path.join(ROOT, "tests", "lookdev_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
HANDLES = []  # this script's own draw handlers
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
    """Viewer at the VR start pose looking along +Y, controllers in front (as tools_preview.py)."""
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.viewer_pose_location = Vector((0.0, -0.6, 1.45))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        down_forward = Quaternion((1, 0, 0), 0.9)
        self.aim_loc = [Vector((-0.2, -0.38, 1.18)), Vector((0.16, -0.3, 1.22))]
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


def setup():
    import vr_sculpt
    from vr_sculpt import lookdev, menus, primitives, vr_ui
    try:
        vr_sculpt.register()
        vr_ui.ANIMATE = False
        lookdev.PREVIEWS_PER_FRAME = 100  # Every tile in the one frame shot.
        ctx = bpy.context
        win, area, region, space = view()
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.show_region_ui = space.show_region_toolbar = False
        space.overlay.show_text = False
        s = ctx.scene.vr_sculpt
        sh = lookdev.shading(ctx)
        menus.set_mode(ctx, 'TOOLS')
        s.tool = 'LIGHT'
        s.light_type = 'SPOT'
        spot = lookdev.new_light(ctx, 'SPOT', Matrix.Translation((0.3, -0.3, 1.6)))
        spot.name = "Key Spot"
        lookdev.light_set(ctx, spot, 'color', lookdev.LIGHT_PALETTE[1][2])
        menus.set_more('TOOLS_LIGHT', True)
        heights = [add(ctx, -0.33, "light_cone", y=-0.17)]
        menus.set_more('TOOLS_LIGHT', False)
        menus.open_look()
        sh.type, sh.light = 'SOLID', 'MATCAP'
        heights.append(add(ctx, -0.11, "look_matcap", y=-0.17))
        menus.open_look_tiles(ctx, 'MATCAP')
        heights.append(add(ctx, 0.11, None, y=-0.17))
        sh.type = 'MATERIAL'
        menus.open_look_tiles(ctx, 'WORLD')
        heights.append(add(ctx, 0.33, None, y=-0.17))
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
        more_panels()
        bpy.app.timers.register(lambda: (shoot_more(), None)[1], first_interval=0.6)
    except Exception:
        log(traceback.format_exc())
        done()


def more_panels():
    """The Look sheet in Material Preview and Rendered (with the world it built), and the Material page."""
    from vr_sculpt import lookdev, menus, primitives
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    sh = lookdev.shading(ctx)
    menus.open_look()
    sh.type = 'MATERIAL'
    lookdev.set_studio(ctx, 'WORLD', "sunset.exr")
    sh.studiolight_rotate_z = math.radians(40.0)
    menus.set_more('LOOK_MATERIAL', True)
    heights = [add(ctx, -0.33, "look_use_world", y=-0.17)]
    menus.set_more('LOOK_MATERIAL', False)
    lookdev.use_as_world(ctx)
    sh.type = 'RENDERED'
    heights.append(add(ctx, -0.11, "look_strength", y=-0.17))
    menus.reset()
    menus.set_mode(ctx, 'MODEL')
    ball = bpy.data.objects.get("SculptBall")
    primitives.make_active(ctx, ball)
    s.model_page = 'MATERIAL'
    menus.open_sheet('OBJECT')
    lookdev.apply_preset(ctx, ball, 'GOLD')
    menus.set_more('OBJECT_MATERIAL', True)
    heights.append(add(ctx, 0.11, "mat_preset", y=-0.17))
    menus.set_more('OBJECT_MATERIAL', False)
    box = primitives.create(ctx, 'CUBE', (5, 5, 5))
    primitives.make_active(ctx, box)
    menus.open_sheet('OBJECT')
    heights.append(add(ctx, 0.33, "mat_color_RED", y=-0.17))
    bpy.data.objects.remove(box)
    primitives.make_active(ctx, ball)
    log("more panel heights:", [round(h, 3) for h in heights])


def shoot_more():
    try:
        shoot(MORE_PNG)
        panels.clear()
        build_scene()
        bpy.app.timers.register(lambda: (shoot_scene(), None)[1], first_interval=4.0)
    except Exception:
        log(traceback.format_exc())
        done()


def build_scene():
    """The sculpt ball in gold, lit by a spot, a point, an area light and the scene's sun, in Material
    Preview; the Light tool's gizmos and the hand panel drawn as in the headset."""
    from vr_sculpt import lookdev, menus, primitives
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    menus.set_mode(ctx, 'TOOLS')
    s.tool = 'LIGHT'
    s.show_panel, s.panel_pin = True, True
    point = lookdev.new_light(ctx, 'POINT', Matrix.Translation((-0.35, -0.15, 1.45)))
    point.name = "Fill"
    lookdev.light_set(ctx, point, 'color', lookdev.LIGHT_PALETTE[3][2])
    lookdev.light_set(ctx, point, 'power', 15.0)
    rim = Vector((0.05, 0.4, 1.6))
    area = lookdev.new_light(ctx, 'AREA', Matrix.LocRotScale(rim, (Vector((0, 0, 1.25)) - rim).to_track_quat('-Z', 'Z'),
                                                             None))
    area.name = "Rim"
    spot = bpy.data.objects["Key Spot"]
    spot.matrix_world = Matrix.LocRotScale((0.28, -0.3, 1.55), (Vector((0, 0, 1.25)) - Vector((0.28, -0.3, 1.55)))
                                           .to_track_quat('-Z', 'Z'), None)
    primitives.make_active(ctx, spot)
    sh = lookdev.shading(ctx)
    sh.type = 'MATERIAL'
    _win, _area, _region, space = view()
    space.shading.type = 'MATERIAL'
    space.shading.use_scene_lights = True
    space.shading.use_scene_world = False
    space.shading.studio_light = "sunset.exr"
    space.overlay.show_floor = True
    space.overlay.show_overlays = True
    space.overlay.show_extras = False  # Blender's own light drawing: the headset doesn't show it either.
    rv3d = space.region_3d
    rv3d.view_location = (0.0, -0.05, 1.3)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
    rv3d.view_distance = 1.15
    state["overlay"] = True
    log("lights:", [(o.name, o.data.type) for o in lookdev.lights(ctx)])


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
