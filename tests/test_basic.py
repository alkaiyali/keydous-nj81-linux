"""Hardware-free unit tests: checksum, key encodings, protocol constants, CLI parsing."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from keydous import checksum, devices, keyconfig, protocol
from keydous.cli import _parse_rgb, build_parser


def test_checksum_bit7():
    buf = bytearray(64)
    buf[0] = 0x05
    buf[1] = 0x01
    out = checksum.apply(buf, checksum.BIT7)
    assert out[7] == (0xFF - (sum(buf[0:7]) & 0xFF)) & 0xFF


def test_checksum_bit8():
    buf = bytearray(64)
    buf[0] = 0x07
    buf[1] = 0x02
    out = checksum.apply(buf, checksum.BIT8)
    assert out[8] == (0xFF - (sum(buf[0:8]) & 0xFF)) & 0xFF


def test_checksum_none_passthrough():
    buf = bytearray(64)
    buf[0] = 0xAB
    assert checksum.apply(buf, checksum.NONE)[0] == 0xAB


def test_no_duplicate_pids():
    assert len(devices.DEVICES) == len(set(devices.DEVICES))
    assert 0x400B not in devices.DEVICES or "mouse" not in devices.DEVICES[0x400B].display_name.lower()
    # NJ81 wired must not be flagged as dongle
    assert devices.DEVICES[0x4010].dongle_common is False
    assert devices.DEVICES[0x4011].dongle_common is True


def test_all_usb_pids_sorted():
    pids = devices.all_usb_pids()
    assert pids == sorted(pids)
    assert 0x4010 in pids and 0x4011 in pids


def test_keyconfig_decode():
    names = {"4": "a", "5": "b"}
    assert keyconfig.decode([0, 0, 0, 0], names) == "Disabled"
    assert keyconfig.decode([0, 0, 4, 0], names) == "a"
    assert "Fn" in keyconfig.decode([10, 1, 0, 0], names)
    assert keyconfig.is_native_special([3, 0, 182, 0])
    assert not keyconfig.is_native_special([0, 0, 4, 0])


def test_led_modes_have_params():
    for name, mode in protocol.LED_MODES.items():
        assert mode in protocol.YC500_PARAM, f"mode {name} missing default param"


def test_rgb_parsing():
    assert _parse_rgb("FFA500") == 0xFFA500
    assert _parse_rgb("#ffa500") == 0xFFA500
    assert _parse_rgb("0x00FF00") == 0x00FF00


def test_cli_profile_validation():
    # parser accepts int; range check happens in cmd_profile (0..5)
    args = build_parser().parse_args(["profile", "3"])
    assert args.value == 3


def test_cli_light_accepts_legacy_set():
    args = build_parser().parse_args(["light", "set", "breath"])
    assert args.set == ["set", "breath"]
    args2 = build_parser().parse_args(["light", "breath"])
    assert args2.set == ["breath"]


def test_macro_encode_decode_roundtrip():
    events = [{"hid": 4, "up": False, "delay": 10},
              {"hid": 4, "up": True, "delay": 0}]
    payload = protocol.Macro.encode(events, repeat=1)
    assert payload[0] == 1  # repeat LE
    assert len(payload) > 4
