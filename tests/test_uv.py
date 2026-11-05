"""Background tests for the UV page: seams marked with the trigger (and joined along the mesh), live
unwrap, the Fix brush's partial unwrap, Smart UV / Pack / Seams from Sharp, the stretch measure, the
view's arrays, islands and their view, Twist and Size, seams kept through primitive resizes, and the
panel page.

blender --background --factory-startup --python tests/test_uv.py
"""

import math
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import vr_sculpt  # noqa: E402
from vr_sculpt import menus, operators, primitives, shading, uv  # noqa: E402
import test_model as tm  # noqa: E402  (fakes and the press() helper)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)


def setup():
    operators.session_running = lambda context: True
    ss = tm.FakeSessionState()
    wm = tm.FakeWM(ss)
    ctx = tm.FakeContext(wm)
    settings = bpy.context.scene.vr_sculpt
    settings.mode = 'MODEL'
    settings.tip_offset = 0.0
    settings.show_panel = False  # Presses go to the scene, not the panel.
    settings.model_mark = 'NONE'
    settings.uv_tool = 'NONE'
    settings.uv_live = True
    settings.uv_method = 'ANGLE_BASED'
    return ss, ctx, settings


def cube(size, loc, name="Cube", subdivisions=0):
    obj = primitives.create(bpy.context, 'CUBE', Vector(loc))
    obj.name = name
    obj.vr_prim.size = (size,) * 3
    obj.vr_prim.subdivisions = subdivisions
    bpy.context.view_layer.update()
    return obj


def sphere(name="Ball", segments=32, rings=16):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=0.1)
    obj = bpy.context.active_object
    obj.name = name
    primitives.make_active(bpy.context, obj)
    return obj


def uvs(obj):
    return uv._uvs(obj.data)


def edge_world(obj):
    return shading._edge_segments(obj)


def test_seams():
    print("== seams with the trigger")
    clear()
    box = cube(0.1, (0, 0, 0), "SeamBox")
    ss, ctx, settings = setup()
    primitives.make_active(bpy.context, box)
    settings.uv_tool = 'SEAM'
    settings.uv_live = False
    # Along the top front edge (y = -0.05, z = 0.05).
    op, r = tm.press(ctx, ss, 1, [(-0.03, -0.05, 0.05), (0.0, -0.05, 0.05), (0.03, -0.05, 0.05)])
    check(type(op.handler).__name__ == "SeamMark" and r == {'FINISHED'}, "Seams tool: the trigger marks seams")
    check(uv.seams(box.data).sum() == 1, f"one edge marked as a seam ({uv.seams(box.data).sum()})")
    check(not shading.marks(box.data)[0].any(), "seams aren't shading marks")
    op, r = tm.press(ctx, ss, 0, [(-0.03, -0.05, 0.05), (0.03, -0.05, 0.05)])
    check(not uv.seams(box.data).any(), "the left hand clears seams")

    # A sweep with samples far apart (a fast hand) is joined along the edges between them.
    grid = cube(0.2, (1.0, 0.0, 0.0), "SeamGrid", subdivisions=7)
    primitives.make_active(bpy.context, grid)
    segs = edge_world(grid)
    # The top face's line y = 0 runs across 8 edges; touch only both ends and the middle. (The handler's
    # first update is the second sample, so the first is given twice.)
    pts = [(1.0 + x, 0.0, 0.1) for x in (-0.1, -0.1, 0.0, 0.1)]
    op, r = tm.press(ctx, ss, 1, pts)
    s = uv.seams(grid.data)
    on_line = (np.abs(segs[:, :, 1]) < 1e-5).all(1) & (np.abs(segs[:, :, 2] - 0.1) < 1e-5).all(1)
    check(s[on_line].sum() == 8, f"the whole line between samples is marked ({s[on_line].sum()} of 8)")
    check(not s[~on_line].any(), f"and nothing else ({s[~on_line].sum()} other edges)")

    # Live: the mesh is unwrapped when the trigger is let go.
    settings.uv_live = True
    check(not grid.get("vrs_unwrapped") and not grid.data.get("vrs_unwrapped"), "not unwrapped before")
    tm.press(ctx, ss, 1, [(0.9, -0.1, 0.1), (0.9, -0.1, 0.1), (0.9, 0.0, 0.1), (0.9, 0.1, 0.1)])
    u = uvs(grid)
    check(grid.data.get("vrs_unwrapped") and u.min() >= -1e-6 and u.max() <= 1 + 1e-6 and np.ptp(u, 0).min() > 0.3,
          f"live unwrap after the stroke (UVs in {u.min():.3f}..{u.max():.3f})")
    check(grid.mode == 'OBJECT' and grid.select_get() and bpy.context.view_layer.objects.active == grid,
          "back in Object Mode with the selection as it was")
    settings.uv_tool = 'NONE'


