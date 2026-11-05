"""UV mapping for Model mode's UV page: seams marked by hand, unwrapping, the Fix brush, tools that turn
and size one island from the surface, and a checker / stretch / island view drawn on the mesh in the
headset.

- Seams: the tip marks the edges it passes near as seams (Blender's own "uv_seam", what Mark Seam
  sets); the off hand clears them. A fast sweep leaves gaps between samples, so each new vertex the
  tip reaches is joined to the last one along the shortest edge path. With Live on, the mesh is
  unwrapped again when the trigger is let go.
- Fix: the faces the tip brushes over unwrap again, and only those: their corners on the border of the
  brushed patch are pinned for the unwrap (and unpinned after), so the patch stays stitched into the
  layout and nothing outside it moves. For where sculpting has stretched or scrambled the UVs.
- Twist: hold the trigger on an island and roll the wrist: its texture turns on the surface with the hand.
- Size: hold the trigger on an island and raise or lower the hand: it gets more or less of the UV square.
  Both are previewed on the island in the shader, and written (and packed) on release.
- The view: a checker on the UVs, each face colored by how much its UVs are stretched against the
  mesh (blue: true to the surface; red: twice or half the size, or skewed as much), or each island in
  its own color, following a seam stroke while it's traced, with the seams that don't separate
  anything in another color.

Unwrapping is Blender's uv.unwrap, which only runs in Edit Mode: the object is taken into Edit Mode
alone (the rest of the selection is put back after) and out again. Selection and pins are put back as
they were.
"""

import heapq
import math
import time
from contextlib import contextmanager

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

METHODS = (('ANGLE_BASED', "Angle"), ('CONFORMAL', "Conformal"), ('MINIMUM_STRETCH', "Min stretch"))
MARGIN = 0.01  # between islands, as a share of the UV square
SMART_ANGLE = math.radians(66.0)  # Smart UV Project's default
SHARP_ANGLE = math.radians(30.0)  # Seams from Sharp, when the object has no Auto shading angle
PATH_SLACK = 1.6  # a seam path may be this much longer than the straight line between its ends
SEAM_COLOR = (1.0, 0.32, 0.28)
OPEN_SEAM_COLOR = (1.0, 0.8, 0.2)  # a seam with the same island on both sides
FIX_COLOR = (0.55, 0.9, 0.45)
ISLAND_COLOR = (0.45, 0.8, 1.0)  # Twist and Size
STRETCH_FULL = 1.0  # log2 of the stretch shown fully red: twice (or half) the size
VIEW_ALPHA = 0.9
HOVER_ALPHA = 0.35
ISLAND_PULL = 1.5  # the island under the tip sits this many times the view's pull toward the eye
ISLAND_TOOLS = {'TWIST', 'SIZE'}
ISLAND_REACH = 0.06  # how near the surface the tip picks an island (hand-space meters)
UV_MATCH = 1e-5  # corners this close in UV space are stitched together
GOLDEN = 0.61803398875  # hue step between island labels: near labels get far colors
TWIST_STEP = math.radians(15.0)  # Twist's snap
TWIST_MIN = math.radians(0.5)  # less than this isn't a change
SIZE_DOUBLE = 0.1  # raising the hand this far (hand-space meters) doubles an island's size
SIZE_MIN, SIZE_MAX = 0.1, 10.0  # the most one Size stroke can do
FOLLOW_MIN = 0.05  # the island view rebuilds at most this often during a seam stroke (seconds)
FOLLOW_SHARE = 4.0  # and waits this many times its last rebuild, so a dense mesh costs a share of the frames
TOOL_STYLES = {'SEAM': (SEAM_COLOR, "seam"), 'FIX': (FIX_COLOR, "fix"), 'TWIST': (ISLAND_COLOR, "gizmo_turn"),
               'SIZE': (ISLAND_COLOR, "gizmo_size")}

ORIENTED = "vrs_uv_oriented"  # On a mesh with an island turned by hand: packing mustn't turn islands.

# Bumped whenever our own operations change UVs or seams, and on depsgraph geometry updates; the view
# rebuilds when it changes.
_version = [0]
_message = {"text": "", "time": 0.0}
MESSAGE_SECONDS = 4.0


def say(text):
    _message.update(text=text, time=time.monotonic())


def message():
    return _message["text"] if time.monotonic() - _message["time"] < MESSAGE_SECONDS else ""


def changed():
    _version[0] += 1


# --- mesh data ---------------------------------------------------------------------

def seams(mesh):
    out = np.zeros(len(mesh.edges), dtype=bool)
    if len(out):
        mesh.edges.foreach_get("use_seam", out)
    return out


def set_seams(mesh, values):
    mesh.edges.foreach_set("use_seam", np.asarray(values, dtype=bool))
    mesh.update()
    changed()


def clear_seams(obj):
    set_seams(obj.data, np.zeros(len(obj.data.edges), dtype=bool))


def has_uvs(obj):
    return obj is not None and obj.type == 'MESH' and len(obj.data.uv_layers) > 0


def _uvs(mesh):
    out = np.empty(len(mesh.loops) * 2, dtype=np.float64)
    mesh.uv_layers.active.data.foreach_get("uv", out)
    return out.reshape(-1, 2)


def _loop_faces(mesh):
    """(face of each loop, vertex of each loop)."""
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    loop_v = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("vertex_index", loop_v)
    return np.repeat(np.arange(len(mesh.polygons)), totals), loop_v


def _pin_attr_name(mesh):
    return ".pn." + mesh.uv_layers.active.name


def _get_pins(mesh):
    """The active UV map's pins, or None if it has none."""
    attr = mesh.attributes.get(_pin_attr_name(mesh))
    if attr is None:
        return None
    out = np.zeros(len(mesh.loops), dtype=bool)
    attr.data.foreach_get("value", out)
    return out


def _set_pins(mesh, values):
    """values None removes the pins."""
    name = _pin_attr_name(mesh)
    attr = mesh.attributes.get(name)
    if values is None:
        if attr is not None:
            mesh.attributes.remove(attr)
        return
    if attr is None:
        attr = mesh.attributes.new(name, 'BOOLEAN', 'CORNER')
    attr.data.foreach_set("value", np.asarray(values, dtype=bool))


def _get_selection(mesh):
    out = []
    for items in (mesh.vertices, mesh.edges, mesh.polygons):
        sel = np.zeros(len(items), dtype=bool)
        if len(sel):
            items.foreach_get("select", sel)
        out.append(sel)
    return out


def _set_selection(mesh, selection):
    for items, sel in zip((mesh.vertices, mesh.edges, mesh.polygons), selection):
        if len(sel):
            items.foreach_set("select", sel)


def _edge_pairs(mesh):
    """(a, b, edge): corners a and b lie on the same edge (more than two faces on one edge chain up)."""
    loop_e = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("edge_index", loop_e)
    order = np.argsort(loop_e, kind='stable')
    same = loop_e[order[1:]] == loop_e[order[:-1]]
    a, b = order[:-1][same], order[1:][same]
    return a, b, loop_e[a]


