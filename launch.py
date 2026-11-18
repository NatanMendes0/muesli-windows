"""Entry point that works from any working directory (used by the startup Run key).

run.bat uses `python -m muesli` after cd-ing here; the registry Run key launches
processes from System32, where -m muesli cannot resolve, so it points at this file.
"""
import csv
import os
import re
import sys

# Inject native OS certificate store (Windows CryptoAPI) to trust corporate/proxy Root CAs
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def unblock_self():
    """Remove NTFS Zone.Identifier streams so .NET/pythonnet can load DLLs."""
    if sys.platform != "win32":
        return
    targets = []
    if getattr(sys, "frozen", False):
        targets.append(os.path.dirname(os.path.abspath(sys.executable)))
        if hasattr(sys, "_MEIPASS"):
            targets.append(sys._MEIPASS)
    else:
        targets.append(os.path.dirname(os.path.abspath(__file__)))

    for base in set(targets):
        if not base or not os.path.exists(base):
            continue
        for root, _, files in os.walk(base):
            for f in files:
                p = os.path.join(root, f)
                try:
                    os.remove(p + ":Zone.Identifier")
                except OSError:
                    pass


def _other_instance_pids(tasklist_csv, current_pid):
    for row in csv.reader(tasklist_csv.splitlines()):
        if len(row) < 2:
            continue
        image, pid = row[:2]
        if (re.fullmatch(r'Muesli(?:-windows-x64-v\d+\.\d+\.\d+)?\.exe', image, re.I)
                and re.fullmatch(r'[0-9]+', pid) and int(pid) != current_pid):
            yield pid


def kill_other_instances():
    """Ensure opening a new version terminates any orphan Muesli instance in background."""
    if sys.platform != "win32":
        return
    try:
        import subprocess
        system32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
        listed = subprocess.run([os.path.join(system32, 'tasklist.exe'), '/FO', 'CSV', '/NH'],
                                capture_output=True, text=True, errors='replace', timeout=8, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        for pid in _other_instance_pids(listed.stdout, os.getpid()):
            subprocess.run([os.path.join(system32, 'taskkill.exe'), '/F', '/PID', pid], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def selftest():
    """Headless check that the heavy native pieces load inside a frozen build.
    Writes selftest.json next to the executable; exit code 0 on success."""
    import json
    import traceback
    out: dict[str, str | int | bool] = {"ok": False}
    try:
        from muesli import APP_VERSION, audio
        out["version"] = APP_VERSION
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
        unblock_self()
        selftest()

    from pathlib import Path
    from muesli import APP_VERSION, updater

    root = updater.install_root()
    trial = len(sys.argv) == 3 and sys.argv[1] == updater.TRIAL_ARG and updater.valid_trial(sys.argv[2])
    rollback = updater.ROLLBACK_ARG in sys.argv
    if not trial and not rollback:
        active = updater.active_executable(root, APP_VERSION) if root else None
        if active:
            try:
                updater.spawn(active, *sys.argv[1:])
            except OSError:
                pass  # The downloaded copy is still usable if policy blocks the managed copy.
            else:
                sys.exit(0)
        if not root or not Path(sys.executable).resolve().is_relative_to(root):
            unblock_self()
        kill_other_instances()
    from muesli.app import main
    main()
