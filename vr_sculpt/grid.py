"""3D grid: a lattice of small crosses around the controller tips, and snapping to it.

The grid is in scene units (its size doesn't change when you scale the world), aligned to
the world axes through the origin.
"""

import math

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

REACH = 0.12  # hand-space meters around each tip where grid points show
MAX_STEPS = 5  # at most this many grid points from the tip along each axis
COLOR = (0.75, 0.85, 1.0)


def snap(point, size):
    return Vector([round(c / size) * size for c in point])


def snap_if(settings, point):
    """point snapped to the grid when Snap to Grid is on (a copy either way)."""
    return snap(point, settings.grid_size) if settings.grid_snap else Vector(point)


def lattice(center, size, reach):
    """[(point, fade)] for grid points within reach of center; fade is 1 at the center, 0 at reach.

    When the grid is too fine for the reach, only every k-th point is listed.
    """
    step = size * max(math.ceil(reach / (size * MAX_STEPS)), 1)
    n = int(reach / step) + 1
    base = snap(center, step)
    out = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            for k in range(-n, n + 1):
                p = base + Vector((i * step, j * step, k * step))
                d = (p - center).length
                if d < reach:
                    out.append((p, 1.0 - d / reach))
    return out, step


def draw(settings, tips, scale):
    """Crosses at the grid points near each tip, plus the snap target when snapping."""
    reach = REACH * scale
    coords, colors = [], []
    for tip in tips:
        points, step = lattice(tip, settings.grid_size, reach)
        arm = min(step * 0.18, 0.006 * scale)
        for p, fade in points:
            c = (*COLOR, 0.85 * fade * fade)
            for axis in range(3):
                d = Vector((0.0, 0.0, 0.0))
                d[axis] = arm
                coords += [p - d, p + d]
                colors += [c, c]
        if settings.grid_snap:
            # Where a press would land: a bigger, solid cross.
            p = snap(tip, settings.grid_size)
            for axis in range(3):
                d = Vector((0.0, 0.0, 0.0))
                d[axis] = arm * 2.5
                coords += [p - d, p + d]
                colors += [(1.0, 1.0, 1.0, 1.0)] * 2
    if not coords:
        return
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    batch = batch_for_shader(shader, 'LINES', {"pos": coords, "color": colors})
    batch.draw(shader)
