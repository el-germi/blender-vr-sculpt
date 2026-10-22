"""Probe the Blender API pieces the hard surface tools rely on. Read only (makes a scratch scene).

blender --background --factory-startup --python tests/probe_hard_surface.py
"""

import time
import traceback

import bmesh
import bpy
import numpy as np
from mathutils import Vector


def step(name, fn):
    try:
        print("PROBE", name, "->", fn(), flush=True)
    except Exception:
        print("PROBE", name, "FAILED", traceback.format_exc(), flush=True)


for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)


def cube(name, size=0.1, loc=(0, 0, 0), subdiv=0):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    if subdiv:
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=subdiv, use_grid_fill=True)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)
    obj.location = loc
    return obj


target = cube("Target")
cutter = cube("Cutter", 0.05, (0.05, 0.0, 0.05))

step("solvers", lambda: [i.identifier for i in bpy.types.BooleanModifier.bl_rna.properties["solver"].enum_items])
step("operations", lambda: [i.identifier for i in bpy.types.BooleanModifier.bl_rna.properties["operation"].enum_items])
step("modifiers.move", lambda: hasattr(target.modifiers, "move"))
step("Mesh.shade_smooth", lambda: hasattr(bpy.types.Mesh, "shade_smooth"))
step("bevel props", lambda: [p.identifier for p in bpy.types.BevelModifier.bl_rna.properties
                             if p.identifier in {"harden_normals", "limit_method", "angle_limit", "use_clamp_overlap",
                                                 "segments", "width", "miter_outer", "profile"}])
step("weighted normal props", lambda: [p.identifier for p in bpy.types.WeightedNormalModifier.bl_rna.properties][:20])
step("modifier types", lambda: [i.identifier for i in bpy.types.Modifier.bl_rna.properties["type"].enum_items
                                if "SMOOTH" in i.identifier or "NORMAL" in i.identifier])

mod = target.modifiers.new("VR Boolean", 'BOOLEAN')
mod.object = cutter
mod.operation = 'DIFFERENCE'