def test_fix():
    print("== Fix: partial unwrap stitched to the rest")
    clear()
    ss, ctx, settings = setup()
    ball = sphere()
    me = ball.data
    # A seam down one meridian, then a clean unwrap.
    segs = edge_world(ball)
    seam = (np.abs(segs[:, :, 1]) < 1e-6).all(1) & (segs[:, :, 0] > -1e-6).all(1)
    uv.set_seams(me, seam)
    check(uv.unwrap(bpy.context, ball), "unwrap")
    good = uvs(ball).copy()
    # Mangle a patch: squash its inside UVs toward their center (what a sculpt does to them).
    loop_f, loop_v = uv._loop_faces(me)
    fc = np.zeros((len(me.polygons), 3))
    np.add.at(fc, loop_f, np.array([v.co[:] for v in me.vertices])[loop_v])
    fc /= np.bincount(loop_f)[:, None]
    patch = np.linalg.norm(fc - np.array([0.0, -0.1, 0.0]), axis=1) < 0.045
    inside = patch[loop_f]
    in_v = np.zeros(len(me.vertices), dtype=bool)
    in_v[loop_v[inside]] = True
    out_v = np.zeros(len(me.vertices), dtype=bool)
    out_v[loop_v[~inside]] = True
    interior = inside & ~(in_v & out_v)[loop_v]
    bad = good.copy()
    c = bad[interior].mean(0)
    bad[interior] = c + (bad[interior] - c) * 0.2
    me.uv_layers.active.data.foreach_set("uv", bad.ravel())
    before_err = np.abs(bad[interior] - good[interior]).max()

    # The user's own pin somewhere else survives.
    uv._set_pins(me, np.zeros(len(me.loops), dtype=bool))
    pins = np.zeros(len(me.loops), dtype=bool)
    pins[0] = True
    uv._set_pins(me, pins)
    # Brush over the patch with the trigger.
    ball.select_set(True)
    settings.uv_tool = 'FIX'
    settings.uv_fix_radius = 0.045
    op, r = tm.press(ctx, ss, 1, [(0.0, -0.1, 0.0), (0.0, -0.1, 0.0)])
    check(type(op.handler).__name__ == "FixUV" and r == {'FINISHED'}, "Fix tool: the trigger fixes")
    check(op.handler.faces.sum() == patch.sum(), f"the brushed faces are the patch ({op.handler.faces.sum()} of "
          f"{patch.sum()})")
    after = uvs(ball)
    outside_moved = np.abs(after[~inside] - bad[~inside]).max()
    border = inside & ~interior
    border_moved = np.abs(after[border] - bad[border]).max()
    err = np.abs(after[interior] - good[interior]).max()
    check(outside_moved < 1e-6 and border_moved < 1e-6, f"nothing outside the patch moves ({outside_moved:.2g}, "
          f"border {border_moved:.2g})")
    check(err < before_err / 3, f"the patch is back near its clean layout ({err:.4f}, was {before_err:.4f})")
    pins_after = uv._get_pins(me)
    check(pins_after is not None and pins_after.sum() == 1 and pins_after[0], "pins are put back as they were")
    sel = uv._get_selection(me)[2]
    check(not sel[patch].all() or sel.all(), "the mesh selection is put back")
    check(ball.mode == 'OBJECT', "back in Object Mode")

    # The patch is unwrapped on its own, so a dense mesh costs about the same as a light one.
    import time
    dense = sphere("Dense", 256, 128)
    segs = edge_world(dense)
    uv.set_seams(dense.data, (np.abs(segs[:, :, 1]) < 1e-6).all(1) & (segs[:, :, 0] > -1e-6).all(1))
    uv.unwrap(bpy.context, dense)
    dme = dense.data
    dfc = np.array([p.center[:] for p in dme.polygons])
    dpatch = np.linalg.norm(dfc - np.array([0.0, -0.1, 0.0]), axis=1) < 0.01
    t0 = time.perf_counter()
    uv.fix(bpy.context, dense, dpatch)
    ms = (time.perf_counter() - t0) * 1000
    check(ms < 400, f"Fix of {dpatch.sum()} faces on {len(dme.polygons)}: {ms:.0f} ms")
    bpy.data.objects.remove(dense)
    primitives.make_active(bpy.context, ball)

    # A whole island brushed (no border): unwrapped where it was.
    uv._set_pins(me, None)
    everything = np.ones(len(me.polygons), dtype=bool)
    lo, hi = uvs(ball).min(0), uvs(ball).max(0)
    check(uv.fix(bpy.context, ball, everything), "fix with no border")
    lo2, hi2 = uvs(ball).min(0), uvs(ball).max(0)
    check(np.abs((lo + hi) / 2 - (lo2 + hi2) / 2).max() < 0.02, "it stays where it was")
    settings.uv_tool = 'NONE'


def test_buttons():
    print("== Smart UV, Pack, Even out, Seams from Sharp")
    clear()
    ss, ctx, settings = setup()
    box = cube(0.1, (0, 0, 0), "Buttons")
    check(not uv.has_uvs(box) or not box.data.get("vrs_unwrapped"), "a new cube isn't unwrapped by us")
    check(uv.seams_from_sharp(box) and uv.seams(box.data).sum() == 12, f"Seams from Sharp: a cube's 12 edges "
          f"({uv.seams(box.data).sum()})")
    uv.clear_seams(box)
    check(not uv.seams(box.data).any(), "Clear seams")
    shading.set_mode(box, 'AUTO')
    hard, soft = shading.marks(box.data)
    soft[0] = True
    shading._write(box.data, shading.SOFT, soft)
    uv.seams_from_sharp(box)
    check(uv.seams(box.data).sum() == 11, "a soft edge doesn't get a seam")
    uv.clear_seams(box)
    check(uv.smart_project(bpy.context, box), "Smart UV")
    u = uvs(box)
    check(u.min() >= -1e-6 and u.max() <= 1 + 1e-6, "Smart UV fits the UV square")
    check(uv.seams(box.data).sum() >= 7, f"Smart UV marks seams along its islands ({uv.seams(box.data).sum()})")
    me = box.data
    me.uv_layers.active.data.foreach_set("uv", (u * 0.3).ravel())
    check(uv.pack(bpy.context, box) and uvs(box).max() > 0.6, f"Pack fills the square again ({uvs(box).max():.2f})")
    check(uv.average_scale(bpy.context, box), "Even out")


