import time

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import actions, brushes, native
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


class VRSCULPT_OT_stroke(bpy.types.Operator):
    """Sculpt with a VR controller while the trigger is held"""
    bl_idname = "vrsculpt.stroke"
    bl_label = "VR Sculpt Stroke"
    bl_options = {'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return (session_running(context)
                and context.scene.vr_sculpt.enabled
                and sculpt_target(context) is not None)

    def invoke(self, context, event):
        xr = event.xr
        if xr is None:
            return {'CANCELLED'}
        settings = context.scene.vr_sculpt
        obj = sculpt_target(context)
        self.hand = xr.user_path
        self.hand_idx = actions.hand_index(self.hand)
        self.threshold = xr.float_threshold

        scale = viewer_scale(context)
        self.offset = settings.tip_offset * scale
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
                invert=invert, symmetry_x=settings.symmetry_x, grab_rotate=settings.grab_rotate,
            )
        self.times = []
        self.write_ms = []
        self.last_update = 0.0
        # How updates arrive, for tuning: XR modal events vs. timer polling.
        self.n_xr = 0
        self.n_timer = 0
        self._apply(context, xr.controller_location, xr.controller_rotation, xr.state[0])

        # XR modal events drive the stroke; the timer is a fallback that polls the
        # controller in case the runtime only sends events when values change.
        self.timer = context.window_manager.event_timer_add(1.0 / 90.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        active_hands.add(self.hand_idx)
        return {'RUNNING_MODAL'}

    def _apply(self, context, loc, rot, pressure):
        pose = controller_pose(loc, rot)
        tip = brush_tip(pose, self.offset)
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


class VRSCULPT_OT_cycle_brush(bpy.types.Operator):
    """Switch to the next VR sculpt brush"""
    bl_idname = "vrsculpt.cycle_brush"
    bl_label = "Next VR Brush"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = context.scene.vr_sculpt
        if settings.engine == 'NATIVE':
            try:
                native.cycle_brush(context)
            except (RuntimeError, TypeError) as ex:
                self.report({'WARNING'}, f"Could not switch brush: {ex}")
            return {'FINISHED'}
        ids = [b[0] for b in BRUSH_ITEMS]
        settings.brush = ids[(ids.index(settings.brush) + 1) % len(ids)]
        return {'FINISHED'}

    def invoke(self, context, event):
        self.execute(context)
        if event.xr:
            haptic(context, event.xr.user_path, 0.6, 0.05)
        return {'FINISHED'}


class VRSCULPT_OT_cycle_radius(bpy.types.Operator):
    """Switch to the next VR brush radius"""
    bl_idname = "vrsculpt.cycle_radius"
    bl_label = "Next VR Brush Radius"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = context.scene.vr_sculpt
        bigger = [r for r in RADIUS_STEPS if r > settings.radius + 1e-6]
        settings.radius = bigger[0] if bigger else RADIUS_STEPS[0]
        return {'FINISHED'}

    def invoke(self, context, event):
        self.execute(context)
        if event.xr:
            haptic(context, event.xr.user_path, 0.4, 0.03)
        return {'FINISHED'}


classes = (VRSCULPT_OT_stroke, VRSCULPT_OT_cycle_brush, VRSCULPT_OT_cycle_radius)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    drop_state()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
