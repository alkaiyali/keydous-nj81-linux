"""Raw USB transport for Keydous keyboards.

The vendor daemon (iot_driver) talks to the keyboard's vendor HID
interface (interface 2, usage page 0xffff, usage 2) through feature
reports. On Linux, when usbhid owns that interface, *SET* feature
reports work but *GET* feature reports return all zeros. The reliable
path (matching the daemon's behavior) is raw USB control transfers:

  SET_REPORT : bmRequestType=0x21 bRequest=0x09 wValue=0x0300 iface=2
  GET_REPORT : bmRequestType=0xA1 bRequest=0x01 wValue=0x0300 iface=2

To issue these we must detach usbhid from interface 2 (USBDEVFS
DISCONNECT_CLAIM), which requires write access to /dev/bus/usb (see
udev/50-keydous-usb.rules). The interface is released afterwards so
usbhid rebinds.
"""

import ctypes
import os
import time

# ioctl numbers (linux/usbdevice_fs.h)
USBDEVFS_CONTROL = 0xC0185500          # _IOWR('U', 0, usbdevfs_ctrltransfer)
USBDEVFS_DISCONNECT_CLAIM = 0x8108551B  # _IOR('U', 27, usbdevfs_disconnect_claim)
USBDEVFS_RELEASEINTERFACE = 0x80045510  # _IOR('U', 16, unsigned int)

MAXDRIVERNAME = 255

_libc = ctypes.CDLL(None, use_errno=True)
_libc.ioctl.restype = ctypes.c_int


class _CtrlTransfer(ctypes.Structure):
    _fields_ = [("bRequestType", ctypes.c_uint8),
                ("bRequest", ctypes.c_uint8),
                ("wValue", ctypes.c_uint16),
                ("wIndex", ctypes.c_uint16),
                ("wLength", ctypes.c_uint16),
                ("timeout", ctypes.c_uint32),
                ("data", ctypes.c_void_p)]


class _DisconnectClaim(ctypes.Structure):
    _fields_ = [("interface", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("driver", ctypes.c_char * (MAXDRIVERNAME + 1))]


class UsbBusError(Exception):
    pass


def _find_usb_node(vid: int, pid: int) -> str:
    """Locate /dev/bus/usb/<bus>/<dev> for the first device with vid/pid."""
    for name in os.listdir("/sys/bus/usb/devices"):
        d = os.path.join("/sys/bus/usb/devices", name)
        if not os.path.isdir(d) or ":" in name:
            continue
        try:
            with open(os.path.join(d, "idVendor")) as f:
                v = int(f.read().strip(), 16)
            with open(os.path.join(d, "idProduct")) as f:
                p = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if v == vid and p == pid:
            try:
                with open(os.path.join(d, "busnum")) as f:
                    bus = f.read().strip()
                with open(os.path.join(d, "devnum")) as f:
                    dev = f.read().strip()
                return f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}"
            except OSError:
                continue
    raise UsbBusError(f"USB device {vid:04x}:{pid:04x} not found in "
                      f"/dev/bus/usb")


