"""macagentd — Artemis desktop-control agent for kind:"mac" fleet devices.

Runs on a macOS box (this Mac Studio) and exposes the same action dialect the
fleet uses for phones, translated to macOS APIs:

    GET  /health       liveness + screen size (points) + capability report
    GET  /doctor       TCC permission probe + grant instructions (no faking)
    GET  /screenshot   PNG (or ?format=jpg) — screencapture(1)
    GET  /frame        alias of /screenshot (fleet tile path)
    GET  /stream       MJPEG — periodic screencapture, same boundary as fleet
    POST /input        {action?, cx,cy[,cx2,cy2] 0..1 | nx,ny 0..1000 | x,y pts,
                        duration_ms, times, dx,dy} -> click/double/right/long/
                        swipe/scroll via CGEvent (ctypes, zero-dep)
    POST /type         {text, clear_existing?} -> CGEvent unicode keystrokes
    POST /key          {key} -> CGKeyCode map (ENTER/TAB/ESC/BACK/HOME/arrows...)
    POST /shell        {cmd} -> local shell, metachar-blocked like fleet.py
    POST /app          {action: launch|terminate, name} -> open -a / osascript
    GET  /hierarchy    AXUIElement tree of the frontmost app (ctypes AX bridge);
                       honest 501 when the bridge can't run

Trust model (spec A.7): binds loopback by default — never 0.0.0.0 unless
--host is passed for tailnet enrollment; when env MACAGENTD_TOKEN is set every
route requires `Authorization: Bearer <token>`.

Permissions are TCC-gated: Accessibility (input) + Screen Recording
(screenshots). They cannot be automated — /doctor reports state, a first real
action triggers the OS prompt. No stubs: anything unavailable answers 501.
"""

import argparse
import ctypes
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "0.1.0"
BIND_HOST = os.environ.get("MACAGENTD_HOST", "127.0.0.1")
PORT = int(os.environ.get("MACAGENTD_PORT", "7778"))
TOKEN = os.environ.get("MACAGENTD_TOKEN") or None
_STARTED = time.time()

logger = logging.getLogger("artemis.desktop.macagentd")

# ---------------------------------------------------------------------------
# Quartz / CoreFoundation / ApplicationServices bridge via ctypes.
# pyobjc (Quartz/AppKit) is preferred when importable; this box has none, so
# the same C APIs are reached through the system frameworks — still the real
# event path, still TCC-enforced. Everything degrades to an honest error.


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


_CG = _CF = _HIS = None
_bridge_err: str | None = None


