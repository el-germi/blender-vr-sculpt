"""When the hand panel shows.

The panel stays out of the way: it fades in when you check your left wrist like a watch (the back of
the left hand turned toward your face, while you look at it) and fades out when you turn it back.
Pinned, it stays. While the right controller points at it, or holds one of its sliders, it stays
too, and it lingers a moment after, so a glance away doesn't drop it mid-reach. Hidden, it leaves a
small ghost with the mode icon; pointing at the ghost brings the panel back.

Reveal modes (Settings > Reveal): WRIST (the watch check), LOOK (just look at the left hand) and
ALWAYS (always shown, like before).
"""

import math
import time

from mathutils import Quaternion, Vector

from . import actions

WRIST_ON = math.cos(math.radians(55))  # the back of the hand within 55 degrees of the head
WRIST_OFF = math.cos(math.radians(70))
GAZE_ON = math.cos(math.radians(32))  # the hand within 32 degrees of where you look
GAZE_OFF = math.cos(math.radians(48))
LINGER = 0.6  # seconds the panel stays after the reveal ends
FADE_IN = 0.07  # time constants, seconds
FADE_OUT = 0.14

_state = {"shown": False, "wrist": False, "gaze": False, "alpha": 0.0, "until": 0.0, "time": None}


def wrist_turned(left_rot, hand, head, was=False):
    """Whether the back of the left hand (the controller's -X: it's on the outside of a left hand
    holding it) faces the head. was: the last answer, for hysteresis."""
    back = left_rot.to_matrix() @ Vector((-1.0, 0.0, 0.0))
    to_head = Vector(head) - Vector(hand)
    if to_head.length < 1e-6:
        return was
    facing = back.normalized().dot(to_head.normalized())
    return facing > (WRIST_OFF if was else WRIST_ON)


def looking_at(head, head_rot, target, was=False):
    """Whether the head looks toward target (its view direction is -Z)."""
    if head_rot is None:
        return True  # No head rotation (a test fake): count as looking.
    forward = head_rot.to_matrix() @ Vector((0.0, 0.0, -1.0))
    to_target = Vector(target) - Vector(head)
    if to_target.length < 1e-6:
        return was
    look = forward.normalized().dot(to_target.normalized())
    return look > (GAZE_OFF if was else GAZE_ON)


def rule(settings, wrist, gaze):
    """Whether the panel wants to show from the pose alone (no pointing, pin aside)."""
    if settings.panel_reveal == 'ALWAYS':
        return True
    if settings.panel_reveal == 'LOOK':
        return gaze
    return wrist and gaze


def update(context, ss, using=False, now=None):
    """Advance the show / hide state and the fade; returns the panel's alpha. using: the right
    controller points at the panel or its ghost, or holds one of its widgets."""
    settings = context.scene.vr_sculpt
    now = time.monotonic() if now is None else now
    s = _state
    hand = Vector(ss.controller_grip_location_get(context, 0))
    head = Vector(ss.viewer_pose_location)
    head_rot = getattr(ss, "viewer_pose_rotation", None)
    left_rot = Quaternion(ss.controller_aim_rotation_get(context, 0))
    s["wrist"] = wrist_turned(left_rot, hand, head, s["wrist"])
    s["gaze"] = looking_at(head, Quaternion(head_rot) if head_rot is not None else None,
                           hand + Vector((0.0, 0.0, 0.12 * ss.viewer_scale)), s["gaze"])
    pinned = settings.panel_pin
    wants = settings.show_panel and (pinned or using or rule(settings, s["wrist"], s["gaze"]))
    if wants:
        s["until"] = now + (0.0 if pinned else LINGER)
    shown = settings.show_panel and (wants or now < s["until"])
    if shown and not s["shown"] and not pinned and settings.panel_reveal != 'ALWAYS':
        tick(context, ss, 0, 0.12, 0.02)  # A tap on the left hand: the panel is there.
    s["shown"] = shown
    last = s["time"]
    dt = 0.0 if last is None else max(now - last, 0.0)
    target = 1.0 if shown else 0.0
    s["alpha"] = target + (s["alpha"] - target) * math.exp(-dt / (FADE_IN if shown else FADE_OUT))
    if abs(s["alpha"] - target) < 0.004:
        s["alpha"] = target
    s["time"] = now
    return s["alpha"]


def shown(context, ss):
    """Whether the panel takes presses now."""
    update(context, ss)
    return _state["shown"]


def alpha():
    return _state["alpha"]


def reset():
    _state.update(shown=False, wrist=False, gaze=False, alpha=0.0, until=0.0, time=None)


def toggle_pin(context):
    settings = context.scene.vr_sculpt
    settings.panel_pin = not settings.panel_pin
    if settings.panel_pin:
        settings.show_panel = True


def tick(context, ss, hand_idx, amplitude, duration):
    """A short haptic pulse on a controller (0 = left, 1 = right)."""
    try:
        ss.haptic_action_apply(context, actions.ACTIONMAP, actions.ACTION_HAPTIC, actions.HANDS[hand_idx],
                               duration, 0.0, amplitude)
    except Exception:
        pass  # No haptics (no session, or no haptic action): the panel works without.