def test_stretch():
    print("== stretch measure")
    clear()
    ball = sphere("Stretch")
    me = ball.data
    segs = edge_world(ball)
    uv.set_seams(me, (np.abs(segs[:, :, 1]) < 1e-6).all(1) & (segs[:, :, 0] > -1e-6).all(1))
    uv.unwrap(bpy.context, ball, 'MINIMUM_STRETCH')

    def scores(u):
        co = np.array([v.co[:] for v in me.vertices])
        loop_v = np.array([lp.vertex_index for lp in me.loops])
        tris = np.array([t.loops[:] for t in me.loop_triangles])
        tri_f = np.array([t.polygon_index for t in me.loop_triangles])
        return uv.face_stretch(co, loop_v, u, tris, tri_f, len(me.polygons))

    clean = scores(uvs(ball))
    scaled = scores(uvs(ball) * 0.5)
    check(np.abs(scaled - clean).max() < 1e-6, "scaling the whole map doesn't count as stretch")
    squashed = uvs(ball) * np.array([1.0, 0.25])
    check(np.median(scores(squashed)) > 0.9, f"squashed to a quarter: red ({np.median(scores(squashed)):.2f})")
    # A sphere cut along one meridian really is stretched near the poles; around the equator it's not.
    fc = np.array([p.center[:] for p in me.polygons])
    equator = np.abs(fc[:, 2]) < 0.03
    poles = np.abs(fc[:, 2]) > 0.09
    check(clean[equator].mean() < clean[poles].mean(), f"stretched at the poles more than at the equator "
          f"({clean[poles].mean():.2f} vs {clean[equator].mean():.2f})")
    # Flat faces, each its own island: true to the surface.
    box = cube(0.1, (1.0, 0.0, 0.0), "FlatFaces", subdivisions=2)
    uv.smart_project(bpy.context, box)
    me = box.data
    flat = scores(uvs(box))
    check(flat.max() < 0.05, f"Smart UV on a cube: all blue ({flat.max():.3f})")


def test_view():
    print("== checker / stretch view arrays")
    clear()
    ss, ctx, settings = setup()
    box = cube(0.1, (0, 0, 0), "View")
    uv.smart_project(bpy.context, box)
    out = uv.build_view(bpy.context, box, 'STRETCH')
    check(out is not None, "built")
    pos, u, color, tris = out
    me = box.data
    check(len(pos) == len(me.loops) == len(u) == len(color) and tris.shape == (12, 3),
          f"one vertex per corner, 12 triangles ({len(pos)}, {tris.shape})")
    check(pos.dtype == np.float32 and color.shape[1] == 4, "float32 positions, RGBA colors")
    settings.uv_view = 'CHECKER'
    settings.uv_tool = 'NONE'
    check(not uv.view_wanted(bpy.context), "no view while the UV page isn't shown")
    settings.model_page = 'UV'
    menus.open_sheet('OBJECT')
    check(uv.view_wanted(bpy.context), "the UV page shows the view")
    uv.tick(bpy.context)
    check(uv._view["arrays"] is not None, "the timer builds it")
    key = uv._view["key"]
    uv.tick(bpy.context)
    check(uv._view["key"] == key, "and keeps it while nothing changes")
    uv.clear_seams(box)
    uv.tick(bpy.context)
    check(uv._view["key"] != key, "a change rebuilds it")
    operators._on_data_swap()
    check(uv._view["arrays"] is None, "undo forgets it")
    menus.close_sheet(bpy.context)
    settings.model_page = 'SHAPE'


def seamed_cube(name="Isles", loc=(0.0, 0.0, 0.0), subdivisions=2):
    """A cube unwrapped from its 12 edges: six islands, one per side."""
    box = cube(0.2, loc, name, subdivisions=subdivisions)
    primitives.make_active(bpy.context, box)
    uv.seams_from_sharp(box)
    check(uv.unwrap(bpy.context, box), "unwrap the seamed cube")
    bpy.context.view_layer.update()
    return box


def side_faces(obj, axis, value):
    """Bool per face: on the side of a cube at this coordinate (local)."""
    fc = np.array([p.center[:] for p in obj.data.polygons])
    return np.abs(fc[:, axis] - value) < 1e-5


def loops_of(mesh, faces):
    return faces[uv._loop_faces(mesh)[0]]


def island_area(obj, faces):
    me = obj.data
    u = uvs(obj)
    tris = np.array([t.loops[:] for t in me.loop_triangles if faces[t.polygon_index]])
    d1, d2 = u[tris[:, 1]] - u[tris[:, 0]], u[tris[:, 2]] - u[tris[:, 0]]
    return float(np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]).sum() / 2)