def _load_bridge() -> bool:
    """Load the frameworks once; False (with _bridge_err) if unavailable."""
    global _CG, _CF, _HIS, _bridge_err
    if _CG is not None:
        return True
    try:
        _CG = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        _CF = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        _HIS = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework"
            "/Frameworks/HIServices.framework/HIServices"
        )
        vp, u32, u16, ul, b = (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint16,
            ctypes.c_ulong, ctypes.c_bool,
        )
        _CG.CGMainDisplayID.restype = u32
        _CG.CGDisplayBounds.restype = _CGRect
        _CG.CGDisplayBounds.argtypes = [u32]
        _CG.CGEventCreateMouseEvent.restype = vp
        _CG.CGEventCreateMouseEvent.argtypes = [vp, u32, _CGPoint, u32]
        _CG.CGEventCreateKeyboardEvent.restype = vp
        _CG.CGEventCreateKeyboardEvent.argtypes = [vp, u16, b]
        _CG.CGEventKeyboardSetUnicodeString.argtypes = [vp, ul, ctypes.POINTER(u16)]
        _CG.CGEventSetFlags.argtypes = [vp, ctypes.c_uint64]
        _CG.CGEventPost.argtypes = [u32, vp]
        _CG.CGEventCreateScrollWheelEvent.restype = vp
        # variadic in C — fixed args declared; wheel ints pass as extra args
        _CG.CGEventCreateScrollWheelEvent.argtypes = [vp, u32, u32]
        _CG.CGPreflightScreenCaptureAccess.restype = b
        _CG.CGRequestScreenCaptureAccess.restype = b
        _HIS.AXIsProcessTrusted.restype = b
        _HIS.AXIsProcessTrustedWithOptions.restype = b
        _HIS.AXIsProcessTrustedWithOptions.argtypes = [vp]
        _CF.CFRelease.argtypes = [vp]
        _CF.CFGetTypeID.argtypes = [vp]
        _CF.CFGetTypeID.restype = ul
        for fn in ("CFStringGetTypeID", "CFBooleanGetTypeID", "CFNumberGetTypeID",
                   "CFArrayGetTypeID"):
            getattr(_CF, fn).restype = ul
        _CF.CFStringCreateWithCString.restype = vp
        _CF.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
        _CF.CFStringGetCString.restype = b
        _CF.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, u32]
        _CF.CFStringGetLength.argtypes = [vp]
        _CF.CFStringGetLength.restype = ctypes.c_long
        _CF.CFBooleanGetValue.restype = b
        _CF.CFBooleanGetValue.argtypes = [vp]
        _CF.CFNumberGetValue.restype = b
        _CF.CFNumberGetValue.argtypes = [vp, ctypes.c_long, vp]
        _CF.CFArrayGetCount.argtypes = [vp]
        _CF.CFArrayGetCount.restype = ctypes.c_long
        _CF.CFArrayGetValueAtIndex.restype = vp
        _CF.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
        _CF.CFDictionaryCreate.restype = vp
        _CF.CFDictionaryCreate.argtypes = [
            vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        # AX element layer (ApplicationServices/HIServices)
        _HIS.AXUIElementCreateApplication.restype = vp
        _HIS.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
        _HIS.AXUIElementCopyAttributeValue.argtypes = [vp, vp, ctypes.POINTER(vp)]
        _HIS.AXUIElementGetTypeID.restype = ul
        _HIS.AXValueGetTypeID.restype = ul
        _HIS.AXValueGetType.argtypes = [vp]
        _HIS.AXValueGetType.restype = u32
        _HIS.AXValueGetValue.restype = b
        _HIS.AXValueGetValue.argtypes = [vp, u32, vp]
        return True
    except Exception as e:  # pragma: no cover - platform dependent
        _CG = _CF = _HIS = None
        _bridge_err = f"{type(e).__name__}: {e}"
        logger.warning("Quartz bridge unavailable: %s", _bridge_err)
        return False


def _cfstr(text: str):
    return _CF.CFStringCreateWithCString(None, text.encode(), 0x08000100)


def _cf_release(ref):
    if ref:
        _CF.CFRelease(ctypes.c_void_p(ref) if isinstance(ref, int) else ref)


# --- TCC permission probes ---------------------------------------------------


def ax_trusted(prompt: bool = False):
    """True/False, or None when the API can't be reached."""
    if not _load_bridge():
        return None
    if not prompt:
        return bool(_HIS.AXIsProcessTrusted())
    try:
        key = ctypes.c_void_p.in_dll(_HIS, "kAXTrustedCheckOptionPrompt")
        truth = ctypes.c_void_p.in_dll(_CF, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key.value)
        vals = (ctypes.c_void_p * 1)(truth.value)
        d = _CF.CFDictionaryCreate(None, keys, vals, 1, None, None)
        res = bool(_HIS.AXIsProcessTrustedWithOptions(d))
        _cf_release(d)
        return res
    except Exception as e:
        logger.warning("AXIsProcessTrustedWithOptions failed: %s", e)
        return None


def screen_recording_ok(prompt: bool = False):
    if not _load_bridge():
        return None
    try:
        if prompt:
            return bool(_CG.CGRequestScreenCaptureAccess())
        return bool(_CG.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


# --- screen geometry ---------------------------------------------------------


def screen_points() -> tuple[float, float]:
    """Main display size in CG points (the coordinate space CGEvent uses)."""
    if _load_bridge():
        b = _CG.CGDisplayBounds(_CG.CGMainDisplayID())
        if b.size.width > 0:
            return float(b.size.width), float(b.size.height)
    raise RuntimeError("cannot determine main-display bounds")


def screenshot(fmt: str = "png") -> tuple[bytes, str]:
    """screencapture(1) — zero-dep capture path. Returns (bytes, media_type)."""
    fmt = "jpg" if fmt in ("jpg", "jpeg") else "png"
    tmp = tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False)
    tmp.close()
    try:
        proc = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", fmt, tmp.name],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0 or not os.path.exists(tmp.name):
            raise RuntimeError(
                f"screencapture rc={proc.returncode} {proc.stderr.strip()[:200]}")
        data = open(tmp.name, "rb").read()
        if len(data) < 500:  # empty/blank capture — usually missing TCC grant
            raise RuntimeError(
                "screencapture produced an empty image — grant Screen "
                "Recording in System Settings > Privacy & Security")
        return data, ("image/jpeg" if fmt == "jpg" else "image/png")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# --- CGEvent input -----------------------------------------------------------

# CGEventTypes / buttons / flags
_EV = {"ldown": 1, "lup": 2, "rdown": 3, "rup": 4, "move": 5, "ldrag": 6, "rdrag": 7}
_BTN = {"left": 0, "right": 1}
_FLAG_CMD = 0x100000
_HID_TAP = 0  # kCGHIDEventTap

_PRESSABLE = {
    "AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXCheckBox",
    "AXRadioButton", "AXPopUpButton", "AXPopOver", "AXTab",
    "AXSegmentedControl", "AXDisclosureTriangle", "AXDockItem",
    "AXComboBox", "AXIncrementor", "AXSlider", "AXSwitch", "AXCell",
}
_TEXTABLE = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox",
             "AXSecureTextField", "AXTextEdit"}

