"""Background tests for look dev: the Light tool (placing, grabbing, selecting, deleting lights, the panel and
gizmos), the VR view's look (shading, matcaps, HDRIs, previews, Use as world) and Model mode's Material page.

blender --background --factory-startup --python tests/test_lookdev.py
"""

import math
import os
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import vr_sculpt  # noqa: E402
from vr_sculpt import (actions, controllers, hud, interactions, lookdev, menus, operators, primitives,  # noqa: E402
                       vr_ui)

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


def widgets():
    return {w.id: w for w in menus.layout(bpy.context)[0] if w.id}


def press(wid):
    w = widgets().get(wid)
    if w is None:
        check(False, f"widget {wid} is on the panel")
        return None
    w.on_press()
    return w


def slide(wid, value):
    w = widgets().get(wid)
    if w is None:
        check(False, f"slider {wid} is on the panel")
        return None
    w.on_change(value)
    return w


def clear_scene():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    for coll in (bpy.data.lights, bpy.data.materials, bpy.data.meshes):
        for d in list(coll):
            if d.users == 0:
                coll.remove(d)


def aim_pose(loc, direction):
    """A controller pose at loc pointing along direction (aim poses point down -Z)."""
    rot = Vector(direction).to_track_quat('-Z', 'Z')
    return Matrix.LocRotScale(Vector(loc), rot, None)


def forward(m):
    return (m.to_3x3().normalized() @ Vector((0.0, 0.0, -1.0))).normalized()


def light_tool():
    ctx = bpy.context
    menus.reset()
    menus.set_mode(ctx, 'TOOLS')
    ctx.scene.vr_sculpt.tool = 'LIGHT'
    return ctx, ctx.scene.vr_sculpt


# --- the Light tool -------------------------------------------------------------------

def test_place():
    print("== placing lights")
    clear_scene()
    ctx, s = light_tool()
    xs = ctx.window_manager.xr_session_settings
    xs.shading.use_scene_lights = False
    s.light_color = (1.0, 0.82, 0.62)
    for kind in ('POINT', 'SUN', 'SPOT', 'AREA'):
        s.light_type = kind
        pose = aim_pose((0.3, -0.2, 1.1), (0.2, 1.0, -0.5))
        tip = pose.copy()
        tip.translation = pose @ Vector((0.0, 0.0, -0.04))
        h = lookdev.pick(ctx, s, False, pose, tip, 1.0)
        check(isinstance(h, lookdev.PlaceLight) and h.changed,
              f"{kind}: the main trigger in empty space places a light")
        obj = h.obj
        check(obj.type == 'LIGHT' and obj.data.type == kind, f"{kind}: of the New type ({obj.data.type})")
        check(close(obj.matrix_world.translation, tip.translation), f"{kind}: at the tip")
        check((forward(obj.matrix_world) - forward(pose)).length < 1e-5, f"{kind}: aimed where the controller points")
        check(lookdev.selected_light(ctx) == obj, f"{kind}: and selected")
        power = s.light_sun if kind == 'SUN' else s.light_power
        check(abs(obj.data.energy - power) < 1e-4, f"{kind}: with the power for new lights ({obj.data.energy:.2f})")
        check(close(obj.data.color, lookdev.to_linear(s.light_color)), f"{kind}: and their color, in linear")
        pose2 = aim_pose((0.0, 0.1, 1.3), (-1.0, 0.0, 0.0))
        tip2 = pose2.copy()
        tip2.translation = pose2 @ Vector((0.0, 0.0, -0.04))
        h.update(ctx, pose2, tip2, 1.0)
        h.finish(ctx)
        check(close(obj.matrix_world.translation, tip2.translation) and
              (forward(obj.matrix_world) - Vector((-1, 0, 0))).length < 1e-5,
              f"{kind}: it follows and aims with the controller while held")
    check(len(lookdev.lights(ctx)) == 4, "four lights placed")
    check(xs.shading.use_scene_lights, "placing a light turns on scene lights for Material Preview in VR")
    spot = next(o for o in lookdev.lights(ctx) if o.data.type == 'SPOT')
    area = next(o for o in lookdev.lights(ctx) if o.data.type == 'AREA')
    sun = next(o for o in lookdev.lights(ctx) if o.data.type == 'SUN')
    check(abs(spot.data.spot_size - s.light_spot) < 1e-5 and abs(spot.data.shadow_soft_size - s.light_radius) < 1e-5,
          "a spot gets the cone and radius for new lights")
    check(abs(area.data.size - s.light_area) < 1e-5 and abs(area.data.size_y - s.light_area) < 1e-5,
          "an area light gets the area size")
    check(abs(sun.data.angle - s.light_angle) < 1e-5, "a sun gets the sun angle")

    # Grid snap places it on the grid, like new primitives.
    s.grid_snap, s.grid_size = True, 0.1
    s.light_type = 'POINT'
    pose = aim_pose((0.33, 0.0, 1.04), (0, 1, 0))
    h = lookdev.pick(ctx, s, False, pose, pose, 1.0)
    check(close(h.obj.matrix_world.translation, (0.3, 0.0, 1.0)), "with Snap to Grid it lands on the grid")
    s.grid_snap = False
    lookdev.delete_light(ctx, h.obj)

    # Through the trigger's dispatch.
    s.show_panel = False
    pose = aim_pose((2.0, 2.0, 2.0), (0, 1, 0))
    h = operators._pick_handler(ctx, s, actions.HANDS[actions.main_index(ctx)], pose, pose, 1.0, 1.0)
    check(isinstance(h, lookdev.PlaceLight), "Tools > Light: the trigger operator places a light")
    lookdev.delete_light(ctx, h.obj)
    s.show_panel = True


