"""JS bridge exposed to the UI via pywebview. All methods return JSON-safe values.

Slow operations (enhance, chat) run as background jobs the UI polls with job_status.
"""
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from . import audio, chatlogic, db, enhance, fonts, keypool, local_minutes, minutes_pipeline, minutes_queue, session, settings, stt, templates
from . import APP_VERSION


class Api:
    def __init__(self):
        self._session = None       # active MeetingSession
        self._jobs = {}            # job_id -> {state, result, error, kind, done_at}
        self._window = None        # set by app.py
        self._start_lock = threading.Lock()
        self._stop_job = None
        self._draining = set()
        self._minutes_active = False
        self._capture_requested = False
        self._minutes_cancel = threading.Event()
        self._minutes_idle = threading.Event()
        self._minutes_idle.set()
        self._closing = threading.Event()
        self._minutes_worker = None
        self._minutes_wake = threading.Event()
        self._minutes_running_id = None
        self._minutes_download_job = None

    def _new_job(self, kind):
        job_id = uuid.uuid4().hex[:12]
        self._jobs[job_id] = {"state": "running", "kind": kind,
                              "result": None, "error": "", "done_at": None}
        return job_id

    def _prune_jobs(self):
        now = time.time()
        for jid in [j for j, job in self._jobs.items()
                    if job["done_at"] and now - job["done_at"] > 600]:
            self._jobs.pop(jid, None)

    # ------------------------------------------------------------- app state

    def get_state(self):
        s = settings.load()
        llm = enhance.llm_available()
        return {
            "version": APP_VERSION,
            "recording": bool(self._session and self._session.status == "recording"),
            "active_meeting_id": self._session.meeting_id if self._session else None,
            "llm": llm,
            "stt_model": stt.model_status(),
            "audio_backend_ok": audio.PYAUDIO_OK,
            "audio_backend_error": audio.PYAUDIO_ERR,
            "consent": s["consent"],
            "launch_at_startup": settings.get_launch_at_startup(),
            "font_faces": fonts.font_faces(),
        }

    # -------------------------------------------------------------- meetings

    def start_meeting(self, title, template_id, category_id=None):
        with self._start_lock:
            if self._session or self._capture_requested:
                return {"ok": False, "error": "A recording is already running."}
            self._capture_requested = True
            self._minutes_cancel.set()
        self._minutes_idle.wait(timeout=3)
        with self._start_lock:
            try:
                if self._minutes_active:
                    return {"ok": False, "error": "Encerrando a geração de ata local. Tente iniciar novamente."}
                meeting_id = db.create_meeting(title or "Untitled meeting", template_id, category_id)
                try:
                    sess = session.MeetingSession(meeting_id)
                    sess.start()
                    self._session = sess
                except Exception as e:
                    db.end_meeting(meeting_id, 0)
                    return {"ok": False, "error": str(e), "meeting_id": meeting_id}
                return {"ok": True, "meeting_id": meeting_id, "warnings": sess.capture.warnings}
            finally:
                self._capture_requested = False

    def stop_meeting(self):
        with self._start_lock:
            if not self._session:
                return {"ok": False, "error": "No active recording."}
            if self._stop_job:
                return {"ok": True, "job_id": self._stop_job}
            sess = self._session
            job_id = self._stop_job = self._new_job("stop")

        def worker():
            finalize_id = None
            try:
                duration = sess.stop()
                with self._start_lock:
                    self._draining.add(sess.meeting_id)
                    finalize_id = self._new_job('finalize_transcription')
                    self._session = None
                    self._stop_job = None
                self._jobs[job_id].update(state='done', done_at=time.time(), result={
                    'meeting_id': sess.meeting_id, 'duration': duration, 'finalize_job_id': finalize_id})
                sess.wait_for_transcription()
                m = db.get_meeting(sess.meeting_id)
                enabled = settings.load().get("minutes", {}).get("enabled")
                if not enabled and m and (not m.get("title_manual") or m["title"] == "Untitled meeting"):
                    guess = enhance.suggest_title(db.get_segments(sess.meeting_id))
                    if guess:
                        db.update_meeting(sess.meeting_id, title=guess, title_manual=0)
                with self._start_lock:
                    minutes_job_id = self._queue_minutes(sess.meeting_id, automatic=True) if enabled else None
                self._jobs[finalize_id].update(state='done', done_at=time.time(), result={
                    'minutes_job_id': minutes_job_id, 'local_minutes': bool(enabled)})
            except Exception as e:
                self._jobs[finalize_id or job_id].update(state='error', error=str(e), done_at=time.time())
            finally:
                with self._start_lock:
                    self._draining.discard(sess.meeting_id)
                    self._minutes_wake.set()
                    if self._session is sess:
                        self._session = None
                        self._stop_job = None

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def poll_session(self, since: int):
        if not self._session:
            return {"status": "none"}
        return self._session.poll(int(since or 0))

    def list_meetings(self, search):
        return db.list_meetings(search or "")

    def list_categories(self):
        return db.list_categories()

    def create_category(self, name):
        category_id = db.create_category(name)
        if category_id is None:
            return {"ok": False, "error": "O nome está vazio ou já existe."}
        return {"ok": True, "category_id": category_id}

    def rename_category(self, category_id, name):
        ok = db.rename_category(int(category_id), name)
        return {"ok": ok, "error": "O nome está vazio ou já existe." if not ok else ""}

    def delete_category(self, category_id):
        db.delete_category(int(category_id))
        return {"ok": True}

    def move_meeting(self, meeting_id, category_id=None):
        meeting_id = int(meeting_id)
        if category_id in (None, ""):
            category_id = None
        else:
            category_id = int(category_id)
            if not any(c["id"] == category_id for c in db.list_categories()):
                return {"ok": False, "error": "Categoria não encontrada."}
        db.update_meeting(meeting_id, category_id=category_id)
        return {"ok": True}

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
        clean = (title or "").strip()
        db.update_meeting(int(meeting_id),
                          title=clean or "Untitled meeting",
                          title_manual=1 if clean else 0)
        return {"ok": True}

    def set_meeting_template(self, meeting_id, template_id):
        db.update_meeting(int(meeting_id), template_id=template_id)
        return {"ok": True}

    def delete_meeting(self, meeting_id):
        mid = int(meeting_id)
        with self._start_lock:
            if (self._session and self._session.meeting_id == mid) or mid in self._draining:
                return {"ok": False, "error": "Finalize a gravação e a transcrição antes de excluir a reunião."}
            m = db.get_meeting(mid)
            db.delete_meeting(mid)
        if m and m.get("audio_path"):
            try:
                os.remove(m["audio_path"])
            except OSError:
                pass
        return {"ok": True}

    # ---------------------------------------------------------------- enhance

    def enhance_meeting(self, meeting_id, template_id, minutes_profile=None):
        meeting_id = int(meeting_id)
        if settings.load().get("minutes", {}).get("enabled"):
            with self._start_lock:
                if (self._session and self._session.meeting_id == meeting_id) or meeting_id in self._draining:
                    return {"ok": False, "error": "Finalize a gravação antes de gerar uma ata local."}
                job_id = self._queue_minutes(meeting_id, profile=minutes_profile)
                if not job_id:
                    return {"ok": False, "error": "Finalize a gravação antes de gerar uma ata local."}
                return {"ok": True, "job_id": job_id, "persistent": True}
        job_id = self._new_job("enhance")

        def worker():
            try:
                m = db.get_meeting(meeting_id)
                segs = db.get_segments(meeting_id)
                tpl = templates.get(template_id or m.get("template_id") or "auto")
                md, provider = enhance.enhance(m, segs, tpl)
                new_title = (m.get("title") or "Untitled meeting") if m else "Untitled meeting"
                if m and (not m.get("title_manual") or new_title in ("Untitled meeting", "")):
                    ai_title = ""
                    if provider not in ("extractive", "none", ""):
                        ai_title = enhance.suggest_title_with_llm(m, segs, md)
                    if not ai_title and new_title in ("Untitled meeting", ""):
                        ai_title = enhance.suggest_title(segs)
                    if ai_title:
                        new_title = ai_title
                db.update_meeting(meeting_id, title=new_title, summary_md=md,
                                  summary_provider=provider, template_id=tpl["id"])
                self._jobs[job_id].update(state="done", done_at=time.time(),
                                         result={"summary_md": md, "provider": provider,
                                                 "meeting_id": meeting_id, "title": new_title})
            except Exception as e:
                self._jobs[job_id].update(state="error", error=str(e), done_at=time.time())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def _queue_minutes(self, meeting_id, automatic=False, profile=None):
        # Caller holds _start_lock. Preserve an existing draft on automatic stop.
        meeting = db.get_meeting(meeting_id)
        if not meeting or (automatic and meeting.get('summary_md')):
            return None
        default = settings.load().get('minutes', {}).get('profile', 'executiva')
        if profile not in ('executiva', 'treinamento'):
            profile = default if default in ('executiva', 'treinamento') else 'executiva'
        current = db.get_minutes_job(meeting_id)
        if current and current['id'] == self._minutes_running_id and current['profile'] != profile:
            self._minutes_cancel.set()
        job = db.enqueue_minutes(meeting_id, profile, minutes_pipeline.STRATEGY + ':' + local_minutes.MODEL_HASH)
        if job:
            self._start_minutes_worker()
            self._minutes_wake.set()
            return job['id']
        return None

    def _start_minutes_worker(self):
        if not self._closing.is_set() and (self._minutes_worker is None or not self._minutes_worker.is_alive()):
            restarting = self._minutes_worker is not None
            self._minutes_worker = threading.Thread(target=minutes_queue.run, args=(self, restarting),
                                                    daemon=True, name='local-minutes')
            self._minutes_worker.start()

    def list_minutes_jobs(self):
        jobs = db.list_minutes_jobs()
        if any(job['state'] == 'queued' for job in jobs):
            reason = minutes_queue.blocker(self, settings.load().get('minutes', {}))
            for job in jobs:
                if job['state'] == 'queued':
                    job['detail'] = reason or 'Na fila de atas locais…'
        return jobs

    def set_minutes_paused(self, meeting_id, paused):
        with self._start_lock:
            job = db.get_minutes_job(int(meeting_id))
            if not job or job['state'] in ('done', 'conflict'):
                return {'ok': False, 'error': 'A ata não está na fila.'}
            if paused:
                if not db.set_minutes_state(job['id'], 'paused', 'Pausada manualmente.', expected=job['state']):
                    return {'ok': False, 'error': 'O estado da ata mudou. Atualize a fila.'}
                if job['id'] == self._minutes_running_id:
                    self._minutes_cancel.set()
            elif job['state'] in ('paused', 'partial', 'error'):
                db.set_minutes_state(job['id'], 'queued', expected=job['state'])
                self._start_minutes_worker()
                self._minutes_wake.set()
            return {'ok': True}

    def get_minutes_preview(self, meeting_id):
        return minutes_queue.preview(int(meeting_id))

    def _close_minutes(self):
        with self._start_lock:
            self._closing.set()
            self._minutes_cancel.set()
            self._minutes_wake.set()
        if self._minutes_worker:
            self._minutes_worker.join(timeout=3)

    def save_minutes_edit(self, meeting_id, markdown, original):
        markdown = (markdown or "").strip()
        if not markdown or not isinstance(original, str):
            return {'ok': False}
        mid = int(meeting_id)
        if db.write_local_minutes(mid, markdown, original):
            return {'ok': True}
        meeting = db.get_meeting(mid)
        if meeting and meeting['summary_provider'] == 'local_minutes':
            return {'ok': False, 'latest': meeting['summary_md']}
        return {'ok': False}

    def local_minutes_status(self):
        return {"installed": local_minutes.model_installed(),
                "runtime": local_minutes.runtime_path() is not None,
                "size": local_minutes.MODEL_BYTES,
                "download_job_id": self._minutes_download_job}

    def download_minutes_model(self):
        with self._start_lock:
            if self._minutes_download_job:
                return {"ok": True, "job_id": self._minutes_download_job}
            if local_minutes.runtime_path() is None:
                return {"ok": False, "error": "Motor de atas indisponível. Use o pacote Windows x64."}
            jid = self._minutes_download_job = self._new_job("minutes_download")

        def worker():
            job = self._jobs[jid]
            try:
                local_minutes.download(lambda received: job.update(received=received))
                job.update(state="done", result={"installed": True}, done_at=time.time())
            except Exception as e:
                job.update(state="error", error=str(e), done_at=time.time())
            finally:
                with self._start_lock:
                    self._minutes_download_job = None

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": jid}

    # ------------------------------------------------------------------- chat

    def chat(self, meeting_id, question):
        mid = int(meeting_id) if meeting_id else None
        job_id = self._new_job("chat")

        def worker():
            try:
                res = chatlogic.answer(question, mid)
                res["meeting_id"] = mid
                self._jobs[job_id].update(state="done", result=res, done_at=time.time())
            except Exception as e:
                self._jobs[job_id].update(state="error", error=str(e), done_at=time.time())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def get_global_chat(self):
        return db.get_chat_messages(None)

    def job_status(self, job_id):
        if isinstance(job_id, str) and job_id.startswith('minutes-'):
            job = db.get_minutes_job_by_id(job_id)
            if not job:
                return {'state': 'unknown'}
            mid, current = job['meeting_id'], job['state']
            detail = job['detail']
            if current == 'queued':
                detail = minutes_queue.blocker(self, settings.load().get('minutes', {})) or 'Na fila de atas locais…'
            summary = None
            if current == 'done':
                meeting = db.get_meeting(mid)
                if meeting:
                    summary = {'summary_md': meeting['summary_md'], 'provider': meeting['summary_provider'],
                               'meeting_id': mid, 'title': meeting['title'], 'backend': job['backend'],
                               'elapsed_sec': job['elapsed_sec'], 'detail': detail}
            return {'kind': 'minutes', 'state': current, 'detail': detail,
                    'completed': job['completed'], 'failed': job['failed'], 'total': job['total'],
                    'result': summary, 'error': detail if current in ('error', 'conflict') else ''}
        self._prune_jobs()
        job = self._jobs.get(job_id)
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
            path = self._window.create_file_dialog(
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

    def get_copilot_prompt(self, meeting_id):
        m = db.get_meeting(int(meeting_id))
        if not m:
            return ""
        date_str = time.strftime("%d/%m/%Y %H:%M", time.localtime(m["created_at"]))
        date_iso = time.strftime("%Y-%m-%d", time.localtime(m["created_at"]))
        mins = int((m.get("duration_sec") or 0) // 60)
        duracao_str = f"{mins} min" if mins else "< 1 min"

        segs = db.get_segments(int(meeting_id))
        transcricao = enhance.format_transcript(segs) if segs else "(Nenhuma transcrição capturada)"
        rascunho = (m.get("notes_md") or "").strip() or "Nenhum rascunho manual registrado."
        titulo = m.get("title") or "Reunião"

        prompt = f"""Por favor, processe esta reunião gravada e gere a Ata Executiva padronizada (padrão SESI) em formato Markdown pronto para uso no Obsidian/Notion.

### Metadados da Reunião:
- **Título:** {titulo}
- **Data:** {date_str}
- **Duração:** {duracao_str}

### Anotações Rápidas de Rascunho:
{rascunho}

### Transcrição Completa da Fala (Eu vs Outros):
{transcricao}

---
### Diretrizes de Estruturação Obrigatórias (Template SESI):
1. **Frontmatter YAML (início do documento):**
```yaml
---
tipo: reuniao
data: {date_iso}
duracao: "{duracao_str}"
participantes:
  - "Participante"
tags: [reuniao/executiva]
---
```

2. **Cabeçalho:**
- `# 📋 Ata Executiva: {titulo}`
- `> 📅 **Data:** {date_str} · ⏱️ **Duração:** {duracao_str}`
- `---`

3. **## 📌 Resumo Executivo:**
- Visão geral em 3 a 5 tópicos destacando o objetivo e as principais conclusões.

4. **## 💡 Deliberações e Discussão Técnica:**
- Tópicos numerados (`### 1. [Tema / Assunto]`) contendo:
  - `- **Contexto Técnico:**` O que foi discutido em detalhes.
  - `- **Status/Decisão:**` O que foi formalmente acordado ou definido.

5. **## 🎯 Plano de Ação:**
- Tabela Markdown estruturada exatamente com as colunas:
`| Ação / Tarefa | Responsável | Prazo / Momento |`
(Cada ação deve ter um responsável e prazo inferidos da conversa).
"""
        return prompt

    def copy_copilot_prompt(self, meeting_id):
        prompt = self.get_copilot_prompt(meeting_id)
        return self.copy_text(prompt)

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
        date_str = time.strftime("%d/%m/%Y %H:%M", time.localtime(m["created_at"]))
        date_iso = time.strftime("%Y-%m-%d", time.localtime(m["created_at"]))
        mins = int((m.get("duration_sec") or 0) // 60)
        duracao_str = f"{mins} min" if mins else "< 1 min"

        out = [
            "---",
            "tipo: reuniao",
            f"data: {date_iso}",
            f'duracao: "{duracao_str}"',
            "tags: [reuniao]",
            "---",
            "",
            f"# 📋 {m['title']}",
            f"> 📅 **Data:** {date_str} · ⏱️ **Duração:** {duracao_str}",
            "---",
            ""
        ]
        if (m.get("summary_md") or "").strip():
            summary = m["summary_md"].strip()
            if m.get('summary_provider') == 'local_minutes':
                summary = minutes_pipeline.export_refs(summary)
            out += ["## 📌 Resumo & Pontos Chave", summary, ""]
        if (m.get("notes_md") or "").strip():
            out += ["## ✍️ Anotações Rápidas", m["notes_md"].strip(), ""]
        if include_transcript:
            segs = db.get_segments(meeting_id)
            if segs:
                out.append("## 💬 Transcrição Completa")
                out.append(enhance.format_transcript(segs))
        return "\n".join(out)

    # --------------------------------------------------------------- settings

    def get_settings(self):
        s = settings.load()
        # Never leak more than a hint of stored keys back to the UI.
        llm_out = dict(s["llm"])
        stt_out = dict(s["stt"])
        for field_base in ("openrouter", "openai", "groq", "anthropic"):
            sing, plur = f"{field_base}_key", f"{field_base}_keys"
            if sing in llm_out or plur in llm_out:
                keys = keypool.collect_keys(llm_out, sing, plur)
                llm_out[sing] = keypool.mask_key(keys[0]) if keys else ""
                llm_out[plur] = keypool.mask_keys(keys)
            if sing in stt_out or plur in stt_out:
                keys = keypool.collect_keys(stt_out, sing, plur)
                stt_out[sing] = keypool.mask_key(keys[0]) if keys else ""
                stt_out[plur] = keypool.mask_keys(keys)
        out = {**s, "llm": llm_out, "stt": stt_out}
        out["launch_at_startup"] = settings.get_launch_at_startup()
        return out

    def save_settings(self, patch):
        patch = patch or {}
        cur = settings.load()
        for section, providers in (
            ("llm", ("openrouter", "openai", "groq", "anthropic")),
            ("stt", ("openrouter", "openai", "groq")),
        ):
            sub = patch.get(section)
            if not isinstance(sub, dict):
                continue
            for p in providers:
                sing, plur = f"{p}_key", f"{p}_keys"
                if sing not in sub and plur not in sub:
                    continue
                old_keys = keypool.collect_keys(cur.get(section, {}), sing, plur)
                prim, full_list = keypool.resolve_incoming_keys(
                    old_keys,
                    incoming_list=sub.get(plur) if plur in sub else None,
                    incoming_single=sub.get(sing) if sing in sub else None,
                )
                sub[sing] = prim
                sub[plur] = full_list
        warning = ""
        if "launch_at_startup" in patch:
            try:
                settings.set_launch_at_startup(bool(patch.pop("launch_at_startup")))
            except OSError as e:
                patch.pop("launch_at_startup", None)
                warning = f"Could not change launch at startup: {e}"
        settings.save(patch)
        keypool.rotator.clear()
        stt.reset_status()
        return {"ok": True, "warning": warning}

    def test_openrouter_key(self, raw_keys=""):
        s = settings.load()
        saved_keys = keypool.collect_keys(s["stt"], "openrouter_key", "openrouter_keys")
        if not saved_keys:
            saved_keys = keypool.collect_keys(s["llm"], "openrouter_key", "openrouter_keys")

        if isinstance(raw_keys, list):
            _, keys = keypool.resolve_incoming_keys(saved_keys, incoming_list=raw_keys)
        else:
            raw = (raw_keys or "").strip()
            if not raw or raw.startswith("********"):
                keys = saved_keys
            else:
                keys = [raw]

        if not keys:
            return {"ok": False, "error": "Informe ao menos uma chave de API do OpenRouter primeiro."}

        results = []
        any_ok = False
        for idx, key in enumerate(keys, start=1):
            try:
                url = "https://openrouter.ai/api/v1/key"
                headers = {"Authorization": f"Bearer {key}"}
                try:
                    r = requests.get(url, headers=headers, timeout=10)
                except requests.exceptions.SSLError:
                    r = requests.get(url, headers=headers, timeout=10, verify=False)
                if r.status_code != 200:
                    msg = stt._extract_api_error(r)
                    results.append({"index": idx, "ok": False, "error": f"HTTP {r.status_code}: {msg}"})
                    continue
                d = r.json().get("data") or {}
                any_ok = True
                results.append({
                    "index": idx,
                    "ok": True,
                    "label": d.get("label", ""),
                    "limit": d.get("limit"),
                    "limit_remaining": d.get("limit_remaining"),
                    "usage": d.get("usage", 0),
                    "is_free_tier": d.get("is_free_tier", False),
                })
            except Exception as e:
                results.append({"index": idx, "ok": False, "error": str(e)})

        first_ok = next((x for x in results if x["ok"]), None)
        return {
            "ok": any_ok,
            "count": len(results),
            "results": results,
            "limit_remaining": first_ok.get("limit_remaining") if first_ok else None,
            "usage": first_ok.get("usage", 0) if first_ok else 0,
            "error": "" if any_ok else (results[0].get("error") if results else "Falha"),
        }

    def list_openrouter_stt_models(self):
        try:
            url = "https://openrouter.ai/api/v1/models?output_modalities=transcription"
            headers = {"User-Agent": f"Muesli-App/{APP_VERSION}"}
            try:
                r = requests.get(url, headers=headers, timeout=12)
            except requests.exceptions.SSLError:
                r = requests.get(url, headers=headers, timeout=12, verify=False)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            items = r.json().get("data") or []
            models = []
            for m in items:
                mid = (m.get("id") or "").strip()
                if not mid:
                    continue
                models.append({"id": mid, "name": (m.get("name") or mid).strip()})
            models.sort(key=lambda x: (0 if "whisper" in x["id"].lower() else 1, x["id"]))
            return {"ok": True, "models": models}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def list_audio_devices(self):
        return audio.list_devices()

    def list_ollama_models(self):
        cfg = settings.load()["llm"]
        return enhance.ollama_models(cfg.get("ollama_url") or "http://localhost:11434")

    def open_data_dir(self):
        os.startfile(settings.data_dir())
        return {"ok": True}

    # ------------------------------------------------------------- updates

    def check_updates(self):
        def parse_ver(tag_str):
            clean = tag_str.lstrip("v").strip()
            res = []
            for p in clean.split("."):
                try:
                    res.append(int(p))
                except ValueError:
                    res.append(0)
            return res

        try:
            url = "https://api.github.com/repos/NatanMendes0/muesli-windows/releases/latest"
            headers = {"User-Agent": f"Muesli-App/{APP_VERSION}"}
            try:
                r = requests.get(url, timeout=10, headers=headers)
            except requests.exceptions.SSLError:
                r = requests.get(url, timeout=10, headers=headers, verify=False)

            if r.status_code == 200:
                data = r.json()
                latest_tag = (data.get("tag_name") or "").strip()
                has_update = parse_ver(latest_tag) > parse_ver(APP_VERSION)
                zip_name = f"Muesli-windows-x64-{latest_tag}.zip"
                assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
                if not assets.get(zip_name) and assets.get("Muesli-windows-x64.zip"):
                    zip_name = "Muesli-windows-x64.zip"
                zip_url = assets.get(zip_name)

                return {
                    "ok": True,
                    "current_version": APP_VERSION,
                    "latest_version": latest_tag,
                    "has_update": has_update,
                    "release_name": data.get("name") or latest_tag,
                    "release_notes": data.get("body") or "Atualizações e correções de estabilidade.",
                    "html_url": data.get("html_url") or "https://github.com/NatanMendes0/muesli-windows/releases",
                    "zip_url": zip_url or data.get("html_url", ""),
                    "zip_name": zip_name,
                }

            # Fallback via redirect HTML caso api.github.com retorne 403 (rate limit em IP corporativo)
            web_url = "https://github.com/NatanMendes0/muesli-windows/releases/latest"
            try:
                rw = requests.get(web_url, timeout=10, headers=headers, allow_redirects=False)
            except requests.exceptions.SSLError:
                rw = requests.get(web_url, timeout=10, headers=headers, allow_redirects=False, verify=False)
            loc = rw.headers.get("Location") or ""
            if "/releases/tag/" in loc:
                latest_tag = loc.rstrip("/").split("/releases/tag/")[-1].strip()
                has_update = parse_ver(latest_tag) > parse_ver(APP_VERSION)
                zip_name = (f"Muesli-windows-x64-{latest_tag}.zip" if parse_ver(latest_tag) >= [1, 6, 1]
                            else "Muesli-windows-x64.zip")
                return {
                    "ok": True,
                    "current_version": APP_VERSION,
                    "latest_version": latest_tag,
                    "has_update": has_update,
                    "release_name": latest_tag,
                    "release_notes": "Nova versão disponível no GitHub Releases.",
                    "html_url": loc,
                    "zip_url": f"https://github.com/NatanMendes0/muesli-windows/releases/download/{latest_tag}/{zip_name}",
                    "zip_name": zip_name,
                }

            return {"ok": False, "error": f"Status HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_external_url(self, url):
        if url and (url.startswith("http://") or url.startswith("https://")):
            webbrowser.open(url)
            return {"ok": True}
        return {"ok": False, "error": "URL inválida"}

    def download_update(self, zip_url, filename=""):
        job_id = self._new_job("download_update")

        def worker():
            try:
                downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
                if not os.path.exists(downloads_dir):
                    downloads_dir = os.path.expanduser("~")

                safe_name = filename or "Muesli-windows-update.zip"
                dest_path = os.path.join(downloads_dir, safe_name)

                headers = {"User-Agent": f"Muesli-App/{APP_VERSION}"}
                try:
                    r = requests.get(zip_url, stream=True, timeout=180, headers=headers)
                except requests.exceptions.SSLError:
                    r = requests.get(zip_url, stream=True, timeout=180, headers=headers, verify=False)

                r.raise_for_status()
                total_bytes = int(r.headers.get("content-length", 0))
                downloaded = 0

                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_bytes > 0:
                                self._jobs[job_id]["progress"] = int((downloaded / total_bytes) * 100)

                # Desbloqueia o arquivo baixado automaticamente no Windows
                try:
                    os.remove(dest_path + ":Zone.Identifier")
                except OSError:
                    pass

                # Abre a pasta Downloads no Windows Explorer
                try:
                    if hasattr(os, "startfile"):
                        os.startfile(downloads_dir)
                    else:
                        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", downloads_dir])
                except Exception:
                    pass

                self._jobs[job_id].update(
                    state="done",
                    result={"path": dest_path, "filename": safe_name},
                    done_at=time.time()
                )
            except Exception as e:
                self._jobs[job_id].update(state="error", error=str(e), done_at=time.time())

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "job_id": job_id}

    def exit_app(self):
        def _die():
            time.sleep(0.15)
            self._close_minutes()
            if self._session is not None:
                try:
                    self._session.stop()
                except Exception:
                    pass
            os._exit(0)

        threading.Thread(target=_die, daemon=True).start()
        return {"ok": True}
