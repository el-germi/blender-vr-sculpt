"""Retopo mode: draw clean low-poly topology on a high-poly mesh (a sculpt) with the controllers.

The reference is the mesh active when Retopo starts; the result is a separate mesh object,
"<reference>.retopo", parented to it with an identity parent inverse (it moves with it and shares its
space). Its "vrs_retopo_of" ID property points at the reference, so a later session, or the file
opened again, continues it; the scene settings keep the reference and the mesh being edited
(retopo_reference, retopo_object).

Everything placed lands on the reference's surface: a BVH tree of the evaluated reference in world
space, built once per reference change (a depsgraph handler counts the reference's geometry updates),
answers the closest point near the tip, or the first hit along the controller toward the tip when the
tip is inside the mesh. New vertices snap to the retopo vertices in reach.

Tools (retopo_tool):
  Strip  drag along the surface: a strip of roughly square quads, the size wide. A drag that starts
         on an open border edge carries on from that edge (its length is the width).
  Quad   click to place vertices: every four make a quad (three, then the first again, a triangle)
         and its last edge stays open, so two more clicks make the next quad. A click on a border edge
         starts from it, a drag pulls a quad out of it; a drag on a vertex slides it, and dropping it on
         another merges them. B or the off hand in empty space ends the open quad.
  Relax  a brush that evens out the spacing: vertices move toward their neighbors' mean along the
         surface and back onto it (border vertices along the border; corners stay).
  Tweak  grab the vertices in reach, with falloff, and slide them along the surface.
The off-hand trigger sweeps faces and loose vertices away: they turn red and go on release.

Strip and Quad edit a bmesh made from the mesh at the press, written back with to_mesh() when it
changes (Object Mode, no Edit Mode round trips); Relax and Tweak only move vertices, with numpy and
foreach_set. Mirror X keeps a twin at the mirrored position (across the reference's local X) for each
vertex off the center line, and puts vertices near it on x = 0.

The headset view is drawn here (draw()): faces see-through at the chosen opacity, edges and vertices
over them, all pulled toward the eye like the weight heat map so they don't z-fight the reference;
border edges stand out, and the tips show where they land and what they would snap to or delete.

Nothing keeps Blender data from one press to the next: objects are found by name or through the
settings' pointers, and the caches (the BVH, the display arrays, the open quad) are cleared on undo,
redo and load (forget_caches).
"""

import math
import time

import bmesh
import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector, kdtree
from mathutils.bvhtree import BVHTree

from . import brushes

LINK = "vrs_retopo_of"  # ID property of a retopo mesh: its reference object
DISPLAY = "vrs_retopo_display"  # the display type a retopo mesh had before it was shown as a wire
SUFFIX = ".retopo"
SHRINKWRAP = "VR Retopo Shrinkwrap"

REACH = 0.05  # hand-space meters from the tip to the surface that still count as on it
SNAP = 0.01  # hand-space meters: a vertex placed this close to an existing one is that one...
SNAP_SHARE = 0.4  # ... or within this share of the size, whichever is more
CENTER_SHARE = 0.5  # Mirror X: within this share of the snap reach from the center line, a vertex sits on it
MAX_STEPS = 8  # strip quads laid per update at most (a fast sweep catches up over a few frames)
RELAX_RATE = 12.0  # how fast Relax moves a vertex toward its neighbors' mean, at full strength and pressure
DELETE_SHARE = 0.5  # the off hand deletes within this share of the size (at least SNAP)
CHECK_SECONDS = 0.25  # how often the drawn mesh is compared with the mesh, for edits made elsewhere
HOVER_SECONDS = 0.006  # both eyes of one headset frame share the hover tests
MESSAGE_SECONDS = 6.0

TOOLS = (('STRIP', "Strip", "strip"), ('QUAD', "Quad", "quad"), ('RELAX', "Relax", "relax"),
         ('TWEAK', "Tweak", "tweak"))
TOOL_ICONS = {ident: icon for ident, _name, icon in TOOLS}
TOOL_COLORS = {'STRIP': (0.35, 0.8, 1.0), 'QUAD': (0.5, 1.0, 0.7), 'RELAX': (0.75, 0.6, 1.0),
               'TWEAK': (1.0, 0.78, 0.35)}
INFO = {
    'STRIP': ("Strip", "Drag on the surface to lay quads. Start on a border to carry it on"),
    'QUAD': ("Quad Draw", "Click points: four make a quad. Drag a border edge to pull one out"),
    'RELAX': ("Relax", "Brush to even out the spacing, on the surface"),
    'TWEAK': ("Tweak", "Grab vertices and slide them along the surface"),
}
TRIGGER_HINTS = {'STRIP': ("strip", "Lay strip"), 'QUAD': ("quad", "Place point"), 'RELAX': ("relax", "Relax"),
                 'TWEAK': ("tweak", "Tweak")}

FACE_COLOR = (0.3, 0.7, 1.0)
EDGE_COLOR = (0.6, 0.88, 1.0)
BORDER_COLOR = (1.0, 0.68, 0.3)
VERT_COLOR = (0.92, 0.97, 1.0)
DELETE_COLOR = (1.0, 0.35, 0.35)
HOVER_COLOR = (1.0, 0.9, 0.35)
MIRROR_COLOR = (1.0, 0.35, 0.35)
# Pulled toward the eye by this share of their distance (view space): the same pixels, a little nearer
# (3 mm at 1 m), so the retopo mesh wins over the reference surface it lies on. Flat quads sag under a
# curved surface (a 2 cm quad on a 5 cm radius, by 1 mm): with less, the reference showed through them.
FACE_PULL = 0.003
EDGE_PULL = 0.0035
VERT_PULL = 0.004

# pen: [(vertex index, local co)] of the open quad, on the mesh named pen_key. message: (text, time).
_state = {"pen": [], "pen_key": None, "message": ("", 0.0)}
_deleting = [None]  # the off hand's Delete while it sweeps, for drawing


# --- reference and result ------------------------------------------------------------

def is_retopo(obj):
    return obj is not None and isinstance(obj.get(LINK), bpy.types.Object)


def reference(context):
    """The mesh being retopologized, or None."""
    ref = context.scene.vr_sculpt.retopo_reference
    if ref is None or ref.type != 'MESH' or ref.name not in context.scene.objects:
        return None
    return ref


def result(context):
    """The retopo mesh being edited (linked to the reference), or None: the next stroke starts one."""
    obj = context.scene.vr_sculpt.retopo_object
    ref = reference(context)
    if obj is None or ref is None or obj.type != 'MESH' or obj.name not in context.scene.objects:
        return None
    return obj if obj.get(LINK) == ref else None


def references(context):
    return [o for o in context.scene.objects if o.type == 'MESH' and not is_retopo(o)]


def results_of(context, ref):
    return [o for o in context.scene.objects if o.type == 'MESH' and ref is not None and o.get(LINK) == ref]


def faces(context):
    obj = result(context)
    return len(obj.data.polygons) if obj is not None else 0


def _style(obj, editing):
    """While edited, a retopo mesh shows as a wire in front (the headset draws it see-through itself);
    afterwards as it was."""
    if obj is None:
        return
    if editing:
        if DISPLAY not in obj:
            obj[DISPLAY] = obj.display_type
        obj.display_type = 'WIRE'
        obj.show_in_front = True
    else:
        if DISPLAY in obj:
            try:
                obj.display_type = obj[DISPLAY]
            except (TypeError, ValueError):
                obj.display_type = 'TEXTURED'
            del obj[DISPLAY]
        obj.show_in_front = False


