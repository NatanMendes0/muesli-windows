/* Muesli frontend. Talks to Python through window.pywebview.api. */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  meetings: [],
  current: null,        // full meeting object currently open
  recording: false,
  activeMeetingId: null,
  segCount: 0,          // segments received for the live session
  renderedSegIds: new Set(),
  pollTimer: null,
  saveTimer: null,
  templates: [],
  settings: null,
  chatBusy: false,
  enhanceBusy: false,
  startingNote: false,
  booted: false,
  searchSeq: 0,
};

function isApiReady() {
  return typeof window.pywebview !== "undefined" &&
         typeof window.pywebview.api !== "undefined" &&
         typeof window.pywebview.api.get_templates === "function" &&
         typeof window.pywebview.api.get_settings === "function";
}

function api() {
  if (!isApiReady()) {
    toast("Conectando ao backend... aguarde um instante.", 3000);
    throw new Error("Backend API not ready yet");
  }
  return window.pywebview.api;
}

function toast(msg, ms = 2600) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("visible");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("visible"), ms);
}

/* ------------------------------------------------------- tiny markdown */
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function inlineMd(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*(?!\s)(.+?)(?<!\s)\*(?=\W|$)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function renderMd(md) {
  const lines = (md || "").split(/\r?\n/);
  let html = "", inUl = false, inOl = false, inQuote = false;
  const closeLists = () => {
    if (inUl) { html += "</ul>"; inUl = false; }
    if (inOl) { html += "</ol>"; inOl = false; }
    if (inQuote) { html += "</blockquote>"; inQuote = false; }
  };
  for (const raw of lines) {
    const line = escapeHtml(raw);
    const h = line.match(/^(#{1,3})\s+(.*)/);
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    const q = line.match(/^&gt;\s?(.*)/); // lines are already HTML-escaped
    if (h) { closeLists(); html += `<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`; }
    else if (ul) {
      if (inOl) { html += "</ol>"; inOl = false; }
      if (!inUl) { html += "<ul>"; inUl = true; }
      html += `<li>${inlineMd(ul[1])}</li>`;
    } else if (ol) {
      if (inUl) { html += "</ul>"; inUl = false; }
      if (!inOl) { html += "<ol>"; inOl = true; }
      html += `<li>${inlineMd(ol[1])}</li>`;
    } else if (q) {
      if (!inQuote) { closeLists(); html += "<blockquote>"; inQuote = true; }
      html += `<p>${inlineMd(q[1])}</p>`;
    } else if (line.trim() === "") { closeLists(); }
    else { closeLists(); html += `<p>${inlineMd(line)}</p>`; }
  }
  closeLists();
  return html;
}

/* ------------------------------------------------------------ sidebar */
function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function dateGroup(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return "This week";
  return d.toLocaleDateString([], { month: "long", year: "numeric" });
}
function fmtDur(sec) {
  if (!sec) return "";
  const m = Math.round(sec / 60);
  return m < 1 ? "<1 min" : `${m} min`;
}

async function refreshMeetings() {
  const q = $("searchInput").value.trim();
  const seq = ++state.searchSeq;
  const meetings = await api().list_meetings(q);
  if (seq !== state.searchSeq) return; // a newer search finished first
  state.meetings = meetings;
  const list = $("meetingList");
  list.innerHTML = "";
  if (!state.meetings.length) {
    list.innerHTML = `<div class="list-empty">${q ? "No matches." : "No meetings yet.<br>Hit New note to start one."}</div>`;
    return;
  }
  let lastGroup = "";
  for (const m of state.meetings) {
    const g = dateGroup(m.created_at);
    if (g !== lastGroup) {
      lastGroup = g;
      const gd = document.createElement("div");
      gd.className = "date-group";
      gd.textContent = g;
      list.appendChild(gd);
    }
    const btn = document.createElement("button");
    btn.className = "meeting-item" + (state.current && state.current.id === m.id ? " active" : "");
    const live = m.id === state.activeMeetingId && state.recording;
    btn.innerHTML = `<div class="title"></div>
      <div class="meta">${live ? '<span class="live">● recording</span>' : ""}
        <span>${fmtDate(m.created_at)}</span><span>${fmtDur(m.duration_sec)}</span></div>`;
    btn.querySelector(".title").textContent = m.title;
    btn.addEventListener("click", () => openMeeting(m.id));
    list.appendChild(btn);
  }
}

/* ------------------------------------------------------- meeting view */
async function showHome() {
  await flushNotes();
  state.current = null;
  $("homeView").style.display = "flex";
  $("homeChatDock").style.display = "block";
  $("meetingView").classList.remove("visible");
  refreshMeetings();
}

async function openMeeting(id) {
  await flushNotes(); // save pending edits of the meeting we are leaving
  const m = await api().get_meeting(id);
  if (!m) return;
  state.current = m;
  $("homeView").style.display = "none";
  $("homeChatDock").style.display = "none";
  $("meetingView").classList.add("visible");
  $("noteTitle").value = m.title === "Untitled meeting" ? "" : m.title;
  $("notepad").value = m.notes_md || "";
  const d = new Date(m.created_at * 1000);
  $("noteMeta").textContent =
    d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) +
    " " + fmtDate(m.created_at) + (m.duration_sec ? "  ·  " + fmtDur(m.duration_sec) : "");
  $("tplSelect").value = m.template_id || "auto";
  if (!$("tplSelect").value) $("tplSelect").value = "auto";

  renderTranscript(m.segments || []);
  renderEnhanced(m.summary_md, m.summary_provider);
  renderChat($("chatPanel"), m.chat || []);
  if ($("tabCopilot")?.classList.contains("active")) {
    selectTab("copilot");
  } else {
    selectTab(m.summary_md ? "enhanced" : "notes");
  }

  const isLive = m.id === state.activeMeetingId && state.recording;
  setRecordingUi(isLive);
  if (isLive) {
    state.segCount = (m.segments || []).length;
  }
  $("warnBanner").classList.remove("visible");
  refreshMeetings();
}

function setRecordingUi(on) {
  // Pill, stop button and meters belong to the meeting view being shown;
  // the sidebar button reflects the global recording state.
  $("recPill").classList.toggle("visible", on);
  $("btnStop").classList.toggle("visible", on);
  $("meters").style.display = on ? "flex" : "none";
  $("btnNewLabel").textContent = state.recording ? "Recording…" : "New note";
  $("btnNew").classList.toggle("recording", state.recording);
  const cb = $("consentBanner");
  if (on && state.settings && state.settings.consent && state.settings.consent.enabled) {
    cb.innerHTML = "Consent reminder: let people know you are using an AI notepad. " +
      '<span class="copy-link" id="consentCopy">Copy snippet</span>';
    cb.classList.add("visible");
    $("consentCopy").addEventListener("click", async () => {
      await api().copy_text(state.settings.consent.snippet);
      toast("Consent snippet copied");
    });
  } else {
    cb.classList.remove("visible");
  }
}

async function selectTab(which) {
  $("tabNotes").classList.toggle("active", which === "notes");
  $("tabEnhanced").classList.toggle("active", which === "enhanced");
  $("tabCopilot")?.classList.toggle("active", which === "copilot");
  $("notepad").classList.toggle("hidden", which !== "notes");
  $("enhancedBody").classList.toggle("visible", which === "enhanced");

  const copilotEl = $("copilotBody");
  if (copilotEl) {
    copilotEl.style.display = which === "copilot" ? "flex" : "none";
    if (which === "copilot" && state.current) {
      try {
        if (isApiReady()) {
          const prompt = await api().get_copilot_prompt(state.current.id);
          $("copilotPromptPreview").value = prompt;
        }
      } catch (e) {
        console.error("Erro ao carregar prompt copilot:", e);
      }
    }
  }
}

function renderEnhanced(md, provider) {
  const body = $("enhancedBody");
  if (!md) {
    body.innerHTML = `<div class="enhance-empty">No enhanced notes yet.<br>
      Pick a template below and hit <b>Enhance notes</b> after the meeting.</div>`;
    return;
  }
  const provNames = { ollama: "local AI (Ollama)", extractive: "built-in summarizer",
                      anthropic: "Anthropic", openai: "OpenAI", groq: "Groq" };
  body.innerHTML = `<div class="provider-tag">Generated with ${provNames[provider] || provider || "AI"}</div>` +
                   renderMd(md);
}

function renderTranscript(segments) {
  const body = $("transcriptBody");
  body.innerHTML = '<div class="tr-status" id="trStatus"></div>';
  state.renderedSegIds = new Set();
  for (const s of segments) addSegmentEl(s);
  if (!segments.length) {
    $("trStatus").textContent = state.recording && state.current &&
      state.current.id === state.activeMeetingId
      ? "Listening…" : "No transcript captured for this meeting.";
  }
  body.scrollTop = body.scrollHeight;
}

function addSegmentEl(s) {
  if (s.id !== undefined) {
    if (state.renderedSegIds.has(s.id)) return; // poll/open race duplicate
    state.renderedSegIds.add(s.id);
  }
  const body = $("transcriptBody");
  const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
  const div = document.createElement("div");
  div.className = "seg";
  const mm = String(Math.floor(s.t0 / 60)).padStart(2, "0");
  const ss = String(Math.floor(s.t0 % 60)).padStart(2, "0");
  const who = s.speaker === "Me" ? "Eu" : s.speaker === "Them" ? "Outros" : "·";
  const cls = s.speaker === "Me" ? "me" : s.speaker === "Them" ? "them" : "them";
  div.innerHTML = `<span class="chip ${cls}">${who}</span>
    <span class="txt"><span class="t">${mm}:${ss}</span></span>`;
  div.querySelector(".txt").appendChild(document.createTextNode(s.text));
  body.appendChild(div);
  if (nearBottom) body.scrollTop = body.scrollHeight;
}

/* ------------------------------------------------------ record / stop */
async function startNewNote() {
  if (state.recording) {
    if (state.activeMeetingId) openMeeting(state.activeMeetingId);
    return;
  }
  if (!isApiReady()) {
    toast("O Muesli ainda está inicializando o backend. Aguarde alguns segundos...", 3500);
    return;
  }
  if (state.startingNote) return; // double click guard
  state.startingNote = true;
  $("btnNew").disabled = true;
  $("btnNewHome").disabled = true;
  toast("Iniciando gravação...");
  try {
    const tpl = (state.settings && state.settings.ui && state.settings.ui.default_template) || "auto";
    const res = await api().start_meeting("", tpl);
    if (!res.ok) {
      toast(res.error || "Não foi possível iniciar a gravação", 4000);
      if (res.meeting_id) { await refreshMeetings(); openMeeting(res.meeting_id); }
      return;
    }
    state.recording = true;
    state.activeMeetingId = res.meeting_id;
    state.segCount = 0;
    await refreshMeetings();
    await openMeeting(res.meeting_id);
    if (res.warnings && res.warnings.length) showWarn(res.warnings.join(" "));
    startPolling();
    toast("Gravação em andamento!");
  } catch (err) {
    console.error("Erro ao iniciar reunião:", err);
    toast("Erro ao iniciar: " + (err.message || err), 4000);
  } finally {
    state.startingNote = false;
    $("btnNew").disabled = false;
    $("btnNewHome").disabled = false;
  }
}
window.trayNewNote = startNewNote;

function showWarn(msg) {
  const b = $("warnBanner");
  b.textContent = msg;
  b.classList.add("visible");
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    if (!state.recording) return stopPolling();
    let s;
    try { s = await api().poll_session(state.segCount); }
    catch (e) { return; }
    if (s.status === "none") { state.recording = false; setRecordingUi(false); refreshMeetings(); return stopPolling(); }
    // Live widgets only when the live meeting is the one on screen.
    const viewingLive = state.current && state.current.id === state.activeMeetingId;
    if (viewingLive) {
      const el = Math.floor(s.elapsed || 0);
      $("recTime").textContent =
        String(Math.floor(el / 60)).padStart(2, "0") + ":" + String(el % 60).padStart(2, "0");
      // meters (rms 0..~0.3 mapped to 0..100)
      $("meterMic").style.width = Math.min(100, (s.levels.mic || 0) * 400) + "%";
      $("meterLoop").style.width = Math.min(100, (s.levels.loop || 0) * 400) + "%";
      const tr = $("trStatus");
      if (tr) {
        if (s.model && s.model.state === "loading") tr.textContent = s.model.detail + "…";
        else if (s.detail) tr.textContent = s.detail;
        else if (state.segCount === 0) tr.textContent = "Listening…";
        else tr.textContent = "";
      }
      if (s.warnings && s.warnings.length) showWarn(s.warnings.join(" "));
      for (const seg of s.segments) addSegmentEl(seg);
    }
    state.segCount = s.total;
  }, 700);
}
function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function stopRecording() {
  if (!state.recording) return;
  const stoppedId = state.activeMeetingId;
  $("btnStop").textContent = "Finalizando…";
  $("btnStop").disabled = true;
  const res = await api().stop_meeting();
  if (res.ok) {
    await waitJob(res.job_id);
  }
  state.recording = false;
  state.activeMeetingId = null;
  stopPolling();
  $("btnStop").textContent = "Parar";
  $("btnStop").disabled = false;
  setRecordingUi(false);
  refreshMeetings();
  // Only reload the view if the user is still looking at the stopped meeting.
  if (state.current && state.current.id === stoppedId) {
    await openMeeting(stoppedId);
    toast("Gravação finalizada. Aprimorando notas com IA...");
    await enhanceNow();
  } else {
    toast("Gravação finalizada. Transcrição salva.");
  }
}

