"""Build the starter kit: vr_sculpt/assets/kit/starter_kit.blend and its catalog file.

Small hard-surface parts (panels, vents, bolts, pipes, greebles) made with bmesh, marked as assets
in catalogs, with previews rendered by kit.render_thumbnail. Vents are cutters (tagged "cutter",
centered on their origin so they sink halfway into a surface); everything else stands on z = 0.
Sizes are real ones, in meters: details for an object a meter or two across.

blender --background --factory-startup --python tests/make_starter_kit.py
"""

import math
import os
import sys
import uuid

import bmesh
import bpy
import gpu
from mathutils import Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vr_sculpt import kit  # noqa: E402

OUT_DIR = os.path.join(ROOT, "vr_sculpt", "assets", "kit")
BLEND = os.path.join(OUT_DIR, "starter_kit.blend")
CM = 0.01
NAMESPACE = uuid.UUID("5f1c1f0e-2d4b-4b63-9a43-7a1e0f6b5a10")  # catalog ids stay the same between builds


def catalog_id(path):
    return str(uuid.uuid5(NAMESPACE, path))


# --- shapes -------------------------------------------------------------------------

def box(bm, center, size, bevel=0.0):
    """A closed box; size in meters. Bevelled edges when bevel > 0."""
    geom = bmesh.ops.create_cube(bm, size=1.0)
    verts = geom["verts"]
    bmesh.ops.scale(bm, vec=Vector(size), verts=verts)
    bmesh.ops.translate(bm, vec=Vector(center), verts=verts)
    if bevel > 0:
        edges = list({e for v in verts for e in v.link_edges})
        bmesh.ops.bevel(bm, geom=edges, offset=bevel, segments=2, profile=0.5, affect='EDGES', clamp_overlap=True)
    return verts


def cylinder(bm, center, radius, depth, segments=24, axis='Z', radius2=None):
    rot = {'Z': Matrix.Identity(4), 'X': Matrix.Rotation(math.pi / 2, 4, 'Y'),
           'Y': Matrix.Rotation(math.pi / 2, 4, 'X')}[axis]
    geom = bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segments, radius1=radius,
                                 radius2=radius if radius2 is None else radius2, depth=depth,
                                 matrix=Matrix.Translation(Vector(center)) @ rot)
    return geom["verts"]


def prism(bm, outline, z0, z1):
    """Extrude a closed 2D outline (counter-clockwise) from z0 to z1."""
    bottom = [bm.verts.new((x, y, z0)) for x, y in outline]
    top = [bm.verts.new((x, y, z1)) for x, y in outline]
    n = len(outline)
    bm.faces.new(list(reversed(bottom)))
    bm.faces.new(top)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((bottom[i], bottom[j], top[j], top[i]))


def stadium(length, radius, segments=10):
    """Outline of a slot: two half circles joined by straight sides, along X."""
    half = length / 2 - radius
    pts = []
    for i in range(segments + 1):
        a = -math.pi / 2 + math.pi * i / segments
        pts.append((half + radius * math.cos(a), radius * math.sin(a)))
    for i in range(segments + 1):
        a = math.pi / 2 + math.pi * i / segments
        pts.append((-half + radius * math.cos(a), radius * math.sin(a)))
    return pts


def dome(bm, center, radius, height, segments=16):
    geom = bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=segments // 2, radius=radius)
    verts = geom["verts"]
    bmesh.ops.bisect_plane(bm, geom=verts + list({e for v in verts for e in v.link_edges}) +
                           list({f for v in verts for f in v.link_faces}),
                           plane_co=(0.0, 0.0, 0.0), plane_no=(0.0, 0.0, 1.0), clear_inner=True)
    bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=0)
    verts = [v for v in bm.verts if v.co.z >= -1e-6 and v.co.length <= radius * 1.001 + 1e-6]
    bmesh.ops.scale(bm, vec=(1.0, 1.0, height / radius), verts=verts)
    bmesh.ops.translate(bm, vec=Vector(center), verts=verts)


