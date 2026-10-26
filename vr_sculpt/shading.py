"""Shading for Model mode: Flat / Smooth / Auto (by angle), plus hard and soft edges marked by hand.

A "VR Shading" Geometry Nodes modifier does it, so it survives everything that rebuilds or cuts the
mesh (a primitive's settings, booleans, mirror, arrays):
- Flat: every face flat.
- Smooth: every face smooth, except edges marked hard.
- Auto: smooth, except edges sharper than the Angle, and edges marked hard. Edges marked soft
  stay smooth whatever their angle.

Blender's own Smooth by Angle can't do both: it keeps faces that are already flat-shaded flat (cutters
bring flat faces into a cut), and its Ignore Sharpness, which fixes that, also drops edges marked
sharp.

Marks are edge attributes on the mesh: Blender's own "sharp_edge" (Mark Sharp) for hard, and
"vrs_soft" for soft. The Mark tool paints them with the tip: the main hand marks, the off hand clears.
They're kept when a primitive is rebuilt with the same topology (resizing it); changing its
segments or subdivisions drops them.
"""

import math

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

MODIFIER = "VR Shading"
GROUP = "VR Shading"
SHARP = "sharp_edge"
SOFT = "vrs_soft"
DEFAULT_ANGLE = math.radians(30.0)

MODES = (('FLAT', "Flat"), ('SMOOTH', "Smooth"), ('AUTO', "Auto"))
MARK_REACH = 0.012  # hand-space meters from the tip to an edge it marks
HARD_COLOR = (0.3, 0.85, 1.0)
SOFT_COLOR = (1.0, 0.75, 0.3)


# --- the node group ---------------------------------------------------------------

def _build_group():
    g = bpy.data.node_groups.new(GROUP, 'GeometryNodeTree')
    g.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    angle = g.interface.new_socket("Angle", in_out='INPUT', socket_type='NodeSocketFloat')
    try:
        angle.subtype = 'ANGLE'
    except TypeError:
        pass
    angle.default_value = DEFAULT_ANGLE
    angle.min_value, angle.max_value = 0.0, math.pi
    g.interface.new_socket("Flat", in_out='INPUT', socket_type='NodeSocketBool')
    g.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    nodes, links = g.nodes, g.links

    inp = nodes.new('NodeGroupInput')
    out = nodes.new('NodeGroupOutput')
    faces = nodes.new('GeometryNodeSetShadeSmooth')
    faces.domain = 'FACE'
    edges = nodes.new('GeometryNodeSetShadeSmooth')
    edges.domain = 'EDGE'
    not_flat = nodes.new('FunctionNodeBooleanMath')
    not_flat.operation = 'NOT'
    edge_angle = nodes.new('GeometryNodeInputMeshEdgeAngle')
    steep = nodes.new('FunctionNodeCompare')
    steep.data_type, steep.operation = 'FLOAT', 'GREATER_THAN'
    hard = nodes.new('GeometryNodeInputNamedAttribute')
    hard.data_type = 'BOOLEAN'
    hard.inputs["Name"].default_value = SHARP
    soft = nodes.new('GeometryNodeInputNamedAttribute')
    soft.data_type = 'BOOLEAN'
    soft.inputs["Name"].default_value = SOFT
    sharp_any = nodes.new('FunctionNodeBooleanMath')
    sharp_any.operation = 'OR'
    sharp = nodes.new('FunctionNodeBooleanMath')
    sharp.operation = 'NIMPLY'  # (steep or hard) and not soft
    smooth = nodes.new('FunctionNodeBooleanMath')
    smooth.operation = 'NOT'

    # Faces: all smooth (or all flat). The named attributes are read from the input geometry, so
    # the hard marks are seen before the edge node rewrites sharp_edge.
    links.new(inp.outputs["Geometry"], faces.inputs[0])
    links.new(inp.outputs["Flat"], not_flat.inputs[0])
    links.new(not_flat.outputs[0], faces.inputs["Shade Smooth"])
    links.new(edge_angle.outputs["Unsigned Angle"], steep.inputs[0])
    links.new(inp.outputs["Angle"], steep.inputs[1])
    links.new(steep.outputs[0], sharp_any.inputs[0])
    links.new(hard.outputs["Attribute"], sharp_any.inputs[1])
    links.new(sharp_any.outputs[0], sharp.inputs[0])
    links.new(soft.outputs["Attribute"], sharp.inputs[1])
    links.new(sharp.outputs[0], smooth.inputs[0])
    links.new(faces.outputs[0], edges.inputs[0])
    links.new(smooth.outputs[0], edges.inputs["Shade Smooth"])
    links.new(edges.outputs[0], out.inputs[0])
    for i, n in enumerate((inp, edge_angle, hard, soft, steep, sharp_any, sharp, smooth, not_flat, faces, edges, out)):
        n.location = (i * 180.0, 0.0)
    return g


