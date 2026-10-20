"""Primitives made by their own add-mesh operator (UV Sphere, Torus, Gear, ...).

The object remembers which menu entry made it and the settings changed in VR.
Changing a setting re-runs the operator and swaps the new mesh into the object,
so its transform, name and selection stay. Panel widgets come straight from
the operator's RNA properties.
"""

import json
import math

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import catalog

# Never shown or scaled: placement, internal flags.
EXCLUDE = {"align", "location", "rotation", "scale", "enter_editmode", "change", "name"}
_LENGTH_WORDS = ("radius", "size", "width", "height", "depth", "length", "thick", "diameter")


def operator(idname):
    mod, name = idname.split(".", 1)
    return getattr(getattr(bpy.ops, mod), name)


def rna(entry):
    return operator(entry.idname).get_rna_type()


def param_props(entry):
    """Operator properties shown in the panel, in the operator's order."""
    out = []
    for p in rna(entry).properties:
        ident = p.identifier
        if ident == "rna_type" or ident in EXCLUDE or ident in entry.preset or p.is_hidden:
            continue
        if p.type not in {'FLOAT', 'INT', 'BOOLEAN', 'ENUM'}:
            continue
        if p.type in {'FLOAT', 'INT', 'BOOLEAN'} and p.array_length > 0:
            continue
        if p.type == 'ENUM' and p.is_enum_flag:
            continue
        # Extra Mesh Objects marks its objects with capitalized bools ("Gear", "Roundcube").
        if p.type == 'BOOLEAN' and ident[:1].isupper():
            continue
        out.append(p)
    return out


# Check the unit before the subtype: reading .subtype of a Python-defined property that
# has a unit logs "matches no enum" warnings (the unit is packed into the subtype).

def is_length(p):
    if p.type != 'FLOAT' or p.array_length > 0:
        return False
    unit = getattr(p, "unit", 'NONE')
    if unit != 'NONE':
        return unit == 'LENGTH'
    return p.subtype == 'DISTANCE' or any(w in p.identifier.lower() for w in _LENGTH_WORDS)


def is_angle(p):
    if p.type != 'FLOAT':
        return False
    unit = getattr(p, "unit", 'NONE')
    if unit != 'NONE':
        return unit == 'ROTATION'
    return p.subtype == 'ANGLE'


def defaults(entry):
    return {p.identifier: p.default for p in param_props(entry)}


def stored(obj):
    try:
        return json.loads(obj.vr_prim.op_params or "{}")
    except ValueError:
        return {}


def effective(entry, obj):
    """Settings to run the operator with: defaults, lengths scaled by the drag, then VR edits."""
    prim = obj.vr_prim
    params = {}
    for p in param_props(entry):
        v = p.default
        if is_length(p):
            v *= prim.op_scale
        params[p.identifier] = v
    params.update(stored(obj))
    return params


def has_lengths(entry):
    return any(is_length(p) for p in param_props(entry))


def _desktop(context):
    windows = context.window_manager.windows
    window = context.window or (windows[0] if len(windows) else None)
    if window is None:
        return {}
    from . import native
    view = native.largest_view3d(window)
    if view is None:
        return {"window": window}
    return {"window": window, "area": view[0], "region": view[1]}


def run(context, entry, params):
    """Run the entry's operator; returns the new objects (active one first)."""
    names = {p.identifier for p in rna(entry).properties}
    kw = dict(entry.preset)
    kw.update(params)
    if "align" in names:
        kw["align"] = 'WORLD'
    if "enter_editmode" in names:
        kw["enter_editmode"] = False
    before = set(bpy.data.objects)
    with context.temp_override(**_desktop(context)):
        result = operator(entry.idname)('EXEC_DEFAULT', **kw)
    if 'FINISHED' not in result:
        raise RuntimeError(f"{entry.idname} returned {result}")
    new = [o for o in bpy.data.objects if o not in before]
    active = context.view_layer.objects.active
    if active in new:
        new.remove(active)
        new.insert(0, active)
    if not new:
        raise RuntimeError(f"{entry.idname} created no object")
    return new


def mesh_bounds(mesh):
    n = len(mesh.vertices)
    if n == 0:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    co = np.empty(n * 3, dtype=np.float32)
    # Not attributes["position"]: some extension-made meshes don't expose it.
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(n, 3)
    return Vector(co.min(axis=0)), Vector(co.max(axis=0))


def local_bounds(context, obj):
    """Bounds of what you see (modifiers included, e.g. the rock generator's), in object space."""
    context.view_layer.update()
    corners = [Vector(c) for c in obj.bound_box]
    lo = Vector([min(c[i] for c in corners) for i in range(3)])
    hi = Vector([max(c[i] for c in corners) for i in range(3)])
    return lo, hi


def _extent(context, obj):
    lo, hi = local_bounds(context, obj)
    return max((hi - lo)[:])


# Generators slower than this (e.g. Wall Factory) aren't re-run on every drag frame; they're scaled.
SLOW_MS = 40.0


def create(context, entry, location):
    """Run the operator with defaults; the object it makes becomes the editable primitive."""
    objs = run(context, entry, {})
    obj = objs[0]
    for extra in objs[1:]:
        bpy.data.objects.remove(extra)
    prim = obj.vr_prim
    prim.kind = 'OPERATOR'
    prim.op_key = entry.key
    prim.op_params = "{}"
    prim.op_scale = 1.0
    prim.op_extent = _extent(context, obj)
    obj.location = location
    from . import primitives
    primitives.make_active(context, obj)
    return obj