def _next_loops(mesh):
    """The next corner around its face, for each corner."""
    totals = np.empty(len(mesh.polygons), dtype=np.int64)
    mesh.polygons.foreach_get("loop_total", totals)
    out = np.arange(1, len(mesh.loops) + 1)
    ends = np.cumsum(totals)
    out[ends - 1] = ends - totals
    return out


def _components(n, a, b):
    """Per item (of n, joined in pairs a[i], b[i]), the lowest item it's connected to."""
    labels = np.arange(n)
    while len(a):
        la, lb = labels[a], labels[b]
        np.minimum.at(labels, np.maximum(la, lb), np.minimum(la, lb))
        while True:
            jumped = labels[labels]
            if (jumped == labels).all():
                break
            labels = jumped
        keep = labels[a] != labels[b]
        a, b = a[keep], b[keep]
    return labels


class _Stitching:
    """Which faces meet across which edges, and where their UVs join. The slow part of finding islands, and it
    only changes with the UVs or the topology: a seam stroke keeps one and only asks it about new seams."""

    def __init__(self, mesh):
        self.loop_f, loop_v = _loop_faces(mesh)
        a, b, self.edges = _edge_pairs(mesh)
        self.face_a, self.face_b = self.loop_f[a], self.loop_f[b]
        self.n_faces, self.n_edges = len(mesh.polygons), len(mesh.edges)
        if len(mesh.uv_layers):
            uv = _uvs(mesh)
            nxt = _next_loops(mesh)
            same = loop_v[a] == loop_v[b]  # The faces wind the same way along the edge: normals disagree.
            at_a, at_next = np.where(same, b, nxt[b]), np.where(same, nxt[b], b)
            self.joined = (np.abs(uv[a] - uv[at_a]).max(1) < UV_MATCH) & (np.abs(uv[nxt[a]] - uv[at_next]).max(1) < UV_MATCH)
        else:
            self.joined = np.ones(len(a), dtype=bool)

    def labels(self, cuts=None):
        """Per face, its island's label: the lowest face index in it, so when an island splits only the new
        part changes label (and color). Faces are in one island when joined across edges whose UVs match on
        both sides, and that aren't in cuts (bool per edge)."""
        keep = self.joined if cuts is None else self.joined & ~cuts[self.edges]
        return _components(self.n_faces, self.face_a[keep], self.face_b[keep])

    def open_seams(self, labels, marked):
        """Bool per edge: one of the marked seams with the same island on both sides."""
        out = np.zeros(self.n_edges, dtype=bool)
        out[self.edges[labels[self.face_a] == labels[self.face_b]]] = True
        return out & marked


def island_labels(mesh, use_seams=True):
    """_Stitching.labels(), cut by the mesh's seams with use_seams: that shows what a new seam cuts before an
    unwrap."""
    return _Stitching(mesh).labels(seams(mesh) if use_seams else None)


def open_seams(mesh, labels):
    """Bool per edge: a seam with the same island on both sides, so it doesn't separate anything (yet)."""
    return _Stitching(mesh).open_seams(labels, seams(mesh))


def island_colors(labels):
    """(n, 4) float32 per label: bright, well apart for near labels, the same for a label as it stays."""
    hue = (labels * GOLDEN + 0.55) % 1.0
    rgb = np.clip(np.abs((hue[:, None] * 6.0 + np.array((0.0, 4.0, 2.0))) % 6.0 - 3.0) - 1.0, 0.0, 1.0)
    out = np.empty((len(labels), 4), dtype=np.float32)
    out[:, :3] = 0.95 * (0.4 + 0.6 * rgb)
    out[:, 3] = VIEW_ALPHA
    return out


def _select_faces(mesh, faces):
    """Select exactly faces (bool per face), with their vertices and edges."""
    loop_f, loop_v = _loop_faces(mesh)
    verts = np.zeros(len(mesh.vertices), dtype=bool)
    verts[loop_v[faces[loop_f]]] = True
    ev = np.empty(len(mesh.edges) * 2, dtype=np.int64)
    mesh.edges.foreach_get("vertices", ev)
    edges = verts[ev.reshape(-1, 2)].all(1)
    _set_selection(mesh, (verts, edges, faces))


def ensure_layer(mesh):
    if not len(mesh.uv_layers):
        mesh.uv_layers.new(name="UVMap")


def save(mesh):
    """What restore() needs to put the seams and UVs back on a rebuilt mesh (a primitive resized)."""
    s = seams(mesh)
    uv = _uvs(mesh) if len(mesh.uv_layers) and mesh.get("vrs_unwrapped") else None
    if not s.any() and uv is None:
        return None
    return len(mesh.edges), s, len(mesh.loops), uv


def restore(mesh, saved):
    """Put saved seams (and our UVs) back if the mesh still has the same edges and corners."""
    if saved is None:
        return False
    n_edges, s, n_loops, uv = saved
    if n_edges != len(mesh.edges):
        return False
    mesh.edges.foreach_set("use_seam", s)
    if uv is not None and n_loops == len(mesh.loops):
        ensure_layer(mesh)
        mesh.uv_layers.active.data.foreach_set("uv", uv.ravel())
        mesh["vrs_unwrapped"] = True
    return True


# --- Edit Mode round trips -----------------------------------------------------------

def _override(context, obj):
    from . import menus
    window, view = menus._desktop_view(context)
    kw = dict(active_object=obj, object=obj, edit_object=obj)
    if view is not None:
        kw.update(window=window, area=view[0], region=view[1])
    return kw


@contextmanager
def _editing(context, obj, faces=None):
    """obj alone in Edit Mode with faces (all if None) selected; on exit, its mode, the selection
    (object and mesh) and the active object as they were."""
    from . import menus
    view_layer = context.view_layer
    mode = obj.mode
    if mode == 'EDIT':
        menus.set_object_mode(context, obj, 'OBJECT')  # So obj.data is current.
        mode = 'OBJECT'
    active = view_layer.objects.active
    others = [o for o in view_layer.objects if o != obj and o.select_get()]
    was_selected = obj.select_get()
    mesh = obj.data
    selection = _get_selection(mesh)
    _select_faces(mesh, np.ones(len(mesh.polygons), dtype=bool) if faces is None else faces)
    for o in others:
        o.select_set(False)
    obj.select_set(True)
    view_layer.objects.active = obj
    try:
        menus.set_object_mode(context, obj, 'EDIT')
        with context.temp_override(**_override(context, obj)):
            yield
    finally:
        menus.set_object_mode(context, obj, mode)
        _set_selection(obj.data, selection)
        obj.select_set(was_selected)
        for o in others:
            o.select_set(True)
        view_layer.objects.active = active
        obj.data.update()


def _uv_select_all():
    """UV operators that act on the selected UVs (pack, average) need them selected too."""
    try:
        bpy.ops.uv.select_all(action='SELECT')
    except RuntimeError:
        pass


