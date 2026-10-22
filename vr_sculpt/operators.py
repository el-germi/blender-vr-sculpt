import time

import bpy
from bpy.props import EnumProperty
from mathutils import Matrix, Quaternion, Vector

from . import (actions, brushes, camera, desktop, drawing, generic, hardsurface, hud, interactions, measure, menus,
               native, navigation, paint, paths, primitives, radial, shading, snapping, strokes, symmetry)
from . import pose as pose_mode  # "pose" is the controller pose in the handlers below

# Shared geometry cache, so both hands sculpt the same arrays.
_state = None

# Last stroke numbers, shown in the panel.
stats = {"verts": 0, "dabs": 0, "avg_ms": 0.0, "max_ms": 0.0, "write_ms": 0.0, "brush": ""}

# Controller tips being drawn as "active" (stroking) per hand index.
active_hands = set()

# Undo, redo and loading a file replace Blender's data with new copies, so whatever a trigger
# handler kept from before (objects, meshes, the scene's settings, F-curves) points at freed
# memory, and touching it crashes Blender. They are counted here, so a running trigger operator
# can tell and let go without touching any of it.
data_generation = 0


@bpy.app.handlers.persistent
def _on_data_swap(*_args):
    global data_generation
    data_generation += 1
    drop_state()
    snapping.clear()
    brushes.forget_caches()
    strokes.forget_caches()
    pose_mode.forget_caches()


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
    if settings.mode == 'POSE':
        return pose_mode.engaged(context, tip, scale)
    return False


def _pick_handler(context, settings, hand, pose, tip_pose, scale, value):
    """Pie choice, grab, create or draw for this trigger press; None means sculpt (or nothing)."""
    ss = context.window_manager.xr_session_state
    if radial.pie is not None and not radial.pie.closed:
        # While a pie is open, the trigger chooses in it (a held pie, or one left open on a branch).
        return interactions.PieConfirm(context, radial.pie)
    if settings.show_desktop and ss is not None and not _tip_engaged(context, settings, tip_pose.translation, scale):
        handler = desktop.pick(context, ss, hand, pose)
        if handler is not None:
            return handler
    if settings.show_paths:
        key = paths.nearest_key(context, tip_pose.translation, scale)
        if key is not None:
            return paths.KeyDrag(context, key[0], key[1], tip_pose.translation)
    if settings.mode == 'MODEL' and settings.model_mark != 'NONE' and shading.mark_targets(context):
        how = 'CLEAR' if hand == actions.HANDS[0] else settings.model_mark
        return shading.EdgeMark(context, how, scale)
    if settings.mode == 'MODEL':
        obj = primitives.touching(context, tip_pose.translation, interactions.TOUCH_MARGIN * scale)
        # Making cutters, a press on the object being cut starts a new cutter there (anything else
        # in reach, e.g. one of its cutters, is grabbed).
        cutting = settings.model_add_as != 'OBJECT' and obj is not None and obj == hardsurface.cut_target(context)
        if obj is not None and not cutting:
            return interactions.MoveObject(context, obj, tip_pose, scale)
        return interactions.CreatePrimitive(context, settings.primitive, tip_pose.translation, scale)
    if settings.mode == 'DRAW':
        return drawing.pick(context, settings, tip_pose, value, scale)
    if settings.mode == 'PAINT':
        return paint.pick(context, settings, hand == actions.HANDS[0], pose, tip_pose, scale, value)
    if settings.mode == 'POSE':
        return pose_mode.pick(context, settings, tip_pose, scale)
    if settings.mode == 'TOOLS':
        if settings.tool == 'CAMERA':
            return camera.AimCamera(context, pose)
        return measure.Measure(context, settings, tip_pose.translation, scale)
    return None


class VRSCULPT_OT_stroke(bpy.types.Operator):
    """Sculpt, model, draw, paint, pose or measure with a VR controller while the trigger is held"""
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
        self.generation = data_generation
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
        if self.generation != data_generation:
            return self._abandon(context)
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

    def _abandon(self, context):
        """Blender's data was swapped (undo / redo) while the trigger was held: stop here, without
        finishing (that would touch the stale references). A handler can release what isn't
        Blender data (a held mouse button, a drawn preview) in abandon()."""
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        active_hands.discard(self.hand_idx)
        abandon = getattr(self.handler, "abandon", None)
        if abandon is not None:
            abandon()
        name = type(self.handler or self.stroke).__name__
        self.handler = self.stroke = None
        print(f"VR Sculpt {name}: undo or redo while the trigger was held; the action was dropped", flush=True)
        return {'CANCELLED'}

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


def undo_last_stroke(context):
    """Quick undo for Draw mode: delete the latest stroke or note, as one regular undo step."""
    what = strokes.undo_last(context)
    if what is not None:
        try:
            bpy.ops.ed.undo_push(message=f"Undo VR {what}")
        except RuntimeError:
            pass  # No undo stack (background); the stroke is gone either way.
    return what


