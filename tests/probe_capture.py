"""Probe: can Python capture desktop regions (read_color in POST_PIXEL) and render the scene
camera offscreen (draw_view3d in POST_PIXEL)? Saves tests/probe_capture.png.

blender --factory-startup test_scene.blend --python tests/probe_capture.py
"""

import os
import time
import traceback

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "tests", "probe_capture_result.txt")
PNG = os.path.join(ROOT, "tests", "probe_capture.png")
lines = []
caps = {}  # region pointer -> (x, y, w, h, tex, ms, kind)
cam = {}
handles = []
show = {"on": False}


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def capture():
    region = bpy.context.region
    area = bpy.context.area
    if region is None or show["on"]:
        return
    try:
        fb = gpu.state.active_framebuffer_get()
        t = time.perf_counter()
        w, h = region.width, region.height
        try:
            buf = fb.read_color(0, 0, w, h, 4, 0, 'FLOAT')
            fmt = 'RGBA16F'
        except Exception as ex:
            log("UBYTE read failed", ex)
            buf = fb.read_color(0, 0, w, h, 4, 0, 'FLOAT')
            fmt = 'RGBA16F'
        buf.dimensions = w * h * 4
        tex = gpu.types.GPUTexture((w, h), format=fmt, data=buf)
        ms = (time.perf_counter() - t) * 1000
        key = region.as_pointer()
        if key not in caps:
            log(f"captured {area.type}/{region.type} {w}x{h} at {region.x},{region.y} fmt {fmt} in {ms:.1f} ms; "
                f"fb viewport {gpu.state.viewport_get()}")
        caps[key] = (region.x, region.y, w, h, tex, ms, f"{area.type}/{region.type}")
    except Exception:
        log(traceback.format_exc())


def render_camera():
    ctx = bpy.context
    if show["on"] or cam.get("done"):
        return
    scene = ctx.scene
    try:
        t = time.perf_counter()
        w, h = 320, 180
        off = cam.get("off") or gpu.types.GPUOffScreen(w, h)
        cam["off"] = off
        camera = scene.camera
        view = camera.matrix_world.inverted()
        proj = camera.calc_matrix_camera(ctx.evaluated_depsgraph_get(), x=w, y=h)
        off.draw_view3d(scene, ctx.view_layer, ctx.space_data, ctx.region, view, proj, do_color_management=True)
        cam["ms"] = (time.perf_counter() - t) * 1000
        cam["done"] = True
        log(f"camera offscreen rendered in {cam['ms']:.1f} ms")
    except Exception:
        log(traceback.format_exc())
        cam["done"] = True


def draw_mosaic():
    if not show["on"]:
        return
    region = bpy.context.region
    shader = gpu.shader.from_builtin('IMAGE')
    gpu.state.blend_set('ALPHA')
    # Whole-window mosaic scaled to 45% at the bottom left of this region.
    s = 0.45
    for x, y, w, h, tex, ms, kind in caps.values():
        if kind != "VIEW_3D/WINDOW":
            continue
        pts = [(x * s, y * s), ((x + w) * s, y * s), ((x + w) * s, (y + h) * s), (x * s, (y + h) * s)]
        pts = [(px - region.x * 0, py) for px, py in pts]
        batch = batch_for_shader(shader, 'TRI_FAN', {"pos": pts, "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        shader.uniform_sampler("image", tex)
        batch.draw(shader)
    if "off" in cam:
        x0, y0 = region.width - 340, region.height - 200
        pts = [(x0, y0), (x0 + 320, y0), (x0 + 320, y0 + 180), (x0, y0 + 180)]
        batch = batch_for_shader(shader, 'TRI_FAN', {"pos": pts, "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))})
        shader.uniform_sampler("image", cam["off"].texture_color)
        batch.draw(shader)
    gpu.state.blend_set('NONE')


def setup():
    try:
        for st in (bpy.types.SpaceView3D, bpy.types.SpaceOutliner, bpy.types.SpaceProperties,
                   bpy.types.SpaceDopeSheetEditor, bpy.types.SpaceTextEditor, bpy.types.SpaceNodeEditor):
            for rt in ('WINDOW', 'HEADER', 'UI', 'TOOLS', 'NAVIGATION_BAR', 'TOOL_HEADER'):
                try:
                    handles.append((st, rt, st.draw_handler_add(capture, (), rt, 'POST_PIXEL')))
                except Exception as ex:
                    pass
        handles.append((bpy.types.SpaceView3D, 'WINDOW',
                        bpy.types.SpaceView3D.draw_handler_add(render_camera, (), 'WINDOW', 'POST_PIXEL')))
        handles.append((bpy.types.SpaceView3D, 'WINDOW',
                        bpy.types.SpaceView3D.draw_handler_add(draw_mosaic, (), 'WINDOW', 'POST_PIXEL')))
        log("handlers:", len(handles), "backend:", gpu.platform.backend_type_get())
        for a in bpy.context.window_manager.windows[0].screen.areas:
            a.tag_redraw()
        bpy.app.timers.register(phase2, first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def phase2():
    try:
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        log("captured regions:", len(caps), "avg ms:",
            f"{sum(c[5] for c in caps.values()) / max(len(caps), 1):.2f}")
        show["on"] = True
        for a in bpy.context.window_manager.windows[0].screen.areas:
            a.tag_redraw()
        bpy.app.timers.register(phase3, first_interval=0.5)
    except Exception:
        log(traceback.format_exc())
        done()


def phase3():
    try:
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        bpy.ops.screen.screenshot(filepath=PNG)
        log("saved", PNG)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(setup, first_interval=1.5)
