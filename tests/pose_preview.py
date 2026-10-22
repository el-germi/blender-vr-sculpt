"""Windowed check of Pose mode and puppeteering with simulated controllers: entering Pose Mode from
a timer (no area, like XR operators), a bone motion path, a recorded puppet take while the
timeline really plays (ending when playback wraps around), and the VR overlay with the bones and
the Pose page drawn. Writes tests/pose_preview_result.txt and tests/pose_preview.png.

blender --factory-startup test_scene.blend --python tests/pose_preview.py
"""

import math
import os
import sys
import time
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "pose_preview_result.txt")
PNG = os.path.join(ROOT, "tests", "pose_preview.png")
lines = []
failures = []


HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand, so Blender won't disable it on exit: unregister it
    first, or its GPU offscreens are freed after Blender's own teardown and the exit crashes. This
    script's draw handlers go first, since Blender redraws once more before quitting and they could
    render into a new offscreen."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
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


class FakeSessionState:
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.viewer_pose_location = Vector((0.0, -1.6, 1.5))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        down_forward = Quaternion((1, 0, 0), 0.9)
        # Right tip near the left forearm, so it's highlighted.
        self.aim_loc = [Vector((-0.3, -1.0, 1.15)), Vector((0.55, 0.03, 1.4 + 0.035))]
        self.aim_rot = [down_forward, Quaternion((1, 0, 0), math.pi / 2)]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + Vector((0.0, -0.03, -0.03))

    def controller_grip_rotation_get(self, context, i):
        return self.aim_rot[i]

    def haptic_action_apply(self, *a):
        pass


fake = FakeSessionState()
errors = []
take = {"ticks": 0, "frames": set(), "t0": 0.0}


def draw():
    from vr_sculpt import draw as vr_draw
    try:
        vr_draw.draw_overlay(bpy.context, fake, xr=True)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def run():
    import vr_sculpt
    from test_pose import make_rig
    from vr_sculpt import anim, menus, native, paths, pose, puppet
    try:
        vr_sculpt.register()
        from vr_sculpt import live
        # Without a VR session the live timer ends puppeteering; tick() below stands in for it.
        bpy.app.timers.unregister(live._tick)
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        settings = ctx.scene.vr_sculpt
        settings.show_desktop = False
        settings.camera_preview = False
        scene = ctx.scene
        scene.frame_start, scene.frame_end = 1, 12
        scene.frame_set(1)
        for name in ("SculptBall", "HeavyBall_164k"):
            if name in bpy.data.objects:
                bpy.data.objects[name].hide_set(True)
        with ctx.temp_override(window=win, area=area, region=region):
            rig, body = make_rig(bpy.context)
        # From a timer with no area, like the hand panel's buttons in VR.
        ctx.view_layer.objects.active = body
        menus.set_mode(ctx, 'POSE')
        check(rig.mode == 'POSE' and ctx.view_layer.objects.active == rig, "Pose mode enters Pose Mode on the rig")

        # Bind the left hand to the right grip with IK, and record a take while really playing.
        pose.select_bone(rig, rig.pose.bones["Hand.L"])
        settings.pose_tool = 'IK'
        check(puppet.bind(ctx, fake, 'RIGHT') == "Hand.L", "Hand.L bound to the right grip")
        puppet.toggle_record(ctx)
        puppet._take["start"] -= puppet.COUNTDOWN  # Skip the wait.
        take["t0"] = time.monotonic()
        bpy.app.timers.register(lambda: tick(win, area, region), first_interval=0.01)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def tick(win, area, region):
    """Our stand-in for the live timer while the take runs: move the 'hand' and tick the puppet."""
    from vr_sculpt import anim, puppet
    ctx = bpy.context
    try:
        take["ticks"] += 1
        t = time.monotonic() - take["t0"]
        fake.aim_loc[1] = Vector((0.55, 0.03 + 0.1 * math.sin(t * 6), 1.43 - 0.1 * t))
        puppet.tick(ctx, fake)
        if puppet.state() == 'RECORDING':
            take["frames"].add(ctx.scene.frame_current)
        if puppet.state() is None or t > 5.0:
            after_take(win, area, region)
            return None
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()
        return None
    return 1.0 / 90.0


def after_take(win, area, region):
    from vr_sculpt import anim, paths, puppet
    try:
        ctx = bpy.context
        scene = ctx.scene
        rig = bpy.data.objects["Puppet"]
        check(puppet.state() is None and not anim.is_playing(ctx),
              f"the take ended when playback wrapped around ({take['ticks']} ticks)")
        curves = {fc.data_path: fc for fc in anim.fcurves(rig)}
        hand = curves.get('pose.bones["Hand.L"].rotation_quaternion')
        frames = sorted({round(k.co[0]) for k in hand.keyframe_points}) if hand else []
        check(frames == list(range(1, 13)), f"every frame of the range keyed ({frames})")
        fore = curves.get('pose.bones["Forearm.L"].rotation_quaternion')
        check(fore is not None and len(fore.keyframe_points) == 12, "with the IK chain")
        scene.frame_set(1)
        z1 = (rig.matrix_world @ rig.pose.bones["Hand.L"].head).z
        scene.frame_set(12)
        z12 = (rig.matrix_world @ rig.pose.bones["Hand.L"].head).z
        check(z1 - z12 > 0.02, f"the recording plays back: the hand went down ({z1:.3f} -> {z12:.3f})")
        puppet.unbind_all()

        scene.vr_sculpt.show_paths = True
        t0 = time.perf_counter()
        paths.ensure(ctx, force=True)
        pb = rig.pose.bones["Hand.L"]
        check(pb.motion_path is not None and len(pb.motion_path.points) == 12,
              f"the hand's motion path ({(time.perf_counter() - t0) * 1000:.1f} ms)")
        scene.frame_set(6)
        rv3d = area.spaces.active.region_3d
        area.spaces.active.shading.type = 'SOLID'
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, 1.25)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
        rv3d.view_distance = 1.6
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
    bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=0.6)


def shoot(win, area, region):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=PNG)
        log("saved", PNG, "draw errors:", len(errors))
        if errors:
            failures.append("draw errors")
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
