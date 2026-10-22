"""Windowed renders of the hand panel, flat-on: every mode collapsed and expanded, the second levels,
the timeline, the tip status and the hidden panel's ghost. Saves tests/ui_<name>.png.

blender --factory-startup test_scene.blend --python tests/ui_preview.py
"""

import math
import os
import sys
import time
import traceback

import bpy
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "ui_preview_result.txt")
lines = []
HANDLES = []  # this script's own draw handlers
items = []  # what the draw handler draws: (kind, args)
errors = []
timings = []  # ms per draw_panel call (CPU side)
profiled = []


def quit_cleanly():
    """Unregister vr_sculpt (registered by hand) and this script's draw handlers before quitting, or
    the exit can crash on GPU resources freed after Blender's own teardown."""
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


def facing(x, z):
    from vr_sculpt import vr_ui
    return vr_ui.panel_matrix(Vector((x, 0.0, z)), Vector((x, -1.0, z)), 1.0)


def draw():
    from vr_sculpt import hud, vr_ui
    if os.environ.get("UI_PROFILE") and len(timings) > 40 and not profiled:
        import cProfile
        import io
        import pstats
        profiled.append(1)
        pr = cProfile.Profile()
        pr.enable()
        draw()
        pr.disable()
        out = io.StringIO()
        pstats.Stats(pr, stream=out).sort_stats("tottime").print_stats(18)
        log(out.getvalue())
        return
    try:
        for kind, a in items:
            if kind == 'panel':
                m, widgets, height, width, hover, pointer = a
                t0 = time.perf_counter()
                vr_ui.draw_panel(m, widgets, height, hover, None, width=width, pointer=pointer)
                timings.append((time.perf_counter() - t0) * 1000)
            elif kind == 'caption':
                m, text = a
                w = hud.text_width(text, 0.0075)
                hud.draw_text(m, -w / 2, -0.016, text, 0.0075, (0.85, 0.87, 0.9, 1.0))
            elif kind == 'status':
                anchor, content, size, desc = a
                hud.draw_status(anchor, anchor + Vector((0, -1, 0)), 1.0, content, size, with_description=desc)
            elif kind == 'ghost':
                m, icon, hot = a
                vr_ui.draw_ghost(m, icon, 1.0, hot)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def center_of(widgets, id):
    from vr_sculpt import vr_ui
    w = vr_ui.find(widgets, id)
    if w is None:
        log("  (no widget", id, ")")
        return None
    return ((w.rect[0] + w.rect[2]) / 2, (w.rect[1] + w.rect[3]) / 2)


def add_panel(x, z, caption, hover=None):
    from vr_sculpt import menus, vr_ui
    widgets, height = menus.layout(bpy.context)
    m = facing(x, z)
    items.append(('panel', (m, widgets, height, vr_ui.WIDTH, hover, center_of(widgets, hover) if hover else None)))
    items.append(('caption', (m, caption)))
    return height


def add_timeline(x, z, hover=None):
    from vr_sculpt import interactions, menus, vr_ui
    widgets, height = vr_ui.layout(menus.timeline_rows(bpy.context, bpy.context.scene.vr_sculpt),
                                   interactions.TIMELINE_WIDTH)
    m = facing(x, z)
    items.append(('panel', (m, widgets, height, interactions.TIMELINE_WIDTH, hover,
                            center_of(widgets, hover) if hover else None)))
    return height


def make_rig(ctx):
    arm = bpy.data.armatures.new("Arm")
    rig = bpy.data.objects.new("Rig", arm)
    ctx.scene.collection.objects.link(rig)
    rig.location = (0.0, 2.0, 1.0)
    ctx.view_layer.objects.active = rig
    from vr_sculpt import menus
    menus.set_object_mode(ctx, rig, 'OBJECT')
    window, view = menus._desktop_view(ctx)
    with ctx.temp_override(window=window, area=view[0], region=view[1], active_object=rig, object=rig):
        bpy.ops.object.mode_set(mode='EDIT')
        for name, head, tail, parent in (("Upper.L", (0, 0, 0), (0.3, 0, 0), None),
                                         ("Fore.L", (0.3, 0, 0), (0.6, 0, 0), "Upper.L"),
                                         ("Hand.L", (0.6, 0, 0), (0.7, 0, 0), "Fore.L")):
            b = arm.edit_bones.new(name)
            b.head, b.tail = head, tail
            if parent:
                b.parent = arm.edit_bones[parent]
                b.use_connect = True
        bpy.ops.object.mode_set(mode='OBJECT')
    return rig


