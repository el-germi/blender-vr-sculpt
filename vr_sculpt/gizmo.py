"""Transform gizmo: handles on the selected object to move it along one axis, turn it around one, and size it
along one or all three, taken with either hand's trigger (Model mode, and Tools' Light and Refs).

Grabbing an object moves and turns it freely, every axis at once; the gizmo is for one at a time:
- arrows (Move) slide it along an axis;
- rings (Turn) turn it around one, through its origin;
- cubes on its own axes (Size) stretch it along one, and the white ring facing you sizes it evenly.

Arrows and rings follow the world's axes, or the object's own with Local. Size cubes are always on the object's
own axes, since that's what an object's scale is. The handles are a fixed size in hand space (SIZE) around
the object's origin and drawn over everything; they're faint until a tip comes near. While one is held the
others go, a guide (the axis, the ring) shows instead, and the amount shows by the tip. Snap to Grid moves in
grid steps and sizes in tenths; 15 degree steps turns in 15 degree steps.
"""

import math

from mathutils import Matrix, Vector

from . import actions, presence, primitives

SIZE = 0.09  # hand-space meters: the gizmo's unit, the other sizes are in units
ARROW = (0.95, 1.3)  # an arrow's start and tip
CONE = 0.2  # its head's length
RING = 0.72  # the turn rings' radius
CUBE = 0.42  # a size cube's distance from the origin
CUBE_HALF = 0.055
OUTER = 1.5  # the size-evenly ring's radius
REACH = 0.012  # hand-space meters from a handle that still take it
NEAR = 2.2  # a tip within this many units of the origin shows the gizmo fully; else it's faint
FAINT = 0.35
GUIDE = 4.0  # a held arrow's axis line reaches this far each way
SEGMENTS = 48

AXIS_COLORS = ((1.0, 0.35, 0.35), (0.45, 0.9, 0.35), (0.35, 0.55, 1.0))  # X, Y, Z like Blender's axes
OUTER_COLOR = (0.85, 0.87, 0.9)
HOT_COLOR = (1.0, 0.85, 0.3)
AXIS_NAMES = "XYZ"
KIND_ICONS = {'MOVE': "gizmo_move", 'TURN': "gizmo_turn", 'SIZE': "gizmo_size", 'SIZE_ALL': "gizmo_size"}

TURN_STEP = math.radians(15.0)  # 15 degree steps
SCALE_STEP = 0.1  # Snap to Grid: sizes in tenths
MIN_SCALE = 0.01
TICK_MOVE = 0.01  # hand-space meters between haptic ticks, unsnapped
TICK_TURN = math.radians(5.0)
TICK_SCALE = 0.05

drag = None  # the Drag being held, for drawing
_hover = {}  # controller index -> the handle under its tip, for a tick when it changes


def forget_caches():
    """Undo / redo / load: a held drag's object is gone (the trigger operator lets go of it too)."""
    global drag
    drag = None
    _hover.clear()


# --- where the handles are -------------------------------------------------------

def target(context):
    """The object the gizmo is on now (the selected one, where the trigger selects objects), or None."""
    s = context.scene.vr_sculpt
    if not (s.gizmo_move or s.gizmo_rotate or s.gizmo_scale):
        return None
    obj = context.view_layer.objects.active
    if obj is None or not obj.select_get() or not obj.visible_get():
        return None
    if s.mode == 'MODEL':
        # Marking edges and the UV tools take the trigger.
        return obj if s.uv_tool == 'NONE' and s.model_mark == 'NONE' else None
    if s.mode == 'TOOLS':
        from . import refs
        if (s.tool == 'LIGHT' and obj.type == 'LIGHT') or (s.tool == 'REFS' and refs.is_ref(obj)):
            return obj
    return None


def kinds(settings, obj):
    """The handle kinds shown on obj: a light's size does nothing, so it has none."""
    out = set()
    if settings.gizmo_move:
        out.add('MOVE')
    if settings.gizmo_rotate:
        out.add('TURN')
    if settings.gizmo_scale and obj.type != 'LIGHT':
        out |= {'SIZE', 'SIZE_ALL'}
    return out


def _ring_distance(p, center, normal, radius):
    v = p - center
    h = v.dot(normal)
    return math.hypot((v - normal * h).length - radius, h)


def _segment_distance(p, a, b):
    ab = b - a
    t = min(max((p - a).dot(ab) / max(ab.length_squared, 1e-12), 0.0), 1.0)
    return (p - (a + ab * t)).length


WORLD_AXES = (Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0)))


