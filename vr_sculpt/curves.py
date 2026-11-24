"""Bezier curves: Draw mode's Curve tool and curve editing with the Edit tool.

Curve tool (pen style): each press adds a point at the tip to the open curve; dragging while the
trigger is held pulls its handles out (the outgoing handle follows the tip, the other mirrors it).
Pressing on the first point closes the curve, on the last point (or B) finishes it. A new curve is
a Blender curve object (bevelled to the Width, in the chosen color) in the "VR Strokes" collection.

Edit tool, on any visible Bezier curve in Object Mode (ours or not): grab a point (its handles come
along and turn with the wrist) or a handle (an aligned pair stays aligned); a press on the curve
between points inserts a point there without changing the shape, then drags it. A click on a point
(press and let go without moving) switches it between smooth and corner. The off hand deletes the
point it presses.

Handlers follow interactions.py: update(context, aim_pose, tip_pose, value), finish(context),
`changed`. Blender's RNA can't remove a Bezier point, so inserting and deleting rebuild the spline.
"""

import math

import bpy
import numpy as np
from mathutils import Vector

from . import strokes

PROP = "vrs_curve"
POINT_REACH = 0.02  # hand-space meters: a point or handle this close to the tip is grabbed
SEGMENT_REACH = 0.015  # hand-space meters (beyond the bevel): a press this close to the curve inserts a point
DRAG_START = 0.008  # hand-space meters: moving this far from a new point's press pulls its handles out
CLICK_MOVE = 0.004  # hand-space meters: an Edit press that moves less is a click (corner / smooth)
CLICK_TURN = math.radians(8.0)  # ... and turns the wrist less than this
SAMPLES = 16  # points per segment, for hit tests, erasing and drawing
RESOLUTION_U = 16
BEVEL_RESOLUTION = 4
MAX_DRAWN_POINTS = 4000  # control points drawn in the headset, over every curve

_state = {"active": None}  # Name of the curve being built (open), or None.


# --- curve objects --------------------------------------------------------------

def is_ours(obj):
    return obj is not None and obj.type == 'CURVE' and PROP in obj


def editable(obj):
    """A visible curve object with Bezier splines, in Object Mode (Edit Mode keeps its own copy)."""
    return (obj is not None and obj.type == 'CURVE' and obj.mode == 'OBJECT' and obj.visible_get()
            and any(sp.type == 'BEZIER' for sp in obj.data.splines))


def editable_curves(context):
    return [obj for obj in context.view_layer.objects if editable(obj)]


def new_curve(context, settings, origin, scale):
    cu = bpy.data.curves.new("Curve", 'CURVE')
    cu.dimensions = '3D'
    cu.resolution_u = RESOLUTION_U
    cu.bevel_resolution = BEVEL_RESOLUTION
    cu.use_fill_caps = True
    cu.materials.append(strokes.material(settings.draw_color))
    obj = bpy.data.objects.new("Curve", cu)
    strokes.collection(context).objects.link(obj)
    obj.location = origin
    obj[PROP] = 1
    style(obj, settings, scale)
    cu.splines.new('BEZIER')  # Comes with one point.
    return obj


def style(obj, settings, scale):
    """Width (the bevel) and color from the Draw settings."""
    cu = obj.data
    cu.bevel_depth = settings.draw_width * scale / 2.0 / max(max(obj.scale), 1e-9)
    mat = strokes.material(settings.draw_color)
    if len(cu.materials):
        cu.materials[0] = mat
    else:
        cu.materials.append(mat)


def restyle_active(context):
    """The Width slider and the swatches also change the curve being built."""
    obj = active(context)
    if obj is not None:
        from . import operators
        style(obj, context.scene.vr_sculpt, operators.viewer_scale(context))


def delete(obj):
    if _state["active"] == obj.name:
        _state["active"] = None
    data = obj.data
    bpy.data.objects.remove(obj)
    if data is not None and data.users == 0:
        bpy.data.curves.remove(data)


def forget():
    """Undo, redo or a file load replaced the data: let go of the open curve."""
    _state["active"] = None


