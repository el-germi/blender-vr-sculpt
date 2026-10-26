"""Thumbstick navigation with a Walk and a Fly mode, switchable while VR runs.

The stock fly actions (left stick: forward/back/left/right, right stick: up/down/turn) are
rebound to vrsculpt.navigate when the session starts (actions.py), so the mode is read live
instead of being baked into the action map. Left Handed swaps the sticks' jobs (SWAPPED).

Walk: the left stick moves on the horizontal plane and the floor follows the scene's surfaces
under you (stairs, slopes), the right stick snap-turns. Fly: the left stick flies where you
look, the right stick goes up/down and turns smoothly.

The grips are ours too (vrsculpt.grab on the stock nav_grab action): Blender's own grab only
moves and zooms. One grip holds the world in that hand, so it follows the hand and, with Grip
set to Turn (settings.nav_grab_turn), turns with the wrist about the vertical axis; Free also
lets it tilt. Both grips zoom with the distance between the hands and turn with the line between
them. The work is done in tracking space (the poses with the navigation taken out), against the
navigation and hand poses from when the set of held grips last changed, so nothing drifts.
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

SCROLL_ACTIONS = ("fly_up", "fly_down", "fly_turnleft", "fly_turnright")  # the right stick
# Left Handed: each stick does the other's job (the bindings stay; the actions read swapped).
SWAPPED = {"fly_forward": "fly_up", "fly_back": "fly_down", "fly_left": "fly_turnleft",
           "fly_right": "fly_turnright"}
SWAPPED.update({v: k for k, v in SWAPPED.items()})
SCROLL_RATE = 12.0  # wheel notches per second at full tilt, over the desktop screen
TURN_SPEED = math.radians(90.0)  # Fly: radians per second at full tilt
DEAD_ZONE = 0.3  # stick values below this count as zero (the bindings' threshold)
STEP_UP = 0.3  # Walk: hand-space meters the floor may rise at once (stairs, not tables)
FALL_MAX = 3.0  # Walk: hand-space meters to look down for a floor
FLOOR_RATE = 12.0  # Walk: how fast the floor height catches up (1/seconds)
GRAB_ACTION = "nav_grab"  # the stock grip action, rebound to vrsculpt.grab (actions.py)
GRAB_MIN_SPAN = 0.02  # meters between the hands below which two grips don't zoom or turn
GRAB_SCALE = (1e-3, 1e4)  # the navigation scale a two-hand zoom stays within
GRAB_ALIVE = 0.25  # seconds without a step after which a grab counts as gone (a modal Blender dropped)


def navigation_matrix(ss):
    s = ss.navigation_scale
    return Matrix.LocRotScale(Vector(ss.navigation_location), Quaternion(ss.navigation_rotation), Vector((s, s, s)))


def set_navigation(ss, matrix, scale=False):
    loc, rot, size = matrix.decompose()
    ss.navigation_location = loc
    ss.navigation_rotation = rot
    if scale:
        ss.navigation_scale = size[0]


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
    """Thumbstick navigation (Walk or Fly, from the VR Authoring panel)"""
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
        settings = context.scene.vr_sculpt
        role = SWAPPED[xr.action] if settings.left_handed else xr.action
        self.kind, self.x, self.y = ACTIONS[role]
        self.value = xr.state[0]
        self.last = time.perf_counter()
        ss = context.window_manager.xr_session_state
        self.floor = FloorFollower(context, ss) if self.kind == 'MOVE' and settings.nav_mode == 'WALK' else None
        from . import desktop
        if role in SCROLL_ACTIONS and ss is not None and desktop.pointing_at_content(context, ss):
            # Main stick while pointing at the desktop screen: the mouse wheel.
            self.kind = 'SCROLL' if role in ("fly_up", "fly_down") else 'HOLD'
            self.notches = 0.0
            if self.kind == 'SCROLL':
                desktop.scroll(context, ss, self.x)
        elif self.kind == 'TURN' and settings.nav_mode == 'WALK':
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
        elif self.kind == 'SCROLL':
            from . import desktop
            # A first notch on push (in invoke), then a steady rate after a short delay.
            self.notches += SCROLL_RATE * ramp(self.value) * dt
            while self.notches >= 1.0:
                self.notches -= 1.0
                desktop.scroll(context, ss, self.x)

    def _finish(self, context):
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        ss = context.window_manager.xr_session_state
        if self.floor is not None and ss is not None:
            self.floor.finish(ss)
        return {'FINISHED'}


def twist_z(q):
    """The angle of q's rotation about the vertical axis (its swing-twist twist)."""
    return 2.0 * math.atan2(q.z, q.w)


