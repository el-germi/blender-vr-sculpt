"""Windowed probe: can native sculpt brushes be driven by a 3D point?

blender --factory-startup --python tests/native_probe.py
Writes tests/native_probe_result.txt and quits.
"""

import os
import sys
import time
import traceback

import bpy
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Quaternion, Vector

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_probe_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def view3d():
    win = bpy.context.window_manager.windows[0]
    for area in win.screen.areas:
        if area.type == 'VIEW_3D':
            region = next(r for r in area.regions if r.type == 'WINDOW')
            return win, area, region
    raise RuntimeError("no 3D view")


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3).astype(np.float64)


def moved_centroid(before, after):
    d = np.linalg.norm(after - before, axis=1)
    if d.max() < 1e-7:
        return None, 0.0
    return (before * d[:, None]).sum(axis=0) / d.sum(), float(d.max())


def stroke_elem(location, mouse, size=60.0, start=True):
    return {"name": "", "location": tuple(location), "mouse": tuple(mouse), "mouse_event": tuple(mouse),
            "pressure": 1.0, "size": size, "x_tilt": 0.0, "y_tilt": 0.0, "time": 0.0, "is_start": start}


def run_stroke(win, area, region, obj, elems):
    before = positions(obj)
    t0 = time.perf_counter()
    with bpy.context.temp_override(window=win, area=area, region=region):
        res = bpy.ops.sculpt.brush_stroke(stroke=elems, mode='NORMAL')
    ms = (time.perf_counter() - t0) * 1000
    after = positions(obj)
    return res, ms, before, after


def fmt(v):
    return "None" if v is None else "(" + ", ".join(f"{x:.3f}" for x in v) + ")"


