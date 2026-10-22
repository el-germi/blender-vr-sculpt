"""Drawing for the radial UI: pies, dial readouts, the status badge at the right hand, and the hints.

All of it is in hand space (meters at viewer scale 1) on cards that face the headset. The pie's
plane is the one it was opened in (radial.Pie.matrix); the rest follows the controllers.
"""

import math
import time

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import hud, icons, radial, vr_ui

INK = (0.95, 0.95, 0.97, 1.0)
DIM = (0.72, 0.73, 0.78, 1.0)
DARK = (0.04, 0.04, 0.05, 1.0)
WEDGE = (0.085, 0.085, 0.1, 0.84)
DISC = (0.05, 0.05, 0.06, 0.9)
DANGER = (0.9, 0.3, 0.3)
GAP = math.radians(2.2)  # between wedges
R_MID = (radial.RING_INNER + radial.RING_OUTER) / 2
LABEL_R = radial.RING_OUTER + 0.007
ICON = 0.0085  # half size
CENTER_R = 0.0175
GAUGE_START = math.radians(225.0)  # gauges sweep clockwise from bottom left ...
GAUGE_SWEEP = math.radians(270.0)  # ... to bottom right

def _uniform():
    return gpu.shader.from_builtin('UNIFORM_COLOR')


def _tris(pts, color, z=0.0):
    shader = _uniform()
    batch = batch_for_shader(shader, 'TRIS', {"pos": [(x, y, z) for x, y in pts]})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _sector(cx, cy, r0, r1, a0, a1, steps=None):
    """Triangles of an annulus sector between angles a0 < a1 (radians)."""
    steps = steps or max(int(abs(a1 - a0) / math.radians(6.0)), 2)
    out = []
    for i in range(steps):
        t0 = a0 + (a1 - a0) * i / steps
        t1 = a0 + (a1 - a0) * (i + 1) / steps
        p = [(cx + r0 * math.cos(t0), cy + r0 * math.sin(t0)), (cx + r1 * math.cos(t0), cy + r1 * math.sin(t0)),
             (cx + r1 * math.cos(t1), cy + r1 * math.sin(t1)), (cx + r0 * math.cos(t1), cy + r0 * math.sin(t1))]
        out += [p[0], p[1], p[2], p[0], p[2], p[3]]
    return out


def _disc(cx, cy, r, steps=40):
    out = []
    for i in range(steps):
        t0, t1 = 2 * math.pi * i / steps, 2 * math.pi * (i + 1) / steps
        out += [(cx, cy), (cx + r * math.cos(t0), cy + r * math.sin(t0)), (cx + r * math.cos(t1), cy + r * math.sin(t1))]
    return out


def _gradient_arc(cx, cy, r0, r1, a0, a1, color_at, z=0.0, steps=48):
    """An arc whose color changes along it: color_at(t) for t in 0..1."""
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    pos, col = [], []
    for i in range(steps):
        t0, t1 = i / steps, (i + 1) / steps
        b0, b1 = a0 + (a1 - a0) * t0, a0 + (a1 - a0) * t1
        c0, c1 = (*color_at(t0), 1.0), (*color_at(t1), 1.0)
        quad = [((cx + r0 * math.cos(b0), cy + r0 * math.sin(b0)), c0), ((cx + r1 * math.cos(b0), cy + r1 * math.sin(b0)), c0),
                ((cx + r1 * math.cos(b1), cy + r1 * math.sin(b1)), c1), ((cx + r0 * math.cos(b1), cy + r0 * math.sin(b1)), c1)]
        for k in (0, 1, 2, 0, 2, 3):
            pos.append((*quad[k][0], z))
            col.append(quad[k][1])
    batch = batch_for_shader(shader, 'TRIS', {"pos": pos, "color": col})
    batch.draw(shader)


