"""Small solid shapes for the spatial UI: dock tokens and gizmo handles, drawn lit with one shader.

There are no image assets, so every tool is a little mesh made of a few parts (a sphere, a box, a
cone, ...). Meshes are built once, in a unit-ish local space, and drawn under a gpu.matrix transform
with a light that comes from above and in front of the viewer, so they read as objects you can take.
"""

import math

import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

_shader = {"lit": None, "failed": False}
_batches = {}


def _lit_shader():
    """A tiny lit shader: base color, a wrap-around diffuse light, and a glow toward white (hover)."""
    if _shader["lit"] is not None or _shader["failed"]:
        return _shader["lit"]
    try:
        iface = gpu.types.GPUStageInterfaceInfo("vrs_lit_iface")
        iface.smooth('VEC3', "v_nor")
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', "mvp")
        info.push_constant('VEC4', "color")
        info.push_constant('VEC3', "light")
        info.push_constant('FLOAT', "glow")
        info.vertex_in(0, 'VEC3', "pos")
        info.vertex_in(1, 'VEC3', "nor")
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', "frag")
        info.vertex_source("void main() { v_nor = nor; gl_Position = mvp * vec4(pos, 1.0); }")
        info.fragment_source(
            "void main() {"
            "  vec3 n = normalize(v_nor);"
            "  float d = dot(n, normalize(light)) * 0.5 + 0.5;"
            "  float rim = pow(1.0 - abs(n.z), 3.0) * 0.15;"
            "  vec3 c = color.rgb * (0.32 + 0.78 * d * d) + rim;"
            "  frag = vec4(mix(c, vec3(1.0), glow), color.a);"
            "}")
        _shader["lit"] = gpu.shader.create_from_info(info)
    except Exception as ex:  # No GPU (background) or an API change: fall back to flat colors.
        print("VR Sculpt: lit shader unavailable, drawing flat:", ex)
        _shader["failed"] = True
    return _shader["lit"]


# --- mesh builders: lists of (position, normal) triangles ----------------------------------

def _tri_list(quads_or_tris):
    out = []
    for face in quads_or_tris:
        if len(face) == 4:
            a, b, c, d = face
            out += [a, b, c, a, c, d]
        else:
            out += list(face)
    return out


def sphere(rings=8, segments=14):
    pts = []
    for i in range(rings):
        t0, t1 = math.pi * i / rings, math.pi * (i + 1) / rings
        for j in range(segments):
            p0, p1 = 2 * math.pi * j / segments, 2 * math.pi * (j + 1) / segments

            def v(t, p):
                return Vector((math.sin(t) * math.cos(p), math.sin(t) * math.sin(p), math.cos(t)))
            pts.append((v(t0, p0), v(t1, p0), v(t1, p1), v(t0, p1)))
    tris = _tri_list(pts)
    return [p.copy() for p in tris], [p.normalized() for p in tris]


def box(sx=1.0, sy=1.0, sz=1.0):
    pos, nor = [], []
    for axis in range(3):
        for sign in (-1.0, 1.0):
            n = Vector((0.0, 0.0, 0.0))
            n[axis] = sign
            u, v = [a for a in range(3) if a != axis]
            corners = []
            for cu, cv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = Vector((0.0, 0.0, 0.0))
                p[axis] = sign
                p[u], p[v] = cu, cv
                corners.append(p)
            if (corners[1] - corners[0]).cross(corners[2] - corners[0]).dot(n) < 0:
                corners.reverse()  # Counter-clockwise seen from outside, for back-face culling.
            for p in _tri_list([corners]):
                pos.append(Vector((p.x * sx, p.y * sy, p.z * sz)))
                nor.append(n.copy())
    return pos, nor


def cylinder(r=1.0, h=2.0, segments=16, r_top=None, caps=True):
    """Along Z, centered. r_top != r makes a cone (r_top=0)."""
    r_top = r if r_top is None else r_top
    pos, nor = [], []
    slope = (r - r_top) / h
    for j in range(segments):
        a0, a1 = 2 * math.pi * j / segments, 2 * math.pi * (j + 1) / segments
        c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
        b0, b1 = Vector((c0 * r, s0 * r, -h / 2)), Vector((c1 * r, s1 * r, -h / 2))
        t0, t1 = Vector((c0 * r_top, s0 * r_top, h / 2)), Vector((c1 * r_top, s1 * r_top, h / 2))
        n0, n1 = Vector((c0, s0, slope)).normalized(), Vector((c1, s1, slope)).normalized()
        pos += [b0, b1, t1, b0, t1, t0]
        nor += [n0, n1, n1, n0, n1, n0]
        if caps:
            pos += [Vector((0, 0, -h / 2)), b1, b0]
            nor += [Vector((0, 0, -1))] * 3
            if r_top > 1e-6:
                pos += [Vector((0, 0, h / 2)), t0, t1]
                nor += [Vector((0, 0, 1))] * 3
    return pos, nor


