"""Convert Meta's Quest Pro controller models (the "Oculus Controller Art" download) into the add-on's
compact controller assets: vr_sculpt/assets/controllers/{left,right}.npz.

blender --background --factory-startup --python tools/convert_controllers.py -- <download folder>

The download folder holds models/questpro_controllers_{left,right}.fbx (ASCII FBX, read with Blender's
newer FBX importer) and textures/controller_{l,r}_lo_{BaseColor,ORM}.png. Each .npz holds the mesh in
the controller frame (meters; OpenXR axes: +X right, +Y up, -Z forward; origin at the model's origin),
split into parts by bone (every vertex follows one bone), and for each moving part how it moves at a
full press, read from the file's own press animation, and the texture: the base color with the
ambient occlusion multiplied in, at 1024 px.
"""

import os
import sys

import bpy
import numpy as np
from mathutils import Matrix, Vector

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vr_sculpt", "assets",
                   "controllers")
TEXTURE_SIZE = 1024

# FBX bone -> part. The face buttons: Y / B on top (further from you), X / A below.
BONES = {
    'left': {'b_button_y': 'top', 'b_button_x': 'bottom', 'b_button_oculus': 'system',
             'b_trigger_front': 'trigger', 'b_trigger_grip': 'grip', 'b_thumbstick': 'stick'},
    'right': {'b_button_b': 'top', 'b_button_a': 'bottom', 'b_button_oculus': 'system',
              'b_trigger_front': 'trigger', 'b_trigger_grip': 'grip', 'b_thumbstick': 'stick'},
}
PARTS = ('body', 'top', 'bottom', 'system', 'trigger', 'grip', 'stick')

# The file is Y up, +Z forward, in centimeters: turning it 180 degrees about Y gives OpenXR's -Z forward.
FLIP = Matrix.Diagonal((-1.0, 1.0, -1.0))


def to_c(v):
    return FLIP @ Vector(v) * 0.01


def import_fbx(path):
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.wm.fbx_import(filepath=path)
    arm = next(o for o in bpy.data.objects if o.type == 'ARMATURE')
    return arm


def mesh_arrays(hand, arm):
    """Vertices (one per distinct corner), triangles and each vertex's part, in the controller frame."""
    bones = BONES[hand]
    pos, nor, uv, part, tris = [], [], [], [], []
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or "battery" in obj.name:
            continue
        me = obj.data
        assert obj.matrix_world == arm.matrix_world, "meshes are expected in the armature's space"
        groups = {g.index: g.name for g in obj.vertex_groups}
        vpart = []
        for v in me.vertices:
            best = max(v.groups, key=lambda g: g.weight, default=None)
            name = groups.get(best.group) if best else None
            vpart.append(PARTS.index(bones.get(name, 'body')))
        uvs = me.uv_layers.active.data
        normals = me.corner_normals
        seen = {}
        for lt in me.loop_triangles:
            tri = []
            for li in lt.loops:
                vi = me.loops[li].vertex_index
                n = normals[li].vector
                t = uvs[li].uv
                key = (vi, round(n.x, 4), round(n.y, 4), round(n.z, 4), round(t.x, 5), round(t.y, 5))
                i = seen.get(key)
                if i is None:
                    i = seen[key] = len(pos)
                    pos.append(to_c(me.vertices[vi].co))
                    nor.append((FLIP @ n).normalized())
                    uv.append((t.x, t.y))
                    part.append(vpart[vi])
                tri.append(i)
            tris.append(tri)
    return (np.array(pos, np.float32), np.array(nor, np.float32), np.array(uv, np.float32),
            np.array(part, np.uint8), np.array(tris, np.uint32))


