"""Motion paths in VR: Blender's own object motion paths, drawn in the headset, with a marker on
every location key that the trigger can grab to move that key.

Paths are (re)calculated with object.paths_calculate over the scene's frame range for the
selected animated objects, when the selection or the keys change (at most UPDATE_HZ times a
second, so dragging a key stays smooth). For an armature in Pose Mode, its selected animated
bones get paths of their tails instead (pose.paths_calculate), with markers on their keys.
"""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

from . import anim

UPDATE_HZ = 10.0
KEY_GRAB = 0.015  # hand-space meters from a key marker that grab it
LINE_COLOR = (1.0, 1.0, 1.0, 0.55)
KEY_COLOR = (1.0, 0.85, 0.2, 1.0)
CURRENT_COLOR = (0.35, 0.75, 1.0, 1.0)

_state = {"dirty": True, "signature": None, "time": 0.0}


def mark_dirty():
    _state["dirty"] = True


def targets(context):
    """Selected objects that have their own animation (not an armature being posed: its bones get paths)."""
    return [o for o in anim.selection(context) if anim.fcurves(o) and not anim.posing(o)]


def bone_targets(context):
    """(rig, [selected pose bones that have keys]) for an armature in Pose Mode, or (None, [])."""
    rig = context.view_layer.objects.active
    if not anim.posing(rig):
        return None, []
    keyed = {fc.data_path for fc in anim.fcurves(rig)}
    bones = [pb for pb in anim.selected_bones(rig)
             if any(p.startswith(anim.bone_prefix(pb.name)) for p in keyed)]
    return rig, bones


def _signature(context, objs):
    scene = context.scene
    rig, bones = bone_targets(context)
    return (tuple(o.name for o in objs), rig.name if rig else None, tuple(pb.name for pb in bones),
            scene.frame_start, scene.frame_end)


def ensure(context, force=False):
    """Recalculate the paths if the selection, the range or the keys changed (throttled)."""
    settings = context.scene.vr_sculpt
    if not settings.show_paths:
        return False
    objs = targets(context)
    sig = _signature(context, objs)
    if not force and not _state["dirty"] and sig == _state["signature"]:
        return False
    if not force and time.monotonic() - _state["time"] < 1.0 / UPDATE_HZ:
        return False
    _state.update(dirty=False, signature=sig, time=time.monotonic())
    rig, bones = bone_targets(context)
    if bones:
        _bone_paths(context, rig, bones)
    if not objs:
        return bool(bones)
    scene = context.scene
    for obj in objs:
        mp = obj.animation_visualization.motion_path
        mp.type = 'RANGE'
        mp.frame_start, mp.frame_end = scene.frame_start, scene.frame_end
    ov = anim._window_override(context) or {}
    with context.temp_override(**ov, selected_objects=objs, selected_editable_objects=objs,
                               active_object=objs[0], object=objs[0]):
        bpy.ops.object.paths_calculate(display_type='RANGE', range='SCENE')
    return True


def _bone_paths(context, rig, bones):
    scene = context.scene
    mp = rig.pose.animation_visualization.motion_path
    mp.type = 'RANGE'
    mp.frame_start, mp.frame_end = scene.frame_start, scene.frame_end
    ov = anim._window_override(context) or {}
    with context.temp_override(**ov, object=rig, active_object=rig, selected_pose_bones=bones):
        bpy.ops.pose.paths_calculate(display_type='RANGE', range='SCENE', bake_location='TAILS')


def bone_markers(rig, pb):
    """[(frame, world position)] for the bone's keys inside its motion path (the tail's position)."""
    mp = pb.motion_path
    if mp is None or not len(mp.points):
        return []
    prefix = anim.bone_prefix(pb.name)
    frames = set()
    for fc in anim.fcurves(rig):
        if fc.data_path.startswith(prefix):
            frames.update(int(round(k.co[0])) for k in fc.keyframe_points)
    out = []
    for f in sorted(frames):
        i = f - mp.frame_start
        if 0 <= i < len(mp.points):
            out.append((f, Vector(mp.points[i].co)))
    return out


def clear(context):
    for obj in context.scene.objects:
        if obj.motion_path is not None:
            ov = anim._window_override(context) or {}
            with context.temp_override(**ov, selected_objects=[obj], selected_editable_objects=[obj],
                                       active_object=obj, object=obj):
                bpy.ops.object.paths_clear(only_selected=True)
    _state["signature"] = None


