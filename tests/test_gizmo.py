"""Background tests for the transform gizmo (gizmo.py): which object it's on and which handles it has, finding
the handle at a tip, moving along / turning around / sizing along one axis and sizing evenly, World and Local
axes, snapping, the trigger taking a handle before anything else, the legend, and the panel row.

blender --background --factory-startup --python tests/test_gizmo.py
"""

import math
import os
import shutil
import sys
import tempfile
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import actions, controllers, gizmo, lookdev, menus, operators, primitives, refs  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def widgets():
    return {w.id: w for w in menus.layout(bpy.context)[0]}


U = gizmo.SIZE  # the gizmo's unit at viewer scale 1


def fresh(ctx):
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    s = ctx.scene.vr_sculpt
    s.grid_snap = s.snap_angle = s.gizmo_local = False
    s.gizmo_move = s.gizmo_rotate = s.gizmo_scale = True
    s.uv_tool, s.model_mark = 'NONE', 'NONE'
    s.grab_delay = s.grab_deadzone = 0.0  # Exact amounts: the drag gate's dead zone is tested on its own.
    menus.set_mode(ctx, 'MODEL')
    mesh = bpy.data.meshes.new("Box")
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.2)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("Box", mesh)
    ctx.scene.collection.objects.link(obj)
    obj.location = (0.5, 0.0, 1.0)
    ctx.view_layer.update()
    primitives.make_active(ctx, obj)
    return obj


def at(p):
    return Matrix.Translation(Vector(p))


def drag(ctx, obj, start, end, steps=4):
    """A gizmo drag from the handle at start to end, as the trigger does it; the handler."""
    h = gizmo.pick(ctx, actions.HANDS[1], at(start), 1.0)
    if h is None:
        return None
    start, end = Vector(start), Vector(end)
    for i in range(1, steps + 1):
        h.update(ctx, None, at(start.lerp(end, i / steps)), 1.0)
    h.finish(ctx)
    return h


def test_target_and_handles(ctx):
    obj = fresh(ctx)
    s = ctx.scene.vr_sculpt
    check(gizmo.target(ctx) == obj, "Model: the selected object has the gizmo")
    c = obj.matrix_world.translation
    f = gizmo.frame_for(ctx, obj, 1.0)
    tip = c + Vector((1.1 * U, 0.0, 0.0))
    check(f.handle_at(tip) == ('MOVE', 0), "a tip on the X arrow is on the X move handle")
    check(f.handle_at(c + Vector((0.0, 0.0, 1.2 * U))) == ('MOVE', 2), "on the Z arrow: Z move")
    on_ring = c + Vector((0.0, math.cos(0.8), math.sin(0.8))) * gizmo.RING * U
    check(f.handle_at(on_ring) == ('TURN', 0), "on the ring in the YZ plane: turn around X")
    check(f.handle_at(c + Vector((0.0, gizmo.CUBE * U, 0.0))) == ('SIZE', 1), "on the Y cube: size along Y")
    outer = c + (Vector((1.0, 0.0, 1.0)).normalized()) * gizmo.OUTER * U  # In the plane facing -Y (no viewer).
    check(f.handle_at(outer) == ('SIZE_ALL', None), "on the outer ring: size evenly")
    check(f.handle_at(c + Vector((0.3 * U, 0.3 * U, 0.3 * U))) is None, "between handles: nothing")
    check(f.handle_at(c + Vector((3 * U, 0.0, 0.0))) is None, "far off: nothing")

    s.gizmo_move = False
    check(gizmo.frame_for(ctx, obj, 1.0).handle_at(tip) is None, "Move off: no arrows")
    s.gizmo_move = s.gizmo_rotate = s.gizmo_scale = False
    check(gizmo.target(ctx) is None, "every handle off: no gizmo")
    s.gizmo_move = s.gizmo_rotate = s.gizmo_scale = True
    s.model_mark = 'HARD'
    check(gizmo.target(ctx) is None, "marking edges takes the trigger: no gizmo")
    s.model_mark = 'NONE'
    f2 = gizmo.frame_for(ctx, obj, 2.0)
    check(f2.handle_at(c + Vector((2.2 * U, 0.0, 0.0))) == ('MOVE', 0), "at viewer scale 2 the gizmo is twice as big")
    primitives.make_active(ctx, None)
    check(gizmo.target(ctx) is None, "nothing selected: no gizmo")


