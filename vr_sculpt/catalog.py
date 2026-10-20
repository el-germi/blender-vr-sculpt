"""Every primitive in Add > Mesh, including ones extensions append (e.g. Extra Mesh Objects).

Instead of a hard-coded list, the menu's draw functions run against a recording
layout, which notes each `layout.operator(...)` (with any preset properties set
on it) and follows `layout.menu(...)` into submenus. So whatever an extension
adds to Add > Mesh on the desktop shows up in VR too.
"""

import bpy

# Mesh-producing operators only; skips things like "object.parent_to_empty".
_OP_PREFIX = "mesh."
# Our own bmesh versions replace these two (non-uniform drag sizing, subdivisions).
BUILTIN_KIND = {"mesh.primitive_cube_add": 'CUBE', "mesh.primitive_cylinder_add": 'CYLINDER'}


class Entry:
    def __init__(self, idname, label, category, preset):
        self.idname = idname  # operator, e.g. "mesh.primitive_gear"
        self.label = label
        self.category = category  # submenu path, "" for the top level
        self.preset = preset  # properties the menu sets on the operator, e.g. {"change": False}

    @property
    def key(self):
        """Stable id for settings and widgets."""
        return self.idname + ("?" + "&".join(f"{k}={v}" for k, v in sorted(self.preset.items())) if self.preset else "")

    def __repr__(self):
        return f"Entry({self.category!r}/{self.label!r}: {self.idname} {self.preset})"


class _Props:
    """Stands in for the OperatorProperties that layout.operator() returns."""

    def __init__(self):
        object.__setattr__(self, "values", {})

    def __setattr__(self, name, value):
        self.values[name] = value

    def __getattr__(self, name):
        return None


class _Recorder:
    """A fake UILayout that records operators and submenus."""

    def __init__(self, sink):
        object.__setattr__(self, "sink", sink)

    def __setattr__(self, name, value):
        pass  # operator_context, enabled, scale_y, ...

    def operator(self, idname, text="", **_kw):
        props = _Props()
        self.sink.append(("op", idname, text, props))
        return props

    def menu(self, menu, text="", **_kw):
        self.sink.append(("menu", menu, text, None))

    def __getattr__(self, name):
        # row(), column(), separator(), label(), template_*()...: return something chainable.
        return lambda *a, **k: self


def _op_label(idname):
    try:
        mod, name = idname.split(".", 1)
        return getattr(getattr(bpy.ops, mod), name).get_rna_type().name or name
    except (AttributeError, ValueError, KeyError):
        return idname


def _draw_menu(menu_idname, context):
    menu_cls = getattr(bpy.types, menu_idname, None)
    if menu_cls is None:
        return []
    sink = []
    fake = type("FakeMenu", (), {})()
    fake.layout = _Recorder(sink)
    fake.bl_idname = menu_idname
    draw = menu_cls.draw
    # Extensions append to a menu by wrapping its draw; _draw_funcs holds them all.
    funcs = getattr(draw, "_draw_funcs", None) or [draw]
    for func in funcs:
        try:
            func(fake, context)
        except Exception as ex:  # One broken extension must not hide the rest.
            print(f"VR Sculpt: skipped a draw function of {menu_idname}: {ex}")
    return sink


def discover(context=None, root="VIEW3D_MT_mesh_add"):
    """List of Entry for every mesh operator reachable from the Add > Mesh menu."""
    context = context or bpy.context
    entries, seen_menus, seen_keys = [], set(), set()

    def walk(menu_idname, category, depth):
        if menu_idname in seen_menus or depth > 4:
            return
        seen_menus.add(menu_idname)
        for kind, ident, text, props in _draw_menu(menu_idname, context):
            if kind == "menu":
                sub_cls = getattr(bpy.types, ident, None)
                label = text or (sub_cls.bl_label if sub_cls else ident)
                walk(ident, f"{category}/{label}" if category else label, depth + 1)
            elif ident.startswith(_OP_PREFIX):
                entry = Entry(ident, text or _op_label(ident), category, dict(props.values))
                if entry.key not in seen_keys and _op_exists(ident):
                    seen_keys.add(entry.key)
                    entries.append(entry)

    walk(root, "", 0)
    return entries


def _op_exists(idname):
    mod, name = idname.split(".", 1)
    try:
        getattr(getattr(bpy.ops, mod), name).get_rna_type()
        return True
    except (AttributeError, KeyError):
        return False


_cache = {"entries": None}


def entries(refresh=False):
    if refresh or _cache["entries"] is None:
        _cache["entries"] = discover()
    return _cache["entries"]


def find(key):
    return next((e for e in entries() if e.key == key), None)
