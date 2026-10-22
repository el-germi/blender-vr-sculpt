"""What the pies offer in each mode, the knobs' values, the quick taps, and the mode/engine switches.

Four pies, one per face button (radial.py runs them, operators.py binds them):
  Y (left)   MODE    the modes, each opening that mode's tools; View; Help
  B (right)  TOOL    what the trigger does in this mode, and how big / strong
  A (right)  OBJECT  the thing you work on: the selected object's settings, the bone, the camera
  X (left)   TIME    the timeline (twist to scrub), keys, playback, undo / redo
The stick clicks are knobs: R turns the mode's size, L its amount (see knob_dial()).

Actions look up bpy.context when they run (a pie runs them several modal events after it was
built, and XR operators get a temporary context).
"""

import colorsys
import math

import bpy

from . import (anim, camera, catalog, desktop, generic, hardsurface, measure, native, paint, pose, primitives, puppet,
               shading, strokes, symmetry)
from .props import BRUSH_ITEMS, RADIUS_STEPS
from .radial import Dial, Item, Level

NATIVE_SHORT = {
    "Draw": "Draw", "Clay Strips": "Clay", "Smooth": "Smooth",
    "Inflate/Deflate": "Inflate", "Crease Sharp": "Crease", "Flatten/Contrast": "Flatten",
}

MODES = (('SCULPT', "Sculpt"), ('MODEL', "Model"), ('DRAW', "Draw"), ('PAINT', "Paint"), ('POSE', "Pose"),
         ('TOOLS', "Tools"))
MODE_COLORS = {
    'SCULPT': (1.0, 0.55, 0.2),
    'MODEL': (0.45, 0.88, 0.82),
    'DRAW': (0.78, 0.92, 0.35),
    'PAINT': (1.0, 0.45, 0.7),
    'POSE': (0.55, 0.8, 1.0),
    'TOOLS': (0.8, 0.8, 0.88),
}
VIEW_COLOR = (0.72, 0.66, 1.0)
TIME_COLOR = (1.0, 0.8, 0.3)

_previous_mode = {"mode": 'MODEL'}  # Y tap goes back to it
last_model_dial = {"dial": None}  # Model mode's R knob turns the last value turned on the object


def C():
    return bpy.context


def S():
    return bpy.context.scene.vr_sculpt


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
    if settings.mode != mode:
        _previous_mode["mode"] = settings.mode
    if settings.mode == 'POSE' and mode != 'POSE':
        pose.leave(context)
    settings.mode = mode
    settings.model_mark = 'NONE'
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


def _percent(v):
    return f"{v * 100:.0f}%"


# --- item helpers ---------------------------------------------------------------

def _act(id, label, icon, fn, active=False, danger=False, swatch=None, repeat=True, push=True, enabled=True):
    return Item(id, label, icon, action=fn, active=active, danger=danger, swatch=swatch, repeat=repeat, push=push,
                enabled=enabled)


def _toggle(id, label, icon, owner, prop):
    return Item(id, label, icon, action=lambda: setattr(owner, prop, not getattr(owner, prop)),
                active=bool(getattr(owner, prop)))


def _choice(id, label, icon, owner, prop, value):
    return Item(id, label, icon, action=lambda: setattr(owner, prop, value), active=getattr(owner, prop) == value)


def _dial(id, label, icon, get, set, lo, hi, scale='LINEAR', fmt=None, enabled=True, **kw):
    return Item(id, label, icon, dial=Dial(label, get, set, lo, hi, scale, fmt, **kw), enabled=enabled)


def _branch(id, label, icon, build, action=None, active=False):
    return Item(id, label, icon, action=action, children=build, active=active)


def _in_mode(mode, level):
    """Make the actions of a mode's level (reached from the Mode pie) switch to that mode first."""
    def run(fn):
        def wrapped():
            if bpy.context.scene.vr_sculpt.mode != mode:
                set_mode(bpy.context, mode)
            fn()
        return wrapped

    def children(build):
        return lambda: _in_mode(mode, build())

    for item in level.items:
        if item.action is not None:
            item.action = run(item.action)
        if item.children is not None:
            item.children = children(item.children)
    return level


# --- Y: modes --------------------------------------------------------------------

def mode_level(context=None):
    context = context or C()
    settings = context.scene.vr_sculpt
    items = [_branch("mode_" + ident.lower(), name, ident.lower(),
                     (lambda i=ident: _in_mode(i, tool_level(C(), i))),
                     action=(lambda i=ident: set_mode(C(), i)), active=settings.mode == ident)
             for ident, name in MODES]
    items.append(_branch("view", "View", 'view', lambda: view_level(C())))
    items.append(_act("hints", "Help", 'hints', _show_hints, repeat=False))
    return Level("Mode", items, color=MODE_COLORS[settings.mode])


def _show_hints():
    from . import radial
    radial.show_hints()


def previous_mode(context):
    mode = _previous_mode["mode"]
    if mode == context.scene.vr_sculpt.mode or mode not in dict(MODES):
        mode = 'MODEL' if context.scene.vr_sculpt.mode != 'MODEL' else 'SCULPT'
    set_mode(context, mode)
    return mode


def view_level(context=None):
    context = context or C()
    settings = context.scene.vr_sculpt
    xs = context.window_manager.xr_session_settings
    items = [
        _act("show_desktop", "Desktop", 'desktop', lambda: _show_desktop(C()), active=settings.show_desktop),
        _act("bring_desktop", "Bring here", 'bring', lambda: _bring_desktop(C())),
        _act("passthrough", "Passthrough", 'passthrough', lambda: _passthrough(C()),
             active=bool(xs and xs.show_passthrough)),
        _toggle("camera_preview", "Preview", 'preview', settings, "camera_preview"),
        _choice("nav_walk", "Walk", 'walk', settings, "nav_mode", 'WALK'),
        _choice("nav_fly", "Fly", 'fly', settings, "nav_mode", 'FLY'),
        _dial("nav_speed", "Speed", 'nav_speed', lambda: settings.nav_speed,
              lambda v: setattr(settings, "nav_speed", v), 0.2, 6.0, 'LOG', fmt=lambda v: f"{v:.1f} m/s"),
        _toggle("tracking", "Tracking", 'tracking', settings, "tracking"),
        _toggle("tracking_record", "Record", 'tracking_record', settings, "tracking_record"),
        _act("reset_view", "Reset view", 'reset_view', lambda: _reset_view(C())),
    ] + _grid_items(settings)
    return Level("View", items, color=VIEW_COLOR)


