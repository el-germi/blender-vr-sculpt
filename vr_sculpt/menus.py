"""What the hand panel shows, and the mode/engine switches behind it.

The panel is an icon rail (the eight modes) beside the content. The content is:
  - a header: the mode's name, its context, and icon actions (undo where there is one, Settings, Pin);
  - the mode's 3-5 most used controls;
  - a More row that opens the rest (it opens upward, so the row stays under the pointer).
Second levels replace the content, with a Back button in the header: the selected object's
settings (Model), the Kit of parts (Model), Settings (from the gear, in every mode) and pickers
(from any select).
"""

import math

import bpy

from . import (actions, anim, camera, catalog, desktop, generic, hardsurface, hud, kit, measure, native, paint,
               physics, pose, presence, primitives, puppet, rest, rig, shading, strokes, symmetry, uv, vr_ui)
from .props import BRUSH_ITEMS

NATIVE_SHORT = {
    "Draw": "Draw", "Clay Strips": "Clay", "Smooth": "Smooth",
    "Inflate/Deflate": "Inflate", "Crease Sharp": "Crease", "Flatten/Contrast": "Flatten",
}

MODES = (('SCULPT', "Sculpt", "sculpt"), ('MODEL', "Model", "model"), ('DRAW', "Draw", "draw"),
         ('PAINT', "Paint", "paint"), ('RIG', "Rig", "rig"), ('POSE', "Pose", "pose"),
         ('PHYSICS', "Physics", "physics"), ('TOOLS', "Tools", "tools"))
MODE_ICONS = {ident: icon for ident, _name, icon in MODES}

LABEL_W = 0.034  # row-leading labels ("Symmetry", "Mirror"): one column, so the controls line up


def _desktop_view(context):
    windows = context.window_manager.windows
    window = context.window or (windows[0] if len(windows) else None)
    if window is None:
        return None, None
    return window, native.largest_view3d(window)


def set_object_mode(context, obj, mode):
    """Switch a mesh to 'OBJECT' or 'SCULPT' mode, or an armature to 'OBJECT', 'POSE' or 'EDIT' mode, from
    any context (XR operators have no area)."""
    if obj is None or obj.mode == mode:
        return
    if obj.type != 'MESH' and not (obj.type == 'ARMATURE' and mode in {'OBJECT', 'POSE', 'EDIT'}):
        return
    if mode == 'SCULPT' and obj.type == 'MESH' and obj.data.get(rest.DYNTOPO):
        rest.prepare(obj)  # Blender turns dyntopo back on as it enters: keep UVs and shape keys first.
    window, view = _desktop_view(context)
    if view is None:
        if bpy.app.background:  # Tests: no window, and none needed.
            with context.temp_override(active_object=obj, object=obj):
                bpy.ops.object.mode_set(mode=mode)
            rest.sync(obj)
        return
    with context.temp_override(window=window, area=view[0], region=view[1], active_object=obj, object=obj):
        bpy.ops.object.mode_set(mode=mode)
    rest.sync(obj)  # Leaving a dyntopo session gives back what it reset.


def set_mode(context, mode):
    settings = context.scene.vr_sculpt
    if settings.mode == 'POSE' and mode != 'POSE':
        pose.leave(context)
    if settings.mode == 'RIG' and mode != 'RIG':
        rig.leave(context)
    close_picker()
    close_sheet(context)
    settings.mode = mode
    settings.model_mark = 'NONE'
    settings.uv_tool = 'NONE'
    obj = context.view_layer.objects.active
    if mode in {'MODEL', 'DRAW', 'TOOLS', 'PHYSICS'}:
        set_object_mode(context, obj, 'OBJECT')
    elif mode == 'PAINT':
        paint.enter(context)
    elif mode == 'POSE':
        if not anim.posing(obj):
            pose.enter(context)
    elif mode == 'RIG':
        rig.enter(context)
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


def _percent(v):
    return f"{v * 100:.0f}%"


def _toggle(settings, prop):
    return lambda: setattr(settings, prop, not getattr(settings, prop))


# --- panel state: sheets, disclosure, picker ------------------------------------

_sheet = None  # None, 'OBJECT' (the selected object's settings), 'KIT', 'SIMULATION' (Physics) or 'SETTINGS'
_open = set()  # disclosure keys whose More is open


def open_sheet(name):
    global _sheet
    close_picker()
    _sheet = name


def close_sheet(context=None):
    global _sheet
    if _sheet == 'OBJECT' and context is not None:
        # Their buttons are in the sheet; don't leave them on unseen.
        context.scene.vr_sculpt.model_mark = 'NONE'
        context.scene.vr_sculpt.uv_tool = 'NONE'
    _sheet = None


def sheet():
    return _sheet


def more_open(key):
    return key in _open


def set_more(key, is_open):
    if is_open:
        _open.add(key)
    else:
        _open.discard(key)


def _disclose(key, rows, more, hint=""):
    """rows plus, when there's more, the More row; open, the extra rows go between the two."""
    if not more:
        return rows
    is_open = key in _open
    toggle = [vr_ui.more("more" if key != 'TIMELINE' else "timeline_more", is_open,
                         lambda: set_more(key, key not in _open), hint)]
    return rows + (more if is_open else []) + [toggle]


def reset():
    """Forget the panel's navigation state (sheets, open More rows, picker)."""
    global _sheet
    _sheet = None
    _open.clear()
    _stack_open.clear()
    _stack_pages.clear()
    close_picker()


# --- picker (the open state of a select widget) -----------------------------

PICKER_COLS = 3
PICKER_ROWS = 6

_picker = None


class Option:
    def __init__(self, value, label, category=""):
        self.value, self.label, self.category = value, label, category


def open_picker(title, options, current, on_select):
    """Replace the panel's content with a list to choose from. options: list of Option."""
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


def _pager(prefix, page, pages, set_page):
    return [
        vr_ui.icon_button(prefix + "_prev", "back", lambda: set_page(page - 1), "Previous page")
        if page > 0 else vr_ui.label("", fixed=vr_ui.ROW_H),
        vr_ui.label(f"{page + 1} / {pages}", align='CENTER'),
        vr_ui.icon_button(prefix + "_next", "chevron_right", lambda: set_page(page + 1), "Next page")
        if page < pages - 1 else vr_ui.label("", fixed=vr_ui.ROW_H),
    ]


def _picker_rows():
    p = _picker
    in_category = p["category"] is not None and len(p["categories"]) > 1
    out = [[vr_ui.icon_button("picker_back", "back", _back, "Back"),
            vr_ui.title(p["category"] if in_category else p["title"], p["title"] if in_category else "")]]
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
        shown, page, pages = _paged(cells, p["page"], PICKER_COLS * PICKER_ROWS)
        out += _grid(shown, PICKER_COLS)
    if pages > 1:
        out.append(_pager("picker", page, pages, _set_page))
    return out


# --- the panel ------------------------------------------------------------------

def panel(context):
    """The hand panel: the mode rail and the content for the current mode, sheet or picker."""
    settings = context.scene.vr_sculpt
    if _sheet == 'OBJECT' and (settings.mode != 'MODEL' or _selected(context) is None):
        close_sheet(context)  # The object went away (deleted, deselected) or the mode changed.
    if _sheet == 'KIT' and settings.mode != 'MODEL':
        close_sheet(context)
    if _sheet == 'SIMULATION' and settings.mode != 'PHYSICS':
        close_sheet(context)
    if _picker is not None:
        content = _picker_rows()
    elif _sheet == 'SETTINGS':
        content = _settings_rows(context, settings)
    elif _sheet == 'KIT':
        content = _kit_rows(context, settings)
    elif _sheet == 'SIMULATION':
        content = _simulation_rows(context, settings)
    elif _sheet == 'OBJECT':
        content = _object_rows(context, settings, _selected(context))
    else:
        content = _MODE_ROWS.get(settings.mode, _model_rows)(context, settings)
    return vr_ui.Panel(_rail(context, settings), content)


def layout(context):
    """(widgets, height) of the hand panel, laid out."""
    return vr_ui.layout_panel(panel(context))


def rows(context):
    """The content rows (no rail)."""
    return panel(context).rows


def _rail(context, settings):
    return [vr_ui.rail("mode_" + ident.lower(), icon, (lambda i=ident: _choose_mode(context, i)),
                       settings.mode == ident, name) for ident, name, icon in MODES]


def _choose_mode(context, mode):
    if mode == context.scene.vr_sculpt.mode:
        close_picker()
        close_sheet(context)  # The current mode's icon: back to its first level.
    else:
        set_mode(context, mode)


def _header(context, settings, text, subtitle="", actions=()):
    return [vr_ui.title(text, subtitle)] + list(actions) + [
        vr_ui.icon_button("settings", "gear", lambda: open_sheet('SETTINGS'), "Settings"),
        _pin(context, settings)]


def _pin(context, settings):
    return vr_ui.icon_button("pin", "pinned" if settings.panel_pin else "pin", lambda: presence.toggle_pin(context),
                             "Unpin" if settings.panel_pin else "Keep open", active=settings.panel_pin)


def _segments(prefix, group, items, current, choose, icons=None, dots=None):
    """A segmented control: items (value, label); ids prefix + value."""
    return [vr_ui.segment(prefix + str(value), text, (lambda v=value: choose(v)), current == value, group,
                          icon=(icons or {}).get(value), dot=(dots or {}).get(value))
            for value, text in items]


def _axes_row(text, prefix, states, toggle, here=None):
    """'Mirror  X Y Z (here)': axis toggles after a leading label."""
    row = [vr_ui.label(text, fixed=LABEL_W)] + [
        vr_ui.toggle(prefix + a, a, states[i], (lambda i=i: toggle(i))) for i, a in enumerate(symmetry.AXES)]
    if here is not None:
        row.append(vr_ui.icon_button("mirror_here", "here", here, "Center at other hand"))
    return row


