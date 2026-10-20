import time

import bpy
from bpy.props import BoolProperty, EnumProperty
from mathutils import Matrix, Quaternion, Vector

from . import (actions, brushes, camera, catalog, desktop, drawing, interactions, measure, menus, native,
               paint, primitives, strokes, symmetry, vr_ui)
from .props import BRUSH_ITEMS, RADIUS_STEPS

# Shared geometry cache, so both hands sculpt the same arrays.
_state = None

# Last stroke numbers, shown in the panel.
stats = {"verts": 0, "dabs": 0, "avg_ms": 0.0, "max_ms": 0.0, "write_ms": 0.0, "brush": ""}

# Controller tips being drawn as "active" (stroking) per hand index.
active_hands = set()


def get_state(obj):
    global _state
    # Reuse only while another hand is mid-stroke. Otherwise re-read the mesh, since
    # undo or other tools may have changed it since the last stroke.
    if not active_hands or _state is None or not _state.is_valid_for(obj):
        _state = brushes.MeshState(obj)
    return _state


def drop_state():
    global _state
    _state = None


def controller_pose(loc, rot):
    m = Quaternion(rot).to_matrix().to_4x4()
    m.translation = Vector(loc)
    return m


def brush_tip(pose, offset):
    # OpenXR aim poses point down local -Z.
    return pose @ Vector((0.0, 0.0, -offset))


def viewer_scale(context):
    ss = context.window_manager.xr_session_state
    return ss.viewer_scale if ss else 1.0


def haptic(context, user_path, amplitude, duration=0.015):
    ss = context.window_manager.xr_session_state
    if ss:
        ss.haptic_action_apply(context, actions.ACTIONMAP, actions.ACTION_HAPTIC, user_path,
                               duration, 0.0, min(max(amplitude, 0.0), 1.0))


def session_running(context):
    return bpy.types.XrSessionState.is_running(context)


def required_mode(context):
    return 'SCULPT' if context.scene.vr_sculpt.engine == 'NATIVE' else 'OBJECT'


def sculpt_target(context):
    obj = context.active_object
    if obj and obj.type == 'MESH' and obj.mode == required_mode(context) and obj.visible_get():
        return obj
    return None


def _tip_engaged(context, settings, tip, scale):
    """Whether the tip is at work on something, so a desktop screen behind it doesn't take the trigger."""
    if settings.mode == 'SCULPT':
        return primitives.touching(context, tip, settings.radius * scale) is not None
    if settings.mode == 'MODEL':
        return primitives.touching(context, tip, interactions.TOUCH_MARGIN * scale) is not None
    if settings.mode == 'DRAW' and settings.draw_tool in {'EDIT', 'ERASE'}:
        return strokes.nearest(context, tip, drawing.EDIT_RADIUS * scale) is not None
    if settings.mode == 'PAINT':
        obj = paint.target(context)
        return obj is not None and settings.paint_reach == 'CONTACT' and \
            paint.surface_near(obj, tip, settings.paint_radius * scale) is not None
    return False


def _pick_handler(context, settings, hand, pose, tip_pose, scale, value):
    """Panel press, grab, create or draw for this trigger press; None means sculpt (or nothing)."""
    ss = context.window_manager.xr_session_state
    if settings.show_panel and hand == actions.HANDS[1] and ss is not None:
        widgets, hit, _m, _h = interactions.panel_pointer(context, ss, pose)
        if hit is not None:
            # A press anywhere on the panel belongs to the panel, even between widgets.
            return interactions.UIPress(context, ss, vr_ui.hit_test(widgets, *hit), hit)
    if settings.show_desktop and ss is not None and not _tip_engaged(context, settings, tip_pose.translation, scale):
        handler = desktop.pick(context, ss, hand, pose)
        if handler is not None:
            return handler
    if settings.mode == 'MODEL':
        obj = primitives.touching(context, tip_pose.translation, interactions.TOUCH_MARGIN * scale)
        if obj is not None:
            return interactions.MoveObject(context, obj, tip_pose)
        return interactions.CreatePrimitive(context, settings.primitive, tip_pose.translation, scale)
    if settings.mode == 'DRAW':
        return drawing.pick(context, settings, tip_pose, value, scale)
    if settings.mode == 'PAINT':
        return paint.pick(context, settings, hand == actions.HANDS[0], pose, tip_pose, scale, value)
    if settings.mode == 'TOOLS':
        if settings.tool == 'CAMERA':
            return camera.AimCamera(context, pose)
        return measure.Measure(context, settings, tip_pose.translation, scale)
    return None


