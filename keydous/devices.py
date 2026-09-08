"""Device database recovered from iot_driver's embedded `SupportVender` table.

Re-verified 2026-08-26 against the vendor driver bundle in
`~/keydous-project/study/device-table.json` (1027 entries extracted from the
minified Electron app). Only VID 0x3151 entries are listed here.

Keydous keyboards expose two personalities:
  * USB wired / dongle (2.4G): a vendor HID interface.
      vid 0x3151, usage_page 0xffff, usage 2, bInterfaceNumber 2,
      feature report length 65 (report-id + 64 bytes).
  * BLE: the keyboard advertises as a vendor HID over GATT using the
      Feasycom/Nordic UART Service, reachable only over the air.

Vendor PID semantics across the whole table:
  0x4002 wired YZW generation   0x4003 dongle YC200/300
  0x4007 wired YC400            0x400b MOUSE (common_mouse) - never a keyboard
  0x4010 wired YC500 (NJ81/NJ68/NJ80 ...)     0x4011 its 2.4G dongle
  0x4015 SOC-generation boards  0x4017 SOC dongle variant
  0x4018 k433p/k84-single       0x401e lj_fd98t    0x4021/0x4022 yc3016a
"""

from dataclasses import dataclass
from typing import Optional

# --- BLE GATT service/characteristic UUIDs (from macOS iot_v217 binary) ---
# Feasycom / Nordic UART Service used by the Keydous BLE module.
BLE_SERVICE_UUID = "49535343-FE7D-4AE5-8FA9-9FAFD205E455"
BLE_RX_UUID = "49535343-8841-43f4-A8D4-ECBE34729BB3"    # host -> keyboard (write)
BLE_TX_UUID = "49535343-1E4D-4BD9-BA61-23C647249616"    # keyboard -> host (notify)


@dataclass
class DevDesc:
    vid: int
    pid: int
    usage: int = 2
    usage_page: int = 0xFFFF
    interface_number: int = 2
    feature_report_len: int = 65
    dongle_common: bool = False
    ble: bool = False
    name: str = ""
    display_name: str = ""

    def usb_match(self, vid, pid, usage, usage_page) -> bool:
        return (self.vid == vid and self.pid == pid
                and self.usage == usage and self.usage_page == usage_page)


def _d(pid, name, display, dongle=False, ble=False, usage=2, up=0xFFFF, itf=2):
    return DevDesc(0x3151, pid, usage, up, itf, dongle_common=dongle, ble=ble,
                   name=name, display_name=display)


# Recovered from iot_driver.exe SupportVender table; cross-checked against the
# full 2026 vendor dump (see module docstring). PIDs that appear ONLY under
# other VIDs (0x0461/0x25a7/0x05ac/...) are intentionally not registered.
DEVICES: dict[int, DevDesc] = {}


def _reg(desc: DevDesc) -> DevDesc:
    if desc.pid in DEVICES:
        raise ValueError(f"duplicate PID {desc.pid:#06x} in device table")
    DEVICES[desc.pid] = desc
    return desc


# --- NJ81 family (YC500 generation) ---------------------------------------
_reg(_d(0x4010, "yc500_nj81", "NJ81"))                    # wired personality
_reg(_d(0x4011, "yc500_nj81", "NJ81", dongle=True))       # 2.4G dongle
_reg(_d(0x4015, "yc500_nj81s", "NJ81S", dongle=True))     # SOC gen shares 0x4015
_reg(_d(0x4018, "yc500_nj81_ed", "NJ81-ED"))              # k433p / k84-single share it

# --- other Keydous keyboards on VID 0x3151 ---------------------------------
_reg(_d(0x4007, "yc400_nj80", "NJ80/NJ68 (YC400)", dongle=True))
_reg(_d(0x400B, "yc200_nj68", "NJ68 (YC200 legacy)", dongle=True))
_reg(_d(0x401e, "yc3016a_lj_fd98t", "FD98T"))
_reg(_d(0x4021, "yc3016a_hf_k1", "HF-K1 / FD98T (SOC)", dongle=True))
_reg(_d(0x4022, "yc3016a_hf_k1", "HF-K1 (SOC)", dongle=True))

# NOTE: 0x400b is common_mouse in the vendor table -- deliberately NOT
# registered as a keyboard. The old entry claimed it was an NJ68.


def find_usb(vid: int, pid: int, usage: int, usage_page: int) -> Optional[DevDesc]:
    d = DEVICES.get(pid)
    if d and d.usb_match(vid, pid, usage, usage_page) and not d.ble:
        return d
    return None


def lookup(pid: int) -> Optional[DevDesc]:
    return DEVICES.get(pid)


def is_keydous_vid(vid: int) -> bool:
    """True for the Keydous USB vendor id (0x3151)."""
    return vid == 0x3151


def all_usb_pids() -> list[int]:
    """Every known keyboard PID on VID 0x3151 (wired + dongles)."""
    return sorted(p for p, d in DEVICES.items() if not d.ble)
