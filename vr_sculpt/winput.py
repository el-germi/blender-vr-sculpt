"""Windows only: capture Blender's own window from the OS and drive it with real mouse and keyboard
input, so the floating desktop screen in VR can be used, not just watched.

Blender ignores posted window messages (it reads the real cursor), so input goes through
SendInput and really moves the mouse. Clicks are guarded: a button is only pressed when the
window under that point belongs to Blender, so a covering window never gets clicked.

Capture runs on a worker thread: GDI BitBlt of the window, split into horizontal strips, and
only strips that changed since the last grab are converted (to the float buffers GPUTexture
needs). The VR draw callback turns them into textures.
"""

import ctypes
import os
import sys
import threading
import time

import numpy as np

AVAILABLE = sys.platform == "win32"

if AVAILABLE:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    _H = wintypes.HANDLE
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    for _fn, _res, _args in (
            (user32.GetDC, _H, [wintypes.HWND]),
            (user32.ReleaseDC, ctypes.c_int, [wintypes.HWND, _H]),
            (user32.GetForegroundWindow, wintypes.HWND, []),
            (user32.SetForegroundWindow, wintypes.BOOL, [wintypes.HWND]),
            (user32.IsWindow, wintypes.BOOL, [wintypes.HWND]),
            (user32.IsIconic, wintypes.BOOL, [wintypes.HWND]),
            (user32.WindowFromPoint, wintypes.HWND, [wintypes.POINT]),
            (user32.GetAncestor, wintypes.HWND, [wintypes.HWND, wintypes.UINT]),
            (user32.GetClientRect, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
            (user32.ClientToScreen, wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]),
            (user32.SetCursorPos, wintypes.BOOL, [ctypes.c_int, ctypes.c_int]),
            (user32.GetCursorPos, wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)]),
            (user32.SendInput, wintypes.UINT, [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]),
            (user32.GetWindowThreadProcessId, wintypes.DWORD, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]),
            (user32.IsWindowVisible, wintypes.BOOL, [wintypes.HWND]),
            (user32.GetWindowTextLengthW, ctypes.c_int, [wintypes.HWND]),
            (user32.GetWindowTextW, ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
            (user32.EnumWindows, wintypes.BOOL, [_ENUMPROC, wintypes.LPARAM]),
            (gdi32.CreateCompatibleDC, _H, [_H]),
            (gdi32.CreateCompatibleBitmap, _H, [_H, ctypes.c_int, ctypes.c_int]),
            (gdi32.SelectObject, _H, [_H, _H]),
            (gdi32.DeleteObject, wintypes.BOOL, [_H]),
            (gdi32.DeleteDC, wintypes.BOOL, [_H]),
            (gdi32.BitBlt, wintypes.BOOL, [_H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _H,
                                           ctypes.c_int, ctypes.c_int, wintypes.DWORD]),
            (gdi32.GetDIBits, ctypes.c_int, [_H, _H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                             ctypes.c_void_p, wintypes.UINT])):
        _fn.restype, _fn.argtypes = _res, _args

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

BUTTONS = {'LEFT': (0x0002, 0x0004), 'RIGHT': (0x0008, 0x0010), 'MIDDLE': (0x0020, 0x0040)}
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
WHEEL_DELTA = 120

VK = {
    "BACK": 0x08, "TAB": 0x09, "RETURN": 0x0D, "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12, "ESC": 0x1B,
    "SPACE": 0x20, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "DELETE": 0x2E,
    "F2": 0x71, "F3": 0x72, "MINUS": 0xBD, "PERIOD": 0xBE, "COMMA": 0xBC, "HOME": 0x24, "END": 0x23,
}

_window = {"hwnd": None}


def find_window():
    """Blender's main window handle (the visible top-level window of this process), cached."""
    if not AVAILABLE:
        return None
    hwnd = _window["hwnd"]
    if hwnd and user32.IsWindow(hwnd):
        return hwnd
    pid = os.getpid()
    found = []

    @_ENUMPROC
    def cb(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            found.append((h, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    # The main window's title ends with "Blender <version>"; others (e.g. preferences) don't come first.
    found.sort(key=lambda f: 0 if "Blender" in f[1] else 1)
    _window["hwnd"] = found[0][0] if found else None
    return _window["hwnd"]


def client_rect(hwnd):
    """(screen x, screen y, width, height) of the window's client area."""
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right, r.bottom


def minimized(hwnd):
    return bool(user32.IsIconic(hwnd))


def ours_at(hwnd, x, y):
    """Whether the window under screen point (x, y) is Blender's."""
    h = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return bool(h) and user32.GetAncestor(h, 2) == hwnd  # GA_ROOT


def ensure_foreground(hwnd):
    if user32.GetForegroundWindow() != hwnd:
        user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


def to_screen(hwnd, u, v):
    """Screen pixel for client-area fractions u (left to right) and v (bottom to top)."""
    x, y, w, h = client_rect(hwnd)
    px = min(max(int(round(u * w)), 0), w - 1)
    py = min(max(int(round((1.0 - v) * h)), 0), h - 1)
    return x + px, y + py


def cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def move(x, y):
    user32.SetCursorPos(int(x), int(y))


def _send_mouse(flags, data=0):
    i = _INPUT(type=0)
    i.u.mi = _MOUSEINPUT(0, 0, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))


def button(name, down):
    _send_mouse(BUTTONS[name][0 if down else 1])


def wheel(notches):
    _send_mouse(MOUSEEVENTF_WHEEL, int(notches * WHEEL_DELTA))


def _send_key(vk, up):
    i = _INPUT(type=1)
    i.u.ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))


