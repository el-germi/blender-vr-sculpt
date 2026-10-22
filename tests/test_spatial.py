"""Background tests for the spatial UI: the tool dock, trays, tags, the palette, the selection's chips,
gizmo handles, brush ring pulls, pickers and touch haptics, all through the real trigger operator.

blender --background --factory-startup --python tests/test_spatial.py
"""

import math
import os
import sys
import time
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import (catalog, chips, dock, gizmos, hardsurface, menus, operators, primitives,  # noqa: E402
                       spatial, symmetry)
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


class Session(tm.FakeSessionState):
    """Looking along +Y from (0, -1, 1.5); the grips follow the aim poses (cards ride on the hands)."""

    def __init__(self):
        super().__init__()
        self.viewer_pose_location = Vector((0.0, -1.0, 1.5))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        self.haptics = 0
        self.away()

    def away(self):
        """Hands out in front, away from the dock and everything else."""
        self.aim_loc = [Vector((-0.35, 0.4, 1.3)), Vector((0.35, 0.4, 1.3))]
        self.aim_rot = [Quaternion(), Quaternion()]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + Vector((0.0, 0.03, -0.03))

    def haptic_action_apply(self, *args):
        self.haptics += 1
        return True


def setup():
    operators.session_running = lambda context: True
    ss = Session()
    wm = tm.FakeWM(ss)
    ctx = tm.FakeContext(wm)
    s = bpy.context.scene.vr_sculpt
    s.tip_offset = 0.0
    s.show_panel = True
    s.show_timeline = False
    s.model_add_as = 'OBJECT'
    s.grid_snap = s.snap_surface = s.snap_angle = False
    menus.set_mode(bpy.context, 'SCULPT')
    spatial.reset()
    return ss, ctx, s


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)


def slot(ss, id):
    return dict((t.id, p) for t, p in dock.slots(ss))[id]


def card(ctx, ss, id):
    spatial.invalidate()
    return spatial.card_by_id(ctx, ss, id)


def chip_point(c, id):
    """World point at the middle of a chip."""
    w = c.find(id)
    x0, y0, x1, y1 = w.rect
    return c.matrix @ Vector(((x0 + x1) / 2, (y0 + y1) / 2, 0.0))


def press_chip(ctx, ss, hand, card_id, chip_id):
    """Reach into a chip with a tip (keeping the card's own hand still) and pull the trigger."""
    c = card(ctx, ss, card_id)
    if c is None or c.find(chip_id) is None:
        return None, None
    return tm.press(ctx, ss, hand, [chip_point(c, chip_id)])


# --- the dock --------------------------------------------------------------------------------

def test_dock_place():
    print("== the dock: a belt at your waist that follows you lazily")
    ss, ctx, s = setup()
    origin, yaw, _scale = dock.frame(ss)
    check(close(origin, (0.0, -1.0, 1.5 - dock.DROP)) and abs(yaw) < 1e-6, "at waist height under the headset")
    places = dock.slots(ss)
    check(len(places) == 8 and [t.id for t, _p in places][-1] == "drawer", "seven tools and the drawer")
    check(all(p.y > origin.y + 0.15 for _t, p in places), "all in front of you")
    xs = [p.x for _t, p in places]
    check(xs == sorted(xs), "left to right as you face them")
    gaps = [(places[i + 1][1] - places[i][1]).length for i in range(len(places) - 1)]
    check(min(gaps) > 2 * dock.REACH, f"tokens far enough apart to take one at a time ({min(gaps):.3f} m)")

    ss.viewer_pose_location = Vector((0.15, -1.0, 1.5))
    check(close(dock.frame(ss)[0], origin), "a small head move doesn't move it")
    ss.viewer_pose_location = Vector((0.0, -1.0, 1.5))
    ss.aim_loc[1] = slot(ss, "model")
    ss.viewer_pose_location = Vector((1.0, -1.0, 1.5))
    check(close(dock.frame(ss, spatial.tips(ctx, ss))[0], origin), "nor a big one while you reach into it")
    ss.away()
    for _i in range(200):
        o, _y, _s = dock.frame(ss)
        if close(o, (1.0, -1.0, 1.5 - dock.DROP), 1e-3):
            break
        time.sleep(0.01)
    check(close(o, (1.0, -1.0, 1.5 - dock.DROP), 1e-3), "after a walk it glides after you")
    ss.viewer_pose_rotation = Quaternion((0, 0, 1), math.radians(90)) @ Quaternion((1, 0, 0), math.pi / 2)
    for _i in range(200):
        o, y, _s = dock.frame(ss)
        if abs(y - math.radians(90)) < 1e-3:
            break
        time.sleep(0.01)
    check(abs(y - math.radians(90)) < 1e-3, "and turns with you once you turn away")
    p = dict((t.id, p) for t, p in dock.slots(ss))["drawer"]
    check(p.x < o.x - 0.15, "facing -X now, the belt is on that side")


