"""Windowed check of Physics mode: a box, a pinned curtain, a soft blob and a rig with spring bones set
up from a timer with no area (like XR operators), the hand panel's Physics pages flat-on, and the VR
overlay with Hands on (the spring bones, a pull ring at the right hand, the hand colliders). Writes
tests/physics_preview_result.txt, tests/physics_panels.png (Rigid, Rigid + More, Cloth),
tests/physics_panels_more.png (Soft, spring bones + More, the Simulation sheet with Hands on) and
tests/physics_scene.png.

blender --factory-startup test_scene.blend --python tests/physics_preview.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
LOG = os.path.join(ROOT, "tests", "physics_preview_result.txt")
lines = []
failures = []
errors = []
panels = []  # (matrix, widgets, height, hover_id)
later = []  # the second row of pages, shot second
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
        self.aim_loc = [Vector((-0.55, -0.35, 0.7)), Vector((0.35, -0.3, 0.95))]
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


def setup():
    import vr_sculpt
    from test_physics import cube, make_rig
    from vr_sculpt import menus, native, physics, primitives, vr_ui
    try:
        vr_sculpt.register()
        from vr_sculpt import live
        bpy.app.timers.unregister(live._tick)  # No VR session.
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
        s.tip_offset = 0.0
        vr_ui.ANIMATE = False
        with ctx.temp_override(window=win, area=area, region=region):
            floor = cube("Floor", (0, 0, -0.05), 2.0)
            floor.scale.z = 0.05
            box = cube("Box", (-0.45, 0.0, 0.4))
            bpy.ops.mesh.primitive_grid_add(x_subdivisions=16, y_subdivisions=16, size=0.6, location=(0.0, 0.2, 0.75),
                                            rotation=(math.pi / 2, 0.0, 0.0))
            curtain = ctx.active_object
            curtain.name = "Curtain"
            bpy.ops.mesh.primitive_uv_sphere_add(radius=0.12, location=(0.45, 0.0, 0.3))
            blob = ctx.active_object
            blob.name = "Blob"
            arm = make_rig(bpy.context)
            arm.location = (0.3, 0.3, 0.0)
            arm.scale = (0.6, 0.6, 0.6)

        # From a timer with no area, like the hand panel's buttons and the trigger in VR.
        menus.set_mode(ctx, 'PHYSICS')
        physics.set_kind(ctx, floor, 'STATIC')
        physics.set_kind(ctx, box, 'RIGID')
        physics.set_kind(ctx, curtain, 'CLOTH')
        ctx.view_layer.update()
        physics.set_pin(curtain, physics.PIN_TOP)
        physics.set_cloth_preset(curtain, "Silk")
        physics.set_kind(ctx, blob, 'SOFT')
        physics.set_cloth_pressure(blob, 2.0)
        tail = arm.pose.bones["Tail"]
        physics.set_spring(arm, tail, True)
        check(physics.kind(box) == 'RIGID' and physics.kind(curtain) == 'CLOTH' and physics.kind(blob) == 'SOFT' and
              physics.has_spring(tail), "a body, a curtain, a blob and a spring bone set up")

        def add(into, x, hover=None):
            widgets, height = menus.layout(ctx)
            m = vr_ui.panel_matrix(Vector((x, 0.0, -0.12)), Vector((x, -0.6, 0.0)), 1.0)
            into.append((m, widgets, height, hover))
            log(f"panel at {x}: {len(widgets)} widgets, height {height:.3f}")
            check(height < 0.34, f"panel at {x} fits")

        menus.reset()
        primitives.make_active(ctx, box)
        add(panels, -0.235, "kind_rigid")
        menus.set_more('PHYSICS', True)
        add(panels, 0.0, "rb_shape_box")
        menus.set_more('PHYSICS', False)
        primitives.make_active(ctx, curtain)
        add(panels, 0.235, "cloth_pin")
        primitives.make_active(ctx, blob)
        add(later, -0.235, "soft_pressure")
        primitives.make_active(ctx, arm)
        arm.data.bones.active = arm.data.bones["Tail"]
        menus.set_more('PHYSICS', True)
        add(later, 0.0, "spring_on")
        menus.set_more('PHYSICS', False)
        s.physics_hands = True
        menus.open_sheet('SIMULATION')
        add(later, 0.235, "physics_keep")
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


def shoot_panels(win, area, region):
    from vr_sculpt import operators, physics
    try:
        screenshot(win, area, region, "physics_panels.png")
        panels[:] = later
        screenshot(win, area, region, "physics_panels_more.png")
        panels.clear()
        ctx = bpy.context
        s = ctx.scene.vr_sculpt
        s.show_panel = False
        s.physics_hands, s.physics_tool = True, 'FORCE'
        running = operators.session_running
        operators.session_running = lambda context: True
        try:
            physics.sync_hands(ctx, fake)
            check(all(bpy.data.objects.get(n) is not None for n in physics.HAND_NAMES), "hand colliders at the tips")
            pull = physics.HandForce(ctx, 1, 1.0)  # Held: the pull ring shows at the right hand.
            physics.sync_hands(ctx, fake)
            state["pull"] = pull
            state["overlay"] = True
            rv3d = area.spaces.active.region_3d
            rv3d.view_location = (0.0, 0.0, 0.55)
            rv3d.view_rotation = Quaternion((1, 0, 0), 1.35)
            rv3d.view_distance = 2.2
            bpy.app.timers.register(lambda: (shoot_scene(win, area, region, running), None)[1], first_interval=0.4)
        except Exception:
            operators.session_running = running
            raise
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def shoot_scene(win, area, region, running):
    from vr_sculpt import operators, physics
    try:
        operators.session_running = lambda context: True
        screenshot(win, area, region, "physics_scene.png")
        state["pull"].finish(bpy.context)
        operators.session_running = running
        physics.remove_hands()
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
