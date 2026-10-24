"""Kit parts: mesh and collection assets from Blender's asset libraries, placed in Model mode.

Where they come from: the starter kit that ships in assets/kit, "My Kit" (parts saved from VR, in
the extension's user folder), then every asset library in Preferences > File Paths. The library's
asset catalogs (blender_assets.cats.txt) are the categories.

Listing: reading a .blend means reading the whole file, and asset libraries hold files of hundreds
of megabytes, so it happens in a background Blender (kit_worker.py), never in the session. It links
each file's assets there and writes their catalog, tags and preview (raw RGBA bytes) to a cache
folder, one json per file, keyed by the file's path and checked against its size and time. The
session only reads that cache: tick() (from live.py) takes in each file as the worker finishes it,
so parts appear while it works, and a later session starts from the cache with no worker at all.
Previews become GPU textures lazily, a few per frame, as the panel draws them.

Placing: a part is appended the way the Asset Browser's "Append (Reuse Data)" does it
(reuse_local_id), so every copy of a part shares one mesh, and its asset data is cleared. A
collection asset is placed as a collection instance (an empty). A part tagged "cutter" (or
carrying the vrs_cutter flag of a VR cutter) defaults to New as Cut when it's chosen.

Add to Kit writes the selected mesh, as it looks (modifiers applied, scale baked in), to its own
.blend in My Kit, marked as an asset, with a preview rendered here (render_thumbnail).
"""

import hashlib
import json
import os
import subprocess
import time
import uuid

import bpy
import numpy as np
from mathutils import Matrix, Vector

PREFIX = "kit:"  # settings.primitive holds "kit:<blend>|<OBJECT or COLLECTION>|<name>" for a part
CATS_FILE = "blender_assets.cats.txt"
STARTER = "Starter Kit"
STARTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "kit")
MINE = "My Kit"
UNASSIGNED = "Unassigned"
CUTTER_TAG = "cutter"
ID_DIRS = {'OBJECT': "Object", 'COLLECTION': "Collection"}
MAX_FILES = 2000  # per library: a huge library is listed in part rather than stall the session
THUMB = 128  # pixels, previews rendered here
THUMBS_PER_FRAME = 4  # new thumbnail textures per frame
MESSAGE_SECONDS = 4.0

CATS_HEADER = """# This is an Asset Catalog Definition file for Blender.
#
# Empty lines and lines starting with `#` will be ignored.
# The first non-ignored line should be the version indicator.
# Other lines are of the format "UUID:catalog/path/for/assets:simple catalog name"

VERSION 1

"""


class Asset:
    """One part: an object or collection asset in a .blend file."""

    def __init__(self, library, blend, id_type, name, catalog="", cutter=False, description=""):
        self.library = library  # library name, "" when unknown
        self.blend = blend
        self.id_type = id_type  # 'OBJECT' or 'COLLECTION'
        self.name = name
        self.catalog = catalog  # catalog path ("Panels/Vents"), "" when not in one
        self.cutter = cutter
        self.description = description
        self.preview = None  # (width, height, raw RGBA file in the cache), or None without one

    @property
    def key(self):
        return f"{PREFIX}{self.blend}|{self.id_type}|{self.name}"

    def __repr__(self):
        return f"Asset({self.library}/{self.catalog}: {self.name} [{self.id_type}]{' cutter' if self.cutter else ''})"


class Library:
    def __init__(self, name, folder, assets):
        self.name, self.folder, self.assets = name, folder, assets

    def catalogs(self):
        """The catalog paths that hold parts, sorted ("" for parts in none)."""
        return sorted({a.catalog for a in self.assets}, key=lambda c: (c == "", c.lower()))

    def in_catalog(self, catalog):
        """Parts in catalog and its sub-catalogs; None for all of them."""
        if catalog is None:
            return list(self.assets)
        return [a for a in self.assets if a.catalog == catalog or (catalog and a.catalog.startswith(catalog + "/"))]


def is_key(key):
    return bool(key) and key.startswith(PREFIX)


def parse_key(key):
    """(blend, id_type, name) of a part key."""
    blend, id_type, name = key[len(PREFIX):].rsplit("|", 2)
    return blend, id_type, name


