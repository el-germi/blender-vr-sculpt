"""Keep UVs, corner colors and shape keys through dyntopo, from rest positions.

Dyntopo resets every face-corner layer (UVs, corner colors) and scrambles shape keys, but it carries
point attributes onto the vertices it makes (README, "What we learned about dynamic topology"). So:

- Before a session, each vertex gets its position as a hidden point attribute (REST), and the mesh is
  copied with its vertices moved to those rest positions (the source, a fake-user mesh). Shape keys
  become offsets from the basis in point attributes, and are taken off the mesh while dyntopo runs.
- When the session ends, every face of the new mesh is matched to the source in rest space, and its
  corners take their values from the source triangle nearest to their vertex: interpolated for float
  data, the nearest corner for the rest. For UVs, where that triangle's UV is far from the one the
  face's own triangle (nearest its center) gives, the corner is across a seam and takes the face's
  (extrapolated), so faces don't smear across seams. The shape keys are rebuilt from the offsets, with their settings, action and drivers.

obj.data isn't written while dyntopo sculpts, so it still holds the pre-session mesh for the whole
session: a session can be opened late (tick() opens one for dyntopo turned on from the desktop, if the
mesh already has rest positions). Leaving Sculpt Mode writes the new mesh, and tick() (or set_object_mode
from VR) closes the session. Turning dyntopo off writes it too, so the corners are restored right away
then, and once more on leaving. The state is on the mesh (ID properties), so it survives saving.
"""

import time

import bpy
import numpy as np
from mathutils.bvhtree import BVHTree

REST = ".vrs_rest"  # point attribute: where each vertex was when the session's source was taken
KEY_PREFIX = ".vrs_key_"  # point attributes: shape key offsets from the basis, by key index
SOURCE = "vrs_rest_source"  # mesh ID property: the source mesh while a session is open
KEYS = "vrs_rest_keys"  # mesh ID property: the shape keys' old Key while they're kept as offsets
DIRTY = "vrs_rest_dirty"  # mesh ID property: dyntopo has run since the corners were last restored
DYNTOPO = "vrs_dyntopo"  # mesh ID property: dyntopo was last turned on (Blender keeps its own flag hidden)
SKIP = {"custom_normal"}  # corner layers that mean nothing after sculpting
INSIDE_EPS = 1e-4  # barycentric slack for "on this triangle"
SEAM_TOLERANCE = 1.0  # a corner's UV may differ from its face's by this many of the face's UV sizes

# How to read each attribute type: (foreach property, components, interpolated).
_TYPES = {
    'FLOAT': ("value", 1, True), 'FLOAT2': ("vector", 2, True), 'FLOAT_VECTOR': ("vector", 3, True),
    'FLOAT_COLOR': ("color", 4, True), 'BYTE_COLOR': ("color", 4, True), 'QUATERNION': ("value", 4, False),
    'INT': ("value", 1, False), 'INT8': ("value", 1, False), 'BOOLEAN': ("value", 1, False),
    'INT32_2D': ("value", 2, False),
}


def _corner_layers(me):
    return [a for a in me.attributes
            if a.domain == 'CORNER' and not a.name.startswith(".") and a.name not in SKIP
            and a.data_type in _TYPES]


def worth_keeping(me):
    return bool(_corner_layers(me)) or me.shape_keys is not None


def is_open(me):
    return SOURCE in me


def _read(attr, n):
    prop, k, _interp = _TYPES[attr.data_type]
    dtype = {'INT': np.int32, 'INT8': np.int8, 'INT32_2D': np.int32, 'BOOLEAN': bool}.get(attr.data_type, np.float64)
    a = np.empty(n * k, dtype=dtype)
    attr.data.foreach_get(prop, a)
    return a.reshape(n, k)


def _write(attr, values):
    prop, _k, _interp = _TYPES[attr.data_type]
    attr.data.foreach_set(prop, values.ravel())


def _positions(me):
    a = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def _set_vectors(me, name, values):
    attr = me.attributes.get(name)
    if attr is None or attr.domain != 'POINT' or attr.data_type != 'FLOAT_VECTOR':
        if attr is not None:
            me.attributes.remove(attr)
        attr = me.attributes.new(name, 'FLOAT_VECTOR', 'POINT')
    attr.data.foreach_set("vector", values.ravel())


