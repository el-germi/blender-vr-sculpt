"""VR Sculpt prototype: sculpt and model with VR controllers.

Needs the bundled "VR Scene Inspection" add-on, which starts the VR session.
"""

if "bpy" in locals():
    import importlib
    for _m in (brushes, native, strokes, props, catalog, primitives, generic, actions, vr_ui, menus,
               interactions, drawing, operators, hud, draw, ui):
        importlib.reload(_m)
else:
    from . import (brushes, native, strokes, props, catalog, primitives, generic, actions, vr_ui, menus,
                   interactions, drawing, operators, hud, draw, ui)

import bpy

_modules = (props, primitives, actions, operators, draw, ui)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
