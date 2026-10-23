"""Windowed check of Rig mode: a spine and mirrored arms built with simulated trigger presses (from a
timer with no area, like XR operators), the hand panel's Rig pages flat-on, and the VR overlay in
the Bones tool (bones, joints, the rubber band, the mirror plane) and the Weights tool (the heat map
of a bone's automatic weights, after a painted stroke). Writes tests/rig_preview_result.txt,
tests/rig_panels.png (Bones, Bones + More, Weights before Bind), tests/rig_panels_weights.png
(Weights, Weights + More, the bone picker), tests/rig_bones.png and tests/rig_weights.png.

blender --factory-startup test_scene.blend --python tests/rig_preview.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "rig_preview_result.txt")
lines = []
failures = []
errors = []
panels = []  # (matrix, widgets, height, hover_id)
later = []  # the Weights pages, shot second
state = {"overlay": False}

HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand: unregister it first (and drop this script's draw
    handlers), or its GPU offscreens are freed after Blender's own teardown and the exit crashes."""
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


class FakeSessionState:
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.viewer_pose_location = Vector((0.0, -1.6, 1.3))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        level = Quaternion((1, 0, 0), math.pi / 2)  # pointing at +Y
        self.aim_loc = [Vector((-0.45, -0.45, 0.9)), Vector((0.62, -0.2, 1.2))]
        self.aim_rot = [level, level]

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


def draw():
    from vr_sculpt import draw as vr_draw, vr_ui
    try:
        if state["overlay"]:
            vr_draw.draw_overlay(bpy.context, fake, xr=True)
        for m, widgets, height, hover in panels:
            vr_ui.draw_panel(m, widgets, height, hover)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def at(p):
    return Matrix.LocRotScale(Vector(p), Quaternion(), None)


def press(ctx, *points, left=False):
    from vr_sculpt import rig
    s = ctx.scene.vr_sculpt
    for p in points:
        h = rig.pick(ctx, s, left, at(p), 1.0)
        if h is not None:
            h.finish(ctx)


def setup():
    import vr_sculpt
    from test_rig import make_body
    from vr_sculpt import menus, native, rig, vr_ui
    try:
        vr_sculpt.register()
        from vr_sculpt import live
        bpy.app.timers.unregister(live._tick)  # No VR session: rig.tick() is called below instead.
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.shading.type = 'SOLID'
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        s = ctx.scene.vr_sculpt
        s.show_desktop = s.camera_preview = s.show_timeline = False
        s.grab_delay = 0.0
        s.tip_offset = 0.0
        vr_ui.ANIMATE = False
        with ctx.temp_override(window=win, area=area, region=region):
            body = make_body(bpy.context)

        # From a timer with no area, like the hand panel's buttons and the trigger in VR.
        ctx.view_layer.objects.active = body
        menus.set_mode(ctx, 'RIG')
        press(ctx, (0, 0, 0.45), (0, 0, 0.85), (0, 0, 1.2), (0, 0, 1.55))
        arm = rig.target(ctx)
        check(arm is not None and arm.mode == 'EDIT' and len(arm.data.edit_bones) == 3,
              "a three-bone spine from four presses")
        s.rig_mirror = True
        press(ctx, (0.004, 0, 1.2), (0.3, 0, 1.3), (0.55, 0, 1.25))
        names = sorted(arm.data.edit_bones.keys())
        check(len(names) == 7 and any(n.endswith(".R") for n in names), f"mirrored arms: {names}")
        press(ctx, (0.004, 0, 1.55))  # The rubber band runs from the top joint.

        def add(into, x, hover=None):
            widgets, height = menus.layout(ctx)
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.12)), Vector((x, -0.6, 0.0)), 1.0)
            into.append((m, widgets, height, hover))
            log(f"panel at {x}: {len(widgets)} widgets, height {height:.3f}")
            check(height < 0.32, f"panel at {x} fits")

        menus.reset()
        add(panels, -0.235, "rig_end")
        menus.set_more('RIG', True)
        add(panels, 0.0, "rig_mirror")
        rig.set_tool(ctx, 'WEIGHTS')
        add(panels, 0.235, "rig_bind")
        log(rig.bind(ctx))
        check(any(m.type == 'ARMATURE' for m in body.modifiers), "bound with automatic weights")
        rig.set_active_bone(arm, arm.data.bones[1].name)
        add(later, -0.235, "wbrush_ADD")
        menus.set_more('RIG_WEIGHTS', True)
        add(later, 0.0, "weight_normalize")
        menus.open_bone_picker(ctx)
        add(later, 0.235, None)
        menus.close_picker()
        menus.reset()

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, 0.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 0.62
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (shoot_panels(win, area, region), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def screenshot(win, area, region, name):
    area.tag_redraw()
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
    path = os.path.join(ROOT, "tests", name)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    log("saved", path, "draw errors so far:", len(errors))


def look_at_rig(area, distance=1.5):
    rv3d = area.spaces.active.region_3d
    rv3d.view_location = (0.05, 0.0, 1.05)
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
    rv3d.view_distance = distance


def shoot_panels(win, area, region):
    from vr_sculpt import rig
    try:
        screenshot(win, area, region, "rig_panels.png")
        panels[:] = later
        screenshot(win, area, region, "rig_panels_weights.png")
        panels.clear()
        ctx = bpy.context
        s = ctx.scene.vr_sculpt
        s.show_panel = False
        rig.set_tool(ctx, 'BONES')
        rig._state["anchor"] = None
        press(ctx, (0.004, 0, 1.55))  # Anchored at the top joint: the rubber band goes to the right tip.
        state["overlay"] = True
        look_at_rig(area)
        bpy.app.timers.register(lambda: (shoot_bones(win, area, region), None)[1], first_interval=0.4)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def shoot_bones(win, area, region):
    from vr_sculpt import rig
    try:
        screenshot(win, area, region, "rig_bones.png")
        ctx = bpy.context
        s = ctx.scene.vr_sculpt
        rig.set_tool(ctx, 'WEIGHTS')
        arm = rig.target(ctx)
        body = bpy.data.objects["Body"]
        spine = arm.data.bones[1].name
        rig.set_active_bone(arm, spine)
        # A stroke down the front: the spine's weights reach lower.
        s.weight_radius, s.weight_strength = 0.06, 1.0
        h = rig.pick(ctx, s, False, at((0.0, -0.15, 0.95)), 1.0)
        check(isinstance(h, rig.PaintWeights), "a press on the front paints the spine bone")
        for z in (0.95, 0.9, 0.85, 0.8, 0.75):
            h.dab((0.0, -0.15, z), 1.0, 0.1)
        h.finish(ctx)
        rig.tick(ctx)
        check(rig._heat["co"] is not None and (body.name, spine) in rig._layers, "the heat map's geometry and weights")
        fake.aim_loc[1] = Vector((0.0, -0.17, 0.8))  # The right tip on the painted stroke.
        fake.aim_loc[0] = Vector((-0.25, -0.4, 1.45))
        bpy.app.timers.register(lambda: (shoot_weights(win, area, region), None)[1], first_interval=0.4)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def shoot_weights(win, area, region):
    from vr_sculpt import rig
    try:
        screenshot(win, area, region, "rig_weights.png")
        check(rig._heat["batch"] is not None, "the heat map was drawn")
        if errors:
            failures.append("draw errors")
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
