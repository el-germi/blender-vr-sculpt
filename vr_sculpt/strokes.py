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


# --- stroke orientation ------------------------------------------------------
#
# Each sample stores a frame normal (the "s" array): the direction across a ribbon,
# and where a tube's rings start. It is computed sample by sample, never from the
# controller directly: projecting the controller's side axis onto the plane across
# the stroke breaks down when you draw along that axis (the usual sideways stroke),
# where it shrinks to nothing, swings wildly and flips 180 degrees (a bow-tie in a
# ribbon, a pinched knot in a tube). Instead the previous normal is carried along
# the path (a rotation-minimizing frame), and ribbons turn toward the controller
# only as far as its direction is meaningful, at a capped rate, never flipping.

TWIST_RATE = math.pi / 0.1  # max ribbon twist: half a turn per 10 cm of stroke (hand space)
STEER_DEAD = 0.26  # sin(15 deg): controller axis this close to the stroke direction doesn't steer
STEER_FULL = 0.6  # sin(37 deg): from here on it steers at the full rate


def _across(v, t):
    """v with its component along t removed: (unit vector or None, length before normalizing)."""
    v = v - np.dot(v, t) * t
    n = float(np.linalg.norm(v))
    return (v / n, n) if n > 1e-9 else (None, 0.0)


def any_perpendicular(t):
    a = np.cross(t, [0.0, 0.0, 1.0])
    if np.linalg.norm(a) < 1e-6:
        a = np.cross(t, [1.0, 0.0, 0.0])
    return a / np.linalg.norm(a)


def orient_step(prev_n, t, desired=None, max_angle=0.0, steer_t=None):
    """Next frame normal: prev_n carried across to tangent t, turned toward `desired` by at most max_angle.

    steer_t: the stroke's direction over a longer stretch, to judge how far `desired` stands out
    across the stroke without hand jitter in the sample-to-sample direction t.
    """
    n, _ = _across(prev_n, t)
    if n is None:
        n = any_perpendicular(t)
    if desired is None or max_angle <= 0.0:
        return n
    desired = np.asarray(desired, dtype=np.float64)
    _, strength = _across(desired, steer_t if steer_t is not None else t)
    d, _ = _across(desired, t)
    if d is None:
        return n
    if np.dot(d, n) < 0.0:
        d = -d  # A ribbon looks the same either way round: never flip.
    angle = math.acos(min(max(float(np.dot(n, d)), -1.0), 1.0))
    if angle < 1e-6:
        return d
    # The less the desired axis stands out across the stroke, the less it may steer: no say
    # at all within ~15 degrees of the stroke direction (STEER_DEAD), full say past ~37.
    k = min(max((strength - STEER_DEAD) / (STEER_FULL - STEER_DEAD), 0.0), 1.0)
    turn = min(angle, max_angle * k * k * (3.0 - 2.0 * k))
    u, _ = _across(d, n)
    if u is None:
        return n
    out = n * math.cos(turn) + u * math.sin(turn)
    return out / np.linalg.norm(out)


def stable_normals(points, guide=None, max_angle_per_unit=0.0):
    """Frame normals along a whole polyline (used after Edit reshapes a stroke).

    guide: per-point directions to steer toward (ribbons), or None for twist-free (tubes).
    """
    n_pts = len(points)
    if n_pts == 0:
        return np.zeros((0, 3))
    t = _normalize(np.gradient(points, axis=0)) if n_pts > 1 else np.array([[0.0, 0.0, 1.0]])
    out = np.zeros((n_pts, 3))
    start = guide[0] if guide is not None else any_perpendicular(t[0])
    first, _ = _across(np.asarray(start, dtype=np.float64), t[0])
    out[0] = first if first is not None else any_perpendicular(t[0])
    for i in range(1, n_pts):
        seg = float(np.linalg.norm(points[i] - points[i - 1]))
        out[i] = orient_step(out[i - 1], t[i], None if guide is None else guide[i],
                             max_angle_per_unit * seg)
    return out


def frames(points, sides):
    """Tangents, normals (the stored frame normals made exactly perpendicular) and binormals."""
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


TAPER_MIN = 0.1  # width at a tapered stroke's tips, as a fraction of its full width


def taper_envelope(points, fraction):
    """Per-sample width factor for Taper strokes: ramps up from thin tips over `fraction` of the
    stroke's length at each end (eased), full width in between. Proportional to the length, so
    a short flick and a long sweep get the same shape."""
    n = len(points)
    if n < 2 or fraction <= 0.0:
        return np.ones(n)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))])
    length = s[-1]
    if length <= 1e-9:
        return np.ones(n)
    ramp = max(min(fraction, 0.5), 1e-3) * length
    x = np.clip(np.minimum(s, length - s) / ramp, 0.0, 1.0)
    return TAPER_MIN + (1.0 - TAPER_MIN) * x * x * (3.0 - 2.0 * x)


# While the trigger is held, strokes longer than this are rebuilt without the Catmull-Rom
# subdivision (about 3x fewer vertices); the full mesh is built on release.
PREVIEW_SAMPLES = 150


def build(samples, preview=False):
    """Mesh arrays for a stroke's samples (dict with p, w, s, style, sym, c)."""
    p, w, s = samples["p"], samples["w"], samples["s"]
    if len(p) < 2:
        return np.zeros((0, 3)), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    subdiv = 1 if preview and len(p) > PREVIEW_SAMPLES else SUBDIV
    if samples.get("taper", 0.0) > 0.0:
        w = w * taper_envelope(p, samples["taper"])
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
        "taper": float(samples.get("taper", 0.0)),
        "v": version,
    }
    _cache.pop(obj.name, None)


_cache = {}


def forget_caches():
    _cache.clear()


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
        "taper": float(data.get("taper", 0.0)),
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

# Most recent last: ("mesh", object name), ("note", [(layer name, frame number, stroke count)]) or
# ("pencil", (object name, layer name, frame number, stroke count)).
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
        elif kind == "pencil":
            from . import pencil
            if pencil.undo(ref):
                return "pencil stroke"
    return None