def catalog_label(catalog):
    return catalog.replace("/", " / ") if catalog else UNASSIGNED


# --- libraries -------------------------------------------------------------------

_user_dir = [None]  # tests point My Kit at a folder of their own


def user_dir():
    """My Kit's folder (it may not exist yet)."""
    if _user_dir[0]:
        return _user_dir[0]
    try:
        return bpy.utils.extension_path_user(__package__, path="kit")
    except ValueError:  # Loaded from source (tests), not as an installed extension.
        return os.path.join(bpy.utils.user_resource('CONFIG'), "vr_sculpt", "kit")


def _same_folder(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def libraries(context=None):
    """[(name, folder)]: the starter kit, My Kit, then the asset libraries in the Preferences."""
    out = [(STARTER, STARTER_DIR), (MINE, user_dir())]
    prefs = (context or bpy.context).preferences
    for lib in prefs.filepaths.asset_libraries:
        path = getattr(lib, "path", "")
        if not getattr(lib, "enabled", True) or not path:
            continue
        path = bpy.path.abspath(path)
        if os.path.isdir(path) and not any(_same_folder(path, folder) for _name, folder in out):
            out.append((lib.name, path))
    return out


def _read_catalogs(folder):
    """{catalog uuid: catalog path} from the library's catalog file."""
    out = {}
    try:
        with open(os.path.join(folder, CATS_FILE), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("VERSION"):
                    continue
                parts = line.split(":")
                if len(parts) >= 2:
                    out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


def _blend_files(folder):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(".blend")]
        if len(out) >= MAX_FILES:
            break
    return out[:MAX_FILES]


# --- the listing cache and its worker ---------------------------------------------------

_cache_dir = [None]  # tests point the cache at a folder of their own


def cache_dir():
    if _cache_dir[0]:
        return _cache_dir[0]
    try:
        return bpy.utils.extension_path_user(__package__, path="kit_cache")
    except ValueError:
        return os.path.join(bpy.utils.user_resource('CONFIG'), "vr_sculpt", "kit_cache")


def _cache_paths(path):
    """(listing json, thumbnail folder) in the cache for a .blend file."""
    key = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:24]
    return os.path.join(cache_dir(), key + ".json"), os.path.join(cache_dir(), key)


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime, st.st_size


