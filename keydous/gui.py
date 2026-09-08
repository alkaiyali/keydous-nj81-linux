#!/usr/bin/env python3
"""Local web UI for the Keydous NJ81.

The page is deliberately served by the driver instead of relying on a
framework or a separate desktop runtime.  The browser handles layout and
image preview; this module owns all device writes and validation.
"""

import argparse
import base64
import json
import os
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import follow, keyconfig, protocol
from .follow import FollowError, FollowSession
from .rawusb import RawUsbKeyboard, UsbBusError

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(DATA_DIR, "gui.html")
DEFAULT_PORT = 8765


def _load_json(name):
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


LAYOUT = _load_json("nj81_layout.json")
HID_NAMES = _load_json("hid_key_names.json")
DEFAULT_MATRIX = _load_json("nj81_matrix.json")

# Map a physical-layout HID value to the firmware matrix slot.
POSITION_BY_HID = {}
_SPECIAL = {167837696: [10, 1, 0, 0]}
for pos in range(protocol.Matrix.MATRIX_SIZE):
    entry = DEFAULT_MATRIX[pos * 4:pos * 4 + 4]
    if entry[0] == 0 and entry[2]:
        POSITION_BY_HID[entry[2]] = pos
    for hid, cfg in _SPECIAL.items():
        if entry == cfg:
            POSITION_BY_HID[hid] = pos


# The high nibble is the mode option.  The low nibble is supplied by the
# protocol layer: 7 for exact RGB, 4 for music modes, and 0 for triggers.
LIGHT_OPTIONS = {
    "wave": [
        {"value": 0, "label": "Right"},
        {"value": 16, "label": "Left"},
        {"value": 32, "label": "Down"},
        {"value": 48, "label": "Up"},
    ],
    "snake": [{"value": 0, "label": "Z"},
               {"value": 16, "label": "Return"}],
    "kaleidoscope": [{"value": 0, "label": "Out"},
                      {"value": 16, "label": "In"}],
    "line_wave": [{"value": 0, "label": "Right"},
                   {"value": 16, "label": "Left"}],
    "user_picture": [{"value": 0, "label": "Picture 1"},
                      {"value": 16, "label": "Picture 2"},
                      {"value": 32, "label": "Picture 3"},
                      {"value": 48, "label": "Picture 4"},
                      {"value": 64, "label": "Picture 5"}],
    "circle_wave": [{"value": 0, "label": "Anti-clockwise"},
                    {"value": 16, "label": "Clockwise"}],
    "fireworks": [{"value": 0, "label": "Right"},
                   {"value": 16, "label": "Left"}],
    "music_follow3": [{"value": 4, "label": "Upright"},
                       {"value": 20, "label": "Separate"},
                       {"value": 36, "label": "Intersect"}],
    "music_follow2": [{"value": 4, "label": "Upright"},
                       {"value": 20, "label": "Separate"},
                       {"value": 36, "label": "Intersect"}],
}


def _open_dev():
    """Return a raw USB NJ-family device, preferring direct keyboard PIDs.

    Order matters: wired personalities first (0x4010/0x4007), then dongles
    (0x4011/0x4015/...). All PIDs are from devices.py, which mirrors the
    vendor SupportVender table (re-verified 2026-08).
    """
    from .devices import all_usb_pids

    errors = []
    for pid in (0x4010, 0x4007) + tuple(p for p in all_usb_pids()
                                        if p not in (0x4010, 0x4007)):
        try:
            return RawUsbKeyboard(pid=pid)
        except UsbBusError as exc:
            errors.append(str(exc))
    raise UsbBusError("no supported Keydous USB keyboard found")


# One device handle for the whole GUI session, serialized with a lock so
# the follow streams (music/screen) can share it safely.
_DEV_LOCK = threading.Lock()
_SHARED = {"dev": None}


@contextmanager
def _device():
    with _DEV_LOCK:
        if _SHARED["dev"] is None:
            dev = _open_dev()
            dev.open()
            _SHARED["dev"] = dev
        yield _SHARED["dev"]


