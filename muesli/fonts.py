"""Fetches the Satoshi typeface on first run.

Fontshare's free-font EULA does not allow redistributing the font files, so they
are not shipped with Muesli. Instead each installation downloads them once from
Fontshare's own API (a delivery mode the EULA explicitly supports) into the local
data folder. If the download is not possible the UI falls back to system fonts.
"""
import io
import os
import threading
import zipfile

import requests

from . import settings

FONTSHARE_URL = "https://api.fontshare.com/v2/fonts/download/satoshi"
WANTED = ["Satoshi-Variable.woff2", "Satoshi-Regular.woff2",
          "Satoshi-Medium.woff2", "Satoshi-Bold.woff2"]


def fonts_dir() -> str:
    d = os.path.join(settings.data_dir(), "fonts")
    os.makedirs(d, exist_ok=True)
    return d


def fonts_present() -> bool:
    return all(os.path.exists(os.path.join(fonts_dir(), n)) for n in WANTED)


def _copy_into_ui(ui_dir: str):
    """Place the cached fonts where the stylesheet already looks (ui/fonts).
    Works whenever the app folder is user-writable; the file:/// injection in
    the frontend is the fallback when it is not."""
    if not ui_dir:
        return
    try:
        target = os.path.join(ui_dir, "fonts")
        os.makedirs(target, exist_ok=True)
        for name in WANTED:
            src = os.path.join(fonts_dir(), name)
            dst = os.path.join(target, name)
            if os.path.exists(src) and not os.path.exists(dst):
                with open(src, "rb") as s, open(dst, "wb") as d:
                    d.write(s.read())
    except OSError:
        pass


def ensure_fonts_async(ui_dir: str = ""):
    """Download Satoshi in the background if it is not cached yet."""
    if fonts_present():
        _copy_into_ui(ui_dir)
        return

    def worker():
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            try:
                r = requests.get(FONTSHARE_URL, timeout=60, headers=headers)
            except requests.exceptions.SSLError:
                r = requests.get(FONTSHARE_URL, timeout=60, headers=headers, verify=False)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                for info in z.infolist():
                    base = os.path.basename(info.filename)
                    if base in WANTED and "/WEB/" in info.filename.replace("\\", "/"):
                        with z.open(info) as src:
                            with open(os.path.join(fonts_dir(), base), "wb") as dst:
                                dst.write(src.read())
            _copy_into_ui(ui_dir)
        except Exception:
            pass  # offline or blocked: system font fallback covers it

    threading.Thread(target=worker, daemon=True, name="font-fetch").start()


def font_faces() -> list:
    """@font-face descriptors for the UI, as file:/// URLs into the data dir."""
    if not fonts_present():
        return []
    base = "file:///" + fonts_dir().replace("\\", "/")
    return [
        {"file": f"{base}/Satoshi-Variable.woff2", "weight": "300 900"},
        {"file": f"{base}/Satoshi-Regular.woff2", "weight": "400"},
        {"file": f"{base}/Satoshi-Medium.woff2", "weight": "500"},
        {"file": f"{base}/Satoshi-Bold.woff2", "weight": "700"},
    ]
