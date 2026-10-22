"""Hard surface tools for Model mode: live booleans, a bevel finish, solidify and arrays.

All of it is modifiers, so primitives stay editable and nothing is final until you apply it.

Booleans: with New as Cut / Union / Intersect, a primitive dragged out becomes a cutter on the
selected object (the target): a wireframe object in the "VR Cutters" collection, parented to the
target (it moves with it), with a Boolean modifier on the target pointing at it. Grab the cutter
or change its settings and the cut follows live. The Manifold solver is used when both meshes are
closed (it's ~2.5x faster than Exact and fast enough to follow a drag), Exact otherwise (Manifold
silently does nothing with an open mesh).

Our modifiers are kept in this order, whatever order they were added in: Solidify, Booleans,
Array, Mirror, Shading, Bevel. So cuts happen once on the base shape, arrays and the mirror repeat
them, and the bevel rounds the cut edges too. The shading (shading.py) has to come before the bevel:
Harden Normals bakes the shading as it finds it, so with flat faces at that point the curved walls
of a cut stay faceted.
"""

import math

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import primitives, shading, symmetry

BOOLEAN = "VR Boolean"  # one per cutter: "VR Boolean", "VR Boolean.001", ...
BEVEL = "VR Bevel"
SOLIDIFY = "VR Solidify"
ARRAY = "VR Array"
CUTTERS = "VR Cutters"  # collection for cutters and array helpers

OPERATIONS = (('DIFFERENCE', "Cut"), ('UNION', "Union"), ('INTERSECT', "Intersect"))
STACK = (SOLIDIFY, BOOLEAN, ARRAY, symmetry.MODIFIER, shading.MODIFIER, BEVEL)

BEVEL_ANGLE = math.radians(30.0)
BEVEL_SEGMENTS = 3


# --- modifier stack ----------------------------------------------------------

def _rank(mod):
    for i, prefix in enumerate(STACK):
        if mod.name == prefix or mod.name.startswith(prefix + "."):
            return i
    return None


def sort_stack(obj):
    """Put our modifiers in STACK order. Others stay where they are: ours are sorted within the
    slots they already take."""
    mods = obj.modifiers
    names = [m.name for m in mods]
    slots = [i for i, m in enumerate(mods) if _rank(m) is not None]
    ours = sorted((mods[i] for i in slots), key=_rank)  # stable: booleans keep their order
    for slot, mod in zip(slots, ours):
        names[slot] = mod.name
    for i, name in enumerate(names):
        j = mods.find(name)
        if j != i:
            mods.move(j, i)


def _world_scale(obj):
    s = obj.matrix_world.to_scale()
    return max((abs(s.x) + abs(s.y) + abs(s.z)) / 3.0, 1e-9)


# --- booleans ----------------------------------------------------------------

def booleans(target):
    """Our Boolean modifiers on target."""
    if target is None or target.type != 'MESH':
        return []
    return [m for m in target.modifiers if m.type == 'BOOLEAN' and _rank(m) == 1]


def cutters(target):
    return [m.object for m in booleans(target) if m.object is not None]


def is_cutter(obj):
    return obj is not None and bool(obj.get("vrs_cutter", False))


def target_of(cutter):
    """The object cutter cuts (its parent, normally), or None."""
    if not is_cutter(cutter):
        return None
    candidates = [cutter.parent] if cutter.parent is not None else []
    candidates += [o for o in bpy.data.objects if o is not cutter.parent]
    for obj in candidates:
        if any(m.object == cutter for m in booleans(obj)):
            return obj
    return None


def modifier_of(cutter):
    target = target_of(cutter)
    return next((m for m in booleans(target) if m.object == cutter), None)


def cut_target(context):
    """What a new cutter would cut: the selected mesh, or the target of the selected cutter."""
    obj = context.view_layer.objects.active
    if obj is None or not obj.select_get():
        return None
    if is_cutter(obj):
        return target_of(obj)
    return obj if obj.type == 'MESH' else None


def is_manifold(mesh):
    """Closed: every edge has exactly two faces."""
    if len(mesh.edges) == 0 or len(mesh.polygons) == 0:
        return False
    edges = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("edge_index", edges)
    return bool((np.bincount(edges, minlength=len(mesh.edges)) == 2).all())


def best_solver(target, cutter):
    solid = target.modifiers.get(SOLIDIFY) is not None or is_manifold(target.data)
    return 'MANIFOLD' if solid and is_manifold(cutter.data) else 'EXACT'


