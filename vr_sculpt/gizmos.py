"""Gizmos on the selected object (Model mode): grab a handle with the trigger and pull.

- A handle on each face resizes the object from that side (the opposite face stays put).
- A bevel bead on the top edge nearest you: pull it in along the corner to round the edges.
- An arrow out of each side: drag it out and copies follow it (a linear array along that axis).
  A bead on the first copy sets the gap: the copy follows your hand.
- Radial arrays get a ring: drag its bead around (closer to the original = more copies), and the
  center cross moves the ring's center.
- The mirror center, while the object mirrors, is a diamond you can move.

Handles show when a tip comes near the object and hide otherwise, so the scene stays clean. Each
drag is a trigger handler (update / finish, `changed` for the undo step), like interactions.py.
"""

import math
import time

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import grid, hardsurface, hud, primitives, shapes, symmetry

REACH = 0.02  # hand-space meters from a handle that still grab it
NEAR = 0.2  # hand-space meters: gizmos show when a tip is this close to the object's box
ARROW_OUT = 0.05  # hand-space meters from a face to its array arrow
HANDLE_SIZE = 0.0055  # hand-space meters
MIN_SIZE = 0.002  # world meters, the smallest a drag makes a side
MAX_COUNT = 24
AXIS_COLORS = ((1.0, 0.36, 0.38), (0.5, 0.9, 0.35), (0.35, 0.6, 1.0))
FACE_COLOR = (0.95, 0.96, 1.0)
BEVEL_COLOR = (1.0, 0.72, 0.3)
RADIAL_COLOR = (0.8, 0.6, 1.0)
HOT_COLOR = (1.0, 0.85, 0.3)


class Handle:
    """kind: 'face', 'bevel', 'array', 'gap', 'radial', 'center' or 'mirror'. direction: the world
    direction a drag along it changes things (faces and arrows: outward)."""

    def __init__(self, id, kind, position, direction=None, axis=0, sign=1):
        self.id, self.kind, self.position = id, kind, Vector(position)
        self.direction = direction
        self.axis, self.sign = axis, sign


# --- the object's own box (without modifiers: an array or mirror would inflate it) ----------

_bounds = {}


def local_box(obj):
    """(lo, hi) of the object's base shape in object space."""
    prim = obj.vr_prim
    if prim.kind == 'CUBE':
        h = Vector(prim.size) / 2
        return -h, h.copy()
    if prim.kind == 'CYLINDER':
        r, z = prim.radius, prim.height / 2
        return Vector((-r, -r, -z)), Vector((r, r, z))
    me = obj.data
    key = (me.as_pointer(), len(me.vertices))
    hit = _bounds.get(obj.name)
    now = time.monotonic()
    if hit is None or hit[0] != key or now - hit[1] > 1.0:
        from . import generic
        lo, hi = generic.mesh_bounds(me)
        hit = _bounds[obj.name] = (key, now, lo, hi)
    return hit[2].copy(), hit[3].copy()


def forget_caches():
    _bounds.clear()


def target(context):
    """The object the gizmos are on: the selected, visible mesh in Model mode."""
    settings = context.scene.vr_sculpt
    if not settings.show_panel or settings.mode != 'MODEL' or settings.model_mark != 'NONE':
        return None
    obj = context.view_layer.objects.active
    if obj is None or obj.type != 'MESH' or not obj.select_get() or not obj.visible_get():
        return None
    return obj


def _axis_dir(obj, axis):
    d = obj.matrix_world.to_3x3() @ Vector([1.0 if i == axis else 0.0 for i in range(3)])
    return d.normalized() if d.length > 1e-12 else Vector((0.0, 0.0, 1.0))


def _axis_scale(obj, axis):
    return max(obj.matrix_world.to_3x3().col[axis].length, 1e-12)


def world_size(obj, axis):
    lo, hi = local_box(obj)
    return (hi[axis] - lo[axis]) * _axis_scale(obj, axis)


def _face(obj, lo, hi, axis, sign):
    c = (lo + hi) / 2
    c[axis] = hi[axis] if sign > 0 else lo[axis]
    return obj.matrix_world @ c