def poses(hand, arm, pos, part):
    """For each moving part: pivot, rotation axis and angle, and move, at a full press (from the action's
    keys). The stick gets two rotations, forward and right, picked by where they take its tip."""
    bones = BONES[hand]
    scene = bpy.context.scene
    start, end = (int(f) for f in arm.animation_data.action.frame_range)
    found = {}
    for frame in range(start, end + 1):
        scene.frame_set(frame)
        for bone_name, name in bones.items():
            pb = arm.pose.bones[bone_name]
            rest = pb.bone.matrix_local.to_3x3()
            q = pb.matrix_basis.to_quaternion()
            loc = pb.matrix_basis.translation
            axis, angle = q.to_axis_angle()
            entry = {"pivot": to_c(pb.bone.head_local), "axis": (FLIP @ (rest @ axis)).normalized(),
                     "angle": angle, "move": to_c(rest @ loc)}
            size = angle * 0.05 + entry["move"].length
            if size < 1e-5:
                continue
            if name == 'stick':
                # Where the stick's cap goes: forward (-Z) or right (+X).
                cap = Vector(pos[part == PARTS.index('stick')].mean(axis=0)) + Vector((0, 0.01, 0))
                p = entry["pivot"]
                moved = Matrix.Rotation(angle, 3, entry["axis"]) @ (cap - p) + p - cap
                for key, score in (('stick_forward', -moved.z), ('stick_right', moved.x)):
                    if score > 0.9 * moved.length and (key not in found or size > found[key][0]):
                        found[key] = (size, entry)
            elif name not in found or size > found[name][0]:
                found[name] = (size, entry)
    return {k: e for k, (_s, e) in found.items()}


def texture(hand, folder):
    """Base color times ambient occlusion, TEXTURE_SIZE square, RGB bytes, bottom row first (as UVs)."""
    side = hand[0]
    imgs = []
    for kind in ("BaseColor", "ORM"):
        img = bpy.data.images.load(os.path.join(folder, "textures", f"controller_{side}_lo_{kind}.png"))
        img.colorspace_settings.name = 'Non-Color'  # Keep the stored values: the add-on draws them as they are.
        img.scale(TEXTURE_SIZE, TEXTURE_SIZE)
        px = np.empty(TEXTURE_SIZE * TEXTURE_SIZE * 4, np.float32)
        img.pixels.foreach_get(px)
        imgs.append(px.reshape(TEXTURE_SIZE, TEXTURE_SIZE, 4))
    color, orm = imgs
    rgb = color[..., :3] * orm[..., :1]
    return np.clip(np.round(rgb * 255.0), 0, 255).astype(np.uint8)


def convert(folder):
    os.makedirs(OUT, exist_ok=True)
    for hand in ('left', 'right'):
        arm = import_fbx(os.path.join(folder, "models", f"questpro_controllers_{hand}.fbx"))
        pos, nor, uv, part, tris = mesh_arrays(hand, arm)
        moves = poses(hand, arm, pos, part)
        names = sorted(moves)
        np.savez_compressed(
            os.path.join(OUT, f"{hand}.npz"), pos=pos, nor=nor, uv=uv, part=part, tris=tris,
            parts=np.array(PARTS), moves=np.array(names),
            pivot=np.array([moves[n]["pivot"] for n in names], np.float32),
            axis=np.array([moves[n]["axis"] for n in names], np.float32),
            angle=np.array([moves[n]["angle"] for n in names], np.float32),
            move=np.array([moves[n]["move"] for n in names], np.float32), tex=texture(hand, folder))
        print(f"{hand}: {len(pos)} vertices, {len(tris)} triangles, "
              f"bounds {pos.min(axis=0).round(4)} .. {pos.max(axis=0).round(4)}")
        for i, p in enumerate(PARTS):
            sel = pos[part == i]
            if len(sel):
                print(f"  {p:8s} {len(sel):5d} verts, center {sel.mean(axis=0).round(4)}")
        for n in names:
            m = moves[n]
            print(f"  move {n:14s} pivot {tuple(round(v, 4) for v in m['pivot'])} axis "
                  f"{tuple(round(v, 3) for v in m['axis'])} {np.degrees(m['angle']):.1f} deg, "
                  f"move {tuple(round(v * 1000, 2) for v in m['move'])} mm")


if __name__ == "__main__":
    convert(sys.argv[sys.argv.index("--") + 1])