def _cutters_collection(context):
    coll = bpy.data.collections.get(CUTTERS)
    if coll is None:
        coll = bpy.data.collections.new(CUTTERS)
        coll.hide_render = True
    if coll.name not in context.scene.collection.children:
        context.scene.collection.children.link(coll)
    return coll


def _move_to_cutters(context, obj):
    coll = _cutters_collection(context)
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    for other in list(obj.users_collection):
        if other != coll:
            other.objects.unlink(obj)


def make_cutter(context, cutter, target, operation='DIFFERENCE', parent=True):
    """Turn cutter into a live boolean cutter on target. parent=False leaves it unparented (while a
    drag still places it in world space); call attach() after."""
    _move_to_cutters(context, cutter)
    cutter["vrs_cutter"] = True
    cutter.display_type = 'WIRE'
    cutter.hide_render = True
    mod = target.modifiers.new(BOOLEAN, 'BOOLEAN')
    mod.object = cutter
    mod.operation = operation
    mod.solver = best_solver(target, cutter)
    mod.show_expanded = False
    sort_stack(target)
    if parent:
        attach(cutter, target)
    return mod


def attach(child, parent):
    """Parent child to parent, keeping where it is."""
    if child.parent == parent:
        return
    # Not matrix_world: right after a drag set the location it's stale until the next depsgraph update.
    mw = _world(child)
    child.parent = parent
    child.matrix_parent_inverse = Matrix.Identity(4)
    child.matrix_basis = _world(parent).inverted_safe() @ mw


def _world(obj):
    """obj's world matrix from its own transform (fresh even before the depsgraph updates)."""
    if obj.parent is None:
        return obj.matrix_basis.copy()
    return _world(obj.parent) @ obj.matrix_parent_inverse @ obj.matrix_basis


def set_operation(cutter, operation):
    mod = modifier_of(cutter)
    if mod is not None:
        mod.operation = operation


def refresh_solver(cutter):
    """Pick the solver again (e.g. after the cutter's shape changed)."""
    mod = modifier_of(cutter)
    if mod is not None:
        mod.solver = best_solver(target_of(cutter), cutter)


def _apply(context, obj, names):
    """Apply the named modifiers of obj, top to bottom. The mesh becomes plain (not an editable
    primitive any more)."""
    from . import menus
    window, view = menus._desktop_view(context)
    override = {"object": obj, "active_object": obj, "selected_objects": [obj], "selected_editable_objects": [obj]}
    if view is not None:
        override.update(window=window, area=view[0], region=view[1])
    if obj.mode != 'OBJECT':
        menus.set_object_mode(context, obj, 'OBJECT')
    with context.temp_override(**override):
        for name in names:
            if obj.modifiers.get(name) is not None:
                bpy.ops.object.modifier_apply(modifier=name)
    primitives.forget(obj)


def apply_cutter(context, cutter):
    """Make the cut permanent and remove the cutter. Modifiers above it (Solidify) are applied too,
    so the result is what you saw."""
    target = target_of(cutter)
    mod = modifier_of(cutter)
    if target is None or mod is None:
        return False
    index = target.modifiers.find(mod.name)
    names = [m.name for m in target.modifiers[:index] if _rank(m) == 0] + [mod.name]
    _apply(context, target, names)
    _delete_object(cutter)
    primitives.make_active(context, target)
    return True


def apply_all(context, target):
    """Apply every cut on target (and the Solidify above them); remove the cutters."""
    mods = booleans(target)
    if not mods:
        return False
    last = max(target.modifiers.find(m.name) for m in mods)
    names = [m.name for m in target.modifiers[:last + 1] if _rank(m) in {0, 1}]
    gone = [m.object for m in mods if m.object is not None]
    _apply(context, target, names)
    for cutter in gone:
        _delete_object(cutter)
    return True


def _delete_object(obj):
    data = obj.data
    bpy.data.objects.remove(obj)
    if isinstance(data, bpy.types.Mesh) and data.users == 0:
        bpy.data.meshes.remove(data)


def _helpers(obj):
    """Children that only exist for obj: its cutters and its array center."""
    return [c for c in obj.children if is_cutter(c) or c.get("vrs_helper", False)]