def _grid_items(settings, model=False):
    out = [
        _toggle("grid_show", "Grid", 'grid', settings, "grid_show"),
        _toggle("grid_snap", "Snap" if model else "Snap to grid", 'snap', settings, "grid_snap"),
    ]
    if model:
        out += [
            _toggle("snap_surface", "Surface", 'surface', settings, "snap_surface"),
            _toggle("snap_angle", "15\u00b0", 'angle', settings, "snap_angle"),
        ]
    out.append(_grid_size(settings))
    return out


def _grid_size(settings):
    return _dial("grid_size", "Grid size", 'grid_size', lambda: settings.grid_size,
                 lambda v: setattr(settings, "grid_size", v), 0.005, 0.5, 'LOG', fmt=_meters, length=True)


def _passthrough(context):
    xs = context.window_manager.xr_session_settings
    xs.show_passthrough = not xs.show_passthrough


def _show_desktop(context):
    settings = context.scene.vr_sculpt
    settings.show_desktop = not settings.show_desktop
    if settings.show_desktop:
        desktop.request_full_capture(context)
        ss = context.window_manager.xr_session_state
        if ss is not None:
            desktop.place_in_front(context, ss)


def _bring_desktop(context):
    if not context.scene.vr_sculpt.show_desktop:
        _show_desktop(context)  # Shows it in front of you.
        return
    ss = context.window_manager.xr_session_state
    if ss is not None:
        desktop.place_in_front(context, ss)


def _reset_view(context):
    """Back to where the VR session started (what A / X did in Blender's stock controls)."""
    ss = context.window_manager.xr_session_state
    if ss is not None:
        ss.navigation_location = (0.0, 0.0, 0.0)
        ss.navigation_rotation = (1.0, 0.0, 0.0, 0.0)
        ss.navigation_scale = 1.0


# --- B: the mode's tools -------------------------------------------------------------

def tool_level(context=None, mode=None):
    context = context or C()
    settings = context.scene.vr_sculpt
    mode = mode or settings.mode
    build = {'SCULPT': _sculpt_tools, 'MODEL': _model_tools, 'DRAW': _draw_tools, 'PAINT': _paint_tools,
             'POSE': _pose_tools, 'TOOLS': _tools_tools}[mode]
    level = build(context, settings)
    level.color = MODE_COLORS[mode]
    if level.center is None and level.items:
        level.center = knob_item(context, 'R', mode)
    return level


def _sculpt_tools(context, settings):
    if settings.engine == 'NATIVE':
        brush = native.sculpt_brush(context)
        current = brush.name if brush else ""
        items = [_act("brush_" + n, NATIVE_SHORT[n], "brush_" + n, (lambda n=n: native.activate_brush(C(), n)),
                      active=n == current) for n in native.NATIVE_BRUSHES]
    else:
        items = [_choice("brush_" + ident, name, "brush_" + ident, settings, "brush", ident)
                 for ident, name, _desc in BRUSH_ITEMS]
    items.append(_strength_item(context, settings))
    items.append(_radius_item(settings))
    return Level("Sculpt", items)


def _radius_item(settings):
    return _dial("radius", "Radius", 'radius', lambda: settings.radius, lambda v: setattr(settings, "radius", v),
                 0.003, 0.3, 'LOG', fmt=_meters, length=True)


def _strength_item(context, settings):
    if settings.engine == 'NATIVE':
        return _dial("strength", "Strength", 'strength', lambda: native.get_strength(C()) or 0.0,
                     lambda v: native.set_strength(C(), v), 0.0, 1.0, fmt=lambda v: f"{v:.2f}")
    return _dial("strength", "Strength", 'strength', lambda: settings.strength,
                 lambda v: setattr(settings, "strength", v), 0.0, 1.0, fmt=lambda v: f"{v:.2f}")


def _model_tools(context, settings):
    items = [_choice("add_as_" + ident.lower(), name, icon, settings, "model_add_as", ident)
             for (ident, name), icon in zip(ADD_AS, ('object', 'cut', 'union', 'intersect'))]
    items.insert(0, _branch("add", primitives.label(settings.primitive), 'add', lambda: add_level(C())))
    items.append(_branch("mark", "Mark", 'mark', lambda: _mark_level(S()), active=settings.model_mark != 'NONE'))
    items.append(_branch("snap", "Snap", 'snap', lambda: Level("Snap", _grid_items(S(), model=True)),
                         active=settings.grid_snap or settings.snap_surface or settings.snap_angle))
    return Level("Model", items)


ADD_AS = (('OBJECT', "Object"), ('DIFFERENCE', "Cut"), ('UNION', "Union"), ('INTERSECT', "Intersect"))
PRIMITIVE_ICONS = {
    "mesh.primitive_cube_add": 'object', "mesh.primitive_cylinder_add": 'prim_cylinder',
    "mesh.primitive_uv_sphere_add": 'sculpt', "mesh.primitive_ico_sphere_add": 'prim_ico',
    "mesh.primitive_cone_add": 'prim_cone', "mesh.primitive_torus_add": 'prim_torus',
    "mesh.primitive_plane_add": 'prim_plane', "mesh.primitive_circle_add": 'off',
    "mesh.primitive_grid_add": 'setup', "mesh.primitive_monkey_add": 'prim_monkey',
}


def add_level(context=None):
    """Add > Mesh: categories first (when there are several), then their entries, paged by More."""
    context = context or C()
    settings = context.scene.vr_sculpt
    entries = catalog.entries(refresh=True)

    def entry_item(e):
        return _choice("opt_" + e.key, e.label, PRIMITIVE_ICONS.get(e.idname, ""), settings, "primitive", e.key)

    categories = list(dict.fromkeys(e.category or "Mesh" for e in entries))
    if len(categories) <= 1:
        return Level("Add", [entry_item(e) for e in entries])
    current = next((e.category or "Mesh" for e in entries if e.key == settings.primitive), None)

    def category(cat):
        return Level(cat, [entry_item(e) for e in entries if (e.category or "Mesh") == cat])

    return Level("Add", [_branch("cat_" + cat, cat, 'object' if cat == "Mesh" else "",
                                 (lambda c=cat: category(c)), active=cat == current) for cat in categories])


