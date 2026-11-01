"""Windowed check that a real undo while a VR trigger action is held doesn't crash: the trigger
operator (driven with fake XR events) holds a tape measure, then a pose grab, while Blender undoes;
the next event must drop the action instead of touching freed data. Also checks that trigger
actions after the undo work, that each finished trigger action is its own undo step, and that a native
dyntopo stroke undoes dab by dab without crashing.
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


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand, so Blender won't disable it on exit: unregister it
    first, or its GPU offscreens are freed after Blender's own teardown and the exit crashes."""
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        import traceback
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


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
    from vr_sculpt import live, measure, menus, native, operators, pose, rest
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

        # A native dyntopo stroke: each dab is its own undo step, and undoing them doesn't crash. (With the
        # trigger operator's UNDO step on top of dabs that had none, Blender crashed in BM_log_undo.)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.3, location=(0.0, 2.0, 1.0),
                                             calc_uvs=True)
        ball = bpy.context.active_object
        name, n0 = ball.name, len(ball.data.vertices)  # Undo frees Blender data: look it up again after.
        settings = bpy.context.scene.vr_sculpt  # The undos above freed the old references.
        wm.windows = bpy.context.window_manager.windows
        settings.smooth_strokes = False  # The stabilizer is time-based; these updates come all at once.

        class WinCtx(Ctx):
            window = wm.windows[0]  # Native dabs aim the desktop view (an XR action's context has a window).
        settings.engine = 'NATIVE'
        menus.set_mode(bpy.context, 'SCULPT')
        native.activate_brush(bpy.context, "Draw")
        native.toggle_dyntopo(bpy.context)
        sculpt = bpy.context.tool_settings.sculpt
        sculpt.detail_type_method = 'CONSTANT'
        sculpt.constant_detail_resolution = 100.0
        check(ball.mode == 'SCULPT' and ball.use_dynamic_topology_sculpting, "native sculpting with dyntopo")
        top = 1.3 + settings.tip_offset  # The tip (below the controller) on top of the ball.
        op = harness()
        ss.loc = Vector((-0.1, 2.0, top))
        r = op.invoke(WinCtx(), FakeEvent('XR_ACTION', 'PRESS', FakeXR(ss.loc)))
        check(r == {'RUNNING_MODAL'} and isinstance(op.stroke, native.NativeStroke), "a native stroke")
        for i in range(1, 8):
            ss.loc = Vector((-0.1 + 0.03 * i, 2.0, top))
            op.last_update = 0.0
            op.modal(WinCtx(), FakeEvent('TIMER'))
        r = op.modal(WinCtx(), FakeEvent('XR_ACTION', 'RELEASE', FakeXR(ss.loc)))
        dabs = op.stroke.dabs
        check(r == {'FINISHED'} and dabs >= 4, f"it finishes ({dabs} dabs)")
        for i in range(dabs):
            undo("undo")
            if bpy.context.active_object.mode != 'SCULPT':
                break
        ball = bpy.data.objects[name]
        check(ball.mode == 'SCULPT' and ball.use_dynamic_topology_sculpting,
              f"{dabs} undos take back the {dabs} dabs, one each, still in Sculpt Mode with dyntopo")
        undo("undo")  # Dynamic topology enable, back to before the ball (added without a step of its own)
        ball = bpy.data.objects.get(name)
        check(ball is None or not (ball.mode == 'SCULPT' and ball.use_dynamic_topology_sculpting),
              "one more leaves the dyntopo session: exactly one undo step per dab")

        # Undo back across Show texture, into the session before it: Blender rebuilds that session's mesh
        # from its dyntopo log with no UVs, material indices or rest positions; rest.py gives them back.
        menus.set_mode(bpy.context, 'TOOLS')
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.3, location=(0.0, 2.0, 1.0),
                                             calc_uvs=True)
        ball = bpy.context.active_object
        name = ball.name
        for m in ("Red", "Blue"):
            ball.data.materials.append(bpy.data.materials.new(m))
        for p in ball.data.polygons:
            p.material_index = 1 if p.center.z > 0 else 0
        settings = bpy.context.scene.vr_sculpt  # The undo above took the engine setting back too.
        settings.smooth_strokes = False
        settings.engine = 'NATIVE'
        push("two materials")
        menus.set_mode(bpy.context, 'SCULPT')
        native.activate_brush(bpy.context, "Draw")
        native.toggle_dyntopo(bpy.context)

        def stroke():
            op = harness()
            ss.loc = Vector((-0.1, 2.0, top))
            op.invoke(WinCtx(), FakeEvent('XR_ACTION', 'PRESS', FakeXR(ss.loc)))
            for i in range(1, 6):
                ss.loc = Vector((-0.1 + 0.03 * i, 2.0, top))
                op.last_update = 0.0
                op.modal(WinCtx(), FakeEvent('TIMER'))
            op.modal(WinCtx(), FakeEvent('XR_ACTION', 'RELEASE', FakeXR(ss.loc)))
            return op.stroke.dabs

        stroke()
        with ctx.temp_override(window=win, area=area, region=region):
            native.refresh_texture(bpy.context)
        dabs = stroke()
        for i in range(dabs):
            undo("undo")
        for i in range(3):  # Show texture: leaving, the restored mesh, entering.
            undo("undo")
            ball = bpy.data.objects[name]
            check(len(ball.data.uv_layers) and len({p.material_index for p in ball.data.polygons}) == 2,
                  f"undo {i + 1} across Show texture lands on the whole mesh ({ball.mode})")
        undo("undo")  # A dab of the session before.
        ball = bpy.data.objects[name]
        check(ball.mode == 'SCULPT' and ball.use_dynamic_topology_sculpting, "back in the earlier session")
        menus.set_object_mode(bpy.context, ball, 'OBJECT')
        ball = bpy.data.objects[name]
        me = ball.data
        check("UVMap" in me.uv_layers and {p.material_index for p in me.polygons} == {0, 1}
              and not rest.is_open(me), "leaving Sculpt Mode: UVs and both materials back")
        wrong = sum((p.material_index == 1) != (p.center.z > 0) for p in me.polygons if abs(p.center.z) > 0.03)
        check(wrong == 0, f"each face on its own material ({wrong} wrong)")
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