def surface_direction(obj, faces):
    """Which way U runs along the surface on these faces (a flat side), as a world unit vector."""
    me = obj.data
    u = uvs(obj)
    co = np.array([v.co[:] for v in me.vertices])
    loop_v = uv._loop_faces(me)[1]
    tri = next(t.loops[:] for t in me.loop_triangles if faces[t.polygon_index])
    p = co[loop_v[list(tri)]]
    t = u[list(tri)]
    e1, e2 = p[1] - p[0], p[2] - p[0]
    u1, u2 = t[1] - t[0], t[2] - t[0]
    det = u1[0] * u2[1] - u1[1] * u2[0]
    du = (e1 * u2[1] - e2 * u1[1]) / det
    return du / np.linalg.norm(du)


def hold(ctx, ss, hand_idx, start, frames):
    """Hold the trigger through frames, each (aim location, aim rotation), then let go."""
    ss.trigger = 1.0
    ss.aim_loc[hand_idx], ss.aim_rot[hand_idx] = Vector(start[0]), Quaternion(start[1])
    op = tm.harness()
    r = op.invoke(ctx, tm.FakeEvent('XR_ACTION', 'PRESS', tm.FakeXR(start[0], start[1], hand=tm.actions.HANDS[hand_idx])))
    if r != {'RUNNING_MODAL'}:
        return op, r
    for loc, rot in frames:
        time.sleep(0.01)
        ss.aim_loc[hand_idx], ss.aim_rot[hand_idx] = Vector(loc), Quaternion(rot)
        op.modal(ctx, tm.FakeEvent('TIMER'))
    ss.trigger = 0.0
    time.sleep(0.01)
    return op, op.modal(ctx, tm.FakeEvent('TIMER'))


def rolled(degrees, steps=9):
    """Aim rotations from none to this roll about the controller's own axis, a few degrees a frame."""
    return [Quaternion((0.0, 0.0, 1.0), math.radians(degrees) * (i + 1) / steps) for i in range(steps)]


def test_islands():
    print("== islands: from UVs and seams")
    clear()
    ss, ctx, settings = setup()
    box = cube(0.2, (0, 0, 0), "Plain", subdivisions=1)
    primitives.make_active(bpy.context, box)
    me = box.data
    me.uv_layers.new(name="UVMap") if not len(me.uv_layers) else None
    me.uv_layers.active.data.foreach_set("uv", np.zeros(len(me.loops) * 2))
    check(len(np.unique(uv.island_labels(me))) == 1, "UVs all alike and no seams: one island")

    # A seam ring around the middle splits it before any unwrap. An open line doesn't.
    segs = edge_world(box)
    ring = (np.abs(segs[:, :, 2]) < 1e-5).all(1)
    line = ring & (segs[:, :, 1] < 1e-5).all(1) & (segs[:, :, 0] > -1e-5).all(1)
    uv.set_seams(me, line)
    labels = uv.island_labels(me)
    check(len(np.unique(labels)) == 1, "an open seam line separates nothing")
    check((uv.open_seams(me, labels) == line).all() and line.any(), "and it's the open one")
    uv.set_seams(me, ring)
    labels = uv.island_labels(me)
    check(len(np.unique(labels)) == 2, f"a closed ring makes two islands ({len(np.unique(labels))})")
    check(not uv.open_seams(me, labels).any(), "and no seam is open")
    fc = np.array([p.center[:] for p in me.polygons])
    check(len(set(labels[fc[:, 2] > 0])) == 1 and labels[fc[:, 2] > 0][0] != labels[fc[:, 2] < 0][0],
          "above and below the ring are the two")
    check(len(np.unique(uv.island_labels(me, use_seams=False))) == 1, "without seams, the UVs still join them")
    low = labels.min()
    before = uv.island_colors(labels)
    check(before.shape == (len(labels), 4) and (before[labels == low] == before[labels == low][0]).all(),
          "one color per island")
    check(not np.allclose(before[labels == low][0], before[labels != low][0]), "different islands, different colors")

    # After a real unwrap the UVs separate them, seams or not.
    uv.unwrap(bpy.context, box)
    uv.clear_seams(box)
    check(len(np.unique(uv.island_labels(me))) == 2, "unwrapped: the islands come from the UVs too")

    # A big mesh mustn't be slow to label: this runs while a seam is traced.
    dense = sphere("DenseIslands", 256, 128)
    t0 = time.perf_counter()
    n = len(np.unique(uv.island_labels(dense.data)))
    ms = (time.perf_counter() - t0) * 1000
    print(f"    {ms:.1f} ms for {len(dense.data.polygons)} faces, {n} islands")
    check(ms < 150, f"labels a {len(dense.data.polygons)}-face mesh quickly ({ms:.0f} ms)")
    bpy.data.objects.remove(dense)