def _follow_send(payload):
    with _DEV_LOCK:
        dev = _SHARED["dev"]
        if dev is None:
            dev = _open_dev()
            dev.open()
            _SHARED["dev"] = dev
        dev.send_feature(payload)


_FOLLOW = FollowSession(_follow_send)


def _close_device():
    with _DEV_LOCK:
        if _SHARED["dev"] is not None:
            try:
                _SHARED["dev"].close()
            except Exception:
                pass
            _SHARED["dev"] = None


def _light_view(light):
    mode = int(light["mode"])
    name = next((n for n, value in protocol.LED_MODES.items() if value == mode),
                "unknown")
    param = int(light["param"])
    options = LIGHT_OPTIONS.get(name, [])
    option = None
    for item in options:
        if item["value"] == param:
            option = item
            break
        # Color-capable modes store the option in the high nibble and use
        # 7 as the exact-RGB selector in the low nibble.
        if item["value"] == (param & 0xF0):
            option = item
            break
    return dict(light, name=name, option=option,
                dazzle=bool(light.get("dazzle")))


# Preset colors matching the vendor COMMONCOLOR palette.
LIGHT_PRESETS = [
    {"label": "Red", "hex": "#ff0000"},
    {"label": "Green", "hex": "#00ff00"},
    {"label": "Blue", "hex": "#0000ff"},
    {"label": "Orange", "hex": "#ff8000"},
    {"label": "Magenta", "hex": "#ff00ff"},
    {"label": "Yellow", "hex": "#ffff00"},
    {"label": "White", "hex": "#ffffff"},
]


_screen_cache = {"at": 0.0, "present": None}


def _screen_available():
    """Probe the RGB565 screen at most once every 30 seconds."""
    now = time.time()
    if _screen_cache["present"] is not None and now - _screen_cache["at"] < 30:
        return _screen_cache["present"]
    present = False
    try:
        with _device() as dev:
            present = protocol.Screen(dev).probe(size=0, box=(0, 0, 0, 0),
                                                 tries=3)
    except Exception:
        present = False
    _screen_cache.update(at=now, present=present)
    return present


def _status():
    with _device() as dev:
        kb = protocol.Keyboard(dev)
        batt = _battery_summary(kb)
        pct, battery_status = batt["percent"], batt["status"]
        # The wired interface reports 0% while charging on USB (the real
        # level comes from the wireless link); hide the misleading 0%.
        if pct == 0 and battery_status == "charging":
            battery_status = "charging on USB (level unknown while wired)"
        return {
            "connected": True,
            "firmware": kb.firmware_version(),
            "battery": pct,
            "battery_status": battery_status,
            "battery_lp": batt.get("battery_lp"),
            "dongle": batt.get("dongle"),
            "profile": kb.profile(),
            "report_rate": kb.report_rate(),
            "options": kb.keyboard_option(),
            "light": _light_view(kb.get_light()),
            # Do not probe the screen during periodic status refreshes. The
            # vendor probe changes screen-controller state and is only safe
            # immediately before an upload.
            "screen": _screen_cache["present"],
            "follow": _FOLLOW.status(),
        }


def _matrix(body):
    fn = bool(body.get("fn"))
    profile = int(body.get("profile", 0))
    with _device() as dev:
        kb = protocol.Keyboard(dev)
        if "profile" not in body:
            profile = kb.profile()
        matrix = protocol.Matrix(dev)
        raw = matrix.read_fn(profile) if fn else matrix.read(profile)
        # The base layer is fully writable. In the Fn view, report the
        # positions that hold native shortcuts so the UI can warn before
        # they are overwritten (no lock).
        reserved = ([pos for pos, cfg in enumerate(raw) if any(cfg)]
                    if fn else [])
    return {
        "profile": profile,
        "fn": fn,
        "reserved": reserved,
        "entries": [
            {"position": pos, "cfg": cfg,
             "name": keyconfig.decode(cfg, HID_NAMES)}
            for pos, cfg in enumerate(raw)
        ],
    }


