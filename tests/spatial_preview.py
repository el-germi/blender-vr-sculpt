"""Windowed preview of the spatial UI from a headset-like view: the tool dock with a tray, the tool's
tag, gizmos on a selected primitive, an array being dragged out, a radial ring, a brush ring pull,
the drawer with the timeline strip, and the Draw palette.

blender --factory-startup test_scene.blend --python tests/spatial_preview.py
(any .blend will do; loading one keeps the splash screen away)
Saves tests/spatial_<name>.png and tests/spatial_preview_result.txt.
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "spatial_preview_result.txt")
lines = []
HANDLES = []  # this script's own draw handlers
errors = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def quit_cleanly():
    """Unregister by hand (the add-on was registered by this script) after removing this script's draw
    handlers, then quit."""
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    try:
        import vr_sculpt
        if hasattr(bpy.types.Scene, "vr_sculpt"):
            vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def look(pitch_deg, yaw_deg=0.0):
    """A headset rotation: looking along +Y, turned by yaw, tipped down by pitch."""
    return Quaternion((0, 0, 1), math.radians(yaw_deg)) @ Quaternion((1, 0, 0), math.radians(90 - pitch_deg))


class FakeSessionState:
    viewer_scale = 1.0
    navigation_scale = 1.0

    def __init__(self):
        self.viewer_pose_location = Vector((0.0, -0.9, 1.5))
        self.viewer_pose_rotation = look(20)
        self.navigation_location = Vector((0.0, 0.0, 0.0))
        self.navigation_rotation = Quaternion()
        self.rest()

    def rest(self):
        down = Quaternion((1, 0, 0), 0.9)
        self.aim_loc = [Vector((-0.22, -0.6, 1.22)), Vector((0.22, -0.6, 1.22))]
        self.aim_rot = [down, down]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + self.aim_rot[i] @ Vector((0.0, 0.0, 0.03))


fake = FakeSessionState()


def tip_at(i, point):
    """Put controller i's tip at point (keeping its rotation)."""
    offset = bpy.context.scene.vr_sculpt.tip_offset
    fake.aim_loc[i] = Vector(point) - fake.aim_rot[i] @ Vector((0.0, 0.0, -offset))


def draw():
    from vr_sculpt import draw as vr_draw
    try:
        vr_draw.draw_overlay(bpy.context, fake, True)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def view(area):
    """The viewport sees what the headset would (a wide lens, from the viewer's eye)."""
    space = area.spaces.active
    space.lens = 36.0  # The viewport lens is on a 72 mm sensor: about 90 degrees across, like a headset.
    rv3d = space.region_3d
    rv3d.view_perspective = 'PERSP'
    rv3d.view_rotation = fake.viewer_pose_rotation
    forward = fake.viewer_pose_rotation @ Vector((0.0, 0.0, -1.0))
    rv3d.view_distance = 0.3
    rv3d.view_location = fake.viewer_pose_location + forward * 0.3
    rv3d.update()
    log("eye", tuple(round(c, 3) for c in rv3d.view_matrix.inverted().translation), "lens", space.lens,
        "region", area.width, area.height)


def reset_scene(ctx, keep_balls=False):
    from vr_sculpt import menus, spatial
    for o in list(bpy.data.objects):
        if o.name in {"SculptBall", "HeavyBall_164k"}:
            o.hide_set(not keep_balls)
        elif o.type == 'CAMERA':
            o.hide_set(True)  # The VR start camera would sit right in front of the eye.
        elif o.type in {'MESH', 'EMPTY'}:
            bpy.data.objects.remove(o)
    s = ctx.scene.vr_sculpt
    s.show_timeline = False
    s.show_brush_card = True
    menus.close_picker()
    fake.rest()
    fake.viewer_pose_location = Vector((0.0, -0.9, 1.5))
    fake.viewer_pose_rotation = look(20)
    spatial.reset()


def slot(id):
    from vr_sculpt import dock
    return dict((t.id, p) for t, p in dock.slots(fake))[id]


