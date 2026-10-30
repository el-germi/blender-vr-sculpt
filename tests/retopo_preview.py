"""Windowed check of Retopo mode: the hand panel's Retopo pages (Strip, Quad with an open quad, Relax with
More open, the Mesh picker) and the headset drawing of a retopo mesh on a subdivided Suzanne (Mirror X
strips around the eyes, a strip down the center line, an open quad, the tip's ring and snap, the off
hand's delete preview). Saves tests/retopo_panels.png and tests/retopo_scene.png.

blender --window-geometry 0 0 1920 1080 --factory-startup test_scene.blend --python tests/retopo_preview.py
"""

import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "retopo_preview_result.txt")
lines = []
panels = []  # (matrix, widgets, height, hover_id)
scene_state = {"on": False, "tips": None, "poses": None}
HANDLES = []  # this script's own draw handlers


def quit_cleanly():
    """Our draw handlers first, then the add-on (its GPU offscreens), then quit (see panel_preview.py)."""
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


def at(p):
    return Matrix.LocRotScale(Vector(p), Quaternion(), None)


def draw():
    from vr_sculpt import hud, retopo, vr_ui
    try:
        if scene_state["on"]:
            retopo.draw(bpy.context, scene_state["tips"], scene_state["poses"], 1.0)
            # The tip status of each tool, above the head.
            s = bpy.context.scene.vr_sculpt
            tool = s.retopo_tool
            viewer = Vector(bpy.context.region_data.view_matrix.inverted().translation)
            for i, (ident, _name, _icon) in enumerate(retopo.TOOLS):
                s.retopo_tool = ident
                anchor = Vector((-0.42, 0.0, -0.1 - 0.045 * i))  # A column, lower left.
                hud.draw_status(anchor, viewer, 1.0, hud.card_content(s, None), hud.status_size(bpy.context, s),
                                with_description=True)
            s.retopo_tool = tool
        else:
            for m, widgets, height, hover in panels:
                vr_ui.draw_panel(m, widgets, height, hover)
    except Exception:
        log(traceback.format_exc())


def front(surf, x, z, lift=0.004):
    """The tip just in front of the reference's front surface at (x, z)."""
    hit = surf.ray(Vector((x, -2.0, z)), Vector((0.0, 1.0, 0.0)), 5.0)
    if hit is None:
        return None
    return hit[0] + hit[1] * lift


def stroke(ctx, pts):
    from vr_sculpt import retopo
    pts = [p for p in pts if p is not None]
    if not pts:
        log("stroke: no point on the surface")
        return None
    s = ctx.scene.vr_sculpt
    h = retopo.pick(ctx, s, False, at(pts[0]), at(pts[0]), 1.0, 1.0)
    if h is None:
        return None
    for p in pts[1:]:
        h.update(ctx, at(p), at(p), 1.0)
    h.finish(ctx)
    return h


def snapshot(ctx, m, hover=None):
    from vr_sculpt import menus
    widgets, height = menus.layout(ctx)
    panels.append((m, widgets, height, hover))
    log("panel", len(panels), "height", round(height, 3), "widgets", len(widgets))