def unwrap(context, obj, method='ANGLE_BASED'):
    """Unwrap the whole mesh from its seams (pinned UVs stay)."""
    if obj is None or obj.type != 'MESH' or not len(obj.data.polygons):
        return False
    if not seams(obj.data).any() and _closed(obj.data):
        say("Can't flatten a closed mesh: trace seams first, or Smart UV")
        return False
    t0 = time.perf_counter()
    ensure_layer(obj.data)
    with _editing(context, obj):
        bpy.ops.uv.unwrap(method=method, margin=MARGIN)
    _laid_out(obj.data)
    changed()
    say(f"Unwrapped {obj.name} ({_seconds(t0)})")
    return True


def _laid_out(mesh):
    """Our UVs, from scratch: no island has been turned by hand any more."""
    mesh["vrs_unwrapped"] = True
    if ORIENTED in mesh:
        del mesh[ORIENTED]


def _closed(mesh):
    """Every edge has two faces: with no seams, there's nowhere to open it."""
    loop_e = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("edge_index", loop_e)
    return bool((np.bincount(loop_e, minlength=len(mesh.edges)) == 2).all())


def uv_area(mesh):
    """Total UV area of the mesh's faces."""
    if not len(mesh.uv_layers) or not len(mesh.loop_triangles):
        return 0.0
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", tris)
    u = _uvs(mesh)[tris.reshape(-1, 3)]
    d1, d2 = u[:, 1] - u[:, 0], u[:, 2] - u[:, 0]
    return float(np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]).sum() / 2)


def smart_project(context, obj):
    """Smart UV Project, then seams along the islands it made (so they show, and a later Unwrap keeps them)."""
    if obj is None or obj.type != 'MESH' or not len(obj.data.polygons):
        return False
    t0 = time.perf_counter()
    ensure_layer(obj.data)
    with _editing(context, obj):
        bpy.ops.uv.smart_project(angle_limit=SMART_ANGLE, island_margin=MARGIN)
        _uv_select_all()
        bpy.ops.uv.seams_from_islands()
    _laid_out(obj.data)
    changed()
    say(f"Smart UV: {obj.name} ({_seconds(t0)})")
    return True


def pack(context, obj, rotate=None):
    """rotate: whether packing may turn islands; by default not once one has been turned by hand (Twist)."""
    if not has_uvs(obj) or not len(obj.data.polygons):
        return False
    if rotate is None:
        rotate = ORIENTED not in obj.data
    with _editing(context, obj):
        _uv_select_all()
        bpy.ops.uv.pack_islands(margin=MARGIN, rotate=rotate)
    changed()
    return True


def average_scale(context, obj):
    """Islands scaled to their share of the surface, then packed again."""
    if not has_uvs(obj) or not len(obj.data.polygons):
        return False
    with _editing(context, obj):
        _uv_select_all()
        bpy.ops.uv.average_islands_scale()
        bpy.ops.uv.pack_islands(margin=MARGIN, rotate=ORIENTED not in obj.data)
    changed()
    return True


def _seconds(t0):
    dt = time.perf_counter() - t0
    return f"{dt * 1000:.0f} ms" if dt < 1.0 else f"{dt:.1f} s"


def fix(context, obj, faces, method='ANGLE_BASED'):
    """Unwrap only faces (bool per face) again, stitched to the rest: their corners on the patch's border
    are pinned for the unwrap. A patch with no border (a whole island) is unwrapped where it was: moved
    and scaled back over its old place.

    Blender's unwrap costs as much for a few faces as for the whole mesh (and Edit Mode needs the whole
    mesh converted), so the patch is copied into a small mesh of its own, unwrapped there and its UVs
    copied back: the cost goes with the patch, not the sculpt, and obj never leaves its mode."""
    me = obj.data
    faces = np.asarray(faces, dtype=bool)
    if not faces.any() or not len(me.uv_layers):
        return False
    t0 = time.perf_counter()
    loop_f, loop_v = _loop_faces(me)
    inside = faces[loop_f]
    inside_v = np.zeros(len(me.vertices), dtype=bool)
    inside_v[loop_v[inside]] = True
    outside_v = np.zeros(len(me.vertices), dtype=bool)
    outside_v[loop_v[~inside]] = True
    pins = inside & (inside_v & outside_v)[loop_v]
    old_pins = _get_pins(me)
    if old_pins is not None:
        pins |= old_pins
    before = _uvs(me)
    after = before.copy()
    after[inside] = _unwrap_patch(context, me, faces, inside, loop_v, before, pins, method)
    if not (pins & inside).any():
        after[inside] = _placed_like(after[inside], before[inside])
    me.uv_layers.active.data.foreach_set("uv", after.ravel())
    me.update()
    changed()
    say(f"Fixed {int(faces.sum())} faces ({_seconds(t0)})")
    return True


def _unwrap_patch(context, me, faces, inside, loop_v, uv, pins, method):
    """The new UVs of the loops in inside (in order), from a temporary mesh of just those faces with their
    seams and the given pins."""
    verts = np.unique(loop_v[inside])
    remap = np.full(len(me.vertices), -1, dtype=np.int64)
    remap[verts] = np.arange(len(verts))
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    totals = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_total", totals)
    loop_e = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("edge_index", loop_e)
    totals = totals[faces]
    tmp = bpy.data.meshes.new(".vrs_uv_patch")
    obj = bpy.data.objects.new(".vrs_uv_patch", tmp)
    context.scene.collection.objects.link(obj)
    try:
        tmp.vertices.add(len(verts))
        tmp.vertices.foreach_set("co", co.reshape(-1, 3)[verts].ravel())
        tmp.loops.add(int(inside.sum()))
        tmp.loops.foreach_set("vertex_index", remap[loop_v[inside]].astype(np.int32))
        tmp.polygons.add(len(totals))
        tmp.polygons.foreach_set("loop_start", np.concatenate(([0], np.cumsum(totals)[:-1])).astype(np.int32))
        tmp.update(calc_edges=True)
        # Loops keep their order, so each loop's edge maps across.
        new_e = np.empty(len(tmp.loops), dtype=np.int64)
        tmp.loops.foreach_get("edge_index", new_e)
        tmp_seams = np.zeros(len(tmp.edges), dtype=bool)
        tmp_seams[new_e[seams(me)[loop_e[inside]]]] = True
        tmp.edges.foreach_set("use_seam", tmp_seams)
        layer = tmp.uv_layers.new(name="UVMap")
        layer.data.foreach_set("uv", uv[inside].ravel())
        _set_pins(tmp, pins[inside])
        with _editing(context, obj):
            bpy.ops.uv.unwrap(method=method, margin=MARGIN)
        return _uvs(tmp)
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(tmp)


