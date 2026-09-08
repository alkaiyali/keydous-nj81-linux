#!/usr/bin/env python3
"""Command-line driver for Keydous keyboards on Linux.

Usage:
  python -m keydous.cli list
  python -m keydous.cli info [--ble ADDR]
  python -m keydous.cli battery [--ble ADDR]
  python -m keydous.cli profile                # print active profile
  python -m keydous.cli profile set N
  python -m keydous.cli report-rate
  python -m keydous.cli report-rate set 1000
  python -m keydous.cli option
  python -m keydous.cli option set win_key_lock=1 led_off=0
  python -m keydous.cli light
  python -m keydous.cli light set breath --speed 2 --value 4 --rgb FFA500
  python -m keydous.cli reset
  python -m keydous.cli raw --cmd 131 --data 00 --scheme 0   # low-level
"""

import argparse
import sys

from . import checksum, protocol
from .rawusb import RawUsbKeyboard, UsbBusError
from .transport import HidrawUSB, KeydousError


def _open_device(args):
    if getattr(args, "ble", None):
        from .ble import BLEKeyboard
        return BLEKeyboard(args.ble)
    # The feature interface may be detached after previous driver use, so
    # fall back to scanning the USB bus for any Keydous device.
    from .devices import DEVICES
    devs = HidrawUSB.enumerate()
    pid = devs[0].dev.pid if devs else None
    if pid is None:
        for candidate in DEVICES.values():
            if not candidate.ble:
                try:
                    return RawUsbKeyboard(pid=candidate.pid)
                except UsbBusError:
                    continue
        raise KeydousError("no Keydous keyboard found over USB "
                           "(plug it in, check udev rules)")
    return RawUsbKeyboard(pid=pid)


def _print_hex(b):
    return " ".join(f"{x:02x}" for x in b)


def cmd_list(args):
    devs = HidrawUSB.enumerate()
    if devs:
        for d in devs:
            print(f"{d.path}  vid={d.dev.vid:04x} pid={d.dev.pid:04x} "
                  f"usage={d.dev.usage} usage_page={d.dev.usage_page:04x} "
                  f"iface={d.dev.interface_number}")
    from .devices import DEVICES
    from .rawusb import _find_usb_node
    found = False
    for desc in DEVICES.values():
        if desc.ble:
            continue
        try:
            node = _find_usb_node(desc.vid, desc.pid)
            print(f"{node}  vid={desc.vid:04x} pid={desc.pid:04x} "
                  f"{desc.display_name}")
            found = True
        except Exception:
            continue
    if not found and not devs:
        print("no Keydous keyboard found (plug it in, check udev rules)")
        return 1
    return 0


def cmd_info(args):
    with _open_device(args) as dev:
        kb = protocol.Keyboard(dev)
        fw = kb.firmware_version()  # single USB round-trip, reuse below
        print(f"firmware: v{(fw & 0xffff) // 100}."
              f"{(fw % 100):02d} "
              f"(raw 0x{fw:04x})")
        try:
            pct, status = kb.battery()
            if pct == 0 and status == "charging":
                print("battery: charging on USB (level unknown while wired)")
            else:
                print(f"battery: {pct}% ({status})")
        except Exception as e:
            print(f"battery: n/a ({e})")
        print(f"profile: {kb.profile()}")
        try:
            print(f"report rate: {kb.report_rate()} Hz")
        except Exception as e:
            print(f"report rate: n/a ({e})")
        try:
            opt = kb.keyboard_option()
            for k, v in opt.items():
                print(f"  {k}: {v}")
        except Exception as e:
            print(f"options: n/a ({e})")
    return 0


def cmd_battery(args):
    with _open_device(args) as dev:
        kb = protocol.Keyboard(dev)
        pct, status = kb.battery()
        if pct == 0 and status == "charging":
            print("charging on USB (level unknown while wired)")
        else:
            print(f"{pct}% ({status})")
    return 0


