"""Windowed check of Paint mode with simulated controllers: set up, Contact and Spray strokes,
second color, dab size, stroke undo, color picking, and a screenshot of the overlay.

blender --factory-startup test_scene.blend --python tests/paint_preview.py
Writes tests/paint_preview_result.txt and tests/paint_preview.png.
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
LOG = os.path.join(ROOT, "tests", "paint_preview_result.txt")
PNG = os.path.join(ROOT, "tests", "paint_preview.png")
lines = []
failures = []


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
        self.viewer_pose_location = Vector((0.0, -0.9, 1.45))
        self.viewer_pose_rotation = Quaternion((1, 0, 0), math.pi / 2)
        self.aim_loc = [Vector((-0.25, -0.6, 1.2)), Vector((0.2, -0.5, 1.3))]
        self.aim_rot = [Quaternion((1, 0, 0), 1.2), Quaternion((1, 0, 0), 1.45)]

    def controller_aim_location_get(self, context, i):
        return self.aim_loc[i]

    def controller_aim_rotation_get(self, context, i):
        return self.aim_rot[i]

    def controller_grip_location_get(self, context, i):
        return self.aim_loc[i] + Vector((0.0, -0.03, -0.03))

    def controller_grip_rotation_get(self, context, i):
        return self.aim_rot[i]


fake = FakeSessionState()
errors = []


def draw():
    from vr_sculpt import draw as vr_draw
    try:
        vr_draw.draw_overlay(bpy.context, fake, xr=True)
    except Exception:
        if not errors:
            log(traceback.format_exc())
        errors.append(1)


def pixels(img):
    px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(img.size[1], img.size[0], 4)


def pose_at(loc, look_dir):
    return Matrix.LocRotScale(Vector(loc), Vector(look_dir).to_track_quat('-Z', 'Y'), None)


def stroke(ctx, settings, obj, poses, left=False, value=1.0):
    """Run a PaintStroke through a list of aim poses (tip = aim pose moved by the tip offset)."""
    from vr_sculpt import operators, paint
    tip_off = settings.tip_offset
    s = None
    for i, pose in enumerate(poses):
        tip = pose.copy()
        tip.translation = operators.brush_tip(pose, tip_off)
        if s is None:
            s = paint.pick(ctx, settings, left, pose, tip, 1.0, value)
            if s is None:
                return None
        else:
            s.update(ctx, pose, tip, value)
    s.finish(ctx)
    return s


def run():
    import vr_sculpt
    from vr_sculpt import menus, native, paint, primitives
    try:
        vr_sculpt.register()
        ctx = bpy.context
        win = ctx.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        settings = ctx.scene.vr_sculpt
        settings.paint_texture_size = '1024'
        with ctx.temp_override(window=win, area=area, region=region):
            # 1. Set up a cube (no UVs, no material).
            menus.set_mode(bpy.context, 'MODEL')
            cube = primitives.create(bpy.context, 'CUBE', (0.6, 0.0, 1.0))
            check(len(cube.data.uv_layers) == 0 and not paint.ready(cube), "new cube isn't paintable yet")
            ok = paint.setup(bpy.context, cube, 1024)
            check(ok and len(cube.data.uv_layers) > 0 and paint.paint_image(cube).size[0] == 1024,
                  "Set up: UVs and a 1K image")
            check(cube.mode == 'TEXTURE_PAINT', f"and Texture Paint mode ({cube.mode})")
            menus.set_object_mode(bpy.context, cube, 'OBJECT')
            cube.hide_set(True)

            # 2. The ball: set up (has UVs, gets an image), then paint.
            ball = bpy.data.objects["SculptBall"]
            for o in bpy.context.view_layer.objects:
                if o is not None:
                    o.select_set(False)
            ball.select_set(True)
            bpy.context.view_layer.objects.active = ball
            paint.setup(bpy.context, ball, 1024)
            menus.set_mode(bpy.context, 'PAINT')
            check(ball.mode == 'TEXTURE_PAINT' and paint.brush(bpy.context) is not None,
                  f"Paint mode puts the ball in Texture Paint with a brush ({ball.mode})")
            paint.activate_brush(bpy.context, "Paint Hard")
            img = paint.paint_image(ball)
            base = pixels(img).copy()
            rv3d = area.spaces.active.region_3d
            view_before = (rv3d.view_location.copy(), rv3d.view_rotation.copy(), rv3d.view_distance)

            paint.set_color(bpy.context, (1.0, 0.1, 0.1))
            paint.set_color(bpy.context, (0.1, 0.2, 1.0), secondary=True)
            ip = bpy.context.tool_settings.image_paint
            b = ip.brush
            ups = ip.unified_paint_settings
            log("debug: brush", b.name, "lib", b.library, "is_editable", getattr(b, "is_editable", "?"),
                "brush.color", tuple(round(c, 2) for c in b.color), "ups.use_unified_color", ups.use_unified_color,
                "ups.color", tuple(round(c, 2) for c in ups.color), "owner", type(paint.color_owner(bpy.context)).__name__,
                "brush.blend", b.blend, "image_brush_type", b.image_brush_type)
            settings.paint_reach = 'CONTACT'
            settings.paint_radius = 0.015
            settings.tip_offset = 0.04
            center = ball.matrix_world.translation
            r = 0.25
            # Contact: the tip slides over the front, 3 mm above the surface, along the equator.
            poses = []
            for a in np.linspace(-0.5, 0.5, 30):
                n = Vector((math.sin(a), -math.cos(a), 0.0))
                tip = center + n * (r + 0.003)
                poses.append(pose_at(tip + n * settings.tip_offset, -n))
            t0 = time.perf_counter()
            s = stroke(bpy.context, settings, ball, poses)
            dt = (time.perf_counter() - t0) * 1000
            check(s is not None and s.dabs > 10, f"contact stroke painted {s.dabs if s else 0} dabs "
                  f"({dt / max(s.dabs, 1):.1f} ms per dab incl. overhead)")
            after = pixels(img)
            changed = np.any(np.abs(after[..., :3] - base[..., :3]) > 0.2, axis=2)
            check(changed.sum() > 200, f"{int(changed.sum())} texels painted")
            reds = after[changed][:, :3].mean(axis=0)
            check(reds[0] > 0.7 and reds[2] < 0.4, f"in the primary color ({np.round(reds, 2)})")
            # Where: the texel under the middle of the stroke is red.
            h = paint.surface_near(ball, center + Vector((0.0, -r, 0.0)), 0.01)
            uv = paint.uv_at(ball, h[0], h[2])
            x, y = int(uv.x * img.size[0]), int(uv.y * img.size[1])
            check(after[y, x, 0] > 0.7 and after[y, x, 2] < 0.4, f"the stroke passes where the tip was ({uv.x:.3f}, {uv.y:.3f})")
            # Size: walk across the stroke on the surface (up/down through its middle) and see how far
            # the painted texels reach.
            painted_dz = []
            for dz in np.linspace(-0.04, 0.04, 161):
                p = center + Vector((0.0, -r, dz)).normalized() * r
                hh = paint.surface_near(ball, p, 0.01)
                if hh is None:
                    continue
                uv2 = paint.uv_at(ball, hh[0], hh[2])
                tx, ty = int(uv2.x * img.size[0]) % img.size[0], int(uv2.y * img.size[1]) % img.size[1]
                if changed[ty, tx]:
                    painted_dz.append(dz)
            width_world = (max(painted_dz) - min(painted_dz)) if painted_dz else 0.0
            check(0.7 * 2 * settings.paint_radius < width_world < 1.4 * 2 * settings.paint_radius,
                  f"stroke is about the brush diameter ({width_world * 100:.1f} cm for {2 * settings.paint_radius * 100:.1f} cm)")
            view_after = (rv3d.view_location.copy(), rv3d.view_rotation.copy(), rv3d.view_distance)
            check((view_before[0] - view_after[0]).length < 1e-6 and
                  view_before[1].rotation_difference(view_after[1]).angle < 1e-5 and
                  abs(view_before[2] - view_after[2]) < 1e-6, "the desktop view is put back")

            # Stroke undo.
            check(paint.undo_last(bpy.context), "B / Undo stroke")
            check(np.allclose(pixels(img), base, atol=1e-3), "takes the whole stroke back")

            # Left hand: second color, at the top.
            poses = []
            for a in np.linspace(-0.4, 0.4, 20):
                n = Vector((math.sin(a) * 0.5, -0.5, 0.7)).normalized()
                tip = center + n * (r + 0.003)
                poses.append(pose_at(tip + n * settings.tip_offset, -n))
            stroke(bpy.context, settings, ball, poses, left=True)
            after = pixels(img)
            changed = np.any(np.abs(after[..., :3] - base[..., :3]) > 0.2, axis=2)
            blues = after[changed][:, :3].mean(axis=0) if changed.any() else np.zeros(3)
            check(changed.sum() > 100 and blues[2] > 0.7 and blues[0] < 0.4,
                  f"left hand paints the second color ({np.round(blues, 2)})")

            # Spray from 60 cm away.
            settings.paint_reach = 'SPRAY'
            paint.set_color(bpy.context, (0.1, 0.9, 0.1))
            before = pixels(img).copy()
            poses = []
            for a in np.linspace(-0.3, 0.3, 20):
                target = center + Vector((math.sin(a), -math.cos(a), -0.3)).normalized() * r
                origin = target + Vector((0.1, -0.6, 0.05))
                poses.append(pose_at(origin, target - origin))
            stroke(bpy.context, settings, ball, poses)
            after = pixels(img)
            changed = np.any(np.abs(after[..., :3] - before[..., :3]) > 0.2, axis=2)
            greens = after[changed][:, :3].mean(axis=0) if changed.any() else np.zeros(3)
            check(changed.sum() > 100 and greens[1] > 0.6, f"spray paints from a distance ({int(changed.sum())} texels)")

            # Pick color from the red-free green area.
            settings.paint_tool = 'PICK'
            paint.set_color(bpy.context, (0.0, 0.0, 0.0))
            mid = poses[len(poses) // 2]
            tip = mid.copy()
            stroke(bpy.context, settings, ball, [mid])
            check(paint.get_color(bpy.context)[1] > 0.6 and settings.paint_tool == 'PAINT',
                  f"Pick takes the sprayed green ({np.round(paint.get_color(bpy.context), 2)})")

            # Overlay screenshot: Contact cursor near the ball, panel up.
            settings.paint_reach = 'CONTACT'
            settings.show_panel = True
            paint.set_color(bpy.context, (1.0, 0.45, 0.1))
            n = Vector((0.3, -1.0, 0.2)).normalized()
            fake.aim_loc[1] = center + n * (r + 0.01 + settings.tip_offset)
            fake.aim_rot[1] = (-n).to_track_quat('-Z', 'Y')
            area.spaces.active.shading.type = 'SOLID'
            area.spaces.active.shading.color_type = 'TEXTURE'
            rv3d.view_perspective = 'PERSP'
            rv3d.view_location = (0.0, -0.2, 1.3)
            rv3d.view_rotation = Quaternion((1, 0, 0), 1.45)
            rv3d.view_distance = 1.3
        bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_VIEW')
        bpy.app.timers.register(lambda: (shoot(win, area, region), None)[1], first_interval=0.8)
    except Exception:
        log(traceback.format_exc())
        failures.append("raised")
        done()


def shoot(win, area, region):
    try:
        area.tag_redraw()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=PNG)
        log("saved", PNG, "draw errors:", len(errors))
        if errors:
            failures.append("draw errors")
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    log("RESULT:", "ALL PASSED" if not failures else f"FAILED {failures}")
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (run(), None)[1], first_interval=1.5)
