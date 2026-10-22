"""Chip cards: small groups of rounded chips that float in the world and are pressed by touch.

A card is laid out with vr_ui.layout (rows of buttons, selects, sliders and labels), but nobody
points at it: you reach in with a controller tip and pull the trigger while the tip is in the chip. Sliders drag along the card's plane. Cards fade in when a tip comes near and
fade out otherwise; one attached to a hand can only be pressed by the other hand.
"""

import math

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import hud, vr_ui

TOUCH_DEPTH = 0.03  # hand-space meters in front of or behind a card that still press it
CURSOR_DEPTH = 0.08  # the touch cursor shows this far from the card
MARGIN = 0.005  # around each chip that still counts as touching it
RADIUS = 0.0055  # chip corner radius

COL_CHIP = (0.15, 0.15, 0.17, 0.9)
COL_ACTIVE = (0.26, 0.52, 0.98, 1.0)
COL_DANGER = (0.72, 0.24, 0.27, 1.0)
COL_TRACK = (0.11, 0.11, 0.13, 0.92)
COL_BACKING = (0.03, 0.03, 0.04, 0.55)
COL_TEXT = (1.0, 1.0, 1.0, 1.0)
COL_DIM = (0.72, 0.73, 0.78, 1.0)


class Card:
    """A laid-out card at a world matrix (origin at its bottom center, Y up, scaled to hand space).

    owner: the hand index the card is attached to (0 left, 1 right), which can't press it; None for
    cards that stay in the world. alpha: how far it has faded in (0 = hidden, not pressable).
    """

    def __init__(self, id, matrix, rows, width=vr_ui.WIDTH, owner=None, alpha=1.0, accent=None, backing=True):
        self.id = id
        self.matrix = matrix
        self.widgets, self.height = vr_ui.layout(rows, width)
        self.width = width
        self.owner = owner
        self.alpha = alpha
        self.accent = accent
        self.backing = backing
        self._inv = None

    @property
    def inv(self):
        if self._inv is None:
            self._inv = self.matrix.inverted_safe()
        return self._inv

    def local(self, point):
        return self.inv @ Vector(point)

    def center(self):
        return self.matrix @ Vector((0.0, self.height / 2, 0.0))

    def find(self, id):
        return vr_ui.find(self.widgets, id)


class Touch:
    """Where a tip touches a card: the chip (None between chips), card-space x, y and depth."""

    def __init__(self, card, widget, x, y, depth):
        self.card, self.widget, self.x, self.y, self.depth = card, widget, x, y, depth


def chip_at(widgets, x, y, margin=MARGIN):
    """The pressable chip under (x, y), or the nearest one within margin."""
    best, best_d = None, None
    for w in widgets:
        if w.kind == 'label':
            continue
        x0, y0, x1, y1 = w.rect
        dx = max(x0 - x, 0.0, x - x1)
        dy = max(y0 - y, 0.0, y - y1)
        d = math.hypot(dx, dy)
        if d <= margin and (best is None or d < best_d):
            best, best_d = w, d
    return best


def touch(card, point, depth=TOUCH_DEPTH):
    """Touch for a tip at point (world), or None when it's off the card or too far in front/behind."""
    if card.alpha <= 0.05:
        return None
    p = card.local(point)
    if abs(p.z) > depth:
        return None
    w = card.width
    if not (-w / 2 - MARGIN <= p.x <= w / 2 + MARGIN and -MARGIN <= p.y <= card.height + MARGIN):
        return None
    return Touch(card, chip_at(card.widgets, p.x, p.y), p.x, p.y, p.z)


def plane_x(card, point):
    """Card-space x of a point projected onto the card's plane (slider drags, no bounds)."""
    return card.local(point).x


# --- drawing -------------------------------------------------------------------------

def rounded(x0, y0, x1, y1, r, z=0.0, segments=4):
    """Outline points of a rounded rectangle, counter-clockwise."""
    r = max(min(r, (x1 - x0) / 2, (y1 - y0) / 2), 0.0)
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90.0), (x1 - r, y1 - r, 0.0), (x0 + r, y1 - r, 90.0),
                       (x0 + r, y0 + r, 180.0)):
        for i in range(segments + 1):
            a = math.radians(a0 + 90.0 * i / segments)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a), z))
    return pts


