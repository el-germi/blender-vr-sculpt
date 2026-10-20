"""Draw mode trigger handlers: draw a stroke, draw a note, reshape a stroke, erase strokes.

Same interface as interactions.py: update(context, aim_pose, tip_pose, value) every
frame while the trigger is held, finish(context) on release, `changed` for undo.
"""

import math
import time

import numpy as np
from mathutils import Matrix, Vector

from . import brushes, strokes

MIN_SPACING = 0.002  # hand-space meters between samples, at the thinnest
EDIT_RADIUS = 0.04  # hand-space meters reached by the Edit tool
ERASE_MARGIN = 0.01  # hand-space meters around the tip that erase


def side_vector(pose):
    """The controller's local X axis: ribbons steer to lie across it (see strokes.orient_step)."""
    return pose.to_3x3() @ Vector((1.0, 0.0, 0.0))


def mirror_bits(settings):
    return (1 if settings.mirror_x else 0) | (2 if settings.mirror_y else 0) | (4 if settings.mirror_z else 0)


TRIGGER_THRESHOLD = 0.3  # the stroke action fires past this much trigger travel
MIN_WIDTH = 0.15  # thinnest, as a fraction of the width setting
SPEED_SLOW = 0.15  # hand-space m/s: at or below, speed modes give the "slow" width
SPEED_FAST = 2.0  # hand-space m/s: at or above, the "fast" width (eased in between)
SPEED_TAU = 0.3  # seconds: speed is averaged over about this long, so wobbles don't show
SPEED_WIDTH_RATE = 0.05  # hand-space meters of stroke for speed modes to go from thinnest to widest
STEER_BASELINE = 0.02  # hand-space meters of stroke used to judge its direction for ribbon steering


def _clamp01(x):
    return min(max(x, 0.0), 1.0)


def _ease(x):
    x = _clamp01(x)
    return x * x * (3.0 - 2.0 * x)


def stroke_width(settings, scale, value, speed):
    """Width for a new sample. value: trigger travel 0..1; speed: hand-space m/s.

    Taper mode stores the plain width; the ramps are applied over the whole stroke when it's
    built (strokes.taper_envelope), since the end isn't known until the trigger is released.
    """
    width = settings.draw_width * scale
    mode = settings.draw_thickness
    if mode == 'PRESSURE':
        f = _clamp01((value - TRIGGER_THRESHOLD) / (1.0 - TRIGGER_THRESHOLD))
    elif mode in {'SPEED_THIN', 'SPEED_THICK'}:
        f = _ease((speed - SPEED_SLOW) / (SPEED_FAST - SPEED_SLOW))
        if mode == 'SPEED_THIN':
            f = 1.0 - f
    else:
        return width
    return width * (MIN_WIDTH + (1.0 - MIN_WIDTH) * f)


class _Stabilizer:
    """Exponential smoothing of the tip, so hand jitter doesn't end up in the stroke."""

    def __init__(self, start, amount):
        self.pos = Vector(start)
        self.keep = min(max(amount, 0.0), 0.95)

    def __call__(self, tip):
        self.pos = self.pos.lerp(Vector(tip), 1.0 - self.keep)
        return self.pos