class Frame:
    """An object's handles as they are now: its origin, the unit (SIZE in world meters), the arrows' and rings'
    axes (the world's, or its own with Local), its own axes (the size cubes) and the outer ring's normal,
    toward the viewer."""

    def __init__(self, obj, scale, viewer, local, kinds):
        mw = obj.matrix_world
        self.center = mw.translation.copy()
        self.unit = SIZE * scale
        self.reach = REACH * scale
        rot = mw.to_3x3()
        self.own = [rot.col[i].normalized() if rot.col[i].length > 1e-9 else WORLD_AXES[i].copy()
                    for i in range(3)]
        self.local = local
        self.axes = self.own if local else [a.copy() for a in WORLD_AXES]
        toward = Vector(viewer) - self.center if viewer is not None else Vector((0.0, -1.0, 0.0))
        self.facing = toward.normalized() if toward.length > 1e-9 else Vector((0.0, -1.0, 0.0))
        self.kinds = kinds

    def handles(self):
        for kind in ('MOVE', 'TURN', 'SIZE'):
            if kind in self.kinds:
                for axis in range(3):
                    yield (kind, axis)
        if 'SIZE_ALL' in self.kinds:
            yield ('SIZE_ALL', None)

    def arrow(self, axis):
        """(start, tip) of an arrow."""
        a = self.axes[axis] * self.unit
        return self.center + a * ARROW[0], self.center + a * ARROW[1]

    def cube(self, axis):
        return self.center + self.own[axis] * CUBE * self.unit

    def distance(self, handle, p):
        kind, axis = handle
        if kind == 'MOVE':
            return _segment_distance(p, *self.arrow(axis))
        if kind == 'TURN':
            return _ring_distance(p, self.center, self.axes[axis], RING * self.unit)
        if kind == 'SIZE':
            # Touching the cube itself counts.
            return max((p - self.cube(axis)).length - CUBE_HALF * self.unit, 0.0)
        return _ring_distance(p, self.center, self.facing, OUTER * self.unit)

    def handle_at(self, p):
        """The handle nearest the point within reach, or None."""
        p = Vector(p)
        best, best_d = None, self.reach
        for handle in self.handles():
            d = self.distance(handle, p)
            if d <= best_d:
                best, best_d = handle, d
        return best


def _viewer(context):
    ss = context.window_manager.xr_session_state
    return Vector(ss.viewer_pose_location) if ss is not None else None


def frame_for(context, obj, scale):
    s = context.scene.vr_sculpt
    return Frame(obj, scale, _viewer(context), s.gizmo_local, kinds(s, obj))


def handle_under(context, tip, scale):
    """(object, frame, handle) for the gizmo handle at the tip, or None."""
    obj = target(context)
    if obj is None:
        return None
    frame = frame_for(context, obj, scale)
    handle = frame.handle_at(tip)
    return None if handle is None else (obj, frame, handle)


def describe(handle, local):
    kind, axis = handle
    if kind == 'SIZE_ALL':
        return "Size evenly"
    name = AXIS_NAMES[axis]
    if kind == 'SIZE' or local:
        name = "own " + name
    return {'MOVE': "Move along ", 'TURN': "Turn around ", 'SIZE': "Size along "}[kind] + name


def hint(context, tip, scale):
    """(icon, label) for the trigger's legend when the tip is on a handle, else None."""
    hit = handle_under(context, tip, scale)
    if hit is None:
        return None
    handle = hit[2]
    return KIND_ICONS[handle[0]], describe(handle, hit[1].local)


def pick(context, hand, tip_pose, scale):
    """The trigger's handler when the tip is on a handle (either hand), else None."""
    hit = handle_under(context, tip_pose.translation, scale)
    if hit is None:
        return None
    return Drag(context, *hit, tip_pose, actions.hand_index(hand))


# --- dragging a handle ------------------------------------------------------------

def _meters(v, signed=False):
    sign = ("+" if v >= 0 else "-") if signed else ""
    v = abs(v) if signed else v
    return sign + (f"{v * 100:.1f} cm" if v < 1.0 else f"{v:.2f} m")