def step(obj):
    """World distance between linear array copies."""
    axis = hardsurface.array_axis(obj)
    return world_size(obj, axis) * (1.0 + hardsurface.array_gap(obj))


def _bevel_edge(obj, lo, hi, viewer):
    """The top edge nearest the viewer: (edge midpoint, inward diagonal, side normal, top normal), world."""
    c = (lo + hi) / 2
    best = None
    top = _axis_dir(obj, 2)
    for axis in (0, 1):
        for sign in (-1, 1):
            local = c.copy()
            local[axis] = hi[axis] if sign > 0 else lo[axis]
            local.z = hi.z
            p = obj.matrix_world @ local
            d = (p - Vector(viewer)).length
            if best is None or d < best[0]:
                side = _axis_dir(obj, axis) * sign
                best = (d, p, (-side - top).normalized(), side, top)
    return best[1:]


def bevel_handle_position(obj, viewer):
    lo, hi = local_box(obj)
    p, inward, _side, _top = _bevel_edge(obj, lo, hi, viewer)
    return p + inward * hardsurface.bevel_width(obj) * math.sqrt(2.0)


def radial_center(obj):
    c = hardsurface._array_center(obj)
    return obj.matrix_world @ c if c is not None else None


def _turn(obj, angle):
    """World rotation about the radial axis (through the center)."""
    axis = _axis_dir(obj, hardsurface.array_axis(obj))
    return Quaternion(axis, angle)


def handles(context, obj, viewer, scale):
    """Every handle on obj, in world space."""
    settings = context.scene.vr_sculpt
    lo, hi = local_box(obj)
    if (hi - lo).length < 1e-9:
        return []
    out = []
    cutting = settings.model_add_as != 'OBJECT' and not hardsurface.is_cutter(obj)
    if not cutting:  # A press on the object makes a cutter there, so faces stay free.
        for axis in range(3):
            for sign in (-1, 1):
                out.append(Handle(f"face_{'XYZ'[axis]}{'+' if sign > 0 else '-'}", 'face',
                                  _face(obj, lo, hi, axis, sign), _axis_dir(obj, axis) * sign, axis, sign))
    if not hardsurface.is_cutter(obj):
        p, inward, _side, _top = _bevel_edge(obj, lo, hi, viewer)
        out.append(Handle("bevel", 'bevel', p + inward * hardsurface.bevel_width(obj) * math.sqrt(2.0), inward))
    count = hardsurface.array_count(obj)
    radial = count > 1 and hardsurface.array_mode(obj) == 'RADIAL'
    current = hardsurface.array_axis(obj) if count > 1 else None
    if not radial:
        for axis in range(3):
            d = _axis_dir(obj, axis)
            t = ARROW_OUT * scale + (step(obj) * (count - 1) if axis == current else 0.0)
            out.append(Handle("array_" + "XYZ"[axis], 'array', _face(obj, lo, hi, axis, 1) + d * t, d, axis))
        if count > 1:
            d = _axis_dir(obj, current)
            out.append(Handle("array_gap", 'gap', obj.matrix_world @ ((lo + hi) / 2) + d * step(obj), d, current))
    else:
        center = radial_center(obj)
        if center is not None:
            here = obj.matrix_world @ ((lo + hi) / 2)
            bead = center + _turn(obj, 2 * math.pi / count) @ (here - center)
            out.append(Handle("radial_count", 'radial', bead, axis=current))
            out.append(Handle("radial_center", 'center', center, axis=current))
    if symmetry.model_bits(obj):
        mod = obj.modifiers.get(symmetry.MODIFIER)
        if mod is not None and mod.mirror_object is not None:
            out.append(Handle("mirror_center", 'mirror', mod.mirror_object.matrix_world.translation))
    return out


