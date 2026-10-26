"""Gets the controller models from the OpenXR runtime, for controllers.py, in a background Blender of its own.

Run by controllers.py, never imported by the add-on (so it isn't in __init__'s module lists):

    blender --background --factory-startup --python controller_worker.py -- <out folder>

Blender keeps its OpenXR instance to itself and asks for XR_MSFT_controller_model, which SteamVR doesn't
have; SteamVR has the Khronos pair instead (XR_EXT_render_model + XR_EXT_interaction_render_model). So
this talks to the runtime DLL over ctypes (the loader/runtime negotiation, as the OpenXR loader does) and
makes an instance of its own with a headless session (XR_MND_headless). It can't run beside Blender's VR
session: SteamVR allows one instance per process, and a second process becomes the scene app and makes
Blender's session quit. So it runs before one, with the headset awake and the controllers in hand
(without them, SteamVR serves its hand tracking placeholder).

For each hand it writes <out>/<hand>.npz in the format of assets/controllers (see
tools/convert_controllers.py): the mesh in the aim pose's frame (measured with xrLocateSpace, so
offset is zero), split into parts by glTF node, the texture, and the press motions. Every button
keeps its own part, with its key letter, and a role: which of the add-on's buttons (ROLES) moves and
lights it. The face buttons' roles follow the Touch layout the stock bindings assume (Y / B on top,
X / A below); a controller's extra buttons get none and stay still (see docs/controller_bindings.md
for finding the real binding). The runtime's glTF has no animation, so the motions are guessed from
the shapes: buttons sink along their face, the stick tilts about its base, the trigger swings back
about the lateral axis, the grip squeezes in.
<out>/status.json is written last, atomically: {"ok", "message", "runtime", "hands": {hand: npz}}.
"""

import ctypes as C
import json
import math
import os
import re
import struct
import sys
import time

import numpy as np

TEXTURE_SIZE = 1024
ROLES = ('body', 'top', 'bottom', 'system', 'trigger', 'grip', 'stick')  # the parts controllers.py moves
TIMEOUT = 12.0  # seconds to wait for both controllers' models
START_TIMEOUT = 30.0  # seconds to wait for the runtime to start

BUTTON_TRAVEL = 0.0015  # meters a face button sinks
STICK_TILT = math.radians(20.0)
STICK_SINK = 0.0012
TRIGGER_SWING = math.radians(-15.0)  # about +X: the bottom of the trigger comes back (+Z)
GRIP_TRAVEL = 0.002


# --- the parts ---------------------------------------------------------------------------

LETTERS = ("a", "b", "x", "y")
# Which face button plays our top / bottom one, by preference: Touch's layout (what Blender's stock
# bindings assume), then the other hand's letters for a controller that has those instead.
FACE = {'left': (("y", "b"), ("x", "a")), 'right': (("b", "y"), ("a", "x"))}


def kind_of(node_name):
    """(kind, letter) of a glTF node from its name (SteamVR: body, button_x / y / a / b, button_enter,
    button_grip, thumbstick, trigger): kind is 'button' (with its letter), 'trigger', 'grip', 'stick',
    'system' or 'body' (anything else: drawn, never moved)."""
    n = node_name.lower()
    words = set(re.split(r"[^a-z0-9]+", n))
    if "trigger" in n:
        return 'trigger', ""
    if "grip" in n or "squeeze" in n:
        return 'grip', ""
    if "thumbstick" in n or "joystick" in n:
        return 'stick', ""
    letters = words & set(LETTERS)
    if letters:
        return 'button', min(letters)
    if words & {"enter", "menu", "system", "home", "oculus"}:
        return 'system', ""
    return 'body', ""


def is_controller(node_names):
    """A controller has moving parts; SteamVR's hand tracking placeholder is a body alone."""
    return any(kind_of(n)[0] != 'body' for n in node_names)


def assign(node_names, hand):
    """{node: (part name, role, label)}. Every node that isn't body is a part of its own; role is one of
    ROLES or "" (a button no binding of ours reaches), label the key letter for the button hints."""
    kinds = {n: kind_of(n) for n in node_names}
    present = [letter for kind, letter in kinds.values() if kind == 'button']
    roles = {}
    for role, prefer in zip(('top', 'bottom'), FACE[hand]):
        letter = next((p for p in prefer if p in present and p not in roles), None)
        if letter is not None:
            roles[letter] = role
    out = {}
    for n, (kind, letter) in kinds.items():
        if kind == 'body':
            out[n] = ('body', 'body', "")
        elif kind == 'button':
            out[n] = (n, roles.get(letter, ""), letter.upper())
        else:
            out[n] = (n, kind, "")
    return out


# --- glTF ----------------------------------------------------------------------------------

_COMPONENTS = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(data):
    """(json, binary chunk) of a .glb."""
    magic, _version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("not a .glb")
    offset, gltf, binary = 12, None, b""
    while offset < len(data):
        length, kind = struct.unpack_from("<I4s", data, offset)
        chunk = data[offset + 8:offset + 8 + length]
        if kind == b"JSON":
            gltf = json.loads(chunk)
        elif kind == b"BIN\0":
            binary = chunk
        offset += 8 + length
    return gltf, binary


