"""What the hand panel shows in each mode, and the mode/engine switches behind it."""

import math

import bpy

from . import (anim, camera, catalog, desktop, generic, measure, native, paint, pose, primitives, puppet, strokes,
               symmetry, vr_ui)
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
    """Switch a mesh to 'OBJECT' or 'SCULPT' mode, or an armature to 'OBJECT' or 'POSE' mode, from any
    context (XR operators have no area)."""
    if obj is None or obj.mode == mode:
        return
    if obj.type != 'MESH' and not (obj.type == 'ARMATURE' and mode in {'OBJECT', 'POSE'}):
        return
    window, view = _desktop_view(context)
    if view is None:
        if bpy.app.background:  # Tests: no window, and none needed.
            with context.temp_override(active_object=obj, object=obj):
                bpy.ops.object.mode_set(mode=mode)
        return
    with context.temp_override(window=window, area=view[0], region=view[1], active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode=mode)


def set_mode(context, mode):
    settings = context.scene.vr_sculpt
    if settings.mode == 'POSE' and mode != 'POSE':
        pose.leave(context)
    settings.mode = mode
    obj = context.view_layer.objects.active
    if mode in {'MODEL', 'DRAW', 'TOOLS'}:
        set_object_mode(context, obj, 'OBJECT')
    elif mode == 'PAINT':
        paint.enter(context)
    elif mode == 'POSE':
        if not anim.posing(obj):
            pose.enter(context)
    elif settings.engine == 'NATIVE':
        set_object_mode(context, obj, 'SCULPT')
    else:
        set_object_mode(context, obj, 'OBJECT')  # E.g. coming back from Texture Paint.


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

MODE_TABS = (('SCULPT', "Sculpt"), ('MODEL', "Model"), ('DRAW', "Draw"), ('PAINT', "Paint"), ('POSE', "Pose"),
             ('TOOLS', "Tools"))


def rows(context):
    """Rows of widgets for the current mode (or the open picker)."""
    if _picker is not None:
        return _picker_rows()
    settings = context.scene.vr_sculpt
    mode = settings.mode
    tabs = [vr_ui.button("mode_" + ident.lower(), name, (lambda i=ident: set_mode(context, i)), active=mode == ident)
            for ident, name in MODE_TABS]
    out = [tabs[:3], tabs[3:]]
    if mode == 'SCULPT':
        out += _sculpt_rows(context, settings)
    elif mode == 'DRAW':
        out += _draw_rows(context, settings)
    elif mode == 'TOOLS':
        out += _tools_rows(context, settings)
    elif mode == 'PAINT':
        out += _paint_rows(context, settings)
    elif mode == 'POSE':
        out += _pose_rows(context, settings)
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
    symmetry.move_center(context, tip)


def _toggle(settings, prop):
    return lambda: setattr(settings, prop, not getattr(settings, prop))


def _grid_rows(settings):
    out = [[
        vr_ui.button("grid_show", "Grid", _toggle(settings, "grid_show"), active=settings.grid_show),
        vr_ui.button("grid_snap", "Snap to grid", _toggle(settings, "grid_snap"), active=settings.grid_snap),
    ]]
    if settings.grid_show or settings.grid_snap:
        out.append([vr_ui.slider("grid_size", "Grid size", settings.grid_size, 0.005, 0.5,
                                 lambda v: setattr(settings, "grid_size", v), fmt=_meters)])
    return out


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
    else:
        strength = native.get_strength(context)
        if strength is not None:
            out.append([vr_ui.slider("strength", "Strength", strength, 0.0, 1.0,
                                     lambda v: native.set_strength(context, v), fmt=lambda v: f"{v:.2f}")])
    axes = symmetry.sculpt_axes(context)
    out.append([vr_ui.label("Symmetry:")] + [
        vr_ui.button("sym_" + a, a, (lambda i=i: symmetry.sculpt_toggle(context, i)), active=axes[i])
        for i, a in enumerate(symmetry.AXES)])
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
    out = [[vr_ui.select("add", "Add", primitives.label(settings.primitive), lambda: open_add_picker(context))]] + _grid_rows(settings)
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
    if obj.type == 'MESH':
        axes = symmetry.model_axes(obj)
        out.append([vr_ui.label("Mirror:")] + [
            vr_ui.button("mirror_" + a, a, (lambda i=i: symmetry.model_toggle(context, obj, i)), active=axes[i])
            for i, a in enumerate(symmetry.AXES)] + [
            vr_ui.button("mirror_here", "Here", lambda: _cursor_to_left_hand(context))])
    out.append([
        vr_ui.button("duplicate", "Duplicate", lambda: primitives.duplicate(context, obj)),
        vr_ui.button("delete", "Delete", lambda: primitives.delete(context, obj), danger=True),
    ])
    return out