def _load_listing(path, stat):
    """The cached listing of path if it's of the file as it is now, else None."""
    js, thumbs = _cache_paths(path)
    try:
        with open(js, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if (data.get("mtime"), data.get("size")) != stat:
        return None
    if data.get("error"):
        print(f"VR Sculpt: couldn't list the assets in {path}: {data['error']}", flush=True)
    data["thumbs"] = thumbs
    return data


# .blend path -> its listing: {"mtime", "size", "parts": [{"id_type", "name", "catalog_id", "cutter",
# "description", "preview": {"width", "height", "file"} or None}], "thumbs": folder}
_listings = {}
_failed = {}  # .blend path -> stat of a file the worker couldn't list (not tried again this session)
_worker = {"proc": None, "files": [], "started": 0.0, "rescan": False}
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit_worker.py")
WORKER_TIMEOUT = 600.0  # seconds


def _take(path, data):
    _listings[path] = data
    for key in [k for k, t in _thumbs.items() if t.asset.blend == path]:
        del _thumbs[key]  # Its previews may have changed with it.


def _outdated(paths):
    """The files whose listing isn't known yet (taking any from the cache on the way)."""
    todo = []
    for path in paths:
        stat = _stat(path)
        if stat is None:
            continue
        known = _listings.get(path)
        if known is not None and (known.get("mtime"), known.get("size")) == stat:
            continue
        data = _load_listing(path, stat)
        if data is not None:
            _take(path, data)
        elif _failed.get(path) != stat:
            todo.append(path)
    return todo


def _start_worker(paths):
    """List paths in a background Blender: reading big .blend files would stall the session."""
    binary = bpy.app.binary_path
    if not binary or not os.path.isfile(WORKER):
        for path in paths:
            _failed[path] = _stat(path)
        return
    os.makedirs(cache_dir(), exist_ok=True)
    job = os.path.join(cache_dir(), "job.json")
    with open(job, "w", encoding="utf-8") as f:
        json.dump({"files": [[p, *_cache_paths(p)] for p in paths]}, f)
    flags = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW: no console flashing up
    _worker["proc"] = subprocess.Popen(
        [binary, "--background", "--factory-startup", "--python", WORKER, "--", job],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    _worker["files"] = list(paths)
    _worker["started"] = time.monotonic()
    _worker["rescan"] = False


def pending():
    """True while the worker is listing files."""
    return _worker["proc"] is not None


def tick(context=None):
    """Take in what the worker has listed so far (live.py calls this a few times a second)."""
    proc = _worker["proc"]
    if proc is None:
        return
    finished = proc.poll() is not None
    if not finished and time.monotonic() - _worker["started"] > WORKER_TIMEOUT:
        proc.kill()
        finished = True
    changed = False
    for path in list(_worker["files"]):
        stat = _stat(path)
        data = _load_listing(path, stat) if stat is not None else None
        if data is not None:
            _take(path, data)
            _worker["files"].remove(path)
            changed = True
    if finished:
        for path in _worker["files"]:
            _failed[path] = _stat(path)
        rescan = _worker["rescan"]
        _worker.update(proc=None, files=[], rescan=False)
        changed = True
        if rescan:
            scan(context)
            return
    if changed:
        _rebuild()


_state = {"libraries": None, "folders": None, "message": ("", 0.0)}


def scan(context=None, wait=False):
    """[Library], each with its parts sorted by catalog and name, as far as they're listed. Files not
    listed yet (new or changed) go to the worker; their parts show up as it gets through them (see
    tick). wait: block until it's done (tests)."""
    folders = []
    for name, folder in libraries(context):
        folders.append((name, folder, _blend_files(folder) if os.path.isdir(folder) else []))
    _state["folders"] = folders
    todo = _outdated([path for _name, _folder, files in folders for path in files])
    if todo:
        if pending():
            _worker["rescan"] = True
        else:
            _start_worker(todo)
    if wait:
        while pending():
            _worker["proc"].wait(WORKER_TIMEOUT)
            tick(context)
    _rebuild()
    return _state["libraries"]


def _rebuild():
    out = []
    for name, folder, files in _state["folders"] or ():
        cats = _read_catalogs(folder)
        assets = []
        for path in files:
            listing = _listings.get(path)
            for part in listing["parts"] if listing else ():
                asset = Asset(name, path, part["id_type"], part["name"], cats.get(part["catalog_id"], ""),
                              part["cutter"], part["description"])
                prev = part.get("preview")
                if prev:
                    asset.preview = (prev["width"], prev["height"], os.path.join(listing["thumbs"], prev["file"]))
                assets.append(asset)
        assets.sort(key=lambda a: (a.catalog == "", a.catalog.lower(), a.name.lower()))
        out.append(Library(name, folder, assets))
    _state["libraries"] = out


def prewarm(context=None):
    """Start listing if nothing has been yet, so the Kit is ready by the time it's opened."""
    if _state["libraries"] is None:
        scan(context)


def cached(context=None):
    """The last scan (scanning now if there's none)."""
    return _state["libraries"] if _state["libraries"] is not None else scan(context)


def library(name, context=None):
    return next((lib for lib in cached(context) if lib.name == name), None)


def find(key):
    """The Asset for a part key, or None. Works before any scan (e.g. a key saved in the .blend)."""
    if not is_key(key):
        return None
    for lib in _state["libraries"] or ():
        for asset in lib.assets:
            if asset.key == key:
                return asset
    try:
        blend, id_type, name = parse_key(key)
    except ValueError:
        return None
    if id_type not in ID_DIRS or not os.path.isfile(blend):
        return None
    return Asset("", blend, id_type, name)


def siblings(key):
    """Keys of the parts in the same library and catalog as key's part (for cycling)."""
    asset = find(key)
    if asset is None:
        return []
    lib = library(asset.library)
    if lib is None:
        return [key]
    return [a.key for a in lib.assets if a.catalog == asset.catalog]


def message():
    text, when = _state["message"]
    return text if text and time.monotonic() - when < MESSAGE_SECONDS else ""


def say(text):
    _state["message"] = (text, time.monotonic())
    print("VR Sculpt kit:", text, flush=True)
    return text


# --- thumbnails ----------------------------------------------------------------

_thumbs = {}  # key -> Thumb
_clock = [0.0, 0]  # (when the current frame's budget started, textures made in it)


class Thumb:
    """An asset's preview, turned into a GPU texture on first draw. Hashable by key, so panels can
    compare widgets that hold one."""

    def __init__(self, asset):
        self.key = asset.key
        self.asset = asset
        self.tex = None
        self.missing = False  # the asset has no preview (or it couldn't be read)

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return isinstance(other, Thumb) and other.key == self.key

    def texture(self):
        """The GPU texture, or None (no preview, or this frame's budget is spent: try next frame)."""
        if self.tex is not None or self.missing or not _budget():
            return self.tex
        import gpu
        px = preview_pixels(self.asset)
        if px is None:
            self.missing = True
            return None
        w, h, data = px
        self.tex = gpu.types.GPUTexture((w, h), format='RGBA8', data=gpu.types.Buffer('FLOAT', data.size, data))
        return self.tex


def _budget():
    now = time.monotonic()
    if now - _clock[0] > 0.008:  # A new frame (both eyes draw within a few milliseconds).
        _clock[0], _clock[1] = now, 0
    if _clock[1] >= THUMBS_PER_FRAME:
        return False
    _clock[1] += 1
    return True


def thumb(asset):
    """The asset's Thumb, or None when it has no preview (the panel shows an icon)."""
    if asset.preview is None:
        return None
    t = _thumbs.get(asset.key)
    if t is None:
        t = _thumbs[asset.key] = Thumb(asset)
    return None if t.missing else t


def preview_pixels(asset):
    """(width, height, float32 RGBA, bottom row first) of the asset's preview, from the listing cache
    (the worker took it from the .blend); None when it has none."""
    if asset.preview is None:
        return None
    w, h, path = asset.preview
    try:
        raw = np.fromfile(path, np.uint8)
    except OSError:
        return None
    if raw.size != w * h * 4:
        return None
    return w, h, raw.astype(np.float32) / 255.0


def forget_thumbs():
    """Drop thumbnails (their textures go with them)."""
    _thumbs.clear()


def _write_listing(path, parts, pixels):
    """Put a file we wrote ourselves (Add to Kit) straight into the cache: parts as the worker lists them,
    pixels {part name: float RGBA (THUMB x THUMB)}."""
    js, thumbs = _cache_paths(path)
    os.makedirs(thumbs, exist_ok=True)
    for i, part in enumerate(parts):
        px = pixels.get(part["name"])
        if px is not None:
            name = f"O{i}.rgba"
            (np.clip(px, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).tofile(os.path.join(thumbs, name))
            part["preview"] = {"width": THUMB, "height": THUMB, "file": name}
    mtime, size = _stat(path)
    data = {"path": path, "mtime": mtime, "size": size, "parts": parts, "error": ""}
    with open(js, "w", encoding="utf-8") as f:
        json.dump(data, f)
    data["thumbs"] = thumbs
    _take(path, data)


# --- placing ---------------------------------------------------------------------

KEY_PROP = "vrs_kit_part"  # on an appended collection: the key of the part it came from


def append(asset):
    """The asset's object or collection, appended to this file (sharing data appended before), with
    its asset data cleared. Not linked to any scene. A collection appended before is reused as it is
    (Blender would append a new one), so every instance of a part shows the same collection."""
    if asset.id_type == 'COLLECTION':
        for coll in bpy.data.collections:
            if coll.get(KEY_PROP) == asset.key and coll.library is None:
                return coll
    attr = "objects" if asset.id_type == 'OBJECT' else "collections"
    with bpy.data.libraries.load(asset.blend, link=False, assets_only=True, reuse_local_id=True,
                                 clear_asset_data=True) as (src, dst):
        if asset.name not in getattr(src, attr):
            raise RuntimeError(f"'{asset.name}' is no longer in {os.path.basename(asset.blend)}")
        setattr(dst, attr, [asset.name])
    idb = getattr(dst, attr)[0]
    if idb is None:
        raise RuntimeError(f"Couldn't append '{asset.name}' from {os.path.basename(asset.blend)}")
    if asset.id_type == 'COLLECTION':
        idb[KEY_PROP] = asset.key
    return idb


def place(context, asset, size=1.0, select=True):
    """A new object for the asset in the active collection: the part itself, or an empty instancing a
    collection asset. At the origin, with the asset's own rotation and scale times size."""
    from . import primitives
    idb = append(asset)
    if asset.id_type == 'COLLECTION':
        obj = bpy.data.objects.new(asset.name, None)
        obj.instance_type = 'COLLECTION'
        obj.instance_collection = idb
        basis = Matrix.Identity(4)
    else:
        obj = idb
        basis = obj.matrix_basis.copy()
        obj.parent = None
    basis.translation = (0.0, 0.0, 0.0)
    context.collection.objects.link(obj)
    obj.matrix_world = basis @ Matrix.Scale(size, 4)
    if asset.id_type == 'COLLECTION':
        lo, hi = primitives.local_bounds(obj)
        obj.empty_display_size = max(max((hi - lo)[:]) * 0.1, 0.005)
    if select:
        primitives.make_active(context, obj)
    return obj


def label(key):
    asset = find(key)
    return asset.name if asset is not None else parse_key(key)[2] if is_key(key) else key


# --- add to kit ------------------------------------------------------------------

def _catalog_id(folder, path):
    """The uuid of the catalog at path in folder's catalog file, added (file and all) if missing."""
    for cat_id, cat_path in _read_catalogs(folder).items():
        if cat_path == path:
            return cat_id
    cat_id = str(uuid.uuid4())
    file = os.path.join(folder, CATS_FILE)
    new = not os.path.isfile(file)
    with open(file, "a", encoding="utf-8") as f:
        if new:
            f.write(CATS_HEADER)
        f.write(f"{cat_id}:{path}:{path.replace('/', '-')}\n")
    return cat_id


def _free_path(folder, name):
    base = bpy.path.clean_name(name) or "Part"
    path = os.path.join(folder, base + ".blend")
    i = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}_{i}.blend")
        i += 1
    return path


def add_to_kit(context, obj):
    """Save obj, as it looks, as a part in My Kit. Returns the new Asset; raises ValueError for an object
    that can't be a part."""
    from . import hardsurface
    if obj is None or obj.type != 'MESH':
        raise ValueError("Select a mesh to add it to the kit")
    depsgraph = context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), preserve_all_data_layers=True,
                                           depsgraph=depsgraph)
    # The part keeps its own orientation (its local Z sits on surfaces) but the size you see.
    mesh.transform(Matrix.Diagonal((*obj.matrix_world.to_scale(), 1.0)))
    if len(mesh.vertices) == 0:
        bpy.data.meshes.remove(mesh)
        raise ValueError(f"{obj.name} has no geometry to save")
    cutter = hardsurface.is_cutter(obj)
    folder = user_dir()
    os.makedirs(folder, exist_ok=True)
    # The part is saved under obj's name: obj steps aside for the moment (two objects can't share one).
    name = obj.name
    obj.name = name + " (saving)"
    part = bpy.data.objects.new(name, mesh)
    try:
        part.asset_mark()
        part.asset_data.catalog_id = _catalog_id(folder, "Cutters" if cutter else "Parts")
        part.asset_data.description = "Added in VR"
        if cutter:
            part.asset_data.tags.new(CUTTER_TAG)
        pixels = render_thumbnail([(mesh, Matrix.Identity(4))], cutter=cutter)
        part_entry = {"id_type": 'OBJECT', "name": name, "catalog_id": part.asset_data.catalog_id,
                      "cutter": cutter, "description": part.asset_data.description, "preview": None}
        if pixels is not None:
            prev = part.preview_ensure()
            prev.image_size = (THUMB, THUMB)
            prev.image_pixels_float.foreach_set(pixels)
        path = _free_path(folder, name)
        bpy.data.libraries.write(path, {part}, fake_user=True, compress=True)
        _write_listing(path, [part_entry], {name: pixels} if pixels is not None else {})
    finally:
        bpy.data.objects.remove(part)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        obj.name = name
    scan(context)
    asset = find(Asset(MINE, path, 'OBJECT', name).key)
    say(f"Added {name} to {MINE}")
    return asset


