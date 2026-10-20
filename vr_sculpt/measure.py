"""Tape measure: drag with the trigger to measure; measurements stay until removed.

Ends snap to a nearby mesh vertex (within SNAP_DISTANCE), else to the 3D grid when Snap to Grid
is on. Measurements are kept on the scene (an ID property), in world space, and the label uses
the scene's unit settings.
"""

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, kdtree

from . import grid, hud, primitives

PROP = "vrs_measures"
SNAP_DISTANCE = 0.012  # hand-space meters from a vertex that snaps a tape end to it
MIN_LENGTH = 0.002  # hand-space meters; shorter drags are dropped
COLOR = hud.MEASURE_COLOR
LABEL_H = 0.012  # hand-space meters

# Tape being dragged right now (Measure handlers), for drawing.
active = []


def stored(scene):
    """[(a, b)] world-space measurements."""
    flat = list(scene.get(PROP, ()))
    return [(Vector(flat[i:i + 3]), Vector(flat[i + 3:i + 6])) for i in range(0, len(flat) - 5, 6)]


def _store(scene, pairs):
    if pairs:
        scene[PROP] = [c for a, b in pairs for c in (*a, *b)]
    elif PROP in scene:
        del scene[PROP]


def add(scene, a, b):
    _store(scene, stored(scene) + [(Vector(a), Vector(b))])


def undo_last(scene):
    pairs = stored(scene)
    if not pairs:
        return False
    _store(scene, pairs[:-1])
    return True


def clear(scene):
    had = bool(stored(scene))
    _store(scene, [])
    return had


def label(scene, length):
    """Length in the scene's units, e.g. "12.3 cm"."""
    us = scene.unit_settings
    return bpy.utils.units.to_string(us.system, 'LENGTH', length * us.scale_length, precision=4)


class _VertexSnap:
    """Nearest mesh vertex to a point, over the meshes whose bounds the point is in."""

    def __init__(self):
        self.trees = {}  # object name -> (kd tree in world space)

    def nearest(self, context, point, distance):
        obj = primitives.touching(context, point, distance)
        if obj is None:
            return None
        tree = self.trees.get(obj.name)
        if tree is None:
            # Built once per press, for the meshes the tape touches.
            mesh = obj.data
            n = len(mesh.vertices)
            co = np.empty(n * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", co)
            mw = np.array(obj.matrix_world)
            world = co.reshape(-1, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
            tree = kdtree.KDTree(n)
            for i, p in enumerate(world.tolist()):
                tree.insert(p, i)
            tree.balance()
            self.trees[obj.name] = tree
        co, _index, dist = tree.find(point)
        return co if co is not None and dist <= distance else None


def snap_point(context, settings, point, scale, vertex_snap=None):
    if vertex_snap is not None:
        hit = vertex_snap.nearest(context, Vector(point), SNAP_DISTANCE * scale)
        if hit is not None:
            return Vector(hit)
    return grid.snap_if(settings, point)


class Measure:
    """Trigger handler: press at one end, release at the other."""

    def __init__(self, context, settings, tip, scale):
        self.scale = scale
        self.snapper = _VertexSnap()
        self.a = snap_point(context, settings, tip, scale, self.snapper)
        self.b = self.a.copy()
        self.changed = False
        active.append(self)

    def update(self, context, pose, tip_pose, value):
        # The settings are looked up every time: a reference kept from the press goes stale on undo.
        self.b = snap_point(context, context.scene.vr_sculpt, tip_pose.translation, self.scale, self.snapper)

    def abandon(self):
        if self in active:
            active.remove(self)

    def finish(self, context):
        if self in active:
            active.remove(self)
        if (self.b - self.a).length >= MIN_LENGTH * self.scale:
            add(context.scene, self.a, self.b)
            self.changed = True


def draw(context, ss, scale):
    """All stored measurements plus the ones being dragged, with billboard labels."""
    scene = context.scene
    pairs = stored(scene) if scene.vr_sculpt.show_measures else []
    pairs = pairs + [(m.a, m.b) for m in active]
    if not pairs:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    viewer = Vector(ss.viewer_pose_location)
    coords = []
    for a, b in pairs:
        coords += [a, b]
        # End ticks: short crosses facing the viewer.
        for p in (a, b):
            m = hud.billboard(p, viewer, scale)
            t = 0.006
            coords += [m @ Vector((-t, 0, 0)), m @ Vector((t, 0, 0)), m @ Vector((0, -t, 0)), m @ Vector((0, t, 0))]
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.uniform_float("color", (*COLOR, 0.95))
    batch.draw(shader)
    for a, b in pairs:
        text = label(scene, (b - a).length)
        m = hud.billboard((a + b) / 2, viewer, scale)
        w = hud.text_width(text, LABEL_H)
        pad = 0.003
        quad = [(-w / 2 - pad, 0.004, 0), (w / 2 + pad, 0.004, 0),
                (w / 2 + pad, 0.004 + LABEL_H + 2 * pad, 0), (-w / 2 - pad, 0.004 + LABEL_H + 2 * pad, 0)]
        gpu.matrix.push()
        gpu.matrix.multiply_matrix(m)
        batch = batch_for_shader(shader, 'TRIS', {"pos": quad}, indices=((0, 1, 2), (0, 2, 3)))
        shader.uniform_float("color", (0.06, 0.06, 0.07, 0.8))
        batch.draw(shader)
        gpu.matrix.pop()
        hud.draw_text(m, -w / 2, 0.004 + pad, text, LABEL_H, (*COLOR, 1.0))
    gpu.state.blend_set('NONE')