def _vectors(me, name):
    attr = me.attributes.get(name)
    if attr is None or attr.domain != 'POINT' or attr.data_type != 'FLOAT_VECTOR':
        return None
    a = np.empty(len(me.vertices) * 3)
    attr.data.foreach_get("vector", a)
    return a.reshape(-1, 3)


# --- session -------------------------------------------------------------------------

def prepare(obj):
    """Before dyntopo turns on (or Sculpt Mode is entered with it on), while obj.data is still the mesh
    as it is: rest positions from now, shape keys as offsets, and the source. Nothing to keep: nothing."""
    me = obj.data
    if is_open(me) or not worth_keeping(me):
        return False
    _stash_keys(obj)
    _set_vectors(me, REST, _positions(me))
    begin(me)
    return True


def begin(me):
    """Open a session: copy the mesh, with its vertices at their rest positions, as the source."""
    rest = _vectors(me, REST)
    if rest is None or is_open(me):
        return False
    src = me.copy()
    src.name = ".VRS rest " + me.name
    src.use_fake_user = True
    src.vertices.foreach_set("co", rest.ravel())
    src.update()
    me[SOURCE] = src
    me[DIRTY] = True
    return True


def finish(obj):
    """Close the session: restore the corners and the shape keys, and take the new rest positions."""
    me = obj.data
    if not is_open(me):
        return False
    src = me[SOURCE]
    if src is not None and me.get(DIRTY, True):
        restore_corners(me, src)
    _unstash_keys(obj)
    for name in [a.name for a in me.attributes if a.name.startswith(KEY_PREFIX)]:
        me.attributes.remove(me.attributes[name])
    _set_vectors(me, REST, _positions(me))
    del me[SOURCE]
    me.pop(DIRTY, None)
    if src is not None:
        src.use_fake_user = False
        bpy.data.meshes.remove(src)
    me.update()
    return True


def sync(obj):
    """Open, restore or close obj's session to match its mode and dyntopo state."""
    if obj is None or obj.type != 'MESH':
        return
    me = obj.data
    dyntopo = obj.mode == 'SCULPT' and obj.use_dynamic_topology_sculpting
    if dyntopo and not me.get(DYNTOPO):
        me[DYNTOPO] = True
    if is_open(me):
        if obj.mode == 'EDIT':
            pass  # Edit Mode works on its own copy and writes it back on leaving: restore after that.
        elif obj.mode != 'SCULPT':
            finish(obj)
        elif dyntopo:
            if not me.get(DIRTY):
                me[DIRTY] = True
        elif me.get(DIRTY, True) and me[SOURCE] is not None:
            # Dyntopo turned off in Sculpt Mode: the new mesh is written, so show it textured now.
            restore_corners(me, me[SOURCE])
            me[DIRTY] = False
    elif dyntopo and REST in me.attributes and worth_keeping(me):
        begin(me)  # Turned on from the desktop: obj.data is still the mesh from before the session.


def tick(context):
    """From the live timer: sessions end however Sculpt Mode is left (desktop included)."""
    active = context.view_layer.objects.active if context.view_layer else None
    sync(active)
    for obj in bpy.data.objects:
        if obj is not active and obj.type == 'MESH' and SOURCE in obj.data:
            sync(obj)


# --- corners ---------------------------------------------------------------------------

def _nearest(bvh, points):
    """(triangle index, point on it) nearest each point."""
    found = [bvh.find_nearest(p) for p in points.tolist()]
    tri = np.array([f[2] if f[2] is not None else 0 for f in found], dtype=np.int64)
    at = np.array([f[0] if f[0] is not None else p for f, p in zip(found, points)], dtype=np.float64)
    return tri, at.reshape(-1, 3)


def _same_points(a, b):
    """For each point of a, the index of a point of b at exactly the same place (as float32), or -1."""
    rows = np.ascontiguousarray(np.vstack([b, a]).astype(np.float32)).view(np.int32)
    _u, first, inverse = np.unique(rows, axis=0, return_index=True, return_inverse=True)
    match = first[inverse.ravel()[len(b):]]
    return np.where(match < len(b), match, -1)


