"""Background tests for the hard surface tools: live booleans with cutters, the modifier stack order,
the bevel, shading modes and hard / soft edges, solidify, arrays, surface and rotation snapping, and the Model mode panel pages.

blender --background --factory-startup --python tests/test_hard_surface.py
"""

import math
import os
import sys
import time
import traceback

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import hardsurface, menus, operators, primitives, shading, snapping, symmetry, vr_ui  # noqa: E402
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)


def evaluated(obj):
    """(vertex count, volume, world bounds lo, hi) of what you see."""
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    bm = bmesh.new()
    bm.from_mesh(me)
    volume = abs(bm.calc_volume())
    bm.free()
    co = [obj.matrix_world @ v.co for v in me.vertices]
    n = len(me.vertices)
    ev.to_mesh_clear()
    lo = Vector([min(c[i] for c in co) for i in range(3)]) if co else Vector()
    hi = Vector([max(c[i] for c in co) for i in range(3)]) if co else Vector()
    return n, volume, lo, hi


def plate_shading(plate):
    """Worst corner-normal error on a hole's wall (vs. radial) and on the top (vs. up), and the share
    of bevel corners whose normal is bent away from their face's."""
    import numpy as np
    bpy.context.view_layer.update()
    ev = plate.evaluated_get(bpy.context.evaluated_depsgraph_get())
    me = ev.to_mesh()
    n = np.array([v.vector[:] for v in me.corner_normals])
    co = np.array([v.co[:] for v in me.vertices])[[lp.vertex_index for lp in me.loops]]
    fn = np.array([p.normal[:] for p in me.polygons])
    area = np.array([p.area for p in me.polygons])
    loop_f = np.empty(len(me.loops), dtype=np.int64)
    for p in me.polygons:
        loop_f[p.loop_start:p.loop_start + p.loop_total] = p.index
    ev.to_mesh_clear()
    f = fn[loop_f]
    r = np.hypot(co[:, 0], co[:, 1])
    wall = (np.abs(f[:, 2]) < 0.01) & (np.abs(r - 0.03) < 1e-3) & (np.abs(co[:, 2]) < 0.02)
    inward = -co[:, :2] / np.maximum(r[:, None], 1e-9)
    wall_dev = np.degrees(np.arccos(np.clip((n[wall, :2] * inward[wall]).sum(1), -1, 1))).max()
    top = f[:, 2] > 0.999
    top_dev = np.degrees(np.arccos(np.clip(n[top, 2], -1, 1))).max()
    small = area[loop_f] < 1e-5  # bevel strips
    bent = np.degrees(np.arccos(np.clip((n[small] * f[small]).sum(1), -1, 1))) > 1.0
    return wall_dev, top_dev, bent.mean()


def cube(size, loc, name="Cube"):
    obj = primitives.create(bpy.context, 'CUBE', Vector(loc))
    obj.name = name
    obj.vr_prim.size = (size,) * 3
    bpy.context.view_layer.update()
    return obj


def setup():
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    wm = tm.FakeWM(ss)
    ctx = tm.FakeContext(wm)
    settings = bpy.context.scene.vr_sculpt
    settings.mode = 'MODEL'
    settings.tip_offset = 0.0
    settings.show_panel = False  # Presses go to the scene, not the panel.
    settings.primitive = 'mesh.primitive_cube_add'
    settings.model_add_as = 'OBJECT'
    settings.snap_surface = settings.snap_angle = settings.grid_snap = False
    return ss, ctx, settings