def _text(x, y, text, height, color, align='LEFT', max_width=None):
    """Text in the current matrix, its baseline at y; returns its width."""
    if max_width is not None:
        text, height = vr_ui.fit_text(text, max_width, height)
    w = hud.text_width(text, height)
    if align == 'CENTER':
        x -= w / 2
    elif align == 'RIGHT':
        x -= w
    hud.draw_text(Matrix.Identity(4), x, y, text, height, color)
    return w


def _with_alpha(c, a):
    return (c[0], c[1], c[2], (c[3] if len(c) > 3 else 1.0) * a)


def _gauge(cx, cy, r0, r1, fraction, color, gradient=None, alpha=1.0, z=0.0001, ticks=()):
    """A 270 degree gauge: the track, then the filled part (or the gradient with a marker), and ticks
    (fractions, e.g. keyframes) just outside it."""
    a_start = GAUGE_START
    a_end = GAUGE_START - GAUGE_SWEEP
    _tris(_sector(cx, cy, r0, r1, a_end, a_start), _with_alpha((0.16, 0.16, 0.19, 0.95), alpha), z)
    for t in ticks:
        a = a_start - GAUGE_SWEEP * min(max(t, 0.0), 1.0)
        _tris(_sector(cx, cy, r1 + 0.0006, r1 + 0.0034, a - math.radians(1.2), a + math.radians(1.2), 1),
              _with_alpha((1.0, 0.82, 0.25, 1.0), alpha), z + 0.0001)
    if gradient is not None:
        _gradient_arc(cx, cy, r0, r1, a_start, a_end, gradient, z + 0.0001)
        a = a_start - GAUGE_SWEEP * fraction
        _tris(_sector(cx, cy, r0 - 0.0018, r1 + 0.0018, a - math.radians(2.5), a + math.radians(2.5), 2),
              _with_alpha(INK, alpha), z + 0.0002)
    elif fraction > 0.0:
        _tris(_sector(cx, cy, r0, r1, a_start - GAUGE_SWEEP * fraction, a_start), _with_alpha((*color[:3], 1.0), alpha),
              z + 0.0001)


# --- pies ---------------------------------------------------------------------------

def draw_pie(pie, now=None):
    if pie is None or pie.closed:
        return
    now = time.monotonic() if now is None else now
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(pie.matrix)
    try:
        # Fade in over a few frames, so a quick tap doesn't flash a pie.
        appear = min(max((now - pie.t0 - 0.04) / 0.1, 0.0), 1.0) if not pie.used else 1.0
        if appear <= 0.0:
            return
        for parent, child in zip(pie.frames, pie.frames[1:]):
            _draw_crumb(parent, child, appear)
        _draw_level(pie, appear)
    finally:
        gpu.matrix.pop()
        gpu.state.blend_set('NONE')


def _draw_crumb(parent, child, alpha):
    """Where a sub-pie came from: a small disc with the slice's icon, and a line to the new center."""
    p, c = parent.center, child.center
    level = parent.level
    item = level.items[child.via] if child.via is not None and 0 <= child.via < len(level.items) else None
    d = (c - p)
    if d.length > CENTER_R * 2:
        n = Vector((-d.y, d.x)).normalized() * 0.0007
        a, b = p + d.normalized() * 0.011, c - d.normalized() * (CENTER_R + 0.002)
        _tris([a + n, b + n, b - n, a + n, b - n, a - n], _with_alpha(DIM, 0.35 * alpha))
    _tris(_disc(p.x, p.y, 0.011), _with_alpha(DISC, 0.8 * alpha))
    color = level.color or INK
    if item is not None and not icons.draw(item.icon, p.x, p.y, 0.0065, _with_alpha((*color[:3], 1.0), 0.8 * alpha),
                                           0.0002):
        _text(p.x, p.y - 0.002, item.label[:3], 0.004, _with_alpha(INK, 0.8 * alpha), 'CENTER')