function waitJob(jobId) {
  return new Promise((resolve) => {
    let inFlight = false;
    const t = setInterval(async () => {
      if (inFlight) return; // never overlap polls for the same job
      inFlight = true;
      let j;
      try { j = await api().job_status(jobId); }
      catch (e) { inFlight = false; return; }
      inFlight = false;
      if (j.state === "done") { clearInterval(t); resolve(j.result); }
      else if (j.state === "error" || j.state === "unknown") {
        clearInterval(t);
        if (j.error) toast(j.error, 4000);
        resolve(null);
      }
    }, 700);
  });
}

/* ----------------------------------------------------------- enhance */
async function enhanceNow() {
  if (!state.current || state.enhanceBusy) return;
  const mid = state.current.id;
  state.enhanceBusy = true;
  const btn = $("btnEnhance");
  btn.disabled = true;
  $("enhanceLabel").innerHTML = 'Aprimorando&hellip; <span class="spin"></span>';
  await flushNotes();
  const res = await api().enhance_meeting(mid, $("tplSelect").value);
  const out = res.ok ? await waitJob(res.job_id) : null;
  state.enhanceBusy = false;
  btn.disabled = false;
  $("enhanceLabel").textContent = "Aprimorar com IA";
  if (out) {
    // Render only if the same meeting is still on screen; the summary is in
    // the DB either way and shows on next open.
    if (state.current && state.current.id === mid) {
      state.current.summary_md = out.summary_md;
      state.current.summary_provider = out.provider;
      renderEnhanced(out.summary_md, out.provider);
      selectTab("enhanced");
    }
    toast("Notas aprimoradas com sucesso!");
  } else {
    toast("Falha no aprimoramento. Verifique as Configurações de IA.", 4000);
  }
}

