"""Probe: discover Add > Mesh entries (with installed extensions) and try each operator.

blender --background --factory-startup --python tests/probe_mesh_ops.py
"""

import os
import sys
import time
import traceback

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vr_sculpt import catalog  # noqa: E402

SKIP_PROPS = {"location", "rotation", "scale", "align", "enter_editmode"}


def main():
    import addon_utils
    # Run with --factory-startup: enabling here then never touches the saved preferences.
    # default_set=True because the extension's menu code reads its own preferences entry.
    mod = addon_utils.enable("bl_ext.blender_org.extra_mesh_objects", default_set=True)
    print("== Extra Mesh Objects enabled:", mod is not None)
    entries = catalog.discover()
    print(f"== {len(entries)} entries")
    for e in entries:
        mod, name = e.idname.split(".", 1)
        op = getattr(getattr(bpy.ops, mod), name)
        rna = op.get_rna_type()
        props = []
        for p in rna.properties:
            if p.identifier in SKIP_PROPS or p.identifier == "rna_type":
                continue
            extra = ""
            if p.type in ('FLOAT', 'INT'):
                extra = f" [{getattr(p, 'soft_min', '?')}..{getattr(p, 'soft_max', '?')}] len={p.array_length}"
            if p.type == 'ENUM':
                extra = f" {[i.identifier for i in p.enum_items][:6]}"
            flags = ("H" if p.is_hidden else "") + ("S" if p.is_skip_save else "")
            props.append(f"{p.identifier}:{p.type}/{p.subtype}/{getattr(p, 'unit', '')}{extra}{' ' + flags if flags else ''}")
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        t0 = time.perf_counter()
        try:
            kw = dict(e.preset)
            names = {p.identifier for p in rna.properties}
            if "align" in names:
                kw["align"] = 'WORLD'
            if "enter_editmode" in names:
                kw["enter_editmode"] = False
            res = op('EXEC_DEFAULT', **kw)
            ms = (time.perf_counter() - t0) * 1000
            obj = bpy.context.view_layer.objects.active
            info = f"{res} {ms:.1f} ms -> {obj.type if obj else None} {len(obj.data.vertices) if obj and obj.type == 'MESH' else ''} verts, {len(bpy.data.objects)} objects, dims {tuple(round(d, 2) for d in obj.dimensions) if obj else None}"
        except Exception as ex:
            info = f"ERROR {str(ex).splitlines()[0]}"
        print(f"-- {e.category or 'Mesh'} / {e.label} [{e.idname}] preset={e.preset}\n   {info}\n   props: {'; '.join(props)}")


try:
    main()
except Exception:
    traceback.print_exc()
