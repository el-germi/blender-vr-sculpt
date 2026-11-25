"""Freehand 3D strokes: samples in, curves (and from them tubes or ribbons) out.

A stroke is a Curves object: one Catmull-Rom curve through its samples (positions,
half the width as `radius`, the frame normal as `custom_normal`), in object space.
Its "VR Stroke" Geometry Nodes modifier makes the tube or ribbon, the taper and the
mirrored copies, so the style can be changed afterwards (Raw: no mesh, just the
curve) or the node group swapped for the user's own (settings.draw_nodes). The
ID property keeps the rest (style, mirror axes and center, taper).

Strokes from before 0.14 are meshes whose ID property holds all the samples; they
stay meshes, rebuilt with numpy (build / write_mesh).

"Note" strokes go to Blender's annotations instead (3D-space annotation strokes).
"""

import math

import bpy
import numpy as np
from mathutils import Vector

from . import primitives

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


# --- the stroke node group ------------------------------------------------------

NODES = "VR Stroke"  # The node group, and the modifier's name on each stroke.
NODES_TAG = "vrs_stroke_nodes"  # On our own group: its version (a newer add-on builds a new one).
NODES_VERSION = 1
MESH_STYLES = ('TUBE', 'RIBBON')  # Draw styles made as a Curves stroke; RAW too (no mesh).
CURVE_STYLES = MESH_STYLES + ('RAW',)
STYLE_NAMES = {'TUBE': "Tube", 'RIBBON': "Ribbon", 'RAW': "Raw"}  # The group's Style menu items.
NORMAL_FREE = 2  # Curves `normal_mode`: 0 minimum twist, 1 Z up, 2 free (`custom_normal`).