# --- thumbnails rendered here --------------------------------------------------------

LIGHT = Vector((0.45, -0.6, 0.66)).normalized()
VIEW_FROM = Vector((0.9, -1.25, 0.85)).normalized()  # three-quarter view from the front right, above
BASE = (0.74, 0.77, 0.83)
CUTTER_BASE = (0.95, 0.52, 0.47)  # cutters' thumbnails are red, as cuts are in the panel


def _triangles(parts, base):
    """(positions, colors) of every triangle of parts [(mesh, matrix)], lit from LIGHT."""
    base = np.array(base, np.float32)
    pos, col = [], []
    for mesh, matrix in parts:
        mesh.calc_loop_triangles()
        n = len(mesh.loop_triangles)
        if n == 0:
            continue
        co = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", co)
        tri = np.empty(n * 3, np.int32)
        mesh.loop_triangles.foreach_get("vertices", tri)
        nor = np.empty(n * 3, np.float32)
        mesh.loop_triangles.foreach_get("normal", nor)
        m = np.array(matrix, np.float32)
        co = co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
        nor = nor.reshape(-1, 3) @ np.linalg.inv(m[:3, :3])
        nor /= np.maximum(np.linalg.norm(nor, axis=1, keepdims=True), 1e-9)
        light = np.clip(nor @ np.array(LIGHT, np.float32), 0.0, 1.0)
        shade = (0.32 + 0.68 * light)[:, None] * base
        rgba = np.ones((n, 4), np.float32)
        rgba[:, :3] = np.clip(shade, 0.0, 1.0)
        pos.append(co[tri])
        col.append(np.repeat(rgba, 3, axis=0))
    if not pos:
        return None, None
    return np.concatenate(pos), np.concatenate(col)


