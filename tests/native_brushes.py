"""Windowed test: every brush of the panel's native brush list does something when driven by NativeStroke,
and the drag brushes (Snake Hook, Grab, Pose...) follow the tip.

blender --factory-startup test_scene.blend --python tests/native_brushes.py

Writes tests/native_brushes_result.txt (ok / FAIL lines and a RESULT line).
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "native_brushes_result.txt")
lines = []
failures = []

RADIUS = 0.12
STEPS = 20
# Brushes that need something this test doesn't set up: they're only checked for not failing.
NEEDS = {"Density": "dyntopo", "Erase Multires Displacement": "Multires", "Smear Multires Displacement": "Multires",
         "Scene Project": "other objects to project on"}
# How much of the hand's move the grabbed vertex should follow (at least), by brush; the rest follow strength.
FOLLOW_MIN = {"Elastic Snake Hook": 0.4, "Elastic Grab": 0.2, "Thumb": 0.15, "Nudge": 0.1, "Pose": 0.15,
              "Pinch Point Cloth": 0.3, "Drag Cloth": 0.8, "Boundary": 0.1}


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


def check(cond, msg):
    log(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def read(obj, name, size):
    attr = obj.data.attributes.get(name)
    if attr is None:
        return None
    n = len(attr.data)
    a = np.empty(n * size, dtype=np.float32)
    key = "vector" if name == "position" else "color" if attr.data_type in {'FLOAT_COLOR', 'BYTE_COLOR'} else "value"
    attr.data.foreach_get(key, a)
    return a


def state(obj):
    """Everything a brush may change: positions, mask, face sets, colors."""
    return {"position": read(obj, "position", 3), "mask": read(obj, ".sculpt_mask", 1),
            "face sets": read(obj, ".sculpt_face_set", 1), "color": read(obj, "Col", 4)}


def changed(a, b):
    out = []
    for k in a:
        x, y = a[k], b[k]
        if x is None and y is not None:
            out.append(k)
        elif x is not None and y is not None and (len(x) != len(y) or np.abs(x - y).max() > 1e-5):
            out.append(k)
    return out


def make(kind, ctx, win, area, region):
    with ctx.temp_override(window=win, area=area, region=region):
        if ctx.active_object is not None and ctx.active_object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    if kind == 'sheet':  # a grid with an open border
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=120, y_subdivisions=120, size=2.0)
    else:
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
        if kind == 'limb':  # a long ellipsoid along Z, for Pose to bend
            bpy.context.active_object.scale = (0.15, 0.15, 1.0)
            bpy.ops.object.transform_apply(scale=True)
    obj = bpy.context.active_object
    col = obj.data.color_attributes.new("Col", 'FLOAT_COLOR', 'POINT')
    rgba = np.random.default_rng(1).random(len(col.data) * 4).astype(np.float32)  # varied, so Blur has work
    rgba[3::4] = 1.0
    col.data.foreach_set("color", rgba)
    obj.data.color_attributes.active_color_name = "Col"
    with ctx.temp_override(window=win, area=area, region=region):
        bpy.ops.object.mode_set(mode='SCULPT')
    return obj


def line(a, b, n=STEPS):
    a, b = np.array(a, float), np.array(b, float)
    return [a + (b - a) * i / n for i in range(n + 1)]


def main():
    import vr_sculpt
    from vr_sculpt import native
    vr_sculpt.register()
    try:
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)

        def use(name, kind):
            obj = make(kind, ctx, win, area, region)
            with ctx.temp_override(window=win, area=area, region=region):
                native.activate_brush(bpy.context, name)  # Also switches to the brush's tool (Paint, Mask...).
            return obj

        def stroke(obj, path):
            with ctx.temp_override(window=win, area=area, region=region):
                s = native.NativeStroke(bpy.context, obj, RADIUS)
                for p in path:
                    s.update(Vector(p), None, 1.0)
                    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
                s.finish()
            return s

        # Surface brushes: a sweep across the front of the ball, the tip just touching it, changes something.
        sweep = [Vector((np.sin(a), -np.cos(a), 0.0)) * 0.995 for a in np.linspace(-0.4, 0.4, 25)]
        drag_names = []
        for name in native.CATALOG_NAMES:
            obj = use(name, 'sphere')
            brush = bpy.context.tool_settings.sculpt.brush
            if brush is None or brush.name != name:
                check(False, f"{name}: activates (the brush is {brush.name if brush else None})")
                continue
            if native.is_drag(brush):
                drag_names.append(name)
                continue
            if name in NEEDS and name != "Scene Project":
                # Without Multires, Blender's Multires brushes crash it: the stroke must refuse them.
                try:
                    stroke(obj, sweep)
                    check(False, f"{name}: refused without {NEEDS[name]}")
                except RuntimeError as ex:
                    check(NEEDS[name].split()[0].lower() in str(ex).lower(), f"{name}: refused: {ex}")
                continue
            before = state(obj)
            s = stroke(obj, sweep)
            what = changed(before, state(obj))
            if name in NEEDS:
                check(True, f"{name}: ran ({s.dabs} dabs), needs {NEEDS[name]} to show anything")
            else:
                check(bool(what) and s.dabs > 0, f"{name}: {s.dabs} dabs, {s.dropped} dropped, changed {what}")

        # Drag brushes: the grabbed vertex follows the hand, out of the surface and across it.
        check(set(drag_names) == {"Snake Hook", "Elastic Snake Hook", "Pull", "Grab", "Grab 2D", "Elastic Grab", "Pose",
                                  "Thumb", "Nudge", "Drag Cloth", "Pinch Point Cloth"},
              f"drag brushes: {drag_names}")
        # Boundary isn't in the list (it writes NaN positions); picked on the desktop, the stroke refuses it.
        obj = use("Boundary", 'sheet')
        try:
            stroke(obj, line((0.0, -1.0, 0.0), (0.0, -1.0, 0.3)))
            check(False, "Boundary: refused")
        except RuntimeError as ex:
            check(np.isfinite(read(obj, "position", 3)).all(), f"Boundary: refused ({ex}), the mesh untouched")
        s0 = (0.0, -1.0, 0.0)
        cases = [('sphere', "out", line(s0, (0.0, -1.4, 0.0))),
                 ('sphere', "across", line(s0, (0.4, -1.0, 0.0))),
                 ('sphere', "out, then across",
                  line(s0, (0.0, -1.4, 0.0))[:-1] + line((0.0, -1.4, 0.0), (0.3, -1.4, 0.0))),
                 ('limb', "limb tip across", line((0.0, 0.0, 1.0), (0.4, 0.0, 0.9))),
                 ('sheet', "sheet edge up", line((0.0, -1.0, 0.0), (0.0, -1.0, 0.3))),
                 ('sheet', "sheet middle up", line((0.0, 0.0, 0.0), (0.0, 0.0, 0.3)))]
        for name in drag_names:
            for kind, label, path in cases:
                if name == "Pose" and kind != 'limb':
                    continue  # Pose bends limbs; on a ball it turns the whole side.
                if name == "Boundary" and label != "sheet edge up":
                    continue  # Boundary needs an open border.
                if name in {"Thumb", "Nudge", "Grab 2D"} and ("up" in label or "out" in label):
                    continue  # They slide the surface along itself.
                obj = use(name, kind)
                strength = native.get_strength(bpy.context)
                co = read(obj, "position", 3).reshape(-1, 3).astype(np.float64)
                k = int(np.argmin(np.linalg.norm(co - path[0], axis=1)))
                s = stroke(obj, path)
                after = read(obj, "position", 3).reshape(-1, 3)
                got = after[k] - co[k]
                want = np.array(path[-1]) - np.array(path[0])
                ratio = float(np.dot(got, want) / np.dot(want, want))
                low = FOLLOW_MIN.get(name, strength * 0.9)
                check(ratio >= low and s.dropped == 0,
                      f"{name} {label}: follows {ratio:.2f} of the move (want >= {low:.2f}, strength {strength:.2f}), "
                      f"{s.dabs} dabs, {s.dropped} dropped")

        # A low-poly mesh, edges (~30 cm) far longer than the brush (4 cm): the dabs used to be dropped (the ray's
        # point, a quarter of the way to the neighbors, fell outside the aimed view). Now the vertex follows.
        for name in ("Snake Hook", "Grab"):
            obj = make('sphere', ctx, win, area, region)
            with ctx.temp_override(window=win, area=area, region=region):
                bpy.ops.object.mode_set(mode='OBJECT')
                for o in list(bpy.data.objects):
                    bpy.data.objects.remove(o)
                bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=1.0)
                obj = bpy.context.active_object
                bpy.ops.object.mode_set(mode='SCULPT')
                native.activate_brush(bpy.context, name)
            co = read(obj, "position", 3).reshape(-1, 3).astype(np.float64)
            k = int(np.argmin(co[:, 1]))  # the front-most vertex
            start = co[k]
            path = line(start, start + start / np.linalg.norm(start) * 0.4)
            with ctx.temp_override(window=win, area=area, region=region):
                s = native.NativeStroke(bpy.context, obj, 0.04)
                for p in path:
                    s.update(Vector(p), None, 1.0)
                s.finish()
                strength = native.get_strength(bpy.context)
            got = read(obj, "position", 3).reshape(-1, 3)[k] - start
            want = path[-1] - start
            ratio = float(np.dot(got, want) / np.dot(want, want))
            check(s.dropped == 0 and ratio >= strength * 0.9,
                  f"{name} on a low-poly ball, 4 cm brush: follows {ratio:.2f} (strength {strength:.2f}), "
                  f"{s.dabs} dabs, {s.dropped} dropped")

        # Pulled with the vertex tracked or not (dyntopo / Multires / shape key): about the same.
        obj = use("Snake Hook", 'sphere')
        with ctx.temp_override(window=win, area=area, region=region):
            s = native.NativeStroke(bpy.context, obj, RADIUS)
            check(s.drag and s.tracked, "Snake Hook on a plain mesh reads the grabbed vertex back")
            s.tracked = False  # As with dyntopo: the grabbed point moves by the drag times the strength.
            path = cases[2][2]
            k = int(np.argmin(np.linalg.norm(read(obj, "position", 3).reshape(-1, 3) - path[0], axis=1)))
            s.anchor_vert = None
            for p in path:
                s.update(Vector(p), None, 1.0)
                if s.anchor_vert is not None:
                    s.anchor_vert = None
            s.finish()
        got = read(obj, "position", 3).reshape(-1, 3)[k]
        check(np.linalg.norm(got - path[-1]) < 0.03, f"untracked Snake Hook ends at the tip: {np.round(got, 3)}")

        # The panel: the list, a pick, the picked brush beside the quick buttons and in the B cycle.
        from vr_sculpt import menus, vr_ui
        c = bpy.context
        c.scene.vr_sculpt.engine = 'NATIVE'
        c.scene.vr_sculpt.mode = 'SCULPT'
        with ctx.temp_override(window=win, area=area, region=region):
            c.scene.vr_sculpt.native_picked = ""
            native.activate_brush(bpy.context, "Draw")
            widgets, _h = vr_ui.layout(menus.rows(bpy.context))
            cell = vr_ui.find(widgets, "brush_picked")
            check(cell is not None and cell.text == "Snake Hook", "Snake Hook is a quick button until one is picked")
            vr_ui.find(widgets, "brush_list").on_press()
            check(menus.picker_open(), "More opens the list")
            widgets, _h = vr_ui.layout(menus.rows(bpy.context))
            vr_ui.find(widgets, "cat_Transform").on_press()
            widgets, _h = vr_ui.layout(menus.rows(bpy.context))
            vr_ui.find(widgets, "opt_Pose").on_press()
            check(native.sculpt_brush(bpy.context).name == "Pose" and not menus.picker_open(),
                  "picking Pose makes it the brush")
            widgets, _h = vr_ui.layout(menus.rows(bpy.context))
            cell = vr_ui.find(widgets, "brush_picked")
            check(cell is not None and cell.text == "Pose" and cell.active, "Pose takes the picked quick button")
            names = [native.cycle_brush(bpy.context) for _ in range(len(native.NATIVE_BRUSHES) + 1)]
            check(names[-1] == "Pose" and names[0] == "Draw", f"B cycles through it: {names}")
    except Exception:
        log(traceback.format_exc())
        failures.append("exception")
    finally:
        log("RESULT: ALL PASSED" if not failures else f"RESULT: FAILED {failures}")
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
