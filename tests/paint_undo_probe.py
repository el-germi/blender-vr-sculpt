"""Repro for "stroke undo only covers 2-3 strokes, then Blender crashed": paint several strokes on a
2K image, undo some, paint again, undo again, then use Blender's own undo. Logs each step (flushed),
so the last line shows where a crash happened.

blender --factory-startup test_scene.blend --python tests/paint_undo_probe.py
"""

import math
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "paint_undo_probe_result.txt")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def run():
    from paint_preview import pose_at, stroke
    import vr_sculpt
    from vr_sculpt import menus, native, paint
    open(LOG, "w").close()
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        settings = ctx.scene.vr_sculpt
        with ctx.temp_override(window=win, area=area, region=region):
            ball = bpy.data.objects["SculptBall"]
            for o in bpy.context.view_layer.objects:
                if o is not None:
                    o.select_set(False)
            ball.select_set(True)
            bpy.context.view_layer.objects.active = ball
            paint.setup(bpy.context, ball, 2048)
            menus.set_mode(bpy.context, 'PAINT')
            img = paint.paint_image(ball)
            log("image", img.name, img.size[:], "float" if img.is_float else "byte")
            settings.paint_reach, settings.paint_radius, settings.tip_offset = 'CONTACT', 0.02, 0.04
            center, r = ball.matrix_world.translation, 0.25

            def one_stroke(i):
                poses = []
                for a in np.linspace(-0.6, 0.6, 25):
                    n = Vector((math.sin(a), -math.cos(a), 0.6 - 0.15 * i)).normalized()
                    poses.append(pose_at(center + n * (r + 0.003 + settings.tip_offset), -n))
                t0 = time.perf_counter()
                s = stroke(bpy.context, settings, ball, poses)
                log(f"stroke {i}: {s.dabs} dabs in {(time.perf_counter() - t0) * 1000:.0f} ms, "
                    f"history {len(paint._history)} ({paint.history_bytes() / 2**20:.1f} MB)")

            def pixels():
                px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
                img.pixels.foreach_get(px)
                return px

            start = pixels()
            for i in range(6):
                paint.set_color(bpy.context, ((i % 3) / 2, 1 - (i % 3) / 2, 0.3))
                one_stroke(i)
            for i in range(3):
                log("undo ->", paint.undo_last(bpy.context), "history", len(paint._history))
            for i in range(6, 9):
                one_stroke(i)
            for i in range(10):
                ok = paint.undo_last(bpy.context)
                log("undo ->", ok, "history", len(paint._history))
                if not ok:
                    break
            diff = np.abs(pixels() - start).max()
            log(f"after undoing everything, max difference from the start: {diff:.4f}",
                "OK" if diff <= 1 / 255 + 1e-6 else "MISMATCH")
            log("Blender undo x5")
            for i in range(5):
                bpy.ops.ed.undo()
                log("  ed.undo", i)
            one_stroke(9)
            log("survived")
    except Exception:
        log(traceback.format_exc())
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
