"""A small in-VR panel toolkit: rows of buttons, sliders and labels on a floating card.

The panel floats above the left controller, facing the headset. The right
controller points at it (its aim ray) and the trigger presses buttons or drags
sliders. Layout and hit testing are plain math so they can be tested without a
headset; drawing is in draw_panel().
"""

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import hud

# Panel space is meters in hand space; origin at the panel's bottom center, Y up.
WIDTH = 0.18
PAD = 0.006
GAP = 0.004
ROW_H = 0.024
LABEL_H = 0.016
TEXT_H = 0.0085
RAY_LENGTH = 1.5  # meters, hand space

COL_BG = (0.06, 0.06, 0.07, 0.88)
COL_BUTTON = (0.19, 0.19, 0.21, 1.0)
COL_ACTIVE = (0.24, 0.45, 0.85, 1.0)
COL_DANGER = (0.6, 0.2, 0.22, 1.0)
COL_TRACK = (0.14, 0.14, 0.16, 1.0)
COL_TEXT = (1.0, 1.0, 1.0, 1.0)
COL_DIM = (0.7, 0.7, 0.74, 1.0)


class Widget:
    """One cell of the panel. kind: 'label', 'button' or 'slider'."""

    def __init__(self, kind, id="", text="", *, active=False, danger=False, on_press=None,
                 value=0.0, min=0.0, max=1.0, integer=False, fmt=None, on_change=None):
        self.kind, self.id, self.text = kind, id, text
        self.active, self.danger, self.on_press = active, danger, on_press
        self.value, self.min, self.max, self.integer = value, min, max, integer
        self.fmt = fmt or ((lambda v: str(int(v))) if integer else (lambda v: f"{v:.3f}"))
        self.on_change = on_change
        self.rect = (0.0, 0.0, 0.0, 0.0)

    def contains(self, x, y):
        x0, y0, x1, y1 = self.rect
        return x0 <= x <= x1 and y0 <= y <= y1

    def fraction(self):
        span = self.max - self.min
        return 0.0 if span <= 0 else min(max((self.value - self.min) / span, 0.0), 1.0)

    def value_at(self, x):
        """Slider value for a pointer at panel x (clamped to the track)."""
        x0, _, x1, _ = self.rect
        inner = max(x1 - x0 - 2 * PAD, 1e-9)
        t = min(max((x - x0 - PAD) / inner, 0.0), 1.0)
        v = self.min + t * (self.max - self.min)
        return int(round(v)) if self.integer else v


def label(text):
    return Widget('label', text=text)


def button(id, text, on_press, active=False, danger=False):
    return Widget('button', id, text, active=active, danger=danger, on_press=on_press)


def slider(id, text, value, lo, hi, on_change, integer=False, fmt=None):
    return Widget('slider', id, text, value=value, min=lo, max=hi, integer=integer, fmt=fmt,
                  on_change=on_change)


def layout(rows):
    """Assign rects to the widgets in rows (list of lists). Returns (widgets, height)."""
    heights = [LABEL_H if all(w.kind == 'label' for w in row) else ROW_H for row in rows]
    height = PAD * 2 + sum(heights) + GAP * max(len(rows) - 1, 0)
    y = height - PAD
    widgets = []
    for row, h in zip(rows, heights):
        n = len(row)
        cell = (WIDTH - 2 * PAD - GAP * (n - 1)) / max(n, 1)
        x = -WIDTH / 2 + PAD
        for w in row:
            w.rect = (x, y - h, x + cell, y)
            widgets.append(w)
            x += cell + GAP
        y -= h + GAP
    return widgets, height


def hit_test(widgets, x, y):
    for w in widgets:
        if w.kind != 'label' and w.contains(x, y):
            return w
    return None


def find(widgets, id):
    return next((w for w in widgets if w.id == id), None)


def panel_matrix(anchor, viewer, scale):
    return hud.billboard(anchor, viewer, scale)


def ray_hit(matrix, origin, direction, height, scale):
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
    if -WIDTH / 2 <= p.x <= WIDTH / 2 and 0.0 <= p.y <= height:
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


# --- drawing -------------------------------------------------------------

def _quad(shader, x0, y0, x1, y1, color, z=0.0):
    pts = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": pts}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", color)
    batch.draw(shader)


def _lighter(c, amount=0.08):
    return (min(c[0] + amount, 1.0), min(c[1] + amount, 1.0), min(c[2] + amount, 1.0), c[3])


def draw_panel(matrix, widgets, height, hover_id=None, pressed_id=None, title=None):
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    _quad(shader, -WIDTH / 2, 0.0, WIDTH / 2, height, COL_BG)
    for w in widgets:
        x0, y0, x1, y1 = w.rect
        hot = w.id and (w.id == hover_id or w.id == pressed_id)
        if w.kind == 'button':
            c = COL_DANGER if w.danger else (COL_ACTIVE if w.active else COL_BUTTON)
            _quad(shader, x0, y0, x1, y1, _lighter(c, 0.14 if w.id == pressed_id else 0.08) if hot else c, 0.0002)
        elif w.kind == 'slider':
            _quad(shader, x0, y0, x1, y1, _lighter(COL_TRACK) if hot else COL_TRACK, 0.0002)
            _quad(shader, x0, y0, x0 + (x1 - x0) * w.fraction(), y0 + 0.003, COL_ACTIVE, 0.0003)
        if hot:
            # A clear outline: subtle color changes are hard to see in a headset.
            pts = [(x0, y0, 0.0004), (x1, y0, 0.0004), (x1, y1, 0.0004), (x0, y1, 0.0004)]
            batch = batch_for_shader(shader, 'LINE_LOOP', {"pos": pts})
            shader.uniform_float("color", (1.0, 1.0, 1.0, 0.95))
            batch.draw(shader)
    gpu.matrix.pop()

    for w in widgets:
        x0, y0, x1, y1 = w.rect
        ty = y0 + (y1 - y0 - TEXT_H) / 2
        if w.kind == 'label':
            hud.draw_text(matrix, x0 + 0.002, ty, w.text, TEXT_H, COL_DIM)
        elif w.kind == 'button':
            tw = hud.text_width(w.text, TEXT_H)
            hud.draw_text(matrix, (x0 + x1 - tw) / 2, ty, w.text, TEXT_H, COL_TEXT)
        elif w.kind == 'slider':
            hud.draw_text(matrix, x0 + PAD, ty + 0.0015, w.text, TEXT_H, COL_DIM)
            value = w.fmt(w.value)
            hud.draw_text(matrix, x1 - PAD - hud.text_width(value, TEXT_H), ty + 0.0015, value, TEXT_H, COL_TEXT)
    gpu.state.blend_set('NONE')


def draw_ray(origin, end, hit):
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    batch = batch_for_shader(shader, 'LINES', {"pos": [origin, end]})
    shader.uniform_float("color", (0.55, 0.75, 1.0, 0.9 if hit else 0.35))
    batch.draw(shader)
    gpu.state.blend_set('NONE')
