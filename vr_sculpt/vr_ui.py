"""The in-VR panel toolkit: widgets, layout, hit testing and drawing, in one visual language.

A panel is rows of widgets on a card. The hand panel also has an icon rail for the modes down its
left side. The main controller points at a panel (its aim ray) and the trigger presses or drags.
Layout and hit testing are plain math so they can be tested without a headset; drawing is in
draw_panel().

Widget kinds:
  label, section, title  text (not pressable)
  button     text and/or icon; active = filled with the accent
  toggle     on/off, icon and text; on = tinted, with an accent bar
  segment    one option of a segmented control (segments sharing a group draw as one track
             with a pill that slides to the chosen option)
  icon       an icon-only button (header actions, transport)
  select     label, value and a chevron; opens a picker sheet
  slider     value inside the track, dragged with the ray
  stepper    label, then - value + (holding repeats)
  swatch     a color
  tile       a thumbnail (a texture, or an icon without one) over a name; kit parts
  more       the disclosure row (More / Less)
  rail       a mode in the icon rail
"""

import math
import time

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import gfx, hud

# Panel space is meters in hand space; origin at the panel's bottom center, Y up.
WIDTH = 0.21  # the hand panel, rail included
RAIL_W = 0.03
RAIL_ITEM = 0.022
RAIL_GAP = 0.004
PAD = 0.007
GAP = 0.003
ROW_H = 0.022
LABEL_H = 0.013
HEADER_H = 0.024
SWATCH_H = 0.017
MORE_H = 0.015
TILE_H = 0.04
TILE_TEXT = 0.0085  # the name strip under a tile's thumbnail
CORNER = 0.004
CARD_CORNER = 0.008
SLIDER_INSET = 0.003  # the slider's ends: pointing this close to an end gives min / max
STEPPER_SPLIT = 0.42  # where a stepper's - / + area starts, as a fraction of its width
TEXT_H = 0.0068
SMALL_H = 0.0056
TITLE_H = 0.0088
ICON = 0.0118
RAY_LENGTH = 1.5  # meters, hand space

# A restrained palette: neutrals, one accent, red only on destructive actions, key yellow for keys.
COL_CARD = (0.066, 0.07, 0.082, 0.95)
COL_RAIL = (0.042, 0.045, 0.053, 0.95)
COL_EDGE = (1.0, 1.0, 1.0, 0.075)
COL_SURFACE = (0.152, 0.16, 0.182, 1.0)
COL_SURFACE_HOT = (0.215, 0.226, 0.258, 1.0)
COL_TRACK = (0.098, 0.103, 0.12, 1.0)
COL_TEXT = (0.94, 0.95, 0.97, 1.0)
COL_DIM = (0.6, 0.625, 0.68, 1.0)
COL_FAINT = (0.4, 0.42, 0.48, 1.0)
COL_ACCENT = (0.33, 0.6, 1.0, 1.0)
COL_DANGER = (1.0, 0.45, 0.45, 1.0)
COL_KEY = (1.0, 0.8, 0.28, 1.0)
WHITE = (1.0, 1.0, 1.0, 1.0)

# Animation time constants (seconds): hover eases in fast, out a little slower.
HOVER_IN = 0.045
HOVER_OUT = 0.11
SLIDE = 0.06
PULSE = 0.28


class Widget:
    """One cell of a panel; see the module docstring for the kinds."""

    def __init__(self, kind, id="", text="", *, active=False, danger=False, on_press=None,
                 value=0.0, min=0.0, max=1.0, integer=False, fmt=None, on_change=None, value_text="",
                 swatch=None, ticks=None, icon=None, group=None, dot=None, tip="", weight=1.0, fixed=None,
                 align='LEFT', image=None):
        self.kind, self.id, self.text = kind, id, text
        self.ticks = ticks or ()  # slider: marks at these fractions of the track (keyframes)
        self.value_text = value_text
        self.swatch = swatch  # (r, g, b): a color button
        self.active, self.danger, self.on_press = active, danger, on_press
        self.value, self.min, self.max, self.integer = value, min, max, integer
        self.fmt = fmt or ((lambda v: str(int(v))) if integer else (lambda v: f"{v:.3f}"))
        self.on_change = on_change
        self.icon, self.group, self.dot, self.tip = icon, group, dot, tip or text
        self.weight, self.fixed, self.align = weight, fixed, align
        self.image = image  # tile: anything with texture() -> GPUTexture or None, hashable
        self.icon_only = False  # drawn without its text (set while drawing)
        self.rect = (0.0, 0.0, 0.0, 0.0)

    def height(self):
        return {'label': LABEL_H, 'section': LABEL_H, 'title': HEADER_H, 'swatch': SWATCH_H, 'more': MORE_H,
                'rail': RAIL_ITEM, 'tile': TILE_H}.get(self.kind, ROW_H)

    def pressable(self):
        return self.kind not in {'label', 'section', 'title'}

    def contains(self, x, y):
        x0, y0, x1, y1 = self.rect
        return x0 <= x <= x1 and y0 <= y <= y1

    def fraction(self):
        span = self.max - self.min
        return 0.0 if span <= 0 else min(max((self.value - self.min) / span, 0.0), 1.0)

    def value_at(self, x):
        """Slider value for a pointer at panel x (clamped to the track)."""
        x0, _, x1, _ = self.rect
        inner = max(x1 - x0 - 2 * SLIDER_INSET, 1e-9)
        t = min(max((x - x0 - SLIDER_INSET) / inner, 0.0), 1.0)
        v = self.min + t * (self.max - self.min)
        return int(round(v)) if self.integer else v

    def step_at(self, x):
        """Stepper: -1 or +1 for a pointer over its - or + half, 0 over the label."""
        x0, _, x1, _ = self.rect
        start = x0 + (x1 - x0) * STEPPER_SPLIT
        if x < start:
            return 0
        return -1 if x < (start + x1) / 2 else 1

    def stepped(self, step):
        """The stepper's value one step along, clamped (None if it can't move)."""
        v = min(max(int(round(self.value)) + step, int(self.min)), int(self.max))
        return None if v == int(round(self.value)) else v


