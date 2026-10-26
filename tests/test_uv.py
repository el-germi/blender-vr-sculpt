"""Background tests for the UV page: seams marked with the trigger (and joined along the mesh), live
unwrap, the Fix brush's partial unwrap, Smart UV / Pack / Seams from Sharp, the stretch measure, the
view's arrays, seams kept through primitive resizes, and the panel page.

blender --background --factory-startup --python tests/test_uv.py
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
    out = uv.build_view(bpy.context, box, stretch=True)
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
    want = ("page_uv", "uv_tool_seam", "uv_tool_fix", "uv_view_checker", "uv_view_stretch", "uv_unwrap", "uv_smart",
            "uv_pack", "uv_method_conformal", "uv_live", "uv_checker", "uv_fix_radius", "uv_from_sharp",
            "uv_average", "uv_clear_seams")
    missing = [k for k in want if k not in ids]
    check(not missing, f"UV page widgets ({missing or 'all there'})")
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
        for test in (test_seams, test_fix, test_buttons, test_stretch, test_view, test_primitive_rebuild, test_panel):
            try:
                test()
            except Exception:
                traceback.print_exc()
                failures.append(test.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