def _same_triangles(same, loop_v, starts, totals, tri_verts, n_verts):
    """For each face that is a source triangle (its three vertices on that triangle's), that triangle;
    else -1."""
    out = np.full(len(starts), -1, dtype=np.int64)
    tris = np.flatnonzero(totals == 3)
    if not len(tris):
        return out
    corners = same[loop_v[starts[tris][:, None] + np.arange(3)]]
    ok = (corners >= 0).all(axis=1)
    tris, corners = tris[ok], np.sort(corners[ok], axis=1)
    n = max(n_verts, int(tri_verts.max()) + 1)
    src_keys = np.sort(tri_verts, axis=1)
    src_keys = (src_keys[:, 0] * n + src_keys[:, 1]) * n + src_keys[:, 2]
    order = np.argsort(src_keys)
    keys = (corners[:, 0] * n + corners[:, 1]) * n + corners[:, 2]
    pos = np.minimum(np.searchsorted(src_keys[order], keys), len(order) - 1)
    hit = src_keys[order][pos] == keys
    out[tris[hit]] = order[pos[hit]]
    return out


def _weights(corners, p):
    """Barycentric weights of points p (n, 3) in triangles corners (n, 3, 3), projected onto each plane
    (outside the triangle they extrapolate)."""
    a = corners[:, 0]
    v0, v1, v2 = corners[:, 1] - a, corners[:, 2] - a, p - a
    d00 = np.einsum("ij,ij->i", v0, v0)
    d01 = np.einsum("ij,ij->i", v0, v1)
    d11 = np.einsum("ij,ij->i", v1, v1)
    d20 = np.einsum("ij,ij->i", v2, v0)
    d21 = np.einsum("ij,ij->i", v2, v1)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-20
    den = np.where(ok, den, 1.0)
    wb = np.where(ok, (d11 * d20 - d01 * d21) / den, 0.0)
    wc = np.where(ok, (d00 * d21 - d01 * d20) / den, 0.0)
    return np.stack([1.0 - wb - wc, wb, wc], axis=1)


