"""Focused probe: native brush size in pixels vs world radius, and repeated calls.

blender --factory-startup --python tests/native_probe2.py
"""

import os
import traceback

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Quaternion, Vector

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_probe2_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3).astype(np.float64)


def main():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    rv3d = area.spaces.active.region_3d
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
    obj = bpy.context.active_object
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='SCULPT')
    rv3d.view_perspective = 'PERSP'
    rv3d.view_location = (0, 0, 0)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
    rv3d.view_distance = 6.0
    bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)
    center = (region.width / 2, region.height / 2)
    target = Vector((0.7071, -0.7071, 0.0))
    right = rv3d.view_rotation @ Vector((1, 0, 0))
    p0 = view3d_utils.location_3d_to_region_2d(region, rv3d, target)

    def stroke(loc, size, mouse=center):
        b = positions(obj)
        e = {"name": "", "location": tuple(loc), "mouse": tuple(mouse), "mouse_event": tuple(mouse),
             "pressure": 1.0, "size": size, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0, "is_start": True}
        with bpy.context.temp_override(window=win, area=area, region=region):
            r = bpy.ops.sculpt.brush_stroke(stroke=[e], mode='NORMAL')
        a = positions(obj)
        d = np.linalg.norm(a - b, axis=1)
        hit = d > 1e-6
        ext = np.linalg.norm(b[hit] - np.array(loc), axis=1).max() if hit.any() else 0.0
        return r, int(hit.sum()), ext, d.max()

    for i in range(3):
        log("repeat", i, stroke(target, 60.0))
    k = 0
    for radius in (0.1, 0.2, 0.3):
        p1 = view3d_utils.location_3d_to_region_2d(region, rv3d, target + right * radius)
        px = (p1 - p0).length
        for size in (px, px * 2):
            # Each dab at a fresh spot around the sphere's equator.
            k += 1
            a = -0.785 + 0.35 * k
            loc = Vector((np.sin(a), -np.cos(a), 0.0))
            log(f"radius {radius} size {size:.1f}px ->", stroke(loc, size))
    # Does 'mouse' over empty space cancel the dab?
    log("mouse in a corner (misses mesh) ->", stroke(target, 60.0, mouse=(5.0, 5.0)))
    # Fast repeated calls at a moving location.
    for i in range(5):
        log("moving", i, stroke(target + Vector((0, 0, 0.05 * i)), 60.0))


try:
    bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
except Exception:
    log(traceback.format_exc())


def _finish():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (_finish(), None)[1], first_interval=6.0)
