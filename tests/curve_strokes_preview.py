"""Windowed check of curve-based strokes (tests/curve_strokes.png): a tapered tube, a ribbon rolling
along its length, a Raw curve (no mesh) and a mirrored tube, as the desktop's solid view (and the
headset, which shows the same evaluated geometry) draws them. tests/curve_strokes_vr.png adds the
wide line the headset draws over a Raw stroke (strokes.draw_raw).

blender --factory-startup test_scene.blend --python tests/curve_strokes_preview.py
"""

import math
import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PNG = os.path.join(ROOT, "tests", "curve_strokes.png")
VR_PNG = os.path.join(ROOT, "tests", "curve_strokes_vr.png")
LOG = os.path.join(ROOT, "tests", "curve_strokes_preview_result.txt")
lines = []
HANDLES = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


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


def view():
    from vr_sculpt import native
    win = bpy.context.window_manager.windows[0]
    area, region = native.largest_view3d(win)
    return win, area, region, area.spaces.active


def make(ctx, style, color, pts, widths, sides, sym=0, taper=0.0):
    from vr_sculpt import strokes
    origin = pts[0]
    obj = strokes.new_object(ctx, style, color, origin)
    samples = {"style": style, "sym": sym, "c": np.array(ctx.scene.cursor.location[:]) - origin,
               "p": pts - origin, "w": widths, "s": sides, "taper": taper}
    strokes.rebuild(obj, samples)
    strokes.save(obj, samples)
    return obj


def setup():
    import vr_sculpt
    try:
        vr_sculpt.register()
        ctx = bpy.context
        for o in list(bpy.data.objects):
            if o.type == 'MESH':
                o.hide_set(True)
        ctx.scene.cursor.location = (0.0, 0.0, 1.0)
        n = 80
        u = np.linspace(0.0, 1.0, n)
        up = np.tile([0.0, 0.0, 1.0], (n, 1))
        # A tapered tube: an S in the XZ plane.
        tube = np.stack([-0.45 + 0.25 * u, 0 * u, 1.0 + 0.12 * np.sin(u * 2 * math.pi)], 1)
        make(ctx, 'TUBE', 'RED', tube, np.full(n, 0.03), np.tile([0.0, 1.0, 0.0], (n, 1)), taper=0.3)
        # A ribbon rolling half a turn along its length.
        band = np.stack([-0.12 + 0.24 * u, 0 * u, 1.0 + 0.05 * np.sin(u * 4)], 1)
        roll = np.stack([0 * u, np.cos(u * math.pi), np.sin(u * math.pi)], 1)
        make(ctx, 'RIBBON', 'BLUE', band, np.full(n, 0.05), roll)
        # A Raw curve: a helix, no mesh.
        t = u * 4 * math.pi
        helix = np.stack([0.2 + 0.2 * u, 0.04 * np.cos(t), 1.0 + 0.04 * np.sin(t)], 1)
        raw = make(ctx, 'RAW', 'GREEN', helix, np.full(n, 0.01), up)
        # A tube mirrored across the cursor's X plane, below the others.
        arc = np.stack([0.08 + 0.3 * u, 0 * u, 0.75 + 0.06 * np.sin(u * math.pi)], 1)
        make(ctx, 'TUBE', 'ORANGE', arc, 0.01 + 0.015 * np.sin(u * math.pi), np.tile([0.0, 1.0, 0.0], (n, 1)),
             sym=1)
        depsgraph = ctx.evaluated_depsgraph_get()
        for o in bpy.data.objects:
            if o.type == 'CURVES':
                geometry = o.evaluated_get(depsgraph).evaluated_geometry()
                mesh = geometry.mesh
                log(o.name, "points", len(o.data.points), "mesh faces", len(mesh.polygons) if mesh else None,
                    "curves", geometry.curves is not None)
        log("raw shows curves only:", raw.evaluated_get(depsgraph).evaluated_geometry().mesh is None)

        win, area, region, space = view()
        space.shading.type = 'SOLID'
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.show_region_ui = space.show_region_toolbar = False
        space.overlay.show_text = False
        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, 0.92)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
        rv3d.view_distance = 1.3
        bpy.app.timers.register(lambda: (shoot(), None)[1], first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_to(path):
    win, area, region, _space = view()
    area.tag_redraw()
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    log("saved", path)


def draw_raw():
    from vr_sculpt import strokes
    try:
        strokes.draw_raw(bpy.context)
    except Exception:
        log(traceback.format_exc())


def shoot():
    try:
        shoot_to(PNG)
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw_raw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (shoot_vr(), None)[1], first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot_vr():
    try:
        shoot_to(VR_PNG)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
