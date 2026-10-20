"""What the hand panel shows in each mode, and the mode/engine switches behind it."""

import bpy

from . import native, primitives, vr_ui
from .props import BRUSH_ITEMS

NATIVE_SHORT = {
    "Draw": "Draw", "Clay Strips": "Clay", "Smooth": "Smooth",
    "Inflate/Deflate": "Inflate", "Crease Sharp": "Crease", "Flatten/Contrast": "Flatten",
}


def _desktop_view(context):
    windows = context.window_manager.windows
    window = context.window or (windows[0] if len(windows) else None)
    if window is None:
        return None, None
    return window, native.largest_view3d(window)


def set_object_mode(context, obj, mode):
    """Switch obj to 'OBJECT' or 'SCULPT' mode from any context (XR operators have no area)."""
    if obj is None or obj.type != 'MESH' or obj.mode == mode:
        return
    window, view = _desktop_view(context)
    if view is None:
        return
    with context.temp_override(window=window, area=view[0], region=view[1], active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode=mode)


def set_mode(context, mode):
    settings = context.scene.vr_sculpt
    settings.mode = mode
    obj = context.view_layer.objects.active
    if mode == 'MODEL':
        set_object_mode(context, obj, 'OBJECT')
    elif settings.engine == 'NATIVE':
        set_object_mode(context, obj, 'SCULPT')


def set_engine(context, engine):
    settings = context.scene.vr_sculpt
    settings.engine = engine
    if settings.mode == 'SCULPT':
        set_object_mode(context, context.view_layer.objects.active,
                        'SCULPT' if engine == 'NATIVE' else 'OBJECT')


def _setter(obj, attr, index=None):
    def set_value(v):
        if index is None:
            setattr(obj.vr_prim, attr, v)
        else:
            size = list(obj.vr_prim.size)
            size[index] = v
            obj.vr_prim.size = size
    return set_value


def _meters(v):
    return f"{v * 100:.1f} cm" if v < 1.0 else f"{v:.2f} m"


def rows(context):
    """Rows of widgets for the current mode."""
    settings = context.scene.vr_sculpt
    mode = settings.mode
    out = [[
        vr_ui.button("mode_sculpt", "Sculpt", lambda: set_mode(context, 'SCULPT'), active=mode == 'SCULPT'),
        vr_ui.button("mode_model", "Model", lambda: set_mode(context, 'MODEL'), active=mode == 'MODEL'),
    ]]
    if mode == 'SCULPT':
        out += _sculpt_rows(context, settings)
    else:
        out += _model_rows(context, settings)
    return out


def _sculpt_rows(context, settings):
    out = [[
        vr_ui.button("engine_python", "Python", lambda: set_engine(context, 'PYTHON'),
                     active=settings.engine == 'PYTHON'),
        vr_ui.button("engine_native", "Native", lambda: set_engine(context, 'NATIVE'),
                     active=settings.engine == 'NATIVE'),
    ]]
    if settings.engine == 'NATIVE':
        brush = native.sculpt_brush(context)
        current = brush.name if brush else ""
        cells = [vr_ui.button("brush_" + n, NATIVE_SHORT[n], (lambda n=n: native.activate_brush(context, n)),
                              active=n == current) for n in native.NATIVE_BRUSHES]
        out += [cells[:3], cells[3:]]
    else:
        cells = []
        for ident, name, _desc in BRUSH_ITEMS:
            cells.append(vr_ui.button("brush_" + ident, name,
                                      (lambda ident=ident: setattr(settings, "brush", ident)),
                                      active=settings.brush == ident))
        out.append(cells)
    out.append([vr_ui.slider("radius", "Radius", settings.radius, 0.005, 0.2,
                             lambda v: setattr(settings, "radius", v), fmt=_meters)])
    if settings.engine == 'PYTHON':
        out.append([vr_ui.slider("strength", "Strength", settings.strength, 0.0, 1.0,
                                 lambda v: setattr(settings, "strength", v), fmt=lambda v: f"{v:.2f}")])
    return out


def _model_rows(context, settings):
    out = [[
        vr_ui.label("Add:"),
        vr_ui.button("add_cube", "Cube", lambda: setattr(settings, "primitive", 'CUBE'),
                     active=settings.primitive == 'CUBE'),
        vr_ui.button("add_cylinder", "Cylinder", lambda: setattr(settings, "primitive", 'CYLINDER'),
                     active=settings.primitive == 'CYLINDER'),
    ]]
    obj = context.view_layer.objects.active
    if obj is None or not obj.select_get():
        out.append([vr_ui.label("Drag in empty space to add")])
        return out
    prim = obj.vr_prim if obj.type == 'MESH' else None
    out.append([vr_ui.label(obj.name)])
    if prim is not None and prim.kind == 'CUBE':
        for i, axis in enumerate("XYZ"):
            out.append([vr_ui.slider(f"size_{axis}", f"Size {axis}", prim.size[i], 0.01, 1.0,
                                     _setter(obj, "size", i), fmt=_meters)])
        out.append([vr_ui.slider("subdivisions", "Subdivisions", prim.subdivisions, 0, 16,
                                 _setter(obj, "subdivisions"), integer=True)])
    elif prim is not None and prim.kind == 'CYLINDER':
        out.append([vr_ui.slider("radius_prim", "Radius", prim.radius, 0.005, 0.5,
                                 _setter(obj, "radius"), fmt=_meters)])
        out.append([vr_ui.slider("height", "Height", prim.height, 0.01, 1.0,
                                 _setter(obj, "height"), fmt=_meters)])
        out.append([vr_ui.slider("segments", "Segments", prim.segments, 3, 64,
                                 _setter(obj, "segments"), integer=True)])
    else:
        out.append([vr_ui.label("Plain mesh (sculpted or not a VR primitive)")])
    out.append([
        vr_ui.button("duplicate", "Duplicate", lambda: primitives.duplicate(context, obj)),
        vr_ui.button("delete", "Delete", lambda: primitives.delete(context, obj), danger=True),
    ])
    return out