def test_take_tools():
    print("== taking tools from the dock")
    ss, ctx, s = setup()
    clear()
    n = len(bpy.data.objects)
    op, r = tm.press(ctx, ss, 1, [slot(ss, "model")])
    check(type(op.handler).__name__ == "TokenPick" and s.mode == 'MODEL' and r == {'FINISHED'},
          "the right tip in the Model token takes it (an undo step)")
    check(len(bpy.data.objects) == n, "nothing is created where the dock is")
    check(dock.held_token(s).id == "model", "the Model token is in your hand (its socket is empty)")
    op, r = tm.press(ctx, ss, 0, [slot(ss, "measure")])
    check(s.mode == 'TOOLS' and s.tool == 'MEASURE', "the left hand can take tools too: the tape measure")
    tm.press(ctx, ss, 1, [slot(ss, "camera")])
    check(s.mode == 'TOOLS' and s.tool == 'CAMERA', "the camera swaps it")
    tm.press(ctx, ss, 1, [slot(ss, "camera")])
    check(s.mode == 'HAND' and dock.held_token(s) is None, "touching the held tool's socket puts it back")
    cube = primitives.create(bpy.context, 'CUBE', Vector((0.0, 0.5, 1.2)))
    primitives.make_active(bpy.context, None)
    bpy.context.view_layer.update()
    op, r = tm.press(ctx, ss, 1, [(0.0, 0.5, 1.2), (0.05, 0.5, 1.2), (0.1, 0.5, 1.2)])
    check(type(op.handler).__name__ == "MoveObject" and close(cube.location, (0.1, 0.5, 1.2)),
          "an empty hand grabs and moves objects")
    op, r = tm.press(ctx, ss, 1, [(0.6, 0.5, 1.2), (0.7, 0.5, 1.3)])
    check(len(bpy.data.objects) == 1, "and makes nothing in empty space")

    spatial.reset()
    tm.press(ctx, ss, 1, [slot(ss, "drawer")])
    check(spatial.drawer_open() and card(ctx, ss, "drawer") is not None, "the last slot opens the drawer")
    op, r = press_chip(ctx, ss, 1, "drawer", "nav_fly")
    check(op is not None and s.nav_mode == 'FLY', "a drawer chip, pressed by touch")
    s.nav_mode = 'WALK'
    tm.press(ctx, ss, 1, [slot(ss, "sculpt")])
    check(s.mode == 'SCULPT' and not spatial.drawer_open(), "taking a tool closes the drawer")
    primitives.delete(bpy.context, cube)


def test_trays():
    print("== a token's variants fan out above it")
    ss, ctx, s = setup()
    s.mode, s.engine, s.brush = 'SCULPT', 'PYTHON', 'DRAW'
    check(card(ctx, ss, "tray") is None, "no tray with the hands away")
    ss.aim_loc[1] = slot(ss, "sculpt")
    tray = card(ctx, ss, "tray")
    check(tray is not None and tray.find("brush_SMOOTH") is not None and tray.alpha > 0.9,
          "touching the Sculpt token shows its brushes")
    check(tray.matrix.translation.z > slot(ss, "sculpt").z + 0.02, "above the token")
    menus.set_mode(bpy.context, 'MODEL')
    op, r = press_chip(ctx, ss, 1, "tray", "brush_SMOOTH")
    check(type(op.handler).__name__ == "ChipPress" and s.mode == 'SCULPT' and s.brush == 'SMOOTH',
          "a brush on the tray takes the tool with that brush")
    ss.aim_loc[1] = slot(ss, "draw")
    op, r = press_chip(ctx, ss, 1, "tray", "style_RIBBON")
    check(s.mode == 'DRAW' and s.draw_style == 'RIBBON', "Draw's tray: Ribbon")
    ss.aim_loc[1] = slot(ss, "model")
    op, r = press_chip(ctx, ss, 1, "tray", "mark_hard")
    check(s.mode == 'MODEL' and s.model_mark == 'HARD', "Model's tray: the hard edge marker")
    op, r = press_chip(ctx, ss, 1, "tray", "shape_cylinder")
    check(s.model_mark == 'NONE' and s.primitive == "mesh.primitive_cylinder_add", "a shape puts the marker down")
    asked = chip_point(card(ctx, ss, "tray"), "add")
    op, r = press_chip(ctx, ss, 1, "tray", "add")
    picker = card(ctx, ss, "picker")
    check(menus.picker_open() and picker is not None and card(ctx, ss, "tray") is None,
          "All shapes opens a picker card (on its own)")
    check((picker.center() - asked).length < 0.06, "where it was asked for")
    press_chip(ctx, ss, 1, "picker", "cat_Mesh")
    key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_monkey_add")
    op, r = press_chip(ctx, ss, 1, "picker", "opt_" + key)
    check(s.primitive == key and not menus.picker_open(), "choosing Monkey sets it and closes the picker")
    s.primitive = primitives.DEFAULT_ADD
    menus.set_mode(bpy.context, 'SCULPT')