def grab_motion(how, start, now):
    """How the hands moved the world in tracking space, as a matrix: start / now map hand index ->
    (location, rotation) in tracking space, both with the same hands. how: settings.nav_grab_turn."""
    if len(now) == 1:
        (p0, q0), = start.values()
        (p1, q1), = now.values()
        if how == 'FREE':
            return Matrix.LocRotScale(p1, q1, None) @ Matrix.LocRotScale(p0, q0, None).inverted()
        angle = twist_z(q1 @ q0.inverted()) if how == 'TURN' else 0.0
        return Matrix.Translation(p1) @ Matrix.Rotation(angle, 4, 'Z') @ Matrix.Translation(-p0)
    (a0, _qa0), (b0, _qb0) = (start[i] for i in sorted(start))
    (a1, _qa1), (b1, _qb1) = (now[i] for i in sorted(now))
    mid0, mid1 = (a0 + b0) / 2, (a1 + b1) / 2
    span0, span1 = b0 - a0, b1 - a1
    k = span1.length / span0.length if span0.length > GRAB_MIN_SPAN and span1.length > GRAB_MIN_SPAN else 1.0
    angle = 0.0
    if how != 'OFF' and span0.xy.length > GRAB_MIN_SPAN and span1.xy.length > GRAB_MIN_SPAN:
        angle = math.atan2(span1.y, span1.x) - math.atan2(span0.y, span0.x)
    return (Matrix.Translation(mid1) @ Matrix.Rotation(angle, 4, 'Z') @ Matrix.Diagonal((k, k, k, 1.0)) @
            Matrix.Translation(-mid0))


class Grab:
    """The world held by one or both grips. step() takes the holding hands' world poses (read with the
    current navigation) and moves the navigation so the world keeps its place in those hands."""

    def __init__(self):
        self.start = None  # hand index -> tracking (location, rotation), from when the hands changed
        self.nav0 = None
        self.last = None  # the world poses last read, to skip stale ones

    def step(self, ss, settings, world):
        """world: hand index -> (location, rotation) in world space, the hands that hold."""
        reading = tuple((i, tuple(p), tuple(q)) for i, (p, q) in sorted(world.items()))
        if reading == self.last:
            # No new tracking since we last moved the world: the poses are still the ones read with the
            # navigation before that move, so they'd count it twice.
            return False
        self.last = reading
        nav = navigation_matrix(ss)
        inv = nav.inverted()
        now = {}
        for i, (p, q) in world.items():
            loc, rot, _s = (inv @ Matrix.LocRotScale(Vector(p), Quaternion(q), None)).decompose()
            now[i] = (loc, rot)
        if self.start is None or set(self.start) != set(now):
            self.start, self.nav0 = now, nav
            return False
        moved = self.nav0 @ grab_motion(settings.nav_grab_turn, self.start, now).inverted()
        if len(now) == 2:
            s = moved.to_scale()[0]
            lo, hi = GRAB_SCALE
            if not lo <= s <= hi:
                # Past the zoom limits: hold the scale there and start over from here.
                self.start, self.nav0 = now, nav
                return False
        set_navigation(ss, moved, scale=len(now) == 2)
        return True


class VRSCULPT_OT_grab(bpy.types.Operator):
    """Grip navigation: hold the world in one hand (move, turn) or both (also zoom)"""
    bl_idname = "vrsculpt.grab"
    bl_label = "VR Grab World"
    bl_options = {'INTERNAL'}

    alive = 0.0  # perf_counter() of the running one's last step: one operator follows both grips

    @classmethod
    def poll(cls, context):
        return bpy.types.XrSessionState.is_running(context)

    def invoke(self, context, event):
        xr = event.xr
        if xr is None or time.perf_counter() - VRSCULPT_OT_grab.alive < GRAB_ALIVE:
            return {'CANCELLED'}  # the other grip's grab is running and takes this hand in
        self.threshold = xr.float_threshold
        self.first = actions.hand_index(xr.user_path)
        self.grab = Grab()
        self.timer = context.window_manager.event_timer_add(1.0 / 90.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        self._step(context, context.window_manager.xr_session_state)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            import traceback
            traceback.print_exc()
            return self._finish(context)

    def _modal(self, context, event):
        ss = context.window_manager.xr_session_state
        if ss is None or not bpy.types.XrSessionState.is_running(context):
            return self._finish(context)
        if event.type == 'TIMER' or (event.type == 'XR_ACTION' and event.xr.action == GRAB_ACTION):
            if not self._step(context, ss):
                return self._finish(context)
        return {'PASS_THROUGH'}

    def held(self, context, ss):
        out = set()
        for i, path in enumerate(actions.HANDS):
            value = ss.action_state_get(context, actions.ACTIONMAP, GRAB_ACTION, path)
            if not isinstance(value, float):
                value = value[0]
            if value > self.threshold:
                out.add(i)
        if self.first is not None:
            # The press that started us, before its state reads as held.
            out.add(self.first)
            self.first = None
        return out

    def _step(self, context, ss):
        """Follow the held grips; False once none holds."""
        hands = self.held(context, ss)
        if not hands:
            return False
        VRSCULPT_OT_grab.alive = time.perf_counter()
        world = {i: (ss.controller_grip_location_get(context, i), ss.controller_grip_rotation_get(context, i))
                 for i in hands}
        self.grab.step(ss, context.scene.vr_sculpt, world)
        return True

    def _finish(self, context):
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        VRSCULPT_OT_grab.alive = 0.0
        return {'FINISHED'}


def register():
    bpy.utils.register_class(VRSCULPT_OT_navigate)
    bpy.utils.register_class(VRSCULPT_OT_grab)


def unregister():
    bpy.utils.unregister_class(VRSCULPT_OT_grab)
    bpy.utils.unregister_class(VRSCULPT_OT_navigate)
