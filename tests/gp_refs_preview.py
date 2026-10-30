"""Windowed check of this round's panels and drawing: Draw with the Pencil style and smoothing, the Refs page
and its tiles, the Settings sheet's smoothing row, Pose's take smoothing (tests/gp_refs_panels.png), then
reference pictures around the sculpt ball, one pinned beside the hand panel, and Pencil strokes, with the
desktop's own empty drawing off as in the headset (tests/gp_refs_scene.png).

blender --factory-startup test_scene.blend --python tests/gp_refs_preview.py
"""

import math
import os
import shutil
import sys
import tempfile
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PANELS_PNG = os.path.join(ROOT, "tests", "gp_refs_panels.png")
SCENE_PNG = os.path.join(ROOT, "tests", "gp_refs_scene.png")
LOG = os.path.join(ROOT, "tests", "gp_refs_preview_result.txt")
FOLDER = tempfile.mkdtemp(prefix="vrs_refs_preview_")
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


def picture(name, w, h, hue):
    """A test picture: a soft colored gradient with a grid and a disc, saved as PNG in FOLDER."""
    img = bpy.data.images.new(name, w, h, alpha=True)
    y, x = np.mgrid[0:h, 0:w] / np.array([h, w])[:, None, None]
    import colorsys
    base = np.array(colorsys.hsv_to_rgb(hue, 0.55, 0.95))
    top = np.array(colorsys.hsv_to_rgb((hue + 0.15) % 1.0, 0.35, 0.6))
    rgb = base[None, None, :] * (1 - y[..., None]) + top[None, None, :] * y[..., None]
    grid = ((x * 8) % 1 < 0.03) | ((y * 5) % 1 < 0.04)
    rgb[grid] *= 0.55
    disc = (x - 0.68) ** 2 * (w / h) ** 2 + (y - 0.62) ** 2 < 0.04
    rgb[disc] = (1.0, 0.95, 0.8)
    rgba = np.concatenate([rgb, np.ones((h, w, 1))], axis=2).astype(np.float32)
    img.pixels.foreach_set(rgba.ravel())
    path = os.path.join(FOLDER, name + ".png")
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)
    return path


def setup():
    import vr_sculpt
    from vr_sculpt import lookdev, menus, refs, vr_ui
    try:
        vr_sculpt.register()
        vr_ui.ANIMATE = False
        lookdev.PREVIEWS_PER_FRAME = 100
        ctx = bpy.context
        win, area, region, space = view()
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.show_region_ui = space.show_region_toolbar = False
        space.overlay.show_text = False
        s = ctx.scene.vr_sculpt
        paths = [picture(n, w, h, hue) for n, w, h, hue in
                 (("Front", 320, 200, 0.58), ("Side", 200, 320, 0.08), ("Mood", 256, 256, 0.85),
                  ("Detail", 300, 180, 0.33), ("Turnaround", 400, 160, 0.0))]
        s.refs_folder = FOLDER

        menus.set_mode(ctx, 'DRAW')
        s.draw_style = 'PENCIL'
        menus.set_more('DRAW', True)
        heights = [add(ctx, -0.44, "style_PENCIL", y=-0.17)]
        menus.set_more('DRAW', False)

        menus.set_mode(ctx, 'TOOLS')
        s.tool = 'REFS'
        s.ref_image = paths[0]
        obj = refs.new_ref(ctx, paths[1], Matrix.Translation((0.5, 0.2, 1.4)), 0.4)
        refs.set_opacity(obj, 0.7)
        heights.append(add(ctx, -0.22, "ref_pin", y=-0.17))
        menus.open_ref_tiles(ctx)
        heights.append(add(ctx, 0.0, None, y=-0.17))
        menus.close_sheet(ctx)
        menus.open_sheet('SETTINGS')
        heights.append(add(ctx, 0.22, "smooth_amount", y=-0.17))
        menus.close_sheet(ctx)
        menus.set_mode(ctx, 'POSE')
        menus.set_more('POSE', True)
        heights.append(add(ctx, 0.44, "take_smooth_frames", y=-0.17))
        menus.set_more('POSE', False)
        bpy.data.objects.remove(obj)
        log("panel heights:", [round(h, 3) for h in heights])

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, -0.04)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.8
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (shoot_panels(paths), None)[1], first_interval=0.8)
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


