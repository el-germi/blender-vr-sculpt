"""Pencil strokes (Draw mode, Pencil style): real Grease Pencil strokes, drawn with the trigger.

Unlike Note (an annotation, for marking things up), Pencil makes a Grease Pencil object: it renders,
takes Grease Pencil modifiers and materials, and is animated frame by frame. Strokes go to the active
Grease Pencil object if there is one (so you can draw into your own), else to our "VR Pencil" object
in the VR Strokes collection. They go on the active layer, on the frame showing at the current frame;
with Blender's Auto Keying on, drawing on a frame that has no key of its own keys a new, empty one
(draw a frame, step, draw the next), the same as Grease Pencil's own draw mode.

Each palette color is a Grease Pencil material ("VR Pencil Red", ...). Points are written in the
object's space; the layer's own transform is left alone (identity unless set on the desktop). The
width follows Draw's Thickness (pressure, speed, taper), as the point radius.
"""

import bpy
import numpy as np
from mathutils import Vector

from . import drawing, strokes

OBJECT_PROP = "vrs_pencil"
OBJECT_NAME = "VR Pencil"
LAYER_NAME = "Lines"


def target_object(context):
    """The Grease Pencil object strokes go to: the active one, else ours (made on first use)."""
    active = context.view_layer.objects.active
    if active is not None and active.type == 'GREASEPENCIL':
        return active
    obj = next((o for o in context.scene.objects if o.get(OBJECT_PROP)), None)
    if obj is None:
        gp = bpy.data.grease_pencils.new(OBJECT_NAME)
        obj = bpy.data.objects.new(OBJECT_NAME, gp)
        obj[OBJECT_PROP] = True
        strokes.collection(context).objects.link(obj)
    return obj


def material_index(obj, color_id):
    """Slot of this palette color's Grease Pencil material on obj (added if missing)."""
    name = f"VR Pencil {color_id.title()}"
    mat = bpy.data.materials.get(name)
    if mat is None or not mat.is_grease_pencil:
        mat = bpy.data.materials.new(name)
        bpy.data.materials.create_gpencil_data(mat)
        rgb = strokes.COLORS.get(color_id, (0.8, 0.8, 0.8))
        mat.grease_pencil.color = (*rgb, 1.0)
        mat.diffuse_color = (*rgb, 1.0)
    mats = obj.data.materials
    for i, m in enumerate(mats):
        if m == mat:
            return i
    mats.append(mat)
    return len(mats) - 1


def layer_frame(context, obj):
    """(layer, frame) a new stroke goes to: the active layer (or a new one), at the current frame."""
    gp = obj.data
    layer = gp.layers.active
    if layer is None:
        layer = gp.layers.get(LAYER_NAME) or gp.layers.new(LAYER_NAME)
        gp.layers.active = layer
    current = context.scene.frame_current
    frame = layer.get_frame_at(current)
    auto = context.scene.tool_settings.use_keyframe_insert_auto
    if frame is None or (auto and frame.frame_number != current):
        frame = layer.frames.new(current)
    return layer, frame


def is_pencil(obj):
    return obj is not None and obj.type == 'GREASEPENCIL'


def visible_drawings(context):
    """(object, layer, drawing) showing now on every visible Grease Pencil object."""
    out = []
    for obj in context.view_layer.objects:
        if not is_pencil(obj) or not obj.visible_get():
            continue
        for layer in obj.data.layers:
            if layer.hide:
                continue
            frame = layer.get_frame_at(context.scene.frame_current)
            if frame is not None:
                out.append((obj, layer, frame.drawing))
    return out


def stroke_positions(obj, stroke):
    pts = np.array([p.position[:] for p in stroke.points], dtype=np.float64).reshape(-1, 3)
    m = np.array(obj.matrix_world)
    return pts @ m[:3, :3].T + m[:3, 3]


