"""Puppeteering: bind bones (or objects) to a controller or the headset, and record takes.

A binding is a Follow (pose.py) that never lets go: the bound bone keeps the offset it had to the
controller's grip (or the headset) when it was bound, so moving your hand moves it. In FK a bound
bone turns with the controller (and moves too if it's a free bone); in IK it follows rigidly
and its parent chain bends to reach, so a bound hand drags the arm along.

Rec counts down, then plays the timeline from the start and keys every bound target (and
whatever the triggers are posing) on each frame, until Stop, the end of the range, or the
playback stopping. A take only replaces the keys of what it records, so takes layer: record
the head, then each hand.
"""

import time

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import anim, pose, smoothing

SOURCES = (('LEFT', "Left hand"), ('RIGHT', "Right hand"), ('HEAD', "Headset"))
SOURCE_COLORS = {'LEFT': (1.0, 0.5, 0.35), 'RIGHT': (0.4, 0.9, 0.5), 'HEAD': (0.75, 0.6, 1.0)}
COUNTDOWN = 3.0  # seconds before a take starts

_bindings = {}  # (object name, bone name or "") -> (source, Follow)
_grabs = []  # Follows of trigger grabs in progress
_take = {"state": None, "start": 0.0, "last_frame": None, "frames": 0, "beeps": 0}


def source_pose(context, ss, source):
    if source == 'HEAD':
        return Matrix.LocRotScale(Vector(ss.viewer_pose_location), Quaternion(ss.viewer_pose_rotation), None)
    i = 0 if source == 'LEFT' else 1
    return Matrix.LocRotScale(Vector(ss.controller_grip_location_get(context, i)),
                              Quaternion(ss.controller_grip_rotation_get(context, i)), None)


def bind_target(context):
    """(object, pose bone or None) a Bind button binds: the active bone in Pose Mode, else the active object."""
    obj = context.view_layer.objects.active
    if obj is None:
        return None
    if anim.posing(obj):
        pb = pose.active_bone(obj)
        return (obj, pb) if pb is not None else None
    return obj, None


def bind(context, ss, source):
    """Bind the active bone (or object) to a controller or the headset. Returns its name or None."""
    found = bind_target(context) if ss is not None else None
    if found is None:
        return None
    obj, pb = found
    settings = context.scene.vr_sculpt
    kind = 'IK' if settings.pose_tool == 'IK' else 'RIGID'
    follow = pose.Follow(obj, pb, source_pose(context, ss, source), kind, chain=settings.pose_chain,
                         mirror=settings.pose_mirror)
    _bindings[(obj.name, pb.name if pb else "")] = (source, follow)
    return pb.name if pb else obj.name


def unbind_all():
    n = len(_bindings)
    _bindings.clear()
    return n


def binding_count():
    return len(_bindings)


def bindings():
    """[(source, Follow)]."""
    return list(_bindings.values())


def grab_started(follow):
    _grabs.append(follow)


def grab_ended(follow):
    if follow in _grabs:
        _grabs.remove(follow)


def active():
    """Whether anything here needs the fast app timer."""
    return bool(_bindings) or _take["state"] is not None


def live():
    """Whether bones or objects are being moved live (grabs, bindings or a take)."""
    return bool(_grabs) or active()


def apply(context, ss):
    """Move every bound target to its source's current pose. Returns what was written."""
    written = {}
    for key, (source, follow) in list(_bindings.items()):
        if follow.target() is None:
            del _bindings[key]  # Deleted or undone away.
            continue
        written.update(follow.apply(source_pose(context, ss, source)))
    return written


# --- takes -----------------------------------------------------------------

def state():
    """None, 'COUNTDOWN', 'RECORDING' or 'ENDING' (the timer stops it soon)."""
    return _take["state"]


def countdown_left():
    return max(COUNTDOWN - (time.monotonic() - _take["start"]), 0.0)


def status():
    """Short text for the Rec button."""
    if _take["state"] == 'COUNTDOWN':
        return f"Rec in {int(countdown_left()) + 1}"
    if _take["state"] in {'RECORDING', 'ENDING'}:
        return "Stop rec"
    return "Rec"


def toggle_record(context):
    if _take["state"] is None:
        _take.update(state='COUNTDOWN', start=time.monotonic(), last_frame=None, frames=0, beeps=0)
    else:
        stop(context)


def start_take(context):
    """Countdown over: from the first frame, play and record."""
    scene = context.scene
    scene.frame_set(scene.frame_start)
    _take.update(state='RECORDING', last_frame=None, frames=0, first=None, targets=set())
    if not anim.is_playing(context):
        anim.toggle_play(context)