def test_grab_select():
    print("== grabbing, selecting, deleting lights")
    clear_scene()
    ctx, s = light_tool()
    s.light_type = 'SPOT'
    pose = aim_pose((0.0, 0.0, 1.0), (0, 0, -1))
    obj = lookdev.PlaceLight(ctx, pose, pose).obj
    primitives.make_active(ctx, None)
    start = obj.matrix_world.copy()

    # Main hand on the light: grab it.
    near = aim_pose((0.01, 0.0, 1.0), (0, 1, 0))
    h = lookdev.pick(ctx, s, False, near, near, 1.0)
    check(isinstance(h, lookdev.GrabLight), "the main trigger on a light grabs it")
    check(lookdev.selected_light(ctx) == obj, "and selects it")
    jitter = aim_pose((0.012, 0.002, 1.0), (0, 1, 0))
    h.update(ctx, jitter, jitter, 1.0)
    check(obj.matrix_world == start and not h.changed, "a press that barely moves doesn't nudge it (grab threshold)")
    moved = aim_pose((0.31, 0.2, 1.1), (1.0, 0.0, 0.0))
    h.update(ctx, moved, moved, 1.0)
    h.finish(ctx)
    check(h.changed, "moving the hand moves it")
    check((obj.matrix_world.translation - (moved.translation + (start.translation - jitter.translation))).length < 1e-4,
          "it keeps its offset from the tip")
    check((forward(obj.matrix_world) - Vector((1, 0, 0))).length < 1e-5, "and aims where the controller points")

    # Off hand on the light: select it, and a move keeps its aim relative to the hand.
    primitives.make_active(ctx, None)
    tip = obj.matrix_world.translation.copy()
    off = lookdev.pick(ctx, s, True, Matrix.Translation(tip), Matrix.Translation(tip), 1.0)
    check(isinstance(off, interactions.MoveObject) and off.snap is False,
          "the off trigger on a light selects and moves it")
    check(lookdev.selected_light(ctx) == obj, "the off trigger selects the light")
    before = forward(obj.matrix_world)
    off.update(ctx, Matrix(), Matrix.Translation(tip + Vector((0.1, 0.0, 0.0))), 1.0)
    off.finish(ctx)
    check(close(obj.matrix_world.translation, tip + Vector((0.1, 0.0, 0.0))) and
          (forward(obj.matrix_world) - before).length < 1e-5, "moved by the off hand, it keeps its aim")
    empty = lookdev.pick(ctx, s, True, Matrix.Translation((5, 5, 5)), Matrix.Translation((5, 5, 5)), 1.0)
    check(isinstance(empty, interactions.Deselect) and lookdev.selected_light(ctx) is None,
          "the off trigger in empty space selects nothing")
    check(lookdev.light_at(ctx, tip + Vector((0.1, 0.0, 0.0)), 1.0) == obj and
          lookdev.light_at(ctx, tip + Vector((0.1, 0.0, 0.05)), 1.0) is None, "a light is in reach within 3 cm")
    check(lookdev.light_at(ctx, tip + Vector((0.1, 0.0, 0.05)), 2.0) == obj, "reach scales with the world")

    # Duplicate and delete (panel buttons).
    primitives.make_active(ctx, obj)
    press("light_duplicate")
    copy = lookdev.selected_light(ctx)
    check(copy is not None and copy != obj and copy.data != obj.data, "Duplicate copies the light (its own data)")
    data, name = copy.data.name, copy.name
    press("light_delete")
    check(name not in bpy.data.objects and data not in bpy.data.lights and lookdev.selected_light(ctx) is None,
          "Delete removes the light and its data")


