"""Speech to text: local faster-whisper by default, OpenRouter, OpenAI or Groq Whisper API when configured.

The streaming loop pulls mixed audio from the capture session, buffers it, and
flushes a chunk to the transcriber when it hears a pause (or hits a max length).
"""
import base64
import os
import sys
import threading
import time

import numpy as np
import requests

from . import audio as audio_mod
from . import keypool, settings

SILENCE_RMS = 0.008
MIN_CHUNK_SEC = 1.5
MAX_CHUNK_SEC = 12.0
TAIL_SILENCE_SEC = 0.7

_model_lock = threading.Lock()
_model = None
_model_name = ""
_model_status = {"state": "idle", "detail": ""}  # idle | loading | ready | error
_openrouter_no_verbose = set()
_last_cloud_warning = ""


def _cuda_libs_dir():
    d = os.path.join(settings.data_dir(), "cuda_libs")
    if os.path.isdir(d):
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        add_dll = getattr(os, "add_dll_directory", None)
        if add_dll is not None:
            try:
                add_dll(d)
            except OSError:
                pass


_cuda_libs_dir()


def _http_with_ssl_fallback(method: str, url: str, **kwargs):
    fn = requests.get if method == "GET" else requests.post
    try:
        return fn(url, **kwargs)
    except requests.exceptions.SSLError:
        kwargs["verify"] = False
        return fn(url, **kwargs)


def _extract_api_error(resp) -> str:
    try:
        j = resp.json()
        err = j.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if j.get("message"):
            return str(j["message"])
    except Exception:
        pass
    txt = (resp.text or "").strip()
    return txt[:180] if txt else f"HTTP {resp.status_code}"


def _parse_retry_after(resp) -> float:
    val = (resp.headers.get("retry-after") or "").strip()
    try:
        return max(1.0, float(val))
    except (TypeError, ValueError):
        return 0.0


def _resolve_cloud_keys_and_model(engine: str):
    all_cfg = settings.load()
    stt_cfg = all_cfg["stt"]
    llm_cfg = all_cfg["llm"]
    if engine == "openrouter":
        keys = keypool.collect_keys(stt_cfg, "openrouter_key", "openrouter_keys")
        if not keys:
            keys = keypool.collect_keys(llm_cfg, "openrouter_key", "openrouter_keys")
        model = (stt_cfg.get("openrouter_model") or "openai/whisper-large-v3-turbo").strip()
        return keys, model
    if engine == "groq":
        keys = keypool.collect_keys(stt_cfg, "groq_key", "groq_keys")
        if not keys:
            keys = keypool.collect_keys(llm_cfg, "groq_key", "groq_keys")
        model = (stt_cfg.get("groq_model") or "whisper-large-v3-turbo").strip()
        return keys, model
    keys = keypool.collect_keys(stt_cfg, "openai_key", "openai_keys")
    if not keys:
        keys = keypool.collect_keys(llm_cfg, "openai_key", "openai_keys")
    model = (stt_cfg.get("openai_model") or "whisper-1").strip()
    return keys, model


def reset_status():
    global _last_cloud_warning
    _last_cloud_warning = ""
    if _model_status.get("state") != "loading":
        _model_status.update(state="idle", detail="")


def model_status():
    cfg = settings.load()["stt"]
    engine = cfg.get("engine", "local")
    if engine in ("openrouter", "openai", "groq"):
        label = {"openrouter": "OpenRouter", "openai": "OpenAI", "groq": "Groq"}[engine]
        keys, model = _resolve_cloud_keys_and_model(engine)
        if _model_status["state"] in ("error", "loading"):
            return dict(_model_status, name=model)
        if not keys:
            return {"state": "error", "detail": f"{label} sem chave configurada", "name": model}
        extra = []
        try:
            sec = int(cfg.get("cloud_interval_sec") or 12)
        except (TypeError, ValueError):
            sec = 12
        if sec > 15:
            extra.append(f"Lote {max(1, int(round(sec / 60)))}m")
        if len(keys) > 1:
            extra.append(f"{len(keys)} chaves")
        suffix = f" ({' · '.join(extra)})" if extra else ""
        detail = _last_cloud_warning or f"{label}: {model}{suffix}"
        return {"state": "ready", "detail": detail, "name": model}
    return dict(_model_status, name=_model_name)


