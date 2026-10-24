"""Probe: can an asset's preview (and catalog) be read from its .blend without appending it?

Makes a small asset library in a temp folder (objects and a collection marked as assets, in
catalogs, with custom previews of known colors), then from an empty file tries each way in:
  - bpy.data.libraries.load(assets_only=True): names only?
  - bpy.utils.previews ... load("lib.blend/Object/Name", 'BLEND'): the ID's own preview?
  - linking it (link=True), reading asset_data and the preview, then removing the library;
  - appending it.

blender --background --factory-startup --python tests/probe_asset_previews.py
Writes tests/probe_asset_previews_result.txt.
"""

import os
import shutil
import tempfile
import time
import traceback

import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "probe_asset_previews_result.txt")
_lines = []

CATALOGS = {
    "Bolt": ("8d0e1d2a-0000-4000-8000-000000000001", "Kit/Bolts"),
    "Vent": ("8d0e1d2a-0000-4000-8000-000000000002", "Kit/Cutters"),
    "PipeKit": ("8d0e1d2a-0000-4000-8000-000000000003", "Kit/Pipes"),
}
COLORS = {"Bolt": (1.0, 0.0, 0.0, 1.0), "Vent": (0.0, 1.0, 0.0, 1.0), "PipeKit": (0.0, 0.0, 1.0, 1.0)}
SIZE = 32


def log(*parts):
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    _lines.append(line)


def set_preview(idb, rgba):
    prev = idb.preview_ensure()
    prev.image_size = (SIZE, SIZE)
    prev.image_pixels_float = list(rgba) * (SIZE * SIZE)


def make_library(folder):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bolt = bpy.data.objects.new("Bolt", bpy.data.meshes.new("Bolt"))
    vent = bpy.data.objects.new("Vent", bpy.data.meshes.new("Vent"))
    vent["vrs_cutter"] = True
    coll = bpy.data.collections.new("PipeKit")
    for name in ("PipeA", "PipeB"):
        coll.objects.link(bpy.data.objects.new(name, bpy.data.meshes.new(name)))
    scene.collection.objects.link(bolt)
    scene.collection.objects.link(vent)
    scene.collection.children.link(coll)
    for idb in (bolt, vent, coll):
        idb.asset_mark()
        idb.asset_data.catalog_id = CATALOGS[idb.name][0]
        idb.asset_data.tags.new("vrs-test")
        set_preview(idb, COLORS[idb.name])
    vent.asset_data.tags.new("cutter")
    with open(os.path.join(folder, "blender_assets.cats.txt"), "w") as f:
        f.write("VERSION 1\n\n")
        for name, (uid, path) in CATALOGS.items():
            f.write(f"{uid}:{path}:{path.replace('/', '-')}\n")
    path = os.path.join(folder, "kit.blend")
    bpy.ops.wm.save_as_mainfile(filepath=path)
    log("library saved:", path, os.path.getsize(path), "bytes")
    return path


