"""Windowed check of the radial UI: pies at each level, scrubbing, the tool status and the hints,
flat-on. Saves tests/radial_preview_<sheet>.png (and docs/ui_radial_<sheet>.png with --docs).

blender --factory-startup test_scene.blend --python tests/radial_preview.py
blender --factory-startup test_scene.blend --python tests/radial_preview.py -- --docs
"""

import math
import os
import shutil
import sys
import time
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "radial_preview_result.txt")
DOCS = "--docs" in sys.argv
lines = []
drawn = []  # callables drawing the current sheet
HANDLES = []
CELL_X, CELL_Z = 0.21, 0.2
COLS = 3
VIEWER_Y = -0.6


def quit_cleanly():
    """Unregister first (see panel_preview.py): Blender redraws once more while quitting."""
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
    print(s)
    lines.append(s)


def draw():
    try:
        for fn in drawn:
            fn()
    except Exception:
        log(traceback.format_exc())


def cell(i):
    """World position of grid cell i (3 per row, top row first)."""
    col, row = i % COLS, i // COLS
    return Vector(((col - (COLS - 1) / 2) * CELL_X, 0.0, (0.5 - row) * CELL_Z))


def card(anchor):
    from vr_sculpt import hud
    return hud.billboard(anchor, Vector((anchor.x, VIEWER_Y, anchor.z)), 1.0)


def caption(i, text):
    from vr_sculpt import hud
    m = card(cell(i) + Vector((0.0, 0.0, -CELL_Z / 2 + 0.006)))
    w = hud.text_width(text, 0.0065)
    drawn.append(lambda: hud.draw_text(m, -w / 2, 0.0, text, 0.0065, (1.0, 0.85, 0.4, 1.0)))


def twist(degrees):
    return Quaternion((0.0, 0.0, 1.0), -math.radians(degrees))


def open_pie(i, kind, hand=1, at=None):
    """A pie of `kind` opened at cell i (or at), already faded in."""
    from vr_sculpt import menus, radial, radial_draw
    center = at if at is not None else cell(i) + Vector((0.0, 0.0, 0.008))
    pie = radial.Pie(kind, hand, menus.root_level(bpy.context, kind), card(center), Quaternion())
    pie.t0 -= 10.0
    drawn.append(lambda p=pie: radial_draw.draw_pie(p))
    return pie


def point(pie, id=None, distance=0.04, stick=None, rot=Quaternion(), dwell=False):  # noqa: B008
    """Move the pie's hand toward the slice with this id (in the current level)."""
    from vr_sculpt import radial
    level = pie.level
    now = time.monotonic()
    if id is None:
        pos = pie.matrix @ Vector((pie.center.x, pie.center.y, 0.0))
    else:
        i = [it.id for it in level.items].index(id)
        a = radial.slice_angle(i, len(level.items))
        local = pie.center + Vector((math.cos(a), math.sin(a))) * distance
        pos = pie.matrix @ Vector((local.x, local.y, 0.0))
    pie.update(pos, rot, stick or (0.0, 0.0), now=now)
    if dwell:
        pie.update(pos, rot, stick or (0.0, 0.0), now=now + 1.0)
    return pie


def recenter(pie, i):
    """Slide the whole pie so its current level sits in cell i (where its levels opened doesn't matter)."""
    c = pie.matrix @ Vector((pie.center.x, pie.center.y, 0.0))
    target = cell(i) + Vector((0.0, 0.0, 0.0))
    pie.matrix = Matrix.Translation(target - c) @ pie.matrix
    pie.inv = pie.matrix.inverted_safe()
    return pie


def descend(pie, id):
    """Push through the slice with this id: its pie opens at the hand."""
    return point(pie, id, distance=0.08)


def sheet_levels():
    """Sheet 1: the four pies at their first level."""
    from vr_sculpt import menus, primitives
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    menus.set_mode(ctx, 'SCULPT')
    point(open_pie(0, 'MODE', 0), "mode_draw")
    caption(0, "Y  Modes: hand moves toward Draw")
    point(open_pie(1, 'TOOL'), "brush_SMOOTH")
    caption(1, "B  Tool (Sculpt): Smooth hovered")
    pie = open_pie(2, 'TOOL')
    point(pie, None, rot=twist(2))
    caption(2, "B  Tool: hand in the middle = Radius dial")
    s.mode = 'MODEL'
    point(open_pie(3, 'OBJECT'), "page_finish")
    caption(3, "A  Object (a cube): Finish hovered")
    point(open_pie(4, 'TIME', 0), "key_insert")
    caption(4, "X  Time: Key hovered")
    rig = bpy.data.objects["Rig"]
    primitives.make_active(ctx, rig)
    menus.set_mode(ctx, 'POSE')
    point(open_pie(5, 'TOOL'), stick=(-0.7, 0.7))
    menus.set_mode(ctx, 'SCULPT')
    primitives.make_active(ctx, bpy.data.objects["Cube"])
    caption(5, "B  Tool (Pose): stick flicked up-left")
    s.mode = 'SCULPT'


