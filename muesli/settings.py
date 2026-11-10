"""Local settings and data paths. Everything lives under %APPDATA%/Muesli."""
import json
import os
import sys
import threading

APP_DIR_NAME = "Muesli"

def data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d

def models_dir() -> str:
    d = os.path.join(data_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d

def audio_dir() -> str:
    d = os.path.join(data_dir(), "audio")
    os.makedirs(d, exist_ok=True)
    return d

SETTINGS_PATH = os.path.join(data_dir(), "settings.json")

DEFAULTS = {
    "stt": {
        "engine": "local",          # local | openai | groq
        "model": "base",            # tiny | base | small | medium | large-v3-turbo
        "device": "cpu",            # cpu | cuda | auto
        "language": "pt",           # pt | en | auto
        "compute_type": "int8",
    },
    "llm": {
        "provider": "auto",         # auto | ollama | openai | groq | anthropic | none
        "ollama_url": "http://localhost:11434",
        "ollama_model": "",         # empty = first installed model
        "openai_key": "",
        "openai_url": "",           # custom base URL or empty for api.openai.com
        "openai_model": "gpt-4o-mini",
        "groq_key": "",
        "groq_model": "llama-3.3-70b-versatile",
        "anthropic_key": "",
        "anthropic_model": "claude-haiku-4-5-20251001",
    },
    "audio": {
        "mic_device_index": None,     # None = system default
        "loopback_device_index": None,
        "retain_audio": False,
    },
    "consent": {
        "enabled": False,
        "snippet": ("Aviso rápido: utilizo um aplicativo local que transcreve a nossa chamada "
                    "no meu próprio computador para eu me concentrar na conversa. Nenhum áudio "
                    "é enviado para a nuvem. Me avise se preferir que eu desligue."),
    },
    "ui": {
        "default_template": "auto",
    },
}

_lock = threading.Lock()
_cache = None

def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out

def _load_locked() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                merged = _merge(DEFAULTS, json.load(f))
        except (OSError, ValueError):
            merged = DEFAULTS
        # Deep copy so the cache never aliases DEFAULTS sub-dicts.
        _cache = json.loads(json.dumps(merged))
    return _cache

def load() -> dict:
    with _lock:
        return _load_locked()

def save(patch: dict) -> dict:
    global _cache
    with _lock:
        current = _load_locked()
        _cache = json.loads(json.dumps(_merge(current, patch)))
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return _cache

# Launch at startup via HKCU Run key. Only touches the registry on Windows.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return '"{}"'.format(sys.executable)  # the packaged Muesli.exe
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pyw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    # Run keys start in System32, where "-m muesli" cannot resolve; launch.py can.
    return '"{}" "{}"'.format(pyw, os.path.join(root, "launch.py"))

def get_launch_at_startup() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_DIR_NAME)
            return True
    except OSError:
        return False

def set_launch_at_startup(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_DIR_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_DIR_NAME)
            except OSError:
                pass
    return enabled