class Drag:
    """A handle held by the trigger: the object moves along, turns around or sizes along the handle's axis (or
    sizes evenly) by how far the tip goes from where it took the handle. Nothing moves until the hand clearly
    does (interactions.DragGate), so pressing the trigger doesn't nudge it."""

    def __init__(self, context, obj, frame, handle, tip_pose, hand_idx):
        global drag
        from . import interactions
        self.obj = obj
        self.frame = frame
        self.kind, self.axis = handle
        self.start = obj.matrix_world.copy()
        self.hand = hand_idx
        self.gate = interactions.DragGate(context, tip_pose)
        self.grab = tip_pose.translation.copy()
        self.tip = self.grab.copy()
        self.first = None  # TURN: the spoke where it was taken, and the latest one
        self.spoke = None
        self.turned = 0.0  # TURN: the angle so far, unwrapped, so it can go past half a turn
        self.amount = 1.0 if self.kind in {'SIZE', 'SIZE_ALL'} else 0.0
        self.step = None  # the haptic tick's step
        self.changed = False
        primitives.make_active(context, obj)
        drag = self

    def update(self, context, pose, tip_pose, value):
        self.tip = tip_pose.translation.copy()
        if not self.gate.open:
            base = self.gate.update(tip_pose)
            if base is None:
                return
            self.grab = base.translation.copy()
        settings = context.scene.vr_sculpt
        new = getattr(self, "_" + self.kind.lower())(settings, self.tip)
        if new is None:
            return
        self._tick(context)
        if new != self.obj.matrix_world:
            self.obj.matrix_world = new
            self.changed = True

    def _move(self, settings, tip):
        a = self.frame.axes[self.axis]
        d = (tip - self.grab).dot(a)
        if settings.grid_snap:
            g = settings.grid_size
            if self.frame.local:
                d = round(d / g) * g  # In grid steps from where it was.
            else:
                at = self.start.translation[self.axis]
                d = round((at + d) / g) * g - at  # Onto the grid, as a grab snaps.
        self.amount = d
        return Matrix.Translation(a * d) @ self.start

    def _spoke(self, p):
        a = self.frame.axes[self.axis]
        v = p - self.frame.center
        return v - a * v.dot(a)

    def _turn(self, settings, tip):
        a = self.frame.axes[self.axis]
        if self.first is None:
            self.first = self.spoke = self._spoke(self.grab)
        v = self._spoke(tip)
        if v.length < 0.15 * self.frame.unit or self.spoke.length < 1e-9:
            return None  # Too near the axis to tell the angle: it holds.
        self.turned += math.atan2(a.dot(self.spoke.cross(v)), self.spoke.dot(v))
        self.spoke = v
        angle = self.turned
        if settings.snap_angle:
            angle = round(angle / TURN_STEP) * TURN_STEP
        self.amount = angle
        c = self.frame.center
        return Matrix.Translation(c) @ Matrix.Rotation(angle, 4, a) @ Matrix.Translation(-c) @ self.start

    def _ratio(self, settings, ratio):
        if settings.grid_snap:
            return max(round(ratio / SCALE_STEP) * SCALE_STEP, SCALE_STEP)
        return max(ratio, MIN_SCALE)

    def _size(self, settings, tip):
        b = self.frame.own[self.axis]
        c = self.frame.center
        base = (self.grab - c).dot(b)
        if abs(base) < 0.2 * self.frame.unit:
            base = math.copysign(0.2 * self.frame.unit, base)
        ratio = self._ratio(settings, (tip - c).dot(b) / base)
        self.amount = ratio
        s = [1.0, 1.0, 1.0]
        s[self.axis] = ratio
        return self.start @ Matrix.Diagonal((*s, 1.0))

    def _size_all(self, settings, tip):
        c = self.frame.center
        base = max((self.grab - c).length, 0.2 * self.frame.unit)
        ratio = self._ratio(settings, (tip - c).length / base)
        self.amount = ratio
        return self.start @ Matrix.Diagonal((ratio, ratio, ratio, 1.0))

    def _tick(self, context):
        """A light tick on the hand at each step: of the snap, or every TICK_* without."""
        settings = context.scene.vr_sculpt
        if self.kind == 'MOVE':
            step = settings.grid_size if settings.grid_snap else TICK_MOVE * self.frame.unit / SIZE
        elif self.kind == 'TURN':
            step = TURN_STEP if settings.snap_angle else TICK_TURN
        else:
            step = SCALE_STEP if settings.grid_snap else TICK_SCALE
        n = round(self.amount / step)
        if n != self.step:
            ss = context.window_manager.xr_session_state
            if self.step is not None and ss is not None:
                presence.tick(context, ss, self.hand, 0.25, 0.006)
            self.step = n

    def amount_text(self):
        """How far it's gone: "+12.0 cm", "+45\u00b0", "\u00d71.50 (24.0 cm)" (the new length along that axis)."""
        if self.kind == 'MOVE':
            return _meters(self.amount, signed=True)
        if self.kind == 'TURN':
            return f"{math.degrees(self.amount):+.0f}\u00b0"
        text = f"\u00d7{self.amount:.2f}"
        length = _extent(self.obj, self.axis) if self.kind == 'SIZE' else 0.0
        return text + f" ({_meters(length)})" if length > 1e-6 else text

    def readout(self):
        """The axis and amount, for the tag by the tip: "X +12.0 cm", "own X \u00d71.50 (24.0 cm)"."""
        if self.kind == 'SIZE_ALL':
            return self.amount_text()
        name = AXIS_NAMES[self.axis]
        if self.kind == 'SIZE' or self.frame.local:
            name = "own " + name
        return f"{name} {self.amount_text()}"

    def abandon(self):
        global drag
        drag = None

    def finish(self, context):
        global drag
        drag = None
        if self.changed:
            context.view_layer.update()
            from . import anim
            anim.auto_key(context, self.obj)


