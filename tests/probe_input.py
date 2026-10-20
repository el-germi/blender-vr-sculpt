"""Probe (Windows): capture the Blender window from the OS (menus and popups included) and send it
mouse input without moving the real cursor (PostMessage). Saves tests/probe_input_*.png.

blender --factory-startup test_scene.blend --python tests/probe_input.py
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
LOG = os.path.join(ROOT, "tests", "probe_input_result.txt")
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


WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x200, 0x201, 0x202, 0x204, 0x205


def post(hwnd, msg, x, y, wparam=0):
    user32.PostMessageW(hwnd, msg, wparam, (y << 16) | (x & 0xFFFF))


state = {}


def setup():
    try:
        hwnd = find_hwnd()
        state["hwnd"] = hwnd
        log("hwnd", hwnd, "client", client_rect(hwnd), "fg is us:", user32.GetForegroundWindow() == hwnd)
        win = bpy.context.window_manager.windows[0]
        log("bpy window", win.x, win.y, win.width, win.height)
        for method in ("screen", "window", "printwindow"):
            data, ms, size = capture(hwnd, method)
            tex, conv = to_texture(data)
            log(f"{method}: {size} grab {ms:.1f} ms, convert {conv[0]:.1f} ms, Buffer {conv[1]:.1f} ms, "
                f"texture {conv[2]:.1f} ms, mean {data[..., :3].mean():.1f}")
            save_png(data, f"probe_input_{method}")
        # Half resolution conversion cost.
        data, ms, size = capture(hwnd, "screen")
        t0 = time.perf_counter()
        half = data[::2, ::2]
        tex, conv = to_texture(np.ascontiguousarray(half))
        log(f"half res convert+upload {(time.perf_counter() - t0) * 1000:.1f} ms")

        # Click on SculptBall with PostMessage: does it get selected?
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        area = max((a for a in win.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
        region = next(r for r in area.regions if r.type == 'WINDOW')
        rv3d = area.spaces.active.region_3d
        ball = bpy.data.objects["SculptBall"]
        bpy.ops.object.select_all(action='DESELECT')
        p = location_3d_to_region_2d(region, rv3d, ball.matrix_world.translation)
        wx, wy = region.x + p.x, region.y + p.y
        cx, cy = int(wx), int(win.height - wy)
        state["click"] = (cx, cy)
        log("ball at window", (wx, wy), "client", (cx, cy), "selected before:", ball.select_get())
        post(hwnd, WM_MOUSEMOVE, cx - 3, cy)
        post(hwnd, WM_MOUSEMOVE, cx, cy)
        post(hwnd, WM_LBUTTONDOWN, cx, cy, 1)
        post(hwnd, WM_LBUTTONUP, cx, cy, 0)
        bpy.app.timers.register(phase2, first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def phase2():
    try:
        ball = bpy.data.objects["SculptBall"]
        log("selected after PostMessage click:", ball.select_get(), "active:",
            bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None)
        # Right click: the context menu (a popup) should open; capture it.
        cx, cy = state["click"]
        post(state["hwnd"], WM_MOUSEMOVE, cx, cy)
        post(state["hwnd"], WM_RBUTTONDOWN, cx, cy, 2)
        post(state["hwnd"], WM_RBUTTONUP, cx, cy, 0)
        bpy.app.timers.register(phase3, first_interval=1.0)
    except Exception:
        log(traceback.format_exc())
        done()


def phase3():
    try:
        data, ms, size = capture(state["hwnd"], "screen")
        save_png(data, "probe_input_menu")
        data, ms, size = capture(state["hwnd"], "printwindow")
        save_png(data, "probe_input_menu_pw")
        # Escape the menu with a key message (likely ignored: GHOST reads raw input).
        post(state["hwnd"], WM_MOUSEMOVE, 5, 5)
    except Exception:
        log(traceback.format_exc())
    done()


def done():
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(setup, first_interval=2.0)
