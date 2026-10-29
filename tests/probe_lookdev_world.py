"""Windowed probe: does a World built from a studio HDRI (Environment Texture, Mapping rotation, Background
strength) look like Material Preview's own HDRI at the same rotation and intensity? Renders a mirror ball
with the desktop view in Material Preview, first lit by the studio light, then by the scene world at a few
rotation conventions, and compares the screenshots. Writes tests/probe_lookdev_world_result.txt.

blender --factory-startup test_scene.blend --python tests/probe_lookdev_world.py
"""

import math
import os
import sys

import bpy
import numpy as np
from mathutils import Quaternion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LOG = os.path.join(ROOT, "tests", "probe_lookdev_world_result.txt")
HDRI = "city.exr"
ROT = 0.8  # radians
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


def largest_view():
    win = bpy.context.window_manager.windows[0]
    best = None
    for area in win.screen.areas:
        if area.type == 'VIEW_3D':
            region = next(r for r in area.regions if r.type == 'WINDOW')
            if best is None or region.width * region.height > best[1].width * best[1].height:
                best = (area, region)
    return win, best[0], best[1]


def build_world(rotation, strength=1.0):
    sl = next(s for s in bpy.context.preferences.studio_lights if s.name == HDRI)
    world = bpy.data.worlds.get("ProbeWorld") or bpy.data.worlds.new("ProbeWorld")
    nt = world.node_tree
    nt.nodes.clear()
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(sl.path, check_existing=True)
    bg = nt.nodes.new("ShaderNodeBackground")
    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, rotation)
    bg.inputs["Strength"].default_value = strength
    bpy.context.scene.world = world


def setup():
    win, area, region = largest_view()
    space = area.spaces.active
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.3, location=(0, 0, 0))
    ball = bpy.context.active_object
    bpy.ops.object.shade_smooth()
    mat = bpy.data.materials.new("Mirror")
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.0
    bsdf.inputs["Base Color"].default_value = (1, 1, 1, 1)
    ball.data.materials.append(mat)
    ball.select_set(False)
    ov = space.overlay
    ov.show_overlays = False
    rv3d = space.region_3d
    rv3d.view_perspective = 'PERSP'
    rv3d.view_location = (0, 0, 0)
    rv3d.view_rotation = Quaternion((0, 0, 1), math.radians(30)) @ Quaternion((1, 0, 0), math.radians(75))
    rv3d.view_distance = 1.6
    sh = space.shading
    sh.type = 'MATERIAL'
    sh.use_scene_world = False
    sh.use_scene_lights = False
    sh.studio_light = HDRI
    sh.studiolight_rotate_z = ROT
    sh.studiolight_intensity = 1.0
    sh.studiolight_background_alpha = 1.0
    sh.studiolight_background_blur = 0.0
    return win, area, region, space


SHOTS = [("studio", None)]
for sign in (1, -1):
    for off in (0.0, math.pi / 2, math.pi, -math.pi / 2):
        SHOTS.append((f"world s{sign:+d} off{math.degrees(off):+.0f}", sign * ROT + off))
SHOTS.append(("world strength 2", "S2"))
SHOTS.append(("studio intensity 2", "I2"))


def shoot_path(i):
    return os.path.join(ROOT, "tests", f"probe_lookdev_world_{i}.png")


def run(state, i):
    space, area = state["space"], state["area"]
    name, rot = SHOTS[i]
    sh = space.shading
    if rot is None:
        sh.use_scene_world = False
    elif rot == "S2":
        build_world(state["best"][0], 2.0)
        sh.use_scene_world = True
    elif rot == "I2":
        sh.use_scene_world = False
        sh.studiolight_intensity = 2.0
    else:
        build_world(rot)
        sh.use_scene_world = True
    area.tag_redraw()
    bpy.app.timers.register(lambda: (shoot(state, i), None)[1], first_interval=4.0 if i == 0 else 2.5)


def load(path):
    import OpenImageIO as oiio
    return oiio.ImageBuf(path).get_pixels(oiio.FLOAT)[..., :3]


def shoot(state, i):
    try:
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=3)
        with bpy.context.temp_override(window=state["win"], area=state["area"], region=state["region"]):
            bpy.ops.screen.screenshot_area(filepath=shoot_path(i))
        name, rot = SHOTS[i]
        img = load(shoot_path(i))
        if i == 0:
            state["ref"] = img
            log(f"{name}: mean {img.mean():.4f}")
        else:
            ref = state["ref"]
            h, w = min(ref.shape[0], img.shape[0]), min(ref.shape[1], img.shape[1])
            diff = float(np.abs(ref[:h, :w] - img[:h, :w]).mean())
            log(f"{name} (rotation {rot if isinstance(rot, str) else round(rot, 3)}): mean {img.mean():.4f}, "
                f"diff from studio {diff:.4f}")
            if not isinstance(rot, str) and (state.get("best") is None or diff < state["best"][1]):
                state["best"] = (rot, diff, name)
    except Exception:
        import traceback
        log(traceback.format_exc())
    if i + 1 < len(SHOTS):
        run(state, i + 1)
    else:
        log("best match:", state["best"][2], "diff", round(state["best"][1], 4))
        done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


def start():
    try:
        win, area, region, space = setup()
        run({"win": win, "area": area, "region": region, "space": space}, 0)
    except Exception:
        import traceback
        log(traceback.format_exc())
        done()


bpy.app.timers.register(lambda: (start(), None)[1], first_interval=1.5)