def shoot_panels(paths):
    try:
        shoot(PANELS_PNG)
        panels.clear()
        build_scene(paths)
        bpy.app.timers.register(lambda: (shoot_scene(), None)[1], first_interval=1.5)
    except Exception:
        log(traceback.format_exc())
        done()


def pencil_spiral(ctx, s):
    """Pencil strokes drawn as the trigger would: a spiral around the ball and a mirrored swoosh."""
    from vr_sculpt import pencil
    s.draw_thickness, s.draw_color = 'TAPER', 'ORANGE'
    s.draw_width, s.draw_taper = 0.012, 0.3
    tips = [Vector((0.22 * math.cos(t), 0.22 * math.sin(t), 1.1 + 0.05 * t)) for t in np.linspace(0, 4 * math.pi, 160)]
    h = pencil.PencilStroke(ctx, s, Matrix.Translation(tips[0]), 1.0, 1.0)
    for p in tips[1:]:
        h.update(ctx, None, Matrix.Translation(p), 1.0)
    h.finish(ctx)
    s.draw_thickness, s.draw_color, s.mirror_x = 'PRESSURE', 'BLUE', True
    ctx.scene.cursor.location = (0.0, 0.0, 0.0)
    pts = [Vector((0.12 + 0.2 * t, -0.25, 1.55 + 0.08 * math.sin(t * 6))) for t in np.linspace(0, 1, 60)]
    h = pencil.PencilStroke(ctx, s, Matrix.Translation(pts[0]), 0.4, 1.0)
    for i, p in enumerate(pts[1:]):
        h.update(ctx, None, Matrix.Translation(p), 0.4 + 0.6 * i / len(pts))
    h.finish(ctx)
    s.mirror_x = False


def build_scene(paths):
    from vr_sculpt import menus, refs
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.reset()
    menus.set_mode(ctx, 'TOOLS')
    s.tool = 'REFS'
    s.show_panel, s.panel_pin = True, True
    viewer = fake.viewer_pose_location
    front = refs.new_ref(ctx, paths[0], refs.facing((-0.45, 0.35, 1.45), viewer), 0.45)
    side = refs.new_ref(ctx, paths[1], refs.facing((0.5, 0.3, 1.35), viewer), 0.4)
    refs.set_opacity(side, 0.75)
    refs.new_ref(ctx, paths[4], refs.facing((0.0, 0.7, 1.75), viewer), 0.7)
    mood = refs.new_ref(ctx, paths[2], refs.facing((0.0, 2.0, 0.0), viewer), 0.3)
    refs.toggle_pin(ctx, mood)
    pencil_spiral(ctx, s)
    from vr_sculpt import primitives
    primitives.make_active(ctx, front)
    _win, _area, _region, space = view()
    space.shading.type = 'SOLID'
    space.overlay.show_floor = True
    space.overlay.show_overlays = True
    space.overlay.show_extras = False  # Blender's own empty drawing: the headset doesn't show it either.
    rv3d = space.region_3d
    rv3d.view_location = (0.0, 0.1, 1.35)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
    rv3d.view_distance = 1.5
    fake.aim_loc[1] = Vector((-0.3, 0.3, 1.4))  # The main tip touching the front picture (yellow outline).
    state["overlay"] = True
    log("refs:", [o.name for o in refs.refs(ctx)], "pinned:", s.ref_pin)
    log("pencil:", [(o.name, sum(len(f.drawing.strokes) for l in o.data.layers for f in l.frames))
                    for o in bpy.data.objects if o.type == 'GREASEPENCIL'])


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
