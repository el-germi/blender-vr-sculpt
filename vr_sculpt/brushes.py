"""Numpy sculpt brush engine.

Works on a mesh's vertex positions in object-local space. Nothing here knows
about VR: callers pass brush positions/matrices in world space and the engine
converts them. That keeps it testable in background mode.
"""

import time

import numpy as np
from mathutils import Matrix

BRUSHES = ("DRAW", "SMOOTH", "GRAB", "INFLATE")


def falloff(dist, radius):
    """Smooth falloff: 1 at the center, 0 at the radius."""
    t = np.clip(1.0 - dist / radius, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _mat_np(m):
    return np.array(m, dtype=np.float64)


def _apply_mat(m, pts):
    return pts @ m[:3, :3].T + m[:3, 3]


class MeshState:
    """Cached numpy copy of one mesh object's geometry.

    Strokes from both hands share one state so they don't overwrite each other.
    """

    def __init__(self, obj):
        self.obj = obj
        self.mesh = obj.data
        me = self.mesh
        n = len(me.vertices)
        self.num_edges = len(me.edges)
        # float32 reads are a plain copy; float64 would convert per element and is much slower.
        self._buf = np.empty(n * 3, dtype=np.float32)
        me.attributes["position"].data.foreach_get("vector", self._buf)
        self.co = self._buf.reshape(n, 3).astype(np.float64)
        self.normals = None
        self.refresh_normals()

        self._grid = None
        self.update_world()
        self.last_write_ms = 0.0
        self.last_dab_ms = 0.0

    def is_valid_for(self, obj):
        try:
            return (obj is not None and obj == self.obj and obj.data == self.mesh
                    and len(obj.data.vertices) == len(self.co)
                    and len(obj.data.edges) == self.num_edges)
        except ReferenceError:
            return False

    def update_world(self):
        mw = self.obj.matrix_world
        self.world = _mat_np(mw)
        self.world_inv = _mat_np(mw.inverted_safe())
        # Mean scale, used to convert a world-space radius to local space.
        self.scale = float(np.mean(mw.to_scale()))

    def refresh_normals(self):
        n = len(self.co)
        normals = np.empty(n * 3, dtype=np.float32)
        self.mesh.vertex_normals.foreach_get("vector", normals)
        self.normals = normals.reshape(n, 3).astype(np.float64)

    @property
    def edges(self):
        return _edges(self)

    def write(self):
        t0 = time.perf_counter()
        self._buf[:] = self.co.reshape(-1)
        self.mesh.attributes["position"].data.foreach_set("vector", self._buf)
        self.mesh.update()
        self.last_write_ms = (time.perf_counter() - t0) * 1000.0

    # --- helpers ---------------------------------------------------------

    def to_local(self, p_world):
        return _apply_mat(self.world_inv, np.asarray(p_world, dtype=np.float64)[None, :])[0]

    def local_radius(self, radius_world):
        return radius_world / max(self.scale, 1e-9)

    def select(self, center, radius):
        """Indices and falloff weights of vertices inside the sphere."""
        if len(self.co) >= GRID_MIN_VERTS:
            if self._grid is None or self._grid.stale(radius):
                self._grid = _Grid(self.co, radius)
            cand = self._grid.candidates(center, radius)
        else:
            cand = np.arange(len(self.co))
        d = self.co[cand] - center
        dist2 = np.einsum("ij,ij->i", d, d)
        keep = dist2 < radius * radius
        return cand[keep], falloff(np.sqrt(dist2[keep]), radius)

    def moved(self, distance):
        """Report how far vertices moved, so the grid knows when to rebuild."""
        if self._grid is not None:
            self._grid.drift += distance

    def neighbor_mean(self, idx):
        """Average position of the edge-neighbors of each vertex in idx."""
        indptr, nbr = _adjacency(self)
        starts = indptr[idx]
        counts = indptr[idx + 1] - starts
        mean = self.co[idx].copy()
        has = counts > 0
        if not has.any():
            return mean
        starts, counts = starts[has], counts[has]
        offsets = np.cumsum(counts) - counts
        flat = np.repeat(starts - offsets, counts) + np.arange(counts.sum())
        sums = np.add.reduceat(self.co[nbr[flat]], offsets, axis=0)
        mean[has] = sums / counts[:, None]
        return mean


GRID_MIN_VERTS = 20000


class _Grid:
    """Uniform hash grid over vertex positions, rebuilt when vertices drift too far."""

    _OFF = 1 << 20

    def __init__(self, co, cell):
        self.co = co
        self.cell = cell
        self.drift = 0.0
        keys = self._keys(np.floor(co / cell).astype(np.int64))
        self.order = np.argsort(keys, kind="stable")
        self.sorted_keys = keys[self.order]

    @classmethod
    def _keys(cls, ijk):
        ijk = ijk + cls._OFF
        return (ijk[..., 0] << 42) | (ijk[..., 1] << 21) | ijk[..., 2]

    def stale(self, radius):
        return radius > self.cell * 1.01 or radius < self.cell * 0.5 or self.drift > self.cell * 0.5

    def candidates(self, center, radius):
        reach = radius + self.drift
        lo = np.floor((center - reach) / self.cell).astype(np.int64)
        hi = np.floor((center + reach) / self.cell).astype(np.int64)
        ranges = [np.arange(lo[i], hi[i] + 1) for i in range(3)]
        cells = np.stack(np.meshgrid(*ranges, indexing="ij"), axis=-1).reshape(-1, 3)
        keys = self._keys(cells)
        left = np.searchsorted(self.sorted_keys, keys, side="left")
        right = np.searchsorted(self.sorted_keys, keys, side="right")
        counts = right - left
        if counts.sum() == 0:
            return np.empty(0, dtype=np.int64)
        offsets = np.cumsum(counts) - counts
        flat = np.repeat(left - offsets, counts) + np.arange(counts.sum())
        return self.order[flat]


_edges_cache = {}
_adjacency_cache = {}


def forget_caches():
    """Keyed by mesh pointers, which a new mesh can reuse after an undo."""
    _edges_cache.clear()
    _adjacency_cache.clear()


def _topology_key(state):
    return (state.mesh.as_pointer(), len(state.co), state.num_edges)


def _edges(state):
    """Edge vertex pairs. Reading them is slow, so only Smooth asks, and it's cached
    while the mesh keeps the same vertex and edge counts."""
    key = _topology_key(state)
    hit = _edges_cache.get(key)
    if hit is None:
        ed = np.empty(state.num_edges * 2, dtype=np.int32)
        state.mesh.edges.foreach_get("vertices", ed)
        hit = ed.reshape(-1, 2)
        _edges_cache.clear()
        _edges_cache[key] = hit
    return hit


def _adjacency(state):
    """CSR vertex adjacency, cached while the mesh topology stays the same."""
    key = _topology_key(state)
    hit = _adjacency_cache.get(key)
    if hit is not None:
        return hit
    n = len(state.co)
    e = state.edges.astype(np.int64)
    a = np.concatenate([e[:, 0], e[:, 1]])
    b = np.concatenate([e[:, 1], e[:, 0]])
    order = np.argsort(a, kind="stable")
    nbr = b[order]
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(a, minlength=n), out=indptr[1:])
    _adjacency_cache.clear()
    _adjacency_cache[key] = (indptr, nbr)
    return indptr, nbr


