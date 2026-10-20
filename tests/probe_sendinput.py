"""Probe (Windows): drive Blender with real OS input (SendInput), guarded so it only clicks when
the Blender window is the one under the cursor. Saves tests/probe_sendinput_*.png.

blender --factory-startup test_scene.blend --python tests/probe_sendinput.py
"""

import ctypes
import os
import time
import traceback
from ctypes import wintypes

import bpy
import gpu
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "tests", "probe_sendinput_result.txt")
lines = []

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
H = wintypes.HANDLE
for fn, res, args in (
        (user32.GetDC, H, [wintypes.HWND]),
        (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, H]),
        (user32.PrintWindow, wintypes.BOOL, [wintypes.HWND, H, wintypes.UINT]),
        (user32.PostMessageW, wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
        (user32.GetForegroundWindow, wintypes.HWND, []),
        (gdi32.CreateCompatibleDC, H, [H]),
        (gdi32.CreateCompatibleBitmap, H, [H, ctypes.c_int, ctypes.c_int]),
        (gdi32.SelectObject, H, [H, H]),
        (gdi32.DeleteObject, wintypes.BOOL, [H]),
        (gdi32.DeleteDC, wintypes.BOOL, [H]),
        (gdi32.BitBlt, wintypes.BOOL, [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int,
                                       ctypes.c_int, wintypes.DWORD]),
        (gdi32.GetDIBits, ctypes.c_int, [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.UINT])):
    fn.restype, fn.argtypes = res, args


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    lines.append(s)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def find_hwnd():
    pid = os.getpid()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    log("windows of this process:", found)
    return next((h for h, t in found if "Blender" in t), found[0][0] if found else None)


def client_rect(hwnd):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right, r.bottom


def capture(hwnd, method):
    x, y, w, h = client_rect(hwnd)
    t0 = time.perf_counter()
    if method == "screen":
        src = user32.GetDC(None)
    else:
        src = user32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    old = gdi32.SelectObject(mem, bmp)
    if method == "screen":
        gdi32.BitBlt(mem, 0, 0, w, h, src, x, y, 0x00CC0020)
    elif method == "printwindow":
        user32.PrintWindow(hwnd, mem, 0x1 | 0x2)  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
    else:
        gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, 0x00CC0020)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, h, 1, 32  # bottom-up, like GL
    data = np.empty((h, w, 4), dtype=np.uint8)
    gdi32.GetDIBits(mem, bmp, 0, h, data.ctypes.data, ctypes.byref(bmi), 0)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None if method == "screen" else hwnd, src)
    t1 = time.perf_counter()
    return data, (t1 - t0) * 1000, (w, h)


def to_texture(data):
    h, w = data.shape[:2]
    t0 = time.perf_counter()
    rgba = data[..., [2, 1, 0, 3]].astype(np.float32)
    rgba *= 1.0 / 255.0
    rgba[..., 3] = 1.0
    t1 = time.perf_counter()
    buf = gpu.types.Buffer('FLOAT', w * h * 4, rgba.ravel())
    t2 = time.perf_counter()
    tex = gpu.types.GPUTexture((w, h), format='RGBA8', data=buf)
    t3 = time.perf_counter()
    return tex, ((t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000)


def save_png(data, name):
    h, w = data.shape[:2]
    img = bpy.data.images.new(name, w, h, alpha=True)
    img.pixels.foreach_set((data[..., [2, 1, 0, 3]].astype(np.float32) / 255.0).ravel())
    img.filepath_raw = os.path.join(ROOT, "tests", name + ".png")
    img.file_format = 'PNG'
    img.save()
    log("saved", img.filepath_raw)



class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]

LDOWN, LUP, RDOWN, RUP = 0x2, 0x4, 0x8, 0x10


def mouse(flags):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def key(vk):
    for flags in (0, 2):
        i = INPUT(type=1)
        i.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def ours_at(x, y):
    h = user32.WindowFromPoint(wintypes.POINT(x, y))
    return h and user32.GetAncestor(h, 2) == state["hwnd"]  # GA_ROOT


state = {}


def setup():
    try:
        hwnd = find_hwnd()
        state["hwnd"] = hwnd
        log("SetForegroundWindow:", user32.SetForegroundWindow(hwnd), "fg is us:", user32.GetForegroundWindow() == hwnd)
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        win = bpy.context.window_manager.windows[0]
        area = max((a for a in win.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
        region = next(r for r in area.regions if r.type == 'WINDOW')
        p = location_3d_to_region_2d(region, area.spaces.active.region_3d,
                                     bpy.data.objects["SculptBall"].matrix_world.translation)
        cx, cy, cw, ch = client_rect(hwnd)
        sx, sy = cx + int(region.x + p.x), cy + int(ch - (region.y + p.y))
        state["pt"] = (sx, sy)
        old = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(old))
        state["old"] = (old.x, old.y)
        bpy.ops.object.select_all(action='DESELECT')
        if not ours_at(sx, sy):
            log("ABORT: Blender is not the window under", (sx, sy))
            done()
            return
        user32.SetCursorPos(sx, sy)
        bpy.app.timers.register(click, first_interval=0.2)
    except Exception:
        log(traceback.format_exc())
        done()


def click():
    if ours_at(*state["pt"]):
        mouse(LDOWN)
        mouse(LUP)
        log("left click sent at", state["pt"])
    bpy.app.timers.register(after_click, first_interval=0.6)


def after_click():
    try:
        log("SculptBall selected after SendInput click:", bpy.data.objects["SculptBall"].select_get())
        if ours_at(*state["pt"]):
            mouse(RDOWN)
            mouse(RUP)
        bpy.app.timers.register(after_menu, first_interval=0.6)
    except Exception:
        log(traceback.format_exc())
        restore()


def after_menu():
    try:
        data, ms, size = capture(state["hwnd"], "window")
        log(f"window-DC capture with the menu open: {ms:.1f} ms")
        save_png(data, "probe_sendinput_menu")
        key(0x1B)  # Escape closes the menu
    except Exception:
        log(traceback.format_exc())
    restore()


def restore():
    user32.SetCursorPos(*state.get("old", (0, 0)))
    log("cursor restored")
    bpy.app.timers.register(lambda: (done(), None)[1], first_interval=0.3)


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(setup, first_interval=2.0)