# --- splines as arrays ------------------------------------------------------------

_FIELDS = ("co", "handle_left", "handle_right")


class Spline:
    """A Bezier spline's points in object space, as numpy arrays, plus its settings."""

    def __init__(self, spline=None):
        if spline is None:
            return
        pts = spline.bezier_points
        n = len(pts)
        for field in _FIELDS:
            arr = np.empty(n * 3)
            pts.foreach_get(field, arr)
            setattr(self, field, arr.reshape(n, 3))
        self.radius = np.empty(n)
        pts.foreach_get("radius", self.radius)
        self.tilt = np.empty(n)
        pts.foreach_get("tilt", self.tilt)
        self.left_type = [p.handle_left_type for p in pts]
        self.right_type = [p.handle_right_type for p in pts]
        self.cyclic = spline.use_cyclic_u
        self.settings = {k: getattr(spline, k) for k in ("resolution_u", "material_index", "use_smooth", "tilt_interpolation",
                                                          "radius_interpolation")}

    def copy(self):
        out = Spline()
        for field in _FIELDS + ("radius", "tilt"):
            setattr(out, field, getattr(self, field).copy())
        out.left_type, out.right_type = list(self.left_type), list(self.right_type)
        out.cyclic, out.settings = self.cyclic, dict(self.settings)
        return out

    def __len__(self):
        return len(self.co)

    def segments(self):
        """(start index, end index) of every segment."""
        n = len(self)
        out = [(i, i + 1) for i in range(n - 1)]
        if self.cyclic and n > 1:
            out.append((n - 1, 0))
        return out

    def insert(self, seg, t):
        """Split segment seg at t (de Casteljau): same shape, one more point. Returns its index."""
        i, j = self.segments()[seg]
        p0, p1, p2, p3 = self.co[i], self.handle_right[i], self.handle_left[j], self.co[j]
        a, b, c = p0 + (p1 - p0) * t, p1 + (p2 - p1) * t, p2 + (p3 - p2) * t
        d, e = a + (b - a) * t, b + (c - b) * t
        f = d + (e - d) * t
        straight = self.right_type[i] == 'VECTOR' and self.left_type[j] == 'VECTOR'
        # Auto handles would recompute around the new point and move: keep them where they are.
        for k in (i, j):
            if self.left_type[k] == 'AUTO' or self.right_type[k] == 'AUTO':
                self.left_type[k] = self.right_type[k] = 'ALIGNED'
        self.handle_right[i], self.handle_left[j] = a, c
        new = i + 1
        self.co = np.insert(self.co, new, f, axis=0)
        self.handle_left = np.insert(self.handle_left, new, d, axis=0)
        self.handle_right = np.insert(self.handle_right, new, e, axis=0)
        self.radius = np.insert(self.radius, new, self.radius[i] + (self.radius[j] - self.radius[i]) * t)
        self.tilt = np.insert(self.tilt, new, self.tilt[i] + (self.tilt[j] - self.tilt[i]) * t)
        kind = 'VECTOR' if straight else 'ALIGNED'
        self.left_type.insert(new, kind)
        self.right_type.insert(new, kind)
        return new

    def remove(self, index):
        for field in _FIELDS:
            setattr(self, field, np.delete(getattr(self, field), index, axis=0))
        self.radius = np.delete(self.radius, index)
        self.tilt = np.delete(self.tilt, index)
        del self.left_type[index]
        del self.right_type[index]
        if len(self) < 3:
            self.cyclic = False

    def append(self, co):
        """A new last point at co (object space), with auto handles."""
        last = len(self) - 1
        self.co = np.vstack([self.co, co])
        self.handle_left = np.vstack([self.handle_left, co])
        self.handle_right = np.vstack([self.handle_right, co])
        self.radius = np.append(self.radius, self.radius[last] if last >= 0 else 1.0)
        self.tilt = np.append(self.tilt, 0.0)
        self.left_type.append('AUTO')
        self.right_type.append('AUTO')
        return last + 1