class VRSCULPT_OT_stroke(bpy.types.Operator):
    """Sculpt, model or use the hand panel with a VR controller while the trigger is held"""
    bl_idname = "vrsculpt.stroke"
    bl_label = "VR Trigger"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return session_running(context) and context.scene.vr_sculpt.enabled

    def invoke(self, context, event):
        xr = event.xr
        if xr is None:
            return {'CANCELLED'}
        settings = context.scene.vr_sculpt
        self.hand = xr.user_path
        self.hand_idx = actions.hand_index(self.hand)
        self.threshold = xr.float_threshold
        self.timer = None
        self.stroke = None

        scale = viewer_scale(context)
        self.offset = settings.tip_offset * scale
        pose = controller_pose(xr.controller_location, xr.controller_rotation)
        tip_pose = pose.copy()
        tip_pose.translation = brush_tip(pose, self.offset)
        try:
            self.handler = _pick_handler(context, settings, self.hand, pose, tip_pose, scale, xr.state[0])
        except Exception as ex:
            # E.g. an extension's operator failing to create its primitive.
            import traceback
            traceback.print_exc()
            self.report({'WARNING'}, f"VR Sculpt: {ex}")
            return {'CANCELLED'}
        if self.handler is not None:
            haptic(context, self.hand, 0.5, 0.03)
            return self._start_modal(context)
        if settings.mode != 'SCULPT':
            return {'CANCELLED'}  # E.g. the Edit tool with no stroke in reach.

        obj = sculpt_target(context)
        if obj is None:
            return {'CANCELLED'}
        # Sculpting turns an editable primitive or stroke into a plain mesh.
        primitives.forget(obj)
        strokes.forget(obj)
        left = self.hand == actions.HANDS[0]
        if settings.engine == 'NATIVE':
            try:
                self.stroke = native.NativeStroke(context, obj, settings.radius * scale,
                                                  invert=settings.left_inverts and left)
            except RuntimeError as ex:
                self.report({'WARNING'}, str(ex))
                return {'CANCELLED'}
        else:
            invert = settings.left_inverts and left and settings.brush in {'DRAW', 'INFLATE'}
            self.stroke = brushes.Stroke(
                get_state(obj), settings.brush, settings.radius * scale, settings.strength,
                invert=invert, symmetry=symmetry.sculpt_bits(settings), grab_rotate=settings.grab_rotate,
            )
        self.times = []
        self.write_ms = []
        self.last_update = 0.0
        self._apply(context, xr.controller_location, xr.controller_rotation, xr.state[0])
        return self._start_modal(context)

    def _start_modal(self, context):
        self.last_update = time.perf_counter()
        # How updates arrive, for tuning: XR modal events vs. timer polling.
        self.n_xr = 0
        self.n_timer = 0
        # XR modal events drive the stroke; the timer is a fallback that polls the
        # controller in case the runtime only sends events when values change.
        self.timer = context.window_manager.event_timer_add(1.0 / 90.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        active_hands.add(self.hand_idx)
        return {'RUNNING_MODAL'}

    def _apply(self, context, loc, rot, pressure):
        pose = controller_pose(loc, rot)
        tip = brush_tip(pose, self.offset)
        if self.handler is not None:
            tip_pose = pose.copy()
            tip_pose.translation = tip
            self.handler.update(context, pose, tip_pose, pressure)
            self.last_update = time.perf_counter()
            return
        # Grab moves vertices with the tip, so use the tip as the pose origin.
        pose_tip = pose.copy()
        pose_tip.translation = tip
        touched = self.stroke.update(tip, pose_tip, pressure=max(pressure, 0.05))
        st = self.stroke.state
        if touched:
            self.times.append(st.last_dab_ms)
            self.write_ms.append(st.last_write_ms)
            haptic(context, self.hand, 0.15 + 0.3 * pressure)
        self.last_update = time.perf_counter()

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            # Never leave a broken modal (and its 90 Hz timer) running.
            import traceback
            traceback.print_exc()
            self._finish(context)
            self.report({'ERROR'}, "VR Sculpt stroke failed, see the system console")
            return {'CANCELLED'}

    def _modal(self, context, event):
        if event.type == 'XR_ACTION':
            xr = event.xr
            if xr.action != actions.ACTION_STROKE or xr.user_path != self.hand:
                return {'PASS_THROUGH'}
            if event.value == 'RELEASE':
                return self._finish(context)
            self.n_xr += 1
            self._apply(context, xr.controller_location, xr.controller_rotation, xr.state[0])
            return {'RUNNING_MODAL'}

        # Python events don't say which timer fired; ours runs at 90 Hz, so any tick will do.
        if event.type == 'TIMER':
            ss = context.window_manager.xr_session_state
            if ss is None or not session_running(context):
                return self._finish(context)
            value = ss.action_state_get(context, actions.ACTIONMAP, actions.ACTION_STROKE, self.hand)
            if not isinstance(value, float):
                value = value[0]
            if value <= self.threshold:
                return self._finish(context)
            # Only poll if no XR event arrived this frame.
            if time.perf_counter() - self.last_update > 1.0 / 120.0:
                loc = ss.controller_aim_location_get(context, self.hand_idx)
                rot = ss.controller_aim_rotation_get(context, self.hand_idx)
                self.n_timer += 1
                self._apply(context, loc, rot, value)
        return {'PASS_THROUGH'}

    def _finish(self, context):
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        active_hands.discard(self.hand_idx)
        if self.handler is not None:
            self.handler.finish(context)
            print(f"VR Sculpt {type(self.handler).__name__}: changed={self.handler.changed} | hand {self.hand}, "
                  f"updates: {self.n_xr} xr events, {self.n_timer} timer polls", flush=True)
            for area in context.screen.areas if context.screen else ():
                area.tag_redraw()
            return {'FINISHED'} if self.handler.changed else {'CANCELLED'}
        if hasattr(self.stroke, "finish"):
            self.stroke.finish()
        if self.times:
            if isinstance(self.stroke, native.NativeStroke):
                brush = native.sculpt_brush(context)
                name = "native " + (brush.name if brush else "?")
            else:
                name = self.stroke.brush
            stats.update(
                verts=len(self.stroke.state.co),
                dabs=len(self.times),
                avg_ms=sum(self.times) / len(self.times),
                max_ms=max(self.times),
                write_ms=sum(self.write_ms) / len(self.write_ms),
                brush=name,
            )
            print("VR Sculpt stroke: {brush} {dabs} dabs on {verts} verts, avg {avg_ms:.1f} ms "
                  "(write {write_ms:.1f} ms), max {max_ms:.1f} ms".format(**stats),
                  f"| hand {self.hand}, updates: {self.n_xr} xr events, {self.n_timer} timer polls, "
                  f"viewer scale {viewer_scale(context):.3f}", flush=True)
        else:
            print(f"VR Sculpt stroke: nothing touched | hand {self.hand}, updates: {self.n_xr} xr events, "
                  f"{self.n_timer} timer polls (is the tip within the brush radius of the mesh?)", flush=True)
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return {'FINISHED'} if self.times else {'CANCELLED'}


MODE_CYCLE = ('SCULPT', 'MODEL', 'DRAW', 'PAINT', 'TOOLS')


def undo_last_stroke(context):
    """Quick undo for Draw mode: delete the latest stroke or note, as one regular undo step."""
    what = strokes.undo_last(context)
    if what is not None:
        try:
            bpy.ops.ed.undo_push(message=f"Undo VR {what}")
        except RuntimeError:
            pass  # No undo stack (background); the stroke is gone either way.
    return what


class VRSCULPT_OT_cycle_brush(bpy.types.Operator):
    """Right hand (B): next brush (Sculpt), next primitive (Model), undo last stroke (Draw),
    remove the last measurement or switch camera (Tools). Left hand (Y): next mode"""
    bl_idname = "vrsculpt.cycle_brush"
    bl_label = "VR Next Brush / Switch Mode"
    bl_options = {'INTERNAL'}

    left_hand: BoolProperty(name="Left Hand", options={'SKIP_SAVE'})

    def execute(self, context):
        settings = context.scene.vr_sculpt
        ss = context.window_manager.xr_session_state
        if not self.left_hand and ss is not None and desktop.pointing_at_content(context, ss):
            desktop.right_click(context, ss)
            return {'FINISHED'}
        if self.left_hand:
            i = MODE_CYCLE.index(settings.mode) if settings.mode in MODE_CYCLE else -1
            menus.set_mode(context, MODE_CYCLE[(i + 1) % len(MODE_CYCLE)])
        elif settings.mode == 'DRAW':
            undo_last_stroke(context)
        elif settings.mode == 'PAINT':
            paint.undo_last(context)
        elif settings.mode == 'TOOLS':
            if settings.tool == 'CAMERA':
                camera.next_camera(context)
            else:
                measure.undo_last(context.scene)
        elif settings.mode == 'MODEL':
            keys = [e.key for e in catalog.entries()]
            if keys:
                i = keys.index(settings.primitive) if settings.primitive in keys else -1
                settings.primitive = keys[(i + 1) % len(keys)]
        elif settings.engine == 'NATIVE':
            try:
                native.cycle_brush(context)
            except (RuntimeError, TypeError) as ex:
                self.report({'WARNING'}, f"Could not switch brush: {ex}")
        else:
            ids = [b[0] for b in BRUSH_ITEMS]
            settings.brush = ids[(ids.index(settings.brush) + 1) % len(ids)]
        return {'FINISHED'}

    def invoke(self, context, event):
        if event.xr:
            self.left_hand = event.xr.user_path == actions.HANDS[0]
        self.execute(context)
        if event.xr:
            haptic(context, event.xr.user_path, 0.8 if self.left_hand else 0.6, 0.08 if self.left_hand else 0.05)
        return {'FINISHED'}


class VRSCULPT_OT_cycle_radius(bpy.types.Operator):
    """Right stick click: next brush radius. Left stick click: show or hide the hand panel"""
    bl_idname = "vrsculpt.cycle_radius"
    bl_label = "VR Next Radius / Toggle Panel"
    bl_options = {'INTERNAL'}

    left_hand: BoolProperty(name="Left Hand", options={'SKIP_SAVE'})

    def execute(self, context):
        settings = context.scene.vr_sculpt
        if self.left_hand:
            settings.show_panel = not settings.show_panel
            return {'FINISHED'}
        prop = "paint_radius" if settings.mode == 'PAINT' else "radius"
        bigger = [r for r in RADIUS_STEPS if r > getattr(settings, prop) + 1e-6]
        setattr(settings, prop, bigger[0] if bigger else RADIUS_STEPS[0])
        return {'FINISHED'}

    def invoke(self, context, event):
        if event.xr:
            self.left_hand = event.xr.user_path == actions.HANDS[0]
        self.execute(context)
        if event.xr:
            haptic(context, event.xr.user_path, 0.4, 0.03)
        return {'FINISHED'}


class VRSCULPT_OT_undo_stroke(bpy.types.Operator):
    """Delete the last stroke or note drawn in VR"""
    bl_idname = "vrsculpt.undo_stroke"
    bl_label = "Undo Last Stroke"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if undo_last_stroke(context) is None:
            self.report({'INFO'}, "No VR stroke to undo")
            return {'CANCELLED'}
        return {'FINISHED'}


class VRSCULPT_OT_paint_setup(bpy.types.Operator):
    """Give the active mesh UVs (Smart UV Project, if it has none) and an image to paint on"""
    bl_idname = "vrsculpt.paint_setup"
    bl_label = "Set Up for Painting"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return paint.target(context) is not None

    def execute(self, context):
        obj = paint.target(context)
        if not paint.setup(context, obj, int(context.scene.vr_sculpt.paint_texture_size)):
            self.report({'WARNING'}, "Could not set up the mesh for painting")
            return {'CANCELLED'}
        return {'FINISHED'}


class VRSCULPT_OT_clear_measures(bpy.types.Operator):
    """Remove all tape measurements made in VR"""
    bl_idname = "vrsculpt.clear_measures"
    bl_label = "Clear VR Measurements"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return {'FINISHED'} if measure.clear(context.scene) else {'CANCELLED'}


class VRSCULPT_OT_set_mode(bpy.types.Operator):
    """Switch the VR tools between Sculpt, Model, Draw, Paint and Tools mode"""
    bl_idname = "vrsculpt.set_mode"
    bl_label = "Set VR Mode"
    bl_options = {'INTERNAL'}

    mode: EnumProperty(items=(('SCULPT', "Sculpt", ""), ('MODEL', "Model", ""), ('DRAW', "Draw", ""),
                              ('PAINT', "Paint", ""), ('TOOLS', "Tools", "")))

    def execute(self, context):
        menus.set_mode(context, self.mode)
        return {'FINISHED'}


classes = (VRSCULPT_OT_stroke, VRSCULPT_OT_cycle_brush, VRSCULPT_OT_cycle_radius, VRSCULPT_OT_undo_stroke,
           VRSCULPT_OT_paint_setup, VRSCULPT_OT_clear_measures, VRSCULPT_OT_set_mode)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    drop_state()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
