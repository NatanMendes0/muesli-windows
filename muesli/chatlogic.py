"""Chat with one meeting or across all meetings, grounded in transcripts and notes."""
import datetime

from . import db, enhance

CHAT_SYSTEM = """You are Muesli, an assistant that answers questions about the user's meetings.
You are given context retrieved from local meeting transcripts, notes and summaries.

Rules:
- Ground every claim in the provided context. If the context does not contain the answer,
  say so plainly instead of guessing.
- When useful, mention which meeting (title and date) something came from.
- If asked to draft something (follow-up email, summary, action list), produce it ready
  to send, using only facts from the context.
- Be concise. Use Markdown. Do not use em dashes."""


def _meeting_context(meeting_id: int, max_chars: int, question: str = "") -> str:
    m = db.get_meeting(meeting_id)
    if not m:
        return ""
    date = datetime.datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d %H:%M")
    parts = [f"# Meeting: {m['title']} ({date})"]
    if (m.get("notes_md") or "").strip():
        parts.append("## User's own notes\n" + m["notes_md"].strip())
    if (m.get("summary_md") or "").strip():
        parts.append("## Enhanced summary\n" + m["summary_md"].strip())
    head = sum(len(p) for p in parts)
    segs = enhance.shrink_segments(db.get_segments(meeting_id),
                                   set(enhance._words(question)),
                                   max(2000, max_chars - head))
    transcript = enhance.format_transcript(segs)
    if transcript:
        parts.append("## Transcript (most relevant parts)\n" + transcript)
    return "\n\n".join(parts)[:max_chars]


def _global_context(question: str, max_chars: int) -> str:
    hits = db.search_segments(question, meeting_id=None, limit=30)
    by_meeting = {}
    for h in hits:
        by_meeting.setdefault(h["meeting_id"], []).append(h)
    parts = []
    for mid, segs in list(by_meeting.items())[:8]:
        m = db.get_meeting(mid)
        if not m:
            continue
        date = datetime.datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d")
        parts.append(f"# From meeting: {m['title']} ({date})")
        if (m.get("summary_md") or "").strip():
            parts.append(m["summary_md"].strip()[:1200])
        for s in segs[:8]:
            mm, ss = divmod(int(s["t0"]), 60)
            parts.append(f"[{mm:02d}:{ss:02d}] {s.get('speaker') or ''} {s['text']}".strip())
    if not parts:
        recents = db.list_meetings(limit=6)
        for m in recents:
            date = datetime.datetime.fromtimestamp(m["created_at"]).strftime("%Y-%m-%d")
            parts.append(f"# Recent meeting: {m['title']} ({date})")
            full = db.get_meeting(m["id"])
            if full and (full.get("summary_md") or "").strip():
                parts.append(full["summary_md"][:1500])
    return "\n\n".join(parts)[:max_chars]


def answer(question: str, meeting_id=None) -> dict:
    """Returns {text, provider, grounded}. Degrades to excerpts when no LLM exists."""
    budget = enhance.transcript_char_budget()
    if meeting_id:
        context = _meeting_context(meeting_id, budget, question)
        scope = "this meeting"
    else:
        context = _global_context(question, budget)
        scope = "your meetings"
    if not context.strip():
        return {"text": "I could not find any meeting content to answer from yet.",
                "provider": "none", "grounded": False}

    user = (f"Context from {scope}:\n\n{context}\n\n"
            f"Question or request from the user:\n{question}")
    text, provider = enhance.llm_complete(CHAT_SYSTEM, user)
    if text:
        db.add_chat_message(meeting_id, "user", question)
        db.add_chat_message(meeting_id, "assistant", text)
        return {"text": text, "provider": provider, "grounded": True}

    # No LLM anywhere: return the most relevant excerpts instead.
    hits = db.search_segments(question, meeting_id=meeting_id, limit=10)
    if hits:
        lines = ["No AI model is available for chat (install Ollama or add an API key in "
                 "Settings). Here are the most relevant transcript excerpts instead:", ""]
        for h in hits:
            date = datetime.datetime.fromtimestamp(h["created_at"]).strftime("%Y-%m-%d")
            mm, ss = divmod(int(h["t0"]), 60)
            lines.append(f"- **{h['title']}** ({date}) [{mm:02d}:{ss:02d}] {h['text']}")
        text = "\n".join(lines)
    else:
        text = ("No AI model is available for chat, and no transcript text matched your "
                "question. Install Ollama or add an API key in Settings to enable chat.")
    db.add_chat_message(meeting_id, "user", question)
    db.add_chat_message(meeting_id, "assistant", text)
    return {"text": text, "provider": "excerpts", "grounded": False}