class RawUsbKeyboard:
    """Feature-report transport over raw USB control transfers."""

    def __init__(self, vid: int = 0x3151, pid: int = None,
                 interface: int = 2, node: str = None):
        self.vid = vid
        self.pid = pid
        self.interface = interface
        self.node = node or _find_usb_node(vid, pid) if pid else None
        self._fd = -1
        self._detached = False

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        if self.node is None:
            self.node = _find_usb_node(self.vid, self.pid)
        try:
            self._fd = os.open(self.node, os.O_RDWR)
        except PermissionError as e:
            raise UsbBusError(
                f"permission denied opening {self.node}: install "
                f"udev/50-keydous-usb.rules or run with sudo") from e
        except OSError as e:
            raise UsbBusError(f"failed to open {self.node}: {e}") from e
        self._detach_usbhid()

    def close(self) -> None:
        if self._fd >= 0:
            if self._detached:
                arg = ctypes.c_uint(self.interface)
                _libc.ioctl(self._fd, USBDEVFS_RELEASEINTERFACE,
                            ctypes.byref(arg))
            os.close(self._fd)
            self._fd = -1
        self._detached = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def _detach_usbhid(self) -> None:
        dc = _DisconnectClaim()
        dc.interface = self.interface
        dc.flags = 0x01                     # IF_DRIVER
        dc.driver = b"usbhid"
        r = _libc.ioctl(self._fd, USBDEVFS_DISCONNECT_CLAIM, ctypes.byref(dc))
        if r < 0:
            err = ctypes.get_errno()
            if err in (16, 25, 19):         # EBUSY/ENOTTY/ENODEV: not claimed
                # maybe already detached by a previous crash; try to proceed
                self._detached = False
                return
            raise UsbBusError(
                f"failed to detach usbhid from interface {self.interface}: "
                f"errno {err}")
        self._detached = True

    # -- control transfers ------------------------------------------------
    def _ctrl(self, bmRequestType, bRequest, wValue, data, wLength, timeout=2000):
        try:
            return self._ctrl_raw(bmRequestType, bRequest, wValue, data,
                                  wLength, timeout)
        except UsbBusError:
            # The keyboard wedges after some SETs (e.g. report rate);
            # a USB reset restores it.
            self._reset()
            return self._ctrl_raw(bmRequestType, bRequest, wValue, data,
                                  wLength, timeout)

    def _ctrl_raw(self, bmRequestType, bRequest, wValue, data, wLength,
                  timeout):
        buf = (ctypes.c_uint8 * max(wLength, 1))()
        if data:
            for i, b in enumerate(data):
                if i < wLength:
                    buf[i] = b
        ct = _CtrlTransfer()
        ct.bRequestType = bmRequestType
        ct.bRequest = bRequest
        ct.wValue = wValue
        ct.wIndex = self.interface
        ct.wLength = wLength
        ct.timeout = timeout
        ct.data = ctypes.cast(buf, ctypes.c_void_p)
        ret = _libc.ioctl(self._fd, USBDEVFS_CONTROL, ctypes.byref(ct))
        if ret < 0:
            raise UsbBusError(f"control transfer failed: errno "
                              f"{ctypes.get_errno()}")
        return bytes(buf[:ret])

    def _sysfs_dir(self) -> str | None:
        """Resolve /sys/bus/usb/devices/<dev> for self.node.

        self.node looks like /dev/bus/usb/003/005 (busnum/devnum). The
        matching sysfs dir is found by comparing busnum/devnum files,
        e.g. /sys/bus/usb/devices/3-2. This also works after re-enumeration
        where the old /dev path is stale. Returns None when not found."""
        if not self.node:
            return None
        try:
            parts = self.node.rsplit("/", 2)
            bus = str(int(parts[-2]))
            dev = str(int(parts[-1]))
        except (ValueError, IndexError):
            return None
        import glob
        for d in glob.glob("/sys/bus/usb/devices/*"):
            # skip interface dirs like 3-2:1.2, keep device dirs like 3-2
            if ":" in os.path.basename(d):
                continue
            try:
                with open(os.path.join(d, "busnum")) as fh:
                    b = fh.read().strip()
                with open(os.path.join(d, "devnum")) as fh:
                    n = fh.read().strip()
            except OSError:
                continue
            if b.lstrip("0") == bus.lstrip("0") or b == bus:
                if n.lstrip("0") == dev.lstrip("0") or n == dev:
                    return d
        return None

    def _reset(self):
        """USBDEVFS_RESET - re-enumerates the device and clears the wedge.

        Refuse when the node is a hub: USBDEVFS_RESET on a hub takes down
        every device behind it (keyboards, disks, ...)."""
        USBDEVFS_RESET = 0x00005514
        sysfs_dir = self._sysfs_dir()
        if sysfs_dir:
            try:
                with open(os.path.join(sysfs_dir, "bDeviceClass")) as fh:
                    if int(fh.read().strip(), 16) == 0x09:   # 0x09 = hub
                        raise UsbBusError("refusing to USB-reset a hub")
            except UsbBusError:
                raise
            except (OSError, ValueError):
                pass  # sysfs read failed; fall through to the reset
        _libc.ioctl(self._fd, USBDEVFS_RESET)
        time.sleep(0.6)
        # the reset may have re-enumerated the interface; re-detach usbhid
        self._detached = False
        try:
            self._detach_usbhid()
        except UsbBusError:
            pass  # best effort; the retried transfer will tell us

    def send_feature(self, payload: bytes) -> None:
        """SET_REPORT, feature report id 0, 64-byte payload."""
        assert len(payload) == 64
        self._ctrl(0x21, 0x09, 0x0300, payload, 64)

    def get_feature(self) -> bytes:
        """GET_REPORT, feature report id 0. Returns the 64-byte response."""
        return self._ctrl(0xA1, 0x01, 0x0300, None, 64)

    def command(self, cmd: int, data: bytes = b"",
                scheme: int = 0, pad: int = 64) -> bytes:
        from . import checksum
        buf = bytearray(pad)
        buf[0] = cmd & 0xFF
        for i, b in enumerate(data):
            buf[1 + i] = b
        buf = checksum.apply(buf, scheme)
        for attempt in range(4):
            self.send_feature(buf)
            time.sleep(0.02)
            resp = self.get_feature()
            if len(resp) == 0:
                raise UsbBusError("empty feature-report response")
            # The firmware echoes the command byte in the first byte of the
            # response; a shifted/garbage response means it is still
            # recovering from a previous SET.
            if resp[0] == (cmd & 0xFF):
                return resp
            time.sleep(0.15)
        # The device is wedged; a USB reset clears it.
        self._reset()
        self.send_feature(buf)
        time.sleep(0.05)
        resp = self.get_feature()
        if len(resp) and resp[0] == (cmd & 0xFF):
            return resp
        raise UsbBusError(
            f"bad response to cmd {cmd:#04x}: {resp[:8].hex() if resp else ''}")

    def command_raw(self, cmd: int, data: bytes = b"",
                    scheme: int = 0, pad: int = 64) -> bytes:
        """Like command(), but does not validate the command echo.

        Some firmware responses (e.g. matrix reads) return raw data
        without echoing the command byte."""
        from . import checksum
        buf = bytearray(pad)
        buf[0] = cmd & 0xFF
        for i, b in enumerate(data):
            buf[1 + i] = b
        buf = checksum.apply(buf, scheme)
        for attempt in range(4):
            self.send_feature(buf)
            time.sleep(0.02)
            resp = self.get_feature()
            if len(resp):
                return resp
            time.sleep(0.15)
        self._reset()
        self.send_feature(buf)
        time.sleep(0.05)
        resp = self.get_feature()
        if len(resp) == 0:
            raise UsbBusError("empty feature-report response")
        return resp