def _draw_level(pie, alpha):
    level = pie.level
    cx, cy = pie.center.x, pie.center.y
    n = len(level.items)
    accent = (*(level.color or (0.6, 0.75, 1.0))[:3], 1.0)
    grabbed = pie.grabbed
    hover = pie.hover
    fade = 0.22 if grabbed is not None and grabbed is not level.center else 1.0
    half = math.pi / max(n, 1)
    for i, item in enumerate(level.items):
        mid = radial.slice_angle(i, n)
        hot = hover == i
        is_grabbed = grabbed is item
        a = (fade if not is_grabbed else 1.0) * alpha
        r1 = radial.RING_OUTER + (0.005 if hot or is_grabbed else 0.0)
        a0, a1 = mid - half + GAP / 2, mid + half - GAP / 2
        if n == 1:
            a0, a1 = mid - math.pi + GAP, mid + math.pi - GAP
        hot_color = (*DANGER, 1.0) if item.danger else accent
        if item.swatch is not None:
            fill = (*item.swatch[:3], 1.0)
        else:
            fill = hot_color if hot or is_grabbed else WEDGE
        _tris(_sector(cx, cy, radial.RING_INNER, r1, a0, a1), _with_alpha(fill, a))
        if item.active:
            # The current setting: a bright band on the outer edge.
            band = INK if item.swatch is not None or hot else accent
            _tris(_sector(cx, cy, r1 - 0.0028, r1, a0, a1), _with_alpha(band, a), 0.0001)
        if hot and item.swatch is not None:
            _tris(_sector(cx, cy, radial.RING_INNER, radial.RING_INNER + 0.0025, a0, a1), _with_alpha(INK, a), 0.0001)
        ix, iy = cx + R_MID * math.cos(mid), cy + R_MID * math.sin(mid)
        if item.dial is not None:
            # Its value, as a thin arc along the outer edge.
            f = item.dial.fraction()
            span = (a1 - a0) * f
            _tris(_sector(cx, cy, r1 + 0.0012, r1 + 0.0032, a1 - span, a1),
                  _with_alpha(accent if not (hot or is_grabbed) else INK, a), 0.0001)
        icon_color = DARK if (hot or is_grabbed) and item.swatch is None else (accent if item.active else INK)
        if item.swatch is None:
            if not icons.draw(item.icon, ix, iy, ICON, _with_alpha(icon_color, a), 0.0002):
                width = 2 * R_MID * math.sin(half) * 0.92 if n > 1 else 0.03
                _text(ix, iy - 0.0022, item.label, 0.0052, _with_alpha(icon_color, a), 'CENTER',
                      max_width=min(width, radial.RING_OUTER - radial.RING_INNER))
        if item.children is not None and item.dial is None:
            # A chevron on the outer edge: it opens.
            tip = r1 - 0.0022
            base = tip - 0.0035
            ca, sa = math.cos(mid), math.sin(mid)
            w = 0.0028
            pts = [(cx + tip * ca, cy + tip * sa), (cx + base * ca - w * sa, cy + base * sa + w * ca),
                   (cx + base * ca + w * sa, cy + base * sa - w * ca)]
            _tris(pts, _with_alpha(DARK if hot else DIM, a), 0.0002)
        _draw_label(cx, cy, mid, item, hot or is_grabbed, a)

    _draw_center(pie, level, accent, alpha)


def _draw_label(cx, cy, mid, item, hot, alpha):
    ca, sa = math.cos(mid), math.sin(mid)
    x, y = cx + LABEL_R * ca, cy + LABEL_R * sa
    h = 0.0062 if hot else 0.0049
    align = 'LEFT' if ca > 0.25 else 'RIGHT' if ca < -0.25 else 'CENTER'
    if sa > 0.25 or align == 'CENTER' and sa > 0:
        y += 0.0008
    elif sa < -0.25 or align == 'CENTER':
        y -= h + 0.0012
    else:
        y -= h / 2
    if align == 'CENTER':
        y += 0.001 if sa > 0 else -0.001
    color = INK if hot else DIM
    if item.danger and hot:
        color = (*DANGER, 1.0)
    _text(x, y, item.text(), h, _with_alpha(color, alpha), align, max_width=0.075 if hot else 0.06)