def label(text, weight=1.0, fixed=None, align='LEFT'):
    return Widget('label', text=text, weight=weight, fixed=fixed, align=align)


def section(text):
    return Widget('section', text=text)


def title(text, subtitle=""):
    return Widget('title', text=text, value_text=subtitle, weight=3.0)


def button(id, text, on_press, active=False, danger=False, swatch=None, icon=None, weight=1.0, tip=""):
    if swatch is not None:
        return swatch_button(id, swatch, on_press, active)
    return Widget('button', id, text, active=active, danger=danger, on_press=on_press, icon=icon, weight=weight,
                  tip=tip)


def toggle(id, text, on, on_press, icon=None, weight=1.0, tip=""):
    return Widget('toggle', id, text, active=on, on_press=on_press, icon=icon, weight=weight, tip=tip)


def segment(id, text, on_press, active, group, icon=None, dot=None, weight=1.0):
    return Widget('segment', id, text, active=active, on_press=on_press, group=group, icon=icon, dot=dot,
                  weight=weight)


def icon_button(id, icon, on_press, tip="", active=False, danger=False):
    return Widget('icon', id, "", active=active, danger=danger, on_press=on_press, icon=icon, tip=tip, fixed=ROW_H)


def select(id, text, value_text, on_press, icon=None, weight=1.0):
    return Widget('select', id, text, on_press=on_press, value_text=value_text, icon=icon, weight=weight)


def slider(id, text, value, lo, hi, on_change, integer=False, fmt=None, ticks=None, weight=1.0):
    return Widget('slider', id, text, value=value, min=lo, max=hi, integer=integer, fmt=fmt,
                  on_change=on_change, ticks=ticks, weight=weight)


def stepper(id, text, value, lo, hi, on_change, fmt=None, weight=1.0):
    return Widget('stepper', id, text, value=value, min=lo, max=hi, integer=True, fmt=fmt, on_change=on_change,
                  weight=weight)


def swatch_button(id, rgb, on_press, active=False):
    return Widget('swatch', id, "", active=active, on_press=on_press, swatch=rgb)


def tile(id, text, on_press, image=None, icon=None, active=False):
    """A thumbnail over a name. image: see Widget.image; without one, icon is drawn instead."""
    return Widget('tile', id, text, active=active, on_press=on_press, image=image, icon=icon)


def tile_image_rect(rect):
    """Where a tile's thumbnail goes: a square above the name strip."""
    x0, y0, x1, y1 = rect
    side = min(x1 - x0, y1 - y0 - TILE_TEXT) - 0.004
    cx = (x0 + x1) / 2
    top = y1 - 0.002
    return (cx - side / 2, top - side, cx + side / 2, top)


def more(id, is_open, on_press, hidden=""):
    """The disclosure row. hidden: what it holds, shown dim while closed."""
    return Widget('more', id, "Less" if is_open else "More", active=is_open, on_press=on_press, value_text=hidden)


def rail(id, icon, on_press, active, tip):
    return Widget('rail', id, "", active=active, on_press=on_press, icon=icon, tip=tip)


class Panel:
    """What the hand panel shows: the mode rail and the content rows."""

    def __init__(self, rail_items, rows):
        self.rail, self.rows = rail_items, rows


# --- layout ------------------------------------------------------------------

def _row_height(row):
    return max((w.height() for w in row), default=ROW_H)


def _place_row(row, x0, x1, top, h):
    gaps = [0.0 if (a.group and a.group == b.group) else GAP for a, b in zip(row, row[1:])]
    fixed = sum(w.fixed for w in row if w.fixed)
    flex = max(x1 - x0 - fixed - sum(gaps), 0.0)
    weights = sum(w.weight for w in row if not w.fixed) or 1.0
    x = x0
    for i, w in enumerate(row):
        cw = w.fixed if w.fixed else flex * w.weight / weights
        w.rect = (x, top - h, x + cw, top)
        x += cw + (gaps[i] if i < len(gaps) else 0.0)


def _place_rows(rows, x0, x1, top):
    """Lay rows out downward from top; returns (widgets, height used)."""
    widgets = []
    y = top
    for i, row in enumerate(rows):
        h = _row_height(row)
        _place_row(row, x0, x1, y, h)
        widgets += row
        y -= h + (GAP if i < len(rows) - 1 else 0.0)
    return widgets, top - y


def layout(rows, width=WIDTH):
    """Assign rects to the widgets in rows (list of lists) on a plain card. Returns (widgets, height)."""
    heights = [_row_height(row) for row in rows]
    height = PAD * 2 + sum(heights) + GAP * max(len(rows) - 1, 0)
    widgets, _used = _place_rows(rows, -width / 2 + PAD, width / 2 - PAD, height - PAD)
    return widgets, height