def test_cutters():
    print("== cutters made with the trigger")
    clear()
    ss, ctx, settings = setup()
    op, r = tm.press(ctx, ss, 1, [(0, 0, 0), (0.1, 0.1, 0.1), (0.2, 0.2, 0.2)])
    target = bpy.context.view_layer.objects.active
    check(r == {'FINISHED'} and not hardsurface.is_cutter(target), "New as Object: a drag makes a plain object")
    n0, v0, _lo, _hi = evaluated(target)

    settings.model_add_as = 'DIFFERENCE'
    # Start inside the target (that would grab it as an Object) and drag out past its corner.
    op, r = tm.press(ctx, ss, 1, [(0.15, 0.15, 0.15), (0.2, 0.2, 0.2), (0.25, 0.25, 0.25)])
    cutter = bpy.context.view_layer.objects.active
    check(type(op.handler).__name__ == "CreatePrimitive", "Cut: a press on the selected object starts a cutter")
    check(hardsurface.is_cutter(cutter) and hardsurface.target_of(cutter) == target, "the new primitive is its cutter")
    check(cutter.parent == target, "the cutter is parented to the target (moves with it)")
    check(close(cutter.matrix_world.translation, (0.2, 0.2, 0.2)), "parenting kept where the cutter was dragged")
    check(cutter.display_type == 'WIRE' and cutter.hide_render, "the cutter is a wireframe that doesn't render")
    check([c.name for c in cutter.users_collection] == [hardsurface.CUTTERS], "the cutter is in VR Cutters")
    mod = hardsurface.modifier_of(cutter)
    check(mod is not None and mod.operation == 'DIFFERENCE' and mod.solver == 'MANIFOLD',
          f"Boolean modifier on the target, Difference, Manifold ({mod and mod.solver})")
    n1, v1, _lo, _hi = evaluated(target)
    # A 0.1 cube corner cut out of the 0.2 cube's corner: 0.05^3 removed.
    check(abs((v0 - v1) - 0.05 ** 3) < 1e-6, f"the cut removed the overlap ({v0 - v1:.6f})")
    check(primitives.is_editable(target) and primitives.is_editable(cutter), "both stay editable primitives")

    # The cutter's settings update the cut live.
    cutter.vr_prim.size = (0.2, 0.2, 0.2)
    _n, v2, _lo, _hi = evaluated(target)
    check(abs((v0 - v2) - 0.1 ** 3) < 1e-6, f"a bigger cutter cuts more ({v0 - v2:.6f})")
    cutter.vr_prim.size = (0.1, 0.1, 0.1)

    # Grab the cutter (the smaller object under the tip) and move it out of the target.
    op, r = tm.press(ctx, ss, 1, [(0.22, 0.22, 0.22), (0.4, 0.22, 0.22), (0.6, 0.22, 0.22)])
    check(type(op.handler).__name__ == "MoveObject" and op.handler.obj == cutter,
          "Cut: a press on a cutter grabs it")
    _n, v3, _lo, _hi = evaluated(target)
    check(abs(v3 - v0) < 1e-6, "moved clear of the target, nothing is cut")
    # Moving the target takes its cutters along.
    before = cutter.matrix_world.translation.copy()
    target.location.z += 1.0
    bpy.context.view_layer.update()
    check(close(cutter.matrix_world.translation - before, (0, 0, 1.0)), "the cutter follows its target")
    target.location.z -= 1.0
    bpy.context.view_layer.update()
    cutter.matrix_world = Matrix.Translation((0.2, 0.2, 0.2))

    hardsurface.set_operation(cutter, 'UNION')
    _n, v4, _lo, _hi = evaluated(target)
    check(abs((v4 - v0) - (0.1 ** 3 - 0.05 ** 3)) < 1e-6, f"Union adds the part outside ({v4 - v0:.6f})")
    hardsurface.set_operation(cutter, 'INTERSECT')
    _n, v5, _lo, _hi = evaluated(target)
    check(abs(v5 - 0.05 ** 3) < 1e-6, f"Intersect keeps only the overlap ({v5:.6f})")
    hardsurface.set_operation(cutter, 'DIFFERENCE')

    # Hidden cutters still cut, and can't be grabbed.
    hardsurface.set_cutters_visible(ctx, False)
    check(not cutter.visible_get() and not hardsurface.cutters_visible(ctx), "Show cutters off hides them")
    _n, v6, _lo, _hi = evaluated(target)
    check(abs((v0 - v6) - 0.05 ** 3) < 1e-6, "hidden cutters keep cutting")
    check(primitives.touching(bpy.context, Vector((0.18, 0.18, 0.18)), 0.01) == target,
          "a hidden cutter isn't touched (the target is)")
    hardsurface.set_cutters_visible(ctx, True)

    # A second cutter from the selected cutter cuts the same target.
    settings.primitive = 'mesh.primitive_cylinder_add'
    op, r = tm.press(ctx, ss, 1, [(-0.05, -0.05, 0.15), (-0.1, -0.1, 0.25)])
    second = bpy.context.view_layer.objects.active
    check(hardsurface.target_of(second) == target and len(hardsurface.cutters(target)) == 2,
          "with a cutter selected, the next cutter goes on the same target")
    settings.primitive = 'mesh.primitive_cube_add'

    # Open cutters can't use Manifold (it would do nothing): Exact.
    plane = bpy.data.objects.new("Plane", bpy.data.meshes.new("Plane"))
    plane.data.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    bpy.context.collection.objects.link(plane)
    plane.scale = (0.05,) * 3
    plane.location = (0, 0, 0.05)
    pmod = hardsurface.make_cutter(bpy.context, plane, target, 'DIFFERENCE')
    check(pmod.solver == 'EXACT', "an open cutter gets the Exact solver")
    hardsurface.delete(bpy.context, plane)
    check(pmod.name not in target.modifiers, "deleting a cutter removes its Boolean modifier")

    # Duplicate a cutter: one more cut on the same target.
    copy = hardsurface.duplicate(bpy.context, second)
    check(hardsurface.target_of(copy) == target and len(hardsurface.cutters(target)) == 3,
          "a duplicated cutter cuts the same target")
    hardsurface.delete(bpy.context, copy)
    # Duplicate the target: its copy gets its own cutters.
    twin = hardsurface.duplicate(bpy.context, target)
    twin_cutters = hardsurface.cutters(twin)
    check(len(twin_cutters) == 2 and not set(twin_cutters) & set(hardsurface.cutters(target))
          and all(c.parent == twin for c in twin_cutters), "a duplicated target gets copies of its cutters")
    _n, vt, _lo, _hi = evaluated(twin)
    _n, vo, _lo, _hi = evaluated(target)
    check(abs(vt - vo) < 1e-6, "the copy is cut the same way")
    names = [c.name for c in twin_cutters]
    hardsurface.delete(bpy.context, twin)
    check(not any(n in bpy.data.objects for n in names), "deleting a target deletes its cutters")

    # Apply: the cut becomes real geometry, the cutter goes, the target is a plain mesh.
    n_before, v_before, _lo, _hi = evaluated(target)
    hardsurface.apply_cutter(bpy.context, cutter)
    check(len(hardsurface.cutters(target)) == 1 and target.vr_prim.kind == 'NONE',
          "Apply removes the cutter and its modifier; the target is a plain mesh")
    hardsurface.apply_all(bpy.context, target)
    _n, v_after, _lo, _hi = evaluated(target)
    check(not hardsurface.booleans(target) and abs(v_after - v_before) < 1e-6
          and len(target.data.vertices) > 8, f"Apply all keeps the shape you saw ({v_before:.6f} -> {v_after:.6f})")