def group():
    g = bpy.data.node_groups.get(GROUP)
    if g is None or g.bl_idname != 'GeometryNodeTree':
        g = _build_group()
    return g


def _input_id(name):
    for item in group().interface.items_tree:
        if getattr(item, "in_out", None) == 'INPUT' and item.name == name:
            return item.identifier
    raise KeyError(name)


def _get(mod, name):
    ident = _input_id(name)
    props = getattr(mod, "properties", None)
    if props is not None:  # Blender 5.x
        return getattr(props.inputs, ident).value
    return mod[ident]


def _set(mod, name, value):
    ident = _input_id(name)
    props = getattr(mod, "properties", None)
    if props is not None:
        getattr(props.inputs, ident).value = value
    else:
        mod[ident] = value


# --- shading modes --------------------------------------------------------------------

def modifier(obj):
    mod = obj.modifiers.get(MODIFIER) if obj is not None and obj.type == 'MESH' else None
    return mod if mod is not None and mod.type == 'NODES' else None


def mode(obj):
    """'FLAT', 'SMOOTH', 'AUTO', or None (no VR shading: the mesh's own)."""
    mod = modifier(obj)
    if mod is None:
        return None
    if _get(mod, "Flat"):
        return 'FLAT'
    return 'SMOOTH' if _get(mod, "Angle") >= math.pi - 1e-4 else 'AUTO'


def angle(obj):
    mod = modifier(obj)
    return _get(mod, "Angle") if mod is not None else DEFAULT_ANGLE


def set_mode(obj, new_mode):
    """Flat, Smooth or Auto; None takes the VR shading off."""
    mod = modifier(obj)
    if new_mode is None:
        if mod is not None:
            obj.modifiers.remove(mod)
        return
    if mod is None:
        mod = obj.modifiers.new(MODIFIER, 'NODES')
        mod.node_group = group()
        mod.show_expanded = False
        obj["vrs_auto_angle"] = DEFAULT_ANGLE
        from . import hardsurface
        hardsurface.sort_stack(obj)
    if new_mode == 'AUTO':
        _set(mod, "Angle", float(obj.get("vrs_auto_angle", DEFAULT_ANGLE)))
    elif new_mode == 'SMOOTH':
        _set(mod, "Angle", math.pi)
    _set(mod, "Flat", new_mode == 'FLAT')
    obj.update_tag()


def set_angle(obj, value):
    """The Auto angle (radians)."""
    mod = modifier(obj)
    obj["vrs_auto_angle"] = float(value)
    if mod is not None and mode(obj) == 'AUTO':
        _set(mod, "Angle", float(value))
        obj.update_tag()


# --- edge marks -----------------------------------------------------------------------

def _bool_attr(mesh, name, create=False):
    attr = mesh.attributes.get(name)
    if attr is not None and (attr.domain != 'EDGE' or attr.data_type != 'BOOLEAN'):
        return None
    if attr is None and create:
        attr = mesh.attributes.new(name, 'BOOLEAN', 'EDGE')
    return attr


def _read(mesh, name):
    attr = _bool_attr(mesh, name)
    out = np.zeros(len(mesh.edges), dtype=bool)
    if attr is not None and len(out):
        attr.data.foreach_get("value", out)
    return out


def _write(mesh, name, values):
    attr = _bool_attr(mesh, name, create=bool(values.any()))
    if attr is not None:
        attr.data.foreach_set("value", values)


def marks(mesh):
    """(hard, soft) bool arrays per edge."""
    return _read(mesh, SHARP), _read(mesh, SOFT)