def test_islands_view():
    print("== island view: tints the mesh, follows a seam stroke")
    clear()
    ss, ctx, settings = setup()
    box = cube(0.2, (0, 0, 0), "IslandView", subdivisions=1)
    primitives.make_active(bpy.context, box)
    me = box.data
    check(uv.build_view(bpy.context, box, 'CHECKER') is None or len(me.uv_layers), "checker needs UVs")
    settings.model_page = 'UV'
    menus.open_sheet('OBJECT')
    settings.uv_view = 'ISLANDS'
    uv.tick(bpy.context)
    arrays = uv._view["arrays"]
    check(arrays is not None and len(arrays[2]) == len(me.loops), "built, one color per corner")
    check(uv._view["count"] == 1 and uv.island_note(settings) == "1 island", f"counted ({uv.island_note(settings)!r})")

    # The ring around the middle traced with the trigger, seen during the stroke.
    settings.uv_tool = 'SEAM'
    settings.uv_live = False
    uv.tick(bpy.context)
    check(not uv.busy(bpy.context), "the timer's not hurried without a stroke")
    operators.active_hands.add(1)
    try:
        check(uv.busy(bpy.context), "a seam stroke hurries the timer")
        segs = edge_world(box)
        ring = (np.abs(segs[:, :, 2]) < 1e-5).all(1)
        half = ring & (segs[:, :, 1] < 1e-5).all(1)
        uv.set_seams(me, half)
        uv._view["next"] = 0.0
        uv.tick(bpy.context)
        check("open seam" in uv.island_note(settings), f"half a ring: open ({uv.island_note(settings)!r})")
        uv.set_seams(me, ring)
        uv._view["next"] = time.monotonic() + 100.0
        uv.tick(bpy.context)
        check(uv._view["count"] == 1, "rebuilds are spaced out during the stroke")
        uv._view["next"] = 0.0
        uv.tick(bpy.context)
        check(uv._view["count"] == 2, f"the whole ring: two islands, during the stroke ({uv._view['count']})")
        check(uv.island_note(settings) == "2 islands", f"none open ({uv.island_note(settings)!r})")
        tint = uv._view["arrays"][2]
        check(len(np.unique(tint, axis=0)) == 2, "and two colors on the mesh")
        settings.uv_view = 'CHECKER'
        check(not uv.busy(bpy.context), "the other views don't follow a stroke")
        key = uv._view["key"]
        uv.tick(bpy.context)
        check(uv._view["key"] == key, "so they wait for it to end")
    finally:
        operators.active_hands.discard(1)
    # Modifiers changing the faces: labels come from the shown mesh.
    settings.uv_view = 'ISLANDS'
    settings.uv_tool = 'NONE'
    box.modifiers.new("Sub", 'SUBSURF').levels = 1
    bpy.context.view_layer.update()
    uv.tick(bpy.context)
    arrays = uv._view["arrays"]
    check(arrays is not None and len(np.unique(arrays[2], axis=0)) == 2 and uv._view["count"] == 2,
          f"under a Subsurf, still two islands ({len(np.unique(arrays[2], axis=0)) if arrays else None})")
    box.modifiers.clear()
    operators._on_data_swap()
    check(uv._view["open"] is None, "undo forgets it")
    menus.close_sheet(bpy.context)
    settings.uv_view = 'CHECKER'
    settings.model_page = 'SHAPE'