def enter(context):
    """Retopo mode: the active mesh is the reference (or, if it's a retopo mesh, its reference, and
    it's the one edited); its latest retopo mesh carries on. The surface is read now, not at the first
    stroke."""
    from . import menus, primitives
    settings = context.scene.vr_sculpt
    forget_caches()
    active = context.view_layer.objects.active
    if active is not None and active.type == 'MESH':
        menus.set_object_mode(context, active, 'OBJECT')
        if is_retopo(active):
            ref = active[LINK]
            settings.retopo_reference = ref
            settings.retopo_object = active
            if ref.name in context.view_layer.objects:
                primitives.make_active(context, ref)  # Sculpt (say) goes back to the reference.
        else:
            settings.retopo_reference = active
            if result(context) is None:
                found = results_of(context, active)
                settings.retopo_object = found[-1] if found else None
    _style(result(context), True)
    ref = reference(context)
    if ref is not None:
        context.view_layer.update()  # The mode switch's geometry update counted before the tree is built.
        surface(context, ref)


def leave(context):
    _style(result(context), False)
    end_quad()
    _deleting[0] = None


def choose_reference(context, name):
    obj = bpy.data.objects.get(name)
    if obj is None or obj.type != 'MESH':
        return
    settings = context.scene.vr_sculpt
    _style(result(context), False)
    settings.retopo_reference = obj
    found = results_of(context, obj)
    settings.retopo_object = found[-1] if found else None
    _style(result(context), True)
    end_quad()


def choose_result(context, name):
    """Mesh picker: carry on with this retopo mesh, or "" for a new one."""
    obj = bpy.data.objects.get(name) if name else None
    if obj is None or obj.get(LINK) != reference(context):
        new_mesh(context)
        return
    _style(result(context), False)
    context.scene.vr_sculpt.retopo_object = obj
    _style(obj, True)
    end_quad()


def new_mesh(context):
    """The next stroke starts a new retopo mesh (the current one is kept)."""
    _style(result(context), False)
    context.scene.vr_sculpt.retopo_object = None
    end_quad()
    _say("The next stroke starts a new mesh")


def new_result(context, ref):
    """An empty retopo mesh for ref, in its collection, parented to it, the one being edited."""
    me = bpy.data.meshes.new(ref.name + SUFFIX)
    obj = bpy.data.objects.new(ref.name + SUFFIX, me)
    linked = False
    for coll in ref.users_collection:
        try:
            coll.objects.link(obj)
            linked = True
            break
        except RuntimeError:
            continue  # A linked (library) collection.
    if not linked:
        context.scene.collection.objects.link(obj)
    obj.parent = ref
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj[LINK] = ref
    _style(obj, True)
    context.scene.vr_sculpt.retopo_object = obj
    context.view_layer.update()
    return obj


def finish(context):
    """Done for now: the retopo mesh shows as a mesh again, selected, in Model mode."""
    from . import menus, primitives
    obj = result(context)
    if obj is None or not len(obj.data.polygons):
        return _say("Nothing to finish yet")
    n = len(obj.data.polygons)
    menus.set_mode(context, 'MODEL')
    if obj.name in context.view_layer.objects:
        primitives.make_active(context, obj)
    return _say(f"{obj.name}: {n} faces")


def has_shrinkwrap(obj):
    return obj is not None and obj.modifiers.get(SHRINKWRAP) is not None


def toggle_shrinkwrap(context):
    """A Shrinkwrap modifier onto the reference on the retopo mesh, or off again."""
    obj, ref = result(context), reference(context)
    if obj is None:
        return _say("Lay some quads first")
    mod = obj.modifiers.get(SHRINKWRAP)
    if mod is not None:
        obj.modifiers.remove(mod)
        return False
    mod = obj.modifiers.new(SHRINKWRAP, 'SHRINKWRAP')
    mod.target = ref
    mod.wrap_method = 'NEAREST_SURFACEPOINT'
    return True


def message():
    text, when = _state["message"]
    return text if text and time.monotonic() - when < MESSAGE_SECONDS else ""


def _say(text):
    _state["message"] = (text, time.monotonic())
    print("VR Retopo:", text, flush=True)
    return text


# --- tools, the open quad -----------------------------------------------------------

def set_tool(context, tool):
    context.scene.vr_sculpt.retopo_tool = tool
    end_quad()


def end_quad():
    _state.update(pen=[], pen_key=None)


def pen_count():
    return len(_state["pen"])


def cycle_tool(context):
    """B: end the open quad, or else the next tool."""
    if _state["pen"]:
        end_quad()
        return
    settings = context.scene.vr_sculpt
    ids = [t[0] for t in TOOLS]
    settings.retopo_tool = ids[(ids.index(settings.retopo_tool) + 1) % len(ids)]


def snap_reach(settings, scale):
    return max(SNAP, SNAP_SHARE * settings.retopo_size) * scale


def ring_radius(settings):
    """The tip ring's radius (hand space): half the strip's width, the snap reach, or the brush."""
    tool = settings.retopo_tool
    if tool == 'STRIP':
        return settings.retopo_size / 2
    if tool == 'QUAD':
        return snap_reach(settings, 1.0)
    return settings.retopo_size


def ring_color(settings, off):
    return DELETE_COLOR if off else TOOL_COLORS[settings.retopo_tool]


def card_content(settings):
    """(title, description, color, icon) for the tip status."""
    title, desc = INFO[settings.retopo_tool]
    return title, desc, TOOL_COLORS[settings.retopo_tool], TOOL_ICONS[settings.retopo_tool]


def status_size(settings):
    if settings.retopo_tool == 'QUAD':
        return f"{pen_count()} of 4" if pen_count() else ""
    v = settings.retopo_size
    return f"{v * 100:.1f} cm" if v < 1.0 else f"{v:.2f} m"


def hint_top():
    """What B does in Retopo mode."""
    return ("close", "End quad") if _state["pen"] else ("chevron_right", "Next tool")


# --- spaces ----------------------------------------------------------------------------

def _np(m):
    return np.array(m, dtype=np.float64)


def _apply(m, pts):
    m = _np(m)
    return pts @ m[:3, :3].T + m[:3, 3]


class Frame:
    """World space and the retopo mesh's own space, and Mirror X across the reference's local X."""

    def __init__(self, obj, ref):
        rm = ref.matrix_world.copy()
        # A mesh still to be made gets the reference's space (it's parented to it, identity inverse).
        self.mw = obj.matrix_world.copy() if obj is not None else rm.copy()
        self.mwi = self.mw.inverted_safe()
        self.rm, self.rmi = rm, rm.inverted_safe()
        self.flip = rm @ Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0)) @ self.rmi
        self.sx = max(rm.col[0].xyz.length, 1e-12)
        self.normal = self.mwi.transposed().to_3x3()

    def world(self, co):
        return self.mw @ co

    def local(self, p):
        return self.mwi @ p

    def mirrored(self, p):
        return self.flip @ p

    def off_center(self, p):
        """World distance of p from the mirror plane."""
        return abs((self.rmi @ p).x) * self.sx

    def onto_center(self, p):
        q = self.rmi @ p
        q.x = 0.0
        return self.rm @ q

    def side(self, p):
        return 1.0 if (self.rmi @ p).x >= 0.0 else -1.0

    def ref_x(self, pts):
        """The reference-space x of world points (n, 3)."""
        return _apply(self.rmi, pts)[:, 0]


# --- the surface ------------------------------------------------------------------------