def _build_nodes():
    """The VR Stroke group: Style (Tube / Ribbon / Raw), Tube Sides, Taper and Mirror X/Y/Z about Mirror
    Center. Width comes from the curve's radius, the ribbon's roll from its normals."""
    ng = bpy.data.node_groups.new(NODES, 'GeometryNodeTree')
    ng[NODES_TAG] = NODES_VERSION
    ng.is_modifier = True
    itf = ng.interface
    itf.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    itf.new_socket("Style", in_out='INPUT', socket_type='NodeSocketMenu')
    sides = itf.new_socket("Tube Sides", in_out='INPUT', socket_type='NodeSocketInt')
    sides.default_value, sides.min_value, sides.max_value = TUBE_SEGMENTS, 3, 64
    taper = itf.new_socket("Taper", in_out='INPUT', socket_type='NodeSocketFloat')
    taper.default_value, taper.min_value, taper.max_value = 0.0, 0.0, 0.5
    taper.subtype = 'FACTOR'
    taper.description = "Share of the length each end takes to ramp up to full width (0: no taper)"
    for a in "XYZ":
        itf.new_socket(f"Mirror {a}", in_out='INPUT', socket_type='NodeSocketBool')
    itf.new_socket("Mirror Center", in_out='INPUT', socket_type='NodeSocketVector')
    itf.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    nodes, links = ng.nodes, ng.links

    def node(kind, x, y, **props):
        n = nodes.new(kind)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def math(op, a, b, x, y, clamp=False):
        n = node("ShaderNodeMath", x, y, operation=op, use_clamp=clamp)
        for i, v in enumerate((a, b)):
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                links.new(v, n.inputs[i])
        return n.outputs[0]

    gi = node("NodeGroupInput", -1400, 0)
    go = node("NodeGroupOutput", 1400, 0)

    # Width along the stroke: the radius, times the taper's ramps at both ends (as taper_envelope).
    f = node("GeometryNodeSplineParameter", -1200, -400).outputs["Factor"]
    ends = math('MINIMUM', f, math('SUBTRACT', 1.0, f, -1000, -500), -800, -400)
    ramp = math('MAXIMUM', math('MINIMUM', gi.outputs["Taper"], 0.5, -1000, -700), 1e-3, -800, -650)
    x = math('DIVIDE', ends, ramp, -600, -450, clamp=True)
    env = node("ShaderNodeMapRange", -400, -450, interpolation_type='SMOOTHSTEP')
    links.new(x, env.inputs["Value"])
    env.inputs["To Min"].default_value = TAPER_MIN
    tapered = node("FunctionNodeCompare", -400, -700, data_type='FLOAT', operation='GREATER_THAN')
    links.new(gi.outputs["Taper"], tapered.inputs["A"])
    pick = node("GeometryNodeSwitch", -200, -500, input_type='FLOAT')
    links.new(tapered.outputs[0], pick.inputs["Switch"])
    pick.inputs["False"].default_value = 1.0
    links.new(env.outputs["Result"], pick.inputs["True"])
    width = math('MULTIPLY', node("GeometryNodeInputRadius", -200, -300).outputs[0], pick.outputs[0], 0, -350)

    # Tube: a circle swept along the curve, capped, smooth.
    circle = node("GeometryNodeCurvePrimitiveCircle", -200, 300, mode='RADIUS')
    links.new(gi.outputs["Tube Sides"], circle.inputs["Resolution"])
    circle.inputs["Radius"].default_value = 1.0
    tube_mesh = node("GeometryNodeCurveToMesh", 200, 300)
    links.new(gi.outputs["Geometry"], tube_mesh.inputs["Curve"])
    links.new(circle.outputs["Curve"], tube_mesh.inputs["Profile Curve"])
    links.new(width, tube_mesh.inputs["Scale"])
    tube_mesh.inputs["Fill Caps"].default_value = True
    tube_smooth = node("GeometryNodeSetShadeSmooth", 400, 300)
    links.new(tube_mesh.outputs[0], tube_smooth.inputs["Mesh"])
    tube_smooth.inputs["Shade Smooth"].default_value = True

    # Ribbon: a line across the curve's normal, flat shaded.
    line = node("GeometryNodeCurvePrimitiveLine", -200, 0)
    line.inputs["Start"].default_value = (-1.0, 0.0, 0.0)
    line.inputs["End"].default_value = (1.0, 0.0, 0.0)
    ribbon_mesh = node("GeometryNodeCurveToMesh", 200, 0)
    links.new(gi.outputs["Geometry"], ribbon_mesh.inputs["Curve"])
    links.new(line.outputs["Curve"], ribbon_mesh.inputs["Profile Curve"])
    links.new(width, ribbon_mesh.inputs["Scale"])
    ribbon_flat = node("GeometryNodeSetShadeSmooth", 400, 0)
    links.new(ribbon_mesh.outputs[0], ribbon_flat.inputs["Mesh"])
    ribbon_flat.inputs["Shade Smooth"].default_value = False

    menu = node("GeometryNodeMenuSwitch", 600, 100, data_type='GEOMETRY')
    menu.enum_items.clear()
    for style in CURVE_STYLES:
        menu.enum_items.new(STYLE_NAMES[style])
    links.new(gi.outputs["Style"], menu.inputs["Menu"])
    links.new(tube_smooth.outputs[0], menu.inputs[STYLE_NAMES['TUBE']])
    links.new(ribbon_flat.outputs[0], menu.inputs[STYLE_NAMES['RIBBON']])
    links.new(gi.outputs["Geometry"], menu.inputs[STYLE_NAMES['RAW']])
    itf.items_tree["Style"].default_value = STYLE_NAMES['TUBE']

    # Mirrors, one axis at a time (as mirror_variants): the copy is reflected about the center, its faces
    # flipped back outward.
    geo = menu.outputs[0]
    for k, a in enumerate("XYZ"):
        x0 = 800 + k * 200
        axis = [0.0, 0.0, 0.0]
        axis[k] = 2.0
        offset = node("ShaderNodeVectorMath", x0, -300, operation='MULTIPLY')
        links.new(gi.outputs["Mirror Center"], offset.inputs[0])
        offset.inputs[1].default_value = axis
        reflect_ = node("GeometryNodeTransform", x0, -100)
        links.new(geo, reflect_.inputs["Geometry"])
        links.new(offset.outputs[0], reflect_.inputs["Translation"])
        reflect_.inputs["Scale"].default_value = [-1.0 if i == k else 1.0 for i in range(3)]
        flip = node("GeometryNodeFlipFaces", x0, 100)
        links.new(reflect_.outputs[0], flip.inputs["Mesh"])
        join = node("GeometryNodeJoinGeometry", x0, 250)
        links.new(flip.outputs[0], join.inputs[0])
        links.new(geo, join.inputs[0])
        switch = node("GeometryNodeSwitch", x0, 400, input_type='GEOMETRY')
        links.new(gi.outputs[f"Mirror {a}"], switch.inputs["Switch"])
        links.new(geo, switch.inputs["False"])
        links.new(join.outputs[0], switch.inputs["True"])
        geo = switch.outputs[0]
    links.new(geo, go.inputs[0])
    return ng