def probe():
    win, area, region = view3d()
    rv3d = area.spaces.active.region_3d
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
    obj = bpy.context.active_object
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='SCULPT')
    log("mode:", obj.mode, "brush:", getattr(bpy.context.tool_settings.sculpt.brush, "name", None))

    # Front view looking at the origin.
    rv3d.view_perspective = 'PERSP'
    rv3d.view_location = (0, 0, 0)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)  # look along +Y
    rv3d.view_distance = 6.0
    area.tag_redraw()
    bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)

    center = (region.width / 2, region.height / 2)
    target = Vector((0.7071, -0.7071, 0.0))  # a point on the sphere, off-center in this view
    hit_center = Vector((0, -1, 0))  # where the center ray hits the sphere
    log("region", region.width, "x", region.height)

    # A: location says target, mouse says center. Which one moves?
    res, ms, b, a = run_stroke(win, area, region, obj, [stroke_elem(target, center)])
    c, m = moved_centroid(b, a)
    log(f"A location=target, mouse=center -> {res}, {ms:.1f} ms, moved centroid {fmt(c)} max {m:.4f}")
    if c is not None:
        log("   dist to target", round((Vector(c) - target).length, 3), "dist to center hit", round((Vector(c) - hit_center).length, 3))

    # B: mouse at the target's screen projection (laser-pointer style).
    mouse = view3d_utils.location_3d_to_region_2d(region, rv3d, target)
    res, ms, b, a = run_stroke(win, area, region, obj, [stroke_elem(target, mouse)])
    c, m = moved_centroid(b, a)
    log(f"B mouse=projected target -> {res}, {ms:.1f} ms, centroid {fmt(c)} max {m:.4f}")
    if c is not None:
        log("   dist to target", round((Vector(c) - target).length, 3))

    # C: re-aim the view at the target from outside (what a VR 'virtual camera' would do).
    rv3d.view_location = target
    rv3d.view_rotation = Quaternion((0, 0, 1), 0.7854) @ Quaternion((1, 0, 0), 1.5708)
    rv3d.view_distance = 0.5
    bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)
    res, ms, b, a = run_stroke(win, area, region, obj, [stroke_elem(target, center)])
    c, m = moved_centroid(b, a)
    log(f"C view re-aimed, mouse=center -> {res}, {ms:.1f} ms, centroid {fmt(c)} max {m:.4f}")
    if c is not None:
        log("   dist to target", round((Vector(c) - target).length, 3))

    # Back to the front view: the center ray hits the sphere again.
    rv3d.view_location = (0, 0, 0)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
    rv3d.view_distance = 6.0
    bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)

    # C2: target on the far side of the sphere (hidden from this view), mouse over the mesh.
    back = Vector((0.0, 1.0, 0.0))
    res, ms, b, a = run_stroke(win, area, region, obj, [stroke_elem(back, center)])
    c, m = moved_centroid(b, a)
    log(f"C2 hidden back-side target, mouse=center -> {res}, {ms:.1f} ms, centroid {fmt(c)} max {m:.4f}")

    # C3: target in empty space next to the sphere surface (not on it).
    air = Vector((0.0, 0.0, 1.05))
    res, ms, b, a = run_stroke(win, area, region, obj, [stroke_elem(air, center)])
    c, m = moved_centroid(b, a)
    log(f"C3 target 5 cm above the surface -> {res}, centroid {fmt(c)} max {m:.4f}")

    # D: an 11-dab stroke in one call vs 11 single-dab calls.
    pts = [target + Vector((0, 0, -0.3 + 0.06 * i)) for i in range(11)]
    elems = [stroke_elem(p, center, start=(i == 0)) for i, p in enumerate(pts)]
    res, ms_multi, b, a = run_stroke(win, area, region, obj, elems)
    c, m = moved_centroid(b, a)
    log(f"D 11-dab stroke, one call -> {res}, {ms_multi:.1f} ms, centroid {fmt(c)} max {m:.4f}")
    singles = []
    before_singles = positions(obj)
    for p in pts:
        _, ms1, _, _ = run_stroke(win, area, region, obj, [stroke_elem(p, center)])
        singles.append(ms1)
    after_singles = positions(obj)
    log(f"D' 11 single-dab calls: avg {np.mean(singles):.2f} ms each")

    # F: does one undo revert one dab or all 11?
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.ed.undo()
        after_undo = positions(obj)
        total = np.linalg.norm(after_singles - before_singles, axis=1).sum()
        reverted = np.linalg.norm(after_singles - after_undo, axis=1).sum()
        log(f"F one undo reverted {reverted / max(total, 1e-12) * 100:.0f}% of the 11 single-dab calls")
    except RuntimeError as ex:
        log("F undo check skipped:", str(ex).splitlines()[0])

    # E: scene-space brush size, so radius doesn't depend on pixels or view distance.
    # E: pixel size -> world radius. Project a world radius into pixels at the target.
    p0 = view3d_utils.location_3d_to_region_2d(region, rv3d, target)
    right = rv3d.view_rotation @ Vector((1, 0, 0))
    for radius in (0.1, 0.3):
        p1 = view3d_utils.location_3d_to_region_2d(region, rv3d, target + right * radius)
        px = (p1 - p0).length
        res, ms, b, a = run_stroke(win, area, region, obj, [stroke_elem(target, center, size=px)])
        d = np.linalg.norm(a - b, axis=1)
        ext = np.linalg.norm(b[d > 1e-6] - np.array(target), axis=1).max() if (d > 1e-6).any() else 0
        log(f"E size={px:.1f}px for world radius {radius}: affected verts reach {ext:.3f} from the target")
    brush = bpy.context.tool_settings.sculpt.brush
    log("   brush.size", brush.size, "use_locked_size", brush.use_locked_size, "unprojected_size", brush.unprojected_size)

    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='OBJECT')


def redraw_cost():
    """How much a Python position write costs once the viewport redraws the mesh."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from vr_sculpt import brushes
    win, area, region = view3d()
    rv3d = area.spaces.active.region_3d
    rv3d.view_location = (0, 0, 0)
    rv3d.view_distance = 6.0
    log("== redraw cost after a Python write (Solid, one window)")
    for subdiv in (6, 7, 8):
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=subdiv, radius=1.0)
        obj = bpy.context.active_object
        n = len(obj.data.vertices)
        st = brushes.MeshState(obj)

        def redraw(k=5):
            t0 = time.perf_counter()
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=k)
            return (time.perf_counter() - t0) * 1000 / k

        redraw()
        idle = redraw()
        times = []
        s = brushes.Stroke(st, "DRAW", 0.1, 0.5)
        s.update(Vector((-0.3, 0, 1.0)))
        for i in range(1, 15):
            p = Vector((-0.3 + i * 0.02, 0, 1.0))
            t0 = time.perf_counter()
            s.update(p)
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
            times.append((time.perf_counter() - t0) * 1000)
        log(f"  {n:>9,} verts: idle redraw {idle:.1f} ms, dab+redraw {np.mean(times):.1f} ms (max {np.max(times):.1f})")


def main():
    try:
        log("Blender", bpy.app.version_string)
        probe()
        redraw_cost()
    except Exception:
        log(traceback.format_exc())
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