def press_keys(vks):
    """Press a chord (modifiers first), then release in reverse order."""
    for vk in vks:
        _send_key(vk, False)
    for vk in reversed(vks):
        _send_key(vk, True)


def vk_for(char):
    """Virtual key for a letter, digit or one of a few symbols, or None."""
    c = char.upper()
    if "A" <= c <= "Z" or "0" <= c <= "9":
        return ord(c)
    return {" ": VK["SPACE"], "-": VK["MINUS"], ".": VK["PERIOD"], ",": VK["COMMA"]}.get(char)


def grab(hwnd):
    """The window's client area as a bottom-up (h, w, 4) BGRA uint8 array, or None."""
    _x, _y, w, h = client_rect(hwnd)
    if w < 2 or h < 2:
        return None
    src = user32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, 0x00CC0020)  # SRCCOPY
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, h, 1, 32  # positive height: bottom-up, like GL
        data = np.empty((h, w, 4), dtype=np.uint8)
        gdi32.GetDIBits(mem, bmp, 0, h, data.ctypes.data, ctypes.byref(bmi), 0)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, src)
    return data


def strip_float(data, y0, y1):
    """Rows y0..y1 of a BGRA grab as flat RGBA float32 (opaque), ready for a GPU buffer."""
    part = data[y0:y1]
    out = np.empty(part.shape, dtype=np.float32)
    out[..., 0] = part[..., 2]
    out[..., 1] = part[..., 1]
    out[..., 2] = part[..., 0]
    out *= 1.0 / 255.0
    out[..., 3] = 1.0
    return out.ravel()


class Capturer(threading.Thread):
    """Grabs the window FPS times a second and publishes the strips that changed."""

    STRIP = 64  # rows per strip
    FPS = 30.0

    def __init__(self, hwnd):
        super().__init__(daemon=True, name="VR desktop capture")
        self.hwnd = hwnd
        self.lock = threading.Lock()
        self.pending = {}  # strip index -> (gpu Buffer, width, height, y0)
        self.size = (0, 0)
        self.stop_event = threading.Event()
        self.prev = None
        self.grab_ms = 0.0
        self.error = None

    def run(self):
        import gpu
        while not self.stop_event.is_set():
            t0 = time.perf_counter()
            try:
                data = grab(self.hwnd) if not minimized(self.hwnd) else None
                if data is not None:
                    h, w = data.shape[:2]
                    if (w, h) != self.size or self.prev is None or self.prev.shape != data.shape:
                        changed = range(0, (h + self.STRIP - 1) // self.STRIP)
                        with self.lock:
                            self.pending.clear()
                            self.size = (w, h)
                    else:
                        changed = [i for i in range(0, (h + self.STRIP - 1) // self.STRIP)
                                   if not np.array_equal(data[i * self.STRIP:(i + 1) * self.STRIP],
                                                         self.prev[i * self.STRIP:(i + 1) * self.STRIP])]
                    out = {}
                    for i in changed:
                        y0, y1 = i * self.STRIP, min((i + 1) * self.STRIP, h)
                        out[i] = (gpu.types.Buffer('FLOAT', w * (y1 - y0) * 4, strip_float(data, y0, y1)),
                                  w, y1 - y0, y0)
                    if out:
                        with self.lock:
                            self.pending.update(out)
                    self.prev = data
                self.grab_ms = (time.perf_counter() - t0) * 1000.0
            except Exception as ex:  # Keep the thread alive; report once.
                if self.error is None:
                    self.error = ex
                    print("VR desktop capture failed:", ex, flush=True)
            self.stop_event.wait(max(1.0 / self.FPS - (time.perf_counter() - t0), 0.005))

    def take(self, limit=None):
        """Pop up to limit pending strips: {index: (buffer, w, h, y0)}."""
        with self.lock:
            keys = sorted(self.pending)[:limit] if limit else list(self.pending)
            return {k: self.pending.pop(k) for k in keys}

    def stop(self):
        self.stop_event.set()
