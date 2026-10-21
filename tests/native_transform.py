"""Windowed test: native strokes on a moved, rotated and scaled object.

blender --factory-startup --python tests/native_transform.py
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "native_transform_result.txt")
lines = []


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand, so Blender won't disable it on exit: unregister it
    first, or its GPU offscreens are freed after Blender's own teardown and the exit crashes."""
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        import traceback
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def world_positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    co = a.reshape(n, 3).astype(np.float64)
    m = np.array(obj.matrix_world)
    return co @ m[:3, :3].T + m[:3, 3]


def main():
    import vr_sculpt
    from vr_sculpt import native
    vr_sculpt.register()
    try:
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0, location=(0.5, 0.3, 1.25))
        obj = ctx.active_object
        obj.scale = (0.25, 0.25, 0.25)
        obj.rotation_euler = (0.3, 0.0, 0.7)
        bpy.context.view_layer.update()
        center = Vector(obj.matrix_world.translation)
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = center
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 2.0
        bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)

        with ctx.temp_override(window=win, area=area, region=region):
            for radius in (0.03, 0.08):
                s = native.NativeStroke(bpy.context, obj, radius)
                start = world_positions(obj)
                # Sweep across the front of the 25 cm ball, 5 mm off the surface (world space).
                for i in range(25):
                    a = -0.5 + i * 0.04
                    d = Vector((np.sin(a), -np.cos(a), 0.0))
                    s.update(center + d * 0.255, None, 1.0)
                s.finish()
                end = world_positions(obj)
                moved = np.linalg.norm(end - start, axis=1)
                hit = moved > 1e-6
                if hit.any():
                    # How far moved vertices are from the stroke plane z = center.z (world).
                    width = np.abs(start[hit, 2] - center.z).max()
                    log(f"radius {radius}: {s.dabs} dabs, {s.dropped} dropped, {int(hit.sum())} verts moved, "
                        f"half-width {width:.3f} (want ~{radius}), max move {moved.max():.4f}")
                else:
                    log(f"radius {radius}: {s.dabs} dabs, {s.dropped} dropped, NOTHING MOVED")
    except Exception:
        log(traceback.format_exc())
    finally:
        vr_sculpt.unregister()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