def describe_pixels(prev, kind="image"):
    size = tuple(getattr(prev, kind + "_size"))
    if size[0] * size[1] == 0:
        return f"{kind} size {size} (empty)"
    px = list(getattr(prev, kind + "_pixels_float"))
    mid = (size[1] // 2 * size[0] + size[0] // 2) * 4
    return f"{kind} size {size}, center {tuple(round(v, 2) for v in px[mid:mid + 4])}"


def try_names(path):
    t0 = time.perf_counter()
    with bpy.data.libraries.load(path, assets_only=True) as (src, _dst):
        names = {"objects": list(src.objects), "collections": list(src.collections), "meshes": list(src.meshes)}
    log(f"[names] {(time.perf_counter() - t0) * 1000:.1f} ms:", names)
    log("[names] libraries after:", [lib.filepath for lib in bpy.data.libraries])


def try_preview_collection(path):
    import bpy.utils.previews
    pcoll = bpy.utils.previews.new()
    try:
        for idtype, name in (("Object", "Bolt"), ("Object", "Vent"), ("Collection", "PipeKit")):
            for sep in ("/", "\\"):
                key = f"{idtype}{sep}{name}"
                fp = path + sep + idtype + sep + name
                t0 = time.perf_counter()
                try:
                    prev = pcoll.load(key, fp, 'BLEND')
                    ms = (time.perf_counter() - t0) * 1000
                    log(f"[previews.load {sep!r}] {idtype}/{name}: {ms:.1f} ms, icon_id {prev.icon_id},",
                        describe_pixels(prev), "|", describe_pixels(prev, "icon"))
                except Exception as ex:
                    log(f"[previews.load {sep!r}] {idtype}/{name}: ERROR {ex}")
        prev = pcoll.load("file", path, 'BLEND')
        log("[previews.load] whole file:", describe_pixels(prev))
    finally:
        bpy.utils.previews.remove(pcoll)
    log("[previews.load] objects after:", [o.name for o in bpy.data.objects],
        "libraries:", [lib.filepath for lib in bpy.data.libraries])


def try_link(path):
    t0 = time.perf_counter()
    with bpy.data.libraries.load(path, link=True, assets_only=True) as (src, dst):
        dst.objects = list(src.objects)
        dst.collections = list(src.collections)
    ms = (time.perf_counter() - t0) * 1000
    log(f"[link] {ms:.1f} ms")
    for idb in list(dst.objects) + list(dst.collections):
        ad = idb.asset_data
        log(f"[link] {idb.name}: catalog {ad.catalog_id if ad else None} ({ad.catalog_simple_name if ad else ''}),",
            f"tags {[t.name for t in ad.tags] if ad else None},",
            describe_pixels(idb.preview) if idb.preview else "no preview",
            "| cutter prop:", idb.get("vrs_cutter"))
    t0 = time.perf_counter()
    for lib in list(bpy.data.libraries):
        bpy.data.libraries.remove(lib)
    log(f"[link] removed libraries in {(time.perf_counter() - t0) * 1000:.1f} ms;",
        "objects left:", [o.name for o in bpy.data.objects], "meshes left:", [m.name for m in bpy.data.meshes],
        "collections left:", [c.name for c in bpy.data.collections])


def try_append(path):
    t0 = time.perf_counter()
    with bpy.data.libraries.load(path, link=False, assets_only=True) as (src, dst):
        dst.objects = ["Bolt"]
        dst.collections = ["PipeKit"]
    ms = (time.perf_counter() - t0) * 1000
    log(f"[append] {ms:.1f} ms:", [o.name for o in dst.objects], [c.name for c in dst.collections])
    for idb in list(dst.objects) + list(dst.collections):
        log(f"[append] {idb.name}: asset_data {idb.asset_data is not None}, users {idb.users},",
            describe_pixels(idb.preview) if idb.preview else "no preview")
    log("[append] libraries after:", [lib.filepath for lib in bpy.data.libraries],
        "objects:", [o.name for o in bpy.data.objects])
    # Append again: a second copy, or the same one (reuse_local_id)?
    with bpy.data.libraries.load(path, link=False, assets_only=True, reuse_local_id=True) as (src, dst):
        dst.objects = ["Bolt"]
    log("[append again, reuse_local_id]", [o.name for o in dst.objects], "objects:", [o.name for o in bpy.data.objects])


def try_catalog_file(folder):
    with open(os.path.join(folder, "blender_assets.cats.txt")) as f:
        log("[cats file]", f.read().strip().replace("\n", " | "))


def main():
    folder = tempfile.mkdtemp(prefix="vrs_assets_")
    try:
        log("blender", bpy.app.version_string)
        path = make_library(folder)
        bpy.ops.wm.read_factory_settings(use_empty=True)
        for step in (try_names, try_preview_collection, try_link, try_append):
            try:
                step(path)
            except Exception:
                log(f"[{step.__name__}] FAILED\n" + traceback.format_exc())
        try_catalog_file(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        with open(OUT, "w") as f:
            f.write("\n".join(_lines) + "\n")


main()