class Surface:
    """The reference's evaluated mesh in world space, as a BVH tree."""

    def __init__(self, context, ref):
        t0 = time.perf_counter()
        ev = ref.evaluated_get(context.evaluated_depsgraph_get())
        me = ev.to_mesh()
        try:
            n = len(me.vertices)
            co = np.empty(n * 3, dtype=np.float64)
            me.vertices.foreach_get("co", co)
            tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
            me.loop_triangles.foreach_get("vertices", tris)
        finally:
            ev.to_mesh_clear()
        world = _apply(ref.matrix_world, co.reshape(-1, 3))
        self.triangles = len(tris) // 3
        self.tree = BVHTree.FromPolygons(world.tolist(), tris.reshape(-1, 3).tolist(), all_triangles=True) \
            if self.triangles else None
        self.ms = (time.perf_counter() - t0) * 1000.0

    def nearest(self, point, distance=None):
        """(point, normal) of the closest surface point (within distance), or None."""
        if self.tree is None:
            return None
        hit = self.tree.find_nearest(point) if distance is None else self.tree.find_nearest(point, distance)
        return None if hit[0] is None else (hit[0], hit[1].normalized())

    def ray(self, origin, direction, distance):
        if self.tree is None:
            return None
        hit = self.tree.ray_cast(origin, direction, distance)
        return None if hit[0] is None else (hit[0], hit[1].normalized())

    def locate(self, tip, origin, reach):
        """Where the tip lands: the closest point within reach, else (the tip inside the mesh) the first
        hit from the controller toward it. (point, normal) or None."""
        tip = Vector(tip)
        hit = self.nearest(tip, reach)
        if hit is not None or origin is None:
            return hit
        d = tip - Vector(origin)
        length = d.length
        if length < 1e-9:
            return None
        return self.ray(Vector(origin), d / length, length + reach)

    def project(self, point):
        """point moved onto the surface (unchanged without one)."""
        hit = self.nearest(Vector(point))
        return hit[0] if hit is not None else Vector(point)


_surface = {"key": None, "surface": None}
_ref_version = [0]  # bumped when the reference's geometry changes (depsgraph handler)


def _matrix_key(m):
    return tuple(round(v, 6) for row in m for v in row)


def surface(context, ref):
    """The reference's Surface, built once per reference change."""
    key = (ref.name, ref.data.name if ref.data is not None else "", _matrix_key(ref.matrix_world), _ref_version[0])
    if _surface["key"] != key or _surface["surface"] is None:
        surf = Surface(context, ref)
        print(f"VR Retopo: surface of {ref.name}, {surf.triangles} triangles, in {surf.ms:.0f} ms", flush=True)
        _surface.update(key=key, surface=surf)
    return _surface["surface"]


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    key = _surface["key"]
    if key is None:
        return
    for update in depsgraph.updates:
        if update.is_updated_geometry and isinstance(update.id, bpy.types.Object):
            obj = getattr(update.id, "original", update.id)
            if obj.name == key[0]:
                _ref_version[0] += 1
                return


def _tangent(v, n):
    return v - n * v.dot(n)


def _segment_distance(p, a, b):
    ab = b - a
    length2 = ab.length_squared
    t = 0.0 if length2 < 1e-18 else min(max((p - a).dot(ab) / length2, 0.0), 1.0)
    return (a + ab * t - p).length


# --- editing topology (Strip, Quad) ----------------------------------------------------------------

class Editor:
    """A bmesh of the retopo mesh for one press: snapping, Mirror X twins, the open quad and writing it
    back. Without a retopo mesh yet, it starts empty and the mesh is made by the first write."""

    def __init__(self, context, settings, obj, ref, surf, scale):
        self.name = obj.name if obj is not None else None
        self.ref_name = ref.name
        self.frame = Frame(obj, ref)
        self.surf = surf
        self.mirror = settings.retopo_mirror
        self.snap = snap_reach(settings, scale)
        self.center = self.snap * CENTER_SHARE
        self.bm = bmesh.new()
        if obj is not None:
            self.bm.from_mesh(obj.data)
        self.bm.verts.ensure_lookup_table()
        self.verts = list(self.bm.verts)
        self.kd = kdtree.KDTree(len(self.verts))
        for i, v in enumerate(self.verts):
            self.kd.insert(self.frame.world(v.co), i)
        self.kd.balance()
        self.added = []
        self.twins = {}
        self.pen = self._load_pen()
        self.dirty = False
        self.freed = False

    # vertices

    def world(self, v):
        return self.frame.world(v.co)

    def find(self, p, reach=None, exclude=()):
        """The vertex nearest p (world) within reach (the snap reach), or None. Uses the positions at
        the press (plus the vertices made since)."""
        reach = self.snap if reach is None else reach
        best, best_d = None, reach
        for _co, i, d in self.kd.find_range(p, reach):
            v = self.verts[i]
            if d <= best_d and v.is_valid and v not in exclude:
                best, best_d = v, d
        for v in self.added:
            if v.is_valid and v not in exclude:
                d = (self.world(v) - p).length
                if d <= best_d:
                    best, best_d = v, d
        return best

    def nearest_vert(self, p, reach, exclude=()):
        """Like find(), from the current positions of every vertex."""
        best, best_d = None, reach
        for v in self.bm.verts:
            if v not in exclude:
                d = (self.world(v) - p).length
                if d <= best_d:
                    best, best_d = v, d
        return best

    def _vert(self, p):
        v = self.bm.verts.new(self.frame.local(p))
        self.added.append(v)
        self.dirty = True
        return v

    def new_vert(self, p):
        """A new vertex at p (world, on the surface). With Mirror X it gets its twin, or sits on the center
        line when it's near it."""
        p = Vector(p)
        if not self.mirror:
            return self._vert(p)
        if self.frame.off_center(p) <= self.center:
            v = self._vert(self.frame.onto_center(p))
            self.twins[v] = v
            return v
        v = self._vert(p)
        q = self.frame.mirrored(p)
        t = self.find(q, self.center, exclude={v}) or self._vert(q)
        self.twins[v], self.twins[t] = t, v
        return v

    def vert_at(self, p, exclude=()):
        """The vertex in snap reach of p, or a new one there."""
        return self.find(p, exclude=exclude) or self.new_vert(p)

    def twin(self, v, create=True):
        """v's mirror twin (v itself on the center line); made at the mirrored position if missing."""
        t = self.twins.get(v)
        if t is not None and t.is_valid:
            return t
        p = self.world(v)
        if self.frame.off_center(p) <= self.center:
            self.twins[v] = v
            return v
        q = self.frame.mirrored(p)
        t = self.find(q, self.center, exclude={v})
        if t is None:
            if not create:
                return None
            t = self._vert(q)
        self.twins[v], self.twins[t] = t, v
        return t

    def border_edge(self, p, reach):
        """The open border edge (one face) nearest p within reach, or None."""
        best, best_d = None, reach
        for e in self.bm.edges:
            if not e.is_boundary:
                continue
            a, b = (self.world(v) for v in e.verts)
            d = _segment_distance(p, a, b)
            if d <= best_d:
                best, best_d = e, d
        return best

    # faces

    def can_face(self, verts):
        verts = [v for v in verts if v is not None]
        if len(set(verts)) < 3 or self.bm.faces.get(verts) is not None:
            return False
        for a, b in zip(verts, verts[1:] + verts[:1]):
            e = self.bm.edges.get((a, b))
            if e is not None and len(e.link_faces) >= 2:
                return False
        return True

    def _face(self, verts):
        verts = list(dict.fromkeys(verts))
        if len(verts) < 3 or not self.can_face(verts):
            return None
        try:
            face = self.bm.faces.new(verts)
        except ValueError:
            return None
        face.normal_update()
        hit = self.surf.nearest(self.frame.world(face.calc_center_median()))
        if hit is not None and (self.frame.normal @ face.normal).dot(hit[1]) < 0.0:
            face.normal_flip()  # Facing out of the reference, so neighbors agree.
        self.dirty = True
        return face

    def add_face(self, verts):
        """A face through verts, facing out of the reference; with Mirror X its mirror image too. None if
        it would be degenerate, exists already or would give an edge a third face."""
        face = self._face(verts)
        if face is not None and self.mirror:
            twins = [self.twin(v) for v in reversed(verts)]
            if set(twins) != set(verts):
                self._face(twins)
        return face

    # the open quad

    def _load_pen(self):
        if self.name is None or _state["pen_key"] != self.name:
            return []
        out = []
        for i, co in _state["pen"]:
            if i >= len(self.verts) or (self.verts[i].co - Vector(co)).length > 1e-6:
                return []  # The mesh changed under it.
            out.append(self.verts[i])
        return out

    def save_pen(self):
        self.pen = [v for v in self.pen if v.is_valid]
        self.bm.verts.index_update()
        _state.update(pen=[(v.index, tuple(v.co)) for v in self.pen], pen_key=self.name if self.pen else None)

    # writing

    def object(self, context, create=False):
        obj = bpy.data.objects.get(self.name) if self.name else None
        if obj is not None and obj.type == 'MESH':
            return obj
        if not create:
            return None
        ref = bpy.data.objects.get(self.ref_name)
        if ref is None:
            return None
        obj = new_result(context, ref)
        self.name = obj.name
        return obj

    def write(self, context):
        """The bmesh into the retopo mesh (made now if there's none yet)."""
        obj = self.object(context, create=True)
        if obj is None:
            return False
        self.bm.to_mesh(obj.data)
        obj.data.update()
        self.dirty = False
        self.save_pen()
        changed()
        return True

    def free(self):
        if not self.freed:
            self.freed = True
            self.bm.free()