def _draw_center(pie, level, accent, alpha):
    cx, cy = pie.center.x, pie.center.y
    grabbed = pie.grabbed
    _tris(_disc(cx, cy, CENTER_R), _with_alpha(DISC, alpha))
    hovered = pie.hovered()
    dial_item = grabbed if grabbed is not None else level.center if pie.hover == radial.CENTER else None
    if dial_item is not None:
        dial = dial_item.dial
        big = grabbed is not None
        r0, r1 = (CENTER_R + 0.0035, CENTER_R + 0.0075) if big else (CENTER_R - 0.0028, CENTER_R - 0.001)
        _gauge(cx, cy, r0, r1, dial.fraction(), accent, dial.gradient, alpha,
               ticks=dial.ticks() if dial.ticks is not None else ())
        _text(cx, cy - 0.0015, dial.text(), 0.0068 if big else 0.0056, _with_alpha(INK, alpha), 'CENTER',
              max_width=CENTER_R * 1.75)
        _text(cx, cy + 0.0052, dial.label, 0.0042, _with_alpha(DIM, alpha), 'CENTER', max_width=CENTER_R * 1.6)
        if not big:
            _text(cx, cy - 0.0095, "twist", 0.0036, _with_alpha(DIM, 0.8 * alpha), 'CENTER')
    elif hovered is not None:
        _text(cx, cy - 0.0022, hovered.label, 0.0056, _with_alpha(INK, alpha), 'CENTER', max_width=CENTER_R * 1.8)
    else:
        _text(cx, cy - 0.0022, level.title, 0.0052, _with_alpha(DIM, alpha), 'CENTER', max_width=CENTER_R * 1.8)
    if not level.items and level.empty:
        _text(cx, cy - CENTER_R - 0.009, level.empty, 0.0058, _with_alpha(INK, alpha), 'CENTER', max_width=0.14)
    if grabbed is None:
        # Where the hand (or stick) points.
        p = pie.pointer
        r = min(p.length, radial.RING_INNER - 0.003)
        d = p.normalized() * r if p.length > 1e-6 else Vector((0.0, 0.0))
        if p.length > radial.DEAD * 0.5:
            _tris(_disc(cx + d.x, cy + d.y, 0.0022, 12), _with_alpha(accent, alpha), 0.0003)
    if pie.sticky:
        _text(cx, cy - radial.RING_OUTER - 0.016, "trigger: choose", 0.0042, _with_alpha(DIM, alpha), 'CENTER')


# --- knob readout --------------------------------------------------------------------

def draw_knob(knob, matrix, color):
    """While a stick-click knob is held: a gauge with the value, by the hand (matrix: a card there)."""
    dial = knob.dial
    if dial is None:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    try:
        accent = (*color[:3], 1.0)
        _tris(_disc(0.0, 0.0, 0.019), DISC)
        _gauge(0.0, 0.0, 0.0205, 0.0245, dial.fraction(), accent, dial.gradient,
               ticks=dial.ticks() if dial.ticks is not None else ())
        _text(0.0, -0.0018, dial.text(), 0.0068, INK, 'CENTER', max_width=0.034)
        _text(0.0, 0.0055, dial.label, 0.0042, DIM, 'CENTER', max_width=0.03)
        if not knob.grabbed:
            _text(0.0, -0.034, "twist to change", 0.0045, DIM, 'CENTER')
    finally:
        gpu.matrix.pop()
        gpu.state.blend_set('NONE')


# --- status at the right hand ------------------------------------------------------------

BADGE_R = 0.0105
TITLE_SECONDS = 1.6