# --- Sculpt ---------------------------------------------------------------------

def _sculpt_rows(context, settings):
    is_native = settings.engine == 'NATIVE'
    dyntopo = is_native and native.dyntopo_enabled(context)
    subtitle = (native.message() or ("Native, dyntopo" if dyntopo else "Native brushes")) if is_native else ""
    out = [_header(context, settings, "Sculpt", subtitle)]
    if is_native:
        brush = native.sculpt_brush(context)
        current = brush.name if brush else ""
        cells = [vr_ui.segment("brush_" + n, NATIVE_SHORT[n], (lambda n=n: native.activate_brush(context, n)),
                               n == current, "brushes_a" if i < 3 else "brushes_b", dot=hud.NATIVE_COLORS.get(n))
                 for i, n in enumerate(native.NATIVE_BRUSHES)]
        out += [cells[:3], cells[3:]]
    else:
        out.append(_segments("brush_", "brushes", [(i, n) for i, n, _d in BRUSH_ITEMS], settings.brush,
                             lambda v: setattr(settings, "brush", v), dots=hud.PYTHON_COLORS))
    out.append([vr_ui.slider("radius", "Radius", settings.radius, 0.005, 0.2,
                             lambda v: setattr(settings, "radius", v), fmt=_meters)])
    if not is_native:
        out.append([vr_ui.slider("strength", "Strength", settings.strength, 0.0, 1.0,
                                 lambda v: setattr(settings, "strength", v), fmt=_percent)])
    else:
        strength = native.get_strength(context)
        if strength is not None:
            out.append([vr_ui.slider("strength", "Strength", strength, 0.0, 1.0,
                                     lambda v: native.set_strength(context, v), fmt=_percent)])
    out.append(_axes_row("Symmetry", "sym_", symmetry.sculpt_axes(context),
                         lambda i: symmetry.sculpt_toggle(context, i)))
    more = _dyntopo_rows(context) if is_native else []
    more += [
        [vr_ui.label("Engine", fixed=LABEL_W)] + _segments(
            "engine_", "engine", (('python', "Python"), ('native', "Native")), settings.engine.lower(),
            lambda v: set_engine(context, v.upper())),
        [vr_ui.toggle("left_inverts", "Other hand inverts", settings.left_inverts, _toggle(settings, "left_inverts"),
                      icon="swap")],
    ]
    return _disclose('SCULPT', out, more, "Dyntopo, engine, other hand" if is_native else "Engine, other hand")


def _dyntopo_rows(context):
    """Blender's dynamic topology (native engine): the toggle, then detail and refine while it's on."""
    on = native.dyntopo_enabled(context)
    if not on:
        return [[vr_ui.toggle("dyntopo", "Dyntopo", on, lambda: native.toggle_dyntopo(context), icon="grid")]]
    sculpt = context.tool_settings.sculpt
    method = sculpt.detail_type_method
    # The detail methods share the toggle's row (no room for its icon there), so the panel still fits.
    toggle = vr_ui.toggle("dyntopo", "Dyntopo", on, lambda: native.toggle_dyntopo(context))
    rows = [[toggle] + _segments(
        "detail_", "detail", [(m.lower(), n) for m, n in native.DETAIL_METHODS], method.lower(),
        lambda v: setattr(sculpt, "detail_type_method", v.upper()))]
    value = native.detail_value(context)
    # Strokes leave the patches they touch untextured until the session ends; these show it now.
    settings = context.scene.vr_sculpt
    texture = [
        vr_ui.icon_button("dyntopo_texture", "shading", lambda: native.refresh_texture(context), "Show texture"),
        vr_ui.toggle("dyntopo_auto_texture", "Auto", settings.dyntopo_auto_texture,
                     _toggle(settings, "dyntopo_auto_texture"), tip="Show the texture 2 s after a stroke"),
    ] if native.texture_restorable(context) else []
    if method == 'BRUSH':
        rows.append([vr_ui.slider("detail_size", "Edge", value, 0.05, 1.0,
                                  lambda v: native.set_detail_value(context, v), fmt=_percent, weight=3.0)]
                    + texture)
    elif value is not None:
        rows.append([vr_ui.slider("detail_size", "Edge", min(max(value, native.DETAIL_EDGE_MIN),
                                                             native.DETAIL_EDGE_MAX),
                                  native.DETAIL_EDGE_MIN, native.DETAIL_EDGE_MAX,
                                  lambda v: native.set_detail_value(context, v), fmt=_meters, weight=3.0),
                     vr_ui.button("detail_fill", "Fill", lambda: native.flood_fill(context))] + texture)
    else:
        rows.append([vr_ui.label("Detail follows the desktop view", weight=3.0, align='CENTER')] + texture)
    if method != 'MANUAL':
        rows.append([vr_ui.label("Refine", fixed=LABEL_W)] + _segments(
            "refine_", "refine", [(m.lower(), n) for m, n in native.REFINE_METHODS],
            sculpt.detail_refine_method.lower(), lambda v: setattr(sculpt, "detail_refine_method", v.upper())))
    return rows


# --- Model ----------------------------------------------------------------------

ADD_AS = (('OBJECT', "Object"), ('DIFFERENCE', "Cut"), ('UNION', "Union"), ('INTERSECT', "Intersect"))
MODEL_PAGES = (('SHAPE', "Shape"), ('BOOLEAN', "Cuts"), ('FINISH', "Finish"), ('ARRAY', "Array"),
               ('STACK', "Stack"), ('UV', "UV"))


def _selected(context):
    obj = context.view_layer.objects.active
    return obj if obj is not None and obj.select_get() else None


def open_add_picker(context):
    settings = context.scene.vr_sculpt
    options = [Option(e.key, e.label, e.category or "Mesh") for e in catalog.entries(refresh=True)]
    open_picker("Add primitive", options, settings.primitive, lambda key: setattr(settings, "primitive", key))


def _model_rows(context, settings):
    out = [
        _header(context, settings, "Model"),
        [vr_ui.select("add", "Add", primitives.label(settings.primitive), lambda: open_add_picker(context),
                      icon="kit" if kit.is_key(settings.primitive) else "model"),
         vr_ui.icon_button("kit", "kit", lambda: open_kit(context), "Kit parts")],
        _segments("add_as_", "add_as", [(i.lower(), n) for i, n in ADD_AS], settings.model_add_as.lower(),
                  lambda v: setattr(settings, "model_add_as", v.upper())),
    ]
    obj = _selected(context)
    if obj is None:
        out.append([vr_ui.label("Right: add. Left: select", align='CENTER')])
    else:
        cutter = hardsurface.is_cutter(obj)
        out.append([
            vr_ui.select("edit_object", obj.name, "", lambda: open_sheet('OBJECT'),
                         icon="cut" if cutter else "object", weight=3.0),
            vr_ui.icon_button("duplicate", "duplicate", lambda: hardsurface.duplicate(context, obj), "Duplicate"),
            vr_ui.icon_button("delete", "trash", lambda: hardsurface.delete(context, obj), "Delete", danger=True),
        ])
    return _disclose('MODEL', out, _snap_rows(settings), "Snapping")


# --- Kit (Model mode's library of parts) ---------------------------------------------

KIT_COLS = 4
KIT_ROWS = 3
ALL = "*"  # the catalog value for every part of a library

# What the Kit sheet shows: a library, one of its catalogs (None: all of them) and a page.
_kit = {"library": None, "catalog": None, "page": 0}


def open_kit(context):
    """Open the Kit sheet; the libraries are looked at again (changed files go to the worker)."""
    kit.scan(context)
    if kit.library(_kit["library"], context) is None:
        asset = kit.find(context.scene.vr_sculpt.primitive)
        libs = kit.cached(context)
        if asset is not None and kit.library(asset.library, context) is not None:
            _kit.update(library=asset.library, catalog=asset.catalog, page=0)
        else:
            first = next((lib for lib in libs if lib.assets), libs[0] if libs else None)
            _kit.update(library=first.name if first else None, catalog=None, page=0)
    open_sheet('KIT')


def choose_part(context, key):
    """Make key the part the trigger places. A cutter part switches New As from Object to Cut, and a
    plain part chosen after one switches it back."""
    settings = context.scene.vr_sculpt
    before = kit.find(settings.primitive)
    asset = kit.find(key)
    settings.primitive = key
    if asset is None:
        return
    if asset.cutter and settings.model_add_as == 'OBJECT':
        settings.model_add_as = 'DIFFERENCE'
    elif not asset.cutter and before is not None and before.cutter and settings.model_add_as == 'DIFFERENCE':
        settings.model_add_as = 'OBJECT'


def _set_kit_view(value):
    name, catalog = value.split("\n", 1)
    _kit.update(library=name, catalog=None if catalog == ALL else catalog, page=0)


def _kit_view_value(name, catalog):
    return f"{name}\n{ALL if catalog is None else catalog}"


def open_catalog_picker(context):
    """Libraries, then each one's catalogs."""
    options = []
    for lib in kit.cached(context):
        options.append(Option(_kit_view_value(lib.name, None), "All parts", lib.name))
        options += [Option(_kit_view_value(lib.name, cat), kit.catalog_label(cat), lib.name) for cat in lib.catalogs()]
    open_picker("Kit", options, _kit_view_value(_kit["library"], _kit["catalog"]), _set_kit_view)


def _add_to_kit(context):
    try:
        asset = kit.add_to_kit(context, _selected(context))
    except ValueError as ex:
        kit.say(str(ex))
        return
    if asset is not None:
        _kit.update(library=asset.library, catalog=asset.catalog, page=0)