def test_panel():
    print("== the Light page")
    clear_scene()
    ctx, s = light_tool()
    s.light_type = 'POINT'
    ids = widgets()
    need = {"tool_measure", "tool_camera", "tool_light", "light_point", "light_sun", "light_spot", "light_area",
            "light_pick", "light_color_WHITE", "light_power", "light_size", "view_solid", "view_material",
            "view_rendered", "look"}
    check(need <= set(ids), f"the page has its controls (missing {sorted(need - set(ids))})")
    check("light_delete" not in ids and "more" not in ids, "nothing selected: no Delete, no More")
    rail = vr_ui.rail_height(len(menus.MODES))
    _w, height = menus.layout(ctx)
    check(height <= max(0.2, rail) + 1e-9, f"page height {height:.3f} m (the rail: {rail:.3f} m)")

    # Nothing selected: the controls set what new lights get.
    press("light_sun")
    check(s.light_type == 'SUN' and widgets()["light_power"].text == "Strength", "Sun: the power is a Strength")
    slide("light_power", math.log10(8.0))
    check(abs(s.light_sun - 8.0) < 1e-4, f"Strength sets new suns' strength ({s.light_sun:.2f})")
    press("light_point")
    slide("light_power", math.log10(150.0))
    check(abs(s.light_power - 150.0) < 1e-3, f"the Power slider is logarithmic ({s.light_power:.1f} W)")
    check(widgets()["light_power"].fmt(widgets()["light_power"].value) == "150 W", "and reads in watts")
    press("light_color_COOL")
    check(close(s.light_color, dict((i, c) for i, _n, c in lookdev.LIGHT_PALETTE)['COOL']), "a swatch sets the color")
    check(widgets()["light_color_COOL"].active, "and shows as chosen")

    # A placed light is selected: the controls edit it, and new lights carry on from it.
    pose = aim_pose((0, 0, 1), (0, 1, 0))
    obj = lookdev.PlaceLight(ctx, pose, pose).obj
    ids = widgets()
    check({"light_delete", "light_duplicate", "more"} <= set(ids) and ids["light_pick"].value_text == obj.name,
          "selected: its name, Duplicate, Delete and More")
    check(close(obj.data.color, lookdev.to_linear(s.light_color)), "it got the chosen color")
    slide("light_power", math.log10(40.0))
    check(abs(obj.data.energy - 40.0) < 1e-3 and abs(s.light_power - 40.0) < 1e-3,
          "Power sets the light (and new lights follow)")
    slide("light_size", 0.2)
    check(abs(obj.data.shadow_soft_size - 0.2) < 1e-6, "Radius sets a point light's radius")
    press("light_color_RED")
    red = dict((i, c) for i, _n, c in lookdev.LIGHT_PALETTE)['RED']
    check(close(obj.data.color, lookdev.to_linear(red)) and close(lookdev.light_get(ctx, obj, 'color'), red),
          "a swatch sets its color (stored linear, shown as picked)")
    menus.set_more('TOOLS_LIGHT', True)
    ids = widgets()
    check({"light_kelvin", "light_temperature", "light_shadow"} <= set(ids), "More: temperature and shadow")
    slide("light_temperature", 3200.0)
    check(obj.data.use_temperature and abs(obj.data.temperature - 3200.0) < 1e-3, "Temperature turns Kelvin on")
    press("light_kelvin")
    check(not obj.data.use_temperature, "Kelvin turns it off")
    press("light_shadow")
    check(not obj.data.use_shadow, "Shadow toggles its shadow")
    menus.set_more('TOOLS_LIGHT', False)

    spot = lookdev.new_light(ctx, 'SPOT', Matrix.Translation((1, 0, 1)))
    ids = widgets()
    check("light_cone" in ids and "light_size" not in ids, "a spot shows its Cone")
    slide("light_cone", math.radians(30.0))
    check(abs(spot.data.spot_size - math.radians(30.0)) < 1e-5, "Cone sets the spot's angle")
    menus.set_more('TOOLS_LIGHT', True)
    slide("light_blend", 0.6)
    slide("light_radius", 0.08)
    check(abs(spot.data.spot_blend - 0.6) < 1e-5 and abs(spot.data.shadow_soft_size - 0.08) < 1e-5,
          "More: the spot's blend and radius")
    _w, height = menus.layout(ctx)
    check(height < 0.32, f"with More open: {height:.3f} m")
    menus.set_more('TOOLS_LIGHT', False)
    area = lookdev.new_light(ctx, 'AREA', Matrix.Translation((2, 0, 1)))
    slide("light_size", 0.6)
    check(abs(area.data.size - 0.6) < 1e-5 and abs(area.data.size_y - 0.6) < 1e-5, "Size sets a square area's side")
    sun = lookdev.new_light(ctx, 'SUN', Matrix.Translation((3, 0, 1)))
    slide("light_size", math.radians(4.0))
    check(abs(sun.data.angle - math.radians(4.0)) < 1e-5, "Angle sets a sun's angle")

    # The lights picker selects one by name, or none.
    press("light_pick")
    check(menus.picker_open(), "Edit opens the lights picker")
    pick = widgets()
    check(f"opt_{obj.name}" in pick and "opt_" in pick, "it lists the lights and None")
    pick[f"opt_{obj.name}"].on_press()
    check(lookdev.selected_light(ctx) == obj, "choosing one selects it")
    press("light_pick")
    widgets()["opt_"].on_press()
    check(lookdev.selected_light(ctx) is None, "None: back to editing new lights")

    # B steps through the types; the hints and tip status follow.
    s.light_type = 'POINT'
    op = type("H", (), {"execute": operators.VRSCULPT_OT_cycle_brush.execute, "off_hand": False,
                        "report": lambda *a: None})()
    op.execute(ctx)
    check(s.light_type == 'SUN', "B: the next type")
    hints = controllers.hints(s, controllers.Info())
    main = hints['right']
    check(main['trigger'][1] == "Place, grab light" and main['top'][1] == "Next: Spot", f"hints ({main['top']})")
    check(hints['left']['trigger'][1].startswith("Select"), "the off hand's hint: select")
    title, _desc, color, icon = hud.card_content(s, None)
    check(title == "Sun Light" and icon == "light" and color == hud.LIGHT_COLOR, f"tip status: {title}")
    check(hud.status_size(ctx, s).endswith("W/m\u00b2"), f"tip size: {hud.status_size(ctx, s)}")
    check(hud.tool_color(s, True) != hud.tool_color(s, False), "the off hand's ring is the selection's color")
    lookdev.choose_light(ctx, obj.name)
    check(hud.status_size(ctx, s) == lookdev.watts(obj.data.energy), "with a light selected, its power")
    s.light_type = 'POINT'