/* -------------------------------------------------------------- chat */
function renderChat(panel, messages) {
  panel.innerHTML = "";
  for (const m of messages) appendChat(panel, m.role, m.content);
  panel.classList.toggle("visible", messages.length > 0);
}
function appendChat(panel, role, content) {
  const div = document.createElement("div");
  div.className = "chat-msg " + role;
  div.innerHTML = `<div class="who">${role === "user" ? "You" : "Muesli"}</div>
                   <div class="body md"></div>`;
  div.querySelector(".body").innerHTML = renderMd(content);
  panel.appendChild(div);
  panel.classList.add("visible");
  panel.scrollTop = panel.scrollHeight;
}

async function sendChat(scopeMeetingId, inputEl, panelEl) {
  const q = inputEl.value.trim();
  if (!q || state.chatBusy) return;
  state.chatBusy = true;
  inputEl.value = "";
  appendChat(panelEl, "user", q);
  const thinking = document.createElement("div");
  thinking.className = "chat-msg";
  thinking.innerHTML = '<div class="who">Muesli</div><div class="body"><span class="spin"></span></div>';
  panelEl.appendChild(thinking);
  panelEl.scrollTop = panelEl.scrollHeight;
  const res = await api().chat(scopeMeetingId, q);
  const out = res.ok ? await waitJob(res.job_id) : null;
  thinking.remove();
  // Meeting-scoped replies only belong on screen if that meeting is still open
  // (the reply is stored in the DB and reappears with the meeting either way).
  const stillRelevant = !scopeMeetingId ||
    (state.current && state.current.id === scopeMeetingId);
  if (stillRelevant) {
    appendChat(panelEl, "assistant", out ? out.text : "Something went wrong. Try again.");
  } else if (!out) {
    toast("Chat request failed. Try again.", 3500);
  }
  state.chatBusy = false;
}