def nodes_group(settings=None):
    """The node group new strokes get: the user's (settings.draw_nodes), else our VR Stroke group."""
    ng = getattr(settings, "draw_nodes", None) if settings is not None else None
    if ng is not None and ng.bl_idname == 'GeometryNodeTree':
        return ng
    for ng in bpy.data.node_groups:
        if ng.get(NODES_TAG) == NODES_VERSION and ng.bl_idname == 'GeometryNodeTree':
            return ng
    return _build_nodes()


def _modifier(obj):
    mod = obj.modifiers.get(NODES)
    return mod if mod is not None and mod.type == 'NODES' else None


def set_inputs(obj, samples):
    """The modifier's inputs from the stroke's settings, by name (a user's group may have any of them)."""
    mod = _modifier(obj)
    ng = mod.node_group if mod is not None else None
    if ng is None:
        return
    sym = int(samples.get("sym", 0))
    values = {"Style": STYLE_NAMES.get(samples["style"], STYLE_NAMES['TUBE']),
              "Taper": float(samples.get("taper", 0.0)),
              "Mirror X": bool(sym & 1), "Mirror Y": bool(sym & 2), "Mirror Z": bool(sym & 4),
              "Mirror Center": [float(v) for v in samples["c"]]}
    inputs = mod.properties.inputs
    for item in ng.interface.items_tree:
        if item.item_type != 'SOCKET' or item.in_out != 'INPUT' or item.name not in values:
            continue
        socket = getattr(inputs, item.identifier, None)
        if socket is None:
            continue
        value = values[item.name]
        try:
            current = socket.value
            if hasattr(current, "__len__") and not isinstance(current, str):
                current = list(current)
            if current != value:
                socket.value = value
        except (AttributeError, TypeError, ValueError):
            pass


# --- stroke objects ---------------------------------------------------------

def new_object(context, style, color_id, origin, nodes=None):
    """An empty stroke object at origin: a Curves stroke with the node group (nodes, else VR Stroke)."""
    data = bpy.data.hair_curves.new("Stroke")
    obj = bpy.data.objects.new("Stroke", data)
    collection(context).objects.link(obj)
    obj.location = origin
    data.materials.append(material(color_id))
    mod = obj.modifiers.new(NODES, 'NODES')
    mod.node_group = nodes if nodes is not None else nodes_group()
    return obj


def is_stroke(obj):
    return obj is not None and obj.type in {'CURVES', 'MESH'} and PROP in obj


def is_curve_stroke(obj):
    return is_stroke(obj) and obj.type == 'CURVES'


def save(obj, samples):
    """Keep what the stroke was drawn with (a Curves stroke's samples are its points already)."""
    old = obj.get(PROP)
    version = (old.get("v", 0) + 1) if old is not None else 0
    data = {
        "style": samples["style"], "sym": int(samples.get("sym", 0)),
        "c": [float(x) for x in samples["c"]],
        "taper": float(samples.get("taper", 0.0)),
        "v": version,
    }
    if obj.type == 'MESH':
        data["p"] = samples["p"].astype(float).ravel().tolist()
        data["w"] = samples["w"].astype(float).tolist()
        data["s"] = samples["s"].astype(float).ravel().tolist()
    obj[PROP] = data
    _cache.pop(obj.name, None)


_cache = {}  # object name -> (key, samples)


def forget_caches():
    _cache.clear()


def _attribute(cd, name, kind, domain):
    attr = cd.attributes.get(name)
    if attr is not None and (attr.data_type != kind or attr.domain != domain):
        cd.attributes.remove(attr)
        attr = None
    return attr if attr is not None else cd.attributes.new(name, kind, domain)


