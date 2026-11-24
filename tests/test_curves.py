"""Background tests for Bezier curves: the Curve tool (place, drag handles, close, finish, undo) and editing
curves with the Edit tool (move points and handles, insert, corner / smooth, delete), erasing, the panel.

blender --background --factory-startup --python tests/test_curves.py
"""

import os
import sys
import traceback

import time

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import controllers, curves, menus, operators, strokes, vr_ui  # noqa: E402
import test_model as tm  # noqa: E402

failures = []
MAIN, OFF = 1, 0  # Right-handed: the right controller is the main hand.


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def setup():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    strokes.history.clear()
    curves.forget()
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    ctx = tm.FakeContext(tm.FakeWM(ss))
    s = bpy.context.scene.vr_sculpt
    s.mode, s.tip_offset, s.show_panel = 'DRAW', 0.0, False
    s.draw_tool, s.draw_color, s.draw_width, s.smooth_strokes = 'CURVE', 'RED', 0.02, False
    s.mirror_x = s.mirror_y = s.mirror_z = False
    return ss, ctx, s


def hold(ctx, ss, hand, points, rots=None):
    """Hold the trigger through points (and controller rotations), then release (as test_draw.hold)."""
    rots = rots or [Quaternion()] * len(points)
    ss.aim_loc[hand], ss.aim_rot[hand], ss.trigger = Vector(points[0]), Quaternion(rots[0]), 1.0
    op = tm.harness()
    r = op.invoke(ctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR(points[0], rots[0], 1.0,
                                                                     hand=tm.actions.HANDS[hand])))
    if r != {'RUNNING_MODAL'}:
        return op, r
    for p, q in zip(points[1:], rots[1:]):
        time.sleep(0.012)
        ss.aim_loc[hand], ss.aim_rot[hand] = Vector(p), Quaternion(q)
        op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.trigger = 0.0
    time.sleep(0.012)
    r = op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.aim_rot[hand] = Quaternion()
    return op, r


def click(ctx, ss, p, hand=MAIN):
    return hold(ctx, ss, hand, [p, p])


def our_curves():
    return [o for o in bpy.data.objects if curves.is_ours(o)]


def world_points(obj, field="co"):
    s = curves.read(obj, 0)
    return curves._world(obj, getattr(s, field))


def dense(obj, n=64):
    pts, _t = curves.sample(curves.read(obj, 0), n)
    return curves._world(obj, pts.reshape(-1, 3))


def test_place_and_finish():
    print("== Curve tool: place points, drag handles, finish")
    ss, ctx, s = setup()
    spots = [(0, 0, 1), (0.2, 0, 1), (0.4, 0.1, 1)]
    for p in spots:
        op, r = click(ctx, ss, p)
        check(r == {'FINISHED'}, f"a press at {p} adds a point (one undo step)")
    objs = our_curves()
    check(len(objs) == 1, "one curve object for the points")
    obj = objs[0]
    check(np.allclose(world_points(obj), spots, atol=1e-5), "points sit where the tip pressed")
    sp = obj.data.splines[0]
    check(all(p.handle_left_type == 'AUTO' and p.handle_right_type == 'AUTO' for p in sp.bezier_points),
          "clicked points get smooth (auto) handles")
    check(abs(obj.data.bevel_depth - 0.01) < 1e-6 and obj.data.materials[0].name == "VR Stroke Red",
          "bevel is half the width, the color is the chosen one")
    check(obj.users_collection[0].name == strokes.COLLECTION, "curves go to the 'VR Strokes' collection")
    check(curves.active(bpy.context) == obj, "the curve stays open for more points")

    # Press and drag: the handles come out.
    op, r = hold(ctx, ss, MAIN, [(0.6, 0, 1), (0.62, 0.02, 1), (0.65, 0.05, 1)])
    pt = obj.data.splines[0].bezier_points[3]
    check(pt.handle_left_type == 'ALIGNED' and np.allclose(pt.handle_right[:], (0.65, 0.05, 0), atol=1e-4) and
          np.allclose(pt.handle_left[:], (0.55, -0.05, 0), atol=1e-4),
          "dragging pulls the outgoing handle to the tip and mirrors the other")
    check(np.allclose(world_points(obj)[3], (0.6, 0, 1), atol=1e-5), "the point stays where it was pressed")

    op, r = click(ctx, ss, (0.605, 0.005, 1))
    check(curves.active(bpy.context) is None and len(obj.data.splines[0].bezier_points) == 4,
          "a press on the last point finishes the curve (no point added)")
    click(ctx, ss, (0, 1, 1))
    check(len(our_curves()) == 2, "the next press starts a new curve")
    click(ctx, ss, (0, 1, 1))
    check(len(our_curves()) == 1 and curves.active(bpy.context) is None,
          "finishing a curve of one point removes it")