def _fill(shader, pts, color):
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    batch = batch_for_shader(shader, 'TRI_FAN', {"pos": [(cx, cy, pts[0][2])] + pts + [pts[0]]})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _outline(shader, pts, color):
    batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": pts})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _fade(c, alpha):
    return (c[0], c[1], c[2], c[3] * alpha)


def _lighter(c, amount):
    return (min(c[0] + amount, 1.0), min(c[1] + amount, 1.0), min(c[2] + amount, 1.0), c[3])


def _gradient(x0, y0, x1, y1, colors, z, alpha):
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    n = len(colors)
    pos, col = [], []
    for i in range(n - 1):
        xa = x0 + (x1 - x0) * i / (n - 1)
        xb = x0 + (x1 - x0) * (i + 1) / (n - 1)
        ca, cb = (*colors[i][:3], alpha), (*colors[i + 1][:3], alpha)
        pos += [(xa, y0, z), (xb, y0, z), (xb, y1, z), (xa, y0, z), (xb, y1, z), (xa, y1, z)]
        col += [ca, cb, cb, ca, cb, ca]
    batch = batch_for_shader(shader, 'TRIS', {"pos": pos, "color": col})
    batch.draw(shader)


def draw_card(card, hover_id=None, pressed_id=None, cursor=None):
    """Draw a card. cursor: (x, y, depth) of a tip near it, drawn as a ring that closes on contact."""
    a = card.alpha
    if a <= 0.01:
        return
    accent = (*card.accent[:3], 1.0) if card.accent else COL_ACTIVE
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(card.matrix)
    w, h = card.width, card.height
    if card.backing:
        _fill(shader, rounded(-w / 2, 0.0, w / 2, h, RADIUS * 1.6, 0.0, 6), _fade(COL_BACKING, a))
    for wd in card.widgets:
        x0, y0, x1, y1 = wd.rect
        hot = wd.id and (wd.id == hover_id or wd.id == pressed_id)
        lift = 0.0015 if hot else 0.0  # A touched chip comes forward a little.
        z = 0.0002 + lift
        shape = rounded(x0, y0, x1, y1, RADIUS, z)
        if wd.kind in {'button', 'select'}:
            if wd.swatch is not None:
                _fill(shader, shape, _fade((*wd.swatch[:3], 1.0), a))
                if wd.active:
                    ring = rounded(x0 + 0.0015, y0 + 0.0015, x1 - 0.0015, y1 - 0.0015, RADIUS * 0.7, z + 0.0002)
                    for k in range(2):  # Thick: line widths can't be relied on in VR.
                        _outline(shader, [(p[0] + 0.0004 * k, p[1], p[2]) for p in ring], _fade(COL_TEXT, a))
            else:
                c = COL_DANGER if wd.danger else (accent if wd.active else COL_CHIP)
                if hot:
                    c = _lighter(c, 0.16 if wd.id == pressed_id else 0.09)
                _fill(shader, shape, _fade(c, a))
        elif wd.kind == 'slider':
            _fill(shader, shape, _fade(_lighter(COL_TRACK, 0.06) if hot else COL_TRACK, a))
            inner = x1 - x0 - 2 * vr_ui.PAD
            fx = x0 + vr_ui.PAD + inner * wd.fraction()
            if getattr(wd, "gradient", None):
                _gradient(x0 + vr_ui.PAD, y0 + 0.002, x1 - vr_ui.PAD, y0 + 0.0055, wd.gradient, z + 0.0001, a)
            else:
                _fill(shader, rounded(x0 + 0.0015, y0 + 0.002, max(fx, x0 + 0.0045), y0 + 0.0045, 0.00125,
                                      z + 0.0001), _fade(accent, a))
            for t in wd.ticks:
                tx = x0 + vr_ui.PAD + inner * min(max(t, 0.0), 1.0)
                _fill(shader, rounded(tx - 0.0007, y0 + 0.001, tx + 0.0007, y0 + 0.008, 0.0003, z + 0.00015),
                      _fade(vr_ui.COL_KEY, a))
            # The thumb: a dot on the track where the value is.
            r = 0.0028
            dot = [(fx + r * math.cos(t), y0 + 0.0037 + r * math.sin(t), z + 0.0003)
                   for t in (2 * math.pi * i / 16 for i in range(16))]
            _fill(shader, dot, _fade(COL_TEXT, a))
        if hot:
            _outline(shader, rounded(x0, y0, x1, y1, RADIUS, z + 0.0004), _fade((1.0, 1.0, 1.0, 0.95), a))
    if cursor is not None:
        cx, cy, depth = cursor
        t = min(abs(depth) / CURSOR_DEPTH, 1.0)
        r = 0.0025 + 0.01 * t
        ring = [(cx + r * math.cos(q), cy + r * math.sin(q), 0.001) for q in (2 * math.pi * i / 24 for i in range(24))]
        _outline(shader, ring, (1.0, 1.0, 1.0, (0.95 - 0.6 * t) * a))
        if abs(depth) <= TOUCH_DEPTH:
            _fill(shader, [(cx + 0.0015 * math.cos(q), cy + 0.0015 * math.sin(q), 0.0011)
                           for q in (2 * math.pi * i / 12 for i in range(12))], (1.0, 1.0, 1.0, a))
    gpu.matrix.pop()

    m = card.matrix
    for wd in card.widgets:
        x0, y0, x1, y1 = wd.rect
        hot = wd.id and (wd.id == hover_id or wd.id == pressed_id)
        lift = 0.0015 if hot else 0.0
        mz = m @ Matrix.Translation((0.0, 0.0, lift)) if lift else m
        mid = (y0 + y1) / 2
        inner = 0.0025
        if wd.kind == 'label':
            _text(mz, x0 + inner, mid, wd.text, x1 - x0 - inner, _fade(COL_DIM, a))
        elif wd.kind == 'button':
            if wd.text:
                dark = wd.swatch is not None and sum(wd.swatch[:3]) > 1.8
                color = (0.05, 0.05, 0.06, a) if dark else _fade(COL_TEXT, a)
                _text(mz, x0 + inner, mid, wd.text, x1 - x0 - 2 * inner, color, 'CENTER')
        elif wd.kind in {'select', 'slider'}:
            value = wd.value_text + "  ▾" if wd.kind == 'select' else wd.fmt(wd.value)
            ymid = mid if wd.kind == 'select' else mid + 0.0018
            room = x1 - x0 - 2 * vr_ui.PAD
            vw = _text(mz, x0 + vr_ui.PAD + room * 0.4, ymid, value, room * 0.6, _fade(COL_TEXT, a), 'RIGHT')
            _text(mz, x0 + vr_ui.PAD, ymid, wd.text, room - vw - 0.004, _fade(COL_DIM, a))
    gpu.state.blend_set('NONE')


def _text(matrix, x, y_mid, text, max_width, color, align='LEFT'):
    text, h = vr_ui.fit_text(text, max_width)
    w = hud.text_width(text, h)
    if align == 'CENTER':
        x += (max_width - w) / 2
    elif align == 'RIGHT':
        x += max_width - w
    hud.draw_text(matrix, x, y_mid - h / 2, text, h, color)
    return w


def draw_label(matrix, x, y, text, height, color, align='CENTER'):
    """A free-floating label (dock token names, gizmo values) with a soft pill behind it."""
    w = hud.text_width(text, height)
    if align == 'CENTER':
        x -= w / 2
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    pad = height * 0.6
    _fill(shader, rounded(x - pad, y - pad, x + w + pad, y + height + pad, height, 0.0),
          (0.03, 0.03, 0.04, 0.6 * color[3]))
    gpu.matrix.pop()
    hud.draw_text(matrix, x, y, text, height, color)
    gpu.state.blend_set('NONE')
