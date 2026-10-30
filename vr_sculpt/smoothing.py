"""Smoothing: the stroke stabilizer every trigger stroke goes through, and smoothing recorded takes.

Strokes: when Smooth is on, the trigger operator passes the tip through a Stabilizer before a
stroke-like handler (one with `smooth = True`) or the sculpt stroke sees it. Two filters, both
measured in time and hand space, so they feel the same at any frame rate and world scale:
- Amount: exponential smoothing with a time constant of up to a quarter second (jitter goes, the
  stroke lags a little behind fast moves).
- Lag: a lazy string. The stroke point only moves once the tip is farther than this from it, and
  then it's pulled along at that distance: straighter lines and clean corners.
Off, the stroke follows the tip exactly, shake and all.

Takes: when Smooth takes is on, the keys a puppet take wrote are smoothed when it ends, with a
Gaussian of the chosen width in frames. Quaternion curves are smoothed as one rotation (signs made
continuous first, normalized after).
"""

import math
import time

import numpy as np
from mathutils import Vector

MAX_TAU = 0.25  # seconds: the time constant at 100% Amount


def time_constant(amount):
    """Seconds for an Amount 0..1. Steep at the top: 50% is about 16 ms, close to the old Draw default."""
    return MAX_TAU * min(max(amount, 0.0), 1.0) ** 4


class Stabilizer:
    """Filters the tip of one stroke. Call it with each new tip; it returns the smoothed point."""

    def __init__(self, start, amount, lag, clock=time.perf_counter):
        self.pos = Vector(start)
        self.tau = time_constant(amount)
        self.lag = max(lag, 0.0)
        self.clock = clock
        self.last = clock()

    def __call__(self, tip):
        tip = Vector(tip)
        now = self.clock()
        dt, self.last = now - self.last, now
        target = tip
        if self.lag > 0.0:
            d = tip - self.pos
            length = d.length
            if length <= self.lag:
                return self.pos.copy()
            target = self.pos + d * (1.0 - self.lag / length)
        if self.tau > 1e-6:
            alpha = 1.0 - math.exp(-max(dt, 0.0) / self.tau)
            self.pos = self.pos.lerp(target, alpha)
        else:
            self.pos = target
        return self.pos.copy()


def for_stroke(settings, handler, tip, scale):
    """A Stabilizer for this trigger press, or None: Smooth off, or an action that isn't a stroke
    (a grab, a panel press, ...). handler None is a sculpt stroke."""
    if not settings.smooth_strokes:
        return None
    if handler is not None and not getattr(handler, "smooth", False):
        return None
    if settings.smooth_amount <= 0.0 and settings.smooth_lag <= 0.0:
        return None
    return Stabilizer(tip, settings.smooth_amount, settings.smooth_lag * scale)


# --- takes -----------------------------------------------------------------

def _gaussian(values, sigma):
    """values (n, k) smoothed along n with a Gaussian of sigma samples; the ends are held, not faded."""
    n = len(values)
    if n < 3 or sigma <= 0.0:
        return values
    radius = max(int(math.ceil(3.0 * sigma)), 1)
    x = np.arange(-radius, radius + 1, dtype=float)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.concatenate([np.repeat(values[:1], radius, 0), values, np.repeat(values[-1:], radius, 0)])
    out = np.empty_like(values)
    for j in range(values.shape[1]):
        out[:, j] = np.convolve(padded[:, j], kernel, mode='valid')
    return out


def _keys_in(fc, lo, hi):
    return [k for k in fc.keyframe_points if lo - 1e-3 <= k.co[0] <= hi + 1e-3]


def smooth_curves(fcurves, lo, hi, sigma):
    """Smooth the keys between frames lo and hi on these F-curves. Curves of one quaternion (same data
    path, indices 0-3) are smoothed together. Returns how many curves changed."""
    groups = {}
    for fc in fcurves:
        groups.setdefault(fc.data_path, []).append(fc)
    changed = 0
    for path, curves in groups.items():
        quat = path.endswith("rotation_quaternion") and len(curves) == 4
        sets = [sorted(curves, key=lambda c: c.array_index)] if quat else [[c] for c in curves]
        for group in sets:
            keys = [_keys_in(fc, lo, hi) for fc in group]
            n = len(keys[0])
            if n < 3 or any(len(k) != n for k in keys):
                continue
            values = np.array([[k[i].co[1] for k in keys] for i in range(n)])
            if quat:
                for i in range(1, n):
                    if float(np.dot(values[i], values[i - 1])) < 0.0:
                        values[i] = -values[i]
            smoothed = _gaussian(values, sigma)
            if quat:
                norms = np.linalg.norm(smoothed, axis=1, keepdims=True)
                smoothed = smoothed / np.maximum(norms, 1e-9)
            for j, ks in enumerate(keys):
                for i, k in enumerate(ks):
                    old = k.co[1]
                    k.co[1] = float(smoothed[i, j])
                    shift = k.co[1] - old
                    k.handle_left[1] += shift
                    k.handle_right[1] += shift
            for fc in group:
                fc.update()
            changed += len(group)
    return changed