class DrawStroke:
    def __init__(self, context, settings, tip_pose, value, scale):
        self.settings = settings
        self.scale = scale
        tip = tip_pose.translation
        self.origin = tip.copy()
        self.obj = strokes.new_object(context, settings.draw_style, settings.draw_color, tip)
        center = context.scene.cursor.location - self.origin
        self.samples = {
            "style": settings.draw_style, "sym": mirror_bits(settings), "c": np.array(center[:]),
            "p": np.zeros((0, 3)), "w": np.zeros(0), "s": np.zeros((0, 3)),
            "taper": settings.draw_taper if settings.draw_thickness == 'TAPER' else 0.0,
        }
        self.spacing = max(settings.draw_width * scale * 0.25, MIN_SPACING * scale)
        self.stab = _Stabilizer(tip, settings.draw_smoothing)
        self.ribbon = settings.draw_style == 'RIBBON'
        self.speed = 0.0
        self.last_point = tip.copy()
        self.last_time = time.perf_counter()
        self.changed = False
        self._add(tip, side_vector(tip_pose), value)

    def _add(self, point, side, value):
        s = self.samples
        p = np.array((point - self.origin)[:])
        desired = np.array(side[:]) if self.ribbon else None
        if len(s["p"]) == 0:
            # No direction yet: start across the controller; fixed up by the second sample.
            normal = np.array(side[:])
        else:
            t = p - s["p"][-1]
            seg = float(np.linalg.norm(t))
            t = t / seg
            if len(s["p"]) == 1:
                first, _ = strokes._across(s["s"][0], t)
                s["s"][0] = first if first is not None else strokes.any_perpendicular(t)
            normal = strokes.orient_step(s["s"][-1], t, desired, strokes.TWIST_RATE * seg / self.scale,
                                         steer_t=self._long_tangent(p))
        width = stroke_width(self.settings, self.scale, value, self.speed)
        if len(s["w"]) and self.settings.draw_thickness in {'SPEED_THIN', 'SPEED_THICK'}:
            # Speed widths change gradually along the stroke, never in steps.
            seg = float(np.linalg.norm(p - s["p"][-1]))
            full = self.settings.draw_width * self.scale * (1.0 - MIN_WIDTH)
            step = full * seg / (SPEED_WIDTH_RATE * self.scale)
            width = min(max(width, s["w"][-1] - step), s["w"][-1] + step)
        s["p"] = np.vstack([s["p"], p])
        s["w"] = np.append(s["w"], width)
        s["s"] = np.vstack([s["s"], normal])

    def _long_tangent(self, p):
        """Direction from a sample at least STEER_BASELINE back (hand jitter averages out over it)."""
        pts = self.samples["p"]
        reach = STEER_BASELINE * self.scale
        for i in range(len(pts) - 1, -1, -1):
            d = p - pts[i]
            length = float(np.linalg.norm(d))
            if length >= reach or i == 0:
                return d / length if length > 1e-9 else None
        return None

    def _track_speed(self, point):
        """Hand speed averaged over about SPEED_TAU seconds (the same at any frame rate)."""
        now = time.perf_counter()
        dt = now - self.last_time
        if dt > 1e-4:
            v = (point - self.last_point).length / dt / self.scale
            alpha = 1.0 - math.exp(-dt / SPEED_TAU)
            self.speed += (v - self.speed) * alpha
        self.last_point, self.last_time = point.copy(), now

    def update(self, context, pose, tip_pose, value):
        point = self.stab(tip_pose.translation)
        self._track_speed(point)
        last = Vector(self.samples["p"][-1]) + self.origin
        if (point - last).length >= self.spacing:
            self._add(point, side_vector(tip_pose), value)
            strokes.rebuild(self.obj, self.samples, preview=True)

    def finish(self, context):
        if len(self.samples["p"]) < 2:
            strokes.delete(self.obj)  # A click, not a stroke.
            return
        # End exactly where the stabilizer was heading, then keep the samples for editing.
        strokes.rebuild(self.obj, self.samples)
        strokes.save(self.obj, self.samples)
        strokes.push(("mesh", self.obj.name))
        # Refresh its transform and bounds now, so Edit/Erase/grab find it right away.
        context.view_layer.update()
        self.changed = True


class NoteStroke:
    """A 3D annotation stroke (plus mirrored copies), shown in the headset and on the desktop."""

    def __init__(self, context, settings, tip_pose, value, scale):
        self.layer, self.frame = strokes.notes_frame(context, settings.draw_color)
        self.center = context.scene.cursor.location.copy()
        self.masks = strokes.mirror_variants(mirror_bits(settings))
        self.items = []
        for _mask in self.masks:
            stroke = self.frame.strokes.new()
            stroke.display_mode = '3DSPACE'
            self.items.append(stroke)
        self.spacing = MIN_SPACING * scale
        self.stab = _Stabilizer(tip_pose.translation, settings.draw_smoothing)
        self.last = None
        self.count = 0
        self.changed = False
        self._add(tip_pose.translation)

    def _add(self, point):
        p = np.array(point[:])
        for mask, stroke in zip(self.masks, self.items):
            q = strokes.reflect(p, mask, np.array(self.center[:])) if mask else p
            stroke.points.add(1)
            stroke.points[len(stroke.points) - 1].co = q.tolist()
        self.last = Vector(point)
        self.count += 1

    def update(self, context, pose, tip_pose, value):
        point = self.stab(tip_pose.translation)
        if (point - self.last).length >= self.spacing:
            self._add(point)

    def finish(self, context):
        if self.count < 2:
            for stroke in self.items:
                self.frame.strokes.remove(stroke)
            return
        strokes.push(("note", [(self.layer.info, self.frame.frame_number, len(self.items))]))
        self.changed = True