def _set_kit_page(page):
    _kit["page"] = page


def _kit_rows(context, settings):
    """Second level of Model: the parts of a library catalog as thumbnails. Choosing one makes it what
    the trigger places, and the sheet stays open to drop it (or stamp it) right away."""
    lib = kit.library(_kit["library"], context)
    parts = lib.in_catalog(_kit["catalog"]) if lib is not None else []
    cells = [vr_ui.tile("part_" + a.key, a.name, (lambda k=a.key: choose_part(context, k)), image=kit.thumb(a),
                        icon="cut" if a.cutter else "kit", active=a.key == settings.primitive) for a in parts]
    shown, page, pages = _paged(cells, _kit["page"], KIT_COLS * KIT_ROWS)
    _kit["page"] = page
    where = "All parts" if _kit["catalog"] is None else kit.catalog_label(_kit["catalog"])
    listing = kit.pending()  # Parts come in as the background worker lists the libraries.
    out = [
        [vr_ui.icon_button("back", "back", lambda: close_sheet(context), "Back"),
         vr_ui.title("Kit", kit.message() or ("listing parts\u2026" if listing else lib.name if lib else "")),
         vr_ui.icon_button("kit_add", "kit_add", lambda: _add_to_kit(context), "Add selection to " + kit.MINE)],
        [vr_ui.select("kit_catalog", "In", where, lambda: open_catalog_picker(context), weight=3.0)] +
        (_pager("kit", page, pages, _set_kit_page) if pages > 1 else []),
    ]
    if shown:
        for row in _grid(shown, KIT_COLS):
            out.append(row + [vr_ui.label("") for _ in range(KIT_COLS - len(row))])
    elif listing:
        out.append([vr_ui.label("Listing parts\u2026", align='CENTER')])
    elif lib is not None and lib.name == kit.MINE:
        out.append([vr_ui.label("Select an object, then + to add it here", align='CENTER')])
    else:
        out.append([vr_ui.label("No parts here", align='CENTER')])
    out.append(_segments("add_as_", "add_as", [(i.lower(), n) for i, n in ADD_AS], settings.model_add_as.lower(),
                         lambda v: setattr(settings, "model_add_as", v.upper())))
    out.append(_segments("kit_place_", "kit_place", (('drop', "Drop"), ('stamp', "Stamp")),
                         'stamp' if settings.kit_stamp else 'drop',
                         lambda v: setattr(settings, "kit_stamp", v == 'stamp'),
                         icons={'drop': "kit", 'stamp': "stamp"}))
    more = [
        [vr_ui.slider("kit_size", "Part size", settings.kit_scale, 0.1, 4.0,
                      lambda v: setattr(settings, "kit_scale", v), fmt=_percent)],
        [vr_ui.slider("kit_spacing", "Stamp spacing", settings.kit_spacing, 0.005, 0.2,
                      lambda v: setattr(settings, "kit_spacing", v), fmt=_meters)],
    ]
    return _disclose('KIT', out, more, "Size, spacing")


def _snap_rows(settings):
    out = [[
        vr_ui.label("Grid", fixed=LABEL_W),
        vr_ui.toggle("grid_show", "Show", settings.grid_show, _toggle(settings, "grid_show"), icon="grid"),
        vr_ui.toggle("grid_snap", "Snap", settings.grid_snap, _toggle(settings, "grid_snap"), icon="magnet"),
    ], [
        vr_ui.label("Grab", fixed=LABEL_W),
        vr_ui.toggle("snap_surface", "Surface", settings.snap_surface, _toggle(settings, "snap_surface"),
                     icon="surface", tip="Snap to surface"),
        vr_ui.toggle("snap_angle", "15\u00b0 steps", settings.snap_angle, _toggle(settings, "snap_angle"),
                     icon="angle"),
    ]]
    if settings.grid_show or settings.grid_snap:
        out.append([vr_ui.slider("grid_size", "Grid size", settings.grid_size, 0.005, 0.5,
                                 lambda v: setattr(settings, "grid_size", v), fmt=_meters)])
    return out


def _object_rows(context, settings, obj):
    """Second level: the selected object's settings, in four pages."""
    cutter = hardsurface.is_cutter(obj)
    out = [[
        vr_ui.icon_button("back", "back", lambda: close_sheet(context), "Back"),
        vr_ui.title(obj.name, "cutter" if cutter else ""),
        vr_ui.icon_button("duplicate", "duplicate", lambda: hardsurface.duplicate(context, obj), "Duplicate"),
        vr_ui.icon_button("delete", "trash", lambda: hardsurface.delete(context, obj), "Delete", danger=True),
    ]]
    if obj.type != 'MESH':
        out.append([vr_ui.label("No settings for this object")])
        return out
    page = settings.model_page
    out.append(_segments("page_", "pages", [(i.lower(), n) for i, n in MODEL_PAGES], page.lower(),
                         lambda v: _set_model_page(settings, v.upper())))
    if page == 'BOOLEAN':
        rows, more, hint = _boolean_rows(context, obj)
    elif page == 'FINISH':
        rows, more, hint = _finish_rows(context, settings, obj)
    elif page == 'ARRAY':
        rows, more, hint = _array_rows(context, obj)
    elif page == 'STACK':
        rows, more, hint = _stack_rows(context, obj)
    elif page == 'UV':
        rows, more, hint = _uv_rows(context, settings, obj)
    else:
        rows, more, hint = _shape_rows(context, obj)
    return _disclose('OBJECT_' + page, out + rows, more, hint)


def _set_model_page(settings, page):
    settings.model_page = page
    _stack_open.clear()  # The Stack page opens on its list.
    if page != 'FINISH':
        settings.model_mark = 'NONE'  # Its buttons are on the Finish page; don't leave it on unseen.
    if page != 'UV':
        settings.uv_tool = 'NONE'  # Same for the UV page's tools.


# Settings page per object, for operators with many settings.
PARAMS_PER_PAGE = 5
_param_pages = {}


def _param_widget(context, obj, spec):
    def set_value(v, ident=spec.id):
        # Slow generators (e.g. Wall Factory) regenerate when the slider is released.
        live = not (spec.type in {'INT', 'FLOAT'} and obj.get("vrs_slow", False))
        generic.set_param(context, obj, ident, v, live=live)

    return _spec_widget("param_" + spec.id, spec, set_value)


def _spec_widget(wid, spec, set_value):
    """The widget for a setting spec (generic.Param, hardsurface.Setting)."""
    if spec.type == 'ENUM':
        names = dict(spec.items)
        return vr_ui.select(wid, spec.label, names.get(spec.value, str(spec.value)),
                            lambda: open_picker(spec.label, [Option(i, n) for i, n in spec.items],
                                                spec.value, set_value))
    if spec.type == 'BOOLEAN':
        return vr_ui.toggle(wid, spec.label, spec.value, lambda: set_value(not spec.value))
    if spec.type == 'INT':
        if spec.hi - spec.lo <= 32:
            return vr_ui.stepper(wid, spec.label, spec.value, spec.lo, spec.hi, set_value)
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
        out.append(_pager("params", page, pages, lambda p: _param_pages.__setitem__(key, p)))
    return out


def _shape_rows(context, obj):
    prim = obj.vr_prim
    out = []
    if prim.kind == 'CUBE':
        for i, axis in enumerate("XYZ"):
            out.append([vr_ui.slider(f"size_{axis}", f"Size {axis}", prim.size[i], 0.01, 1.0,
                                     _setter(obj, "size", i), fmt=_meters)])
        out.append([vr_ui.stepper("subdivisions", "Subdivisions", prim.subdivisions, 0, 16,
                                  _setter(obj, "subdivisions"))])
    elif prim.kind == 'CYLINDER':
        out.append([vr_ui.slider("radius_prim", "Radius", prim.radius, 0.005, 0.5,
                                 _setter(obj, "radius"), fmt=_meters)])
        out.append([vr_ui.slider("height", "Height", prim.height, 0.01, 1.0,
                                 _setter(obj, "height"), fmt=_meters)])
        out.append([vr_ui.stepper("segments", "Segments", prim.segments, 3, 64, _setter(obj, "segments"))])
    elif prim.kind == 'OPERATOR':
        out += _operator_rows(context, obj)
    extra = [_axes_row("Mirror", "mirror_", symmetry.model_axes(obj), lambda i: symmetry.model_toggle(context, obj, i),
                       here=lambda: _cursor_to_off_hand(context))]
    solid = hardsurface.solidify(obj) is not None
    row = [vr_ui.toggle("solidify", "Solidify", solid, lambda: hardsurface.set_solidify(obj, not solid), icon="shell")]
    if solid:
        row.append(vr_ui.slider("solidify_thickness", "Thickness", hardsurface.solidify_thickness(obj), 0.0005,
                                max(0.05, 0.5 * max(obj.dimensions)),
                                lambda v: hardsurface.set_solidify_thickness(obj, v), fmt=_meters, weight=1.8))
    extra.append(row)
    if not out:
        # A plain mesh (sculpted, or not a VR primitive): mirror and solidify are all there is.
        return [[vr_ui.label("Plain mesh (sculpted or not a VR primitive)")]] + extra, [], ""
    return out, extra, "Mirror, solidify"