def test_close_undo_and_b():
    print("== close, quick undo, B")
    ss, ctx, s = setup()
    for p in [(0, 0, 1), (0.3, 0, 1), (0.3, 0.3, 1)]:
        click(ctx, ss, p)
    obj = our_curves()[0]
    click(ctx, ss, (0.005, 0, 1))
    check(obj.data.splines[0].use_cyclic_u and curves.active(bpy.context) is None,
          "a press on the first point closes the curve")
    check(operators.undo_last_stroke(bpy.context) == "curve close" and not obj.data.splines[0].use_cyclic_u
          and curves.active(bpy.context) == obj, "undo opens it again, to go on building")
    check(operators.undo_last_stroke(bpy.context) == "curve point" and len(obj.data.splines[0].bezier_points) == 2,
          "undo again drops the last point")
    b = type("Op", (), {"off_hand": False, "report": lambda *a: None})()
    operators.VRSCULPT_OT_cycle_brush.execute(b, bpy.context)
    check(curves.active(bpy.context) is None and len(our_curves()) == 1, "B finishes the open curve")
    operators.VRSCULPT_OT_cycle_brush.execute(b, bpy.context)
    check(len(obj.data.splines[0].bezier_points) == 1, "with none open, B undoes (a point)")
    operators.undo_last_stroke(bpy.context)
    check(not our_curves(), "undoing the only point removes the curve")
    check(operators.undo_last_stroke(bpy.context) is None, "nothing left to undo is fine")

    click(ctx, ss, (0, 0, 1))
    click(ctx, ss, (0.2, 0, 1))
    operators._on_data_swap()
    check(curves.active(bpy.context) is None, "undo / file load lets go of the open curve")


def make_curve(ctx, ss):
    for p in [(0, 0, 1), (0.2, 0.1, 1), (0.4, 0, 1), (0.6, 0.1, 1)]:
        click(ctx, ss, p)
    obj = our_curves()[0]
    curves.end(bpy.context)
    return obj


def test_edit_points_and_handles():
    print("== Edit: move points and handles, corner / smooth")
    ss, ctx, s = setup()
    obj = make_curve(ctx, ss)
    s.draw_tool = 'EDIT'
    hold(ctx, ss, MAIN, [(0.2, 0.1, 1), (0.2, 0.1, 1.05), (0.2, 0.1, 1.1)])
    check(np.allclose(world_points(obj)[1], (0.2, 0.1, 1.1), atol=1e-4), "Edit moves the grabbed point")
    check(np.allclose(world_points(obj)[[0, 2, 3]], [(0, 0, 1), (0.4, 0, 1), (0.6, 0.1, 1)], atol=1e-5),
          "the other points stay")

    # A handle of an auto point: the pair becomes aligned, and stays in line.
    sp = obj.data.splines[0]
    co = Vector(world_points(obj)[2])
    h = Vector(world_points(obj, "handle_right")[2])
    keep = (Vector(world_points(obj, "handle_left")[2]) - co).length
    hold(ctx, ss, MAIN, [tuple(h), tuple(h + Vector((0, 0.03, 0.02))), tuple(h + Vector((0, 0.06, 0.04)))])
    pt = sp.bezier_points[2]
    hr = Vector(world_points(obj, "handle_right")[2])
    hl = Vector(world_points(obj, "handle_left")[2])
    check(pt.handle_left_type == 'ALIGNED' and pt.handle_right_type == 'ALIGNED',
          "grabbing an auto handle aligns the pair")
    check((hr - (h + Vector((0, 0.06, 0.04)))).length < 1e-4, "the handle follows the tip")
    check((hl - co).normalized().dot((hr - co).normalized()) < -0.9999 and abs((hl - co).length - keep) < 1e-4,
          "the other handle stays opposite, its length kept")
    check((Vector(world_points(obj)[2]) - co).length < 1e-6, "moving a handle doesn't move the point")

    hold(ctx, ss, MAIN, [tuple(co), tuple(co)])
    check(sp.bezier_points[2].handle_left_type == 'VECTOR', "a click on a point makes it a corner")
    hold(ctx, ss, MAIN, [tuple(co), tuple(co)])
    check(sp.bezier_points[2].handle_left_type == 'AUTO', "another click makes it smooth again")

    # Rotation: hold the point and roll the wrist (the pose's rotation changes between frames).
    for p in sp.bezier_points:
        p.handle_left_type = p.handle_right_type = 'ALIGNED'
    before = Vector(world_points(obj, "handle_right")[2]) - co
    turn = Quaternion((0, 0, 1), 0.5)
    hold(ctx, ss, MAIN, [tuple(co), tuple(co)], [Quaternion(), turn])
    after = Vector(world_points(obj, "handle_right")[2]) - Vector(world_points(obj)[2])
    check((after - turn @ before).length < 1e-4, "the handles of a held point turn with the wrist")


