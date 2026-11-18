/* Muesli frontend. Talks to Python through window.pywebview.api. */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  meetings: [],
  categories: [],
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
  minutesEdits: new Map(),
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
  const cells = row => {
    const text = (row || "").trim();
    if (!text.startsWith("|") || !text.endsWith("|")) return [];
    return text.slice(1, -1).split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, "|"));
  };
  let html = "", inUl = false, inOl = false, inQuote = false;
  const closeLists = () => {
    if (inUl) { html += "</ul>"; inUl = false; }
    if (inOl) { html += "</ol>"; inOl = false; }
    if (inQuote) { html += "</blockquote>"; inQuote = false; }
  };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const header = cells(raw), separator = cells(lines[i + 1]);
    if (header.length && header.length === separator.length && separator.every(cell => /^:?-{3,}:?$/.test(cell))) {
      closeLists();
      const row = (items, tag) => `<tr>${items.map(cell => `<${tag}>${inlineMd(escapeHtml(cell))}</${tag}>`).join("")}</tr>`;
      html += `<div class="md-table-wrap"><table><thead>${row(header, "th")}</thead><tbody>`;
      i++;
      while (cells(lines[i + 1]).length === header.length) html += row(cells(lines[++i]), "td");
      html += "</tbody></table></div>";
      continue;
    }
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
  const [meetings, categories] = await Promise.all([
    api().list_meetings(q), api().list_categories(),
  ]);
  if (seq !== state.searchSeq) return; // a newer search finished first
  state.meetings = meetings;
  state.categories = categories;
  const categorySelect = $("meetingCategory");
  if (categorySelect) {
    categorySelect.innerHTML = '<option value="">Sem categoria</option>';
    for (const category of categories) {
      const option = document.createElement("option");
      option.value = category.id;
      option.textContent = category.name;
      categorySelect.appendChild(option);
    }
    categorySelect.value = state.current && state.current.category_id != null
      ? String(state.current.category_id) : "";
  }
  const list = $("meetingList");
  closeMeetingMenu();
  list.innerHTML = "";
  if (!state.meetings.length) {
    list.innerHTML = `<div class="list-empty">${q ? "No matches." : "No meetings yet.<br>Hit New note to start one."}</div>`;
  }
  let collapsed = new Set();
  try { collapsed = new Set(JSON.parse(localStorage.getItem("muesli_collapsed_categories") || "[]")); } catch (e) {}
  const sections = [{ id: "uncategorized", name: "Sem categoria", categoryId: null },
    ...categories.map(c => ({ id: String(c.id), name: c.name, categoryId: c.id }))];
  for (const sectionData of sections) {
    const items = state.meetings.filter(m => (m.category_id ?? null) === sectionData.categoryId);
    if (items.length === 0 && sectionData.id !== "uncategorized" && q) continue;
    const section = document.createElement("section");
    section.className = "category-section";
    const heading = document.createElement("div");
    heading.className = "category-heading";
    const toggle = document.createElement("button");
    toggle.className = "category-toggle";
    const isCollapsed = collapsed.has(sectionData.id);
    toggle.innerHTML = `<span class="chevron">${isCollapsed ? "▶" : "▼"}</span>`;
    toggle.appendChild(document.createTextNode(sectionData.name));
    toggle.title = isCollapsed ? "Expandir categoria" : "Recolher categoria";
    toggle.setAttribute("aria-expanded", String(!isCollapsed));
    toggle.addEventListener("click", () => {
      const next = new Set();
      try { JSON.parse(localStorage.getItem("muesli_collapsed_categories") || "[]").forEach(x => next.add(x)); } catch (e) {}
      if (next.has(sectionData.id)) next.delete(sectionData.id); else next.add(sectionData.id);
      localStorage.setItem("muesli_collapsed_categories", JSON.stringify([...next]));
      refreshMeetings();
    });
    heading.appendChild(toggle);
    if (sectionData.categoryId !== null) {
      const add = document.createElement("button");
      add.className = "category-action"; add.textContent = "+"; add.title = `Nova reunião em ${sectionData.name}`;
      add.disabled = state.recording;
      add.addEventListener("click", () => startNewNote(sectionData.categoryId));
      heading.appendChild(add);
      const rename = document.createElement("button");
      rename.className = "category-action"; rename.textContent = "…"; rename.title = "Renomear categoria";
      rename.addEventListener("click", async () => {
        const name = prompt("Nome da categoria:", sectionData.name);
        if (name === null) return;
        const result = await api().rename_category(sectionData.categoryId, name);
        if (!result.ok) return toast(result.error);
        await refreshMeetings();
      });
      heading.appendChild(rename);
      const remove = document.createElement("button");
      remove.className = "category-action"; remove.textContent = "×"; remove.title = "Excluir categoria";
      remove.addEventListener("click", async () => {
        if (!confirm(`Excluir “${sectionData.name}”? As reuniões serão mantidas sem categoria.`)) return;
        await api().delete_category(sectionData.categoryId);
        await refreshMeetings();
        if (state.current) openMeeting(state.current.id);
      });
      heading.appendChild(remove);
    }
    section.appendChild(heading);
    if (!isCollapsed || q) {
      let lastGroup = "";
      for (const m of items) {
        const group = dateGroup(m.created_at);
        if (group !== lastGroup) {
          lastGroup = group;
          const dateHeading = document.createElement("div");
          dateHeading.className = "date-group"; dateHeading.textContent = group;
          section.appendChild(dateHeading);
        }
        const btn = document.createElement("button");
        btn.className = "meeting-item" + (state.current && state.current.id === m.id ? " active" : "");
        const live = m.id === state.activeMeetingId && state.recording;
        btn.innerHTML = `<div class="title"></div><div class="meta">${live ? '<span class="live">● recording</span>' : ""}<span>${fmtDate(m.created_at)}</span><span>${fmtDur(m.duration_sec)}</span></div>`;
        btn.querySelector(".title").textContent = m.title;
        btn.setAttribute("aria-haspopup", "menu");
        btn.addEventListener("click", () => openMeeting(m.id));
        btn.addEventListener("contextmenu", event => {
          event.preventDefault();
          openMeetingMenu(m, btn, event.clientX, event.clientY);
        });
        btn.addEventListener("keydown", event => {
          if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
            event.preventDefault();
            const rect = btn.getBoundingClientRect();
            openMeetingMenu(m, btn, rect.left + 8, rect.bottom);
          }
        });
        section.appendChild(btn);
      }
    }
    list.appendChild(section);
  }
}

/* ------------------------------------------------------- meeting menu */
let meetingMenuTarget = null;

function closeMeetingMenu(restoreFocus = false) {
  if (!meetingMenuTarget) return;
  const {card} = meetingMenuTarget;
  meetingMenuTarget = null;
  $("meetingContextMenu").hidden = true;
  if (restoreFocus && card.isConnected) card.focus({preventScroll: true});
}