MARKS = (('HARD', "Hard"), ('SOFT', "Soft"), ('CLEAR', "Clear"))


def _mark_level(settings):
    items = [_act("mark_" + ident.lower(), name, "mark_" + ident,
                  (lambda i=ident: setattr(S(), "model_mark", 'NONE' if S().model_mark == i else i)),
                  active=settings.model_mark == ident) for ident, name in MARKS]
    items.append(_act("mark_off", "Off", 'off', lambda: setattr(S(), "model_mark", 'NONE'),
                      active=settings.model_mark == 'NONE'))
    return Level("Mark edges", items)


def _draw_tools(context, settings):
    def stroke(style, name):
        return _act("style_" + style, name, style.lower(),
                    lambda: (setattr(S(), "draw_tool", 'DRAW'), setattr(S(), "draw_style", style)),
                    active=settings.draw_tool == 'DRAW' and settings.draw_style == style)

    items = [stroke('TUBE', "Tube"), stroke('RIBBON', "Ribbon"), stroke('NOTE', "Note"),
             _choice("tool_EDIT", "Edit", 'edit', settings, "draw_tool", 'EDIT'),
             _choice("tool_ERASE", "Erase", 'erase', settings, "draw_tool", 'ERASE'),
             _branch("draw_color", "Color", 'color', lambda: _draw_colors(S())),
             _width_item(settings)]
    return Level("Draw", items)


def _width_item(settings):
    return _dial("draw_width", "Width", 'width', lambda: settings.draw_width,
                 lambda v: setattr(settings, "draw_width", v), 0.0005, 0.1, 'LOG', fmt=_meters, length=True)


def _draw_colors(settings):
    return Level("Color", [_act("color_" + ident, name, "", (lambda i=ident: setattr(S(), "draw_color", i)),
                                active=settings.draw_color == ident, swatch=rgb)
                           for ident, name, rgb in strokes.PALETTE], color=strokes.COLORS[settings.draw_color])


def _paint_tools(context, settings):
    obj = paint.target(context)
    if obj is None:
        return Level("Paint", [], empty="Select a mesh to paint")
    if not paint.ready(obj):
        return _paint_setup_level(obj)
    items = [
        _branch("paint_brush", paint.SHORT.get(paint.brush(context).name, "Brush") if paint.brush(context) else "Brush",
                'paint', lambda: _paint_brushes(C())),
        _branch("paint_color", "Color", 'color', lambda: _paint_colors(C())),
        _choice("reach_contact", "Contact", 'contact', settings, "paint_reach", 'CONTACT'),
        _choice("reach_spray", "Spray", 'spray', settings, "paint_reach", 'SPRAY'),
        _act("paint_pick", "Pick color", 'pick',
             lambda: setattr(S(), "paint_tool", 'PAINT' if S().paint_tool == 'PICK' else 'PICK'),
             active=settings.paint_tool == 'PICK'),
        _act("paint_swap", "Swap", 'swap', lambda: paint.swap_colors(C())),
        _paint_strength(context),
        _dial("paint_radius", "Radius", 'radius', lambda: settings.paint_radius,
              lambda v: setattr(settings, "paint_radius", v), 0.002, 0.15, 'LOG', fmt=_meters, length=True),
    ]
    return Level("Paint", [i for i in items if i is not None])


def _paint_strength(context):
    b = paint.brush(context)
    if b is None:
        return None
    return _dial("paint_strength", "Strength", 'strength', lambda: b.strength, lambda v: setattr(b, "strength", v),
                 0.0, 1.0, fmt=lambda v: f"{v:.2f}")


def _paint_setup_level(obj):
    settings = S()
    items = [_act("paint_size_" + i.identifier, f"Set up {i.name}", 'setup',
                  (lambda size=i.identifier: _paint_setup(C(), size)), active=settings.paint_texture_size == i.identifier)
             for i in settings.bl_rna.properties["paint_texture_size"].enum_items]
    return Level("Paint", items, empty=f"{obj.name} needs UVs and an image")


def _paint_setup(context, size=None):
    settings = context.scene.vr_sculpt
    if size is not None:
        settings.paint_texture_size = size
    obj = paint.target(context)
    if obj is not None:
        paint.setup(context, obj, int(settings.paint_texture_size))


def _paint_brushes(context):
    b = paint.brush(context)
    current = b.name if b else ""
    return Level("Brush", [_act("pbrush_" + n, paint.SHORT[n], "pbrush_" + paint.SHORT[n],
                                (lambda n=n: paint.activate_brush(C(), n)), active=n == current)
                           for n in paint.BRUSHES])


def _paint_colors(context):
    color = paint.get_color(context)
    items = [_act("pcolor_" + ident, name, "", (lambda rgb=rgb: paint.set_color(C(), rgb)),
                  active=all(abs(a - b) < 1e-3 for a, b in zip(color, rgb)), swatch=rgb)
             for ident, name, rgb in strokes.PALETTE]
    return Level("Color", items, center=_hsv_item(context, 'h'), color=tuple(color))


def _hsv_item(context, which):
    names = {'h': ("paint_hue", "Hue", 'hue'), 's': ("paint_sat", "Saturation", 'saturation'),
             'v': ("paint_val", "Value", 'value')}
    ident, label, icon = names[which]
    index = "hsv".index(which)

    def get():
        return paint.get_hsv(C())[index]

    def set_value(x):
        if which == 'h':
            _h, sat, val = paint.get_hsv(C())
            if sat < 0.05:
                # A gray has no hue to turn: turning it picks a full color.
                paint.set_hsv(C(), h=x, s=1.0, v=max(val, 0.5))
                return
        paint.set_hsv(C(), **{which: x})

    def gradient(f):
        hsv = list(paint.get_hsv(C()))
        hsv[index] = f
        if index == 0:
            hsv[1:] = [1.0, 1.0]  # The hue ring shows pure hues.
        return colorsys.hsv_to_rgb(*hsv)

    fmt = (lambda x: f"{x * 360:.0f}\u00b0") if which == 'h' else _percent
    return _dial(ident, label, icon, get, set_value, 0.0, 1.0, fmt=fmt, gradient=gradient)


