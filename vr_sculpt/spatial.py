"""The spatial UI: everything you reach for lives in the world, and you touch it to use it.

- The dock (dock.py): tool tokens on a belt at waist height. Take one with the trigger; its
  variants fan out above it.
- Gizmos (gizmos.py): handles on the selected object (resize, bevel, arrays, mirror center).
- Chip cards (chips.py), built by menus.py and placed here:
  - the tag of the tool in your right hand: its name and, as your left hand comes near, its options;
  - the palette in your left hand (Draw and Paint colors);
  - chips next to the selected object (Model mode), shown while a tip is near it;
  - the drawer (the dock's last slot): the few settings with no place of their own;
  - the timeline strip above the dock, and pickers, which float where they were opened.
- The brush ring at each tip: pull the other hand's ring with your tip to resize it.

There is no pointing ray (except at the desktop screen): a trigger press acts on what the tip is
in. pick() decides that for operators.py, in this order: a chip, a dock token, a ring, a handle.
"""

import math
import time

from mathutils import Matrix, Vector

from . import chips, dock, gizmos, hud, menus, shapes, vr_ui

TAG_WIDTH = 0.13  # hand-space meters
PALETTE_WIDTH = 0.12
OBJECT_WIDTH = 0.16
TRAY_WIDTH = 0.15
DRAWER_WIDTH = 0.17
TIMELINE_WIDTH = 0.22
PICKER_WIDTH = 0.2
HAND_WAKE = 0.24  # hand-space meters: the other tip this close to a hand's card wakes it
CARD_WAKE = 0.26  # ... and to the selection's chips
REACH_MAX = 0.55  # hand-space meters from the headset: farther chips are pulled in to here
RING_RIM = 0.015  # hand-space meters around a brush ring's rim that still pull it
RING_STEP = 0.005  # hand-space meters between the ticks you feel while pulling a ring
CACHE_SECONDS = 0.006  # both eyes of one headset frame (and the hover tick) reuse the cards

_drawer = {"open": False}
_picker = {"matrix": None}
_press = {"card": None, "widget": None}  # the chip the trigger holds, drawn pressed
_hover = {0: None, 1: None}  # what each tip was on at the last tick (for haptics)
_cache = {"t": -1.0, "cards": None}
_tray = {"token": None}  # the token whose variants show (the last one a tip was on)
_pull = {"handler": None}
_drag = {"handler": None}


def drawer_open():
    return _drawer["open"]


def set_drawer(on):
    _drawer["open"] = on
    invalidate()


def invalidate():
    _cache["t"] = -1.0


def reset():
    """Forget everything placed in the world (a new session, or tests)."""
    _drawer["open"] = False
    _picker["matrix"] = None
    _press.update(card=None, widget=None)
    _hover.update({0: None, 1: None})
    _tray["token"] = None
    _pull["handler"] = None
    _drag["handler"] = None
    invalidate()
    dock.reset()


def poses(context, ss):
    from . import operators
    return [operators.controller_pose(ss.controller_aim_location_get(context, i),
                                      ss.controller_aim_rotation_get(context, i)) for i in range(2)]


def tips(context, ss):
    from . import operators
    offset = context.scene.vr_sculpt.tip_offset * ss.viewer_scale
    return [operators.brush_tip(p, offset) for p in poses(context, ss)]


def _fade(distance, wake):
    """1 within half the wake distance, 0 beyond it."""
    return min(max((wake - distance) / (wake * 0.5), 0.0), 1.0)


# --- where the cards go ---------------------------------------------------------------

def tag_anchor(context, ss):
    return Vector(ss.controller_grip_location_get(context, 1)) + Vector((0.0, 0.0, 0.06 * ss.viewer_scale))


def palette_anchor(context, ss):
    return Vector(ss.controller_grip_location_get(context, 0)) + Vector((0.0, 0.0, 0.07 * ss.viewer_scale))


def _above(card, anchor, viewer, scale, lift=0.0):
    card.matrix = hud.billboard(anchor + Vector((0.0, 0.0, lift * scale)), viewer, scale)
    card._inv = None
    return card


def _centered(card, anchor, viewer, scale):
    card.matrix = hud.billboard(anchor, viewer, scale) @ Matrix.Translation((0.0, -card.height / 2, 0.0))
    card._inv = None
    return card