def delete(context, obj):
    """Delete obj. A cutter takes its Boolean modifier with it; a target takes its cutters and
    helpers."""
    if is_cutter(obj):
        target = target_of(obj)
        for mod in booleans(target):
            if mod.object == obj:
                target.modifiers.remove(mod)
    for child in _helpers(obj):
        delete(context, child)
    primitives.delete(context, obj)


def duplicate(context, obj):
    """Duplicate obj like primitives.duplicate. A cutter's copy cuts the same target; a target's copy
    gets its own copies of the cutters and helpers."""
    target = target_of(obj) if is_cutter(obj) else None
    mod = modifier_of(obj) if target is not None else None
    new = primitives.duplicate(context, obj)
    if target is not None and mod is not None:
        make_cutter(context, new, target, mod.operation)
        primitives.make_active(context, new)
        return new
    for child in _helpers(obj):
        copy = child.copy()
        if child.data is not None:
            copy.data = child.data.copy()
        for coll in child.users_collection:
            coll.objects.link(copy)
        copy.parent = new
        copy.matrix_parent_inverse = child.matrix_parent_inverse.copy()
        copy.matrix_basis = child.matrix_basis.copy()
        for m in new.modifiers:
            if getattr(m, "object", None) == child:
                m.object = copy
            if getattr(m, "offset_object", None) == child:
                m.offset_object = copy
    return new


def cutters_visible(context):
    lc = context.view_layer.layer_collection.children.get(CUTTERS)
    return lc is None or not lc.hide_viewport


def set_cutters_visible(context, visible):
    """Show or hide all cutters (they keep cutting while hidden)."""
    _cutters_collection(context)
    lc = context.view_layer.layer_collection.children.get(CUTTERS)
    if lc is not None:
        lc.hide_viewport = not visible


# --- finish: bevel ------------------------------------------------------------------

def bevel(obj):
    mod = obj.modifiers.get(BEVEL) if obj is not None and obj.type == 'MESH' else None
    return mod if mod is not None and mod.type == 'BEVEL' else None


def default_bevel_width(obj):
    """3% of the object's smallest side (world units)."""
    dims = [d for d in obj.dimensions if d > 1e-6]
    return max(0.03 * min(dims), 0.0005) if dims else 0.002


def bevel_width(obj):
    mod = bevel(obj)
    return mod.width * _world_scale(obj) if mod is not None else 0.0


def set_bevel_width(obj, width):
    mod = bevel(obj)
    if mod is not None:
        mod.width = width / _world_scale(obj)


def set_bevel(obj, on):
    """Bevel the edges sharper than 30 degrees. Harden Normals keeps the faces next to them flat;
    for that the shading has to be smooth where it should be, so Auto shading comes with it when the
    object has no VR shading yet (shading.py)."""
    if not on:
        mod = bevel(obj)
        if mod is not None:
            obj.modifiers.remove(mod)
        return
    if bevel(obj) is None:
        width = default_bevel_width(obj)
        mod = obj.modifiers.new(BEVEL, 'BEVEL')
        mod.limit_method = 'ANGLE'
        mod.angle_limit = BEVEL_ANGLE
        mod.segments = BEVEL_SEGMENTS
        mod.harden_normals = True
        mod.use_clamp_overlap = True
        mod.miter_outer = 'MITER_ARC'
        mod.show_expanded = False
        set_bevel_width(obj, width)
        sort_stack(obj)
    if shading.mode(obj) is None:
        shading.set_mode(obj, 'AUTO')


def has_bevel(obj):
    return bevel(obj) is not None


# --- solidify ------------------------------------------------------------------

def solidify(obj):
    mod = obj.modifiers.get(SOLIDIFY) if obj is not None and obj.type == 'MESH' else None
    return mod if mod is not None and mod.type == 'SOLIDIFY' else None


def set_solidify(obj, on):
    mod = solidify(obj)
    if not on:
        if mod is not None:
            obj.modifiers.remove(mod)
        return
    if mod is None:
        mod = obj.modifiers.new(SOLIDIFY, 'SOLIDIFY')
        mod.use_even_offset = True
        mod.offset = -1.0
        mod.show_expanded = False
        set_solidify_thickness(obj, max(0.05 * max(obj.dimensions), 0.001))
        sort_stack(obj)


def solidify_thickness(obj):
    mod = solidify(obj)
    return mod.thickness * _world_scale(obj) if mod is not None else 0.0


def set_solidify_thickness(obj, thickness):
    mod = solidify(obj)
    if mod is not None:
        mod.thickness = thickness / _world_scale(obj)