def bent_pipe(bm, radius, bend, angle, rings=16, steps=12):
    """A pipe lying on z = 0 along a horizontal arc of the given bend radius, capped at both ends."""
    loops = []
    for i in range(steps + 1):
        a = angle * i / steps
        center = Vector((bend * math.sin(a), bend * (1 - math.cos(a)), radius))
        side = Vector((-math.sin(a), math.cos(a), 0.0))
        up = Vector((0.0, 0.0, 1.0))
        loops.append([bm.verts.new(center + radius * (math.cos(2 * math.pi * k / rings) * side +
                                                      math.sin(2 * math.pi * k / rings) * up))
                      for k in range(rings)])
    for a, b in zip(loops, loops[1:]):
        for k in range(rings):
            bm.faces.new((a[k], a[(k + 1) % rings], b[(k + 1) % rings], b[k]))
    bm.faces.new(list(reversed(loops[0])))
    bm.faces.new(loops[-1])


# --- the parts -----------------------------------------------------------------------

def panel_square(bm):
    box(bm, (0, 0, 0.25 * CM), (10 * CM, 10 * CM, 0.5 * CM), bevel=0.12 * CM)


def panel_long(bm):
    box(bm, (0, 0, 0.25 * CM), (20 * CM, 5 * CM, 0.5 * CM), bevel=0.12 * CM)


def hatch(bm):
    box(bm, (0, 0, 0.2 * CM), (12 * CM, 12 * CM, 0.4 * CM), bevel=0.1 * CM)
    box(bm, (0, 0, 0.55 * CM), (9 * CM, 9 * CM, 0.3 * CM), bevel=0.08 * CM)
    for x in (-2.2 * CM, 2.2 * CM):
        box(bm, (x, 0, 1.0 * CM), (0.6 * CM, 0.6 * CM, 0.6 * CM))
    box(bm, (0, 0, 1.55 * CM), (5.2 * CM, 0.8 * CM, 0.5 * CM), bevel=0.1 * CM)


def vent_slots(bm):
    for i in range(5):
        y = (i - 2) * 1.4 * CM
        prism(bm, [(x, y + yy) for x, yy in stadium(7 * CM, 0.35 * CM, 6)], -1 * CM, 1 * CM)


def round_hole(bm):
    cylinder(bm, (0, 0, 0), 1.5 * CM, 4 * CM, segments=32)


def slot(bm):
    prism(bm, stadium(8 * CM, 1 * CM, 10), -1.5 * CM, 1.5 * CM)


def grille(bm):
    for i in range(4):
        for j in range(4):
            box(bm, ((i - 1.5) * 1.8 * CM, (j - 1.5) * 1.8 * CM, 0), (1.2 * CM, 1.2 * CM, 2 * CM))


def hex_bolt(bm):
    cylinder(bm, (0, 0, 0.06 * CM), 0.85 * CM, 0.12 * CM, segments=24)
    cylinder(bm, (0, 0, 0.12 * CM + 0.225 * CM), 0.6 * CM, 0.45 * CM, segments=6)


def rivet(bm):
    dome(bm, (0, 0, 0), 0.45 * CM, 0.3 * CM)