/* -------------------------------------------------------------- notes */
function scheduleNoteSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(flushNotes, 800);
}
async function flushNotes() {
  clearTimeout(state.saveTimer);
  if (state.current) {
    await api().save_notes(state.current.id, $("notepad").value);
    state.current.notes_md = $("notepad").value;
  }
}

/* ----------------------------------------------------------- settings */
async function openSettings() {
  if (!isApiReady()) {
    toast("Aguardando inicialização do backend para abrir configurações...", 3500);
    return;
  }
  $("settingsModal").classList.add("visible");
  toast("Carregando configurações...", 1200);
  try {
    const s = await api().get_settings();
    state.settings = s;
    // audio devices
    const devs = await api().list_audio_devices();
    const fill = (sel, items, chosen) => {
      sel.innerHTML = '<option value="">System default</option>';
      for (const d of items) {
        const o = document.createElement("option");
        o.value = d.index;
        o.textContent = d.name + (d.default ? "  (default)" : "");
        sel.appendChild(o);
      }
      sel.value = chosen === null || chosen === undefined ? "" : String(chosen);
      if (sel.selectedIndex < 0) sel.value = "";
    };
    fill($("setMic"), devs.mics || [], s.audio.mic_device_index);
    fill($("setLoop"), devs.loopbacks || [], s.audio.loopback_device_index);
    if (!devs.ok) toast("Problema de áudio: " + devs.error, 5000);
    $("setRetain").checked = !!s.audio.retain_audio;
    // stt
    $("setSttEngine").value = s.stt.engine;
    $("setSttModel").value = s.stt.model;
    $("setSttLang").value = s.stt.language || "";
    // ai
    $("setLlmProvider").value = s.llm.provider;
    $("setOllamaUrl").value = s.llm.ollama_url;
    const models = await api().list_ollama_models();
    const om = $("setOllamaModel");
    om.innerHTML = '<option value="">First available</option>';
    for (const m of models) {
      const o = document.createElement("option");
      o.value = m; o.textContent = m;
      om.appendChild(o);
    }
    om.value = s.llm.ollama_model || "";
    $("ollamaHint").textContent = models.length
      ? `Ollama está ativo com ${models.length} modelo(s).`
      : "Ollama não detectado. Instale de ollama.com para IA local gratuita.";
    $("setKeyAnthropic").value = s.llm.anthropic_key;
    $("setKeyOpenai").value = s.llm.openai_key;
    $("setKeyGroq").value = s.llm.groq_key;
    $("setModelAnthropic").value = s.llm.anthropic_model;
    $("setModelOpenai").value = s.llm.openai_model;
    $("setModelGroq").value = s.llm.groq_model;
    // general
    $("setStartup").checked = !!s.launch_at_startup;
    $("setConsent").checked = !!s.consent.enabled;
    $("setConsentText").value = s.consent.snippet;
    renderTplManager();
  } catch (err) {
    console.error("Erro ao carregar configurações:", err);
    toast("Erro ao carregar configurações: " + (err.message || err), 4000);
  }
}