def test_move(ctx):
    obj = fresh(ctx)
    s = ctx.scene.vr_sculpt
    c = obj.matrix_world.translation.copy()
    grab = c + Vector((1.1 * U, 0.0, 0.0))
    h = drag(ctx, obj, grab, grab + Vector((0.12, 0.05, -0.03)))
    check(isinstance(h, gizmo.Drag) and h.changed, "dragging the X arrow is a gizmo drag that changes it")
    check(close(obj.location, c + Vector((0.12, 0.0, 0.0))), f"it moved along X only: {tuple(obj.location)}")
    check(h.readout() == "X +12.0 cm", f"readout: {h.readout()!r}")
    check(gizmo.drag is None, "let go: nothing held")

    s.grid_snap, s.grid_size = True, 0.05
    obj.location = (0.52, 0.0, 1.0)
    ctx.view_layer.update()
    c = obj.matrix_world.translation.copy()
    grab = c + Vector((1.1 * U, 0.0, 0.0))
    drag(ctx, obj, grab, grab + Vector((0.07, 0.0, 0.0)))
    check(close(obj.location, (0.6, 0.0, 1.0)), f"Snap to Grid, World: lands on the grid ({obj.location.x:.3f})")
    s.grid_snap = False

    # Local: the object turned 90 degrees around Z, its X arrow points along world Y.
    obj.rotation_euler = (0.0, 0.0, math.pi / 2)
    s.gizmo_local = True
    ctx.view_layer.update()
    c = obj.matrix_world.translation.copy()
    grab = c + Vector((0.0, 1.1 * U, 0.0))
    check(gizmo.frame_for(ctx, obj, 1.0).handle_at(grab) == ('MOVE', 0), "Local: the X arrow is on its own X")
    h = drag(ctx, obj, grab, grab + Vector((0.03, 0.1, 0.0)))
    check(close(obj.location, c + Vector((0.0, 0.1, 0.0))), f"Local: moves along its own X ({tuple(obj.location)})")
    check(h.readout().startswith("own X +"), f"Local readout: {h.readout()!r}")
    check(gizmo.describe(('MOVE', 0), True) == "Move along own X", "described as its own axis")


