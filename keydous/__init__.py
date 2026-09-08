"""keydous - a from-scratch Linux driver for Keydous (NJ-series) keyboards.

Reconstructed from reverse engineering of:
  * Keydous Driver Windows Electron app (dist/index.js, 2023)
  * iot_driver.exe (Windows daemon, 2023)
  * iot_v217.dmg (macOS daemon v2.17, Rust/gRPC, 2025)

Transport options:
  * USB HID via Linux hidraw ioctls (zero dependencies)
  * BLE via BlueZ D-Bus (requires python-dbus/gi or bleak)
"""

__version__ = "1.0.0"