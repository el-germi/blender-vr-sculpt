"""Windowed test: native strokes driven from inside a modal operator, like in VR.

blender --factory-startup --python tests/native_modal.py
"""

import os
import sys
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "tests", "native_modal_result.txt")
lines = []


def quit_cleanly():
    """Quit Blender. vr_sculpt was registered by hand, so Blender won't disable it on exit: unregister it
    first, or its GPU offscreens are freed after Blender's own teardown and the exit crashes."""
    try:
        import vr_sculpt
        vr_sculpt.unregister()
    except Exception:
        import traceback
        traceback.print_exc()
    bpy.ops.wm.quit_blender()


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def positions(obj):
    n = len(obj.data.vertices)
    a = np.empty(n * 3, dtype=np.float32)
    obj.data.attributes["position"].data.foreach_get("vector", a)
    return a.reshape(n, 3).astype(np.float64)


class TEST_OT_native_modal(bpy.types.Operator):
    """Creates the NativeStroke in invoke and updates it from timer events, like VRSCULPT_OT_stroke"""
    bl_idname = "test.native_modal"
    bl_label = "Native modal test"

    def invoke(self, context, event):
        from vr_sculpt import native
        self.obj = context.active_object
        self.start = positions(self.obj)
        self.stroke = native.NativeStroke(context, self.obj, 0.15)
        self.i = 0
        self.errors = 0
        self.timer = context.window_manager.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        a = -0.6 + self.i * 0.05
        tip = Vector((np.sin(a), -np.cos(a), 0.0)) * 1.02
        try:
            self.stroke.update(tip, None, 1.0)
        except Exception as ex:
            self.errors += 1
            if self.errors == 1:
                log("update error:", str(ex).splitlines()[0])
        self.i += 1
        if self.i < 20:
            return {'RUNNING_MODAL'}
        context.window_manager.event_timer_remove(self.timer)
        self.stroke.finish()
        d = np.linalg.norm(positions(self.obj) - self.start, axis=1)
        log(f"modal stroke: {self.stroke.dabs} dabs, {self.stroke.dropped} dropped, {self.errors} errors, "
            f"moved {int((d > 1e-6).sum())} verts")
        next_stroke()
        return {'FINISHED'}


stroke_count = [0]


def next_stroke():
    """Second stroke: with a non-brush tool active, the engine must switch to the Brush tool."""
    stroke_count[0] += 1
    if stroke_count[0] > 1:
        finish()
        return
    from vr_sculpt import native
    win = bpy.context.window_manager.windows[0]
    area, region = native.largest_view3d(win)

    def run():
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.wm.tool_set_by_id(name="builtin.box_mask")
            log("tool before stroke 2:", bpy.context.workspace.tools.from_space_view3d_mode("SCULPT").idname)
        with bpy.context.temp_override(window=win):
            bpy.ops.test.native_modal('INVOKE_DEFAULT')
    bpy.app.timers.register(lambda: (run(), None)[1], first_interval=0.2)


def finish():
    import vr_sculpt
    vr_sculpt.unregister()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.app.timers.register(lambda: (quit_cleanly(), None)[1], first_interval=0.2)


def start():
    try:
        import vr_sculpt
        from vr_sculpt import native
        vr_sculpt.register()
        bpy.utils.register_class(TEST_OT_native_modal)
        win = bpy.context.window_manager.windows[0]
        area, region = native.largest_view3d(win)
        rv3d = area.spaces.active.region_3d
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=6, radius=1.0)
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.object.mode_set(mode='SCULPT')
        rv3d.view_perspective = 'PERSP'
        rv3d.view_location = (0, 0, 0)
        rv3d.view_rotation = Quaternion((1, 0, 0), 1.5708)
        rv3d.view_distance = 6.0
        # Invoke with a window but no area, roughly how XR action operators run.
        with bpy.context.temp_override(window=win):
            bpy.ops.test.native_modal('INVOKE_DEFAULT')
    except Exception:
        log(traceback.format_exc())
        finish()


bpy.app.timers.register(lambda: (start(), None)[1], first_interval=1.5)
bpy.app.timers.register(lambda: (log("TIMEOUT"), finish(), None)[2], first_interval=20.0)
