"""Note enhancement: merge rough notes with the transcript into structured markdown.

Provider order when set to auto: Anthropic, OpenAI, Groq (cloud keys win if present),
then local Ollama, then a built-in extractive summarizer that needs nothing installed.
"""
import json
import re
import time
from collections import Counter

import requests

from . import keypool, settings

REQUEST_TIMEOUT = 240
OLLAMA_TIMEOUT = 900          # local models on CPU can be slow; do not give up on them
CLOUD_TRANSCRIPT_CHARS = 26000
LOCAL_TRANSCRIPT_CHARS = 11000

STOPWORDS = set("""a an the and or but if then else for while of to in on at by with from as is are was
were be been being am do does did doing have has had having i you he she it we they me him her us them
my your his its our their this that these those there here what which who whom when where why how not
no nor so too very can will just should would could may might must shall about into over under again
further once more most other some such only own same than up down out off all any both each few because
until against during before after above below between through don didn t s ll re ve d m won isn aren
yeah yes ok okay right well like know think going gonna get got let lets um uh hmm mm really actually
say said one two also kind sort thing things stuff want wanted make sure bit lot little maybe""".split())


# ---------------------------------------------------------------- LLM router

def _strip_think(text: str) -> str:
    """Remove <think> blocks emitted by reasoning models such as qwen3."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "<think>" in text:  # unterminated block (truncated response)
        text = text.split("<think>")[0]
    return text.strip()


def ollama_models(url: str) -> list:
    try:
        r = requests.get(url.rstrip("/") + "/api/tags", timeout=3)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def _call_ollama(system: str, user: str, cfg: dict) -> str:
    url = cfg.get("ollama_url") or "http://localhost:11434"
    model = cfg.get("ollama_model") or ""
    if not model:
        models = ollama_models(url)
        if not models:
            raise RuntimeError("Ollama is not running or has no models")
        model = models[0]
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_ctx": 16384},
    }
    r = requests.post(url.rstrip("/") + "/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
    if r.status_code == 400:
        payload.pop("think", None)
        r = requests.post(url.rstrip("/") + "/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
    r.raise_for_status()
    out = _strip_think(r.json().get("message", {}).get("content", ""))
    if not out:
        raise RuntimeError("Ollama returned an empty response")
    return out


def _post_with_ssl_fallback(url: str, **kwargs):
    try:
        return requests.post(url, **kwargs)
    except requests.exceptions.SSLError:
        kwargs["verify"] = False
        return requests.post(url, **kwargs)


def _call_openai_style(system: str, user: str, url: str, key: str, model: str, extra_headers=None) -> str:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    r = _post_with_ssl_fallback(
        url, timeout=REQUEST_TIMEOUT,
        headers=headers,
        json={"model": model, "temperature": 0.3,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _call_anthropic(system: str, user: str, key: str, model: str) -> str:
    r = _post_with_ssl_fallback(
        "https://api.anthropic.com/v1/messages", timeout=REQUEST_TIMEOUT,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json={"model": model, "max_tokens": 4000, "system": system,
              "messages": [{"role": "user", "content": user}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()


_avail_cache = {"at": 0.0, "value": None, "cfg": ""}

def llm_available() -> dict:
    """What is usable right now, for UI display. Cached briefly: the UI polls
    this every few seconds and the Ollama probe is a blocking HTTP call."""
    cfg = settings.load()["llm"]
    cfg_key = json.dumps(cfg, sort_keys=True)
    now = time.time()
    if (_avail_cache["value"] is not None and _avail_cache["cfg"] == cfg_key
            and now - _avail_cache["at"] < 10):
        return dict(_avail_cache["value"])
    value = _llm_available_uncached(cfg)
    _avail_cache.update(at=now, value=dict(value), cfg=cfg_key)
    return value


def _call_with_rotation(pool_name: str, keys: list, fn):
    ordered = keypool.rotator.ordered_keys(pool_name, keys)
    last_exc = None
    for key in ordered:
        try:
            res = fn(key)
            keypool.rotator.mark_success(pool_name, key, keys)
            return res
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            code = resp.status_code if resp is not None else 0
            retry_after = 0.0
            if resp is not None:
                try:
                    retry_after = float((resp.headers.get("retry-after") or "").strip() or 0)
                except ValueError:
                    retry_after = 0.0
            keypool.rotator.mark_failure(pool_name, key, code, retry_after)
            last_exc = e
        except requests.RequestException as e:
            keypool.rotator.mark_failure(pool_name, key, 0)
            last_exc = e
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Nenhuma chave configurada para {pool_name}")


def _llm_available_uncached(cfg: dict) -> dict:
    provider = cfg.get("provider", "auto")
    out = {"provider": "", "detail": ""}
    order = _provider_order(cfg, provider)
    for name in order:
        if name == "anthropic" and keypool.collect_keys(cfg, "anthropic_key", "anthropic_keys"):
            out.update(provider="anthropic", detail=cfg.get("anthropic_model", ""))
            return out
        if name == "openrouter":
            keys = keypool.collect_keys(cfg, "openrouter_key", "openrouter_keys")
            if not keys and provider == "openrouter":
                keys = keypool.collect_keys(settings.load()["stt"], "openrouter_key", "openrouter_keys")
            if keys:
                out.update(provider="openrouter", detail=cfg.get("openrouter_model") or "openai/gpt-4o-mini")
                return out
        if name == "openai" and (keypool.collect_keys(cfg, "openai_key", "openai_keys") or cfg.get("openai_url")):
            out.update(provider="openai", detail=cfg.get("openai_model", ""))
            return out
        if name == "groq" and keypool.collect_keys(cfg, "groq_key", "groq_keys"):
            out.update(provider="groq", detail=cfg.get("groq_model", ""))
            return out
        if name == "ollama":
            models = ollama_models(cfg.get("ollama_url") or "http://localhost:11434")
            if models:
                out.update(provider="ollama",
                           detail=cfg.get("ollama_model") or models[0])
                return out
    return out


def _provider_order(cfg: dict, provider: str):
    if provider in ("anthropic", "openrouter", "openai", "groq", "ollama"):
        return [provider]
    if provider == "none":
        return []
    return ["anthropic", "openrouter", "openai", "groq", "ollama"]


def llm_complete(system: str, user: str):
    """Try providers in order. Returns (text, provider_name) or (None, '') if none work."""
    cfg = settings.load()["llm"]
    errors = []
    for name in _provider_order(cfg, cfg.get("provider", "auto")):
        try:
            if name == "anthropic":
                keys = keypool.collect_keys(cfg, "anthropic_key", "anthropic_keys")
                if keys:
                    model = cfg.get("anthropic_model") or "claude-haiku-4-5-20251001"
                    return _call_with_rotation("llm:anthropic", keys,
                                               lambda k: _call_anthropic(system, user, k, model)), "anthropic"
            if name == "openrouter":
                keys = keypool.collect_keys(cfg, "openrouter_key", "openrouter_keys")
                if not keys and cfg.get("provider") == "openrouter":
                    keys = keypool.collect_keys(settings.load()["stt"], "openrouter_key", "openrouter_keys")
                if keys:
                    model = cfg.get("openrouter_model") or "openai/gpt-4o-mini"
                    return _call_with_rotation(
                        "llm:openrouter", keys,
                        lambda k: _call_openai_style(
                            system, user,
                            "https://openrouter.ai/api/v1/chat/completions",
                            k, model,
                            extra_headers={
                                "HTTP-Referer": "https://github.com/NatanMendes0/muesli-windows",
                                "X-Title": "Muesli",
                            },
                        )), "openrouter"
            if name == "openai":
                keys = keypool.collect_keys(cfg, "openai_key", "openai_keys")
                if keys or cfg.get("openai_url"):
                    url = cfg.get("openai_url") or "https://api.openai.com/v1/chat/completions"
                    model = cfg.get("openai_model") or "gpt-4o-mini"
                    return _call_with_rotation(
                        "llm:openai", keys or ["dummy-key"],
                        lambda k: _call_openai_style(system, user, url, k, model)), "openai"
            if name == "groq":
                keys = keypool.collect_keys(cfg, "groq_key", "groq_keys")
                if keys:
                    model = cfg.get("groq_model") or "llama-3.3-70b-versatile"
                    return _call_with_rotation(
                        "llm:groq", keys,
                        lambda k: _call_openai_style(
                            system, user,
                            "https://api.groq.com/openai/v1/chat/completions",
                            k, model)), "groq"
            if name == "ollama":
                return _call_ollama(system, user, cfg), "ollama"
        except Exception as e:
            errors.append(f"{name}: {e}")
    return None, "; ".join(errors)


# ------------------------------------------------------------ transcript text

def format_transcript(segments: list) -> str:
    lines = []
    for s in segments:
        mm, ss = divmod(int(s["t0"]), 60)
        who = s.get("speaker") or ""
        prefix = f"[{mm:02d}:{ss:02d}]" + (f" {who}:" if who else "")
        lines.append(f"{prefix} {s['text']}")
    return "\n".join(lines)


def transcript_char_budget() -> int:
    """Local models get a smaller context so enhancement stays fast enough."""
    return (LOCAL_TRANSCRIPT_CHARS if llm_available()["provider"] in ("ollama", "")
            else CLOUD_TRANSCRIPT_CHARS)


def shrink_segments(segments: list, note_terms, max_chars: int) -> list:
    """Long meeting: keep the most important segments (scored, note-anchored)
    up to the char budget, in chronological order. Deterministic and fast."""
    total = sum(len(s["text"]) + 14 for s in segments)
    if total <= max_chars:
        return segments
    scores = _score_segments(segments, note_terms)
    order = sorted(range(len(segments)), key=lambda i: -scores[i])
    keep, used = set(), 0
    for i in order:
        cost = len(segments[i]["text"]) + 14
        if used + cost > max_chars:
            continue
        keep.add(i)
        used += cost
    return [segments[i] for i in sorted(keep)]


# ------------------------------------------------------------- LLM enhancement

ENHANCE_SYSTEM = """Você é um especialista em atas e anotações executivas de reuniões.
Você recebe as anotações rápidas do usuário mais a transcrição em tempo real da reunião.
Produza anotações de reunião limpas, objetivas e estruturadas em Markdown, em português brasileiro.

