import bpy

from . import (actions, catalog, controllers, measure, native, operators, paint, physics, pose, primitives, puppet,
               retopo, rig)


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
        for mode, text in (('SCULPT', "Sculpt"), ('RETOPO', "Retopo"), ('MODEL', "Model"), ('DRAW', "Draw"),
                           ('PAINT', "Paint"),
                           ('RIG', "Rig"), ('POSE', "Pose"), ('PHYSICS', "Physics"), ('TOOLS', "Tools")):
            row.operator("vrsculpt.set_mode", text=text, depress=settings.mode == mode).mode = mode
        row = layout.row(heading="Hand Panel")
        row.prop(settings, "show_panel", text="Show")
        row.prop(settings, "panel_pin", text="Pin")
        sub = layout.row()
        sub.active = settings.show_panel and not settings.panel_pin
        sub.prop(settings, "panel_reveal")
        col = layout.column()
        col.active = settings.enabled
        if settings.mode == 'RETOPO':
            retopo.desktop_panel(layout, col, context)
            return
        if settings.mode == 'DRAW':
            col.prop(settings, "draw_tool")
            col.prop(settings, "draw_style")
            col.prop(settings, "draw_color")
            col.prop(settings, "draw_width")
            row = col.row(heading="Smooth")
            row.prop(settings, "smooth_strokes", text="")
            sub = row.row(align=True)
            sub.active = settings.smooth_strokes
            sub.prop(settings, "smooth_amount")
            sub.prop(settings, "smooth_lag")
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
            col.label(text=f"Puppet bindings: {n}" if n else "Puppet: bind bones on the hand panel")
            col.prop(settings, "tip_offset")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        if settings.mode == 'RIG':
            arm, obj = rig.target(context), rig.mesh(context)
            col.label(text=f"Rig: {arm.name if arm else 'new (the first joint starts one)'}", icon='ARMATURE_DATA')
            col.prop(settings, "rig_mesh")
            col.prop(settings, "rig_tool", expand=True)
            if settings.rig_tool == 'BONES':
                col.prop(settings, "rig_mirror")
            else:
                if arm is not None and obj is not None and not rig.bound(obj, arm):
                    col.label(text=f"Bind {obj.name} on the hand panel to paint it", icon='INFO')
                col.label(text=f"Bone: {rig.active_bone(arm) or 'none'}")
                col.prop(settings, "weight_brush", expand=True)
                col.prop(settings, "weight_radius")
                col.prop(settings, "weight_strength")
                col.prop(settings, "weight_mirror")
                col.prop(settings, "weight_show")
            col.prop(settings, "tip_offset")
            box = layout.box()
            box.scale_y = 0.8
            box.label(text=actions.last_status)
            return
        if settings.mode == 'PHYSICS':
            obj = physics.selected(context)
            if obj is not None and obj.type == 'ARMATURE':
                pb = physics.active_bone(obj)
                if pb is None:
                    col.label(text="Touch a bone to give it a spring", icon='INFO')
                else:
                    col.label(text=f"{obj.name}: {pb.name}", icon='BONE_DATA')
                    sp = pb.vr_spring
                    col.prop(sp, "enabled")
                    sub = col.column()
                    sub.active = sp.enabled
                    sub.prop(sp, "stiffness")
                    sub.prop(sp, "damping")
                    sub.prop(sp, "gravity")
            elif obj is not None and obj.type == 'MESH':
                col.label(text=f"{obj.name}: {dict(physics.KINDS)[physics.kind(obj)]} "
                               "(settings in Properties > Physics)", icon='PHYSICS')
            else:
                col.label(text="Select an object or touch one in VR", icon='INFO')
            col.prop(settings, "physics_hands")
            sub = col.column()
            sub.active = settings.physics_hands
            sub.prop(settings, "physics_tool", expand=True)
            sub.prop(settings, "physics_force")
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
            elif settings.tool == 'LIGHT':
                col.prop(settings, "light_type")
                col.prop(settings, "light_color")
            elif settings.tool == 'REFS':
                col.prop(settings, "refs_folder")
                # The picture is chosen from the tiles in VR; here it's only named, not a second path.
                from . import refs
                col.label(text="Picture: " + (refs.label(settings.ref_image) or "choose one in VR"), icon='IMAGE_DATA')
                col.prop(settings, "ref_size")
                col.prop(settings, "refs_show")
            else:
                col.prop(context.scene, "camera")
            col.prop(context.window_manager.xr_session_settings.shading, "type", text="VR View")
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
        col.prop(settings, "nav_grab_turn")

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
        col.prop(settings, "left_handed")
        col.prop(settings, "own_controllers")
        sub = col.column()
        sub.active = settings.own_controllers
        sub.prop(settings, "controller_hints")
        row = sub.row(align=True)
        if controllers.fetching():
            row.label(text="Getting models...", icon='TIME')
        else:
            row.operator("vrsculpt.fetch_controllers", icon='IMPORT',
                         text="Update From Runtime" if controllers.fetched() else "Get From Runtime")
            if controllers.fetched():
                row.operator("vrsculpt.forget_controllers", text="", icon='X')
        message = controllers.fetch_message()
        if message:
            sub.label(text=message, icon='INFO')
        elif not controllers.fetched():
            sub.label(text="Drawing Meta's Quest Pro controllers")

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
            layout.label(text="Edit its settings from the VR hand panel", icon='INFO')
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


class VRSCULPT_OT_fetch_controllers(bpy.types.Operator):
    """Get your controllers' models from the OpenXR runtime (SteamVR) and draw them in VR instead of the \
Quest Pro ones. Wear the headset and hold both controllers: for a few seconds this becomes the running \
VR app, closing any other one (SteamVR Home). Not during a VR session"""
    bl_idname = "vrsculpt.fetch_controllers"
    bl_label = "Get Controller Models"

    @classmethod
    def poll(cls, context):
        return not controllers.fetching() and not operators.session_running(context)

    def execute(self, context):
        if not controllers.start_fetch():
            self.report({'ERROR'}, "Couldn't start the model fetch")
            return {'CANCELLED'}
        return {'FINISHED'}


class VRSCULPT_OT_forget_controllers(bpy.types.Operator):
    """Forget the runtime's controller models and draw the Quest Pro ones again"""
    bl_idname = "vrsculpt.forget_controllers"
    bl_label = "Forget Controller Models"

    @classmethod
    def poll(cls, context):
        return not controllers.fetching()

    def execute(self, context):
        controllers.forget_fetched()
        return {'FINISHED'}


classes = (VIEW3D_PT_vr_sculpt, VIEW3D_PT_vr_extras, VIEW3D_PT_vr_primitive, VRSCULPT_OT_choose_primitive,
           VRSCULPT_OT_fetch_controllers, VRSCULPT_OT_forget_controllers)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