def cmd_profile(args):
    if args.value is not None and not 0 <= args.value <= 5:
        print("error: profile must be 0..5", file=sys.stderr)
        return 1
    with _open_device(args) as dev:
        kb = protocol.Keyboard(dev)
        if args.value is None:
            print(kb.profile())
        else:
            kb.set_profile(args.value)
            print(f"profile set to {args.value}")
    return 0


def cmd_report_rate(args):
    # Accept: `report-rate` (get), `report-rate 500`,
    # `report-rate set 500` (legacy form).
    value = args.value
    if value is None and args.set not in (None, "get", "set"):
        try:
            value = int(args.set)
        except ValueError:
            print(f"error: invalid report rate {args.set!r} "
                  "(choose 125, 250, 500, 1000)", file=sys.stderr)
            return 1
    with _open_device(args) as dev:
        kb = protocol.Keyboard(dev)
        if value is None:
            print(f"{kb.report_rate()} Hz")
        else:
            if value not in (125, 250, 500, 1000):
                print("error: report rate must be 125, 250, 500, or 1000 Hz",
                      file=sys.stderr)
                return 1
            kb.set_report_rate(value)
            print(f"report rate set to {value} Hz")
    return 0


def cmd_option(args):
    with _open_device(args) as dev:
        kb = protocol.Keyboard(dev)
        sets = list(args.set)
        if sets and sets[0] == "set":
            sets = sets[1:]          # allow `option set k=v`
        if not sets:
            for k, v in kb.keyboard_option().items():
                print(f"{k}: {int(v)}")
            return 0
        kw = {}
        for item in sets:
            k, _, v = item.partition("=")
            kw[k.strip()] = int(v)
        kb.set_keyboard_option(**kw)
        print("option updated")
    return 0


def _parse_rgb(s):
    """Parse hex color: FFA500, #FFA500, 0xFFA500."""
    s = s.strip().lstrip("#")
    if s.lower().startswith("0x"):
        s = s[2:]
    value = int(s, 16)
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError("rgb must be 24-bit hex")
    return value


def cmd_light(args):
    # Accept both `light breath` and legacy `light set breath` (docstring form).
    mode_name = args.set
    if isinstance(mode_name, list):
        mode_name = [a for a in mode_name if a != "set"]
        mode_name = mode_name[0] if mode_name else None
    with _open_device(args) as dev:
        kb = protocol.Keyboard(dev)
        if mode_name is None:
            cur = kb.get_light()
            print(f"mode: {cur['mode']} speed: {cur['speed']} "
                  f"value: {cur['value']} param: {cur['param']} "
                  f"rgb: #{cur['rgb']:06x}")
            return 0
        mode = protocol.LED_MODES.get(mode_name)
        if mode is None:
            print(f"unknown mode {mode_name!r}; choose from: "
                  + ", ".join(protocol.LED_MODES), file=sys.stderr)
            return 1
        rgb = args.rgb or 0xFFFFFF
        kb.set_light(mode, speed=args.speed, value=args.value,
                     param=args.param,
                     r=(rgb >> 16) & 0xFF, g=(rgb >> 8) & 0xFF, b=rgb & 0xFF)
        print(f"light set to {mode_name}")
    return 0

def cmd_reset(args):
    with _open_device(args) as dev:
        protocol.Keyboard(dev).reset()
        print("reset sent")
    return 0


def cmd_raw(args):
    with _open_device(args) as dev:
        resp = dev.command(args.raw_cmd, bytes.fromhex(args.data),
                           scheme=args.scheme)
        print("resp:", _print_hex(resp))
    return 0