def read(obj, index):
    return Spline(obj.data.splines[index])


def _write_point(pt, spline, i):
    pt.handle_left_type, pt.handle_right_type = spline.left_type[i], spline.right_type[i]
    pt.co = spline.co[i]
    pt.handle_left = spline.handle_left[i]
    pt.handle_right = spline.handle_right[i]
    pt.radius, pt.tilt = float(spline.radius[i]), float(spline.tilt[i])


def write(obj, index, spline, only=None):
    """Write spline back as obj's spline number index. The same point count is written in place (only:
    the indices that changed); a different count rebuilds it, as the last spline. Returns its index."""
    splines = obj.data.splines
    old = splines[index]
    if len(old.bezier_points) == len(spline):
        old.use_cyclic_u = spline.cyclic
        pts = old.bezier_points
        # Blender recomputes auto and vector handles (the neighbors' too) as a point's position is set.
        for i in (range(len(spline)) if only is None else only):
            _write_point(pts[i], spline, i)
        obj.data.update_tag()
        return index
    splines.remove(old)
    new = splines.new('BEZIER')
    if len(spline) > 1:
        new.bezier_points.add(len(spline) - 1)
    for key, value in spline.settings.items():
        try:
            setattr(new, key, value)
        except (AttributeError, TypeError):
            pass
    new.use_cyclic_u = spline.cyclic
    for i, pt in enumerate(new.bezier_points):
        _write_point(pt, spline, i)
    obj.data.update_tag()
    return len(splines) - 1


# --- sampling and hit tests ---------------------------------------------------------

def sample(spline, n=SAMPLES):
    """Points along every segment: array (segments, n + 1, 3) in object space, and the t of each."""
    segs = spline.segments()
    t = np.linspace(0.0, 1.0, n + 1)
    if not segs:
        return np.zeros((0, n + 1, 3)), t
    i = np.array([s[0] for s in segs])
    j = np.array([s[1] for s in segs])
    p0, p1, p2, p3 = (a[:, None, :] for a in (spline.co[i], spline.handle_right[i], spline.handle_left[j], spline.co[j]))
    u = (1.0 - t)[None, :, None]
    tt = t[None, :, None]
    pts = u ** 3 * p0 + 3 * u * u * tt * p1 + 3 * u * tt * tt * p2 + tt ** 3 * p3
    return pts, t


def _world(obj, arr):
    m = np.array(obj.matrix_world)
    return arr @ m[:3, :3].T + m[:3, 3]


def _bezier_splines(obj):
    return [(i, sp) for i, sp in enumerate(obj.data.splines) if sp.type == 'BEZIER' and len(sp.bezier_points)]


def bevel_radius(obj):
    from . import primitives
    return obj.data.bevel_depth * max(primitives.axis_scale(obj.matrix_world))


def nearest_point(context, point_world, reach, handles=True, objects=None):
    """The closest control point or handle within reach: (obj, spline index, point index, part) with part
    'co', 'left' or 'right'; or None. Points win over handles that are as close."""
    p = np.array(point_world[:])
    best, best_d = None, math.inf
    for obj in objects if objects is not None else editable_curves(context):
        for si, sp in _bezier_splines(obj):
            s = Spline(sp)
            parts = [("co", s.co, None)]
            if handles:
                # Corners show no handles, so they can't be grabbed either.
                smooth = np.array([not (a == 'VECTOR' and b == 'VECTOR') for a, b in zip(s.left_type, s.right_type)])
                parts += [("left", s.handle_left, smooth), ("right", s.handle_right, smooth)]
            for part, arr, mask in parts:
                d = np.linalg.norm(_world(obj, arr) - p, axis=1)
                if mask is not None:
                    d = np.where(mask, d + reach * 0.25, math.inf)  # A point wins over a handle as close.
                k = int(d.argmin())
                if d[k] < best_d:
                    best, best_d = (obj, si, k, part), float(d[k])
    return best if best is not None and best_d <= reach else None