def _pose_tools(context, settings):
    obj = pose.rig(context)
    if obj is None:
        return Level("Objects", [_branch("pose_rig", "Objects", 'rig', lambda: _rig_level(C()))])
    items = [
        _choice("pose_fk", "FK", 'FK', settings, "pose_tool", 'FK'),
        _choice("pose_ik", "IK", 'IK', settings, "pose_tool", 'IK'),
        _chain_item(settings),
        _toggle("pose_mirror", "Mirror X", 'mirror', settings, "pose_mirror"),
        _toggle("pose_bones", "Bones", 'bones', settings, "pose_show_bones"),
        _branch("pose_rig", obj.name if obj else "Objects", 'rig', lambda: _rig_level(C())),
    ]
    return Level("Pose", items)


def _chain_item(settings):
    return _dial("pose_chain", "IK chain", 'chain', lambda: settings.pose_chain,
                 lambda v: setattr(settings, "pose_chain", int(v)), 1, 6, 'STEP')


def _rig_level(context):
    current = pose.rig(context)
    name = current.name if current else ""
    items = [_act("rig_" + o.name, o.name, 'rig', (lambda n=o.name: pose.choose_rig(C(), n)), active=o.name == name)
             for o in pose.rigs(context)]
    items.append(_act("rig_none", "Objects", 'object', lambda: pose.choose_rig(C(), ""), active=not name))
    return Level("Rig", items)


def _tools_tools(context, settings):
    items = [
        _choice("tool_measure", "Measure", 'measure', settings, "tool", 'MEASURE'),
        _choice("tool_camera", "Camera", 'camera', settings, "tool", 'CAMERA'),
    ] + _grid_items(settings)
    return Level("Tools", items)


# --- A: the object (or bone, stroke settings, camera) -------------------------------------------

def object_level(context=None):
    context = context or C()
    settings = context.scene.vr_sculpt
    build = {'SCULPT': _sculpt_object, 'MODEL': _model_object, 'DRAW': _draw_object, 'PAINT': _paint_object,
             'POSE': _pose_object, 'TOOLS': _tools_object}[settings.mode]
    level = build(context, settings)
    level.color = level.color or MODE_COLORS[settings.mode]
    return level


def _sculpt_object(context, settings):
    obj = context.view_layer.objects.active
    axes = symmetry.sculpt_axes(context)
    items = [_act("sym_" + a, f"Symmetry {a}", "axis_" + a, (lambda i=i: symmetry.sculpt_toggle(C(), i)),
                  active=axes[i]) for i, a in enumerate(symmetry.AXES)]
    items += [
        _act("engine_python", "Python", 'python', lambda: set_engine(C(), 'PYTHON'), active=settings.engine == 'PYTHON'),
        _act("engine_native", "Native", 'native', lambda: set_engine(C(), 'NATIVE'), active=settings.engine == 'NATIVE'),
    ]
    return Level(obj.name if obj is not None else "Sculpt", items)


def _model_object(context, settings):
    obj = context.view_layer.objects.active
    if obj is None or not obj.select_get():
        return Level("Model", [], empty="Touch an object to select it")
    title = f"{obj.name} (cutter)" if hardsurface.is_cutter(obj) else obj.name
    items = []
    if obj.type == 'MESH':
        solid = hardsurface.solidify(obj) is not None
        items += [
            _branch("page_shape", "Shape", 'shape', lambda: _shape_level(C(), obj)),
            _branch("page_boolean", "Cuts", 'cuts', lambda: _boolean_level(C(), obj),
                    active=bool(hardsurface.booleans(obj)) or hardsurface.is_cutter(obj)),
            _branch("page_finish", "Finish", 'finish', lambda: _finish_level(C(), S(), obj),
                    active=shading.mode(obj) is not None or hardsurface.has_bevel(obj)),
            _branch("page_array", "Array", 'array', lambda: _array_level(C(), obj),
                    active=hardsurface.array_count(obj) > 1),
            _branch("mirror", "Mirror", 'mirror', lambda: _mirror_level(C(), obj),
                    active=any(symmetry.model_axes(obj))),
            _branch("solidify", "Solidify", 'solidify', lambda: _solidify_level(obj),
                    action=lambda: hardsurface.set_solidify(obj, hardsurface.solidify(obj) is None), active=solid),
        ]
    items += [
        _act("duplicate", "Duplicate", 'duplicate', lambda: hardsurface.duplicate(C(), obj)),
        _act("delete", "Delete", 'delete', lambda: hardsurface.delete(C(), obj), danger=True),
    ]
    return Level(title, items)


def _model_dial(item, obj):
    """A dial on the selected object: remembered for Model mode's R knob once it is turned."""
    name = obj.name
    item.dial.on_turn = lambda dial: last_model_dial.__setitem__("dial", (name, dial))
    return item


def _shape_level(context, obj):
    prim = obj.vr_prim
    items = []
    if prim.kind == 'CUBE':
        for i, axis in enumerate("XYZ"):
            items.append(_model_dial(_dial(f"size_{axis}", f"Size {axis}", "axis_" + axis,
                                           (lambda i=i: obj.vr_prim.size[i]), _setter(obj, "size", i), 0.005, 2.0,
                                           'LOG', fmt=_meters), obj))
        items.append(_model_dial(_dial("subdivisions", "Subdivisions", 'segments', lambda: obj.vr_prim.subdivisions,
                                       _setter(obj, "subdivisions"), 0, 16, 'STEP'), obj))
    elif prim.kind == 'CYLINDER':
        items += [
            _model_dial(_dial("radius_prim", "Radius", 'radius', lambda: obj.vr_prim.radius, _setter(obj, "radius"),
                              0.003, 1.0, 'LOG', fmt=_meters), obj),
            _model_dial(_dial("height", "Height", 'width', lambda: obj.vr_prim.height, _setter(obj, "height"),
                              0.005, 2.0, 'LOG', fmt=_meters), obj),
            _model_dial(_dial("segments", "Segments", 'segments', lambda: obj.vr_prim.segments,
                              _setter(obj, "segments"), 3, 64, 'STEP'), obj),
        ]
    elif prim.kind == 'OPERATOR':
        return _operator_level(context, obj)
    else:
        return Level("Shape", [], empty="Plain mesh (sculpted, or not a VR primitive)")
    return Level("Shape", items)