def _placed_like(new, old):
    """new UVs moved and scaled so their bounds sit over old's."""
    old_size = old.max(0) - old.min(0)
    new_size = new.max(0) - new.min(0)
    if new_size.max() < 1e-12 or old_size.max() < 1e-12:
        return new
    k = math.sqrt(max(old_size[0] * old_size[1], 1e-18) / max(new_size[0] * new_size[1], 1e-18))
    return (new - (new.max(0) + new.min(0)) / 2) * k + (old.max(0) + old.min(0)) / 2


def seams_from_sharp(obj):
    """Seams on the edges that shade sharp: marked hard, or sharper than the Auto angle (30 degrees without
    Auto shading); soft edges are left out."""
    from . import shading
    me = obj.data
    if not len(me.edges):
        return False
    limit = shading.angle(obj) if shading.mode(obj) == 'AUTO' else SHARP_ANGLE
    hard, soft = shading.marks(me)
    s = seams(me) | ((hard | (edge_angles(me) > limit)) & ~soft)
    set_seams(me, s)
    return True


def edge_angles(mesh):
    """Angle between the two faces of each edge (0 for edges with one face or more than two)."""
    n = len(mesh.edges)
    out = np.zeros(n)
    if not len(mesh.polygons):
        return out
    loop_f, _loop_v = _loop_faces(mesh)
    loop_e = np.empty(len(mesh.loops), dtype=np.int64)
    mesh.loops.foreach_get("edge_index", loop_e)
    fn = np.empty(len(mesh.polygons) * 3)
    mesh.polygons.foreach_get("normal", fn)
    fn = fn.reshape(-1, 3)
    count = np.bincount(loop_e, minlength=n)
    order = np.argsort(loop_e, kind='stable')
    starts = np.concatenate(([0], np.cumsum(count)[:-1]))
    two = np.nonzero(count == 2)[0]
    a = loop_f[order[starts[two]]]
    b = loop_f[order[starts[two] + 1]]
    out[two] = np.arccos(np.clip((fn[a] * fn[b]).sum(1), -1.0, 1.0))
    return out


# --- stretch --------------------------------------------------------------------------

def face_stretch(co, loop_v, uv, tri_loops, tri_face, n_faces):
    """Per face, log2 of how far its UVs are from the surface's shape and size (0: true; 1: twice or
    half the size, or skewed as much), the size measured against the typical face's (the area-weighted
    median): against the average instead, a few faces hogging the UV square (a pinched pole) would turn
    the rest of the mesh red. co: world vertex positions; tri_loops (t, 3) corner indices of each
    triangle; tri_face its face."""
    p = co[loop_v[tri_loops]]
    u = uv[tri_loops]
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    u1, u2 = u[:, 1] - u[:, 0], u[:, 2] - u[:, 0]
    det = u1[:, 0] * u2[:, 1] - u1[:, 1] * u2[:, 0]
    area3d = np.linalg.norm(np.cross(e1, e2), axis=1) / 2
    flat = np.abs(det) < 1e-12
    safe = np.where(flat, 1.0, det)[:, None]
    ju = (e1 * u2[:, 1:2] - e2 * u1[:, 1:2]) / safe
    jv = (e2 * u1[:, 0:1] - e1 * u2[:, 0:1]) / safe
    a, b, c = (ju * ju).sum(1), (ju * jv).sum(1), (jv * jv).sum(1)
    mid, half = (a + c) / 2, np.sqrt(((a - c) / 2) ** 2 + b * b)
    s1 = np.sqrt(np.maximum(mid + half, 1e-30))
    s2 = np.sqrt(np.maximum(mid - half, 1e-30))
    size = np.log2(s1 * s2)
    score = np.maximum(np.log2(s1 / s2), np.abs(size - _weighted_median(size[~flat], area3d[~flat]))) / STRETCH_FULL
    score = np.where(flat, 1.0, np.clip(score, 0.0, 1.0))
    score[area3d < 1e-18] = 0.0
    weight = np.maximum(area3d, 1e-18)
    total = np.bincount(tri_face, weights=score * weight, minlength=n_faces)
    return total / np.maximum(np.bincount(tri_face, weights=weight, minlength=n_faces), 1e-18)


def _weighted_median(values, weights):
    if not len(values):
        return 0.0
    order = np.argsort(values)
    cum = np.cumsum(weights[order])
    return float(values[order][min(np.searchsorted(cum, cum[-1] / 2), len(values) - 1)])


# --- trigger handlers -----------------------------------------------------------------

class _Graph:
    """The mesh's edges as adjacency lists, for seam paths."""

    def __init__(self, world, ev):
        self.co = world
        self.ev = ev
        self.length = np.linalg.norm(world[ev[:, 0]] - world[ev[:, 1]], axis=1).tolist()
        n = len(world)
        nbr = [[] for _ in range(n)]
        for e, (a, b) in enumerate(ev.tolist()):
            nbr[a].append((b, e))
            nbr[b].append((a, e))
        self.nbr = nbr

    def path(self, a, b, limit):
        """The edges of the shortest path from vertex a to b, or None if it's longer than limit."""
        dist = {a: 0.0}
        prev = {}
        heap = [(0.0, a)]
        while heap:
            d, v = heapq.heappop(heap)
            if v == b:
                break
            if d > dist[v]:
                continue
            for w, e in self.nbr[v]:
                nd = d + self.length[e]
                if nd <= limit and nd < dist.get(w, math.inf):
                    dist[w] = nd
                    prev[w] = (v, e)
                    heapq.heappush(heap, (nd, w))
        if b not in prev:
            return None
        out = []
        v = b
        while v != a:
            v, e = prev[v]
            out.append(e)
        return out


def _world(obj):
    me = obj.data
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    m = np.array(obj.matrix_world)
    return co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]


def _face_centers(mesh, world):
    loop_f, loop_v = _loop_faces(mesh)
    out = np.zeros((len(mesh.polygons), 3))
    np.add.at(out, loop_f, world[loop_v])
    return out / np.maximum(np.bincount(loop_f, minlength=len(mesh.polygons)), 1)[:, None]