def _get_local_model():
    """Load (and cache) the faster-whisper model. Downloads on first use."""
    global _model, _model_name
    cfg = settings.load()["stt"]
    name = cfg.get("model") or "base"
    wanted_device = cfg.get("device") or "cpu"
    key = f"{name}:{wanted_device}"
    with _model_lock:
        if _model is not None and _model_name == key:
            return _model
        if _model is not None:
            del _model
            _model = None
        _model_status.update(state="loading",
                             detail=f"Carregando Whisper '{name}' ({wanted_device.upper()})...")
        try:
            _cuda_libs_dir()
            from faster_whisper import WhisperModel
            model_id = name
            if name in ("large-v3-turbo", "turbo"):
                try:
                    from faster_whisper.utils import available_models
                    if "large-v3-turbo" not in available_models():
                        model_id = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
                except Exception:
                    pass

            target_device = "cpu"
            cuda_warn = ""
            if wanted_device in ("cuda", "auto"):
                can_cuda = False
                try:
                    import ctranslate2
                    can_cuda = ctranslate2.get_cuda_device_count() > 0
                except Exception:
                    pass

                if can_cuda:
                    # Pascal (CC 6.1, e.g. Quadro P400 / GTX 10xx) runs best with int8_float32/int8;
                    # newer GPUs (Turing/Ampere+) prefer float16. Try both gracefully.
                    for ct in ("float16", "int8_float32", "int8"):
                        try:
                            _model = WhisperModel(model_id, device="cuda",
                                                  compute_type=ct,
                                                  download_root=settings.models_dir())
                            target_device = "cuda"
                            break
                        except Exception as e:
                            cuda_warn = str(e)
                            _model = None

                if _model is None:
                    _model = WhisperModel(model_id, device="cpu",
                                          compute_type=cfg.get("compute_type") or "int8",
                                          download_root=settings.models_dir())
                    target_device = "cpu"
            else:
                _model = WhisperModel(model_id, device="cpu",
                                      compute_type=cfg.get("compute_type") or "int8",
                                      download_root=settings.models_dir())

            _model_name = key
            if target_device == "cuda":
                hw_info = "GPU NVIDIA (CUDA)"
            elif wanted_device == "cuda" and cuda_warn:
                hw_info = "CPU (CUDA indisponível)"
            else:
                hw_info = "CPU"
            _model_status.update(state="ready", detail=f"Whisper '{name}' ({hw_info})")
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


def _parse_cloud_segments(body: dict, pcm_size: int):
    out = []
    for s in body.get("segments") or []:
        text = (s.get("text") or "").strip()
        if text:
            out.append((float(s.get("start", 0)), float(s.get("end", 0)), text))
    if not out and (body.get("text") or "").strip():
        out.append((0.0, pcm_size / audio_mod.TARGET_RATE, body["text"].strip()))
    return out


def _transcribe_openrouter(pcm: np.ndarray, keys: list, model: str, language: str):
    url = "https://openrouter.ai/api/v1/audio/transcriptions"
    b64_audio = base64.b64encode(audio_mod.wav_bytes(pcm)).decode("ascii")
    ordered = keypool.rotator.ordered_keys("stt:openrouter", keys)
    last_err = ""
    for key in ordered:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/NatanMendes0/muesli-windows",
            "X-Title": "Muesli",
        }
        payload = {
            "model": model,
            "input_audio": {
                "data": b64_audio,
                "format": "wav",
            },
            "temperature": 0.0,
        }
        if language:
            payload["language"] = language
        if model not in _openrouter_no_verbose:
            payload["response_format"] = "verbose_json"
        try:
            resp = _http_with_ssl_fallback("POST", url, headers=headers, json=dict(payload), timeout=150)
            if resp.status_code == 400 and "response_format" in payload:
                _openrouter_no_verbose.add(model)
                payload.pop("response_format", None)
                resp = _http_with_ssl_fallback("POST", url, headers=headers, json=dict(payload), timeout=150)
            if resp.ok:
                keypool.rotator.mark_success("stt:openrouter", key, keys)
                return _parse_cloud_segments(resp.json(), pcm.size)
            keypool.rotator.mark_failure("stt:openrouter", key, resp.status_code, _parse_retry_after(resp))
            last_err = f"OpenRouter ({resp.status_code}): {_extract_api_error(resp)}"
        except requests.RequestException as e:
            keypool.rotator.mark_failure("stt:openrouter", key, 0)
            last_err = f"OpenRouter: {e}"
    raise RuntimeError(last_err or "OpenRouter falhou em todas as chaves configuradas.")


