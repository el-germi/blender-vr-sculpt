import os, sys, time
import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vr_sculpt import brushes

bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=8, radius=1.0)
obj = bpy.context.active_object
me = obj.data
n, ne = len(me.vertices), len(me.edges)


def t(label, fn, reps=3):
    best = 1e9
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000)
    print(f"  {label:40} {best:8.1f} ms")


co = np.empty(n * 3)
co32 = np.empty(n * 3, dtype=np.float32)
ed = np.empty(ne * 2, dtype=np.int32)
print(f"{n:,} verts {ne:,} edges")
t("positions foreach_get float64", lambda: me.attributes["position"].data.foreach_get("vector", co))
t("positions foreach_get float32", lambda: me.attributes["position"].data.foreach_get("vector", co32))
t("vertices.co foreach_get float32", lambda: me.vertices.foreach_get("co", co32))
t("edges foreach_get", lambda: me.edges.foreach_get("vertices", ed))
t(".edge_verts attribute foreach_get", lambda: me.attributes[".edge_verts"].data.foreach_get("value", ed) if ".edge_verts" in me.attributes else None)
nrm = np.empty(n * 3, dtype=np.float32)
t("vertex_normals foreach_get (clean)", lambda: me.vertex_normals.foreach_get("vector", nrm))


def dirty_normals():
    me.attributes["position"].data.foreach_set("vector", co32)
    me.update()
    me.vertex_normals.foreach_get("vector", nrm)


t("write+update+normals (dirty)", dirty_normals)
t("write+update only", lambda: (me.attributes["position"].data.foreach_set("vector", co32), me.update()))
c = co.reshape(n, 3)
t("grid build", lambda: brushes._Grid(c, 0.1))
st = brushes.MeshState(obj)
t("MeshState()", lambda: brushes.MeshState(obj))
brushes._adjacency_cache.clear()
t("adjacency build", lambda: (brushes._adjacency_cache.clear(), brushes._adjacency(st)))
