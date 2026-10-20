"""Onion skin: see-through copies of the selection at other frames, for meshes too (Blender's own
onion skinning is Grease Pencil only).

Drawing ghosts is cheap; finding out what a deformed mesh looks like at another frame isn't
(the scene has to be evaluated at that frame). So ghosts are computed in the app timer, with the
frame put back right after, cached as world-space triangles, and only recomputed when the
current frame, the keys or the objects change. While the timeline plays they are hidden.

The selection's meshes are ghosted; for a selected armature, the meshes it deforms.

Changing frames re-applies the keys, which would throw away a pose (or a move) that isn't keyed
yet, so the bones' and objects' own transforms are put back after the ghosts are computed.
"""

import time

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

from . import anim

BEFORE_COLOR = (1.0, 0.45, 0.35)
AFTER_COLOR = (0.4, 0.9, 0.5)
ALPHA = 0.22
MAX_TRIANGLES = 300_000  # per ghost; bigger meshes are thinned out

_state = {"dirty": True, "signature": None, "computing": False, "version": 0, "ms": 0.0}
_watched = set()  # names of the IDs whose changes make the ghosts stale
_ghosts = []  # [(color, alpha, float32 (n*3, 3) world-space triangle corners)]
_batches = {"version": -1, "list": []}


def mark_dirty():
    _state["dirty"] = True


def meshes(context):
    """The meshes to ghost: selected meshes, plus meshes deformed by selected armatures."""
    sel = anim.selection(context)
    out = [o for o in sel if o.type == 'MESH']
    rigs = {o for o in sel if o.type == 'ARMATURE'}
    if rigs:
        for o in context.scene.objects:
            if o.type == 'MESH' and o not in out and any(
                    m.type == 'ARMATURE' and m.object in rigs for m in o.modifiers):
                out.append(o)
    return [o for o in out if o.visible_get()]


def ghost_frames(context):
    """[(frame, side)] to ghost; side -1 = before, +1 = after, nearest first."""
    settings = context.scene.vr_sculpt
    scene = context.scene
    current = scene.frame_current
    n = settings.onion_count
    if settings.onion_mode == 'FRAMES':
        before = [current - settings.onion_step * i for i in range(1, n + 1)]
        after = [current + settings.onion_step * i for i in range(1, n + 1)]
    else:
        objs = anim.selection(context)
        frames = [int(round(f)) for f in anim.key_frames(objs)]
        before = sorted({f for f in frames if f < current}, reverse=True)[:n]
        after = sorted({f for f in frames if f > current})[:n]
    lo, hi = scene.frame_start, scene.frame_end
    return [(f, -1) for f in before if lo <= f <= hi] + [(f, 1) for f in after if lo <= f <= hi]


def _signature(context):
    objs = meshes(context)
    return (context.scene.frame_current, tuple(o.name for o in objs), tuple(ghost_frames(context)))


def _snapshot(context, objs):
    """[(owner, matrix_basis)] of what a frame change could undo: the selected objects, the meshes
    being ghosted, and every bone of the armatures that deform them."""
    owners = {o for o in anim.selection(context)} | set(objs)
    rigs = {o for o in owners if o.type == 'ARMATURE'}
    for o in objs:
        rigs.update(m.object for m in o.modifiers if m.type == 'ARMATURE' and m.object)
    out = [(o, o.matrix_basis.copy()) for o in owners | rigs]
    for rig in rigs:
        out += [(pb, pb.matrix_basis.copy()) for pb in rig.pose.bones]
    return out


def _restore(context, snapshot):
    restored = 0
    for owner, basis in snapshot:
        if owner.matrix_basis != basis:
            owner.matrix_basis = basis
            restored += 1
    if restored:
        context.view_layer.update()
    return restored


def _triangles(obj, depsgraph):
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        me.calc_loop_triangles()
        n = len(me.loop_triangles)
        if n == 0:
            return None
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        tri = np.empty(n * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tri)
        tri = tri.reshape(-1, 3)
        if n > MAX_TRIANGLES:
            tri = tri[::int(np.ceil(n / MAX_TRIANGLES))]
        mw = np.array(ev.matrix_world, dtype=np.float32)
        world = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
        return world[tri.ravel()]
    finally:
        ev.to_mesh_clear()


def update(context, force=False):
    """Recompute the ghosts if anything they depend on changed. Call from a timer, not a draw callback."""
    settings = context.scene.vr_sculpt
    if not settings.onion or anim.is_playing(context):
        if _ghosts:
            _ghosts.clear()
            _state["version"] += 1
        _state["signature"] = None
        return False
    sig = _signature(context)
    if not force and not _state["dirty"] and sig == _state["signature"]:
        return False
    from . import puppet
    if not force and puppet.live():
        return False  # Bones are being posed live: catch up once that stops (the ghosts stay dirty).
    scene = context.scene
    current = scene.frame_current
    t0 = time.perf_counter()
    ghosts = []
    _state["computing"] = True
    objs = meshes(context)
    snapshot = _snapshot(context, objs)
    try:
        frames = ghost_frames(context)
        for rank, (frame, side) in enumerate(sorted(frames, key=lambda fs: abs(fs[0] - current))):
            scene.frame_set(frame)
            depsgraph = context.evaluated_depsgraph_get()
            for obj in objs:
                tris = _triangles(obj, depsgraph)
                if tris is not None:
                    nth = sum(1 for f, s in frames if s == side and abs(f - current) < abs(frame - current))
                    alpha = ALPHA / (1 + nth)  # Further ghosts are fainter.
                    ghosts.append((BEFORE_COLOR if side < 0 else AFTER_COLOR, alpha, tris))
    finally:
        if scene.frame_current != current:
            scene.frame_set(current)
        _restore(context, snapshot)
        _state["computing"] = False
    _ghosts[:] = ghosts
    _watched.clear()
    for obj in objs + anim.selection(context):
        _watched.add(obj.name)
        _watched.update(m.object.name for m in getattr(obj, "modifiers", ()) if m.type == 'ARMATURE' and m.object)
        ad = obj.animation_data
        if ad is not None and ad.action is not None:
            _watched.add(ad.action.name)
    _state.update(dirty=False, signature=_signature(context), ms=(time.perf_counter() - t0) * 1000.0)
    _state["version"] += 1
    return True


def ghost_count():
    return len(_ghosts)


def draw(context):
    """Draw the cached ghosts (GPU batches are rebuilt only when the ghosts change)."""
    if not context.scene.vr_sculpt.onion or not _ghosts:
        return
    if _batches["version"] != _state["version"]:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        _batches["list"] = [(color, alpha, batch_for_shader(shader, 'TRIS', {"pos": tris}))
                            for color, alpha, tris in _ghosts]
        _batches["version"] = _state["version"]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    gpu.state.face_culling_set('BACK')
    for color, alpha, batch in _batches["list"]:
        shader.uniform_float("color", (*color, alpha))
        batch.draw(shader)
    gpu.state.face_culling_set('NONE')
    gpu.state.depth_mask_set(True)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


@bpy.app.handlers.persistent
def _on_depsgraph(scene, depsgraph):
    if _state["computing"]:
        return
    for u in depsgraph.updates:
        if u.id.name in _watched:
            _state["dirty"] = True
            return


def register():
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    _ghosts.clear()
    _batches.update(version=-1, list=[])
