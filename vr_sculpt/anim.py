"""Animation basics for VR: keys, playback, frame stepping, interpolation and auto-key.

Blender 5.x actions are slotted: an object's F-curves live in the channelbag of its action slot
(bpy_extras.anim_utils), not in action.fcurves.

Auto-key is Blender's own setting (Timeline > Auto Keying). Our VR handlers set transforms
directly, which Blender's auto-key doesn't see, so they call auto_key() when you let go.

For an armature in Pose Mode, keys go on its selected bones, and the timeline, key jumps and
interpolation only look at those bones' channels.
"""

import bpy
from bpy_extras import anim_utils

from . import native

INTERPOLATIONS = (('BEZIER', "Bezier"), ('LINEAR', "Linear"), ('CONSTANT', "Constant"))


def selection(context):
    """Selected objects (the active one first), or the active object."""
    active = context.view_layer.objects.active
    objs = [o for o in context.view_layer.objects if o is not None and o.select_get()]
    if active is not None and active in objs:
        objs.remove(active)
        objs.insert(0, active)
    return objs


def fcurves(obj):
    """The object's F-curves (empty if it isn't animated)."""
    ad = obj.animation_data if obj is not None else None
    if ad is None or ad.action is None:
        return []
    bag = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
    return list(bag.fcurves) if bag is not None else []


def posing(obj):
    return obj is not None and obj.type == 'ARMATURE' and obj.mode == 'POSE'


def selected_bones(obj):
    """The armature's selected pose bones, the active one first."""
    active = obj.data.bones.active
    bones = [pb for pb in obj.pose.bones if pb.select]
    first = [pb for pb in bones if active is not None and pb.name == active.name]
    return first + [pb for pb in bones if pb not in first]


def bone_prefix(name):
    return f'pose.bones["{bpy.utils.escape_identifier(name)}"].'


def channels(obj):
    """F-curves the timeline works on: for an armature in Pose Mode with selected bones, only theirs."""
    curves = fcurves(obj)
    if posing(obj):
        prefixes = tuple(bone_prefix(pb.name) for pb in selected_bones(obj))
        if prefixes:
            return [fc for fc in curves if fc.data_path.startswith(prefixes)]
    return curves


def key_frames(objs, data_paths=None):
    """Sorted frames that have a key on any of objs (optionally only on these data paths)."""
    frames = set()
    for obj in objs:
        for fc in channels(obj):
            if data_paths is None or fc.data_path in data_paths:
                frames.update(round(k.co[0], 3) for k in fc.keyframe_points)
    return sorted(frames)


def rotation_path(obj):
    return {'QUATERNION': "rotation_quaternion", 'AXIS_ANGLE': "rotation_axis_angle"}.get(
        obj.rotation_mode, "rotation_euler")


def transform_paths(obj):
    return ("location", rotation_path(obj), "scale")


def insert_key(obj, frame):
    for path in transform_paths(obj):
        obj.keyframe_insert(path, frame=frame)


def delete_key(obj, frame):
    return _delete(obj, transform_paths(obj), frame)


def _delete(owner, data_paths, frame):
    deleted = False
    for path in data_paths:
        try:
            deleted = owner.keyframe_delete(path, frame=frame) or deleted
        except RuntimeError:
            pass  # No key on this channel at this frame.
    return deleted


def bone_paths(pb, location=True):
    rot = {'QUATERNION': "rotation_quaternion", 'AXIS_ANGLE': "rotation_axis_angle"}.get(
        pb.rotation_mode, "rotation_euler")
    return ("location", rot, "scale") if location else (rot,)


def insert_bone_key(pb, frame, location=True):
    """Key a pose bone's rotation (and location and scale), in a group named after the bone."""
    for path in bone_paths(pb, location):
        pb.keyframe_insert(path, frame=frame, group=pb.name)


def key_selection(context):
    frame = context.scene.frame_current
    active = context.view_layer.objects.active
    if posing(active):
        bones = selected_bones(active)
        for pb in bones:
            insert_bone_key(pb, frame)
        if bones:
            changed(context)
        return len(bones)
    objs = selection(context)
    for obj in objs:
        insert_key(obj, frame)
    if objs:
        changed(context)
    return len(objs)