def _near_middle(segments, point, reach):
    """Bool per segment: point within reach of it, away from both its ends. At a vertex every edge
    around it is in reach, and seams on all of them would slit the map; those come from the path
    between vertices instead."""
    if not len(segments):
        return np.zeros(0, dtype=bool)
    a, b = segments[:, 0], segments[:, 1]
    ab = b - a
    t = np.clip(((point - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-18), 0.0, 1.0)
    closest = a + ab * t[:, None]
    return ((np.linalg.norm(closest - point, axis=1) <= reach) & (np.linalg.norm(closest - a, axis=1) > reach) &
            (np.linalg.norm(closest - b, axis=1) > reach))


class SeamMark:
    """Trigger handler for the Seams tool: edges the tip passes along become seams (or stop being, clear).
    The tip walks from vertex to vertex, and each step marks the shortest edge path between them, so a
    fast sweep leaves no gaps; a long edge is also marked when the tip passes its middle. The meshes
    unwrap again on release when live."""

    def __init__(self, context, clear, scale):
        from . import shading
        settings = context.scene.vr_sculpt
        self.value = not clear
        self.reach = shading.MARK_REACH * scale
        self.live = settings.uv_live
        self.method = settings.uv_method
        self.objects = shading.mark_targets(context)
        self.data = {}
        for o in self.objects:
            ev = np.empty(len(o.data.edges) * 2, dtype=np.int64)
            o.data.edges.foreach_get("vertices", ev)
            ev = ev.reshape(-1, 2)
            world = _world(o)
            self.data[o.name] = {"segments": world[ev], "graph": None, "world": world, "ev": ev, "last": None}
        self.touched = set()
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        point = np.array(tip_pose.translation[:])
        for obj in self.objects:
            d = self.data[obj.name]
            near = _near_middle(d["segments"], point, self.reach)
            dist = np.linalg.norm(d["world"] - point, axis=1)
            nearest = int(np.argmin(dist)) if len(dist) else None
            if nearest is not None and dist[nearest] > 2.0 * self.reach:
                nearest = None
            path = []
            if nearest is not None and d["last"] is not None and nearest != d["last"]:
                if d["graph"] is None:
                    d["graph"] = _Graph(d["world"], d["ev"])
                straight = float(np.linalg.norm(d["world"][nearest] - d["world"][d["last"]]))
                path = d["graph"].path(d["last"], nearest, PATH_SLACK * straight + 2.0 * self.reach) or []
            if nearest is not None:
                d["last"] = nearest
            elif not near.any():
                d["last"] = None  # Off the mesh: the next touch starts a new line.
            if path:
                near = near.copy()
                near[path] = True
            if not near.any():
                continue
            s = seams(obj.data)
            if (s[near] != self.value).any():
                s[near] = self.value
                set_seams(obj.data, s)
                self.touched.add(obj.name)
                self.changed = True

    def finish(self, context):
        if not self.live:
            return
        for obj in self.objects:
            if obj.name in self.touched:
                unwrap(context, obj, self.method)


class FixUV:
    """Trigger handler for the Fix tool: the faces the tip brushes over unwrap again on release, stitched
    to the rest of the layout."""
    smooth = True  # The trigger operator stabilizes the tip (smoothing.py).

    def __init__(self, context, obj, scale):
        settings = context.scene.vr_sculpt
        self.obj = obj
        self.radius = settings.uv_fix_radius * scale
        self.method = settings.uv_method
        me = obj.data
        world = _world(obj)
        self.centers = _face_centers(me, world)
        self.faces = np.zeros(len(me.polygons), dtype=bool)
        # For the preview: the triangles of the brushed faces.
        tri_v = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tri_v)
        tri_f = np.empty(len(me.loop_triangles), dtype=np.int32)
        me.loop_triangles.foreach_get("polygon_index", tri_f)
        self.world = world.astype(np.float32)
        self.tri_v, self.tri_f = tri_v.reshape(-1, 3), tri_f
        self.changed = False
        _fixing[0] = self

    def update(self, context, pose, tip_pose, value):
        point = np.array(tip_pose.translation[:])
        self.faces |= np.linalg.norm(self.centers - point, axis=1) <= self.radius

    def preview(self):
        """(n, 3) float32 world triangle corners of the brushed faces."""
        return self.world[self.tri_v[self.faces[self.tri_f]]].reshape(-1, 3)

    def finish(self, context):
        _fixing[0] = None
        if self.faces.any() and has_uvs(self.obj):
            self.changed = fix(context, self.obj, self.faces, self.method)
        elif self.faces.any():
            # No UVs yet: there's nothing to stitch to, so the whole mesh.
            self.changed = unwrap(context, self.obj, self.method)

    def abandon(self):
        _fixing[0] = None


_fixing = [None]  # the Fix stroke in progress, for its preview


class _IslandGrab:
    """Base of the trigger handlers that transform one UV island about its center. The stroke only draws: the
    shader turns the island's UVs (uv_transform()), so a dense mesh costs nothing per frame. Release writes
    them and, with Repack on, packs the layout."""

    def __init__(self, context, obj, face, labels):
        me = obj.data
        loop_f, _loop_v = _loop_faces(me)
        self.obj = obj
        self.repack = context.scene.vr_sculpt.uv_repack
        self.faces = labels == labels[face]
        self.loops = self.faces[loop_f]
        self.before = _uvs(me)
        island = self.before[self.loops]
        self.center = (island.max(0) + island.min(0)) / 2
        self.arrays = _island_arrays(obj, self.faces, (1.0, 1.0, 1.0, 1.0))
        self.batch = None  # Made when drawing.
        self.changed = False
        _grab[0] = self

    def uv_transform(self):
        """(a, b): UVs go to center + (a x - b y, b x + a y), from (x, y) around the center."""
        raise NotImplementedError

    def moved(self):
        raise NotImplementedError

    def summary(self):
        raise NotImplementedError

    def after(self):
        """The whole map's UVs, with this island transformed."""
        a, b = self.uv_transform()
        d = self.before[self.loops] - self.center
        out = self.before.copy()
        out[self.loops] = self.center + np.stack((a * d[:, 0] - b * d[:, 1], b * d[:, 0] + a * d[:, 1]), axis=1)
        return out

    def mark(self, mesh):
        """What the mesh has to remember of this change."""

    def finish(self, context):
        _grab[0] = None
        if not self.moved():
            return
        me = self.obj.data
        t0 = time.perf_counter()
        me.uv_layers.active.data.foreach_set("uv", self.after().ravel())
        self.mark(me)
        me.update()
        changed()
        if self.repack:
            pack(context, self.obj, rotate=False)
        self.changed = True
        say(f"{self.summary()} ({_seconds(t0)})")
        tick(context)  # So the view isn't a beat behind the preview it replaces.

    def abandon(self):
        _grab[0] = None


class TwistIsland(_IslandGrab):
    """Trigger handler for the Twist tool: the texture turns on the surface with the wrist's roll, the way the
    hand turns as seen from behind it. Roll adds up frame by frame, so a stroke can go past half a turn."""

    def __init__(self, context, obj, face, labels, hand, pose):
        super().__init__(context, obj, face, labels)
        self.hand = hand
        self.step = TWIST_STEP if context.scene.vr_sculpt.uv_twist_snap else 0.0
        self.sign = _uv_winding(obj.data, self.loops)
        self.last = pose.to_quaternion()
        self.total = 0.0
        self.angle = 0.0

    def update(self, context, pose, tip_pose, value):
        from . import operators
        q = pose.to_quaternion()
        turn = self.last.inverted() @ q
        self.last = q
        # Roll: the rotation about the controller's own axis (its -Z points away from the hand).
        roll = 2.0 * math.atan2(turn.z, turn.w)
        self.total += (roll + math.pi) % (2.0 * math.pi) - math.pi
        angle = round(self.total / self.step) * self.step if self.step else self.total
        if self.step and angle != self.angle:
            operators.haptic(context, self.hand, 0.3, 0.01)
        self.angle = angle

    def uv_transform(self):
        # To turn the pattern one way on the surface, the UVs under it turn the other way.
        phi = -self.sign * self.angle
        return math.cos(phi), math.sin(phi)

    def moved(self):
        return abs(self.angle) >= TWIST_MIN

    def mark(self, mesh):
        mesh[ORIENTED] = True

    def summary(self):
        return f"Turned {math.degrees(self.angle):.0f}\u00b0"


