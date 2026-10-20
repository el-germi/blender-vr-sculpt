"""Windowed check of the full VR overlay (rings + card) with simulated controllers.

blender --factory-startup test_scene.blend --python tests/overlay_preview.py
Saves tests/overlay_native.png and tests/overlay_python.png.
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "overlay_preview_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


class FakeSessionState:
    """Two controllers held in front of a viewer at the scene camera, aiming forward and down."""
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -0.6, 1.45))
    _aim = Quaternion((1, 0, 0), 0.9)  # tilt local -Z from straight down toward +Y

    def controller_aim_location_get(self, context, i):
        return Vector((-0.18 if i == 0 else 0.18, -0.35, 1.2))

    def controller_aim_rotation_get(self, context, i):
        return self._aim

    def controller_grip_location_get(self, context, i):
        return Vector((-0.18 if i == 0 else 0.18, -0.38, 1.17))


fake = FakeSessionState()
errors = []


def draw():
    from vr_sculpt import draw as vr_draw
    try:
        vr_draw.draw_overlay(bpy.context, fake)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def setup():
    import vr_sculpt
    from vr_sculpt import native
    try:
        vr_sculpt.register()
        win = bpy.context.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, -0.2, 1.25)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
        rv3d.view_distance = 0.45
        bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')
        settings = bpy.context.scene.vr_sculpt
        settings.engine = 'PYTHON'
        settings.brush = 'SMOOTH'
        bpy.app.timers.register(lambda: (shoot(win, area, region, "python"), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(win, area, region, name):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        path = os.path.join(ROOT, "tests", f"overlay_{name}.png")
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=path)
        log("saved", path, "draw errors:", len(errors))
        if name == "python":
            # Native: SculptBall in Sculpt Mode with Inflate/Deflate.
            obj = bpy.data.objects["SculptBall"]
            bpy.context.view_layer.objects.active = obj
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.object.mode_set(mode='SCULPT')
                bpy.ops.brush.asset_activate(
                    asset_library_type='ESSENTIALS',
                    relative_asset_identifier="brushes/essentials_brushes-mesh_sculpt.blend/Brush/Inflate/Deflate")
            bpy.context.scene.vr_sculpt.engine = 'NATIVE'
            bpy.app.timers.register(lambda: (shoot(win, area, region, "native"), None)[1], first_interval=0.8)
            return
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
