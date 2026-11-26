import queue
import threading
import time
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from muesli import api, audio, db, settings, stt


class AudioStream:
    def __init__(self):
        self.samples = queue.Queue()
        self.closed = False

    def read(self, frames, exception_on_overflow):
        try:
            return self.samples.get(timeout=0.01)
        except queue.Empty:
            return b""

    def write(self, data):
        time.sleep(0.01)

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class PortAudio:
    def __init__(self):
        self.streams = {"mic": [], "loop": [], "output": []}
        self.inputs_available = True

    def get_device_info_by_index(self, index):
        return {"index": index, "defaultSampleRate": audio.TARGET_RATE, "maxInputChannels": 1}

    def get_default_input_device_info(self):
        return self.get_device_info_by_index(0)

    def get_default_wasapi_loopback(self):
        return self.get_device_info_by_index(1)

    def get_default_output_device_info(self):
        return self.get_device_info_by_index(2)

    def open(self, **options):
        if options.get("input") and not self.inputs_available:
            raise OSError("device unavailable")
        key = ("mic", "loop")[options["input_device_index"]] if options.get("input") else "output"
        stream = AudioStream()
        self.streams[key].append(stream)
        return stream

    def terminate(self):
        pass


def wait_for(check):
    deadline = time.monotonic() + 4
    while not check():
        assert time.monotonic() < deadline, "timed out waiting for audio or transcription"
        time.sleep(0.01)


@pytest.mark.parametrize("resume", [False, True])
def test_pause_preserves_meeting_and_excludes_paused_audio(tmp_path, monkeypatch, resume):
    device = PortAudio()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(audio, "PYAUDIO_OK", True)
    monkeypatch.setattr(audio, "pyaudio", SimpleNamespace(PyAudio=lambda: device, paInt16=8))
    monkeypatch.setattr(audio, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: None))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(db, "_fts_available", None)
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(settings, "audio_dir", lambda: str(tmp_path))
    monkeypatch.setattr(stt, "preflight_engine", lambda: "")
    transcribed = []

    def transcribe(pcm):
        transcribed.append(pcm.copy())
        yield 0.0, 0.5, "Antes da pausa"
        if resume:
            yield 0.5, 1.0, "Depois da pausa"

    monkeypatch.setattr(stt, "transcribe_chunk", transcribe)
    db.init()
    backend = api.Api()
    backend.save_settings({"audio": {"retain_audio": True}, "llm": {"provider": "none"}, "minutes": {"enabled": False}})
    start = backend.start_meeting("Teste da pausa", "auto")
    assert start["ok"], start
    meeting_id = start["meeting_id"]
    session = backend._session
    capture = session.capture
    original_readers = dict(capture.readers)
    half_second = audio.TARGET_RATE // 2
    quarter_second = half_second // 2

    def feed(key, count, amplitude):
        device.streams[key][-1].samples.put(np.full(count, amplitude, dtype=np.int16).tobytes())

    try:
        feed("mic", half_second, 4096)
        feed("loop", quarter_second, 2048)
        wait_for(lambda: capture.totals == {"mic": half_second, "loop": quarter_second})
        clock.now = 0.5
        assert backend.set_recording_paused(meeting_id, True) == {"ok": True, "status": "paused", "elapsed": 0.5}
        assert all(streams[0].closed for streams in device.streams.values())
        assert not any(reader.is_alive() for reader in original_readers.values())
        feed("mic", half_second, 16000)
        feed("loop", half_second, 16000)
        clock.now = 20.5
        snapshot = backend.poll_session(0)
        assert snapshot["status"] == "paused" and snapshot["elapsed"] == 0.5
        assert snapshot["levels"] == {"mic": 0.0, "loop": 0.0}
        app_state = backend.get_state()
        assert app_state["recording"] and app_state["paused"] and app_state["active_meeting_id"] == meeting_id
        assert not backend.start_meeting("Outra reunião", "auto")["ok"]
        assert not backend.delete_meeting(meeting_id)["ok"]
        assert not backend.set_recording_paused(meeting_id + 1, False)["ok"]
        assert backend.save_notes(meeting_id, "Anotação durante a pausa")["ok"]

        if resume:
            device.inputs_available = False
            assert not backend.set_recording_paused(meeting_id, False)["ok"]
            assert backend.poll_session(0)["status"] == "paused"
            device.inputs_available = True
            assert backend.set_recording_paused(meeting_id, False) == {"ok": True, "status": "recording", "elapsed": 0.5}
            for reader in original_readers.values():
                capture._push(reader, np.ones(half_second, dtype=np.float32))
            feed("mic", half_second, 2048)
            feed("loop", half_second, 1024)
            wait_for(lambda: capture.totals == {"mic": audio.TARGET_RATE, "loop": audio.TARGET_RATE})
            clock.now = 21.0
    finally:
        stop = backend.stop_meeting()
        wait_for(lambda: backend.job_status(stop["job_id"])["state"] != "running")
        result = backend.job_status(stop["job_id"])
        assert result["state"] == "done", result
        finalize_id = result["result"]["finalize_job_id"]
        wait_for(lambda: backend.job_status(finalize_id)["state"] != "running")
        assert backend.job_status(finalize_id)["state"] == "done"

    meeting = backend.get_meeting(meeting_id)
    assert meeting["duration_sec"] == (1.0 if resume else 0.5)
    assert meeting["notes_md"] == "Anotação durante a pausa"
    assert [seg["t0"] for seg in meeting["segments"]] == ([0.0, 0.5] if resume else [0.0])
    expected = np.repeat([6144, 4096, 3072], [quarter_second, quarter_second, half_second if resume else 0])
    (pcm,) = transcribed
    np.testing.assert_array_equal(pcm, expected / 32768.0)
    with wave.open(meeting["audio_path"], "rb") as wav:
        assert wav.getnframes() == expected.size
        np.testing.assert_array_equal(np.frombuffer(wav.readframes(expected.size), dtype=np.int16), expected - 1)
    assert backend.poll_session(0) == {"status": "none"}