def _validate_cfg(body):
    cfg = [int(value) for value in body["cfg"]]
    if len(cfg) != 4 or any(value < 0 or value > 255 for value in cfg):
        raise ValueError("cfg must contain four bytes")
    return cfg


def _set_key(body):
    position = int(body["position"])
    profile = int(body.get("profile", 0))
    if not 0 <= position < protocol.Matrix.MATRIX_SIZE:
        raise ValueError("invalid matrix position")
    fn = bool(body.get("fn"))
    warning = None
    if body.get("reset"):
        if fn:
            # The Fn factory defaults live in the firmware; there is no
            # vendor default-Fn matrix to restore from. Writing the base
            # layer default here would silently clobber the Fn shortcut.
            raise ValueError(
                "Fn-layer reset is disabled: the Fn factory defaults are "
                "stored in firmware (use the hardware factory reset)")
        cfg = DEFAULT_MATRIX[position * 4:position * 4 + 4]
    else:
        cfg = _validate_cfg(body)
    with _device() as dev:
        matrix = protocol.Matrix(dev)
        if fn:
            if any(matrix.read_fn(profile)[position]):
                warning = "overwrote a native Fn shortcut"
            matrix.set_fn_key(position, cfg, profile)
        else:
            matrix.set_key(position, cfg, profile)
    return {"ok": True, "warning": warning}


def _light_param(name, body):
    if body.get("param") is not None:
        return int(body["param"])
    if body.get("option") is None or name not in LIGHT_OPTIONS:
        return protocol.YC500_PARAM[protocol.LED_MODES[name]]
    option = int(body["option"])
    if not any(item["value"] == option for item in LIGHT_OPTIONS[name]):
        raise ValueError("invalid effect option")
    if name == "user_picture" or name.startswith("music_follow"):
        return option
    return (protocol.YC500_PARAM[protocol.LED_MODES[name]] & 0x0F) | option


def _set_light(body):
    name = str(body["mode"])
    mode = protocol.LED_MODES.get(name)
    if mode is None:
        return {"ok": False, "error": f"unknown mode {name}"}
    # Leaving a follow mode stops the stream (music/screen capture runs
    # only while its mode is active).
    if name not in ("music_follow2", "music_follow3", "screen_color"):
        if _FOLLOW.running:
            _FOLLOW.stop()
    speed = max(0, min(4, int(body.get("speed", 0))))
    value = max(0, min(4, int(body.get("value", 4))))
    rgb = int(body.get("rgb", 0xFFFFFF))
    if not 0 <= rgb <= 0xFFFFFF:
        raise ValueError("rgb must be a 24-bit color")
    param = _light_param(name, body)
    dazzle = bool(body.get("dazzle")) and mode in protocol.LED_DAZZLE_MODES
    with _device() as dev:
        kb = protocol.Keyboard(dev)
        kb.set_light(mode, speed=speed, value=value, param=param,
                     r=(rgb >> 16) & 0xFF, g=(rgb >> 8) & 0xFF,
                     b=rgb & 0xFF, dazzle=dazzle)
        light = _light_view(kb.get_light())
    return {"ok": True, "light": light}


def _userpic_read(body):
    """Read one per-key picture layer as 128 [r, g, b] triples."""
    layer = int(body.get("layer", 0))
    with _device() as dev:
        pixels = protocol.UserPicture(dev).read_pixels(layer)
    return {"ok": True, "layer": layer,
            "pixels": [[r, g, b] for r, g, b in pixels]}


