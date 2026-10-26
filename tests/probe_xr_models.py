"""Probe: can Python get the runtime's controller models?

Blender links the OpenXR loader statically and keeps its handles to itself, so this talks to the
runtime DLL directly over ctypes (the loader/runtime negotiation the loader itself does) and makes
its own instance: a headless one (XR_MND_headless) beside Blender's, with the Khronos render model
extensions (XR_EXT_render_model + XR_EXT_interaction_render_model). It lists the controllers'
models and writes each one's glTF to <out_dir>/<cache id>.glb.

Found on this machine: neither SteamVR nor Meta's runtime has XR_MSFT_controller_model or
XR_FB_render_model. SteamVR has the EXT pair and headless, and serves the models over them; Meta's
runtime has neither.

Don't run it during a Blender VR session: SteamVR makes a headless session the scene app anyway
(VRApplication_OpenXRScene), sends Blender's session a quit and ends it.

    python tests/probe_xr_models.py [runtime.json] [out_dir] [--list]
    (defaults: the active runtime, tests/xr_models; --list prints every extension)

Use Blender's Python (5.4/python/bin/python.exe); no Blender needed.
"""

import ctypes as C
import json
import os
import sys
import winreg

XR_NULL = 0


def make_version(major, minor, patch):
    return (major << 48) | (minor << 32) | patch


class NegotiateLoaderInfo(C.Structure):
    _fields_ = [("structType", C.c_int), ("structVersion", C.c_uint32), ("structSize", C.c_size_t),
                ("minInterfaceVersion", C.c_uint32), ("maxInterfaceVersion", C.c_uint32),
                ("minApiVersion", C.c_uint64), ("maxApiVersion", C.c_uint64)]


class NegotiateRuntimeRequest(C.Structure):
    _fields_ = [("structType", C.c_int), ("structVersion", C.c_uint32), ("structSize", C.c_size_t),
                ("runtimeInterfaceVersion", C.c_uint32), ("runtimeApiVersion", C.c_uint64),
                ("getInstanceProcAddr", C.c_void_p)]


class ExtensionProperties(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("extensionName", C.c_char * 128),
                ("extensionVersion", C.c_uint32)]


PFN_GetInstanceProcAddr = C.CFUNCTYPE(C.c_int, C.c_uint64, C.c_char_p, C.POINTER(C.c_void_p))


def active_runtime():
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\OpenXR\1") as key:
        return winreg.QueryValueEx(key, "ActiveRuntime")[0]


class Runtime:
    def __init__(self, manifest):
        with open(manifest) as f:
            info = json.load(f)["runtime"]
        path = os.path.normpath(os.path.join(os.path.dirname(manifest), info["library_path"]))
        self.name = info.get("name", path)
        os.add_dll_directory(os.path.dirname(path))
        self.dll = C.CDLL(path)
        loader = NegotiateLoaderInfo(1, 1, C.sizeof(NegotiateLoaderInfo), 1, 1,
                                     make_version(1, 0, 0), make_version(1, 1, 0xffff))
        req = NegotiateRuntimeRequest(3, 1, C.sizeof(NegotiateRuntimeRequest), 0, 0, None)
        res = self.dll.xrNegotiateLoaderRuntimeInterface(C.byref(loader), C.byref(req))
        if res != 0 or not req.getInstanceProcAddr:
            raise RuntimeError("negotiation failed: %d" % res)
        self.api_version = req.runtimeApiVersion
        self._gipa = PFN_GetInstanceProcAddr(req.getInstanceProcAddr)

    def proc(self, instance, name, restype, *argtypes):
        ptr = C.c_void_p()
        res = self._gipa(instance, name.encode(), C.byref(ptr))
        if res != 0 or not ptr.value:
            raise RuntimeError("%s unavailable: %d" % (name, res))
        return C.CFUNCTYPE(restype, *argtypes)(ptr.value)

    def extensions(self):
        enum = self.proc(XR_NULL, "xrEnumerateInstanceExtensionProperties", C.c_int,
                         C.c_char_p, C.c_uint32, C.POINTER(C.c_uint32), C.POINTER(ExtensionProperties))
        count = C.c_uint32()
        enum(None, 0, C.byref(count), None)
        props = (ExtensionProperties * count.value)()
        for p in props:
            p.type = 2  # XR_TYPE_EXTENSION_PROPERTIES
        enum(None, count.value, C.byref(count), props)
        return {p.extensionName.decode(): p.extensionVersion for p in props}