def _boolean_rows(context, obj):
    if hardsurface.is_cutter(obj):
        target = hardsurface.target_of(obj)
        mod = hardsurface.modifier_of(obj)
        if target is None or mod is None:
            return [[vr_ui.label("Cuts nothing (its object is gone)")]], [], ""
        return [
            [vr_ui.label(f"Cuts {target.name}")],
            _segments("bool_", "bool_op", [(i.lower(), n) for i, n in hardsurface.OPERATIONS], mod.operation.lower(),
                      lambda v: hardsurface.set_operation(obj, v.upper())),
            [
                vr_ui.button("bool_target", "Select object", lambda: primitives.make_active(context, target),
                             icon="cursor", weight=1.6),
                vr_ui.button("bool_apply", "Apply", lambda: hardsurface.apply_cutter(context, obj), icon="check"),
                vr_ui.icon_button("bool_remove", "trash", lambda: hardsurface.delete(context, obj), "Remove the cut",
                                  danger=True),
            ],
        ], [], ""
    mods = hardsurface.booleans(obj)
    if not mods:
        return [[vr_ui.label("No cuts yet: set New to Cut,")], [vr_ui.label("then drag on or near this object")]], [], ""
    visible = hardsurface.cutters_visible(context)
    exact = all(m.solver == 'EXACT' for m in mods)

    def set_solver(solver):
        for m in hardsurface.booleans(obj):
            m.solver = solver

    rows = [
        [vr_ui.label(f"{len(mods)} cutter{'s' if len(mods) != 1 else ''}")],
        [
            vr_ui.toggle("cutters_show", "Show cutters", visible,
                         lambda: hardsurface.set_cutters_visible(context, not visible), icon="eye"),
            vr_ui.button("bool_apply_all", "Apply all", lambda: hardsurface.apply_all(context, obj), danger=True,
                         icon="check"),
        ],
    ]
    more = [[vr_ui.label("Solver", fixed=LABEL_W)] + _segments(
        "solver_", "solver", (('fast', "Fast"), ('exact', "Exact")), 'exact' if exact else 'fast',
        lambda v: set_solver('EXACT' if v == 'exact' else 'MANIFOLD'))]
    return rows, more, "Solver"


MARKS = (('HARD', "Hard"), ('SOFT', "Soft"), ('CLEAR', "Clear"))


def _finish_rows(context, settings, obj):
    current = shading.mode(obj)
    rows = [[vr_ui.label("Shading", fixed=LABEL_W)] + [
        vr_ui.segment("shade_" + ident.lower(), name,
                      (lambda i=ident: shading.set_mode(obj, None if current == i else i)), current == ident, "shade")
        for ident, name in shading.MODES]]
    if current == 'AUTO':
        rows.append([vr_ui.slider("shade_angle", "Angle", shading.angle(obj), math.radians(1.0), math.radians(180.0),
                                  lambda v: shading.set_angle(obj, v), fmt=_degrees)])
    on = hardsurface.has_bevel(obj)
    row = [vr_ui.toggle("bevel", "Bevel", on, lambda: hardsurface.set_bevel(obj, not on), icon="bevel")]
    more = [[vr_ui.label("Mark", fixed=LABEL_W)] + [
        vr_ui.segment("mark_" + ident.lower(), name,
                      (lambda i=ident: setattr(settings, "model_mark", 'NONE' if settings.model_mark == i else i)),
                      settings.model_mark == ident, "mark")
        for ident, name in MARKS]]
    if on:
        mod = hardsurface.bevel(obj)
        dims = [d for d in obj.dimensions if d > 1e-6]
        hi = max(0.25 * min(dims), 0.002) if dims else 0.02
        row.append(vr_ui.slider("bevel_width", "Width", hardsurface.bevel_width(obj), 0.0002, hi,
                                lambda v: hardsurface.set_bevel_width(obj, v), fmt=_meters, weight=1.8))
        more.append([vr_ui.stepper("bevel_segments", "Segments", mod.segments, 1, 8,
                                   lambda v: setattr(mod, "segments", int(v)))])
    rows.append(row)
    return rows, more, "Edge marks" + (", segments" if on else "")


UV_TOOLS = (('SEAM', "Seams", "seam"), ('FIX', "Fix", "fix"))
UV_VIEWS = (('NONE', "Off"), ('CHECKER', "Checker"), ('STRETCH', "Stretch"))


def _uv_rows(context, settings, obj):
    rows = [
        [vr_ui.label("Tool", fixed=LABEL_W)] + [
            vr_ui.segment("uv_tool_" + ident.lower(), name,
                          (lambda i=ident: setattr(settings, "uv_tool", 'NONE' if settings.uv_tool == i else i)),
                          settings.uv_tool == ident, "uv_tool", icon=icon)
            for ident, name, icon in UV_TOOLS],
        [vr_ui.label("View", fixed=LABEL_W)] + _segments(
            "uv_view_", "uv_view", [(i.lower(), n) for i, n in UV_VIEWS], settings.uv_view.lower(),
            lambda v: setattr(settings, "uv_view", v.upper())),
        [vr_ui.button("uv_unwrap", "Unwrap", lambda: _unwrap_all(context, settings),
                      tip="Unwrap from the seams (with the visible cutters)"),
         vr_ui.button("uv_smart", "Smart UV", lambda: uv.smart_project(context, obj),
                      tip="Cut and unwrap by angle, with seams along the islands"),
         vr_ui.button("uv_pack", "Pack", lambda: uv.pack(context, obj), tip="Pack the islands into the UV square")],
    ]
    note = uv.message() or ("" if uv.has_uvs(obj) else "No UVs yet: Unwrap, Smart UV or trace seams")
    if note:
        rows.append([vr_ui.label(note, align='CENTER')])
    more = [
        [vr_ui.label("Method", fixed=LABEL_W)] + _segments(
            "uv_method_", "uv_method", [(i.lower(), n) for i, n in uv.METHODS], settings.uv_method.lower(),
            lambda v: setattr(settings, "uv_method", v.upper())),
        [vr_ui.toggle("uv_live", "Live", settings.uv_live, _toggle(settings, "uv_live"), icon="seam",
                      tip="Unwrap again after each seam stroke"),
         vr_ui.stepper("uv_checker", "Checker", settings.uv_checker, 2, 64,
                       lambda v: setattr(settings, "uv_checker", int(v)), weight=1.6)],
        [vr_ui.slider("uv_fix_radius", "Fix radius", settings.uv_fix_radius, 0.005, 0.2,
                      lambda v: setattr(settings, "uv_fix_radius", v), fmt=_meters)],
        [vr_ui.button("uv_from_sharp", "From sharp", lambda: uv.seams_from_sharp(obj), icon="seam",
                      tip="Seams on the edges that shade sharp"),
         vr_ui.button("uv_average", "Even out", lambda: uv.average_scale(context, obj), icon="islands",
                      tip="Scale islands to their share of the surface, then pack"),
         vr_ui.icon_button("uv_clear_seams", "trash", lambda: uv.clear_seams(obj), "Clear seams", danger=True)],
    ]
    return rows, more, "Method, checker, seams"


def _unwrap_all(context, settings):
    """The object and its visible cutters (their UVs carry into the cut)."""
    for o in shading.mark_targets(context):
        uv.unwrap(context, o, settings.uv_method)


def _array_rows(context, obj):
    count = hardsurface.array_count(obj)
    out = [[vr_ui.stepper("array_count", "Count", count, 1, 24,
                          lambda v: hardsurface.set_array_count(context, obj, v),
                          fmt=lambda v: "Off" if int(v) <= 1 else str(int(v)))]]
    if count <= 1:
        return out, [], ""
    mode = hardsurface.array_mode(obj)
    axis = hardsurface.array_axis(obj)
    out.append(_segments("array_", "array_mode", (('linear', "Linear"), ('radial', "Radial")), mode.lower(),
                         lambda v: hardsurface.set_array_mode(context, obj, v.upper()),
                         icons={'linear': "linear", 'radial': "radial"}))
    out.append([vr_ui.label("Around" if mode == 'RADIAL' else "Along", fixed=LABEL_W)] + [
        vr_ui.segment("array_axis_" + a, a, (lambda i=i: hardsurface.set_array_axis(context, obj, i)), axis == i,
                      "array_axis")
        for i, a in enumerate(symmetry.AXES)])
    if mode == 'RADIAL':
        out.append([vr_ui.button("array_center", "Center at other hand", lambda: _array_center_here(context, obj),
                                 icon="here")])
    else:
        out.append([vr_ui.slider("array_gap", "Gap", hardsurface.array_gap(obj), -0.5, 2.0,
                                 lambda v: hardsurface.set_array_gap(context, obj, v), fmt=_percent)])
    return out, [], ""


# The Stack page: every modifier on the object, top to bottom; one opens to its settings.
STACK_PER_PAGE = 5
MODIFIER_ICONS = {'BOOLEAN': "cut", 'BEVEL': "bevel", 'SOLIDIFY': "shell", 'ARRAY': "array", 'MIRROR': "mirror"}
_stack_open = {}  # object name -> the name of the modifier whose settings show
_stack_pages = {}  # object name (list) or "object/modifier" (settings) -> page


def _type_name(mod):
    return mod.bl_rna.properties["type"].enum_items[mod.type].name


def _modifier_icon(mod):
    return "shading" if mod.name == shading.MODIFIER else MODIFIER_ICONS.get(mod.type)


def _toggle_modifier(obj, name):
    mod = obj.modifiers.get(name)
    if mod is not None:
        mod.show_viewport = not mod.show_viewport