def socket_bolt(bm):
    cylinder(bm, (0, 0, 0.3 * CM), 0.55 * CM, 0.6 * CM, segments=24, radius2=0.5 * CM)
    # The socket: a hexagon pressed into the top.
    top = max(bm.faces, key=lambda f: f.calc_center_median().z)
    bmesh.ops.delete(bm, geom=[top], context='FACES_ONLY')
    rim = sorted((v for v in bm.verts if v.co.z > 0.59 * CM), key=lambda v: math.atan2(v.co.y, v.co.x) % math.tau)
    corners = [min(range(24), key=lambda i: abs((math.atan2(rim[i].co.y, rim[i].co.x) - math.pi * k / 3 + math.pi)
                                                % math.tau - math.pi)) for k in range(6)]
    hexagon = [bm.verts.new((0.25 * CM * math.cos(math.atan2(rim[i].co.y, rim[i].co.x)),
                             0.25 * CM * math.sin(math.atan2(rim[i].co.y, rim[i].co.x)), 0.6 * CM)) for i in corners]
    floor = [bm.verts.new((v.co.x, v.co.y, 0.3 * CM)) for v in hexagon]
    # The top between the rim and the socket: one face per hexagon side; the socket's walls and floor.
    for k in range(6):
        i0, i1 = corners[k], corners[(k + 1) % 6]
        arc = [rim[i % 24] for i in range(i0, i1 + (24 if i1 < i0 else 0) + 1)]
        bm.faces.new([hexagon[k]] + arc + [hexagon[(k + 1) % 6]])
        bm.faces.new((hexagon[(k + 1) % 6], hexagon[k], floor[k], floor[(k + 1) % 6]))
    bm.faces.new(floor)


def pipe_straight(bm):
    cylinder(bm, (0, 0, 1 * CM), 1 * CM, 20 * CM, segments=16, axis='X')


def pipe_elbow(bm):
    bent_pipe(bm, 1 * CM, 4 * CM, math.pi / 2)


def pipe_flange(bm):
    cylinder(bm, (0, 0, 0.3 * CM), 2 * CM, 0.6 * CM, segments=32)
    cylinder(bm, (0, 0, 0.6 * CM + 0.6 * CM), 1.1 * CM, 1.2 * CM, segments=24)
    for k in range(6):
        a = 2 * math.pi * k / 6
        cylinder(bm, (1.5 * CM * math.cos(a), 1.5 * CM * math.sin(a), 0.6 * CM + 0.15 * CM), 0.25 * CM,
                 0.3 * CM, segments=6)


def box_cluster(bm):
    box(bm, (0, 0, 0.2 * CM), (10 * CM, 8 * CM, 0.4 * CM), bevel=0.08 * CM)
    for (x, y), (sx, sy, sz) in (((-2.5, -1.5), (3, 3, 1.5)), ((2, 1.5), (2, 4, 0.9)), ((2, -2.4), (4, 2, 2.2)),
                                 ((-3, 2.2), (2.5, 2, 0.6))):
        box(bm, (x * CM, y * CM, (0.4 + sz / 2) * CM), (sx * CM, sy * CM, sz * CM), bevel=0.08 * CM)


def ribbed_block(bm):
    box(bm, (0, 0, 0.75 * CM), (8 * CM, 6 * CM, 1.5 * CM), bevel=0.1 * CM)
    for i in range(6):
        box(bm, ((i - 2.5) * 1.2 * CM, 0, 1.75 * CM), (0.4 * CM, 6.4 * CM, 0.5 * CM))


def cable_box(bm):
    box(bm, (0, 0, 1.5 * CM), (6 * CM, 4 * CM, 3 * CM), bevel=0.15 * CM)
    for k in range(3):
        cylinder(bm, (4 * CM, (k - 1) * 1.1 * CM, 0.9 * CM), 0.35 * CM, 2.2 * CM, segments=12, axis='X')


