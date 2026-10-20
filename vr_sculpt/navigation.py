"""Thumbstick navigation with a Walk and a Fly mode, switchable while VR runs.

The stock fly actions (left stick: forward/back/left/right, right stick: up/down/turn) are
rebound to vrsculpt.navigate when the session starts (actions.py), so the mode is read live
instead of being baked into the action map.

Walk: the left stick moves on the horizontal plane and the floor follows the scene's surfaces
under you (stairs, slopes), the right stick snap-turns. Fly: the left stick flies where you
look, the right stick goes up/down and turns smoothly.
"""

import math
import time

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import actions

# action -> (kind, x, y): MOVE uses x/y as right/forward, LIFT and TURN use x as the sign.
ACTIONS = {
    "fly_forward": ('MOVE', 0.0, 1.0),
    "fly_back": ('MOVE', 0.0, -1.0),
    "fly_left": ('MOVE', -1.0, 0.0),
    "fly_right": ('MOVE', 1.0, 0.0),
    "fly_up": ('LIFT', 1.0, 0.0),
    "fly_down": ('LIFT', -1.0, 0.0),
    "fly_turnleft": ('TURN', 1.0, 0.0),
    "fly_turnright": ('TURN', -1.0, 0.0),
}

TURN_SPEED = math.radians(90.0)  # Fly: radians per second at full tilt
DEAD_ZONE = 0.3  # stick values below this count as zero (the bindings' threshold)
STEP_UP = 0.3  # Walk: hand-space meters the floor may rise at once (stairs, not tables)
FALL_MAX = 3.0  # Walk: hand-space meters to look down for a floor
FLOOR_RATE = 12.0  # Walk: how fast the floor height catches up (1/seconds)


def navigation_matrix(ss):
    s = ss.navigation_scale
    return Matrix.LocRotScale(Vector(ss.navigation_location), Quaternion(ss.navigation_rotation), Vector((s, s, s)))


def set_navigation(ss, matrix):
    loc, rot, _scale = matrix.decompose()
    ss.navigation_location = loc
    ss.navigation_rotation = rot


def ramp(value):
    """Stick value past the dead zone, rescaled to 0..1 and eased."""
    t = min(max((abs(value) - DEAD_ZONE) / (1.0 - DEAD_ZONE), 0.0), 1.0)
    return t * t


def move(ss, settings, x, y, value, dt):
    """Move the viewer along its right (x) / forward (y) directions."""
    rot = Quaternion(ss.viewer_pose_rotation)
    forward = rot @ Vector((0.0, 0.0, -1.0))
    right = rot @ Vector((1.0, 0.0, 0.0))
    if settings.nav_mode == 'WALK' or abs(y) < 1e-9:
        # Strafing and walking stay level.
        forward.z = 0.0
        right.z = 0.0
    if forward.length > 1e-6:
        forward.normalize()
    if right.length > 1e-6:
        right.normalize()
    delta = (right * x + forward * y) * settings.nav_speed * ss.viewer_scale * ramp(value) * dt
    ss.navigation_location = Vector(ss.navigation_location) + delta
    return delta


def lift(ss, settings, sign, value, dt):
    delta = Vector((0.0, 0.0, sign * settings.nav_speed * ss.viewer_scale * ramp(value) * dt))
    ss.navigation_location = Vector(ss.navigation_location) + delta
    return delta


def turn(ss, angle):
    """Rotate the world around the viewer (about the vertical axis)."""
    pivot = Vector(ss.viewer_pose_location)
    pivot.z = 0.0
    m = Matrix.Translation(pivot) @ Matrix.Rotation(angle, 4, 'Z') @ Matrix.Translation(-pivot)
    set_navigation(ss, m @ navigation_matrix(ss))


def floor_below(context, ss):
    """World z of the first surface below the headset, or None."""
    head = Vector(ss.viewer_pose_location)
    depsgraph = context.evaluated_depsgraph_get()
    hit, location, _normal, _index, _obj, _m = context.scene.ray_cast(
        depsgraph, head, Vector((0.0, 0.0, -1.0)), distance=FALL_MAX * ss.viewer_scale)
    return location.z if hit else None


