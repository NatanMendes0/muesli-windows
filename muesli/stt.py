"""Speech to text: local faster-whisper by default, OpenAI or Groq Whisper API when a key is set.

The streaming loop pulls mixed audio from the capture session, buffers it, and
flushes a chunk to the transcriber when it hears a pause (or hits a max length).
"""
import threading
import time

import numpy as np
import requests

from . import audio as audio_mod
from . import settings

SILENCE_RMS = 0.008
MIN_CHUNK_SEC = 1.5
MAX_CHUNK_SEC = 12.0
TAIL_SILENCE_SEC = 0.7

_model_lock = threading.Lock()
_model = None
_model_name = ""
_model_status = {"state": "idle", "detail": ""}  # idle | loading | ready | error


def model_status():
    return dict(_model_status, name=_model_name)


def _get_local_model():
    """Load (and cache) the faster-whisper model. Downloads on first use."""
    global _model, _model_name
    cfg = settings.load()["stt"]
    name = cfg.get("model") or "base"
    with _model_lock:
        if _model is not None and _model_name == name:
            return _model
        _model_status.update(state="loading",
                             detail=f"Loading Whisper model '{name}' (first run downloads it)")
        try:
            from faster_whisper import WhisperModel
            model_id = name
            if name in ("large-v3-turbo", "turbo"):
                try:
                    from faster_whisper.utils import available_models
                    if "large-v3-turbo" not in available_models():
                        model_id = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
                except Exception:
                    pass
            _model = WhisperModel(model_id, device="cpu",
                                  compute_type=cfg.get("compute_type") or "int8",
                                  download_root=settings.models_dir())
            _model_name = name
            _model_status.update(state="ready", detail=f"Whisper '{name}' ready")
            return _model
        except Exception as e:
            _model_status.update(state="error", detail=str(e))
            raise


def preload_model_async():
    def worker():
        try:
            if settings.load()["stt"].get("engine", "local") == "local":
                _get_local_model()
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True, name="stt-preload").start()


def _transcribe_local(pcm: np.ndarray, language: str):
    model = _get_local_model()
    lang = language or None
    segments, _info = model.transcribe(
        pcm, language=lang, beam_size=1, temperature=0.0,
        condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300})
    return [(float(s.start), float(s.end), s.text.strip()) for s in segments if s.text.strip()]


def _transcribe_cloud(pcm: np.ndarray, engine: str, language: str):
    cfg = settings.load()["llm"]
    if engine == "groq":
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        key = cfg.get("groq_key", "")
        model = "whisper-large-v3-turbo"
    else:
        url = "https://api.openai.com/v1/audio/transcriptions"
        key = cfg.get("openai_key", "")
        model = "whisper-1"
    if not key:
        raise RuntimeError(f"No API key configured for {engine} transcription")
    data = {"model": model, "response_format": "verbose_json"}
    if language:
        data["language"] = language
    resp = requests.post(
        url, headers={"Authorization": f"Bearer {key}"},
        files={"file": ("chunk.wav", audio_mod.wav_bytes(pcm), "audio/wav")},
        data=data, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    out = []
    for s in body.get("segments") or []:
        text = (s.get("text") or "").strip()
        if text:
            out.append((float(s.get("start", 0)), float(s.get("end", 0)), text))
    if not out and (body.get("text") or "").strip():
        out.append((0.0, pcm.size / audio_mod.TARGET_RATE, body["text"].strip()))
    return out


def transcribe_chunk(pcm: np.ndarray):
    """Transcribe one chunk with the configured engine, falling back to local."""
    cfg = settings.load()["stt"]
    engine = cfg.get("engine", "local")
    language = (cfg.get("language") or "").strip()
    if engine in ("openai", "groq"):
        try:
            return _transcribe_cloud(pcm, engine, language)
        except Exception:
            pass  # fall back to local so the meeting is never lost
    return _transcribe_local(pcm, language)


class StreamingTranscriber(threading.Thread):
    """Consumes a CaptureSession, emits transcript segments via callback.

    on_segment(t0, t1, speaker, text) is called from this worker thread.
    """

    def __init__(self, capture, on_segment, on_status=None):
        super().__init__(daemon=True, name="stt-worker")
        self.capture = capture
        self.on_segment = on_segment
        self.on_status = on_status or (lambda s: None)
        self.stop_flag = threading.Event()
        self.pending = np.zeros(0, dtype=np.float32)
        self.pending_offset = 0.0   # session seconds where pending buffer starts
        self.processed_sec = 0.0
        self.error = ""

    def _flush(self, final=False):
        pcm = self.pending
        if pcm.size < int(audio_mod.TARGET_RATE * 0.4):
            if final:
                self.pending = np.zeros(0, dtype=np.float32)
            return
        offset = self.pending_offset
        self.pending = np.zeros(0, dtype=np.float32)
        self.pending_offset = offset + pcm.size / audio_mod.TARGET_RATE
        rms = float(np.sqrt(np.mean(pcm ** 2)))
        if rms < SILENCE_RMS * 0.5:
            return  # pure silence, skip the model call
        try:
            for (s0, s1, text) in transcribe_chunk(pcm):
                t0, t1 = offset + s0, offset + s1
                speaker = self.capture.speaker_for(t0, t1)
                self.on_segment(t0, t1, speaker, text)
        except Exception as e:
            self.error = str(e)
            self.on_status(f"Transcription error: {e}")

    def run(self):
        stt_ok = True
        try:
            transcribe_chunk(np.zeros(int(audio_mod.TARGET_RATE * 0.5), dtype=np.float32))
            self.on_status("")
        except Exception as e:
            # Keep consuming audio anyway: pull_mixed is what trims capture
            # buffers and writes the retained WAV, so it must keep running.
            stt_ok = False
            self.error = str(e)
            self.on_status(f"Transcription unavailable (audio still captured): {e}")
        hop = int(audio_mod.TARGET_RATE * TAIL_SILENCE_SEC)
        while not self.stop_flag.is_set():
            time.sleep(0.35)
            fresh = self.capture.pull_mixed()
            if not stt_ok:
                continue
            if fresh.size:
                self.pending = np.concatenate([self.pending, fresh])
            dur = self.pending.size / audio_mod.TARGET_RATE
            if dur >= MAX_CHUNK_SEC:
                self._flush()
                continue
            if dur >= MIN_CHUNK_SEC:
                tail = self.pending[-hop:]
                if tail.size >= hop and float(np.sqrt(np.mean(tail ** 2))) < SILENCE_RMS:
                    self._flush()
        # Final drain after stop.
        fresh = self.capture.pull_mixed()
        if stt_ok:
            if fresh.size:
                self.pending = np.concatenate([self.pending, fresh])
            self._flush(final=True)

    def stop(self):
        self.stop_flag.set()
        self.join(timeout=90)