def restore_corners(me, src):
    """Give every corner layer of the source back to me, matched through the rest positions."""
    layers = _corner_layers(src)
    rest = _vectors(me, REST)
    if not layers or rest is None or not len(me.polygons):
        return 0
    t0 = time.perf_counter()
    src.calc_loop_triangles()
    nt = len(src.loop_triangles)
    if nt == 0:
        return 0
    tri_verts = np.empty(nt * 3, dtype=np.int64)
    tri_loops = np.empty(nt * 3, dtype=np.int64)
    src.loop_triangles.foreach_get("vertices", tri_verts)
    src.loop_triangles.foreach_get("loops", tri_loops)
    tri_verts, tri_loops = tri_verts.reshape(-1, 3), tri_loops.reshape(-1, 3)
    sco = _positions(src)
    bvh = BVHTree.FromPolygons(sco.tolist(), tri_verts.tolist(), all_triangles=True)

    # The triangle nearest each vertex (and the point on it), and nearest each face's center. Where
    # dyntopo left the mesh alone, vertices sit exactly on source vertices and triangles are source
    # triangles; only the rest needs the BVH (most of the time this takes).
    nv = len(me.vertices)
    same = _same_points(rest, sco)
    first_tri = np.full(len(sco), nt, dtype=np.int64)
    np.minimum.at(first_tri, tri_verts.ravel(), np.repeat(np.arange(nt), 3))
    same = np.where((same >= 0) & (first_tri[np.maximum(same, 0)] < nt), same, -1)  # Not on a loose vertex.
    vert_tri = np.where(same >= 0, first_tri[np.maximum(same, 0)], 0)
    vert_at = rest.copy()
    todo = np.flatnonzero(same < 0)
    vert_tri[todo], vert_at[todo] = _nearest(bvh, rest[todo])
    loop_v = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", loop_v)
    starts = np.empty(len(me.polygons), dtype=np.int64)
    totals = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_start", starts)
    me.polygons.foreach_get("loop_total", totals)
    loop_face = np.repeat(np.arange(len(me.polygons)), totals)
    face_tri = _same_triangles(same, loop_v, starts, totals, tri_verts, nv)
    todo = np.flatnonzero(face_tri < 0)
    if len(todo):
        centers = np.zeros((len(me.polygons), 3))
        np.add.at(centers, loop_face, rest[loop_v])
        centers /= totals[:, None]
        face_tri[todo] = _nearest(bvh, centers[todo])[0]

    # Which source triangle each corner reads: its face's own triangle (nearest the face's center), or
    # another of that source polygon's triangles, when the corner is on it (an untouched face, a small
    # new one); else the triangle nearest its vertex. At a vertex where triangles disagree (a seam, a
    # pole) the nearest one could be from the other side, so the face's own come first.
    tri_poly = np.empty(nt, dtype=np.int64)
    src.loop_triangles.foreach_get("polygon_index", tri_poly)
    poly_first = np.full(len(src.polygons), nt, dtype=np.int64)
    np.minimum.at(poly_first, tri_poly, np.arange(nt))
    poly_count = np.bincount(tri_poly, minlength=len(src.polygons))
    own_tri = face_tri[loop_face]
    own_poly = tri_poly[own_tri]
    p = rest[loop_v]
    tri = vert_tri[loop_v].copy()
    w = _weights(sco[tri_verts[tri]], vert_at[loop_v])
    on_own = np.zeros(len(loop_v), dtype=bool)
    for j in range(int(poly_count.max())):
        cand = np.minimum(poly_first[own_poly] + j, nt - 1)
        cw = _weights(sco[tri_verts[cand]], p)
        hit = ~on_own & (j < poly_count[own_poly]) & (cw >= -INSIDE_EPS).all(axis=1)
        tri[hit], w[hit] = cand[hit], cw[hit]
        on_own |= hit
    own_w = _weights(sco[tri_verts[own_tri]], p)

    n_loops = len(me.loops)
    n_src = len(src.loops)
    for layer in layers:
        name, kind = layer.name, layer.data_type
        values = _read(layer, n_src)
        src_values = values.astype(np.float64)
        if _TYPES[kind][2]:
            out = np.einsum("ij,ijk->ik", w, src_values[tri_loops[tri]])
            if name in src.uv_layers:
                # Across a seam the nearest triangle's UVs jump (by about the whole texture); then the
                # face's own triangle, extrapolated, keeps the face in one piece.
                own_uv = src_values[tri_loops[own_tri]]
                own = np.einsum("ij,ijk->ik", own_w, own_uv)
                size = np.linalg.norm(own_uv - own_uv[:, [1, 2, 0]], axis=2).max(axis=1)
                seam = ~on_own & (np.linalg.norm(out - own, axis=1) > size * SEAM_TOLERANCE)
                out[seam] = own[seam]
            if kind in {'FLOAT_COLOR', 'BYTE_COLOR'}:
                out = np.clip(out, 0.0, None if kind == 'FLOAT_COLOR' else 1.0)
        else:
            out = values[tri_loops[tri][np.arange(n_loops), np.argmax(w, axis=1)]]
        target = me.attributes.get(name)
        if target is None or target.domain != 'CORNER' or target.data_type != kind:
            if target is not None:
                me.attributes.remove(target)
            target = me.attributes.new(name, kind, 'CORNER')
        _write(target, out.astype(values.dtype))
    _copy_active(me, src)
    me.update()
    print(f"VR Sculpt rest: {', '.join(a.name for a in layers)} back on {me.name} ({len(me.vertices)} vertices,"
          f" {len(me.polygons)} faces) in {(time.perf_counter() - t0) * 1000:.0f} ms", flush=True)
    return len(layers)


def _copy_active(me, src):
    """Which UV map and color attribute are active / used for rendering, by name."""
    for uv in src.uv_layers:
        if uv.name in me.uv_layers:
            if uv.active:
                me.uv_layers.active = me.uv_layers[uv.name]
            if uv.active_render:
                me.uv_layers[uv.name].active_render = True
    colors = src.color_attributes
    if colors.active_color_name in me.color_attributes:
        me.color_attributes.active_color = me.color_attributes[colors.active_color_name]
    if 0 <= colors.render_color_index < len(colors):
        name = colors[colors.render_color_index].name
        if name in me.color_attributes:
            me.color_attributes.render_color_index = list(me.color_attributes.keys()).index(name)