# --- core structs (openxr.h) ------------------------------------------------------------

H = C.c_uint64  # handles, atoms, paths, XrTime are all 64-bit


class ApplicationInfo(C.Structure):
    _fields_ = [("applicationName", C.c_char * 128), ("applicationVersion", C.c_uint32),
                ("engineName", C.c_char * 128), ("engineVersion", C.c_uint32), ("apiVersion", C.c_uint64)]


class InstanceCreateInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("createFlags", C.c_uint64),
                ("applicationInfo", ApplicationInfo), ("enabledApiLayerCount", C.c_uint32),
                ("enabledApiLayerNames", C.POINTER(C.c_char_p)), ("enabledExtensionCount", C.c_uint32),
                ("enabledExtensionNames", C.POINTER(C.c_char_p))]


class SystemGetInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("formFactor", C.c_int)]


class SessionCreateInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("createFlags", C.c_uint64), ("systemId", H)]


class SessionBeginInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("primaryViewConfigurationType", C.c_int)]


class EventDataBuffer(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("varying", C.c_uint8 * 4000)]


class EventDataSessionStateChanged(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("session", H), ("state", C.c_int), ("time", C.c_int64)]


class ActionSetCreateInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("actionSetName", C.c_char * 64),
                ("localizedActionSetName", C.c_char * 128), ("priority", C.c_uint32)]


class ActionCreateInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("actionName", C.c_char * 64), ("actionType", C.c_int),
                ("countSubactionPaths", C.c_uint32), ("subactionPaths", C.POINTER(H)),
                ("localizedActionName", C.c_char * 128)]


class ActionSuggestedBinding(C.Structure):
    _fields_ = [("action", H), ("binding", H)]


class InteractionProfileSuggestedBinding(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("interactionProfile", H),
                ("countSuggestedBindings", C.c_uint32), ("suggestedBindings", C.POINTER(ActionSuggestedBinding))]


class SessionActionSetsAttachInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("countActionSets", C.c_uint32),
                ("actionSets", C.POINTER(H))]


class ActiveActionSet(C.Structure):
    _fields_ = [("actionSet", H), ("subactionPath", H)]


class ActionsSyncInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("countActiveActionSets", C.c_uint32),
                ("activeActionSets", C.POINTER(ActiveActionSet))]


class Next(C.Structure):  # every *GetInfoEXT / *EnumerateInfoEXT that carries nothing else
    _fields_ = [("type", C.c_int), ("next", C.c_void_p)]


# --- XR_EXT_render_model / XR_EXT_interaction_render_model --------------------------------

class RenderModelCreateInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("renderModelId", H),
                ("gltfExtensionCount", C.c_uint32), ("gltfExtensions", C.POINTER(C.c_char_p))]


class RenderModelProperties(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("cacheId", C.c_uint8 * 16),
                ("animatableNodeCount", C.c_uint32)]


class RenderModelAssetCreateInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("cacheId", C.c_uint8 * 16)]


class RenderModelAssetData(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("bufferCapacityInput", C.c_uint32),
                ("bufferCountOutput", C.c_uint32), ("buffer", C.POINTER(C.c_uint8))]


class NodeName(C.Structure):
    _fields_ = [("uniqueName", C.c_char * 64)]


class RenderModelAssetProperties(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("nodePropertyCount", C.c_uint32),
                ("nodeProperties", C.POINTER(NodeName))]


class TopLevelUserPathGetInfo(C.Structure):
    _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("topLevelUserPathCount", C.c_uint32),
                ("topLevelUserPaths", C.POINTER(H))]


EXTENSIONS = ("XR_MND_headless", "XR_EXT_uuid", "XR_EXT_render_model", "XR_EXT_interaction_render_model")
STATES = {1: "IDLE", 2: "READY", 3: "SYNCHRONIZED", 4: "VISIBLE", 5: "FOCUSED", 6: "STOPPING",
          7: "LOSS_PENDING", 8: "EXITING"}
