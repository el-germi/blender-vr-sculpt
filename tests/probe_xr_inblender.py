"""Probe: fetch the controller models from a second OpenXR instance inside Blender's own process.

tests/probe_xr_models.py run as its own process makes SteamVR switch the scene app to it and quit
Blender's VR session. This tries the same fetch from inside Blender while its session runs, in case
SteamVR tells apps apart by process. Starts the VR session itself, waits, runs the fetch on a
thread (the XR frame loop stays on the main thread), and logs Blender's session and right
controller pose around it to tests/xr_inblender_result.txt.

    blender test_scene.blend --python tests/probe_xr_inblender.py

Needs the headset awake and the controllers in hand.

Result (SteamVR 2.18, Steam Link): the second xrCreateInstance fails with XR_ERROR_LIMIT_REACHED
(-10), one instance per process; Blender's session carries on untouched.
"""

import os
import sys
import threading
import time
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import probe_xr_models  # noqa: E402

RESULT = os.path.join(HERE, "xr_inblender_result.txt")
_lines = []
_t0 = time.time()
_state = {"phase": "start", "started": None, "thread": None, "done": None}


def log(msg):
    line = "%7.2f %s" % (time.time() - _t0, msg)
    print(line, flush=True)
    _lines.append(line)
    with open(RESULT, "w") as f:
        f.write("\n".join(_lines) + "\n")


def view3d():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                return win, area
    return None, None


def session_info():
    wm = bpy.context.window_manager
    running = bpy.types.XrSessionState.is_running(bpy.context)
    if not running or wm.xr_session_state is None:
        return "Blender session running=%s" % running
    loc = wm.xr_session_state.controller_grip_location_get(bpy.context, 1)
    return "Blender session running=True, right grip %s" % (tuple(round(v, 4) for v in loc),)


def run_fetch():
    try:
        probe_xr_models.fetch(probe_xr_models.active_runtime())
        _state["done"] = "ok"
    except Exception:
        _state["done"] = "error"
        log(traceback.format_exc())


def tick():
    now = time.time()
    phase = _state["phase"]
    if phase == "start" and now - _t0 > 3.0:
        win, area = view3d()
        with bpy.context.temp_override(window=win, area=area):
            bpy.ops.wm.xr_session_toggle()
        log("asked Blender to start its VR session")
        _state["phase"] = "wait_session"
    elif phase == "wait_session":
        log(session_info())
        if bpy.types.XrSessionState.is_running(bpy.context):
            _state["started"] = _state["started"] or now
            if now - _state["started"] > 8.0:  # let it reach FOCUSED
                log("starting the second instance on a thread")
                _state["thread"] = threading.Thread(target=run_fetch, daemon=True)
                _state["thread"].start()
                _state["phase"] = "fetching"
                _state["started"] = now
        elif now - _t0 > 40.0:
            log("Blender's session never started")
            return None
    elif phase == "fetching":
        log(session_info())
        if _state["done"]:
            log("fetch finished: %s" % _state["done"])
            _state["phase"] = "after"
            _state["started"] = now
        elif now - _state["started"] > 30.0:
            log("fetch still running after 30 s")
            _state["phase"] = "after"
            _state["started"] = now
    elif phase == "after":
        log(session_info())
        if now - _state["started"] > 10.0:
            log("done; leaving Blender's session as it is")
            return None
    return 0.5


log("probe loaded")
bpy.app.timers.register(tick, first_interval=0.5, persistent=True)