def _stack_rows(context, obj):
    key = obj.name
    open_name = _stack_open.get(key)
    if open_name is not None:
        if obj.modifiers.get(open_name) is not None:
            return _modifier_rows(context, obj, obj.modifiers[open_name])
        del _stack_open[key]  # Applied or removed.
    if not obj.modifiers:
        return [[vr_ui.label("No modifiers")]], [], ""
    shown, page, pages = _paged(list(obj.modifiers), _stack_pages.get(key, 0), STACK_PER_PAGE)
    _stack_pages[key] = page
    out = []
    for mod in shown:
        i, name = obj.modifiers.find(mod.name), mod.name
        kind = _type_name(mod)
        out.append([
            vr_ui.select(f"mod_{i}", name, "" if kind in name else kind,
                         (lambda n=name: _stack_open.__setitem__(key, n)), icon=_modifier_icon(mod), weight=3.0),
            vr_ui.icon_button(f"mod_show_{i}", "eye", (lambda n=name: _toggle_modifier(obj, n)),
                              "Hide" if mod.show_viewport else "Show", active=mod.show_viewport),
            vr_ui.icon_button(f"mod_apply_{i}", "check", (lambda n=name: hardsurface.apply_modifier(context, obj, n)),
                              "Apply (and those above)"),
        ])
    if pages > 1:
        out.append(_pager("stack", page, pages, lambda p: _stack_pages.__setitem__(key, p)))
    out.append([vr_ui.button("stack_apply_all", "Apply all", lambda: hardsurface.apply_stack(context, obj),
                             danger=True, icon="check", tip="Apply every modifier that shows")])
    return out, [], ""


def _modifier_rows(context, obj, mod):
    """One modifier: move it, its settings, show / apply / remove."""
    key, name = obj.name, mod.name
    head = [vr_ui.icon_button("mod_back", "back", lambda: _stack_open.pop(key, None), "Back to the stack"),
            vr_ui.title(name, _type_name(mod))]
    if hardsurface.can_move(obj, name, -1):
        head.append(vr_ui.icon_button("mod_up", "chevron_up", lambda: hardsurface.move_modifier(obj, name, -1),
                                      "Move up"))
    if hardsurface.can_move(obj, name, 1):
        head.append(vr_ui.icon_button("mod_down", "chevron_down", lambda: hardsurface.move_modifier(obj, name, 1),
                                      "Move down"))
    out = [head]
    specs = hardsurface.modifier_settings(obj, mod)
    pkey = key + "/" + name
    shown, page, pages = _paged(specs, _stack_pages.get(pkey, 0), PARAMS_PER_PAGE)
    _stack_pages[pkey] = page
    out += [[_spec_widget("modp_" + s.id, s, (lambda v, i=s.id: hardsurface.set_modifier_setting(obj, name, i, v)))]
            for s in shown]
    if not specs:
        out.append([vr_ui.label("No settings to show here")])
    if pages > 1:
        out.append(_pager("modp", page, pages, lambda p: _stack_pages.__setitem__(pkey, p)))
    out.append([
        vr_ui.toggle("mod_show", "Show", mod.show_viewport, lambda: _toggle_modifier(obj, name), icon="eye"),
        vr_ui.button("mod_apply", "Apply", lambda: hardsurface.apply_modifier(context, obj, name), icon="check",
                     tip="Apply (and those above)"),
        vr_ui.icon_button("mod_remove", "trash", lambda: hardsurface.remove_modifier(context, obj, name), "Remove",
                          danger=True),
    ])
    return out, [], ""


def _off_tip(context):
    """Where the off-hand controller's tip is, or None outside VR."""
    ss = context.window_manager.xr_session_state
    if ss is None:
        return None
    from . import operators
    off = actions.off_index(context)
    pose = operators.controller_pose(ss.controller_aim_location_get(context, off),
                                     ss.controller_aim_rotation_get(context, off))
    return operators.brush_tip(pose, context.scene.vr_sculpt.tip_offset * ss.viewer_scale)


def _cursor_to_off_hand(context):
    """Put the 3D cursor (the mirror center) at the off-hand controller's tip."""
    tip = _off_tip(context)
    if tip is not None:
        symmetry.move_center(context, tip)


def _array_center_here(context, obj):
    tip = _off_tip(context)
    if tip is not None:
        hardsurface.set_array_center(context, obj, tip)


# --- Draw -----------------------------------------------------------------------

DRAW_TOOLS = (('DRAW', "Draw"), ('EDIT', "Edit"), ('ERASE', "Erase"))
DRAW_STYLES = (('TUBE', "Tube"), ('RIBBON', "Ribbon"), ('NOTE', "Note"))


def _draw_rows(context, settings):
    out = [
        _header(context, settings, "Draw", actions=[
            vr_ui.icon_button("undo_stroke", "undo", lambda: _undo_stroke(context), "Undo stroke")]),
        _segments("tool_", "draw_tool", DRAW_TOOLS, settings.draw_tool, lambda v: setattr(settings, "draw_tool", v),
                  icons={'DRAW': "pen", 'EDIT': "edit", 'ERASE': "eraser"}),
    ]
    if settings.draw_tool == 'DRAW':
        out.append(_segments("style_", "draw_style", DRAW_STYLES, settings.draw_style,
                             lambda v: setattr(settings, "draw_style", v),
                             icons={'TUBE': "tube", 'RIBBON': "ribbon", 'NOTE': "note"}))
        out.append([vr_ui.swatch_button("color_" + ident, rgb, (lambda i=ident: setattr(settings, "draw_color", i)),
                                        settings.draw_color == ident) for ident, _name, rgb in strokes.PALETTE])
    out.append([vr_ui.slider("draw_width", "Width", settings.draw_width, 0.001, 0.05,
                             lambda v: setattr(settings, "draw_width", v), fmt=_meters)])
    items = [(i.identifier, i.name) for i in settings.bl_rna.properties["draw_thickness"].enum_items]
    names = dict(items)
    more = [[vr_ui.select(
        "draw_thickness", "Thickness", names[settings.draw_thickness],
        lambda: open_picker("Thickness", [Option(i, n) for i, n in items], settings.draw_thickness,
                            lambda v: setattr(settings, "draw_thickness", v)))]]
    if settings.draw_thickness == 'TAPER':
        more.append([vr_ui.slider("draw_taper", "Taper length", settings.draw_taper, 0.05, 0.5,
                                  lambda v: setattr(settings, "draw_taper", v), fmt=_percent)])
    more.append([vr_ui.slider("draw_smoothing", "Smoothing", settings.draw_smoothing, 0.0, 0.95,
                              lambda v: setattr(settings, "draw_smoothing", v), fmt=_percent)])
    more.append([vr_ui.label("Mirror", fixed=LABEL_W)] + [
        vr_ui.toggle("mirror_" + a, a.upper(), getattr(settings, "mirror_" + a), _toggle(settings, "mirror_" + a))
        for a in "xyz"] + [vr_ui.icon_button("mirror_here", "here", lambda: _cursor_to_off_hand(context),
                                            "Center at other hand")])
    return _disclose('DRAW', out, more, "Thickness, smoothing, mirror")


def _undo_stroke(context):
    from . import operators
    operators.undo_last_stroke(context)


# --- Paint ----------------------------------------------------------------------

PAINT_SHORT = {"Airbrush": "Air"}  # Six brushes share a row.


def _paint_setup(context):
    obj = paint.target(context)
    if obj is not None:
        paint.setup(context, obj, int(context.scene.vr_sculpt.paint_texture_size))


def _paint_rows(context, settings):
    obj = paint.target(context)
    if obj is None:
        return [_header(context, settings, "Paint"), [vr_ui.label("Select a mesh to paint", align='CENTER')]]
    if not paint.ready(obj):
        items = [(i.identifier, i.name) for i in settings.bl_rna.properties["paint_texture_size"].enum_items]
        return [
            _header(context, settings, "Paint", obj.name),
            [vr_ui.label("It needs UVs and an image to paint on")],
            [vr_ui.label("Texture", fixed=LABEL_W)] + _segments(
                "paint_size_", "paint_size", items, settings.paint_texture_size,
                lambda v: setattr(settings, "paint_texture_size", v)),
            [vr_ui.button("paint_setup", "Set up for painting", lambda: _paint_setup(context), active=True,
                          icon="check")],
        ]
    b = paint.brush(context)
    current = b.name if b else ""
    out = [
        _header(context, settings, "Paint", obj.name, actions=[
            vr_ui.icon_button("paint_undo", "undo", lambda: paint.undo_last(context), "Undo stroke")]),
        _segments("pbrush_", "pbrush", [(n, PAINT_SHORT.get(n, paint.SHORT[n])) for n in paint.BRUSHES], current,
                  lambda n: paint.activate_brush(context, n)),
    ]
    color = paint.get_color(context)
    out.append([
        vr_ui.swatch_button("paint_color1", color, lambda: None, active=True),
        vr_ui.swatch_button("paint_color2", paint.get_color(context, True), lambda: paint.swap_colors(context)),
        vr_ui.icon_button("paint_swap", "swap", lambda: paint.swap_colors(context), "Swap colors"),
        vr_ui.toggle("paint_pick", "Pick", settings.paint_tool == 'PICK',
                     lambda: setattr(settings, "paint_tool", 'PAINT' if settings.paint_tool == 'PICK' else 'PICK'),
                     icon="eyedropper", weight=2.2, tip="Pick a color from the surface"),
    ])
    out.append([vr_ui.swatch_button("pcolor_" + ident, rgb, (lambda rgb=rgb: paint.set_color(context, rgb)),
                                    all(abs(a - b) < 1e-3 for a, b in zip(color, rgb)))
                for ident, _name, rgb in strokes.PALETTE])
    out.append([vr_ui.slider("paint_radius", "Radius", settings.paint_radius, 0.002, 0.15,
                             lambda x: setattr(settings, "paint_radius", x), fmt=_meters)])
    if b is not None:
        out.append([vr_ui.slider("paint_strength", "Strength", b.strength, 0.0, 1.0,
                                 lambda x: setattr(b, "strength", x), fmt=_percent)])
    h, s, v = paint.get_hsv(context)
    more = [
        [vr_ui.label("Reach", fixed=LABEL_W)] + _segments(
            "reach_", "reach", (('contact', "Contact"), ('spray', "Spray")), settings.paint_reach.lower(),
            lambda r: setattr(settings, "paint_reach", r.upper()), icons={'contact': "contact", 'spray': "spray"}),
        [vr_ui.slider("paint_hue", "Hue", h, 0.0, 1.0, lambda x: paint.set_hsv(context, h=x),
                      fmt=lambda x: f"{x * 360:.0f}\u00b0")],
        [vr_ui.slider("paint_sat", "Saturation", s, 0.0, 1.0, lambda x: paint.set_hsv(context, s=x), fmt=_percent)],
        [vr_ui.slider("paint_val", "Value", v, 0.0, 1.0, lambda x: paint.set_hsv(context, v=x), fmt=_percent)],
    ]
    return _disclose('PAINT', out, more, "Spray, hue / sat / value")