def _slide(ed, v, target):
    """Move v onto the surface near target (world); its twin follows, a center vertex stays on x = 0."""
    p = ed.surf.project(target)
    twin = ed.twin(v, create=False) if ed.mirror else None
    if twin is v:
        p = ed.frame.onto_center(p)
    v.co = ed.frame.local(p)
    if twin is not None and twin is not v:
        twin.co = ed.frame.local(ed.frame.mirrored(p))
    ed.dirty = True


def _perimeter(ed, cycle):
    pts = [ed.world(v) for v in cycle]
    return sum((pts[i] - pts[i - 1]).length for i in range(len(pts)))


def best_cycle(ed, pen):
    """The order of four vertices for a quad: the shortest way round (so it isn't a bow tie) that keeps
    the edges already between them as sides."""
    cycles = [(0, 1, 2, 3), (0, 1, 3, 2), (0, 2, 1, 3)]
    existing = {frozenset((a, b)) for i, a in enumerate(pen) for b in pen[i + 1:] if ed.bm.edges.get((a, b))}

    def sides(c):
        return {frozenset((pen[c[i]], pen[c[(i + 1) % 4]])) for i in range(4)}
    keep = [c for c in cycles if existing <= sides(c)] or cycles
    best = min(keep, key=lambda c: _perimeter(ed, [pen[i] for i in c]))
    return [pen[i] for i in best]


def pen_push(ed, v):
    """v joins the open quad. Four vertices make a quad, whose side from the last vertex to the one
    clicked just before it stays open; three and the first again make a triangle. Returns the face
    made, or None."""
    pen = ed.pen
    if v in pen:
        if v is pen[0] and len(pen) == 3:
            face = ed.add_face(pen)
            ed.pen = []
            return face
        return None
    pen.append(v)
    if len(pen) < 4:
        return None
    cycle = best_cycle(ed, pen)
    face = ed.add_face(cycle)
    if face is None:
        ed.pen = [v]  # Not possible (it exists, or an edge is full): start again from here.
        return None
    i = cycle.index(v)
    a, b = cycle[i - 1], cycle[(i + 1) % 4]
    ed.pen = [a if pen.index(a) > pen.index(b) else b, v]
    return face


class Strip:
    """Strip tool: drag along the surface, laying roughly square quads, the size wide. Starting on a
    border edge carries on from it (its length is the width), never back over its own face."""

    def __init__(self, context, settings, obj, ref, surf, pose, tip_pose, scale):
        self.ed = Editor(context, settings, obj, ref, surf, scale)
        self.surf = surf
        self.reach = REACH * scale
        self.width = settings.retopo_size * scale
        self.rail = None  # (a, b): the last row of the strip
        self.start = None  # (point, normal) of the press, until the first row
        self.outward = None  # carrying on from an edge: the way out of its face
        self.changed = False
        hit = surf.locate(tip_pose.translation, pose.translation, self.reach)
        if hit is None:
            return
        edge = self.ed.border_edge(hit[0], self.ed.snap)
        if edge is None:
            self.start = hit
            return
        a, b = edge.verts
        wa, wb = self.ed.world(a), self.ed.world(b)
        self.rail = (a, b)
        self.width = max((wb - wa).length, 1e-6)
        out = (wa + wb) / 2 - self.ed.frame.world(edge.link_faces[0].calc_center_median())
        along = (wb - wa).normalized()
        out -= along * out.dot(along)
        self.outward = out.normalized() if out.length > 1e-9 else None

    def _row(self, c, cn, d, along=None):
        """The two ends (world, on the surface) of a row centered at c (normal cn) across direction d;
        along: the last row's (a, b) positions, so the ends keep their sides. With Mirror X a row near
        the center line is centered on it: along the line, its ends mirror each other; across it, the
        row lies on it."""
        side = d.cross(cn)
        frame = self.ed.frame
        if self.ed.mirror and frame.off_center(c) <= self.width * 0.5:
            c = self.surf.project(frame.onto_center(c))
            across = _tangent(frame.rmi.transposed().to_3x3() @ Vector((1.0, 0.0, 0.0)), cn)
            if across.length > 1e-9 and abs(d.dot(across.normalized())) < 0.5:
                side = across  # The strip runs along the center line.
        if side.length < 1e-9:
            return None
        side.normalize()
        if along is not None and side.dot(along[1] - along[0]) < 0.0:
            side = -side
        return self.surf.project(c - side * (self.width / 2)), self.surf.project(c + side * (self.width / 2))

    def _first_row(self, p, n):
        sp, sn = self.start
        d = _tangent(p - sp, sn)
        if d.length < self.width * 0.5:
            return False
        ends = self._row(sp, sn, d.normalized())
        if ends is None:
            return False
        a = self.ed.vert_at(ends[0])
        b = self.ed.vert_at(ends[1], exclude={a})
        if a is b:
            return False
        self.rail = (a, b)
        return True

    def _advance(self, p, n):
        """One more quad toward p, if it's a step away. Returns whether it made one."""
        a, b = self.rail
        wa, wb = self.ed.world(a), self.ed.world(b)
        mid = (wa + wb) / 2
        d = _tangent(p - mid, n)
        if d.length < self.width:
            return False
        d.normalize()
        if self.outward is not None and d.dot(self.outward) < 0.3:
            return False  # Toward the face it carries on from: wait.
        hit = self.surf.nearest(mid + d * self.width)
        if hit is None:
            return False
        ends = self._row(hit[0], hit[1], d, (wa, wb))
        if ends is None:
            return False
        pa, pb = ends
        fa = self.ed.find(pa, exclude={a, b})
        fb = self.ed.find(pb, exclude={a, b, fa})
        if fa is not None and fb is not None and not self.ed.can_face([a, b, fb, fa]):
            return False
        na = fa or self.ed.new_vert(pa)
        nb = fb or self.ed.vert_at(pb, exclude={a, b, na})  # With Mirror X, na's twin may be it.
        if self.ed.add_face([a, b, nb, na]) is None:
            return False
        self.rail = (na, nb)
        self.outward = None
        return True

    def update(self, context, pose, tip_pose, value):
        if self.rail is None and self.start is None:
            return
        hit = self.surf.locate(tip_pose.translation, pose.translation, self.reach)
        if hit is None:
            return
        p, n = hit
        if self.rail is None and not self._first_row(p, n):
            return
        made = False
        for _ in range(MAX_STEPS):
            if not self._advance(p, n):
                break
            made = True
        if made:  # The first row alone isn't written: a click doesn't leave two loose vertices.
            self.ed.write(context)
            self.changed = True

    def abandon(self):
        self.ed.free()

    def finish(self, context):
        self.ed.free()