def _read(cd, name, width, default):
    attr = cd.attributes.get(name)
    n = len(cd.points)
    if attr is None or attr.domain != 'POINT' or len(attr.data) != n:
        return np.tile(np.asarray(default, dtype=np.float64), (n, 1)) if width > 1 else np.full(n, default, float)
    out = np.empty(n * width, dtype=np.float32)
    attr.data.foreach_get("vector" if width > 1 else "value", out)
    return (out.reshape(-1, width) if width > 1 else out).astype(np.float64)


def load(obj):
    data = obj[PROP]
    key = obj.name
    meta = (data.get("v", 0), len(obj.data.points) if obj.type == 'CURVES' else -1)
    hit = _cache.get(key)
    if hit is not None and hit[0] == meta:
        return hit[1]
    samples = {
        "style": data["style"], "sym": int(data.get("sym", 0)),
        "c": np.array(data["c"].to_list() if hasattr(data["c"], "to_list") else data["c"], dtype=np.float64),
        "taper": float(data.get("taper", 0.0)),
    }
    if obj.type == 'CURVES':
        cd = obj.data
        # One curve per stroke; a curve the user split or joined on the desktop reads as one run of points.
        samples["p"] = _read(cd, "position", 3, (0.0, 0.0, 0.0))
        samples["w"] = _read(cd, "radius", 1, 0.005) * 2.0
        samples["s"] = _read(cd, "custom_normal", 3, (0.0, 0.0, 1.0))
    else:
        samples["p"] = np.array(data["p"].to_list(), dtype=np.float64).reshape(-1, 3)
        samples["w"] = np.array(data["w"].to_list(), dtype=np.float64)
        samples["s"] = np.array(data["s"].to_list(), dtype=np.float64).reshape(-1, 3)
    _cache[key] = (meta, samples)
    return samples


def write_curve(cd, samples, preview=False):
    """The samples into a Curves datablock: one Catmull-Rom curve (SUBDIV points per segment; while
    drawing a long one, only the samples)."""
    p = np.asarray(samples["p"], dtype=np.float64)
    n = len(p)
    if n == 0:
        if len(cd.curves):
            cd.remove_curves()
        return
    if len(cd.curves) != 1:
        if len(cd.curves):
            cd.remove_curves()
        cd.add_curves([n])
    elif len(cd.points) != n:
        cd.resize_curves([n])
    cd.set_types(type='CATMULL_ROM')
    cd.attributes["position"].data.foreach_set("vector", p.astype(np.float32).ravel())
    radius = np.asarray(samples["w"], dtype=np.float64) / 2.0
    _attribute(cd, "radius", 'FLOAT', 'POINT').data.foreach_set("value", radius.astype(np.float32))
    normals = _normalize(np.asarray(samples["s"], dtype=np.float64).reshape(-1, 3))
    _attribute(cd, "custom_normal", 'FLOAT_VECTOR', 'POINT').data.foreach_set(
        "vector", normals.astype(np.float32).ravel())
    _attribute(cd, "normal_mode", 'INT8', 'CURVE').data.foreach_set("value", [NORMAL_FREE])
    resolution = 1 if preview and n > PREVIEW_SAMPLES else SUBDIV
    _attribute(cd, "resolution", 'INT', 'CURVE').data.foreach_set("value", [resolution])
    cd.update_tag()


def rebuild(obj, samples, preview=False):
    if obj.type == 'CURVES':
        write_curve(obj.data, samples, preview)
        if PROP not in obj:
            # Only while it's drawn: afterwards the modifier is the user's to change (Edit keeps its Style).
            set_inputs(obj, samples)
        _cache.pop(obj.name, None)
        return
    write_mesh(obj.data, *build(samples, preview), smooth=samples["style"] == 'TUBE')


def forget(obj):
    """Sculpted or otherwise edited as a mesh: it stops being an editable stroke."""
    if is_stroke(obj):
        del obj[PROP]
        _cache.pop(obj.name, None)