def test_stack():
    print("== modifier stack order, finish, solidify")
    clear()
    obj = cube(0.1, (0, 0, 0), "Stack")
    cutter = cube(0.05, (0.05, 0.05, 0.05), "StackCutter")
    hardsurface.set_bevel(obj, True)
    hardsurface.set_array_count(bpy.context, obj, 3)
    symmetry.model_toggle(bpy.context, obj, 0)
    hardsurface.make_cutter(bpy.context, cutter, obj)
    hardsurface.set_solidify(obj, True)
    names = [m.name for m in obj.modifiers]
    check(names == [hardsurface.SOLIDIFY, hardsurface.BOOLEAN, hardsurface.ARRAY, symmetry.MODIFIER,
                    shading.MODIFIER, hardsurface.BEVEL], f"our modifiers keep their order ({names})")
    other = obj.modifiers.new("Mine", 'DISPLACE')
    obj.modifiers.move(obj.modifiers.find(other.name), 1)  # The user puts their own one in the middle.
    hardsurface.sort_stack(obj)
    check(obj.modifiers.find(other.name) == 1, "someone else's modifier stays where it was")
    obj.modifiers.remove(other)
    hardsurface.set_solidify(obj, False)
    hardsurface.set_array_count(bpy.context, obj, 1)
    check(hardsurface.solidify(obj) is None and hardsurface.array(obj) is None, "Solidify off / Count 1 remove them")

    # Bevel with Harden Normals; Auto shading comes with it.
    clear()
    obj = cube(0.1, (0, 0, 0), "Finish")
    n0, _v, _lo, _hi = evaluated(obj)
    hardsurface.set_bevel(obj, True)
    mod = hardsurface.bevel(obj)
    check(mod is not None and mod.harden_normals and mod.limit_method == 'ANGLE', "Bevel is angle-limited, hardened")
    check(shading.mode(obj) == 'AUTO', "and brings Auto shading")
    check(abs(hardsurface.bevel_width(obj) - 0.003) < 1e-6, f"default width 3% of the smallest side "
          f"({hardsurface.bevel_width(obj):.4f})")
    n1, _v, _lo, _hi = evaluated(obj)
    check(n1 > n0 * 4, f"the edges are beveled ({n0} -> {n1} verts)")
    obj.scale = (2.0, 2.0, 2.0)
    bpy.context.view_layer.update()
    hardsurface.set_bevel_width(obj, 0.004)
    check(abs(mod.width - 0.002) < 1e-6, "the width slider is in world units on a scaled object")
    hardsurface.set_bevel(obj, False)
    check([m.name for m in obj.modifiers] == [shading.MODIFIER], "Bevel off leaves the shading")
    shading.set_mode(obj, None)
    check(not obj.modifiers, "and the shading comes off on its own")

    obj.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()
    hardsurface.set_solidify(obj, True)
    check(abs(hardsurface.solidify_thickness(obj) - 0.005) < 1e-6, "Solidify starts at 5% of the size")

    # Shading: a plate with a drilled hole, finished. The hole's wall must be smooth (corner normals
    # pointing at the hole's axis, not the faces' own), the top flat, the bevels rounded.
    clear()
    plate = cube(0.2, (0, 0, 0), "Plate")
    plate.vr_prim.size = (0.3, 0.2, 0.06)
    hole = primitives.create(bpy.context, 'CYLINDER', Vector((0.0, 0.0, 0.03)))
    hole.vr_prim.radius, hole.vr_prim.height, hole.vr_prim.segments = 0.03, 0.1, 48
    hardsurface.make_cutter(bpy.context, hole, plate)
    bpy.context.view_layer.update()
    hardsurface.set_bevel(plate, True)
    wall, top, rounded = plate_shading(plate)
    check(wall < 1.0, f"Finish: the cut wall is smooth (worst corner {wall:.1f} deg off radial)")
    check(top < 0.5, f"Finish: the top stays flat (worst corner {top:.1f} deg off up)")
    check(rounded > 0.5, f"Finish: the bevels are shaded round ({rounded * 100:.0f}% of their corners bent)")