def rail_height(n):
    return PAD * 2 + n * RAIL_ITEM + max(n - 1, 0) * RAIL_GAP


def layout_panel(panel, width=WIDTH):
    """The hand panel: content rows right of the rail, top-aligned; the rail sits at the bottom, so
    the mode icons stay put above the hand whatever the content's height. When the rail is the taller,
    a More row at the end sits at the bottom too, so opening it doesn't move it. Returns (widgets, height)."""
    heights = [_row_height(row) for row in panel.rows]
    content = PAD * 2 + sum(heights) + GAP * max(len(panel.rows) - 1, 0)
    height = max(content, rail_height(len(panel.rail)))
    x0, x1 = -width / 2 + RAIL_W + PAD, width / 2 - PAD
    rows = panel.rows
    if height > content + 1e-9 and len(rows) > 1 and any(w.kind == 'more' for w in rows[-1]):
        widgets, _used = _place_rows(rows[:-1], x0, x1, height - PAD)
        _place_row(rows[-1], x0, x1, PAD + heights[-1], heights[-1])
        widgets += rows[-1]
    else:
        widgets, _used = _place_rows(rows, x0, x1, height - PAD)
    x0 = -width / 2 + (RAIL_W - RAIL_ITEM) / 2
    y = rail_height(len(panel.rail)) - PAD
    for w in panel.rail:
        w.rect = (x0, y - RAIL_ITEM, x0 + RAIL_ITEM, y)
        y -= RAIL_ITEM + RAIL_GAP
    return list(panel.rail) + widgets, height


def hit_test(widgets, x, y):
    for w in widgets:
        if w.pressable() and w.contains(x, y):
            return w
    return None


def find(widgets, id):
    return next((w for w in widgets if w.id == id), None)


def panel_matrix(anchor, viewer, scale):
    return hud.billboard(anchor, viewer, scale)


def ray_hit(matrix, origin, direction, height, scale, width=WIDTH):
    """Panel-space (x, y) where the ray hits the panel, or None."""
    inv = matrix.inverted_safe()
    o = inv @ Vector(origin)
    d = inv.to_3x3() @ Vector(direction)
    if abs(d.z) < 1e-9:
        return None
    t = -o.z / d.z
    if t <= 0:
        return None
    # The matrix scales by `scale`, so panel-space distances are hand-space meters.
    if (d * t).length > RAY_LENGTH:
        return None
    p = o + d * t
    if -width / 2 <= p.x <= width / 2 and 0.0 <= p.y <= height:
        return (p.x, p.y)
    return None


def ray_plane_x(matrix, origin, direction):
    """Panel-space x where the ray crosses the panel's plane (no bounds), for slider drags."""
    inv = matrix.inverted_safe()
    o = inv @ Vector(origin)
    d = inv.to_3x3() @ Vector(direction)
    if abs(d.z) < 1e-9:
        return None
    t = -o.z / d.z
    return None if t <= 0 else (o + d * t).x


# --- motion ------------------------------------------------------------------

# Eased values per (widget id, channel): (value, time). Time-based, so the panel being drawn
# several times a frame (both eyes, the desktop view) doesn't speed anything up.
_motion = {}
_pulses = {}  # widget id -> when it was pressed
_moving = [0]  # eased values still on their way, while a panel is built


ANIMATE = True  # previews turn this off: every eased value is at its target at once


def ease(key, target, tau, now=None, start=None):
    now = time.monotonic() if now is None else now
    if not ANIMATE:
        _motion[key] = (target, now)
        return target
    v, t = _motion.get(key, (target if start is None else start, now))
    dt = min(max(now - t, 0.0), 0.1)
    v = target + (v - target) * math.exp(-dt / tau)
    if abs(v - target) < 1e-4:
        v = target
    else:
        _moving[0] += 1  # The panel being built isn't settled: build it again next frame.
    _motion[key] = (v, now)
    return v


def pulse(id):
    """Start the press animation of a widget."""
    _pulses[id] = time.monotonic()


def _pulse_level(id, now):
    t = _pulses.get(id) if ANIMATE else None
    if t is None:
        return 0.0
    k = (now - t) / PULSE
    _moving[0] += 1
    if k >= 1.0:
        del _pulses[id]
        return 0.0
    return (1.0 - k) ** 2


# --- drawing -------------------------------------------------------------------

MIN_TEXT_SCALE = 0.75


def fit_text(text, max_width, height=TEXT_H):
    """(text, height) that fits max_width: shrink a little, then cut with an ellipsis."""
    width = hud.text_width(text, height)
    if width <= max_width:
        return text, height
    h = max(height * max_width / max(width, 1e-9), height * MIN_TEXT_SCALE)
    if hud.text_width(text, h) <= max_width:
        return text, h
    while len(text) > 1 and hud.text_width(text + "\u2026", h) > max_width:
        text = text[:-1]
    return text.rstrip() + "\u2026", h


