"""Windowed probe: what EEVEE costs a dyntopo sculpt, one sample per draw as the XR draw renders it. Writes
tests/probe_sculpt_lighting_result.txt, and tests/probe_sculpt_lighting_*.png (a frame of one eye).

blender --factory-startup test_scene.blend --python tests/probe_sculpt_lighting.py

GPUOffScreen.draw_view3d renders all of scene.eevee.taa_samples per call (is_image_render=True) while the XR draw
renders one per redraw, so taa_samples is 1 here. Two eyes of 1800 x 1900, moved a little each frame as a head would,
with a one-pixel read so the GPU has finished.
"""

import math
import os
import sys
import time
import traceback

import bpy
import gpu
from mathutils import Matrix, Quaternion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "probe_sculpt_lighting_result.txt")
W, H = 1800, 1900
lines = []
state = {"armed": False, "ms": None, "region": None, "frame": 0, "offs": [], "grab": None}
HANDLES = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def quit_cleanly():
    for h in HANDLES:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    HANDLES.clear()
    for off in state["offs"]:
        off.free()
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def stereo():
    if not state["armed"] or bpy.context.region != state["region"]:
        return
    ctx = bpy.context
    rv3d = ctx.region_data
    proj = rv3d.window_matrix.copy()
    proj[0][0] = proj[1][1] * H / W
    state["frame"] += 1
    head = Matrix.Translation((0.0008 * (state["frame"] % 40), 0.0, 0.0))
    t = time.perf_counter()
    for off, eye in zip(state["offs"], (-0.032, 0.032)):
        off.draw_view3d(ctx.scene, ctx.view_layer, ctx.space_data, ctx.region,
                        Matrix.Translation((eye, 0.0, 0.0)) @ head @ rv3d.view_matrix, proj,
                        do_color_management=True)
    with state["offs"][1].bind():
        fb = gpu.state.active_framebuffer_get()
        fb.read_color(0, 0, 1, 1, 4, 0, 'FLOAT')
        state["ms"] = (time.perf_counter() - t) * 1000.0
        if state["grab"]:
            buf = fb.read_color(0, 0, W, H, 4, 0, 'FLOAT')
            buf.dimensions = W * H * 4
            img = bpy.data.images.new("probe_grab", W, H)
            img.pixels.foreach_set(buf)
            img.filepath_raw = os.path.join(ROOT, "tests", f"probe_sculpt_lighting_{state['grab']}.png")
            img.file_format = 'PNG'
            img.save()
            bpy.data.images.remove(img)
            state["grab"] = None
    state["armed"] = False


def frame():
    state["armed"] = True
    state["ms"] = None
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    state["armed"] = False
    return state["ms"]


def measure(label, grab=None):
    ms = sorted(m for m in [frame() for _i in range(20)][4:] if m is not None)
    state["grab"] = grab
    if grab:
        frame()
    log(f"{label:44} median {ms[len(ms) // 2]:6.1f} ms  worst {ms[-1]:6.1f} ms" if ms else f"{label}: none")


def setup_scene(ctx, subdiv):
    from vr_sculpt import menus
    active = ctx.view_layer.objects.active
    if active is not None and active.mode != 'OBJECT':
        menus.set_object_mode(ctx, active, 'OBJECT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_plane_add(size=9.0, location=(0.0, 0.0, -1.05))
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=0.2, location=(-0.9, -1.35, 1.12), calc_uvs=False)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=subdiv, radius=1.0, calc_uvs=False)
    obj = ctx.active_object
    obj.data.polygons.foreach_set("use_smooth", [True] * len(obj.data.polygons))
    obj.data.update()
    data = bpy.data.lights.new("Key", 'POINT')
    data.energy = 300.0
    data.shadow_soft_size = 0.1
    key = bpy.data.objects.new("Key", data)
    ctx.scene.collection.objects.link(key)
    key.location = (-2.0, -3.0, 2.5)
    return obj, key


def run():
    from vr_sculpt import menus, native
    ctx = bpy.context
    win = ctx.window_manager.windows[0]
    area, region = native.largest_view3d(win)
    space = area.spaces.active
    state["region"] = region
    state["offs"] = [gpu.types.GPUOffScreen(W, H), gpu.types.GPUOffScreen(W, H)]
    HANDLES.append(bpy.types.SpaceView3D.draw_handler_add(stereo, (), 'WINDOW', 'POST_PIXEL'))
    ctx.scene.eevee.taa_samples = 1
    sh = space.shading
    sh.use_scene_lights = True
    sh.use_scene_world = False
    sh.studiolight_intensity = 0.25
    space.overlay.show_overlays = False
    rv3d = space.region_3d
    rv3d.view_perspective = 'PERSP'
    rv3d.view_location = (0.0, 0.0, 0.0)
    rv3d.view_rotation = Quaternion((1, 0, 0), math.pi / 2)
    rv3d.view_distance = 3.0
    log("gpu:", gpu.platform.renderer_get(), "|", gpu.platform.backend_type_get())
    for subdiv in (7, 8):
        obj, key = setup_scene(ctx, subdiv)
        sh.type = 'SOLID'
        menus.set_object_mode(ctx, obj, 'SCULPT')
        native.activate_brush(ctx, "Draw")
        native.toggle_dyntopo(ctx)
        n = len(obj.data.vertices) // 1000
        measure(f"{n}k Solid")
        sh.type = 'MATERIAL'
        for _i in range(4):
            frame()
            yield 0.6
        first = subdiv == 7
        measure(f"{n}k light + shadow", "shadow" if first else None)
        key.data.use_shadow = False
        yield 0.2
        measure(f"{n}k no shadow", "no_shadow" if first else None)
        receivers = bpy.data.collections.new("Receivers")
        receivers.objects.link(obj)
        key.light_linking.receiver_collection = receivers
        yield 0.2
        measure(f"{n}k no shadow, linked to the sculpt", "linked" if first else None)
        key.light_linking.receiver_collection = None
        bpy.data.collections.remove(receivers)


def start():
    try:
        import vr_sculpt
        vr_sculpt.register()
        gen = run()
    except Exception:
        log(traceback.format_exc())
        quit_cleanly()
        return None

    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            log(traceback.format_exc())
        quit_cleanly()
        return None

    bpy.app.timers.register(tick, first_interval=0.1)
    return None


bpy.app.timers.register(start, first_interval=1.5)