def regenerate(context, obj):
    """Re-run the operator with the object's settings and swap the mesh in."""
    import time
    entry = catalog.find(obj.vr_prim.op_key)
    if entry is None:
        return False
    t0 = time.perf_counter()
    objs = run(context, entry, effective(entry, obj))
    if (time.perf_counter() - t0) * 1000 > SLOW_MS:
        obj["vrs_slow"] = True
    fresh = objs[0]
    old = obj.data
    if fresh.type == obj.type:
        obj.data = fresh.data
    for o in objs:
        bpy.data.objects.remove(o)
    if old is not None and old.users == 0 and isinstance(old, bpy.types.Mesh):
        bpy.data.meshes.remove(old)
    # The operator made the temp object active; give it back.
    from . import primitives
    primitives.make_active(context, obj)
    return True


def fit(context, obj, corner_a, corner_b, min_extent):
    """Uniformly size the primitive so its largest side matches the drag box's; center it there.

    Length settings are scaled and the operator re-run, so detail stays sensible at any size.
    Whatever that leaves off (settings we can't tell are lengths, modifiers, slow generators)
    is corrected with a uniform object scale, so the result always fills the box exactly.
    """
    import time
    a, b = Vector(corner_a), Vector(corner_b)
    target = max(max(abs(b[i] - a[i]) for i in range(3)), min_extent)
    prim = obj.vr_prim
    entry = catalog.find(prim.op_key)
    k = target / prim.op_extent if prim.op_extent > 1e-9 else 1.0
    slow = obj.get("vrs_slow", False)
    if entry is not None and has_lengths(entry) and not slow and abs(k - prim.op_scale) > 1e-6:
        prim.op_scale = k
        regenerate(context, obj)
    lo, hi = local_bounds(context, obj)
    extent = max((hi - lo)[:])
    s = target / extent if extent > 1e-9 else 1.0
    obj.scale = (s, s, s)
    # Put the bounding-box center (not the origin) at the box center.
    obj.location = (a + b) / 2 - Matrix.Diagonal(obj.scale) @ ((lo + hi) / 2)


# --- panel specs -----------------------------------------------------------

class Param:
    def __init__(self, prop, value, lo=0.0, hi=1.0, items=()):
        self.prop = prop
        self.id = prop.identifier
        self.label = prop.name or prop.identifier
        self.type = prop.type
        self.value = value
        self.lo, self.hi = lo, hi
        self.items = items  # (identifier, name) for enums
        self.length = is_length(prop)
        self.angle = is_angle(prop)


def params(entry, obj):
    """Param specs with VR-friendly ranges for the object's current settings."""
    prim = obj.vr_prim
    values = effective(entry, obj)
    length_defaults = [p.default for p in param_props(entry) if is_length(p)]
    ref = max([abs(v) for v in length_defaults] + [prim.op_extent / 2, 1e-3])
    out = []
    for p in param_props(entry):
        v = values.get(p.identifier, p.default)
        if p.type == 'ENUM':
            out.append(Param(p, v, items=[(i.identifier, i.name) for i in p.enum_items]))
        elif p.type == 'BOOLEAN':
            out.append(Param(p, bool(v)))
        elif p.type == 'INT':
            lo = max(p.soft_min, p.hard_min)
            hi = min(p.soft_max, p.hard_max, max(abs(p.default) * 4, lo + 16))
            out.append(Param(p, int(v), lo, max(hi, lo + 1)))
        elif is_length(p):
            scale = prim.op_scale
            base = abs(p.default) if p.default else ref
            lo = max(p.hard_min, 0.0) if p.soft_min >= 0 or p.hard_min >= 0 else -base * 4 * scale
            out.append(Param(p, float(v), lo, base * 4 * scale))
        elif is_angle(p):
            lo = max(p.soft_min, p.hard_min, -math.pi)
            hi = min(p.soft_max, p.hard_max, math.pi)
            out.append(Param(p, float(v), lo, max(hi, lo + 0.01)))
        else:
            span = max(abs(p.default) * 4, 1.0)
            lo = max(p.soft_min, p.hard_min, -span)
            hi = min(p.soft_max, p.hard_max, span)
            out.append(Param(p, float(v), lo, max(hi, lo + 0.01)))
    return out


# Objects whose regeneration waits for the slider to be released (slow generators).
_pending = set()


def set_param(context, obj, ident, value, live=True):
    """Change one setting and regenerate. If the operator fails with it, keep the old mesh and value.

    live=False only stores the value; flush_pending() regenerates later (on slider release).
    """
    before = obj.vr_prim.op_params
    data = stored(obj)
    data[ident] = value
    obj.vr_prim.op_params = json.dumps(data)
    if not live:
        _pending.add(obj.name)
        return True
    try:
        regenerate(context, obj)
        return True
    except Exception as ex:  # RuntimeError from the operator, TypeError for a value it won't take...
        obj.vr_prim.op_params = before
        print(f"VR Sculpt: {ident}={value!r} rejected: {ex}", flush=True)
        return False


def flush_pending(context):
    names = list(_pending)
    _pending.clear()
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        try:
            regenerate(context, obj)
        except Exception as ex:
            print(f"VR Sculpt: regenerating {name} failed: {ex}", flush=True)