def corners(obj):
    """(corner normals, their face normals, corner positions) of what you see, object space."""
    import numpy as np
    bpy.context.view_layer.update()
    ev = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    me = ev.to_mesh()
    n = np.array([v.vector[:] for v in me.corner_normals])
    co = np.array([v.co[:] for v in me.vertices])[[lp.vertex_index for lp in me.loops]]
    fn = np.array([p.normal[:] for p in me.polygons])
    loop_f = np.empty(len(me.loops), dtype=np.int64)
    for p in me.polygons:
        loop_f[p.loop_start:p.loop_start + p.loop_total] = p.index
    ev.to_mesh_clear()
    return n, fn[loop_f], co


def bent(n, f):
    """Degrees each corner normal is turned away from its face's normal."""
    import numpy as np
    return np.degrees(np.arccos(np.clip((n * f).sum(1), -1.0, 1.0)))


def test_shading():
    print("== shading modes and hard / soft edges")
    import numpy as np
    clear()
    cyl = primitives.create(bpy.context, 'CYLINDER', Vector((0.0, 0.0, 0.0)))
    cyl.vr_prim.radius, cyl.vr_prim.height, cyl.vr_prim.segments = 0.05, 0.1, 24
    bpy.context.view_layer.update()
    check(shading.mode(cyl) is None, "no VR shading to start with (the mesh's own)")
    for mode_name in ('FLAT', 'SMOOTH', 'AUTO'):
        shading.set_mode(cyl, mode_name)
        n, f, co = corners(cyl)
        side = np.abs(f[:, 2]) < 1e-3
        cap = np.abs(f[:, 2]) > 0.999
        side_bend, cap_bend = bent(n[side], f[side]).max(), bent(n[cap], f[cap]).max()
        check(shading.mode(cyl) == mode_name, f"{mode_name}: mode reads back")
        if mode_name == 'FLAT':
            check(side_bend < 0.1 and cap_bend < 0.1, f"Flat: every corner keeps its face's normal ({side_bend:.2f})")
        elif mode_name == 'SMOOTH':
            check(side_bend > 3.0 and cap_bend > 10.0, f"Smooth: sides and caps blend ({side_bend:.1f}, "
                  f"{cap_bend:.1f} deg)")
        else:
            check(side_bend > 3.0 and cap_bend < 0.1, f"Auto 30: smooth sides, flat caps ({side_bend:.1f}, "
                  f"{cap_bend:.2f} deg)")
    shading.set_angle(cyl, math.radians(5.0))
    n, f, co = corners(cyl)
    side = np.abs(f[:, 2]) < 1e-3
    check(bent(n[side], f[side]).max() < 0.1, "Auto 5: the 15 degree side edges turn sharp")
    shading.set_angle(cyl, math.radians(30.0))

    # A soft mark smooths a cube edge that Auto would keep sharp; a hard mark creases the cylinder.
    box = cube(0.1, (1.0, 0.0, 0.0), "Box")
    shading.set_mode(box, 'AUTO')
    ss, ctx, settings = setup()
    primitives.make_active(bpy.context, box)
    settings.model_mark = 'SOFT'
    op, r = tm.press(ctx, ss, 1, [(0.97, -0.05, 0.05), (1.0, -0.05, 0.05), (1.03, -0.05, 0.05)])
    check(type(op.handler).__name__ == "EdgeMark" and r == {'FINISHED'}, "Mark tool: the trigger marks edges")
    hard, soft = shading.marks(box.data)
    check(soft.sum() == 1 and not hard.any(), f"one edge marked soft ({soft.sum()})")
    n, f, co = corners(box)
    # The edge's two end corners on the top and front faces (the side faces stay apart: hard edges).
    on_edge = (np.abs(co[:, 1] + 0.05) < 1e-4) & (np.abs(co[:, 2] - 0.05) < 1e-4) & \
        ((f[:, 2] > 0.99) | (f[:, 1] < -0.99))
    check(on_edge.any() and abs(bent(n[on_edge], f[on_edge]).min() - 45.0) < 1.0,
          "the soft edge's corners blend the two faces (45 deg)")
    # Resizing keeps the marks (same edges); more subdivisions can't.
    box.vr_prim.size = (0.1, 0.1, 0.12)
    check(shading.marks(box.data)[1].sum() == 1, "resizing the primitive keeps the marks")
    # The left hand clears.
    box.vr_prim.size = (0.1, 0.1, 0.1)
    op, r = tm.press(ctx, ss, 0, [(0.97, -0.05, 0.05), (1.03, -0.05, 0.05)])
    check(not shading.marks(box.data)[1].any(), "the left hand clears marks")
    settings.model_mark = 'HARD'
    tm.press(ctx, ss, 1, [(0.97, -0.05, 0.05), (1.03, -0.05, 0.05)])
    box.vr_prim.subdivisions = 1
    check(not shading.marks(box.data)[0].any(), "new topology (subdivisions) drops the marks")

    primitives.make_active(bpy.context, cyl)
    x, y = 0.05, 0.0  # The side edge at angle 0.
    tm.press(ctx, ss, 1, [(x, y, -0.03), (x, y, 0.03)])
    hard, _soft = shading.marks(cyl.data)
    check(hard.sum() == 1, f"one cylinder side edge marked hard ({hard.sum()})")
    n, f, co = corners(cyl)
    side = np.abs(f[:, 2]) < 1e-3
    crease = side & (np.abs(co[:, 0] - x) < 1e-4) & (np.abs(co[:, 1] - y) < 1e-4)
    rest = side & ~crease
    check(crease.any() and bent(n[crease], f[crease]).max() < 0.1 and bent(n[rest], f[rest]).min() > 1.0,
          "the hard edge creases; the rest of the side stays smooth")
    settings.model_mark = 'NONE'

    # The Mark tool also reaches the cutters (their marks carry into the cut).
    plate = cube(0.2, (0, 2.0, 0), "MarkPlate")
    hole = primitives.create(bpy.context, 'CYLINDER', Vector((0.0, 2.0, 0.1)))
    hole.vr_prim.radius, hole.vr_prim.height, hole.vr_prim.segments = 0.03, 0.1, 24
    hardsurface.make_cutter(bpy.context, hole, plate)
    shading.set_mode(plate, 'AUTO')
    primitives.make_active(bpy.context, plate)
    check(set(shading.mark_targets(bpy.context)) == {plate, hole}, "the Mark tool works on the object and its cutters")

    # Panel: the Finish page's shading and mark buttons; leaving the page turns the tool off.
    settings.model_page = 'FINISH'
    ids = panel_ids(ctx)
    check(all(k in ids for k in ("shade_flat", "shade_smooth", "shade_auto", "shade_angle", "mark_hard", "bevel")),
          "Finish page: shading modes, Angle, Mark and Bevel")
    ids["shade_auto"].on_press()
    check(shading.mode(plate) is None, "pressing the active mode takes the VR shading off")
    ids["mark_soft"].on_press()
    check(settings.model_mark == 'SOFT', "Mark Soft turns the tool on")
    panel_ids(ctx)["page_shape"].on_press()
    check(settings.model_mark == 'NONE', "leaving the Finish page turns it off")
    panel_ids(ctx)["page_finish"].on_press()
    panel_ids(ctx)["bevel"].on_press()
    check(hardsurface.has_bevel(plate) and shading.mode(plate) == 'AUTO', "Bevel brings Auto shading with it")
    settings.model_page = 'SHAPE'
    menus.reset()