# --- Rig ------------------------------------------------------------------------

RIG_TOOLS = (('bones', "Bones"), ('weights', "Weights"))
WEIGHT_BRUSHES = (('ADD', "Add"), ('SUBTRACT', "Subtract"), ('SMOOTH', "Smooth"))
WEIGHT_ICONS = {'ADD': "plus", 'SUBTRACT': "minus", 'SMOOTH': "smooth"}


def open_rig_choice(context):
    current = rig.target(context)
    options = [Option(o.name, o.name) for o in rig.rigs(context)] + [Option("", "New rig")]
    open_picker("Rig", options, current.name if current else "", lambda name: rig.choose_rig(context, name))


def open_mesh_picker(context):
    current = rig.mesh(context)
    options = [Option(o.name, o.name) for o in rig.meshes(context)]
    open_picker("Mesh", options, current.name if current else "", lambda name: rig.choose_mesh(context, name))


def open_bone_picker(context):
    arm = rig.target(context)
    if arm is None:
        return
    open_picker("Paint bone", [Option(n, n) for n in rig.bone_names(arm, deform=True)], rig.active_bone(arm) or "",
                lambda name: rig.set_active_bone(arm, name))


def _rig_select_row(context, arm, obj):
    return [vr_ui.select("rig_pick", "Rig", arm.name if arm else "New", lambda: open_rig_choice(context), icon="rig"),
            vr_ui.select("rig_mesh", "Mesh", obj.name if obj else "none", lambda: open_mesh_picker(context),
                         icon="model")]


def _rig_rows(context, settings):
    arm, obj = rig.target(context), rig.mesh(context)
    tool_row = _segments("rig_tool_", "rig_tool", RIG_TOOLS, settings.rig_tool.lower(),
                         lambda v: rig.set_tool(context, v.upper()), icons={'bones': "bones", 'weights': "weights"})
    bone = rig.active_bone(arm)
    said = rig.message()
    if settings.rig_tool == 'BONES':
        out = [
            _header(context, settings, "Rig", said or (arm.name if arm else "touch to start a rig"), actions=[
                vr_ui.icon_button("rig_undo", "undo", lambda: rig.undo_bone(context), "Remove the last bone")]),
            _rig_select_row(context, arm, obj),
            tool_row,
            [vr_ui.button("rig_end", "End chain", rig.end_chain, icon="check"),
             vr_ui.button("rig_delete", "Delete bone", lambda: rig.delete_active(context), danger=True, icon="trash")],
            [vr_ui.button("rig_bind", f"Bind {obj.name}" if obj else "Bind", lambda: rig.bind(context), icon="bind",
                          active=arm is not None and obj is not None and not rig.bound(obj, arm))],
        ]
        more = [[vr_ui.toggle("rig_mirror", "Mirror X", settings.rig_mirror, _toggle(settings, "rig_mirror"),
                              icon="mirror")]]
        return _disclose('RIG', out, more, "Mirror")
    out = [_header(context, settings, "Rig", said or bone or "touch a bone", actions=[
        vr_ui.icon_button("weight_undo", "undo", lambda: rig.undo_stroke(context), "Undo weight stroke")])]
    if arm is None or obj is None or not rig.bound(obj, arm):
        # Nothing to paint yet: what's missing, and Bind.
        return out + [
            tool_row,
            _rig_select_row(context, arm, obj),
            [vr_ui.label("Bind the mesh to paint its weights" if arm and obj else "Pick a rig and a mesh",
                         align='CENTER')],
            [vr_ui.button("rig_bind", "Bind with automatic weights", lambda: rig.bind(context), active=True,
                          icon="bind")],
        ]
    out += [
        tool_row,
        [vr_ui.select("weight_bone", "Bone", bone or "none", lambda: open_bone_picker(context), icon="bones")],
        _segments("wbrush_", "wbrush", WEIGHT_BRUSHES, settings.weight_brush,
                  lambda v: setattr(settings, "weight_brush", v), icons=WEIGHT_ICONS),
        [vr_ui.slider("weight_radius", "Radius", settings.weight_radius, 0.005, 0.2,
                      lambda v: setattr(settings, "weight_radius", v), fmt=_meters)],
        [vr_ui.slider("weight_strength", "Strength", settings.weight_strength, 0.0, 1.0,
                      lambda v: setattr(settings, "weight_strength", v), fmt=_percent)],
    ]
    more = [
        _rig_select_row(context, arm, obj),
        [vr_ui.toggle("weight_mirror", "Mirror X", settings.weight_mirror, _toggle(settings, "weight_mirror"),
                      icon="mirror"),
         vr_ui.toggle("weight_show", "Show", settings.weight_show, _toggle(settings, "weight_show"), icon="eye")],
        [vr_ui.button("weight_normalize", "Normalize", lambda: rig.normalize(context), icon="weights"),
         vr_ui.button("rig_rebind", "Rebind", lambda: rig.bind(context), danger=True, icon="bind")],
    ]
    return _disclose('RIG_WEIGHTS', out, more, "Rig, mirror, normalize")


# --- Pose -----------------------------------------------------------------------

def open_rig_picker(context):
    current = pose.rig(context)
    options = [Option(o.name, o.name) for o in pose.rigs(context)] + [Option("", "Objects (no rig)")]
    open_picker("Pose", options, current.name if current else "", lambda name: pose.choose_rig(context, name))


def _bind(context, source):
    puppet.bind(context, context.window_manager.xr_session_state, source)


def _puppet_rows(context):
    n = puppet.binding_count()
    take = puppet.state()
    recording = take in {'RECORDING', 'ENDING'}
    return [
        [vr_ui.label("Bind", fixed=LABEL_W)] + [
            vr_ui.button("bind_" + ident.lower(), short, (lambda i=ident: _bind(context, i)))
            for ident, short in (('LEFT', "L hand"), ('RIGHT', "R hand"), ('HEAD', "Head"))],
        [
            vr_ui.button("unbind", f"Unbind {n}", puppet.unbind_all, danger=True, icon="close") if n else
            vr_ui.label("Puppet", fixed=LABEL_W),
            vr_ui.button("puppet_rec", puppet.status(), lambda: puppet.toggle_record(context),
                         active=take is not None, danger=recording, icon="stop" if take else "record"),
        ],
    ]


def _pose_rows(context, settings):
    obj = pose.rig(context)
    pb = pose.active_bone(obj) if obj is not None else None
    subtitle = pb.name if pb is not None else ("touch a bone" if obj is not None else "")
    out = [
        _header(context, settings, "Pose", subtitle),
        [vr_ui.select("pose_rig", "Rig", obj.name if obj else "Objects", lambda: open_rig_picker(context),
                      icon="bones")],
    ]
    more = []
    if obj is not None:
        out.append(_segments("pose_", "pose_tool", (('fk', "FK"), ('ik', "IK")), settings.pose_tool.lower(),
                             lambda v: setattr(settings, "pose_tool", v.upper())))
        if settings.pose_tool == 'IK':
            out.append([vr_ui.stepper("pose_chain", "IK chain", settings.pose_chain, 1, 6,
                                      lambda v: setattr(settings, "pose_chain", int(v)))])
        out.append([
            vr_ui.toggle("pose_mirror", "Mirror X", settings.pose_mirror, _toggle(settings, "pose_mirror"),
                         icon="mirror"),
            vr_ui.toggle("pose_bones", "Bones", settings.pose_show_bones, _toggle(settings, "pose_show_bones"),
                         icon="eye"),
        ])
        more.append([
            vr_ui.button("pose_reset", "Reset bone", lambda: pose.reset_selected(context), icon="reset"),
            vr_ui.button("pose_reset_all", "Reset pose", lambda: pose.reset_selected(context, True), danger=True,
                         icon="reset"),
        ])
    else:
        out.append([vr_ui.label("Touch an object to move it", align='CENTER')])
    if puppet.binding_count() or puppet.state() is not None:
        out += _puppet_rows(context)  # A puppet in use: its controls come up front.
    else:
        more += _puppet_rows(context)
    return _disclose('POSE', out, more, "Reset, puppeteering")


# --- Physics --------------------------------------------------------------------

HAND_TOOLS = (('grab', "Grab"), ('force', "Push / Pull"))


def _named(name):
    """Pickers answer after the panel that opened them is gone: they find the object again by name."""
    return bpy.data.objects.get(name)


def open_cloth_picker(context, obj):
    open_picker("Cloth", [Option(n, n) for n in physics.CLOTH_PRESETS], physics.cloth_preset(obj),
                lambda value, n=obj.name: physics.set_cloth_preset(_named(n), value))


def open_pin_picker(context, obj):
    open_picker("Pin", [Option(v, label) for v, label in physics.pin_options(obj)], physics.pin(obj),
                lambda value, n=obj.name: physics.set_pin(_named(n), value))