def cmd_remap(args):
    import json as _json
    from . import keyconfig
    from .protocol import Matrix
    hid_names = _json.load(open(
        __import__("os").path.join(__import__("os").path.dirname(
            __import__("os").path.abspath(__file__)), "hid_key_names.json")))
    name2hid = {v.lower(): int(k) for k, v in hid_names.items()}

    def resolve(value):
        if value.isdigit():
            return int(value)
        return name2hid.get(value.lower())

    with _open_device(args) as dev:
        m = Matrix(dev)
        if args.list:
            matrix = m.read()
            for pos, cfg in enumerate(matrix):
                if any(cfg):
                    print(f"{pos:3d}: {keyconfig.decode(cfg, hid_names)}")
            return 0
        if args.get is not None:
            cfg = m.read()[args.get]
            print(f"pos {args.get}: {cfg} = "
                  f"{keyconfig.decode(cfg, hid_names)}")
            return 0
        if args.set is not None:
            pos, _, target = args.set.partition("=")
            pos = int(pos)
            hid = resolve(target.strip())
            if hid is None:
                print(f"unknown key {target!r}", file=sys.stderr)
                return 1
            m.remap(pos, hid)
            print(f"pos {pos} -> {hid_names.get(str(hid), hid)} ({hid})")
            return 0
        if args.disable is not None:
            m.reset_key(int(args.disable))
            print(f"pos {args.disable} disabled")
            return 0
    return 0


def cmd_gui(args):
    from .gui import main as gui_main
    gui_main(["--host", args.host, "--port", str(args.port)])
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="keydous",
                                description="Keydous keyboard driver (Linux)")
    p.add_argument("--ble", metavar="ADDR", default=None,
                   help="use BLE transport at MAC address (e.g. AA:BB:..)")
    p.add_argument("--version", action="store_true",
                   help="print driver version and exit")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list Keydous USB HID devices")
    sub.add_parser("info", help="firmware / battery / profile / options")
    sub.add_parser("battery", help="battery percentage and status")

    gui = sub.add_parser("gui", help="start the web remapping GUI")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8765)

    rm = sub.add_parser("remap", help="key remapping")
    rm.add_argument("--list", action="store_true", help="list current matrix")
    rm.add_argument("--get", type=int, metavar="POS", help="show one position")
    rm.add_argument("--set", metavar="POS=KEY",
                    help="remap position to key (hid code or name, e.g. 0=a)")
    rm.add_argument("--disable", type=int, metavar="POS",
                    help="disable a key position")

    sp = sub.add_parser("profile", help="get/set active profile")
    sp.add_argument("value", nargs="?", type=int, metavar="N")

    rr = sub.add_parser("report-rate", help="get/set polling rate")
    rr.add_argument("set", nargs="?", const="get", metavar="HZ")
    rr.add_argument("value", nargs="?", type=int, metavar="HZ")

    op = sub.add_parser("option", help="get/set keyboard options")
    op.add_argument("set", nargs="*", metavar="KEY=VALUE")

    lt = sub.add_parser("light", help="get/set LED effect")
    lt.add_argument("set", nargs="*", metavar="MODE",
                    help="off|always_on|breath|wave|ripple|raindrop|snake|...")
    lt.add_argument("--speed", type=int, default=0)
    lt.add_argument("--value", type=int, default=4)
    lt.add_argument("--param", type=int, default=None)
    lt.add_argument("--rgb", type=_parse_rgb, default=None)

    sub.add_parser("reset", help="restore defaults / reset device")

    sub.add_parser("version", help="print driver version")

    raw = sub.add_parser("raw", help="low-level feature report")
    raw.add_argument("--cmd", dest="raw_cmd",
                     type=lambda s: int(s, 0), required=True)
    raw.add_argument("--data", default="", help="hex bytes for byte[1..]")
    raw.add_argument("--scheme", type=int, default=checksum.BIT7,
                     choices=[0, 1, 2])

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "version", False):
        from . import __version__
        print(f"keydous-nj81 {__version__}")
        return 0
    try:
        if args.cmd is None:
            build_parser().print_help()
            return 0
        if args.cmd == "version":
            from . import __version__
            print(f"keydous-nj81 {__version__}")
            return 0
        return {
            "list": cmd_list,
            "info": cmd_info,
            "battery": cmd_battery,
            "profile": cmd_profile,
            "report-rate": cmd_report_rate,
            "option": cmd_option,
            "light": cmd_light,
            "reset": cmd_reset,
            "raw": cmd_raw,
            "remap": cmd_remap,
            "gui": cmd_gui,
        }[args.cmd](args)
    except KeydousError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except UsbBusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