class PlacePoint:
    """Quad tool, a press on the surface away from the mesh: a new vertex (hold and move to place it),
    which joins the open quad."""

    def __init__(self, context, ed, hit, tip_pose):
        from . import interactions
        self.ed = ed
        self.v = ed.new_vert(hit[0])
        self.offset = hit[0] - tip_pose.translation
        self.gate = interactions.DragGate(context, tip_pose)
        pen_push(ed, self.v)
        ed.write(context)
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.offset = self.ed.world(self.v) - base.translation
        if self.v.is_valid:
            _slide(self.ed, self.v, tip_pose.translation + self.offset)
            self.ed.write(context)

    def abandon(self):
        self.ed.free()

    def finish(self, context):
        self.ed.free()


def _weld(ed, pairs):
    """Merge each (vertex, onto) pair, and with Mirror X their twins the same way. Returns how many."""
    targetmap = {}
    for v, t in pairs:
        if v is None or t is None or v is t or not v.is_valid or not t.is_valid:
            continue
        if ed.mirror:
            tv, tt = ed.twin(v, create=False), ed.twin(t, create=False)
            if tv is not None and tt is not None and tv not in (v, t) and tt not in (v, t, tv):
                targetmap[tv] = tt
        targetmap[v] = t
    if targetmap:
        bmesh.ops.weld_verts(ed.bm, targetmap=targetmap)
        ed.twins = {a: b for a, b in ed.twins.items() if a.is_valid and b.is_valid}
        ed.dirty = True
    return len(targetmap)


class VertexPress:
    """Quad tool, a press on a vertex: a click adds it to the open quad; a drag slides it along the
    surface, and dropping it on another vertex merges the two."""

    def __init__(self, context, ed, v, tip_pose):
        from . import interactions
        self.ed, self.v = ed, v
        self.offset = ed.world(v) - tip_pose.translation
        self.gate = interactions.DragGate(context, tip_pose)
        self.twin = ed.twin(v, create=False) if ed.mirror else None
        self.moved = False
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.offset = self.ed.world(self.v) - base.translation
        _slide(self.ed, self.v, tip_pose.translation + self.offset)
        self.ed.write(context)
        self.moved = self.changed = True

    def abandon(self):
        self.ed.free()

    def finish(self, context):
        ed = self.ed
        if self.moved:
            onto = ed.nearest_vert(ed.world(self.v), ed.snap, exclude={self.v, self.twin})
            if onto is not None and _weld(ed, [(self.v, onto)]):
                ed.write(context)
        elif pen_push(ed, self.v) is not None:
            ed.write(context)
            self.changed = True
        else:
            ed.save_pen()
        ed.free()


class EdgePress:
    """Quad tool, a press on a border edge: a click starts the open quad from it; a drag pulls a new quad
    out of it, stitched to the vertices it's dropped on."""

    def __init__(self, context, ed, edge, tip_pose):
        from . import interactions
        self.ed = ed
        self.a, self.b = edge.verts
        self.wa, self.wb = ed.world(self.a), ed.world(self.b)
        self.start = tip_pose.translation.copy()
        self.gate = interactions.DragGate(context, tip_pose)
        self.new = None
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.start = base.translation.copy()
        delta = tip_pose.translation - self.start
        ed = self.ed
        if self.new is None:
            na = ed.new_vert(ed.surf.project(self.wa + delta))
            pb = ed.surf.project(self.wb + delta)
            twin = ed.twins.get(na)  # Pulling a symmetric edge across the center line: na's twin is the other.
            nb = twin if twin not in (None, na) and (ed.world(twin) - pb).length <= ed.snap else ed.new_vert(pb)
            if ed.add_face([self.a, self.b, nb, na]) is None:
                return
            self.new = (na, nb)
        else:
            _slide(ed, self.new[0], self.wa + delta)
            _slide(ed, self.new[1], self.wb + delta)
        ed.write(context)
        self.changed = True

    def abandon(self):
        self.ed.free()

    def finish(self, context):
        ed = self.ed
        if self.new is None:
            ed.pen = [self.b, self.a]
            ed.save_pen()
        else:
            skip = {self.a, self.b, *self.new}
            if ed.mirror:
                skip |= {ed.twin(v, create=False) for v in self.new}
            pairs = [(v, ed.nearest_vert(ed.world(v), ed.snap, exclude=skip)) for v in self.new]
            if _weld(ed, pairs):
                ed.write(context)
        ed.free()


def _quad_pick(context, settings, obj, ref, surf, pose, tip_pose, scale):
    hit = surf.locate(tip_pose.translation, pose.translation, REACH * scale)
    ed = Editor(context, settings, obj, ref, surf, scale)
    point = hit[0] if hit is not None else tip_pose.translation
    v = ed.find(point)
    if v is not None:
        return VertexPress(context, ed, v, tip_pose)
    edge = ed.border_edge(point, ed.snap)
    if edge is not None:
        return EdgePress(context, ed, edge, tip_pose)
    if hit is None:
        ed.free()
        return None
    return PlacePoint(context, ed, hit, tip_pose)


# --- moving vertices (Relax, Tweak) -------------------------------------------------------------

def _csr(n, pairs):
    """(indptr, neighbors) of an undirected graph with n nodes and (m, 2) edges."""
    indptr = np.zeros(n + 1, dtype=np.int64)
    if not len(pairs):
        return indptr, np.zeros(0, dtype=np.int64)
    a = np.concatenate([pairs[:, 0], pairs[:, 1]])
    b = np.concatenate([pairs[:, 1], pairs[:, 0]])
    order = np.argsort(a, kind='stable')
    np.add.at(indptr, a + 1, 1)
    return np.cumsum(indptr), b[order]