class _Texts:
    """Text to draw after the shapes, all at once."""

    def __init__(self):
        self.items = []

    def add(self, x, y_mid, text, max_width, color, align='LEFT', height=TEXT_H):
        """Queue fitted text; returns its width."""
        if not text or max_width <= 0:
            return 0.0
        text, h = fit_text(text, max_width, height)
        w = hud.text_width(text, h)
        if align == 'CENTER':
            x += (max_width - w) / 2
        elif align == 'RIGHT':
            x += max_width - w
        self.items.append((x, y_mid - h / 2, text, h, color))
        return w

    def draw(self, matrix, alpha):
        for x, y, text, h, color in self.items:
            hud.draw_text(matrix, x, y, text, h, (color[0], color[1], color[2], color[3] * alpha))


def _inset(rect, d):
    x0, y0, x1, y1 = rect
    return (x0 + d, y0 + d, x1 - d, y1 - d)


def _with_alpha(c, a):
    return (c[0], c[1], c[2], c[3] * a)


def _content(cv, texts, w, rect, color, icon_color=None, size=TEXT_H, align='CENTER'):
    """An icon and/or text in rect; the text is dropped (icon only) when both don't fit."""
    x0, y0, x1, y1 = rect
    mid = (y0 + y1) / 2
    room = x1 - x0 - 0.004
    ic = ICON if w.kind not in {'segment'} else ICON * 0.9
    text = w.text
    if w.icon and text:
        tw = hud.text_width(text, size)
        if ic + 0.0025 + tw > room or getattr(w, "icon_only", False):
            text = ""  # icon only; the tooltip names it
    if w.icon and not text:
        gfx.icon(cv, w.icon, (x0 + x1) / 2, mid, ic, icon_color or color)
        return
    if not w.icon:
        if w.dot is not None:
            tw = min(hud.text_width(text, size), room - 0.006)
            sx = (x0 + x1) / 2 - (tw + 0.0055) / 2
            cv.disc(sx + 0.0018, mid, 0.0019, (*w.dot[:3], 1.0))
            texts.add(sx + 0.0055, mid, text, room - 0.0055, color, 'LEFT', size)
        else:
            texts.add(x0 + 0.002, mid, text, room, color, align, size)
        return
    tw = min(hud.text_width(text, size), room - ic - 0.0025)
    sx = (x0 + x1) / 2 - (ic + 0.0025 + tw) / 2
    gfx.icon(cv, w.icon, sx + ic / 2, mid, ic, icon_color or color)
    texts.add(sx + ic + 0.0025, mid, text, tw + 1e-6, color, 'LEFT', size)


def _groups(widgets):
    """Segment groups: {group: [x0, y0, x1, y1, active rect or None, text height]}. The text height is
    the one every option fits at, so a control's labels stay one size."""
    out = {}
    for w in widgets:
        if w.kind != 'segment':
            continue
        x0, y0, x1, y1 = w.rect
        room = x1 - x0 - 0.004 - (0.0055 if w.dot is not None else 0.0)
        tw = hud.text_width(w.text, TEXT_H) if w.text else 0.0
        if w.icon and tw and ICON * 0.9 + 0.0025 + tw <= room:
            room -= ICON * 0.9 + 0.0025
        size = TEXT_H * min(room / tw, 1.0) if tw else TEXT_H
        g = out.get(w.group)
        if g is None:
            out[w.group] = [x0, y0, x1, y1, w.rect if w.active else None, size]
        else:
            g[0], g[1], g[2], g[3] = min(g[0], x0), min(g[1], y0), max(g[2], x1), max(g[3], y1)
            g[5] = min(g[5], size)
            if w.active:
                g[4] = w.rect
    for g in out.values():
        g[5] = max(g[5], TEXT_H * MIN_TEXT_SCALE)
    return out


def draw_panel(matrix, widgets, height, hover_id=None, pressed_id=None, title=None, width=WIDTH, alpha=1.0,
               pointer=None):
    """Draw a panel. hover_id / pressed_id: the widget under the ray / held by the trigger; alpha fades
    the whole panel; pointer: panel-space (x, y) of the ray, for the cursor dot and steppers.

    The panel's triangles and text are built in panel space, so they don't change as the hand moves:
    they're kept and drawn again (both eyes, every frame) until a widget looks different or an
    animation is running."""
    key = _signature(widgets, height, width, hover_id, pressed_id, pointer)
    built = _built.get(key)
    if built is None or not built[4]:
        _moving[0] = 0
        cv, texts, over, over_texts, images = _build(widgets, height, width, hover_id, pressed_id, pointer)
        if len(_built) > 32:
            _built.clear()
        built = _built[key] = (cv, texts, over, over_texts, _moving[0] == 0, images)
    cv, texts, over, over_texts, _settled, images = built
    cursor = gfx.Canvas(0.0006)
    if pointer is not None:
        px, py = pointer
        cursor.ring(px, py, 0.0017, 0.0007, (1.0, 1.0, 1.0, 0.95))
        cursor.disc(px, py, 0.0006, (1.0, 1.0, 1.0, 0.95))

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    cv.draw(alpha)
    _draw_images(images, alpha)
    gpu.matrix.pop()
    texts.draw(matrix, alpha)
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    over.draw(alpha)
    cursor.draw(alpha)
    gpu.matrix.pop()
    over_texts.draw(matrix, alpha)
    gpu.state.blend_set('NONE')


_built = {}  # panel signature -> (canvas, texts, overlay canvas, overlay texts, settled, images)