def near(obj, point, scale, reach=NEAR):
    """Whether point is within reach of obj's box, or of what its modifiers make (array copies...)."""
    p = obj.matrix_world.inverted_safe() @ Vector(point)
    s = max(obj.matrix_world.to_scale())
    m = reach * scale / max(s, 1e-9)
    lo, hi = local_box(obj)
    corners = [Vector(c) for c in obj.bound_box]
    for a, b in ((lo, hi), (Vector([min(c[i] for c in corners) for i in range(3)]),
                            Vector([max(c[i] for c in corners) for i in range(3)]))):
        if all(a[i] - m <= p[i] <= b[i] + m for i in range(3)):
            return True
    return False


FREE = {'center', 'mirror'}  # handles that can be away from the object (they show when a tip nears them)


def handle_at(context, ss, tip, tips=None):
    """(object, handle) a tip grabs, or None. Handles only work while they show (a tip near)."""
    obj = target(context)
    if obj is None:
        return None
    scale = ss.viewer_scale
    close_by = near(obj, tip, scale)
    best = None
    for h in handles(context, obj, Vector(ss.viewer_pose_location), scale):
        if not close_by and h.kind not in FREE:
            continue
        d = (h.position - Vector(tip)).length
        if d <= REACH * scale and (best is None or d < best[0]):
            best = (d, h)
    return (obj, best[1]) if best else None


# --- drags ----------------------------------------------------------------------------

def _haptic(context, hand, amplitude=0.35):
    if hand is None:
        return
    from . import operators
    operators.haptic(context, hand, amplitude, 0.012)


class _Drag:
    def __init__(self, context, obj, handle, tip, scale, hand=None):
        self.obj = obj
        self.handle = handle
        self.tip0 = Vector(tip)
        self.scale = scale
        self.hand = hand
        self.changed = False
        self.label = ""  # what it's set to, drawn next to the handle
        self.position = Vector(handle.position)  # where the handle is drawn while it's dragged

    def along(self, tip, direction):
        return (Vector(tip) - self.tip0).dot(direction)

    def finish(self, context):
        context.view_layer.update()


def _meters(v):
    return f"{v * 100:.1f} cm" if abs(v) < 1.0 else f"{v:.2f} m"


class FaceDrag(_Drag):
    """Pull a face out or push it in; the opposite face stays where it is."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        prim = obj.vr_prim
        self.kind = prim.kind
        self.size0 = Vector(prim.size)
        self.radius0, self.height0 = prim.radius, prim.height
        self.mw0 = obj.matrix_world.copy()
        self.lo, self.hi = local_box(obj)
        self.extent0 = world_size(obj, handle.axis)
        self.label = _meters(self.extent0)

    def update(self, context, pose, tip_pose, value):
        settings = context.scene.vr_sculpt
        h, obj, axis = self.handle, self.obj, self.handle.axis
        extent = max(self.extent0 + self.along(tip_pose.translation, h.direction), MIN_SIZE)
        if settings.grid_snap:
            extent = max(round(extent / settings.grid_size) * settings.grid_size, settings.grid_size)
        k = extent / max(self.extent0, 1e-12)
        if self.kind == 'CYLINDER' and axis < 2:
            obj.vr_prim.radius = self.radius0 * k
        elif self.kind == 'CYLINDER':
            obj.vr_prim.height = self.height0 * k
        elif self.kind == 'CUBE':
            size = self.size0.copy()
            size[axis] *= k
            obj.vr_prim.size = size
        # Keep the opposite face where it was: scale about it (object scale for other meshes).
        fixed = self.lo[axis] if h.sign > 0 else self.hi[axis]
        e = Vector([1.0 if i == axis else 0.0 for i in range(3)])
        if self.kind in {'CUBE', 'CYLINDER'}:
            center = (self.lo[axis] + self.hi[axis]) / 2
            shift = (center - fixed) * (k - 1.0)
            obj.matrix_world = self.mw0 @ Matrix.Translation(e * shift)
        else:
            s = Matrix.Diagonal([k if i == axis else 1.0 for i in range(3)] + [1.0])
            obj.matrix_world = self.mw0 @ Matrix.Translation(e * fixed) @ s @ Matrix.Translation(-e * fixed)
        self.label = _meters(extent)
        self.position = _face(obj, *local_box(obj), axis, h.sign)
        self.changed = True


class BevelDrag(_Drag):
    """Pull the bead in along the corner: the bevel width follows (back out to the edge: no bevel)."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        self.width0 = hardsurface.bevel_width(obj)
        dims = [d for d in obj.dimensions if d > 1e-6]
        self.max = max(0.5 * min(dims), 0.001) if dims else 0.05
        self.label = _meters(self.width0) if self.width0 else "no bevel"

    def update(self, context, pose, tip_pose, value):
        width = min(max(self.width0 + self.along(tip_pose.translation, self.handle.direction) / math.sqrt(2.0),
                        0.0), self.max)
        obj = self.obj
        if width < 0.0004:
            if hardsurface.has_bevel(obj):
                hardsurface.set_bevel(obj, False)
                self.changed = True
            self.label = "no bevel"
        else:
            if not hardsurface.has_bevel(obj):
                hardsurface.set_bevel(obj, True)
                _haptic(context, self.hand)
            hardsurface.set_bevel_width(obj, width)
            self.changed = True
            self.label = "bevel " + _meters(width)
        self.position = Vector(self.handle.position) + self.handle.direction * (width - self.width0) * math.sqrt(2.0)


