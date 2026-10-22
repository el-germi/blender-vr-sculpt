"""Snapping for grabbed objects: onto surfaces, and rotation in 15 degree steps.

Surface: while a grabbed object's anchor is within reach of another mesh's surface, the object
sits on it: its local Z turns to the surface normal and the anchor lands on the surface. The
anchor is the bottom center of the object's bounds (it stands on the surface), or for a cutter
the center (it sinks halfway in, so a cylinder cutter drills a hole). Twisting the controller
still turns it about the normal (in 15 degree steps with Snap Rotation on). Pull away and it's
free again.

A cutter snaps to its own target's original surface (not the cut one, which has a hole where the
cutter is); cutters are never snapped to.
"""

import math

from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from . import hardsurface

REACH = 0.03  # hand-space meters from the anchor to a surface that it snaps to
ANGLE_STEP = math.radians(15.0)

# (point, normal) of the current surface snap, for drawing; None when not snapped.
last_hit = None


def _bounds(obj):
    corners = [Vector(c) for c in obj.bound_box]
    lo = Vector([min(c[i] for c in corners) for i in range(3)])
    hi = Vector([max(c[i] for c in corners) for i in range(3)])
    return lo, hi


def anchor(obj):
    """The point (object space) that lands on the surface."""
    lo, hi = _bounds(obj)
    center = (lo + hi) / 2
    if not hardsurface.is_cutter(obj):
        center.z = lo.z
    return center


def snap_rotation(rot):
    """A 3x3 rotation with each Euler angle rounded to ANGLE_STEP."""
    e = rot.to_euler()
    for i in range(3):
        e[i] = round(e[i] / ANGLE_STEP) * ANGLE_STEP
    return e.to_matrix()


def _descendants(obj):
    out = set()
    stack = list(obj.children)
    while stack:
        c = stack.pop()
        out.add(c)
        stack.extend(c.children)
    return out


class SurfaceSnap:
    """Snaps one grabbed object. Trees for the meshes near it are built on first use and kept for
    the rest of the grab."""

    def __init__(self, context, obj, scale):
        self.obj = obj
        self.reach = REACH * scale
        self.anchor = anchor(obj)
        self.own_target = hardsurface.target_of(obj)
        skip = {obj} | _descendants(obj)
        self.candidates = [o for o in context.view_layer.objects
                           if o is not None and o.type == 'MESH' and o not in skip and o.visible_get()
                           and not hardsurface.is_cutter(o)]
        self.trees = {}

    def _tree(self, context, other):
        tree = self.trees.get(other.name)
        if tree is None:
            if other == self.own_target:
                me = other.data
                tree = BVHTree.FromPolygons([v.co for v in me.vertices], [p.vertices[:] for p in me.polygons])
            else:
                tree = BVHTree.FromObject(other, context.evaluated_depsgraph_get())
            self.trees[other.name] = tree
        return tree

    def _near_bounds(self, other, point):
        p = other.matrix_world.inverted_safe() @ point
        lo, hi = _bounds(other)
        m = self.reach / max(max(other.matrix_world.to_scale()), 1e-9)
        return all(lo[i] - m <= p[i] <= hi[i] + m for i in range(3))

    def nearest(self, context, point):
        """(point, normal) of the closest surface within reach of a world point, or None."""
        best = None
        for other in self.candidates:
            if not self._near_bounds(other, point):
                continue
            mw = other.matrix_world
            inv = mw.inverted_safe()
            hit = self._tree(context, other).find_nearest(inv @ point)
            if hit[0] is None:
                continue
            world = mw @ hit[0]
            d = (world - point).length
            if d <= self.reach and (best is None or d < best[0]):
                normal = (inv.transposed().to_3x3() @ hit[1]).normalized()
                best = (d, world, normal)
        return None if best is None else best[1:]

    def place(self, context, matrix, snap_angle=False):
        """matrix moved onto the nearest surface, or None when there's none in reach."""
        global last_hit
        loc, rot, scale = matrix.decompose()
        r = rot.to_matrix()
        point = matrix @ self.anchor
        hit = self.nearest(context, point)
        if hit is None:
            last_hit = None
            return None
        surface, normal = hit
        r = (r @ Vector((0.0, 0.0, 1.0))).rotation_difference(normal).to_matrix() @ r
        if snap_angle:
            r = _snap_twist(r, normal)
        rs = r @ Matrix.Diagonal(scale)
        t = surface - rs @ self.anchor
        last_hit = (surface, normal)
        out = rs.to_4x4()
        out.translation = t
        return out

    def finish(self):
        global last_hit
        last_hit = None


def _snap_twist(r, normal):
    """Turn r about normal so its X axis is a multiple of ANGLE_STEP from a fixed reference."""
    ref = Vector((1.0, 0.0, 0.0)) if abs(normal.x) < 0.9 else Vector((0.0, 1.0, 0.0))
    ref = (ref - normal * ref.dot(normal)).normalized()
    x = r @ Vector((1.0, 0.0, 0.0))
    x = (x - normal * x.dot(normal))
    if x.length < 1e-6:
        return r
    x.normalize()
    angle = math.atan2(normal.dot(ref.cross(x)), ref.dot(x))
    snapped = round(angle / ANGLE_STEP) * ANGLE_STEP
    return Matrix.Rotation(snapped - angle, 3, normal) @ r


def clear():
    global last_hit
    last_hit = None


def draw(shader, scale):
    """A ring on the surface where the grabbed object snapped."""
    if last_hit is None:
        return
    point, normal = last_hit
    rot = normal.to_track_quat('Z', 'Y').to_matrix().to_4x4()
    m = Matrix.Translation(point + normal * 0.0005 * scale) @ rot @ Matrix.Scale(0.012 * scale, 4)
    n = 24
    ring = [m @ Vector((math.cos(2 * math.pi * i / n), math.sin(2 * math.pi * i / n), 0.0)) for i in range(n)]
    coords = [p for i in range(n) for p in (ring[i], ring[(i + 1) % n])]
    coords += [point, point + normal * 0.02 * scale]
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.uniform_float("color", (0.4, 1.0, 0.6, 0.95))
    batch.draw(shader)