def save_marks(mesh):
    """What restore_marks() needs to put the marks back on a rebuilt mesh, or None if there are none."""
    hard, soft = marks(mesh)
    if not hard.any() and not soft.any():
        return None
    return len(mesh.edges), hard, soft


def restore_marks(mesh, saved):
    """Put saved marks back if the mesh still has the same edges (a primitive resized, not re-cut)."""
    if saved is None or saved[0] != len(mesh.edges):
        return False
    _write(mesh, SHARP, saved[1])
    _write(mesh, SOFT, saved[2])
    return True


def _edge_segments(obj):
    """(n, 2, 3) world-space edge end points of obj's own mesh."""
    me = obj.data
    n = len(me.edges)
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    idx = np.empty(n * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", idx)
    m = np.array(obj.matrix_world)
    world = co @ m[:3, :3].T + m[:3, 3]
    return world[idx.reshape(-1, 2)]


def _near(segments, point, reach):
    """Bool per segment: within reach of point."""
    if not len(segments):
        return np.zeros(0, dtype=bool)
    a, b = segments[:, 0], segments[:, 1]
    ab = b - a
    t = np.clip(((point - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-18), 0.0, 1.0)
    d = np.linalg.norm(a + ab * t[:, None] - point, axis=1)
    return d <= reach


def mark_targets(context):
    """The meshes the Mark tool works on: the selected object (or a selected cutter's target) and its
    visible cutters (their marks carry into the cut)."""
    from . import hardsurface
    obj = context.view_layer.objects.active
    if obj is None or not obj.select_get():
        return []
    if hardsurface.is_cutter(obj):
        obj = hardsurface.target_of(obj) or obj
    if obj.type != 'MESH':
        return []
    out = [obj] + [c for c in hardsurface.cutters(obj) if c.visible_get()]
    return [o for o in out if o.type == 'MESH' and len(o.data.edges)]


class EdgeMark:
    """Trigger handler for the Mark tool: edges the tip passes near get marked hard or soft (or
    cleared)."""

    def __init__(self, context, how, scale):
        self.how = how  # 'HARD', 'SOFT' or 'CLEAR'
        self.reach = MARK_REACH * scale
        self.objects = mark_targets(context)
        self.segments = {o.name: _edge_segments(o) for o in self.objects}
        self.changed = False

    def update(self, context, pose, tip_pose, value):
        point = np.array(tip_pose.translation[:])
        for obj in self.objects:
            near = _near(self.segments[obj.name], point, self.reach)
            if not near.any():
                continue
            hard, soft = marks(obj.data)
            new_hard, new_soft = hard.copy(), soft.copy()
            new_hard[near] = self.how == 'HARD'
            new_soft[near] = self.how == 'SOFT'
            if (new_hard != hard).any() or (new_soft != soft).any():
                _write(obj.data, SHARP, new_hard)
                _write(obj.data, SOFT, new_soft)
                obj.data.update()
                self.changed = True

    def finish(self, context):
        pass


def draw(shader, context, tips, scale, show_reach):
    """The marked edges of what the Mark tool works on (hard blue, soft orange), and with show_reach
    the edges in reach of each tip."""
    objs = mark_targets(context)
    hard_pts, soft_pts, reach_pts = [], [], []
    for obj in objs:
        segs = _edge_segments(obj)
        hard, soft = marks(obj.data)
        hard_pts.append(segs[hard].reshape(-1, 3))
        soft_pts.append(segs[soft].reshape(-1, 3))
        if show_reach:
            for tip in tips:
                reach_pts.append(segs[_near(segs, np.array(tip[:]), MARK_REACH * scale)].reshape(-1, 3))
    gpu.state.line_width_set(3.0)
    for pts, color in ((hard_pts, (*HARD_COLOR, 0.95)), (soft_pts, (*SOFT_COLOR, 0.95)),
                       (reach_pts, (1.0, 1.0, 1.0, 0.8))):
        coords = np.concatenate(pts) if pts else np.zeros((0, 3))
        if not len(coords):
            continue
        batch = batch_for_shader(shader, 'LINES', {"pos": coords.astype(np.float32)})
        shader.uniform_float("color", color)
        batch.draw(shader)
    gpu.state.line_width_set(1.0)