# --- arrays --------------------------------------------------------------------
#
# Linear: copies side by side along a local axis, a Gap apart (relative to the object's size).
# Radial: copies around a center point (set with Here, at the left hand), turning about a local
# axis. That's the Array modifier's object offset: an empty parented to the object, whose transform
# relative to it is one step of the turn about the center.

ARRAY_GAP = 0.25


def array(obj):
    mod = obj.modifiers.get(ARRAY) if obj is not None and obj.type == 'MESH' else None
    return mod if mod is not None and mod.type == 'ARRAY' else None


def array_count(obj):
    mod = array(obj)
    return mod.count if mod is not None else 1


def array_mode(obj):
    return obj.get("vrs_array_mode", 'LINEAR')


def array_axis(obj):
    return int(obj.get("vrs_array_axis", 0))


def array_gap(obj):
    return float(obj.get("vrs_array_gap", ARRAY_GAP))


def _array_center(obj):
    """The radial center in object space."""
    c = obj.get("vrs_array_center")
    return Vector(c) if c is not None else None


def _default_center(context, obj):
    """The 3D cursor, or (when it's inside the object) a point beside it."""
    inv = obj.matrix_world.inverted_safe()
    cursor = inv @ context.scene.cursor.location
    corners = [Vector(c) for c in obj.bound_box]
    lo = Vector([min(c[i] for c in corners) for i in range(3)])
    hi = Vector([max(c[i] for c in corners) for i in range(3)])
    if all(lo[i] - 1e-6 <= cursor[i] <= hi[i] + 1e-6 for i in range(3)):
        side = (hi - lo)
        axis = array_axis(obj)
        out = (axis + 1) % 3  # beside it, across the turning axis
        cursor = (lo + hi) / 2
        cursor[out] = lo[out] - 1.5 * max(side[out], 1e-3)
    return cursor


def _offset_empty(context, obj):
    mod = array(obj)
    empty = mod.offset_object if mod is not None else None
    if empty is None:
        empty = bpy.data.objects.new(obj.name + " Array Center", None)
        empty.empty_display_type = 'PLAIN_AXES'
        empty.empty_display_size = 0.005
        empty["vrs_helper"] = True
        _move_to_cutters(context, empty)
        empty.parent = obj
        empty.matrix_parent_inverse = Matrix.Identity(4)
        empty.hide_select = True
    return empty


def _update_array(context, obj):
    mod = array(obj)
    if mod is None:
        return
    axis = array_axis(obj)
    if array_mode(obj) == 'RADIAL':
        center = _array_center(obj)
        if center is None:
            center = _default_center(context, obj)
            obj["vrs_array_center"] = center[:]
        empty = _offset_empty(context, obj)
        step = Matrix.Rotation(2.0 * math.pi / max(mod.count, 1), 4, "XYZ"[axis])
        empty.matrix_basis = Matrix.Translation(center) @ step @ Matrix.Translation(-center)
        mod.use_relative_offset = False
        mod.use_object_offset = True
        mod.offset_object = empty
    else:
        empty = mod.offset_object
        mod.use_object_offset = False
        mod.offset_object = None
        if empty is not None:
            _delete_object(empty)
        mod.use_relative_offset = True
        d = Vector((0.0, 0.0, 0.0))
        d[axis] = 1.0 + array_gap(obj)
        mod.relative_offset_displace = d


def set_array_count(context, obj, count):
    """count copies (1 takes the array off)."""
    count = int(count)
    mod = array(obj)
    if count <= 1:
        if mod is not None:
            empty = mod.offset_object
            obj.modifiers.remove(mod)
            if empty is not None:
                _delete_object(empty)
        return
    if mod is None:
        mod = obj.modifiers.new(ARRAY, 'ARRAY')
        mod.show_expanded = False
        sort_stack(obj)
    mod.count = count
    _update_array(context, obj)


def set_array_mode(context, obj, mode):
    obj["vrs_array_mode"] = mode
    _update_array(context, obj)


def set_array_axis(context, obj, axis):
    obj["vrs_array_axis"] = int(axis)
    _update_array(context, obj)


def set_array_gap(context, obj, gap):
    obj["vrs_array_gap"] = float(gap)
    _update_array(context, obj)


def set_array_center(context, obj, point):
    """Radial center at a world point (the left hand's tip)."""
    obj["vrs_array_center"] = (obj.matrix_world.inverted_safe() @ Vector(point))[:]
    _update_array(context, obj)