def test_tag_and_palette():
    print("== the tool's tag (right hand) and the palette (left hand)")
    ss, ctx, s = setup()
    s.mode, s.engine, s.strength = 'SCULPT', 'PYTHON', 0.5
    tag = card(ctx, ss, "tag")
    check(tag is not None and tag.owner == 1 and tag.alpha < 0.05, "the tag's chips hide while the left hand is away")
    ss.aim_loc[0] = spatial.tag_anchor(ctx, ss) + Vector((0.0, -0.02, 0.07))
    tag = card(ctx, ss, "tag")
    check(tag.alpha > 0.9 and tag.matrix.translation.z > spatial.tag_anchor(ctx, ss).z, "they come up as it nears, "
          "above the tool's name")
    s_chip = tag.find("strength")
    y = (s_chip.rect[1] + s_chip.rect[3]) / 2
    left = tag.matrix @ Vector((s_chip.rect[0] + 0.006, y, 0.0))
    right = tag.matrix @ Vector((s_chip.rect[2] - 0.006, y, 0.0))
    op, r = tm.press(ctx, ss, 0, [left, left.lerp(right, 0.5), right])
    check(type(op.handler).__name__ == "ChipPress" and s.strength > 0.95 and r == {'FINISHED'},
          f"the left tip drags the strength slider on the tag ({s.strength:.2f})")
    ss.aim_loc[1] = left  # The right hand can't reach its own tag.
    op, r = tm.press(ctx, ss, 1, [left])
    check(op.handler is None or type(op.handler).__name__ != "ChipPress", "the right tip can't press its own tag")
    ss.away()
    op, r = press_chip(ctx, ss, 0, "tag", "sym_X")
    check(s.symmetry_x, "Symmetry X on the tag")
    s.symmetry_x = False

    menus.set_mode(bpy.context, 'DRAW')
    ss.away()
    pal = card(ctx, ss, "palette")
    check(pal is not None and pal.owner == 0, "Draw mode: a palette rides on the left hand")
    op, r = press_chip(ctx, ss, 1, "palette", "color_BLUE")
    check(s.draw_color == 'BLUE', "the right tip dips into a color")
    menus.set_mode(bpy.context, 'SCULPT')


def test_object_chips():
    print("== chips beside the selected object")
    ss, ctx, s = setup()
    clear()
    menus.set_mode(bpy.context, 'MODEL')
    cube = primitives.create(bpy.context, 'CUBE', Vector((0.0, 0.3, 1.2)))
    cube.vr_prim.size = (0.1, 0.1, 0.1)
    bpy.context.view_layer.update()
    c = card(ctx, ss, "object")
    check(c is not None and c.alpha < 0.05, "they hide while no hand is near the object")
    ss.aim_loc[1] = Vector((0.0, 0.3, 1.32))  # A tip over the cube.
    c = card(ctx, ss, "object")
    check(c.alpha > 0.9 and c.find("shade_auto") is not None, "a tip near it brings them up")
    check(c.center().x < cube.location.x - 0.05, "on your left of the object (your right hand's tag is on the right)")
    op, r = press_chip(ctx, ss, 1, "object", "shade_flat")
    from vr_sculpt import shading
    check(type(op.handler).__name__ == "ChipPress" and shading.mode(cube) == 'FLAT', "pressing Flat by touch")
    far = primitives.create(bpy.context, 'CUBE', Vector((0.0, 4.0, 1.2)))
    anchor = spatial.object_anchor(far, ss.viewer_pose_location, 1.0)
    check((anchor - ss.viewer_pose_location).length <= spatial.REACH_MAX + 1e-6,
          "a far object's chips come in to arm's reach")
    s.mode = 'SCULPT'
    check(card(ctx, ss, "object") is None, "only in Model mode")
    clear()


