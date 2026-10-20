import bpy


def props(t):
    rna = t.bl_rna
    out = []
    for p in rna.properties:
        if p.identifier == "rna_type":
            continue
        extra = ""
        if p.type == 'ENUM':
            extra = " " + str([i.identifier for i in p.enum_items])
        out.append(f"    {p.identifier}: {p.type}{extra}")
    for f in rna.functions:
        params = ", ".join(f"{q.identifier}:{q.type}" for q in f.parameters)
        out.append(f"    def {f.identifier}({params})")
    return "\n".join(out)


for name in ("XrSessionState", "XrActionMaps", "XrActionMap", "XrActionMapItems", "XrActionMapItem",
             "XrActionMapBindings", "XrActionMapBinding", "XrComponentPaths", "XrUserPaths",
             "XrSessionSettings", "XrEventData"):
    t = getattr(bpy.types, name, None)
    print(f"== {name}: {'MISSING' if t is None else ''}")
    if t:
        print(props(t))

print("== XR handlers:", [h for h in dir(bpy.app.handlers) if "xr" in h])
print("== openxr build:", bpy.app.build_options.xr_openxr)
try:
    h = bpy.types.SpaceView3D.draw_handler_add(lambda: None, (), 'XR', 'POST_VIEW')
    bpy.types.SpaceView3D.draw_handler_remove(h, 'XR')
    print("== XR draw handler: OK")
except Exception as e:
    print("== XR draw handler error:", e)

import gpu
print("== shaders:", [s for s in ("UNIFORM_COLOR", "POLYLINE_UNIFORM_COLOR", "SMOOTH_COLOR", "POLYLINE_SMOOTH_COLOR")])
m = bpy.data.meshes.new("t")
print("== mesh attrs:", [a.name for a in m.attributes], hasattr(m, "vertex_normals"), hasattr(m, "edges"))
print("== OperatorStrokeElement:")
print(props(bpy.types.OperatorStrokeElement))
print("== addon module:", bpy.utils.script_paths())