def _sim_row(context, settings):
    playing = anim.is_playing(context)
    return [
        vr_ui.button("sim_play", "Stop" if playing else "Simulate", lambda: physics.simulate(context), active=playing,
                     icon="stop" if playing else "play"),
        vr_ui.button("sim_reset", "Reset", lambda: physics.reset(context), icon="reset", tip="Back to the start"),
        vr_ui.toggle("physics_hands", "Hands", settings.physics_hands, _toggle(settings, "physics_hands"),
                     icon="hands", tip="Hands take part"),
    ]


def _hand_rows(settings):
    """(up front, More): the hand tool, and Push / Pull's strength."""
    out = [_segments("ptool_", "physics_tool", HAND_TOOLS, settings.physics_tool.lower(),
                     lambda v: setattr(settings, "physics_tool", v.upper()), icons={'grab': "throw", 'force': "force"})]
    more = []
    if settings.physics_tool == 'FORCE':
        more.append([vr_ui.slider("physics_force", "Force", settings.physics_force, 0.0, 2000.0,
                                  lambda v: setattr(settings, "physics_force", v), fmt=lambda v: f"{v:.0f}")])
    return out, more


def _rigid_rows(obj, static):
    rb = obj.rigid_body
    if rb is None:  # A Collision modifier alone: choosing Static again adds the rigid body.
        return [], []
    out = []
    if not static:
        out.append([vr_ui.slider("rb_mass", "Mass", rb.mass, 0.05, 20.0, lambda v: setattr(rb, "mass", v),
                                 fmt=lambda v: f"{v:.2f} kg")])
    out.append([vr_ui.slider("rb_bounce", "Bounce", rb.restitution, 0.0, 1.0,
                             lambda v: setattr(rb, "restitution", v), fmt=_percent)])
    more = [
        [vr_ui.label("Shape", fixed=LABEL_W)] + _segments(
            "rb_shape_", "rb_shape", [(i.lower(), n) for i, n in physics.SHAPES], rb.collision_shape.lower(),
            lambda v: setattr(rb, "collision_shape", v.upper())),
        [vr_ui.slider("rb_friction", "Friction", rb.friction, 0.0, 1.0, lambda v: setattr(rb, "friction", v),
                      fmt=_percent)],
    ]
    if not static:
        more.append([vr_ui.slider("rb_air", "Air drag", rb.linear_damping, 0.0, 1.0,
                                  lambda v: setattr(rb, "linear_damping", v), fmt=_percent)])
    return out, more


def _cloth_rows(context, obj):
    s = physics.cloth(obj)
    collision = physics.cloth_collision(obj)
    pin = physics.pin(obj)
    out = [
        [vr_ui.select("cloth_preset", "Cloth", physics.cloth_preset(obj), lambda: open_cloth_picker(context, obj),
                      icon="cloth"),
         vr_ui.select("cloth_pin", "Pin", pin or "none", lambda: open_pin_picker(context, obj), icon="pin")],
        [vr_ui.slider("cloth_stiffness", "Stiffness", physics.cloth_stiffness(obj), 1.0, 100.0,
                      lambda v: physics.set_cloth_stiffness(obj, v), fmt=lambda v: f"{v:.0f}")],
    ]
    more = [
        [vr_ui.slider("cloth_pressure", "Pressure", physics.cloth_pressure(obj), 0.0, 20.0,
                      lambda v: physics.set_cloth_pressure(obj, v), fmt=lambda v: f"{v:.1f}" if v > 1e-4 else "off")],
        [vr_ui.slider("cloth_mass", "Mass", s.mass, 0.05, 5.0, lambda v: setattr(s, "mass", v),
                      fmt=lambda v: f"{v:.2f} kg")],
        [vr_ui.toggle("cloth_self", "Self collision", collision.use_self_collision,
                      lambda: setattr(collision, "use_self_collision", not collision.use_self_collision),
                      icon="cloth")],
    ]
    return out, more


def _soft_rows(obj):
    stiffness = [vr_ui.slider("soft_stiffness", "Stiffness", physics.soft_stiffness(obj), 0.0, 1.0,
                              lambda v: physics.set_soft_stiffness(obj, v), fmt=_percent)]
    s = physics.cloth(obj)
    if s is None:  # A Soft Body modifier, made on the desktop.
        return [stiffness, [vr_ui.label("A slow Soft Body: Soft remakes it", align='CENTER')]], []
    collision = physics.cloth_collision(obj)
    out = [
        stiffness,
        [vr_ui.slider("soft_pressure", "Pressure", physics.cloth_pressure(obj), 0.0, 10.0,
                      lambda v: physics.set_cloth_pressure(obj, v), fmt=lambda v: f"{v:.1f}" if v > 1e-4 else "off")],
    ]
    more = [
        [vr_ui.slider("soft_mass", "Mass", s.mass, 0.05, 5.0, lambda v: setattr(s, "mass", v),
                      fmt=lambda v: f"{v:.2f} kg")],
        [vr_ui.toggle("soft_self", "Self collision", collision.use_self_collision,
                      lambda: setattr(collision, "use_self_collision", not collision.use_self_collision),
                      icon="soft")],
    ]
    return out, more


def _object_physics_rows(context, obj):
    k = physics.kind(obj)
    out = [_segments("kind_", "physics_kind", [(i.lower(), n) for i, n in physics.KINDS], k.lower(),
                     lambda v: physics.set_kind(context, obj, v.upper()))]  # Words: five icons need learning.
    if k in {'RIGID', 'STATIC'}:
        rows, more = _rigid_rows(obj, k == 'STATIC')
    elif k == 'CLOTH':
        rows, more = _cloth_rows(context, obj)
    elif k == 'SOFT':
        rows, more = _soft_rows(obj)
    else:
        rows, more = [[vr_ui.label("Rigid falls, Static holds, Cloth drapes", align='CENTER')]], []
    return out + rows, more


def _spring_rows(context, arm):
    pb = physics.active_bone(arm)
    if pb is None:
        return [[vr_ui.label("Touch a bone to give it a spring", align='CENTER')]], []
    sp = pb.vr_spring
    out = [[
        vr_ui.toggle("spring_on", "Spring", sp.enabled, lambda: physics.set_spring(arm, pb, not sp.enabled),
                     icon="spring"),
        vr_ui.button("spring_chain", "Whole chain", lambda: physics.spring_chain(arm, pb), icon="bones",
                     tip="This bone's spring for every bone below it"),
    ]]
    if sp.enabled:
        out += [
            [vr_ui.slider("spring_stiffness", "Stiffness", sp.stiffness, 0.0, 1.0,
                          lambda v: setattr(sp, "stiffness", v), fmt=_percent)],
            [vr_ui.slider("spring_damping", "Damping", sp.damping, 0.0, 1.0, lambda v: setattr(sp, "damping", v),
                          fmt=_percent)],
        ]
    n = len(physics.springs(arm))
    more = [[vr_ui.slider("spring_gravity", "Gravity", sp.gravity, 0.0, 2.0, lambda v: setattr(sp, "gravity", v),
                          fmt=_percent)]] if sp.enabled else []
    more += [[
        vr_ui.button("spring_bake", f"Bake {n}" if n else "Bake", lambda: physics.bake(context, arm), icon="key",
                     tip="Key the springs over the frame range"),
        vr_ui.button("spring_rest", "Rest", lambda: physics.reset_springs(context), icon="reset",
                     tip="Springs back to rest"),
    ]]
    return out, more


def _simulation_rows(context, settings):
    """The Simulation sheet: the scene's gravity, speed and length, and keeping a run."""
    kept = physics.baked(context)
    return [
        [vr_ui.icon_button("back", "back", lambda: close_sheet(context), "Back"),
         vr_ui.title("Simulation", "kept run" if kept else ""), _pin(context, settings)],
        [vr_ui.slider("physics_gravity", "Gravity", physics.gravity(context), 0.0, 2.0,
                      lambda v: physics.set_gravity(context, v), fmt=lambda v: f"{v:.2f} g")],
        [vr_ui.slider("physics_speed", "Speed", physics.speed(context), 0.1, 2.0,
                      lambda v: physics.set_speed(context, v), fmt=lambda v: f"{v:.2f}x")],
        [vr_ui.slider("physics_length", "Length", physics.length(context), 1.0, 60.0,
                      lambda v: physics.set_length(context, v), fmt=lambda v: f"{v:.1f} s")],
        [vr_ui.button("physics_keep", "Kept" if kept else "Keep run", lambda: physics.keep(context), icon="check",
                      active=kept, tip="Bake the run just played"),
         vr_ui.button("physics_free", "Clear kept", lambda: physics.free(context), danger=True, icon="trash")
         if kept else vr_ui.label("")],
    ]


def _physics_rows(context, settings):
    obj = physics.selected(context)
    arm = obj if obj is not None and obj.type == 'ARMATURE' else None
    if arm is not None:
        bone = physics.active_bone(arm)
        subtitle = bone.name if bone is not None else arm.name
    else:
        subtitle = obj.name if obj is not None else "touch an object"
    out = [_header(context, settings, "Physics", "kept run" if physics.baked(context) else subtitle),
           _sim_row(context, settings)]
    hand_more = []
    if settings.physics_hands:
        hand_rows, hand_more = _hand_rows(settings)
        out += hand_rows
    if arm is not None:
        rows, more = _spring_rows(context, arm)
    elif obj is not None and obj.type == 'MESH':
        rows, more = _object_physics_rows(context, obj)
    else:
        rows, more = [[vr_ui.label("Touch an object or a bone", align='CENTER')]], []
    sim = [vr_ui.select("physics_sim", "Simulation",
                        f"{physics.gravity(context):.2f} g, {physics.length(context):.0f} s",
                        lambda: open_sheet('SIMULATION'), icon="gravity")]
    return _disclose('PHYSICS', out + rows, hand_more + more + [sim], "Simulation: gravity, speed, keep")