class SizeIsland(_IslandGrab):
    """Trigger handler for the Size tool: raising the hand gives the island more of the UV square (the
    texture gets finer on it), lowering it less."""

    def __init__(self, context, obj, face, labels, tip_pose, scale):
        super().__init__(context, obj, face, labels)
        self.start = tip_pose.translation.z
        self.scale = scale
        self.factor = 1.0

    def update(self, context, pose, tip_pose, value):
        rise = (tip_pose.translation.z - self.start) / self.scale
        self.factor = min(max(2.0 ** (rise / SIZE_DOUBLE), SIZE_MIN), SIZE_MAX)

    def uv_transform(self):
        return self.factor, 0.0

    def moved(self):
        return abs(self.factor - 1.0) >= 0.01

    def summary(self):
        return f"Island size \u00d7{self.factor:.2f}"


class NoIsland:
    """The trigger with Twist or Size and no island to work on: says why, and doesn't create or grab anything."""
    changed = False

    def __init__(self, text):
        say(text)

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


def _uv_winding(mesh, loops):
    """1 if these corners' UVs wind the way their faces do seen from outside, -1 if they're flipped."""
    tris = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("loops", tris)
    tris = tris.reshape(-1, 3)
    u = _uvs(mesh)[tris[loops[tris[:, 0]]]]
    d1, d2 = u[:, 1] - u[:, 0], u[:, 2] - u[:, 0]
    return 1.0 if (d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]).sum() >= 0.0 else -1.0


def _lifted(mesh):
    """Vertex positions pushed out along their normals, so the view sits on the mesh."""
    from . import rig
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    no = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertex_normals.foreach_get("vector", no)
    size = float(np.linalg.norm(co.max(0) - co.min(0)))
    return co + no.reshape(-1, 3) * (rig.HEAT_LIFT * size)


def _island_arrays(obj, faces, color):
    """(pos, uv, color, tris) of faces alone, for the view shader. They follow the view's surface (modifiers
    applied) if it has the same corners, or the object's own mesh."""
    me = obj.data
    arrays = _view["arrays"]
    if arrays is not None and _view["key"][0] == obj.name and len(arrays[0]) == len(me.loops):
        pos = arrays[0]
    else:
        pos = _lifted(me)[_loop_faces(me)[1]].astype(np.float32)
    tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("loops", tris)
    tri_f = np.empty(len(me.loop_triangles), dtype=np.int32)
    me.loop_triangles.foreach_get("polygon_index", tri_f)
    tris = tris.reshape(-1, 3)[faces[tri_f]]
    used, tris = np.unique(tris, return_inverse=True)
    colors = np.empty((len(used), 4), dtype=np.float32)
    colors[:] = color
    return pos[used], _uvs(me)[used].astype(np.float32), colors, tris.reshape(-1, 3).astype(np.int32)


def _centers_if_remade(obj):
    """Face centers to find a face by, when modifiers change the faces the surface queries land on (their
    indices aren't the mesh's then); None when they're the mesh's own."""
    from . import paint
    if len(paint.evaluated_mesh(obj).polygons) == len(obj.data.polygons):
        return None
    return _face_centers(obj.data, _world(obj))


def face_under(obj, point, reach, centers):
    """The obj.data face the tip is at, if the surface is in reach. centers: from _centers_if_remade()."""
    from . import paint
    hit = paint.surface_near(obj, point, reach)
    if hit is None:
        return None
    if centers is None:
        return hit[2]
    return int(np.argmin(np.linalg.norm(centers - np.array(point[:]), axis=1)))


def _pick_island(context, settings, hand, pose, tip_pose, scale):
    obj = fix_target(context)
    if obj is None or not len(obj.data.polygons):
        return None
    me = obj.data
    if not has_uvs(obj) or uv_area(me) < 1e-12:
        return NoIsland("No UVs yet: Unwrap or Smart UV first")
    face = face_under(obj, tip_pose.translation, ISLAND_REACH * scale, _centers_if_remade(obj))
    if face is None:
        return NoIsland("Touch an island with the tip")
    labels = _hover["labels"] if _hover["key"] == _hover_key(obj) else island_labels(me, use_seams=False)
    if settings.uv_tool == 'TWIST':
        return TwistIsland(context, obj, face, labels, hand, pose)
    if settings.uv_repack and (labels == labels[face]).all():
        return NoIsland("Only one island: packing would size it back")
    return SizeIsland(context, obj, face, labels, tip_pose, scale)


_grab = [None]  # the Twist or Size stroke in progress, for its preview


def fix_target(context):
    from . import shading
    objs = shading.mark_targets(context)
    return objs[0] if objs else None


def pick(context, settings, left, scale, hand, pose, tip_pose):
    """The trigger handler for the UV tool, or None."""
    from . import shading
    if settings.uv_tool == 'SEAM' and shading.mark_targets(context):
        return SeamMark(context, left, scale)
    if settings.uv_tool == 'FIX':
        obj = fix_target(context)
        if obj is not None and len(obj.data.polygons):
            return FixUV(context, obj, scale)
    if settings.uv_tool in ISLAND_TOOLS:
        return _pick_island(context, settings, hand, pose, tip_pose, scale)
    return None


# --- the view ---------------------------------------------------------------------------

# The checker / stretch / island view of the selected object: per corner positions (pushed out along the
# normals), UVs and colors, and the triangles, built in the app timer (tick) from the evaluated mesh;
# the batch is made when drawing. open and count: the island view's seams that separate nothing (a bool per
# edge of the object's own mesh) and how many islands there are; labels and stitching: what a seam stroke
# works them out from; next: when a seam stroke can do it again.
_view = {"key": None, "arrays": None, "batch": None, "batch_key": None, "open": None, "count": 0, "labels": None,
         "stitching": None, "next": 0.0}

# The island under each tip with Twist or Size on: the object's island labels, from the timer, and a batch
# for each island drawn so far.
_hover = {"key": None, "labels": None, "centers": None, "batches": {}}


def _forget_view():
    _view.update(key=None, arrays=None, batch=None, batch_key=None, open=None, count=0, labels=None, stitching=None)


def forget_caches():
    _forget_view()
    _view["next"] = 0.0
    _hover.update(key=None, labels=None, centers=None, batches={})
    _fixing[0] = None
    _grab[0] = None