def _draw_images(images, alpha):
    """Tile thumbnails: [image, rect, batch], the batch made on first draw. An image whose texture
    isn't ready yet (they load a few per frame) is skipped this time."""
    if not images:
        return
    shader = gpu.shader.from_builtin('IMAGE_COLOR')
    for item in images:
        tex = item[0].texture()
        if tex is None:
            continue
        if item[2] is None:
            x0, y0, x1, y1 = item[1]
            z = 0.0002
            item[2] = batch_for_shader(shader, 'TRI_FAN', {
                "pos": ((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)),
                "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        shader.uniform_sampler("image", tex)
        shader.uniform_float("color", (1.0, 1.0, 1.0, alpha))
        item[2].draw(shader)


def _signature(widgets, height, width, hover_id, pressed_id, pointer):
    """Everything a panel's look depends on (but where the ray's cursor is)."""
    sig = [round(height, 6), width, hover_id, pressed_id]
    for w in widgets:
        value = w.fmt(w.value) if w.kind in {'slider', 'stepper'} else None
        sig.append((w.kind, w.id, w.text, w.value_text, w.active, w.danger, w.icon, w.group, w.rect, value,
                    round(w.fraction(), 4) if w.kind == 'slider' else None, tuple(w.ticks),
                    tuple(w.swatch) if w.swatch is not None else None, tuple(w.dot) if w.dot is not None else None,
                    w.image))
        if w.kind == 'stepper' and w.id == hover_id and pointer is not None:
            sig.append(w.step_at(pointer[0]))
    return tuple(sig)


def _build(widgets, height, width, hover_id, pressed_id, pointer):
    now = time.monotonic()
    cv = gfx.Canvas()
    texts = _Texts()
    over = gfx.Canvas(0.0004)
    over_texts = _Texts()
    images = []
    x0, x1 = -width / 2, width / 2
    has_rail = any(w.kind == 'rail' for w in widgets)

    # The card, its rail, and a hairline edge (crisp against any background).
    cv.rect(x0, 0.0, x1, height, COL_EDGE, CARD_CORNER)
    if has_rail:
        cv.rect(x0 + 0.0006, 0.0006, x0 + RAIL_W, height - 0.0006, COL_RAIL, CARD_CORNER - 0.0006, (1, 0, 0, 1))
        cv.rect(x0 + RAIL_W, 0.0006, x1 - 0.0006, height - 0.0006, COL_CARD, CARD_CORNER - 0.0006, (0, 1, 1, 0))
    else:
        cv.rect(x0 + 0.0006, 0.0006, x1 - 0.0006, height - 0.0006, COL_CARD, CARD_CORNER - 0.0006)

    groups = _groups(widgets)
    for group, (gx0, gy0, gx1, gy1, active_rect, _size) in groups.items():
        cv.rect(gx0, gy0, gx1, gy1, COL_TRACK, CORNER)
        shown = ease((group, "on"), 1.0 if active_rect else 0.0, SLIDE, now, 0.0)
        if active_rect is not None:
            px0 = ease((group, "x0"), active_rect[0], SLIDE, now)
            px1 = ease((group, "x1"), active_rect[2], SLIDE, now)
        else:
            px0, _ = _motion.get((group, "x0"), (gx0, now))
            px1, _ = _motion.get((group, "x1"), (gx0, now))
        if shown > 0.01:
            cv.rect(px0 + 0.0016, gy0 + 0.0016, px1 - 0.0016, gy1 - 0.0016, _with_alpha(COL_ACCENT, shown),
                    CORNER - 0.001)

    rail_items = [w for w in widgets if w.kind == 'rail']
    active_rail = next((w for w in rail_items if w.active), None)
    if active_rail is not None:
        # The current mode: a short accent bar on the rail's edge that slides between modes.
        ry0 = ease(("rail", "y0"), active_rail.rect[1], SLIDE, now)
        ry1 = ease(("rail", "y1"), active_rail.rect[3], SLIDE, now)
        cv.rect(x0 + 0.0009, ry0 + 0.005, x0 + 0.0025, ry1 - 0.005, COL_ACCENT, 0.0008)

    # A row whose buttons don't all fit icon and text shows them all as icons (one look per row).
    tight = {round(w.rect[3], 5) for w in widgets if w.kind in {'button', 'toggle'} and w.icon and w.text and
             hud.text_width(w.text, TEXT_H) + ICON + 0.0065 > w.rect[2] - w.rect[0]}
    for w in widgets:
        w.icon_only = bool(w.icon and w.text and w.kind in {'button', 'toggle'} and round(w.rect[3], 5) in tight)

    for w in widgets:
        hot = bool(w.id) and w.id == hover_id
        held = bool(w.id) and w.id == pressed_id
        h = ease((w.id, "hover"), 1.0 if (hot or held) else 0.0, HOVER_IN if (hot or held) else HOVER_OUT, now, 0.0) \
            if w.id else 0.0
        q = max(_pulse_level(w.id, now), 1.0 if held else 0.0) if w.id else 0.0
        rect = _inset(w.rect, 0.0007 * q)
        size = groups[w.group][5] if w.kind == 'segment' else TEXT_H
        _draw_widget(cv, texts, w, rect, h, q, now, pointer if hot else None, size)
        if w.kind == 'tile' and w.image is not None:
            images.append([w.image, tile_image_rect(rect), None])
        cut_short = w.kind == 'tile' and hud.text_width(w.text, SMALL_H) > w.rect[2] - w.rect[0] - 0.003
        if h > 0.02 and w.tip and (w.kind in {'rail', 'icon'} or w.icon_only or cut_short):
            _tooltip(over, over_texts, w, h, x0)
    return cv, texts, over, over_texts, images


def _tooltip(cv, texts, w, h, panel_x0):
    tw = hud.text_width(w.tip, SMALL_H)
    pad = 0.0025
    bw, bh = tw + 2 * pad, SMALL_H + 2 * pad + 0.001
    x0, y0, x1, y1 = w.rect
    if w.kind == 'rail':
        # Left of the panel, level with the icon.
        bx1 = panel_x0 - 0.003
        bx0, by0 = bx1 - bw, (y0 + y1) / 2 - bh / 2
    else:
        bx0, by0 = (x0 + x1) / 2 - bw / 2, y1 + 0.003
    a = min(h * 1.4, 1.0)
    cv.rect(bx0, by0, bx0 + bw, by0 + bh, (0.02, 0.022, 0.028, 0.94 * a), 0.003)
    texts.add(bx0 + pad, by0 + bh / 2, w.tip, tw + 1e-6, _with_alpha(COL_TEXT, a), 'LEFT', SMALL_H)


def _draw_widget(cv, texts, w, rect, h, q, now, pointer, size=TEXT_H):
    x0, y0, x1, y1 = rect
    mid = (y0 + y1) / 2
    k = w.kind
    if k == 'label':
        texts.add(x0 + 0.001, mid, w.text, x1 - x0 - 0.001, COL_DIM, w.align, SMALL_H if w.fixed else TEXT_H)
    elif k == 'section':
        tw = texts.add(x0 + 0.001, mid, w.text.upper(), x1 - x0, COL_FAINT, 'LEFT', SMALL_H * 0.92)
        if x0 + tw + 0.004 < x1:
            cv.rect(x0 + tw + 0.004, mid - 0.00025, x1, mid + 0.00025, (1.0, 1.0, 1.0, 0.07))
    elif k == 'title':
        tw = texts.add(x0 + 0.001, mid, w.text, x1 - x0, COL_TEXT, 'LEFT', TITLE_H)
        if w.value_text:
            texts.add(x0 + tw + 0.0045, mid - 0.0003, w.value_text, x1 - x0 - tw - 0.0045, COL_DIM, 'LEFT', SMALL_H)
    elif k == 'rail':
        on = ease((w.id, "on"), 1.0 if w.active else 0.0, SLIDE, now, 0.0)
        bg = gfx.mix((1.0, 1.0, 1.0, 0.0), (1.0, 1.0, 1.0, 0.07), h)
        bg = gfx.mix(bg, _with_alpha(COL_ACCENT, 0.2), on)
        if bg[3] > 0.003:
            cv.rect(x0, y0, x1, y1, bg, CORNER + 0.001)
        if q > 0:
            cv.rect(x0, y0, x1, y1, (1.0, 1.0, 1.0, 0.18 * q), CORNER + 0.001)
        color = gfx.mix(gfx.mix(COL_DIM, COL_TEXT, h), COL_ACCENT, on)
        gfx.icon(cv, w.icon, (x0 + x1) / 2, mid, ICON * 1.08, color)
    elif k == 'segment':
        if not w.active and h > 0.01:
            cv.rect(x0 + 0.0016, y0 + 0.0016, x1 - 0.0016, y1 - 0.0016, (1.0, 1.0, 1.0, 0.06 * h), CORNER - 0.001)
        if q > 0:
            cv.rect(x0 + 0.0016, y0 + 0.0016, x1 - 0.0016, y1 - 0.0016, (1.0, 1.0, 1.0, 0.2 * q), CORNER - 0.001)
        color = WHITE if w.active else gfx.mix(COL_DIM, COL_TEXT, h)
        _content(cv, texts, w, rect, color, size=size)
    elif k in {'button', 'toggle'}:
        on = ease((w.id, "on"), 1.0 if w.active else 0.0, SLIDE, now, 0.0) if k == 'toggle' else 0.0
        if k == 'button' and w.active:
            bg = gfx.mix(COL_ACCENT, (0.5, 0.72, 1.0, 1.0), h)
        elif w.danger:
            bg = gfx.mix(COL_SURFACE, (0.3, 0.12, 0.13, 1.0), h)
        else:
            bg = gfx.mix(COL_SURFACE, COL_SURFACE_HOT, h)
            bg = gfx.mix(bg, gfx.mix(bg, COL_ACCENT, 0.3), on)
        cv.rect(x0, y0, x1, y1, bg, CORNER)
        if q > 0:
            cv.rect(x0, y0, x1, y1, (1.0, 1.0, 1.0, 0.2 * q), CORNER)
        if k == 'toggle' and on > 0.01:
            bw = (x1 - x0) * 0.32
            cx = (x0 + x1) / 2
            cv.rect(cx - bw / 2, y0 + 0.0014, cx + bw / 2, y0 + 0.0026, _with_alpha(COL_ACCENT, on), 0.0006)
        if k == 'button' and w.active:
            color = icon_color = WHITE
        elif w.danger:
            color = icon_color = gfx.mix(gfx.mix(COL_DIM, COL_DANGER, 0.4), COL_DANGER, h)
        elif k == 'toggle':
            color = gfx.mix(COL_DIM, COL_TEXT, max(on, h))
            icon_color = gfx.mix(color, COL_ACCENT, on)
        else:
            color = icon_color = COL_TEXT
        _content(cv, texts, w, (x0, y0 + (0.0008 if k == 'toggle' else 0.0), x1, y1), color, icon_color)
    elif k == 'icon':
        on = ease((w.id, "on"), 1.0 if w.active else 0.0, SLIDE, now, 0.0)
        bg = gfx.mix((1.0, 1.0, 1.0, 0.0), (1.0, 1.0, 1.0, 0.08), h)
        bg = gfx.mix(bg, _with_alpha(COL_ACCENT, 0.22), on)
        r = min(x1 - x0, y1 - y0) / 2
        if bg[3] > 0.003:
            cv.rect(x0, y0, x1, y1, bg, r)
        if q > 0:
            cv.rect(x0, y0, x1, y1, (1.0, 1.0, 1.0, 0.2 * q), r)
        if w.danger:
            color = gfx.mix(COL_DIM, COL_DANGER, h)
        else:
            color = gfx.mix(gfx.mix(COL_DIM, COL_TEXT, h), COL_ACCENT, on)
        gfx.icon(cv, w.icon, (x0 + x1) / 2, mid, ICON, color)
    elif k == 'select':
        cv.rect(x0, y0, x1, y1, gfx.mix(COL_SURFACE, COL_SURFACE_HOT, h), CORNER)
        if q > 0:
            cv.rect(x0, y0, x1, y1, (1.0, 1.0, 1.0, 0.2 * q), CORNER)
        gfx.icon(cv, "chevron_right", x1 - 0.0062, mid, 0.0072, gfx.mix(COL_DIM, COL_TEXT, h), 0.16)
        lx = x0 + 0.004
        if w.icon:
            gfx.icon(cv, w.icon, lx + ICON / 2, mid, ICON * 0.9, COL_DIM)
            lx += ICON + 0.002
        room = x1 - 0.012 - lx
        vw = texts.add(lx + room * 0.35, mid, w.value_text, room * 0.65, COL_TEXT, 'RIGHT')
        texts.add(lx, mid, w.text, room - vw - 0.003, COL_DIM)
    elif k == 'slider':
        _draw_slider(cv, texts, w, rect, h, q, now)
    elif k == 'stepper':
        _draw_stepper(cv, texts, w, rect, h, q, pointer)
    elif k == 'swatch':
        r = min(x1 - x0, y1 - y0) / 2 - 0.0016
        r *= 1.0 + 0.12 * h - 0.08 * q
        cx = (x0 + x1) / 2
        if w.active:
            cv.ring(cx, mid, r + 0.0017, 0.0008, COL_TEXT)
        cv.disc(cx, mid, r, (*w.swatch[:3], 1.0))
    elif k == 'tile':
        cv.rect(x0, y0, x1, y1, gfx.mix(COL_SURFACE, COL_SURFACE_HOT, h), CORNER)
        if q > 0:
            cv.rect(x0, y0, x1, y1, (1.0, 1.0, 1.0, 0.2 * q), CORNER)
        if w.active:
            cv.frame(x0, y0, x1, y1, 0.0012, COL_ACCENT, CORNER)
        if w.image is None:
            ix0, iy0, ix1, iy1 = tile_image_rect(rect)
            gfx.icon(cv, w.icon or "object", (ix0 + ix1) / 2, (iy0 + iy1) / 2, ICON * 1.3, COL_FAINT)
        color = COL_TEXT if w.active else gfx.mix(COL_DIM, COL_TEXT, h)
        texts.add(x0 + 0.0015, y0 + TILE_TEXT / 2 + 0.0008, w.text, x1 - x0 - 0.003, color, 'CENTER', SMALL_H)
    elif k == 'more':
        cv.rect(x0, y1 - 0.00025, x1, y1 + 0.00025, (1.0, 1.0, 1.0, 0.06))
        color = gfx.mix(COL_FAINT, COL_TEXT, h)
        text = w.text if w.active or not w.value_text else f"{w.text}  \u00b7  {w.value_text}"
        tw = min(hud.text_width(text, SMALL_H), x1 - x0 - 0.012)
        cx = (x0 + x1) / 2
        texts.add(cx - (tw + 0.008) / 2, mid, text, tw + 1e-6, color, 'LEFT', SMALL_H)
        # The rows open upward (the panel grows away from the hand), so the chevron points up.
        gfx.icon(cv, "chevron_down" if w.active else "chevron_up", cx + tw / 2 + 0.002, mid, 0.0062, color, 0.17)


def _draw_slider(cv, texts, w, rect, h, q, now):
    x0, y0, x1, y1 = rect
    mid = (y0 + y1) / 2
    cv.rect(x0, y0, x1, y1, gfx.mix(COL_TRACK, COL_SURFACE, 0.5 + 0.5 * h), CORNER)
    t = ease((w.id, "value"), w.fraction(), 0.05, now) if w.id else w.fraction()
    fx = x0 + SLIDER_INSET + (x1 - x0 - 2 * SLIDER_INSET) * t
    if t >= 0.999:
        fx = x1
    if fx - x0 > 0.0005:
        cv.rect(x0, y0, fx, y1, gfx.mix(gfx.mix(COL_TRACK, COL_ACCENT, 0.42), gfx.mix(COL_TRACK, COL_ACCENT, 0.6), h),
                CORNER, (1, 1 if t >= 0.999 else 0, 1 if t >= 0.999 else 0, 1))
    if t < 0.999 and max(h, q) > 0.01:
        # Pointed at or dragged: the fill's edge lights up, as nubs above and below the text.
        hx = max(fx, x0 + 0.0008)
        c = _with_alpha(gfx.mix(COL_ACCENT, WHITE, 0.3), max(h, q))
        cv.rect(hx - 0.0005, y1 - 0.0045, hx + 0.0005, y1 - 0.0012, c, 0.0004)
        cv.rect(hx - 0.0005, y0 + 0.0012, hx + 0.0005, y0 + 0.0045, c, 0.0004)
    inner_w = x1 - x0 - 2 * SLIDER_INSET
    for tk in w.ticks:
        tx = x0 + SLIDER_INSET + inner_w * min(max(tk, 0.0), 1.0)
        d = 0.0016
        cv.quad((tx, y0 + 0.0008), (tx + d, y0 + 0.0008 + d), (tx, y0 + 0.0008 + 2 * d), (tx - d, y0 + 0.0008 + d),
                COL_KEY)
    room = x1 - x0 - 0.008
    vw = texts.add(x0 + 0.004 + room * 0.4, mid, w.fmt(w.value), room * 0.6, COL_TEXT, 'RIGHT')
    texts.add(x0 + 0.004, mid, w.text, room - vw - 0.004, gfx.mix(COL_DIM, COL_TEXT, 0.25))


def _draw_stepper(cv, texts, w, rect, h, q, pointer):
    x0, y0, x1, y1 = rect
    mid = (y0 + y1) / 2
    cv.rect(x0, y0, x1, y1, gfx.mix(COL_TRACK, COL_SURFACE, 0.5 + 0.5 * h), CORNER)
    start = x0 + (x1 - x0) * STEPPER_SPLIT
    zone = w.step_at(pointer[0]) if pointer is not None else 0
    s = 0.0068
    r = (y1 - y0) / 2 - 0.0022
    for step, cx, icon, can in ((-1, start + r + 0.0012, "minus", w.value > w.min),
                                (1, x1 - r - 0.0022, "plus", w.value < w.max)):
        # Each half of the control is its button; the round one under the pointer lights up.
        on = zone == step and can
        cv.disc(cx, mid, r, gfx.mix(COL_SURFACE, COL_SURFACE_HOT, 1.0 if on else 0.0) if can else COL_TRACK)
        if on and q > 0:
            cv.disc(cx, mid, r, (1.0, 1.0, 1.0, 0.2 * q))
        gfx.icon(cv, icon, cx, mid, s, COL_TEXT if can else COL_FAINT, 0.24)
    vx0, vx1 = start + 2 * r + 0.003, x1 - 2 * r - 0.004
    texts.add(vx0, mid, w.fmt(w.value), vx1 - vx0, COL_TEXT, 'CENTER')
    texts.add(x0 + 0.004, mid, w.text, start - x0 - 0.006, gfx.mix(COL_DIM, COL_TEXT, 0.25))


def draw_ghost(matrix, icon, alpha, hot):
    """What's left of a hidden panel: a small disc with the mode icon, where the panel shows."""
    h = ease(("ghost", "hover"), 1.0 if hot else 0.0, HOVER_IN if hot else HOVER_OUT, None, 0.0)
    cv = gfx.Canvas()
    r = GHOST_R * (1.0 + 0.12 * h)
    cv.disc(0.0, GHOST_Y, r + 0.0007, COL_EDGE)
    cv.disc(0.0, GHOST_Y, r, COL_CARD)
    if h > 0.01:
        cv.ring(0.0, GHOST_Y, r, 0.0009, _with_alpha(COL_ACCENT, h))
    gfx.icon(cv, icon, 0.0, GHOST_Y, r * 1.05, gfx.mix(COL_DIM, COL_TEXT, h))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    cv.draw(alpha)
    gpu.matrix.pop()
    gpu.state.blend_set('NONE')


GHOST_R = 0.011  # drawn radius (hand-space meters)
GHOST_Y = 0.02  # its center above the panel's anchor
GHOST_HIT = 0.026  # pointing this close to its center counts: easier to hit than it looks


def ghost_hit(matrix, origin, direction):
    """Whether the ray points at the hidden panel's ghost."""
    inv = matrix.inverted_safe()
    o = inv @ Vector(origin)
    d = inv.to_3x3() @ Vector(direction)
    if abs(d.z) < 1e-9:
        return False
    t = -o.z / d.z
    if t <= 0 or (d * t).length > RAY_LENGTH:
        return False
    p = o + d * t
    return math.hypot(p.x, p.y - GHOST_Y) <= GHOST_HIT


def draw_ray(origin, end, hit):
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    a = 0.9 if hit else 0.35
    batch = batch_for_shader(shader, 'LINES', {"pos": [origin, end],
                                               "color": [(*COL_ACCENT[:3], 0.05), (*COL_ACCENT[:3], a)]})
    batch.draw(shader)
    gpu.state.blend_set('NONE')


def lift(matrix, alpha):
    """The panel's matrix while it fades in or out: it rises a little and grows into place."""
    if alpha >= 0.999:
        return matrix
    k = 1.0 - alpha
    return matrix @ Matrix.Translation((0.0, -0.012 * k * k, 0.0)) @ Matrix.Scale(1.0 - 0.05 * k, 4)