def test_gizmos():
    print("== gizmos")
    clear_scene()
    ctx, s = light_tool()
    lights = {k: lookdev.new_light(ctx, k, aim_pose((i, 0, 1), (0, 0, -1))) for i, k in
              enumerate(('POINT', 'SUN', 'SPOT', 'AREA'))}
    for kind, obj in lights.items():
        lines = lookdev.gizmo_lines(obj, 1.0)
        check(len(lines) % 2 == 0 and (kind == 'POINT') == (not lines), f"{kind}: {len(lines) // 2} gizmo lines")
    spot = lights['SPOT']
    tip = max(lookdev.gizmo_lines(spot, 1.0), key=lambda p: (p - spot.matrix_world.translation).length)
    check(tip.z < spot.matrix_world.translation.z, "the spot's cone points where it aims")
    lights['POINT'].data.color = (1.0, 0.5, 0.0)
    c = lookdev.gizmo_color(lights['POINT'])
    check(abs(max(c) - 1.0) < 1e-4 and c[2] < 0.05, f"gizmos show the light's color at full brightness ({c})")
    lights['POINT'].data.color = (1.0, 1.0, 1.0)
    lights['POINT'].data.use_temperature, lights['POINT'].data.temperature = True, 2000.0
    c = lookdev.gizmo_color(lights['POINT'])
    check(c[0] > c[2], "a warm temperature shows warm")
    try:
        import gpu
        gpu.init()
        off = gpu.types.GPUOffScreen(64, 64)
        ss = type("SS", (), {"viewer_pose_location": Vector((0, -2, 1.5))})()
        with off.bind():
            lookdev.draw(ctx, ss, [Vector((0.0, 0.0, 1.0)), Vector((9, 9, 9))], 1.0)
        off.free()
        check(True, "the gizmos draw (offscreen)")
    except Exception as ex:  # noqa: BLE001
        check(False, f"the gizmos draw (offscreen): {ex}")


# --- the VR view's look --------------------------------------------------------------------

