"""VR Sculpt prototype: sculpt, model, draw and measure with VR controllers.

Needs the bundled "VR Scene Inspection" add-on, which starts the VR session.
"""

if "bpy" in locals():
    import importlib
    for _m in (smoothing, brushes, rest, native, strokes, retopo, props, catalog, primitives, generic, kit, actions,
               hud, grid, symmetry, shading, uv, hardsurface, snapping, anim, paths, onion, paint, pose, rig,
               puppet, physics, measure, camera, lookdev, refs, desktop, tracking, navigation, gfx, vr_ui, presence,
               menus, interactions, drawing, pencil, operators, controllers, live, draw, ui):
        importlib.reload(_m)
else:
    from . import (smoothing, brushes, rest, native, strokes, retopo, props, catalog, primitives, generic, kit,
                   actions, hud, grid, symmetry, shading, uv, hardsurface, snapping, anim, paths, onion, paint,
                   pose, rig, puppet, physics, measure, camera, lookdev, refs, desktop, tracking, navigation, gfx,
                   vr_ui, presence, menus, interactions, drawing, pencil, operators, controllers, live, draw, ui)

import bpy

_modules = (props, primitives, physics, retopo, kit, uv, actions, operators, navigation, camera, lookdev, desktop,
            onion, paint, puppet, controllers, live, draw, ui)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