# --- gizmos ----------------------------------------------------------------------------------

def handle(ctx, ss, obj, id):
    return next((h for h in gizmos.handles(bpy.context, obj, ss.viewer_pose_location, 1.0) if h.id == id), None)


def drag(ctx, ss, start, delta, steps=4, hand=1):
    start = Vector(start)
    return tm.press(ctx, ss, hand, [start + Vector(delta) * (i / steps) for i in range(steps + 1)])


def test_face_handles():
    print("== face handles resize from one side")
    ss, ctx, s = setup()
    clear()
    menus.set_mode(bpy.context, 'MODEL')
    cube = primitives.create(bpy.context, 'CUBE', Vector((0.0, 0.4, 1.2)))
    cube.vr_prim.size = (0.2, 0.2, 0.2)
    bpy.context.view_layer.update()
    h = handle(ctx, ss, cube, "face_X+")
    check(h is not None and close(h.position, (0.1, 0.4, 1.2)), "a handle in the middle of each face")
    op, r = drag(ctx, ss, h.position, (0.05, 0.0, 0.0))
    check(type(op.handler).__name__ == "_Gizmo" and type(op.handler.drag).__name__ == "FaceDrag",
          "the trigger on it grabs the handle (not the object)")
    check(close(cube.vr_prim.size, (0.25, 0.2, 0.2)) and abs(cube.location.x - 0.025) < 1e-5,
          f"pulling +X out 5 cm: 25 cm wide, the -X face stays put ({tuple(round(v, 3) for v in cube.vr_prim.size)})")
    check(r == {'FINISHED'}, "one undo step")
    h = handle(ctx, ss, cube, "face_Z-")
    drag(ctx, ss, h.position, (0.0, 0.0, 0.05))
    check(abs(cube.vr_prim.size[2] - 0.15) < 1e-5 and abs(cube.location.z - 1.225) < 1e-5,
          "pushing the bottom up: shorter, the top stays")
    cube.rotation_euler = (0.0, 0.0, math.radians(90))
    bpy.context.view_layer.update()
    h = handle(ctx, ss, cube, "face_X+")
    check(close(h.direction, (0.0, 1.0, 0.0)), "a turned object's handles turn with it")
    drag(ctx, ss, h.position, (0.0, 0.05, 0.0))
    check(abs(cube.vr_prim.size[0] - 0.3) < 1e-5, "and pull along its own axis")

    cyl = primitives.create(bpy.context, 'CYLINDER', Vector((0.3, 0.6, 1.2)))
    cyl.vr_prim.radius, cyl.vr_prim.height = 0.05, 0.1
    bpy.context.view_layer.update()
    h = handle(ctx, ss, cyl, "face_X+")
    drag(ctx, ss, h.position, (0.02, 0.0, 0.0))
    check(abs(cyl.vr_prim.radius - 0.06) < 1e-5 and abs(cyl.location.x - 0.31) < 1e-5,
          f"a cylinder's side handle sets its radius ({cyl.vr_prim.radius:.4f}), the far side stays")

    key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_uv_sphere_add")
    ball = primitives.create_from_entry(bpy.context, key, Vector((-0.3, 0.6, 1.2)))
    ball.scale = (0.05, 0.05, 0.05)  # radius 5 cm
    bpy.context.view_layer.update()
    h = handle(ctx, ss, ball, "face_Z+")
    check(h is not None and abs(h.position.z - 1.25) < 1e-4, "operator primitives get handles too")
    drag(ctx, ss, h.position, (0.0, 0.0, 0.05))
    lo, hi = gizmos.local_box(ball)
    bottom = (ball.matrix_world @ lo).z
    top = (ball.matrix_world @ hi).z
    check(abs(top - 1.3) < 1e-4 and abs(bottom - 1.15) < 1e-4, f"they stretch it from that side ({bottom:.3f}..{top:.3f})")

    s.model_add_as = 'DIFFERENCE'
    primitives.make_active(bpy.context, cube)
    check(handle(ctx, ss, cube, "face_X+") is None, "making cutters, the target's faces are free to press on")
    s.model_add_as = 'OBJECT'
    s.model_mark = 'HARD'
    check(gizmos.target(bpy.context) is None, "and the edge markers hide the handles")
    s.model_mark = 'NONE'
    s.show_panel = False
    check(gizmos.target(bpy.context) is None, "hiding the UI hides them")
    s.show_panel = True
    ss.away()
    check(gizmos.handle_at(bpy.context, ss, Vector((5.0, 5.0, 5.0))) is None, "handles only work near the object")
    clear()


