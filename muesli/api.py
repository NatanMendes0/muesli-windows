"""JS bridge exposed to the UI via pywebview. All methods return JSON-safe values.

Slow operations (enhance, chat) run as background jobs the UI polls with job_status.
"""
import os
import subprocess
import threading
import time
import uuid

from . import audio, chatlogic, db, enhance, fonts, session, settings, stt, templates
from . import APP_VERSION


class Api:
    def __init__(self):
        self.session = None       # active MeetingSession
        self.jobs = {}            # job_id -> {state, result, error, kind, done_at}
        self.window = None        # set by app.py
        self._start_lock = threading.Lock()

    def _new_job(self, kind):
        job_id = uuid.uuid4().hex[:12]
        self.jobs[job_id] = {"state": "running", "kind": kind,
                             "result": None, "error": "", "done_at": None}
        return job_id

    def _prune_jobs(self):
        now = time.time()
        for jid in [j for j, job in self.jobs.items()
                    if job["done_at"] and now - job["done_at"] > 600]:
            self.jobs.pop(jid, None)

    # ------------------------------------------------------------- app state

    def get_state(self):
        s = settings.load()
        llm = enhance.llm_available()
        return {
            "version": APP_VERSION,
            "recording": bool(self.session and self.session.status == "recording"),
            "active_meeting_id": self.session.meeting_id if self.session else None,
            "llm": llm,
            "stt_model": stt.model_status(),
            "audio_backend_ok": audio.PYAUDIO_OK,
            "audio_backend_error": audio.PYAUDIO_ERR,
            "consent": s["consent"],
            "launch_at_startup": settings.get_launch_at_startup(),
            "font_faces": fonts.font_faces(),
        }

    # -------------------------------------------------------------- meetings

    def start_meeting(self, title, template_id):
        with self._start_lock:
            if self.session and self.session.status in ("starting", "recording"):
                return {"ok": False, "error": "A recording is already running."}
            meeting_id = db.create_meeting(title or "Untitled meeting", template_id)
            try:
                sess = session.MeetingSession(meeting_id)
                sess.start()
                self.session = sess
            except Exception as e:
                db.end_meeting(meeting_id, 0)
                return {"ok": False, "error": str(e), "meeting_id": meeting_id}
            return {"ok": True, "meeting_id": meeting_id,
                    "warnings": self.session.capture.warnings}

    def stop_meeting(self):
        if not self.session:
            return {"ok": False, "error": "No active recording."}
        sess = self.session
        job_id = self._new_job("stop")

        def worker():
            try:
                duration = sess.stop()
                m = db.get_meeting(sess.meeting_id)
                if m and m["title"] == "Untitled meeting":
                    guess = enhance.suggest_title(db.get_segments(sess.meeting_id))
                    if guess:
                        db.update_meeting(sess.meeting_id, title=guess)
                self.jobs[job_id].update(state="done", done_at=time.time(),
                                         result={"meeting_id": sess.meeting_id,
                                                 "duration": duration})
            except Exception as e:
                self.jobs[job_id].update(state="error", error=str(e), done_at=time.time())
            finally:
                if self.session is sess:
                    self.session = None

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def poll_session(self, since):
        if not self.session:
            return {"status": "none", "segments": [], "total": since}
        return self.session.poll(int(since or 0))

    def list_meetings(self, search):
        return db.list_meetings(search or "")

    def get_meeting(self, meeting_id):
        m = db.get_meeting(int(meeting_id))
        if not m:
            return None
        m["segments"] = db.get_segments(int(meeting_id))
        m["chat"] = db.get_chat_messages(int(meeting_id))
        return m

    def save_notes(self, meeting_id, notes_md):
        db.update_meeting(int(meeting_id), notes_md=notes_md or "")
        return {"ok": True}

    def rename_meeting(self, meeting_id, title):
        db.update_meeting(int(meeting_id),
                          title=(title or "").strip() or "Untitled meeting")
        return {"ok": True}

    def set_meeting_template(self, meeting_id, template_id):
        db.update_meeting(int(meeting_id), template_id=template_id)
        return {"ok": True}

    def delete_meeting(self, meeting_id):
        m = db.get_meeting(int(meeting_id))
        if m and m.get("audio_path"):
            try:
                os.remove(m["audio_path"])
            except OSError:
                pass
        db.delete_meeting(int(meeting_id))
        return {"ok": True}

    # ---------------------------------------------------------------- enhance

    def enhance_meeting(self, meeting_id, template_id):
        meeting_id = int(meeting_id)
        job_id = self._new_job("enhance")

        def worker():
            try:
                m = db.get_meeting(meeting_id)
                segs = db.get_segments(meeting_id)
                tpl = templates.get(template_id or m.get("template_id") or "auto")
                md, provider = enhance.enhance(m, segs, tpl)
                db.update_meeting(meeting_id, summary_md=md, summary_provider=provider,
                                  template_id=tpl["id"])
                self.jobs[job_id].update(state="done", done_at=time.time(),
                                         result={"summary_md": md, "provider": provider,
                                                 "meeting_id": meeting_id})
            except Exception as e:
                self.jobs[job_id].update(state="error", error=str(e), done_at=time.time())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    # ------------------------------------------------------------------- chat

    def chat(self, meeting_id, question):
        mid = int(meeting_id) if meeting_id else None
        job_id = self._new_job("chat")

        def worker():
            try:
                res = chatlogic.answer(question, mid)
                res["meeting_id"] = mid
                self.jobs[job_id].update(state="done", result=res, done_at=time.time())
            except Exception as e:
                self.jobs[job_id].update(state="error", error=str(e), done_at=time.time())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def get_global_chat(self):
        return db.get_chat_messages(None)

    def job_status(self, job_id):
        self._prune_jobs()
        job = self.jobs.get(job_id)
        if not job:
            return {"state": "unknown"}
        return job

    # -------------------------------------------------------------- templates

    def get_templates(self):
        return templates.all_templates()

    def save_template(self, tpl):
        tpl_id = tpl.get("id") or ("custom_" + uuid.uuid4().hex[:8])
        if any(b["id"] == tpl_id for b in templates.BUILTIN):
            return {"ok": False, "error": "Built-in templates cannot be edited."}
        sections = [s for s in (tpl.get("sections") or [])
                    if (s.get("title") or "").strip()]
        if not (tpl.get("name") or "").strip() or not sections:
            return {"ok": False, "error": "A template needs a name and at least one section."}
        db.save_custom_template(tpl_id, tpl["name"].strip(),
                                (tpl.get("description") or "").strip(), sections)
        return {"ok": True, "id": tpl_id}

    def delete_template(self, tpl_id):
        db.delete_custom_template(tpl_id)
        return {"ok": True}

    # ----------------------------------------------------------------- export

    def export_markdown(self, meeting_id, include_transcript):
        md = self._meeting_markdown(int(meeting_id), bool(include_transcript))
        m = db.get_meeting(int(meeting_id))
        safe = "".join(c for c in (m["title"] if m else "meeting")
                       if c.isalnum() or c in " -_").strip() or "meeting"
        try:
            import webview
            path = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=f"{safe}.md",
                file_types=("Markdown (*.md)",))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not path:
            return {"ok": False, "error": ""}
        if isinstance(path, (list, tuple)):
            path = path[0]
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        return {"ok": True, "path": str(path)}

    def copy_markdown(self, meeting_id, include_transcript):
        md = self._meeting_markdown(int(meeting_id), bool(include_transcript))
        return self.copy_text(md)

    def copy_text(self, text):
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            # UTF-16 LE BOM so clip.exe treats the input as Unicode, not ANSI.
            p.communicate(b"\xff\xfe" + (text or "").encode("utf-16-le"))
            return {"ok": p.returncode == 0}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _meeting_markdown(self, meeting_id, include_transcript):
        m = db.get_meeting(meeting_id)
        if not m:
            return ""
        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["created_at"]))
        mins = int((m.get("duration_sec") or 0) // 60)
        out = [f"# {m['title']}", f"*{date}* " + (f"({mins} min)" if mins else ""), ""]
        if (m.get("summary_md") or "").strip():
            out += [m["summary_md"].strip(), ""]
        if (m.get("notes_md") or "").strip():
            out += ["## My raw notes", m["notes_md"].strip(), ""]
        if include_transcript:
            segs = db.get_segments(meeting_id)
            if segs:
                out.append("## Transcript")
                out.append(enhance.format_transcript(segs))
        return "\n".join(out)

    # --------------------------------------------------------------- settings

    def get_settings(self):
        s = settings.load()
        # Never leak more than a hint of stored keys back to the UI.
        red = {k: ("*" * 8 + v[-4:] if v else "")
               for k, v in s["llm"].items() if k.endswith("_key")}
        out = {**s, "llm": {**s["llm"], **red}}
        out["launch_at_startup"] = settings.get_launch_at_startup()
        return out

    def save_settings(self, patch):
        patch = patch or {}
        llm = patch.get("llm") or {}
        for k in list(llm.keys()):
            if k.endswith("_key") and llm[k].startswith("********"):
                llm.pop(k)  # unchanged masked value
        warning = ""
        if "launch_at_startup" in patch:
            try:
                settings.set_launch_at_startup(bool(patch.pop("launch_at_startup")))
            except OSError as e:
                patch.pop("launch_at_startup", None)
                warning = f"Could not change launch at startup: {e}"
        settings.save(patch)
        return {"ok": True, "warning": warning}

    def list_audio_devices(self):
        return audio.list_devices()

    def list_ollama_models(self):
        cfg = settings.load()["llm"]
        return enhance.ollama_models(cfg.get("ollama_url") or "http://localhost:11434")

    def open_data_dir(self):
        os.startfile(settings.data_dir())
        return {"ok": True}
