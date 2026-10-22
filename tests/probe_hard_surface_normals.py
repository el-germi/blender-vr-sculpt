"""Which finish stack shades a hard surface cylinder right: flat caps, smooth sides, smooth bevels?
Read only (scratch scene).

Careful reading the results: shade_auto_smooth also shade-smooths the object's own faces, so these
cylinders come out smooth either way. Faces a boolean brings in from a flat-shaded cutter stay flat
unless Smooth by Angle's "Ignore Sharpness" is on (test_hard_surface.py checks that case).

blender --background --factory-startup --python tests/probe_hard_surface_normals.py
"""

import math

import bmesh
import bpy
import numpy as np

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)


def cylinder(name):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=24, radius1=0.05, radius2=0.05, depth=0.1)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)
    return obj


def smooth_by_angle(obj):
    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
        bpy.ops.object.shade_auto_smooth(use_auto_smooth=True, angle=math.radians(30))
    return next(m for m in obj.modifiers if m.type == 'NODES')


def report(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    n = np.array([v.vector[:] for v in me.corner_normals])
    co = np.array([v.co[:] for v in me.vertices])
    loop_v = np.array([lp.vertex_index for lp in me.loops])
    fn = np.array([pl.normal[:] for pl in me.polygons])
    loop_f = np.empty(len(me.loops), dtype=np.int64)
    for pl in me.polygons:
        loop_f[pl.loop_start:pl.loop_start + pl.loop_total] = pl.index
    f = fn[loop_f]
    p = co[loop_v]
    # Cap faces (normal straight up or down): corners should keep the face normal (flat).
    cap = np.abs(f[:, 2]) > 0.999
    # Side faces (horizontal normal): corners should point radially (smooth).
    side = np.abs(f[:, 2]) < 0.001
    r = np.hypot(p[:, 0], p[:, 1])
    radial = np.zeros_like(p)
    radial[:, :2] = p[:, :2] / np.maximum(r[:, None], 1e-9)
    cap_dev = np.degrees(np.arccos(np.clip((n[cap] * f[cap]).sum(1), -1, 1))).max()
    side_dev = np.degrees(np.arccos(np.clip((n[side] * radial[side]).sum(1), -1, 1))).max()
    out = f"faces={len(me.polygons)} caps flat: max dev {cap_dev:.1f} deg | sides smooth: max dev from radial {side_dev:.1f} deg"
    ev.to_mesh_clear()
    return out


def bevel(obj, harden):
    b = obj.modifiers.new("VR Bevel", 'BEVEL')
    b.width = 0.005
    b.segments = 3
    b.limit_method = 'ANGLE'
    b.angle_limit = math.radians(30)
    b.harden_normals = harden
    return b


cases = {}
o = cylinder("plain")
cases["flat, no mods"] = o
o = cylinder("bevel_harden")
bevel(o, True)
cases["bevel harden"] = o
o = cylinder("sba_then_bevel")
smooth_by_angle(o).use_pin_to_last = False
bevel(o, True)
cases["smooth by angle, then bevel harden"] = o
o = cylinder("bevel_then_sba")
bevel(o, True)
smooth_by_angle(o)
cases["bevel harden, then smooth by angle"] = o
o = cylinder("sba_bevel_wn")
smooth_by_angle(o).use_pin_to_last = False
bevel(o, False)
w = o.modifiers.new("VR Weighted Normal", 'WEIGHTED_NORMAL')
w.keep_sharp = True
cases["smooth by angle, bevel, weighted normal"] = o
o = cylinder("bevel_wn_sba")
bevel(o, False)
w = o.modifiers.new("VR Weighted Normal", 'WEIGHTED_NORMAL')
w.keep_sharp = True
w.mode = 'FACE_AREA'
smooth_by_angle(o)
cases["bevel, weighted normal, then smooth by angle"] = o

for name, obj in cases.items():
    print("NORMALS", f"{name:45s}", report(obj), [m.name for m in obj.modifiers], flush=True)
