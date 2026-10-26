"""UV mapping for Model mode's UV page: seams marked by hand, unwrapping, the Fix brush, and a checker /
stretch view drawn on the mesh in the headset.

- Seams: the tip marks the edges it passes near as seams (Blender's own "uv_seam", what Mark Seam
  sets); the off hand clears them. A fast sweep leaves gaps between samples, so each new vertex the
  tip reaches is joined to the last one along the shortest edge path. With Live on, the mesh is
  unwrapped again when the trigger is let go.
- Fix: the faces the tip brushes over unwrap again, and only those: their corners on the border of the
  brushed patch are pinned for the unwrap (and unpinned after), so the patch stays stitched into the
  layout and nothing outside it moves. For where sculpting has stretched or scrambled the UVs.
- The view: a checker on the UVs, or each face colored by how much its UVs are stretched against the
  mesh (blue: true to the surface; red: twice or half the size, or skewed as much).

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

METHODS = (('ANGLE_BASED', "Angle"), ('CONFORMAL', "Conformal"), ('MINIMUM_STRETCH', "Min stretch"))
MARGIN = 0.01  # between islands, as a share of the UV square
SMART_ANGLE = math.radians(66.0)  # Smart UV Project's default
SHARP_ANGLE = math.radians(30.0)  # Seams from Sharp, when the object has no Auto shading angle
PATH_SLACK = 1.6  # a seam path may be this much longer than the straight line between its ends
SEAM_COLOR = (1.0, 0.32, 0.28)
FIX_COLOR = (0.55, 0.9, 0.45)
STRETCH_FULL = 1.0  # log2 of the stretch shown fully red: twice (or half) the size
VIEW_ALPHA = 0.9

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
    obj.data["vrs_unwrapped"] = True
    changed()
    say(f"Unwrapped {obj.name} ({_seconds(t0)})")
    return True


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
    obj.data["vrs_unwrapped"] = True
    changed()
    say(f"Smart UV: {obj.name} ({_seconds(t0)})")
    return True


def pack(context, obj):
    if not has_uvs(obj) or not len(obj.data.polygons):
        return False
    with _editing(context, obj):
        _uv_select_all()
        bpy.ops.uv.pack_islands(margin=MARGIN)
    changed()
    return True


def average_scale(context, obj):
    """Islands scaled to their share of the surface, then packed again."""
    if not has_uvs(obj) or not len(obj.data.polygons):
        return False
    with _editing(context, obj):
        _uv_select_all()
        bpy.ops.uv.average_islands_scale()
        bpy.ops.uv.pack_islands(margin=MARGIN)
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

    def __init__(self, context, obj, scale):
        settings = context.scene.vr_sculpt
        self.obj = obj
        self.radius = settings.uv_fix_radius * scale
        self.method = settings.uv_method
        me = obj.data
        world = _world(obj)
        loop_f, loop_v = _loop_faces(me)
        totals = np.bincount(loop_f, minlength=len(me.polygons))
        self.centers = np.zeros((len(me.polygons), 3))
        np.add.at(self.centers, loop_f, world[loop_v])
        self.centers /= np.maximum(totals, 1)[:, None]
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


def fix_target(context):
    from . import shading
    objs = shading.mark_targets(context)
    return objs[0] if objs else None


def pick(context, settings, left, scale):
    """The trigger handler for the UV tool, or None."""
    from . import shading
    if settings.uv_tool == 'SEAM' and shading.mark_targets(context):
        return SeamMark(context, left, scale)
    if settings.uv_tool == 'FIX':
        obj = fix_target(context)
        if obj is not None and len(obj.data.polygons):
            return FixUV(context, obj, scale)
    return None


# --- the view ---------------------------------------------------------------------------

# The checker / stretch view of the selected object: per corner positions (pushed out along the
# normals), UVs and colors, and the triangles, built in the app timer (tick) from the evaluated mesh;
# the batch is made when drawing.
_view = {"key": None, "arrays": None, "batch": None, "batch_key": None}


def forget_caches():
    _view.update(key=None, arrays=None, batch=None, batch_key=None)
    _fixing[0] = None


def page_shown(context):
    from . import menus
    settings = context.scene.vr_sculpt
    return settings.mode == 'MODEL' and (settings.uv_tool != 'NONE' or (
        menus.sheet() == 'OBJECT' and settings.model_page == 'UV'))


def view_wanted(context):
    return page_shown(context) and context.scene.vr_sculpt.uv_view != 'NONE'


def build_view(context, obj, stretch):
    """(pos, uv, color, tris) of obj as shown, for the view; None without UVs."""
    from . import rig
    dg = context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        if me is None or not len(me.uv_layers) or not len(me.loop_triangles):
            return None
        nv, nl = len(me.vertices), len(me.loops)
        co = np.empty(nv * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        no = np.empty(nv * 3, dtype=np.float64)
        me.vertex_normals.foreach_get("vector", no)
        no = no.reshape(-1, 3)
        loop_v = np.empty(nl, dtype=np.int64)
        me.loops.foreach_get("vertex_index", loop_v)
        uv = _uvs(me)
        tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("loops", tris)
        tris = tris.reshape(-1, 3)
        size = float(np.linalg.norm(co.max(0) - co.min(0)))
        pos = (co + no * (rig.HEAT_LIFT * size))[loop_v].astype(np.float32)
        if stretch:
            tri_f = np.empty(len(me.loop_triangles), dtype=np.int64)
            me.loop_triangles.foreach_get("polygon_index", tri_f)
            m = np.array(obj.matrix_world)
            world = co @ m[:3, :3].T
            score = face_stretch(world, loop_v, uv, tris, tri_f, len(me.polygons))
            totals = np.empty(len(me.polygons), dtype=np.int64)
            me.polygons.foreach_get("loop_total", totals)
            color = rig.heat_colors(np.repeat(score, totals), VIEW_ALPHA)
        else:
            color = np.ones((nl, 4), dtype=np.float32)
            color[:, 3] = VIEW_ALPHA
        return pos, uv.astype(np.float32), color, tris
    finally:
        ev.to_mesh_clear()


def tick(context):
    """App timer (live.py): rebuild the view's arrays when the object, its geometry or the view changed."""
    from . import operators
    if not view_wanted(context):
        if _view["key"] is not None:
            forget_caches()
        return
    if operators.active_hands:
        return
    obj = fix_target(context)
    if obj is None:
        _view.update(key=None, arrays=None)
        return
    settings = context.scene.vr_sculpt
    key = (obj.name, obj.data.as_pointer(), settings.uv_view, _version[0])
    if key == _view["key"]:
        return
    _view.update(key=key, arrays=build_view(context, obj, settings.uv_view == 'STRETCH'), batch=None)


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    for update in depsgraph.updates:
        if update.is_updated_geometry and isinstance(update.id, bpy.types.Object):
            changed()
            return