def build_modes(ctx):
    """Row 1: each mode as it opens. Row 2: the same with More open."""
    from vr_sculpt import menus, paint, primitives
    menus.reset()
    ball = bpy.data.objects["SculptBall"]
    box = primitives.create(ctx, 'CUBE', (0.6, 2.0, 1.0))
    canvas = primitives.create(ctx, 'CUBE', (-0.6, 2.0, 1.0))
    canvas.name = "Canvas"
    rig = make_rig(ctx)
    step = 0.235
    x0 = -2.5 * step
    for row, expanded in ((1, False), (0, True)):
        z = 0.02 + row * 0.36
        for i, mode in enumerate(('SCULPT', 'MODEL', 'DRAW', 'PAINT', 'POSE', 'TOOLS')):
            menus.reset()
            if mode == 'MODEL':
                primitives.make_active(ctx, box)
            elif mode == 'PAINT':
                primitives.make_active(ctx, canvas)
            elif mode == 'POSE':
                for o in ctx.view_layer.objects:
                    o.select_set(False)
                rig.select_set(True)
                ctx.view_layer.objects.active = rig
            else:
                primitives.make_active(ctx, ball)
            menus.set_mode(ctx, mode)
            if mode == 'PAINT' and not paint.ready(canvas):
                paint.setup(ctx, canvas, 1024)
            if mode == 'POSE' and rig.mode == 'POSE':
                rig.data.bones.active = rig.data.bones["Fore.L"]
            if expanded:
                menus.set_more(mode, True)
            hover = {('SCULPT', False): "radius", ('MODEL', False): "mode_paint", ('DRAW', True): "more",
                     ('TOOLS', False): "tool_camera", ('PAINT', True): "paint_pick", ('POSE', False): "pose_ik",
                     }.get((mode, expanded))
            name = mode.title() + (" + More" if expanded else "")
            h = add_panel(x0 + i * step, z, name, hover)
            log(f"{name}: height {h:.3f} m")
    menus.reset()
    primitives.make_active(ctx, ball)
    menus.set_mode(ctx, 'SCULPT')