def _set_picture(body):
    layer = int(body.get("layer", 0))
    if not 0 <= layer < protocol.UserPicture.LAYERS:
        raise ValueError("picture layer must be 0..4")
    raw_pixels = body.get("pixels")
    if not isinstance(raw_pixels, list) or not raw_pixels:
        raise ValueError("pixels must be a non-empty list")
    pixels = {}
    for item in raw_pixels:
        position = int(item["position"])
        if not 0 <= position < protocol.UserPicture.POSITIONS:
            raise ValueError("invalid picture position")
        pixels[position] = tuple(
            max(0, min(255, int(item[channel])))
            for channel in ("r", "g", "b"))
    with _device() as dev:
        protocol.UserPicture(dev).set_pixels(
            layer, ((position, *rgb) for position, rgb in pixels.items()))
        # Activate the image only after all pixels have been written.
        protocol.Keyboard(dev).set_light(
            protocol.LED_MODES["user_picture"], speed=0, value=4,
            param=layer * 16, r=0, g=0, b=0)
    return {"ok": True, "layer": layer, "pixels": len(pixels)}


def _set_screen(body):
    """Upload a column-major RGB565 image to a 160x80 screen layer.

    After the upload the screen enters custom-image view through LED mode
    13 (user_picture) with ``param = layer * 16``."""
    raw = body.get("image")
    box = body.get("box")
    layer = int(body.get("layer", 0))
    if not 0 <= layer < 5:
        raise ValueError("screen layer must be 0..4")
    if not isinstance(raw, str) or not raw:
        raise ValueError("image must be base64 RGB565 data")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("box must be [left, top, right, bottom]")
    box = tuple(int(value) for value in box)
    pixels = base64.b64decode(raw)
    with _device() as dev:
        protocol.Screen(dev).upload_rgb565(
            pixels, frame=layer, total_frames=1,
            delay=int(body.get("delay", 0)), box=box)
        protocol.Keyboard(dev).set_light(
            protocol.LED_MODES["user_picture"], speed=0, value=4,
            param=layer * 16, r=0, g=0, b=0)
    return {"ok": True, "bytes": len(pixels), "box": list(box),
            "layer": layer}


def _set_rate(body):
    hz = int(body["hz"])
    if hz not in (125, 250, 500, 1000):
        raise ValueError("report rate must be 125, 250, 500, or 1000 Hz")
    with _device() as dev:
        protocol.Keyboard(dev).set_report_rate(hz)
    return {"ok": True, "report_rate": hz}


def _set_option(body):
    kw = {key: int(value) for key, value in body.items() if key != "action"}
    with _device() as dev:
        protocol.Keyboard(dev).set_keyboard_option(**kw)
    return {"ok": True}


def _reset():
    with _device() as dev:
        protocol.Keyboard(dev).reset()
    # The vendor UI waits for the firmware to rewrite its defaults before
    # reloading state.  Do the same so the next status read is meaningful.
    time.sleep(5)
    return {"ok": True}


def _set_follow(body):
    action = str(body.get("action", ""))
    if action == "start":
        kind = str(body.get("kind", ""))
        _FOLLOW.start(kind)
    elif action == "stop":
        _FOLLOW.stop()
    elif action:
        raise ValueError("action must be start or stop")
    return _FOLLOW.status()


def _set_profile(body):
    profile = int(body.get("profile", 0))
    if not 0 <= profile <= 5:
        raise ValueError("profile must be 0..5")
    with _device() as dev:
        protocol.Keyboard(dev).set_profile(profile)
    return {"ok": True, "profile": profile}


def _power(body):
    with _device() as dev:
        kb = protocol.Keyboard(dev)
        if body.get("action") == "set":
            updates = {}
            if "debounce" in body:
                updates["debounce"] = kb.set_debounce(int(body["debounce"]))
            sleep = {}
            for key in ("time_bt", "time_24", "deep_bt", "deep_24"):
                if key in body:
                    sleep[key] = int(body[key])
            if sleep:
                cur = kb.sleep_time()
                updates["sleep"] = kb.set_sleep_time(
                    time_bt=sleep.get("time_bt", cur[0]),
                    time_24=sleep.get("time_24", cur[1]),
                    deep_bt=sleep.get("deep_bt", cur[2]),
                    deep_24=sleep.get("deep_24", cur[3]))
            if "battery_lp" in body:
                updates["battery_lp"] = kb.set_battery_lp(int(body["battery_lp"]))
            return {"ok": True, "updated": updates}
        return {
            "debounce": kb.debounce(),
            "sleep": list(kb.sleep_time()),
            "battery_lp": kb.battery_lp(),
        }


