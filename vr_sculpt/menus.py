"""What the hand panel shows in each mode, and the mode/engine switches behind it."""

import math

import bpy

from . import catalog, generic, native, primitives, strokes, vr_ui
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
    if mode in {'MODEL', 'DRAW'}:
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
    return f"{v * 100:.1f} cm" if abs(v) < 1.0 else f"{v:.2f} m"


def _degrees(v):
    return f"{math.degrees(v):.0f}\u00b0"


def _number(v):
    return f"{v:.3g}"


# --- picker (the open state of a select widget) -----------------------------

PICKER_COLS = 3
PICKER_ROWS = 6

_picker = None


class Option:
    def __init__(self, value, label, category=""):
        self.value, self.label, self.category = value, label, category


def open_picker(title, options, current, on_select):
    """Replace the panel with a list to choose from. options: list of Option."""
    global _picker
    categories = list(dict.fromkeys(o.category for o in options))
    _picker = {
        "title": title, "options": options, "current": current, "on_select": on_select,
        "categories": categories,
        # With several categories, start on the category list, at the current value's category.
        "category": None if len(categories) > 1 else categories[0] if categories else "",
        "page": 0,
    }


def close_picker():
    global _picker
    _picker = None


def picker_open():
    return _picker is not None


def _pick(value):
    on_select = _picker["on_select"]
    close_picker()
    on_select(value)


def _set_category(category):
    _picker["category"] = category
    _picker["page"] = 0


def _set_page(page):
    _picker["page"] = page


def _back():
    if _picker["category"] is not None and len(_picker["categories"]) > 1:
        _picker["category"] = None
        _picker["page"] = 0
    else:
        close_picker()


def _grid(cells, cols):
    return [cells[i:i + cols] for i in range(0, len(cells), cols)]


