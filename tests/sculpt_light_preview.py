"""Windowed check of the hand light on a dyntopo sculpt, through the code a session runs. Saves
tests/sculpt_light_scene.png (the mesh, flat with VR Shading Smooth, after real strokes, lit by the hand light with the
floor left out) and tests/sculpt_light_panels.png (the Sculpt page: Solid, and with the light on and More open), and
writes tests/sculpt_light_preview_result.txt.

blender --factory-startup test_scene.blend --python tests/sculpt_light_preview.py
"""

import math
import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SCENE_PNG = os.path.join(ROOT, "tests", "sculpt_light_scene.png")
PANELS_PNG = os.path.join(ROOT, "tests", "sculpt_light_panels.png")
LOG = os.path.join(ROOT, "tests", "sculpt_light_preview_result.txt")
lines = []
failures = []
panels = []
HANDLES = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def check(cond, msg):
    log(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def quit_cleanly():
    """See tests/panel_preview.py: our draw handlers go, then the add-on, then Blender."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


class Session:
    viewer_scale = 1.0

    def __init__(self, left):
        self.left = left

    def controller_aim_location_get(self, context, index):
        return self.left.copy() if index == 0 else Vector((3.0, 0.0, 0.0))

    def controller_aim_rotation_get(self, context, index):
        return Quaternion()


def draw_panels():
    from vr_sculpt import vr_ui
    try:
        for m, widgets, height in panels:
            vr_ui.draw_panel(m, widgets, height, None)
    except Exception:
        log(traceback.format_exc())


def positions(obj):
    a = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get("co", a)
    return a.reshape(-1, 3)


def shoot(win, area, region, path):
    area.tag_redraw()
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot_area(filepath=path)
    log("saved", path)


def run():
    import vr_sculpt
    from vr_sculpt import lookdev, menus, native, shading, vr_ui
    vr_sculpt.register()
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    win = ctx.window_manager.windows[0]
    area, region = native.largest_view3d(win)
    space = area.spaces.active
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_plane_add(size=9.0, location=(0.0, 0.0, -1.05))
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1.0, calc_uvs=False)
    ball = ctx.active_object
    mat = bpy.data.materials.new("Clay")
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    bsdf.inputs["Base Color"].default_value = (0.8, 0.42, 0.28, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.3
    ball.data.materials.append(mat)
    shading.set_mode(ball, 'SMOOTH')

    s.engine = 'NATIVE'
    menus.reset()
    menus.set_mode(ctx, 'SCULPT')
    native.activate_brush(ctx, "Draw")
    menus.set_more('SCULPT', True)

    def add_panel(x):
        widgets, height = menus.layout(ctx)
        panels.append((vr_ui.panel_matrix(Vector((x, 0.0, -0.15)), Vector((x, -0.6, 0.0)), 1.0), widgets, height))
        return height

    add_panel(-0.14)  # Solid, light off, dyntopo off.
    native.toggle_dyntopo(ctx)
    sculpt = ctx.tool_settings.sculpt
    sculpt.detail_type_method = 'CONSTANT'
    sculpt.constant_detail_resolution = 30.0
    check(ball.use_dynamic_topology_sculpting, "dyntopo on from the panel's code path")

    # The toggle the panel presses, then the timer's job at the off hand.
    vr_ui.find(menus.layout(ctx)[0], "hand_light").on_press()
    sculpt.detail_type_method = 'BRUSH'
    height = add_panel(0.14)
    log(f"panel height with every row: {height:.3f} m")
    check(height < 0.335, "the panel still fits with the light's slider and all dyntopo rows")
    vr_ui.find(menus.layout(ctx)[0], "hand_lines").on_press()
    height = add_panel(0.42)
    log(f"panel height with the lines' sliders: {height:.3f} m")
    check(height < 0.335, "and with the lines' sliders")
    vr_ui.find(menus.layout(ctx)[0], "hand_light").on_press()
    sculpt.detail_type_method = 'CONSTANT'
    ss = Session(Vector((-1.1, -1.4, 1.4)))
    check(lookdev.sync_hand_light(ctx, ss), "the hand light exists")
    light = bpy.data.objects[lookdev.HAND_LIGHT]
    light.data.energy = 60.0  # It's meant for a hand next to the mesh; here the mesh is 2 m across.
    check(ball.use_dynamic_topology_sculpting and ball.mode == 'SCULPT', "linking it left the session alone")

    # Real strokes with the light moving, as in a session.
    sh = space.shading
    sh.type = 'MATERIAL'
    sh.use_scene_lights = True
    sh.use_scene_world = False
    sh.studiolight_intensity = 0.25
    space.overlay.show_overlays = False
    rv3d = space.region_3d
    rv3d.view_perspective = 'PERSP'
    rv3d.view_location = (0.0, 0.0, 0.0)
    rv3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
    rv3d.view_distance = 3.6
    keep_power = s.hand_light_power
    stroke = native.NativeStroke(ctx, ball, 0.25)
    for i in range(20):
        a = (i - 10) * 0.05
        ss.left = Vector((-1.1 + 0.01 * i, -1.4, 1.4))
        lookdev.sync_hand_light(ctx, ss)
        stroke.update(Vector((math.sin(a), -math.cos(a), -0.15)).normalized() * 1.01, None, 1.0)
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    stroke.finish()
    light.data.energy = 60.0
    s.hand_light_power = keep_power
    check(abs(light.matrix_world.translation.x - (-1.1 + 0.19)) < 0.06, "the light followed the hand during the stroke")
    for _i in range(4):
        area.tag_redraw()
        yield 0.7
    shoot(win, area, region, SCENE_PNG)

    panels_only(space, rv3d, region, win, area)
    yield 0.8
    shoot(win, area, region, PANELS_PNG)
    panels.clear()

    menus.set_object_mode(ctx, ball, 'OBJECT')
    check(len(ball.data.polygons) > 320, f"the stroke made new faces ({len(ball.data.polygons)})")
    check(sum(p.use_smooth for p in ball.data.polygons) == len(ball.data.polygons), "every face came out smooth")
    check(np.linalg.norm(positions(ball), axis=1).max() > 1.03, "the stroke moved the surface")
    check(not lookdev.sync_hand_light(ctx, None), "the session's end takes the light away")
    check(bpy.data.objects.get(lookdev.HAND_LIGHT) is None and len(ball.users_collection) == 1, "nothing left behind")


def panels_only(space, rv3d, region, win, area):
    space.shading.type = 'SOLID'
    rv3d.view_location = (0.42, 0.0, -0.1)  # The panel with the lines on: their sliders.
    rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
    rv3d.view_distance = 0.38
    bpy.context.view_layer.objects.active.hide_viewport = False
    for o in bpy.data.objects:
        if o.type == 'MESH' and o.mode == 'OBJECT':
            o.hide_viewport = True
    bpy.context.view_layer.objects.active.hide_set(False)
    space.overlay.show_overlays = True
    space.overlay.show_floor = False
    space.overlay.show_axis_x = space.overlay.show_axis_y = False
    HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw_panels, (), 'WINDOW', 'POST_VIEW'))


def start():
    try:
        gen = run()
    except Exception:
        log(traceback.format_exc())
        failures.append("start")
        quit_cleanly()
        return None

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            log(traceback.format_exc())
            failures.append("raised")
        quit_cleanly()
        return None

    bpy.app.timers.register(tick, first_interval=0.1)
    return None


bpy.app.timers.register(start, first_interval=1.5)