def _operator_level(context, obj):
    entry = catalog.find(obj.vr_prim.op_key)
    if entry is None:
        return Level("Shape", [], empty="Its Add > Mesh entry is gone (extension off?)")
    specs = generic.params(entry, obj)
    if not specs:
        return Level("Shape", [], empty="No settings")
    return Level(entry.label, [_param_item(obj, s) for s in specs])


def _param_item(obj, spec):
    wid = "param_" + spec.id

    def set_value(v, ident=spec.id):
        # Slow generators (e.g. Wall Factory) regenerate when the pie or knob lets go.
        live = not (spec.type in {'INT', 'FLOAT'} and obj.get("vrs_slow", False))
        generic.set_param(C(), obj, ident, v, live=live)

    def get(ident=spec.id, default=spec.value):
        return generic.stored(obj).get(ident, default)

    if spec.type == 'ENUM':
        names = dict(spec.items)
        return _branch(wid, f"{spec.label}: {names.get(spec.value, spec.value)}", "",
                       lambda: Level(spec.label, [_act("opt_" + str(i), n, "", (lambda i=i: set_value(i)),
                                                       active=i == spec.value) for i, n in spec.items]))
    if spec.type == 'BOOLEAN':
        return _act(wid, spec.label, 'on' if spec.value else 'off', lambda: set_value(not spec.value),
                    active=spec.value)
    if spec.type == 'INT':
        return _model_dial(_dial(wid, spec.label, "", get, set_value, spec.lo, spec.hi, 'STEP'), obj)
    fmt = _meters if spec.length else _degrees if spec.angle else _number
    scale = 'LOG' if spec.length and spec.lo > 0 else 'LINEAR'
    return _model_dial(_dial(wid, spec.label, "", get, set_value, spec.lo, spec.hi, scale, fmt=fmt), obj)


def _mirror_level(context, obj):
    axes = symmetry.model_axes(obj)
    items = [_act("mirror_" + a, f"Mirror {a}", "axis_" + a, (lambda i=i: symmetry.model_toggle(C(), obj, i)),
                  active=axes[i]) for i, a in enumerate(symmetry.AXES)]
    items.append(_act("mirror_here", "Center here", 'here', lambda: _cursor_to_left_hand(C())))
    return Level("Mirror", items)


def _solidify_level(obj):
    solid = hardsurface.solidify(obj) is not None
    items = [_act("solidify_toggle", "On" if not solid else "Off", 'on' if not solid else 'off',
                  lambda: hardsurface.set_solidify(obj, not solid), active=solid),
             _model_dial(_dial("solidify_thickness", "Thickness", 'thickness',
                               lambda: hardsurface.solidify_thickness(obj),
                               lambda v: hardsurface.set_solidify_thickness(obj, v), 0.0005,
                               max(0.05, 0.5 * max(obj.dimensions)), 'LOG', fmt=_meters, enabled=solid), obj)]
    return Level("Solidify", items)


def _boolean_level(context, obj):
    if hardsurface.is_cutter(obj):
        target = hardsurface.target_of(obj)
        mod = hardsurface.modifier_of(obj)
        if target is None or mod is None:
            return Level("Cuts", [], empty="Cuts nothing (its object is gone)")
        items = [_act("bool_" + ident.lower(), name, icon, (lambda i=ident: hardsurface.set_operation(obj, i)),
                      active=mod.operation == ident)
                 for (ident, name), icon in zip(hardsurface.OPERATIONS, ('cut', 'union', 'intersect'))]
        items += [
            _act("bool_target", "Select object", 'select', lambda: primitives.make_active(C(), target)),
            _act("bool_apply", "Apply", 'apply', lambda: hardsurface.apply_cutter(C(), obj)),
            _act("bool_remove", "Remove", 'delete', lambda: hardsurface.delete(C(), obj), danger=True),
        ]
        return Level(f"Cuts {target.name}", items)
    mods = hardsurface.booleans(obj)
    if not mods:
        return Level("Cuts", [], empty="No cuts yet: B > Cut, then drag on the object")
    visible = hardsurface.cutters_visible(context)
    exact = all(m.solver == 'EXACT' for m in mods)

    def set_solver(solver):
        for m in hardsurface.booleans(obj):
            m.solver = solver

    return Level(f"{len(mods)} cutter{'s' if len(mods) != 1 else ''}", [
        _act("cutters_show", "Show cutters", 'view' if visible else 'eye_off',
             lambda: hardsurface.set_cutters_visible(C(), not visible), active=visible),
        _act("bool_apply_all", "Apply all", 'apply', lambda: hardsurface.apply_all(C(), obj), danger=True),
        _act("solver_fast", "Fast", 'fast', lambda: set_solver('MANIFOLD'), active=not exact),
        _act("solver_exact", "Exact", 'exact', lambda: set_solver('EXACT'), active=exact),
    ])


