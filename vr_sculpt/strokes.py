"""Freehand 3D strokes: samples in, tube or ribbon meshes out.

A stroke object keeps its raw samples (positions, widths, the controller's side
vector) in object space as an ID property, so its mesh can be rebuilt after
editing, mirrored copies included. Geometry is built with numpy and written
straight into the mesh, fast enough to rebuild every frame while drawing.

"Note" strokes go to Blender's annotations instead (3D-space annotation strokes).
"""

import math

import bpy
import numpy as np
from mathutils import Vector

PROP = "vrs_stroke"
COLLECTION = "VR Strokes"
NOTES_LAYER = "VR Notes"
TUBE_SEGMENTS = 8
SUBDIV = 3  # Catmull-Rom points per sample segment

PALETTE = (
    ('WHITE', "White", (0.92, 0.92, 0.92)),
    ('BLACK', "Black", (0.04, 0.04, 0.045)),
    ('RED', "Red", (0.9, 0.12, 0.1)),
    ('ORANGE', "Orange", (1.0, 0.45, 0.08)),
    ('YELLOW', "Yellow", (0.98, 0.82, 0.15)),
    ('GREEN', "Green", (0.2, 0.72, 0.28)),
    ('BLUE', "Blue", (0.18, 0.42, 0.95)),
    ('PURPLE', "Purple", (0.58, 0.28, 0.85)),
)
COLORS = {ident: rgb for ident, _name, rgb in PALETTE}


# --- geometry -------------------------------------------------------------

def catmull_rom(points, values=(), subdiv=SUBDIV):
    """Resample a polyline smoothly. values: per-point arrays interpolated linearly alongside."""
    n = len(points)
    if n < 3 or subdiv <= 1:
        return points, list(values)
    p = np.vstack([points[:1], points, points[-1:]])
    p0, p1, p2, p3 = p[:-3], p[1:-2], p[2:-1], p[3:]
    t = (np.arange(subdiv) / subdiv)[None, :, None]
    t2, t3 = t * t, t * t * t
    seg = 0.5 * ((2 * p1[:, None]) + (-p0 + p2)[:, None] * t
                 + (2 * p0 - 5 * p1 + 4 * p2 - p3)[:, None] * t2
                 + (-p0 + 3 * p1 - 3 * p2 + p3)[:, None] * t3)
    out = np.vstack([seg.reshape(-1, 3), points[-1:]])
    tt = t[0, :, 0]
    resampled = []
    for v in values:
        v = np.asarray(v)
        a, b = v[:-1], v[1:]
        if v.ndim == 1:
            r = a[:, None] + (b - a)[:, None] * tt[None, :]
            resampled.append(np.concatenate([r.reshape(-1), v[-1:]]))
        else:
            r = a[:, None] + (b - a)[:, None] * tt[None, :, None]
            resampled.append(np.vstack([r.reshape(-1, v.shape[1]), v[-1:]]))
    return out, resampled


def _normalize(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-12)


def frames(points, sides):
    """Tangents and a normal that follows the controller's side vector (so ribbons follow your wrist)."""
    t = np.gradient(points, axis=0) if len(points) > 1 else np.array([[0.0, 0.0, 1.0]])
    t = _normalize(t)
    n = sides - np.sum(sides * t, axis=1, keepdims=True) * t
    bad = np.linalg.norm(n, axis=1) < 1e-6
    if bad.any():
        # Side vector parallel to the stroke: any perpendicular will do.
        alt = np.cross(t[bad], [0.0, 0.0, 1.0])
        tiny = np.linalg.norm(alt, axis=1) < 1e-6
        alt[tiny] = np.cross(t[bad][tiny], [1.0, 0.0, 0.0])
        n[bad] = alt
    n = _normalize(n)
    b = np.cross(t, n)
    return t, n, b


def tube(points, radii, sides, segments=TUBE_SEGMENTS):
    """(verts, face_indices, face_sizes) of a capped tube."""
    _t, n, b = frames(points, sides)
    ang = np.linspace(0.0, 2 * math.pi, segments, endpoint=False)
    ring = np.cos(ang)[None, :, None] * n[:, None] + np.sin(ang)[None, :, None] * b[:, None]
    verts = (points[:, None] + radii[:, None, None] * ring).reshape(-1, 3)
    rows, m = len(points), segments
    i = np.arange(rows - 1)[:, None]
    k = np.arange(m)[None, :]
    k1 = (k + 1) % m
    # Outward-facing order (see test_draw.py for the winding check).
    quads = np.stack([i * m + k, i * m + k1, (i + 1) * m + k1, (i + 1) * m + k], axis=-1).reshape(-1)
    start_cap = np.arange(m)[::-1]
    end_cap = (rows - 1) * m + np.arange(m)
    faces = np.concatenate([quads, start_cap, end_cap])
    sizes = np.concatenate([np.full((rows - 1) * m, 4), [m, m]])
    return verts, faces, sizes


