"""Windowed probe, round 2: drive drag brushes so the grabbed part follows the tip.

Round 1 (native_drag_probe.py) found: Blender turns the screen motion of the mouse between the two stroke
elements into the drag, so motion along the view is lost (pulling out, with the view along the normal),
and a mouse that only grazes the mesh drops the dab. And each call grabs anew at the tip: with a strength
under 1 the grabbed vertices lag a little more every call, until nothing follows.

Here, per call:
- the dab starts where the grabbed point is now (a tracked vertex), not at the tip;
- the view is orthographic and looks across the motion (as close to the start normal as it can), so the
  whole motion is on screen and maps linearly to the drag;
- the lead mouse is over a point inside the mesh (the grabbed point minus the start normal), so the ray
  hits it; the second mouse is the lead plus the motion on screen.

blender --factory-startup test_scene.blend --python tests/native_drag_probe2.py
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
OUT = os.path.join(ROOT, "tests", "native_drag_probe2_result.txt")
lines = []

BRUSHES = ("Snake Hook", "Elastic Snake Hook", "Pull", "Grab", "Grab 2D", "Grab Silhouette", "Elastic Grab",
           "Thumb", "Nudge", "Pose", "Boundary", "Drag Cloth", "Grab Cloth", "Push Cloth", "Pinch Point Cloth",
           "Bend/Twist Cloth", "Stretch/Move Cloth", "Bend Boundary Cloth")
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


def make(kind, ctx, win, area, region):
    with ctx.temp_override(window=win, area=area, region=region):
        if ctx.active_object is not None and ctx.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    if kind == 'sphere':
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
    elif kind == 'limb':  # a long ellipsoid along Z: something for Pose to bend
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
        bpy.context.active_object.scale = (0.15, 0.15, 1.0)
        bpy.ops.object.transform_apply(scale=True)
    else:  # 'sheet': a grid with an open border, for Boundary
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=120, y_subdivisions=120, size=2.0)
    obj = bpy.context.active_object
    with ctx.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='SCULPT')
    return obj


def drag(obj, win, area, region, path, n0, twist=0.0):
    from vr_sculpt import native
    size = native._SceneSize(bpy.context, RADIUS)
    v = native.AimedView(area, region)
    rv3d = v.rv3d
    co = positions(obj)
    start = np.array(path[0])
    k = int(np.argmin(np.linalg.norm(co - start, axis=1)))
    offset = Vector(start - co[k])
    g = Vector(path[0])
    last = Vector(path[0])
    n0 = Vector(n0).normalized()
    misses = 0
    try:
        for tip in path[1:]:
            tip = Vector(tip)
            delta = tip - last
            last = tip
            if delta.length < 1e-9:
                continue
            m = delta.normalized()
            d = n0 - m * n0.dot(m)
            if d.length < 1e-3:
                d = m.orthogonal()
            d.normalize()
            v.look(g + d * RADIUS * 6.0, g)
            rv3d.view_perspective = 'ORTHO'
            rv3d.update()
            inside = g - n0 * RADIUS * 0.5
            a, b, c = v.mouse(inside), v.mouse(g), v.mouse(g + delta)
            if a is None or b is None or c is None:
                misses += 1
                continue
            lead_mouse = a
            mouse = (a[0] + c[0] - b[0], a[1] + c[1] - b[1])
            base = {"name": "", "size": 50.0, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0}
            elems = [dict(base, location=tuple(g), mouse=lead_mouse, mouse_event=lead_mouse, pressure=0.0,
                          is_start=True),
                     dict(base, location=tuple(g + delta), mouse=mouse, mouse_event=mouse, pressure=1.0,
                          is_start=False)]
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.sculpt.brush_stroke('EXEC_DEFAULT', True, stroke=elems, mode='NORMAL')
            g = Vector(obj.data.vertices[k].co) + offset
    finally:
        v.restore()
        size.restore()
    return k, misses


def main():
    import vr_sculpt
    from vr_sculpt import native
    vr_sculpt.register()
    try:
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)

        def line(a, b, n=STEPS):
            a, b = np.array(a, float), np.array(b, float)
            return [a + (b - a) * i / n for i in range(n + 1)]

        s0 = (0.0, -1.0, 0.0)
        cases = [
            ('sphere', "out", line(s0, (0.0, -1.4, 0.0)), (0, -1, 0)),
            ('sphere', "sideways", line(s0, (0.4, -1.0, 0.0)), (0, -1, 0)),
            ('sphere', "out+side", line(s0, (0.0, -1.4, 0.0))[:-1] + line((0.0, -1.4, 0.0), (0.3, -1.4, 0.0)),
             (0, -1, 0)),
            ('limb', "tip side", line((0.0, 0.0, 1.0), (0.4, 0.0, 0.9)), (0, 0, 1)),
            ('sheet', "edge up", line((0.0, -1.0, 0.0), (0.0, -1.0, 0.3)), (0, 0, 1)),
        ]
        for name in BRUSHES:
            for kind, label, path, n0 in cases:
                obj = make(kind, ctx, win, area, region)
                with ctx.temp_override(window=win, area=area, region=region):
                    native.activate_brush(bpy.context, name)
                    bpy.ops.wm.tool_set_by_id(name="builtin.brush", space_type='VIEW_3D')
                strength = bpy.context.tool_settings.sculpt.brush.strength
                before = positions(obj)
                try:
                    k, misses = drag(obj, win, area, region, path, n0)
                except Exception as ex:
                    log(f"{name:20s} {kind:6s} {label:9s} ERROR {ex}")
                    continue
                with ctx.temp_override(window=win, area=area, region=region):
                    bpy.ops.object.mode_set(mode='OBJECT')
                after = positions(obj)
                moved = np.linalg.norm(after - before, axis=1)
                want = np.array(path[-1]) - np.array(path[0])
                got = after[k] - before[k]
                ratio = float(np.dot(got, want) / max(np.dot(want, want), 1e-12))
                log(f"{name:20s} {kind:6s} {label:9s} s={strength:.2f} moved {int((moved > 1e-5).sum()):6d} "
                    f"| grabbed vert went {np.round(got, 3)} want {np.round(want, 2)} -> {ratio:.2f} "
                    f"| misses {misses}")
                bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    except Exception:
        log(traceback.format_exc())
    finally:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