def sheet_nested():
    """Sheet 2: deeper levels, with the breadcrumb back to where each started."""
    from vr_sculpt import menus
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    pie = open_pie(0, 'MODE', 0, at=cell(0) + Vector((-0.035, 0.0, -0.03)))
    descend(pie, "mode_draw")
    point(pie, "draw_color")
    recenter(pie, 0)
    caption(0, "Y > Draw (mode > tools)")
    pie = open_pie(1, 'MODE', 0, at=cell(1) + Vector((-0.04, 0.0, -0.045)))
    descend(pie, "mode_draw")
    descend(pie, "draw_color")
    point(pie, "color_BLUE")
    recenter(pie, 1)
    caption(1, "Y > Draw > Color (3 levels)")
    s.mode = 'MODEL'
    pie = open_pie(2, 'TOOL', at=cell(2) + Vector((-0.03, 0.0, -0.035)))
    descend(pie, "add")
    point(pie, pie.level.items[1].id)
    recenter(pie, 2)
    caption(2, "B > Add (Model): primitives")
    pie = open_pie(3, 'OBJECT', at=cell(3) + Vector((0.035, 0.0, 0.03)))
    descend(pie, "page_finish")
    point(pie, "bevel_width")
    recenter(pie, 3)
    caption(3, "A > Finish: shading, Mark, Bevel")
    pie = open_pie(4, 'OBJECT', at=cell(4) + Vector((0.0, 0.0, -0.04)))
    descend(pie, "page_array")
    point(pie, "array_count")
    recenter(pie, 4)
    caption(4, "A > Array (Count dial, axes)")
    s.mode = 'TOOLS'
    pie = open_pie(5, 'MODE', 0, at=cell(5) + Vector((0.04, 0.0, 0.0)))
    descend(pie, "view")
    point(pie, "passthrough")
    recenter(pie, 5)
    caption(5, "Y > View: desktop, passthrough, walk / fly")
    s.mode = 'SCULPT'
    menus.set_mode(ctx, 'SCULPT')


def sheet_scrub():
    """Sheet 3: values being turned (pie dials and stick-click knobs)."""
    from vr_sculpt import menus, paint, radial, radial_draw
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    s.mode, s.strength = 'SCULPT', 0.5
    pie = open_pie(0, 'TOOL')
    point(pie, "strength")
    point(pie, "strength", rot=twist(40))
    caption(0, "B > Strength: twisting (others fade)")
    s.radius = 0.04
    pie = open_pie(1, 'TOOL')
    point(pie, None, rot=twist(0))
    point(pie, None, rot=twist(60))
    caption(1, "B, twist in the middle: Radius")
    ctx.scene.frame_current = 34
    pie = open_pie(2, 'TIME', 0)
    point(pie, None, rot=twist(0))
    point(pie, None, rot=twist(20))
    caption(2, "X, twist: scrub frames (keys ticked)")
    # Stick-click knobs: the readout by the hand.
    for i, (side, deg) in enumerate((('R', 45), ('L', -30))):
        knob = radial.Knob(1 if side == 'R' else 0, menus.knob_dial(ctx, side), Quaternion())
        knob.update(twist(deg))
        m = card(cell(3 + i))
        drawn.append(lambda k=knob, m=m: radial_draw.draw_knob(k, m, menus.MODE_COLORS['SCULPT']))
    caption(3, "R stick click + twist: Radius")
    caption(4, "L stick click + twist: Strength")
    menus.set_mode(ctx, 'PAINT')
    cube = bpy.data.objects["Cube"]
    if not paint.ready(cube):
        paint.setup(ctx, cube, 1024)
    pie = open_pie(5, 'TOOL', at=cell(5) + Vector((-0.03, 0.0, -0.035)))
    if pie.level.items and pie.level.items[0].id != "paint_size_1024":
        descend(pie, "paint_color")
        point(pie, None, rot=twist(0))
        point(pie, None, rot=twist(30))
        recenter(pie, 5)
        caption(5, "B > Color: twist turns the hue")
    else:
        caption(5, "Paint: Set up first")
    menus.set_mode(ctx, 'SCULPT')