def torus(r=1.0, tube=0.25, segments=20, sides=8):
    pos, nor = [], []
    for i in range(segments):
        for j in range(sides):
            quad, ns = [], []
            for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                a = 2 * math.pi * (i + di) / segments
                b = 2 * math.pi * (j + dj) / sides
                center = Vector((math.cos(a) * r, math.sin(a) * r, 0.0))
                n = Vector((math.cos(a) * math.cos(b), math.sin(a) * math.cos(b), math.sin(b)))
                quad.append(center + n * tube)
                ns.append(n)
            pos += _tri_list([quad])
            nor += _tri_list([ns])
    return pos, nor


def octahedron(length=2.0, width=0.35, head=0.2):
    """Blender's bone shape along +Z, from 0 to length."""
    tip0, tip1 = Vector((0, 0, 0)), Vector((0, 0, length))
    ring = [Vector((width * math.cos(a), width * math.sin(a), head * length))
            for a in (math.pi / 4 + math.pi / 2 * k for k in range(4))]
    pos, nor = [], []
    for k in range(4):
        a, b = ring[k], ring[(k + 1) % 4]
        for tri in ((tip0, b, a), (a, b, tip1)):
            n = (tri[1] - tri[0]).cross(tri[2] - tri[0]).normalized()
            pos += [p.copy() for p in tri]
            nor += [n] * 3
    return pos, nor


def transformed(mesh, matrix):
    pos, nor = mesh
    rot = matrix.to_3x3().inverted_safe().transposed()
    return [matrix @ p for p in pos], [(rot @ n).normalized() for n in nor]


# --- drawing -------------------------------------------------------------------------

class Part:
    """One colored piece of a shape: a mesh in the shape's local space and its color (or None: the
    shape's tint color, e.g. a paint brush's bristles in the current paint color)."""

    def __init__(self, mesh, color=None):
        self.mesh = mesh
        self.color = color


def _batch(key, mesh, shader):
    batch = _batches.get(key)
    if batch is None:
        pos, nor = mesh
        batch = batch_for_shader(shader, 'TRIS', {"pos": [p[:] for p in pos], "nor": [n[:] for n in nor]})
        _batches[key] = batch
    return batch


def draw_shape(key, parts, matrix, light_world, alpha=1.0, glow=0.0, tint=(1.0, 1.0, 1.0)):
    """Draw a cached shape (a list of Part) at matrix. light_world: direction toward the light."""
    shader = _lit_shader()
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    if shader is None:
        flat = gpu.shader.from_builtin('UNIFORM_COLOR')
        for i, part in enumerate(parts):
            pos, _nor = part.mesh
            batch = _batches.get((key, i, "flat"))
            if batch is None:
                batch = batch_for_shader(flat, 'TRIS', {"pos": [p[:] for p in pos]})
                _batches[(key, i, "flat")] = batch
            c = part.color or tint
            flat.uniform_float("color", (*c[:3], alpha))
            batch.draw(flat)
        gpu.matrix.pop()
        return
    mvp = gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix()
    light = (matrix.to_3x3().inverted_safe() @ Vector(light_world)).normalized()
    shader.bind()
    shader.uniform_float("mvp", mvp)
    shader.uniform_float("light", light[:])
    shader.uniform_float("glow", glow)
    for i, part in enumerate(parts):
        c = part.color or tint
        shader.uniform_float("color", (*c[:3], alpha))
        _batch((key, i), part.mesh, shader).draw(shader)
    gpu.matrix.pop()


def begin_solid():
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(True)
    gpu.state.face_culling_set('BACK')


def end_solid():
    gpu.state.face_culling_set('NONE')
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def clear():
    _batches.clear()
    _shader["lit"] = None
    _shader["failed"] = False


def light_from(viewer, at):
    """Light direction for something at `at`: from above and from the viewer's side."""
    d = Vector(viewer) - Vector(at)
    if d.length < 1e-6:
        d = Vector((0.0, -1.0, 0.0))
    return (d.normalized() + Vector((0.0, 0.0, 1.2))).normalized()


def matrix_at(location, rotation, size):
    return Matrix.LocRotScale(Vector(location), rotation, Vector((size, size, size)))