# key name -> (CGKeyCode, flags). Phone names map to macOS semantics per spec
# A.6: BACK -> esc (53); HOME -> ⌘-space (spotlight); APP_SWITCH -> ⌘-tab.
_KEYMAP = {
    "ENTER": (36, 0), "RETURN": (36, 0), "TAB": (48, 0),
    "ESC": (53, 0), "ESCAPE": (53, 0), "BACK": (53, 0),
    "SPACE": (49, 0), "BACKSPACE": (51, 0), "DELETE": (51, 0),
    "FORWARD_DELETE": (117, 0),
    "HOME": (49, _FLAG_CMD), "SPOTLIGHT": (49, _FLAG_CMD),
    "END": (119, 0), "PAGE_UP": (116, 0), "PAGE_DOWN": (121, 0),
    "LEFT": (123, 0), "ARROW_LEFT": (123, 0),
    "RIGHT": (124, 0), "ARROW_RIGHT": (124, 0),
    "DOWN": (125, 0), "ARROW_DOWN": (125, 0),
    "UP": (126, 0), "ARROW_UP": (126, 0),
    "APP_SWITCH": (48, _FLAG_CMD), "F1": (122, 0), "F2": (120, 0),
    "F3": (99, 0), "F4": (118, 0), "F5": (96, 0), "F6": (97, 0),
    "F7": (98, 0), "F8": (100, 0), "F9": (101, 0), "F10": (109, 0),
    "F11": (103, 0), "F12": (111, 0),
}


def _post_event(ev):
    _CG.CGEventPost(_HID_TAP, ctypes.c_void_p(ev))
    _cf_release(ev)


def _mouse(kind: str, x: float, y: float, button: str = "left"):
    ev = _CG.CGEventCreateMouseEvent(
        None, _EV[kind], _CGPoint(x, y), _BTN[button])
    if not ev:
        raise RuntimeError("CGEventCreateMouseEvent failed")
    _post_event(ev)


def _click(x: float, y: float, button: str = "left", times: int = 1,
           hold_ms: int = 0):
    _mouse("move", x, y)
    time.sleep(0.02)
    dn, up = ("rdown", "rup") if button == "right" else ("ldown", "lup")
    for i in range(times):
        _mouse(dn, x, y, button)
        time.sleep(max(0.035, hold_ms / 1000.0))
        _mouse(up, x, y, button)
        if i < times - 1:
            time.sleep(0.08)