def _accessor(gltf, binary, index):
    acc = gltf["accessors"][index]
    dtype = np.dtype(_COMPONENTS[acc["componentType"]])
    n = _COUNTS[acc["type"]]
    count = acc["count"]
    if "bufferView" not in acc:
        return np.zeros((count, n), dtype)
    view = gltf["bufferViews"][acc["bufferView"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or dtype.itemsize * n
    out = np.ndarray((count, n), dtype, buffer=binary, offset=start, strides=(stride, dtype.itemsize)).copy()
    if acc.get("normalized") and dtype.kind in "iu":
        return np.maximum(out / np.iinfo(dtype).max, -1.0)
    return out


def _node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], np.float64).reshape(4, 4).T  # glTF stores columns
    m = np.identity(4)
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    m[:3, :3] = _quat_matrix((w, x, y, z))
    m[:3, :3] *= np.array(node.get("scale", (1.0, 1.0, 1.0)))
    m[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
    return m


def _quat_matrix(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def gltf_meshes(gltf, binary):
    """[(node name, world matrix, primitive dict)] for every triangle primitive in the default scene."""
    scenes = gltf.get("scenes") or [{"nodes": list(range(len(gltf.get("nodes", []))))}]
    roots = scenes[gltf.get("scene", 0)]["nodes"]
    out = []

    def walk(index, parent, name):
        node = gltf["nodes"][index]
        world = parent @ _node_matrix(node)
        name = node.get("name") or name
        if "mesh" in node:
            mesh = gltf["meshes"][node["mesh"]]
            for prim in mesh["primitives"]:
                if prim.get("mode", 4) == 4:
                    out.append((name or mesh.get("name", ""), world, prim))
        for child in node.get("children", ()):
            walk(child, world, name)

    for root in roots:
        walk(root, np.identity(4), "")
    return out


def _vertex_normals(pos, tris):
    nor = np.zeros_like(pos)
    face = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
    for k in range(3):
        np.add.at(nor, tris[:, k], face)
    return nor / np.maximum(np.linalg.norm(nor, axis=1, keepdims=True), 1e-12)


# --- motions ---------------------------------------------------------------------------------

def _face_normal(pos, tris):
    """The area-weighted mean outward normal of a part and how much it agrees (0..1)."""
    face = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
    total = np.linalg.norm(face, axis=1).sum()
    mean = face.sum(axis=0)
    length = np.linalg.norm(mean)
    if total < 1e-12 or length < 1e-12:
        return None, 0.0
    return mean / length, length / total


def guess_moves(pos, role, tris):
    """{move: (pivot, axis, angle, move)} for the parts present, in the aim frame (+X right, +Y up, -Z
    forward), by role (role: each vertex's index in ROLES, or -1 for none). Same keys as the bundled
    models (top, bottom, trigger, grip, stick_forward, stick_right) plus stick_click."""
    tri_role = role[tris[:, 0]]
    zero = np.zeros(3)
    moves = {}
    center = {}
    for i, name in enumerate(ROLES):
        sel = role == i
        if sel.any():
            center[name] = pos[sel].mean(axis=0)
    body = center.get('body', pos.mean(axis=0))

    def normal(name):
        t = tris[tri_role == ROLES.index(name)]
        return _face_normal(pos, t) if len(t) else (None, 0.0)

    faces = []
    for name in ('top', 'bottom'):
        if name not in center:
            continue
        n, agree = normal(name)
        if n is None or agree < 0.3:
            n = center[name] - body
            n /= max(np.linalg.norm(n), 1e-12)
        faces.append(n)
        moves[name] = (center[name], np.array((1.0, 0.0, 0.0)), 0.0, -n * BUTTON_TRAVEL)
    if 'stick' in center:
        # The face plate's normal (from the buttons beside it) is the stick's axis; its own shape is a
        # less sure guess (a shaft and a cap).
        if faces:
            up = np.mean(faces, axis=0)
        else:
            up, _agree = normal('stick')
            up = up if up is not None else np.array((0.0, 1.0, 0.0))
        up /= max(np.linalg.norm(up), 1e-12)
        sel = pos[role == ROLES.index('stick')]
        heights = sel @ up
        pivot = center['stick'] + up * (heights.min() - center['stick'] @ up)
        for key, toward in (('stick_forward', np.array((0.0, 0.0, -1.0))), ('stick_right', np.array((1.0, 0.0, 0.0)))):
            flat = toward - up * (toward @ up)
            flat /= max(np.linalg.norm(flat), 1e-12)
            axis = np.cross(up, flat)  # turning about up x flat takes the cap toward flat
            moves[key] = (pivot, axis / max(np.linalg.norm(axis), 1e-12), STICK_TILT, zero)
        moves['stick_click'] = (pivot, up, 0.0, -up * STICK_SINK)
    if 'trigger' in center:
        sel = pos[role == ROLES.index('trigger')]
        y = sel[:, 1]
        top = sel[y >= y.max() - 0.15 * max(np.ptp(y), 1e-6)]  # the hinge: along its top edge
        pivot = np.array((center['trigger'][0], top[:, 1].mean(), top[:, 2].mean()))
        moves['trigger'] = (pivot, np.array((1.0, 0.0, 0.0)), TRIGGER_SWING, zero)
    if 'grip' in center:
        inward = body[0] - center['grip'][0]
        moves['grip'] = (center['grip'], np.array((0.0, 0.0, 1.0)), 0.0,
                         np.array((math.copysign(GRIP_TRAVEL, inward), 0.0, 0.0)))
    return moves


# --- textures ----------------------------------------------------------------------------------

def _decode_png(data, folder, size):
    """RGB float pixels (size x size, bottom row first) of an image's bytes, read with Blender."""
    import bpy
    path = os.path.join(folder, "_texture.tmp")
    with open(path, "wb") as f:
        f.write(data)
    img = bpy.data.images.load(path)
    try:
        img.colorspace_settings.name = 'Non-Color'  # Keep the stored values: the add-on draws them as they are.
        img.scale(size, size)
        px = np.empty(size * size * 4, np.float32)
        img.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(img)
        os.remove(path)
    return px.reshape(size, size, 4)[..., :3]


def _material_source(gltf, prim):
    """(image index or None, base color factor) of a primitive's material."""
    mat = gltf.get("materials", [])[prim["material"]] if "material" in prim else {}
    pbr = mat.get("pbrMetallicRoughness", {})
    factor = tuple(pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))[:3])
    tex = pbr.get("baseColorTexture")
    image = gltf["textures"][tex["index"]].get("source") if tex else None
    return image, factor


