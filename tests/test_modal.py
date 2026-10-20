"""Drive the stroke operator's invoke/modal with fake XR and timer events.

blender --background --factory-startup --python tests/test_modal.py

Real bpy Events can't be built from Python, so these stand-ins only carry the
attributes the real ones have (notably: no `timer` attribute).
"""

import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import actions, operators  # noqa: E402

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


class FakeXR:
    def __init__(self, loc, value=1.0, hand=actions.HANDS[1], action=actions.ACTION_STROKE):
        self.action_set = actions.ACTIONMAP
        self.action = action
        self.user_path = hand
        self.controller_location = Vector(loc)
        # Aim straight down -Z of the world, so the tip offset points down.
        self.controller_rotation = Quaternion()
        self.state = (value, 0.0)
        self.float_threshold = 0.3


class FakeEvent:
    __slots__ = ("type", "value", "xr")

    def __init__(self, type, value='NOTHING', xr=None):
        self.type, self.value, self.xr = type, value, xr


class FakeSessionState:
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -1.0, 1.5))

    def __init__(self):
        self.trigger = 1.0
        self.loc = Vector((0, 0, 1.2))
        self.fail = False
        self.haptics = 0

    def action_state_get(self, context, action_set, action, user_path):
        return self.trigger

    def controller_aim_location_get(self, context, index):
        if self.fail:
            raise RuntimeError("simulated failure")
        return self.loc.copy()

    def controller_aim_rotation_get(self, context, index):
        return Quaternion()

    def controller_grip_location_get(self, context, index):
        return Vector((-0.3, -0.5, 1.0))

    def haptic_action_apply(self, *args):
        self.haptics += 1
        return True


class FakeWM:
    def __init__(self, ss):
        self.xr_session_state = ss
        self.timers = set()
        self.windows = bpy.context.window_manager.windows

    def event_timer_add(self, step, window=None):
        t = object()
        self.timers.add(t)
        return t

    def event_timer_remove(self, t):
        self.timers.remove(t)

    def modal_handler_add(self, op):
        return True


class FakeContext:
    """Overrides a few members; everything else comes from bpy.context."""

    def __getattr__(self, name):
        return getattr(bpy.context, name)

    def __init__(self, wm, obj):
        self.window_manager = wm
        self.scene = bpy.context.scene
        self.active_object = obj
        self.window = None
        self.screen = None


def harness():
    """An object with the operator's methods, without Blender's operator machinery."""
    cls = operators.VRSCULPT_OT_stroke
    h = type("StrokeHarness", (), {k: cls.__dict__[k] for k in ("invoke", "modal", "_modal", "_apply", "_finish", "_abandon", "_start_modal")})()
    h.report = lambda kind, msg: print("    report:", kind, msg)
    return h


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3)


def main():
    vr_sculpt.register()
    operators.session_running = lambda context: True
    try:
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=5, radius=1.0)
        obj = bpy.context.active_object
        settings = bpy.context.scene.vr_sculpt
        settings.radius = 0.2
        settings.tip_offset = 0.1  # tip 0.1 below the controller along -Z
        settings.brush = 'DRAW'

        print("== timer-driven stroke")
        ss = FakeSessionState()
        wm = FakeWM(ss)
        ctx = FakeContext(wm, obj)
        op = harness()
        before = positions(obj).copy()
        # Controller 0.1 above the top of the sphere, so the tip sits on it.
        r = op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0, 0, 1.1))))
        check(r == {'RUNNING_MODAL'}, f"invoke starts the modal ({r})")
        check(len(wm.timers) == 1, "invoke adds one timer")
        for i in range(10):
            time.sleep(0.01)
            ss.loc = Vector((-0.2 + 0.04 * i, 0, 1.1))
            r = op.modal(ctx, FakeEvent('TIMER'))
            check(r == {'PASS_THROUGH'}, f"timer tick {i} keeps running") if i in (0, 9) else None
        check(op.n_timer >= 9, f"timer ticks polled the controller ({op.n_timer})")
        moved = np.linalg.norm(positions(obj) - before, axis=1).max()
        check(moved > 1e-3, f"timer-driven stroke sculpted (max move {moved:.4f})")
        check(ss.haptics > 0, "haptics fired on contact")

        print("== XR-event-driven updates and other events")
        for i in range(5):
            op.modal(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.2, 0.05 * i, 1.1))))
        check(op.n_xr == 5, f"XR events update the stroke ({op.n_xr})")
        other = op.modal(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0, 0, 1.1), hand=actions.HANDS[0])))
        check(other == {'PASS_THROUGH'}, "other hand's events pass through")
        check(op.modal(ctx, FakeEvent('MOUSEMOVE')) == {'PASS_THROUGH'}, "mouse events pass through")

        print("== releasing the trigger")
        ss.trigger = 0.0
        time.sleep(0.01)
        r = op.modal(ctx, FakeEvent('TIMER'))
        check(r == {'FINISHED'}, f"trigger below threshold finishes ({r})")
        check(len(wm.timers) == 0, "timer removed on finish")
        check(operators.stats["dabs"] > 0, f"stats recorded ({operators.stats['dabs']} dabs)")

        print("== release event")
        ss.trigger = 1.0
        op = harness()
        op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0, 0, 1.1))))
        r = op.modal(ctx, FakeEvent('XR_ACTION', 'RELEASE', FakeXR((0, 0, 1.1), value=0.0)))
        check(r in ({'FINISHED'}, {'CANCELLED'}) and not wm.timers, f"XR release ends the stroke ({r})")

        print("== errors inside the modal")
        op = harness()
        op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0, 0, 1.1))))
        ss.fail = True
        time.sleep(0.01)
        r = op.modal(ctx, FakeEvent('TIMER'))
        check(r == {'CANCELLED'}, f"an exception cancels the stroke ({r})")
        check(len(wm.timers) == 0, "...and removes its timer")
        check(not operators.active_hands, "...and clears the active hand")

        print("== action_state_get returning a pair")
        ss.fail = False
        ss.trigger = (0.0, 0.0)
        op = harness()
        op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0, 0, 1.1))))
        time.sleep(0.01)
        r = op.modal(ctx, FakeEvent('TIMER'))
        check(r == {'FINISHED'} or r == {'CANCELLED'}, f"pair state handled ({r})")
        check(len(wm.timers) == 0, "...timer removed")
    except Exception:
        traceback.print_exc()
        failures.append("exception")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "FAILED " + str(failures) if failures else "ALL PASSED")


if __name__ == "__main__":
    main()