class PencilStroke:
    """Trigger handler: a Grease Pencil stroke (plus mirrored copies) while held."""
    smooth = True  # The trigger operator stabilizes the tip (smoothing.py).

    def __init__(self, context, settings, tip_pose, value, scale):
        self.settings = settings
        self.scale = scale
        self.obj = target_object(context)
        self.layer, self.frame = layer_frame(context, self.obj)
        self.drawing = self.frame.drawing
        self.to_local = self.obj.matrix_world.inverted()
        self.radius_scale = 1.0 / max(self.obj.matrix_world.to_scale().length / 3 ** 0.5, 1e-6)
        self.center = np.array(context.scene.cursor.location[:])
        self.masks = strokes.mirror_variants(drawing.mirror_bits(settings))
        mat = material_index(self.obj, settings.draw_color)
        first = len(self.drawing.strokes)
        self.drawing.add_strokes([1] * len(self.masks))
        self.indices = list(range(first, first + len(self.masks)))
        for i in self.indices:
            stroke = self.drawing.strokes[i]
            stroke.material_index = mat
            stroke.select = False
        self.last = tip_pose.translation.copy()
        self.spacing = drawing.MIN_SPACING * scale
        self.world = []  # The main stroke's points in world space, for the taper at the end.
        self.widths = []
        self.hand_speed = drawing.HandSpeed(tip_pose.translation, scale)
        self.speed = 0.0
        self.changed = False
        self._add(tip_pose.translation, value, first=True)

    def _add(self, point, value, first=False):
        width = drawing.stroke_width(self.settings, self.scale, value, self.speed)
        p = np.array(point[:])
        self.world.append(p)
        self.widths.append(width)
        radius = 0.5 * width * self.radius_scale
        for mask, i in zip(self.masks, self.indices):
            q = strokes.reflect(p, mask, self.center) if mask else p
            # Fetched here, not before the loop: growing one stroke moves the points of the ones after it.
            stroke = self.drawing.strokes[i]
            if not first:
                stroke.add_points(1)
            pt = stroke.points[len(stroke.points) - 1]
            pt.position = (self.to_local @ Vector(q))[:]
            pt.radius = radius
            pt.opacity = 1.0
        self.last = Vector(point)

    def update(self, context, pose, tip_pose, value):
        point = tip_pose.translation
        self.speed = self.hand_speed(point)
        if (point - self.last).length >= self.spacing:
            self._add(point, value)

    def finish(self, context):
        if len(self.world) < 2:
            self.drawing.remove_strokes(indices=self.indices)  # A click, not a stroke.
            return
        if self.settings.draw_thickness == 'TAPER':
            env = strokes.taper_envelope(np.array(self.world), self.settings.draw_taper)
            for i in self.indices:
                for pt, w, f in zip(self.drawing.strokes[i].points, self.widths, env):
                    pt.radius = 0.5 * w * f * self.radius_scale
        self.drawing.tag_positions_changed()
        strokes.push(("pencil", (self.obj.name, self.layer.name, self.frame.frame_number, len(self.indices))))
        self.changed = True


def undo(ref):
    """Remove the strokes of one Pencil history entry. Returns whether any went."""
    obj_name, layer_name, frame_number, count = ref
    obj = bpy.data.objects.get(obj_name)
    layer = obj.data.layers.get(layer_name) if is_pencil(obj) else None
    frame = next((f for f in layer.frames if f.frame_number == frame_number), None) if layer else None
    if frame is None or not len(frame.drawing.strokes):
        return False
    n = len(frame.drawing.strokes)
    frame.drawing.remove_strokes(indices=list(range(max(n - count, 0), n)))
    return True


def erase(context, sweep, margin):
    """Remove the showing Pencil strokes that come within margin of any point in sweep (n, 3). Returns how
    many went."""
    removed = 0
    for obj, _layer, d in visible_drawings(context):
        hit = []
        for i, stroke in enumerate(d.strokes):
            if not len(stroke.points):
                continue
            pts = stroke_positions(obj, stroke)
            if np.linalg.norm(pts[:, None, :] - sweep[None, :, :], axis=2).min() <= margin:
                hit.append(i)
        if hit:
            d.remove_strokes(indices=hit)
            removed += len(hit)
    return removed
