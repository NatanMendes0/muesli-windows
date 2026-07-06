"""Audio capture: microphone plus WASAPI loopback (system audio), mixed to 16 kHz mono.

Uses PyAudioWPatch, which exposes Windows WASAPI loopback devices as input devices.
Each source runs in its own reader thread at the device's native rate and is
resampled to 16 kHz mono float32. The session keeps a growing buffer per source
plus a coarse energy timeline used to tag transcript segments as Me / Them.
"""
import io
import math
import os
import threading
import time
import wave

import numpy as np

TARGET_RATE = 16000
ENERGY_HOP = 0.25          # seconds per energy timeline bucket
STALL_SEC = 5.0            # a source with no data for this long stops gating the mixer

try:
    import pyaudiowpatch as pyaudio
    PYAUDIO_OK = True
    PYAUDIO_ERR = ""
except Exception as e:  # pragma: no cover
    pyaudio = None
    PYAUDIO_OK = False
    PYAUDIO_ERR = str(e)


def _resample_mono(data: np.ndarray, src_rate: int) -> np.ndarray:
    """data: float32 mono at src_rate -> float32 mono at TARGET_RATE (linear interp)."""
    if src_rate == TARGET_RATE or data.size == 0:
        return data
    n_out = int(round(data.size * TARGET_RATE / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=data.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, data).astype(np.float32)


def list_devices():
    """Return dict with mics, loopbacks and defaults for the settings UI."""
    if not PYAUDIO_OK:
        return {"ok": False, "error": PYAUDIO_ERR, "mics": [], "loopbacks": []}
    p = pyaudio.PyAudio()
    try:
        mics, loopbacks = [], []
        default_mic = None
        try:
            default_mic = p.get_default_input_device_info()["index"]
        except OSError:
            pass
        default_loop = None
        try:
            default_loop = p.get_default_wasapi_loopback()["index"]
        except (OSError, LookupError):
            pass
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            entry = {"index": i, "name": info.get("name", f"Device {i}"),
                     "rate": int(info.get("defaultSampleRate", 48000)),
                     "channels": int(info.get("maxInputChannels", 1))}
            if info.get("isLoopbackDevice"):
                entry["default"] = (i == default_loop)
                loopbacks.append(entry)
            else:
                entry["default"] = (i == default_mic)
                mics.append(entry)
        return {"ok": True, "error": "", "mics": mics, "loopbacks": loopbacks}
    finally:
        p.terminate()


class _SilenceKeeper(threading.Thread):
    """Plays silence on the default output device while recording.

    WASAPI loopback streams only deliver data while the render device is active;
    with nothing playing, reads block forever, and the render engine starting up
    mid-read can crash PortAudio. Feeding zeros keeps the device active the whole
    time, which makes loopback capture reliable. It is inaudible.
    """

    def __init__(self):
        super().__init__(daemon=True, name="silence-keeper")
        self.stop_flag = threading.Event()
        self.error = ""

    def run(self):
        p = pyaudio.PyAudio()
        stream = None
        try:
            out = p.get_default_output_device_info()
            rate = int(out["defaultSampleRate"])
            n = int(rate * 0.1)
            stream = p.open(format=pyaudio.paInt16, channels=1, rate=rate,
                            output=True, frames_per_buffer=n,
                            output_device_index=int(out["index"]))
            zeros = b"\x00" * (n * 2)
            while not self.stop_flag.is_set():
                stream.write(zeros)
        except Exception as e:
            self.error = str(e)
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except OSError:
                pass
            p.terminate()


class _SourceReader(threading.Thread):
    """Reads one input stream, resamples to 16 kHz mono, appends to shared buffer."""

    def __init__(self, session, key: str, device_index, rate: int, channels: int):
        super().__init__(daemon=True, name=f"audio-{key}")
        self.session = session
        self.key = key
        self.device_index = device_index
        self.rate = rate
        self.channels = channels
        self.stop_flag = threading.Event()
        self.error = ""

    def run(self):
        p = pyaudio.PyAudio()
        stream = None
        frames = max(256, int(self.rate * 0.1))
        try:
            stream = p.open(format=pyaudio.paInt16, channels=self.channels,
                            rate=self.rate, input=True, frames_per_buffer=frames,
                            input_device_index=self.device_index)
            while not self.stop_flag.is_set():
                raw = stream.read(frames, exception_on_overflow=False)
                pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if self.channels > 1:
                    pcm = pcm.reshape(-1, self.channels).mean(axis=1)
                pcm16k = _resample_mono(pcm, self.rate)
                self.session._push(self.key, pcm16k)
        except Exception as e:
            self.error = str(e)
            self.session._source_failed(self.key, str(e))
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except OSError:
                pass
            p.terminate()


class CaptureSession:
    """Owns mic and loopback readers for one recording. Thread safe."""

    def __init__(self, mic_index=None, loopback_index=None, retain_path: str = ""):
        self.lock = threading.Lock()
        self.buffers = {"mic": np.zeros(0, dtype=np.float32),
                        "loop": np.zeros(0, dtype=np.float32)}
        self.totals = {"mic": 0, "loop": 0}      # total samples ever pushed
        self.energy = {"mic": [], "loop": []}    # rms per ENERGY_HOP bucket
        self._energy_accum = {"mic": [0.0, 0], "loop": [0.0, 0]}  # sumsq, count
        self.consumed = 0                        # samples handed to the mixer
        self.readers = {}
        self.warnings = []
        self.started_at = None
        self.active = False
        self.mic_index = mic_index
        self.loopback_index = loopback_index
        self.retain_path = retain_path
        self._wav = None
        self._wav_lock = threading.Lock()
        self.levels = {"mic": 0.0, "loop": 0.0}  # recent rms for UI meters
        self._keeper = None
        self._keeper_restarts = 0
        self.last_push = {"mic": 0.0, "loop": 0.0}
        self.stalled = {"mic": False, "loop": False}

    def start(self):
        if not PYAUDIO_OK:
            raise RuntimeError("PyAudioWPatch not available: " + PYAUDIO_ERR)
        p = pyaudio.PyAudio()
        try:
            mic_info = None
            try:
                if self.mic_index is not None:
                    mic_info = p.get_device_info_by_index(int(self.mic_index))
                else:
                    mic_info = p.get_default_input_device_info()
            except OSError as e:
                self.warnings.append("No microphone available: " + str(e))
            loop_info = None
            try:
                if self.loopback_index is not None:
                    loop_info = p.get_device_info_by_index(int(self.loopback_index))
                else:
                    loop_info = p.get_default_wasapi_loopback()
            except (OSError, LookupError) as e:
                self.warnings.append("No system loopback device: " + str(e))
        finally:
            p.terminate()

        if mic_info is None and loop_info is None:
            raise RuntimeError("No usable audio devices (mic and loopback both missing).")

        self.started_at = time.time()
        self.active = True
        if loop_info is not None:
            # Must be running before the loopback stream opens; see _SilenceKeeper.
            self._keeper = _SilenceKeeper()
            self._keeper.start()
            time.sleep(0.4)
        if mic_info is not None:
            r = _SourceReader(self, "mic", int(mic_info["index"]),
                              int(mic_info["defaultSampleRate"]),
                              max(1, min(2, int(mic_info["maxInputChannels"]))))
            self.readers["mic"] = r
            r.start()
        if loop_info is not None:
            r = _SourceReader(self, "loop", int(loop_info["index"]),
                              int(loop_info["defaultSampleRate"]),
                              max(1, min(2, int(loop_info["maxInputChannels"]))))
            self.readers["loop"] = r
            r.start()
        now = time.time()
        self.last_push = {"mic": now, "loop": now}
        if self.retain_path:
            self._wav = wave.open(self.retain_path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(TARGET_RATE)

    def _push(self, key: str, pcm16k: np.ndarray):
        with self.lock:
            self.last_push[key] = time.time()
            self.buffers[key] = np.concatenate([self.buffers[key], pcm16k])
            self.totals[key] += pcm16k.size
            # Energy timeline in ENERGY_HOP buckets for speaker tagging.
            acc = self._energy_accum[key]
            hop = int(TARGET_RATE * ENERGY_HOP)
            i = 0
            while i < pcm16k.size:
                take = min(pcm16k.size - i, hop - acc[1])
                chunk = pcm16k[i:i + take]
                acc[0] += float(np.sum(chunk * chunk))
                acc[1] += take
                i += take
                if acc[1] >= hop:
                    self.energy[key].append((acc[0] / max(1, acc[1])) ** 0.5)
                    acc[0], acc[1] = 0.0, 0
            if pcm16k.size:
                self.levels[key] = float(np.sqrt(np.mean(pcm16k ** 2)))

    def _source_failed(self, key: str, err: str):
        with self.lock:
            self.warnings.append(f"Audio source '{key}' stopped: {err}")

    def _gates(self, key: str, now: float) -> bool:
        """Does this source still constrain how far the mixer may advance?

        A dead, errored or stalled source must not hold back the other one,
        otherwise a stuck loopback stream silently freezes transcription.
        """
        r = self.readers.get(key)
        if r is None or r.error or not r.is_alive():
            return False
        if self.stalled[key]:
            return False
        if now - self.last_push[key] > STALL_SEC:
            self.stalled[key] = True
            name = "System audio" if key == "loop" else "Microphone"
            self.warnings.append(
                f"{name} stopped delivering data; continuing with the other source.")
            return False
        return True

    def _check_keeper(self):
        """Restart the silence keeper if it died (e.g. output device unplugged)."""
        if not self.active or self._keeper is None or self._keeper.is_alive():
            return
        if self._keeper_restarts >= 3:
            return
        self._keeper_restarts += 1
        with self.lock:
            self.warnings.append(
                "Output device hiccup ({}); restarting loopback keeper (attempt {}).".format(
                    self._keeper.error or "stopped", self._keeper_restarts))
        self._keeper = _SilenceKeeper()
        self._keeper.start()

    def pull_mixed(self) -> np.ndarray:
        """Return newly available mixed 16 kHz mono audio since the last call."""
        self._check_keeper()
        now = time.time()
        with self.lock:
            gating = [self.totals[k] for k in ("mic", "loop") if self._gates(k, now)]
            avail = min(gating) if gating else max(self.totals.values() or [0])
            n = avail - self.consumed
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            out = np.zeros(n, dtype=np.float32)
            for key in ("mic", "loop"):
                buf = self.buffers[key]
                start = self.consumed - (self.totals[key] - buf.size)
                if 0 <= start < buf.size:
                    take = min(n, buf.size - start)
                    out[:take] += buf[start:start + take]
            self.consumed += n
            # Trim buffers so memory stays bounded on long meetings.
            keep_from = self.consumed
            for key in ("mic", "loop"):
                buf = self.buffers[key]
                offset = self.totals[key] - buf.size
                cut = keep_from - offset
                if cut > 0:
                    self.buffers[key] = buf[cut:]
            np.clip(out, -1.0, 1.0, out=out)
        if out.size:
            with self._wav_lock:
                if self._wav is not None:
                    self._wav.writeframes((out * 32767).astype(np.int16).tobytes())
        return out

    def speaker_for(self, t0: float, t1: float) -> str:
        """Tag a time span (seconds from session start) as Me / Them by source energy."""
        with self.lock:
            i0 = max(0, int(t0 / ENERGY_HOP))
            i1 = max(i0 + 1, math.ceil(t1 / ENERGY_HOP))
            mic = self.energy["mic"][i0:i1]
            loop = self.energy["loop"][i0:i1]
        mic_e = float(np.mean(mic)) if mic else 0.0
        loop_e = float(np.mean(loop)) if loop else 0.0
        if mic_e < 1e-4 and loop_e < 1e-4:
            return ""
        if mic_e > loop_e * 1.5:
            return "Me"
        if loop_e > mic_e * 1.5:
            return "Them"
        return ""

    def stop(self):
        self.active = False
        # A blocked loopback read only returns while the render device is active,
        # so make sure the keeper is running before joining the readers.
        if (self._keeper is not None and not self._keeper.is_alive()
                and any(r.is_alive() for r in self.readers.values())):
            self._keeper = _SilenceKeeper()
            self._keeper.start()
        for r in self.readers.values():
            r.stop_flag.set()
        for r in self.readers.values():
            r.join(timeout=2.0)
        if self._keeper is not None:
            self._keeper.stop_flag.set()
            self._keeper.join(timeout=2.0)
            self._keeper = None
        # The WAV stays open: the transcriber's final drain still writes to it.
        # session.stop() calls finalize_wav() once the drain is done.

    def finalize_wav(self):
        with self._wav_lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except OSError:
                    pass
                self._wav = None

    def elapsed(self) -> float:
        return (time.time() - self.started_at) if self.started_at else 0.0


def wav_bytes(pcm: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    """Encode float32 mono PCM as 16 bit WAV bytes (for cloud STT uploads)."""
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
    w.close()
    return buf.getvalue()
