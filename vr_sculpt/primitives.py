"""Editable primitives: settings live on the object, the mesh is rebuilt when they change.

Sculpting a primitive turns it into a plain mesh (kind = 'NONE'), so a later
settings change can't wipe the sculpt.
"""

import bmesh
import bpy
from bpy.props import (EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty,
                       StringProperty)
from mathutils import Matrix, Vector

KIND_ITEMS = (
    ('NONE', "None", "Not an editable primitive"),
    ('CUBE', "Cube", "Box with per-axis size"),
    ('CYLINDER', "Cylinder", "Capped cylinder"),
    ('OPERATOR', "Operator", "Made by an Add > Mesh operator; settings re-run it (see generic.py)"),
)

DEFAULT_SIZE = 0.1  # meters, for a click without a drag
DEFAULT_ADD = "mesh.primitive_cube_add"


def _rebuild(self, context):
    rebuild(self.id_data)


class VRPrimitive(bpy.types.PropertyGroup):
    kind: EnumProperty(name="Primitive", items=KIND_ITEMS, default='NONE')
    size: FloatVectorProperty(
        name="Size", size=3, default=(DEFAULT_SIZE,) * 3, min=0.001, soft_max=10.0,
        subtype='XYZ_LENGTH', unit='LENGTH', update=_rebuild)
    subdivisions: IntProperty(
        name="Subdivisions", description="Cuts per cube edge, for sculpting", default=0, min=0, max=32,
        update=_rebuild)
    radius: FloatProperty(name="Radius", default=DEFAULT_SIZE / 2, min=0.0005, soft_max=5.0,
                          subtype='DISTANCE', unit='LENGTH', update=_rebuild)
    height: FloatProperty(name="Height", default=DEFAULT_SIZE, min=0.001, soft_max=10.0,
                          subtype='DISTANCE', unit='LENGTH', update=_rebuild)
    segments: IntProperty(name="Segments", default=24, min=3, max=128, update=_rebuild)
    # 'OPERATOR' primitives (generic.py).
    op_key: StringProperty(name="Generator", description="Add > Mesh entry that made this object")
    op_params: StringProperty(name="Settings", description="JSON of the operator settings changed in VR")
    op_scale: FloatProperty(name="Drag Scale", default=1.0, description="Scale applied to length settings")
    op_extent: FloatProperty(name="Default Extent", default=1.0,
                             description="Largest side of the mesh made with default settings")


def build_bmesh(prim):
    bm = bmesh.new()
    if prim.kind == 'CUBE':
        bmesh.ops.create_cube(bm, size=1.0)
        if prim.subdivisions:
            bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=prim.subdivisions, use_grid_fill=True)
        bmesh.ops.scale(bm, vec=Vector(prim.size), verts=bm.verts)
    elif prim.kind == 'CYLINDER':
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=prim.segments,
                              radius1=prim.radius, radius2=prim.radius, depth=prim.height)
    return bm


def rebuild(obj):
    prim = obj.vr_prim
    if prim.kind not in {'CUBE', 'CYLINDER'} or obj.type != 'MESH':
        return
    bm = build_bmesh(prim)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def is_editable(obj):
    return obj is not None and obj.type == 'MESH' and obj.vr_prim.kind != 'NONE'


def forget(obj):
    """Turn a primitive into a plain mesh (e.g. once it's sculpted)."""
    if obj is not None and obj.type == 'MESH' and obj.vr_prim.kind != 'NONE':
        obj.vr_prim.kind = 'NONE'


def label(key):
    """Display name for an Add entry key."""
    from . import catalog
    entry = catalog.find(key)
    return entry.label if entry else key.split(".")[-1]


def create_from_entry(context, key, location):
    """New editable primitive for a catalog entry key (our own Cube/Cylinder, or any Add > Mesh operator)."""
    from . import catalog, generic
    entry = catalog.find(key)
    kind = catalog.BUILTIN_KIND.get(entry.idname if entry else key)
    if kind is not None:
        return create(context, kind, location)
    if entry is None:
        raise RuntimeError(f"'{key}' is not in Add > Mesh (is its extension enabled?)")
    return generic.create(context, entry, location)


def create(context, kind, location):
    """New primitive object at location, linked to the active collection, made active."""
    name = "Cube" if kind == 'CUBE' else "Cylinder"
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(obj)
    obj.location = location
    obj.vr_prim.kind = kind
    rebuild(obj)
    make_active(context, obj)
    return obj


def fit_box(prim, corner_a, corner_b, min_size=0.005):
    """Size the primitive to the world-axis box between two points. Returns the box center."""
    a, b = Vector(corner_a), Vector(corner_b)
    ext = Vector([max(abs(b[i] - a[i]), min_size) for i in range(3)])
    if prim.kind == 'CUBE':
        prim.size = ext
    elif prim.kind == 'CYLINDER':
        prim.radius = max(ext.x, ext.y) / 2.0
        prim.height = ext.z
    return (a + b) / 2.0


def make_active(context, obj):
    # Right after a delete, the view layer's object list can hold removed (None) entries until it resyncs.
    for o in context.view_layer.objects:
        if o is not None and o.select_get():
            o.select_set(False)
    if obj is not None:
        obj.select_set(True)
    context.view_layer.objects.active = obj


def duplicate(context, obj, offset=(0.05, 0.0, 0.0)):
    new = obj.copy()
    if obj.data is not None:
        new.data = obj.data.copy()
    for coll in obj.users_collection:
        coll.objects.link(new)
    new.matrix_world = Matrix.Translation(Vector(offset)) @ obj.matrix_world
    make_active(context, new)
    return new


def delete(context, obj):
    mesh = obj.data if obj.type == 'MESH' else None
    bpy.data.objects.remove(obj)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def touching(context, point, margin):
    """The visible mesh object whose bounds contain point (+margin), smallest first, or None."""
    best, best_vol = None, None
    for obj in context.view_layer.objects:
        if obj is None or obj.type != 'MESH' or not obj.visible_get():
            continue
        inv = obj.matrix_world.inverted_safe()
        p = inv @ Vector(point)
        scale = max(obj.matrix_world.to_scale())
        m = margin / max(scale, 1e-9)
        corners = [Vector(c) for c in obj.bound_box]
        lo = Vector([min(c[i] for c in corners) - m for i in range(3)])
        hi = Vector([max(c[i] for c in corners) + m for i in range(3)])
        if all(lo[i] <= p[i] <= hi[i] for i in range(3)):
            dims = obj.dimensions
            vol = dims.x * dims.y * dims.z
            if best is None or vol < best_vol:
                best, best_vol = obj, vol
    return best


def register():
    bpy.utils.register_class(VRPrimitive)
    bpy.types.Object.vr_prim = PointerProperty(type=VRPrimitive)


def unregister():
    del bpy.types.Object.vr_prim
    bpy.utils.unregister_class(VRPrimitive)
