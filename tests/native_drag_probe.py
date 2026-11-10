"""Windowed probe: do Blender's drag brushes (Snake Hook, Grab, Pose...) follow the tip when driven one
short sculpt.brush_stroke call per controller sample, as native.NativeStroke does?

Each brush pulls a point of a sphere straight out (along the normal) and then sideways, with the desktop
view aimed two ways: along the surface normal (what surface brushes use) and from the side of the motion.

blender --factory-startup test_scene.blend --python tests/native_drag_probe.py
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "native_drag_probe_result.txt")
lines = []

BRUSHES = ("Snake Hook", "Elastic Snake Hook", "Pull", "Grab", "Elastic Grab", "Thumb", "Nudge", "Twist",
           "Pose", "Boundary", "Grab Cloth", "Drag Cloth", "Draw")
RADIUS = 0.12
STEPS = 20


def quit_cleanly():
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3).astype(np.float64)


def fresh_sphere(ctx, win, area, region):
    with ctx.temp_override(window=win, area=area, region=region):
        if ctx.active_object is not None and ctx.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
    obj = bpy.context.active_object
    with ctx.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='SCULPT')
    return obj


def run(obj, win, area, region, path, side):
    """Drive the active brush along path (local = world here), one 2-element call per sample."""
    from vr_sculpt import native
    size = native._SceneSize(bpy.context, RADIUS)
    v = native.AimedView(area, region)
    last = None
    drops = 0
    try:
        for i, tip in enumerate(path):
            tip = Vector(tip)
            if last is None:
                last = tip
                continue
            motion = tip - last
            n = Vector(last).normalized()  # sphere normal near the start; good enough for the probe
            if side and motion.length > 1e-9:
                m = motion.normalized()
                d = n - m * n.dot(m)
                if d.length < 1e-3:
                    d = m.orthogonal()
                look_dir = d.normalized()
            else:
                look_dir = n
            eye = RADIUS * 4.0
            v.look(last + look_dir * eye, last)
            lead_mouse, mouse = v.mouse(last), v.mouse(tip)
            if lead_mouse is None or mouse is None:
                drops += 1
                last = tip
                continue
            base = {"name": "", "size": 50.0, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0}
            elems = [dict(base, location=tuple(last), mouse=lead_mouse, mouse_event=lead_mouse, pressure=0.0,
                          is_start=True),
                     dict(base, location=tuple(tip), mouse=mouse, mouse_event=mouse, pressure=1.0,
                          is_start=False)]
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.sculpt.brush_stroke('EXEC_DEFAULT', True, stroke=elems, mode='NORMAL')
            last = tip
    finally:
        v.restore()
        size.restore()
    return drops


def main():
    import vr_sculpt
    from vr_sculpt import native
    vr_sculpt.register()
    try:
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        start = np.array((0.0, -1.0, 0.0))
        out_path = [start + np.array((0.0, -0.4, 0.0)) * i / STEPS for i in range(STEPS + 1)]
        side_path = [start + np.array((0.4, 0.0, 0.0)) * i / STEPS for i in range(STEPS + 1)]
        for name in BRUSHES:
            for side in (False, True):
                for label, path in (("out", out_path), ("sideways", side_path)):
                    obj = fresh_sphere(ctx, win, area, region)
                    with ctx.temp_override(window=win, area=area, region=region):
                        native.activate_brush(bpy.context, name)
                        bpy.ops.wm.tool_set_by_id(name="builtin.brush", space_type='VIEW_3D')
                    before = positions(obj)
                    try:
                        drops = run(obj, win, area, region, path, side)
                    except Exception as ex:
                        log(f"{name:20s} {'side' if side else 'normal':6s} {label:8s} ERROR {ex}")
                        continue
                    # Leave Sculpt Mode so obj.data has the result.
                    with ctx.temp_override(window=win, area=area, region=region):
                        bpy.ops.object.mode_set(mode='OBJECT')
                    after = positions(obj)
                    moved = np.linalg.norm(after - before, axis=1)
                    end = path[-1]
                    nearest = float(np.min(np.linalg.norm(after - end, axis=1)))
                    # The vertex that started nearest the start point: where did it go?
                    k = int(np.argmin(np.linalg.norm(before - start, axis=1)))
                    follow = after[k] - before[k]
                    want = end - start
                    log(f"{name:20s} {'side' if side else 'normal':6s} {label:8s} moved {int((moved > 1e-5).sum()):5d} "
                        f"max {moved.max():.3f} | start vert went {np.round(follow, 3)} (want {np.round(want, 2)}) "
                        f"| nearest vert to end {nearest:.3f} | drops {drops}")
                    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    except Exception:
        log(traceback.format_exc())
    finally:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
