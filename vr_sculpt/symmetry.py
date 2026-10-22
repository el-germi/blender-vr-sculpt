"""Symmetry for Model and Sculpt mode.

Model mode mirrors an object with a Mirror modifier around a shared "VR Mirror Center" empty
(placed at the 3D cursor), so the mirrored copy stays live while you move or edit it.
Sculpt mode mirrors brush strokes: the Python brushes use the add-on's own X/Y/Z settings,
the native brushes the mesh's own symmetry flags (the same ones as the desktop sculpt header).
"""

import bpy

MODIFIER = "VR Mirror"
CENTER = "VR Mirror Center"
AXES = "XYZ"


def center(context):
    """The mirror center empty, created at the 3D cursor if it doesn't exist."""
    obj = bpy.data.objects.get(CENTER)
    if obj is None:
        obj = bpy.data.objects.new(CENTER, None)
        obj.empty_display_type = 'SPHERE'
        obj.empty_display_size = 0.01
        obj.location = context.scene.cursor.location
        obj.hide_render = True
    if obj.name not in context.scene.collection.all_objects:
        context.scene.collection.objects.link(obj)
    return obj


def move_center(context, location):
    """Move the 3D cursor and the mirror center (if there is one) to location."""
    context.scene.cursor.location = location
    obj = bpy.data.objects.get(CENTER)
    if obj is not None:
        obj.location = location


def model_axes(obj):
    """(x, y, z) mirror flags of obj's VR Mirror modifier."""
    mod = obj.modifiers.get(MODIFIER) if obj is not None else None
    if mod is None or mod.type != 'MIRROR':
        return (False, False, False)
    return tuple(mod.use_axis)


def model_toggle(context, obj, axis):
    """Turn mirroring across axis (0-2) on or off for obj; drops the modifier when no axis is left."""
    mod = obj.modifiers.get(MODIFIER)
    if mod is None or mod.type != 'MIRROR':
        mod = obj.modifiers.new(MODIFIER, 'MIRROR')
        mod.use_axis = (False, False, False)
        mod.use_mirror_merge = True
        mod.mirror_object = center(context)
        from . import hardsurface
        hardsurface.sort_stack(obj)  # Mirror after the booleans and arrays, before the bevel.
    flags = list(mod.use_axis)
    flags[axis] = not flags[axis]
    if any(flags):
        mod.use_axis = flags
    else:
        obj.modifiers.remove(mod)


def model_bits(obj):
    x, y, z = model_axes(obj)
    return (1 if x else 0) | (2 if y else 0) | (4 if z else 0)


def sculpt_axes(context):
    """(x, y, z) symmetry for the current sculpt engine."""
    settings = context.scene.vr_sculpt
    if settings.engine == 'NATIVE':
        obj = context.view_layer.objects.active
        if obj is None or obj.type != 'MESH':
            return (False, False, False)
        return (obj.data.use_mirror_x, obj.data.use_mirror_y, obj.data.use_mirror_z)
    return (settings.symmetry_x, settings.symmetry_y, settings.symmetry_z)


def sculpt_toggle(context, axis):
    settings = context.scene.vr_sculpt
    name = "use_mirror_" + AXES[axis].lower()
    if settings.engine == 'NATIVE':
        obj = context.view_layer.objects.active
        if obj is not None and obj.type == 'MESH':
            setattr(obj.data, name, not getattr(obj.data, name))
        return
    prop = "symmetry_" + AXES[axis].lower()
    setattr(settings, prop, not getattr(settings, prop))


def sculpt_bits(settings):
    return (1 if settings.symmetry_x else 0) | (2 if settings.symmetry_y else 0) | (4 if settings.symmetry_z else 0)