def test_twist():
    print("== Twist: a wrist roll turns the island on the surface")
    clear()
    ss, ctx, settings = setup()
    box = seamed_cube("Twist")
    me = box.data
    settings.uv_tool = 'TWIST'
    settings.uv_repack = False
    settings.uv_twist_snap = True
    top = side_faces(box, 2, 0.1)
    top_loops = loops_of(me, top)
    before = uvs(box).copy()
    dir0 = surface_direction(box, top)
    point = (0.02, 0.03, 0.11)

    # The controller points down at the top side, seen from above: rolling it counterclockwise (seen from
    # behind the hand, from above) by 30 degrees turns the texture counterclockwise by 30 degrees.
    op, r = hold(ctx, ss, 1, (point, Quaternion()), [(point, q) for q in rolled(32.0)])
    check(type(op.handler).__name__ == "TwistIsland" and r == {'FINISHED'}, f"the trigger twists ({r})")
    check(abs(op.handler.angle - math.radians(30.0)) < 1e-6, f"32 degrees snaps to 30 ({math.degrees(op.handler.angle):.1f})")
    check(op.handler.faces[top].all() and op.handler.faces.sum() == top.sum(), "it takes the whole island, only")
    after = uvs(box)
    check(np.abs(after[~top_loops] - before[~top_loops]).max() < 1e-6, "the other islands stay")
    dir1 = surface_direction(box, top)
    cross = dir0[0] * dir1[1] - dir0[1] * dir1[0]
    angle = math.degrees(math.atan2(cross, float(dir0 @ dir1)))
    check(abs(angle - 30.0) < 0.1, f"on the surface, the texture turned 30 degrees the hand's way ({angle:.2f})")
    check(me.get(uv.ORIENTED) and "Turned 30" in uv.message(), f"remembered, and said ({uv.message()!r})")

    # An island flipped in the UV map still turns the hand's way.
    u = uvs(box).copy()
    tl = u[top_loops]
    u[top_loops] = tl * np.array((-1.0, 1.0)) + np.array((tl[:, 0].max() + tl[:, 0].min(), 0.0))
    me.uv_layers.active.data.foreach_set("uv", u.ravel())
    uv.changed()
    dir0 = surface_direction(box, top)
    op, r = hold(ctx, ss, 1, (point, Quaternion()), [(point, q) for q in rolled(45.0)])
    dir1 = surface_direction(box, top)
    cross = dir0[0] * dir1[1] - dir0[1] * dir1[0]
    angle = math.degrees(math.atan2(cross, float(dir0 @ dir1)))
    check(op.handler.sign == -1.0 and abs(angle - 45.0) < 0.1, f"a flipped island too ({op.handler.sign}, {angle:.2f})")

    # Past half a turn, in small steps.
    settings.uv_twist_snap = False
    op, r = hold(ctx, ss, 1, (point, Quaternion()), [(point, q) for q in rolled(170.0, 20)])
    check(abs(math.degrees(op.handler.angle) - 170.0) < 0.1, f"no snap: exact ({math.degrees(op.handler.angle):.2f})")
    # Roll adds up: past the half turn a single pose can tell.
    grab = uv.TwistIsland(bpy.context, box, 0, uv.island_labels(me, use_seams=False), tm.actions.HANDS[1],
                          Matrix.Identity(4))
    for deg in range(20, 261, 20):
        grab.update(bpy.context, Quaternion((0.0, 0.0, 1.0), math.radians(deg)).to_matrix().to_4x4(), None, 1.0)
    check(abs(math.degrees(grab.total) - 260.0) < 1e-3, f"three quarters of a turn adds up ({math.degrees(grab.total):.1f})")
    grab.abandon()
    check(uv._grab[0] is None, "abandoned: no preview left")

    # Nothing rolled: nothing happens. A controller held off-axis but not rolled counts nothing.
    before = uvs(box).copy()
    tilt = Quaternion((1.0, 0.0, 0.0), math.radians(40.0))
    op, r = hold(ctx, ss, 1, (point, Quaternion()), [(point, Quaternion()), (point, tilt)])
    check(r == {'CANCELLED'} and not op.handler.changed and np.abs(uvs(box) - before).max() == 0.0,
          f"a tilt isn't a roll ({r})")

    # Nothing under the tip: no cube made, nothing grabbed.
    count = len(bpy.data.objects)
    op, r = hold(ctx, ss, 1, ((2.0, 2.0, 2.0), Quaternion()), [((2.0, 2.0, 2.0), Quaternion())])
    check(type(op.handler).__name__ == "NoIsland" and r == {'CANCELLED'} and len(bpy.data.objects) == count,
          "away from the surface: nothing happens")
    check("Touch" in uv.message(), f"and it says so ({uv.message()!r})")

    # Pack keeps a hand-turned island where it points.
    settings.uv_repack = True
    settings.uv_twist_snap = True
    before = surface_direction(box, top)
    op, r = hold(ctx, ss, 1, (point, Quaternion()), [(point, q) for q in rolled(45.0)])
    check(r == {'FINISHED'} and uv.message().startswith("Turned 45"), f"repacked after the stroke ({uv.message()!r})")
    u = uvs(box)
    check(u.min() >= -1e-6 and u.max() <= 1 + 1e-6, f"in the UV square ({u.min():.3f}..{u.max():.3f})")
    after = surface_direction(box, top)
    cross = before[0] * after[1] - before[1] * after[0]
    angle = math.degrees(math.atan2(cross, float(before @ after)))
    check(abs(angle - 45.0) < 0.5, f"the repack keeps the turn ({angle:.2f})")
    check(box.mode == 'OBJECT' and bpy.context.view_layer.objects.active == box, "back in Object Mode")
    turned = surface_direction(box, top)
    check(uv.pack(bpy.context, box), "the Pack button")
    after = surface_direction(box, top)
    check(np.abs(after - turned).max() < 1e-3, "Pack leaves a turned island turned")
    check(uv.average_scale(bpy.context, box), "Even out")
    after = surface_direction(box, top)
    check(np.abs(after - turned).max() < 1e-3, "and Even out")
    check(uv.unwrap(bpy.context, box) and uv.ORIENTED not in me, "a new unwrap starts over")

    # The left hand does the same, a right-handed user's off hand.
    settings.uv_repack = False
    op, r = hold(ctx, ss, 0, (point, Quaternion()), [(point, q) for q in rolled(15.0, 3)])
    check(type(op.handler).__name__ == "TwistIsland" and r == {'FINISHED'}, "either hand twists")
    settings.uv_tool = 'NONE'


