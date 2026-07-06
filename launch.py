"""Entry point that works from any working directory (used by the startup Run key).

run.bat uses `python -m muesli` after cd-ing here; the registry Run key launches
processes from System32, where -m muesli cannot resolve, so it points at this file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def selftest():
    """Headless check that the heavy native pieces load inside a frozen build.
    Writes selftest.json next to the executable; exit code 0 on success."""
    import json
    import traceback
    out = {"ok": False}
    try:
        from muesli import audio
        d = audio.list_devices()
        out["audio_backend"] = d["ok"]
        out["mics"] = len(d["mics"])
        out["loopbacks"] = len(d["loopbacks"])
        import ctranslate2            # noqa: F401  whisper inference engine
        import onnxruntime            # noqa: F401  VAD
        import av                     # noqa: F401  audio decode
        from faster_whisper import WhisperModel  # noqa: F401
        import webview                # noqa: F401
        import pystray                # noqa: F401
        out["ok"] = True
    except Exception:
        out["error"] = traceback.format_exc()
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "selftest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    from muesli.app import main
    main()
