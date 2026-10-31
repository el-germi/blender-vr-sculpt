"""Background tests for smoothing: the stroke stabilizer (on, off, amount, lag), which trigger actions it
applies to, take smoothing, and its panel rows.

blender --background --factory-startup --python tests/test_smoothing.py
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
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import anim, drawing, interactions, menus, operators, puppet, smoothing, strokes  # noqa: E402
import test_model as tm  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class Clock:
    def __init__(self, dt):
        self.t, self.dt = 0.0, dt

    def __call__(self):
        return self.t

    def tick(self):
        self.t += self.dt


def run(stab, clock, points):
    out = []
    for p in points:
        clock.tick()
        out.append(stab(p))
    return np.array([v[:] for v in out])


def jittery_line(n=200, length=0.4, jitter=0.003, seed=3):
    rng = np.random.default_rng(seed)
    pts = np.zeros((n, 3))
    pts[:, 0] = np.linspace(0.0, length, n)
    return pts + rng.normal(0.0, jitter, (n, 3)) * [0, 1, 1]


def test_stabilizer():
    print("== stabilizer")
    pts = jittery_line()
    clock = Clock(1 / 90)
    out = run(smoothing.Stabilizer(pts[0], 0.0, 0.0, clock), clock, pts)
    check(np.allclose(out, pts), "Amount 0 and no lag: the stroke is the tip exactly")

    clock = Clock(1 / 90)
    out = run(smoothing.Stabilizer(pts[0], 0.6, 0.0, clock), clock, pts)
    raw = np.abs(pts[50:, 1:]).std()
    smooth = np.abs(out[50:, 1:]).std()
    check(smooth < raw * 0.6, f"Amount 60% takes most of the jitter out ({smooth * 1000:.2f} vs {raw * 1000:.2f} mm)")

    # The same move at 45 and 90 Hz ends in about the same place: the filter is in time, not per update.
    ends = []
    for hz in (45, 90):
        clock = Clock(1 / hz)
        stab = smoothing.Stabilizer((0, 0, 0), 0.7, 0.0, clock)
        n = hz // 5  # 0.2 s
        for i in range(1, n + 1):
            clock.tick()
            p = stab((0.2 * i / n, 0, 0))
        ends.append(p.x)
    check(abs(ends[0] - ends[1]) < 0.01, f"same lag at 45 and 90 Hz ({ends[0]:.3f} / {ends[1]:.3f})")
    check(smoothing.time_constant(0.5) < 0.02 and smoothing.time_constant(1.0) == smoothing.MAX_TAU,
          "50% is a light filter, 100% the strongest")

    clock = Clock(1 / 90)
    stab = smoothing.Stabilizer((0, 0, 0), 0.0, 0.02, clock)
    clock.tick()
    check(stab((0.015, 0, 0)).length == 0.0, "Lag: inside the string the stroke point stays put")
    clock.tick()
    p = stab((0.05, 0, 0))
    check(abs(p.x - 0.03) < 1e-6, f"past it, the point is pulled along at the string's length ({p.x:.3f})")
    clock.tick()
    p = stab((0.045, 0.005, 0))
    check(abs(p.y) < 1e-6 and abs(p.x - 0.03) < 1e-6, "a small wobble inside the string doesn't move it")


def test_which_actions():
    print("== which trigger actions are smoothed")
    s = bpy.context.scene.vr_sculpt
    s.smooth_strokes, s.smooth_amount, s.smooth_lag = True, 0.5, 0.0
    check(smoothing.for_stroke(s, None, Vector(), 1.0) is not None, "a sculpt stroke is smoothed")
    for cls in (drawing.DrawStroke, drawing.NoteStroke):
        check(getattr(cls, "smooth", False), f"{cls.__name__} is a stroke")
    from vr_sculpt import paint, retopo, rig, uv
    for cls in (paint.PaintStroke, rig.PaintWeights, uv.FixUV, retopo.Strip, retopo.Relax):
        check(getattr(cls, "smooth", False), f"{cls.__module__.split('.')[-1]}.{cls.__name__} is a stroke")
    check(not getattr(uv.SeamMark, "smooth", False), "Seams aren't: they snap to edges and must reach the end")
    for cls in (interactions.UIPress, interactions.MoveObject, interactions.CreatePrimitive):
        handler = cls.__new__(cls)
        check(smoothing.for_stroke(s, handler, Vector(), 1.0) is None, f"{cls.__name__} isn't smoothed (no lag)")
    s.smooth_strokes = False
    check(smoothing.for_stroke(s, None, Vector(), 1.0) is None, "Smooth off: nothing is filtered")
    s.smooth_strokes, s.smooth_amount = True, 0.0
    check(smoothing.for_stroke(s, None, Vector(), 1.0) is None, "Amount 0 and no lag: nothing to do")
    s.smooth_amount = 0.5


def hold(ctx, ss, hand, points):
    ss.aim_loc[hand], ss.aim_rot[hand] = Vector(points[0]), Quaternion()
    ss.trigger = 1.0
    op = tm.harness()
    r = op.invoke(ctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR(points[0], Quaternion(), 1.0,
                                                                     hand=tm.actions.HANDS[hand])))
    if r != {'RUNNING_MODAL'}:
        return r
    for p in points[1:]:
        time.sleep(0.004)
        ss.aim_loc[hand] = Vector(p)
        op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.trigger = 0.0
    return op.modal(ctx, tm.FakeEvent('TIMER'))


def roughness(obj):
    p = strokes.load(obj)["p"]
    return float(np.abs(np.diff(p[:, 1:], 2, axis=0)).mean())


def test_draw_through_operator():
    print("== drawing with smoothing on and off")
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    ctx = tm.FakeContext(tm.FakeWM(ss))
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tip_offset, s.show_panel = 'DRAW', 0.0, False
    s.draw_tool, s.draw_style, s.draw_thickness, s.draw_width = 'DRAW', 'TUBE', 'FIXED', 0.004
    s.mirror_x = s.mirror_y = s.mirror_z = False
    pts = [tuple(p + [0, 0, 1]) for p in jittery_line(120, jitter=0.004)]
    rough = {}
    for on in (False, True):
        s.smooth_strokes, s.smooth_amount = on, 0.8
        before = set(bpy.data.objects)
        check(hold(ctx, ss, 1, pts) == {'FINISHED'}, f"a stroke is drawn (Smooth {'on' if on else 'off'})")
        obj = next(o for o in bpy.data.objects if o not in before)
        rough[on] = roughness(obj)
    check(rough[True] < rough[False] * 0.7,
          f"Smooth on draws a calmer stroke ({rough[True] * 1000:.2f} vs {rough[False] * 1000:.2f} mm)")
    s.smooth_strokes, s.smooth_amount = True, 0.5


def noisy_object(name, frames, seed=5):
    rng = np.random.default_rng(seed)
    obj = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(obj)
    obj.rotation_mode = 'QUATERNION'
    for f in frames:
        obj.location = (f * 0.01, rng.normal(0, 0.01), 0.0)
        q = Quaternion((0, 0, 1), 0.02 * f + rng.normal(0, 0.05))
        if f % 2:
            q.negate()  # The same rotation with the sign flipped, as keys can come out.
        obj.rotation_quaternion = q
        anim.insert_key(obj, f)
    return obj


def curve(obj, path, index):
    fc = next(c for c in anim.fcurves(obj) if c.data_path == path and c.array_index == index)
    return np.array([k.co[1] for k in fc.keyframe_points])


def test_takes():
    print("== take smoothing")
    frames = range(1, 41)
    obj = noisy_object("Take", frames)
    y0 = curve(obj, "location", 1)
    puppet._take["targets"] = {(obj.name, "")}
    n = puppet.smooth_take(bpy.context, 1, 40, 2.0)
    check(n == 10, f"location, rotation and scale curves smoothed ({n})")
    y1 = curve(obj, "location", 1)
    check(np.abs(np.diff(y1)).mean() < np.abs(np.diff(y0)).mean() * 0.5, "the wobble in a take is smoothed")
    x1 = curve(obj, "location", 0)
    check(abs(x1[20] - 0.21) < 1e-3, "a steady move keeps its path")
    q = np.stack([curve(obj, "rotation_quaternion", i) for i in range(4)], axis=1)
    check(np.allclose(np.linalg.norm(q, axis=1), 1.0, atol=1e-5), "smoothed rotations stay unit quaternions")
    angles = [Quaternion(r).to_euler().z for r in q]
    check(abs(angles[20] - 0.42) < 0.08, f"sign-flipped keys smooth as one rotation ({angles[20]:.3f} rad)")

    other = noisy_object("Untouched", frames, seed=9)
    before = curve(other, "location", 1)
    puppet._take["targets"] = {(obj.name, "")}
    puppet.smooth_take(bpy.context, 1, 40, 2.0)
    check(np.allclose(curve(other, "location", 1), before), "only the take's own targets are smoothed")

    obj2 = noisy_object("Range", frames, seed=11)
    y0 = curve(obj2, "location", 1)
    puppet._take["targets"] = {(obj2.name, "")}
    puppet.smooth_take(bpy.context, 21, 40, 2.0)
    y1 = curve(obj2, "location", 1)
    check(np.allclose(y1[:20], y0[:20]), "keys outside the take's frames stay as they were")
    puppet._take["targets"] = set()


def test_panel():
    print("== panel")
    s = bpy.context.scene.vr_sculpt
    s.smooth_strokes = True
    menus.open_sheet('SETTINGS')
    ids = {w.id for w in menus.layout(bpy.context)[0]}
    check({"smooth_strokes", "smooth_amount", "smooth_lag"} <= ids, "Settings: Smooth, Amount, Lag (every mode)")
    menus.close_sheet()
    for mode in ('DRAW', 'PAINT'):
        s.mode = mode
        menus.set_more(mode, True)
        ids = {w.id for w in menus.layout(bpy.context)[0]}
        if mode == 'PAINT' and "smooth_strokes" not in ids:
            check(True, "Paint needs a paintable mesh for its page; skipped")
            continue
        check({"smooth_strokes", "smooth_amount", "smooth_lag"} <= ids, f"{mode} More: Smooth, Amount, Lag")
        menus.set_more(mode, False)
    s.mode = 'DRAW'
    menus.set_more('DRAW', True)
    w = next(w for w in menus.layout(bpy.context)[0] if w.id == "smooth_strokes")
    w.on_press()
    ids = {w.id for w in menus.layout(bpy.context)[0]}
    check(not s.smooth_strokes and "smooth_amount" not in ids, "Smooth off hides Amount and Lag")
    lag = None
    s.smooth_strokes = True
    for w in menus.layout(bpy.context)[0]:
        if w.id == "smooth_lag":
            lag = w
    check(lag is not None and lag.fmt(0.0) == "Off" and lag.fmt(0.01) == "1.0 cm", "Lag reads Off at zero")
    menus.set_more('DRAW', False)
    s.mode = 'POSE'
    menus.set_more('POSE', True)
    ids = {w.id for w in menus.layout(bpy.context)[0]}
    check({"take_smooth", "take_smooth_frames"} <= ids, "Pose More: smooth takes and how much")
    menus.set_more('POSE', False)
    s.mode = 'SCULPT'


def main():
    vr_sculpt.register()
    try:
        test_stabilizer()
        test_which_actions()
        test_draw_through_operator()
        test_takes()
        test_panel()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