def build_sheets(ctx):
    """Second levels: the object's four pages, Settings, a picker; and the native brushes."""
    from vr_sculpt import catalog, hardsurface, menus, native, primitives, shading
    s = ctx.scene.vr_sculpt
    menus.reset()
    menus.set_mode(ctx, 'MODEL')
    plate = primitives.create(ctx, 'CUBE', (0.0, 3.0, 1.0))
    plate.name = "Plate"
    plate.vr_prim.size = (0.3, 0.2, 0.06)
    hole = primitives.create(ctx, 'CYLINDER', (0.05, 3.0, 1.03))
    hole.vr_prim.radius = 0.03
    hardsurface.make_cutter(ctx, hole, plate)
    shading.set_mode(plate, 'AUTO')
    hardsurface.set_bevel(plate, True)
    hardsurface.set_array_count(ctx, plate, 3)
    primitives.make_active(ctx, plate)
    step = 0.235
    x0 = -2.5 * step
    z_top, z_bottom = 0.4, 0.02
    menus.open_sheet('OBJECT')
    for i, page in enumerate(('SHAPE', 'BOOLEAN', 'FINISH', 'ARRAY')):
        s.model_page = page
        menus.set_more('OBJECT_' + page, page in {'SHAPE', 'FINISH'})
        hover = {'SHAPE': "subdivisions", 'FINISH': "bevel_width"}.get(page)
        add_panel(x0 + i * step, z_top, "Object: " + page.title(), hover)
    primitives.make_active(ctx, hole)
    s.model_page = 'BOOLEAN'
    add_panel(x0 + 4 * step, z_top, "Cutter: Cuts", "bool_apply")
    menus.close_sheet(ctx)
    menus.open_sheet('SETTINGS')
    add_panel(x0 + 5 * step, z_top, "Settings", "reveal_wrist")
    menus.close_sheet(ctx)
    primitives.make_active(ctx, plate)
    menus.open_add_picker(ctx)
    add_panel(x0, z_bottom, "Add picker", "cat_Mesh")
    menus._set_category("Mesh")
    add_panel(x0 + step, z_bottom, "Add picker: Mesh", "opt_mesh.primitive_torus_add")
    menus.close_picker()
    torus_key = next(e.key for e in catalog.entries() if e.idname == "mesh.primitive_torus_add")
    torus = primitives.create_from_entry(ctx, torus_key, (0, 4, 1))
    s.model_page = 'SHAPE'
    menus.open_sheet('OBJECT')
    add_panel(x0 + 2 * step, z_bottom, "Operator settings", "param_mode")
    menus.reset()
    ball = bpy.data.objects["SculptBall"]
    primitives.make_active(ctx, ball)
    menus.set_mode(ctx, 'SCULPT')
    menus.set_engine(ctx, 'NATIVE')
    try:
        native.activate_brush(ctx, "Clay Strips")
    except Exception as ex:
        log("native brush:", ex)
    add_panel(x0 + 3 * step, z_bottom, "Sculpt: native", "brush_Smooth")
    menus.set_engine(ctx, 'PYTHON')
    bare = bpy.data.objects.new("Bare", bpy.data.meshes.new("Bare"))
    ctx.scene.collection.objects.link(bare)
    primitives.make_active(ctx, bare)
    menus.set_mode(ctx, 'PAINT')
    add_panel(x0 + 4 * step, z_bottom, "Paint: not set up", "paint_setup")
    menus.set_mode(ctx, 'MODEL')
    for o in ctx.view_layer.objects:
        o.select_set(False)
    add_panel(x0 + 5 * step, z_bottom, "Model: nothing selected", "add")
    torus.select_set(False)


def build_overlay(ctx):
    """In place: the panel with the timeline beside it (collapsed, expanded), the tip status for each
    mode, the ghost."""
    from vr_sculpt import anim, hud, interactions, menus, primitives, vr_ui
    s = ctx.scene.vr_sculpt
    menus.reset()
    ball = bpy.data.objects["SculptBall"]
    primitives.make_active(ctx, ball)
    ball.keyframe_insert("location", frame=1)
    ball.keyframe_insert("location", frame=40)
    ctx.scene.frame_current = 24
    menus.set_mode(ctx, 'SCULPT')
    side = vr_ui.WIDTH / 2 + interactions.TIMELINE_GAP + interactions.TIMELINE_WIDTH / 2
    for i, expanded in enumerate((False, True)):
        x = -0.55 + i * 0.5
        menus.set_more('TIMELINE', expanded)
        add_panel(x, 0.24, "Panel + timeline" + (" (More)" if expanded else ""), None)
        add_timeline(x + side, 0.24, "play" if not expanded else "onion")
    # Tip status for each mode, with the description under the first.
    contents = []
    for mode in ('SCULPT', 'MODEL', 'DRAW', 'PAINT', 'POSE', 'TOOLS'):
        s.mode = mode
        brush = None
        contents.append((hud.card_content(s, brush), hud.status_size(ctx, s), mode == 'SCULPT'))
    for i, (content, size, desc) in enumerate(contents):
        items.append(('status', (Vector((-0.58 + i * 0.2, 0.0, 0.1)), content, size, desc)))
    s.mode = 'SCULPT'
    items.append(('ghost', (facing(0.4, 0.3), "sculpt", False)))
    items.append(('caption', (facing(0.4, 0.3), "Hidden")))
    items.append(('ghost', (facing(0.54, 0.3), "model", True)))
    items.append(('caption', (facing(0.54, 0.3), "Pointed at")))
    anim.set_frame(ctx, 24)