def scenario_dock(ctx):
    """Looking down at the belt, the right tip in the Sculpt token: its brushes fan out above it."""
    from vr_sculpt import menus
    reset_scene(ctx)
    menus.set_mode(ctx, 'DRAW')
    s = ctx.scene.vr_sculpt
    s.draw_color = 'ORANGE'
    fake.viewer_pose_rotation = look(48)
    tip_at(1, slot("sculpt") + Vector((0.0, -0.005, 0.004)))
    tip_at(0, Vector((-0.32, -0.55, 1.12)))


def scenario_tag(ctx):
    """Sculpting: the left hand reaches for the right hand's tag; its chips come up."""
    from vr_sculpt import menus, spatial
    reset_scene(ctx, keep_balls=True)
    menus.set_mode(ctx, 'SCULPT')
    s = ctx.scene.vr_sculpt
    s.engine, s.brush, s.strength = 'PYTHON', 'INFLATE', 0.65
    s.symmetry_x = True
    fake.viewer_pose_rotation = look(14, 8)
    fake.aim_loc[1] = Vector((0.12, -0.55, 1.28))
    fake.aim_rot[1] = Quaternion((1, 0, 0), 1.1)
    anchor = spatial.tag_anchor(ctx, fake)
    tip_at(0, anchor + Vector((-0.02, -0.03, 0.09)))


def _cube(ctx, loc, size):
    from vr_sculpt import primitives
    cube = primitives.create(ctx, 'CUBE', Vector(loc))
    cube.vr_prim.size = size
    ctx.view_layer.update()
    return cube


def scenario_gizmos(ctx):
    """A selected box: face handles, the bevel bead, array arrows, and its chips beside it."""
    from vr_sculpt import hardsurface, menus, shading
    reset_scene(ctx)
    menus.set_mode(ctx, 'MODEL')
    cube = _cube(ctx, (0.06, -0.45, 1.25), (0.14, 0.1, 0.09))
    hardsurface.set_bevel(cube, True)
    hardsurface.set_bevel_width(cube, 0.008)
    shading.set_mode(cube, 'AUTO')
    fake.viewer_pose_rotation = look(30)
    tip_at(1, Vector((0.13, -0.45, 1.25)) + Vector((0.006, -0.004, 0.0)))  # on the +X face handle
    tip_at(0, Vector((-0.25, -0.55, 1.1)))


def scenario_array(ctx):
    """Dragging the X arrow out: the copies follow it."""
    from vr_sculpt import gizmos, menus, spatial
    reset_scene(ctx)
    menus.set_mode(ctx, 'MODEL')
    cube = _cube(ctx, (-0.05, -0.5, 1.25), (0.06, 0.06, 0.06))
    fake.viewer_pose_rotation = look(30)
    h = next(h for h in gizmos.handles(ctx, cube, fake.viewer_pose_location, 1.0) if h.id == "array_X")
    d = gizmos.ArrayDrag(ctx, cube, h, h.position, 1.0)
    end = h.position + Vector((0.2, 0.0, 0.0))
    from mathutils import Matrix
    for i in range(1, 11):
        p = h.position.lerp(end, i / 10)
        d.update(ctx, Matrix.Translation(p), Matrix.Translation(p), 1.0)
    spatial._drag["handler"] = d
    tip_at(1, end)
    tip_at(0, Vector((-0.3, -0.6, 1.08)))


def scenario_radial(ctx):
    """A radial array: the ring, a bead per copy, the count bead and the center cross."""
    from vr_sculpt import hardsurface, menus, spatial
    reset_scene(ctx)
    menus.set_mode(ctx, 'MODEL')
    bolt = _cube(ctx, (0.06, -0.48, 1.22), (0.03, 0.03, 0.05))
    hardsurface.set_array_count(ctx, bolt, 8)
    hardsurface.set_array_axis(ctx, bolt, 2)
    hardsurface.set_array_mode(ctx, bolt, 'RADIAL')
    hardsurface.set_array_center(ctx, bolt, Vector((0.06, -0.38, 1.22)))
    ctx.view_layer.update()
    fake.viewer_pose_rotation = look(40)
    spatial.reset()
    tip_at(1, Vector((0.12, -0.44, 1.27)))
    tip_at(0, Vector((-0.3, -0.6, 1.08)))