async function saveSettings() {
  const patch = {
    audio: {
      mic_device_index: $("setMic").value === "" ? null : parseInt($("setMic").value, 10),
      loopback_device_index: $("setLoop").value === "" ? null : parseInt($("setLoop").value, 10),
      retain_audio: $("setRetain").checked,
    },
    stt: {
      engine: $("setSttEngine").value,
      model: $("setSttModel").value,
      language: $("setSttLang").value,
    },
    llm: {
      provider: $("setLlmProvider").value,
      ollama_url: $("setOllamaUrl").value.trim() || "http://localhost:11434",
      ollama_model: $("setOllamaModel").value,
      anthropic_key: $("setKeyAnthropic").value.trim(),
      openai_key: $("setKeyOpenai").value.trim(),
      groq_key: $("setKeyGroq").value.trim(),
      anthropic_model: $("setModelAnthropic").value.trim(),
      openai_model: $("setModelOpenai").value.trim(),
      groq_model: $("setModelGroq").value.trim(),
    },
    consent: {
      enabled: $("setConsent").checked,
      snippet: $("setConsentText").value,
    },
    launch_at_startup: $("setStartup").checked,
  };
  await api().save_settings(patch);
  $("settingsModal").classList.remove("visible");
  toast("Settings saved");
  refreshStatus();
  state.settings = await api().get_settings();
}

/* ------------------------------------------------- templates manager */
let editingTpl = null;