def build_detail(ctx):
    """Two panels at about the headset's pixel density (a Quest 3 shows ~3.5 px per mm at 40 cm)."""
    from vr_sculpt import menus, primitives
    s = ctx.scene.vr_sculpt
    menus.reset()
    ball = bpy.data.objects["SculptBall"]
    primitives.make_active(ctx, ball)
    menus.set_mode(ctx, 'SCULPT')
    add_panel(-0.125, 0.0, "", "mode_model")
    box = primitives.create(ctx, 'CUBE', (0.6, 5.0, 1.0))
    menus.set_mode(ctx, 'MODEL')
    primitives.make_active(ctx, box)
    s.model_page = 'SHAPE'
    menus.open_sheet('OBJECT')
    menus.set_more('OBJECT_SHAPE', True)
    add_panel(0.125, 0.0, "", "subdivisions")
    menus.reset()
    menus.set_mode(ctx, 'SCULPT')


SHOTS = (("modes", build_modes, (0.0, 0.0, 0.34), 1.02), ("sheets", build_sheets, (0.0, 0.0, 0.34), 1.02),
         ("overlay", build_overlay, (-0.05, 0.0, 0.24), 0.95), ("detail", build_detail, (0.0, 0.0, 0.11), 0.37))


def setup():
    import vr_sculpt
    from vr_sculpt import native, vr_ui
    try:
        import addon_utils
        addon_utils.enable("bl_ext.blender_org.extra_mesh_objects", default_set=True)  # Picker categories.
        vr_sculpt.register()
        vr_ui.ANIMATE = False
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.overlay.show_cursor = False
        space.overlay.show_object_origins = False
        space.show_region_ui = False
        space.show_region_toolbar = False
        space.show_region_header = False
        space.shading.background_type = 'VIEWPORT'
        space.shading.background_color = (0.2, 0.21, 0.23)
        ctx.scene.vr_sculpt.show_timeline = True
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screen_full_area(use_hide_panels=True)
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))
        bpy.app.timers.register(lambda: (start_shots(win), None)[1], first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def start_shots(win):
    """The full-screen area is a new screen: find its 3D view again."""
    from vr_sculpt import native
    area, region = native.largest_view3d(win)
    space = area.spaces.active
    space.overlay.show_floor = False
    space.overlay.show_axis_x = space.overlay.show_axis_y = False
    space.overlay.show_cursor = False
    space.overlay.show_text = False
    space.show_gizmo = False
    space.shading.background_type = 'VIEWPORT'
    space.shading.background_color = (0.2, 0.21, 0.23)
    log("view", region.width, "x", region.height)
    next_shot(win, area, region, 0)


def next_shot(win, area, region, i):
    if i >= len(SHOTS):
        done()
        return
    name, build, center, distance = SHOTS[i]
    try:
        items.clear()
        build(bpy.context)
        rv3d = area.spaces.active.region_3d
        rv3d.view_perspective = 'ORTHO'
        rv3d.view_location = center
        rv3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
        rv3d.view_distance = distance
        # Hide the scene's own objects: only the UI is of interest here.
        for o in bpy.context.scene.objects:
            o.hide_viewport = True
    except Exception:
        log(traceback.format_exc())
    bpy.app.timers.register(lambda: (shoot(win, area, region, i), None)[1], first_interval=0.4)


def shoot(win, area, region, i):
    name = SHOTS[i][0]
    png = os.path.join(ROOT, "tests", f"ui_{name}.png")
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=png)
        log("saved", png)
    except Exception:
        log(traceback.format_exc())
    for o in bpy.context.scene.objects:
        o.hide_viewport = False
    bpy.app.timers.register(lambda: (next_shot(win, area, region, i + 1), None)[1], first_interval=0.1)


def done():
    log("draw errors:", len(errors))
    if timings:
        log(f"draw_panel: {sum(timings) / len(timings):.2f} ms average, {max(timings):.2f} ms max "
            f"({len(timings)} calls)")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
