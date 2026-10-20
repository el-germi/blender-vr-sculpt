import bpy

from . import actions, native, operators


class VIEW3D_PT_vr_sculpt(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "VR"
    bl_label = "VR Sculpt"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.vr_sculpt

        if actions.vr_addon() is None:
            layout.label(text="Enable the 'VR Scene Inspection' add-on", icon='ERROR')

        layout.prop(settings, "enabled")
        col = layout.column()
        col.active = settings.enabled
        col.prop(settings, "engine")
        is_native = settings.engine == 'NATIVE'
        if is_native:
            brush = native.sculpt_brush(context)
            col.label(text=f"Brush: {brush.name if brush else 'none'} (active sculpt brush)")
        else:
            col.prop(settings, "brush")
        col.prop(settings, "radius")
        if not is_native:
            col.prop(settings, "strength")
        col.prop(settings, "tip_offset")
        if not is_native:
            col.prop(settings, "symmetry_x")
            col.prop(settings, "grab_rotate")
        col.prop(settings, "left_inverts")
        col.prop(settings, "show_cursor")
        sub = col.column()
        sub.active = settings.show_cursor
        sub.prop(settings, "show_brush_card")

        obj = context.active_object
        box = layout.box()
        box.scale_y = 0.8
        if operators.sculpt_target(context) is None:
            mode = "Sculpt Mode" if is_native else "Object Mode"
            box.label(text=f"Select a mesh in {mode}", icon='INFO')
        else:
            box.label(text=f"{obj.name}: {len(obj.data.vertices):,} verts", icon='MESH_DATA')
        box.label(text=actions.last_status)
        s = operators.stats
        if s["dabs"]:
            box.label(text=f"Last {s['brush'].lower()} stroke: {s['dabs']} dabs")
            box.label(text=f"avg {s['avg_ms']:.1f} ms, max {s['max_ms']:.1f} ms (mesh update {s['write_ms']:.1f} ms)")


def register():
    bpy.utils.register_class(VIEW3D_PT_vr_sculpt)


def unregister():
    bpy.utils.unregister_class(VIEW3D_PT_vr_sculpt)
