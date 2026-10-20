"""Tracking objects: empties that follow the headset and the controllers while VR runs.

They live in a "VR Tracking" collection, so they show in the Outliner and can be parented to
or used in drivers. The hand empties follow the grip pose and carry the trigger, grip and
thumbstick values as custom properties ("trigger", "grip", "stick_x", "stick_y", 0..1 / -1..1).
With Record Tracking on, they are keyframed on every frame while the timeline plays.
"""

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import actions

COLLECTION = "VR Tracking"
HEADSET = "VR Headset"
HANDS = ("VR Hand Left", "VR Hand Right")

# Stock actions read for the stick values: (positive, negative) per axis and hand. The left stick
# moves, the right one turns and goes up/down (see navigation.py).
_STICK = {
    0: {"stick_x": ("fly_right", "fly_left"), "stick_y": ("fly_forward", "fly_back")},
    1: {"stick_x": ("fly_turnright", "fly_turnleft"), "stick_y": ("fly_up", "fly_down")},
}
GRIP_ACTION = "nav_grab"

_recorded = {"frame": None}


def collection(context):
    coll = bpy.data.collections.get(COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(COLLECTION)
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def _empty(coll, name, display, size):
    obj = bpy.data.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = display
        obj.empty_display_size = size
        obj.rotation_mode = 'QUATERNION'
        obj.hide_render = True
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    return obj


def ensure(context):
    """(headset, left hand, right hand) empties, created on first use."""
    coll = collection(context)
    head = _empty(coll, HEADSET, 'CUBE', 0.06)
    hands = [_empty(coll, name, 'ARROWS', 0.04) for name in HANDS]
    for obj in hands:
        for key in ("trigger", "grip", "stick_x", "stick_y"):
            if key not in obj:
                obj[key] = 0.0
                ui = obj.id_properties_ui(key)
                ui.update(min=-1.0 if key.startswith("stick") else 0.0, max=1.0)
    return head, hands[0], hands[1]


def _value(context, ss, action, user_path):
    try:
        v = ss.action_state_get(context, actions.ACTIONMAP, action, user_path)
    except Exception:
        return 0.0
    if not isinstance(v, float):
        v = v[0]
    return abs(v)


def _set(obj, matrix):
    if obj.matrix_world != matrix:
        obj.matrix_world = matrix


def update(context, ss):
    """Move the tracking empties to the current poses and refresh their input values."""
    head, left, right = ensure(context)
    _set(head, Matrix.LocRotScale(Vector(ss.viewer_pose_location), Quaternion(ss.viewer_pose_rotation), None))
    for idx, obj in enumerate((left, right)):
        _set(obj, Matrix.LocRotScale(Vector(ss.controller_grip_location_get(context, idx)),
                                     Quaternion(ss.controller_grip_rotation_get(context, idx)), None))
        path = actions.HANDS[idx]
        values = {"trigger": _value(context, ss, actions.ACTION_STROKE, path),
                  "grip": _value(context, ss, GRIP_ACTION, path)}
        for key, (pos, neg) in _STICK[idx].items():
            values[key] = _value(context, ss, pos, path) - _value(context, ss, neg, path)
        changed = False
        for key, v in values.items():
            if abs(obj.get(key, 0.0) - v) > 1e-4:
                obj[key] = v
                changed = True
        if changed:
            # Custom property changes don't re-run drivers by themselves.
            obj.update_tag()
    if context.scene.vr_sculpt.tracking_record:
        _record(context, (head, left, right))


def _record(context, objects):
    windows = context.window_manager.windows
    playing = any(w.screen.is_animation_playing for w in windows if w.screen)
    frame = context.scene.frame_current
    if not playing:
        _recorded["frame"] = None
        return
    if frame == _recorded["frame"]:
        return
    _recorded["frame"] = frame
    for obj in objects:
        obj.keyframe_insert("location", frame=frame, group="VR Tracking")
        obj.keyframe_insert("rotation_quaternion", frame=frame, group="VR Tracking")
    hands = objects[1:]
    for obj in hands:
        for key in ("trigger", "grip", "stick_x", "stick_y"):
            obj.keyframe_insert(f'["{key}"]', frame=frame, group="VR Tracking")
