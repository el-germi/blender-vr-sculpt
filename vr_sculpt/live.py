"""One app timer for the things that update while VR runs, outside of any operator:
tracking objects, desktop window captures, camera preview redraws, puppet bindings and takes, and the
spatial UI's touch haptics."""

import time

import bpy

from . import camera, desktop, onion, operators, paths, puppet, spatial, tracking

FAST = 1.0 / 90.0
IDLE = 0.25

_last = {"camera": 0.0}
_errors = []


def _tick():
    context = bpy.context
    try:
        scene = context.scene
        settings = getattr(scene, "vr_sculpt", None) if scene else None
        if settings is None:
            return IDLE
        desktop.tick(context)
        if not operators.session_running(context):
            if puppet.active():
                puppet.end_session(context)
            return IDLE
        ss = context.window_manager.xr_session_state
        if settings.tracking:
            tracking.update(context, ss)
        if puppet.active():
            puppet.tick(context, ss)
        paths.ensure(context)
        onion.update(context)
        if settings.show_panel and ss is not None:
            spatial.tick(context, ss)
        if camera.wanted(context):
            # The preview renders in the desktop 3D view's draw callback, so keep it drawing.
            now = time.monotonic()
            if now - _last["camera"] >= 1.0 / camera.PREVIEW_FPS:
                _last["camera"] = now
                region = camera.host_region(context)
                if region is not None:
                    region.tag_redraw()
        busy = (settings.tracking or camera.wanted(context) or desktop.interactive(context)
                or settings.onion or settings.show_paths or puppet.active() or settings.show_panel)
        return FAST if busy else IDLE
    except Exception:
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(1)
        return IDLE


def register():
    bpy.app.timers.register(_tick, first_interval=IDLE, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