def render_thumbnail(parts, size=THUMB, cutter=False):
    """RGBA float32 pixels (size x size, bottom row first) of parts [(mesh, matrix)], lit, from a
    three-quarter view, on a transparent background. None without a GPU or geometry."""
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except ImportError:
        return None
    pos, col = _triangles(parts, CUTTER_BASE if cutter else BASE)
    if pos is None:
        return None
    lo, hi = pos.min(axis=0), pos.max(axis=0)
    center = (lo + hi) / 2
    forward = -np.array(VIEW_FROM, np.float32)
    right = np.cross(forward, (0.0, 0.0, 1.0))
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    d = pos - center
    view = np.stack((d @ right, d @ up, d @ forward), axis=1)
    reach = max(np.abs(view[:, :2]).max(), 1e-6) * 1.12
    depth = max(np.abs(view[:, 2]).max(), 1e-6) * 1.01
    ndc = np.stack((view[:, 0] / reach, view[:, 1] / reach, view[:, 2] / depth), axis=1)
    big = size * 2  # rendered at twice the size and averaged down, for smooth edges
    try:
        offscreen = gpu.types.GPUOffScreen(big, big)
    except Exception:  # No GPU (background without gpu.init()).
        return None
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    batch = batch_for_shader(shader, 'TRIS', {"pos": ndc.tolist(), "color": col.tolist()})
    try:
        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 0.0), depth=1.0)
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.depth_mask_set(True)
            with gpu.matrix.push_pop():
                gpu.matrix.load_identity()
                with gpu.matrix.push_pop_projection():
                    gpu.matrix.load_projection_matrix(Matrix.Identity(4))
                    batch.draw(shader)
            buf = fb.read_color(0, 0, big, big, 4, 0, 'FLOAT')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
    finally:
        offscreen.free()
    buf.dimensions = big * big * 4
    px = np.array(buf.to_list(), np.float32).reshape(size, 2, size, 2, 4)
    # Average premultiplied, so edges don't darken against the transparent background.
    alpha = px[..., 3].mean(axis=(1, 3))
    rgb = (px[..., :3] * px[..., 3:4]).mean(axis=(1, 3)) / np.maximum(alpha, 1e-6)[..., None]
    out = np.concatenate((rgb, alpha[..., None]), axis=2)
    return np.ascontiguousarray(out, np.float32).ravel()


def register():
    pass


def unregister():
    proc = _worker["proc"]
    if proc is not None and proc.poll() is None:
        proc.kill()
    _worker.update(proc=None, files=[], rescan=False)
    _thumbs.clear()
    _state["libraries"] = None