def _finish_level(context, settings, obj):
    current = shading.mode(obj)
    items = [_act("shade_" + ident.lower(), name, "shade_" + ident,
                  (lambda i=ident: shading.set_mode(obj, None if shading.mode(obj) == i else i)), active=current == ident)
             for ident, name in shading.MODES]
    # Eight slices whatever is on: what doesn't apply yet is greyed out, so nothing moves.
    items.append(_model_dial(_dial("shade_angle", "Angle", 'angle', lambda: shading.angle(obj),
                                   lambda v: shading.set_angle(obj, v), math.radians(1.0), math.radians(180.0),
                                   fmt=_degrees, enabled=current == 'AUTO'), obj))
    items.append(_branch("mark", "Mark", 'mark', lambda: _mark_level(S()), active=settings.model_mark != 'NONE'))
    on = hardsurface.has_bevel(obj)
    items.append(_act("bevel", "Bevel", 'bevel', lambda: hardsurface.set_bevel(obj, not on), active=on))
    dims = [d for d in obj.dimensions if d > 1e-6]
    hi = max(0.25 * min(dims), 0.002) if dims else 0.02
    items.append(_model_dial(_dial("bevel_width", "Width", 'width', lambda: hardsurface.bevel_width(obj),
                                   lambda v: hardsurface.set_bevel_width(obj, v), 0.0002, hi, 'LOG',
                                   fmt=_meters, enabled=on), obj))
    items.append(_model_dial(_dial("bevel_segments", "Segments", 'segments', lambda: hardsurface.bevel(obj).segments,
                                   lambda v: setattr(hardsurface.bevel(obj), "segments", int(v)), 1, 8, 'STEP',
                                   enabled=on), obj))
    return Level("Finish", items)


def _array_level(context, obj):
    count = hardsurface.array_count(obj)
    items = [_model_dial(_dial("array_count", "Count", 'array_count', lambda: hardsurface.array_count(obj),
                               lambda v: hardsurface.set_array_count(C(), obj, v), 1, 24, 'STEP',
                               fmt=lambda v: "Off" if int(v) <= 1 else str(int(v))), obj)]
    on = count > 1  # Without copies the rest is greyed out (in place, so nothing moves when it turns on).
    mode = hardsurface.array_mode(obj) if on else 'LINEAR'
    axis = hardsurface.array_axis(obj) if on else -1
    items += [
        _act("array_linear", "Linear", 'array_LINEAR', lambda: hardsurface.set_array_mode(C(), obj, 'LINEAR'),
             active=on and mode == 'LINEAR', enabled=on),
        _act("array_radial", "Radial", 'array_RADIAL', lambda: hardsurface.set_array_mode(C(), obj, 'RADIAL'),
             active=on and mode == 'RADIAL', enabled=on),
    ]
    items += [_act("array_axis_" + a, ("Around " if mode == 'RADIAL' else "Along ") + a, "axis_" + a,
                   (lambda i=i: hardsurface.set_array_axis(C(), obj, i)), active=axis == i, enabled=on)
              for i, a in enumerate(symmetry.AXES)]
    if mode == 'RADIAL':
        items.append(_act("array_center", "Center here", 'here', lambda: _array_center_here(C(), obj)))
    else:
        items.append(_model_dial(_dial("array_gap", "Gap", 'gap', lambda: hardsurface.array_gap(obj),
                                       lambda v: hardsurface.set_array_gap(C(), obj, v), -0.5, 2.0,
                                       fmt=_percent, enabled=on), obj))
    return Level("Array", items)


def _left_tip(context):
    """Where the left controller's tip is, or None outside VR."""
    ss = context.window_manager.xr_session_state
    if ss is None:
        return None
    from . import operators
    pose_ = operators.controller_pose(ss.controller_aim_location_get(context, 0),
                                      ss.controller_aim_rotation_get(context, 0))
    return operators.brush_tip(pose_, context.scene.vr_sculpt.tip_offset * ss.viewer_scale)


def _cursor_to_left_hand(context):
    """Put the 3D cursor (the mirror center) at the left controller's tip."""
    tip = _left_tip(context)
    if tip is not None:
        symmetry.move_center(context, tip)


def _array_center_here(context, obj):
    tip = _left_tip(context)
    if tip is not None:
        hardsurface.set_array_center(context, obj, tip)


def _draw_object(context, settings):
    items = [_toggle("mirror_" + a.lower(), f"Mirror {a}", "axis_" + a, settings, "mirror_" + a.lower())
             for a in symmetry.AXES]
    items += [
        _act("mirror_here", "Center here", 'here', lambda: _cursor_to_left_hand(C())),
        _branch("draw_thickness", "Thickness", 'thickness', lambda: _thickness_level(S())),
        _dial("draw_smoothing", "Smoothing", 'smoothing', lambda: settings.draw_smoothing,
              lambda v: setattr(settings, "draw_smoothing", v), 0.0, 0.95, fmt=lambda v: f"{v:.2f}"),
        _act("undo_stroke", "Undo stroke", 'undo', lambda: _undo_stroke(C()), repeat=True),
    ]
    return Level("Strokes", items)


THICKNESS_ICONS = {'PRESSURE': 'pressure', 'SPEED_THIN': 'speed', 'SPEED_THICK': 'speed', 'TAPER': 'taper',
                   'FIXED': 'fixed'}


def _thickness_level(settings):
    items = [_choice("opt_" + i.identifier, i.name, THICKNESS_ICONS.get(i.identifier, ""), settings,
                     "draw_thickness", i.identifier)
             for i in settings.bl_rna.properties["draw_thickness"].enum_items]
    items.append(_dial("draw_taper", "Taper", 'taper', lambda: settings.draw_taper,
                       lambda v: setattr(settings, "draw_taper", v), 0.05, 0.5, fmt=_percent))
    return Level("Thickness", items)


def _undo_stroke(context):
    from . import operators
    return operators.undo_last_stroke(context)


def _paint_object(context, settings):
    obj = paint.target(context)
    if obj is None:
        return Level("Paint", [], empty="Select a mesh to paint")
    if not paint.ready(obj):
        return _paint_setup_level(obj)
    items = [_hsv_item(context, w) for w in "hsv"]
    items += [
        _act("paint_swap", "Swap", 'swap', lambda: paint.swap_colors(C())),
        _act("paint_undo", "Undo stroke", 'undo', lambda: paint.undo_last(C())),
        _branch("paint_resetup", "Image", 'setup', lambda: _paint_setup_level(obj)),
    ]
    return Level(obj.name, items, color=tuple(paint.get_color(context)))


def _bind(context, source):
    puppet.bind(context, context.window_manager.xr_session_state, source)