P = C.POINTER


class Session:
    """Our own instance + headless session with the render model extensions on."""

    def __init__(self, rt):
        self.rt = rt
        names = (C.c_char_p * len(EXTENSIONS))(*[e.encode() for e in EXTENSIONS])
        self.instance = H()
        create = rt.proc(XR_NULL, "xrCreateInstance", C.c_int, P(InstanceCreateInfo), P(H))
        for api in (make_version(1, 0, 34), make_version(1, 1, 0)):
            app = ApplicationInfo(b"vr_sculpt_probe", 1, b"vr_sculpt", 1, api)
            info = InstanceCreateInfo(3, None, 0, app, 0, None, len(EXTENSIONS), names)
            res = create(C.byref(info), C.byref(self.instance))
            print("xrCreateInstance (API %d.%d): %d" % (api >> 48, (api >> 32) & 0xffff, res))
            if res == 0:
                break
        if res != 0:
            raise RuntimeError("xrCreateInstance: %d" % res)
        f = self.f = {}
        for name, args in (
                ("xrDestroyInstance", (H,)),
                ("xrGetSystem", (H, P(SystemGetInfo), P(H))),
                ("xrCreateSession", (H, P(SessionCreateInfo), P(H))),
                ("xrBeginSession", (H, P(SessionBeginInfo))),
                ("xrEndSession", (H,)),
                ("xrPollEvent", (H, P(EventDataBuffer))),
                ("xrStringToPath", (H, C.c_char_p, P(H))),
                ("xrPathToString", (H, H, C.c_uint32, P(C.c_uint32), C.c_char_p)),
                ("xrCreateActionSet", (H, P(ActionSetCreateInfo), P(H))),
                ("xrCreateAction", (H, P(ActionCreateInfo), P(H))),
                ("xrSuggestInteractionProfileBindings", (H, P(InteractionProfileSuggestedBinding))),
                ("xrAttachSessionActionSets", (H, P(SessionActionSetsAttachInfo))),
                ("xrSyncActions", (H, P(ActionsSyncInfo))),
                ("xrEnumerateInteractionRenderModelIdsEXT", (H, P(Next), C.c_uint32, P(C.c_uint32), P(H))),
                ("xrCreateRenderModelEXT", (H, P(RenderModelCreateInfo), P(H))),
                ("xrDestroyRenderModelEXT", (H,)),
                ("xrGetRenderModelPropertiesEXT", (H, P(Next), P(RenderModelProperties))),
                ("xrGetRenderModelPoseTopLevelUserPathEXT", (H, P(TopLevelUserPathGetInfo), P(H))),
                ("xrEnumerateRenderModelSubactionPathsEXT", (H, P(Next), C.c_uint32, P(C.c_uint32), P(H))),
                ("xrCreateRenderModelAssetEXT", (H, P(RenderModelAssetCreateInfo), P(H))),
                ("xrDestroyRenderModelAssetEXT", (H,)),
                ("xrGetRenderModelAssetDataEXT", (H, P(Next), P(RenderModelAssetData))),
                ("xrGetRenderModelAssetPropertiesEXT", (H, P(Next), P(RenderModelAssetProperties))),
        ):
            f[name] = rt.proc(self.instance.value, name, C.c_int, *args)
        self.system = H()
        res = f["xrGetSystem"](self.instance, C.byref(SystemGetInfo(4, None, 1)), C.byref(self.system))
        if res != 0:
            raise RuntimeError("xrGetSystem: %d (is the headset connected?)" % res)
        self.session = H()
        res = f["xrCreateSession"](self.instance, C.byref(SessionCreateInfo(8, None, 0, self.system)),
                                   C.byref(self.session))
        if res != 0:
            raise RuntimeError("xrCreateSession (headless): %d" % res)
        self.state = 0

    def path(self, s):
        p = H()
        self.f["xrStringToPath"](self.instance, s.encode(), C.byref(p))
        return p.value

    def path_str(self, p):
        buf = C.create_string_buffer(256)
        n = C.c_uint32()
        self.f["xrPathToString"](self.instance, p, 256, C.byref(n), buf)
        return buf.value.decode()

    def attach_touch_actions(self):
        f = self.f
        self.action_set = H()
        check_res(f["xrCreateActionSet"](self.instance, C.byref(ActionSetCreateInfo(28, None, b"probe", b"Probe", 0)),
                                         C.byref(self.action_set)), "xrCreateActionSet")
        hands = (H * 2)(self.path("/user/hand/left"), self.path("/user/hand/right"))
        grip = H()
        check_res(f["xrCreateAction"](self.action_set, C.byref(ActionCreateInfo(29, None, b"grip", 4, 2, hands,
                                                                                b"Grip")),
                                      C.byref(grip)), "xrCreateAction")
        for profile in ("/interaction_profiles/oculus/touch_controller",
                        "/interaction_profiles/meta/touch_plus_controller",
                        "/interaction_profiles/khr/simple_controller"):
            binds = (ActionSuggestedBinding * 2)(
                ActionSuggestedBinding(grip, self.path("/user/hand/left/input/grip/pose")),
                ActionSuggestedBinding(grip, self.path("/user/hand/right/input/grip/pose")))
            res = f["xrSuggestInteractionProfileBindings"](
                self.instance, C.byref(InteractionProfileSuggestedBinding(51, None, self.path(profile), 2, binds)))
            print("suggest %s: %d" % (profile, res))
        sets = (H * 1)(self.action_set)
        check_res(f["xrAttachSessionActionSets"](self.session, C.byref(SessionActionSetsAttachInfo(60, None, 1, sets))),
                  "xrAttachSessionActionSets")

    def sync(self):
        active = (ActiveActionSet * 1)(ActiveActionSet(self.action_set, 0))
        return self.f["xrSyncActions"](self.session, C.byref(ActionsSyncInfo(61, None, 1, active)))

    def pump(self):
        ev = EventDataBuffer()
        while True:
            ev.type, ev.next = 16, None
            if self.f["xrPollEvent"](self.instance, C.byref(ev)) != 0:  # XR_EVENT_UNAVAILABLE = 4
                return
            if ev.type == 18:
                state = C.cast(C.byref(ev), P(EventDataSessionStateChanged)).contents.state
                self.state = state
                print("session state:", STATES.get(state, state))
                if state == 2:
                    res = self.f["xrBeginSession"](self.session, C.byref(SessionBeginInfo(10, None, 2)))
                    print("xrBeginSession:", res)
            else:
                print("event type", ev.type)

    def model_ids(self):
        info = Next(1000301000, None)
        n = C.c_uint32()
        res = self.f["xrEnumerateInteractionRenderModelIdsEXT"](self.session, C.byref(info), 0, C.byref(n), None)
        if res != 0:
            return res, []
        ids = (H * n.value)()
        res = self.f["xrEnumerateInteractionRenderModelIdsEXT"](self.session, C.byref(info), n.value, C.byref(n), ids)
        return res, list(ids)

    def dump_model(self, model_id, out_dir, gltf_exts=()):
        f = self.f
        exts = (C.c_char_p * max(1, len(gltf_exts)))(*[e.encode() for e in gltf_exts])
        model = H()
        res = f["xrCreateRenderModelEXT"](self.session, C.byref(RenderModelCreateInfo(
            1000300000, None, model_id, len(gltf_exts), exts if gltf_exts else None)), C.byref(model))
        if res != 0:
            print("  xrCreateRenderModelEXT: %d" % res)
            return res
        props = RenderModelProperties(1000300002, None)
        check_res(f["xrGetRenderModelPropertiesEXT"](model, C.byref(Next(1000300001, None)), C.byref(props)),
                  "xrGetRenderModelPropertiesEXT")
        cache_id = bytes(props.cacheId).hex()
        top = H()
        res = f["xrGetRenderModelPoseTopLevelUserPathEXT"](model, C.byref(TopLevelUserPathGetInfo(1000301003, None, 0,
                                                                                                    None)), C.byref(top))
        n = C.c_uint32()
        f["xrEnumerateRenderModelSubactionPathsEXT"](model, C.byref(Next(1000301001, None)), 0, C.byref(n), None)
        subs = (H * n.value)()
        f["xrEnumerateRenderModelSubactionPathsEXT"](model, C.byref(Next(1000301001, None)), n.value, C.byref(n), subs)
        print("  id %d: cache %s, %d animatable nodes, pose path %s (%d), subaction paths %s" % (
            model_id, cache_id, props.animatableNodeCount, self.path_str(top.value) if top.value else "-", res,
            [self.path_str(p) for p in subs]))
        asset = H()
        check_res(f["xrCreateRenderModelAssetEXT"](self.session, C.byref(RenderModelAssetCreateInfo(
            1000300006, None, props.cacheId)), C.byref(asset)), "xrCreateRenderModelAssetEXT")
        data = RenderModelAssetData(1000300008, None, 0, 0, None)
        get = Next(1000300007, None)
        check_res(f["xrGetRenderModelAssetDataEXT"](asset, C.byref(get), C.byref(data)), "asset data size")
        buf = (C.c_uint8 * data.bufferCountOutput)()
        data.bufferCapacityInput, data.buffer = data.bufferCountOutput, buf
        check_res(f["xrGetRenderModelAssetDataEXT"](asset, C.byref(get), C.byref(data)), "asset data")
        path = os.path.join(out_dir, "%s.glb" % cache_id)
        with open(path, "wb") as fh:
            fh.write(bytes(buf))
        print("  wrote %s (%d bytes)" % (path, len(buf)))
        nodes = (NodeName * props.animatableNodeCount)()
        aprops = RenderModelAssetProperties(1000300010, None, props.animatableNodeCount, nodes)
        res = f["xrGetRenderModelAssetPropertiesEXT"](asset, C.byref(Next(1000300009, None)), C.byref(aprops))
        print("  animatable nodes (%d): %s" % (res, [n.uniqueName.decode() for n in nodes]))
        f["xrDestroyRenderModelAssetEXT"](asset)
        f["xrDestroyRenderModelEXT"](model)
        return 0

    def close(self):
        if self.state in (2, 3, 4, 5, 6):
            self.f["xrEndSession"](self.session)
        self.f["xrDestroyInstance"](self.instance)