def _passthrough(context):
    xs = context.window_manager.xr_session_settings
    xs.show_passthrough = not xs.show_passthrough


def _show_desktop(context, settings):
    settings.show_desktop = not settings.show_desktop
    if settings.show_desktop:
        desktop.request_full_capture(context)
        ss = context.window_manager.xr_session_state
        if ss is not None:
            desktop.place_in_front(context, ss)


def _bring_desktop(context):
    ss = context.window_manager.xr_session_state
    if ss is not None:
        desktop.place_in_front(context, ss)


def open_camera_picker(context):
    scene = context.scene
    options = [Option(o.name, o.name) for o in camera.cameras(scene)]
    current = scene.camera.name if scene.camera else ""

    def choose(name):
        obj = scene.objects.get(name)
        if obj is not None:
            scene.camera = obj

    open_picker("Scene camera", options, current, choose)


def _new_camera_here(context):
    ss = context.window_manager.xr_session_state
    pose = None
    if ss is not None:
        from . import operators
        pose = operators.controller_pose(ss.controller_aim_location_get(context, 1),
                                         ss.controller_aim_rotation_get(context, 1))
    camera.new_camera(context, pose)


def _tools_rows(context, settings):
    scene = context.scene
    out = [[
        vr_ui.button("tool_measure", "Measure", lambda: setattr(settings, "tool", 'MEASURE'),
                     active=settings.tool == 'MEASURE'),
        vr_ui.button("tool_camera", "Camera", lambda: setattr(settings, "tool", 'CAMERA'),
                     active=settings.tool == 'CAMERA'),
    ]]
    if settings.tool == 'MEASURE':
        n = len(measure.stored(scene))
        out.append([
            vr_ui.button("measures_show", "Show", _toggle(settings, "show_measures"), active=settings.show_measures),
            vr_ui.button("measure_undo", "Undo", lambda: measure.undo_last(scene)),
            vr_ui.button("measure_clear", f"Clear ({n})", lambda: measure.clear(scene), danger=True),
        ])
    else:
        cam = scene.camera
        out.append([vr_ui.select("camera", "Camera", cam.name if cam else "none",
                                 lambda: open_camera_picker(context))])
        row = [vr_ui.button("camera_preview", "Pin preview", _toggle(settings, "camera_preview"),
                            active=settings.camera_preview)]
        if settings.show_new_camera:
            row.insert(0, vr_ui.button("camera_new", "New here", lambda: _new_camera_here(context)))
        out.append(row)
        if cam is not None and cam.data.type == 'PERSP':
            out.append([vr_ui.slider("camera_lens", "Lens", cam.data.lens, 10.0, 200.0,
                                     lambda v: setattr(cam.data, "lens", v), fmt=lambda v: f"{v:.0f} mm")])
    out += _grid_rows(settings)
    xs = context.window_manager.xr_session_settings
    out.append([
        vr_ui.button("show_desktop", "Desktop", lambda: _show_desktop(context, settings),
                     active=settings.show_desktop),
        vr_ui.button("bring_desktop", "Bring here", lambda: _bring_desktop(context))
        if settings.show_desktop else vr_ui.label(""),
        vr_ui.button("passthrough", "Passthrough", lambda: _passthrough(context),
                     active=bool(xs and xs.show_passthrough)),
    ])
    out.append([
        vr_ui.button("tracking", "Tracking", _toggle(settings, "tracking"), active=settings.tracking),
        vr_ui.button("tracking_record", "Record", _toggle(settings, "tracking_record"),
                     active=settings.tracking_record),
    ])
    out.append([
        vr_ui.label("Move:"),
        vr_ui.button("nav_walk", "Walk", lambda: setattr(settings, "nav_mode", 'WALK'),
                     active=settings.nav_mode == 'WALK'),
        vr_ui.button("nav_fly", "Fly", lambda: setattr(settings, "nav_mode", 'FLY'),
                     active=settings.nav_mode == 'FLY'),
    ])
    out.append([vr_ui.slider("nav_speed", "Speed", settings.nav_speed, 0.2, 6.0,
                             lambda v: setattr(settings, "nav_speed", v), fmt=lambda v: f"{v:.1f} m/s")])
    return out