def to_mesh(context, obj):
    """A Curves stroke as a plain mesh object in its place, mirrored copies included (to sculpt or paint
    it). Returns the new object, or obj itself when it isn't a Curves stroke or makes no mesh."""
    if not is_curve_stroke(obj):
        return obj
    depsgraph = context.evaluated_depsgraph_get()
    geometry = obj.evaluated_get(depsgraph).evaluated_geometry()
    shown = geometry.mesh  # (new_from_object can't take a Curves object; a copy of its result can.)
    if shown is None or not len(shown.polygons):
        return obj
    mesh = shown.copy()
    name, matrix, parent = obj.name, obj.matrix_world.copy(), obj.parent
    collections = list(obj.users_collection)
    if not len(mesh.materials):
        for mat in obj.data.materials:
            mesh.materials.append(mat)
    was_active = context.view_layer.objects.active == obj
    selected = obj.select_get()
    delete(obj)
    new = bpy.data.objects.new(name, mesh)
    mesh.name = name
    for coll in collections:
        coll.objects.link(new)
    new.parent = parent
    new.matrix_world = matrix
    if selected:
        new.select_set(True)
    if was_active:
        context.view_layer.objects.active = new
    return new


def distance(obj, point_world):
    """Distance from a world point to the stroke's surface (approximate), any mirrored copy."""
    s = load(obj)
    if len(s["p"]) == 0:
        return math.inf
    inv = obj.matrix_world.inverted_safe()
    p = np.array(inv @ Vector(point_world))
    scale = max(primitives.axis_scale(obj.matrix_world))
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
    data = obj.data
    _cache.pop(obj.name, None)
    bpy.data.objects.remove(obj)
    if data is not None and data.users == 0:
        if isinstance(data, bpy.types.Curves):
            bpy.data.hair_curves.remove(data)
        elif isinstance(data, bpy.types.Mesh):
            bpy.data.meshes.remove(data)


RAW_LINE_WIDTH = 3.0  # Pixels: the headset draws a bare curve one pixel wide, too thin to see.


def shows_raw(obj):
    """Whether a Curves stroke shows just its curve: our group set to Raw, or no working modifier. (The
    user's own group shows whatever it makes.)"""
    mod = _modifier(obj)
    if mod is None or not mod.show_viewport or mod.node_group is None:
        return True
    ng = mod.node_group
    if ng.get(NODES_TAG) is None:
        return False
    item = ng.interface.items_tree.get("Style")
    socket = getattr(mod.properties.inputs, item.identifier, None) if item is not None else None
    return socket is not None and socket.value == STYLE_NAMES['RAW']


def draw_raw(context):
    """Raw strokes in the headset, mirrored copies included: a line a few pixels wide in their color."""
    import gpu
    from gpu_extras.batch import batch_for_shader
    groups = {}
    for obj in context.view_layer.objects:
        if not is_curve_stroke(obj) or not obj.visible_get() or not shows_raw(obj):
            continue
        smp = load(obj)
        if len(smp["p"]) < 2:
            continue
        pts, _ = catmull_rom(smp["p"], (), SUBDIV)
        m = np.array(obj.matrix_world)
        mats = obj.data.materials
        color = tuple(mats[0].diffuse_color[:3]) if len(mats) and mats[0] is not None else (0.9, 0.9, 0.9)
        for mask in mirror_variants(smp["sym"]):
            q = reflect(pts, mask, smp["c"]) if mask else pts
            w = q @ m[:3, :3].T + m[:3, 3]
            groups.setdefault(color, []).append(np.stack([w[:-1], w[1:]], axis=1).reshape(-1, 3))
    if not groups:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(RAW_LINE_WIDTH)
    for color, segs in groups.items():
        batch = batch_for_shader(shader, 'LINES', {"pos": np.concatenate(segs).astype(np.float32)})
        shader.uniform_float("color", (*color, 1.0))
        batch.draw(shader)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('NONE')


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    """A Curves stroke edited on the desktop (its points moved without a new version): read it again."""
    if not _cache:
        return
    changed = {u.id.original.name for u in depsgraph.updates
               if isinstance(u.id, bpy.types.Curves) and u.is_updated_geometry}
    if not changed:
        return
    for name in list(_cache):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'CURVES' or obj.data.name in changed:
            _cache.pop(name, None)


def register():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)


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

# Most recent last: ("mesh", object name), ("note", [(layer name, frame number, stroke count)]),
# ("pencil", (object name, layer name, frame number, stroke count)), ("curve", object name) for a point
# added to a curve or ("curve_close", object name).
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
        elif kind in {"curve", "curve_close"}:
            from . import curves
            what = curves.undo(kind, ref)
            if what is not None:
                return what
    return None