# --- Tools ----------------------------------------------------------------------

def open_camera_picker(context):
    scene = context.scene
    options = [Option(o.name, o.name) for o in camera.cameras(scene)]
    current = scene.camera.name if scene.camera else ""

    def choose(name):
        obj = scene.objects.get(name)
        if obj is not None:
            camera.set_camera(context, obj)

    open_picker("Scene camera", options, current, choose)


def _new_camera_here(context):
    ss = context.window_manager.xr_session_state
    pose_ = None
    if ss is not None:
        from . import operators
        main = actions.main_index(context)
        pose_ = operators.controller_pose(ss.controller_aim_location_get(context, main),
                                          ss.controller_aim_rotation_get(context, main))
    camera.new_camera(context, pose_)


def _tools_rows(context, settings):
    scene = context.scene
    out = [
        _header(context, settings, "Tools"),
        _segments("tool_", "tool", (('measure', "Measure"), ('camera', "Camera")), settings.tool.lower(),
                  lambda v: setattr(settings, "tool", v.upper()), icons={'measure': "ruler", 'camera': "camera"}),
    ]
    if settings.tool == 'MEASURE':
        n = len(measure.stored(scene))
        out.append([
            vr_ui.toggle("measures_show", "Show", settings.show_measures, _toggle(settings, "show_measures"),
                         icon="eye"),
            vr_ui.button("measure_undo", "Undo", lambda: measure.undo_last(scene), icon="undo"),
            vr_ui.button("measure_clear", f"Clear {n}", lambda: measure.clear(scene), danger=True, icon="trash"),
        ])
    else:
        cam = scene.camera
        out.append([vr_ui.select("camera", "Camera", cam.name if cam else "none", lambda: open_camera_picker(context),
                                 icon="camera")])
        row = [vr_ui.toggle("camera_preview", "Pin preview", settings.camera_preview,
                            _toggle(settings, "camera_preview"), icon="pin")]
        if settings.show_new_camera:
            row.insert(0, vr_ui.button("camera_new", "New here", lambda: _new_camera_here(context), icon="plus"))
        out.append(row)
        if cam is not None and cam.data.type == 'PERSP':
            out.append([vr_ui.slider("camera_lens", "Lens", cam.data.lens, 10.0, 200.0,
                                     lambda v: setattr(cam.data, "lens", v), fmt=lambda v: f"{v:.0f} mm")])
    return out


# --- Settings (the gear, in every mode) --------------------------------------------

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


REVEALS = (('wrist', "Wrist"), ('look', "Look"), ('always', "Always"))


def _settings_rows(context, settings):
    xs = context.window_manager.xr_session_settings
    out = [
        [vr_ui.icon_button("back", "back", lambda: close_sheet(context), "Back"), vr_ui.title("Settings"),
         _pin(context, settings)],
        [vr_ui.label("Move", fixed=LABEL_W)] + _segments(
            "nav_", "nav", (('walk', "Walk"), ('fly', "Fly")), settings.nav_mode.lower(),
            lambda v: setattr(settings, "nav_mode", v.upper()), icons={'walk': "walk", 'fly': "fly"}),
        [vr_ui.slider("nav_speed", "Speed", settings.nav_speed, 0.2, 6.0,
                      lambda v: setattr(settings, "nav_speed", v), fmt=lambda v: f"{v:.1f} m/s")],
        [vr_ui.label("Grip", fixed=LABEL_W)] + _segments(
            "grab_", "grab", (('off', "Move"), ('turn', "Turn"), ('free', "Free")), settings.nav_grab_turn.lower(),
            lambda v: setattr(settings, "nav_grab_turn", v.upper())),
        [vr_ui.label("Grid", fixed=LABEL_W),
         vr_ui.toggle("grid_show", "Show", settings.grid_show, _toggle(settings, "grid_show"), icon="grid"),
         vr_ui.toggle("grid_snap", "Snap", settings.grid_snap, _toggle(settings, "grid_snap"), icon="magnet")],
    ]
    if settings.grid_show or settings.grid_snap:
        out.append([vr_ui.slider("grid_size", "Grid size", settings.grid_size, 0.005, 0.5,
                                 lambda v: setattr(settings, "grid_size", v), fmt=_meters)])
    out += [
        [vr_ui.toggle("show_timeline", "Timeline", settings.show_timeline, _toggle(settings, "show_timeline"),
                      icon="timeline"),
         vr_ui.toggle("passthrough", "Passthrough", bool(xs and xs.show_passthrough), lambda: _passthrough(context),
                      icon="passthrough")],
        [vr_ui.label("Desktop", fixed=LABEL_W),
         vr_ui.toggle("show_desktop", "Show", settings.show_desktop, lambda: _show_desktop(context, settings),
                      icon="desktop", tip="Desktop window"),
         vr_ui.button("bring_desktop", "Bring here", lambda: _bring_desktop(context), icon="here")
         if settings.show_desktop else vr_ui.label("")],
        [vr_ui.label("Hands", fixed=LABEL_W)] + _segments(
            "controllers_", "controllers", (('ours', "Quest"), ('blender', "Blender")),
            'ours' if settings.own_controllers else 'blender',
            lambda v: setattr(settings, "own_controllers", v == 'ours')) + [
            vr_ui.toggle("controller_hints", "Hints", settings.controller_hints, _toggle(settings, "controller_hints"),
                         icon="eye", tip="Button hints")],
        [vr_ui.label("Handed", fixed=LABEL_W)] + _segments(
            "handed_", "handed", (('right', "Right"), ('left', "Left")),
            'left' if settings.left_handed else 'right',
            lambda v: setattr(settings, "left_handed", v == 'left')),
        [vr_ui.label("Panel", fixed=LABEL_W)] + _segments(
            "reveal_", "reveal", REVEALS, settings.panel_reveal.lower(),
            lambda v: setattr(settings, "panel_reveal", v.upper())),
        [vr_ui.label("Track", fixed=LABEL_W),
         vr_ui.toggle("tracking", "Objects", settings.tracking, _toggle(settings, "tracking"), icon="tracking",
                      tip="Tracking objects"),
         vr_ui.toggle("tracking_record", "Record", settings.tracking_record, _toggle(settings, "tracking_record"),
                      icon="record", tip="Record tracking")],
    ]
    return out


_MODE_ROWS = {'SCULPT': _sculpt_rows, 'MODEL': _model_rows, 'DRAW': _draw_rows, 'PAINT': _paint_rows,
              'RIG': _rig_rows, 'POSE': _pose_rows, 'PHYSICS': _physics_rows, 'TOOLS': _tools_rows}


# --- timeline strip --------------------------------------------------------------

def timeline_rows(context, settings):
    """Frame scrubber with key ticks, transport and keying; More has the interpolation, range, paths and
    onion skin."""
    scene = context.scene
    start, end = scene.frame_start, scene.frame_end
    span = max(end - start, 1)
    keys = anim.key_frames(anim.selection(context))
    ticks = [(f - start) / span for f in keys if start <= f <= end]
    playing = anim.is_playing(context)
    interp = anim.interpolation_at(context)
    names = dict(anim.INTERPOLATIONS)
    tool = scene.tool_settings
    out = [
        [vr_ui.slider("frame", "Frame", scene.frame_current, start, end,
                      lambda v: anim.set_frame(context, v), integer=True,
                      fmt=lambda v: f"{int(v)} / {end}", ticks=ticks)],
        [
            vr_ui.icon_button("key_prev", "key_prev", lambda: anim.jump_key(context, -1), "Previous key"),
            vr_ui.icon_button("frame_prev", "frame_prev", lambda: anim.step(context, -1), "Previous frame"),
            vr_ui.button("play", "Stop" if playing else "Play", lambda: anim.toggle_play(context), active=playing,
                         icon="stop" if playing else "play"),
            vr_ui.icon_button("frame_next", "frame_next", lambda: anim.step(context, 1), "Next frame"),
            vr_ui.icon_button("key_next", "key_next", lambda: anim.jump_key(context, 1), "Next key"),
        ],
        [
            vr_ui.button("key_insert", "Key", lambda: anim.key_selection(context), icon="key"),
            vr_ui.button("key_delete", "Delete", lambda: anim.delete_selection_keys(context), danger=True,
                         icon="key_delete", tip="Delete key"),
            vr_ui.toggle("autokey", "Auto", tool.use_keyframe_insert_auto,
                         lambda: setattr(tool, "use_keyframe_insert_auto", not tool.use_keyframe_insert_auto),
                         icon="record", tip="Auto keying"),
        ],
    ]
    more = [
        [vr_ui.select("interp", "Ease", names.get(interp, "-") if interp != 'MIXED' else "Mixed",
                      lambda: open_picker("Key interpolation", [Option(i, n) for i, n in anim.INTERPOLATIONS],
                                          interp, lambda v: anim.set_interpolation(context, v)))],
        [
            vr_ui.button("range_start", "Start", lambda: anim.set_start(context), icon="range_start",
                         tip="Start the range here"),
            vr_ui.button("range_end", "End", lambda: anim.set_end(context), icon="range_end",
                         tip="End the range here"),
        ],
        [
            vr_ui.toggle("show_paths", "Paths", settings.show_paths, _toggle(settings, "show_paths"), icon="paths",
                         tip="Motion paths"),
            vr_ui.toggle("onion", "Onion skin", settings.onion, _toggle(settings, "onion"), icon="onion"),
        ],
    ]
    return _disclose('TIMELINE', out, more, "Ease, range, paths, onion")
