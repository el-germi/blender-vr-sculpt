"""Windowed check of the hand's lines against shading and dyntopo, in the real viewport (not an image render, like the
XR draw: draw_view3d would draw the mesh from before the session). Saves tests/lines_shading.png, one tile per case in
the order of CASES and then the extras named in the result, and writes tests/lines_preview_result.txt.

blender --factory-startup test_scene.blend --python tests/lines_preview.py
"""

import math
import os
import sys
import time
import traceback

import bmesh
import bpy
import numpy as np
from mathutils import Euler, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SHEET = os.path.join(ROOT, "tests", "lines_shading.png")
TILE = os.path.join(ROOT, "tests", "lines_tile.png")
LOG = os.path.join(ROOT, "tests", "lines_preview_result.txt")
lines = []
failures = []

# (name, shape, the mesh's own faces smooth, VR Shading, dyntopo, edges marked hard)
CASES = (
    ("flat mesh", 'BALL', False, None, False, False),
    ("flat mesh, dyntopo", 'BALL', False, None, True, False),
    ("smooth mesh", 'BALL', True, None, False, False),
    ("smooth mesh, dyntopo", 'BALL', True, None, True, False),
    ("VR Smooth", 'BALL', False, 'SMOOTH', False, False),
    ("VR Smooth, dyntopo", 'BALL', False, 'SMOOTH', True, False),
    ("VR Flat, dyntopo", 'BALL', True, 'FLAT', True, False),
    ("box VR Auto", 'BOX', False, 'AUTO', False, False),
    ("box VR Auto, dyntopo", 'BOX', False, 'AUTO', True, False),
    ("box VR Smooth + hard edges", 'BOX', False, 'SMOOTH', False, True),
    ("box VR Smooth + hard edges, dyntopo", 'BOX', False, 'SMOOTH', True, True),
)
LEVEL = Quaternion((1.0, 0.0, 0.0), math.pi / 2)  # A controller held level, pointing at the mesh (+Y).


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def check(cond, msg):
    log(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def quit_cleanly():
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

    def __init__(self):
        self.loc = Vector((-0.3, -1.0, 1.0))
        self.rot = LEVEL.copy()

    def controller_aim_location_get(self, context, index):
        return self.loc.copy()

    def controller_aim_rotation_get(self, context, index):
        return self.rot.copy()


def run():
    from vr_sculpt import lookdev, menus, native, rest, shading
    ctx = bpy.context
    s = ctx.scene.vr_sculpt
    win = ctx.window_manager.windows[0]
    area, region = native.largest_view3d(win)
    space = area.spaces.active
    xr_view = ctx.window_manager.xr_session_settings.shading
    ss = Session()
    tiles = []
    s.engine = 'NATIVE'
    menus.reset()

    def view(box=False):
        sh = space.shading
        sh.type = 'MATERIAL'
        sh.use_scene_lights = False
        sh.use_scene_world = False
        space.overlay.show_overlays = False
        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, 0.0, 0.0)
        # A box shows its corner.
        rv3d.view_rotation = Euler((1.25, 0.0, -0.6)).to_quaternion() if box else Quaternion((1, 0, 0), math.pi / 2)
        rv3d.view_distance = 3.6 if box else 2.9

    def shot():
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=TILE)
        im = bpy.data.images.load(TILE, check_existing=False)
        w, h = im.size
        px = np.empty(w * h * 4, np.float32)
        im.pixels.foreach_get(px)
        bpy.data.images.remove(im)
        px = px.reshape(h, w, 4)
        # The middle of the viewport, past the toolbars.
        side = min(int(h * 0.62), int(w * 0.5))
        y0, x0 = int(h * 0.2), (w - side) // 2
        tiles.append(px[y0:y0 + side, x0:x0 + side].copy())

    def add_box():
        me = bpy.data.meshes.new("Box")
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.6)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=24, use_grid_fill=True)
        bm.to_mesh(me)
        bm.free()
        obj = bpy.data.objects.new("Box", me)
        ctx.scene.collection.objects.link(obj)
        ctx.view_layer.objects.active = obj
        obj.select_set(True)
        return obj

    def stroke(obj, x, z, radius, invert=False, brush="Draw", n=8, step=0.015, strength=None, box=False):
        native.activate_brush(ctx, brush)
        if strength is not None:
            native.set_strength(ctx, strength)
        st = native.NativeStroke(ctx, obj, radius, invert=invert)
        for i in range(n):
            tip = Vector((x + step * i, -0.802, z)) if box else Vector((x + step * i, -1.0, z)).normalized() * 1.002
            st.update(tip, None, 1.0)
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        st.finish()

    first = True
    for name, shape, smooth, mode, dyn, hard in CASES:
        log("---", name)
        box = shape == 'BOX'
        view(box)
        active = ctx.view_layer.objects.active
        if active is not None and active.mode != 'OBJECT':
            menus.set_object_mode(ctx, active, 'OBJECT')
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_plane_add(size=9.0, location=(0.0, 0.0, -1.05))
        if box:
            obj = add_box()
        else:
            bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=5, radius=1.0, calc_uvs=False)
            obj = ctx.active_object
        me = obj.data
        me.polygons.foreach_set("use_smooth", [smooth] * len(me.polygons))
        if hard:  # The box's own edges, marked by hand as the Mark tool would.
            co = np.empty(len(me.vertices) * 3)
            me.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            ends = np.empty(len(me.edges) * 2, dtype=np.int64)
            me.edges.foreach_get("vertices", ends)
            on_rim = (np.abs(co) > 0.799).sum(axis=1) >= 2
            shading._write(me, shading.SHARP, on_rim[ends.reshape(-1, 2)].all(axis=1))
        me.update()
        if mode is not None:
            shading.set_mode(obj, mode)
        menus.set_mode(ctx, 'SCULPT')
        native.activate_brush(ctx, "Draw")
        if dyn:
            native.toggle_dyntopo(ctx)
            sculpt = ctx.tool_settings.sculpt
            sculpt.detail_type_method = 'CONSTANT'
            sculpt.constant_detail_resolution = 40.0
        xr_view.type = 'MATERIAL'
        s.hand_lines = False
        lookdev.toggle_lines(ctx)  # What the panel's press does.
        check(lookdev.sync_lines(ctx, ss), "the lines are on")
        check(obj.mode == 'SCULPT' and obj.use_dynamic_topology_sculpting == dyn,
              "turning them on left the session alone")
        for _i in range(6 if first else 2):
            area.tag_redraw()
            yield 0.5
        first = False
        # A dent, then a bump, both real strokes while the lines show.
        stroke(obj, -0.45, 0.3, 0.12, invert=True, strength=0.25, box=box)
        stroke(obj, 0.25, 0.3, 0.1, strength=0.25, box=box)
        lookdev.sync_lines(ctx, ss)
        yield 0.3
        shot()
        check(ctx.view_layer.material_override is not None, "the override is still there after the strokes")
        menus.set_object_mode(ctx, obj, 'OBJECT')
        rest.tick(ctx)
        sm = np.zeros(len(me.polygons), dtype=bool)
        me.polygons.foreach_get("use_smooth", sm)
        log(f"mesh after: {len(me.polygons)} faces, {sm.sum()} smooth, VR shading {shading.mode(obj)}")
        if not smooth and mode is None:
            check(sm.all(), "turning the lines on smoothed a flat mesh")
        lookdev.sync_lines(ctx, None)
        check(ctx.view_layer.material_override is None and lookdev.LINES_MATERIAL not in bpy.data.materials
              and lookdev.LINES_MARK not in obj, "gone without a trace at the end of the session")
        s.hand_lines = False
        yield 0.05

    # Fresh strokes under the lines: does the Smooth brush calm a rough one?
    log("--- smoothing a rough stroke")
    view()
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_plane_add(size=9.0, location=(0.0, 0.0, -1.05))
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=5, radius=1.0, calc_uvs=False)
    obj = ctx.active_object
    obj.data.polygons.foreach_set("use_smooth", [True] * len(obj.data.polygons))
    obj.data.update()
    menus.set_mode(ctx, 'SCULPT')
    native.activate_brush(ctx, "Draw")
    native.toggle_dyntopo(ctx)
    ctx.tool_settings.sculpt.detail_type_method = 'CONSTANT'
    ctx.tool_settings.sculpt.constant_detail_resolution = 100.0
    s.hand_lines = True
    lookdev.sync_lines(ctx, ss)
    yield 0.4
    stroke(obj, -0.1, 0.1, 0.1, invert=True, strength=0.5)
    yield 0.2
    shot()
    for k in range(4):
        stroke(obj, -0.15, 0.1, 0.22, brush="Smooth", strength=1.0, n=30, step=0.006)
        if k in (0, 3):
            yield 0.2
            shot()

    # The board rolling and sliding, as a hand does, and what it costs, in the same session.
    t = []
    for i in range(40):
        ss.rot = LEVEL @ Quaternion((0.0, 0.0, 1.0), 0.015 * i)  # Rolled about where it points.
        ss.loc = Vector((-0.3, -1.0, 1.0 + 0.002 * i))
        lookdev.sync_lines(ctx, ss)
        t0 = time.perf_counter()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        t.append((time.perf_counter() - t0) * 1000.0)
    t = sorted(t[5:])
    log(f"lines turning every frame: median {t[len(t) // 2]:.2f} ms, worst {t[-1]:.2f} ms "
        f"(region {region.width}x{region.height})")
    yield 0.2
    shot()

    # Everything else stays plain gray.
    space.region_3d.view_distance = 6.0
    yield 0.2
    shot()
    menus.set_object_mode(ctx, obj, 'OBJECT')
    rest.tick(ctx)
    lookdev.sync_lines(ctx, None)
    s.hand_lines = False

    cols = 4
    th, tw = tiles[0].shape[:2]
    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    sheet = np.zeros((th * len(rows), tw * cols, 4), np.float32)
    sheet[..., 3] = 1.0
    for r, row in enumerate(rows):
        for c, tile in enumerate(row):
            y0 = th * (len(rows) - 1 - r)
            sheet[y0:y0 + th, tw * c:tw * (c + 1)] = tile
    img = bpy.data.images.new("lines_sheet", tw * cols, th * len(rows))
    img.pixels.foreach_set(sheet.ravel())
    img.filepath_raw = SHEET
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)
    log("saved", SHEET, "tiles", len(tiles), "=", [c[0] for c in CASES],
        "+ rough, smoothed once, smoothed 4x, rolled, wide")


def start():
    try:
        import vr_sculpt
        from vr_sculpt import live
        vr_sculpt.register()
        live.unregister()  # It sees no session and would take the lines away; this script syncs them itself.
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