def ribbon(points, radii, sides):
    """(verts, face_indices, face_sizes) of a flat band across the controller's side vector."""
    _t, n, _b = frames(points, sides)
    left = points - radii[:, None] * n
    right = points + radii[:, None] * n
    verts = np.stack([left, right], axis=1).reshape(-1, 3)
    i = np.arange(len(points) - 1)
    faces = np.stack([2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1], axis=-1).reshape(-1)
    sizes = np.full(len(i), 4)
    return verts, faces, sizes


def mirror_variants(sym):
    """Bitmasks of the mirrored copies for enabled axes (bit 0 = X, 1 = Y, 2 = Z), identity first."""
    return [m for m in range(8) if (m & ~sym) == 0]


def reflect(arr, mask, center=None):
    out = np.array(arr, dtype=np.float64, copy=True)
    for axis in range(3):
        if mask & (1 << axis):
            out[..., axis] = (2 * center[axis] - out[..., axis]) if center is not None else -out[..., axis]
    return out


# While the trigger is held, strokes longer than this are rebuilt without the Catmull-Rom
# subdivision (about 3x fewer vertices); the full mesh is built on release.
PREVIEW_SAMPLES = 150


def build(samples, preview=False):
    """Mesh arrays for a stroke's samples (dict with p, w, s, style, sym, c)."""
    p, w, s = samples["p"], samples["w"], samples["s"]
    if len(p) < 2:
        return np.zeros((0, 3)), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    subdiv = 1 if preview and len(p) > PREVIEW_SAMPLES else SUBDIV
    pts, (radii, sides) = catmull_rom(p, (w / 2.0, s), subdiv)
    sides = _normalize(sides)
    make = tube if samples["style"] == 'TUBE' else ribbon
    all_v, all_f, all_s, offset = [], [], [], 0
    for mask in mirror_variants(samples.get("sym", 0)):
        mp = reflect(pts, mask, samples["c"]) if mask else pts
        ms = reflect(sides, mask) if mask else sides
        v, f, z = make(mp, radii, ms)
        if make is ribbon and bin(mask).count("1") % 2:
            # A reflection flips handedness: reverse each face to keep the ribbon's front side.
            # (Tubes need nothing: their B = T x N axis flips too, which already reverses each ring.)
            starts = np.cumsum(z) - z
            f = np.concatenate([f[a:a + c][::-1] for a, c in zip(starts, z)])
        all_v.append(v)
        all_f.append(f + offset)
        all_s.append(z)
        offset += len(v)
    return np.vstack(all_v), np.concatenate(all_f), np.concatenate(all_s)


def write_mesh(mesh, verts, faces, sizes, smooth=True):
    mesh.clear_geometry()
    if len(verts) == 0:
        mesh.update()
        return
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.astype(np.float32).ravel())
    mesh.loops.add(len(faces))
    mesh.loops.foreach_set("vertex_index", faces.astype(np.int32))
    mesh.polygons.add(len(sizes))
    mesh.polygons.foreach_set("loop_start", (np.cumsum(sizes) - sizes).astype(np.int32))
    mesh.update(calc_edges=True)
    if smooth:
        mesh.shade_smooth()


# --- stroke objects ---------------------------------------------------------