def setup():
    import vr_sculpt
    from vr_sculpt import menus, native, primitives, retopo, vr_ui
    try:
        vr_sculpt.register()
        vr_ui.ANIMATE = False
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        space = area.spaces.active
        space.overlay.show_floor = False
        space.overlay.show_axis_x = space.overlay.show_axis_y = False
        space.overlay.show_cursor = False
        space.shading.type = 'SOLID'
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        s = ctx.scene.vr_sculpt
        s.retopo_size = 0.022
        s.retopo_mirror = True
        s.retopo_opacity = 0.35

        bpy.ops.mesh.primitive_monkey_add(size=0.6, location=(0.0, 0.0, 0.0))
        ref = ctx.active_object
        ref.name = "Suzanne"
        mod = ref.modifiers.new("Subdivision", 'SUBSURF')
        mod.levels = 2
        bpy.ops.object.shade_smooth()
        primitives.make_active(ctx, ref)
        menus.set_mode(ctx, 'RETOPO')
        surf = retopo.surface(ctx, ref)
        log("surface triangles:", surf.triangles, "ms:", round(surf.ms, 1))
        corners = [ref.matrix_world @ Vector(c) for c in ref.bound_box]
        log("bounds:", [round(min(c[i] for c in corners), 3) for i in range(3)],
            [round(max(c[i] for c in corners), 3) for i in range(3)])
        for z in (0.3, 0.2, 0.1, 0.0, -0.1, -0.2):
            row = [front(surf, x, z) for x in (0.0, 0.05, 0.1, 0.15, 0.2, 0.3)]
            log("z", z, ["%.3f" % p.y if p else "-" for p in row])

        # Strips around the left eye (mirrored to the right one).
        retopo.set_tool(ctx, 'STRIP')
        ring = [front(surf, 0.14 + 0.075 * math.cos(a), 0.075 + 0.065 * math.sin(a))
                for a in [2 * math.pi * k / 120 for k in range(121)]]
        stroke(ctx, ring)
        # A strip down the center line, forehead to nose.
        stroke(ctx, [front(surf, 0.001, 0.22 - 0.4 * k / 80) for k in range(81)])
        # A strip across the cheek.
        stroke(ctx, [front(surf, 0.05 + 0.15 * k / 40, -0.03) for k in range(41)])
        obj = retopo.result(ctx)
        log("retopo:", obj.name if obj else None, len(obj.data.polygons) if obj else 0, "faces")
        # Relax the eye ring a little.
        retopo.set_tool(ctx, 'RELAX')
        tip = front(surf, 0.14, 0.14)
        h = retopo.pick(ctx, s, False, at(tip), at(tip), 1.0, 1.0)
        if h is not None:
            for _ in range(10):
                h.step(tip, 1.0, 1.0 / 90.0)
        # An open quad on the chin.
        retopo.set_tool(ctx, 'QUAD')
        for x, z in ((0.015, -0.19), (0.05, -0.19), (0.05, -0.15)):
            stroke(ctx, [front(surf, x, z)])
        log("open quad:", retopo.pen_count())

        # Panels: Strip, Quad (open quad), Relax + More, the Mesh picker.
        viewer = Vector((0.0, -2.0, 0.0))
        spots = ((-0.113, 0.006), (0.113, 0.006), (-0.113, -0.256), (0.113, -0.256))  # 2 x 2, bottom centers

        def m(i):
            x, z = spots[i]
            return vr_ui.panel_matrix(Vector((x, -1.2, z)), viewer + Vector((x, 0.0, z)), 1.0)
        s.retopo_tool = 'STRIP'
        snapshot(ctx, m(0), "retopo_tool_STRIP")
        s.retopo_tool = 'QUAD'
        snapshot(ctx, m(1), "retopo_end")
        s.retopo_tool = 'RELAX'
        menus.set_more('RETOPO', True)
        snapshot(ctx, m(2), "retopo_wrap")
        menus.set_more('RETOPO', False)
        menus.open_retopo_picker(ctx)
        snapshot(ctx, m(3))
        menus.close_picker()
        s.retopo_tool = 'QUAD'

        rv3d = space.region_3d
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0.0, -1.2, 0.0)
        rv3d.view_rotation = Quaternion((1, 0, 0), math.radians(90))
        rv3d.view_distance = 0.7
        ref.hide_set(True)
        obj.hide_set(True)
        HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW'))

        # The scene: main tip by the open quad's area, off tip over the cheek strip.
        main = front(surf, 0.022, -0.15, 0.006)
        off = front(surf, -0.12, -0.03, 0.004)
        scene_state.update(tips=(off, main), poses=(at(off + Vector((0, -0.1, 0))), at(main + Vector((0, -0.1, 0)))))
        bpy.app.timers.register(lambda: (shoot(win, area, region, "panels", ref, obj), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        done()


def shoot(win, area, region, name, ref, obj):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        png = os.path.join(ROOT, "tests", f"retopo_{name}.png")
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=png)
        log("saved", png)
        if name == "panels":
            ref.hide_set(False)
            obj.hide_set(False)
            scene_state["on"] = True
            rv3d = area.spaces.active.region_3d
            rv3d.view_location = (0.04, 0.0, 0.0)
            rv3d.view_rotation = Quaternion((0, 0, 1), math.radians(-12)) @ Quaternion((1, 0, 0), math.radians(80))
            rv3d.view_distance = 0.95
            bpy.app.timers.register(lambda: (shoot(win, area, region, "scene", ref, obj), None)[1], first_interval=0.5)
            return
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    quit_cleanly()


bpy.app.timers.register(lambda: (setup(), None)[1], first_interval=1.5)