def nearest_segment(context, point_world, reach, objects=None):
    """The closest place on a curve within reach (of its surface): (obj, spline index, segment, t, distance)
    or None."""
    p = np.array(point_world[:])
    best, best_d = None, math.inf
    for obj in objects if objects is not None else editable_curves(context):
        allow = reach + bevel_radius(obj)
        for si, sp in _bezier_splines(obj):
            s = Spline(sp)
            pts, t = sample(s)
            if not len(pts):
                continue
            d = np.linalg.norm(_world(obj, pts.reshape(-1, 3)) - p, axis=1).reshape(pts.shape[:2])
            seg, k = np.unravel_index(int(d.argmin()), d.shape)
            gap = float(d[seg, k]) - allow
            if gap <= 0.0 and gap < best_d:
                best_d = gap
                best = (obj, si, int(seg), float(t[k]), float(d[seg, k]))
    return best


def touching(context, point_world, scale):
    """Whether the Edit tool would take hold of a curve here."""
    return (nearest_point(context, point_world, POINT_REACH * scale) is not None or
            nearest_segment(context, point_world, SEGMENT_REACH * scale) is not None)


# --- the open curve ---------------------------------------------------------------

def active(context):
    """The curve being built: ours, open, still in the scene. Otherwise there's none."""
    name = _state["active"]
    if name is None:
        return None
    obj = bpy.data.objects.get(name)
    ok = (is_ours(obj) and obj.name in context.view_layer.objects and obj.mode == 'OBJECT'
          and len(obj.data.splines) == 1 and obj.data.splines[0].type == 'BEZIER'
          and not obj.data.splines[0].use_cyclic_u)
    if not ok:
        _state["active"] = None
        return None
    return obj


def end(context):
    """Finish the open curve. One with a single point is no curve: it goes. Returns whether one was open."""
    obj = active(context)
    _state["active"] = None
    if obj is None:
        return False
    if len(obj.data.splines[0].bezier_points) < 2:
        delete(obj)
    return True


def is_open(context):
    return active(context) is not None


class PlacePoint:
    """Curve tool press: a new point at the tip (a new curve if none is open); held and moved, it pulls the
    point's handles out. On the open curve's first point it closes the curve, on its last it finishes it."""

    def __init__(self, context, settings, tip_pose, scale):
        self.changed = False
        self.obj = None
        self.scale = scale
        tip = tip_pose.translation.copy()
        obj = active(context)
        if obj is not None:
            s = read(obj, 0)
            ends = _world(obj, s.co[[0, -1]])
            d_first, d_last = (float(np.linalg.norm(e - np.array(tip[:]))) for e in ends)
            reach = POINT_REACH * scale
            if len(s) >= 2 and d_first <= reach and d_first <= d_last:
                obj.data.splines[0].use_cyclic_u = True
                obj.data.update_tag()
                strokes.push(("curve_close", obj.name))
                _state["active"] = None
                self.changed = True
                return
            if d_last <= reach:
                end(context)
                self.changed = True  # It may have been deleted (a single point).
                return
        if obj is None:
            obj = new_curve(context, settings, tip, scale)
            _state["active"] = obj.name
            self.index = 0
            self.spline = read(obj, 0)
            self.spline.co[0] = self.spline.handle_left[0] = self.spline.handle_right[0] = 0.0
            self.spline.left_type[0] = self.spline.right_type[0] = 'AUTO'
            self.spline.radius[0] = 1.0
        else:
            self.spline = read(obj, 0)
            self.index = self.spline.append(np.array((obj.matrix_world.inverted_safe() @ tip)[:]))
        self.obj = obj
        self.inv = obj.matrix_world.inverted_safe()
        self.anchor = tip
        self.dragging = False
        write(obj, 0, self.spline)
        strokes.push(("curve", obj.name))
        self.changed = True

    def update(self, context, pose, tip_pose, value):
        if self.obj is None:
            return
        tip = tip_pose.translation
        if not self.dragging and (tip - self.anchor).length < DRAG_START * self.scale:
            return
        self.dragging = True
        s, i = self.spline, self.index
        co = np.array((self.inv @ self.anchor)[:])
        out = np.array((self.inv @ tip)[:])
        s.left_type[i] = s.right_type[i] = 'ALIGNED'
        s.handle_right[i] = out
        s.handle_left[i] = 2.0 * co - out
        write(self.obj, 0, s, only=[i])

    def finish(self, context):
        if self.obj is not None:
            context.view_layer.update()


