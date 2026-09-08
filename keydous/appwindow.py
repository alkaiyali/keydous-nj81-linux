"""Native GTK/WebKit window for the Keydous NJ81 driver.

Runs the same Python HTTP backend as ``keydous-driver gui`` but renders it
in a WebKit2 window instead of a browser tab, so the driver feels like a
real desktop app (own window, launcher icon, desktop shortcut).

Uses GTK3 + WebKit2 4.1 (this system's WebKitGTK 4.1 typelib depends on
GTK3). Requirements: ``python3-gi``, ``gir1.2-webkit2-4.1``.

Single-instance: launching the app twice focuses the existing window
(Gtk.Application with default flags) instead of fighting over the USB
device and opening a second backend.
"""

import os
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

# Allow running as a plain script (install.sh launcher) or as a module.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from keydous import gui  # noqa: E402

# Gio.ApplicationFlags.FLAGS_NONE == 0: GApplication enforces uniqueness by
# APP_ID, so a second launch activates the first instance and exits.
APP_ID = "io.github.yapplecunt.keydousnj81"
DEFAULT_SIZE = (1160, 840)
ICON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "icons", "keydous-nj81.png")


def _find_free_port(start=gui.DEFAULT_PORT):
    import socket
    port = start
    while True:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1


class KeydousApp:
    def __init__(self):
        self.server = None
        self.port = _find_free_port()
        self.app = Gtk.Application.new(APP_ID, 0)
        self.app.connect("activate", self._on_activate)
        self.app.connect("shutdown", self._on_shutdown)

    def _on_activate(self, app):
        # activate() fires for every launch; if the window already exists
        # this is a second invocation -> present it and keep the single
        # backend/device that the first window owns.
        windows = app.get_windows()
        if windows:
            windows[0].present()
            return
        server = gui.create_server("127.0.0.1", self.port)
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()

        window = Gtk.ApplicationWindow.new(app)
        window.set_title("Keydous NJ81 Driver")
        window.set_default_size(*DEFAULT_SIZE)
        if os.path.exists(ICON_PATH):
            try:
                window.set_icon_from_file(ICON_PATH)
            except GLib.Error:
                pass
        window.connect("delete-event", lambda *_a: self.app.quit())

        web = WebKit2.WebView.new()
        settings = web.get_settings()
        settings.set_enable_developer_extras(True)
        window.add(web)
        web.load_uri(f"http://127.0.0.1:{self.port}/")
        window.show_all()

    def _on_shutdown(self, _app):
        gui.shutdown_server(self.server)
        self.server = None

    def run(self, argv=None):
        return self.app.run(argv or None)


def main(argv=None):
    app = KeydousApp()
    return app.run(argv or None)


if __name__ == "__main__":
    raise SystemExit(main())