def _paged(cells, page, per_page):
    pages = max((len(cells) + per_page - 1) // per_page, 1)
    page = min(max(page, 0), pages - 1)
    return cells[page * per_page:(page + 1) * per_page], page, pages


def _picker_rows():
    p = _picker
    out = [[vr_ui.label(p["title"])]]
    per_page = PICKER_COLS * PICKER_ROWS
    if p["category"] is None:
        current_cat = next((o.category for o in p["options"] if o.value == p["current"]), None)
        cells = []
        for cat in p["categories"]:
            cells.append(vr_ui.button("cat_" + cat, cat, (lambda c=cat: _set_category(c)),
                                      active=cat == current_cat))
        shown, page, pages = _paged(cells, p["page"], 2 * PICKER_ROWS)
        out += _grid(shown, 2)
    else:
        options = [o for o in p["options"] if o.category == p["category"]]
        cells = [vr_ui.button("opt_" + str(o.value), o.label, (lambda v=o.value: _pick(v)),
                              active=o.value == p["current"]) for o in options]
        shown, page, pages = _paged(cells, p["page"], per_page)
        out += _grid(shown, PICKER_COLS)
    nav = [vr_ui.button("picker_back", "Back", _back)]
    if pages > 1:
        nav.append(vr_ui.button("picker_prev", "\u2039 Prev", lambda: _set_page(page - 1)) if page > 0
                   else vr_ui.label(""))
        nav.append(vr_ui.label(f"  {page + 1} / {pages}"))
        nav.append(vr_ui.button("picker_next", "Next \u203a", lambda: _set_page(page + 1)) if page < pages - 1
                   else vr_ui.label(""))
    out.append(nav)
    return out


# --- rows ------------------------------------------------------------------

def rows(context):
    """Rows of widgets for the current mode (or the open picker)."""
    if _picker is not None:
        return _picker_rows()
    settings = context.scene.vr_sculpt
    mode = settings.mode
    out = [[
        vr_ui.button("mode_sculpt", "Sculpt", lambda: set_mode(context, 'SCULPT'), active=mode == 'SCULPT'),
        vr_ui.button("mode_model", "Model", lambda: set_mode(context, 'MODEL'), active=mode == 'MODEL'),
        vr_ui.button("mode_draw", "Draw", lambda: set_mode(context, 'DRAW'), active=mode == 'DRAW'),
    ]]
    if mode == 'SCULPT':
        out += _sculpt_rows(context, settings)
    elif mode == 'DRAW':
        out += _draw_rows(context, settings)
    else:
        out += _model_rows(context, settings)
    return out


def _cursor_to_left_hand(context):
    """Put the 3D cursor (the mirror center) at the left controller's tip."""
    ss = context.window_manager.xr_session_state
    if ss is None:
        return
    from . import operators
    pose = operators.controller_pose(ss.controller_aim_location_get(context, 0),
                                     ss.controller_aim_rotation_get(context, 0))
    tip = operators.brush_tip(pose, context.scene.vr_sculpt.tip_offset * ss.viewer_scale)
    context.scene.cursor.location = tip


def _draw_rows(context, settings):
    def choice(prefix, prop, items):
        return [vr_ui.button(f"{prefix}_{ident}", name, (lambda i=ident: setattr(settings, prop, i)),
                             active=getattr(settings, prop) == ident) for ident, name in items]

    out = [
        choice("tool", "draw_tool", (('DRAW', "Draw"), ('EDIT', "Edit"), ('ERASE', "Erase"))),
        choice("style", "draw_style", (('TUBE', "Tube"), ('RIBBON', "Ribbon"), ('NOTE', "Note"))),
    ]
    swatches = [vr_ui.button("color_" + ident, "", (lambda i=ident: setattr(settings, "draw_color", i)),
                             active=settings.draw_color == ident, swatch=rgb)
                for ident, _name, rgb in strokes.PALETTE]
    out += [swatches[:4], swatches[4:]]
    out.append([vr_ui.slider("draw_width", "Width", settings.draw_width, 0.001, 0.05,
                             lambda v: setattr(settings, "draw_width", v), fmt=_meters)])
    out.append([vr_ui.slider("draw_smoothing", "Smoothing", settings.draw_smoothing, 0.0, 0.95,
                             lambda v: setattr(settings, "draw_smoothing", v), fmt=lambda v: f"{v:.2f}")])
    out.append([
        vr_ui.label("Mirror:"),
        vr_ui.button("mirror_x", "X", lambda: setattr(settings, "mirror_x", not settings.mirror_x),
                     active=settings.mirror_x),
        vr_ui.button("mirror_y", "Y", lambda: setattr(settings, "mirror_y", not settings.mirror_y),
                     active=settings.mirror_y),
        vr_ui.button("mirror_z", "Z", lambda: setattr(settings, "mirror_z", not settings.mirror_z),
                     active=settings.mirror_z),
        vr_ui.button("mirror_here", "Here", lambda: _cursor_to_left_hand(context)),
    ])
    items = [(i.identifier, i.name) for i in settings.bl_rna.properties["draw_thickness"].enum_items]
    names = dict(items)
    out.append([vr_ui.select(
        "draw_thickness", "Thickness", names[settings.draw_thickness],
        lambda: open_picker("Thickness", [Option(i, n) for i, n in items], settings.draw_thickness,
                            lambda v: setattr(settings, "draw_thickness", v)))])
    if settings.draw_thickness == 'TAPER':
        out.append([vr_ui.slider("draw_taper", "Taper length", settings.draw_taper, 0.05, 0.5,
                                 lambda v: setattr(settings, "draw_taper", v), fmt=lambda v: f"{v * 100:.0f}%")])
    out.append([vr_ui.button("undo_stroke", "Undo stroke", lambda: _undo_stroke(context), danger=True)])
    return out


def _undo_stroke(context):
    from . import operators
    operators.undo_last_stroke(context)


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


def open_add_picker(context):
    settings = context.scene.vr_sculpt
    options = [Option(e.key, e.label, e.category or "Mesh") for e in catalog.entries(refresh=True)]
    open_picker("Add primitive", options, settings.primitive, lambda key: setattr(settings, "primitive", key))


# Settings page per object, for operators with many settings.
PARAMS_PER_PAGE = 6
_param_pages = {}


def _param_widget(context, obj, spec):
    wid = "param_" + spec.id

    def set_value(v, ident=spec.id):
        # Slow generators (e.g. Wall Factory) regenerate when the slider is released.
        live = not (spec.type in {'INT', 'FLOAT'} and obj.get("vrs_slow", False))
        generic.set_param(context, obj, ident, v, live=live)

    if spec.type == 'ENUM':
        names = dict(spec.items)
        return vr_ui.select(wid, spec.label, names.get(spec.value, str(spec.value)),
                            lambda: open_picker(spec.label, [Option(i, n) for i, n in spec.items],
                                                spec.value, set_value))
    if spec.type == 'BOOLEAN':
        return vr_ui.button(wid, f"{spec.label}: {'On' if spec.value else 'Off'}",
                            lambda: set_value(not spec.value), active=spec.value)
    if spec.type == 'INT':
        return vr_ui.slider(wid, spec.label, spec.value, spec.lo, spec.hi, set_value, integer=True)
    fmt = _meters if spec.length else _degrees if spec.angle else _number
    return vr_ui.slider(wid, spec.label, spec.value, spec.lo, spec.hi, set_value, fmt=fmt)


def _operator_rows(context, obj):
    entry = catalog.find(obj.vr_prim.op_key)
    if entry is None:
        return [[vr_ui.label("Its Add > Mesh entry is gone (extension off?)")]]
    specs = generic.params(entry, obj)
    if not specs:
        return [[vr_ui.label("No settings")]]
    key = obj.name
    shown, page, pages = _paged(specs, _param_pages.get(key, 0), PARAMS_PER_PAGE)
    _param_pages[key] = page
    out = [[_param_widget(context, obj, s)] for s in shown]
    if pages > 1:
        out.append([
            vr_ui.button("params_prev", "\u2039 Prev", lambda: _param_pages.__setitem__(key, page - 1))
            if page > 0 else vr_ui.label(""),
            vr_ui.label(f"     {page + 1} / {pages}"),
            vr_ui.button("params_next", "Next \u203a", lambda: _param_pages.__setitem__(key, page + 1))
            if page < pages - 1 else vr_ui.label(""),
        ])
    return out


def _model_rows(context, settings):
    out = [[vr_ui.select("add", "Add", primitives.label(settings.primitive), lambda: open_add_picker(context))]]
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
    elif prim is not None and prim.kind == 'OPERATOR':
        out += _operator_rows(context, obj)
    else:
        out.append([vr_ui.label("Plain mesh (sculpted or not a VR primitive)")])
    out.append([
        vr_ui.button("duplicate", "Duplicate", lambda: primitives.duplicate(context, obj)),
        vr_ui.button("delete", "Delete", lambda: primitives.delete(context, obj), danger=True),
    ])
    return out
