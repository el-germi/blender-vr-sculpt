import bpy

from . import actions, native, operators, primitives


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
        row = layout.row(align=True)
        row.active = settings.enabled
        for mode, text in (('SCULPT', "Sculpt"), ('MODEL', "Model")):
            row.operator("vrsculpt.set_mode", text=text, depress=settings.mode == mode).mode = mode
        layout.prop(settings, "show_panel")
        col = layout.column()
        col.active = settings.enabled
        if settings.mode == 'MODEL':
            col.prop(settings, "primitive")
            col.prop(settings, "tip_offset")
            col.prop(settings, "show_cursor")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
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


class VIEW3D_PT_vr_primitive(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "VR"
    bl_label = "VR Primitive"

    @classmethod
    def poll(cls, context):
        return primitives.is_editable(context.active_object)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        prim = context.active_object.vr_prim
        layout.label(text=prim.bl_rna.properties["kind"].enum_items[prim.kind].name)
        if prim.kind == 'CUBE':
            layout.prop(prim, "size")
            layout.prop(prim, "subdivisions")
        else:
            layout.prop(prim, "radius")
            layout.prop(prim, "height")
            layout.prop(prim, "segments")


classes = (VIEW3D_PT_vr_sculpt, VIEW3D_PT_vr_primitive)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