async function loadTemplates() {
  state.templates = await api().get_templates();
  const sel = $("tplSelect");
  const prev = sel.value;
  sel.innerHTML = "";
  for (const t of state.templates) {
    const o = document.createElement("option");
    o.value = t.id;
    o.textContent = t.name;
    sel.appendChild(o);
  }
  if (prev) sel.value = prev;
  if (!sel.value) sel.value = "auto";
}

function renderTplManager() {
  const list = $("tplManagerList");
  list.innerHTML = "";
  for (const t of state.templates) {
    const row = document.createElement("div");
    row.className = "tpl-row";
    row.innerHTML = `<span class="nm"></span><span class="ds"></span>` +
      (t.builtin ? '<span class="builtin-tag">built-in</span>'
                 : '<button class="edit">Edit</button><button class="del">Delete</button>');
    row.querySelector(".nm").textContent = t.name;
    row.querySelector(".ds").textContent = t.description || t.sections.map(s => s.title).join(", ");
    if (!t.builtin) {
      row.querySelector(".edit").addEventListener("click", () => openTplEditor(t));
      row.querySelector(".del").addEventListener("click", async () => {
        await api().delete_template(t.id);
        await loadTemplates();
        renderTplManager();
      });
    }
    list.appendChild(row);
  }
}

function openTplEditor(tpl) {
  editingTpl = tpl ? tpl.id : null;
  $("tplEditor").style.display = "block";
  $("tplName").value = tpl ? tpl.name : "";
  $("tplDesc").value = tpl ? tpl.description || "" : "";
  $("tplSections").innerHTML = "";
  const secs = tpl ? tpl.sections : [{ title: "Summary", hint: "" }, { title: "Action items", hint: "" }];
  for (const s of secs) addSectionRow(s.title, s.hint || "");
}

function addSectionRow(title, hint) {
  const row = document.createElement("div");
  row.className = "sec-row";
  row.innerHTML = `<input placeholder="Section title" class="sec-title">
                   <input placeholder="What goes in it (guides the AI)" class="sec-hint">
                   <button class="del">&times;</button>`;
  row.querySelector(".sec-title").value = title || "";
  row.querySelector(".sec-hint").value = hint || "";
  row.querySelector(".del").addEventListener("click", () => row.remove());
  $("tplSections").appendChild(row);
}

async function saveTpl() {
  const sections = [...$("tplSections").querySelectorAll(".sec-row")].map(r => ({
    title: r.querySelector(".sec-title").value.trim(),
    hint: r.querySelector(".sec-hint").value.trim(),
  })).filter(s => s.title);
  const res = await api().save_template({
    id: editingTpl, name: $("tplName").value, description: $("tplDesc").value, sections,
  });
  if (!res.ok) return toast(res.error || "Could not save template");
  $("tplEditor").style.display = "none";
  await loadTemplates();
  renderTplManager();
  toast("Template saved");
}

/* -------------------------------------------------------- status line */
let fontsInjected = false;
function injectFonts(faces) {
  // Satoshi is fetched per-machine from Fontshare (its EULA forbids shipping the
  // files with the app); once cached locally these file:/// faces take over from
  // the system-font fallback.
  if (fontsInjected || !faces || !faces.length) return;
  fontsInjected = true;
  const css = faces.map(f =>
    `@font-face { font-family: "Satoshi"; src: url("${f.file}") format("woff2");
      font-weight: ${f.weight}; font-style: normal; font-display: swap; }`).join("\n");
  const el = document.createElement("style");
  el.textContent = css;
  document.head.appendChild(el);
}

async function refreshStatus() {
  try {
    const st = await api().get_state();
    injectFonts(st.font_faces);
    const bits = [];
    // escapeHtml on provider/model strings: they come from user settings.
    if (st.llm.provider) bits.push(`AI: <b>${escapeHtml(st.llm.provider)}</b> (${escapeHtml(st.llm.detail || "")})`);
    else bits.push("AI: built-in summarizer only");
    if (st.stt_model.state === "loading") bits.push("Whisper: loading…");
    else if (st.stt_model.state === "error") bits.push("Whisper: error");
    else bits.push(`Whisper: ${escapeHtml(st.stt_model.name || "ready")}`);
    if (!st.audio_backend_ok) bits.push("Audio backend failed");
    $("statusLine").innerHTML = bits.join("<br>");
    // resume live session state after a UI reload
    if (st.recording && !state.recording) {
      state.recording = true;
      state.activeMeetingId = st.active_meeting_id;
      state.segCount = 0; // renderedSegIds dedup absorbs the replayed backlog
      startPolling();
      setRecordingUi(!!(state.current && state.current.id === st.active_meeting_id));
      refreshMeetings(); // show the recording badge in the sidebar
    }
    state.settingsState = st;
  } catch (e) { /* api not ready yet */ }
}