def page_shown(context):
    from . import menus
    settings = context.scene.vr_sculpt
    return settings.mode == 'MODEL' and (settings.uv_tool != 'NONE' or (
        menus.sheet() == 'OBJECT' and settings.model_page == 'UV'))


def view_wanted(context):
    return page_shown(context) and context.scene.vr_sculpt.uv_view != 'NONE'


def build_view(context, obj, view, labels=None):
    """(pos, uv, color, tris) of obj as shown, for view ('CHECKER', 'STRETCH' or 'ISLANDS'); None without UVs,
    which only the islands don't need (seams cut them before there's an unwrap). labels: island_labels() of
    the object's own mesh, used if the shown one has the same faces."""
    from . import rig
    dg = context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        if me is None or not len(me.loop_triangles) or (view != 'ISLANDS' and not len(me.uv_layers)):
            return None
        nl = len(me.loops)
        loop_v = np.empty(nl, dtype=np.int64)
        me.loops.foreach_get("vertex_index", loop_v)
        uv = _uvs(me) if len(me.uv_layers) else np.zeros((nl, 2))
        tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("loops", tris)
        tris = tris.reshape(-1, 3)
        pos = _lifted(me)[loop_v].astype(np.float32)
        if view == 'STRETCH':
            tri_f = np.empty(len(me.loop_triangles), dtype=np.int64)
            me.loop_triangles.foreach_get("polygon_index", tri_f)
            co = np.empty(len(me.vertices) * 3, dtype=np.float64)
            me.vertices.foreach_get("co", co)
            m = np.array(obj.matrix_world)
            world = co.reshape(-1, 3) @ m[:3, :3].T
            score = face_stretch(world, loop_v, uv, tris, tri_f, len(me.polygons))
            totals = np.empty(len(me.polygons), dtype=np.int64)
            me.polygons.foreach_get("loop_total", totals)
            color = rig.heat_colors(np.repeat(score, totals), VIEW_ALPHA)
        elif view == 'ISLANDS':
            if labels is None or len(labels) != len(me.polygons):
                labels = island_labels(me)
            color = island_colors(labels)[_loop_faces(me)[0]]
        else:
            color = np.ones((nl, 4), dtype=np.float32)
            color[:, 3] = VIEW_ALPHA
        return pos, uv.astype(np.float32), color, tris
    finally:
        ev.to_mesh_clear()


def _following(settings):
    """A seam stroke with the island view: it rebuilds during the stroke, to show what the cut does."""
    return settings.uv_view == 'ISLANDS' and settings.uv_tool == 'SEAM'


def busy(context):
    """Whether the app timer has to run at full rate for the view to follow a stroke."""
    from . import operators
    return bool(operators.active_hands) and _following(context.scene.vr_sculpt) and view_wanted(context)


def _hover_tick(context, settings):
    """Twist and Size: the labels the island under a tip is looked up in. (Not while one is held: that
    island is its own preview.)"""
    from . import operators
    if settings.mode != 'MODEL' or settings.uv_tool not in ISLAND_TOOLS:
        if _hover["key"] is not None:
            _hover.update(key=None, labels=None, centers=None, batches={})
        return
    if operators.active_hands:
        return
    obj = fix_target(context)
    if obj is None or not has_uvs(obj) or not len(obj.data.polygons):
        _hover.update(key=None, labels=None, centers=None, batches={})
        return
    key = _hover_key(obj)
    if key != _hover["key"]:
        _hover.update(key=key, labels=island_labels(obj.data, use_seams=False), centers=_centers_if_remade(obj),
                      batches={})


def _hover_key(obj):
    return obj.name, obj.data.as_pointer(), _version[0]


def _follow_tick(context, obj):
    """A seam stroke with the island view: new seams against the stitching from before the stroke (the UVs and
    the topology hold still during it), which costs a few milliseconds on a dense mesh. The colors change only
    when an island has split or joined, which is rarely, so most of the stroke rebuilds nothing."""
    t0 = time.perf_counter()
    stitching = _view["stitching"]
    mesh = obj.data
    if stitching is None or stitching.n_edges != len(mesh.edges) or stitching.n_faces != len(mesh.polygons):
        return
    marked = seams(mesh)
    labels = stitching.labels(marked)
    _view["open"] = stitching.open_seams(labels, marked)
    if _view["labels"] is None or not np.array_equal(labels, _view["labels"]):
        _view.update(labels=labels, count=len(np.unique(labels)), batch=None)
        arrays = _view["arrays"]
        if arrays is not None and len(arrays[0]) == len(stitching.loop_f):
            arrays[2][:] = island_colors(labels)[stitching.loop_f]
        else:
            _view["arrays"] = build_view(context, obj, 'ISLANDS')  # Under modifiers: the faces shown are others.
    _view["next"] = time.monotonic() + max(FOLLOW_MIN, FOLLOW_SHARE * (time.perf_counter() - t0))


def tick(context):
    """App timer (live.py): rebuild the view's arrays when the object, its geometry or the view changed."""
    from . import operators
    settings = context.scene.vr_sculpt
    _hover_tick(context, settings)
    if not view_wanted(context):
        if _view["key"] is not None:
            _forget_view()
        return
    if operators.active_hands and (not _following(settings) or time.monotonic() < _view["next"]):
        return
    obj = fix_target(context)
    if obj is None:
        _forget_view()
        return
    if operators.active_hands:
        _follow_tick(context, obj)
        return
    key = (obj.name, obj.data.as_pointer(), settings.uv_view, _version[0])
    if key == _view["key"]:
        return
    labels = open_edges = stitching = None
    count = 0
    if settings.uv_view == 'ISLANDS':
        stitching = _Stitching(obj.data)
        marked = seams(obj.data)
        labels = stitching.labels(marked)
        open_edges = stitching.open_seams(labels, marked)
        count = len(np.unique(labels))
    _view.update(key=key, arrays=build_view(context, obj, settings.uv_view, labels), batch=None, open=open_edges,
                 count=count, labels=labels, stitching=stitching)


def island_note(settings):
    """The island view's count for the panel, or ""."""
    if settings.uv_view != 'ISLANDS' or _view["open"] is None:
        return ""
    count, open_edges = _view["count"], int(_view["open"].sum())
    text = f"{count} island" + ("" if count == 1 else "s")
    return text + (f", {open_edges} open seam edges" if open_edges else "")


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    for update in depsgraph.updates:
        if update.is_updated_geometry and isinstance(update.id, bpy.types.Object):
            changed()
            return


_shader = {}