def _image_bytes(gltf, binary, index):
    img = gltf["images"][index]
    view = gltf["bufferViews"][img["bufferView"]]
    start = view.get("byteOffset", 0)
    return bytes(binary[start:start + view["byteLength"]])


def build_texture(sources, folder, decode=_decode_png):
    """One texture for all the parts, and per source where its UVs land: {source: (u0, v0, scale)}.
    Sources are (image bytes or None, color factor); parts sharing an image share one cell (SteamVR's
    parts all use the same image)."""
    keys = sorted(set(sources), key=str)
    cells = max(1, math.ceil(math.sqrt(len(keys))))
    cell = TEXTURE_SIZE // cells
    tex = np.zeros((cells * cell, cells * cell, 3), np.float32)
    where = {}
    decoded = {}
    for i, key in enumerate(keys):
        data, factor = key
        cx, cy = i % cells, i // cells
        if data is None:
            px = np.ones((cell, cell, 3), np.float32)
        else:
            if data not in decoded:
                decoded[data] = decode(data, folder, cell)
            px = decoded[data]
        tex[cy * cell:(cy + 1) * cell, cx * cell:(cx + 1) * cell] = px * np.array(factor, np.float32)
        where[key] = (cx / cells, cy / cells, 1.0 / cells)
    return np.clip(np.round(tex * 255.0), 0, 255).astype(np.uint8), where


# --- the model --------------------------------------------------------------------------------

def convert(glb, model_in_aim, folder, hand, decode=_decode_png):
    """The .npz arrays of one controller: glb bytes, the 4x4 matrix taking the model's space to the
    hand's aim pose space, and the hand ('left' / 'right'). Beside the bundled models' arrays: roles and
    labels, one per part."""
    gltf, binary = read_glb(glb)
    meshes = gltf_meshes(gltf, binary)
    assigned = assign({name for name, _w, _p in meshes}, hand)
    parts = ['body'] + sorted({p for p, _r, _l in assigned.values()} - {'body'})
    roles = ['body'] + [next(r for p, r, _l in assigned.values() if p == part) for part in parts[1:]]
    labels = [""] + [next(lab for p, _r, lab in assigned.values() if p == part) for part in parts[1:]]
    pieces = []
    for name, world, prim in meshes:
        attrs = prim["attributes"]
        pos = _accessor(gltf, binary, attrs["POSITION"]).astype(np.float64)
        n = len(pos)
        tris = (_accessor(gltf, binary, prim["indices"]).reshape(-1, 3).astype(np.int64) if "indices" in prim
                else np.arange(n - n % 3).reshape(-1, 3))
        nor = (_accessor(gltf, binary, attrs["NORMAL"]).astype(np.float64) if "NORMAL" in attrs else None)
        uv = (_accessor(gltf, binary, attrs["TEXCOORD_0"]).astype(np.float64) if "TEXCOORD_0" in attrs
              else np.zeros((n, 2)))
        m = model_in_aim @ world
        pos = pos @ m[:3, :3].T + m[:3, 3]
        if nor is None:
            nor = _vertex_normals(pos, tris)
        else:
            nor = nor @ np.linalg.inv(m[:3, :3])  # the inverse transpose, row vectors
            nor /= np.maximum(np.linalg.norm(nor, axis=1, keepdims=True), 1e-12)
        image, factor = _material_source(gltf, prim)
        source = (_image_bytes(gltf, binary, image) if image is not None else None, factor)
        pieces.append((parts.index(assigned[name][0]), pos, nor, uv, tris, source))
    if not pieces:
        raise ValueError("the model has no meshes")
    tex, where = build_texture([p[5] for p in pieces], folder, decode)
    all_pos, all_nor, all_uv, all_part, all_tris = [], [], [], [], []
    base = 0
    for part, pos, nor, uv, tris, source in pieces:
        u0, v0, scale = where[source]
        # glTF's V runs down the image; the add-on's (Blender's) runs up, bottom row first.
        cell_uv = np.clip(np.stack((uv[:, 0], 1.0 - uv[:, 1]), axis=1), 0.0, 1.0)
        all_uv.append(cell_uv * scale + (u0, v0))
        all_pos.append(pos)
        all_nor.append(nor)
        all_part.append(np.full(len(pos), part, np.uint8))
        all_tris.append(tris + base)
        base += len(pos)
    pos = np.concatenate(all_pos).astype(np.float32)
    part = np.concatenate(all_part)
    tris = np.concatenate(all_tris).astype(np.uint32)
    role_index = np.array([ROLES.index(r) if r else -1 for r in roles])
    moves = guess_moves(pos.astype(np.float64), role_index[part], tris)
    names = sorted(moves)
    return dict(
        pos=pos, nor=np.concatenate(all_nor).astype(np.float32), uv=np.concatenate(all_uv).astype(np.float32),
        part=part, tris=tris, parts=np.array(parts), roles=np.array(roles), labels=np.array(labels),
        moves=np.array(names),
        pivot=np.array([moves[k][0] for k in names], np.float32).reshape(-1, 3),
        axis=np.array([moves[k][1] for k in names], np.float32).reshape(-1, 3),
        angle=np.array([moves[k][2] for k in names], np.float32),
        move=np.array([moves[k][3] for k in names], np.float32).reshape(-1, 3),
        tex=tex, offset=np.zeros(3, np.float32))