_battery_cache = {"at": 0.0, "value": None}


def _battery_summary(kb):
    """Wired battery plus, when present, the 2.4G dongle's own read."""
    now = time.time()
    if now - _battery_cache["at"] < 30 and _battery_cache["value"]:
        return _battery_cache["value"]
    pct, status = kb.battery()
    battery_lp = None
    try:
        battery_lp = kb.battery_lp()
    except Exception:
        pass
    dongle = None
    try:
        d = RawUsbKeyboard(pid=0x4011)
        d.open()
        try:
            r = d.command(protocol.Cmd.GET_BATTERY)
            dongle = (r[1], {1: "charging", 2: "full"}.get(r[2], "not charging"))
        finally:
            d.close()
    except Exception:
        dongle = None
    value = {
        "percent": pct,
        "status": status,
        "battery_lp": battery_lp,
        "dongle": dongle,
    }
    _battery_cache.update(at=now, value=value)
    return value


def _backup(body):
    with _device() as dev:
        kb = protocol.Keyboard(dev)
        matrix = protocol.Matrix(dev)
        light = _light_view(kb.get_light())
        data = {
            "device": "Keydous NJ81",
            "firmware": kb.firmware_version(),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "profiles": [],
            "light": light,
            "options": kb.keyboard_option(),
            "report_rate": kb.report_rate(),
            "debounce": kb.debounce(),
            "sleep": list(kb.sleep_time()),
            "battery_lp": kb.battery_lp(),
            "pictures": {},
        }
        for p in range(6):
            data["profiles"].append({
                "profile": p,
                "base": matrix.read(p),
                "fn": matrix.read_fn(p),
            })
        pic = protocol.UserPicture(dev)
        for layer in range(protocol.UserPicture.LAYERS):
            data["pictures"][layer] = pic.read_pixels(layer)
    return data


def _restore(body):
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise ValueError("backup data missing")
    with _device() as dev:
        matrix = protocol.Matrix(dev)
        kb = protocol.Keyboard(dev)
        for prof in data["profiles"]:
            p = int(prof["profile"])
            if not 0 <= p <= 5:
                continue
            base = prof.get("base")
            fn = prof.get("fn")
            if isinstance(base, list) and len(base) == matrix.MATRIX_SIZE:
                for pos, cfg in enumerate(base):
                    if any(cfg):
                        matrix.set_key(pos, list(cfg[:4]), p)
            if isinstance(fn, list) and len(fn) == matrix.MATRIX_SIZE:
                for pos, cfg in enumerate(fn):
                    if any(cfg):
                        matrix.set_fn_key(pos, list(cfg[:4]), p)
        light = data.get("light") or {}
        name = light.get("name")
        if name in protocol.LED_MODES:
            rgb = light.get("rgb", 0xFFFFFF)
            kb.set_light(
                protocol.LED_MODES[name],
                speed=light.get("speed", 0), value=light.get("value", 4),
                r=(rgb >> 16) & 0xFF, g=(rgb >> 8) & 0xFF, b=rgb & 0xFF,
                dazzle=bool(light.get("dazzle")))
        if "debounce" in data:
            kb.set_debounce(int(data["debounce"]))
        if isinstance(data.get("sleep"), list) and len(data["sleep"]) == 4:
            kb.set_sleep_time(*(int(x) for x in data["sleep"]))
        if isinstance(data.get("pictures"), dict):
            pic = protocol.UserPicture(dev)
            for layer, pixels in data["pictures"].items():
                if isinstance(pixels, list):
                    for pos, rgb in enumerate(pixels[:128]):
                        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
                            pic.set_pixel(int(layer), pos, *rgb)
        if "options" in data:
            opts = data["options"]
            kw = {k: opts[k] for k in (
                "win_key_lock", "system", "wasd_arrow_exchange", "led_off",
                "s_led_off", "keyboard_mode", "keyboard_lock")
                if k in opts}
            kb.set_keyboard_option(**{k: int(bool(v)) for k, v in kw.items()})
        if "report_rate" in data:
            kb.set_report_rate(int(data["report_rate"]))
    return {"ok": True}