_shader = {}


def view_shader():
    """Per corner color and UV; with checker on, a checker of squares per side on the UVs (tinted pink
    outside the UV square). Pulled toward the eye like the weight heat map, so it wins over the mesh."""
    shader = _shader.get("view")
    if shader is None:
        iface = gpu.types.GPUStageInterfaceInfo("vrs_uv_iface")
        iface.smooth('VEC4', "finalColor")
        iface.smooth('VEC2', "finalUV")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "modelViewMatrix")
        info.push_constant('MAT4', "projectionMatrix")
        info.push_constant('FLOAT', "pull")
        info.push_constant('FLOAT', "checker")
        info.push_constant('FLOAT', "squares")
        info.vertex_in(0, 'VEC3', "pos")
        info.vertex_in(1, 'VEC2', "uv")
        info.vertex_in(2, 'VEC4', "color")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "fragColor")
        info.vertex_source(
            "void main() {"
            "  vec4 v = modelViewMatrix * vec4(pos, 1.0);"
            "  v.xyz *= 1.0 - pull;"
            "  gl_Position = projectionMatrix * v;"
            "  finalColor = color;"
            "  finalUV = uv;"
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


def _draw_view(context, settings):
    from . import rig
    arrays = _view["arrays"]
    obj = fix_target(context)
    if arrays is None or obj is None or _view["key"] is None or _view["key"][0] != obj.name:
        return
    shader = view_shader()
    if _view["batch"] is None or _view["batch_key"] != _view["key"]:
        pos, uv, color, tris = arrays
        _view["batch"] = batch_for_shader(shader, 'TRIS', {"pos": pos, "uv": uv, "color": color}, indices=tris)
        _view["batch_key"] = _view["key"]
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(obj.matrix_world)
    shader.bind()
    shader.uniform_float("modelViewMatrix", gpu.matrix.get_model_view_matrix())
    shader.uniform_float("projectionMatrix", gpu.matrix.get_projection_matrix())
    shader.uniform_float("pull", rig.HEAT_PULL)
    shader.uniform_float("checker", 1.0 if settings.uv_view == 'CHECKER' else 0.0)
    shader.uniform_float("squares", float(settings.uv_checker))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    _view["batch"].draw(shader)
    gpu.state.depth_test_set('NONE')
    gpu.matrix.pop()


def draw(shader, context, tips, scale):
    """The UV page in the headset: the view, the seams of what the tools work on, the edges in reach of
    the Seams tool, and the faces a Fix stroke has brushed. shader: UNIFORM_COLOR."""
    from . import shading
    settings = context.scene.vr_sculpt
    if settings.uv_view != 'NONE':
        _draw_view(context, settings)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    seam_pts, reach_pts = [], []
    for obj in shading.mark_targets(context):
        segs = shading._edge_segments(obj)
        seam_pts.append(segs[seams(obj.data)].reshape(-1, 3))
        if settings.uv_tool == 'SEAM':
            for tip in tips:
                reach_pts.append(segs[shading._near(segs, np.array(tip[:]), shading.MARK_REACH * scale)].reshape(-1, 3))
    gpu.state.line_width_set(3.0)
    for pts, color in ((seam_pts, (*SEAM_COLOR, 0.95)), (reach_pts, (1.0, 1.0, 1.0, 0.8))):
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