MIRROR_X = np.diag([-1.0, 1.0, 1.0])


def _mirror_diag(mask):
    return np.array([-1.0 if mask & (1 << a) else 1.0 for a in range(3)])


class Stroke:
    """One trigger press of one hand."""

    def __init__(self, state, brush, radius_world, strength, invert=False,
                 symmetry_x=False, grab_rotate=True, spacing=0.15, symmetry=0):
        self.state = state
        self.brush = brush
        self.radius_world = radius_world
        self.strength = strength
        self.invert = invert
        # Bitmask of mirrored axes in object space: 1 = X, 2 = Y, 4 = Z.
        self.symmetry = symmetry | (1 if symmetry_x else 0)
        self.grab_rotate = grab_rotate
        self.spacing = spacing
        self.last_local = None
        self.dabs = 0
        self.touched = 0
        # Grab data.
        self.grab_sets = None
        self.grab_start_inv = None
        state.update_world()
        state.refresh_normals()

    # Centers in local space, including mirrored ones: (center, mirror mask).
    def _centers(self, c):
        out = [(c, 0)]
        for mask in range(1, 8):
            if mask & ~self.symmetry:
                continue
            # A dab on a mirror plane would just hit itself again.
            if any(mask & (1 << a) and abs(c[a]) <= 1e-9 for a in range(3)):
                continue
            out.append((c * _mirror_diag(mask), mask))
        return out

    def update(self, tip_world, pose_world=None, pressure=1.0):
        """Feed the current controller tip (world space). Returns number of verts touched."""
        t0 = time.perf_counter()
        st = self.state
        r = st.local_radius(self.radius_world)
        tip = st.to_local(tip_world)

        if self.brush == "GRAB":
            touched = self._grab(tip, r, pose_world)
        else:
            touched = 0
            # Interpolate dabs so fast motion doesn't leave gaps.
            if self.last_local is None:
                points = [tip]
            else:
                dist = np.linalg.norm(tip - self.last_local)
                step = max(r * self.spacing, 1e-9)
                if dist < step:
                    st.last_dab_ms = (time.perf_counter() - t0) * 1000.0
                    return 0
                count = min(int(dist / step), 16)
                points = [self.last_local + (tip - self.last_local) * ((i + 1) / count)
                          for i in range(count)]
            for p in points:
                for c, _mirrored in self._centers(p):
                    touched += self._dab(c, r, pressure)
            self.last_local = tip

        if touched:
            st.write()
        self.touched = touched
        st.last_dab_ms = (time.perf_counter() - t0) * 1000.0
        return touched

    def _dab(self, c, r, pressure):
        st = self.state
        idx, w = st.select(c, r)
        if len(idx) == 0:
            return 0
        amount = self.strength * pressure
        sign = -1.0 if self.invert else 1.0
        if self.brush == "DRAW":
            n = (st.normals[idx] * w[:, None]).sum(axis=0)
            ln = np.linalg.norm(n)
            if ln < 1e-12:
                return 0
            n /= ln
            st.co[idx] += n[None, :] * (w * amount * r * 0.04 * sign)[:, None]
            st.moved(amount * r * 0.04)
        elif self.brush == "INFLATE":
            st.co[idx] += st.normals[idx] * (w * amount * r * 0.04 * sign)[:, None]
            st.moved(amount * r * 0.04)
        elif self.brush == "SMOOTH":
            mean = st.neighbor_mean(idx)
            step = (mean - st.co[idx]) * np.clip(w * amount, 0.0, 1.0)[:, None]
            st.co[idx] += step
            st.moved(float(np.sqrt(np.einsum("ij,ij->i", step, step).max())))
        self.dabs += 1
        return len(idx)

    def _grab(self, tip, r, pose_world):
        st = self.state
        if pose_world is None:
            pose_world = Matrix.Translation(st.world[:3, :3] @ tip + st.world[:3, 3])
        pose = _mat_np(pose_world)
        if self.grab_sets is None:
            # First event: capture the vertices under the tip.
            self.grab_sets = []
            for c, mirrored in self._centers(tip):
                idx, w = st.select(c, r)
                if len(idx):
                    self.grab_sets.append((idx, w, st.co[idx].copy(), mirrored))
            self.grab_start_inv = np.linalg.inv(pose)
            return 0
        if not self.grab_sets:
            return 0
        # Controller delta in world space, then in object-local space.
        delta = pose @ self.grab_start_inv
        if not self.grab_rotate:
            delta[:3, :3] = np.eye(3)
        local = st.world_inv @ delta @ st.world
        touched = 0
        for idx, w, orig, mirrored in self.grab_sets:
            m = local
            if mirrored:
                s = np.eye(4)
                s[:3, :3] = np.diag(_mirror_diag(mirrored))
                m = s @ local @ s
            moved = _apply_mat(m, orig)
            st.co[idx] = orig + (moved - orig) * w[:, None]
            touched += len(idx)
        self.dabs += 1
        return touched