def _align(spline, i, moved):
    """Keep point i's other handle in line with the one that moved (its own length), if they're paired."""
    other = "handle_left" if moved == "handle_right" else "handle_right"
    co = spline.co[i]
    d = getattr(spline, moved)[i] - co
    length = float(np.linalg.norm(d))
    if length < 1e-9:
        return
    keep = float(np.linalg.norm(getattr(spline, other)[i] - co))
    getattr(spline, other)[i] = co - d / length * keep


class GrabPoint:
    """Edit tool: move a control point (its handles ride along and turn with the wrist) or one handle."""

    def __init__(self, context, obj, spline_index, index, part, tip_pose, scale):
        self.obj, self.si, self.index, self.part = obj, spline_index, index, part
        self.scale = scale
        self.start = read(obj, spline_index)
        self.spline = self.start.copy()
        self.world = obj.matrix_world.copy()
        self.inv = self.world.inverted_safe()
        self.start_tip = tip_pose.copy()
        self.start_tip_inv = tip_pose.inverted_safe()
        self.moved = False
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        delta = tip_pose @ self.start_tip_inv
        if not self.moved:
            dist = (tip_pose.translation - self.start_tip.translation).length
            turn = delta.to_quaternion().angle
            if dist < CLICK_MOVE * self.scale and turn < CLICK_TURN:
                return
            self.moved = True
        s, st, i = self.spline, self.start, self.index
        if self.part == "co":
            m = np.array(self.inv @ delta @ self.world)
            for field in _FIELDS:
                getattr(s, field)[i] = getattr(st, field)[i] @ m[:3, :3].T + m[:3, 3]
        else:
            field = "handle_left" if self.part == "left" else "handle_right"
            move = np.array((self.inv.to_3x3() @ (tip_pose.translation - self.start_tip.translation))[:])
            getattr(s, field)[i] = getattr(st, field)[i] + move
            mine, theirs = (s.left_type, s.right_type) if self.part == "left" else (s.right_type, s.left_type)
            if mine[i] == 'AUTO' or theirs[i] == 'AUTO':
                mine[i] = theirs[i] = 'ALIGNED'  # As in Blender: grabbing an auto handle aligns the pair.
            elif mine[i] == 'VECTOR':
                mine[i] = 'FREE'
            if mine[i] == 'ALIGNED' and theirs[i] == 'ALIGNED':
                _align(s, i, field)
        n = len(s)
        near = sorted({i, (i - 1) % n, (i + 1) % n})
        write(self.obj, self.si, s, only=near)
        self.changed = True

    def finish(self, context):
        if not self.moved and self.part == "co":
            # A click: corner <-> smooth.
            s = self.spline
            i = self.index
            corner = s.left_type[i] == 'VECTOR' and s.right_type[i] == 'VECTOR'
            s.left_type[i] = s.right_type[i] = 'AUTO' if corner else 'VECTOR'
            write(self.obj, self.si, s, only=[i])
            self.changed = True
        if self.changed:
            context.view_layer.update()


class InsertPoint(GrabPoint):
    """Edit tool on a curve between its points: a new point there (the shape stays), then drag it."""

    def __init__(self, context, obj, spline_index, seg, t, tip_pose, scale):
        spline = read(obj, spline_index)
        index = spline.insert(seg, min(max(t, 0.02), 0.98))
        spline_index = write(obj, spline_index, spline)
        super().__init__(context, obj, spline_index, index, "co", tip_pose, scale)
        self.moved = True  # Not a click: the point is new.
        self.changed = True