def _pose_object(context, settings):
    obj = pose.rig(context)
    items = []
    title = "Objects"
    if obj is not None:
        pb = pose.active_bone(obj)
        title = pb.name if pb is not None else "Touch a bone"
        items += [
            _act("pose_reset", "Reset bone", 'reset', lambda: pose.reset_selected(C())),
            _act("pose_reset_all", "Reset pose", 'reset_all', lambda: pose.reset_selected(C(), True), danger=True),
        ]
    items += [_act("bind_" + ident.lower(), "Bind " + short, "bind_" + ident, (lambda i=ident: _bind(C(), i)))
              for ident, short in (('LEFT', "L"), ('RIGHT', "R"), ('HEAD', "head"))]
    n = puppet.binding_count()
    if n:
        items.append(_act("unbind", f"Unbind ({n})", 'unbind', puppet.unbind_all, danger=True))
    recording = puppet.state() is not None
    items.append(_act("puppet_rec", "Stop rec" if recording else "Rec", 'stop' if recording else 'rec',
                      lambda: puppet.toggle_record(C()), active=recording, repeat=False))
    return Level(title, items)


def _tools_object(context, settings):
    scene = context.scene
    if settings.tool == 'MEASURE':
        n = len(measure.stored(scene))
        return Level("Measure", [
            _toggle("measures_show", "Show", 'measures_show', settings, "show_measures"),
            _act("measure_undo", "Undo last", 'measure_undo', lambda: measure.undo_last(C().scene)),
            _act("measure_clear", f"Clear ({n})", 'measure_clear', lambda: measure.clear(C().scene), danger=True),
        ])
    cam = scene.camera
    items = [_branch("camera", cam.name if cam else "Cameras", 'camera', lambda: _camera_level(C()))]
    if settings.show_new_camera:
        items.append(_act("camera_new", "New here", 'camera_new', lambda: _new_camera_here(C())))
    items.append(_toggle("camera_preview", "Pin preview", 'preview', settings, "camera_preview"))
    if cam is not None and cam.data.type == 'PERSP':
        items.append(_lens_item(cam))
    return Level("Camera", items)


def _lens_item(cam):
    data = cam.data
    return _dial("camera_lens", "Lens", 'lens', lambda: data.lens, lambda v: setattr(data, "lens", v), 10.0, 200.0,
                 'LOG', fmt=lambda v: f"{v:.0f} mm")


def _camera_level(context):
    scene = context.scene
    current = scene.camera.name if scene.camera else ""

    def choose(name):
        obj = C().scene.objects.get(name)
        if obj is not None:
            C().scene.camera = obj

    return Level("Camera", [_act("cam_" + o.name, o.name, 'camera', (lambda n=o.name: choose(n)),
                                 active=o.name == current) for o in camera.cameras(scene)],
                 empty="No cameras: New here adds one")


def _new_camera_here(context):
    ss = context.window_manager.xr_session_state
    pose_ = None
    if ss is not None:
        from . import operators
        pose_ = operators.controller_pose(ss.controller_aim_location_get(context, 1),
                                          ss.controller_aim_rotation_get(context, 1))
    camera.new_camera(context, pose_)


# --- X: time -------------------------------------------------------------------------

def time_level(context=None):
    context = context or C()
    tool = context.scene.tool_settings
    playing = anim.is_playing(context)
    items = [
        _act("play", "Stop" if playing else "Play", 'stop' if playing else 'play', lambda: anim.toggle_play(C()),
             active=playing, repeat=False),
        _act("key_next", "Next key", 'key_next', lambda: anim.jump_key(C(), 1)),
        _act("key_insert", "Key", 'key', lambda: anim.key_selection(C())),
        _act("key_delete", "Delete key", 'key_delete', lambda: anim.delete_selection_keys(C()), danger=True),
        _act("autokey", "Auto key", 'autokey',
             lambda: setattr(C().scene.tool_settings, "use_keyframe_insert_auto",
                             not C().scene.tool_settings.use_keyframe_insert_auto),
             active=tool.use_keyframe_insert_auto),
        _branch("time_more", "More", 'more', lambda: _time_more(C())),
        _act("undo", "Undo", 'undo', lambda: history(C(), 'UNDO'), repeat=False, push=False),
        _act("key_prev", "Prev key", 'key_prev', lambda: anim.jump_key(C(), -1)),
    ]
    return Level("Time", items, center=frame_item(context), color=TIME_COLOR)


def frame_item(context):
    """The current frame as a dial (5 degrees a frame), with the selection's keys marked on its gauge."""
    scene = context.scene
    start, end = scene.frame_start, scene.frame_end
    span = max(end - start, 1)
    dial = Dial("Frame", lambda: C().scene.frame_current, lambda v: anim.set_frame(C(), v), start, end, 'STEP',
                fmt=lambda v: str(int(v)), step_angle=math.radians(5.0))
    dial.ticks = lambda: [(f - start) / span for f in anim.key_frames(anim.selection(C())) if start <= f <= end]
    return Item("frame", "Frame", 'frame', dial=dial)


def _time_more(context):
    settings = context.scene.vr_sculpt
    interp = anim.interpolation_at(context)
    names = dict(anim.INTERPOLATIONS)
    redo = _act("redo", "Redo", 'redo', lambda: history(C(), 'REDO'), repeat=False, push=False)
    return Level("Time", [
        _branch("interp", "Ease: " + (names.get(interp, "-") if interp != 'MIXED' else "Mixed"), 'ease',
                lambda: Level("Ease", [_act("ease_" + i, n, 'ease', (lambda i=i: anim.set_interpolation(C(), i)),
                                            active=interp == i) for i, n in anim.INTERPOLATIONS])),
        _act("range_start", "Start here", 'range_start', lambda: anim.set_start(C())),
        _act("range_end", "End here", 'range_end', lambda: anim.set_end(C())),
        _toggle("show_paths", "Paths", 'paths', settings, "show_paths"),
        _toggle("onion", "Onion", 'onion', settings, "onion"),
        redo,
        _act("frame_prev", "Frame -1", 'key_prev', lambda: anim.step(C(), -1)),
        _act("frame_next", "Frame +1", 'key_next', lambda: anim.step(C(), 1)),
    ], color=TIME_COLOR)


