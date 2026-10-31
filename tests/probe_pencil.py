"""Probe the Grease Pencil (v3) Python API Pencil strokes need: objects, layers, frames, drawings, strokes,
points, materials, removal.

blender --background --factory-startup --python tests/probe_pencil.py
"""

import time
import traceback

import bpy
import numpy as np


def step(name, fn):
    try:
        print(f"{name}: {fn()}")
    except Exception as ex:
        print(f"{name}: FAILED {type(ex).__name__}: {ex}")
        traceback.print_exc()


gp = bpy.data.grease_pencils.new("Probe")
obj = bpy.data.objects.new("Probe", gp)
bpy.context.scene.collection.objects.link(obj)
step("data type", lambda: (type(gp).__name__, obj.type))
step("layers api", lambda: [n for n in dir(gp.layers) if not n.startswith("_")])
layer = gp.layers.new("Lines")
step("layer api", lambda: [n for n in dir(layer) if not n.startswith("_")])
step("frames api", lambda: [n for n in dir(layer.frames) if not n.startswith("_")])
frame = layer.frames.new(1)
step("frame api", lambda: [n for n in dir(frame) if not n.startswith("_")])
drawing = frame.drawing
step("drawing api", lambda: [n for n in dir(drawing) if not n.startswith("_")])
step("add_strokes", lambda: drawing.add_strokes([3]))
step("strokes", lambda: (len(drawing.strokes), [n for n in dir(drawing.strokes[0]) if not n.startswith("_")]))
s = drawing.strokes[0]
step("points api", lambda: [n for n in dir(s.points[0]) if not n.startswith("_")])


def write():
    for i, p in enumerate(s.points):
        p.position = (i * 0.1, 0.0, 1.0)
        p.radius = 0.01
        p.opacity = 1.0
    return [tuple(p.position) for p in s.points]


step("write points", write)
step("add_points", lambda: (s.add_points(2), len(s.points)))
step("remove_points", lambda: (s.remove_points(1), len(s.points)))
step("stroke attrs", lambda: (s.cyclic, s.material_index, s.select))
step("attributes", lambda: [a.name for a in drawing.attributes])


def bulk():
    drawing.add_strokes([200])
    t = time.perf_counter()
    st = drawing.strokes[-1]
    pos = np.random.rand(200 * 3).astype(np.float32)
    attr = drawing.attributes["position"]
    n = len(attr.data)
    allpos = np.empty(n * 3, dtype=np.float32)
    attr.data.foreach_get("vector", allpos)
    allpos[-600:] = pos
    attr.data.foreach_set("vector", allpos)
    return f"{n} points, foreach in {(time.perf_counter() - t) * 1000:.2f} ms, last {tuple(st.points[-1].position)}"


step("bulk positions", bulk)
step("offsets", lambda: [a.name for a in drawing.attributes] + [len(drawing.strokes)])


def grow():
    st = drawing.strokes[0]
    t = time.perf_counter()
    for _ in range(100):
        st.add_points(1)
        st.points[-1].position = (0, 0, 0)
    return f"100 single adds in {(time.perf_counter() - t) * 1000:.1f} ms"


step("grow one by one", grow)
step("remove_strokes", lambda: (drawing.remove_strokes(indices=[len(drawing.strokes) - 1]), len(drawing.strokes)))

mat = bpy.data.materials.new("Pencil Red")
step("create_gpencil_data", lambda: bpy.data.materials.create_gpencil_data(mat))
step("gp style", lambda: (mat.is_grease_pencil, mat.grease_pencil.color[:], mat.grease_pencil.show_fill))
obj.data.materials.append(mat)
step("material slot", lambda: len(obj.data.materials))
step("frame at", lambda: [n for n in dir(layer) if "frame" in n])
step("current frame lookup", lambda: layer.current_frame())
step("frames.new 10 then list", lambda: (layer.frames.new(10).frame_number, [f.frame_number for f in layer.frames]))
step("obj evaluated bbox", lambda: [tuple(v) for v in obj.bound_box][:2])
step("layer matrix", lambda: (hasattr(layer, "matrix_local"), hasattr(layer, "matrix_layer")))
step("gp object modes", lambda: [i.identifier for i in bpy.types.Object.bl_rna.properties["mode"].enum_items])
step("remove frame", lambda: (layer.frames.remove(10), [f.frame_number for f in layer.frames]))