class DeletePoint:
    """Off-hand Edit press on a point: it goes. A spline left with one point goes, and our curve with it."""

    def __init__(self, context, obj, spline_index, index):
        self.changed = True
        spline = read(obj, spline_index)
        if len(spline) <= 2:
            if is_ours(obj) and len(obj.data.splines) == 1:
                delete(obj)
                return
            if len(spline) <= 1 or len(obj.data.splines) > 1:
                obj.data.splines.remove(obj.data.splines[spline_index])
                obj.data.update_tag()
                return
        spline.remove(index)
        write(obj, spline_index, spline)

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        context.view_layer.update()


def pick_edit(context, off, tip_pose, scale):
    """Edit tool press on a curve: grab, insert or (off hand) delete. None if no curve is in reach."""
    tip = tip_pose.translation
    hit = nearest_point(context, tip, POINT_REACH * scale, handles=not off)
    if hit is not None:
        obj, si, i, part = hit
        if off:
            return DeletePoint(context, obj, si, i)
        return GrabPoint(context, obj, si, i, part, tip_pose, scale)
    if off:
        return None
    seg = nearest_segment(context, tip, SEGMENT_REACH * scale)
    if seg is not None:
        obj, si, k, t, _d = seg
        return InsertPoint(context, obj, si, k, t, tip_pose, scale)
    return None


# --- erase and quick undo ------------------------------------------------------------

def erase(context, sweep, margin):
    """Delete our curves the sweep (world points) touches. Returns whether any went."""
    gone = False
    for obj in [o for o in editable_curves(context) if is_ours(o)]:
        allow = margin + bevel_radius(obj)
        for _si, sp in _bezier_splines(obj):
            s = Spline(sp)
            pts, _t = sample(s, 8)
            world = _world(obj, np.vstack([pts.reshape(-1, 3), s.co]))
            d = np.linalg.norm(world[:, None, :] - sweep[None, :, :], axis=2)
            if d.min() <= allow:
                delete(obj)
                gone = True
                break
    return gone


def undo(kind, name):
    """Quick undo of a curve history entry: drop the curve's last point (all of it, if none would be left),
    or open a closed curve again (and keep building it). Returns a description, or None."""
    obj = bpy.data.objects.get(name)
    if not is_ours(obj) or not len(obj.data.splines):
        return None
    sp = obj.data.splines[0]
    if kind == "curve_close":
        if not sp.use_cyclic_u:
            return None
        sp.use_cyclic_u = False
        obj.data.update_tag()
        _state["active"] = obj.name
        return "curve close"
    spline = Spline(sp)
    if len(spline) <= 1:
        delete(obj)
        return "curve"
    spline.remove(len(spline) - 1)
    write(obj, 0, spline)
    if not sp.use_cyclic_u:
        _state["active"] = obj.name
    return "curve point"


# --- drawing in the headset -------------------------------------------------------

POINT_COLOR = (1.0, 1.0, 1.0)
CORNER_COLOR = (1.0, 0.6, 0.25)
HANDLE_COLOR = (0.75, 0.75, 0.8)
HOT_COLOR = (1.0, 0.85, 0.3)
CLOSE_COLOR = (0.35, 1.0, 0.5)
DELETE_COLOR = (1.0, 0.35, 0.3)


def _cross(p, r):
    out = []
    for axis in range(3):
        a, b = list(p), list(p)
        a[axis] -= r
        b[axis] += r
        out += [a, b]
    return out


def _diamond(p, r):
    """An octahedron's edges: control points."""
    tips = []
    for axis in range(3):
        for sign in (-1, 1):
            q = list(p)
            q[axis] += sign * r
            tips.append(q)
    out = []
    for a in range(6):
        for b in range(a + 1, 6):
            if a // 2 != b // 2:
                out += [tips[a], tips[b]]
    return out