def sheet_status():
    """Sheet 4: the status badge in each mode (with the tool's name, a toast, recording) and the hints."""
    from vr_sculpt import draw as vr_draw, hud, radial_draw
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    states = [
        ("SCULPT", dict(engine='PYTHON', brush='INFLATE'), None, "Sculpt: Inflate (name after a change)"),
        ("MODEL", dict(model_add_as='DIFFERENCE'), None, "Model: Cut"),
        ("DRAW", dict(draw_tool='DRAW', draw_style='RIBBON', draw_color='BLUE'), ("Undo stroke", (0.78, 0.92, 0.35)),
         "Draw: Ribbon, a toast"),
        ("POSE", dict(pose_tool='IK'), None, "Pose: IK"),
        ("TOOLS", dict(tool='CAMERA'), None, "Tools: Camera"),
    ]
    for i, (mode, props, toast, text) in enumerate(states):
        s.mode = mode
        for k, v in props.items():
            setattr(s, k, v)
        icon, color, ring, title, rec = hud.status(ctx, s, None, vr_draw.COLORS)
        m = card(cell(i) + Vector((-0.03, 0.0, 0.02)))
        t = (toast[0], toast[1], 1.0) if toast else None
        drawn.append(lambda m=m, a=(icon, color, ring, title, rec, 1.0 if not toast else 0.0, t):
                     radial_draw.draw_status(m, a[0], a[1], a[2], a[3], a[4], a[5], a[6]))
        caption(i, text)
    s.mode = 'SCULPT'
    m = card(cell(5) + Vector((0.0, 0.0, 0.035)))
    drawn.append(lambda: radial_draw.draw_hints(m @ Matrix.Translation((-0.003, 0.0, 0.0)),
                                                radial_draw.LEFT_HINTS, 1.0, right_aligned=True))
    m2 = card(cell(5) + Vector((0.0, 0.0, -0.035)))
    drawn.append(lambda: radial_draw.draw_hints(m2 @ Matrix.Translation((-0.05, 0.0, 0.0)),
                                                radial_draw.RIGHT_HINTS, 1.0))
    caption(5, "Hints (L stick click; at start)")


SHEETS = (("levels", sheet_levels), ("nested", sheet_nested), ("scrub", sheet_scrub), ("status", sheet_status))


def setup():
    import vr_sculpt
    from vr_sculpt import native, primitives
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.shading.background_type = 'VIEWPORT'
        space.shading.background_color = (0.2, 0.21, 0.23)
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        # Something to work on: a cube (Model mode's object pie) with keys (the time pie's ticks).
        cube = primitives.create(ctx, 'CUBE', (0.0, 3.0, 0.0))
        cube.scale = (0.001, 0.001, 0.001)  # Out of the picture, but selected (for Model's object pie).
        space.show_region_toolbar = False
        space.show_region_ui = False
        space.show_gizmo = False
        space.overlay.show_text = False
        space.overlay.show_cursor = False
        bpy.ops.object.armature_add(location=(0.0, 3.0, 0.0))
        rig = ctx.active_object
        rig.name = "Rig"
        rig.scale = (0.001, 0.001, 0.001)
        primitives.make_active(ctx, cube)
        from vr_sculpt import hardsurface
        hardsurface.set_bevel(cube, True)
        hardsurface.set_array_count(ctx, cube, 3)
        cube.keyframe_insert("location", frame=10)
        cube.keyframe_insert("location", frame=60)
        primitives.make_active(ctx, cube)
        rv3d = space.region_3d
        rv3d.view_perspective = 'ORTHO'
        rv3d.view_location = (0.0, 0.0, 0.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
        rv3d.view_distance = 0.5
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (next_sheet(win, area, region, 0), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        done()


def next_sheet(win, area, region, i):
    if i >= len(SHEETS):
        done()
        return
    name, build = SHEETS[i]
    drawn.clear()
    try:
        build()
    except Exception:
        log(traceback.format_exc())
    bpy.app.timers.register(lambda: (shoot(win, area, region, i), None)[1], first_interval=0.4)


def shoot(win, area, region, i):
    name = SHEETS[i][0]
    png = os.path.join(ROOT, "tests", f"radial_preview_{name}.png")
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=png)
        log("saved", png)
        if DOCS:
            shutil.copyfile(png, os.path.join(ROOT, "docs", f"ui_radial_{name}.png"))
    except Exception:
        log(traceback.format_exc())
    next_sheet(win, area, region, i + 1)


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