def test_bevel_handle():
    print("== the bevel bead")
    ss, ctx, s = setup()
    clear()
    menus.set_mode(bpy.context, 'MODEL')
    cube = primitives.create(bpy.context, 'CUBE', Vector((0.0, 0.4, 1.2)))
    cube.vr_prim.size = (0.2, 0.2, 0.2)
    bpy.context.view_layer.update()
    h = handle(ctx, ss, cube, "bevel")
    check(h is not None and close(h.position, (0.0, 0.3, 1.3)), "on the top edge nearest you")
    op, r = drag(ctx, ss, h.position, h.direction * 0.02 * math.sqrt(2.0))
    check(hardsurface.has_bevel(cube) and abs(hardsurface.bevel_width(cube) - 0.02) < 1e-4,
          f"pulled in along the corner: a 2 cm bevel ({hardsurface.bevel_width(cube):.4f})")
    h = handle(ctx, ss, cube, "bevel")
    check(close(h.position, Vector((0.0, 0.3, 1.3)) + h.direction * 0.02 * math.sqrt(2.0)),
          "the bead sits where the bevel ends")
    drag(ctx, ss, h.position, -h.direction * 0.05)
    check(not hardsurface.has_bevel(cube), "back out past the edge: no bevel")
    clear()


def test_array_handles():
    print("== array arrows, the gap bead and the radial ring")
    ss, ctx, s = setup()
    clear()
    menus.set_mode(bpy.context, 'MODEL')
    cube = primitives.create(bpy.context, 'CUBE', Vector((0.0, 0.4, 1.2)))
    cube.vr_prim.size = (0.2, 0.2, 0.2)
    bpy.context.view_layer.update()
    h = handle(ctx, ss, cube, "array_X")
    check(h is not None and close(h.position, (0.1 + gizmos.ARROW_OUT, 0.4, 1.2)), "an arrow out of each side")
    step = 0.2 * (1.0 + hardsurface.ARRAY_GAP)
    op, r = drag(ctx, ss, h.position, (2 * step, 0.0, 0.0), steps=8)
    check(type(op.handler.drag).__name__ == "ArrayDrag", "the trigger on it drags the arrow")
    check(hardsurface.array_count(cube) == 3 and hardsurface.array_axis(cube) == 0,
          f"two steps out: 3 copies along X ({hardsurface.array_count(cube)})")
    check(ss.haptics >= 2, "you feel each copy arrive")
    h = handle(ctx, ss, cube, "array_X")
    check(abs(h.position.x - (0.1 + 2 * step + gizmos.ARROW_OUT)) < 1e-4, "the arrow waits past the last copy")
    drag(ctx, ss, h.position, (-step, 0.0, 0.0))
    check(hardsurface.array_count(cube) == 2, "back one step: 2 copies")
    g = handle(ctx, ss, cube, "array_gap")
    check(g is not None and abs(g.position.x - step) < 1e-4, "a bead on the first copy")
    drag(ctx, ss, g.position, (0.05, 0.0, 0.0))
    check(abs(hardsurface.array_gap(cube) - 0.5) < 1e-3, f"pull it 5 cm: the gap follows ({hardsurface.array_gap(cube):.3f})")
    h = handle(ctx, ss, cube, "array_Z")
    drag(ctx, ss, h.position, (0.0, 0.0, 0.3 * 2 + 0.01), steps=8)
    check(hardsurface.array_axis(cube) == 2 and hardsurface.array_count(cube) == 3,
          f"another side's arrow takes the array there ({hardsurface.array_count(cube)} along Z)")

    hardsurface.set_array_mode(bpy.context, cube, 'RADIAL')
    hardsurface.set_array_count(bpy.context, cube, 3)
    hardsurface.set_array_center(bpy.context, cube, Vector((0.0, 0.8, 1.2)))
    bpy.context.view_layer.update()
    check(handle(ctx, ss, cube, "array_X") is None, "a radial array has a ring instead of arrows")
    b = handle(ctx, ss, cube, "radial_count")
    c = Vector((0.0, 0.8, 1.2))
    check(b is not None and abs((b.position - c).length - 0.4) < 1e-4, "its bead is on the ring, at the next copy")
    sixty = c + Quaternion((0, 0, 1), math.radians(60)) @ (cube.location - c)
    op, r = tm.press(ctx, ss, 1, [b.position, b.position.lerp(sixty, 0.5), sixty])
    check(hardsurface.array_count(cube) == 6, f"drag it to 60 degrees: 6 copies ({hardsurface.array_count(cube)})")
    ch = handle(ctx, ss, cube, "radial_center")
    drag(ctx, ss, ch.position, (0.1, 0.0, 0.0))
    check(close(gizmos.radial_center(cube), (0.1, 0.8, 1.2)), "the center cross moves the ring's center")

    hardsurface.set_array_count(bpy.context, cube, 1)
    symmetry.model_toggle(bpy.context, cube, 0)
    m = handle(ctx, ss, cube, "mirror_center")
    check(m is not None, "a mirrored object shows its mirror center")
    start = m.position.copy()
    drag(ctx, ss, m.position, (0.0, 0.0, 0.1))
    check(close(bpy.data.objects[symmetry.CENTER].location, start + Vector((0, 0, 0.1))), "and you can move it")
    clear()