/* --------------------------------------------------------------- init */
function wireEvents() {
  $("btnNew").addEventListener("click", startNewNote);
  $("btnNewHome").addEventListener("click", startNewNote);
  $("btnStop").addEventListener("click", stopRecording);
  $("btnEnhance").addEventListener("click", enhanceNow);
  $("searchInput").addEventListener("input", () => refreshMeetings());
  $("notepad").addEventListener("input", scheduleNoteSave);
  $("tabNotes").addEventListener("click", () => selectTab("notes"));
  $("tabEnhanced").addEventListener("click", () => selectTab("enhanced"));
  $("tabCopilot")?.addEventListener("click", () => selectTab("copilot"));
  $("btnCollapseTr").addEventListener("click", () => {
    $("transcriptPane").classList.toggle("collapsed");
    $("btnCollapseTr").innerHTML = $("transcriptPane").classList.contains("collapsed") ? "&#x276E;" : "&#x276F;";
  });
  $("noteTitle").addEventListener("change", async () => {
    if (state.current) {
      await api().rename_meeting(state.current.id, $("noteTitle").value);
      refreshMeetings();
    }
  });
  $("tplSelect").addEventListener("change", () => {
    if (state.current) api().set_meeting_template(state.current.id, $("tplSelect").value);
  });
  $("btnDelete").addEventListener("click", async () => {
    if (!state.current) return;
    const doomedId = state.current.id; // pin it: awaits below can change the view
    if (!confirm("Delete this meeting, its transcript and notes?")) return;
    if (doomedId === state.activeMeetingId && state.recording) await stopRecording();
    await api().delete_meeting(doomedId);
    if (state.current && state.current.id === doomedId) showHome();
    else refreshMeetings();
  });
  $("btnExport").addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    if (!state.current) { toast("Nenhuma reunião selecionada", 2500); return; }
    toast("Abrindo diálogo para salvar...", 1500);
    try {
      await flushNotes();
      const res = await api().export_markdown(state.current.id, true);
      if (res.ok) toast("Exportado com sucesso para " + res.path, 4000);
      else if (res.error) toast("Falha ao exportar: " + res.error, 4000);
    } catch (e) {
      toast("Erro ao exportar: " + (e.message || e), 4000);
    }
  });
  const copyCopilotAction = async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    if (!state.current) { toast("Nenhuma reunião selecionada", 2500); return; }
    try {
      await flushNotes();
      const res = await api().copy_copilot_prompt(state.current.id);
      if (res.ok) {
        toast("Prompt copiado! Cole no Microsoft Copilot com Ctrl+V", 4500);
      } else {
        toast("Falha ao copiar prompt: " + (res.error || ""), 4000);
      }
    } catch (e) {
      toast("Erro ao copiar prompt: " + (e.message || e), 4000);
    }
  };
  $("btnCopyCopilot")?.addEventListener("click", copyCopilotAction);
  $("btnCopyCopilotBig")?.addEventListener("click", copyCopilotAction);
  $("btnCopySummary").addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    if (!state.current) { toast("Nenhuma reunião selecionada", 2500); return; }
    try {
      await flushNotes();
      const res = await api().copy_markdown(state.current.id, false);
      toast(res.ok ? "Copiado para Obsidian!" : "Falha ao copiar");
    } catch (e) {
      toast("Erro ao copiar: " + (e.message || e), 4000);
    }
  });
  $("btnCopyAll")?.addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    if (!state.current) { toast("Nenhuma reunião selecionada", 2500); return; }
    try {
      await flushNotes();
      const res = await api().copy_markdown(state.current.id, true);
      toast(res.ok ? "Copiado com transcrição completa!" : "Falha ao copiar");
    } catch (e) {
      toast("Erro ao copiar: " + (e.message || e), 4000);
    }
  });
  // chat: meeting scope (the pill can widen it to all meetings)
  const meetingScope = () =>
    ($("chatScope").textContent === "All meetings" || !state.current) ? null : state.current.id;
  $("btnSend").addEventListener("click", () =>
    sendChat(meetingScope(), $("chatInput"), $("chatPanel")));
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChat(meetingScope(), $("chatInput"), $("chatPanel"));
  });
  document.querySelectorAll(".qa").forEach(b => b.addEventListener("click", () => {
    $("chatInput").value = b.dataset.q;
    sendChat(meetingScope(), $("chatInput"), $("chatPanel"));
  }));
  $("chatScope").addEventListener("click", function () {
    this.textContent = this.textContent === "This meeting" ? "All meetings" : "This meeting";
  });
  // home chat
  $("btnHomeSend").addEventListener("click", () => sendChat(null, $("homeChatInput"), $("homeChatPanel")));
  $("homeChatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChat(null, $("homeChatInput"), $("homeChatPanel"));
  });
  // settings
  $("btnSettings").addEventListener("click", openSettings);
  $("btnCloseSettings").addEventListener("click", () => $("settingsModal").classList.remove("visible"));
  $("btnCancelSettings").addEventListener("click", () => $("settingsModal").classList.remove("visible"));
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnRefreshDevices").addEventListener("click", openSettings);
  $("btnOpenData").addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    try {
      await api().open_data_dir();
      toast("Pasta de dados aberta no Explorer!");
    } catch (e) {
      toast("Erro ao abrir pasta: " + (e.message || e), 4000);
    }
  });
  $("btnCopyConsent").addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    await api().copy_text($("setConsentText").value);
    toast("Texto copiado para a área de transferência!");
  });
  document.querySelectorAll(".modal-tabs .tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".modal-tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    document.querySelectorAll(".set-section").forEach(s =>
      s.classList.toggle("visible", s.dataset.set === t.dataset.set));
  }));
  $("btnNewTemplate").addEventListener("click", () => openTplEditor(null));
  $("btnAddSection").addEventListener("click", () => addSectionRow("", ""));
  $("btnCancelTpl").addEventListener("click", () => $("tplEditor").style.display = "none");
  $("btnSaveTpl").addEventListener("click", saveTpl);
}

