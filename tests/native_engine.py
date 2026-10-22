"""Windowed test of vr_sculpt.native without a headset.

blender --factory-startup --python tests/native_engine.py
"""

import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "native_engine_result.txt")
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


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3).astype(np.float64)


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
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=7, radius=1.0)
        obj = ctx.active_object
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0, 0, 0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 6.0
        bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)

        with ctx.temp_override(window=win, area=area, region=region):
            c = bpy.context
            before = positions(obj)
            ups = c.tool_settings.sculpt.unified_paint_settings; size_before = (ups.use_locked_size, ups.unprojected_size)
            for radius, z0 in ((0.08, -0.4), (0.25, 0.3)):
                s = native.NativeStroke(c, obj, radius)
                start = positions(obj)
                times = []
                # Sweep the tip along the front/side of the sphere, slightly above the surface.
                for i in range(40):
                    a = -0.6 + i * 0.03
                    tip = Vector((np.sin(a), -np.cos(a), z0)).normalized() * 1.02
                    t0 = time.perf_counter()
                    s.update(tip, None, 1.0)
                    times.append((time.perf_counter() - t0) * 1000)
                    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
                s.finish()
                end = positions(obj)
                d = np.linalg.norm(end - start, axis=1)
                hit = d > 1e-6
                # Width of the groove: distance of moved verts from the stroke's plane z=z0.
                width = np.abs(start[hit, 2] - z0 / np.sqrt(1 + z0 * z0)).max() if hit.any() else 0
                log(f"radius {radius}: {s.dabs} dabs, {s.dropped} dropped, {hit.sum()} verts moved, "
                    f"groove half-width {width:.3f}, max {d.max():.4f}, avg {np.mean(times):.2f} ms/update")
            log("brush size restored:", (ups.use_locked_size, ups.unprojected_size) == size_before)

            # Back side, hidden from the desktop view.
            s = native.NativeStroke(c, obj, 0.15)
            start = positions(obj)
            for i in range(10):
                s.update(Vector((0.05 * i - 0.25, 1.02, 0.0)), None, 1.0)
            s.finish()
            d = np.linalg.norm(positions(obj) - start, axis=1)
            log(f"back side: {s.dabs} dabs, {s.dropped} dropped, moved {int((d > 1e-6).sum())} verts")

            # Strength from the hand panel's slider: the same stroke at 1.0 and 0.2.
            from vr_sculpt import menus, vr_ui
            c.scene.vr_sculpt.engine = 'NATIVE'
            c.scene.vr_sculpt.mode = 'SCULPT'  # The saved scene may be in another VR mode.
            depth = {}
            for strength, z0 in ((1.0, -0.75), (0.2, 0.75)):
                widgets, _h = vr_ui.layout(menus.tag_rows(c))
                slider = vr_ui.find(widgets, "strength")
                slider.on_change(strength)
                s = native.NativeStroke(c, obj, 0.15)
                start = positions(obj)
                for i in range(20):
                    a = -0.3 + i * 0.03
                    s.update(Vector((np.sin(a), -np.cos(a), z0)).normalized() * 1.02, None, 1.0)
                s.finish()
                depth[strength] = float(np.linalg.norm(positions(obj) - start, axis=1).max())
            log(f"strength slider: shown {native.get_strength(c):.2f}, max move at 1.0 {depth[1.0]:.4f}, "
                f"at 0.2 {depth[0.2]:.4f}, ratio {depth[0.2] / max(depth[1.0], 1e-9):.2f}")
            log("strength honored:", depth[0.2] < depth[1.0] * 0.6)

            # Brush cycling via essentials assets.
            names = []
            for _ in range(len(native.NATIVE_BRUSHES) + 1):
                try:
                    names.append(native.cycle_brush(bpy.context))
                except Exception as ex:
                    names.append(f"ERR {ex}")
                    break
            log("cycle:", names, "-> active", native.sculpt_brush(bpy.context).name)

            # Does a non-Draw brush work too?
            s = native.NativeStroke(bpy.context, obj, 0.15)
            start = positions(obj)
            for i in range(10):
                s.update(Vector((0.05 * i - 0.25, -1.0, -0.6)).normalized() * 1.02, None, 1.0)
            s.finish()
            d = np.linalg.norm(positions(obj) - start, axis=1)
            log(f"with {native.sculpt_brush(bpy.context).name}: {s.dabs} dabs, moved {int((d > 1e-6).sum())} verts")
    except Exception:
        log(traceback.format_exc())
    finally:
        vr_sculpt.unregister()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