function openMeetingMenu(meeting, card, x, y) {
  const menu = $("meetingContextMenu");
  const running = state.recording && state.activeMeetingId === meeting.id;
  const enhance = $("menuEnhanceMeeting");
  enhance.textContent = (state.current?.id === meeting.id ? state.current.summary_md : meeting.preview)?.trim()
    ? "Reaprimorar" : "Aprimorar";
  enhance.disabled = running || state.enhanceBusy;
  $("menuDeleteMeeting").disabled = running;
  meetingMenuTarget = {meeting, card};
  menu.hidden = false;
  menu.style.left = `${Math.max(4, Math.min(x, window.innerWidth - menu.offsetWidth - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, window.innerHeight - menu.offsetHeight - 4))}px`;
  menu.querySelector('button:not(:disabled)').focus({preventScroll: true});
}

function wireMeetingMenu() {
  const menu = $("meetingContextMenu");
  menu.addEventListener("click", event => {
    const action = event.target.closest("button")?.id;
    const m = meetingMenuTarget?.meeting;
    if (!m || !action) return;
    closeMeetingMenu(true);
    if (action === "menuEnhanceMeeting") {
      if (state.current?.id === m.id) enhanceNow();
      else enhanceMeeting(m.id, m.template_id || "auto",
        minutesJobs.get(m.id)?.profile || state.settings?.minutes?.profile || "executiva");
    } else if (action === "menuCopyMeeting") copyCopilotPrompt(m.id);
    else if (action === "menuDeleteMeeting") deleteSavedMeeting(m.id);
  });
  menu.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeMeetingMenu(true); return; }
    if (event.key === "Tab") { closeMeetingMenu(true); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = [...menu.querySelectorAll('button:not(:disabled)')];
    const index = items.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
      : (index + items.length + (event.key === "ArrowDown" ? 1 : -1)) % items.length;
    items[next].focus();
  });
  document.addEventListener("pointerdown", event => {
    if (!menu.contains(event.target)) closeMeetingMenu();
  });
  $("meetingList").addEventListener("scroll", () => closeMeetingMenu());
  window.addEventListener("resize", () => closeMeetingMenu());
}

async function deleteSavedMeeting(mid) {
  if (mid == null) return;
  if (state.recording && mid === state.activeMeetingId) {
    toast("Pare a gravação antes de excluir a reunião.");
    return;
  }
  const title = (state.meetings.find(m => m.id === mid)?.title || "esta reunião");
  if (!confirm(`Excluir “${title}”, sua transcrição e suas anotações?`)) return;
  try {
    const result = await api().delete_meeting(mid);
    if (!result.ok) return toast(result.error || "Não foi possível excluir a reunião.");
    state.minutesEdits.delete(mid);
    if (state.current?.id === mid) {
      clearTimeout(state.saveTimer);
      state.current = null;
      await showHome();
    } else await refreshMeetings();
    toast("Reunião excluída.");
  } catch (err) {
    toast("Não foi possível excluir a reunião.");
  }
}