def test_ring_pull():
    print("== pulling the brush ring")
    ss, ctx, s = setup()
    menus.set_mode(bpy.context, 'PAINT')
    s.paint_radius = 0.02
    ss.aim_loc[1] = Vector((0.4, 0.4, 1.3))
    op, r = tm.press(ctx, ss, 0, [(0.42, 0.4, 1.3), (0.45, 0.4, 1.3), (0.48, 0.4, 1.3)])
    check(type(op.handler).__name__ == "RingPull" and abs(s.paint_radius - 0.08) < 1e-4,
          f"Paint: the paint radius follows ({s.paint_radius:.3f})")
    menus.set_mode(bpy.context, 'DRAW')
    s.draw_tool, s.draw_width = 'DRAW', 0.008
    op, r = tm.press(ctx, ss, 0, [(0.41, 0.4, 1.3), (0.42, 0.4, 1.3)])
    check(type(op.handler).__name__ == "RingPull" and abs(s.draw_width - 0.04) < 1e-4,
          f"Draw: the ring is the stroke width ({s.draw_width:.3f})")
    menus.set_mode(bpy.context, 'POSE')
    op, r = tm.press(ctx, ss, 0, [(0.42, 0.4, 1.3)])
    check(type(op.handler).__name__ != "RingPull", "Pose has no ring to pull")
    menus.set_mode(bpy.context, 'SCULPT')


def test_haptics_and_hiding():
    print("== touch haptics, hiding the UI")
    ss, ctx, s = setup()
    spatial.tick(bpy.context, ss)
    n = ss.haptics
    ss.aim_loc[1] = slot(ss, "draw")
    spatial.tick(ctx, ss)
    check(ss.haptics == n + 1, "entering a token buzzes")
    spatial.tick(ctx, ss)
    check(ss.haptics == n + 1, "once")
    ss.away()
    spatial.tick(ctx, ss)
    check(ss.haptics == n + 1, "leaving doesn't")
    s.show_panel = False
    spatial.invalidate()
    check(spatial.cards(ctx, ss) == [], "left stick click hides every card")
    op, r = tm.press(ctx, ss, 1, [slot(ss, "model")])
    check(s.mode == 'SCULPT', "and the dock with them")
    s.show_panel = True


def main():
    vr_sculpt.register()
    try:
        catalog.entries(refresh=True)
        for t in (test_dock_place, test_take_tools, test_trays, test_tag_and_palette, test_object_chips,
                  test_face_handles, test_bevel_handle, test_array_handles, test_ring_pull, test_haptics_and_hiding):
            try:
                t()
            except Exception:
                traceback.print_exc()
                failures.append(t.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
