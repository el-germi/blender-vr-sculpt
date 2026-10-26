"""Probe: what Blender's dynamic topology does to mesh data, and how it reacts to our native strokes.

blender --factory-startup --python tests/probe_dyntopo.py

Writes tests/probe_dyntopo_result.txt. The mesh gets layers whose values are linear functions of the
vertex position (UVs, colors, weights, mask) or of the face center (face sets, materials), so after
dyntopo splits edges every new value can be checked against the function at its new position.
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "probe_dyntopo_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def quit_cleanly():
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def positions(me):
    a = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def build():
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=1.0)
    obj = bpy.context.active_object
    me = obj.data
    co = positions(me)
    loop_v = np.array([l.vertex_index for l in me.loops])
    uv = me.uv_layers.new(name="Probe")
    uv_vals = np.stack([co[loop_v, 0] * 0.5 + 0.5, co[loop_v, 1] * 0.5 + 0.5], axis=1)
    uv.data.foreach_set("uv", uv_vals.ravel())
    col = me.color_attributes.new("PointCol", 'FLOAT_COLOR', 'POINT')
    cv = np.ones((len(co), 4))
    cv[:, 0] = co[:, 0] * 0.5 + 0.5
    col.data.foreach_set("color", cv.ravel())
    ccol = me.color_attributes.new("CornerCol", 'BYTE_COLOR', 'CORNER')
    cc = np.ones((len(loop_v), 4))
    cc[:, 1] = co[loop_v, 2] * 0.5 + 0.5
    ccol.data.foreach_set("color", cc.ravel())
    vg = obj.vertex_groups.new(name="Bone")
    for i, p in enumerate(co):
        vg.add([i], float(p[2] * 0.5 + 0.5), 'REPLACE')
    mask = me.attributes.new(".sculpt_mask", 'FLOAT', 'POINT')
    mask.data.foreach_set("value", np.clip(co[:, 2], 0, 1))
    rest = me.attributes.new("rest_position", 'FLOAT_VECTOR', 'POINT')
    rest.data.foreach_set("vector", co.ravel())
    custom = me.attributes.new("my_float", 'FLOAT', 'POINT')
    custom.data.foreach_set("value", co[:, 0])
    # Edges on the equator: crease, sharp, seam, bevel weight.
    ev = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    eq = (np.abs(co[ev[:, 0], 2]) < 1e-4) & (np.abs(co[ev[:, 1], 2]) < 1e-4)
    for name in ("crease_edge", "bevel_weight_edge"):
        a = me.attributes.new(name, 'FLOAT', 'EDGE')
        a.data.foreach_set("value", eq.astype(np.float64))
    a = me.attributes.new("sharp_edge", 'BOOLEAN', 'EDGE')
    a.data.foreach_set("value", eq)
    for e, s in zip(me.edges, eq):
        e.use_seam = bool(s)
    centers = np.array([p.center for p in me.polygons])
    fs = me.attributes.new(".sculpt_face_set", 'INT', 'FACE')
    fs.data.foreach_set("value", np.where(centers[:, 0] > 0, 1, 2))
    for name in ("Red", "Blue"):
        me.materials.append(bpy.data.materials.new(name))
    me.polygons.foreach_set("material_index", np.where(centers[:, 1] > 0, 0, 1))
    me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), dtype=bool))
    log(f"built: {len(me.vertices)} verts, {len(me.polygons)} faces, {int(eq.sum())} equator edges")
    return obj


def report(obj, label):
    me = obj.data
    co = positions(me)
    log(f"--- {label}: {len(me.vertices)} verts, {len(me.edges)} edges, {len(me.polygons)} faces,"
        f" tris only: {all(p.loop_total == 3 for p in me.polygons)}")
    log("  attributes:", sorted(a.name for a in me.attributes))
    log("  uv layers:", [u.name for u in me.uv_layers], "| vertex groups:", [g.name for g in obj.vertex_groups],
        "| materials:", [m.name if m else None for m in me.materials])
    loop_v = np.array([l.vertex_index for l in me.loops])
    if me.uv_layers.get("Probe"):
        uv = np.empty(len(me.loops) * 2)
        me.uv_layers["Probe"].data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2)
        err = np.abs(uv - (co[loop_v, :2] * 0.5 + 0.5)).max()
        bad = (np.abs(uv - (co[loop_v, :2] * 0.5 + 0.5)) > 0.02).any(axis=1)
        log(f"  UV max error vs planar projection: {err:.4f}, {bad.mean():.4f} of corners off by > 0.02",
            "| distinct values:", len(np.unique(uv.round(4), axis=0)), "first:", uv[0].round(3).tolist())
    for name, dom in (("PointCol", 'POINT'), ("CornerCol", 'CORNER')):
        a = me.color_attributes.get(name)
        if a is None:
            log(f"  {name}: missing")
            continue
        n = len(co) if dom == 'POINT' else len(loop_v)
        v = np.empty(n * 4)
        a.data.foreach_get("color", v)
        v = v.reshape(-1, 4)
        if dom == 'POINT':
            err = np.abs(v[:, 0] - (co[:, 0] * 0.5 + 0.5)).max()
            bad = np.abs(v[:, 0] - (co[:, 0] * 0.5 + 0.5)) > 0.02
        else:
            d = np.abs(v[:, 1] - (co[loop_v, 2] * 0.5 + 0.5))
            err = d.max()
            bad = d > 0.02
            if bad.any():
                log(f"    bad corner samples (got, want):",
                    [(round(float(v[i, 1]), 3), round(float(co[loop_v[i], 2] * 0.5 + 0.5), 3))
                     for i in np.flatnonzero(bad)[:6]])
        log(f"  {name}: max error {err:.4f}, {bad.mean():.4f} off by > 0.02")
    if obj.vertex_groups:
        w = np.full(len(co), -1.0)
        for v in me.vertices:
            for g in v.groups:
                w[v.index] = g.weight
        missing = int((w < 0).sum())
        has = w >= 0
        err = np.abs(w[has] - (co[has, 2] * 0.5 + 0.5)).max() if has.any() else -1
        log(f"  weights: {missing} verts without weight, max error {err:.4f}")
    for name, f in ((".sculpt_mask", lambda c: np.clip(c[:, 2], 0, 1)), ("my_float", lambda c: c[:, 0])):
        a = me.attributes.get(name)
        if a is not None:
            v = np.empty(len(co))
            a.data.foreach_get("value", v)
            log(f"  {name}: max error {np.abs(v - f(co)).max():.4f}")
    a = me.attributes.get("rest_position")
    if a is not None:
        rest = np.empty(len(co) * 3)
        a.data.foreach_get("vector", rest)
        rest = rest.reshape(-1, 3)
        moved = np.linalg.norm(co - rest, axis=1)
        log(f"  rest_position: off the original sphere by max {np.abs(np.linalg.norm(rest, axis=1) - 1).max():.4f},"
            f" vertices moved from it by max {moved.max():.4f}")
    ev = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    eq = (np.abs(co[ev[:, 0], 2]) < 1e-3) & (np.abs(co[ev[:, 1], 2]) < 1e-3)
    for name in ("crease_edge", "bevel_weight_edge", "sharp_edge"):
        a = me.attributes.get(name)
        if a is None:
            log(f"  {name}: missing")
            continue
        v = np.empty(len(me.edges), dtype=bool if a.data_type == 'BOOLEAN' else np.float64)
        a.data.foreach_get("value", v)
        on = v > 0.5
        log(f"  {name}: {int(on.sum())} edges set, {int((on & eq).sum())} on equator,"
            f" {int((~on & eq).sum())} equator edges unset (equator edges now {int(eq.sum())})")
    seams = np.array([e.use_seam for e in me.edges])
    log(f"  seams: {int(seams.sum())} set, {int((seams & eq).sum())} on equator")
    centers = np.array([p.center for p in me.polygons])
    a = me.attributes.get(".sculpt_face_set")
    if a is not None:
        v = np.empty(len(me.polygons), dtype=np.int64)
        a.data.foreach_get("value", v)
        off = np.abs(centers[:, 0]) > 0.05
        good = (v == np.where(centers[:, 0] > 0, 1, 2))[off].mean()
        log(f"  face sets: values {sorted(set(v.tolist()))}, {good:.3f} of faces away from x=0 correct")
    mi = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("material_index", mi)
    off = np.abs(centers[:, 1]) > 0.05
    log(f"  material index: values {sorted(set(mi.tolist()))},"
        f" {(mi == np.where(centers[:, 1] > 0, 0, 1))[off].mean():.3f} correct")
    sm = np.empty(len(me.polygons), dtype=bool)
    me.polygons.foreach_get("use_smooth", sm)
    log(f"  smooth faces: {sm.mean():.3f}")


def stats():
    s = bpy.context.scene.statistics(bpy.context.view_layer)
    return s


def main():
    import vr_sculpt
    from vr_sculpt import native
    vr_sculpt.register()
    try:
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        obj = build()
        report(obj, "before")
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0, 0, 0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 6.0
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            sc = bpy.context.tool_settings.sculpt
            log("defaults:", sc.detail_type_method, sc.detail_refine_method, sc.detail_size,
                sc.detail_percent, sc.constant_detail_resolution)
            log("toggle poll:", bpy.ops.sculpt.dynamic_topology_toggle.poll())
            r = bpy.ops.sculpt.dynamic_topology_toggle()
            log("toggle on:", r, "use_dynamic_topology_sculpting =", obj.use_dynamic_topology_sculpting)
            log("stats after toggle:", stats())
            report(obj, "obj.data right after dyntopo on (still in sculpt)")
            # Flood fill to constant detail: every edge split or collapsed to the target length.
            sc.detail_type_method = 'CONSTANT'
            sc.constant_detail_resolution = 12.0
            log("flood fill poll:", bpy.ops.sculpt.detail_flood_fill.poll())
            log("flood fill:", bpy.ops.sculpt.detail_flood_fill())
            log("stats after flood fill:", stats())
            log("obj.data verts after flood fill (in sculpt):", len(obj.data.vertices))
        bpy.ops.wm.redraw_timer(type='DRAW_WIN', iterations=1)

        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='OBJECT')
        report(obj, "after flood fill, back in Object Mode")
        log("dyntopo flag kept in object mode:", obj.use_dynamic_topology_sculpting)

        # Native strokes with dyntopo on: does obj.data follow, and what do new verts get?
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            log("re-enter sculpt, dyntopo:", obj.use_dynamic_topology_sculpting, "|", stats())
            if not obj.use_dynamic_topology_sculpting:
                bpy.ops.sculpt.dynamic_topology_toggle()
            sc = bpy.context.tool_settings.sculpt
            for method, value in (('CONSTANT', 40.0), ('BRUSH', 10.0)):
                sc.detail_type_method = method
                sc.constant_detail_resolution = 40.0
                sc.detail_percent = value
                n0 = len(obj.data.vertices)
                s0 = stats()
                stroke = native.NativeStroke(bpy.context, obj, 0.2)
                for i in range(30):
                    a = -0.5 + i * 0.03
                    tip = Vector((np.sin(a), -np.cos(a), -0.4)).normalized() * 1.0
                    stroke.update(tip, None, 1.0)
                    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
                stroke.finish()
                log(f"stroke with {method}: {stroke.dabs} dabs, {stroke.dropped} dropped |"
                    f" stats {s0} -> {stats()} | obj.data verts {n0} -> {len(obj.data.vertices)}")
                ev = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                log("  evaluated mesh verts:", len(ev.data.vertices))
            # A second stroke over the first stroke's new geometry.
            stroke = native.NativeStroke(bpy.context, obj, 0.1)
            before = stats()
            for i in range(20):
                a = -0.3 + i * 0.03
                stroke.update(Vector((np.sin(a), -np.cos(a), -0.4)).normalized() * 1.05, None, 1.0)
            stroke.finish()
            log(f"stroke over new geometry: {stroke.dabs} dabs, {stroke.dropped} dropped | {before} -> {stats()}")
            log("undo push of the strokes: ed.undo_push ok")
            bpy.ops.object.mode_set(mode='OBJECT')
        report(obj, "after native dyntopo strokes, Object Mode")

        # Dyntopo off again.
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            if obj.use_dynamic_topology_sculpting:
                log("toggle off:", bpy.ops.sculpt.dynamic_topology_toggle())
            log("after off:", obj.use_dynamic_topology_sculpting, "|", stats())
            bpy.ops.object.mode_set(mode='OBJECT')
        report(obj, "after dyntopo off")

        # Shape keys and modifiers: does the toggle refuse?
        bpy.ops.object.shape_key_add(from_mix=False)
        bpy.ops.object.shape_key_add(from_mix=False)
        kb = obj.data.shape_keys.key_blocks[1]
        for d in kb.data:
            d.co = d.co * 1.2
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            log("with shape key, toggle poll:", bpy.ops.sculpt.dynamic_topology_toggle.poll())
            try:
                log("  toggle:", bpy.ops.sculpt.dynamic_topology_toggle(), obj.use_dynamic_topology_sculpting)
                if obj.use_dynamic_topology_sculpting:
                    bpy.context.tool_settings.sculpt.constant_detail_resolution = 4.0
                    bpy.context.tool_settings.sculpt.detail_type_method = 'CONSTANT'
                    log("  flood fill:", bpy.ops.sculpt.detail_flood_fill())
                    bpy.ops.sculpt.dynamic_topology_toggle()
            except RuntimeError as ex:
                log("  toggle error:", str(ex).strip())
            bpy.ops.object.mode_set(mode='OBJECT')
        keys = obj.data.shape_keys
        log("  after: verts", len(obj.data.vertices), "| shape keys:",
            [(k.name, len(k.data)) for k in keys.key_blocks] if keys else None)
        if keys and len(keys.key_blocks) > 1:
            basis = np.array([d.co for d in keys.key_blocks[0].data])
            key = np.array([d.co for d in keys.key_blocks[1].data])
            err = np.linalg.norm(key - basis * 1.2, axis=1)
            same = np.linalg.norm(key - basis, axis=1) < 1e-5
            log(f"  Key 1 vs basis * 1.2: max error {err.max():.4f}, {(err < 0.01).mean():.3f} of verts keep the"
                f" offset, {same.mean():.3f} equal the basis")
        obj.shape_key_clear()
        obj.modifiers.new("Sub", 'SUBSURF')
        obj.modifiers.new("Multi", 'MULTIRES')
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            try:
                log("with multires:", bpy.ops.sculpt.dynamic_topology_toggle(), obj.use_dynamic_topology_sculpting)
            except RuntimeError as ex:
                log("with multires, toggle error:", str(ex).strip())
            bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        log(traceback.format_exc())
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