class ArrayDrag(_Drag):
    """Drag an arrow out of a side: a copy appears each time it passes one more step."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        count = hardsurface.array_count(obj)
        self.on_axis = count > 1 and hardsurface.array_axis(obj) == handle.axis
        self.count0 = count if self.on_axis else 1
        self.t0 = ARROW_OUT * scale
        if self.on_axis:
            self.t0 += step(obj) * (count - 1)
        self.face = _face(obj, *local_box(obj), handle.axis, 1)
        self.label = f"× {self.count0}"

    def update(self, context, pose, tip_pose, value):
        obj, axis = self.obj, self.handle.axis
        t = self.t0 + self.along(tip_pose.translation, self.handle.direction)
        if not self.on_axis and t > self.t0 + 0.01 * self.scale:
            # The first pull on another axis: the array goes there (and becomes linear).
            if hardsurface.array_mode(obj) != 'LINEAR':
                hardsurface.set_array_mode(context, obj, 'LINEAR')
            hardsurface.set_array_axis(context, obj, axis)
            self.on_axis = True
            self.changed = True
        if self.on_axis:
            count = int(min(max(1 + round((t - ARROW_OUT * self.scale) / max(step(obj), 1e-9)), 1), MAX_COUNT))
            if count != hardsurface.array_count(obj):
                hardsurface.set_array_count(context, obj, count)
                _haptic(context, self.hand)
                self.changed = True
            self.label = f"× {count}" if count > 1 else "no array"
        self.position = self.face + self.handle.direction * max(t, 0.0)


class GapDrag(_Drag):
    """The bead on the first copy: the copy follows your hand along the axis, which sets the gap."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        self.step0 = step(obj)
        self.size = max(world_size(obj, handle.axis), 1e-9)
        self.label = f"gap {hardsurface.array_gap(obj) * 100:.0f}%"

    def update(self, context, pose, tip_pose, value):
        gap = (self.step0 + self.along(tip_pose.translation, self.handle.direction)) / self.size - 1.0
        gap = min(max(gap, -0.9), 4.0)
        if abs(gap - hardsurface.array_gap(self.obj)) > 1e-6:
            hardsurface.set_array_gap(context, self.obj, gap)
            self.changed = True
        self.label = f"gap {gap * 100:.0f}%"
        self.position = Vector(self.handle.position) + self.handle.direction * (step(self.obj) - self.step0)


