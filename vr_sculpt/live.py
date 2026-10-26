"""One app timer for the things that update while VR runs, outside of any operator:
tracking objects, desktop window captures, camera preview redraws, puppet bindings and takes, the
weight heat map's geometry, the UV checker / stretch view, kit listings coming in from their background worker, and physics (the
hands' colliders and spring bones, which also run while the timeline plays outside VR). Dyntopo
sessions (rest.py) are closed from here too, VR or not, however Sculpt Mode was left."""

import time

import bpy

from . import (camera, controllers, desktop, kit, native, onion, operators, paths, physics, puppet, rest, rig, tracking,
               uv)

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
        kit.tick(context)
        _rest_tick(context)
        if not operators.session_running(context):
            if puppet.active():
                puppet.end_session(context)
            controllers.tick(context, False)  # Blender's controllers back as they were.
            camera.release_base_pose(context)  # And the base pose, if aiming a camera pinned it.
            physics.end_session(context)
            return FAST if physics.tick(context, None) else IDLE  # Springs while the timeline plays.
        ss = context.window_manager.xr_session_state
        if settings.mode == 'SCULPT' and settings.engine == 'NATIVE':
            native.auto_texture(context, settings, bool(operators.active_hands))
        if settings.mode == 'MODEL':
            kit.prewarm(context)  # Listed in the background before the Kit is first opened.
        if settings.tracking:
            tracking.update(context, ss)
        if puppet.active():
            puppet.tick(context, ss)
        paths.ensure(context)
        onion.update(context)
        rig.tick(context)
        uv.tick(context)
        physics_busy = physics.tick(context, ss)
        if camera.wanted(context):
            # The preview renders in the desktop 3D view's draw callback, so keep it drawing.
            now = time.monotonic()
            if now - _last["camera"] >= 1.0 / camera.PREVIEW_FPS:
                _last["camera"] = now
                region = camera.host_region(context)
                if region is not None:
                    region.tag_redraw()
        busy = (settings.tracking or camera.wanted(context) or desktop.interactive(context)
                or settings.onion or settings.show_paths or puppet.active() or physics_busy)
        return FAST if busy else IDLE
    except Exception:
        if not _errors:
            import traceback
            traceback.print_exc()
        _errors.append(1)
        return IDLE


def _rest_tick(context):
    """Dyntopo sessions end however Sculpt Mode is left, VR or not; a failure here mustn't stop the rest."""
    try:
        rest.tick(context)
    except Exception:
        if not _rest_errors:
            import traceback
            traceback.print_exc()
        _rest_errors.append(1)


_rest_errors = []


def register():
    bpy.app.timers.register(_tick, first_interval=IDLE, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