def test_size():
    print("== Size: raising the hand gives an island more of the UV square")
    clear()
    ss, ctx, settings = setup()
    box = seamed_cube("Size")
    me = box.data
    settings.uv_tool = 'SIZE'
    settings.uv_repack = False
    top = side_faces(box, 2, 0.1)
    side = side_faces(box, 0, 0.1)
    start = (0.02, 0.03, 0.11)
    a0, b0 = island_area(box, top), island_area(box, side)
    center0 = uvs(box)[loops_of(me, top)]
    center0 = (center0.max(0) + center0.min(0)) / 2

    # Up by one doubling.
    up = (start[0], start[1], start[2] + uv.SIZE_DOUBLE)
    op, r = hold(ctx, ss, 1, (start, Quaternion()), [(up, Quaternion())])
    check(type(op.handler).__name__ == "SizeIsland" and r == {'FINISHED'}, f"the trigger sizes ({r})")
    check(abs(op.handler.factor - 2.0) < 1e-3, f"10 cm up: twice as big ({op.handler.factor:.3f})")
    a1, b1 = island_area(box, top), island_area(box, side)
    check(abs(a1 / a0 - 4.0) < 0.01 and abs(b1 / b0 - 1.0) < 1e-6, f"four times the area, the rest as they were "
          f"({a1 / a0:.3f}, {b1 / b0:.3f})")
    loops = uvs(box)[loops_of(me, top)]
    check(np.abs((loops.max(0) + loops.min(0)) / 2 - center0).max() < 1e-5, "about its center")
    check(uv.ORIENTED not in me, "nothing turned")

    # Down, and back to where it started.
    down = (start[0], start[1], start[2] - uv.SIZE_DOUBLE)
    op, r = hold(ctx, ss, 1, (start, Quaternion()), [(down, Quaternion())])
    check(abs(op.handler.factor - 0.5) < 1e-3 and abs(island_area(box, top) / a0 - 1.0) < 0.01, "down halves it again")

    # A small shake isn't a change; nor does the range run away.
    op, r = hold(ctx, ss, 1, (start, Quaternion()), [(start, Quaternion())])
    check(r == {'CANCELLED'}, "no move: no change")
    grab = uv.SizeIsland(bpy.context, box, 0, uv.island_labels(me, use_seams=False),
                         Matrix.Translation((0.0, 0.0, 0.0)), 1.0)
    grab.update(bpy.context, None, Matrix.Translation((0.0, 0.0, 5.0)), 1.0)
    check(grab.factor == uv.SIZE_MAX, f"kept in range ({grab.factor})")
    grab.update(bpy.context, None, Matrix.Translation((0.0, 0.0, -5.0)), 1.0)
    check(grab.factor == uv.SIZE_MIN, f"both ways ({grab.factor})")
    grab.abandon()

    # With the viewer scaled, a move of the same size on the headset's scale.
    ss.viewer_scale = 2.0
    far = (start[0], start[1], start[2] + 2.0 * uv.SIZE_DOUBLE)
    op, r = hold(ctx, ss, 1, (start, Quaternion()), [(far, Quaternion())])
    check(abs(op.handler.factor - 2.0) < 1e-3, f"hand-space distance ({op.handler.factor:.3f})")
    ss.viewer_scale = 1.0

    # Repack: its share against the others survives, in the UV square, and nobody turns.
    settings.uv_repack = True
    uv.unwrap(bpy.context, box)
    dirs = surface_direction(box, side)
    a0, b0 = island_area(box, top), island_area(box, side)
    op, r = hold(ctx, ss, 1, (start, Quaternion()), [(up, Quaternion())])
    a1, b1 = island_area(box, top), island_area(box, side)
    ratio = (a1 / b1) / (a0 / b0)
    check(r == {'FINISHED'} and abs(ratio - 4.0) < 0.05, f"repacked: four times the others' share ({ratio:.3f})")
    u = uvs(box)
    check(u.min() >= -1e-6 and u.max() <= 1 + 1e-6, f"in the UV square ({u.min():.3f}..{u.max():.3f})")
    check(np.abs(surface_direction(box, side) - dirs).max() < 1e-3, "and the others didn't turn")
    check("size" in uv.message(), f"said ({uv.message()!r})")

    # The only island: packing would size it back.
    clear()
    ball = sphere("OneIsland")
    segs = edge_world(ball)
    uv.set_seams(ball.data, (np.abs(segs[:, :, 1]) < 1e-6).all(1) & (segs[:, :, 0] > -1e-6).all(1))
    uv.unwrap(bpy.context, ball)
    bpy.context.view_layer.update()
    before = uvs(ball).copy()
    op, r = hold(ctx, ss, 1, ((0.0, -0.1, 0.0), Quaternion()), [((0.0, -0.1, 0.1), Quaternion())])
    check(type(op.handler).__name__ == "NoIsland" and r == {'CANCELLED'} and "one island" in uv.message(),
          f"one island: says so ({uv.message()!r})")
    check(np.abs(uvs(ball) - before).max() == 0.0, "and leaves it")

    # No UVs yet.
    bare = cube(0.2, (1.0, 0.0, 0.0), "Bare", subdivisions=1)
    primitives.make_active(bpy.context, bare)
    if len(bare.data.uv_layers):
        bare.data.uv_layers.active.data.foreach_set("uv", np.zeros(len(bare.data.loops) * 2))
    bpy.context.view_layer.update()
    op, r = hold(ctx, ss, 1, ((1.0, 0.0, 0.11), Quaternion()), [((1.0, 0.0, 0.11), Quaternion())])
    check(type(op.handler).__name__ == "NoIsland" and "No UVs" in uv.message(), f"no UVs: says so ({uv.message()!r})")
    settings.uv_tool = 'NONE'
    settings.uv_repack = True


def test_hover():
    print("== the island under the tip")
    clear()
    ss, ctx, settings = setup()
    box = seamed_cube("Hover")
    top = side_faces(box, 2, 0.1)
    settings.uv_tool = 'TWIST'
    settings.model_page = 'UV'
    menus.open_sheet('OBJECT')
    uv.tick(bpy.context)
    labels = uv._hover["labels"]
    check(labels is not None and len(np.unique(labels)) == 6, f"labels from the UVs: six islands "
          f"({len(np.unique(labels)) if labels is not None else None})")
    face = uv.face_under(box, Vector((0.02, 0.03, 0.11)), uv.ISLAND_REACH, uv._hover["centers"])
    check(face is not None and top[face], "the tip finds the side it's on")
    check(uv.face_under(box, Vector((0.02, 0.03, 0.5)), uv.ISLAND_REACH, None) is None, "not from far away")
    pos, u, color, tris = uv._island_arrays(box, top, (*uv.ISLAND_COLOR, uv.HOVER_ALPHA))
    check(len(tris) == 2 * top.sum() and tris.max() < len(pos) == len(u) == len(color),
          f"one island's arrays ({len(tris)} triangles, {len(pos)} corners)")
    # Under a modifier that remakes the faces, the face is still one of the mesh's.
    box.modifiers.new("Sub", 'SUBSURF').levels = 1
    bpy.context.view_layer.update()
    centers = uv._centers_if_remade(box)
    face = uv.face_under(box, Vector((0.02, 0.03, 0.11)), uv.ISLAND_REACH, centers)
    check(centers is not None and face is not None and face < len(box.data.polygons) and top[face],
          "under a Subsurf, it's found by position")
    box.modifiers.clear()
    bpy.context.view_layer.update()
    settings.uv_tool = 'NONE'
    uv.tick(bpy.context)
    check(uv._hover["labels"] is None, "dropped with the tool")
    menus.close_sheet(bpy.context)
    settings.model_page = 'SHAPE'