def _transcribe_cloud(pcm: np.ndarray, engine: str, language: str):
    keys, model = _resolve_cloud_keys_and_model(engine)
    if not keys:
        raise RuntimeError(f"Chave de API ({engine}) não configurada em Configurações → Transcrição.")

    if engine == "openrouter":
        return _transcribe_openrouter(pcm, keys, model, language)

    if engine == "groq":
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
    else:
        url = "https://api.openai.com/v1/audio/transcriptions"

    wav_data = audio_mod.wav_bytes(pcm)
    pool_name = f"stt:{engine}"
    ordered = keypool.rotator.ordered_keys(pool_name, keys)
    last_err = ""
    for key in ordered:
        data = {"model": model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        try:
            resp = _http_with_ssl_fallback(
                "POST",
                url,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("chunk.wav", wav_data, "audio/wav")},
                data=data,
                timeout=150,
            )
            if resp.status_code == 400:
                data.pop("response_format", None)
                resp = _http_with_ssl_fallback(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("chunk.wav", wav_data, "audio/wav")},
                    data=data,
                    timeout=150,
                )
            if resp.ok:
                keypool.rotator.mark_success(pool_name, key, keys)
                return _parse_cloud_segments(resp.json(), pcm.size)
            keypool.rotator.mark_failure(pool_name, key, resp.status_code, _parse_retry_after(resp))
            last_err = f"{engine.upper()} ({resp.status_code}): {_extract_api_error(resp)}"
        except requests.RequestException as e:
            keypool.rotator.mark_failure(pool_name, key, 0)
            last_err = f"{engine.upper()}: {e}"
    raise RuntimeError(last_err or f"{engine.upper()} falhou em todas as chaves configuradas.")


def preflight_engine() -> str:
    """Validate STT readiness at meeting start without sending dummy paid audio."""
    global _last_cloud_warning
    _last_cloud_warning = ""
    cfg = settings.load()["stt"]
    engine = cfg.get("engine", "local")
    if engine == "local":
        _get_local_model()
        return ""

    label = {"openrouter": "OpenRouter", "openai": "OpenAI", "groq": "Groq"}.get(engine, engine)
    keys, model = _resolve_cloud_keys_and_model(engine)
    fallback = cfg.get("cloud_fallback", True)

    if not keys:
        msg = f"Chave de API ({label}) não configurada em Configurações → Transcrição."
        if fallback:
            _last_cloud_warning = f"{label} sem chave — usando Whisper local"
            _get_local_model()
            return f"Aviso: {msg} Usando Whisper local como reserva."
        _model_status.update(state="error", detail=msg)
        raise RuntimeError(msg)

    if engine == "openrouter":
        any_valid = False
        last_err = ""
        for key in keys:
            try:
                r = _http_with_ssl_fallback(
                    "GET",
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=5,
                )
                if r.status_code in (401, 403):
                    keypool.rotator.mark_failure("stt:openrouter", key, r.status_code)
                    last_err = f"Chave OpenRouter inválida ou desativada (HTTP {r.status_code})."
                    continue
                if r.status_code == 200:
                    d = r.json().get("data") or {}
                    rem = d.get("limit_remaining")
                    if rem is not None and float(rem) <= 0:
                        keypool.rotator.mark_failure("stt:openrouter", key, 402)
                        last_err = "Limite de crédito da chave OpenRouter esgotado ($0 restante)."
                        continue
                any_valid = True
                break
            except Exception:
                any_valid = True  # Transient network glitch; try at runtime
                break
        if not any_valid and last_err:
            if fallback:
                _last_cloud_warning = f"OpenRouter indisponível ({last_err}) — usando local"
                _get_local_model()
                return f"Aviso OpenRouter: {last_err} Usando Whisper local como reserva."
            _model_status.update(state="error", detail=last_err)
            raise RuntimeError(last_err)

    _model_status.update(state="ready", detail=f"{label}: {model}")
    return ""


def transcribe_chunk(pcm: np.ndarray):
    """Transcribe one chunk with the configured engine, falling back to local if enabled."""
    global _last_cloud_warning
    cfg = settings.load()["stt"]
    engine = cfg.get("engine", "local")
    language = (cfg.get("language") or "").strip()
    if engine in ("openrouter", "openai", "groq"):
        try:
            segs = _transcribe_cloud(pcm, engine, language)
            _last_cloud_warning = ""
            return segs
        except Exception as e:
            if not cfg.get("cloud_fallback", True):
                _model_status.update(state="error", detail=str(e))
                raise
            _last_cloud_warning = f"Erro {engine} ({e}) — usando local"
    return _transcribe_local(pcm, language)