def test_arrays():
    print("== arrays")
    clear()
    obj = cube(0.1, (0, 0, 0), "Arr")
    hardsurface.set_array_count(bpy.context, obj, 3)
    n, _v, lo, hi = evaluated(obj)
    # 3 copies along X, a quarter of the size apart: 3 * 0.1 + 2 * 0.025.
    check(n == 24 and abs((hi.x - lo.x) - 0.35) < 1e-5, f"linear: 3 copies along X ({hi.x - lo.x:.4f})")
    hardsurface.set_array_axis(bpy.context, obj, 2)
    hardsurface.set_array_gap(bpy.context, obj, 0.0)
    _n, _v, lo, hi = evaluated(obj)
    check(abs((hi.z - lo.z) - 0.3) < 1e-5 and abs((hi.x - lo.x) - 0.1) < 1e-5, "axis Z, gap 0: stacked")

    bpy.context.scene.cursor.location = (0.5, 0.0, 0.0)
    hardsurface.set_array_axis(bpy.context, obj, 2)
    hardsurface.set_array_mode(bpy.context, obj, 'RADIAL')
    hardsurface.set_array_count(bpy.context, obj, 4)
    n, _v, lo, hi = evaluated(obj)
    # Four copies around (0.5, 0, 0): at x = 0, 0.5 +- 0.5 and y = +-0.5.
    center = (lo + hi) / 2
    check(n == 32 and close(center, (0.5, 0.0, 0.0), 1e-4) and abs((hi.x - lo.x) - 1.1) < 1e-4,
          f"radial: 4 copies around the 3D cursor (center {tuple(round(c, 3) for c in center)})")
    mod = hardsurface.array(obj)
    empty = mod.offset_object
    check(empty is not None and empty.parent == obj and empty.get("vrs_helper"), "the turn is an empty on the object")
    obj.location.x += 1.0
    _n, _v, lo2, hi2 = evaluated(obj)
    check(close((lo2 + hi2) / 2, (1.5, 0.0, 0.0), 1e-4), "the ring moves with the object")
    obj.location.x -= 1.0
    bpy.context.view_layer.update()
    hardsurface.set_array_center(bpy.context, obj, Vector((0.0, 0.3, 0.0)))
    _n, _v, lo, hi = evaluated(obj)
    check(close((lo + hi) / 2, (0.0, 0.3, 0.0), 1e-4), "Center at left hand moves the ring's center")
    twin = hardsurface.duplicate(bpy.context, obj)
    check(hardsurface.array(twin).offset_object not in (None, empty), "a duplicate gets its own array empty")
    empty_name = empty.name
    hardsurface.set_array_mode(bpy.context, obj, 'LINEAR')
    check(empty_name not in bpy.data.objects and not mod.use_object_offset, "Linear drops the empty")
    name = hardsurface.array(twin).offset_object.name
    hardsurface.set_array_count(bpy.context, twin, 1)
    check(name not in bpy.data.objects, "taking the array off removes its empty")