def _extent(obj, axis):
    """How long obj is along its own axis, in world meters (a picture's side for a reference)."""
    from . import refs
    if refs.is_ref(obj):
        if axis == 2:
            return 0.0
        x0, y0, x1, y1 = refs.local_rect(obj)
        side = (x1 - x0, y1 - y0)[axis]
    else:
        lo, hi = primitives.local_bounds(obj)
        side = hi[axis] - lo[axis]
    return side * obj.matrix_world.to_3x3().col[axis].length


# --- drawing ------------------------------------------------------------------------

def _basis(n):
    """Two unit vectors at right angles to n and each other."""
    x = Vector((1.0, 0.0, 0.0)) if abs(n.x) < 0.9 else Vector((0.0, 1.0, 0.0))
    x = (x - n * x.dot(n)).normalized()
    return x, n.cross(x)


class _Shapes:
    """Lines and triangles with per-vertex colors, drawn in two batches."""

    def __init__(self):
        self.lines, self.line_colors, self.tris, self.tri_colors = [], [], [], []

    def line(self, a, b, color):
        self.lines += [a[:], b[:]]
        self.line_colors += [color, color]

    def ring(self, center, normal, radius, color, a0=0.0, a1=2 * math.pi):
        x, y = _basis(normal)
        n = max(int(SEGMENTS * (a1 - a0) / (2 * math.pi)), 4)
        pts = [center + (x * math.cos(a0 + (a1 - a0) * i / n) + y * math.sin(a0 + (a1 - a0) * i / n)) * radius
               for i in range(n + 1)]
        for p, q in zip(pts, pts[1:]):
            self.line(p, q, color)

    def tri(self, a, b, c, color):
        self.tris += [a[:], b[:], c[:]]
        self.tri_colors += [color, color, color]

    def arrow(self, start, tip, radius, color):
        axis = tip - start
        length = axis.length
        if length < 1e-9:
            return
        d = axis / length
        base = tip - d * min(CONE / (ARROW[1] - ARROW[0]) * length, length)
        self.line(start, base, color)
        x, y = _basis(d)
        rim = [base + (x * math.cos(2 * math.pi * i / 8) + y * math.sin(2 * math.pi * i / 8)) * radius for i in range(8)]
        for i in range(8):
            self.tri(tip, rim[i], rim[(i + 1) % 8], color)
            self.tri(base, rim[(i + 1) % 8], rim[i], color)

    def cube(self, center, axes, half, color):
        c = [center + sum(((axes[k] * half * (1 if (i >> k) & 1 else -1)) for k in range(3)), Vector())
             for i in range(8)]
        for quad in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            a, b, cc, d = (c[i] for i in quad)
            self.tri(a, b, cc, color)
            self.tri(a, cc, d, color)

    def draw(self):
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        if self.tris:
            batch_for_shader(shader, 'TRIS', {"pos": self.tris, "color": self.tri_colors}).draw(shader)
        if self.lines:
            gpu.state.line_width_set(2.0)
            batch_for_shader(shader, 'LINES', {"pos": self.lines, "color": self.line_colors}).draw(shader)
            gpu.state.line_width_set(1.0)


def _color(rgb, alpha):
    return (rgb[0], rgb[1], rgb[2], alpha)