def key_markers(obj):
    """[(frame, world position)] for the object's location keys inside its motion path."""
    mp = obj.motion_path
    if mp is None or not len(mp.points):
        return []
    start = mp.frame_start
    out = []
    for f in anim.key_frames([obj], {"location"}):
        i = int(round(f)) - start
        if 0 <= i < len(mp.points):
            out.append((int(round(f)), Vector(mp.points[i].co)))
    return out


def nearest_key(context, point, scale):
    """(object, frame) of the key marker within reach of point, or None."""
    best, best_d = None, KEY_GRAB * scale
    for obj in targets(context):
        for frame, co in key_markers(obj):
            d = (co - Vector(point)).length
            if d <= best_d:
                best, best_d = (obj, frame), d
    return best


def _parent_space(obj):
    if obj.parent is None:
        return Matrix.Identity(3)
    return (obj.parent.matrix_world @ obj.matrix_parent_inverse).to_3x3()


class KeyDrag:
    """Trigger handler: move one location key (and its handles) with the controller."""

    def __init__(self, context, obj, frame, tip):
        self.obj = obj
        self.frame = frame
        self.start = Vector(tip)
        self.to_local = _parent_space(obj).inverted_safe()
        self.keys = []  # (fcurve, axis, keyframe point, co, left handle, right handle)
        for fc in anim.fcurves(obj):
            if fc.data_path != "location":
                continue
            for k in fc.keyframe_points:
                if abs(k.co[0] - frame) < 0.5:
                    self.keys.append((fc, fc.array_index, k, k.co[1], k.handle_left[1], k.handle_right[1]))
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        d = self.to_local @ (tip_pose.translation - self.start)
        for fc, axis, k, co, hl, hr in self.keys:
            k.co[1] = co + d[axis]
            k.handle_left[1] = hl + d[axis]
            k.handle_right[1] = hr + d[axis]
        for fc in {entry[0] for entry in self.keys}:
            fc.update()
        if self.keys:
            context.scene.frame_set(context.scene.frame_current)
            mark_dirty()
            from . import onion
            onion.mark_dirty()
            self.changed = True

    def finish(self, context):
        if self.changed:
            anim.changed(context)
            ensure(context, force=True)


def draw(context, scale):
    paths = [(obj.motion_path, key_markers(obj)) for obj in targets(context)]
    rig, bones = bone_targets(context)
    paths += [(pb.motion_path, bone_markers(rig, pb)) for pb in bones]
    paths = [(mp, markers) for mp, markers in paths if mp is not None and len(mp.points) >= 2]
    if not paths:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    current = context.scene.frame_current
    for mp, markers in paths:
        pts = [Vector(p.co) for p in mp.points]
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": pts})
        shader.uniform_float("color", LINE_COLOR)
        batch.draw(shader)
        # A tick per frame across the path's direction, so spacing (speed) shows.
        tick = 0.002 * scale
        coords = []
        for i, p in enumerate(pts):
            d = (pts[min(i + 1, len(pts) - 1)] - pts[max(i - 1, 0)])
            side = d.cross(Vector((0.0, 0.0, 1.0)))
            if side.length < 1e-9:
                side = d.cross(Vector((1.0, 0.0, 0.0)))
            if side.length < 1e-9:
                continue
            side = side.normalized() * tick
            coords += [p - side, p + side]
        if coords:
            batch = batch_for_shader(shader, 'LINES', {"pos": coords})
            shader.uniform_float("color", LINE_COLOR)
            batch.draw(shader)
        # Key markers: small diamonds; the current frame in blue.
        for frame, co in markers:
            _diamond(shader, co, 0.006 * scale, CURRENT_COLOR if frame == current else KEY_COLOR)
        i = current - mp.frame_start
        if 0 <= i < len(pts):
            _diamond(shader, pts[i], 0.004 * scale, CURRENT_COLOR)
    gpu.state.blend_set('NONE')


def _diamond(shader, c, r, color):
    axes = [Vector((r, 0, 0)), Vector((0, r, 0)), Vector((0, 0, r))]
    pts = [c + a for a in axes] + [c - a for a in axes]
    edges = [(0, 1), (1, 3), (3, 4), (4, 0), (0, 2), (1, 2), (3, 2), (4, 2), (0, 5), (1, 5), (3, 5), (4, 5)]
    coords = [pts[i] for e in edges for i in e]
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.uniform_float("color", color)
    batch.draw(shader)