class RadialDrag(_Drag):
    """Drag the ring's bead around: where it is, the next copy goes, so closer = more copies."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        self.center = radial_center(obj)
        self.axis = _axis_dir(obj, hardsurface.array_axis(obj))
        lo, hi = local_box(obj)
        self.here = obj.matrix_world @ ((lo + hi) / 2)
        self.label = f"× {hardsurface.array_count(obj)}"

    def _flat(self, p):
        v = Vector(p) - self.center
        return v - self.axis * v.dot(self.axis)

    def update(self, context, pose, tip_pose, value):
        a, b = self._flat(self.here), self._flat(tip_pose.translation)
        if a.length < 1e-9 or b.length < 1e-9:
            return
        angle = a.angle(b)
        count = int(min(max(round(2 * math.pi / max(angle, 1e-3)), 2), MAX_COUNT))
        if count != hardsurface.array_count(self.obj):
            hardsurface.set_array_count(context, self.obj, count)
            _haptic(context, self.hand)
            self.changed = True
        self.label = f"× {count}"
        sign = 1.0 if a.cross(b).dot(self.axis) >= 0 else -1.0
        self.position = self.center + Quaternion(self.axis, sign * angle) @ (self.here - self.center)


class CenterDrag(_Drag):
    """Move the radial array's center."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        self.start = Vector(handle.position)
        self.label = "center"

    def update(self, context, pose, tip_pose, value):
        p = grid.snap_if(context.scene.vr_sculpt, self.start + (Vector(tip_pose.translation) - self.tip0))
        hardsurface.set_array_center(context, self.obj, p)
        self.position = p
        self.changed = True


class MirrorDrag(_Drag):
    """Move the mirror center (the 3D cursor and the VR Mirror Center empty)."""

    def __init__(self, context, obj, handle, tip, scale, hand=None):
        super().__init__(context, obj, handle, tip, scale, hand)
        self.start = Vector(handle.position)
        self.label = "mirror center"

    def update(self, context, pose, tip_pose, value):
        p = grid.snap_if(context.scene.vr_sculpt, self.start + (Vector(tip_pose.translation) - self.tip0))
        symmetry.move_center(context, p)
        self.position = p
        self.changed = True


DRAGS = {'face': FaceDrag, 'bevel': BevelDrag, 'array': ArrayDrag, 'gap': GapDrag, 'radial': RadialDrag,
         'center': CenterDrag, 'mirror': MirrorDrag}


def start_drag(context, obj, handle, tip, scale, hand=None):
    return DRAGS[handle.kind](context, obj, handle, tip, scale, hand)


# --- drawing -------------------------------------------------------------------------

_meshes = {}


def _mesh(name):
    if name not in _meshes:
        S = shapes
        _meshes[name] = {
            "face": [S.Part(S.box(1.0, 1.0, 0.35), None)],
            "bead": [S.Part(S.sphere(6, 12), None)],
            "diamond": [S.Part(S.transformed(S.octahedron(2.0, 0.7, 0.5), Matrix.Translation((0, 0, -1))), None)],
            "cone": [S.Part(S.cylinder(0.55, 1.6, 12, r_top=0.0), None)],
        }[name]
    return _meshes[name]


def _oriented(position, direction, size):
    rot = Vector(direction).to_track_quat('Z', 'Y') if direction is not None and direction.length > 1e-9 \
        else Quaternion()
    return shapes.matrix_at(position, rot, size)


def _lines(coords, color):
    if not coords:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _ring(center, axis, through, segments=48):
    v = through - center
    pts = [center + Quaternion(axis, 2 * math.pi * i / segments) @ v for i in range(segments + 1)]
    return [p for i in range(segments) for p in (pts[i], pts[i + 1])]