# (name, catalog, build, cutter, description)
PARTS = (
    ("Panel Square", "Panels", panel_square, False, "Bevelled 10 cm plate"),
    ("Panel Long", "Panels", panel_long, False, "Bevelled 20 x 5 cm plate"),
    ("Hatch", "Panels", hatch, False, "Plate with a raised lid and a handle"),
    ("Vent Slots", "Vents", vent_slots, True, "Five rounded slots"),
    ("Round Hole", "Vents", round_hole, True, "3 cm hole"),
    ("Slot", "Vents", slot, True, "8 cm rounded slot"),
    ("Grille", "Vents", grille, True, "4 x 4 square holes"),
    ("Hex Bolt", "Bolts", hex_bolt, False, "Hex head on a washer"),
    ("Rivet", "Bolts", rivet, False, "Dome rivet"),
    ("Socket Bolt", "Bolts", socket_bolt, False, "Round head with a hex socket"),
    ("Pipe Straight", "Pipes", pipe_straight, False, "20 cm pipe, 2 cm across"),
    ("Pipe Elbow", "Pipes", pipe_elbow, False, "90 degree bend"),
    ("Pipe Flange", "Pipes", pipe_flange, False, "Bolted flange, standing up"),
    ("Box Cluster", "Greebles", box_cluster, False, "Boxes on a plate"),
    ("Ribbed Block", "Greebles", ribbed_block, False, "Block with cooling ribs"),
    ("Cable Box", "Greebles", cable_box, False, "Box with three cables out the side"),
)


def make_mesh(name, build):
    bm = bmesh.new()
    build(bm)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    return mesh


def set_preview(idb, parts, cutter=False):
    pixels = kit.render_thumbnail(parts, cutter=cutter)
    if pixels is None:
        raise RuntimeError("No GPU for the previews")
    prev = idb.preview_ensure()
    prev.image_size = (kit.THUMB, kit.THUMB)
    prev.image_pixels_float.foreach_set(pixels)


def mark(idb, catalog, description, cutter=False):
    idb.asset_mark()
    idb.asset_data.catalog_id = catalog_id(catalog)
    idb.asset_data.description = description
    idb.asset_data.author = "VR Sculpt"
    if cutter:
        idb.asset_data.tags.new(kit.CUTTER_TAG)


def pipe_run(scene):
    """A collection asset: a pipe with a flange at each end."""
    coll = bpy.data.collections.new("Pipe Run")
    scene.collection.children.link(coll)
    pipe = bpy.data.objects.new("Pipe Run Pipe", make_mesh("Pipe Run Pipe", lambda bm: cylinder(
        bm, (0, 0, 2 * CM), 1 * CM, 30 * CM, segments=16, axis='X')))
    coll.objects.link(pipe)
    flange_mesh = make_mesh("Pipe Run Flange", lambda bm: cylinder(bm, (0, 0, 0), 2 * CM, 0.8 * CM, segments=32,
                                                                      axis='X'))
    for side in (-1, 1):
        flange = bpy.data.objects.new("Pipe Run Flange", flange_mesh)
        flange.location = (side * 14 * CM, 0, 2 * CM)
        coll.objects.link(flange)
    bpy.context.view_layer.update()
    mark(coll, "Pipes", "Pipe with a flange at each end (a collection: placed as an instance)")
    set_preview(coll, [(o.data, o.matrix_world) for o in coll.objects])


def main():
    gpu.init()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    catalogs = []
    for i, (name, catalog, build, cutter, description) in enumerate(PARTS):
        obj = bpy.data.objects.new(name, make_mesh(name, build))
        scene.collection.objects.link(obj)
        obj.location = ((i % 4) * 0.3, 0.5 + (i // 4) * 0.3, 0.0)  # laid out, for editing the file
        if cutter:
            obj.display_type = 'WIRE'
        mark(obj, catalog, description, cutter)
        set_preview(obj, [(obj.data, Matrix.Identity(4))], cutter)
        if catalog not in catalogs:
            catalogs.append(catalog)
    pipe_run(scene)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, kit.CATS_FILE), "w", encoding="utf-8", newline="\n") as f:
        f.write(kit.CATS_HEADER)
        for catalog in catalogs:
            f.write(f"{catalog_id(catalog)}:{catalog}:{catalog}\n")
    bpy.ops.wm.save_as_mainfile(filepath=BLEND, compress=True)
    print(f"Saved {len(PARTS) + 1} parts to {BLEND} ({os.path.getsize(BLEND)} bytes)")


main()