def evaluated_stats(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    out = (len(me.vertices), len(me.polygons))
    ev.to_mesh_clear()
    return out


def timed_eval(obj, solver):
    mod.solver = solver
    t0 = time.perf_counter()
    cutter.location.x += 0.001
    s = evaluated_stats(obj)
    return s, f"{(time.perf_counter() - t0) * 1000:.1f} ms"


for solver in ("FLOAT", "EXACT", "MANIFOLD"):
    step(f"boolean {solver}", lambda s=solver: timed_eval(target, s))

# Dense target: how fast is a live cut while dragging?
dense = cube("Dense", 0.1, (0.5, 0, 0), subdiv=40)
dmod = dense.modifiers.new("VR Boolean", 'BOOLEAN')
dmod.object = cutter
for solver in ("EXACT", "MANIFOLD"):
    def dense_eval(s=solver):
        dmod.solver = s
        cutter.location = (0.55, 0.0, 0.05)
        bpy.context.view_layer.update()
        times = []
        for i in range(5):
            cutter.location.x += 0.002
            t0 = time.perf_counter()
            stats = evaluated_stats(dense)
            times.append((time.perf_counter() - t0) * 1000)
        return len(dense.data.vertices), stats, f"{min(times):.1f}-{max(times):.1f} ms"
    step(f"dense {solver}", dense_eval)

# Manifold with a non-manifold cutter (a plane).
plane_me = bpy.data.meshes.new("Plane")
plane_me.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
plane = bpy.data.objects.new("Plane", plane_me)
bpy.context.collection.objects.link(plane)
plane.scale = (0.03, 0.03, 0.03)
plane.location = (0.0, 0.0, 0.03)


def plane_cut():
    mod.object = plane
    mod.solver = 'MANIFOLD'
    out = evaluated_stats(target)
    mod.object = cutter
    return out


step("manifold plane cutter", plane_cut)


def move_mod():
    target.modifiers.new("VR Bevel", 'BEVEL')
    target.modifiers.new("VR Mirror", 'MIRROR')
    target.modifiers.move(2, 1)
    return [m.name for m in target.modifiers]


step("move", move_mod)


def auto_smooth():
    with bpy.context.temp_override(object=target, active_object=target, selected_objects=[target],
                                   selected_editable_objects=[target]):
        r = bpy.ops.object.shade_auto_smooth(use_auto_smooth=True)
    return r, [(m.name, m.type) for m in target.modifiers]


step("shade_auto_smooth", auto_smooth)


def corner_normals(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    n = np.array([v.vector[:] for v in me.corner_normals])
    f = np.array([p.normal[:] for p in me.polygons])
    loop_face = np.empty(len(me.loops), dtype=np.int64)
    for p in me.polygons:
        loop_face[p.loop_start:p.loop_start + p.loop_total] = p.index
    dev = np.degrees(np.arccos(np.clip((n * f[loop_face]).sum(1), -1, 1)))
    out = (len(me.polygons), me.has_custom_normals, f"max dev from face normal {dev.max():.1f} deg, "
           f"corners bent >1 deg: {(dev > 1).mean() * 100:.0f}%")
    ev.to_mesh_clear()
    return out


def weighted_on_flat():
    obj = cube("WN", 0.1, (0, 0.5, 0))
    b = obj.modifiers.new("VR Bevel", 'BEVEL')
    b.width = 0.01
    b.segments = 3
    b.limit_method = 'ANGLE'
    flat = corner_normals(obj)
    w = obj.modifiers.new("VR Weighted Normal", 'WEIGHTED_NORMAL')
    w.keep_sharp = True
    with_wn = corner_normals(obj)
    b.harden_normals = True
    obj.modifiers.remove(w)
    harden = corner_normals(obj)
    obj.data.shade_smooth()
    harden_smooth = corner_normals(obj)
    w = obj.modifiers.new("VR Weighted Normal", 'WEIGHTED_NORMAL')
    w.keep_sharp = True
    b.harden_normals = False
    wn_smooth = corner_normals(obj)
    return {"flat bevel": flat, "flat + WN": with_wn, "flat + harden": harden,
            "smooth + harden": harden_smooth, "smooth + WN": wn_smooth}


step("normals", weighted_on_flat)


def apply_mod():
    t = cube("ApplyTarget", 0.1, (0, -0.5, 0))
    c = cube("ApplyCutter", 0.05, (0.05, -0.5, 0.05))
    m = t.modifiers.new("VR Boolean", 'BOOLEAN')
    m.object = c
    m.solver = 'MANIFOLD'
    before = len(t.data.vertices)
    with bpy.context.temp_override(object=t, active_object=t, selected_objects=[t], selected_editable_objects=[t]):
        r = bpy.ops.object.modifier_apply(modifier=m.name)
    return r, before, len(t.data.vertices), [x.name for x in t.modifiers]


step("modifier_apply", apply_mod)


def array_radial():
    o = cube("Arr", 0.02, (0, 0, 1))
    e = bpy.data.objects.new("Arr Offset", None)
    bpy.context.collection.objects.link(e)
    e.parent = o
    from mathutils import Matrix
    c = Vector((0.1, 0, 0))
    e.matrix_basis = Matrix.Translation(c) @ Matrix.Rotation(3.14159 * 2 / 6, 4, 'Z') @ Matrix.Translation(-c)
    a = o.modifiers.new("VR Array", 'ARRAY')
    a.count = 6
    a.use_relative_offset = False
    a.use_object_offset = True
    a.offset_object = e
    bpy.context.view_layer.update()
    o.location.x += 1.0
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    ev = o.evaluated_get(dg)
    me = ev.to_mesh()
    co = np.array([v.co[:] for v in me.vertices])
    ev.to_mesh_clear()
    center = co.mean(0)
    return len(co), [round(x, 3) for x in center]


step("radial array", array_radial)


def bvh():
    from mathutils.bvhtree import BVHTree
    dg = bpy.context.evaluated_depsgraph_get()
    t0 = time.perf_counter()
    tree = BVHTree.FromObject(dense, dg)
    t1 = time.perf_counter()
    hit = tree.find_nearest(Vector((0, 0, 0.2)))
    return f"{(t1 - t0) * 1000:.1f} ms", hit[0], hit[1]


step("bvh FromObject", bvh)