def delete_selection_keys(context):
    frame = context.scene.frame_current
    active = context.view_layer.objects.active
    if posing(active):
        n = sum(1 for pb in selected_bones(active) if _delete(pb, bone_paths(pb), frame))
    else:
        n = sum(1 for obj in selection(context) if delete_key(obj, frame))
    if n:
        changed(context)
    return n


def auto_key(context, obj):
    """Key obj's transform at the current frame if Auto Keying is on (called after a VR grab)."""
    if obj is None or not context.scene.tool_settings.use_keyframe_insert_auto:
        return False
    insert_key(obj, context.scene.frame_current)
    changed(context)
    return True


def auto_key_bones(context, bones):
    """Key these pose bones if Auto Keying is on. bones: {pose bone: also key location}."""
    if not bones or not context.scene.tool_settings.use_keyframe_insert_auto:
        return False
    frame = context.scene.frame_current
    for pb, location in bones.items():
        insert_bone_key(pb, frame, location)
    changed(context)
    return True


def changed(context):
    """Keys changed: re-evaluate the current frame and let paths and onion skins refresh."""
    scene = context.scene
    scene.frame_set(scene.frame_current)
    from . import onion, paths
    onion.mark_dirty()
    paths.mark_dirty()


def keys_at(objs, frame):
    """Keyframe points on objs at frame."""
    return [k for obj in objs for fc in channels(obj) for k in fc.keyframe_points if abs(k.co[0] - frame) < 0.5]


def interpolation_at(context):
    """Interpolation of the selection's keys at the current frame ('MIXED' or None if none)."""
    kinds = {k.interpolation for k in keys_at(selection(context), context.scene.frame_current)}
    if not kinds:
        return None
    return kinds.pop() if len(kinds) == 1 else 'MIXED'


def set_interpolation(context, interpolation):
    """Set the interpolation of the selection's keys at the current frame (from them to the next)."""
    keys = keys_at(selection(context), context.scene.frame_current)
    for k in keys:
        k.interpolation = interpolation
    if keys:
        for obj in selection(context):
            for fc in channels(obj):
                fc.update()
        changed(context)
    return len(keys)


# --- time ------------------------------------------------------------------

def set_frame(context, frame):
    scene = context.scene
    scene.frame_set(int(round(min(max(frame, scene.frame_start), scene.frame_end))))


def step(context, delta):
    set_frame(context, context.scene.frame_current + delta)


def jump_key(context, direction):
    """Go to the selection's previous (-1) or next (+1) key; returns the frame or None."""
    current = context.scene.frame_current
    frames = key_frames(selection(context)) or key_frames([o for o in context.scene.objects])
    candidates = [f for f in frames if (f > current + 0.01 if direction > 0 else f < current - 0.01)]
    if not candidates:
        return None
    frame = min(candidates) if direction > 0 else max(candidates)
    context.scene.frame_set(int(round(frame)))
    return frame


def _window_override(context):
    windows = context.window_manager.windows
    window = context.window or (windows[0] if len(windows) else None)
    if window is None:
        return None
    view = native.largest_view3d(window)
    return dict(window=window, screen=window.screen, area=view[0], region=view[1]) if view else \
        dict(window=window, screen=window.screen)


def is_playing(context):
    return any(w.screen.is_animation_playing for w in context.window_manager.windows if w.screen)


def toggle_play(context):
    ov = _window_override(context)
    if ov is None:
        return False
    with context.temp_override(**ov):
        if is_playing(context):
            bpy.ops.screen.animation_cancel(restore_frame=False)
        else:
            bpy.ops.screen.animation_play()
    return True


def set_start(context):
    scene = context.scene
    scene.frame_start = min(scene.frame_current, scene.frame_end)


def set_end(context):
    scene = context.scene
    scene.frame_end = max(scene.frame_current, scene.frame_start)
