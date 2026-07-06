"""App entry: pywebview window plus pystray tray icon. Closing the window hides it."""
import os
import sys
import threading

import webview

from . import APP_NAME, api as api_mod, db, fonts, icon as icon_mod, stt

if getattr(sys, "frozen", False):
    # PyInstaller bundle: data files live under sys._MEIPASS.
    UI_DIR = os.path.join(sys._MEIPASS, "ui")
else:
    UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


class App:
    def __init__(self, debug=False):
        self.debug = debug
        self.api = api_mod.Api()
        self.window = None
        self.tray = None
        self.tray_ready = threading.Event()
        self.quitting = False

    def _on_closing(self):
        # Hide to tray instead of exiting, unless a real quit was requested.
        # With no working tray a hidden window would be unreachable, so in that
        # case let the close proceed as a real exit.
        if self.quitting or not self.tray_ready.is_set():
            self.quitting = True
            return True
        if self.window is not None:
            self.window.hide()
        return False

    def _show_window(self, *_):
        if self.window is not None:
            self.window.show()
            self.window.restore()

    def _new_note(self, *_):
        self._show_window()
        try:
            self.window.evaluate_js("window.trayNewNote && window.trayNewNote()")
        except Exception:
            pass

    def _quit(self, *_):
        self.quitting = True
        if self.api.session is not None:
            try:
                self.api.session.stop()
            except Exception:
                pass
        if self.tray is not None:
            self.tray.stop()
        if self.window is not None:
            self.window.destroy()

    def _start_tray(self):
        try:
            import pystray
        except Exception:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open " + APP_NAME, self._show_window, default=True),
            pystray.MenuItem("New note", self._new_note),
            pystray.MenuItem("Quit", self._quit))
        self.tray = pystray.Icon(APP_NAME, icon_mod.make_icon(64), APP_NAME, menu)

        def setup(icon):
            icon.visible = True  # a custom setup must show the icon itself
            self.tray_ready.set()

        def run_tray():
            try:
                self.tray.run(setup=setup)
            except Exception:
                self.tray_ready.clear()

        threading.Thread(target=run_tray, daemon=True, name="tray").start()

    def run(self):
        db.init()
        fonts.ensure_fonts_async(UI_DIR)
        stt.preload_model_async()
        self.window = webview.create_window(
            APP_NAME, os.path.join(UI_DIR, "index.html"), js_api=self.api,
            width=1320, height=880, min_size=(980, 640),
            background_color="#F7F3EA")
        self.api.window = self.window
        self.window.events.closing += self._on_closing
        self._start_tray()
        webview.start(debug=self.debug, private_mode=False)
        # Window destroyed: make sure everything winds down.
        self.quitting = True
        if self.api.session is not None:
            try:
                self.api.session.stop()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass


def main():
    debug = "--debug" in sys.argv
    App(debug=debug).run()
