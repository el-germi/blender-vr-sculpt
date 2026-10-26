"""Probe: Blender's UV operators from a script, for the UV page (seams, live unwrap, the Fix brush).

blender --background --factory-startup --python tests/probe_uv.py

Writes tests/probe_uv_result.txt: how seams and pins are stored, which unwrap methods exist, whether a
partial unwrap with a pinned border keeps the rest of the layout (and the patch) in place, and how long
Edit Mode round trips and unwraps take on meshes of different sizes.
"""

import os
import time
import traceback

import bpy
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "probe_uv_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")


def uvs(me):
    a = np.empty(len(me.loops) * 2, dtype=np.float64)
    me.uv_layers.active.data.foreach_get("uv", a)
    return a.reshape(-1, 2)


def pins(me):
    layer = me.uv_layers.active
    if not len(layer.pin):
        me.attributes.new(".pn." + layer.name, 'BOOLEAN', 'CORNER')
        layer = me.uv_layers.active
    return layer.pin


def sphere(segments, rings, name):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=1.0)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def edit(obj):
    with bpy.context.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='EDIT')


def leave(obj):
    with bpy.context.temp_override(active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode='OBJECT')


def storage():
    log("== storage")
    obj = sphere(16, 8, "Store")
    me = obj.data
    log("attributes:", sorted(a.name for a in me.attributes))
    seam = np.zeros(len(me.edges), dtype=bool)
    seam[:5] = True
    me.edges.foreach_set("use_seam", seam)
    me.update()
    log("after use_seam set:", sorted(a.name for a in me.attributes))
    layer = me.uv_layers.active
    log("uv layer props:", [p.identifier for p in layer.bl_rna.properties])
    log("ops available:", [n for n in ("unwrap", "smart_project", "pack_islands", "average_islands_scale",
                                       "minimize_stretch", "pin", "select_all", "seams_from_islands",
                                       "mark_seam") if hasattr(bpy.ops.uv, n)])
    props = bpy.ops.uv.unwrap.get_rna_type().properties
    log("unwrap props:", [p.identifier for p in props])
    log("unwrap methods:", [i.identifier for i in props["method"].enum_items])
    bpy.data.objects.remove(obj)


def full_unwrap():
    log("== full unwrap in background, all methods")
    obj = sphere(32, 16, "Full")
    me = obj.data
    me.edges.foreach_set("use_seam", np.zeros(len(me.edges), dtype=bool))
    # A meridian seam: edges whose both ends have y ~ 0 and x >= 0.
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    ev = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    seam = (np.abs(co[ev, 1]) < 1e-4).all(1) & (co[ev, 0] > -1e-4).all(1)
    me.edges.foreach_set("use_seam", seam)
    log("seam edges:", int(seam.sum()))
    props = bpy.ops.uv.unwrap.get_rna_type().properties
    for method in [i.identifier for i in props["method"].enum_items]:
        try:
            t = time.perf_counter()
            edit(obj)
            bpy.ops.mesh.select_all(action='SELECT')
            with bpy.context.temp_override(active_object=obj, object=obj, edit_object=obj):
                r = bpy.ops.uv.unwrap(method=method, margin=0.01)
            leave(obj)
            u = uvs(me)
            log(f"  {method}: {r} {1000 * (time.perf_counter() - t):.1f} ms, uv range {u.min(0)} {u.max(0)}")
        except Exception as ex:
            log(f"  {method}: FAILED {ex!r}")
            if obj.mode != 'OBJECT':
                leave(obj)
    return obj