# --- OpenXR over ctypes (openxr.h) ----------------------------------------------------------

H = C.c_uint64  # handles, atoms, paths and XrTime are all 64-bit
P = C.POINTER


def _version(major, minor, patch):
    return (major << 48) | (minor << 32) | patch


def _struct(name, fields):
    return type(name, (C.Structure,), {"_fields_": fields})


TYPED = [("type", C.c_int), ("next", C.c_void_p)]
NegotiateLoaderInfo = _struct("NegotiateLoaderInfo", [
    ("structType", C.c_int), ("structVersion", C.c_uint32), ("structSize", C.c_size_t),
    ("minInterfaceVersion", C.c_uint32), ("maxInterfaceVersion", C.c_uint32), ("minApiVersion", C.c_uint64),
    ("maxApiVersion", C.c_uint64)])
NegotiateRuntimeRequest = _struct("NegotiateRuntimeRequest", [
    ("structType", C.c_int), ("structVersion", C.c_uint32), ("structSize", C.c_size_t),
    ("runtimeInterfaceVersion", C.c_uint32), ("runtimeApiVersion", C.c_uint64), ("getInstanceProcAddr", C.c_void_p)])
ExtensionProperties = _struct("ExtensionProperties", TYPED + [("extensionName", C.c_char * 128),
                                                              ("extensionVersion", C.c_uint32)])
ApplicationInfo = _struct("ApplicationInfo", [
    ("applicationName", C.c_char * 128), ("applicationVersion", C.c_uint32), ("engineName", C.c_char * 128),
    ("engineVersion", C.c_uint32), ("apiVersion", C.c_uint64)])
InstanceCreateInfo = _struct("InstanceCreateInfo", TYPED + [
    ("createFlags", C.c_uint64), ("applicationInfo", ApplicationInfo), ("enabledApiLayerCount", C.c_uint32),
    ("enabledApiLayerNames", P(C.c_char_p)), ("enabledExtensionCount", C.c_uint32),
    ("enabledExtensionNames", P(C.c_char_p))])
SystemGetInfo = _struct("SystemGetInfo", TYPED + [("formFactor", C.c_int)])
SessionCreateInfo = _struct("SessionCreateInfo", TYPED + [("createFlags", C.c_uint64), ("systemId", H)])
SessionBeginInfo = _struct("SessionBeginInfo", TYPED + [("primaryViewConfigurationType", C.c_int)])
EventDataBuffer = _struct("EventDataBuffer", TYPED + [("varying", C.c_uint8 * 4000)])
SessionStateChanged = _struct("SessionStateChanged", TYPED + [("session", H), ("state", C.c_int),
                                                              ("time", C.c_int64)])
ActionSetCreateInfo = _struct("ActionSetCreateInfo", TYPED + [
    ("actionSetName", C.c_char * 64), ("localizedActionSetName", C.c_char * 128), ("priority", C.c_uint32)])
ActionCreateInfo = _struct("ActionCreateInfo", TYPED + [
    ("actionName", C.c_char * 64), ("actionType", C.c_int), ("countSubactionPaths", C.c_uint32),
    ("subactionPaths", P(H)), ("localizedActionName", C.c_char * 128)])
