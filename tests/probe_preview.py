"""Windowed probe: are brush asset previews readable, and which shaders take 3D image quads?

blender --factory-startup --python tests/probe_preview.py
"""

import os
import traceback

import bpy
import gpu

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_preview_result.txt")
lines = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def main():
    try:
        win = bpy.context.window_manager.windows[0]
        area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
        region = next(r for r in area.regions if r.type == 'WINDOW')
        bpy.ops.mesh.primitive_ico_sphere_add()
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
            for name in ("Draw", "Clay Strips", "Smooth"):
                bpy.ops.brush.asset_activate(
                    asset_library_type='ESSENTIALS',
                    relative_asset_identifier="brushes/essentials_brushes-mesh_sculpt.blend/Brush/" + name)
                b = bpy.context.tool_settings.sculpt.brush
                p = b.preview
                if p is None:
                    log(name, "preview None")
                    continue
                log(name, "lib", b.library and b.library.name, "image", tuple(p.image_size), "icon", tuple(p.icon_size),
                    "len img px", len(p.image_pixels_float), "len icon px", len(p.icon_pixels_float),
                    "icon_id", p.icon_id)
        for sh in ("IMAGE", "IMAGE_COLOR", "IMAGE_SCENE_LINEAR_TO_REC709_SRGB", "UNIFORM_COLOR"):
            try:
                s = gpu.shader.from_builtin(sh)
                attrs = [(a.name, a.type) if hasattr(a, "name") else a for a in getattr(s, "attrs_info_get", lambda: [])()]
                log("shader", sh, "attrs", attrs)
            except Exception as ex:
                log("shader", sh, "ERR", ex)
    except Exception:
        log(traceback.format_exc())
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(lambda: (main(), None)[1], first_interval=1.5)