class Points:
    """The retopo mesh's vertices in world space at the press, its edges, and with Mirror X each
    vertex's twin (itself on the center line, -1 without one)."""

    def __init__(self, settings, obj, ref, scale):
        self.name = obj.name
        me = obj.data
        self.frame = Frame(obj, ref)
        self.n = n = len(me.vertices)
        co = np.empty(n * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        self.world = _apply(self.frame.mw, co.reshape(-1, 3))
        edges = np.empty(len(me.edges) * 2, dtype=np.int64)
        me.edges.foreach_get("vertices", edges)
        self.edges = edges.reshape(-1, 2)
        self.mirror = settings.retopo_mirror
        self.center = snap_reach(settings, scale) * CENTER_SHARE
        self.twin = self._twins() if self.mirror and n else None

    def _twins(self):
        n = self.n
        kd = kdtree.KDTree(n)
        for i, p in enumerate(self.world):
            kd.insert(p, i)
        kd.balance()
        flipped = _apply(self.frame.flip, self.world)
        on_center = np.abs(self.frame.ref_x(self.world)) * self.frame.sx <= self.center
        twin = np.full(n, -1, dtype=np.int64)
        for i in range(n):
            if on_center[i]:
                twin[i] = i
                continue
            _co, j, d = kd.find(flipped[i])
            if j is not None and d <= self.center:
                twin[i] = j
        return twin

    def brush(self, tip, radius):
        """(indices, falloff) of the vertices within radius of the tip (and with Mirror X of the
        mirrored tip), leaving out those that follow their twin on the tip's side."""
        w = np.zeros(self.n)
        centers = [Vector(tip)]
        if self.mirror:
            centers.append(self.frame.mirrored(Vector(tip)))
        for c in centers:
            d = np.linalg.norm(self.world - np.array(c), axis=1)
            inside = d < radius
            w[inside] = np.maximum(w[inside], brushes.falloff(d[inside], radius))
        idx = np.nonzero(w > 0.0)[0]
        if self.twin is not None and len(idx):
            side = self.frame.side(Vector(tip))
            x = self.frame.ref_x(self.world[idx])
            follows = (self.twin[idx] >= 0) & (self.twin[idx] != idx) & (x * side < 0.0)
            idx = idx[~follows]
        return idx, w[idx]

    def mirror_follow(self, idx):
        """Twins of idx to the mirrored positions, center vertices onto x = 0."""
        if self.twin is None:
            return
        for i in idx.tolist():
            t = self.twin[i]
            if t == i:
                self.world[i] = self.frame.onto_center(Vector(self.world[i]))
            elif t >= 0:
                self.world[t] = self.frame.mirrored(Vector(self.world[i]))

    def write(self):
        obj = bpy.data.objects.get(self.name)
        if obj is None or obj.type != 'MESH' or len(obj.data.vertices) != self.n:
            return False
        local = _apply(self.frame.mwi, self.world)
        obj.data.vertices.foreach_set("co", local.astype(np.float32).ravel())
        obj.data.update()
        changed()
        return True


class Relax:
    """Relax tool: while held, the vertices in reach move toward their neighbors' mean, along the
    surface and back onto it. Border vertices follow their border neighbors (so strips even out along
    their sides); corners (two neighbors) stay."""

    def __init__(self, context, settings, obj, ref, surf, scale):
        self.pts = Points(settings, obj, ref, scale)
        self.surf = surf
        self.radius = settings.retopo_size * scale
        self.strength = settings.retopo_relax
        me = obj.data
        loops = np.empty(len(me.loops), dtype=np.int64)
        me.loops.foreach_get("edge_index", loops)
        counts = np.bincount(loops, minlength=len(me.edges)) if len(me.edges) else np.zeros(0, dtype=np.int64)
        border = self.pts.edges[counts == 1] if len(counts) else self.pts.edges[:0]
        n = self.pts.n
        self.all = _csr(n, self.pts.edges)
        self.border = _csr(n, border)
        self.on_border = np.zeros(n, dtype=bool)
        self.on_border[border.ravel()] = True
        valence = np.diff(self.all[0])
        self.pinned = self.on_border & (valence <= 2)
        self.t = None
        self.changed = False

    def _target(self, i):
        indptr, nbr = self.border if self.on_border[i] else self.all
        s, e = indptr[i], indptr[i + 1]
        return self.pts.world[nbr[s:e]].mean(axis=0) if e > s else self.pts.world[i]

    def step(self, tip, pressure, dt):
        """One brush step at tip (world). Returns how many vertices moved."""
        idx, w = self.pts.brush(tip, self.radius)
        keep = ~self.pinned[idx]
        idx, w = idx[keep], w[keep]
        if not len(idx):
            return 0
        k = np.clip(self.strength * RELAX_RATE * dt * pressure * w, 0.0, 1.0)
        targets = [self._target(i) for i in idx.tolist()]
        world = self.pts.world
        for j, i in enumerate(idx.tolist()):
            p = Vector(world[i])
            hit = self.surf.nearest(p)
            if hit is None:
                continue
            d = (Vector(targets[j]) - p) * float(k[j])
            world[i] = self.surf.project(p + _tangent(d, hit[1]))
        self.pts.mirror_follow(idx)
        if self.pts.write():
            self.changed = True
        return len(idx)

    def update(self, context, pose, tip_pose, value):
        now = time.monotonic()
        dt = 1.0 / 90.0 if self.t is None else min(now - self.t, 0.05)
        self.t = now
        self.step(tip_pose.translation, max(value, 0.05), dt)

    def finish(self, context):
        pass


class Tweak:
    """Tweak tool: the vertices in reach at the press follow the tip (with falloff), sliding along the
    surface; with Mirror X their twins mirror them."""

    def __init__(self, context, settings, obj, ref, surf, tip_pose, scale):
        from . import interactions
        self.pts = Points(settings, obj, ref, scale)
        self.surf = surf
        self.idx, self.w = self.pts.brush(tip_pose.translation, settings.retopo_size * scale)
        self.start = self.pts.world[self.idx].copy()
        self.base = tip_pose.translation.copy()
        self.gate = interactions.DragGate(context, tip_pose)
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.base = base.translation.copy()
        delta = np.array(tip_pose.translation - self.base)
        world = self.pts.world
        for j, i in enumerate(self.idx.tolist()):
            world[i] = self.surf.project(Vector(self.start[j] + delta * self.w[j]))
        self.pts.mirror_follow(self.idx)
        if self.pts.write():
            self.changed = True

    def finish(self, context):
        pass


# --- deleting (the off hand) ---------------------------------------------------------------

def delete_reach(settings, scale):
    return max(DELETE_SHARE * settings.retopo_size, SNAP) * scale


class Delete:
    """Off-hand trigger: the faces whose centers it sweeps past (and loose vertices) are marked, and
    deleted on release with the vertices and edges only they used; Mirror X sweeps the mirror image too."""

    def __init__(self, context, settings, obj, ref, tip, scale):
        self.name = obj.name
        self.frame = Frame(obj, ref)
        self.reach = delete_reach(settings, scale)
        self.mirror = settings.retopo_mirror
        me = obj.data
        centers = np.empty(len(me.polygons) * 3, dtype=np.float64)
        me.polygons.foreach_get("center", centers)
        self.centers = _apply(self.frame.mw, centers.reshape(-1, 3))
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        edges = np.empty(len(me.edges) * 2, dtype=np.int64)
        me.edges.foreach_get("vertices", edges)
        used = np.zeros(len(me.vertices), dtype=bool)
        used[edges] = True
        self.loose = np.nonzero(~used)[0]
        self.loose_world = _apply(self.frame.mw, co.reshape(-1, 3)[self.loose])
        self.faces, self.verts = set(), set()
        self.n_faces, self.n_verts = len(me.polygons), len(me.vertices)
        self.sweep(tip)
        self.touched = bool(self.faces or self.verts)
        self.changed = False
        _deleting[0] = self

    def sweep(self, tip):
        points = [Vector(tip)]
        if self.mirror:
            points.append(self.frame.mirrored(Vector(tip)))
        for p in points:
            q = np.array(p)
            if len(self.centers):
                d = np.linalg.norm(self.centers - q, axis=1)
                self.faces.update(np.nonzero(d <= self.reach)[0].tolist())
            if len(self.loose):
                d = np.linalg.norm(self.loose_world - q, axis=1)
                self.verts.update(self.loose[d <= self.reach].tolist())

    def update(self, context, pose, tip_pose, value):
        self.sweep(tip_pose.translation)

    def abandon(self):
        _deleting[0] = None

    def finish(self, context):
        _deleting[0] = None
        if not (self.faces or self.verts):
            return
        obj = bpy.data.objects.get(self.name)
        if obj is None or len(obj.data.polygons) != self.n_faces or len(obj.data.vertices) != self.n_verts:
            return
        bm = bmesh.new()
        try:
            bm.from_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            bm.verts.ensure_lookup_table()
            gone = [bm.faces[i] for i in sorted(self.faces)]
            loose = [bm.verts[i] for i in sorted(self.verts)]
            if gone:
                bmesh.ops.delete(bm, geom=gone, context='FACES')
            loose = [v for v in loose if v.is_valid]
            if loose:
                bmesh.ops.delete(bm, geom=loose, context='VERTS')
            bm.to_mesh(obj.data)
        finally:
            bm.free()
        obj.data.update()
        end_quad()
        changed()
        self.changed = True


# --- picking -----------------------------------------------------------------------

def pick(context, settings, off, pose, tip_pose, scale, value):
    """Trigger press in Retopo mode: the handler for this tool and hand, or None."""
    ref = reference(context)
    if ref is None:
        _say("Select a mesh to draw new topology on")
        return None
    surf = surface(context, ref)
    obj = result(context)
    tool = settings.retopo_tool
    if off:
        if obj is None:
            end_quad()
            return None
        handler = Delete(context, settings, obj, ref, tip_pose.translation, scale)
        if not handler.touched:
            end_quad()  # In empty space, it also ends the open quad.
        return handler
    if tool == 'STRIP':
        return Strip(context, settings, obj, ref, surf, pose, tip_pose, scale)
    if tool == 'QUAD':
        return _quad_pick(context, settings, obj, ref, surf, pose, tip_pose, scale)
    if obj is None or not len(obj.data.vertices):
        return None
    if tool == 'RELAX':
        return Relax(context, settings, obj, ref, surf, scale)
    handler = Tweak(context, settings, obj, ref, surf, tip_pose, scale)
    return handler if len(handler.idx) else None


def engaged(context, settings, tip, scale):
    """Whether the tip is at the reference's surface (so a desktop screen behind it doesn't take the trigger)."""
    ref = reference(context)
    return ref is not None and surface(context, ref).nearest(Vector(tip), REACH * scale) is not None


# --- caches ----------------------------------------------------------------------

# The retopo mesh as drawn: local positions, world positions, edges, border edges, triangles (with
# their faces), face centers (world) and loose vertices, rebuilt when the mesh changes (our own
# writes bump _version; edits made elsewhere are caught by comparing positions every CHECK_SECONDS).
_view = {"key": None, "checked": 0.0, "co": None, "world": None, "edges": None, "border": None, "tris": None,
         "tri_face": None, "centers": None, "loose": None, "batches": None}
_version = [0]
_hover = {"time": 0.0, "key": None, "value": None}


def changed():
    _version[0] += 1


def forget_caches():
    """Undo / redo / load: the surface, the drawn mesh and the open quad are of data that's gone."""
    _surface.update(key=None, surface=None)
    _view.update(key=None, co=None, world=None, batches=None)
    _hover.update(time=0.0, key=None, value=None)
    _deleting[0] = None
    end_quad()


def _read_co(me):
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def view(obj):
    """_view for obj's mesh, up to date."""
    me = obj.data
    mw = obj.matrix_world
    key = (obj.name, me.name, len(me.vertices), len(me.edges), len(me.polygons), _version[0], _matrix_key(mw))
    now = time.monotonic()
    if key == _view["key"]:
        if now - _view["checked"] < CHECK_SECONDS:
            return _view
        _view["checked"] = now
        co = _read_co(me)
        if np.array_equal(co, _view["co"]):
            return _view
    else:
        co = _read_co(me)
    edges = np.empty(len(me.edges) * 2, dtype=np.int32)
    me.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    loops = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("edge_index", loops)
    counts = np.bincount(loops, minlength=len(me.edges)) if len(me.edges) else np.zeros(0, dtype=np.int64)
    tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("vertices", tris)
    tri_face = np.empty(len(me.loop_triangles), dtype=np.int32)
    me.loop_triangles.foreach_get("polygon_index", tri_face)
    centers = np.empty(len(me.polygons) * 3, dtype=np.float64)
    me.polygons.foreach_get("center", centers)
    used = np.zeros(len(me.vertices), dtype=bool)
    used[edges.ravel()] = True
    _view.update(key=key, checked=now, co=co, world=_apply(mw, co.astype(np.float64)), edges=edges,
                 border=counts == 1, tris=tris.reshape(-1, 3), tri_face=tri_face,
                 centers=_apply(mw, centers.reshape(-1, 3)), loose=np.nonzero(~used)[0], batches=None)
    return _view


def hover(context, settings, v, surf, tips, poses, scale):
    """What each tip would act on: {"hit": (point, normal) of the main tip on the surface or None,
    "vert": the vertex the main tip snaps to, "edge": the border edge it would carry on from (an index
    into the edges), "faces": the faces the off hand would delete}."""
    off_tip, main_tip = tips
    key = (tuple(main_tip), tuple(off_tip), settings.retopo_tool, settings.retopo_size, _view["key"], id(surf))
    now = time.monotonic()
    if key == _hover["key"] and now - _hover["time"] < HOVER_SECONDS:
        return _hover["value"]
    hit = surf.locate(main_tip, poses[1].translation, REACH * scale) if surf is not None else None
    vert = edge = None
    faces = np.zeros(0, dtype=np.int64)
    if v is not None and hit is not None and settings.retopo_tool in {'STRIP', 'QUAD'}:
        reach = snap_reach(settings, scale)
        p = np.array(hit[0])
        world = v["world"]
        if settings.retopo_tool == 'QUAD' and len(world):
            d = np.linalg.norm(world - p, axis=1)
            i = int(np.argmin(d))
            if d[i] <= reach:
                vert = i
        border = np.nonzero(v["border"])[0]
        if vert is None and len(border):
            e = v["edges"][border]
            a, b = world[e[:, 0]], world[e[:, 1]]
            ab = b - a
            t = np.clip(np.einsum("ij,ij->i", p - a, ab) / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-18), 0.0, 1.0)
            d = np.linalg.norm(a + ab * t[:, None] - p, axis=1)
            j = int(np.argmin(d))
            if d[j] <= reach:
                edge = int(border[j])
    if v is not None and _deleting[0] is None and len(v["centers"]):
        reach = delete_reach(settings, scale)
        found = set()
        points = [Vector(off_tip)]
        if settings.retopo_mirror:
            obj = result(context)
            if obj is not None:
                points.append(Frame(obj, reference(context)).mirrored(Vector(off_tip)))
        for q in points:
            d = np.linalg.norm(v["centers"] - np.array(q), axis=1)
            found.update(np.nonzero(d <= reach)[0].tolist())
        faces = np.array(sorted(found), dtype=np.int64)
    value = {"hit": hit, "vert": vert, "edge": edge, "faces": faces}
    _hover.update(time=now, key=key, value=value)
    return value


