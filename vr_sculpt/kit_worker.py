"""Lists the assets of .blend files for kit.py, in a background Blender of its own.

Run by kit.py, never imported by the add-on (so it isn't in __init__'s module lists):

    blender --background --factory-startup --python kit_worker.py -- <job.json>

The job is {"files": [[blend path, cache json path, thumbnail folder], ...]}. Reading big .blend
files takes seconds, so it happens here instead of in the session. For each file, its mesh object
and collection assets are linked, and their catalog, tags and preview (scaled down to at most
THUMB pixels, as raw RGBA bytes) are written to the cache; the json is written last, atomically, so
kit.py picks each file up as soon as it's done.
"""

import json
import os
import sys

import bpy
import numpy as np

THUMB = 128
CUTTER_TAG = "cutter"


def preview_bytes(idb):
    """(width, height, RGBA8 bytes, bottom row first) of idb's preview, at most THUMB wide; None without one."""
    prev = idb.preview
    if prev is None:
        return None
    w, h = prev.image_size
    if w * h == 0:
        return None
    px = np.empty(w * h * 4, np.float32)
    prev.image_pixels_float.foreach_get(px)
    px = px.reshape(h, w, 4)
    while w > THUMB and h > THUMB and w % 2 == 0 and h % 2 == 0:
        # Averaged with alpha, so edges don't darken against the transparent background.
        q = px.reshape(h // 2, 2, w // 2, 2, 4)
        alpha = q[..., 3].mean(axis=(1, 3))
        rgb = (q[..., :3] * q[..., 3:4]).mean(axis=(1, 3)) / np.maximum(alpha, 1e-6)[..., None]
        px = np.concatenate((rgb, alpha[..., None]), axis=2)
        h, w = h // 2, w // 2
    return w, h, (np.clip(px, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8).tobytes()


def list_file(path, thumbs):
    parts = []
    with bpy.data.libraries.load(path, link=True, assets_only=True) as (src, dst):
        dst.objects = list(src.objects)
        dst.collections = list(src.collections)
    for id_type, ids in (('OBJECT', dst.objects), ('COLLECTION', dst.collections)):
        for i, idb in enumerate(ids):
            if idb is None or idb.asset_data is None:
                continue
            if id_type == 'OBJECT' and idb.type != 'MESH':
                continue  # Placing, snapping and cutting all work on meshes.
            data = idb.asset_data
            tags = {t.name.lower() for t in data.tags}
            part = {"id_type": id_type, "name": idb.name, "catalog_id": data.catalog_id,
                    "cutter": CUTTER_TAG in tags or bool(idb.get("vrs_cutter", False)),
                    "description": data.description, "preview": None}
            prev = preview_bytes(idb)
            if prev is not None:
                w, h, raw = prev
                os.makedirs(thumbs, exist_ok=True)
                name = f"{id_type[0]}{i}.rgba"
                with open(os.path.join(thumbs, name), "wb") as f:
                    f.write(raw)
                part["preview"] = {"width": w, "height": h, "file": name}
            parts.append(part)
    return parts


def main():
    job_path = sys.argv[sys.argv.index("--") + 1]
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    for path, out, thumbs in job["files"]:
        result = {"path": path, "parts": [], "error": ""}
        try:
            stat = os.stat(path)
            result["mtime"], result["size"] = stat.st_mtime, stat.st_size
            result["parts"] = list_file(path, thumbs)
        except Exception as ex:  # One unreadable file must not stop the rest.
            result["error"] = str(ex)
        for lib in list(bpy.data.libraries):  # Keep memory flat across many files.
            bpy.data.libraries.remove(lib)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f)
        os.replace(tmp, out)


main()
