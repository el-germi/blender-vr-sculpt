"""VR Sculpt prototype: sculpt, model, draw and measure with VR controllers.

Needs the bundled "VR Scene Inspection" add-on, which starts the VR session.
"""

if "bpy" in locals():
    import importlib
    for _m in (brushes, native, strokes, props, catalog, primitives, generic, actions, hud, grid, symmetry,
               shading, hardsurface, snapping, anim, paths, onion, paint, pose, puppet, measure, camera, desktop,
               tracking, navigation, gfx, vr_ui, presence, menus, interactions, drawing, operators, live, draw, ui):
        importlib.reload(_m)
else:
    from . import (brushes, native, strokes, props, catalog, primitives, generic, actions, hud, grid, symmetry,
                   shading, hardsurface, snapping, anim, paths, onion, paint, pose, puppet, measure, camera,
                   desktop, tracking, navigation, gfx, vr_ui, presence, menus, interactions, drawing, operators,
                   live, draw, ui)

import bpy

_modules = (props, primitives, actions, operators, navigation, camera, desktop, onion, paint, puppet, live, draw, ui)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