# --- shape keys --------------------------------------------------------------------------

def _stash_keys(obj):
    """Shape keys to offsets from the basis; the old Key (fake user) keeps their settings and animation."""
    me = obj.data
    key = me.shape_keys
    if key is None:
        return
    n = len(me.vertices)
    basis = np.empty(n * 3)
    key.reference_key.data.foreach_get("co", basis)
    for i, kb in enumerate(key.key_blocks):
        if kb == key.reference_key:
            continue
        co = np.empty(n * 3)
        kb.data.foreach_get("co", co)
        _set_vectors(me, KEY_PREFIX + str(i), (co - basis).reshape(-1, 3))
    key.use_fake_user = True
    me[KEYS] = key
    me["vrs_rest_active_key"] = obj.active_shape_key_index
    me["vrs_rest_show_only"] = obj.show_only_shape_key
    obj.shape_key_clear()


def _unstash_keys(obj):
    me = obj.data
    old = me.get(KEYS)
    if old is None:
        return
    base = _positions(me)
    blocks = []
    for i, okb in enumerate(old.key_blocks):
        kb = obj.shape_key_add(name=okb.name, from_mix=False)
        offset = _vectors(me, KEY_PREFIX + str(i))
        if offset is not None:
            kb.data.foreach_set("co", (base + offset).ravel())
        blocks.append((okb, kb))
    key = me.shape_keys
    key.use_relative = old.use_relative
    key.eval_time = old.eval_time
    for okb, kb in blocks:
        kb.relative_key = key.key_blocks.get(okb.relative_key.name, key.reference_key)
        if okb.slider_min >= kb.slider_max:  # Each must stay on its side of the other.
            kb.slider_max, kb.slider_min = okb.slider_max, okb.slider_min
        else:
            kb.slider_min, kb.slider_max = okb.slider_min, okb.slider_max
        kb.value = okb.value
        kb.mute = okb.mute
        kb.vertex_group = okb.vertex_group
        kb.interpolation = okb.interpolation
        kb.lock_shape = okb.lock_shape
    _copy_animation(old, key)
    obj.active_shape_key_index = min(me.get("vrs_rest_active_key", 0), len(key.key_blocks) - 1)
    obj.show_only_shape_key = bool(me.get("vrs_rest_show_only", False))
    for prop in (KEYS, "vrs_rest_active_key", "vrs_rest_show_only"):
        me.pop(prop, None)
    old.use_fake_user = False  # No users left: Blender drops it when the file is saved and loaded.


def _copy_animation(src, dst):
    ad = src.animation_data
    if ad is None:
        return
    new = dst.animation_data_create()
    if ad.action is not None:
        new.action = ad.action
        slot = getattr(ad, "action_slot", None)
        if slot is not None and hasattr(new, "action_slot"):
            try:
                new.action_slot = slot
            except (TypeError, RuntimeError):
                pass
    for fc in ad.drivers:
        nf = new.drivers.new(fc.data_path, index=fc.array_index)
        d, sd = nf.driver, fc.driver
        d.type = sd.type
        d.use_self = sd.use_self
        if sd.type == 'SCRIPTED':
            d.expression = sd.expression
        for v in sd.variables:
            nv = d.variables.new()
            nv.name = v.name
            nv.type = v.type
            for t, st in zip(nv.targets, v.targets):
                try:
                    if v.type == 'SINGLE_PROP':
                        t.id_type = st.id_type
                    t.id = st.id
                    t.data_path = st.data_path
                    t.bone_target = st.bone_target
                    t.transform_type = st.transform_type
                    t.transform_space = st.transform_space
                    t.rotation_mode = st.rotation_mode
                except (TypeError, AttributeError):
                    pass
        if len(fc.keyframe_points):
            while nf.modifiers:
                nf.modifiers.remove(nf.modifiers[0])
            nf.keyframe_points.add(len(fc.keyframe_points))
            for k, sk in zip(nf.keyframe_points, fc.keyframe_points):
                k.co, k.interpolation = sk.co, sk.interpolation
                k.handle_left, k.handle_right = sk.handle_left, sk.handle_right