def test_look():
    print("== the VR view's look")
    clear_scene()
    ctx, s = light_tool()
    sh = ctx.window_manager.xr_session_settings.shading
    sh.type = 'SOLID'
    press("view_material")
    check(sh.type == 'MATERIAL', "the view row sets the VR session's shading (not the desktop's)")
    press("view_solid")
    check(sh.type == 'SOLID', "Solid")
    press("look")
    check(menus.sheet() == 'LOOK' and {"back", "solid_light_studio", "solid_light_matcap", "look_color_material"}
          <= set(widgets()), "Look opens its sheet")
    press("solid_light_matcap")
    check(sh.light == 'MATCAP' and "look_matcap" in widgets(), "Matcap lighting, with its picker")
    press("look_matcap")
    ids = widgets()
    tiles = [w for w in ids.values() if w.kind == 'tile']
    check(len(tiles) == menus.KIT_COLS * menus.KIT_ROWS and "look_next" in ids, f"matcap tiles, paged ({len(tiles)})")
    check(all(t.image is not None for t in tiles), "every matcap tile has a preview")
    target = next(t for t in tiles if not t.active)
    target.on_press()
    check(sh.studio_light == target.id[len("look_tile_"):], f"a tile sets the matcap ({sh.studio_light})")
    check(menus.sheet() == 'LOOK' and widgets()[target.id].active, "the page stays open, the tile chosen")
    press("look_next")
    check(menus._look["page"] == 1, "next page")
    press("look_back")
    check("look_matcap" in widgets(), "Back to the Look sheet")
    chosen = sh.studio_light
    press("view_material")
    press("view_solid")
    check(sh.studio_light == chosen and sh.light == 'MATCAP', "the matcap stays through a shading switch")
    press("solid_light_studio")
    press("look_studio")
    tiles = [w for w in widgets().values() if w.kind == 'tile']
    check(len(tiles) == len(lookdev.studio_lights(ctx, 'STUDIO')), "studio light tiles")
    next(t for t in tiles if t.text == "Rim").on_press()
    check(sh.light == 'STUDIO' and sh.studio_light == "rim.sl", "a tile sets the studio light")
    press("look_back")
    press("look_color_random")
    check(sh.color_type == 'RANDOM', "Color: Random")
    press("look_color_material")

    press("view_material")
    ids = widgets()
    check({"look_world_hdri", "look_world_scene", "look_world", "look_rotation", "look_strength", "look_use_world",
           "look_lights"} <= set(ids), "Material: the HDRI, its turn and strength, Use as world, scene lights")
    press("look_world")
    tiles = [w for w in widgets().values() if w.kind == 'tile']
    check(len(tiles) == len(lookdev.studio_lights(ctx, 'WORLD')), f"HDRI tiles ({len(tiles)})")
    next(t for t in tiles if t.text == "City").on_press()
    check(sh.studio_light == "city.exr", "a tile sets the HDRI")
    press("look_back")
    slide("look_rotation", 0.5)
    slide("look_strength", 1.5)
    check(abs(sh.studiolight_rotate_z - 0.5) < 1e-5 and abs(sh.studiolight_intensity - 1.5) < 1e-5,
          "Turn and Strength set the HDRI's rotation and intensity")
    sh.use_scene_lights = False
    press("look_lights")
    check(sh.use_scene_lights, "Lights: Material Preview shows the scene lights")
    menus.set_more('LOOK_MATERIAL', True)
    slide("look_background", 1.0)
    check(abs(sh.studiolight_background_alpha - 1.0) < 1e-6, "More: the HDRI as background")
    menus.set_more('LOOK_MATERIAL', False)
    press("view_rendered")
    title = next(w for w in menus.layout(ctx)[0] if w.kind == 'title')
    check(lookdev.engine_name(ctx) in title.value_text, f"Rendered names the engine ({title.value_text})")
    check(lookdev.scene_world_on(ctx), "Rendered shows the scene's world (Blender's default)")
    press("look_world_hdri")
    check(not sh.use_scene_world_render and "look_world" in widgets(), "World: HDRI for Rendered too")
    press("look_world_scene")
    check(sh.use_scene_world_render, "World: Scene")
    sh.type = 'SOLID'
    menus.reset()