def _set_bt_battery(body):
    """Read battery over Bluetooth for a paired Keydous device."""
    address = str(body.get("address", "")).strip()
    if not address:
        raise ValueError("bluetooth address required")
    from .ble import BLEKeyboard
    kb = BLEKeyboard(address)
    try:
        r = kb.command(protocol.Cmd.GET_BATTERY)
    finally:
        kb.close()
    return {"percent": r[1],
            "status": {1: "charging", 2: "full"}.get(r[2], "not charging")}


HANDLERS = {
    "status": lambda body: _status(),
    "matrix": _matrix,
    "key": _set_key,
    "light": _set_light,
    "picture": _set_picture,
    "userpic": _userpic_read,
    "screen": _set_screen,
    "rate": _set_rate,
    "option": _set_option,
    "follow": _set_follow,
    "profile": _set_profile,
    "power": _power,
    "backup": _backup,
    "restore": _restore,
    "btbattery": _set_bt_battery,
    "reset": lambda body: _reset(),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = bytes(body)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, _PAGE, "text/html; charset=utf-8")
            return
        if not parsed.path.startswith("/api/"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        name = parsed.path[len("/api/"):]
        params = parse_qs(parsed.query)
        body = {
            "fn": params.get("fn", ["0"])[0] in ("1", "true", "on"),
        }
        if "profile" in params:
            body["profile"] = int(params["profile"][0])
        self._dispatch(name, body)

    def do_POST(self):
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        name = parsed.path[len("/api/"):]
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return
        self._dispatch(name, body)

    def _dispatch(self, name, body):
        handler = HANDLERS.get(name)
        if handler is None:
            self._send(404, {"ok": False, "error": "unknown API"})
            return
        try:
            self._send(200, handler(body))
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)})


def _build_page():
    with open(PAGE_FILE, encoding="utf-8") as fh:
        page = fh.read()
    replacements = {
        "__LAYOUT__": LAYOUT,
        "__HIDNAMES__": HID_NAMES,
        "__POSMAP__": POSITION_BY_HID,
        "__MEDIA__": keyconfig.FUNCTIONS,
        "__MOUSE__": keyconfig.MOUSE,
        "__COMBOS__": keyconfig.COMBOS,
        "__SPECIAL__": keyconfig.SPECIAL,
        "__LIGHT_MODES__": list(protocol.LED_MODES),
        "__LIGHT_OPTIONS__": LIGHT_OPTIONS,
        "__LIGHT_DAZZLE__": [
            name for name, mode in protocol.LED_MODES.items()
            if mode in protocol.LED_DAZZLE_MODES
        ],
        "__LIGHT_PRESETS__": LIGHT_PRESETS,
    }
    for marker, value in replacements.items():
        page = page.replace(marker, json.dumps(value, ensure_ascii=True))
    return page


_PAGE = ""


def create_server(host="127.0.0.1", port=DEFAULT_PORT):
    """Build (but do not run) the HTTP server and build the page once.

    Returns a ThreadingHTTPServer the caller must ``serve_forever()`` or
    run in a thread. Kept separate so the GTK app window can embed the same
    backend instead of spawning a browser.
    """
    global _PAGE
    _PAGE = _build_page()
    return ThreadingHTTPServer((host, port), Handler)


def shutdown_server(server):
    """Stop the HTTP server and release the device/follow threads."""
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
    _FOLLOW.stop()
    _close_device()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="keydous-gui",
                                     description="Keydous NJ81 web GUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    print(f"Keydous NJ81 GUI: http://{args.host}:{args.port}")
    print("press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_server(server)


if __name__ == "__main__":
    main()
