"""Flat 2D drawing for the hand panel: a triangle batch with per-vertex colors, and vector icons.

Everything is drawn in a plane (panel space, meters) and collected into one batch, so a whole
panel is one draw call. Icons are drawn from line and fill primitives in a unit box; there are no
image assets.
"""

import math

import gpu
from gpu_extras.batch import batch_for_shader


class Canvas:
    """Collects triangles (x, y) with RGBA colors; draw() sends them in one batch, in order."""

    def __init__(self, z=0.0):
        self.pos = []
        self.col = []
        self.z = z
        self._batch = None

    def tri(self, a, b, c, color):
        z = self.z
        self.pos += [(a[0], a[1], z), (b[0], b[1], z), (c[0], c[1], z)]
        self.col += [color, color, color]

    def quad(self, a, b, c, d, color):
        self.tri(a, b, c, color)
        self.tri(a, c, d, color)

    def fan(self, center, pts, color, closed=True):
        n = len(pts)
        for i in range(n if closed else n - 1):
            self.tri(center, pts[i], pts[(i + 1) % n], color)

    def rect(self, x0, y0, x1, y1, color, r=0.0, corners=(1, 1, 1, 1)):
        """A rectangle, rounded by r at the corners flagged in corners (bottom left, bottom right,
        top right, top left)."""
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        if r <= 1e-6 or not any(corners):
            self.quad((x0, y0), (x1, y0), (x1, y1), (x0, y1), color)
            return
        self.fan(((x0 + x1) / 2, (y0 + y1) / 2), rounded_outline(x0, y0, x1, y1, r, corners), color)

    def frame(self, x0, y0, x1, y1, w, color, r=0.0):
        """A rectangle's outline, w wide, inside the rectangle."""
        outer = rounded_outline(x0, y0, x1, y1, r) if r > 0 else [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        ri = max(r - w, 0.0)
        inner = (rounded_outline(x0 + w, y0 + w, x1 - w, y1 - w, ri) if ri > 0 else
                 [(x0 + w, y0 + w), (x1 - w, y0 + w), (x1 - w, y1 - w), (x0 + w, y1 - w)])
        if len(inner) != len(outer):
            inner = _resample(inner, len(outer))
        n = len(outer)
        for i in range(n):
            j = (i + 1) % n
            self.quad(outer[i], outer[j], inner[j], inner[i], color)

    def disc(self, cx, cy, r, color, seg=None):
        pts = [(cx + r * c, cy + r * s) for c, s in _unit_circle(seg or _segments(r))]
        self.fan((cx, cy), pts, color)

    def ring(self, cx, cy, r, w, color, a0=0.0, a1=2 * math.pi, seg=None):
        """A band between radius r - w/2 and r + w/2, from angle a0 to a1."""
        seg = seg or max(int(_segments(r) * abs(a1 - a0) / (2 * math.pi)), 3)
        ri, ro = r - w / 2, r + w / 2
        for i in range(seg):
            t0 = a0 + (a1 - a0) * i / seg
            t1 = a0 + (a1 - a0) * (i + 1) / seg
            c0, s0, c1, s1 = math.cos(t0), math.sin(t0), math.cos(t1), math.sin(t1)
            self.quad((cx + ri * c0, cy + ri * s0), (cx + ro * c0, cy + ro * s0),
                      (cx + ro * c1, cy + ro * s1), (cx + ri * c1, cy + ri * s1), color)

    def line(self, p, q, w, color, caps=True):
        dx, dy = q[0] - p[0], q[1] - p[1]
        n = math.hypot(dx, dy)
        if n < 1e-9:
            return
        ox, oy = -dy / n * w / 2, dx / n * w / 2
        self.quad((p[0] + ox, p[1] + oy), (p[0] - ox, p[1] - oy), (q[0] - ox, q[1] - oy), (q[0] + ox, q[1] + oy),
                  color)
        if caps:
            self.disc(p[0], p[1], w / 2, color, 6)
            self.disc(q[0], q[1], w / 2, color, 6)

    def lines(self, pts, w, color, closed=False):
        n = len(pts)
        for i in range(n if closed else n - 1):
            self.line(pts[i], pts[(i + 1) % n], w, color, caps=False)
        # Round joints and ends.
        for x, y in pts:
            self.disc(x, y, w / 2, color, 6)

    def draw(self, alpha=1.0):
        if not self.pos:
            return
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        if alpha >= 1.0:
            # Opaque: the batch is kept for the next draw (canvases are kept while nothing changes).
            batch = self._batch
            if batch is None:
                batch = self._batch = batch_for_shader(shader, 'TRIS', {"pos": self.pos, "color": self.col})
            batch.draw(shader)
            return
        cols = [(c[0], c[1], c[2], c[3] * alpha) for c in self.col]
        batch_for_shader(shader, 'TRIS', {"pos": self.pos, "color": cols}).draw(shader)


_circles = {}


def _unit_circle(seg):
    pts = _circles.get(seg)
    if pts is None:
        pts = _circles[seg] = [(math.cos(2 * math.pi * i / seg), math.sin(2 * math.pi * i / seg)) for i in range(seg)]
    return pts


def _segments(r):
    # Hand-space meters: enough segments to look round at arm's length, few for small dots.
    return 10 if r < 0.002 else 16 if r < 0.006 else 28


_ARCS = [[(math.cos(a0 + 0.5 * math.pi * i / 5), math.sin(a0 + 0.5 * math.pi * i / 5)) for i in range(6)]
         for a0 in (math.pi, 1.5 * math.pi, 0.0, 0.5 * math.pi)]  # The four corners' quarter circles.


def rounded_outline(x0, y0, x1, y1, r, corners=(1, 1, 1, 1)):
    pts = []
    centers = ((x0 + r, y0 + r), (x1 - r, y0 + r), (x1 - r, y1 - r), (x0 + r, y1 - r))
    sharp = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    for (cx, cy), arc, on, corner in zip(centers, _ARCS, corners, sharp):
        if on:
            pts += [(cx + r * c, cy + r * s) for c, s in arc]
        else:
            pts += [corner] * len(arc)
    return pts


def _resample(pts, n):
    return [pts[int(i * len(pts) / n)] for i in range(n)]


def mix(a, b, t):
    """Blend two RGBA colors."""
    return tuple(x + (y - x) * t for x, y in zip(a, b))


# --- icons -------------------------------------------------------------------

class Pen:
    """Draws an icon's unit box ([-1, 1] on both axes, Y up) into a canvas at (cx, cy), half size s."""

    def __init__(self, canvas, cx, cy, s, color, width=0.13):
        self.c, self.cx, self.cy, self.s, self.color, self.w = canvas, cx, cy, s, color, width * s

    def p(self, x, y):
        return (self.cx + x * self.s, self.cy + y * self.s)

    def line(self, x0, y0, x1, y1, w=None):
        self.c.line(self.p(x0, y0), self.p(x1, y1), self.w if w is None else w * self.s, self.color)

    def path(self, pts, closed=False, w=None):
        self.c.lines([self.p(x, y) for x, y in pts], self.w if w is None else w * self.s, self.color, closed)

    def fill(self, pts):
        q = [self.p(x, y) for x, y in pts]
        cx = sum(x for x, _ in q) / len(q)
        cy = sum(y for _, y in q) / len(q)
        self.c.fan((cx, cy), q, self.color)

    def disc(self, x, y, r):
        self.c.disc(*self.p(x, y), r * self.s, self.color)

    def circle(self, x, y, r, a0=0.0, a1=2 * math.pi, w=None):
        self.c.ring(*self.p(x, y), r * self.s, self.w if w is None else w * self.s, self.color, a0, a1)

    def box(self, x0, y0, x1, y1, r=0.0, fill=False):
        if fill:
            self.c.rect(*self.p(x0, y0), *self.p(x1, y1), self.color, r * self.s)
        else:
            self.c.frame(*self.p(x0 - self.w / self.s / 2, y0 - self.w / self.s / 2),
                         *self.p(x1 + self.w / self.s / 2, y1 + self.w / self.s / 2), self.w, self.color,
                         (r + self.w / self.s / 2) * self.s if r else 0.0)


def _arc_pts(cx, cy, r, a0, a1, n=12):
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def _arrow_head(pen, x, y, angle, size=0.32):
    """Two strokes back from (x, y), for an arrow pointing along angle."""
    for da in (2.5, -2.5):
        pen.line(x, y, x + size * math.cos(angle + da), y + size * math.sin(angle + da))


def _tri(pen, x, y, size, angle):
    pen.fill([(x + size * math.cos(angle + k * 2 * math.pi / 3), y + size * math.sin(angle + k * 2 * math.pi / 3))
              for k in range(3)])


def _sculpt(p):
    # A ball with a dent pressed into it, and the tool that pressed it.
    main = _arc_pts(0.0, 0.0, 0.78, math.radians(69.6), math.radians(380.4), 22)
    dent = _arc_pts(0.69, 0.69, 0.42, math.radians(275.6), math.radians(174.4), 6)
    p.path(main + dent[1:-1], closed=True)
    p.disc(0.69, 0.69, 0.15)


def _model(p):
    hexagon = [(0, 0.86), (0.76, 0.43), (0.76, -0.43), (0, -0.86), (-0.76, -0.43), (-0.76, 0.43)]
    p.path(hexagon, closed=True)
    p.line(0, 0, -0.76, 0.43)
    p.line(0, 0, 0.76, 0.43)
    p.line(0, 0, 0, -0.86)


def _draw(p):
    pts = [(-0.82 + 1.5 * t / 16, 0.42 * math.sin(2 * math.pi * t / 16) * (1 - 0.25 * t / 16) - 0.1) for t in range(17)]
    p.path(pts)
    p.disc(0.72, 0.38, 0.17)


def _paint(p):
    p.line(0.78, 0.78, 0.08, 0.08, 0.16)
    p.fill([(0.2, -0.04), (-0.05, 0.22), (-0.45, -0.05), (-0.82, -0.82), (-0.05, -0.45)])


def _pose(p):
    # A figure mid-move.
    p.disc(0.05, 0.66, 0.19)
    p.line(0.0, 0.38, 0.05, -0.2)
    p.path([(-0.6, 0.62), (-0.32, 0.3), (0.02, 0.26), (0.4, 0.2), (0.62, -0.1)])
    p.path([(-0.45, -0.88), (-0.25, -0.5), (0.05, -0.2), (0.3, -0.55), (0.55, -0.9)])


def _tools(p):
    # A ruler, tilted.
    c, s = math.cos(math.radians(35)), math.sin(math.radians(35))

    def rot(x, y):
        return (x * c - y * s, x * s + y * c)
    p.path([rot(-0.95, -0.3), rot(0.95, -0.3), rot(0.95, 0.3), rot(-0.95, 0.3)], closed=True)
    for i, x in enumerate((-0.55, -0.15, 0.25, 0.65)):
        p.line(*rot(x, 0.3), *rot(x, 0.3 - (0.32 if i % 2 else 0.2)))


def _pin(p):
    p.box(-0.42, 0.18, 0.42, 0.82, 0.12)
    p.line(-0.7, 0.18, 0.7, 0.18)
    p.line(0, 0.18, 0, -0.85)


def _pinned(p):
    p.box(-0.42, 0.18, 0.42, 0.82, 0.12, fill=True)
    p.line(-0.7, 0.18, 0.7, 0.18)
    p.line(0, 0.18, 0, -0.85)


def _gear(p):
    p.circle(0, 0, 0.47, w=0.26)
    for i in range(8):
        a = 2 * math.pi * i / 8
        c, s = math.cos(a), math.sin(a)
        p.fill([(0.5 * c - 0.15 * s, 0.5 * s + 0.15 * c), (0.88 * c - 0.12 * s, 0.88 * s + 0.12 * c),
                (0.88 * c + 0.12 * s, 0.88 * s - 0.12 * c), (0.5 * c + 0.15 * s, 0.5 * s - 0.15 * c)])


def _chevron(angle):
    def icon(p):
        c, s = math.cos(angle), math.sin(angle)
        pts = [(-0.28, 0.55), (0.28, 0.0), (-0.28, -0.55)]
        p.path([(x * c - y * s, x * s + y * c) for x, y in pts])
    return icon


def _undo(p):
    pts = _arc_pts(0.08, -0.12, 0.6, math.radians(160), math.radians(-70), 14)
    p.path(pts)
    x, y = pts[0]
    p.line(x, y, x - 0.05, y + 0.42)
    p.line(x, y, x + 0.4, y + 0.12)


def _redo(p):
    pts = _arc_pts(-0.08, -0.12, 0.6, math.radians(20), math.radians(250), 14)
    p.path(pts)
    x, y = pts[0]
    p.line(x, y, x + 0.05, y + 0.42)
    p.line(x, y, x - 0.4, y + 0.12)


def _trash(p):
    p.line(-0.72, 0.52, 0.72, 0.52)
    p.line(-0.22, 0.52, -0.16, 0.76)
    p.line(-0.16, 0.76, 0.16, 0.76)
    p.line(0.16, 0.76, 0.22, 0.52)
    p.path([(-0.52, 0.52), (-0.42, -0.82), (0.42, -0.82), (0.52, 0.52)])
    p.line(-0.13, 0.25, -0.1, -0.55)
    p.line(0.13, 0.25, 0.1, -0.55)


def _duplicate(p):
    p.box(-0.8, -0.8, 0.3, 0.3, 0.14)
    p.path([(-0.3, 0.52), (-0.3, 0.8), (0.8, 0.8), (0.8, -0.3), (0.52, -0.3)])


def _plus(p):
    p.line(-0.65, 0, 0.65, 0)
    p.line(0, -0.65, 0, 0.65)


def _minus(p):
    p.line(-0.65, 0, 0.65, 0)


def _close(p):
    p.line(-0.55, -0.55, 0.55, 0.55)
    p.line(-0.55, 0.55, 0.55, -0.55)


def _check(p):
    p.path([(-0.7, 0.0), (-0.2, -0.5), (0.72, 0.5)])


def _eye(p):
    p.path(_arc_pts(0, -0.75, 1.2, math.radians(38), math.radians(142)))
    p.path(_arc_pts(0, 0.75, 1.2, math.radians(-38), math.radians(-142)))
    p.disc(0, 0, 0.26)


def _grid(p):
    for x in (-0.6, 0.0, 0.6):
        for y in (-0.6, 0.0, 0.6):
            p.disc(x, y, 0.14)


def _magnet(p):
    p.path([(-0.55, 0.75), (-0.55, -0.05)] + _arc_pts(0, -0.05, 0.55, math.pi, 2 * math.pi, 10)[1:-1] +
           [(0.55, -0.05), (0.55, 0.75)], w=0.3)
    p.line(-0.72, 0.45, -0.38, 0.45, 0.12)
    p.line(0.38, 0.45, 0.72, 0.45, 0.12)


def _surface(p):
    p.line(-0.9, -0.62, 0.9, -0.62)
    p.box(-0.35, -0.35, 0.35, 0.35, 0.06)
    p.line(0, 0.55, 0, 0.9, 0.1)


def _angle(p):
    p.line(-0.7, -0.6, 0.85, -0.6)
    p.line(-0.7, -0.6, 0.55, 0.62)
    p.path(_arc_pts(-0.7, -0.6, 0.85, 0.0, math.radians(43), 6))


def _mirror(p):
    for y in (-0.8, -0.3, 0.2, 0.7):
        p.line(0, y, 0, y + 0.22)
    p.fill([(-0.2, -0.55), (-0.85, 0.0), (-0.2, 0.55)])
    p.path([(0.2, -0.55), (0.85, 0.0), (0.2, 0.55)], closed=True, w=0.1)


def _here(p):
    p.circle(0, 0, 0.5)
    for a in range(4):
        c, s = math.cos(a * math.pi / 2), math.sin(a * math.pi / 2)
        p.line(0.62 * c, 0.62 * s, 0.92 * c, 0.92 * s)
    p.disc(0, 0, 0.14)


def _pen(p):
    c, s = math.cos(math.radians(45)), math.sin(math.radians(45))

    def rot(x, y):
        return (x * c - y * s, x * s + y * c)
    p.path([rot(-0.35, 0.18), rot(0.95, 0.18), rot(0.95, -0.18), rot(-0.35, -0.18)], closed=True)
    p.fill([rot(-0.35, 0.18), rot(-0.35, -0.18), rot(-0.85, 0.0)])


def _edit(p):
    pts = [((1 - t) ** 3 * -0.8 + 3 * (1 - t) ** 2 * t * -0.4 + 3 * (1 - t) * t * t * 0.3 + t ** 3 * 0.8,
            (1 - t) ** 3 * -0.6 + 3 * (1 - t) ** 2 * t * 0.9 + 3 * (1 - t) * t * t * -0.9 + t ** 3 * 0.55)
           for t in (i / 16 for i in range(17))]
    p.path(pts)
    p.line(-0.45, 0.62, 0.35, -0.62, 0.07)
    p.box(-0.58, 0.48, -0.32, 0.76, fill=True)
    p.box(0.22, -0.76, 0.48, -0.48, fill=True)


def _eraser(p):
    c, s = math.cos(math.radians(40)), math.sin(math.radians(40))

    def rot(x, y):
        return (x * c - y * s + 0.05, x * s + y * c + 0.1)
    p.path([rot(-0.75, -0.32), rot(0.75, -0.32), rot(0.75, 0.32), rot(-0.75, 0.32)], closed=True)
    p.fill([rot(-0.75, -0.32), rot(-0.1, -0.32), rot(-0.1, 0.32), rot(-0.75, 0.32)])
    p.line(-0.9, -0.85, 0.3, -0.85, 0.09)


def _tube(p):
    pts = [(-0.75 + 1.5 * t / 12, 0.3 * math.sin(math.pi * t / 6)) for t in range(13)]
    p.path(pts, w=0.46)


def _ribbon(p):
    a = [(-0.85 + 1.7 * t / 16, 0.4 * math.sin(2 * math.pi * t / 16)) for t in range(17)]
    b = [(x, -y) for x, y in a]
    p.path(a, w=0.1)
    p.path(b, w=0.1)
    p.line(-0.85, 0.0, -0.85, 0.0)


def _note(p):
    p.path([(-0.7, -0.8), (-0.7, 0.8), (0.75, 0.8), (0.75, -0.3), (0.25, -0.8)], closed=True)
    p.line(-0.38, 0.38, 0.42, 0.38, 0.1)
    p.line(-0.38, 0.02, 0.42, 0.02, 0.1)
    p.line(-0.38, -0.34, 0.05, -0.34, 0.1)


def _eyedropper(p):
    c, s = math.cos(math.radians(-45)), math.sin(math.radians(-45))

    def rot(x, y):
        return (x * c - y * s, x * s + y * c)
    p.line(*rot(-0.3, 0.55), *rot(-0.3, -0.85), 0.24)
    p.line(*rot(-0.62, 0.55), *rot(0.02, 0.55), 0.16)
    p.line(*rot(-0.3, 0.6), *rot(-0.3, 0.95), 0.4)
    p.line(*rot(-0.3, -0.85), *rot(-0.3, -1.08), 0.08)


def _swap(p):
    p.line(-0.75, 0.32, 0.7, 0.32)
    _arrow_head(p, 0.75, 0.32, 0.0, 0.3)
    p.line(0.75, -0.32, -0.7, -0.32)
    _arrow_head(p, -0.75, -0.32, math.pi, 0.3)


def _spray(p):
    p.box(-0.9, -0.3, -0.35, 0.3, 0.08, fill=True)
    for x, y in ((0.0, 0.0), (0.3, 0.32), (0.3, -0.32), (0.62, 0.62), (0.62, 0.0), (0.62, -0.62), (0.9, 0.3),
                 (0.9, -0.3)):
        p.disc(x, y, 0.09)


def _contact(p):
    p.line(-0.9, -0.62, 0.9, -0.62)
    p.line(0.0, 0.9, 0.0, -0.12, 0.2)
    p.disc(0.0, -0.32, 0.22)


def _record(p):
    p.disc(0, 0, 0.6)


def _play(p):
    p.fill([(-0.45, -0.68), (0.72, 0.0), (-0.45, 0.68)])


def _stop(p):
    p.box(-0.55, -0.55, 0.55, 0.55, 0.1, fill=True)


def _frame_prev(p):
    p.fill([(0.42, -0.6), (-0.48, 0.0), (0.42, 0.6)])


def _frame_next(p):
    p.fill([(-0.42, -0.6), (0.48, 0.0), (-0.42, 0.6)])


def _key_prev(p):
    p.line(-0.72, -0.62, -0.72, 0.62, 0.18)
    p.fill([(0.68, -0.62), (-0.42, 0.0), (0.68, 0.62)])


def _key_next(p):
    p.line(0.72, -0.62, 0.72, 0.62, 0.18)
    p.fill([(-0.68, -0.62), (0.42, 0.0), (-0.68, 0.62)])


def _key(p):
    p.fill([(0, 0.78), (0.6, 0), (0, -0.78), (-0.6, 0)])


def _key_delete(p):
    p.path([(0, 0.78), (0.6, 0), (0, -0.78), (-0.6, 0)], closed=True)
    p.line(-0.85, -0.85, 0.85, 0.85, 0.1)


def _range_start(p):
    p.path([(-0.2, 0.8), (-0.6, 0.8), (-0.6, -0.8), (-0.2, -0.8)])
    p.fill([(-0.1, -0.45), (0.75, 0.0), (-0.1, 0.45)])


def _range_end(p):
    p.path([(0.2, 0.8), (0.6, 0.8), (0.6, -0.8), (0.2, -0.8)])
    p.fill([(0.1, -0.45), (-0.75, 0.0), (0.1, 0.45)])


def _paths(p):
    pts = _arc_pts(0.0, -1.1, 1.5, math.radians(140), math.radians(40), 8)
    for x, y in pts:
        p.disc(x, y, 0.08)
    p.fill([(0, 0.62), (0.22, 0.4), (0, 0.18), (-0.22, 0.4)])


def _onion(p):
    p.circle(-0.38, 0, 0.42, w=0.08)
    p.circle(0.0, 0, 0.42, w=0.1)
    p.circle(0.38, 0, 0.42)


def _ruler(p):
    _tools(p)


def _camera(p):
    p.box(-0.82, -0.55, 0.82, 0.48, 0.12)
    p.path([(-0.35, 0.48), (-0.22, 0.75), (0.22, 0.75), (0.35, 0.48)])
    p.circle(0, -0.04, 0.3)


def _light(p):
    # A light bulb: the glass, its neck and the screw base.
    p.path(_arc_pts(0.0, 0.25, 0.55, math.radians(-50), math.radians(230), 18))
    p.line(-0.35, -0.17, -0.25, -0.45)
    p.line(0.35, -0.17, 0.25, -0.45)
    p.line(-0.27, -0.58, 0.27, -0.58)
    p.line(-0.18, -0.8, 0.18, -0.8)


def _rays(p, r0, r1, n=8, a0=0.0):
    for k in range(n):
        a = a0 + 2 * math.pi * k / n
        p.line(r0 * math.cos(a), r0 * math.sin(a), r1 * math.cos(a), r1 * math.sin(a))


def _light_point(p):
    p.disc(0.0, 0.0, 0.3)
    _rays(p, 0.5, 0.85, 8, math.pi / 8)


def _light_sun(p):
    p.circle(0.0, 0.0, 0.36)
    _rays(p, 0.58, 0.88)


def _light_spot(p):
    # A lamp head throwing a cone of light down.
    p.box(-0.22, 0.45, 0.22, 0.8, 0.06, fill=True)
    p.line(-0.22, 0.45, -0.75, -0.65)
    p.line(0.22, 0.45, 0.75, -0.65)
    p.path(_arc_pts(0.0, -0.65, 0.75, math.radians(200), math.radians(340), 8))


def _light_area(p):
    # A panel, and the light coming off its face.
    p.box(-0.75, 0.4, 0.75, 0.75, 0.05, fill=True)
    for x in (-0.5, 0.0, 0.5):
        p.line(x, 0.12, x, -0.8)


def _material(p):
    # A shaded ball: its outline, a highlight, the shadow side.
    p.circle(0.0, 0.0, 0.8)
    p.disc(-0.3, 0.3, 0.17)
    p.path(_arc_pts(0.0, 0.0, 0.5, math.radians(-80), math.radians(10), 8))


def _look(p):
    # A ball in the light of a small sun.
    p.circle(-0.22, -0.22, 0.58)
    p.fill(_arc_pts(-0.22, -0.22, 0.58, math.radians(225), math.radians(405), 12))
    p.disc(0.62, 0.62, 0.15)
    _rays_at(p, 0.62, 0.62, 0.24, 0.36)


def _rays_at(p, cx, cy, r0, r1, n=6):
    for k in range(n):
        a = 2 * math.pi * k / n
        p.line(cx + r0 * math.cos(a), cy + r0 * math.sin(a), cx + r1 * math.cos(a), cy + r1 * math.sin(a), 0.09)


def _world(p):
    # A globe.
    p.circle(0.0, 0.0, 0.8)
    p.path([(0.38 * math.cos(a), 0.8 * math.sin(a)) for a in (math.pi * k / 12 - math.pi / 2 for k in range(13))])
    p.line(-0.8, 0.0, 0.8, 0.0)
    p.line(-0.68, 0.42, 0.68, 0.42, 0.1)
    p.line(-0.68, -0.42, 0.68, -0.42, 0.1)


def _walk(p):
    p.fill([(-0.55, -0.15), (-0.2, -0.15), (-0.15, 0.55), (-0.38, 0.8), (-0.6, 0.55)])
    p.disc(-0.38, -0.42, 0.17)
    p.fill([(0.2, -0.55), (0.55, -0.55), (0.6, 0.15), (0.38, 0.4), (0.15, 0.15)])
    p.disc(0.38, -0.82, 0.15)


def _fly(p):
    p.fill([(-0.85, 0.05), (0.85, 0.65), (0.1, -0.75)])
    p.line(0.85, 0.65, -0.12, -0.15, 0.1)


def _desktop(p):
    p.box(-0.85, -0.4, 0.85, 0.72, 0.08)
    p.line(0, -0.42, 0, -0.72)
    p.line(-0.4, -0.75, 0.4, -0.75)


def _passthrough(p):
    p.path([(-0.85, 0.42), (0.85, 0.42), (0.85, -0.42), (0.18, -0.42), (0, -0.15), (-0.18, -0.42), (-0.85, -0.42)],
           closed=True)
    p.disc(-0.45, 0.02, 0.15)
    p.disc(0.45, 0.02, 0.15)


def _timeline(p):
    p.line(-0.85, -0.3, 0.85, -0.3)
    for x in (-0.6, -0.2, 0.2, 0.6):
        p.line(x, -0.3, x, -0.5, 0.08)
    p.line(0.1, -0.75, 0.1, 0.55, 0.12)
    p.fill([(-0.12, 0.85), (0.32, 0.85), (0.1, 0.55)])


def _tracking(p):
    p.circle(0, 0, 0.72, w=0.1)
    p.circle(0, 0, 0.38, w=0.1)
    p.disc(0, 0, 0.12)


def _fk(p):
    p.disc(-0.45, -0.45, 0.2)
    p.line(-0.45, -0.45, 0.45, 0.35, 0.2)
    pts = _arc_pts(-0.45, -0.45, 0.9, math.radians(5), math.radians(80), 8)
    p.path(pts, w=0.1)
    _arrow_head(p, *pts[-1], math.radians(170), 0.25)


def _ik(p):
    joints = [(-0.75, -0.55), (-0.05, 0.25), (0.45, -0.3)]
    p.path(joints, w=0.16)
    p.disc(-0.75, -0.55, 0.16)
    p.disc(-0.05, 0.25, 0.16)
    p.circle(0.55, -0.42, 0.28, w=0.09)


def _bones(p):
    p.line(-0.5, -0.5, 0.5, 0.5, 0.22)
    for x, y in ((-0.55, -0.72), (-0.72, -0.55), (0.55, 0.72), (0.72, 0.55)):
        p.disc(x, y, 0.2)


def _rig(p):
    # A bone chain with a branch, joints as dots.
    joints = [(-0.62, -0.72), (-0.05, -0.08), (0.55, 0.68)]
    p.path(joints, w=0.14)
    p.line(-0.05, -0.08, 0.72, -0.42, 0.14)
    for x, y in joints + [(0.72, -0.42)]:
        p.disc(x, y, 0.17)


def _retopo(p):
    # A quad grid hugging a curved surface.
    def bulge(x, y):
        return (x, y + 0.22 * (1.0 - x * x))
    for y in (-0.7, -0.05, 0.6):
        p.path([bulge(-0.85 + 1.7 * t / 8, y) for t in range(9)])
    for x in (-0.85, -0.28, 0.28, 0.85):
        p.path([bulge(x, -0.7 + 1.3 * t / 4) for t in range(5)])


def _strip(p):
    # A strip of quads along a curve: two rails and the rungs between them.
    def rail(off):
        return [(-0.85 + 1.7 * t / 10, off + 0.35 * math.sin(math.pi * t / 10) - 0.15) for t in range(11)]
    a, b = rail(-0.35), rail(0.35)
    p.path(a)
    p.path(b)
    for i in (0, 3, 7, 10):
        p.line(*a[i], *b[i])


def _quad(p):
    # A quad being drawn: three corners placed, the fourth at the pen.
    pts = [(-0.72, -0.62), (0.62, -0.72), (0.72, 0.62), (-0.62, 0.72)]
    p.path(pts[:3])
    p.path([pts[2], pts[3], pts[0]], w=0.07)
    for x, y in pts[:3]:
        p.disc(x, y, 0.15)
    p.circle(*pts[3], 0.2, w=0.09)


def _relax(p):
    # An even grid of points inside a brush ring.
    p.circle(0.0, 0.0, 0.85, w=0.08)
    for v in (-0.42, 0.0, 0.42):
        p.line(-0.42, v, 0.42, v, 0.06)
        p.line(v, -0.42, v, 0.42, 0.06)
    for x in (-0.42, 0.0, 0.42):
        for y in (-0.42, 0.0, 0.42):
            p.disc(x, y, 0.11)


def _tweak(p):
    # A vertex pulled up out of its edges, and the arrow of the pull.
    p.path([(-0.85, -0.65), (0.0, -0.05), (0.85, -0.65)])
    p.disc(0.0, -0.05, 0.18)
    p.line(0.0, 0.2, 0.0, 0.85)
    _arrow_head(p, 0.0, 0.88, math.pi / 2, 0.28)


def _wrap(p):
    # A mesh line pulled down onto a curved surface.
    p.path(_arc_pts(0.0, -1.25, 1.35, math.radians(50), math.radians(130), 12))
    for x in (-0.5, 0.0, 0.5):
        p.line(x, 0.85, x, 0.4)
        _arrow_head(p, x, 0.34, -math.pi / 2, 0.22)


def _weights(p):
    # Rising bars: an amount.
    for x0, top in ((-0.8, -0.2), (-0.2, 0.25), (0.4, 0.75)):
        p.box(x0, -0.75, x0 + 0.4, top, 0.05, fill=True)


def _smooth(p):
    # A wave dying down to flat.
    p.path([(-0.85 + 1.7 * t / 20, 0.5 * math.sin(2 * math.pi * 1.5 * t / 20) * (1 - t / 20)) for t in range(21)])


def _bind(p):
    c, s = math.cos(math.radians(-40)), math.sin(math.radians(-40))

    def rot(x, y):
        return (x * c - y * s, x * s + y * c)
    p.path([rot(-0.9, -0.22), rot(-0.1, -0.22), rot(-0.1, 0.22), rot(-0.9, 0.22)], closed=True)
    p.path([rot(0.1, -0.22), rot(0.9, -0.22), rot(0.9, 0.22), rot(0.1, 0.22)], closed=True)
    p.line(*rot(-0.35, 0), *rot(0.35, 0))


def _reset(p):
    pts = _arc_pts(0, 0, 0.62, math.radians(110), math.radians(110 + 300), 18)
    p.path(pts)
    _arrow_head(p, *pts[-1], math.radians(110 + 300 + 90), 0.3)


def _cursor(p):
    p.fill([(-0.55, 0.85), (-0.55, -0.55), (-0.22, -0.25), (0.05, -0.85), (0.25, -0.75), (0.0, -0.15),
            (0.45, -0.15)])


def _object(p):
    p.box(-0.62, -0.62, 0.62, 0.62, 0.1)


def _cut(p):
    p.box(-0.8, -0.8, 0.3, 0.3, 0.08, fill=True)
    p.circle(0.3, 0.3, 0.48, w=0.1)


def _union(p):
    p.box(-0.8, -0.8, 0.3, 0.3, 0.08, fill=True)
    p.disc(0.3, 0.3, 0.52)


def _intersect(p):
    p.box(-0.8, -0.8, 0.3, 0.3, 0.08)
    p.circle(0.3, 0.3, 0.48, w=0.1)
    lens = [(min(max(x, -0.8), 0.3), min(max(y, -0.8), 0.3))
            for x, y in _arc_pts(0.3, 0.3, 0.48, 0, 2 * math.pi, 24)]
    p.fill(lens)


def _bevel(p):
    p.path([(-0.75, -0.75), (-0.75, 0.75), (0.2, 0.75)] + _arc_pts(0.2, 0.2, 0.55, math.radians(90), 0, 6)[1:] +
           [(0.75, -0.75)], closed=True)


def _shell(p):
    p.box(-0.75, -0.75, 0.75, 0.75, 0.12)
    p.box(-0.38, -0.38, 0.38, 0.38, 0.05)


def _array(p):
    for x in (-0.6, 0.0, 0.6):
        p.box(x - 0.2, -0.2, x + 0.2, 0.2, 0.04, fill=True)


def _radial(p):
    for i in range(6):
        a = 2 * math.pi * i / 6
        p.disc(0.62 * math.cos(a), 0.62 * math.sin(a), 0.17)
    p.disc(0, 0, 0.08)


def _linear(p):
    _array(p)


def _shading(p):
    p.circle(0, 0, 0.75)
    p.fill(_arc_pts(0, 0, 0.75, math.radians(-90), math.radians(90), 12))


def _cube_add(p):
    _model(p)


def _nut(p, cx, cy, r):
    p.path([(cx + r * math.cos(math.pi / 6 + k * math.pi / 3), cy + r * math.sin(math.pi / 6 + k * math.pi / 3))
            for k in range(6)], closed=True)
    p.circle(cx, cy, r * 0.42)


def _kit(p):
    # A hex nut: hardware, parts.
    _nut(p, 0.0, 0.0, 0.8)


def _kit_add(p):
    _nut(p, -0.25, -0.25, 0.6)
    p.line(0.62, 0.3, 0.62, 0.94)
    p.line(0.3, 0.62, 0.94, 0.62)


def _stamp(p):
    # A rubber stamp over the copies it left.
    p.disc(0.0, 0.66, 0.2)
    p.line(0.0, 0.5, 0.0, 0.05)
    p.box(-0.55, -0.32, 0.55, 0.05, 0.08, fill=True)
    for x in (-0.6, 0.0, 0.6):
        p.disc(x, -0.72, 0.13)


def _physics(p):
    # A ball and the arc of its bounce.
    p.path(_arc_pts(-0.15, -0.55, 0.6, math.radians(180), math.radians(20), 10))
    p.line(-0.85, -0.82, 0.85, -0.82)
    p.disc(0.42, 0.25, 0.3)


def _throw(p):
    # A ball flying off, with speed lines behind it.
    p.disc(0.42, 0.2, 0.36)
    for y, x0 in ((0.45, -0.55), (0.2, -0.85), (-0.05, -0.55)):
        p.line(x0, y, -0.05, y)


def _force(p):
    # A point with its field around it.
    p.disc(0.0, 0.0, 0.16)
    for r in (0.45, 0.78):
        for a0 in (20, 110, 200, 290):
            p.path(_arc_pts(0.0, 0.0, r, math.radians(a0), math.radians(a0 + 60), 5))


def _hands(p):
    # A controller's stick and the sphere at its tip that pushes things.
    p.line(-0.85, -0.85, -0.05, -0.05)
    p.circle(0.3, 0.3, 0.48)


def _cloth(p):
    # A sheet hanging from a rail.
    p.line(-0.85, 0.75, 0.85, 0.75)
    left = [(-0.7 + 0.12 * math.sin(t * 2.5), 0.75 - 1.5 * t) for t in (i / 10 for i in range(11))]
    right = [(0.7 + 0.12 * math.sin(t * 2.5 + 1.0), 0.75 - 1.5 * t) for t in (i / 10 for i in range(11))]
    p.path(left)
    p.path(right)
    p.path([(-0.7 + 1.4 * t + 0.0, -0.75 + 0.12 * math.sin(t * 9.0)) for t in (i / 12 for i in range(13))])


def _soft(p):
    # A blob squashed on the ground.
    p.path([(0.78 * math.cos(a), -0.6 + 0.62 * max(math.sin(a), 0.0) + 0.06 * math.sin(a)) for a in
            (math.pi * k / 16 for k in range(17))])
    p.line(-0.9, -0.6, 0.9, -0.6)
    p.disc(-0.2, -0.15, 0.09)


def _spring(p):
    pts = [(-0.85, 0.0)]
    for k in range(7):
        pts.append((-0.6 + 0.2 * k, 0.4 if k % 2 == 0 else -0.4))
    pts.append((0.85, 0.0))
    p.path(pts)


def _gravity(p):
    p.line(0.0, 0.8, 0.0, -0.75)
    _arrow_head(p, 0.0, -0.8, -math.pi / 2, 0.4)


def _seam(p):
    # A square cut open along a dashed line.
    p.path([(-0.15, 0.8), (-0.8, 0.8), (-0.8, -0.8), (-0.15, -0.8)])
    p.path([(0.15, 0.8), (0.8, 0.8), (0.8, -0.8), (0.15, -0.8)])
    for y in (0.55, 0.0, -0.55):
        p.line(0.0, y + 0.2, 0.0, y - 0.2)


def _fix(p):
    # A bandage, tilted.
    c, s = math.cos(math.radians(40)), math.sin(math.radians(40))

    def rot(x, y):
        return (x * c - y * s, x * s + y * c)
    p.path([rot(-0.95, -0.3), rot(0.95, -0.3), rot(0.95, 0.3), rot(-0.95, 0.3)], closed=True)
    p.path([rot(-0.3, -0.3), rot(0.3, -0.3), rot(0.3, 0.3), rot(-0.3, 0.3)], closed=True)
    p.disc(*rot(0.0, 0.0), 0.08)


def _islands(p):
    # Separate islands, as an unwrap lays them out.
    p.box(-0.8, 0.1, -0.1, 0.8, 0.06, fill=True)
    p.box(0.15, 0.3, 0.8, 0.8, 0.06)
    p.fill([(-0.8, -0.8), (0.0, -0.8), (-0.8, -0.1)])
    p.box(0.15, -0.8, 0.8, 0.05, 0.06, fill=True)


ICONS = {
    "sculpt": _sculpt, "model": _model, "draw": _draw, "paint": _paint, "pose": _pose, "tools": _tools,
    "pin": _pin, "pinned": _pinned, "gear": _gear, "back": _chevron(math.pi), "chevron_right": _chevron(0.0),
    "chevron_up": _chevron(math.pi / 2), "chevron_down": _chevron(-math.pi / 2), "undo": _undo, "redo": _redo,
    "trash": _trash, "duplicate": _duplicate, "plus": _plus, "minus": _minus, "close": _close, "check": _check,
    "eye": _eye, "grid": _grid, "magnet": _magnet, "surface": _surface, "angle": _angle, "mirror": _mirror,
    "here": _here, "pen": _pen, "edit": _edit, "eraser": _eraser, "tube": _tube, "ribbon": _ribbon, "note": _note,
    "eyedropper": _eyedropper, "swap": _swap, "spray": _spray, "contact": _contact, "record": _record,
    "play": _play, "stop": _stop, "frame_prev": _frame_prev, "frame_next": _frame_next, "key_prev": _key_prev,
    "key_next": _key_next, "key": _key, "key_delete": _key_delete, "range_start": _range_start,
    "range_end": _range_end, "paths": _paths, "onion": _onion, "ruler": _ruler, "camera": _camera,
    "light": _light, "light_point": _light_point, "light_sun": _light_sun, "light_spot": _light_spot,
    "light_area": _light_area, "material": _material, "look": _look, "world": _world, "walk": _walk,
    "fly": _fly, "desktop": _desktop, "passthrough": _passthrough, "timeline": _timeline, "tracking": _tracking,
    "fk": _fk, "ik": _ik, "bones": _bones, "bind": _bind, "reset": _reset, "cursor": _cursor, "object": _object,
    "cut": _cut, "union": _union, "intersect": _intersect, "bevel": _bevel, "shell": _shell, "array": _array,
    "radial": _radial, "linear": _linear, "shading": _shading, "rig": _rig, "weights": _weights, "smooth": _smooth,
    "retopo": _retopo, "strip": _strip, "quad": _quad, "relax": _relax, "tweak": _tweak, "wrap": _wrap,
    "kit": _kit, "kit_add": _kit_add, "stamp": _stamp, "physics": _physics, "throw": _throw, "force": _force,
    "hands": _hands, "cloth": _cloth, "soft": _soft, "spring": _spring,
    "gravity": _gravity, "seam": _seam, "fix": _fix, "islands": _islands,
}


_icon_cache = {}


def icon(canvas, name, cx, cy, size, color, width=0.13):
    """Draw icon `name` centered at (cx, cy), size wide. Unknown names draw nothing. Each icon's
    triangles are built once, at unit size, and placed from there."""
    key = (name, width)
    unit = _icon_cache.get(key)
    if unit is None:
        fn = ICONS.get(name)
        if fn is None:
            return
        c = Canvas()
        fn(Pen(c, 0.0, 0.0, 0.5, color, width))
        unit = _icon_cache[key] = [(x, y) for x, y, _z in c.pos]
    z = canvas.z
    canvas.pos += [(cx + x * size, cy + y * size, z) for x, y in unit]
    canvas.col += [color] * len(unit)