def collection(context):
    coll = bpy.data.collections.get(COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(COLLECTION)
    if coll.name not in context.scene.collection.children and not any(
            coll.name in c.children for c in context.scene.collection.children_recursive):
        context.scene.collection.children.link(coll)
    return coll


def material(color_id):
    name = f"VR Stroke {color_id.title()}"
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        rgb = COLORS.get(color_id, (0.8, 0.8, 0.8))
        mat.diffuse_color = (*rgb, 1.0)  # Solid view (and the headset) shows this.
        mat.roughness = 0.6
        if mat.node_tree is None:
            mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF") if mat.node_tree else None
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    return mat


def new_object(context, style, color_id, origin):
    mesh = bpy.data.meshes.new("Stroke")
    obj = bpy.data.objects.new("Stroke", mesh)
    collection(context).objects.link(obj)
    obj.location = origin
    mesh.materials.append(material(color_id))
    return obj


def is_stroke(obj):
    return obj is not None and obj.type == 'MESH' and PROP in obj


def save(obj, samples):
    old = obj.get(PROP)
    version = (old.get("v", 0) + 1) if old is not None else 0
    obj[PROP] = {
        "style": samples["style"], "sym": int(samples.get("sym", 0)),
        "c": [float(x) for x in samples["c"]],
        "p": samples["p"].astype(float).ravel().tolist(),
        "w": samples["w"].astype(float).tolist(),
        "s": samples["s"].astype(float).ravel().tolist(),
        "v": version,
    }
    _cache.pop(obj.name, None)


_cache = {}


def load(obj):
    data = obj[PROP]
    key = obj.name
    hit = _cache.get(key)
    if hit is not None and hit[0] == data.get("v", 0):
        return hit[1]
    samples = {
        "style": data["style"], "sym": int(data.get("sym", 0)),
        "c": np.array(data["c"].to_list() if hasattr(data["c"], "to_list") else data["c"], dtype=np.float64),
        "p": np.array(data["p"].to_list(), dtype=np.float64).reshape(-1, 3),
        "w": np.array(data["w"].to_list(), dtype=np.float64),
        "s": np.array(data["s"].to_list(), dtype=np.float64).reshape(-1, 3),
    }
    _cache[key] = (data.get("v", 0), samples)
    return samples


def rebuild(obj, samples, preview=False):
    write_mesh(obj.data, *build(samples, preview), smooth=samples["style"] == 'TUBE')


def forget(obj):
    """Sculpted or otherwise edited as a mesh: it stops being an editable stroke."""
    if is_stroke(obj):
        del obj[PROP]
        _cache.pop(obj.name, None)


def distance(obj, point_world):
    """Distance from a world point to the stroke's surface (approximate), any mirrored copy."""
    s = load(obj)
    if len(s["p"]) == 0:
        return math.inf
    inv = obj.matrix_world.inverted_safe()
    p = np.array(inv @ Vector(point_world))
    scale = max(obj.matrix_world.to_scale())
    best = math.inf
    for mask in mirror_variants(s["sym"]):
        pts = reflect(s["p"], mask, s["c"]) if mask else s["p"]
        d = np.linalg.norm(pts - p, axis=1) - s["w"] / 2.0
        best = min(best, float(d.min()))
    return best * scale


def nearest(context, point_world, reach):
    """The stroke object closest to point (within reach), or None."""
    best, best_d = None, reach
    for obj in context.view_layer.objects:
        if obj is None or not is_stroke(obj) or not obj.visible_get():
            continue
        d = distance(obj, point_world)
        if d <= best_d:
            best, best_d = obj, d
    return best


def delete(obj):
    mesh = obj.data
    _cache.pop(obj.name, None)
    bpy.data.objects.remove(obj)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


# --- annotations (Note strokes) ---------------------------------------------

def notes_frame(context, color_id):
    """The annotation frame Note strokes of this color go to (one layer per color)."""
    scene = context.scene
    if scene.annotation is None:
        scene.annotation = bpy.data.annotations.new("Annotations")
    data = scene.annotation
    name = f"{NOTES_LAYER} {color_id.title()}"
    layer = data.layers.get(name)
    if layer is None:
        layer = data.layers.new(name, set_active=False)
        layer.color = COLORS.get(color_id, (1.0, 1.0, 1.0))
        layer.thickness = 4
        layer.show_in_front = True
    frame = layer.active_frame
    if frame is None:
        frame = layer.frames.new(scene.frame_current, active=True)
    return layer, frame


def note_strokes(context):
    """(layer, frame, stroke) for every stroke in our Note layers."""
    data = context.scene.annotation
    if data is None:
        return []
    out = []
    for layer in data.layers:
        if not layer.info.startswith(NOTES_LAYER):
            continue
        for frame in layer.frames:
            for stroke in frame.strokes:
                out.append((layer, frame, stroke))
    return out


# --- quick undo ---------------------------------------------------------------

# Most recent last: ("mesh", object name) or ("note", [(layer name, frame number, stroke count)]).
history = []


def push(entry):
    history.append(entry)
    del history[:-200]


def undo_last(context):
    """Delete the most recent stroke that still exists. Returns a short description, or None."""
    while history:
        kind, ref = history.pop()
        if kind == "mesh":
            obj = bpy.data.objects.get(ref)
            if obj is not None and is_stroke(obj):
                delete(obj)
                return "stroke"
        elif kind == "note":
            data = context.scene.annotation
            removed = 0
            for layer_name, frame_number, count in ref:
                layer = data.layers.get(layer_name) if data else None
                frame = next((f for f in layer.frames if f.frame_number == frame_number), None) if layer else None
                if frame is None:
                    continue
                for _ in range(min(count, len(frame.strokes))):
                    frame.strokes.remove(frame.strokes[len(frame.strokes) - 1])
                    removed += 1
            if removed:
                return "note"
    return None