def scenario_ring(ctx):
    """Spreading the hands to grow the brush: the left tip pulls the right ring."""
    from vr_sculpt import menus, spatial
    reset_scene(ctx, keep_balls=True)
    menus.set_mode(ctx, 'SCULPT')
    s = ctx.scene.vr_sculpt
    s.engine, s.brush = 'PYTHON', 'DRAW'
    fake.viewer_pose_rotation = look(12)
    tip_at(1, Vector((0.08, -0.32, 1.32)))
    s.radius = 0.06
    tip_at(0, Vector((0.02, -0.32, 1.32)))
    pull = spatial.RingPull(ctx, fake, "/user/hand/left", 1)
    pull.label = "radius 6.0 cm"
    from vr_sculpt import operators
    operators.active_hands.add(0)


def scenario_drawer(ctx):
    """The drawer open over the last slot, and the timeline strip above the dock."""
    from vr_sculpt import menus, operators, spatial
    operators.active_hands.discard(0)
    spatial._pull["handler"] = None
    reset_scene(ctx)
    menus.set_mode(ctx, 'POSE')
    s = ctx.scene.vr_sculpt
    s.show_timeline = True
    spatial.set_drawer(True)
    fake.viewer_pose_rotation = look(42, -14)
    tip_at(1, spatial._timeline_anchor(fake) + Vector((0.03, -0.01, 0.03)))
    tip_at(0, Vector((-0.34, -0.62, 1.05)))


def scenario_palette(ctx):
    """Drawing: the colors ride on the left hand; the right tip dips into one."""
    from vr_sculpt import menus, spatial
    reset_scene(ctx)
    menus.set_mode(ctx, 'DRAW')
    s = ctx.scene.vr_sculpt
    s.draw_color = 'BLUE'
    fake.viewer_pose_rotation = look(16, -6)
    fake.aim_loc[0] = Vector((-0.1, -0.55, 1.25))
    fake.aim_rot[0] = Quaternion((1, 0, 0), 1.2)
    card = next(c for c in spatial.cards(ctx, fake) if c.id == "palette")
    w = card.find("color_GREEN")
    x0, y0, x1, y1 = w.rect
    tip_at(1, card.matrix @ Vector(((x0 + x1) / 2, (y0 + y1) / 2, 0.012)))


SCENARIOS = [("dock", scenario_dock), ("tag", scenario_tag), ("gizmos", scenario_gizmos), ("array", scenario_array),
             ("radial", scenario_radial), ("ring", scenario_ring), ("drawer", scenario_drawer),
             ("palette", scenario_palette)]


def setup():
    import vr_sculpt
    from vr_sculpt import native
    try:
        vr_sculpt.register()
        win = bpy.context.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = True
        space.shading.type = 'SOLID'
        s = bpy.context.scene.vr_sculpt
        s.tip_offset = 0.04
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        only = os.environ.get("VRS_PREVIEW")
        step(win, area, region, 0, only)
    except Exception:
        log(traceback.format_exc())
        done()


def step(win, area, region, i, only):
    while i < len(SCENARIOS) and only and SCENARIOS[i][0] not in only.split(","):
        i += 1
    if i >= len(SCENARIOS):
        done()
        return
    name, fn = SCENARIOS[i]
    try:
        with bpy.context.temp_override(window=win, area=area, region=region):
            fn(bpy.context)
        view(area)
    except Exception:
        log(f"{name} setup failed:\n" + traceback.format_exc())
    bpy.app.timers.register(lambda: (shoot(win, area, region, i, only), None)[1], first_interval=0.6)


def shoot(win, area, region, i, only):
    name = SCENARIOS[i][0]
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        path = os.path.join(ROOT, "tests", f"spatial_{name}.png")
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=path)
        log("saved", path, "draw errors so far:", len(errors))
    except Exception:
        log(traceback.format_exc())
    step(win, area, region, i + 1, only)


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