# --- drawing -----------------------------------------------------------------------

_shader = {}


def shader():
    """A uniform color, each vertex pulled toward the eye (view space) by `pull` of its distance, and a
    point size for POINTS."""
    s = _shader.get("retopo")
    if s is None:
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "modelViewMatrix")
        info.push_constant('MAT4', "projectionMatrix")
        info.push_constant('VEC4', "color")
        info.push_constant('FLOAT', "pull")
        info.push_constant('FLOAT', "size")
        info.vertex_in(0, 'VEC3', "pos")
        info.fragment_out(0, 'VEC4', "fragColor")
        info.vertex_source(
            "void main() {"
            "  vec4 v = modelViewMatrix * vec4(pos, 1.0);"
            "  v.xyz *= 1.0 - pull;"
            "  gl_Position = projectionMatrix * v;"
            "  gl_PointSize = size;"
            "}")
        info.fragment_source("void main() { fragColor = color; }")
        s = _shader["retopo"] = gpu.shader.create_from_info(info)
    return s


def _batches(v):
    """GPU batches of the drawn mesh (local positions), made once per change."""
    if v["batches"] is None:
        s = shader()
        co = v["co"]
        edges = v["edges"]
        out = {}
        if len(v["tris"]):
            out["faces"] = batch_for_shader(s, 'TRIS', {"pos": co}, indices=v["tris"])
        inner, border = edges[~v["border"]], edges[v["border"]]
        if len(inner):
            out["edges"] = batch_for_shader(s, 'LINES', {"pos": co}, indices=inner)
        if len(border):
            out["border"] = batch_for_shader(s, 'LINES', {"pos": co}, indices=border)
        if len(co):
            out["verts"] = batch_for_shader(s, 'POINTS', {"pos": co})
        if len(v["loose"]):
            out["loose"] = batch_for_shader(s, 'POINTS', {"pos": co[v["loose"]]})
        v["batches"] = out
    return v["batches"]


def _use(s, color, pull=0.0, size=1.0):
    s.uniform_float("color", color)
    s.uniform_float("pull", pull)
    s.uniform_float("size", size)


