"""Windowed check that a real undo while a VR trigger action is held doesn't crash: the trigger
operator (driven with fake XR events) holds a tape measure, then a pose grab, while Blender undoes;
the next event must drop the action instead of touching freed data. Also checks that trigger
actions after the undo work, and that each finished trigger action is its own undo step.
Writes tests/undo_preview_result.txt.

blender --factory-startup test_scene.blend --python tests/undo_preview.py
"""

import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "undo_preview_result.txt")
lines = []
failures = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def check(cond, msg):
    log(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def run():
    import vr_sculpt
    from test_modal import FakeEvent, FakeSessionState, FakeWM, FakeXR, harness
    from vr_sculpt import live, measure, menus, native, operators, pose
    try:
        vr_sculpt.register()
        bpy.app.timers.unregister(live._tick)  # No VR session here.
        operators.session_running = lambda context: True
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)

        def undo(op):
            with ctx.temp_override(window=win, area=area, region=region):
                return getattr(bpy.ops.ed, op)() if op != "push" else None

        def push(message):
            with ctx.temp_override(window=win, area=area, region=region):
                bpy.ops.ed.undo_push(message=message)

        ss = FakeSessionState()
        ss.trigger = 1.0
        wm = FakeWM(ss)

        class Ctx:
            def __getattr__(self, name):
                return getattr(bpy.context, name)

            window_manager = wm
            window = None
            screen = None

        settings = ctx.scene.vr_sculpt
        settings.show_panel = False
        settings.show_desktop = False
        menus.set_mode(ctx, 'TOOLS')
        settings.tool = 'MEASURE'
        push("before measuring")

        # Hold a tape measure, then undo while it's held.
        op = harness()
        ss.loc = Vector((0.0, -0.5, 1.0))
        r = op.invoke(Ctx(), FakeEvent('XR_ACTION', 'PRESS', FakeXR(ss.loc)))
        check(r == {'RUNNING_MODAL'} and isinstance(op.handler, measure.Measure), "measuring")
        ss.loc = Vector((0.2, -0.5, 1.0))
        op.modal(Ctx(), FakeEvent('TIMER'))
        before = ctx.scene.vr_sculpt.as_pointer()
        settings.grid_size = 0.07  # A change, so the undo has to re-read the scene.
        push("grid size")
        undo("undo")
        after = bpy.context.scene.vr_sculpt.as_pointer()
        log(f"scene settings moved by the undo: {before != after}")
        op.last_update = 0.0
        r = op.modal(Ctx(), FakeEvent('TIMER'))  # This crashed Blender before the fix.
        check(r == {'CANCELLED'} and op.handler is None, "the next event drops the held measure")
        check(not measure.active, "and stops drawing it")
        check(len(measure.stored(bpy.context.scene)) == 0, "nothing measured")

        # A new trigger action after the undo works and is an undo step of its own.
        op = harness()
        ss.loc = Vector((0.0, -0.5, 1.0))
        op.invoke(Ctx(), FakeEvent('XR_ACTION', 'PRESS', FakeXR(ss.loc)))
        ss.loc = Vector((0.3, -0.5, 1.0))
        op.last_update = 0.0
        op.modal(Ctx(), FakeEvent('TIMER'))
        r = op.modal(Ctx(), FakeEvent('XR_ACTION', 'RELEASE', FakeXR(ss.loc)))
        check(r == {'FINISHED'} and len(measure.stored(bpy.context.scene)) == 1, "measuring after the undo works")

        # Pose grab held across an undo.
        from test_pose import make_rig
        with ctx.temp_override(window=win, area=area, region=region):
            rig, body = make_rig(bpy.context)
        bpy.context.view_layer.objects.active = body
        menus.set_mode(bpy.context, 'POSE')
        push("posing")
        op = harness()
        ss.loc = Vector((0.67, 0.0, 1.4 + bpy.context.scene.vr_sculpt.tip_offset))  # Tip on the forearm.
        r = op.invoke(Ctx(), FakeEvent('XR_ACTION', 'PRESS', FakeXR(ss.loc)))
        check(r == {'RUNNING_MODAL'} and isinstance(op.handler, pose.Grab), "grabbing a bone")
        ss.loc = ss.loc + Vector((0.0, 0.0, -0.2))
        op.last_update = 0.0
        op.modal(Ctx(), FakeEvent('TIMER'))
        push("posed")  # As if the drag were a finished step; the undo goes back to "posing".
        undo("undo")
        op.last_update = 0.0
        r = op.modal(Ctx(), FakeEvent('TIMER'))
        from vr_sculpt import puppet
        check(r == {'CANCELLED'} and not puppet.live(), "an undo mid-grab drops the grab")
        rig = bpy.data.objects["Puppet"]
        check(rig.pose.bones["Forearm.L"].rotation_quaternion.angle < 1e-6, "and the pose is the undone one")
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
