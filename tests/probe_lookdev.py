"""Probe the look dev API: the XR session's shading, studio lights (matcaps, HDRIs), lights,
Principled BSDF materials and worlds. Background, prints what it finds.

blender --background --factory-startup --python tests/probe_lookdev.py
"""

import os
import time

import bpy


def section(name):
    print("\n==", name)


def rna_props(owner, filt=None):
    out = []
    for p in owner.bl_rna.properties:
        if p.identifier == "rna_type" or (filt and not filt(p)):
            continue
        try:
            v = getattr(owner, p.identifier)
        except Exception as ex:  # noqa: BLE001
            v = f"<{ex}>"
        if hasattr(v, "__len__") and not isinstance(v, str) and p.type in {'FLOAT', 'INT', 'BOOLEAN'}:
            v = tuple(v)
        out.append((p.identifier, p.type, v))
    return out


def enum_items(owner, prop):
    try:
        return [i.identifier for i in owner.bl_rna.properties[prop].enum_items]
    except Exception as ex:  # noqa: BLE001
        return f"<{ex}>"


def main():
    ctx = bpy.context
    xs = ctx.window_manager.xr_session_settings

    section("XrSessionSettings")
    for ident, kind, v in rna_props(xs):
        if kind != 'POINTER':
            print(f"  {ident} ({kind}) = {v}")
        else:
            print(f"  {ident} -> {type(v).__name__}")

    section("XR shading (View3DShading) defaults")
    sh = xs.shading
    for ident, kind, v in rna_props(sh):
        print(f"  {ident} ({kind}) = {v!r}")
    print("  type items:", enum_items(sh, "type"))
    print("  light items:", enum_items(sh, "light"))
    print("  color_type items:", enum_items(sh, "color_type"))

    section("studio_light items (static enum_items on the XR shading)")
    for t, light in (('SOLID', 'STUDIO'), ('SOLID', 'MATCAP'), ('MATERIAL', 'STUDIO'), ('RENDERED', 'STUDIO')):
        try:
            sh.type = t
            sh.light = light
            items = enum_items(sh, "studio_light")
            print(f"  {t}/{light}: current {sh.studio_light!r}, {len(items) if isinstance(items, list) else items}"
                  f" items: {items if not isinstance(items, list) else items[:40]}")
        except Exception as ex:  # noqa: BLE001
            print(f"  {t}/{light}: {ex}")

    section("setting studio_light per type")
    sh.type, sh.light = 'SOLID', 'MATCAP'
    try:
        sh.studio_light = 'metal_bronze.exr'
        print("  matcap set:", sh.studio_light)
    except Exception as ex:  # noqa: BLE001
        print("  matcap set failed:", ex)
    sh.type = 'MATERIAL'
    print("  after type MATERIAL, studio_light:", sh.studio_light)
    try:
        sh.studio_light = 'forest.exr'
        print("  world set:", sh.studio_light)
    except Exception as ex:  # noqa: BLE001
        print("  world set failed:", ex)
    sh.type, sh.light = 'SOLID', 'MATCAP'
    print("  back to SOLID/MATCAP, studio_light:", sh.studio_light)
    sh.light = 'STUDIO'
    print("  SOLID/STUDIO, studio_light:", sh.studio_light)
    try:
        sh.studio_light = 'rim.sl'
        print("  studio set:", sh.studio_light)
    except Exception as ex:  # noqa: BLE001
        print("  studio set failed:", ex)
    sh.type = 'RENDERED'
    print("  RENDERED studio_light:", sh.studio_light)
    sh.type = 'MATERIAL'
    print("  MATERIAL studio_light:", sh.studio_light)
    try:
        sh.studio_light = 'no_such.exr'
    except Exception as ex:  # noqa: BLE001
        print("  unknown name:", type(ex).__name__, ex)
    for prop in ("studiolight_rotate_z", "studiolight_intensity", "studiolight_background_alpha",
                 "studiolight_background_blur"):
        p = sh.bl_rna.properties.get(prop)
        if p is not None:
            print(f"  {prop}: default {p.default}, range [{p.hard_min}, {p.hard_max}] soft [{p.soft_min}, {p.soft_max}]"
                  f" subtype {p.subtype}")
    sh.type = 'SOLID'

    section("preferences.studio_lights")
    sls = ctx.preferences.studio_lights
    print("  count:", len(sls))
    first = sls[0]
    print("  attributes:", [p.identifier for p in first.bl_rna.properties])
    for sl in sls:
        print(f"  {sl.type:7} {sl.name!r:32} index {sl.index} user {sl.is_user_defined} "
              f"path {os.path.basename(sl.path)}")

    section("light data")
    print("  type items:", [i.identifier for i in bpy.types.Light.bl_rna.properties["type"].enum_items])
    for t in ('POINT', 'SUN', 'SPOT', 'AREA'):
        d = bpy.data.lights.new("probe_" + t, t)
        props = {i: v for i, _k, v in rna_props(d, lambda p: p.type in {'FLOAT', 'INT', 'BOOLEAN', 'ENUM'})}
        keep = {k: props[k] for k in ("energy", "color", "shadow_soft_size", "size", "size_y", "shape", "spot_size",
                                      "spot_blend", "angle", "temperature", "use_temperature", "use_shadow", "exposure",
                                      "normalize", "use_soft_falloff", "show_cone") if k in props}
        print(f"  {t} ({type(d).__name__}): {keep}")
        bpy.data.lights.remove(d)
    p = bpy.types.Light.bl_rna.properties.get("temperature")
    if p is not None:
        print(f"  temperature: default {p.default}, range [{p.hard_min}, {p.hard_max}]")
    d = bpy.data.lights.new("probe", 'POINT')
    print("  change type in place:", end=" ")
    d.type = 'SPOT'
    print(type(d).__name__, "->", type(bpy.data.lights["probe"]).__name__, "spot_size:",
          getattr(bpy.data.lights["probe"], "spot_size", None))
    bpy.data.lights.remove(bpy.data.lights["probe"])

    section("material")
    m = bpy.data.materials.new("Probe")
    print("  use_nodes:", getattr(m, "use_nodes", "<none>"), "node_tree:", m.node_tree)
    if m.node_tree is None:
        try:
            m.use_nodes = True
        except Exception as ex:  # noqa: BLE001
            print("  use_nodes = True:", ex)
    nt = m.node_tree
    print("  nodes:", [(n.bl_idname, n.name) for n in nt.nodes] if nt else None)
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None) if nt else None
    if bsdf is not None:
        print("  principled inputs:", [(i.name, i.identifier, i.type) for i in bsdf.inputs])
    for ident, kind, v in rna_props(m, lambda p: p.type in {'FLOAT', 'BOOLEAN', 'ENUM', 'INT'}):
        print(f"  {ident} ({kind}) = {v!r}")
    print("  surface_render_method items:", enum_items(m, "surface_render_method"))

    section("world")
    w = bpy.data.worlds.new("ProbeWorld")
    print("  use_nodes:", getattr(w, "use_nodes", "<none>"), "node_tree:", w.node_tree)
    if w.node_tree is None:
        try:
            w.use_nodes = True
        except Exception as ex:  # noqa: BLE001
            print("  use_nodes = True:", ex)
    print("  nodes:", [(n.bl_idname, n.name) for n in w.node_tree.nodes] if w.node_tree else None)
    if w.node_tree:
        env = w.node_tree.nodes.new("ShaderNodeTexEnvironment")
        mapping = w.node_tree.nodes.new("ShaderNodeMapping")
        print("  env props:", [p.identifier for p in env.bl_rna.properties if not p.is_readonly][:40])
        print("  mapping inputs:", [(i.name, i.type) for i in mapping.inputs], "vector_type", mapping.vector_type)
    print("  scene.world:", ctx.scene.world.name if ctx.scene.world else None)

    section("render engine")
    print("  engine:", ctx.scene.render.engine, "items:", enum_items(ctx.scene.render, "engine"))

    section("reading studio light images")
    world_sl = [sl for sl in sls if sl.type == 'WORLD']
    matcap_sl = [sl for sl in sls if sl.type == 'MATCAP']
    import numpy as np
    for sl in world_sl[:2] + matcap_sl[:2]:
        # Through bpy.data: makes an image datablock, and its pixels come slowly (or not at all after scale()).
        t0 = time.perf_counter()
        img = bpy.data.images.load(sl.path, check_existing=False)
        size = tuple(img.size)
        n = len(img.pixels)
        t1 = time.perf_counter()
        bpy.data.images.remove(img)
        # With OpenImageIO (bundled): no datablock, and every channel by name.
        import OpenImageIO as oiio
        t2 = time.perf_counter()
        buf = oiio.ImageBuf(sl.path)
        px = buf.get_pixels(oiio.FLOAT)
        t3 = time.perf_counter()
        print(f"  {sl.name}: bpy {size} {n} floats in {1000 * (t1 - t0):.1f} ms; OIIO {px.shape} "
              f"{list(buf.spec().channelnames)} in {1000 * (t3 - t2):.1f} ms, max {float(np.max(px)):.2f}")

    section("the solid lights of a studio light")
    for sl in sls:
        if sl.type == 'STUDIO':
            print(f"  {sl.name}: ambient {tuple(sl.light_ambient)}, " +
                  ", ".join(f"dir {tuple(round(c, 2) for c in li.direction)} smooth {li.smooth:.2f}"
                            for li in sl.solid_lights if li.use))

    section("light temperature")
    d = bpy.data.lights.new("t", 'POINT')
    d.use_temperature, d.temperature = True, 3000.0
    print("  color", tuple(d.color), "temperature_color", tuple(d.temperature_color))
    bpy.data.lights.remove(d)

    section("material users")
    me = bpy.data.meshes.new("m")
    mat = bpy.data.materials.new("mat")
    me.materials.append(mat)
    a = bpy.data.objects.new("a", me)
    bpy.data.objects.new("b", me)
    print("  a mesh two objects share: mesh users", me.users, "material users", mat.users)
    a.material_slots[0].link = 'OBJECT'
    print("  an object-linked slot starts empty:", a.material_slots[0].material)

main()