def view_shader():
    """Per corner color and UV; with checker on, a checker of squares per side on the UVs (tinted pink
    outside the UV square). uvTurn and uvCenter transform the UVs (a Twist or Size stroke's preview). The
    matrix pulls toward the eye like the weight heat map, so it wins over the mesh."""
    shader = _shader.get("view")
    if shader is None:
        iface = gpu.types.GPUStageInterfaceInfo("vrs_uv_iface")
        iface.smooth('VEC4', "finalColor")
        iface.smooth('VEC2', "finalUV")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "pulledMatrix")
        info.push_constant('VEC2', "uvTurn")
        info.push_constant('VEC2', "uvCenter")
        info.push_constant('FLOAT', "checker")
        info.push_constant('FLOAT', "squares")
        info.vertex_in(0, 'VEC3', "pos")
        info.vertex_in(1, 'VEC2', "uv")
        info.vertex_in(2, 'VEC4', "color")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "fragColor")
        info.vertex_source(
            "void main() {"
            "  gl_Position = pulledMatrix * vec4(pos, 1.0);"
            "  finalColor = color;"
            "  vec2 d = uv - uvCenter;"
            "  finalUV = uvCenter + vec2(uvTurn.x * d.x - uvTurn.y * d.y, uvTurn.y * d.x + uvTurn.x * d.y);"
            "}")
        info.fragment_source(
            "void main() {"
            "  vec3 c = finalColor.rgb;"
            "  if (checker > 0.5) {"
            "    vec2 g = floor(finalUV * squares);"
            "    float odd = mod(g.x + g.y, 2.0);"
            "    c = mix(vec3(0.22), vec3(0.92), odd);"
            "    if (any(lessThan(finalUV, vec2(0.0))) || any(greaterThan(finalUV, vec2(1.0)))) {"
            "      c = mix(c, vec3(1.0, 0.25, 0.65), 0.55);"
            "    }"
            "  }"
            "  fragColor = vec4(c, finalColor.a);"
            "}")
        shader = _shader["view"] = gpu.shader.create_from_info(info)
        _shader["iface"] = iface  # The interface has to outlive the shader's creation info.
    return shader


def _draw_surface(batch, obj, settings, checker, pull=1.0, turn=(1.0, 0.0), center=(0.0, 0.0)):
    """A batch of view shader arrays on obj. pull: as a share of the heat map's."""
    from . import rig
    shader = view_shader()
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(obj.matrix_world)
    k = 1.0 - rig.HEAT_PULL * pull  # Toward the eye, in view space.
    pulled = gpu.matrix.get_projection_matrix() @ Matrix.Diagonal((k, k, k, 1.0)) @ gpu.matrix.get_model_view_matrix()
    shader.bind()
    shader.uniform_float("pulledMatrix", pulled)
    shader.uniform_float("uvTurn", turn)
    shader.uniform_float("uvCenter", center)
    shader.uniform_float("checker", 1.0 if checker else 0.0)
    shader.uniform_float("squares", float(settings.uv_checker))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    batch.draw(shader)
    gpu.state.depth_test_set('NONE')
    gpu.matrix.pop()


def _draw_view(context, settings):
    arrays = _view["arrays"]
    obj = fix_target(context)
    if arrays is None or obj is None or _view["key"] is None or _view["key"][0] != obj.name:
        return
    if _view["batch"] is None or _view["batch_key"] != _view["key"]:
        pos, uv, color, tris = arrays
        _view["batch"] = batch_for_shader(view_shader(), 'TRIS', {"pos": pos, "uv": uv, "color": color}, indices=tris)
        _view["batch_key"] = _view["key"]
    _draw_surface(_view["batch"], obj, settings, settings.uv_view == 'CHECKER')


def _draw_grab(settings):
    """The island a Twist or Size stroke holds: a checker turning with the hand, whatever the view is."""
    grab = _grab[0]
    if grab.batch is None:
        pos, uv, color, tris = grab.arrays
        grab.batch = batch_for_shader(view_shader(), 'TRIS', {"pos": pos, "uv": uv, "color": color}, indices=tris)
    _draw_surface(grab.batch, grab.obj, settings, True, ISLAND_PULL, grab.uv_transform(), grab.center.tolist())


def _draw_hover(context, settings, tips, scale):
    """The islands under the tips, tinted: what the trigger would take."""
    labels = _hover["labels"]
    obj = fix_target(context)
    if labels is None or obj is None or _hover["key"][0] != obj.name or len(labels) != len(obj.data.polygons):
        return
    seen = set()
    for tip in tips:
        face = face_under(obj, tip, ISLAND_REACH * scale, _hover["centers"])
        if face is None:
            continue
        label = int(labels[face])
        if label in seen:
            continue
        seen.add(label)
        batch = _hover["batches"].get(label)
        if batch is None:
            pos, uv, color, tris = _island_arrays(obj, labels == label, (*ISLAND_COLOR, HOVER_ALPHA))
            batch = batch_for_shader(view_shader(), 'TRIS', {"pos": pos, "uv": uv, "color": color}, indices=tris)
            _hover["batches"][label] = batch
        _draw_surface(batch, obj, settings, False, ISLAND_PULL)


def draw(shader, context, tips, scale):
    """The UV page in the headset: the view, the island in reach of Twist and Size or the one they hold, the
    seams of what the tools work on, the edges in reach of the Seams tool, and the faces a Fix stroke has
    brushed. shader: UNIFORM_COLOR."""
    from . import shading
    settings = context.scene.vr_sculpt
    if settings.uv_view != 'NONE':
        _draw_view(context, settings)
    if _grab[0] is not None:
        _draw_grab(settings)
    elif settings.uv_tool in ISLAND_TOOLS:
        _draw_hover(context, settings, tips, scale)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    seam_pts, open_pts, reach_pts = [], [], []
    follow = _view["open"] if settings.uv_view == 'ISLANDS' else None
    view_name = _view["key"][0] if _view["key"] is not None else None
    for obj in shading.mark_targets(context):
        segs = shading._edge_segments(obj)
        marked = seams(obj.data)
        if follow is not None and obj.name == view_name and len(follow) == len(marked):
            open_pts.append(segs[marked & follow].reshape(-1, 3))
            marked = marked & ~follow
        seam_pts.append(segs[marked].reshape(-1, 3))
        if settings.uv_tool == 'SEAM':
            for tip in tips:
                reach_pts.append(segs[shading._near(segs, np.array(tip[:]), shading.MARK_REACH * scale)].reshape(-1, 3))
    gpu.state.line_width_set(3.0)
    for pts, color in ((seam_pts, (*SEAM_COLOR, 0.95)), (open_pts, (*OPEN_SEAM_COLOR, 0.95)),
                       (reach_pts, (1.0, 1.0, 1.0, 0.8))):
        coords = np.concatenate(pts) if pts else np.zeros((0, 3))
        if not len(coords):
            continue
        batch = batch_for_shader(shader, 'LINES', {"pos": coords.astype(np.float32)})
        shader.uniform_float("color", color)
        batch.draw(shader)
    gpu.state.line_width_set(1.0)
    stroke = _fixing[0]
    if stroke is not None:
        tri = stroke.preview()
        if len(tri):
            batch = batch_for_shader(shader, 'TRIS', {"pos": tri})
            shader.uniform_float("color", (*FIX_COLOR, 0.35))
            batch.draw(shader)


def register():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    forget_caches()