def draw_status(matrix, icon, color, ring_color, title, recording=None, title_alpha=0.0, toast=None):
    """A small round badge with the tool's icon (in the tool's color, ringed in the mode's color).
    title_alpha > 0 shows the tool's name beside it (it fades out after a change); a toast (text, color,
    alpha) takes its place. recording: None, 'REC' or a countdown number, drawn in red."""
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    try:
        _tris(_disc(0.0, 0.0, BADGE_R + 0.0016), (*ring_color[:3], 0.95))
        _tris(_disc(0.0, 0.0, BADGE_R), DISC, 0.0001)
        if recording is not None:
            pulse = 0.6 + 0.4 * math.sin(time.monotonic() * 6.0)
            _tris(_disc(0.0, 0.0, BADGE_R * 0.55), (1.0, 0.25, 0.25, pulse), 0.0002)
            _text(BADGE_R + 0.004, -0.0035, str(recording), 0.0075, (1.0, 0.35, 0.35, 1.0))
            return
        if not icons.draw(icon, 0.0, 0.0, BADGE_R * 0.62, (*color[:3], 1.0), 0.0002):
            _tris(_disc(0.0, 0.0, BADGE_R * 0.45), (*color[:3], 1.0), 0.0002)
        if toast is not None:
            text, tcolor, a = toast
            _label_pill(text, (*tcolor[:3], 1.0), a)
        elif title_alpha > 0.0 and title:
            _label_pill(title, INK, title_alpha)
    finally:
        gpu.matrix.pop()
        gpu.state.blend_set('NONE')


def _label_pill(text, color, alpha):
    h = 0.0062
    w = hud.text_width(text, h)
    x0 = BADGE_R + 0.004
    _tris([(x0 - 0.002, -0.006), (x0 + w + 0.003, -0.006), (x0 + w + 0.003, 0.006),
           (x0 - 0.002, -0.006), (x0 + w + 0.003, 0.006), (x0 - 0.002, 0.006)], _with_alpha(DISC, 0.85 * alpha))
    _text(x0, -h / 2, text, h, _with_alpha(color, alpha))


# --- hints -----------------------------------------------------------------------------

LEFT_HINTS = (("Y", "Modes", "tap: last mode"), ("X", "Time", "tap: play / stop"),
              ("Stick click", "Amount knob", "hold + twist"), ("Stick", "Move", ""), ("Trigger", "Do it", "opposite"))
RIGHT_HINTS = (("B", "Tool", "tap: quick action"), ("A", "Object", "tap: again"),
               ("Stick click", "Size knob", "hold + twist"), ("Stick", "Turn", ""), ("Trigger", "Do it", ""))


def draw_hints(matrix, rows, alpha, right_aligned=False):
    """A short list of what each button does, beside a controller."""
    if alpha <= 0.0:
        return
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    try:
        line = 0.0105
        h = 0.0062
        width = 0.105
        x0 = -width if right_aligned else 0.0
        top = line * len(rows) / 2
        _tris([(x0 - 0.004, -top - 0.004), (x0 + width, -top - 0.004), (x0 + width, top + 0.003),
               (x0 - 0.004, -top - 0.004), (x0 + width, top + 0.003), (x0 - 0.004, top + 0.003)],
              _with_alpha(DISC, 0.8 * alpha))
        for i, (button, what, extra) in enumerate(rows):
            y = top - line * (i + 1) + 0.002
            _text(x0, y, button, h, _with_alpha((1.0, 0.82, 0.35, 1.0), alpha), max_width=0.034)
            _text(x0 + 0.036, y, what, h, _with_alpha(INK, alpha), max_width=0.03)
            if extra:
                _text(x0 + 0.067, y, extra, 0.0044, _with_alpha(DIM, alpha), max_width=0.036)
    finally:
        gpu.matrix.pop()
        gpu.state.blend_set('NONE')
