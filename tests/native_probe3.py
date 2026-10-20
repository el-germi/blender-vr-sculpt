"""Probe: which setting controls the native brush radius in 5.4?

blender --factory-startup --python tests/native_probe3.py
"""

import os
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_probe3_result.txt")
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
    try:
        win = bpy.context.window_manager.windows[0]
        area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
        region = next(r for r in area.regions if r.type == 'WINDOW')
        rv3d = area.spaces.active.region_3d
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=7, radius=1.0)
        obj = bpy.context.active_object
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0, 0, 0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 6.0
        bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)
        center = (region.width / 2, region.height / 2)
        ts = bpy.context.tool_settings
        ups = ts.sculpt.unified_paint_settings
        brush = ts.sculpt.brush
        log("ups.use_unified_size", ups.use_unified_size, "ups.size", ups.size, "brush.size", brush.size,
            "brush.use_locked_size", brush.use_locked_size, "ups.use_locked_size", ups.use_locked_size)
        k = [0]

        def dab():
            k[0] += 1
            a = -1.2 + 0.3 * k[0]
            loc = Vector((np.sin(a), -np.cos(a), 0.0))
            b = positions(obj)
            e = {"name": "", "location": tuple(loc), "mouse": center, "mouse_event": center, "pressure": 1.0,
                 "size": 10.0, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0, "is_start": True}
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.sculpt.brush_stroke(stroke=[e], mode='NORMAL')
            d = np.linalg.norm(positions(obj) - b, axis=1)
            hit = d > 1e-5
            return f"{int(hit.sum())} verts, reach {np.linalg.norm(b[hit] - np.array(loc), axis=1).max() if hit.any() else 0:.3f}"

        log("default:", dab())
        for owner_name, owner in (("ups", ups), ("brush", brush)):
            for size in (20, 120):
                old = owner.size
                owner.size = size
                log(f"{owner_name}.size={owner.size}:", dab())
                owner.size = old
        for owner_name, owner in (("ups", ups), ("brush", brush)):
            old_lock, old_u = owner.use_locked_size, owner.unprojected_size
            owner.use_locked_size = 'SCENE'
            for u in (0.1, 0.4):
                owner.unprojected_size = u
                log(f"{owner_name} SCENE unprojected_size={u}:", dab())
            owner.use_locked_size, owner.unprojected_size = old_lock, old_u
    except Exception:
        log(traceback.format_exc())
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
