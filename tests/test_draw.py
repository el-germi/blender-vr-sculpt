"""Background tests for Draw mode: stroke geometry, drawing, notes, symmetry, edit, erase, undo.

blender --background --factory-startup --python tests/test_draw.py
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
from vr_sculpt import drawing, menus, operators, strokes, vr_ui  # noqa: E402
import test_model as tm  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    strokes.history.clear()
    if bpy.context.scene.annotation is not None:
        bpy.data.annotations.remove(bpy.context.scene.annotation)


def line_samples(style='TUBE', n=20, length=0.4, width=0.02, sym=0, center=(0, 0, 0), side=(0, 0, 1)):
    p = np.zeros((n, 3))
    p[:, 0] = np.linspace(0, length, n) + 0.2
    return {"style": style, "sym": sym, "c": np.array(center, dtype=float), "p": p,
            "w": np.full(n, width), "s": np.tile(np.array(side, dtype=float), (n, 1))}


def face_normals(mesh):
    n = len(mesh.polygons)
    normals = np.empty(n * 3)
    centers = np.empty(n * 3)
    mesh.polygons.foreach_get("normal", normals)
    mesh.polygons.foreach_get("center", centers)
    return normals.reshape(n, 3), centers.reshape(n, 3)


def test_geometry():
    print("== geometry")
    p = np.array([[0, 0, 0], [1, 0, 0], [2, 1, 0], [3, 1, 1.0]])
    out, (vals,) = strokes.catmull_rom(p, (np.array([1.0, 2.0, 3.0, 4.0]),), subdiv=3)
    check(len(out) == 10 and np.allclose(out[::3], p), "Catmull-Rom passes through every sample")
    check(np.allclose(vals[::3], [1, 2, 3, 4]), "per-sample values are interpolated alongside")

    mesh = bpy.data.meshes.new("t")
    s = line_samples()
    strokes.write_mesh(mesh, *strokes.build(s))
    check(not mesh.validate(verbose=True), "tube mesh is valid")
    normals, centers = face_normals(mesh)
    axis_y = 0.0
    radial = centers.copy()
    radial[:, 0] = 0
    sides = np.abs(normals[:, 0]) < 0.9
    outward = np.einsum("ij,ij->i", normals[sides], radial[sides] - [0, axis_y, 0]) > 0
    check(outward.all(), f"tube side faces point outward ({outward.sum()}/{len(outward)})")
    caps = ~sides
    check(caps.sum() == 2 and normals[caps][centers[caps][:, 0].argmin(), 0] < 0
          and normals[caps][centers[caps][:, 0].argmax(), 0] > 0, "end caps face away from the stroke")
    radius = np.abs(np.array([v.co[1:] for v in mesh.vertices])).max()
    check(abs(radius - 0.01) < 1e-4, f"tube radius is half the width ({radius:.4f})")

    s = line_samples(sym=1, center=(0, 0, 0))
    strokes.write_mesh(mesh, *strokes.build(s))
    check(not mesh.validate(), "mirrored tube mesh is valid")
    co = np.array([v.co[:] for v in mesh.vertices])
    half = len(co) // 2
    reflected = co[half:] * [-1, 1, 1]
    nearest = np.linalg.norm(reflected[:, None] - co[None, :half], axis=2).min(axis=1)
    check(nearest.max() < 1e-5, "X mirror copies every vertex across the center")
    normals, centers = face_normals(mesh)
    mirrored = centers[:, 0] < 0
    radial = centers.copy()
    radial[:, 0] = 0
    sides = np.abs(normals[:, 0]) < 0.9
    ok = np.einsum("ij,ij->i", normals[mirrored & sides], radial[mirrored & sides]) > 0
    check(ok.all(), "mirrored copy's faces still point outward")

    s = line_samples(style='RIBBON', side=(0, 1, 0))
    strokes.write_mesh(mesh, *strokes.build(s), smooth=False)
    co = np.array([v.co[:] for v in mesh.vertices])
    check(np.allclose(co[:, 2], 0) and abs(np.abs(co[:, 1]).max() - 0.01) < 1e-6,
          "ribbon lies across the controller's side vector, width as set")
    bpy.data.meshes.remove(mesh)


def setup_trigger():
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    wm = tm.FakeWM(ss)
    ctx = tm.FakeContext(wm)
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tip_offset, s.show_panel = 'DRAW', 0.0, False
    s.draw_tool, s.draw_style, s.draw_color = 'DRAW', 'TUBE', 'RED'
    s.draw_width, s.draw_smoothing, s.draw_pressure = 0.02, 0.0, True
    s.mirror_x = s.mirror_y = s.mirror_z = False
    bpy.context.scene.cursor.location = (0, 0, 0)
    return ss, ctx, s


def hold(ctx, ss, hand, points, values=None, rot=Quaternion()):
    """Hold the trigger through points (with per-point trigger values), then release."""
    values = values or [1.0] * len(points)
    ss.aim_loc[hand], ss.aim_rot[hand] = Vector(points[0]), Quaternion(rot)
    ss.trigger = values[0]
    op = tm.harness()
    r = op.invoke(ctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR(points[0], rot, values[0],
                                                                     hand=tm.actions.HANDS[hand])))
    if r != {'RUNNING_MODAL'}:
        return op, r
    for p, v in zip(points[1:], values[1:]):
        time.sleep(0.01)
        ss.aim_loc[hand], ss.trigger = Vector(p), v
        op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.trigger = 0.0
    time.sleep(0.01)
    return op, op.modal(ctx, tm.FakeEvent('TIMER'))


def path(a, b, n=25):
    return [tuple(Vector(a).lerp(Vector(b), i / (n - 1))) for i in range(n)]


def test_draw_and_undo():
    print("== drawing with the trigger, quick undo")
    clear()
    ss, ctx, s = setup_trigger()
    values = list(np.linspace(0.35, 1.0, 25))
    op, r = hold(ctx, ss, 1, path((0, 0, 1), (0.3, 0.1, 1.1)), values)
    objs = [o for o in bpy.data.objects if strokes.is_stroke(o)]
    check(r == {'FINISHED'} and len(objs) == 1, "a drag draws one stroke object")
    obj = objs[0]
    samples = strokes.load(obj)
    check(len(samples["p"]) > 10, f"stroke has samples ({len(samples['p'])})")
    check(samples["w"][-1] > samples["w"][0] * 2, "harder trigger, wider stroke (pressure)")
    check(obj.users_collection[0].name == strokes.COLLECTION, "strokes go to the 'VR Strokes' collection")
    check(obj.data.materials[0].name == "VR Stroke Red", "stroke uses the chosen color's material")
    check(len(obj.data.polygons) > 0 and not obj.data.validate(), "stroke mesh is built and valid")

    op, r = hold(ctx, ss, 1, [(1, 1, 1), (1.0005, 1, 1)])
    check(r != {'FINISHED'} and len([o for o in bpy.data.objects if strokes.is_stroke(o)]) == 1,
          "a click doesn't leave a stroke behind")

    hold(ctx, ss, 0, path((0, 0.5, 1), (0.2, 0.6, 1.2)))
    check(len([o for o in bpy.data.objects if strokes.is_stroke(o)]) == 2, "left hand draws too")
    b = tm.harness()  # not needed; B is the cycle_brush operator:
    operators.VRSCULPT_OT_cycle_brush.execute(type("Op", (), {"left_hand": False, "report": lambda *a: None})(),
                                              bpy.context)
    left = [o for o in bpy.data.objects if strokes.is_stroke(o)]
    check(len(left) == 1 and left[0] == obj, "B (right) deletes the most recent stroke")
    operators.undo_last_stroke(bpy.context)
    check(not any(strokes.is_stroke(o) for o in bpy.data.objects), "undo again deletes the one before")
    check(operators.undo_last_stroke(bpy.context) is None, "nothing left to undo is fine")


def test_mirror_and_notes():
    print("== symmetry and notes")
    clear()
    ss, ctx, s = setup_trigger()
    s.mirror_x = True
    bpy.context.scene.cursor.location = (0.1, 0, 0)
    hold(ctx, ss, 1, path((0.3, 0, 1), (0.5, 0.2, 1.1)))
    obj = next(o for o in bpy.data.objects if strokes.is_stroke(o))
    bpy.context.view_layer.update()
    lo = min((obj.matrix_world @ Vector(c)).x for c in obj.bound_box)
    hi = max((obj.matrix_world @ Vector(c)).x for c in obj.bound_box)
    check(abs((lo + hi) / 2 - 0.1) < 0.01, f"mirrored stroke is symmetric about the cursor (x {lo:.2f}..{hi:.2f})")

    s.draw_style = 'NOTE'
    s.draw_color = 'BLUE'
    hold(ctx, ss, 1, path((0.3, 0, 1), (0.4, 0.3, 1.2)))
    items = strokes.note_strokes(bpy.context)
    check(len(items) == 2, f"a mirrored note is two annotation strokes ({len(items)})")
    layer, frame, stroke = items[0]
    check(layer.info == "VR Notes Blue" and stroke.display_mode == '3DSPACE' and len(stroke.points) > 10,
          "notes are 3D annotation strokes in a per-color layer")
    check(abs(sum(p.co.x for p in items[1][2].points) / len(items[1][2].points)
              + sum(p.co.x for p in stroke.points) / len(stroke.points) - 0.2) < 1e-3,
          "the mirrored note is reflected across the cursor")
    operators.undo_last_stroke(bpy.context)
    check(len(strokes.note_strokes(bpy.context)) == 0, "quick undo removes the note and its mirror")
    operators.undo_last_stroke(bpy.context)
    check(not any(strokes.is_stroke(o) for o in bpy.data.objects), "...then the mirrored stroke")


def test_edit_and_erase():
    print("== edit and erase")
    clear()
    ss, ctx, s = setup_trigger()
    s.draw_pressure = False
    hold(ctx, ss, 1, path((0, 0, 1), (0.4, 0, 1), 41))
    obj = next(o for o in bpy.data.objects if strokes.is_stroke(o))
    before = strokes.load(obj)["p"].copy() + np.array(obj.location[:])
    s.draw_tool = 'EDIT'
    hold(ctx, ss, 1, [(0.2, 0, 1), (0.2, 0, 1.03), (0.2, 0, 1.06)])
    after = strokes.load(obj)["p"] + np.array(obj.location[:])
    mid = np.argmin(np.abs(before[:, 0] - 0.2))
    check(abs(after[mid, 2] - before[mid, 2] - 0.06) < 0.005, f"Edit pulls the grabbed part ({after[mid, 2] - before[mid, 2]:.3f} of 0.06)")
    check(np.allclose(after[[0, -1]], before[[0, -1]]), "the ends, out of reach, stay put")

    # Through a mirrored copy: grab the copy, the original moves mirrored.
    clear()
    ss, ctx, s = setup_trigger()
    s.draw_pressure, s.mirror_x = False, True
    hold(ctx, ss, 1, path((0.1, 0, 1), (0.5, 0, 1), 41))
    obj = next(o for o in bpy.data.objects if strokes.is_stroke(o))
    before = strokes.load(obj)["p"].copy() + np.array(obj.location[:])
    s.draw_tool = 'EDIT'
    hold(ctx, ss, 1, [(-0.3, 0, 1), (-0.35, 0, 1.05)])
    after = strokes.load(obj)["p"] + np.array(obj.location[:])
    mid = np.argmin(np.abs(before[:, 0] - 0.3))
    moved = after[mid] - before[mid]
    check(abs(moved[0] - 0.05) < 0.006 and abs(moved[2] - 0.05) < 0.006,
          f"pulling the mirror copy moves the original mirrored ({np.round(moved, 3)})")

    # Erase: sweep through the stroke and a note.
    s.draw_tool, s.draw_style, s.mirror_x = 'DRAW', 'NOTE', False
    hold(ctx, ss, 1, path((0, 0.5, 1), (0.3, 0.5, 1)))
    check(len(strokes.note_strokes(bpy.context)) == 1, "note drawn")
    s.draw_tool = 'ERASE'
    # One fast sweep (two frames) across an unedited part of the stroke (y=0) and the note (y=0.5).
    hold(ctx, ss, 1, [(0.15, -0.05, 1), (0.15, 0.6, 1)])
    check(not any(strokes.is_stroke(o) for o in bpy.data.objects), "Erase removed the stroke it touched")
    check(len(strokes.note_strokes(bpy.context)) == 0, "Erase removed the note it touched")


def test_sculpt_forgets_and_panel():
    print("== sculpting a stroke, panel rows")
    clear()
    ss, ctx, s = setup_trigger()
    hold(ctx, ss, 1, path((0, 0, 1), (0.3, 0, 1)))
    obj = next(o for o in bpy.data.objects if strokes.is_stroke(o))
    s.mode, s.engine, s.radius = 'SCULPT', 'PYTHON', 0.05
    tm.primitives.make_active(bpy.context, obj)
    tip = obj.matrix_world @ Vector(strokes.load(obj)["p"][5])
    hold(ctx, ss, 1, path(tip, tip + Vector((0.01, 0.01, 0)), 5))
    check(not strokes.is_stroke(obj), "sculpting a stroke turns it into a plain mesh")

    s.mode = 'DRAW'
    widgets, _ = vr_ui.layout(menus.rows(bpy.context))
    ids = {w.id for w in widgets}
    check({"mode_draw", "tool_EDIT", "style_RIBBON", "color_GREEN", "draw_width", "mirror_x", "undo_stroke"} <= ids,
          "Draw panel has tools, styles, colors, width, mirror and undo")
    vr_ui.find(widgets, "color_GREEN").on_press()
    vr_ui.find(widgets, "mirror_z").on_press()
    check(s.draw_color == 'GREEN' and s.mirror_z, "swatches and mirror toggles work")
    sw = vr_ui.find(vr_ui.layout(menus.rows(bpy.context))[0], "color_GREEN")
    check(sw.swatch is not None and sw.active, "the chosen swatch is marked")


def main():
    vr_sculpt.register()
    try:
        test_geometry()
        test_draw_and_undo()
        test_mirror_and_notes()
        test_edit_and_erase()
        test_sculpt_forgets_and_panel()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