def _has_speech(pcm: np.ndarray) -> bool:
    if pcm.size == 0:
        return False
    overall_rms = float(np.sqrt(np.mean(pcm ** 2)))
    if overall_rms >= SILENCE_RMS * 0.5:
        return True
    # For multi-minute cloud batches, inspect 0.5s sub-windows so brief speech isn't diluted by silence
    win = int(audio_mod.TARGET_RATE * 0.5)
    n_blocks = pcm.size // win
    if n_blocks >= 2:
        blocks = pcm[:n_blocks * win].reshape(n_blocks, win)
        max_rms = float(np.sqrt(np.mean(blocks ** 2, axis=1)).max())
        if max_rms >= SILENCE_RMS * 0.6:
            return True
    return False


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
        self._pending_chunks = []
        self._pending_samples = 0
        self.pending_offset = 0.0   # session seconds where pending buffer starts
        self.processed_sec = 0.0
        self.error = ""

    def _append_audio(self, fresh: np.ndarray):
        if fresh.size:
            self._pending_chunks.append(fresh)
            self._pending_samples += fresh.size

    def _tail_rms(self, hop: int) -> float:
        if self._pending_samples < hop:
            return 1.0
        collected = []
        need = hop
        for chunk in reversed(self._pending_chunks):
            if chunk.size >= need:
                collected.append(chunk[-need:])
                break
            collected.append(chunk)
            need -= chunk.size
        tail = np.concatenate(list(reversed(collected)))
        return float(np.sqrt(np.mean(tail ** 2)))

    def _flush(self, final=False, batch_mode=False):
        if self._pending_samples < int(audio_mod.TARGET_RATE * 0.4):
            if final:
                self._pending_chunks = []
                self._pending_samples = 0
            return
        pcm = np.concatenate(self._pending_chunks)
        self._pending_chunks = []
        self._pending_samples = 0
        offset = self.pending_offset
        dur_sec = pcm.size / audio_mod.TARGET_RATE
        self.pending_offset = offset + dur_sec
        if not _has_speech(pcm):
            self.on_status(_last_cloud_warning or "")
            return  # pure silence, skip the model call
        if batch_mode and dur_sec >= 15.0:
            self.on_status(f"Enviando lote de áudio ({int(round(dur_sec))}s) para transcrição...")
        try:
            for (s0, s1, text) in transcribe_chunk(pcm):
                t0, t1 = offset + s0, offset + s1
                speaker = self.capture.speaker_for(t0, t1)
                self.on_segment(t0, t1, speaker, text)
            self.on_status(_last_cloud_warning or "")
        except Exception as e:
            self.error = str(e)
            self.on_status(f"Erro na transcrição: {e}")

    def run(self):
        stt_ok = True
        try:
            warn = preflight_engine()
            self.on_status(warn or "")
        except Exception as e:
            # Keep consuming audio anyway: pull_mixed is what trims capture
            # buffers and writes the retained WAV, so it must keep running.
            stt_ok = False
            self.error = str(e)
            self.on_status(f"Transcrição indisponível (áudio ainda está sendo gravado): {e}")

        hop = int(audio_mod.TARGET_RATE * TAIL_SILENCE_SEC)
        last_status_tick = 0.0
        while not self.stop_flag.is_set():
            time.sleep(0.35)
            fresh = self.capture.pull_mixed()
            if not stt_ok:
                continue
            self._append_audio(fresh)
            dur = self._pending_samples / audio_mod.TARGET_RATE

            cfg = settings.load()["stt"]
            engine = cfg.get("engine", "local")
            try:
                cloud_sec = int(cfg.get("cloud_interval_sec") or 12)
            except (TypeError, ValueError):
                cloud_sec = 12

            is_batch = (engine != "local" and cloud_sec > 15)
            if is_batch:
                max_sec = min(float(cloud_sec), 540.0)
                min_sec = max(12.0, max_sec - 8.0)
            else:
                max_sec = MAX_CHUNK_SEC
                min_sec = MIN_CHUNK_SEC

            # Safety byte cap (~18 MB raw WAV PCM, well under Groq/OpenAI 25 MB limit)
            if dur >= max_sec or (self._pending_samples * 2) >= 18_000_000:
                self._flush(batch_mode=is_batch)
                last_status_tick = 0.0
                continue
            if dur >= min_sec and self._tail_rms(hop) < SILENCE_RMS:
                self._flush(batch_mode=is_batch)
                last_status_tick = 0.0
                continue

            now = time.time()
            if is_batch and now - last_status_tick >= 1.0:
                last_status_tick = now
                if not _last_cloud_warning:
                    rem = max(0, int(round(max_sec - dur)))
                    mins_label = max(1, int(round(max_sec / 60)))
                    self.on_status(
                        f"Modo lote ({mins_label} min): próximo envio em "
                        f"{rem // 60:02d}:{rem % 60:02d} (ou ao clicar em Parar)"
                    )

        # Final drain after stop.
        fresh = self.capture.pull_mixed()
        if stt_ok:
            self._append_audio(fresh)
            cfg = settings.load()["stt"]
            is_batch = (cfg.get("engine", "local") != "local" and int(cfg.get("cloud_interval_sec") or 12) > 15)
            self._flush(final=True, batch_mode=is_batch)

    def stop(self):
        self.stop_flag.set()
        self.join(timeout=180)
