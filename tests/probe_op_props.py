"""Print operator properties for a few add-mesh operators, one per line.

blender --background --factory-startup --python tests/probe_op_props.py
"""

import addon_utils
import bpy

addon_utils.enable("bl_ext.blender_org.extra_mesh_objects", default_set=True)
for idname in ("mesh.primitive_gear", "mesh.primitive_round_cube_add", "mesh.primitive_torus_add",
               "mesh.primitive_uv_sphere_add", "mesh.add_mesh_rock", "mesh.primitive_teapot_add"):
    mod, name = idname.split(".")
    rna = getattr(getattr(bpy.ops, mod), name).get_rna_type()
    print("==", idname)
    for p in rna.properties:
        if p.identifier == "rna_type":
            continue
        rng = ""
        if p.type in ('FLOAT', 'INT'):
            rng = f" hard[{p.hard_min:.3g}..{p.hard_max:.3g}] soft[{p.soft_min:.3g}..{p.soft_max:.3g}] default={p.default if p.array_length == 0 else list(p.default_array)} len={p.array_length}"
        elif p.type == 'ENUM':
            rng = f" {[i.identifier for i in p.enum_items][:8]} default={p.default}"
        elif p.type == 'BOOLEAN':
            rng = f" default={p.default}"
        flags = ",".join(f for f, on in (("hidden", p.is_hidden), ("skip_save", p.is_skip_save)) if on)
        print(f"   {p.identifier:24} {p.type:8} {p.subtype:10} {getattr(p, 'unit', ''):7} '{p.name}'{rng} {flags}")