def _paint_setup(context):
    obj = paint.target(context)
    if obj is not None:
        paint.setup(context, obj, int(context.scene.vr_sculpt.paint_texture_size))


def _paint_rows(context, settings):
    obj = paint.target(context)
    if obj is None:
        return [[vr_ui.label("Select a mesh to paint")]]
    if not paint.ready(obj):
        items = [(i.identifier, i.name) for i in settings.bl_rna.properties["paint_texture_size"].enum_items]
        return [
            [vr_ui.label(f"{obj.name} needs UVs and an image")],
            [vr_ui.button("paint_size_" + ident, name, (lambda i=ident: setattr(settings, "paint_texture_size", i)),
                          active=settings.paint_texture_size == ident) for ident, name in items],
            [vr_ui.button("paint_setup", "Set up for painting", lambda: _paint_setup(context))],
        ]
    b = paint.brush(context)
    current = b.name if b else ""
    cells = [vr_ui.button("pbrush_" + n, paint.SHORT[n], (lambda n=n: paint.activate_brush(context, n)),
                          active=n == current) for n in paint.BRUSHES]
    out = [cells[:3], cells[3:]]
    out.append([
        vr_ui.button("reach_contact", "Contact", lambda: setattr(settings, "paint_reach", 'CONTACT'),
                     active=settings.paint_reach == 'CONTACT'),
        vr_ui.button("reach_spray", "Spray", lambda: setattr(settings, "paint_reach", 'SPRAY'),
                     active=settings.paint_reach == 'SPRAY'),
        vr_ui.button("paint_pick", "Pick color",
                     lambda: setattr(settings, "paint_tool", 'PAINT' if settings.paint_tool == 'PICK' else 'PICK'),
                     active=settings.paint_tool == 'PICK'),
    ])
    color = paint.get_color(context)
    swatches = [vr_ui.button("pcolor_" + ident, "", (lambda rgb=rgb: paint.set_color(context, rgb)),
                             active=all(abs(a - b) < 1e-3 for a, b in zip(color, rgb)), swatch=rgb)
                for ident, _name, rgb in strokes.PALETTE]
    out += [swatches[:4], swatches[4:]]
    h, s, v = paint.get_hsv(context)
    out.append([vr_ui.slider("paint_hue", "Hue", h, 0.0, 1.0, lambda x: paint.set_hsv(context, h=x),
                             fmt=lambda x: f"{x * 360:.0f}" + chr(0xB0))])  # degrees
    out.append([vr_ui.slider("paint_sat", "Saturation", s, 0.0, 1.0, lambda x: paint.set_hsv(context, s=x),
                             fmt=lambda x: f"{x * 100:.0f}%")])
    out.append([vr_ui.slider("paint_val", "Value", v, 0.0, 1.0, lambda x: paint.set_hsv(context, v=x),
                             fmt=lambda x: f"{x * 100:.0f}%")])
    out.append([
        vr_ui.button("paint_color1", "Color", lambda: None, swatch=color, active=True),
        vr_ui.button("paint_color2", "2nd", lambda: None, swatch=paint.get_color(context, True)),
        vr_ui.button("paint_swap", "Swap", lambda: paint.swap_colors(context)),
    ])
    out.append([vr_ui.slider("paint_radius", "Radius", settings.paint_radius, 0.002, 0.15,
                             lambda x: setattr(settings, "paint_radius", x), fmt=_meters)])
    if b is not None:
        out.append([vr_ui.slider("paint_strength", "Strength", b.strength, 0.0, 1.0,
                                 lambda x: setattr(b, "strength", x), fmt=lambda x: f"{x:.2f}")])
    out.append([vr_ui.button("paint_undo", "Undo stroke", lambda: paint.undo_last(context), danger=True)])
    return out


def open_rig_picker(context):
    current = pose.rig(context)
    options = [Option(o.name, o.name) for o in pose.rigs(context)] + [Option("", "Objects (no rig)")]
    open_picker("Pose", options, current.name if current else "", lambda name: pose.choose_rig(context, name))


def _bind(context, source):
    puppet.bind(context, context.window_manager.xr_session_state, source)