SuggestedBinding = _struct("SuggestedBinding", [("action", H), ("binding", H)])
ProfileBindings = _struct("ProfileBindings", TYPED + [
    ("interactionProfile", H), ("countSuggestedBindings", C.c_uint32), ("suggestedBindings", P(SuggestedBinding))])
AttachInfo = _struct("AttachInfo", TYPED + [("countActionSets", C.c_uint32), ("actionSets", P(H))])
ActiveActionSet = _struct("ActiveActionSet", [("actionSet", H), ("subactionPath", H)])
SyncInfo = _struct("SyncInfo", TYPED + [("countActiveActionSets", C.c_uint32),
                                        ("activeActionSets", P(ActiveActionSet))])
Posef = _struct("Posef", [("orientation", C.c_float * 4), ("position", C.c_float * 3)])  # x, y, z, w
ActionSpaceCreateInfo = _struct("ActionSpaceCreateInfo", TYPED + [("action", H), ("subactionPath", H),
                                                                  ("poseInActionSpace", Posef)])
SpaceLocation = _struct("SpaceLocation", TYPED + [("locationFlags", C.c_uint64), ("pose", Posef)])
Next = _struct("Next", TYPED)  # the get / enumerate infos that carry nothing else
RenderModelCreateInfo = _struct("RenderModelCreateInfo", TYPED + [
    ("renderModelId", H), ("gltfExtensionCount", C.c_uint32), ("gltfExtensions", P(C.c_char_p))])
RenderModelProperties = _struct("RenderModelProperties", TYPED + [("cacheId", C.c_uint8 * 16),
                                                                  ("animatableNodeCount", C.c_uint32)])
RenderModelSpaceCreateInfo = _struct("RenderModelSpaceCreateInfo", TYPED + [("renderModel", H)])
AssetCreateInfo = _struct("AssetCreateInfo", TYPED + [("cacheId", C.c_uint8 * 16)])
AssetData = _struct("AssetData", TYPED + [("bufferCapacityInput", C.c_uint32), ("bufferCountOutput", C.c_uint32),
                                          ("buffer", P(C.c_uint8))])
NodeName = _struct("NodeName", [("uniqueName", C.c_char * 64)])
AssetProperties = _struct("AssetProperties", TYPED + [("nodePropertyCount", C.c_uint32),
                                                      ("nodeProperties", P(NodeName))])

T_INSTANCE_CREATE_INFO, T_SYSTEM_GET_INFO, T_SESSION_CREATE_INFO, T_SESSION_BEGIN_INFO = 3, 4, 8, 10
T_EVENT_DATA_BUFFER, T_SESSION_STATE_CHANGED = 16, 18
T_ACTION_SET_CREATE_INFO, T_ACTION_CREATE_INFO, T_ACTION_SPACE_CREATE_INFO = 28, 29, 38
T_SPACE_LOCATION, T_PROFILE_BINDINGS, T_ATTACH_INFO, T_SYNC_INFO = 42, 51, 60, 61
T_RM_CREATE_INFO, T_RM_PROPERTIES_GET_INFO, T_RM_PROPERTIES, T_RM_SPACE_CREATE_INFO = (1000300000, 1000300001,
                                                                                    1000300002, 1000300003)
T_ASSET_CREATE_INFO, T_ASSET_DATA_GET_INFO, T_ASSET_DATA = 1000300006, 1000300007, 1000300008
T_ASSET_PROPERTIES_GET_INFO, T_ASSET_PROPERTIES = 1000300009, 1000300010
T_IDS_ENUMERATE_INFO, T_SUBACTION_PATH_INFO = 1000301000, 1000301001
STATE_READY, STATE_FOCUSED = 2, 5
POSE_INPUT = 4
LOCATION_VALID = 0x3  # orientation and position

EXTENSIONS = ("XR_MND_headless", "XR_EXT_uuid", "XR_EXT_render_model", "XR_EXT_interaction_render_model")
TIME_EXTENSION = "XR_KHR_win32_convert_performance_counter_time"
PROFILES = ("/interaction_profiles/oculus/touch_controller", "/interaction_profiles/meta/touch_plus_controller",
            "/interaction_profiles/meta/touch_pro_controller", "/interaction_profiles/valve/index_controller",
            "/interaction_profiles/htc/vive_controller", "/interaction_profiles/microsoft/motion_controller",
            "/interaction_profiles/khr/simple_controller")
HANDS = ("left", "right")


class Unavailable(Exception):
    """The runtime can't give models; the message says why, for the user."""


def active_runtime():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\OpenXR\1") as key:
        return winreg.QueryValueEx(key, "ActiveRuntime")[0]


