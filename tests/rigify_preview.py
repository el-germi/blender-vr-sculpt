"""Windowed preview of Pose mode on a Rigify rig: generates a Rigify human, poses it the way VR
does, and renders the bones as the headset draws them (custom shapes, controls only), with
Blender's own bone overlay off, the left IK hand hovered and the right FK forearm selected.

blender --factory-startup test_scene.blend --python tests/rigify_preview.py

Writes tests/rigify_preview_result.txt and tests/rigify_preview.png.
"""

import os
import sys
import time
import traceback

import bpy
from mathutils import Quaternion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

LOG = os.path.join(ROOT, "tests", "rigify_preview_result.txt")
PNG = os.path.join(ROOT, "tests", "rigify_preview.png")

lines = []
failures = []
HANDLES = []  # this script's own draw handlers
state = {"tips": []}


def quit_cleanly():
    """Unregister first (see pose_preview.quit_cleanly), our draw handlers before that."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
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


def draw():
    from vr_sculpt import pose
    try:
        pose.draw(bpy.context, state["tips"], 1.0)
    except Exception:
        log(traceback.format_exc())
        failures.append("draw raised")


def run():
    import addon_utils
    import vr_sculpt
    from vr_sculpt import native, pose
    try:
        vr_sculpt.register()
        addon_utils.enable("rigify", default_set=True)
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        for o in list(ctx.scene.objects):
            o.hide_set(True)
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.object.armature_human_metarig_add()
            meta = bpy.context.active_object
            bpy.ops.pose.rigify_generate()
        rig = next(o for o in bpy.data.objects if o.type == 'ARMATURE' and o not in (meta,) and o.visible_get())
        meta.hide_set(True)
        pose.use_rig(ctx, rig)
        check(rig.mode == 'POSE', "the Rigify rig is in Pose Mode")
        pbs = rig.pose.bones
        pbs["hand_ik.L"].location = (-0.1, 0.2, 0.25)  # A raised IK arm...
        pbs["upper_arm_parent.R"]["IK_FK"] = 1.0  # ...and a bent FK one.
        rig.update_tag()
        pbs["forearm_fk.R"].rotation_quaternion = Quaternion((1, 0, 0), 0.8)
        ctx.view_layer.update()
        pose.select_bone(rig, pbs["forearm_fk.R"])
        hand = rig.matrix_world @ pbs["hand_ik.L"].head
        state["tips"] = [hand]
        hit = pose.nearest_bone(ctx, hand, 1.0, rig)
        check(hit is not None, f"a tip at the IK hand hovers a control ({hit and hit[0].name})")
        t0 = time.perf_counter()
        geo = pose.geometry(rig, fresh=True)
        log(f"  {len(geo.bones)} controls, {len(geo.coords) // 2} lines, built in {(time.perf_counter() - t0) * 1000:.1f} ms")

        space = area.spaces.active
        space.overlay.show_bones = False  # Only what the headset draws.
        space.shading.type = 'SOLID'
        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, 1.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
        rv3d.view_distance = 3.2
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()
        return
    bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=0.6)


def shoot(win, area, region):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=PNG)
        log("saved", PNG)
    except Exception:
        log(traceback.format_exc())
        failures.append("screenshot raised")
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=0.5)