def test_insert_delete_erase():
    print("== Edit: insert and delete points; Erase")
    ss, ctx, s = setup()
    obj = make_curve(ctx, ss)
    shape = dense(obj, 2000)
    s.draw_tool = 'EDIT'
    seg = curves.nearest_segment(bpy.context, Vector((0.3, 0.05, 1)), 0.05)
    check(seg is not None and seg[0] == obj and seg[2] == 1, "the press finds the segment between points 2 and 3")
    p = Vector(dense(obj, 400)[np.argmin(np.linalg.norm(dense(obj, 400) - (0.3, 0.05, 1), axis=1))])
    hold(ctx, ss, MAIN, [tuple(p), tuple(p)])
    check(len(obj.data.splines[0].bezier_points) == 5, "a press on the curve between points inserts one")
    gap = max(np.linalg.norm(shape - q, axis=1).min() for q in dense(obj, 16))
    check(gap < 0.0002, f"inserting keeps the shape ({gap * 1000:.2f} mm off at most)")
    check(np.allclose(world_points(obj)[2], p, atol=0.003), "the new point is where the press was")

    hold(ctx, ss, MAIN, [tuple(p), tuple(p + Vector((0, 0, 0.03))), tuple(p + Vector((0, 0, 0.06)))])
    count = len(obj.data.splines[0].bezier_points)
    check(count == 5 and abs(world_points(obj)[2][2] - (p.z + 0.06)) < 1e-4,
          f"a press on the new point grabs it, nothing inserted ({count} points)")

    # Off hand deletes the point it presses.
    target = Vector(world_points(obj)[3])
    ss.aim_loc[OFF] = target
    hold(ctx, ss, OFF, [tuple(target), tuple(target)])
    check(len(obj.data.splines[0].bezier_points) == 4, "the off hand deletes a point")
    check(not np.any(np.linalg.norm(world_points(obj) - np.array(target[:]), axis=1) < 1e-6), "that one")

    # Someone else's curve: editable, not erased.
    cu = bpy.data.curves.new("Theirs", 'CURVE')
    cu.dimensions = '3D'
    theirs = bpy.data.objects.new("Theirs", cu)
    bpy.context.scene.collection.objects.link(theirs)
    sp2 = cu.splines.new('BEZIER')
    sp2.bezier_points.add(1)
    for pt, co in zip(sp2.bezier_points, [(0, 2, 1), (0.3, 2, 1)]):
        pt.handle_left_type = pt.handle_right_type = 'AUTO'
        pt.co = co
    bpy.context.view_layer.update()
    hold(ctx, ss, MAIN, [(0.3, 2, 1), (0.3, 2, 1.05)])
    check(abs(theirs.data.splines[0].bezier_points[1].co.z - 1.05) < 1e-4, "Edit works on any Bezier curve")

    s.draw_tool = 'ERASE'
    hold(ctx, ss, MAIN, [(0.0, -0.05, 1), (0.0, 0.05, 1)])
    hold(ctx, ss, MAIN, [(0.15, 1.95, 1), (0.15, 2.1, 1)])
    check(not our_curves(), "Erase removes our curve it sweeps through")
    check(bpy.data.objects.get("Theirs") is not None, "but not curves the add-on didn't make")


def test_panel_and_hints():
    print("== panel, hints")
    ss, ctx, s = setup()
    widgets, _ = menus.layout(bpy.context)
    ids = {w.id for w in widgets}
    check({"tool_CURVE", "color_RED", "draw_width"} <= ids and "style_TUBE" not in ids and "end_curve" not in ids,
          "Curve tool: color and width, no stroke styles")
    click(ctx, ss, (0, 0, 1))
    click(ctx, ss, (0.2, 0, 1))
    obj = curves.active(bpy.context)
    widgets, _ = menus.layout(bpy.context)
    vr_ui.find(widgets, "color_BLUE").on_press()
    check(obj.data.materials[0].name == "VR Stroke Blue", "a swatch recolors the open curve")
    menus._set_draw(bpy.context, s, "draw_width", 0.04)
    check(abs(obj.data.bevel_depth - 0.02) < 1e-6, "the width restyles it too")
    widgets, _ = menus.layout(bpy.context)
    check(vr_ui.find(widgets, "end_curve") is not None, "a Finish icon while a curve is open")
    vr_ui.find(widgets, "end_curve").on_press()
    check(curves.active(bpy.context) is None, "it finishes the curve")

    Info = controllers.Info
    hint = controllers.hints(s, Info(curve_open=True))
    check(hint['right']['top'][1] == "End curve", "B's hint with a curve open: End curve")
    check(controllers.hints(s, Info())['right']['top'][1] == "Undo stroke", "otherwise Undo stroke")
    check("point" in controllers.hints(s, Info())['right']['trigger'][1], "trigger hint: add point")
    s.draw_tool = 'EDIT'
    check("delete" in controllers.hints(s, Info())['left']['trigger'][1], "off-hand Edit hint: delete point")


def main():
    vr_sculpt.register()
    try:
        test_place_and_finish()
        test_close_undo_and_b()
        test_edit_points_and_handles()
        test_insert_delete_erase()
        test_panel_and_hints()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


main()
