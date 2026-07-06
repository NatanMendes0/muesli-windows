"""Live meeting session: wires CaptureSession to StreamingTranscriber and the DB."""
import os
import threading

from . import audio, db, settings, stt


class MeetingSession:
    def __init__(self, meeting_id: int):
        self.meeting_id = meeting_id
        self.lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stopped_duration = None
        self.segments = []          # dicts, in arrival order
        self.status = "starting"    # starting | recording | stopping | stopped | error
        self.status_detail = ""
        self.error = ""
        cfg = settings.load()["audio"]
        retain_path = ""
        if cfg.get("retain_audio"):
            retain_path = os.path.join(settings.audio_dir(), f"meeting_{meeting_id}.wav")
        self.retain_path = retain_path
        self.capture = audio.CaptureSession(
            mic_index=cfg.get("mic_device_index"),
            loopback_index=cfg.get("loopback_device_index"),
            retain_path=retain_path)
        self.transcriber = None

    def start(self):
        try:
            self.capture.start()
        except Exception as e:
            self.status = "error"
            self.error = str(e)
            raise
        self.transcriber = stt.StreamingTranscriber(
            self.capture, self._on_segment, self._on_status)
        self.transcriber.start()
        self.status = "recording"

    def _on_segment(self, t0, t1, speaker, text):
        seg_id = db.add_segment(self.meeting_id, t0, t1, speaker, text)
        with self.lock:
            self.segments.append(
                {"id": seg_id, "t0": t0, "t1": t1, "speaker": speaker, "text": text})

    def _on_status(self, msg):
        self.status_detail = msg

    def poll(self, since: int):
        with self.lock:
            new = self.segments[since:]
        return {
            "status": self.status,
            "detail": self.status_detail,
            "warnings": list(self.capture.warnings),
            "elapsed": self.capture.elapsed(),
            "levels": dict(self.capture.levels),
            "segments": new,
            "total": since + len(new),
            "model": stt.model_status(),
        }

    def stop(self) -> float:
        """Idempotent and thread safe: tray quit, Stop button and window close
        can all race into this."""
        with self._stop_lock:
            if self._stopped_duration is not None:
                return self._stopped_duration
            self.status = "stopping"
            duration = self.capture.elapsed()
            self.capture.stop()
            if self.transcriber is not None:
                self.transcriber.stop()   # drains remaining audio, blocks until done
            self.capture.finalize_wav()
            self.status = "stopped"
            db.end_meeting(self.meeting_id, duration, self.retain_path)
            self._stopped_duration = duration
            return duration