def _pose_rows(context, settings):
    obj = pose.rig(context)
    out = [[vr_ui.select("pose_rig", "Rig", obj.name if obj else "Objects", lambda: open_rig_picker(context))]]
    if obj is not None:
        pb = pose.active_bone(obj)
        out.append([vr_ui.label(f"Bone: {pb.name}" if pb is not None else "Touch a bone to pose it")])
        out.append([
            vr_ui.button("pose_fk", "FK", lambda: setattr(settings, "pose_tool", 'FK'),
                         active=settings.pose_tool == 'FK'),
            vr_ui.button("pose_ik", "IK", lambda: setattr(settings, "pose_tool", 'IK'),
                         active=settings.pose_tool == 'IK'),
            vr_ui.button("pose_mirror", "Mirror X", _toggle(settings, "pose_mirror"), active=settings.pose_mirror),
            vr_ui.button("pose_bones", "Bones", _toggle(settings, "pose_show_bones"),
                         active=settings.pose_show_bones),
        ])
        if settings.pose_tool == 'IK':
            out.append([vr_ui.slider("pose_chain", "IK chain", settings.pose_chain, 1, 6,
                                     lambda v: setattr(settings, "pose_chain", int(v)), integer=True)])
        out.append([
            vr_ui.button("pose_reset", "Reset bone", lambda: pose.reset_selected(context)),
            vr_ui.button("pose_reset_all", "Reset pose", lambda: pose.reset_selected(context, True), danger=True),
        ])
    else:
        out.append([vr_ui.label("Touch an object to move it")])
    out.append([vr_ui.button("bind_" + ident.lower(), "Bind " + short, (lambda i=ident: _bind(context, i)))
                for ident, short in (('LEFT', "L"), ('RIGHT', "R"), ('HEAD', "head"))])
    n = puppet.binding_count()
    recording = puppet.state() in {'RECORDING', 'ENDING'}
    out.append([
        vr_ui.button("unbind", f"Unbind ({n})", puppet.unbind_all, danger=True) if n else vr_ui.label("Puppet:"),
        vr_ui.button("puppet_rec", puppet.status(), lambda: puppet.toggle_record(context),
                     active=puppet.state() is not None, danger=recording),
    ])
    return out


def timeline_rows(context, settings):
    """Frame scrubber with key ticks, transport, keying and range buttons (at the panel's bottom)."""
    scene = context.scene
    start, end = scene.frame_start, scene.frame_end
    span = max(end - start, 1)
    keys = anim.key_frames(anim.selection(context))
    ticks = [(f - start) / span for f in keys if start <= f <= end]
    playing = anim.is_playing(context)
    interp = anim.interpolation_at(context)
    names = dict(anim.INTERPOLATIONS)
    tool = scene.tool_settings
    return [
        [vr_ui.slider("frame", "Frame", scene.frame_current, start, end,
                      lambda v: anim.set_frame(context, v), integer=True,
                      fmt=lambda v: f"{int(v)} / {end}", ticks=ticks)],
        [
            vr_ui.button("key_prev", "|<", lambda: anim.jump_key(context, -1)),
            vr_ui.button("frame_prev", "<", lambda: anim.step(context, -1)),
            vr_ui.button("play", "Stop" if playing else "Play", lambda: anim.toggle_play(context), active=playing),
            vr_ui.button("frame_next", ">", lambda: anim.step(context, 1)),
            vr_ui.button("key_next", ">|", lambda: anim.jump_key(context, 1)),
        ],
        [
            vr_ui.button("key_insert", "Key", lambda: anim.key_selection(context)),
            vr_ui.button("key_delete", "Del key", lambda: anim.delete_selection_keys(context), danger=True),
            vr_ui.button("autokey", "Auto", lambda: setattr(tool, "use_keyframe_insert_auto",
                                                              not tool.use_keyframe_insert_auto),
                         active=tool.use_keyframe_insert_auto),
            vr_ui.select("interp", "Ease", names.get(interp, "-") if interp != 'MIXED' else "Mixed",
                         lambda: open_picker("Key interpolation", [Option(i, n) for i, n in anim.INTERPOLATIONS],
                                             interp, lambda v: anim.set_interpolation(context, v))),
        ],
        [
            vr_ui.button("range_start", "Start", lambda: anim.set_start(context)),
            vr_ui.button("range_end", "End", lambda: anim.set_end(context)),
            vr_ui.button("show_paths", "Paths", _toggle(settings, "show_paths"), active=settings.show_paths),
            vr_ui.button("onion", "Onion", _toggle(settings, "onion"), active=settings.onion),
        ],
    ]