def test_primitive_rebuild():
    print("== seams and UVs survive a resize")
    clear()
    box = cube(0.1, (0, 0, 0), "Resize")
    uv.smart_project(bpy.context, box)
    s = uv.seams(box.data).copy()
    u = uvs(box).copy()
    box.vr_prim.size = (0.1, 0.1, 0.15)
    check((uv.seams(box.data) == s).all(), "resizing keeps the seams")
    check(np.abs(uvs(box) - u).max() < 1e-6, "and the UVs")
    box.vr_prim.subdivisions = 1
    check(not uv.seams(box.data).any(), "new topology drops them")


def test_panel():
    print("== UV page")
    clear()
    ss, ctx, settings = setup()
    box = cube(0.1, (0, 0, 0), "PanelUV")
    primitives.make_active(bpy.context, box)
    settings.model_page = 'UV'
    menus.open_sheet('OBJECT')
    menus.set_more('OBJECT_UV', True)
    widgets, _h = menus.layout(ctx)
    ids = {w.id: w for w in widgets if w.id}
    want = ("page_uv", "uv_tool_seam", "uv_tool_fix", "uv_tool_twist", "uv_tool_size", "uv_view_checker",
            "uv_view_stretch", "uv_view_islands", "uv_unwrap", "uv_smart", "uv_pack", "uv_method_conformal", "uv_live",
            "uv_repack", "uv_twist_snap", "uv_checker", "uv_fix_radius", "uv_from_sharp", "uv_average",
            "uv_clear_seams")
    missing = [k for k in want if k not in ids]
    check(not missing, f"UV page widgets ({missing or 'all there'})")
    settings.uv_view = 'NONE'
    ids["uv_tool_twist"].on_press()
    check(settings.uv_tool == 'TWIST' and settings.uv_view == 'CHECKER', "Twist shows a checker if nothing's shown")
    settings.uv_tool = 'NONE'
    settings.uv_view = 'STRETCH'
    widgets, _h = menus.layout(ctx)
    {w.id: w for w in widgets if w.id}["uv_tool_size"].on_press()
    check(settings.uv_tool == 'SIZE' and settings.uv_view == 'STRETCH', "and leaves another view alone")
    settings.uv_tool = 'NONE'
    settings.uv_view = 'CHECKER'
    widgets, _h = menus.layout(ctx)
    ids = {w.id: w for w in widgets if w.id}
    ids["uv_tool_seam"].on_press()
    check(settings.uv_tool == 'SEAM', "Seams turns the tool on")
    widgets, _h = menus.layout(ctx)
    {w.id: w for w in widgets if w.id}["uv_tool_seam"].on_press()
    check(settings.uv_tool == 'NONE', "pressing it again turns it off")
    settings.uv_tool = 'FIX'
    widgets, _h = menus.layout(ctx)
    {w.id: w for w in widgets if w.id}["page_shape"].on_press()
    check(settings.uv_tool == 'NONE', "leaving the UV page turns the tool off")
    settings.model_page = 'UV'
    settings.uv_tool = 'SEAM'
    menus.close_sheet(bpy.context)
    check(settings.uv_tool == 'NONE', "closing the object's sheet turns it off")
    settings.uv_tool = 'FIX'
    menus.set_mode(bpy.context, 'DRAW')
    check(settings.uv_tool == 'NONE', "so does switching mode")
    menus.set_mode(bpy.context, 'MODEL')
    primitives.make_active(bpy.context, box)
    menus.open_sheet('OBJECT')
    widgets, _h = menus.layout(ctx)
    {w.id: w for w in widgets if w.id}["uv_unwrap"].on_press()
    check(not box.data.get("vrs_unwrapped"), "Unwrap with no seams on a closed mesh does nothing")
    check("closed" in uv.message(), f"a closed mesh with no seams says so ({uv.message()!r})")
    uv.seams_from_sharp(box)
    widgets, _h = menus.layout(ctx)
    {w.id: w for w in widgets if w.id}["uv_unwrap"].on_press()
    check(box.data.get("vrs_unwrapped") and uv.message().startswith("Unwrapped") and uv.uv_area(box.data) > 0.1,
          f"Unwrap button: with seams it unwraps "
          f"({uv.message()!r})")
    settings.model_page = 'SHAPE'
    menus.reset()


def main():
    vr_sculpt.register()
    try:
        for test in (test_seams, test_fix, test_buttons, test_stretch, test_view, test_islands, test_islands_view,
                     test_twist, test_size, test_hover, test_primitive_rebuild, test_panel):
            try:
                test()
            except Exception:
                traceback.print_exc()
                failures.append(test.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