def _swipe(x1, y1, x2, y2, duration_ms: int):
    steps = max(6, min(24, duration_ms // 40))
    _mouse("move", x1, y1)
    _mouse("ldown", x1, y1)
    dt = max(0.012, (duration_ms / 1000.0) / steps)
    for i in range(1, steps + 1):
        _mouse("ldrag", x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        time.sleep(dt)
    _mouse("lup", x2, y2)


def _scroll(x, y, dx_lines: int, dy_lines: int):
    ev = _CG.CGEventCreateScrollWheelEvent(
        None, 1, 2, int(dy_lines), int(dx_lines))  # line units, 2 wheels
    if not ev:
        raise RuntimeError("CGEventCreateScrollWheelEvent failed")
    _mouse("move", x, y)
    _post_event(ev)


def _key_event(code: int, flags: int = 0):
    for down in (True, False):
        ev = _CG.CGEventCreateKeyboardEvent(None, code, down)
        if not ev:
            raise RuntimeError("CGEventCreateKeyboardEvent failed")
        if flags:
            _CG.CGEventSetFlags(ev, flags)
        _post_event(ev)
        time.sleep(0.02)


def _type_text(text: str, chunk: int = 24):
    """Type `text` into the focused field via unicode keyboard events."""
    i = 0
    while i < len(text):
        piece = text[i:i + chunk]
        raw = piece.encode("utf-16-le")
        units = struct.unpack(f"<{len(raw) // 2}H", raw)
        buf = (ctypes.c_uint16 * len(units))(*units)
        for down in (True, False):
            ev = _CG.CGEventCreateKeyboardEvent(None, 0, down)
            if not ev:
                raise RuntimeError("CGEventCreateKeyboardEvent failed")
            if down:
                _CG.CGEventKeyboardSetUnicodeString(ev, len(units), buf)
            _post_event(ev)
            time.sleep(0.015)
        i += chunk


def _cliclick(*args) -> bool:
    """cliclick fallback (installed at /opt/homebrew/bin/cliclick)."""
    exe = shutil.which("cliclick")
    if not exe:
        return False
    try:
        return subprocess.run([exe, *args], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


def _resolve_point(body: dict, suffix: str = "") -> tuple[float, float]:
    """Resolve one coordinate pair to CG points.

    Accepts cx/cy (0..1, fleet UI dialect), nx/ny (0..1000, actuator dialect),
    or x/y — points by default, or set "space": "norm"|"norm1000".
    """
    w, h = screen_points()
    cx, cy = body.get(f"cx{suffix}"), body.get(f"cy{suffix}")
    if cx is not None and cy is not None:
        return float(cx) * w, float(cy) * h
    nx, ny = body.get(f"nx{suffix}"), body.get(f"ny{suffix}")
    if nx is not None and ny is not None:
        return float(nx) / 1000.0 * w, float(ny) / 1000.0 * h
    x, y = body.get(f"x{suffix}"), body.get(f"y{suffix}")
    if x is not None and y is not None:
        x, y = float(x), float(y)
        space = body.get("space", "points")
        if space in ("norm", "normalized"):
            return x * w, y * h
        if space == "norm1000":
            return x / 1000.0 * w, y / 1000.0 * h
        return x, y
    raise ValueError(f"missing coordinates for point{suffix or ' 1'}")


def handle_input(body: dict) -> dict:
    action = (body.get("action") or "").lower()
    duration_ms = int(body.get("duration_ms", 300) or 300)
    times = int(body.get("times", 1) or 1)
    if not _load_bridge():
        raise RuntimeError(f"CGEvent bridge unavailable: {_bridge_err}")
    ax_trusted(prompt=True)  # first real input triggers the OS prompt (by design)

    p1 = _resolve_point(body)
    second = None
    try:
        second = _resolve_point(body, "2")
    except ValueError:
        pass
    if not action:
        action = "swipe" if second else "click"
    if action == "tap":
        action = "click"
    if action == "swipe" and second is None:
        raise ValueError("swipe needs a second point (cx2/cy2)")

    if action in ("click", "double", "right", "long"):
        hold = duration_ms if action == "long" else 0
        _click(*p1, button="right" if action == "right" else "left",
               times=2 if action == "double" else max(1, times), hold_ms=hold)
        return {"ok": True, "type": "tap" if action == "click" else action,
                "coords": [round(p1[0]), round(p1[1])]}
    if action == "swipe":
        _swipe(*p1, *second, duration_ms)
        return {"ok": True, "type": "swipe",
                "coords": [round(p1[0]), round(p1[1]),
                           round(second[0]), round(second[1])]}
    if action == "scroll":
        dy = body.get("dy", body.get("delta_y"))
        dx = body.get("dx", body.get("delta_x"))
        scale = 1.0 if body.get("unit") == "lines" else 1 / 40.0  # px -> lines
        dy_l = int(round(float(dy if dy is not None else -120) * scale))
        dx_l = int(round(float(dx or 0) * scale))
        _scroll(*p1, dx_l, dy_l)
        return {"ok": True, "type": "scroll",
                "coords": [round(p1[0]), round(p1[1])],
                "wheel": [dx_l, dy_l]}
    raise ValueError(f"unknown action {action!r}")


def handle_key(body: dict) -> dict:
    name = str(body.get("key", "")).upper()
    if name == "POWER":
        return {"ok": False, "key": name, "status": 501,
                "err": "desktop hosts are not power-managed by the fleet"}
    if name in ("VOLUME_UP", "VOLUME_DOWN", "MUTE"):
        # no CGKeyCode — real volume control via osascript
        delta = {"VOLUME_UP": "+10", "VOLUME_DOWN": "-10"}.get(name)
        if delta:
            script = ("set volume output volume "
                      f"(output volume of (get volume settings) {delta})")
        else:
            script = ("set volume output muted "
                      "(not output muted of (get volume settings))")
        rc, out, err = _run_osascript(script)
        return {"ok": rc == 0, "key": name, "err": err}
    entry = _KEYMAP.get(name)
    if entry is None:
        raise ValueError(
            f"unknown key {name!r}; allowed: {sorted(_KEYMAP)} "
            "+ VOLUME_UP/VOLUME_DOWN/MUTE")
    if not _load_bridge():
        raise RuntimeError(f"CGEvent bridge unavailable: {_bridge_err}")
    ax_trusted(prompt=True)
    code, flags = entry
    _key_event(code, flags)
    return {"ok": True, "key": name}


def handle_type(body: dict) -> dict:
    text = str(body.get("text", ""))
    if not text:
        raise ValueError("empty text")
    if not _load_bridge():
        raise RuntimeError(f"CGEvent bridge unavailable: {_bridge_err}")
    ax_trusted(prompt=True)
    if body.get("clear_existing"):
        _key_event(0, _FLAG_CMD)   # ⌘A
        time.sleep(0.05)
        _key_event(51)             # delete
        time.sleep(0.05)
    _type_text(text)
    return {"ok": True, "type": "type", "chars": len(text)}


def handle_shell(body: dict) -> dict:
    cmd = str(body.get("cmd", "")).strip()
    if not cmd or any(c in cmd for c in ("|", ";", "&", ">", "<", "`", "$(")):
        raise ValueError("single shell command, no shell metachars")
    proc = subprocess.run(
        ["/bin/zsh", "-c", cmd], capture_output=True, text=True,
        timeout=int(body.get("timeout_s", 20) or 20),
    )
    return {"ok": proc.returncode == 0, "rc": proc.returncode,
            "out": proc.stdout.strip()[:4000], "err": proc.stderr.strip()[:2000]}


def _run_osascript(script: str, timeout: int = 15) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True,
        text=True, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def handle_app(body: dict) -> dict:
    action = (body.get("action") or "launch").lower()
    name = str(body.get("name", "")).strip()
    if not name:
        raise ValueError("missing app name")
    if action in ("launch", "open", "activate"):
        if body.get("bundle_id"):
            argv = ["open", "-b", name]
        elif name.startswith("/") or name.endswith(".app"):
            argv = ["open", name]
        elif re.match(r"^[a-z]+://", name):
            argv = ["open", name]  # URL
        else:
            argv = ["open", "-a", name]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return {"ok": proc.returncode == 0, "action": "launch", "name": name,
                "err": proc.stderr.strip()[:400]}
    if action in ("terminate", "quit", "stop"):
        rc, _, err = _run_osascript(f'tell application "{name}" to quit')
        if rc != 0:
            proc = subprocess.run(["killall", name], capture_output=True,
                                  text=True, timeout=10)
            if proc.returncode == 0:
                return {"ok": True, "action": "terminate", "name": name,
                        "err": "quit via killall"}
            return {"ok": False, "action": "terminate", "name": name,
                    "err": (err or proc.stderr.strip())[:400]}
        return {"ok": True, "action": "terminate", "name": name}
    raise ValueError(f"unknown action {action!r}; launch|terminate")


# --- AX element layer (uiautomator analog, spec A.2) -------------------------

_AX_MAX_NODES = 400
_AX_MAX_DEPTH = 10
_kCFNumberDoubleType = 13
_kAXPoint, _kAXSize, _kAXRect = 1, 2, 3


def _ax_attr(el, name: str):
    """Copy an attribute value; returns retained ref or None."""
    ref = ctypes.c_void_p()
    key = _cfstr(name)
    rc = _HIS.AXUIElementCopyAttributeValue(el, key, ctypes.byref(ref))
    _cf_release(key)
    return ref if rc == 0 and ref.value else None


def _ax_string(el, name: str) -> str | None:
    ref = _ax_attr(el, name)
    if not ref:
        return None
    try:
        if _CF.CFGetTypeID(ref) != _CF.CFStringGetTypeID():
            return None
        n = _CF.CFStringGetLength(ref)
        buf = ctypes.create_string_buffer(min(4096, n * 4 + 4))
        if _CF.CFStringGetCString(ref, buf, len(buf), 0x08000100):
            return buf.value.decode("utf-8", "replace")[:400]
        return None
    finally:
        _cf_release(ref)


def _ax_bool(el, name: str) -> bool | None:
    ref = _ax_attr(el, name)
    if not ref:
        return None
    try:
        tid = _CF.CFGetTypeID(ref)
        if tid == _CF.CFBooleanGetTypeID():
            return bool(_CF.CFBooleanGetValue(ref))
        if tid == _CF.CFNumberGetTypeID():
            v = ctypes.c_double(0)
            _CF.CFNumberGetValue(ref, _kCFNumberDoubleType, ctypes.byref(v))
            return bool(v.value)
        return None
    finally:
        _cf_release(ref)


def _ax_axvalue(el, name: str):
    """AXValue-wrapped CGPoint/CGSize/CGRect -> tuple or None."""
    ref = _ax_attr(el, name)
    if not ref:
        return None
    try:
        if not hasattr(_HIS, "AXValueGetType") or _CF.CFGetTypeID(ref) != _HIS.AXValueGetTypeID():
            return None
        t = _HIS.AXValueGetType(ref)
        if t == _kAXPoint:
            v = _CGPoint()
            if _HIS.AXValueGetValue(ref, t, ctypes.byref(v)):
                return (v.x, v.y)
        elif t == _kAXSize:
            v = _CGSize()
            if _HIS.AXValueGetValue(ref, t, ctypes.byref(v)):
                return (v.width, v.height)
        elif t == _kAXRect:
            v = _CGRect()
            if _HIS.AXValueGetValue(ref, t, ctypes.byref(v)):
                return (v.origin.x, v.origin.y, v.size.width, v.size.height)
        return None
    finally:
        _cf_release(ref)


def _ax_each_child(el, fn):
    """Copy the AXChildren array and run fn(kid) per element while the array
    is still retained — kids are owned by it, so releasing first is a
    use-after-free (that's what segfaulted v1 of this walk)."""
    ref = _ax_attr(el, "AXChildren")
    if not ref:
        return
    try:
        if _CF.CFGetTypeID(ref) != _CF.CFArrayGetTypeID():
            return
        n = min(_CF.CFArrayGetCount(ref), 200)
        for i in range(n):
            kid = _CF.CFArrayGetValueAtIndex(ref, i)
            if kid and fn(kid) is False:
                break
    finally:
        _cf_release(ref)


def _frontmost_pid() -> int | None:
    rc, out, _ = _run_osascript(
        'tell application "System Events" to get unix id of '
        '(first process whose frontmost is true)')
    if rc == 0 and out.strip().isdigit():
        return int(out.strip())
    try:  # lsappinfo fallback — no special grants needed
        front = subprocess.run(["/usr/bin/lsappinfo", "front"],
                               capture_output=True, text=True, timeout=5)
        m = re.search(r"ASN:(0x[0-9a-fA-Fx-]+)", front.stdout)
        if m:
            info = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "pid", m.group(1)],
                                  capture_output=True, text=True, timeout=5)
            pm = re.search(r'"pid"\s*=\s*(\d+)', info.stdout)
            if pm:
                return int(pm.group(1))
    except Exception:
        pass
    return None


def ax_hierarchy() -> dict:
    """Frontmost app's AX tree as uiautomator-shaped element dicts (spec A.2)."""
    if not _load_bridge():
        return {"ok": False, "status": 501,
                "error": f"AX bridge unavailable: {_bridge_err}"}
    if ax_trusted() is not True:
        return {"ok": False, "status": 503,
                "error": "accessibility not granted — approve the TCC prompt "
                         "(System Settings > Privacy & Security > Accessibility)"}
    pid = _frontmost_pid()
    if not pid:
        return {"ok": False, "status": 503, "error": "no frontmost app pid"}
    app = _HIS.AXUIElementCreateApplication(pid)
    if not app:
        return {"ok": False, "status": 503, "error": "AXUIElementCreateApplication failed"}
    elements: list[dict] = []
    truncated = False

    def walk(el, depth: int):
        nonlocal truncated
        if len(elements) >= _AX_MAX_NODES:
            truncated = True
            return False
        role = _ax_string(el, "AXRole") or "AXUnknown"
        title = _ax_string(el, "AXTitle")
        desc = _ax_string(el, "AXDescription")
        value = _ax_string(el, "AXValue")
        pos = _ax_axvalue(el, "AXPosition")
        size = _ax_axvalue(el, "AXSize")
        focused = _ax_bool(el, "AXFocused")
        enabled = _ax_bool(el, "AXEnabled")
        node = {
            "class": role,
            "text": title or (value if isinstance(value, str) else None),
            "content-desc": desc,
            "focused": bool(focused),
            "enabled": True if enabled is None else bool(enabled),
            "clickable": role in _PRESSABLE,
            "focusable": role in _TEXTABLE or bool(focused),
        }
        if pos and size:
            l, t = pos
            w_, h_ = size
            node["bounds"] = f"[{int(l)},{int(t)}][{int(l + w_)},{int(t + h_)}]"
            node["rect"] = [int(l), int(t), int(w_), int(h_)]
        elements.append(node)
        if depth < _AX_MAX_DEPTH:
            _ax_each_child(el, lambda kid: walk(kid, depth + 1))
        return not truncated

    try:
        _ax_each_child(app, lambda w_: walk(w_, 0))  # app children are windows
    finally:
        _cf_release(app)
    return {"ok": True, "platform": "mac", "app_pid": pid,
            "count": len(elements), "truncated": truncated,
            "elements": elements}


# --- HTTP surface ------------------------------------------------------------


def _doctor() -> dict:
    ax, sr = ax_trusted(), screen_recording_ok()
    return {
        "ok": True,
        "accessibility": ax,
        "screen_recording": sr,
        "pyobjc": False,
        "bridge": "ctypes" if _CG is not None else f"unavailable: {_bridge_err}",
        "cliclick": shutil.which("cliclick"),
        "grant_instructions": {
            "accessibility": "System Settings > Privacy & Security > "
                             "Accessibility — enable the process running "
                             "macagentd (Terminal/Python/launcher).",
            "screen_recording": "System Settings > Privacy & Security > "
                                "Screen Recording — same toggle list.",
        },
        "note": "grants cannot be automated; pass ?prompt=1 to trigger the "
                "OS prompts now, or let the first real action trigger them.",
    }


class AgentdHandler(BaseHTTPRequestHandler):
    server_version = f"macagentd/{VERSION}"

    # -- plumbing --
    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def _json(self, obj: dict, status: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self._json({"ok": False, "error": "unauthorized"}, 401)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 1_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _call(self, fn, body: dict):
        """Run a handler fn -> dict; {status:N} inside becomes the HTTP code."""
        try:
            res = fn(body)
            status = int(res.pop("status", 200))
            self._json(res, status)
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except subprocess.TimeoutExpired:
            self._json({"ok": False, "error": "timeout"}, 504)
        except Exception as e:
            logger.exception("handler failed")
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    # -- routes --
    def do_GET(self):
        if not self._authorized():
            return self._deny()
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        if path == "/health":
            try:
                w, h = screen_points()
                scr = {"w": w, "h": h, "unit": "points"}
            except Exception:
                scr = None
            return self._json({
                "ok": True, "agent": "macagentd", "version": VERSION,
                "platform": "mac", "pid": os.getpid(),
                "uptime_s": round(time.time() - _STARTED, 1),
                "bind": f"{self.server.server_address[0]}:{self.server.server_address[1]}",
                "token_required": bool(TOKEN),
                "screen": scr,
                "permissions": {"accessibility": ax_trusted(),
                                "screen_recording": screen_recording_ok()},
                "capabilities": ["screenshot", "input", "type", "key", "shell",
                                 "hierarchy", "app", "stream", "doctor"],
                "pyobjc": False,
            })
        if path == "/doctor":
            d = _doctor()
            if q.get("prompt") == ["1"]:
                d["prompted"] = True
                d["accessibility"] = ax_trusted(prompt=True)
                d["screen_recording"] = screen_recording_ok(prompt=True)
            return self._json(d)
        if path in ("/screenshot", "/frame"):
            try:
                data, mt = screenshot(q.get("format", ["png"])[0])
                self.send_response(200)
                self.send_header("Content-Type", mt)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 503)
            return
        if path == "/stream":
            return self._mjpeg()
        if path in ("/hierarchy", "/elements"):
            res = ax_hierarchy()
            return self._json(res, int(res.pop("status", 200)) if not res.get("ok") else 200)
        return self._json({"ok": False, "error": f"unknown route {path}"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            body = self._body()
        except Exception as e:
            return self._json({"ok": False, "error": f"bad json: {e}"}, 400)
        routes = {
            "/input": handle_input, "/type": handle_type, "/key": handle_key,
            "/shell": handle_shell, "/app": handle_app,
        }
        if path in routes:
            return self._call(routes[path], body)
        return self._json({"ok": False, "error": f"unknown route {path}"}, 404)

    def _mjpeg(self, interval: float = 0.4):
        """Periodic-screenshot MJPEG, same boundary/shape as fleet_stream_service."""
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        except Exception:
            return
        while True:
            try:
                data, _ = screenshot("png")
            except Exception as e:
                logger.warning("stream frame failed: %s", e)
                time.sleep(1.0)
                continue
            try:
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/png\r\nContent-Length: "
                    + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="macagentd",
        description="Artemis macOS desktop-control agent (kind:mac)")
    ap.add_argument("--host", default=BIND_HOST,
                    help="bind address — default 127.0.0.1; use the tailnet IP "
                         "for remote enrollment, never 0.0.0.0 on a LAN")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.host != "127.0.0.1" and not TOKEN:
        logger.warning("non-loopback bind without MACAGENTD_TOKEN — set a token")
    _load_bridge()
    ax, sr = ax_trusted(), screen_recording_ok()
    logger.info("permissions: accessibility=%s screen_recording=%s", ax, sr)
    srv = ThreadingHTTPServer((args.host, args.port), AgentdHandler)
    logger.info("macagentd %s on http://%s:%d", VERSION, args.host, args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