def test_snapping():
    print("== surface and rotation snapping")
    clear()
    ss, ctx, settings = setup()
    base = cube(0.2, (0, 0, 0), "Base")  # Top at z = 0.1.
    part = cube(0.04, (0.5, 0, 0.5), "Part")
    settings.snap_surface = True
    # Grab the part at its center and bring it 1.5 cm over the top: it lands on it.
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.3, 0, 0.3), (0.02, 0.01, 0.135)])
    check(close(part.location, (0.02, 0.01, 0.12)), f"snapped: standing on the top face ({tuple(part.location)})")
    check(snapping.last_hit is None, "the snap marker goes when you let go")
    # Too far above: free.
    op, r = tm.press(ctx, ss, 1, [(0.02, 0.01, 0.12), (0.02, 0.01, 0.2), (0.02, 0.01, 0.25)])
    check(close(part.location, (0.02, 0.01, 0.25)), "out of reach: it follows the hand")

    # A tilted base: the part turns to its normal, even with the controller held straight.
    base.rotation_euler = (math.radians(30), 0, 0)
    bpy.context.view_layer.update()
    normal = base.matrix_world.to_3x3() @ Vector((0, 0, 1))
    top = base.matrix_world @ Vector((0, 0, 0.1))
    near = top + normal * 0.035  # Part center (2 cm half size + 1.5 cm gap)
    op, r = tm.press(ctx, ss, 1, [(0.02, 0.01, 0.25), (0.02, 0.01, 0.4), tuple(near)])
    z = part.matrix_world.to_3x3() @ Vector((0, 0, 1))
    bottom = part.matrix_world @ Vector((0, 0, -0.02))
    check(z.angle(normal) < 1e-3, f"its Z turned to the surface normal ({math.degrees(z.angle(normal)):.2f} deg)")
    free_bottom = Vector(near) - Vector((0, 0, 0.02))  # where the hand alone would have put it
    expected = free_bottom - normal * (free_bottom - top).dot(normal)
    check((bottom - expected).length < 1e-3, "its bottom center is on the surface, under where the hand put it")

    # Rotation snap with the controller turned 20 degrees: 15 or 30, never 20.
    settings.snap_surface = False
    settings.snap_angle = True
    part.matrix_world = Matrix.Translation((1, 0, 0))
    bpy.context.view_layer.update()
    ss.aim_rot[1] = Quaternion()
    tm.press(ctx, ss, 1, [(1, 0, 0), (1.05, 0, 0), (1.1, 0, 0)], Quaternion())
    op = tm.harness()
    ss.trigger = 1.0
    ss.aim_loc[1] = Vector((1.1, 0, 0))
    op.invoke(ctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR((1.1, 0, 0))))
    ss.aim_rot[1] = Quaternion((0, 0, 1), math.radians(20))
    time.sleep(0.01)
    op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.trigger = 0.0
    time.sleep(0.01)
    op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.aim_rot[1] = Quaternion()
    angle = math.degrees(part.matrix_world.to_euler().z)
    check(abs(angle - round(angle / 15) * 15) < 1e-3 and abs(angle) > 1, f"Snap Rotation: turned in 15 degree "
          f"steps ({angle:.2f})")
    settings.snap_angle = False

    # A cutter snaps to its target's own surface (not the hole it cuts) and sinks halfway in.
    clear()
    base = cube(0.2, (0, 0, 0), "Base")
    hole = cube(0.04, (0.5, 0, 0.5), "Hole")
    hardsurface.make_cutter(bpy.context, hole, base)
    settings.snap_surface = True
    op, r = tm.press(ctx, ss, 1, [(0.5, 0, 0.5), (0.3, 0, 0.3), (0.0, 0.0, 0.115)])
    check(close(hole.matrix_world.translation, (0.0, 0.0, 0.1)), "a cutter's center lands on its target's surface "
          f"({tuple(round(c, 4) for c in hole.matrix_world.translation)})")
    _n, v, _lo, _hi = evaluated(base)
    check(abs((0.2 ** 3 - v) - 0.04 * 0.04 * 0.02) < 1e-7, "and cuts a pocket half its height deep")
    settings.snap_surface = False