Regras:
- As notas rápidas do usuário são âncoras essenciais: tudo o que foi anotado deve constar no documento, expandido com os detalhes da transcrição. Se algo anotado não tiver correspondência clara na transcrição, mantenha como foi escrito.
- Extraia os tópicos mais importantes, o contexto técnico e as discussões relevantes.
- Registre decisões e deliberações com clareza.
- Ações e tarefas devem especificar responsáveis e prazos/momentos sempre que mencionados (use caixas de seleção `- [ ]`).
- Nunca invente fatos, nomes, prazos ou números que não foram ditos na reunião.
- Seja específico: cite nomes de pessoas, sistemas, ferramentas e prazos. Prefira tópicos (bullet points) objetivos em vez de longos blocos de texto.
- Use exatamente os títulos de seção fornecidos como H2 (##) em Markdown. Omita uma seção apenas se realmente não houver nada referente a ela.
- Não use travessões longos nem prefácios. Responda apenas com o Markdown gerado."""


def enhance(meeting: dict, segments: list, template: dict):
    """Returns (markdown, provider). Falls back to extractive when no LLM is available."""
    notes = (meeting.get("notes_md") or "").strip()
    if not segments and not notes:
        return "*Nothing to enhance yet: no notes and no transcript.*", "none"

    note_terms = set()
    for line in notes.splitlines():
        note_terms.update(_words(line))
    transcript = format_transcript(
        shrink_segments(segments, note_terms, transcript_char_budget()))

    sections_desc = "\n".join(
        f"- {s['title']}: {s.get('hint', '')}" for s in template["sections"])

    user = (f"Meeting title: {meeting.get('title', 'Untitled')}\n"
            f"Template: {template['name']}\nSections to produce:\n{sections_desc}\n\n"
            f"My rough notes during the meeting:\n{notes or '(none taken)'}\n\n"
            f"Full transcript:\n{transcript or '(no transcript captured)'}")

    text, provider = llm_complete(ENHANCE_SYSTEM, user)
    if text:
        return text.strip(), provider
    return extractive_enhance(meeting, segments, template), "extractive"


# --------------------------------------------------------- extractive fallback

def _words(text: str):
    return [w for w in re.findall(r"[a-zA-Z][a-zA-Z'-]+", text.lower())
            if w not in STOPWORDS and len(w) > 2]


def _score_segments(segments: list, boost_terms=None):
    freq = Counter()
    seg_words = []
    for s in segments:
        ws = _words(s["text"])
        seg_words.append(ws)
        freq.update(set(ws))
    boost = set(boost_terms or [])
    scores = []
    for s, ws in zip(segments, seg_words):
        if not ws:
            scores.append(0.0)
            continue
        base = sum(freq[w] for w in ws) / len(ws)
        bonus = 2.5 * sum(1 for w in ws if w in boost)
        length_ok = 1.0 if 4 <= len(ws) <= 60 else 0.5
        scores.append((base + bonus) * length_ok)
    return scores


DECISION_CUES = re.compile(
    r"\b(decid\w+|agree\w*|we(?:'| wi)ll go with|final(?:ized|ised)?|finali[sz]e\w*|"
    r"approved|signed off|conclusion|settled on|let's go with|green.?light)\b", re.I)
ACTION_CUES = re.compile(
    r"\b(action item|to.?do|follow.?up|"
    r"(?:i|you|we|he|she|they)(?:'| wi)ll (?:send|share|write|set up|schedule|check|get|review)|"
    r"need(?:s)? to|by (?:monday|tuesday|wednesday|thursday|friday|tomorrow|next week|end of)|"
    r"take care of|assign\w*|deadline|due)\b", re.I)


def extractive_enhance(meeting: dict, segments: list, template: dict) -> str:
    """No-LLM summary: frequency scored key points, cue based decisions and actions,
    and the user's own note lines expanded with matching transcript quotes."""
    notes = (meeting.get("notes_md") or "").strip()
    note_lines = [l.strip(" -*#\t") for l in notes.splitlines() if l.strip(" -*#\t")]
    note_terms = set()
    for l in note_lines:
        note_terms.update(_words(l))

    out = [f"*Generated without an AI model (extractive mode). "
           f"Install Ollama or add an API key in Settings for smarter notes.*", ""]

    scores = _score_segments(segments, note_terms)
    ranked = sorted(range(len(segments)), key=lambda i: -scores[i])

    for section in template["sections"]:
        title = section["title"]
        low = title.lower()
        body = []
        if "decision" in low:
            body = [s["text"] for s in segments if DECISION_CUES.search(s["text"])][:8]
        elif "action" in low or "next step" in low or "follow" in low:
            body = [s["text"] for s in segments if ACTION_CUES.search(s["text"])][:10]
        elif "summary" in low or "key point" in low or low in ("updates", "topics discussed"):
            top = sorted(ranked[:8])
            body = [segments[i]["text"] for i in top if scores[i] > 0]
        else:
            terms = set(_words(title + " " + section.get("hint", "")))
            matches = [(i, s) for i, s in enumerate(segments)
                       if terms & set(_words(s["text"]))]
            body = [s["text"] for _, s in matches[:6]]
        if body:
            out.append(f"## {title}")
            out.extend(f"- {t}" for t in body)
            out.append("")

    if note_lines and segments:
        out.append("## Your notes, expanded")
        for line in note_lines[:20]:
            terms = set(_words(line))
            out.append(f"- **{line}**")
            if terms:
                matches = [s for s in segments if len(terms & set(_words(s["text"]))) >= 1]
                best = sorted(matches, key=lambda s: -len(terms & set(_words(s["text"]))))[:3]
                for s in best:
                    mm, ss = divmod(int(s["t0"]), 60)
                    out.append(f"  - [{mm:02d}:{ss:02d}] \"{s['text']}\"")
        out.append("")

    if len(out) <= 2:
        out.append("*Not enough content captured to summarize.*")
    return "\n".join(out).strip()


# ------------------------------------------------------------------ title poke

def suggest_title(segments: list) -> str:
    """Cheap title from the first minute of conversation, no LLM required."""
    text = " ".join(s["text"] for s in segments[:6])
    ws = _words(text)
    common = [w for w, _ in Counter(ws).most_common(4)]
    if not common:
        return ""
    return " ".join(w.capitalize() for w in common[:3])