class Runtime:
    """The runtime DLL from its manifest, negotiated with as the loader does."""

    def __init__(self, manifest):
        with open(manifest) as f:
            info = json.load(f)["runtime"]
        path = os.path.normpath(os.path.join(os.path.dirname(manifest), info["library_path"]))
        self.name = info.get("name", os.path.basename(path))
        os.add_dll_directory(os.path.dirname(path))
        dll = C.CDLL(path)
        loader = NegotiateLoaderInfo(1, 1, C.sizeof(NegotiateLoaderInfo), 1, 1, _version(1, 0, 0),
                                     _version(1, 1, 0xffff))
        req = NegotiateRuntimeRequest(3, 1, C.sizeof(NegotiateRuntimeRequest), 0, 0, None)
        if dll.xrNegotiateLoaderRuntimeInterface(C.byref(loader), C.byref(req)) != 0 or not req.getInstanceProcAddr:
            raise Unavailable(f"{self.name} didn't answer as an OpenXR runtime")
        self._gipa = C.CFUNCTYPE(C.c_int, H, C.c_char_p, P(C.c_void_p))(req.getInstanceProcAddr)

    def proc(self, instance, name, *argtypes):
        ptr = C.c_void_p()
        if self._gipa(instance, name.encode(), C.byref(ptr)) != 0 or not ptr.value:
            raise RuntimeError(f"{name} unavailable")
        return C.CFUNCTYPE(C.c_int, *argtypes)(ptr.value)

    def extensions(self):
        enum = self.proc(0, "xrEnumerateInstanceExtensionProperties", C.c_char_p, C.c_uint32, P(C.c_uint32),
                         P(ExtensionProperties))
        count = C.c_uint32()
        enum(None, 0, C.byref(count), None)
        props = (ExtensionProperties * count.value)()
        for p in props:
            p.type = 2  # XR_TYPE_EXTENSION_PROPERTIES
        enum(None, count.value, C.byref(count), props)
        return {p.extensionName.decode() for p in props}


def _ok(res, what):
    if res < 0:
        raise RuntimeError(f"{what} failed ({res})")
    return res