class ReshapeStroke:
    """Pull part of a stroke with the controller; mirrored copies follow."""

    def __init__(self, context, obj, tip_pose, scale):
        self.obj = obj
        self.samples = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in strokes.load(obj).items()}
        self.start_p = self.samples["p"].copy()
        self.world = obj.matrix_world.copy()
        self.world_inv = self.world.inverted_safe()
        tip = np.array((self.world_inv @ tip_pose.translation)[:])
        radius = EDIT_RADIUS * scale / max(max(obj.matrix_world.to_scale()), 1e-9)
        # Which copy is the hand closest to? Edits are made on the original through that mirror.
        best = None
        for mask in strokes.mirror_variants(self.samples["sym"]):
            pts = strokes.reflect(self.start_p, mask, self.samples["c"]) if mask else self.start_p
            d = np.linalg.norm(pts - tip, axis=1)
            if best is None or d.min() < best[1].min():
                best = (mask, d)
        self.mask, dist = best
        self.weights = brushes.falloff(dist, radius)
        self.pose_inv = tip_pose.inverted_safe()
        self.scale = scale
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        delta = self.world_inv @ (tip_pose @ self.pose_inv) @ self.world
        m = np.array(delta)
        c = self.samples["c"]
        src = strokes.reflect(self.start_p, self.mask, c) if self.mask else self.start_p
        moved = src @ m[:3, :3].T + m[:3, 3]
        if self.mask:
            moved = strokes.reflect(moved, self.mask, c)
        w = self.weights[:, None]
        self.samples["p"] = self.start_p + (moved - self.start_p) * w
        strokes.rebuild(self.obj, self.samples, preview=True)
        self.changed = True

    def finish(self, context):
        if self.changed:
            # The bends changed: carry the frame along the new path again (no twist, no flips).
            s = self.samples
            guide = s["s"] if s["style"] == 'RIBBON' else None
            s["s"] = strokes.stable_normals(s["p"], guide, strokes.TWIST_RATE / self.scale)
            strokes.rebuild(self.obj, s)
            strokes.save(self.obj, s)


class EraseStrokes:
    """Everything the tip touches while the trigger is held is erased: strokes and notes."""

    def __init__(self, context, settings, tip_pose, scale):
        self.margin = ERASE_MARGIN * scale
        self.changed = False
        self.last = None
        self.update(context, None, tip_pose, 1.0)

    def _swept(self, tip):
        """Points along the tip's path since last frame: a fast sweep mustn't jump over a thin stroke."""
        if self.last is None:
            return [tip]
        steps = max(int((tip - self.last).length / (self.margin * 0.5)), 1)
        return [self.last.lerp(tip, (i + 1) / steps) for i in range(steps)]

    def update(self, context, pose, tip_pose, value):
        tip = tip_pose.translation.copy()
        points = self._swept(tip)
        self.last = tip
        for point in points:
            while True:
                obj = strokes.nearest(context, point, self.margin)
                if obj is None:
                    break
                strokes.delete(obj)
                self.changed = True
        sweep = np.array([p[:] for p in points])
        for _layer, frame, stroke in strokes.note_strokes(context):
            if not len(stroke.points):
                continue
            pts = np.array([pt.co[:] for pt in stroke.points])
            d = np.linalg.norm(pts[:, None, :] - sweep[None, :, :], axis=2)
            if d.min() <= self.margin:
                frame.strokes.remove(stroke)
                self.changed = True

    def finish(self, context):
        pass


def pick(context, settings, tip_pose, value, scale):
    """Handler for a Draw-mode trigger press."""
    if settings.draw_tool == 'ERASE':
        return EraseStrokes(context, settings, tip_pose, scale)
    if settings.draw_tool == 'EDIT':
        obj = strokes.nearest(context, tip_pose.translation, EDIT_RADIUS * scale)
        return ReshapeStroke(context, obj, tip_pose, scale) if obj is not None else None
    if settings.draw_style == 'NOTE':
        return NoteStroke(context, settings, tip_pose, value, scale)
    return DrawStroke(context, settings, tip_pose, value, scale)