def history(context, which):
    """Blender's own undo / redo (the pie operator doesn't push a step for these)."""
    window, _view = _desktop_view(context)
    try:
        if window is not None:
            with context.temp_override(window=window):
                (bpy.ops.ed.undo if which == 'UNDO' else bpy.ops.ed.redo)()
        else:
            (bpy.ops.ed.undo if which == 'UNDO' else bpy.ops.ed.redo)()
        return True
    except RuntimeError:
        return False


def root_level(context, kind):
    return {'MODE': mode_level, 'TOOL': tool_level, 'OBJECT': object_level, 'TIME': time_level}[kind](context)


def find(context, id):
    """The first item with this id in the tool, object, time or mode pie (for tests and previews)."""
    from . import radial
    for kind in ('TOOL', 'OBJECT', 'TIME', 'MODE'):
        item = radial.find(root_level(context, kind), id)
        if item is not None:
            return item
    return None


# --- knobs -------------------------------------------------------------------------------

def knob_item(context, side, mode=None):
    """The Item turned by a stick-click knob: 'R' the mode's size, 'L' its amount."""
    settings = context.scene.vr_sculpt
    mode = mode or settings.mode
    if side == 'R':
        if mode == 'SCULPT':
            return _radius_item(settings)
        if mode == 'DRAW':
            return _width_item(settings)
        if mode == 'PAINT':
            return _dial("paint_radius", "Radius", 'radius', lambda: settings.paint_radius,
                         lambda v: setattr(settings, "paint_radius", v), 0.002, 0.15, 'LOG', fmt=_meters, length=True)
        if mode == 'POSE':
            return _chain_item(settings)
        if mode == 'MODEL':
            remembered = last_model_dial["dial"]
            if remembered is not None and remembered[0] in bpy.data.objects:
                try:
                    remembered[1].get()
                    return Item("last_dial", remembered[1].label, "", dial=remembered[1])
                except (ReferenceError, AttributeError, KeyError):
                    last_model_dial["dial"] = None
            return _grid_size(settings)
        cam = context.scene.camera
        if settings.tool == 'CAMERA' and cam is not None and cam.data.type == 'PERSP':
            return _lens_item(cam)
        return _grid_size(settings)
    if mode == 'SCULPT':
        return _strength_item(context, settings)
    if mode == 'DRAW':
        return _dial("draw_smoothing", "Smoothing", 'smoothing', lambda: settings.draw_smoothing,
                     lambda v: setattr(settings, "draw_smoothing", v), 0.0, 0.95, fmt=lambda v: f"{v:.2f}")
    if mode == 'PAINT':
        item = _paint_strength(context)
        if item is not None:
            return item
    return frame_item(context)


def knob_dial(context, side):
    item = knob_item(context, side)
    return item.dial if item is not None else None


# --- taps -------------------------------------------------------------------------------

def tap(context, kind):
    """A face button pressed and let go without picking anything. Returns a short message, or None."""
    from . import radial
    settings = context.scene.vr_sculpt
    if kind == 'MODE':
        mode = previous_mode(context)
        return dict(MODES)[mode]
    if kind == 'TIME':
        anim.toggle_play(context)
        return "Play" if anim.is_playing(context) else "Stop"
    if kind == 'OBJECT':
        item = radial.last_action
        if item is None:
            return "Hold A for the object's pie"
        try:
            item.action()
        except ReferenceError:
            radial.last_action = None
            return None
        return "Again: " + item.label
    return quick_action(context, settings)


def quick_action(context, settings):
    """B tap: next brush (Sculpt, Paint), next primitive (Model), undo the last stroke (Draw), FK / IK (Pose),
    remove the last measurement or switch camera (Tools)."""
    if settings.mode == 'DRAW':
        what = _undo_stroke(context)
        return f"Undo {what}" if what else "Nothing to undo"
    if settings.mode == 'PAINT':
        return "Undo paint stroke" if paint.undo_last(context) else "Nothing to undo"
    if settings.mode == 'POSE':
        settings.pose_tool = 'IK' if settings.pose_tool == 'FK' else 'FK'
        return settings.pose_tool
    if settings.mode == 'TOOLS':
        if settings.tool == 'CAMERA':
            camera.next_camera(context)
            return context.scene.camera.name if context.scene.camera else None
        measure.undo_last(context.scene)
        return "Removed the last measurement"
    if settings.mode == 'MODEL':
        keys = [e.key for e in catalog.entries()]
        if keys:
            i = keys.index(settings.primitive) if settings.primitive in keys else -1
            settings.primitive = keys[(i + 1) % len(keys)]
        return primitives.label(settings.primitive)
    if settings.engine == 'NATIVE':
        native.cycle_brush(context)
        brush = native.sculpt_brush(context)
        return brush.name if brush else None
    ids = [b[0] for b in BRUSH_ITEMS]
    settings.brush = ids[(ids.index(settings.brush) + 1) % len(ids)]
    return dict((b[0], b[1]) for b in BRUSH_ITEMS)[settings.brush]


def knob_tap(context, side):
    """A stick click without a twist: R steps the radius (Pose: IK chain), L shows the controls."""
    from . import radial
    settings = context.scene.vr_sculpt
    if side == 'L':
        radial.show_hints()
        return None
    if settings.mode == 'POSE':
        settings.pose_chain = settings.pose_chain % 4 + 1
        return f"IK chain {settings.pose_chain}"
    if settings.mode == 'DRAW':
        prop, steps = "draw_width", WIDTH_STEPS
    elif settings.mode in {'SCULPT', 'PAINT'}:
        prop, steps = ("paint_radius" if settings.mode == 'PAINT' else "radius"), RADIUS_STEPS
    else:
        return "Hold the right stick down and twist: " + knob_item(context, 'R').dial.label
    bigger = [r for r in steps if r > getattr(settings, prop) + 1e-6]
    setattr(settings, prop, bigger[0] if bigger else steps[0])
    return ("Width " if prop == "draw_width" else "Radius ") + _meters(getattr(settings, prop))


WIDTH_STEPS = (0.002, 0.004, 0.008, 0.016, 0.032)