class Client:
    """Our own instance and headless session, with an aim pose action per hand."""

    FUNCTIONS = (
        ("xrDestroyInstance", (H,)),
        ("xrGetSystem", (H, P(SystemGetInfo), P(H))),
        ("xrCreateSession", (H, P(SessionCreateInfo), P(H))),
        ("xrBeginSession", (H, P(SessionBeginInfo))),
        ("xrPollEvent", (H, P(EventDataBuffer))),
        ("xrStringToPath", (H, C.c_char_p, P(H))),
        ("xrCreateActionSet", (H, P(ActionSetCreateInfo), P(H))),
        ("xrCreateAction", (H, P(ActionCreateInfo), P(H))),
        ("xrSuggestInteractionProfileBindings", (H, P(ProfileBindings))),
        ("xrAttachSessionActionSets", (H, P(AttachInfo))),
        ("xrSyncActions", (H, P(SyncInfo))),
        ("xrCreateActionSpace", (H, P(ActionSpaceCreateInfo), P(H))),
        ("xrLocateSpace", (H, H, C.c_int64, P(SpaceLocation))),
        ("xrEnumerateInteractionRenderModelIdsEXT", (H, P(Next), C.c_uint32, P(C.c_uint32), P(H))),
        ("xrEnumerateRenderModelSubactionPathsEXT", (H, P(Next), C.c_uint32, P(C.c_uint32), P(H))),
        ("xrCreateRenderModelEXT", (H, P(RenderModelCreateInfo), P(H))),
        ("xrGetRenderModelPropertiesEXT", (H, P(Next), P(RenderModelProperties))),
        ("xrCreateRenderModelSpaceEXT", (H, P(RenderModelSpaceCreateInfo), P(H))),
        ("xrCreateRenderModelAssetEXT", (H, P(AssetCreateInfo), P(H))),
        ("xrGetRenderModelAssetDataEXT", (H, P(Next), P(AssetData))),
        ("xrGetRenderModelAssetPropertiesEXT", (H, P(Next), P(AssetProperties))),
    )

    def __init__(self, rt):
        self.rt = rt
        have = rt.extensions()
        missing = [e for e in EXTENSIONS if e not in have]
        if missing:
            raise Unavailable(f"{rt.name} doesn't give controller models (no {', '.join(missing)})")
        wanted = EXTENSIONS + ((TIME_EXTENSION,) if TIME_EXTENSION in have else ())
        names = (C.c_char_p * len(wanted))(*[e.encode() for e in wanted])
        create = rt.proc(0, "xrCreateInstance", P(InstanceCreateInfo), P(H))
        self.instance = H()
        res = -1
        # SteamVR not running yet: the first request starts it and fails, so keep asking while it comes up.
        end = time.monotonic() + START_TIMEOUT
        while res != 0 and time.monotonic() < end:
            for api in (_version(1, 0, 34), _version(1, 1, 0)):  # SteamVR 2.18 refuses 1.1 here
                app = ApplicationInfo(b"VR Sculpt controller models", 1, b"vr_sculpt", 1, api)
                res = create(C.byref(InstanceCreateInfo(T_INSTANCE_CREATE_INFO, None, 0, app, 0, None, len(wanted),
                                                        names)), C.byref(self.instance))
                if res == 0:
                    break
            else:
                time.sleep(1.0)
        if res != 0:
            raise Unavailable(f"{rt.name} didn't start: is it running, with the headset connected? ({res})")
        self.f = {name: rt.proc(self.instance.value, name, *args) for name, args in self.FUNCTIONS}
        self.to_time = None
        if TIME_EXTENSION in have:
            self.to_time = rt.proc(self.instance.value, "xrConvertWin32PerformanceCounterToTimeKHR", H,
                                   P(C.c_int64), P(C.c_int64))
        self.state, self.last_time = 0, 0
        system = H()
        if self.f["xrGetSystem"](self.instance, C.byref(SystemGetInfo(T_SYSTEM_GET_INFO, None, 1)),
                                 C.byref(system)) != 0:
            raise Unavailable("no headset: turn it on and connect it")
        self.session = H()
        _ok(self.f["xrCreateSession"](self.instance, C.byref(SessionCreateInfo(T_SESSION_CREATE_INFO, None, 0, system)),
                                      C.byref(self.session)), "xrCreateSession")
        self._actions()

    def path(self, s):
        p = H()
        _ok(self.f["xrStringToPath"](self.instance, s.encode(), C.byref(p)), "xrStringToPath")
        return p.value

    def _actions(self):
        f = self.f
        self.action_set = H()
        _ok(f["xrCreateActionSet"](self.instance, C.byref(ActionSetCreateInfo(
            T_ACTION_SET_CREATE_INFO, None, b"vrs_models", b"Controller models", 0)), C.byref(self.action_set)),
            "xrCreateActionSet")
        self.hands = [self.path(f"/user/hand/{h}") for h in HANDS]
        subs = (H * 2)(*self.hands)
        aim = H()
        _ok(f["xrCreateAction"](self.action_set, C.byref(ActionCreateInfo(
            T_ACTION_CREATE_INFO, None, b"aim", POSE_INPUT, 2, subs, b"Aim")), C.byref(aim)), "xrCreateAction")
        binds = (SuggestedBinding * 2)(*[SuggestedBinding(aim, self.path(f"/user/hand/{h}/input/aim/pose"))
                                         for h in HANDS])
        for profile in PROFILES:  # one the runtime doesn't know is refused, and that's fine
            f["xrSuggestInteractionProfileBindings"](self.instance, C.byref(ProfileBindings(
                T_PROFILE_BINDINGS, None, self.path(profile), 2, binds)))
        sets = (H * 1)(self.action_set)
        _ok(f["xrAttachSessionActionSets"](self.session, C.byref(AttachInfo(T_ATTACH_INFO, None, 1, sets))),
            "xrAttachSessionActionSets")
        self.aim_spaces = []
        for hand in self.hands:
            space = H()
            identity = Posef((0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
            _ok(f["xrCreateActionSpace"](self.session, C.byref(ActionSpaceCreateInfo(
                T_ACTION_SPACE_CREATE_INFO, None, aim, hand, identity)), C.byref(space)), "xrCreateActionSpace")
            self.aim_spaces.append(space)

    def pump(self):
        ev = EventDataBuffer()
        while True:
            ev.type, ev.next = T_EVENT_DATA_BUFFER, None
            if self.f["xrPollEvent"](self.instance, C.byref(ev)) != 0:
                return
            if ev.type == T_SESSION_STATE_CHANGED:
                changed = C.cast(C.byref(ev), P(SessionStateChanged)).contents
                self.state, self.last_time = changed.state, changed.time
                if self.state == STATE_READY:
                    _ok(self.f["xrBeginSession"](self.session, C.byref(SessionBeginInfo(T_SESSION_BEGIN_INFO, None, 2))),
                        "xrBeginSession")

    def sync(self):
        active = (ActiveActionSet * 1)(ActiveActionSet(self.action_set, 0))
        self.f["xrSyncActions"](self.session, C.byref(SyncInfo(T_SYNC_INFO, None, 1, active)))

    def now(self):
        if self.to_time is None:
            return self.last_time
        counter, t = C.c_int64(), C.c_int64()
        C.windll.kernel32.QueryPerformanceCounter(C.byref(counter))
        self.to_time(self.instance, C.byref(counter), C.byref(t))
        return t.value

    def model_ids(self):
        enum = self.f["xrEnumerateInteractionRenderModelIdsEXT"]
        info, n = Next(T_IDS_ENUMERATE_INFO, None), C.c_uint32()
        if enum(self.session, C.byref(info), 0, C.byref(n), None) != 0 or not n.value:
            return []
        ids = (H * n.value)()
        enum(self.session, C.byref(info), n.value, C.byref(n), ids)
        return list(ids[:n.value])

    def open_model(self, model_id):
        """(render model handle, hand index or None, node names, glb bytes, model space)."""
        f = self.f
        model = H()
        _ok(f["xrCreateRenderModelEXT"](self.session, C.byref(RenderModelCreateInfo(
            T_RM_CREATE_INFO, None, model_id, 0, None)), C.byref(model)), "xrCreateRenderModelEXT")
        info, n = Next(T_SUBACTION_PATH_INFO, None), C.c_uint32()
        f["xrEnumerateRenderModelSubactionPathsEXT"](model, C.byref(info), 0, C.byref(n), None)
        paths = (H * max(n.value, 1))()
        f["xrEnumerateRenderModelSubactionPathsEXT"](model, C.byref(info), n.value, C.byref(n), paths)
        hand = next((self.hands.index(p) for p in paths[:n.value] if p in self.hands), None)
        props = RenderModelProperties(T_RM_PROPERTIES, None)
        _ok(f["xrGetRenderModelPropertiesEXT"](model, C.byref(Next(T_RM_PROPERTIES_GET_INFO, None)), C.byref(props)),
            "xrGetRenderModelPropertiesEXT")
        asset = H()
        _ok(f["xrCreateRenderModelAssetEXT"](self.session, C.byref(AssetCreateInfo(
            T_ASSET_CREATE_INFO, None, props.cacheId)), C.byref(asset)), "xrCreateRenderModelAssetEXT")
        get = Next(T_ASSET_DATA_GET_INFO, None)
        data = AssetData(T_ASSET_DATA, None, 0, 0, None)
        _ok(f["xrGetRenderModelAssetDataEXT"](asset, C.byref(get), C.byref(data)), "asset size")
        buf = (C.c_uint8 * data.bufferCountOutput)()
        data.bufferCapacityInput, data.buffer = data.bufferCountOutput, buf
        _ok(f["xrGetRenderModelAssetDataEXT"](asset, C.byref(get), C.byref(data)), "asset data")
        nodes = (NodeName * max(props.animatableNodeCount, 1))()
        aprops = AssetProperties(T_ASSET_PROPERTIES, None, props.animatableNodeCount, nodes)
        f["xrGetRenderModelAssetPropertiesEXT"](asset, C.byref(Next(T_ASSET_PROPERTIES_GET_INFO, None)),
                                                C.byref(aprops))
        names = [nd.uniqueName.decode() for nd in nodes[:props.animatableNodeCount]]
        space = H()
        _ok(f["xrCreateRenderModelSpaceEXT"](self.session, C.byref(RenderModelSpaceCreateInfo(
            T_RM_SPACE_CREATE_INFO, None, model)), C.byref(space)), "xrCreateRenderModelSpaceEXT")
        return model, hand, names, bytes(buf), space

    def locate(self, space, hand):
        """4x4 matrix of space in the hand's aim pose space, or None while either isn't tracked."""
        loc = SpaceLocation(T_SPACE_LOCATION, None)
        if self.f["xrLocateSpace"](space, self.aim_spaces[hand], self.now(), C.byref(loc)) != 0:
            return None
        if loc.locationFlags & LOCATION_VALID != LOCATION_VALID:
            return None
        x, y, z, w = loc.pose.orientation
        m = np.identity(4)
        m[:3, :3] = _quat_matrix((w, x, y, z))
        m[:3, 3] = tuple(loc.pose.position)
        return m

    def close(self):
        self.f["xrDestroyInstance"](self.instance)  # Takes the session, spaces and models with it.


def fetch(manifest, timeout=TIMEOUT):
    """{hand index: (glb bytes, model-in-aim matrix, node names)} for both controllers, and the runtime's
    name. Raises Unavailable with a message for the user."""
    rt = Runtime(manifest)
    client = Client(rt)
    found, opened = {}, {}
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end and len(found) < 2:
            client.pump()
            if client.state == STATE_FOCUSED:
                client.sync()
                for model_id in client.model_ids():
                    if model_id not in opened:
                        _model, hand, names, glb, space = client.open_model(model_id)
                        opened[model_id] = (hand, names, glb, space)
                for hand, names, glb, space in opened.values():
                    if hand is None or hand in found or not is_controller(names):
                        continue
                    where = client.locate(space, hand)
                    if where is not None:
                        found[hand] = (glb, where, names)
            time.sleep(0.05)
    finally:
        client.close()
    if len(found) < 2:
        if client.state != STATE_FOCUSED:
            raise Unavailable(f"{rt.name} didn't let the model fetch run (is another VR app in front?)")
        raise Unavailable("no controllers seen: hold both controllers with the headset awake, and try again")
    return found, rt.name


# --- the job -------------------------------------------------------------------------------------

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main():
    out = sys.argv[sys.argv.index("--") + 1]
    os.makedirs(out, exist_ok=True)
    status = {"ok": False, "message": "", "runtime": "", "hands": {}}
    try:
        found, status["runtime"] = fetch(active_runtime())
        for hand, (glb, where, _names) in found.items():
            arrays = convert(glb, where, out, HANDS[hand])
            name = HANDS[hand] + ".npz"
            tmp = os.path.join(out, HANDS[hand] + ".tmp.npz")
            np.savez_compressed(tmp, **arrays)
            os.replace(tmp, os.path.join(out, name))
            status["hands"][HANDS[hand]] = name
        status["ok"] = True
        status["message"] = f"Controller models from {status['runtime']}"
    except Unavailable as ex:
        status["message"] = str(ex)
    except Exception as ex:  # Anything else is a bug or a runtime surprise: say what it was.
        import traceback
        traceback.print_exc()
        status["message"] = f"Couldn't get the controller models: {ex}"
    _write_json(os.path.join(out, "status.json"), status)


if __name__ == "__main__":
    main()
