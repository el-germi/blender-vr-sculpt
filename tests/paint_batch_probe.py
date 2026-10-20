"""Probe: is one paint.image_paint call with many stroke elements (one shared view) much cheaper
than one call per dab? Times both on the test ball and a denser mesh.

blender --factory-startup test_scene.blend --python tests/paint_batch_probe.py
"""

import math
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "paint_batch_probe_result.txt")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def run():
    import vr_sculpt
    from vr_sculpt import menus, native, paint
    open(LOG, "w").close()
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        for name in ("SculptBall", "HeavyBall_164k"):
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            with ctx.temp_override(window=win, area=area, region=region):
                for o in bpy.context.view_layer.objects:
                    if o is not None:
                        o.select_set(False)
                obj.hide_set(False)
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                paint.setup(bpy.context, obj, 2048)
                menus.set_mode(bpy.context, 'PAINT')
                center = obj.matrix_world.translation
                r = max(obj.dimensions) / 2
                pts = []
                for a in np.linspace(-0.4, 0.4, 24):
                    n = Vector((math.sin(a), -math.cos(a), 0.1)).normalized()
                    pts.append((center + n * r, n))
                rv3d = area.spaces.active.region_3d
                saved = (rv3d.view_location.copy(), rv3d.view_rotation.copy(), rv3d.view_distance)

                def look(eye, at):
                    d = at - eye
                    rv3d.view_perspective = 'PERSP'
                    rv3d.view_rotation = d.to_track_quat('-Z', 'Y')
                    rv3d.view_distance = d.length
                    rv3d.view_location = at
                    rv3d.update()

                def elem(p, mouse, start):
                    return {"name": "", "location": tuple(p), "mouse": mouse, "mouse_event": mouse, "pressure": 1.0,
                            "size": 20.0, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0, "is_start": start}

                from bpy_extras.view3d_utils import location_3d_to_region_2d
                # One call per dab.
                t0 = time.perf_counter()
                for p, n in pts:
                    look(p + n * 0.6, p)
                    m = (region.width / 2, region.height / 2)
                    bpy.ops.paint.image_paint(stroke=[elem(p, m, True)], mode='NORMAL')
                per_dab = (time.perf_counter() - t0) * 1000
                # One call for all 24 dabs from one view.
                mid_p = sum((p for p, _n in pts), Vector()) / len(pts)
                mid_n = sum((n for _p, n in pts), Vector()).normalized()
                t0 = time.perf_counter()
                look(mid_p + mid_n * 0.9, mid_p)
                elems = []
                for i, (p, n) in enumerate(pts):
                    m = location_3d_to_region_2d(region, rv3d, p)
                    elems.append(elem(p, (m.x, m.y), i == 0))
                bpy.ops.paint.image_paint(stroke=elems, mode='NORMAL')
                batched = (time.perf_counter() - t0) * 1000
                # Where the time goes in a single call: view setup vs the operator.
                t0 = time.perf_counter()
                for _ in range(24):
                    look(pts[0][0] + pts[0][1] * 0.6, pts[0][0])
                looks = (time.perf_counter() - t0) * 1000 / 24
                rv3d.view_location, rv3d.view_rotation, rv3d.view_distance = saved
                rv3d.update()
                log(f"{name} ({len(obj.data.vertices)} verts): 24 dabs one call each {per_dab:.1f} ms "
                    f"({per_dab / 24:.2f} ms/dab), all in one call {batched:.1f} ms; view setup {looks:.2f} ms")
                menus.set_object_mode(bpy.context, obj, 'OBJECT')
    except Exception:
        log(traceback.format_exc())
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