def test_turn(ctx):
    obj = fresh(ctx)
    s = ctx.scene.vr_sculpt
    c = obj.matrix_world.translation.copy()
    r = gizmo.RING * U
    start = c + Vector((r, 0.0, 0.0))
    check(gizmo.frame_for(ctx, obj, 1.0).handle_at(start + Vector((0.0, 0.0, 0.001))) in {('TURN', 2), ('TURN', 1)},
          "the X point of a ring is on a ring")
    # Around Z: along the ring in the XY plane, a quarter turn (several steps, so the angle unwraps).
    h = gizmo.Drag(ctx, obj, gizmo.frame_for(ctx, obj, 1.0), ('TURN', 2), at(start), 1)
    for i in range(1, 9):
        a = math.pi / 2 * i / 8
        h.update(ctx, None, at(c + Vector((math.cos(a), math.sin(a), 0.0)) * r), 1.0)
    h.finish(ctx)
    check(abs(obj.rotation_euler.z - math.pi / 2) < 1e-3 and abs(obj.rotation_euler.x) < 1e-4,
          f"a quarter turn around Z ({math.degrees(obj.rotation_euler.z):.1f})")
    check(close(obj.location, c), "turning around its origin keeps it in place")
    check(h.readout() == "Z +90\u00b0", f"readout: {h.readout()!r}")

    # Past half a turn, and 15 degree steps.
    obj.rotation_euler = (0.0, 0.0, 0.0)
    ctx.view_layer.update()
    s.snap_angle = True
    h = gizmo.Drag(ctx, obj, gizmo.frame_for(ctx, obj, 1.0), ('TURN', 2), at(start), 1)
    for i in range(1, 25):
        a = math.radians(200) * i / 24
        h.update(ctx, None, at(c + Vector((math.cos(a), math.sin(a), 0.0)) * r), 1.0)
    h.update(ctx, None, at(c + Vector((math.cos(math.radians(203)), math.sin(math.radians(203)), 0.0)) * r), 1.0)
    h.finish(ctx)
    check(abs(math.degrees(h.amount) - 210.0) < 1e-3, f"15 degree steps, past half a turn ({math.degrees(h.amount):.1f})")
    s.snap_angle = False

    # A center the turn goes around that isn't the world's origin, and the tip on the axis holds.
    obj.rotation_euler = (0.0, 0.0, 0.0)
    ctx.view_layer.update()
    h = gizmo.Drag(ctx, obj, gizmo.frame_for(ctx, obj, 1.0), ('TURN', 0), at(c + Vector((0.0, r, 0.0))), 1)
    h.update(ctx, None, at(c + Vector((0.0, 0.0, r))), 1.0)
    before = obj.matrix_world.copy()
    h.update(ctx, None, at(c + Vector((0.01, 0.0, 0.0))), 1.0)
    check(obj.matrix_world == before, "the tip on the axis: it holds")
    h.finish(ctx)
    check(abs(obj.rotation_euler.x - math.pi / 2) < 1e-3, "around X: a quarter turn")


