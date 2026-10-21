"""Repro for "painting a thin but tall and wide cube only works in the corners": a 1 m x 4 cm x 1 m
box (our own Cube primitive, set up for painting), contact strokes across the middle of the big
face and near a corner, and a spray stroke. Logs how many texels each one changed.

blender --factory-startup test_scene.blend --python tests/paint_box_probe.py
"""

import math
import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "paint_box_probe_result.txt")


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
    print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def run():
    from paint_preview import pose_at, stroke
    import vr_sculpt
    from vr_sculpt import menus, native, paint, primitives
    open(LOG, "w").close()
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        settings = ctx.scene.vr_sculpt
        for name in ("SculptBall", "HeavyBall_164k"):
            if name in bpy.data.objects:
                bpy.data.objects[name].hide_set(True)
        with ctx.temp_override(window=win, area=area, region=region):
            menus.set_mode(bpy.context, 'MODEL')
            box = primitives.create(bpy.context, 'CUBE', (0.0, 0.3, 1.0))
            box.vr_prim.size = (1.0, 0.04, 1.0)
            bpy.context.view_layer.update()
            log("box dims", tuple(round(d, 3) for d in box.dimensions), "scale", tuple(box.scale),
                "faces", len(box.data.polygons))
            paint.setup(bpy.context, box, 2048)
            menus.set_mode(bpy.context, 'PAINT')
            img = paint.paint_image(box)
            uv = box.data.uv_layers.active.data
            for poly in box.data.polygons:
                us = [uv[i].uv for i in poly.loop_indices]
                log(f"face {poly.index} normal {tuple(round(c, 2) for c in poly.normal)} area {poly.area:.3f} "
                    f"uv box x {min(u.x for u in us):.3f}-{max(u.x for u in us):.3f} "
                    f"y {min(u.y for u in us):.3f}-{max(u.y for u in us):.3f}")
            settings.paint_radius, settings.tip_offset = 0.03, 0.04
            ip = bpy.context.tool_settings.image_paint
            log("projection options: occlude", ip.use_occlude, "backface", ip.use_backface_culling,
                "normal falloff", ip.use_normal_falloff, "angle", round(math.degrees(ip.normal_angle)))

            def pixels():
                px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
                img.pixels.foreach_get(px)
                return px.reshape(img.size[1], img.size[0], 4)

            front_y = 0.3 - 0.02  # the big face toward -Y

            colors = iter([(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (0, 1, 1), (1, 0, 1), (0.2, 0.2, 0.2),
                           (1, 0.5, 0)])

            def contact(label, pts):
                paint.set_color(bpy.context, next(colors))
                settings.paint_reach = 'CONTACT'
                before = pixels().copy()
                n = Vector((0.0, -1.0, 0.0))
                poses = [pose_at(Vector(p) + n * (0.003 + settings.tip_offset), -n) for p in pts]
                s = stroke(bpy.context, settings, box, poses)
                changed = int(np.any(np.abs(pixels()[..., :3] - before[..., :3]) > 0.05, axis=2).sum())
                log(f"{label}: {s.dabs if s else 0} dabs, {changed} texels changed")

            mid = [(x, front_y, 1.0) for x in np.linspace(-0.3, 0.3, 20)]
            contact("contact across the middle of the big face", mid)
            corner = [(x, front_y, 0.55) for x in np.linspace(-0.48, -0.3, 10)]
            contact("contact near the bottom-left corner", corner)
            settings.paint_radius = 0.1
            contact("contact, big radius, middle", mid)
            settings.paint_radius = 0.03

            for dist in (1.0, 3.0):
                paint.EYE_DISTANCE = dist
                contact(f"eye at {dist} m: contact across the middle", mid)
            paint.EYE_DISTANCE = 0.25
            box.vr_prim.subdivisions = 8
            bpy.context.view_layer.update()
            log("subdivided:", len(box.data.polygons), "faces")
            contact("eye at 0.25 m, subdivided box: contact across the middle", mid)
            box.vr_prim.subdivisions = 0
            settings.paint_reach = 'SPRAY'
            paint.set_color(bpy.context, next(colors))
            before = pixels().copy()
            poses = [pose_at(Vector((x, -0.4, 1.0)), Vector((0.0, 1.0, 0.0))) for x in np.linspace(-0.3, 0.3, 20)]
            s = stroke(bpy.context, settings, box, poses)
            changed = int(np.any(np.abs(pixels()[..., :3] - before[..., :3]) > 0.05, axis=2).sum())
            log(f"spray across the middle from 70 cm: {s.dabs if s else 0} dabs, {changed} texels changed")
    except Exception:
        log(traceback.format_exc())
    quit_cleanly()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