let bootRetryCount = 0;
const MAX_BOOT_RETRIES = 240; // 240 * 250ms = 60 segundos (1 minuto max)

function scheduleBootRetry() {
  if (state.booted) return;
  if (isApiReady()) {
    boot();
    return;
  }
  bootRetryCount++;
  if (bootRetryCount <= MAX_BOOT_RETRIES) {
    if (bootRetryCount % 8 === 0) { // a cada 2 segundos
      const secLeft = Math.round((MAX_BOOT_RETRIES - bootRetryCount) * 0.25);
      const el = $("statusLine");
      if (el) el.innerHTML = `Iniciando backend (${secLeft}s)...`;
    }
    setTimeout(scheduleBootRetry, 250);
  } else {
    const el = $("statusLine");
    if (el) el.innerHTML = `<span style="color:#d9534f;font-weight:600">Backend não respondeu (timeout 1 min)</span>`;
    toast("O backend não respondeu após 1 minuto. Verifique se o WebView2 está instalado ou se a pasta foi extraída do ZIP.", 8000);
  }
}

async function boot() {
  if (state.booted) return;
  if (!isApiReady()) {
    scheduleBootRetry();
    return;
  }
  state.booted = true;

  const el = $("statusLine");
  if (el) el.textContent = "Conectado. Carregando dados...";

  const h = new Date().getHours();
  if ($("homeGreeting")) {
    $("homeGreeting").textContent = h < 5 ? "Boa noite" : h < 12 ? "Bom dia" :
      h < 18 ? "Boa tarde" : "Boa noite";
  }

  try {
    await loadTemplates();
    state.settings = await api().get_settings();
    await refreshMeetings();
    try { renderChat($("homeChatPanel"), await api().get_global_chat()); } catch (e) {}
    await refreshStatus();
    setInterval(refreshStatus, 5000);
    toast("Muesli pronto para uso!", 2000);
  } catch (err) {
    console.error("Erro durante inicialização:", err);
    state.booted = false; // permite tentar novamente se falhou
    toast("Aviso ao carregar dados: " + (err.message || err), 4000);
    setTimeout(scheduleBootRetry, 500);
  }
}

// Conectar todos os eventos imediatamente para a interface nunca ficar inerte
wireEvents();

window.addEventListener("pywebviewready", () => {
  if (!state.booted) boot();
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", scheduleBootRetry);
} else {
  scheduleBootRetry();
}
