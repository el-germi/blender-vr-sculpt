"""Patch the VR Scene Inspection action map before a VR session starts.

Blender syncs one active action set, and OpenXR action sets can only be created
before the session attaches them. So instead of a separate set, we edit the
stock "blender_default" map right before the stock add-on turns it into
OpenXR actions: the trigger teleport becomes our stroke action, two buttons
get brush/radius cycling, and the thumbstick fly actions get our Walk/Fly
navigation.
"""

import sys

import bpy
from bpy.app.handlers import persistent

ACTIONMAP = "blender_default"
ACTION_STROKE = "vrsculpt_stroke"
ACTION_BRUSH = "vrsculpt_cycle_brush"
ACTION_RADIUS = "vrsculpt_cycle_radius"
ACTION_HAPTIC = "haptic"

HANDS = ("/user/hand/left", "/user/hand/right")

# (new action, stock action whose bindings we copy, operator, mode, remove stock)
_PATCHES = (
    (ACTION_STROKE, "teleport", "vrsculpt.stroke", 'MODAL', True),
    (ACTION_BRUSH, "viewfinder_apply_action_up", "vrsculpt.cycle_brush", 'PRESS', False),  # B / Y
    (ACTION_RADIUS, "viewfinder_swap_hands", "vrsculpt.cycle_radius", 'PRESS', False),  # stick click
)

last_status = "VR session not started yet"


def vr_addon():
    return sys.modules.get("viewport_vr_preview")


def hand_index(user_path):
    return HANDS.index(user_path) if user_path in HANDS else 1


def _patch(session_state):
    vr = vr_addon()
    defaults = vr.defaults

    # Start from the stock maps each time so toggling the option works.
    for item in defaults.VRDefaultActionmaps:
        am = session_state.actionmaps.find(session_state, item.value)
        if am:
            session_state.actionmaps.remove(session_state, am)
    if not defaults.vr_ensure_default_actionmaps(session_state):
        return "Could not load the default VR action maps"

    if not bpy.context.scene.vr_sculpt.enabled:
        return "Sculpting off: stock controls"

    am = session_state.actionmaps.find(session_state, ACTIONMAP)
    if not am:
        return f"Action map '{ACTIONMAP}' not found"

    items = am.actionmap_items
    missing = []
    for name, source, op, mode, remove_source in _PATCHES:
        src = items.find(source)
        if not src:
            missing.append(source)
            continue
        ami = items.new_from_item(src)
        ami.name = name
        ami.op = op
        ami.op_mode = mode
        ami.haptic_name = ""
        if remove_source:
            items.remove(src)
    # Thumbstick navigation: our operator reads Walk/Fly live (navigation.py).
    from . import navigation
    for name in navigation.ACTIONS:
        ami = items.find(name)
        if not ami:
            missing.append(name)
            continue
        ami.op = "vrsculpt.navigate"
        ami.op_mode = 'MODAL'
    if missing:
        return "Patched, but stock actions missing: " + ", ".join(missing)
    return "Trigger = sculpt/model/draw/tools, Y = mode, B = next brush / undo, stick clicks: L = panel, R = radius"


@persistent
def on_xr_session_start_pre(_context):
    global last_status
    context = bpy.context
    session_state = context.window_manager.xr_session_state
    if not session_state:
        return
    if vr_addon() is None:
        last_status = "Enable the 'VR Scene Inspection' add-on"
        return
    # Our cursor is drawn by an add-on draw handler, which the headset only shows with this on.
    context.window_manager.xr_session_settings.show_custom_overlays = True
    # Draw mode's Note strokes are annotations.
    context.window_manager.xr_session_settings.show_annotation = True
    # Pick up primitives from extensions enabled since the last session.
    from . import catalog
    try:
        catalog.entries(refresh=True)
    except Exception as ex:
        print("VR Sculpt: could not list Add > Mesh entries:", ex)
    try:
        last_status = _patch(session_state)
    except Exception as ex:  # Never block the VR session from starting.
        last_status = f"Action map patch failed: {ex}"
    print("VR Sculpt:", last_status, flush=True)


def register():
    # Run before the stock handler, which creates the OpenXR actions from the map.
    bpy.app.handlers.xr_session_start_pre.insert(0, on_xr_session_start_pre)


def unregister():
    if on_xr_session_start_pre in bpy.app.handlers.xr_session_start_pre:
        bpy.app.handlers.xr_session_start_pre.remove(on_xr_session_start_pre)