def panel_ids(ctx):
    """The Model panel's widgets with every More open and, with a selection, the object's sheet open."""
    menus.open_sheet('OBJECT')
    for key in ('MODEL', 'OBJECT_SHAPE', 'OBJECT_BOOLEAN', 'OBJECT_FINISH', 'OBJECT_ARRAY'):
        menus.set_more(key, True)
    widgets, _height = menus.layout(ctx)
    return {w.id: w for w in widgets if w.id}


def test_panel():
    print("== Model panel pages")
    clear()
    ss, ctx, settings = setup()
    ids = panel_ids(ctx)
    check(all(i in ids for i in ("add_as_object", "add_as_difference", "snap_surface", "snap_angle")),
          "New as and snap buttons are on the panel")
    ids["add_as_difference"].on_press()
    check(settings.model_add_as == 'DIFFERENCE', "Cut button sets New as")
    obj = cube(0.1, (0, 0, 0), "Panel")
    for page in ('SHAPE', 'BOOLEAN', 'FINISH', 'ARRAY'):
        settings.model_page = page
        ids = panel_ids(ctx)
        check("page_" + page.lower() in ids, f"page {page} draws")
    settings.model_page = 'FINISH'
    panel_ids(ctx)["bevel"].on_press()
    check(hardsurface.has_bevel(obj) and "bevel_width" in panel_ids(ctx), "Bevel button adds the bevel and its sliders")
    settings.model_page = 'ARRAY'
    panel_ids(ctx)["array_count"].on_change(4)
    ids = panel_ids(ctx)
    check(hardsurface.array_count(obj) == 4 and "array_gap" in ids, "Count slider makes an array; Gap shows")
    ids["array_radial"].on_press()
    check("array_center" in panel_ids(ctx), "Radial shows the center button")
    settings.model_page = 'SHAPE'
    panel_ids(ctx)["solidify"].on_press()
    check("solidify_thickness" in panel_ids(ctx), "Solidify button shows Thickness")
    cutter = cube(0.05, (0.05, 0.05, 0.05), "PanelCutter")
    hardsurface.make_cutter(bpy.context, cutter, obj)
    settings.model_page = 'BOOLEAN'
    ids = panel_ids(ctx)
    check("bool_union" in ids and "bool_apply" in ids, "a cutter's page has its operation and Apply")
    ids["bool_union"].on_press()
    check(hardsurface.modifier_of(cutter).operation == 'UNION', "Union button changes the cutter's operation")
    ids["bool_target"].on_press()
    ids = panel_ids(ctx)
    check("bool_apply_all" in ids and "solver_exact" in ids, "the target's page has Apply all and the solver")
    ids["solver_exact"].on_press()
    check(all(m.solver == 'EXACT' for m in hardsurface.booleans(obj)), "Exact switches every cut's solver")
    settings.model_add_as = 'OBJECT'
    menus.reset()


def main():
    vr_sculpt.register()
    try:
        test_cutters()
        test_stack()
        test_shading()
        test_arrays()
        test_snapping()
        test_panel()
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