def _handle(shapes, frame, handle, rgb, alpha):
    kind, axis = handle
    u = frame.unit
    if kind == 'MOVE':
        start, tip = frame.arrow(axis)
        shapes.line(frame.center, start, _color(rgb, alpha * 0.3))
        shapes.arrow(start, tip, 0.06 * u, _color(rgb, alpha))
    elif kind == 'TURN':
        shapes.ring(frame.center, frame.axes[axis], RING * u, _color(rgb, alpha))
    elif kind == 'SIZE':
        p = frame.cube(axis)
        shapes.line(frame.center, p, _color(rgb, alpha * 0.3))
        shapes.cube(p, frame.own, CUBE_HALF * u, _color(rgb, alpha))
    else:
        shapes.ring(frame.center, frame.facing, OUTER * u, _color(rgb, alpha * 0.8))


def _rgb(handle):
    return OUTER_COLOR if handle[0] == 'SIZE_ALL' else AXIS_COLORS[handle[1]]


def status(context):
    """(content, size) for the tip status above the main hand while that hand holds a handle (what the
    handle does, and how far it's gone), else None. hud.draw_status draws it in place of the tool's."""
    held = drag
    if held is None or not held.gate.open or held.hand != actions.main_index(context):
        return None
    handle = (held.kind, held.axis)
    return (describe(handle, held.frame.local), "", _rgb(handle), KIND_ICONS[held.kind]), held.amount_text()


def _draw_held(shapes, held, frame):
    """The held handle and its guide: the axis line, the ring with its two spokes, the cube where the tip has
    it, the outer ring as far out as it's been pulled."""
    f0 = held.frame
    u = f0.unit
    rgb = _rgb((held.kind, held.axis))
    if held.kind == 'MOVE':
        a = f0.axes[held.axis]
        shapes.line(f0.center - a * GUIDE * u, f0.center + a * GUIDE * u, _color(rgb, 0.45))
        shapes.arrow(frame.center + a * ARROW[0] * u, frame.center + a * ARROW[1] * u, 0.06 * u,
                     _color(HOT_COLOR, 1.0))
    elif held.kind == 'TURN':
        a = f0.axes[held.axis]
        shapes.ring(f0.center, a, RING * u, _color(HOT_COLOR, 1.0))
        for spoke, alpha in ((held.first, 0.45), (held.spoke, 1.0)):
            if spoke is not None and spoke.length > 1e-9:
                shapes.line(f0.center, f0.center + spoke.normalized() * RING * u, _color(rgb, alpha))
    elif held.kind == 'SIZE':
        b = frame.own[held.axis]
        reach = abs((held.grab - f0.center).dot(f0.own[held.axis])) * held.amount
        shapes.line(frame.center - b * GUIDE * u, frame.center + b * GUIDE * u, _color(rgb, 0.3))
        shapes.cube(frame.center + b * reach, frame.own, CUBE_HALF * u, _color(HOT_COLOR, 1.0))
    else:
        r = max((held.grab - f0.center).length, 0.2 * u)
        shapes.ring(frame.center, frame.facing, r, _color(OUTER_COLOR, 0.35))
        shapes.ring(frame.center, frame.facing, r * held.amount, _color(HOT_COLOR, 1.0))


def draw(context, ss, tips, scale, xr=True):
    """The gizmo on its object: faint until a tip is near, the handle under a tip in yellow (with a tick on that
    hand when it gets there); while one is held, only it and its guide. How far it's gone shows in the tip status
(status()) for the main hand, in a tag by the tip for the off hand."""
    obj = target(context)
    if obj is None:
        _hover.clear()
        return
    import gpu
    from . import hud
    frame = frame_for(context, obj, scale)
    shapes = _Shapes()
    held = drag if drag is not None and drag.obj == obj else None
    if held is not None:
        _draw_held(shapes, held, frame)
    else:
        hot = set()
        for i, tip in enumerate(tips):
            handle = frame.handle_at(tip)
            if handle is not None:
                hot.add(handle)
            if xr and _hover.get(i) != handle:
                _hover[i] = handle
                if handle is not None:
                    presence.tick(context, ss, i, 0.1, 0.006)
        near = any((Vector(t) - frame.center).length < NEAR * frame.unit for t in tips)
        alpha = 1.0 if near or hot else FAINT
        for handle in frame.handles():
            _handle(shapes, frame, handle, HOT_COLOR if handle in hot else _rgb(handle), alpha)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    shapes.draw()
    gpu.state.blend_set('NONE')
    in_status = context.scene.vr_sculpt.show_brush_card and status(context) is not None
    if held is not None and held.gate.open and ss is not None and not in_status:
        # The off hand's (the tip status is over the main hand): a tag by its tip.
        hud.draw_tag(held.tip + Vector((0.0, 0.0, 0.02 * scale)), Vector(ss.viewer_pose_location), scale,
                     held.readout(), _rgb((held.kind, held.axis)))