def stop(context, push_undo=False):
    """End the take. push_undo: make it one undo step (when the timer ends it; a panel press is
    an undo step of its own)."""
    recorded = _take["state"] in {'RECORDING', 'ENDING'}
    span = (_take.get("first"), _take["last_frame"])
    _take.update(state=None, last_frame=None)
    if recorded and anim.is_playing(context):
        anim.toggle_play(context)
    if recorded:
        print(f"VR Sculpt puppet take: {_take['frames']} frames recorded", flush=True)
        settings = context.scene.vr_sculpt
        if settings.take_smooth and None not in span:
            smooth_take(context, span[0], span[1], settings.take_smooth_frames)
        anim.changed(context)
        if push_undo:
            _undo_push(context, "VR Puppet Take")


def _undo_push(context, message):
    ov = anim._window_override(context) or {}
    try:
        with context.temp_override(**ov):
            bpy.ops.ed.undo_push(message=message)
    except RuntimeError:
        pass  # No undo stack (background).


def record_frame(context, ss):
    """Key the bound and grabbed targets at the current frame (once per frame) while recording."""
    if _take["state"] != 'RECORDING':
        return False
    frame = context.scene.frame_current
    last = _take["last_frame"]
    if frame == last:
        return False
    if last is not None and frame < last:
        # Playback wrapped around: the take is over. Not stopped here, since this may run in a
        # frame change handler, where changing frames or playback isn't safe; the timer does it.
        _take["state"] = 'ENDING'
        return False
    _take["last_frame"] = frame
    if _take.get("first") is None:
        _take["first"] = frame
    written = apply(context, ss) if ss is not None else {}
    for follow in _grabs:
        written.update(follow.apply())  # The latest trigger pose, after the frame's own evaluation.
    targets = _take.setdefault("targets", set())
    for target, location in written.items():
        if isinstance(target, bpy.types.Object):
            anim.insert_key(target, frame)
            targets.add((target.name, ""))
        else:
            anim.insert_bone_key(target, frame, location)
            targets.add((target.id_data.name, target.name))
    _take["frames"] += 1
    return True


def smooth_take(context, first, last, frames):
    """Smooth the keys this take wrote (its targets' transform curves, frames first..last). Returns how
    many F-curves changed."""
    changed = 0
    for obj_name, bone in _take.get("targets", ()):
        obj = bpy.data.objects.get(obj_name)
        if obj is None:
            continue
        if bone:
            pb = obj.pose.bones.get(bone) if obj.pose else None
            if pb is None:
                continue
            prefix = anim.bone_prefix(bone)
            paths = {prefix + p for p in anim.bone_paths(pb)}
        else:
            paths = set(anim.transform_paths(obj))
        curves = [fc for fc in anim.fcurves(obj) if fc.data_path in paths]
        changed += smoothing.smooth_curves(curves, first, last, frames)
    if changed:
        print(f"VR Sculpt puppet take: smoothed {changed} curves over {frames:.1f} frames", flush=True)
    return changed


def tick(context, ss):
    """App timer: keep bound targets on their sources and run the take."""
    if ss is None:
        return
    apply(context, ss)
    if _take["state"] == 'ENDING':
        stop(context, push_undo=True)
    elif _take["state"] == 'COUNTDOWN':
        left = countdown_left()
        beeps = int(COUNTDOWN - left)
        if beeps > _take["beeps"]:
            _take["beeps"] = beeps
            _beep(context, 0.3)
        if left <= 0.0:
            _beep(context, 0.9)
            start_take(context)
    elif _take["state"] == 'RECORDING':
        if not anim.is_playing(context) and _take["last_frame"] is not None:
            stop(context, push_undo=True)
            return
        record_frame(context, ss)


def _beep(context, amplitude):
    from . import actions, operators
    for path in actions.HANDS:
        operators.haptic(context, path, amplitude, 0.06)


def end_session(context):
    """The VR session is over: no sources left to follow."""
    unbind_all()
    _grabs.clear()
    if _take["state"] is not None:
        stop(context, push_undo=True)


@bpy.app.handlers.persistent
def _on_frame_change(scene, depsgraph=None):
    # Playback just evaluated the action for this frame: put the bound targets back on their sources
    # before the frame is drawn and keyed.
    if _take["state"] != 'RECORDING' and not _bindings:
        return
    context = bpy.context
    ss = context.window_manager.xr_session_state
    if ss is None:
        return
    if _take["state"] == 'RECORDING':
        record_frame(context, ss)
    else:
        apply(context, ss)


def draw(context, scale):
    """A line from each source to what it is bound to."""
    if not _bindings:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    ss = context.window_manager.xr_session_state
    if ss is None:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    for source, follow in _bindings.values():
        found = follow.target()
        if found is None:
            continue
        obj, pb = found
        end = obj.matrix_world @ pb.head if pb is not None else obj.matrix_world.translation
        start = source_pose(context, ss, source).translation
        batch = batch_for_shader(shader, 'LINES', {"pos": [start, end]})
        shader.uniform_float("color", (*SOURCE_COLORS[source], 0.6))
        batch.draw(shader)
    gpu.state.blend_set('NONE')


def register():
    bpy.app.handlers.frame_change_post.append(_on_frame_change)


def unregister():
    if _on_frame_change in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_on_frame_change)
    _bindings.clear()
    _grabs.clear()