def partial(obj):
    log("== partial unwrap with a pinned border")
    me = obj.data
    before = uvs(me).copy()
    # Mangle a patch: the faces near (0, -1, 0); their UVs are squashed toward one point.
    fc = np.empty(len(me.polygons) * 3)
    me.polygons.foreach_get("center", fc)
    fc = fc.reshape(-1, 3)
    patch = np.linalg.norm(fc - np.array([0.0, -1.0, 0.0]), axis=1) < 0.45
    loop_f = np.repeat(np.arange(len(me.polygons)), [p.loop_total for p in me.polygons])
    loop_v = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", loop_v)
    inside_v = np.zeros(len(me.vertices), dtype=bool)
    inside_v[loop_v[patch[loop_f]]] = True
    outside_v = np.zeros(len(me.vertices), dtype=bool)
    outside_v[loop_v[~patch[loop_f]]] = True
    border_v = inside_v & outside_v
    interior_loops = patch[loop_f] & ~border_v[loop_v]
    mangled = before.copy()
    center = mangled[interior_loops].mean(0)
    mangled[interior_loops] = center + (mangled[interior_loops] - center) * 0.2
    me.uv_layers.active.data.foreach_set("uv", mangled.ravel())
    log("patch faces:", int(patch.sum()), "border verts:", int(border_v.sum()))

    layer = me.uv_layers.active
    has_pin = hasattr(layer, "pin")
    log("layer.pin exists:", has_pin, "len", len(layer.pin), "loops", len(me.loops))
    for name in (f".pn.{layer.name}", f"{layer.name}.pin", ".uv_pin"):
        if len(layer.pin):
            break
        attr = me.attributes.new(name, 'BOOLEAN', 'CORNER')
        log(f"  made {name}: pin len now {len(layer.pin)}")
        if not len(layer.pin):
            me.attributes.remove(attr)
    log("attributes now:", sorted(a.name for a in me.attributes))
    has_pin = len(layer.pin) == len(me.loops)
    for mode in ("pins", "no pins"):
        me.uv_layers.active.data.foreach_set("uv", mangled.ravel())
        if has_pin:
            pin = np.zeros(len(me.loops), dtype=bool)
            if mode == "pins":
                # Pin every corner of a border vertex, inside the patch and out, and everything outside.
                pin = ~interior_loops
            pins(me).foreach_set("value", pin)
        me.polygons.foreach_set("select", patch)
        me.update()
        try:
            t = time.perf_counter()
            edit(obj)
            with bpy.context.temp_override(active_object=obj, object=obj, edit_object=obj):
                r = bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.01)
            leave(obj)
            after = uvs(me)
            moved_out = np.abs(after[~patch[loop_f]] - mangled[~patch[loop_f]]).max()
            border_loops = patch[loop_f] & border_v[loop_v]
            moved_border = np.abs(after[border_loops] - mangled[border_loops]).max()
            err = np.abs(after[interior_loops] - before[interior_loops]).max()
            log(f"  {mode}: {r} {1000 * (time.perf_counter() - t):.1f} ms; outside moved {moved_out:.5f}, "
                f"border moved {moved_border:.5f}, interior vs. original {err:.4f} "
                f"(mangled was {np.abs(mangled[interior_loops] - before[interior_loops]).max():.4f})")
        except Exception as ex:
            log(f"  {mode}: FAILED {ex!r}")
            traceback.print_exc()
            if obj.mode != 'OBJECT':
                leave(obj)
    if has_pin:
        pins(me).foreach_set("value", np.zeros(len(me.loops), dtype=bool))
    log("  select state after: faces selected", sum(p.select for p in me.polygons))

    log("== minimize_stretch on the patch")
    me.uv_layers.active.data.foreach_set("uv", mangled.ravel())
    if has_pin:
        pins(me).foreach_set("value", ~interior_loops)
    me.polygons.foreach_set("select", patch)
    me.update()
    try:
        t = time.perf_counter()
        edit(obj)
        with bpy.context.temp_override(active_object=obj, object=obj, edit_object=obj):
            r = bpy.ops.uv.minimize_stretch(iterations=50)
        leave(obj)
        after = uvs(me)
        log(f"  {r} {1000 * (time.perf_counter() - t):.1f} ms; interior vs. original "
            f"{np.abs(after[interior_loops] - before[interior_loops]).max():.4f}, outside moved "
            f"{np.abs(after[~patch[loop_f]] - mangled[~patch[loop_f]]).max():.5f}")
    except Exception as ex:
        log(f"  FAILED {ex!r}")
        if obj.mode != 'OBJECT':
            leave(obj)


def other_ops(obj):
    log("== smart project, pack, average")
    me = obj.data
    me.polygons.foreach_set("select", np.ones(len(me.polygons), dtype=bool))
    for name, kw in (("smart_project", {"angle_limit": 1.15192, "island_margin": 0.01}),
                     ("average_islands_scale", {}), ("pack_islands", {"margin": 0.01}),
                     ("seams_from_islands", {})):
        try:
            edit(obj)
            bpy.ops.mesh.select_all(action='SELECT')
            with bpy.context.temp_override(active_object=obj, object=obj, edit_object=obj):
                if hasattr(bpy.ops.uv, "select_all"):
                    try:
                        bpy.ops.uv.select_all(action='SELECT')
                    except RuntimeError as ex:
                        log("  uv.select_all:", ex)
                t = time.perf_counter()
                r = getattr(bpy.ops.uv, name)(**kw)
            leave(obj)
            u = uvs(me)
            log(f"  {name}: {r} {1000 * (time.perf_counter() - t):.1f} ms, range {u.min(0)} {u.max(0)}")
        except Exception as ex:
            log(f"  {name}: FAILED {ex!r}")
            if obj.mode != 'OBJECT':
                leave(obj)


def timing():
    log("== timing: Edit Mode round trip and unwrap")
    for seg, rings in ((32, 16), (128, 64), (256, 128), (512, 256)):
        obj = sphere(seg, rings, f"T{seg}")
        me = obj.data
        n = len(me.polygons)
        t0 = time.perf_counter()
        edit(obj)
        leave(obj)
        trip = time.perf_counter() - t0
        t0 = time.perf_counter()
        edit(obj)
        bpy.ops.mesh.select_all(action='SELECT')
        with bpy.context.temp_override(active_object=obj, object=obj, edit_object=obj):
            bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.01)
        leave(obj)
        full = time.perf_counter() - t0
        # Partial: about 1000 faces.
        fc = np.empty(n * 3)
        me.polygons.foreach_get("center", fc)
        fc = fc.reshape(-1, 3)
        d = np.linalg.norm(fc - np.array([0.0, -1.0, 0.0]), axis=1)
        sel = d <= np.sort(d)[min(999, n - 1)]
        me.polygons.foreach_set("select", sel)
        me.update()
        t0 = time.perf_counter()
        edit(obj)
        with bpy.context.temp_override(active_object=obj, object=obj, edit_object=obj):
            bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.01)
        leave(obj)
        part = time.perf_counter() - t0
        log(f"  {n} faces: edit round trip {1000 * trip:.0f} ms, full unwrap {1000 * full:.0f} ms, "
            f"1000-face unwrap {1000 * part:.0f} ms")
        bpy.data.objects.remove(obj)


def main():
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        storage()
        obj = full_unwrap()
        partial(obj)
        other_ops(obj)
        timing()
    except Exception:
        log(traceback.format_exc())
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")


main()