def test_size(ctx):
    obj = fresh(ctx)
    s = ctx.scene.vr_sculpt
    c = obj.matrix_world.translation.copy()
    cube = c + Vector((gizmo.CUBE * U, 0.0, 0.0))
    h = drag(ctx, obj, cube, c + Vector((2 * gizmo.CUBE * U, 0.02, 0.0)))
    check(close(obj.scale, (2.0, 1.0, 1.0)), f"the X cube pulled twice as far: twice as long ({tuple(obj.scale)})")
    check(close(obj.location, c), "sizing keeps the origin")
    check(h.readout() == "own X \u00d72.00 (40.0 cm)", f"readout: {h.readout()!r}")

    s.grid_snap = True
    obj.scale = (1.0, 1.0, 1.0)
    ctx.view_layer.update()
    drag(ctx, obj, cube, c + Vector((1.57 * gizmo.CUBE * U, 0.0, 0.0)))
    check(close(obj.scale, (1.6, 1.0, 1.0)), f"Snap to Grid: in tenths ({obj.scale.x:.3f})")
    s.grid_snap = False

    # Turned objects size along their own axes, whatever the arrows show.
    obj.scale = (1.0, 1.0, 1.0)
    obj.rotation_euler = (0.0, 0.0, math.pi / 4)
    ctx.view_layer.update()
    d = Vector((1.0, 1.0, 0.0)).normalized()
    f = gizmo.frame_for(ctx, obj, 1.0)
    check(f.handle_at(c + d * gizmo.CUBE * U) == ('SIZE', 0), "World axes: the size cube is still on its own X")
    drag(ctx, obj, c + d * gizmo.CUBE * U, c + d * 1.5 * gizmo.CUBE * U)
    check(close(obj.scale, (1.5, 1.0, 1.0)) and abs(obj.rotation_euler.z - math.pi / 4) < 1e-4,
          "it stretches along its own X, still turned")

    # Evenly, with the outer ring.
    obj.scale = (1.0, 1.0, 1.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    ctx.view_layer.update()
    outer = c + Vector((0.0, 0.0, gizmo.OUTER * U))
    h = drag(ctx, obj, outer, c + Vector((0.0, 0.0, 0.75 * gizmo.OUTER * U)))
    check(close(obj.scale, (0.75, 0.75, 0.75)), f"the outer ring pulled in: smaller evenly ({tuple(obj.scale)})")
    check(h.readout() == "\u00d70.75", f"readout: {h.readout()!r}")
    h = drag(ctx, obj, outer, c)
    check(h is not None and min(obj.scale) > 0.0, "pulled into the middle: tiny, never zero or flipped")


def test_trigger(ctx):
    obj = fresh(ctx)
    s = ctx.scene.vr_sculpt
    c = obj.matrix_world.translation.copy()
    tip = at(c + Vector((1.1 * U, 0.0, 0.0)))
    for hand, name in ((actions.HANDS[1], "main"), (actions.HANDS[0], "off")):
        h = operators._pick_handler(ctx, s, hand, tip, tip, 1.0, 1.0)
        check(isinstance(h, gizmo.Drag), f"Model, {name} hand on a handle: the gizmo takes it")
        h.abandon()
    h = operators._pick_handler(ctx, s, actions.HANDS[1], at(c + Vector((0.4, 0.4, 0.0))),
                                at(c + Vector((0.4, 0.4, 0.0))), 1.0, 1.0)
    check(not isinstance(h, gizmo.Drag), "away from the handles the main hand creates as before")
    if h is not None and getattr(h, "obj", None) is not None:
        h.finish(ctx)

    # A press without moving changes nothing (the drag gate), so no undo step.
    primitives.make_active(ctx, obj)
    s.grab_deadzone, s.grab_delay = 0.01, 10.0
    before = obj.matrix_world.copy()
    h = gizmo.pick(ctx, actions.HANDS[1], tip, 1.0)
    h.update(ctx, None, at(tip.translation + Vector((0.003, 0.0, 0.0))), 1.0)
    h.finish(ctx)
    check(obj.matrix_world == before and not h.changed, "a press that barely moves doesn't nudge it")
    s.grab_delay = s.grab_deadzone = 0.0

    # Undo while held: the caches go.
    h = gizmo.pick(ctx, actions.HANDS[1], tip, 1.0)
    check(gizmo.drag is h, "held: it's the one drawn")
    check(gizmo.status(ctx) is None, "not moved yet: the tip status shows the tool")
    h.update(ctx, None, at(tip.translation + Vector((0.05, 0.0, 0.0))), 1.0)
    content, size = gizmo.status(ctx)
    check(content[0] == "Move along X" and content[3] == "gizmo_move" and size == "+5.0 cm",
          f"moving with the main hand: the tip status says what and how far ({content[0]!r}, {size!r})")
    operators._on_data_swap()
    check(gizmo.drag is None, "undo / redo: forgotten")

    # Lights have no size handles; references have all of them.
    s.mode = 'TOOLS'
    s.tool = 'LIGHT'
    light = lookdev.new_light(ctx, 'POINT', Matrix.Translation((0.0, 1.0, 1.5)))
    check(gizmo.target(ctx) == light, "Tools > Light: the selected light has the gizmo")
    check(gizmo.kinds(s, light) == {'MOVE', 'TURN'}, "a light has move and turn handles only")
    primitives.make_active(ctx, obj)
    check(gizmo.target(ctx) is None, "Tools > Light with a mesh selected: no gizmo")
    s.tool = 'REFS'
    folder = tempfile.mkdtemp(prefix="vrs_gizmo_")
    try:
        img = bpy.data.images.new("Pic", 40, 20)
        img.filepath_raw = os.path.join(folder, "Pic.png")
        img.file_format = 'PNG'
        img.save()
        ref = refs.new_ref(ctx, img.filepath_raw, refs.facing((0.0, 1.0, 1.4), None), 0.4)
        check(gizmo.target(ctx) == ref and gizmo.kinds(s, ref) == {'MOVE', 'TURN', 'SIZE', 'SIZE_ALL'},
              "Tools > Refs: the selected picture has every handle")
        c = ref.matrix_world.translation.copy()
        own_x = ref.matrix_world.to_3x3().col[0].normalized()
        h = drag(ctx, ref, c + own_x * gizmo.CUBE * U, c + own_x * 2 * gizmo.CUBE * U)
        check(close(ref.scale, (2.0, 1.0, 1.0)), "a picture stretched along its width")
        check(h.readout() == "own X \u00d72.00 (80.0 cm)", f"its readout counts the picture's width: {h.readout()!r}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    menus.set_mode(ctx, 'MODEL')


def test_hints_and_panel(ctx):
    obj = fresh(ctx)
    s = ctx.scene.vr_sculpt
    c = obj.matrix_world.translation
    check(gizmo.hint(ctx, c + Vector((1.1 * U, 0.0, 0.0)), 1.0) == ("gizmo_move", "Move along X"),
          "the legend names the handle under a tip")
    check(gizmo.hint(ctx, c + Vector((0.0, 0.0, gizmo.CUBE * U)), 1.0) == ("gizmo_size", "Size along own Z"),
          "size handles are on its own axes")
    info = controllers.Info(gizmo_main=("gizmo_turn", "Turn around Y"))
    check(controllers.hints(s, info)['right']['trigger'] == ("gizmo_turn", "Turn around Y"),
          "right-handed: the right trigger's legend says what the handle does")
    check(controllers.hints(s, controllers.Info(gizmo_off=("gizmo_move", "Move along X")))['left']['trigger'] ==
          ("gizmo_move", "Move along X"), "and the left one's, for the off hand")
    s.left_handed = True
    check(controllers.hints(s, info)['left']['trigger'] == ("gizmo_turn", "Turn around Y"), "left-handed: swapped")
    s.left_handed = False
    check(controllers.hints(s, controllers.Info(gizmo_main=info.gizmo_main, ray_on_panel=True))['right']['trigger']
          == ("cursor", "Press"), "the panel still wins when the ray is on it")

    menus.reset()
    menus.set_mode(ctx, 'MODEL')
    ws = widgets()
    check(all(k in ws for k in ("gizmo_move", "gizmo_rotate", "gizmo_scale", "gizmo_local")),
          "Model with a selection: the gizmo row")
    ws["gizmo_move"].on_press()
    check(not s.gizmo_move, "its Move toggle turns the arrows off")
    ws = widgets()
    ws["gizmo_move"].on_press()
    ws["gizmo_local"].on_press()
    check(s.gizmo_move and s.gizmo_local, "and on again; Local switches the axes")
    s.gizmo_local = False
    height = menus.layout(ctx)[1]
    set_more = menus.set_more
    set_more('MODEL', True)
    open_height = menus.layout(ctx)[1]
    set_more('MODEL', False)
    check(open_height < 0.32, f"Model with the gizmo row and Snapping open fits ({height:.3f} / {open_height:.3f} m)")
    primitives.make_active(ctx, None)
    check("gizmo_move" not in widgets(), "nothing selected: no row")

    s.mode = 'TOOLS'
    s.tool = 'LIGHT'
    lookdev.new_light(ctx, 'SPOT', Matrix.Translation((0.0, 1.0, 1.5)))
    menus.set_more('TOOLS_LIGHT', True)
    ws, height = menus.layout(ctx)
    ids = {w.id for w in ws}
    menus.set_more('TOOLS_LIGHT', False)
    check("gizmo_rotate" in ids and "gizmo_scale" not in ids, "Light: move, turn and local; no size")
    check(height < 0.32, f"the Light page with its More open fits ({height:.3f} m)")
    menus.set_mode(ctx, 'MODEL')


def main():
    vr_sculpt.register()
    ctx = bpy.context
    try:
        test_target_and_handles(ctx)
        test_move(ctx)
        test_turn(ctx)
        test_size(ctx)
        test_trigger(ctx)
        test_hints_and_panel(ctx)
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