async function copyCopilotPrompt(mid) {
  if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
  if (mid == null) { toast("Nenhuma reunião selecionada", 2500); return; }
  try {
    if (state.current?.id === mid) await flushNotes();
    const res = await api().copy_copilot_prompt(mid);
    if (res.ok) toast("Prompt copiado! Cole no Microsoft Copilot com Ctrl+V", 4500);
    else toast("Falha ao copiar prompt: " + (res.error || ""), 4000);
  } catch (err) {
    toast("Erro ao copiar prompt: " + (err.message || err), 4000);
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
  if ($("meetingCategory")) $("meetingCategory").value = m.category_id == null ? "" : String(m.category_id);

  renderTranscript(m.segments || []);
  const edit = state.minutesEdits.get(m.id);
  if (edit) editLocalMinutes($("enhancedBody"), edit);
  else renderEnhanced(m.summary_md, m.summary_provider);
  syncMinutesControls(m);
  renderChat($("chatPanel"), m.chat || []);
  if (edit) {
    selectTab("enhanced");
  } else if ($("tabCopilot")?.classList.contains("active")) {
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
  $("btnNewLabel").textContent = state.recording ? "Gravando…" : "Nova reunião";
  $("btnNew").classList.toggle("recording", state.recording);
  document.body.classList.toggle("is-recording", !!state.recording);
  if (!on && $("trBatchBar")) $("trBatchBar").style.display = "none";
  const cb = $("consentBanner");
  if (state.recording) {
    const s = state.settings || {};
    const stt = s.stt || {};
    const keys = stt[`${stt.engine}_keys`] || (stt[`${stt.engine}_key`] ? [stt[`${stt.engine}_key`]] : []);
    const provider = state.settingsState && state.settingsState.llm && state.settingsState.llm.provider;
    const details = ["Captura: microfone e áudio do sistema quando disponíveis; sem vídeo."];
    if (stt.engine === "local" || !stt.engine) {
      details.push("Transcrição processada localmente; o áudio não é enviado para STT na nuvem.");
    } else if (keys.length) {
      details.push(`Transcrição na nuvem via ${stt.engine}: blocos de áudio são enviados a esse provedor.${stt.cloud_fallback !== false ? " Se a API falhar, o Whisper local será usado como reserva." : ""}`);
    } else if (stt.cloud_fallback !== false) {
      details.push("Nenhuma chave de STT na nuvem está configurada; o app tentará Whisper local como reserva.");
    } else {
      details.push("Nenhuma chave de STT na nuvem está configurada; a transcrição não funcionará até selecionar Whisper local ou configurar uma chave.");
    }
    const llm = s.llm || {};
    const localOpenAI = provider === "openai" && /^(https?:\/\/)?(localhost|127\.0\.0\.1|\[::1\])([:/]|$)/i.test(llm.openai_url || "");
    if (s.minutes?.enabled) {
      details.push("As atas são geradas neste computador. O chat segue a configuração separada e, se usar um provedor na nuvem, pode enviar notas e transcrição.");
    } else if (provider === "ollama" || localOpenAI) {
      details.push("Ao solicitar aprimoramento ou conversar com IA local, notas e transcrição permanecem neste computador.");
    } else if (provider && provider !== "none" && provider !== "extractive") {
      details.push(`Ao solicitar aprimoramento ou conversar com IA, notas e transcrição podem ser enviadas ao provedor ${provider}.`);
    } else {
      details.push("Aprimoramento e conversa usam o sumarizador integrado offline, salvo se você configurar um provedor de IA.");
    }
    cb.textContent = details.join(" ");
    const consent = s.consent || {};
    if (consent.enabled && consent.snippet) {
      cb.appendChild(document.createTextNode(" Lembrete: " + consent.snippet + " "));
      const copy = document.createElement("button");
      copy.type = "button"; copy.className = "copy-link"; copy.textContent = "Copiar aviso";
      copy.addEventListener("click", async () => {
        await api().copy_text(consent.snippet);
        toast("Aviso de consentimento copiado!");
      });
      cb.appendChild(copy);
    }
    cb.classList.add("visible");
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

function renderEnhanced(md, provider, preview = false) {
  const body = $("enhancedBody");
  if (!md) {
    body.innerHTML = `<div class="enhance-empty">Nenhum resumo gerado ainda.<br>
      Após a reunião, clique em <b>Aprimorar com IA</b> ou use a aba <b>Prompt Copilot</b>.</div>`;
    return;
  }
  const provNames = { local_minutes: "IA local · rascunho para revisão", ollama: "IA Local (Ollama)", extractive: "sumarizador integrado (offline)",
                      openrouter: "OpenRouter", anthropic: "Anthropic (Claude)", openai: "OpenAI (ChatGPT)", groq: "Groq" };
  body.innerHTML = `<div class="provider-tag">Gerado com ${provNames[provider] || provider || "IA"}</div>` +
                   renderMd(md);
  if (provider === "local_minutes") {
    body.innerHTML = body.innerHTML.replace(/\[\[seg:(\d+)@(\d+:\d{2}:\d{2})\]\]/g,
      '<button type="button" class="minutes-source" data-segment="$1" aria-label="Abrir transcrição em $2">$2</button>');
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "btn-mini";
    edit.textContent = preview ? "Voltar à ata salva" : "Editar ata";
    edit.onclick = preview ? () => renderEnhanced(state.current?.summary_md, state.current?.summary_provider)
      : () => {
        const draft = {base: md, value: md};
        state.minutesEdits.set(state.current.id, draft);
        editLocalMinutes(body, draft);
      };
    body.prepend(edit);
  }
}

function editLocalMinutes(body, draft) {
  const mid = state.current.id;
  body.innerHTML = '<textarea class="minutes-editor" aria-label="Ata em Markdown"></textarea>' +
    '<button type="button" class="btn-mini">Salvar ata</button> <button type="button" class="btn-mini">Cancelar</button>';
  const editor = body.querySelector("textarea");
  const [save, cancel] = body.querySelectorAll("button");
  editor.value = draft.value;
  editor.oninput = () => { draft.value = editor.value; };
  editor.focus();
  cancel.onclick = () => {
    state.minutesEdits.delete(mid);
    renderEnhanced(state.current.summary_md, state.current.summary_provider);
  };
  save.onclick = async () => {
    const value = editor.value.trim();
    if (!value) { toast("A ata não pode ficar vazia."); return; }
    if (draft.saving) return;
    draft.value = editor.value;
    draft.saving = editor.disabled = save.disabled = cancel.disabled = true;
    try {
      const res = await api().save_minutes_edit(mid, value, draft.base);
      if (!res.ok) {
        if (typeof res.latest === "string") {
          if (state.current?.id === mid) state.current.summary_md = res.latest;
          toast("A ata salva mudou. Sua edição foi mantida. Copie o texto antes de cancelar e abrir a nova versão.", 7500);
        } else toast("Não foi possível salvar a ata.");
        return;
      }
      state.minutesEdits.delete(mid);
      if (state.current?.id === mid) {
        state.current.summary_md = value;
        renderEnhanced(value, "local_minutes");
      }
      toast("Ata revisada salva.");
    } catch (err) {
      toast("Não foi possível salvar a ata.");
    } finally {
      draft.saving = editor.disabled = save.disabled = cancel.disabled = false;
    }
  };
  editor.disabled = save.disabled = cancel.disabled = !!draft.saving;
}

function renderTranscript(segments) {
  const body = $("transcriptBody");
  body.innerHTML = '<div class="tr-status" id="trStatus"></div>';
  state.renderedSegIds = new Set();
  for (const s of segments) addSegmentEl(s);
  if (!segments.length) {
    $("trStatus").textContent = state.recording && state.current &&
      state.current.id === state.activeMeetingId
      ? "Ouvindo áudio em tempo real…" : "Nenhuma fala capturada nesta reunião.";
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
  if (s.id !== undefined) div.id = `transcript-seg-${s.id}`;
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
async function startNewNote(categoryId = null) {
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
    const res = await api().start_meeting("", tpl, categoryId);
    if (!res.ok) {
      toast(res.error || "Não foi possível iniciar a gravação", 4000);
      if (res.meeting_id) { await refreshMeetings(); openMeeting(res.meeting_id); }
      return;
    }
    state.recording = true;
    state.activeMeetingId = res.meeting_id;
    state.segCount = 0;
    if (categoryId === null) {
      $("searchInput").value = "";
      try {
        const key = "muesli_collapsed_categories";
        const collapsed = JSON.parse(localStorage.getItem(key) || "[]");
        localStorage.setItem(key, JSON.stringify(collapsed.filter(id => id !== "uncategorized")));
      } catch (err) { /* no saved collapse preference */ }
    }
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
window.trayNewNote = () => startNewNote(null);

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
      // meters (rms 0..~0.3 mapped to 0..1 via scaleX to avoid layout reflow)
      $("meterMic").style.transform = `scaleX(${Math.min(1, (s.levels.mic || 0) * 4)})`;
      $("meterLoop").style.transform = `scaleX(${Math.min(1, (s.levels.loop || 0) * 4)})`;
      const tr = $("trStatus");
      const bb = $("trBatchBar");
      if (tr) {
        if (s.model && s.model.state === "loading") {
          tr.textContent = s.model.detail + "…";
          if (bb) bb.style.display = "none";
        } else if (s.detail) {
          if (state.segCount > 0 && bb) {
            tr.textContent = "";
            bb.textContent = s.detail;
            bb.style.display = "block";
          } else {
            tr.textContent = s.detail;
            if (bb) bb.style.display = "none";
          }
        } else if (state.segCount === 0) {
          tr.textContent = "Ouvindo áudio em tempo real…";
          if (bb) bb.style.display = "none";
        } else {
          tr.textContent = "";
          if (bb) bb.style.display = "none";
        }
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
  if (state.current?.id === stoppedId) await flushNotes();
  const res = await api().stop_meeting();
  const capture = res.ok ? await waitJob(res.job_id) : null;
  state.recording = false;
  state.activeMeetingId = null;
  stopPolling();
  $("btnStop").textContent = "Parar";
  $("btnStop").disabled = false;
  setRecordingUi(false);
  refreshMeetings();
  if (!capture) { if (!res.ok) toast(res.error); return; }
  const stopped = await waitJob(capture.finalize_job_id);
  if (!stopped) return;
  if (stopped.minutes_job_id) watchMinutesJob(stoppedId, stopped.minutes_job_id);
  // Only reload the view if the user is still looking at the stopped meeting.
  if (state.current && state.current.id === stoppedId) {
    await openMeeting(stoppedId);
  }

  if (stopped.minutes_job_id) {
    toast("Gravação finalizada. Ata local na fila.");
    await refreshMinutesQueue();
    $("minutesQueue").open = true;
  } else if (!stopped.local_minutes && state.current?.id === stoppedId) {
    toast("Gravação finalizada. Aprimorando notas com IA…");
    await enhanceNow();
  } else toast("Gravação finalizada. Transcrição salva.");
}

function waitJob(jobId, onProgress) {
  return new Promise((resolve) => {
    let inFlight = false;
    const t = setInterval(async () => {
      if (inFlight) return; // never overlap polls for the same job
      inFlight = true;
      let j;
      try { j = await api().job_status(jobId); }
      catch (e) { inFlight = false; return; }
      inFlight = false;
      if (j.state === "running" && onProgress) onProgress(j);
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
let minutesJobs = new Map();
const watchedMinutes = new Map();
let minutesPreview;
let minutesQueuePolling = false;

function watchMinutesJob(mid, id) {
  watchedMinutes.set(mid, id);
}

function syncMinutesControls(meeting, chooseFormat = true) {
  const job = minutesJobs.get(meeting?.id);
  const local = !!state.settings?.minutes?.enabled;
  $("tplSelect").hidden = local;
  $("minutesProfile").hidden = !local;
  if (chooseFormat) $("minutesProfile").value = job?.profile || state.settings?.minutes?.profile || "executiva";
  const label = job ? `${job.completed}/${job.total || "–"} blocos${job.failed ? ` · ${job.failed} com falha` : ""} · ${job.detail}` : "";
  if ($("minutesStatus").textContent !== label) $("minutesStatus").textContent = label;
}

async function refreshMinutesQueue() {
  if (minutesQueuePolling) return;
  minutesQueuePolling = true;
  try {
    const rows = await api().list_minutes_jobs();
    const next = new Map(rows.map(job => [job.meeting_id, job]));
    minutesJobs = next;
    for (const job of rows) {
      if (!watchedMinutes.has(job.meeting_id)) watchMinutesJob(job.meeting_id, job.id);
    }
    for (const [mid, id] of watchedMinutes) {
      if (next.get(mid)?.id === id) continue;
      const status = await api().job_status(id);
      if (watchedMinutes.get(mid) !== id) continue;
      if (status.state === "done" && status.result) showEnhancement(mid, status.result);
      if (["done", "unknown"].includes(status.state) || (next.has(mid) && next.get(mid).id !== id)) {
        watchedMinutes.delete(mid);
      }
    }
    syncMinutesControls(state.current, false);
    $("minutesQueue").hidden = !rows.length;
    $("minutesQueueCount").textContent = String(rows.length);
    const container = $("minutesQueueRows");
    const existing = new Map([...container.children].map(row => [row.dataset.job, row]));
    for (const job of rows) {
      const previous = existing.get(job.id);
      existing.delete(job.id);
      const structure = `${job.state}:${!!job.completed}`;
      const statusText = `${job.completed}/${job.total || "–"} blocos · ${job.detail}`;
      if (previous?.dataset.structure === structure) {
        previous.querySelector("strong").textContent = job.title;
        previous.querySelector("p").textContent = statusText;
        const progress = previous.querySelector("progress");
        progress.max = job.total || 1;
        progress.value = job.completed;
        progress.setAttribute("aria-label", `Progresso: ${job.title}`);
        continue;
      }
      const row = document.createElement("section");
      row.className = "minutes-job";
      row.dataset.job = job.id;
      row.dataset.structure = structure;
      const controls = document.createElement("div");
      controls.className = "minutes-controls";
      const button = (label, action) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn-mini";
        b.textContent = label;
        b.onclick = async () => {
          b.disabled = true;
          try { await action(); }
          catch { toast("Não foi possível atualizar a ata."); }
          finally { b.disabled = false; }
        };
        controls.append(b);
      };
      const progress = document.createElement("progress");
      progress.max = job.total || 1;
      progress.value = job.completed;
      progress.setAttribute("aria-label", `Progresso: ${job.title}`);
      const title = document.createElement("strong");
      title.textContent = job.title;
      const detail = document.createElement("p");
      detail.textContent = statusText;
      button("Abrir", () => openMeeting(job.meeting_id));
      if (["running", "queued", "paused", "partial", "error"].includes(job.state)) {
        const pause = ["running", "queued"].includes(job.state);
        button(pause ? "Pausar" : job.state === "partial" ? "Refazer falhas" : "Retomar", async () => {
          const res = await api().set_minutes_paused(job.meeting_id, pause);
          if (!res.ok) toast(res.error);
          await refreshMinutesQueue();
        });
      }
      if (job.completed && job.state !== "conflict") button("Ver parcial", async () => {
        if (state.minutesEdits.has(job.meeting_id)) {
          toast("Salve ou cancele sua edição antes de abrir a prévia.");
          return;
        }
        const request = {id: job.id, mid: job.meeting_id};
        minutesPreview = request;
        if (state.current?.id !== job.meeting_id) await openMeeting(job.meeting_id);
        const result = await api().get_minutes_preview(job.meeting_id);
        if (minutesPreview !== request || minutesJobs.get(request.mid)?.id !== request.id ||
            state.current?.id !== request.mid || state.minutesEdits.has(request.mid)) return;
        if (!result.ok) { toast(result.error); return; }
        if (state.current?.id === job.meeting_id) {
          renderEnhanced(result.summary_md, "local_minutes", true);
          selectTab("enhanced");
        }
      });
      row.append(title, detail, progress, controls);
      if (previous) previous.replaceWith(row);
      else container.append(row);
    }
    for (const row of existing.values()) row.remove();
  } catch (err) {
    console.warn("Fila local indisponível", err);
  } finally {
    minutesQueuePolling = false;
  }
}

async function enhanceNow() {
  if (!state.current) return;
  return enhanceMeeting(state.current.id, $("tplSelect").value, $("minutesProfile").value);
}

async function enhanceMeeting(mid, templateId, minutesProfile) {
  if (state.enhanceBusy) return;
  if (state.recording && mid === state.activeMeetingId) {
    toast("Pare a gravação antes de aprimorar a reunião.");
    return;
  }
  state.enhanceBusy = true;
  const btn = $("btnEnhance");
  btn.disabled = true;
  $("enhanceLabel").innerHTML = 'Aprimorando&hellip; <span class="spin"></span>';
  try {
    if (state.current?.id === mid) await flushNotes();
    else toast("Aprimorando reunião…");
    const res = await api().enhance_meeting(mid, templateId, minutesProfile);
    if (!res.ok) { toast(res.error || "Não foi possível gerar o resumo.", 4000); return; }
    if (res.persistent) {
      watchMinutesJob(mid, res.job_id);
      toast("Ata local adicionada à fila.");
      await refreshMinutesQueue();
      $("minutesQueue").open = true;
      return;
    }
    const out = await waitJob(res.job_id);
    if (out && !out.skipped) showEnhancement(mid, out);
  } catch (err) {
    toast("Não foi possível gerar o resumo.", 4000);
  } finally {
    state.enhanceBusy = false;
    btn.disabled = false;
    $("enhanceLabel").textContent = "Aprimorar com IA";
  }
}

function showEnhancement(mid, out) {
  if (minutesPreview?.mid === mid) minutesPreview = null;
  if (state.current?.id === mid) {
    state.current.summary_md = out.summary_md;
    state.current.summary_provider = out.provider;
    if (out.title) {
      state.current.title = out.title;
      if (document.activeElement !== $("noteTitle")) {
        $("noteTitle").value = out.title === "Untitled meeting" ? "" : out.title;
      }
    }
    if (!$("enhancedBody").querySelector(".minutes-editor")) {
      renderEnhanced(out.summary_md, out.provider);
      selectTab("enhanced");
    }
  }
  refreshMeetings();
  if (out.provider === "local_minutes") {
    const detail = out.backend ? ` · ${out.backend} · ${out.elapsed_sec} s` : "";
    toast(`Rascunho da ata local salvo${detail}. ${out.detail || ""}`.trim(), 6500);
  } else toast("Notas aprimoradas com sucesso!");
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
  const meeting = state.current;
  if (meeting) {
    const notes = $("notepad").value;
    await api().save_notes(meeting.id, notes);
    meeting.notes_md = notes;
  }
}

/* ----------------------------------------------------------- settings */
let minutesDownloadWatch = null;

async function syncMinutesModel() {
  const status = await api().local_minutes_status();
  $("btnDownloadMinutes").disabled = !status.runtime || status.installed || !!status.download_job_id;
  // An existing opt-in can still be cleared if the model was removed.
  $("setLocalMinutes").disabled = (!status.runtime || !status.installed) && !$("setLocalMinutes").checked;
  if (!status.download_job_id) {
    $("minutesModelHint").textContent = !status.runtime ? "Motor indisponível. Use o pacote Windows x64 do Muesli."
      : status.installed ? "Modelo instalado. Você já pode ativar as atas locais."
      : "O download é opcional. Depois de instalado, o modelo funciona offline.";
  }
  return status;
}

async function watchMinutesDownload(jid, total) {
  if (minutesDownloadWatch === jid) return;
  minutesDownloadWatch = jid;
  const bar = $("minutesDownloadProgress");
  bar.value = 0;
  bar.hidden = false;
  $("minutesModelHint").textContent = "Baixando modelo…";
  try {
    const out = await waitJob(jid, j => {
      bar.value = Math.floor(100 * (j.received || 0) / total);
      $("minutesModelHint").textContent = `Baixando modelo… ${bar.value}%`;
    });
    await syncMinutesModel();
    if (out) toast("Modelo de atas instalado.");
  } finally {
    bar.hidden = true;
    minutesDownloadWatch = null;
  }
}

async function downloadMinutesModel() {
  const button = $("btnDownloadMinutes");
  button.disabled = true;
  try {
    const res = await api().download_minutes_model();
    const status = await syncMinutesModel();
    if (res.ok) await watchMinutesDownload(res.job_id, status.size);
    else toast(res.error);
  } catch (err) {
    button.disabled = false;
    toast("Não foi possível iniciar ou acompanhar o download.");
  }
}

function applyTheme(mode) {
  const m = mode || "light";
  try { localStorage.setItem("muesli_theme", m); } catch (e) {}
  let eff = m;
  if (m === "auto") {
    eff = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  document.documentElement.dataset.theme = eff;
}

if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const cur = (state.settings && state.settings.ui && state.settings.ui.theme) || "light";
    if (cur === "auto") applyTheme("auto");
  });
}

function syncSttUi() {
  const eng = $("setSttEngine")?.value || "local";
  if ($("sttLocalBox")) $("sttLocalBox").style.display = eng === "local" ? "block" : "none";
  if ($("sttOpenrouterBox")) $("sttOpenrouterBox").style.display = eng === "openrouter" ? "block" : "none";
  if ($("sttOpenaiBox")) $("sttOpenaiBox").style.display = eng === "openai" ? "block" : "none";
  if ($("sttGroqBox")) $("sttGroqBox").style.display = eng === "groq" ? "block" : "none";
  if ($("sttCloudFallbackWrap")) $("sttCloudFallbackWrap").style.display = eng !== "local" ? "block" : "none";
  const orSel = $("setSttOpenrouterModel");
  if ($("sttOrCustomModelWrap") && orSel) {
    $("sttOrCustomModelWrap").style.display = orSel.value === "__custom__" ? "block" : "none";
  }
}

function renderKeyList(containerId, keys, placeholder) {
  const box = $(containerId);
  if (!box) return;
  box.innerHTML = "";
  const arr = Array.isArray(keys) && keys.length ? keys : [""];
  arr.forEach((k, idx) => appendKeyRow(box, k || "", placeholder, idx + 1));
}

function appendKeyRow(box, value, placeholder, num) {
  const row = document.createElement("div");
  row.className = "key-row";
  row.innerHTML = `
    <span class="key-idx">#${num}</span>
    <input type="password" placeholder="${escapeHtml(placeholder)}" autocomplete="off">
    <button type="button" class="key-btn eye" title="Mostrar ou ocultar">👁</button>
    <button type="button" class="key-btn del" title="Remover chave">&times;</button>
  `;
  const inp = row.querySelector("input");
  inp.value = value || "";
  row.querySelector(".eye").addEventListener("click", () => {
    inp.type = inp.type === "password" ? "text" : "password";
  });
  row.querySelector(".del").addEventListener("click", () => {
    if (box.querySelectorAll(".key-row").length > 1) {
      row.remove();
      box.querySelectorAll(".key-row .key-idx").forEach((el, i) => { el.textContent = `#${i + 1}`; });
    } else {
      inp.value = "";
    }
  });
  box.appendChild(row);
}

function addKeyRow(containerId, placeholder) {
  const box = $(containerId);
  if (!box) return;
  const count = box.querySelectorAll(".key-row").length;
  appendKeyRow(box, "", placeholder, count + 1);
  box.querySelector(".key-row:last-child input")?.focus();
}

function readKeyList(containerId) {
  const box = $(containerId);
  if (!box) return [];
  return [...box.querySelectorAll(".key-row input")].map(i => i.value.trim()).filter(Boolean);
}

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
    $("setSttEngine").value = s.stt.engine || "local";
    $("setSttModel").value = s.stt.model || "base";
    if ($("setSttDevice")) $("setSttDevice").value = s.stt.device || "cpu";
    $("setSttLang").value = s.stt.language || "";
    renderKeyList("keysSttOpenrouter", s.stt.openrouter_keys?.length ? s.stt.openrouter_keys : (s.stt.openrouter_key ? [s.stt.openrouter_key] : []), "sk-or-v1-…");
    renderKeyList("keysSttOpenai", s.stt.openai_keys?.length ? s.stt.openai_keys : (s.stt.openai_key ? [s.stt.openai_key] : []), "sk-… (opcional)");
    if ($("setSttOpenaiModel")) $("setSttOpenaiModel").value = s.stt.openai_model || "whisper-1";
    renderKeyList("keysSttGroq", s.stt.groq_keys?.length ? s.stt.groq_keys : (s.stt.groq_key ? [s.stt.groq_key] : []), "gsk_… (opcional)");
    if ($("setSttGroqModel")) $("setSttGroqModel").value = s.stt.groq_model || "whisper-large-v3-turbo";
    if ($("setSttCloudInterval")) $("setSttCloudInterval").value = String(s.stt.cloud_interval_sec || 12);
    if ($("setSttCloudFallback")) $("setSttCloudFallback").checked = s.stt.cloud_fallback !== false;
    if ($("orKeyTestStatus")) {
      $("orKeyTestStatus").innerHTML = 'Adicione uma ou mais chaves de <code>openrouter.ai/settings/keys</code>. Se uma atingir limite ou falhar, o Muesli alterna automaticamente para a próxima.';
    }
    const orSel = $("setSttOpenrouterModel");
    if (orSel) {
      const savedOrModel = s.stt.openrouter_model || "openai/whisper-large-v3-turbo";
      const hasOption = [...orSel.options].some(o => o.value === savedOrModel);
      if (hasOption) {
        orSel.value = savedOrModel;
        if ($("setSttOpenrouterCustomModel")) $("setSttOpenrouterCustomModel").value = "";
      } else {
        orSel.value = "__custom__";
        if ($("setSttOpenrouterCustomModel")) $("setSttOpenrouterCustomModel").value = savedOrModel;
      }
    }
    syncSttUi();
    // ai
    $("setLocalMinutes").checked = !!s.minutes?.enabled;
    $("setLocalMinutesAc").checked = !!s.minutes?.ac_only;
    $("setLocalMinutesVulkan").checked = !!s.minutes?.prefer_vulkan;
    $("setMinutesProfile").value = s.minutes?.profile || "executiva";
    const minutesModel = await syncMinutesModel();
    if (minutesModel.download_job_id && !minutesDownloadWatch) {
      watchMinutesDownload(minutesModel.download_job_id, minutesModel.size).catch(() => toast("Não foi possível acompanhar o download."));
    }
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
    renderKeyList("keysLlmOpenrouter", s.llm.openrouter_keys?.length ? s.llm.openrouter_keys : (s.llm.openrouter_key ? [s.llm.openrouter_key] : []), "sk-or-v1-…");
    if ($("setModelOpenrouter")) $("setModelOpenrouter").value = s.llm.openrouter_model || "openai/gpt-4o-mini";
    renderKeyList("keysLlmAnthropic", s.llm.anthropic_keys?.length ? s.llm.anthropic_keys : (s.llm.anthropic_key ? [s.llm.anthropic_key] : []), "sk-ant-…");
    renderKeyList("keysLlmOpenai", s.llm.openai_keys?.length ? s.llm.openai_keys : (s.llm.openai_key ? [s.llm.openai_key] : []), "sk-…");
    renderKeyList("keysLlmGroq", s.llm.groq_keys?.length ? s.llm.groq_keys : (s.llm.groq_key ? [s.llm.groq_key] : []), "gsk_…");
    $("setModelAnthropic").value = s.llm.anthropic_model;
    $("setModelOpenai").value = s.llm.openai_model;
    $("setModelGroq").value = s.llm.groq_model;
    // general
    if ($("setTheme")) $("setTheme").value = (s.ui && s.ui.theme) || "light";
    $("setStartup").checked = !!s.launch_at_startup;
    if ($("setDisableUiGpu")) $("setDisableUiGpu").checked = !!(s.ui && s.ui.disable_gpu);
    $("setConsent").checked = !!s.consent.enabled;
    $("setConsentText").value = s.consent.snippet;
    if ($("updateCurrentVer")) {
      const v = (state.settingsState && state.settingsState.version) || "1.6.2";
      $("updateCurrentVer").textContent = "Versão instalada: v" + v;
    }
    renderTplManager();
  } catch (err) {
    console.error("Erro ao carregar configurações:", err);
    toast("Erro ao carregar configurações: " + (err.message || err), 4000);
  }
}

async function saveSettings() {
  let orModel = $("setSttOpenrouterModel") ? $("setSttOpenrouterModel").value : "openai/whisper-large-v3-turbo";
  if (orModel === "__custom__") {
    orModel = ($("setSttOpenrouterCustomModel")?.value || "").trim() || "openai/whisper-large-v3-turbo";
  }
  const sttOrKeys = readKeyList("keysSttOpenrouter");
  const sttOpenaiKeys = readKeyList("keysSttOpenai");
  const sttGroqKeys = readKeyList("keysSttGroq");
  const llmOrKeys = readKeyList("keysLlmOpenrouter");
  const llmAnthKeys = readKeyList("keysLlmAnthropic");
  const llmOpenaiKeys = readKeyList("keysLlmOpenai");
  const llmGroqKeys = readKeyList("keysLlmGroq");

  const patch = {
    audio: {
      mic_device_index: $("setMic").value === "" ? null : parseInt($("setMic").value, 10),
      loopback_device_index: $("setLoop").value === "" ? null : parseInt($("setLoop").value, 10),
      retain_audio: $("setRetain").checked,
    },
    stt: {
      engine: $("setSttEngine").value,
      model: $("setSttModel").value,
      device: $("setSttDevice") ? $("setSttDevice").value : "cpu",
      language: $("setSttLang").value,
      openrouter_key: sttOrKeys[0] || "",
      openrouter_keys: sttOrKeys,
      openrouter_model: orModel,
      openai_key: sttOpenaiKeys[0] || "",
      openai_keys: sttOpenaiKeys,
      openai_model: $("setSttOpenaiModel") ? $("setSttOpenaiModel").value : "whisper-1",
      groq_key: sttGroqKeys[0] || "",
      groq_keys: sttGroqKeys,
      groq_model: $("setSttGroqModel") ? $("setSttGroqModel").value : "whisper-large-v3-turbo",
      cloud_interval_sec: $("setSttCloudInterval") ? parseInt($("setSttCloudInterval").value, 10) || 12 : 12,
      cloud_fallback: $("setSttCloudFallback") ? $("setSttCloudFallback").checked : true,
    },
    minutes: {
      enabled: $("setLocalMinutes").checked,
      ac_only: $("setLocalMinutesAc").checked,
      prefer_vulkan: $("setLocalMinutesVulkan").checked,
      profile: $("setMinutesProfile").value,
    },
    llm: {
      provider: $("setLlmProvider").value,
      ollama_url: $("setOllamaUrl").value.trim() || "http://localhost:11434",
      ollama_model: $("setOllamaModel").value,
      openrouter_key: llmOrKeys[0] || "",
      openrouter_keys: llmOrKeys,
      openrouter_model: $("setModelOpenrouter") ? ($("setModelOpenrouter").value.trim() || "openai/gpt-4o-mini") : "openai/gpt-4o-mini",
      anthropic_key: llmAnthKeys[0] || "",
      anthropic_keys: llmAnthKeys,
      openai_key: llmOpenaiKeys[0] || "",
      openai_keys: llmOpenaiKeys,
      groq_key: llmGroqKeys[0] || "",
      groq_keys: llmGroqKeys,
      anthropic_model: $("setModelAnthropic").value.trim(),
      openai_model: $("setModelOpenai").value.trim(),
      groq_model: $("setModelGroq").value.trim(),
    },
    consent: {
      enabled: $("setConsent").checked,
      snippet: $("setConsentText").value,
    },
    ui: {
      disable_gpu: $("setDisableUiGpu") ? $("setDisableUiGpu").checked : false,
      theme: $("setTheme") ? $("setTheme").value : "light",
    },
    launch_at_startup: $("setStartup").checked,
  };
  await api().save_settings(patch);
  applyTheme(patch.ui.theme);
  $("settingsModal").classList.remove("visible");
  toast("Configurações salvas com sucesso!");
  refreshStatus();
  state.settings = await api().get_settings();
  syncMinutesControls(state.current);
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
      (t.builtin ? '<span class="builtin-tag">integrado</span>'
                 : '<button class="edit">Editar</button><button class="del">Excluir</button>');
    row.querySelector(".nm").textContent = t.name;
    row.querySelector(".ds").textContent = t.description || t.sections.map(s => s.title).join(", ");
    if (!t.builtin) {
      row.querySelector(".edit").addEventListener("click", () => openTplEditor(t));
      row.querySelector(".del").addEventListener("click", async () => {
        if (!confirm(`Deseja excluir o modelo "${t.name}"?`)) return;
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
  const secs = tpl ? tpl.sections : [{ title: "Resumo", hint: "" }, { title: "Plano de Ação", hint: "" }];
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
  if (!res.ok) return toast(res.error || "Não foi possível salvar o modelo");
  $("tplEditor").style.display = "none";
  await loadTemplates();
  renderTplManager();
  toast("Modelo de nota salvo com sucesso!");
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
    if (st.llm.provider) bits.push(`IA: <b>${escapeHtml(st.llm.provider)}</b> (${escapeHtml(st.llm.detail || "")})`);
    else bits.push("IA: sumarizador integrado (offline)");
    if (st.stt_model.state === "loading") bits.push("Whisper: carregando modelo…");
    else if (st.stt_model.state === "error") bits.push(`Whisper: ${escapeHtml(st.stt_model.detail || "erro no modelo")}`);
    else bits.push(`Whisper: ${escapeHtml(st.stt_model.detail || st.stt_model.name || "pronto")}`);
    if (!st.audio_backend_ok) bits.push("Aviso: dispositivo de áudio indisponível");
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
  $("btnNew").addEventListener("click", () => startNewNote(null));
  $("btnNewHome").addEventListener("click", () => startNewNote(null));
  wireMeetingMenu();
  $("btnStop").addEventListener("click", stopRecording);
  $("btnEnhance").addEventListener("click", enhanceNow);
  $("btnDownloadMinutes").addEventListener("click", downloadMinutesModel);
  $("enhancedBody").addEventListener("click", event => {
    const source = event.target.closest("button[data-segment]");
    if (!source) return;
    const segment = $(`transcript-seg-${source.dataset.segment}`);
    if (!segment) { toast("Trecho não encontrado na transcrição desta reunião."); return; }
    if ($("transcriptPane").classList.contains("collapsed")) $("btnCollapseTr").click();
    segment.scrollIntoView({block: "center"});
    segment.classList.add("source-highlight");
    setTimeout(() => segment.classList.remove("source-highlight"), 2500);
  });
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
  $("meetingCategory")?.addEventListener("change", async () => {
    if (!state.current) return;
    const value = $("meetingCategory").value;
    const result = await api().move_meeting(state.current.id, value || null);
    if (!result.ok) return toast(result.error || "Não foi possível alterar a categoria.");
    state.current.category_id = value ? Number(value) : null;
    await refreshMeetings();
  });
  $("btnAddCategory")?.addEventListener("click", async () => {
    const name = prompt("Nome da categoria:");
    if (name === null) return;
    const result = await api().create_category(name);
    if (!result.ok) return toast(result.error);
    await refreshMeetings();
  });
  $("tplSelect").addEventListener("change", () => {
    if (state.current) api().set_meeting_template(state.current.id, $("tplSelect").value);
  });
  $("btnDelete").addEventListener("click", () => deleteSavedMeeting(state.current?.id));
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
  const copyCopilotAction = () => copyCopilotPrompt(state.current?.id);
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
  const closeSettingsModal = () => {
    $("settingsModal").classList.remove("visible");
    if (state.settings && state.settings.ui) applyTheme(state.settings.ui.theme || "light");
  };
  $("btnSettings").addEventListener("click", openSettings);
  $("btnCloseSettings").addEventListener("click", closeSettingsModal);
  $("btnCancelSettings").addEventListener("click", closeSettingsModal);
  $("setTheme")?.addEventListener("change", () => applyTheme($("setTheme").value));
  $("btnThemeQuick")?.addEventListener("click", async () => {
    const curEff = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    const next = curEff === "dark" ? "light" : "dark";
    applyTheme(next);
    if (isApiReady()) {
      try {
        await api().save_settings({ ui: { theme: next } });
        if (state.settings && state.settings.ui) state.settings.ui.theme = next;
      } catch (e) {}
    }
    toast(next === "dark" ? "Tema escuro ativado" : "Tema claro ativado", 1800);
  });
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnRefreshDevices").addEventListener("click", openSettings);
  $("setSttEngine")?.addEventListener("change", syncSttUi);
  $("setSttOpenrouterModel")?.addEventListener("change", syncSttUi);
  $("btnAddSttOrKey")?.addEventListener("click", () => addKeyRow("keysSttOpenrouter", "sk-or-v1-…"));
  $("btnAddSttOpenaiKey")?.addEventListener("click", () => addKeyRow("keysSttOpenai", "sk-…"));
  $("btnAddSttGroqKey")?.addEventListener("click", () => addKeyRow("keysSttGroq", "gsk_…"));
  $("btnAddLlmOrKey")?.addEventListener("click", () => addKeyRow("keysLlmOpenrouter", "sk-or-v1-…"));
  $("btnAddLlmAnthropicKey")?.addEventListener("click", () => addKeyRow("keysLlmAnthropic", "sk-ant-…"));
  $("btnAddLlmOpenaiKey")?.addEventListener("click", () => addKeyRow("keysLlmOpenai", "sk-…"));
  $("btnAddLlmGroqKey")?.addEventListener("click", () => addKeyRow("keysLlmGroq", "gsk_…"));
  $("btnTestOrKey")?.addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    const st = $("orKeyTestStatus");
    const rawKeys = readKeyList("keysSttOpenrouter");
    if (st) st.innerHTML = 'Validando chave(s) no OpenRouter... <span class="spin"></span>';
    try {
      const res = await api().test_openrouter_key(rawKeys);
      if (res.results && res.results.length) {
        const parts = res.results.map(r => {
          if (r.ok) {
            const bal = (r.limit_remaining !== null && r.limit_remaining !== undefined)
              ? `$${Number(r.limit_remaining).toFixed(4)} rest.`
              : `uso $${Number(r.usage || 0).toFixed(4)}`;
            return `<span style="color:var(--ok);">#${r.index} ✅ (${escapeHtml(bal)})</span>`;
          }
          return `<span style="color:var(--danger);">#${r.index} ❌ (${escapeHtml(r.error || "inválida")})</span>`;
        });
        if (st) st.innerHTML = `<strong>Resultado:</strong> ${parts.join(" &nbsp;·&nbsp; ")}`;
        toast(res.ok ? "Chave(s) OpenRouter validadas!" : ("OpenRouter: " + (res.error || "falha")), 4000);
      } else {
        const errMsg = res.error || "Falha ao validar chave.";
        if (st) st.innerHTML = `<span style="color:var(--danger); font-weight:600;">❌ ${escapeHtml(errMsg)}</span>`;
        toast("OpenRouter: " + errMsg, 4000);
      }
    } catch (e) {
      if (st) st.innerHTML = `<span style="color:var(--danger);">❌ Erro: ${escapeHtml(e.message || String(e))}</span>`;
    }
  });
  $("btnRefreshOrModels")?.addEventListener("click", async () => {
    if (!isApiReady()) { toast("Aguarde a conexão com o backend...", 3000); return; }
    const sel = $("setSttOpenrouterModel");
    if (!sel) return;
    const prev = sel.value === "__custom__" ? ($("setSttOpenrouterCustomModel")?.value || "").trim() : sel.value;
    toast("Buscando modelos de transcrição do OpenRouter...");
    try {
      const res = await api().list_openrouter_stt_models();
      if (!res.ok) {
        toast("Erro ao buscar modelos: " + (res.error || ""), 4000);
        return;
      }
      sel.innerHTML = "";
      for (const m of res.models) {
        const o = document.createElement("option");
        o.value = m.id;
        o.textContent = `${m.id} (${m.name})`;
        sel.appendChild(o);
      }
      const cust = document.createElement("option");
      cust.value = "__custom__";
      cust.textContent = "Outro modelo do OpenRouter (digitar ID manualmente)…";
      sel.appendChild(cust);
      if (prev && [...sel.options].some(o => o.value === prev)) {
        sel.value = prev;
      } else if (prev) {
        sel.value = "__custom__";
        if ($("setSttOpenrouterCustomModel")) $("setSttOpenrouterCustomModel").value = prev;
      }
      syncSttUi();
      toast(`${res.models.length} modelos STT carregados do OpenRouter!`);
    } catch (e) {
      toast("Erro ao atualizar modelos: " + (e.message || e), 4000);
    }
  });
  document.addEventListener("visibilitychange", () => {
    document.body.classList.toggle("page-hidden", document.hidden);
  });
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
  $("btnCheckUpdatesNow")?.addEventListener("click", () => checkUpdatesUi(true));
  $("btnDownloadUpdate")?.addEventListener("click", startDownloadUpdate);
  $("btnUcOpen")?.addEventListener("click", async () => {
    await openSettings();
    document.querySelectorAll(".modal-tabs .tab").forEach(x =>
      x.classList.toggle("active", x.dataset.set === "updates"));
    document.querySelectorAll(".set-section").forEach(s =>
      s.classList.toggle("visible", s.dataset.set === "updates"));
    if ($("updateCornerBanner")) $("updateCornerBanner").style.display = "none";
  });
  $("btnUcDismiss")?.addEventListener("click", () => {
    if ($("updateCornerBanner")) $("updateCornerBanner").style.display = "none";
  });
  $("btnOpenUpdateGithub")?.addEventListener("click", () => {
    if (updateData && updateData.html_url) {
      api().open_external_url(updateData.html_url);
    } else {
      api().open_external_url("https://github.com/NatanMendes0/muesli-windows/releases");
    }
  });

  const handleExit = () => {
    if (confirm("Deseja realmente encerrar o Muesli completamente?")) {
      try {
        api().exit_app();
      } catch (e) {
        window.close();
      }
    }
  };
  $("btnQuitApp")?.addEventListener("click", handleExit);
  $("btnExitAppModal")?.addEventListener("click", handleExit);
}

/* ----------------------------------------------------------- updater */
let updateData = null;

async function checkUpdatesUi(interactive = true) {
  const statusEl = $("updateStatusText");
  const actionArea = $("updateActionArea");
  const corner = $("updateCornerBanner");
  if (!isApiReady()) {
    if (interactive && statusEl) statusEl.textContent = "Aguardando inicialização do backend...";
    return;
  }
  if (interactive && statusEl) {
    statusEl.innerHTML = 'Verificando atualizações no GitHub... <span class="spin"></span>';
  }
  if (interactive && actionArea) actionArea.style.display = "none";

  try {
    const res = await api().check_updates();
    if (!res.ok) {
      if (interactive && statusEl) {
        statusEl.textContent = "Não foi possível verificar atualizações: " + (res.error || "erro de rede");
      }
      return;
    }
    updateData = res;
    if ($("updateCurrentVer")) {
      $("updateCurrentVer").textContent = "Versão instalada: v" + res.current_version;
    }

    if (res.has_update) {
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--ok); font-weight:700;">🎉 Nova versão disponível: ${escapeHtml(res.latest_version)}!</span>`;
      $("updateTitle").textContent = res.release_name || res.latest_version;
      $("updateNotes").textContent = res.release_notes || "Novas melhorias e correções de estabilidade.";
      actionArea.style.display = "block";
      $("updateDownloadProgress").style.display = "none";
      $("btnDownloadUpdate").disabled = false;
      if (corner) {
        if ($("ucVer")) $("ucVer").textContent = res.latest_version;
        if ($("ucSub")) $("ucSub").textContent = "Instalada: v" + res.current_version;
        corner.style.display = "flex";
      }
      if (interactive) toast(`Nova versão ${res.latest_version} disponível!`, 4000);
    } else {
      if (corner) corner.style.display = "none";
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--ok); font-weight:600;">✅ Você já está utilizando a versão mais recente (v${escapeHtml(res.current_version)}).</span>`;
      if (actionArea) actionArea.style.display = "none";
      if (interactive) toast("O Muesli já está na versão mais recente!");
    }
  } catch (e) {
    if (interactive && statusEl) {
      statusEl.textContent = "Erro ao checar atualizações: " + (e.message || e);
    }
  }
}

async function startDownloadUpdate() {
  if (!updateData || !updateData.zip_url) return;
  const btn = $("btnDownloadUpdate");
  const progEl = $("updateDownloadProgress");
  const pctEl = $("updatePct");
  btn.disabled = true;
  progEl.style.display = "block";
  toast("Iniciando download da atualização...");

  try {
    const res = await api().download_update(updateData.zip_url, updateData.zip_name);
    if (!res.ok) {
      toast("Falha ao iniciar download: " + (res.error || ""));
      btn.disabled = false;
      progEl.style.display = "none";
      return;
    }
    const jobId = res.job_id;
    const pollInterval = setInterval(async () => {
      try {
        const j = await api().job_status(jobId);
        if (j.progress !== undefined) {
          pctEl.textContent = `(${j.progress}%)`;
        }
        if (j.state === "done") {
          clearInterval(pollInterval);
          btn.disabled = false;
          progEl.style.display = "none";
          toast("Download concluído! A pasta Downloads foi aberta.", 6000);
          $("updateStatusText").innerHTML = `<span style="color:var(--ok); font-weight:700;">✅ Arquivo salvo em Downloads\\${escapeHtml(j.result?.filename || "")}. Extraia a pasta inteira e abra o executável da nova versão.</span>`;
        } else if (j.state === "error") {
          clearInterval(pollInterval);
          btn.disabled = false;
          progEl.style.display = "none";
          toast("Erro no download: " + (j.error || ""));
        }
      } catch (err) {
        clearInterval(pollInterval);
        btn.disabled = false;
        progEl.style.display = "none";
      }
    }, 600);
  } catch (e) {
    btn.disabled = false;
    progEl.style.display = "none";
    toast("Erro ao baixar: " + (e.message || e));
  }
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
    if (el) el.innerHTML = `<span style="color:var(--danger);font-weight:600">Backend não respondeu (timeout 1 min)</span>`;
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
    applyTheme((state.settings.ui && state.settings.ui.theme) || "light");
    await refreshMeetings();
    try { renderChat($("homeChatPanel"), await api().get_global_chat()); } catch (e) {}
    await refreshStatus();
    setInterval(refreshStatus, 5000);
    syncMinutesControls(state.current);
    await refreshMinutesQueue();
    setInterval(refreshMinutesQueue, 1500);
    toast("Muesli pronto para uso!", 2000);
    setTimeout(() => checkUpdatesUi(false), 1200);
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