def test_previews():
    print("== previews")
    ctx = bpy.context
    for kind in ('MATCAP', 'WORLD', 'STUDIO'):
        sl = lookdev.studio_lights(ctx, kind)[0]
        t0 = time.perf_counter()
        px = lookdev.preview_pixels(kind, sl.name)
        ms = 1000 * (time.perf_counter() - t0)
        ok = px is not None and px.shape == (lookdev.PREVIEW_PX, lookdev.PREVIEW_PX, 4) and np.isfinite(px).all()
        check(ok, f"{kind} {sl.name}: a {lookdev.PREVIEW_PX} px preview ({ms:.0f} ms)")
        if not ok:
            continue
        n = lookdev.PREVIEW_PX
        check(px.min() >= 0.0 and px.max() <= 1.0, f"{kind}: values in 0..1")
        check(px[0, 0, 3] < 0.01 and px[n // 2, n // 2, 3] > 0.99, f"{kind}: a ball (clear corners, solid middle)")
        check(px[n // 2, n // 2, :3].max() > 0.05, f"{kind}: not black")
    a, b = lookdev.preview('MATCAP', "basic_grey.exr"), lookdev.preview('MATCAP', "basic_grey.exr")
    check(a is b and a == lookdev.Preview('MATCAP', "basic_grey.exr") and hash(a) == hash(b),
          "previews are kept and compare by name (the panel's cache compares widgets)")
    lookdev.prepare('MATCAP', ["basic_grey.exr"])
    check(a.pixels is not None, "prepare() works them out before drawing")
    check(lookdev.preview('MATCAP', "no_such.exr") is not None and lookdev.preview('MATCAP', "no_such.exr").compute()
          is None and lookdev.preview('MATCAP', "no_such.exr") is None, "an unknown one has no preview (an icon)")
    check(lookdev.pretty("check_normal+y.exr") == "Check normal+y" and lookdev.pretty("rim.sl") == "Rim",
          "names read nicely")


def test_world():
    print("== Use as world")
    clear_scene()
    ctx = bpy.context
    sh = ctx.window_manager.xr_session_settings.shading
    old = ctx.scene.world
    sh.type = 'SOLID'
    lookdev.set_studio(ctx, 'WORLD', "forest.exr")
    check(sh.type == 'SOLID', "choosing an HDRI from Solid leaves the shading as it was")
    sh.studiolight_rotate_z, sh.studiolight_intensity = 0.7, 1.4
    world = lookdev.use_as_world(ctx)
    check(world is not None and ctx.scene.world == world and world.get(lookdev.WORLD_KEY) == "forest.exr",
          "it builds a world from the VR view's HDRI and makes it the scene's")
    nt = world.node_tree
    out = next(n for n in nt.nodes if n.type == 'OUTPUT_WORLD')
    bg = out.inputs["Surface"].links[0].from_node
    env = bg.inputs["Color"].links[0].from_node
    mapping = env.inputs["Vector"].links[0].from_node
    coord = mapping.inputs["Vector"].links[0].from_node
    check((bg.type, env.type, mapping.type, coord.type) == ('BACKGROUND', 'TEX_ENVIRONMENT', 'MAPPING', 'TEX_COORD'),
          "Texture Coordinate > Mapping > Environment Texture > Background > World Output")
    check(mapping.inputs["Vector"].links[0].from_socket.name == "Generated", "from Generated coordinates")
    check(abs(mapping.inputs["Rotation"].default_value[2] - 0.7) < 1e-6 and
          abs(bg.inputs["Strength"].default_value - 1.4) < 1e-6,
          "the Mapping turns it as much as the VR view's rotation; the strength is its intensity")
    check(env.image is not None and env.image.packed_file is not None and env.image.size[0] > 0,
          "the HDRI is packed into the file (Blender's own folder changes with each version)")
    check(old is not None and old.use_fake_user, "the world it replaced is kept (fake user)")
    sh.type = 'MATERIAL'
    check(lookdev.in_world(ctx), "In world: the world matches the VR view")
    sh.studiolight_rotate_z = 0.2
    check(not lookdev.in_world(ctx), "a turn since: not any more")
    again = lookdev.use_as_world(ctx)
    check(again == world and len([w for w in bpy.data.worlds if w.get(lookdev.WORLD_KEY)]) == 1 and
          abs(lookdev.world_value(ctx, 'rotation') - 0.2) < 1e-6 and lookdev.in_world(ctx),
          "again: the same world, rebuilt")
    images = len(bpy.data.images)
    lookdev.use_as_world(ctx)
    check(len(bpy.data.images) == images, "the image is reused")

    # The panel in Scene mode edits our world, and the VR view's HDRI follows.
    menus.reset()
    menus.set_mode(ctx, 'TOOLS')
    menus.open_look()
    lookdev.set_scene_world(ctx, True)
    ids = widgets()
    check({"look_world", "look_rotation", "look_strength"} <= set(ids) and "look_use_world" not in ids,
          "Scene, with our world: its HDRI, turn and strength")
    slide("look_rotation", -1.0)
    slide("look_strength", 0.5)
    check(abs(lookdev.world_value(ctx, 'rotation') + 1.0) < 1e-6 and abs(lookdev.world_value(ctx, 'strength') - 0.5)
          < 1e-6 and abs(sh.studiolight_rotate_z + 1.0) < 1e-6, "the sliders turn the world and the HDRI with it")
    lookdev.choose_hdri(ctx, "night.exr")
    check(lookdev.world_value(ctx, 'hdri') == "night.exr" and sh.studio_light == "night.exr",
          "another HDRI changes the world's too")
    ctx.scene.world = old
    ids = widgets()
    check("look_use_world" in ids and "look_world" not in ids, "a world of its own: its strength, and Use HDRI")
    lookdev.set_scene_world(ctx, False)
    sh.type = 'SOLID'
    menus.reset()


# --- the Material page ------------------------------------------------------------------------

def material_page(obj):
    ctx = bpy.context
    menus.reset()
    menus.set_mode(ctx, 'MODEL')
    primitives.make_active(ctx, obj)
    ctx.scene.vr_sculpt.model_page = 'MATERIAL'
    menus.open_sheet('OBJECT')


def test_material():
    print("== the Material page")
    clear_scene()
    ctx = bpy.context
    check(('MATERIAL', "Material") in menus.MODEL_PAGES, "Material is one of the object's pages")
    box = primitives.create(ctx, 'CUBE', (0, 0, 0))
    material_page(box)
    ids = widgets()
    check({"page_material", "mat_pick", "mat_preset", "mat_color_RED", "mat_roughness", "mat_metallic"} <= set(ids),
          f"the page's controls ({sorted(i for i in ids if i.startswith('mat_'))[:6]}...)")
    check(ids["mat_pick"].value_text == "none" and not box.material_slots, "no material yet")
    _w, height = menus.layout(ctx)
    check(height < 0.25, f"page height {height:.3f} m")
    press("mat_color_RED")
    mat = lookdev.material_of(box)
    bsdf = lookdev.principled(mat)
    red = dict((i, c) for i, _n, c in vr_sculpt.strokes.PALETTE)['RED']
    check(mat is not None and bsdf is not None, "a swatch makes a Principled material")
    check(close(bsdf.inputs["Base Color"].default_value[:3], lookdev.to_linear(red)), "the base color, linear")
    check(close(mat.diffuse_color[:3], bsdf.inputs["Base Color"].default_value[:3]),
          "the viewport display color follows (Solid shows it)")
    check(widgets()["mat_color_RED"].active, "the swatch shows as chosen")
    slide("mat_roughness", 0.2)
    slide("mat_metallic", 0.9)
    check(abs(bsdf.inputs["Roughness"].default_value - 0.2) < 1e-6 and abs(mat.roughness - 0.2) < 1e-6 and
          abs(bsdf.inputs["Metallic"].default_value - 0.9) < 1e-6 and abs(mat.metallic - 0.9) < 1e-6,
          "Rough and Metal set the shader and the viewport display")
    menus.set_more('OBJECT_MATERIAL', True)
    slide("mat_val", 0.5)
    check(abs(max(lookdev.material_value(box, 'color')) - 0.5) < 1e-3, "More: Value darkens the color")
    menus.set_more('OBJECT_MATERIAL', False)

    for ident, name, rgb, inputs in lookdev.PRESETS:
        press("mat_preset")
        widgets()["opt_" + ident].on_press()
        b = lookdev.principled(lookdev.material_of(box))
        ok = all(abs(b.inputs[k].default_value - v) < 1e-5 for k, v in inputs.items())
        if rgb is not None:
            ok = ok and close(lookdev.material_value(box, 'color'), rgb, 2e-3)
        check(ok and lookdev.preset_name(lookdev.material_of(box)) == name, f"preset {name}")
    m = lookdev.material_of(box)
    b = lookdev.principled(m)
    check(close(b.inputs["Emission Color"].default_value[:3], b.inputs["Base Color"].default_value[:3]) and
          b.inputs["Emission Strength"].default_value > 0, "Emissive glows in its base color")
    press("mat_preset")
    widgets()["opt_GLASS"].on_press()
    check(b.inputs["Transmission Weight"].default_value == 1.0 and b.inputs["Emission Strength"].default_value == 0.0
          and m.use_raytrace_refraction and abs(m.diffuse_color[3] - (1 - lookdev.GLASS_ALPHA)) < 1e-6,
          "Glass: full transmission, no glow left over, EEVEE refraction, see-through in Solid")
    press("mat_preset")
    widgets()["opt_PLASTIC"].on_press()
    check(not m.use_raytrace_refraction and m.diffuse_color[3] == 1.0, "Plastic after Glass: opaque again")

    # A shared material: the first change gives the object its own copy.
    other = primitives.create(ctx, 'CUBE', (1, 0, 0))
    other.data.materials.append(m)
    material_page(box)
    check(lookdev.is_shared(box) and widgets()["mat_pick"].text == "Shared", "a shared material says so")
    slide("mat_roughness", 0.77)
    mine = lookdev.material_of(box)
    check(mine != m and lookdev.material_of(other) == m, "editing it gives this object a copy; the other keeps its own")
    check(abs(lookdev.principled(m).inputs["Roughness"].default_value - 0.77) > 1e-3, "the other is untouched")
    check(not lookdev.is_shared(box), "not shared any more")

    # A mesh shared by two objects (linked duplicates): the copy goes on an object-linked slot.
    twin = bpy.data.objects.new("Twin", box.data)
    ctx.scene.collection.objects.link(twin)
    material_page(box)
    check(lookdev.is_shared(box), "a material on a shared mesh is shared")
    press("mat_color_BLUE")
    check(box.material_slots[0].link == 'OBJECT' and lookdev.material_of(box) != lookdev.material_of(twin) and
          box.data == twin.data, "the copy is linked to the object; the mesh stays shared")

    # The picker assigns another material, or a new one.
    material_page(other)
    press("mat_pick")
    widgets()["opt_" + mine.name].on_press()
    check(lookdev.material_of(other) == mine, "the picker assigns a material")
    press("mat_pick")
    widgets()["opt_"].on_press()
    check(lookdev.material_of(other) not in (m, mine) and lookdev.principled(lookdev.material_of(other)) is not None,
          "New material")

    # A material that isn't a Principled BSDF isn't touched.
    odd = bpy.data.materials.new("Odd")
    for n in list(odd.node_tree.nodes):
        if n.type == 'BSDF_PRINCIPLED':
            odd.node_tree.nodes.remove(n)
    other.material_slots[0].material = odd
    material_page(other)
    ids = widgets()
    check("mat_new" in ids and "mat_color_RED" not in ids, "not a Principled BSDF: it offers a new material instead")

    # A painted base color (an image texture): the swatches give way.
    tex = bpy.data.materials.new("Painted")
    bsdf = lookdev.principled(tex)
    img = tex.node_tree.nodes.new("ShaderNodeTexImage")
    tex.node_tree.links.new(img.outputs["Color"], bsdf.inputs["Base Color"])
    other.material_slots[0].material = tex
    material_page(other)
    ids = widgets()
    check("mat_color_RED" not in ids and "mat_roughness" in ids, "a texture's base color: no swatches, the rest stays")
    menus.reset()


# --- undo -------------------------------------------------------------------------------------

class FakeXR:
    def __init__(self, loc, hand):
        self.action_set = actions.ACTIONMAP
        self.action = actions.ACTION_STROKE
        self.user_path = hand
        self.controller_location = Vector(loc)
        self.controller_rotation = Quaternion()
        self.state = (1.0, 0.0)
        self.float_threshold = 0.3


class FakeEvent:
    __slots__ = ("type", "value", "xr")

    def __init__(self, type, value='NOTHING', xr=None):
        self.type, self.value, self.xr = type, value, xr


class FakeSession:
    viewer_scale = 1.0
    viewer_pose_location = Vector((0.0, -1.0, 1.5))

    def __init__(self):
        self.loc = Vector((0.0, 0.0, 1.0))

    def action_state_get(self, context, action_set, action, user_path):
        return 1.0

    def controller_aim_location_get(self, context, index):
        return self.loc.copy()

    def controller_aim_rotation_get(self, context, index):
        return Quaternion()

    def controller_grip_location_get(self, context, index):
        return Vector((-0.3, -0.5, 1.0))

    def controller_grip_rotation_get(self, context, index):
        return Quaternion()

    def haptic_action_apply(self, *args):
        return True


class FakeWM:
    def __init__(self, ss):
        self.xr_session_state = ss
        self.xr_session_settings = bpy.context.window_manager.xr_session_settings
        self.windows = bpy.context.window_manager.windows
        self.timers = set()

    def event_timer_add(self, step, window=None):
        t = object()
        self.timers.add(t)
        return t

    def event_timer_remove(self, t):
        self.timers.discard(t)

    def modal_handler_add(self, op):
        return True


class FakeContext:
    def __getattr__(self, name):
        return getattr(bpy.context, name)

    def __init__(self, wm):
        self.window_manager = wm
        self.scene = bpy.context.scene
        self.window = None
        self.screen = None


def harness():
    cls = operators.VRSCULPT_OT_stroke
    h = type("H", (), {k: cls.__dict__[k] for k in ("invoke", "modal", "_modal", "_apply", "_finish", "_abandon",
                                                      "_start_modal")})()
    h.report = lambda kind, msg: print("    report:", kind, msg)
    return h


def test_undo_guard():
    print("== undo while a light is held")
    clear_scene()
    _ctx, s = light_tool()
    s.show_panel = False
    s.tip_offset = 0.0
    running = operators.session_running
    operators.session_running = lambda context: True
    try:
        ss = FakeSession()
        ctx = FakeContext(FakeWM(ss))
        main = actions.HANDS[actions.main_index(bpy.context)]
        op = harness()
        r = op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0, 0, 1), main)))
        check(r == {'RUNNING_MODAL'} and isinstance(op.handler, lookdev.PlaceLight), "the trigger places a light")
        name = op.handler.obj.name
        operators._on_data_swap()  # What an undo does while the trigger is held.
        r = op.modal(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.5, 0, 1), main)))
        check(r == {'CANCELLED'} and op.handler is None, "an undo mid-place lets go without touching the light")
        check(close(bpy.data.objects[name].matrix_world.translation, (0, 0, 1)), "the light wasn't moved after it")

        op = harness()
        op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.005, 0, 1), main)))
        check(isinstance(op.handler, lookdev.GrabLight), "the trigger on it grabs it")
        operators._on_data_swap()
        r = op.modal(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.5, 0.5, 1), main)))
        check(r == {'CANCELLED'} and close(bpy.data.objects[name].matrix_world.translation, (0, 0, 1)),
              "an undo mid-grab lets go too")

        op = harness()
        op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.005, 0, 1), main)))
        op.modal(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.3, 0.0, 1.0), main)))
        r = op.modal(ctx, FakeEvent('XR_ACTION', 'RELEASE', FakeXR((0.3, 0.0, 1.0), main)))
        check(r == {'FINISHED'}, "a grab that moved the light is one undo step")
        op = harness()
        op.invoke(ctx, FakeEvent('XR_ACTION', 'PRESS', FakeXR((0.3, 0, 1), main)))
        r = op.modal(ctx, FakeEvent('XR_ACTION', 'RELEASE', FakeXR((0.3, 0, 1), main)))
        check(r == {'CANCELLED'}, "a press that only selects isn't")
    finally:
        operators.session_running = running
        s.show_panel = True
        s.tip_offset = 0.04


def main():
    vr_sculpt.register()
    try:
        for t in (test_place, test_grab_select, test_panel, test_gizmos, test_look, test_previews, test_world,
                  test_material, test_undo_guard):
            try:
                t()
            except Exception:
                traceback.print_exc()
                failures.append(t.__name__ + " raised")
    finally:
        vr_sculpt.unregister()
    print("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")


main()