def _draw_mesh(obj, v, settings):
    batches = _batches(v)
    if not batches:
        return
    s = shader()
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(obj.matrix_world)
    s.bind()
    s.uniform_float("modelViewMatrix", gpu.matrix.get_model_view_matrix())
    s.uniform_float("projectionMatrix", gpu.matrix.get_projection_matrix())
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_mask_set(False)
    if "faces" in batches and settings.retopo_opacity > 0.005:
        gpu.state.depth_test_set('LESS_EQUAL')
        _use(s, (*FACE_COLOR, settings.retopo_opacity), FACE_PULL)
        batches["faces"].draw(s)
    # Edges hidden behind the reference, faintly; then the ones in sight.
    gpu.state.depth_test_set('NONE')
    for name, color in (("edges", EDGE_COLOR), ("border", BORDER_COLOR)):
        if name in batches:
            _use(s, (*color, 0.1))
            batches[name].draw(s)
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.line_width_set(2.0)
    for name, color in (("edges", EDGE_COLOR), ("border", BORDER_COLOR)):
        if name in batches:
            _use(s, (*color, 0.95), EDGE_PULL)
            batches[name].draw(s)
    gpu.state.line_width_set(1.0)
    gpu.state.program_point_size_set(True)
    if "verts" in batches:
        _use(s, (*VERT_COLOR, 0.9), VERT_PULL, 4.0)
        batches["verts"].draw(s)
    if "loose" in batches:
        _use(s, (*VERT_COLOR, 1.0), VERT_PULL, 8.0)
        batches["loose"].draw(s)
    gpu.state.program_point_size_set(False)
    gpu.state.depth_test_set('NONE')
    gpu.state.depth_mask_set(True)
    gpu.matrix.pop()


def _draw_faces(v, faces, color):
    """Some faces of the drawn mesh (world), in color, on the surface."""
    if not len(faces) or not len(v["tris"]):
        return
    tris = v["tris"][np.isin(v["tri_face"], faces)]
    if not len(tris):
        return
    s = shader()
    s.bind()
    s.uniform_float("modelViewMatrix", gpu.matrix.get_model_view_matrix())
    s.uniform_float("projectionMatrix", gpu.matrix.get_projection_matrix())
    _use(s, color, EDGE_PULL)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    batch_for_shader(s, 'TRIS', {"pos": v["world"][tris.ravel()].astype(np.float32)}).draw(s)
    gpu.state.depth_mask_set(True)
    gpu.state.depth_test_set('NONE')


def _ring(point, normal, radius, n=32):
    rot = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
    m = Matrix.Translation(point) @ rot @ Matrix.Scale(radius, 4)
    ring = [m @ Vector((math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n), 0.0)) for i in range(n)]
    return [p for i in range(n) for p in (ring[i], ring[(i + 1) % n])]


def _mirror_plane(ref):
    """A light grid on the reference's local x = 0, over its bounds."""
    corners = [Vector(c) for c in ref.bound_box]
    lo = Vector((min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.y for c in corners), max(c.z for c in corners)))
    pad = (hi - lo) * 0.1
    lo, hi = lo - pad, hi + pad
    mw = ref.matrix_world
    coords = []
    for i in range(5):
        f = i / 4
        y = lo.x + (hi.x - lo.x) * f
        z = lo.y + (hi.y - lo.y) * f
        coords += [mw @ Vector((0.0, y, lo.y)), mw @ Vector((0.0, y, hi.y)),
                   mw @ Vector((0.0, lo.x, z)), mw @ Vector((0.0, hi.x, z))]
    return coords


def draw(context, tips, poses, scale):
    """Retopo mode in the headset: the retopo mesh over the reference, where the main tip lands and what
    it snaps to, the open quad, what the off hand would delete and the mirror plane. tips, poses: the
    (off hand's, main hand's) tips and aim poses."""
    settings = context.scene.vr_sculpt
    ref = reference(context)
    if ref is None:
        return
    obj = result(context)
    v = view(obj) if obj is not None and obj.visible_get() else None
    if v is not None:
        _draw_mesh(obj, v, settings)
    surf = surface(context, ref)
    h = hover(context, settings, v, surf, tips, poses, scale)
    deleting = _deleting[0]
    if v is not None:
        if deleting is not None and deleting.name == obj.name:
            _draw_faces(v, np.array(sorted(deleting.faces), dtype=np.int64), (*DELETE_COLOR, 0.55))
        else:
            _draw_faces(v, h["faces"], (*DELETE_COLOR, 0.25))
    s = shader()
    s.bind()
    s.uniform_float("modelViewMatrix", gpu.matrix.get_model_view_matrix())
    s.uniform_float("projectionMatrix", gpu.matrix.get_projection_matrix())
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    groups = []  # (primitive, coords, color, size)
    color = TOOL_COLORS[settings.retopo_tool]
    hit = h["hit"]
    if hit is not None:
        point, normal = hit
        lift = point + normal * (0.0005 * scale)
        groups.append(('LINES', _ring(lift, normal, ring_radius(settings) * scale), (*color, 0.9), 1.0))
        if (Vector(tips[1]) - point).length > 0.002 * scale:
            groups.append(('LINES', [Vector(tips[1]), point], (*color, 0.5), 1.0))
    if v is not None:
        world = v["world"]
        if h["vert"] is not None:
            groups.append(('POINTS', [Vector(world[h["vert"]])], (*HOVER_COLOR, 1.0), 12.0))
        if h["edge"] is not None:
            a, b = v["edges"][h["edge"]]
            groups.append(('LINES', [Vector(world[a]), Vector(world[b])], (*HOVER_COLOR, 1.0), 3.0))
        pen = [Vector(world[i]) for i, _co in _state["pen"] if _state["pen_key"] == obj.name and i < len(world)]
        if pen and settings.retopo_tool == 'QUAD':
            band = [p for i in range(len(pen) - 1) for p in (pen[i], pen[i + 1])]
            if hit is not None:
                band += [pen[-1], hit[0]]
            groups.append(('LINES', band, (*TOOL_COLORS['QUAD'], 0.95), 2.0))
            if hit is not None and len(pen) > 1:
                groups.append(('LINES', [hit[0], pen[0]], (*TOOL_COLORS['QUAD'], 0.4), 1.0))
            groups.append(('POINTS', pen, (*TOOL_COLORS['QUAD'], 1.0), 10.0))
    if settings.retopo_mirror:
        groups.append(('LINES', _mirror_plane(ref), (*MIRROR_COLOR, 0.18), 1.0))
    gpu.state.program_point_size_set(True)
    for prim, coords, col, size in groups:
        if prim == 'LINES':
            gpu.state.line_width_set(size)
        _use(s, col, 0.0, size)
        batch_for_shader(s, prim, {"pos": [tuple(c) for c in coords]}).draw(s)
    gpu.state.line_width_set(1.0)
    gpu.state.program_point_size_set(False)
    gpu.state.blend_set('NONE')


# --- desktop -------------------------------------------------------------------------

def desktop_panel(layout, col, context):
    """Retopo's part of the VR Authoring panel."""
    from . import actions
    settings = context.scene.vr_sculpt
    obj = result(context)
    col.prop(settings, "retopo_reference", text="Reference")
    col.prop(settings, "retopo_object", text="Retopo Mesh")
    col.label(text=f"{obj.name}: {len(obj.data.polygons)} faces" if obj else "The next stroke starts a new mesh",
              icon='MESH_GRID')
    col.prop(settings, "retopo_tool", expand=True)
    col.prop(settings, "retopo_size")
    col.prop(settings, "retopo_mirror")
    col.prop(settings, "retopo_opacity")
    col.prop(settings, "retopo_relax")
    col.prop(settings, "tip_offset")
    box = layout.box()
    box.scale_y = 0.8
    box.label(text=actions.last_status)


def register():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    forget_caches()