PIE_KINDS = {(actions.ACTION_PIE, 0): 'MODE', (actions.ACTION_PIE, 1): 'TOOL',
             (actions.ACTION_PIE_ALT, 0): 'TIME', (actions.ACTION_PIE_ALT, 1): 'OBJECT'}

# (amplitude, seconds) per pie / knob event.
HAPTICS = {'hover': (0.22, 0.012), 'open': (0.45, 0.025), 'back': (0.3, 0.015), 'grab': (0.5, 0.02),
           'tick': (0.12, 0.006), 'close': (0.2, 0.01), 'confirm': (0.65, 0.035), 'tap': (0.5, 0.03)}


def pulse(context, hand, events):
    for e in events:
        amplitude, duration = HAPTICS.get(e, (0.2, 0.01))
        haptic(context, hand, amplitude, duration)


def _button_value(context, ss, action, hand):
    value = ss.action_state_get(context, actions.ACTIONMAP, action, hand)
    return value if isinstance(value, float) else value[0]


class VRSCULPT_OT_pie(bpy.types.Operator):
    """Hold a face button for a pie menu at that hand: Y modes, B the mode's tools, A the selected object,
    X time. Move the hand toward a slice (or flick the stick) and let go. A short tap does a quick action"""
    bl_idname = "vrsculpt.pie"
    bl_label = "VR Pie Menu"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return session_running(context) and context.scene.vr_sculpt.enabled

    def invoke(self, context, event):
        xr = event.xr
        if xr is None:
            return {'CANCELLED'}
        self.hand = xr.user_path
        self.hand_idx = actions.hand_index(self.hand)
        self.action = xr.action
        self.threshold = xr.float_threshold
        self.timer = None
        kind = PIE_KINDS.get((xr.action, self.hand_idx), 'TOOL')
        ss = context.window_manager.xr_session_state
        current = radial.pie
        if current is not None and not current.closed:
            if not current.sticky:
                return {'CANCELLED'}  # Another pie is being held.
            if current.kind == kind and current.hand == self.hand_idx:
                # The same button again: choose what it points at (or close it).
                self.pie = current
                return self._outcome(context, current, current.confirm(), second_press=True)
            current.closed = True
            radial.close()
        if kind == 'TOOL' and ss is not None and desktop.pointing_at_content(context, ss):
            desktop.right_click(context, ss)
            return {'FINISHED'}
        pose = controller_pose(xr.controller_location, xr.controller_rotation)
        viewer = Vector(ss.viewer_pose_location) if ss is not None else pose.translation + Vector((0.0, -0.5, 0.0))
        level = menus.root_level(context, kind)
        self.pie = radial.Pie(kind, self.hand_idx, level, hud.billboard(pose.translation, viewer, viewer_scale(context)),
                              pose.to_quaternion())
        radial.pie = self.pie
        self.generation = data_generation
        self.last = time.perf_counter()
        haptic(context, self.hand, 0.3, 0.015)
        self.timer = context.window_manager.event_timer_add(1.0 / 90.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            import traceback
            traceback.print_exc()
            self._stop(context)
            self.report({'ERROR'}, "VR Sculpt pie failed, see the system console")
            return {'CANCELLED'}

    def _modal(self, context, event):
        pie = self.pie
        if self.generation != data_generation:
            pie.closed = True  # Undo or redo while it was open: its items point at freed data.
        if pie.closed:
            return self._stop(context)
        if event.type == 'XR_ACTION':
            xr = event.xr
            if xr.action != self.action or xr.user_path != self.hand:
                return {'PASS_THROUGH'}
            if event.value == 'RELEASE':
                return self._release(context)
            self._update(context, xr.controller_location, xr.controller_rotation)
            return {'RUNNING_MODAL'}
        if event.type == 'TIMER':
            ss = context.window_manager.xr_session_state
            if ss is None or not session_running(context):
                pie.closed = True
                return self._stop(context)
            if not pie.sticky and _button_value(context, ss, self.action, self.hand) <= self.threshold:
                return self._release(context)
            if time.perf_counter() - self.last > 1.0 / 120.0:
                self._update(context, ss.controller_aim_location_get(context, self.hand_idx),
                             ss.controller_aim_rotation_get(context, self.hand_idx))
        return {'PASS_THROUGH'}

    def _update(self, context, loc, rot):
        now = time.perf_counter()
        dt = min(now - self.last, 0.05)
        self.last = now
        ss = context.window_manager.xr_session_state
        stick = navigation.stick(context, ss, self.hand_idx) if ss is not None else (0.0, 0.0)
        pulse(context, self.hand, self.pie.update(Vector(loc), Quaternion(rot), stick, dt=dt))

    def _release(self, context):
        return self._outcome(context, self.pie, self.pie.release())

    def _outcome(self, context, pie, outcome, second_press=False):
        """After a release (or a second press of a pie left open): run the result. A pie left open on a
        branch keeps running (in the operator that opened it)."""
        if outcome == 'sticky':
            haptic(context, self.hand, 0.45, 0.025)
            return {'CANCELLED'} if second_press else {'RUNNING_MODAL'}
        result = {'CANCELLED'}
        if outcome == 'tap':
            try:
                message = menus.tap(context, pie.kind)
            except (RuntimeError, TypeError) as ex:
                message = str(ex)
            if message:
                radial.toast(message, menus.MODE_COLORS.get(context.scene.vr_sculpt.mode, (1.0, 1.0, 1.0)))
            pulse(context, self.hand, ['tap'])
            # Repeating an object action is an undo step; mode switches and play aren't (as before).
            if pie.kind == 'OBJECT' and radial.last_action is not None:
                result = {'FINISHED'}
        elif outcome == 'done':
            radial.remember(pie.ran)
            generic.flush_pending(context)
            pulse(context, self.hand, ['confirm'])
            if pie.ran is not None:
                radial.toast(pie.ran.label, pie.level.color or (1.0, 1.0, 1.0))
            if pie.changed and (pie.ran is None or pie.ran.push):
                context.view_layer.update()
                result = {'FINISHED'}
        if second_press:
            radial.close()
        else:
            self._stop(context)
        return result

    def _stop(self, context):
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        if radial.pie is self.pie:
            radial.close()
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return {'CANCELLED'}


class VRSCULPT_OT_knob(bpy.types.Operator):
    """Hold a stick click and twist that hand like a knob: right = the mode's size (radius, width, ...),
    left = its amount (strength, smoothing, ...). A click without a twist steps the radius (right) or
    shows the controls (left)"""
    bl_idname = "vrsculpt.knob"
    bl_label = "VR Knob"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return session_running(context) and context.scene.vr_sculpt.enabled

    def invoke(self, context, event):
        xr = event.xr
        if xr is None:
            return {'CANCELLED'}
        self.hand = xr.user_path
        self.hand_idx = actions.hand_index(self.hand)
        self.action = xr.action
        self.threshold = xr.float_threshold
        self.side = 'L' if self.hand_idx == 0 else 'R'
        self.generation = data_generation
        self.knob = radial.Knob(self.hand_idx, menus.knob_dial(context, self.side), Quaternion(xr.controller_rotation))
        radial.knobs[self.hand_idx] = self.knob
        self.timer = context.window_manager.event_timer_add(1.0 / 90.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            import traceback
            traceback.print_exc()
            return self._stop(context)

    def _modal(self, context, event):
        if self.generation != data_generation:
            return self._stop(context)
        if event.type == 'XR_ACTION':
            xr = event.xr
            if xr.action != self.action or xr.user_path != self.hand:
                return {'PASS_THROUGH'}
            if event.value == 'RELEASE':
                return self._finish(context)
            pulse(context, self.hand, self.knob.update(Quaternion(xr.controller_rotation)))
            return {'RUNNING_MODAL'}
        if event.type == 'TIMER':
            ss = context.window_manager.xr_session_state
            if ss is None or not session_running(context):
                return self._stop(context)
            if _button_value(context, ss, self.action, self.hand) <= self.threshold:
                return self._finish(context)
            rot = ss.controller_aim_rotation_get(context, self.hand_idx)
            pulse(context, self.hand, self.knob.update(Quaternion(rot)))
        return {'PASS_THROUGH'}

    def _finish(self, context):
        outcome = self.knob.release()
        self._stop(context)
        if outcome == 'tap':
            message = menus.knob_tap(context, self.side)
            if message:
                radial.toast(message, menus.MODE_COLORS.get(context.scene.vr_sculpt.mode, (1.0, 1.0, 1.0)))
            haptic(context, self.hand, 0.4, 0.03)
        elif outcome == 'done':
            generic.flush_pending(context)
            if self.knob.changed:
                return {'FINISHED'}
        return {'CANCELLED'}

    def _stop(self, context):
        if self.timer is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        if radial.knobs.get(self.hand_idx) is self.knob:
            del radial.knobs[self.hand_idx]
        return {'CANCELLED'}


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
    """Switch the VR tools between Sculpt, Model, Draw, Paint, Pose and Tools mode"""
    bl_idname = "vrsculpt.set_mode"
    bl_label = "Set VR Mode"
    bl_options = {'INTERNAL'}

    mode: EnumProperty(items=(('SCULPT', "Sculpt", ""), ('MODEL', "Model", ""), ('DRAW', "Draw", ""),
                              ('PAINT', "Paint", ""), ('POSE', "Pose", ""), ('TOOLS', "Tools", "")))

    def execute(self, context):
        menus.set_mode(context, self.mode)
        return {'FINISHED'}


classes = (VRSCULPT_OT_stroke, VRSCULPT_OT_pie, VRSCULPT_OT_knob, VRSCULPT_OT_undo_stroke,
           VRSCULPT_OT_paint_setup, VRSCULPT_OT_clear_measures, VRSCULPT_OT_set_mode)


_SWAP_HANDLERS = ("undo_pre", "undo_post", "redo_pre", "redo_post", "load_post")


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    for name in _SWAP_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if _on_data_swap not in handlers:
            handlers.append(_on_data_swap)


def unregister():
    for name in _SWAP_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if _on_data_swap in handlers:
            handlers.remove(_on_data_swap)
    drop_state()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
