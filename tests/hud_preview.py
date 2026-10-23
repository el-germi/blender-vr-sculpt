"""Windowed visual check of the tip status. Saves tests/hud_preview.png.

blender --factory-startup --python tests/hud_preview.py
"""

import os
import sys
import traceback
from types import SimpleNamespace

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PNG = os.path.join(ROOT, "tests", "hud_preview.png")
LOG = os.path.join(ROOT, "tests", "hud_preview_result.txt")
lines = []
cards = []
VIEWER = Vector((0.0, -0.35, 0.0))


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def draw():
    from vr_sculpt import hud
    try:
        for anchor, content, desc in cards:
            hud.draw_status(anchor, VIEWER, 1.0, content, "4.0 cm", with_description=desc)
    except Exception:
        log(traceback.format_exc())


def setup():
    from vr_sculpt import hud, native
    try:
        win = bpy.context.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        rv3d = space.region_3d
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_ico_sphere_add(radius=0.01, location=(0, 0.3, -0.2))
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            names = []
            for name in ("Clay Strips", "Crease Sharp"):
                bpy.ops.brush.asset_activate(
                    asset_library_type='ESSENTIALS',
                    relative_asset_identifier="brushes/essentials_brushes-mesh_sculpt.blend/Brush/" + name)
                names.append(bpy.context.tool_settings.sculpt.brush)
        native_settings = SimpleNamespace(mode='SCULPT', engine='NATIVE', brush='DRAW')
        py_settings = SimpleNamespace(mode='SCULPT', engine='PYTHON', brush='GRAB')
        c1 = hud.card_content(native_settings, names[0])
        c2 = hud.card_content(native_settings, names[1])
        c3 = hud.card_content(py_settings, None)
        log("native:", c1[0], c2[0], "python:", c3[0], c3[2])
        cards[:] = [(Vector((0, 0, 0.055)), c1, True), (Vector((0, 0, 0.0)), c2, False), (Vector((0, 0, -0.06)), c3, True)]
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0, 0, 0.01)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.35
        bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')
        area.tag_redraw()
        bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(win, area, region):
    try:
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