def object_anchor(obj, viewer, scale):
    """Beside the selected object, on your right, pulled in to arm's reach if it's far."""
    lo, hi = gizmos.local_box(obj)
    corners = [obj.matrix_world @ Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    center = sum(corners, Vector()) / 8
    to = center - viewer
    right = Vector((to.y, -to.x, 0.0))
    right = right.normalized() if right.length > 1e-9 else Vector((1.0, 0.0, 0.0))
    reach = max((c - center).dot(right) for c in corners)
    top = max(c.z for c in corners)
    anchor = center + right * (reach + 0.03 * scale)
    anchor.z = (center.z + top) / 2
    d = anchor - viewer
    if d.length > REACH_MAX * scale:
        anchor = viewer + d.normalized() * REACH_MAX * scale
    return anchor


def _timeline_anchor(ss):
    origin, yaw, scale = dock.frame(ss)
    return origin + dock._direction(yaw, 0.0) * (dock.RADIUS + 0.05) * scale + Vector((0.0, 0.0, 0.17 * scale))


def open_picker_here(point, viewer, scale):
    """Pickers open where they were asked for, in front of the chip (toward you)."""
    p = Vector(point) + (Vector(viewer) - Vector(point)).normalized() * 0.04 * scale
    _picker["matrix"] = hud.billboard(p, viewer, scale)


def cards(context, ss, cached=False):
    """Every chip card in the world right now (faded ones included, with their alpha)."""
    now = time.monotonic()
    if cached and _cache["cards"] is not None and now - _cache["t"] < CACHE_SECONDS:
        return _cache["cards"]
    out = _build(context, ss)
    _cache.update(t=now, cards=out)
    return out


def _build(context, ss):
    settings = context.scene.vr_sculpt
    if not settings.show_panel:
        return []
    scale = ss.viewer_scale
    viewer = Vector(ss.viewer_pose_location)
    tl = tips(context, ss)
    pressed = _press["card"]
    out = []

    if menus.picker_open():
        card = chips.Card("picker", Matrix.Identity(4), menus.picker_rows(), PICKER_WIDTH, accent=None)
        if _picker["matrix"] is None:
            open_picker_here(tl[1] + Vector((0.0, 0.25, 0.0)) * scale, viewer, scale)
        card.matrix = _picker["matrix"] @ Matrix.Translation((0.0, -card.height / 2, 0.0))
        out.append(card)
        return out  # A picker is modal: answer it (or Close) first.

    wake = dock.awake(ss, tl)
    slots = dict((t.id, p) for t, p in dock.slots(ss, tl))
    token = _tray_token(ss, tl)
    if token is not None and token != "drawer":
        rows = menus.tray_rows(context, token)
        if rows:
            card = chips.Card("tray", Matrix.Identity(4), rows, TRAY_WIDTH, alpha=1.0 if pressed == "tray" else wake)
            out.append(_above(card, slots[token], viewer, scale, 0.032))
    if _drawer["open"]:
        card = chips.Card("drawer", Matrix.Identity(4), menus.drawer_rows(context), DRAWER_WIDTH)
        out.append(_above(card, slots["drawer"], viewer, scale, 0.032))
    if settings.show_timeline:
        anchor = _timeline_anchor(ss)
        near = min((t - anchor).length for t in tl) / scale
        full = near < CARD_WAKE or pressed == "timeline"
        card = chips.Card("timeline", Matrix.Identity(4), menus.timeline_rows(context, settings, full),
                          TIMELINE_WIDTH, alpha=1.0 if full else 0.55)
        out.append(_above(card, anchor, viewer, scale))

    rows = menus.tag_rows(context)
    if rows:
        anchor = tag_anchor(context, ss)
        alpha = 1.0 if pressed == "tag" else _fade((tl[0] - anchor).length / scale, HAND_WAKE)
        card = chips.Card("tag", Matrix.Identity(4), rows, TAG_WIDTH, owner=1, alpha=alpha,
                          accent=hud_accent(context))
        out.append(_above(card, anchor, viewer, scale, 0.05))
    rows = menus.palette_rows(context)
    if rows:
        anchor = palette_anchor(context, ss)
        alpha = 1.0 if pressed == "palette" else max(_fade((tl[1] - anchor).length / scale, HAND_WAKE), 0.35)
        card = chips.Card("palette", Matrix.Identity(4), rows, PALETTE_WIDTH, owner=0, alpha=alpha)
        out.append(_above(card, anchor, viewer, scale))

    obj = selection(context)
    if obj is not None:
        card = chips.Card("object", Matrix.Identity(4), menus.object_rows(context, obj), OBJECT_WIDTH)
        _centered(card, object_anchor(obj, viewer, scale), viewer, scale)
        card.matrix = card.matrix @ Matrix.Translation((card.width / 2, 0.0, 0.0))
        card._inv = None
        d = min(min((t - card.center()).length, _box_distance(obj, t)) for t in tl) / scale
        card.alpha = 1.0 if pressed == "object" else _fade(d, CARD_WAKE)
        out.append(card)
    return out


def hud_accent(context):
    settings = context.scene.vr_sculpt
    held = dock.held_token(settings)
    return dock.tint(context, held) if held is not None else None


def selection(context):
    """The object whose chips show: the selected visible object in Model mode."""
    settings = context.scene.vr_sculpt
    if settings.mode != 'MODEL':
        return None
    obj = context.view_layer.objects.active
    if obj is None or not obj.select_get() or not obj.visible_get():
        return None
    return obj


def _box_distance(obj, point):
    lo, hi = gizmos.local_box(obj)
    p = obj.matrix_world.inverted_safe() @ Vector(point)
    q = Vector([min(max(p[i], lo[i]), hi[i]) for i in range(3)])
    return (obj.matrix_world @ q - Vector(point)).length


def _tray_token(ss, tl):
    """Whose variants show: the token a tip last touched, while a tip stays near the dock."""
    for tip in tl:
        hit = dock.token_at(ss, tip, tl)
        if hit is not None:
            _tray["token"] = hit[0].id
    if dock.awake(ss, tl) <= 0.0:
        _tray["token"] = None
    return _tray["token"]


def card_by_id(context, ss, id):
    return next((c for c in cards(context, ss) if c.id == id), None)


def touched(context, ss, hand_idx, tip):
    """The chip card touch for this hand's tip (cards on this hand can't be pressed by it)."""
    for card in cards(context, ss, cached=True):
        if card.owner == hand_idx:
            continue
        t = chips.touch(card, tip)
        if t is not None:
            return t
    return None


# --- the brush ring -------------------------------------------------------------------

def ring_prop(settings):
    """(property, factor) the tip ring's radius shows: radius = value * factor; None without a ring."""
    if settings.mode == 'SCULPT':
        return "radius", 1.0
    if settings.mode == 'PAINT':
        return "paint_radius", 1.0
    if settings.mode == 'DRAW' and settings.draw_tool == 'DRAW':
        return "draw_width", 0.5
    return None


RING_LIMITS = {"radius": (0.005, 0.3), "paint_radius": (0.002, 0.2), "draw_width": (0.001, 0.1)}


def ring_at(context, ss, hand_idx, tip):
    """The other hand's index when this tip is on the rim of its brush ring (not inside it: two tips
    working close together aren't pulling each other's rings)."""
    from . import operators
    settings = context.scene.vr_sculpt
    rp = ring_prop(settings)
    other = 1 - hand_idx
    if rp is None or other in operators.active_hands:
        return None
    scale = ss.viewer_scale
    radius = getattr(settings, rp[0]) * rp[1] * scale
    d = (Vector(tip) - tips(context, ss)[other]).length
    return other if abs(d - max(radius, 0.008 * scale)) <= RING_RIM * scale else None


class RingPull:
    """Trigger with a tip on the other hand's ring: the ring's radius follows the distance between
    your tips (spread your hands to grow it), with a tick you feel every 5 mm."""

    def __init__(self, context, ss, hand, other):
        self.ss, self.hand, self.other = ss, hand, other
        self.prop, self.factor = ring_prop(context.scene.vr_sculpt)
        self.changed = False
        self.label = ""
        self.last_tick = None
        _pull["handler"] = self

    def update(self, context, pose, tip_pose, value):
        from . import operators
        settings = context.scene.vr_sculpt
        scale = self.ss.viewer_scale
        d = (Vector(tip_pose.translation) - tips(context, self.ss)[self.other]).length / scale
        lo, hi = RING_LIMITS[self.prop]
        v = min(max(d / self.factor, lo), hi)
        if abs(getattr(settings, self.prop) - v) > 1e-6:
            setattr(settings, self.prop, v)
            self.changed = True
        tick = int(d / RING_STEP)
        if tick != self.last_tick:
            if self.last_tick is not None:
                operators.haptic(context, self.hand, 0.3, 0.008)
            self.last_tick = tick
        shown = v * self.factor
        self.label = f"{'width' if self.prop == 'draw_width' else 'radius'} {shown * 100:.1f} cm"
        invalidate()

    def abandon(self):
        _pull["handler"] = None

    def finish(self, context):
        _pull["handler"] = None


# --- trigger presses --------------------------------------------------------------------

class ChipPress:
    """Trigger with a tip in a chip card. Buttons fire on press; sliders drag along the card. A press
    between chips belongs to the card too (it doesn't create or sculpt behind it)."""

    def __init__(self, context, ss, touch, hand):
        self.ss = ss
        self.hand = hand
        self.card_id = touch.card.id
        widget = touch.widget
        self.widget_id = widget.id if widget else None
        self.kind = widget.kind if widget else None
        self.changed = False
        _press.update(card=self.card_id, widget=self.widget_id)
        if widget is None:
            return
        if widget.kind in {'button', 'select'} and widget.on_press:
            was_open = menus.picker_open()
            point = touch.card.matrix @ Vector((touch.x, touch.y, 0.0))
            widget.on_press()
            if menus.picker_open() and not was_open:
                open_picker_here(point, Vector(ss.viewer_pose_location), ss.viewer_scale)
            self.changed = True
        elif widget.kind == 'slider':
            self._set(widget, touch.x)
        invalidate()

    def _set(self, widget, x):
        value = widget.value_at(x)
        if value != widget.value:
            widget.on_change(value)
            self.changed = True

    def update(self, context, pose, tip_pose, value):
        if self.kind != 'slider':
            return
        # Rebuild the cards: those on hands move, and values may have changed.
        invalidate()
        card = card_by_id(context, self.ss, self.card_id)
        widget = card.find(self.widget_id) if card is not None else None
        if widget is None:
            return
        self._set(widget, chips.plane_x(card, tip_pose.translation))

    def abandon(self):
        _press.update(card=None, widget=None)

    def finish(self, context):
        from . import generic
        _press.update(card=None, widget=None)
        generic.flush_pending(context)
        invalidate()
        if self.changed:
            context.view_layer.update()


class TokenPick:
    """Trigger with a tip in a dock token: take that tool (or put the one you hold back: an empty
    hand). The drawer token opens and closes the drawer."""

    def __init__(self, context, token, hand):
        settings = context.scene.vr_sculpt
        self.token = token
        self.changed = True
        if token.id == "drawer":
            set_drawer(not _drawer["open"])
        elif token.held(settings):
            menus.set_mode(context, 'HAND')
            set_drawer(False)
        else:
            menus.take_tool(context, token.mode, token.tool)
            set_drawer(False)
        _tray["token"] = token.id if token.id != "drawer" else None
        invalidate()

    def update(self, context, pose, tip_pose, value):
        pass

    def finish(self, context):
        pass


class _Gizmo:
    """Wraps a gizmo drag so the drawing knows which handle is held."""

    def __init__(self, drag):
        self.drag = drag
        self.changed = False
        _drag["handler"] = drag

    def update(self, context, pose, tip_pose, value):
        self.drag.update(context, pose, tip_pose, value)
        self.changed = self.drag.changed
        invalidate()

    def abandon(self):
        _drag["handler"] = None

    def finish(self, context):
        _drag["handler"] = None
        self.drag.finish(context)
        invalidate()


def cursor_handle(context, ss):
    """Draw mode's mirror center (the 3D cursor) while strokes mirror: a handle you can move."""
    settings = context.scene.vr_sculpt
    if not settings.show_panel or settings.mode != 'DRAW':
        return None
    if not (settings.mirror_x or settings.mirror_y or settings.mirror_z):
        return None
    return gizmos.Handle("cursor", 'mirror', context.scene.cursor.location.copy())


def pick(context, settings, hand, pose, tip_pose, scale):
    """The handler for a trigger press on the spatial UI, or None (then it's for the scene)."""
    from . import actions
    ss = context.window_manager.xr_session_state
    if ss is None or not settings.show_panel:
        return None
    idx = actions.hand_index(hand)
    tip = tip_pose.translation
    invalidate()  # Fresh cards for the press: settings may have changed since the last frame.
    t = touched(context, ss, idx, tip)
    if t is not None:
        return ChipPress(context, ss, t, hand)
    hit = dock.token_at(ss, tip, tips(context, ss))
    if hit is not None:
        return TokenPick(context, hit[0], hand)
    other = ring_at(context, ss, idx, tip)
    if other is not None:
        return RingPull(context, ss, hand, other)
    grab = gizmos.handle_at(context, ss, tip)
    if grab is not None:
        return _Gizmo(gizmos.start_drag(context, grab[0], grab[1], tip, scale, hand))
    h = cursor_handle(context, ss)
    if h is not None and (h.position - tip).length <= gizmos.REACH * scale:
        return _Gizmo(gizmos.MirrorDrag(context, None, h, tip, scale, hand))
    return None


# --- hover (what each tip is on) and its haptics ------------------------------------------

def hover(context, ss, hand_idx, tip, tl=None):
    """('chip', card id, widget id) / ('token', id) / ('ring',) / ('handle', id), or None."""
    settings = context.scene.vr_sculpt
    if not settings.show_panel:
        return None
    t = touched(context, ss, hand_idx, tip)
    if t is not None:
        return ('chip', t.card.id, t.widget.id if t.widget else None)
    hit = dock.token_at(ss, tip, tl or ())
    if hit is not None:
        return ('token', hit[0].id)
    if ring_at(context, ss, hand_idx, tip) is not None:
        return ('ring',)
    grab = gizmos.handle_at(context, ss, tip)
    if grab is not None:
        return ('handle', grab[1].id)
    h = cursor_handle(context, ss)
    if h is not None and (h.position - tip).length <= gizmos.REACH * ss.viewer_scale:
        return ('handle', h.id)
    return None


def tick(context, ss):
    """A light buzz when a tip enters a chip, token, ring or handle (called at 90 Hz)."""
    from . import actions, operators
    tl = tips(context, ss)
    for i in range(2):
        if i in operators.active_hands:
            continue
        h = hover(context, ss, i, tl[i], tl)
        if h != _hover[i] and h is not None and (h[0] != 'chip' or h[2] is not None):
            operators.haptic(context, actions.HANDS[i], 0.22 if h[0] == 'chip' else 0.35, 0.01)
        _hover[i] = h


# --- drawing -------------------------------------------------------------------------

def draw(context, ss, xr=True):
    """The dock, gizmos, cards, the tool tag's header and ring pulls."""
    settings = context.scene.vr_sculpt
    scale = ss.viewer_scale
    viewer = Vector(ss.viewer_pose_location)
    tl = tips(context, ss)
    if settings.show_panel:
        hovered = {h[1] for h in (hover(context, ss, i, tl[i], tl) for i in range(2)) if h and h[0] == 'token'}
        dock.draw(context, ss, tl, hovered)
        drag = _drag["handler"]
        hot = None
        for i in range(2):
            h = hover(context, ss, i, tl[i], tl)
            if h and h[0] == 'handle':
                hot = h[1]
        gizmos.draw(context, ss, tl, hot, drag)
        cursor = cursor_handle(context, ss)
        if cursor is not None:
            _draw_cursor_handle(ss, cursor, hot == "cursor" or (drag is not None and drag.handle.id == "cursor"))
    if settings.show_brush_card:
        _draw_tag_header(context, ss, viewer, scale)
    for card in cards(context, ss, cached=True):
        hover_id, cursor = None, None
        for i in range(2):
            if card.owner == i:
                continue
            p = card.local(tl[i])
            if abs(p.z) <= chips.CURSOR_DEPTH and -card.width / 2 - 0.01 <= p.x <= card.width / 2 + 0.01 \
                    and -0.01 <= p.y <= card.height + 0.01:
                cursor = (p.x, p.y, p.z)
                t = chips.touch(card, tl[i])
                if t is not None and t.widget is not None:
                    hover_id = t.widget.id
        pressed = _press["widget"] if _press["card"] == card.id else None
        chips.draw_card(card, hover_id, pressed, cursor)
    pull = _pull["handler"]
    if pull is not None:
        _draw_pull(ss, tl, pull)


def _draw_tag_header(context, ss, viewer, scale):
    """The name of what's in your right hand (with its token as the icon), above the controller."""
    from . import draw as vr_draw
    settings = context.scene.vr_sculpt
    sculpt_brush = context.tool_settings.sculpt.brush if settings.engine == 'NATIVE' else None
    content = hud.card_content(settings, sculpt_brush, vr_draw.COLORS)
    held = dock.held_token(settings)
    if held is not None and content[3] is None:
        content = (content[0], content[1], content[2], "token")
    icon = hud.draw_card(tag_anchor(context, ss), viewer, scale, content)
    if held is not None and content[3] == "token" and icon is not None:
        shapes.begin_solid()
        gpu_state_none()
        m = dock.token_matrix(icon, viewer, scale, (time.monotonic() * 0.8) % (2 * math.pi), hud.ICON_SIZE * 0.33)
        shapes.draw_shape("token_" + held.id, dock.parts(held.id), m, shapes.light_from(viewer, icon), 1.0, 0.0,
                          dock.tint(context, held))
        shapes.end_solid()


def gpu_state_none():
    import gpu
    gpu.state.depth_test_set('NONE')  # The icon is drawn over its card, never hidden by the scene.


def _draw_cursor_handle(ss, handle, hot):
    from mathutils import Quaternion
    viewer = Vector(ss.viewer_pose_location)
    size = gizmos.HANDLE_SIZE * ss.viewer_scale * (1.35 if hot else 1.0)
    shapes.begin_solid()
    gpu_state_none()
    shapes.draw_shape("g_diamond", gizmos._mesh("diamond"), shapes.matrix_at(handle.position, Quaternion(), size),
                      shapes.light_from(viewer, handle.position), 0.95, 0.45 if hot else 0.0, (0.85, 0.9, 1.0))
    shapes.end_solid()


def _draw_pull(ss, tl, pull):
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    a, b = tl[0], tl[1]
    batch = batch_for_shader(shader, 'LINES', {"pos": [a, b]})
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.6))
    batch.draw(shader)
    gpu.state.blend_set('NONE')
    if pull.label:
        m = hud.billboard((a + b) / 2 + Vector((0.0, 0.0, 0.02 * ss.viewer_scale)), Vector(ss.viewer_pose_location),
                          ss.viewer_scale)
        chips.draw_label(m, 0.0, 0.0, pull.label, 0.009, (1.0, 1.0, 1.0, 1.0))


def preview_matrix(context, ss):
    """Where the pinned camera preview goes: beside the timeline strip, else above the left hand."""
    viewer = Vector(ss.viewer_pose_location)
    settings = context.scene.vr_sculpt
    if settings.show_timeline and settings.show_panel:
        return hud.billboard(_timeline_anchor(ss), viewer, ss.viewer_scale), -TIMELINE_WIDTH / 2 - 0.006
    return hud.billboard(palette_anchor(context, ss), viewer, ss.viewer_scale), -PALETTE_WIDTH / 2 - 0.006


# Kept for scripts and tests that look a chip up by id wherever it lives.
def all_rows(context):
    rows = []
    for token in ("sculpt", "model", "draw", "paint", "pose"):
        rows += menus.tray_rows(context, token)
    rows += menus.tag_rows(context) + menus.palette_rows(context) + menus.drawer_rows(context)
    obj = selection(context)
    if obj is not None:
        rows += menus.object_rows(context, obj)
    rows += menus.timeline_rows(context, context.scene.vr_sculpt) + menus.picker_rows()
    return rows


def ids(context, rows=None):
    widgets, _h = vr_ui.layout(rows if rows is not None else all_rows(context))
    return {w.id: w for w in widgets if w.id}

