import bpy

from . import actions, catalog, measure, native, operators, paint, pose, primitives, puppet


class VIEW3D_PT_vr_sculpt(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "VR"
    bl_label = "VR Authoring"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.vr_sculpt

        if actions.vr_addon() is None:
            layout.label(text="Enable the 'VR Scene Inspection' add-on", icon='ERROR')

        layout.prop(settings, "enabled")
        row = layout.grid_flow(row_major=True, columns=3, even_columns=True, align=True)
        row.active = settings.enabled
        for mode, text in (('SCULPT', "Sculpt"), ('MODEL', "Model"), ('DRAW', "Draw"), ('PAINT', "Paint"),
                           ('POSE', "Pose"), ('TOOLS', "Tools"), ('HAND', "Hand")):
            row.operator("vrsculpt.set_mode", text=text, depress=settings.mode == mode).mode = mode
        layout.prop(settings, "show_panel")
        col = layout.column()
        col.active = settings.enabled
        if settings.mode == 'DRAW':
            col.prop(settings, "draw_tool")
            col.prop(settings, "draw_style")
            col.prop(settings, "draw_color")
            col.prop(settings, "draw_width")
            col.prop(settings, "draw_smoothing")
            col.prop(settings, "draw_thickness")
            if settings.draw_thickness == 'TAPER':
                col.prop(settings, "draw_taper")
            row = col.row(align=True, heading="Mirror")
            row.prop(settings, "mirror_x", text="X", toggle=True)
            row.prop(settings, "mirror_y", text="Y", toggle=True)
            row.prop(settings, "mirror_z", text="Z", toggle=True)
            col.prop(settings, "tip_offset")
            col.operator("vrsculpt.undo_stroke", icon='LOOP_BACK')
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        if settings.mode == 'PAINT':
            obj = paint.target(context)
            if obj is None:
                col.label(text="Select a mesh to paint", icon='INFO')
            elif not paint.ready(obj):
                col.label(text=f"{obj.name} needs UVs and an image", icon='INFO')
                col.prop(settings, "paint_texture_size")
                col.operator("vrsculpt.paint_setup", icon='TEXTURE')
            else:
                b = paint.brush(context)
                col.label(text=f"Brush: {b.name if b else 'none'} (active texture paint brush)")
                col.prop(settings, "paint_tool")
                col.prop(settings, "paint_reach")
                col.prop(settings, "paint_radius")
                owner = paint.color_owner(context)
                row = col.row(align=True)
                row.prop(owner, "color", text="")
                row.prop(owner, "secondary_color", text="")
                if b is not None:
                    col.prop(b, "strength")
            col.prop(settings, "tip_offset")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        if settings.mode == 'POSE':
            obj = pose.rig(context)
            if obj is None:
                col.label(text="No armature in Pose Mode: the trigger moves objects", icon='INFO')
            else:
                pb = pose.active_bone(obj)
                col.label(text=f"{obj.name}: {pb.name if pb else 'no active bone'}", icon='ARMATURE_DATA')
                col.prop(settings, "pose_tool", expand=True)
                sub = col.column()
                sub.active = settings.pose_tool == 'IK'
                sub.prop(settings, "pose_chain")
                col.prop(settings, "pose_mirror")
                col.prop(settings, "pose_show_bones")
            n = puppet.binding_count()
            col.label(text=f"Puppet bindings: {n}" if n else "Puppet: bind bones from the Pose tool's tag")
            col.prop(settings, "tip_offset")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        if settings.mode == 'TOOLS':
            col.prop(settings, "tool")
            if settings.tool == 'MEASURE':
                col.prop(settings, "show_measures")
                row = col.row(align=True)
                row.label(text=f"{len(measure.stored(context.scene))} measurements")
                row.operator("vrsculpt.clear_measures", text="Clear", icon='TRASH')
            else:
                col.prop(context.scene, "camera")
            col.prop(settings, "camera_preview")
            col.prop(settings, "tip_offset")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        if settings.mode == 'MODEL':
            row = col.row(align=True)
            row.label(text="Add: " + primitives.label(settings.primitive))
            row.operator("vrsculpt.choose_primitive", text="Choose", icon='VIEWZOOM')
            col.prop(settings, "tip_offset")
            col.prop(settings, "show_cursor")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        col.prop(settings, "engine")
        is_native = settings.engine == 'NATIVE'
        obj = context.active_object
        if is_native:
            brush = native.sculpt_brush(context)
            col.label(text=f"Brush: {brush.name if brush else 'none'} (active sculpt brush)")
        else:
            col.prop(settings, "brush")
        col.prop(settings, "radius")
        if not is_native:
            col.prop(settings, "strength")
        col.prop(settings, "tip_offset")
        row = col.row(align=True, heading="Symmetry")
        if is_native:
            if obj is not None and obj.type == 'MESH':
                for axis in "xyz":
                    row.prop(obj.data, "use_mirror_" + axis, text=axis.upper(), toggle=True)
        else:
            for axis in "xyz":
                row.prop(settings, "symmetry_" + axis, text=axis.upper(), toggle=True)
        if not is_native:
            col.prop(settings, "grab_rotate")
        col.prop(settings, "left_inverts")
        col.prop(settings, "show_cursor")
        sub = col.column()
        sub.active = settings.show_cursor
        sub.prop(settings, "show_brush_card")

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


class VIEW3D_PT_vr_extras(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "VR"
    bl_label = "VR Extras"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.vr_sculpt

        col = layout.column(heading="Animation")
        col.prop(settings, "show_timeline")
        col.prop(settings, "show_paths")
        col.prop(settings, "onion")
        sub = col.column()
        sub.active = settings.onion
        sub.prop(settings, "onion_mode")
        sub.prop(settings, "onion_count")
        row = sub.row()
        row.active = settings.onion_mode == 'FRAMES'
        row.prop(settings, "onion_step")

        col = layout.column(heading="Grabbing")
        col.prop(settings, "grab_deadzone", text="Threshold")
        col.prop(settings, "grab_delay", text="Delay")

        col = layout.column(heading="Grid")
        col.prop(settings, "grid_show", text="Show")
        col.prop(settings, "grid_snap", text="Snap")
        col.prop(settings, "grid_size", text="Size")

        col = layout.column(heading="Navigation")
        col.prop(settings, "nav_mode", text="Thumbsticks")
        col.prop(settings, "nav_speed")
        sub = col.column()
        sub.active = settings.nav_mode == 'WALK'
        sub.prop(settings, "nav_turn")

        col = layout.column(heading="Camera")
        col.prop(settings, "show_new_camera", text="New Camera Button")

        col = layout.column(heading="View")
        col.prop(settings, "show_desktop")
        sub = col.column()
        sub.active = settings.show_desktop
        sub.prop(settings, "desktop_size")
        sub.prop(settings, "desktop_interactive")
        sub.prop(settings, "desktop_keyboard")
        col.prop(context.window_manager.xr_session_settings, "show_passthrough", text="Passthrough")

        col = layout.column(heading="Tracking")
        col.prop(settings, "tracking", text="Objects")
        sub = col.column()
        sub.active = settings.tracking
        sub.prop(settings, "tracking_record", text="Record")


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
        if prim.kind == 'OPERATOR':
            layout.label(text=primitives.label(prim.op_key))
            layout.label(text="Edit it in VR: its handles and chips (More)", icon='INFO')
            return
        layout.label(text=prim.bl_rna.properties["kind"].enum_items[prim.kind].name)
        if prim.kind == 'CUBE':
            layout.prop(prim, "size")
            layout.prop(prim, "subdivisions")
        else:
            layout.prop(prim, "radius")
            layout.prop(prim, "height")
            layout.prop(prim, "segments")


_choice_items = []  # Blender needs the enum strings kept alive.


def _primitive_items(self, context):
    _choice_items[:] = [(e.key, f"{e.category} / {e.label}" if e.category else e.label, e.idname)
                        for e in catalog.entries(refresh=True)]
    return _choice_items


class VRSCULPT_OT_choose_primitive(bpy.types.Operator):
    """Choose what the trigger creates in Model mode (every Add > Mesh entry, including extensions)"""
    bl_idname = "vrsculpt.choose_primitive"
    bl_label = "Choose VR Primitive"
    bl_property = "entry"

    entry: bpy.props.EnumProperty(name="Primitive", items=_primitive_items)

    def execute(self, context):
        context.scene.vr_sculpt.primitive = self.entry
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}


classes = (VIEW3D_PT_vr_sculpt, VIEW3D_PT_vr_extras, VIEW3D_PT_vr_primitive, VRSCULPT_OT_choose_primitive)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