def draw(context, ss, tips, hot=None, drag=None):
    """The handles on the target object. hot: id of a handle a tip is on; drag: the running drag."""
    obj = drag.obj if drag is not None else target(context)
    if obj is None:
        return
    scale = ss.viewer_scale
    viewer = Vector(ss.viewer_pose_location)
    hs = handles(context, obj, viewer, scale)
    if drag is None and not any(near(obj, t, scale) for t in tips):
        # Only the free handles a tip comes near (a mirror center away from the object).
        hs = [h for h in hs if h.kind in FREE and any((h.position - t).length < NEAR * scale for t in tips)]
        if not hs:
            return
    lo, hi = local_box(obj)
    here = obj.matrix_world @ ((lo + hi) / 2)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    coords_axis = {0: [], 1: [], 2: []}
    coords_other = []
    for h in hs:
        pos = drag.position if drag is not None and drag.handle.id == h.id else h.position
        if h.kind == 'array':
            face = _face(obj, lo, hi, h.axis, 1)
            coords_axis[h.axis] += [face, pos]
        elif h.kind == 'bevel':
            # The bevel's profile: a quarter arc across the corner (a dot at the edge with no bevel).
            edge, _inward, side, top = _bevel_edge(obj, lo, hi, viewer)
            w = (pos - edge).length / math.sqrt(2.0)
            if w > 1e-5:
                c = edge - (side + top) * w
                arc = [c + (side * math.cos(t) + top * math.sin(t)) * w
                       for t in (math.pi / 2 * i / 10 for i in range(11))]
                coords_other += [p for i in range(10) for p in (arc[i], arc[i + 1])]
    for axis, coords in coords_axis.items():
        _lines(coords, (*AXIS_COLORS[axis], 0.55))
    count = hardsurface.array_count(obj)
    if count > 1 and hardsurface.array_mode(obj) == 'RADIAL':
        center = radial_center(obj)
        if center is not None:
            axis = _axis_dir(obj, hardsurface.array_axis(obj))
            _lines(_ring(center, axis, here), (*RADIAL_COLOR, 0.7))
            coords_other += [center, here]
    _lines(coords_other, (1.0, 1.0, 1.0, 0.35))
    gpu.state.blend_set('NONE')

    shapes.begin_solid()
    gpu.state.depth_test_set('NONE')  # Handles stay visible through the object they're on.
    if count > 1 and hardsurface.array_mode(obj) == 'RADIAL':
        center = radial_center(obj)
        if center is not None:
            for i in range(1, count):
                p = center + _turn(obj, 2 * math.pi * i / count) @ (here - center)
                shapes.draw_shape("g_bead", _mesh("bead"), shapes.matrix_at(p, Quaternion(), 0.0025 * scale),
                                  shapes.light_from(viewer, p), 0.6, 0.0, RADIAL_COLOR)
    for h in hs:
        dragged = drag is not None and drag.handle.id == h.id
        if drag is not None and not dragged:
            continue  # While dragging, only the dragged handle shows.
        pos = drag.position if dragged else h.position
        glow = 0.45 if dragged or h.id == hot else 0.0
        size = HANDLE_SIZE * scale * (1.35 if glow else 1.0)
        light = shapes.light_from(viewer, pos)
        if h.kind == 'face':
            color = HOT_COLOR if glow else FACE_COLOR
            shapes.draw_shape("g_face", _mesh("face"), _oriented(pos, h.direction, size), light, 0.9, glow, color)
        elif h.kind == 'array':
            shapes.draw_shape("g_cone", _mesh("cone"), _oriented(pos, h.direction, size * 1.1), light, 0.95, glow,
                              AXIS_COLORS[h.axis])
        elif h.kind == 'gap':
            shapes.draw_shape("g_diamond", _mesh("diamond"), _oriented(pos, h.direction, size * 0.8), light, 0.95,
                              glow, AXIS_COLORS[h.axis])
        elif h.kind == 'bevel':
            shapes.draw_shape("g_bead", _mesh("bead"), shapes.matrix_at(pos, Quaternion(), size * 0.85), light, 0.95,
                              glow, BEVEL_COLOR)
        elif h.kind == 'radial':
            shapes.draw_shape("g_bead", _mesh("bead"), shapes.matrix_at(pos, Quaternion(), size), light, 0.95, glow,
                              RADIAL_COLOR)
        elif h.kind in {'center', 'mirror'}:
            color = RADIAL_COLOR if h.kind == 'center' else (0.85, 0.9, 1.0)
            shapes.draw_shape("g_diamond", _mesh("diamond"), shapes.matrix_at(pos, Quaternion(), size * 0.9), light,
                              0.95, glow, color)
    shapes.end_solid()
    if drag is not None and drag.label:
        from . import chips
        m = hud.billboard(drag.position + Vector((0.0, 0.0, 0.018 * scale)), viewer, scale)
        chips.draw_label(m, 0.0, 0.0, drag.label, 0.009, (1.0, 1.0, 1.0, 1.0))


def clear():
    _meshes.clear()
    _bounds.clear()