def check_res(res, what):
    if res < 0:
        raise RuntimeError("%s: %d" % (what, res))
    return res


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fetch(args[0] if args else active_runtime(), args[1] if len(args) > 1 else None, "--list" in sys.argv)


def fetch(manifest, out_dir=None, list_all=False):
    import time
    rt = Runtime(manifest)
    v = rt.api_version
    print("runtime: %s, API %d.%d.%d" % (rt.name, v >> 48, (v >> 32) & 0xffff, v & 0xffffffff))
    exts = rt.extensions()
    if list_all:
        for name in sorted(exts):
            print("  %s v%d" % (name, exts[name]))
    for want in ("XR_MSFT_controller_model", "XR_FB_render_model") + EXTENSIONS:
        print("%s: %s" % (want, "YES" if want in exts else "no"))
    if not all(e in exts for e in EXTENSIONS):
        return
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "xr_models")
    os.makedirs(out_dir, exist_ok=True)
    s = Session(rt)
    try:
        s.attach_touch_actions()
        ids, last = [], None
        end = time.time() + 8.0
        while time.time() < end:
            s.pump()
            sync = s.sync()
            res, ids = s.model_ids()
            if (res, len(ids), sync, s.state) != last:
                last = (res, len(ids), sync, s.state)
                print("state %s, xrSyncActions %d, enumerate ids %d: %s" % (STATES.get(s.state, s.state), sync,
                                                                           res, ids))
            if ids:
                break
            time.sleep(0.1)
        for model_id in ids:
            if s.dump_model(model_id, out_dir) == -1000300002:  # needs a glTF extension we didn't list
                s.dump_model(model_id, out_dir, ("KHR_texture_basisu", "KHR_materials_emissive_strength",
                                                 "KHR_texture_transform", "KHR_mesh_quantization",
                                                 "KHR_draco_mesh_compression", "EXT_texture_webp"))
    finally:
        s.close()


if __name__ == "__main__":
    main()