def draw(shader, context, settings, tips, main, scale):
    """Points and handles of the Bezier curves (Curve and Edit tools), what each tip would take hold of, and
    the open curve's next segment to the main tip. tips: the two hands' tip positions; main: the main hand."""
    from gpu_extras.batch import batch_for_shader
    tool = settings.draw_tool
    r = 0.005 * scale
    lines = {}  # color -> list of points (LINES)

    def add(color, pts):
        lines.setdefault(color, []).extend(pts)

    hot = {}
    if tool == 'EDIT':
        for idx, tip in enumerate(tips):
            off = idx != main
            hit = nearest_point(context, tip, POINT_REACH * scale, handles=not off)
            if hit is not None:
                hot[hit] = DELETE_COLOR if off else HOT_COLOR
            elif not off:
                seg = nearest_segment(context, tip, SEGMENT_REACH * scale)
                if seg is not None:
                    # Where a press would insert a point.
                    obj, si, k, t, _d = seg
                    s = read(obj, si)
                    i, j = s.segments()[k]
                    p0, p1, p2, p3 = s.co[i], s.handle_right[i], s.handle_left[j], s.co[j]
                    u = 1.0 - t
                    q = u ** 3 * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t ** 3 * p3
                    add(HOT_COLOR, _cross(_world(obj, q[None, :])[0], r * 1.5))
    open_obj = active(context) if tool == 'CURVE' else None
    drawn = 0
    for obj in editable_curves(context):
        for si, sp in _bezier_splines(obj):
            s = Spline(sp)
            drawn += len(s)
            if drawn > MAX_DRAWN_POINTS:
                break
            co, hl, hr = (_world(obj, a) for a in (s.co, s.handle_left, s.handle_right))
            for i in range(len(s)):
                corner = s.left_type[i] == 'VECTOR' and s.right_type[i] == 'VECTOR'
                color = hot.get((obj, si, i, "co"), CORNER_COLOR if corner else POINT_COLOR)
                add(color, _diamond(co[i], r * (1.8 if color in (HOT_COLOR, DELETE_COLOR) else 1.0)))
                if tool == 'EDIT' and not corner:
                    for part, h in (("left", hl[i]), ("right", hr[i])):
                        add(HANDLE_COLOR, [co[i].tolist(), h.tolist()])
                        if (obj, si, i, part) in hot:
                            add(HOT_COLOR, _diamond(h, r * 1.5))
                        else:
                            add(HANDLE_COLOR, _cross(h, r * 0.8))
            if obj == open_obj and len(s):
                last = co[-1]
                tip = np.array(tips[main][:])
                reach = POINT_REACH * scale
                if len(s) >= 2 and np.linalg.norm(co[0] - tip) <= reach:
                    add(CLOSE_COLOR, _diamond(co[0], r * 2.0))  # Pressing here closes it.
                elif np.linalg.norm(last - tip) <= reach:
                    add(CLOSE_COLOR, _diamond(last, r * 2.0))  # Pressing here finishes it.
                else:
                    # The segment the next press would add, with handles about as Blender's auto ones come out
                    # (along the neighbors, 0.39 of the way): it bends the way the curve will.
                    span = float(np.linalg.norm(tip - last))
                    if s.right_type[-1] != 'AUTO':
                        h1 = hr[-1]
                    else:
                        along = tip - (co[-2] if len(s) >= 2 else last)
                        h1 = last + along / max(float(np.linalg.norm(along)), 1e-9) * span * 0.39
                    back = h1 - tip
                    h2 = tip + back / max(float(np.linalg.norm(back)), 1e-9) * span * 0.39
                    t = np.linspace(0.0, 1.0, 17)[:, None]
                    curve = ((1 - t) ** 3 * last + 3 * (1 - t) ** 2 * t * h1 + 3 * (1 - t) * t * t * h2 + t ** 3 * tip)
                    seg = [q.tolist() for k in range(16) for q in (curve[k], curve[k + 1])]
                    add(strokes.COLORS.get(settings.draw_color, POINT_COLOR), seg)
    for color, pts in lines.items():
        batch = batch_for_shader(shader, 'LINES', {"pos": [tuple(p) for p in pts]})
        shader.uniform_float("color", (*color, 0.95))
        batch.draw(shader)