class FloorFollower:
    """Walk: keeps your height above the surface under you while you walk over it.

    The VR tracking origin isn't the real floor, so instead of putting you on the surface,
    this follows its changes: a step up lifts you by the step, a slope carries you along. A
    rise over STEP_UP (a table, a wall) is ignored, so you walk through it at the same height.
    """

    def __init__(self, context, ss):
        self.floor = floor_below(context, ss)
        self.pending = 0.0

    def step(self, context, ss, dt):
        z = floor_below(context, ss)
        if z is not None:
            if self.floor is None:
                self.floor = z
            elif z - self.floor <= STEP_UP * ss.viewer_scale:
                self.pending += z - self.floor
                self.floor = z
        # Ease into height changes, so stairs don't jolt.
        d = self.pending * min(FLOOR_RATE * dt, 1.0)
        self.apply(ss, d)

    def apply(self, ss, d):
        if abs(d) > 1e-9:
            loc = Vector(ss.navigation_location)
            loc.z += d
            ss.navigation_location = loc
            self.pending -= d

    def finish(self, ss):
        self.apply(ss, self.pending)


class VRSCULPT_OT_navigate(bpy.types.Operator):
    """Thumbstick navigation (Walk or Fly, from the VR Sculpt settings)"""
    bl_idname = "vrsculpt.navigate"
    bl_label = "VR Navigate"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return bpy.types.XrSessionState.is_running(context)

    def invoke(self, context, event):
        xr = event.xr
        if xr is None or xr.action not in ACTIONS:
            return {'CANCELLED'}
        self.action, self.hand = xr.action, xr.user_path
        self.kind, self.x, self.y = ACTIONS[xr.action]
        self.value = xr.state[0]
        self.last = time.perf_counter()
        settings = context.scene.vr_sculpt
        ss = context.window_manager.xr_session_state
        self.floor = FloorFollower(context, ss) if self.kind == 'MOVE' and settings.nav_mode == 'WALK' else None
        if self.kind == 'TURN' and settings.nav_mode == 'WALK':
            # One snap per push; the rest of the push is ignored (but still ours, so it
            # doesn't start another turn every frame).
            turn(ss, self.x * settings.nav_turn)
            self.kind = 'HOLD'
        self.timer = context.window_manager.event_timer_add(1.0 / 90.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            import traceback
            traceback.print_exc()
            self._finish(context)
            return {'CANCELLED'}

    def _modal(self, context, event):
        ss = context.window_manager.xr_session_state
        if ss is None or not bpy.types.XrSessionState.is_running(context):
            return self._finish(context)
        if event.type == 'XR_ACTION':
            xr = event.xr
            if xr.action != self.action or xr.user_path != self.hand:
                return {'PASS_THROUGH'}
            if event.value == 'RELEASE':
                return self._finish(context)
            self.value = xr.state[0]
            self._step(context, ss)
            return {'RUNNING_MODAL'}
        if event.type == 'TIMER':
            value = ss.action_state_get(context, actions.ACTIONMAP, self.action, self.hand)
            if not isinstance(value, float):
                value = value[0]
            if abs(value) < DEAD_ZONE:
                return self._finish(context)
            self.value = value
            if time.perf_counter() - self.last > 1.0 / 120.0:
                self._step(context, ss)
        return {'PASS_THROUGH'}

    def _step(self, context, ss):
        now = time.perf_counter()
        dt = min(now - self.last, 0.05)
        self.last = now
        settings = context.scene.vr_sculpt
        if self.kind == 'MOVE':
            move(ss, settings, self.x, self.y, self.value, dt)
            if self.floor is not None and settings.nav_mode == 'WALK':
                self.floor.step(context, ss, dt)
        elif self.kind == 'LIFT' and settings.nav_mode == 'FLY':
            lift(ss, settings, self.x, self.value, dt)
        elif self.kind == 'TURN' and settings.nav_mode == 'FLY':
            turn(ss, self.x * TURN_SPEED * ramp(self.value) * dt)

    def _finish(self, context):
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        ss = context.window_manager.xr_session_state
        if self.floor is not None and ss is not None:
            self.floor.finish(ss)
        return {'FINISHED'}


def register():
    bpy.utils.register_class(VRSCULPT_OT_navigate)


def unregister():
    bpy.utils.unregister_class(VRSCULPT_OT_navigate)
