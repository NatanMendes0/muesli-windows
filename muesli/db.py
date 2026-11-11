"""SQLite storage. One connection per thread via thread local."""
import json
import os
import sqlite3
import threading
import time

from . import settings

DB_PATH = os.path.join(settings.data_dir(), "muesli.db")

_local = threading.local()
_fts_available = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'Untitled meeting',
    title_manual INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    ended_at REAL,
    duration_sec REAL DEFAULT 0,
    template_id TEXT DEFAULT 'auto',
    notes_md TEXT DEFAULT '',
    summary_md TEXT DEFAULT '',
    summary_provider TEXT DEFAULT '',
    attendees TEXT DEFAULT '',
    audio_path TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL,
    t0 REAL NOT NULL,
    t1 REAL NOT NULL,
    speaker TEXT DEFAULT '',
    text TEXT NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON segments(meeting_id);
CREATE TABLE IF NOT EXISTS custom_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    sections_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, content='segments', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c

def init():
    global _fts_available
    c = conn()
    c.executescript(SCHEMA)
    cols = {row[1] for row in c.execute("PRAGMA table_info(meetings)").fetchall()}
    if "title_manual" not in cols:
        c.execute("ALTER TABLE meetings ADD COLUMN title_manual INTEGER DEFAULT 0")
        c.execute("UPDATE meetings SET title_manual=1 WHERE title != 'Untitled meeting' AND title != ''")
    try:
        c.executescript(FTS_SCHEMA)
        _fts_available = True
    except sqlite3.OperationalError:
        _fts_available = False
    c.commit()

def fts_available() -> bool:
    global _fts_available
    if _fts_available is None:
        init()
    return bool(_fts_available)

# Meetings

def create_meeting(title: str, template_id: str) -> int:
    c = conn()
    clean = (title or "").strip() or "Untitled meeting"
    manual = 1 if clean != "Untitled meeting" else 0
    cur = c.execute(
        "INSERT INTO meetings (title, title_manual, created_at, template_id) VALUES (?, ?, ?, ?)",
        (clean, manual, time.time(), template_id or "auto"))
    c.commit()
    return cur.lastrowid

def end_meeting(meeting_id: int, duration_sec: float, audio_path: str = ""):
    c = conn()
    c.execute("UPDATE meetings SET ended_at=?, duration_sec=?, audio_path=? WHERE id=?",
              (time.time(), duration_sec, audio_path or "", meeting_id))
    c.commit()

def update_meeting(meeting_id: int, **fields):
    allowed = {"title", "title_manual", "notes_md", "summary_md", "summary_provider", "template_id", "attendees"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(meeting_id)
    c = conn()
    c.execute(f"UPDATE meetings SET {', '.join(sets)} WHERE id=?", vals)
    c.commit()

def get_meeting(meeting_id: int):
    row = conn().execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    return dict(row) if row else None

def delete_meeting(meeting_id: int):
    c = conn()
    c.execute("DELETE FROM segments WHERE meeting_id=?", (meeting_id,))
    c.execute("DELETE FROM chat_messages WHERE meeting_id=?", (meeting_id,))
    c.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
    c.commit()

def _like(term: str) -> str:
    """Escape LIKE wildcards; queries using this must add ESCAPE '\\'."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_meetings(search: str = "", limit: int = 500):
    c = conn()
    if search.strip():
        ids = set()
        if fts_available():
            q = fts_query(search)
            if q:
                try:
                    for row in c.execute(
                            "SELECT DISTINCT meeting_id FROM segments "
                            "WHERE id IN (SELECT rowid FROM segments_fts WHERE segments_fts MATCH ?)",
                            (q,)):
                        ids.add(row[0])
                except sqlite3.OperationalError:
                    pass
        like = _like(search.strip())
        rows = c.execute(
            "SELECT id, title, created_at, ended_at, duration_sec, template_id, "
            "substr(summary_md, 1, 200) AS preview FROM meetings "
            "WHERE title LIKE ? ESCAPE '\\' OR notes_md LIKE ? ESCAPE '\\' "
            "OR summary_md LIKE ? ESCAPE '\\' OR id IN ({}) "
            "ORDER BY created_at DESC LIMIT ?".format(
                ",".join(str(i) for i in ids) if ids else "-1"),
            (like, like, like, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT id, title, created_at, ended_at, duration_sec, template_id, "
            "substr(summary_md, 1, 200) AS preview FROM meetings "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

# Segments

def add_segment(meeting_id: int, t0: float, t1: float, speaker: str, text: str) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO segments (meeting_id, t0, t1, speaker, text) VALUES (?, ?, ?, ?, ?)",
        (meeting_id, t0, t1, speaker, text))
    c.commit()
    return cur.lastrowid

def get_segments(meeting_id: int):
    rows = conn().execute(
        "SELECT id, t0, t1, speaker, text FROM segments WHERE meeting_id=? ORDER BY t0",
        (meeting_id,)).fetchall()
    return [dict(r) for r in rows]

def fts_query(text: str) -> str:
    """Build a safe OR query of quoted words for FTS5."""
    words = [w.strip('"\'') for w in text.split()]
    words = [w for w in words if len(w) >= 2]
    if not words:
        return ""
    return " OR ".join('"{}"'.format(w.replace('"', '')) for w in words[:12])

def search_segments(query: str, meeting_id=None, limit: int = 24):
    """Full text search over transcript segments. Returns segments with meeting info."""
    c = conn()
    rows = []
    if fts_available():
        q = fts_query(query)
        if q:
            sql = ("SELECT s.id, s.meeting_id, s.t0, s.t1, s.speaker, s.text, "
                   "m.title, m.created_at FROM segments s "
                   "JOIN meetings m ON m.id = s.meeting_id "
                   "WHERE s.id IN (SELECT rowid FROM segments_fts WHERE segments_fts MATCH ?)")
            args = [q]
            if meeting_id:
                sql += " AND s.meeting_id=?"
                args.append(meeting_id)
            sql += " LIMIT ?"
            args.append(limit)
            try:
                rows = c.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                rows = []
    if not rows:
        words = [w for w in query.split() if len(w) >= 3][:6]
        if words:
            clauses = " OR ".join(["s.text LIKE ? ESCAPE '\\'"] * len(words))
            sql = ("SELECT s.id, s.meeting_id, s.t0, s.t1, s.speaker, s.text, "
                   "m.title, m.created_at FROM segments s "
                   "JOIN meetings m ON m.id = s.meeting_id WHERE (" + clauses + ")")
            args = [_like(w) for w in words]
            if meeting_id:
                sql += " AND s.meeting_id=?"
                args.append(meeting_id)
            sql += " LIMIT ?"
            args.append(limit)
            rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]

# Custom templates

def list_custom_templates():
    rows = conn().execute("SELECT * FROM custom_templates ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sections"] = json.loads(d.pop("sections_json"))
        d["builtin"] = False
        out.append(d)
    return out

def save_custom_template(tpl_id: str, name: str, description: str, sections: list):
    c = conn()
    c.execute(
        "INSERT INTO custom_templates (id, name, description, sections_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, "
        "sections_json=excluded.sections_json",
        (tpl_id, name, description, json.dumps(sections)))
    c.commit()

def delete_custom_template(tpl_id: str):
    c = conn()
    c.execute("DELETE FROM custom_templates WHERE id=?", (tpl_id,))
    c.commit()

# Chat

def add_chat_message(meeting_id, role: str, content: str):
    c = conn()
    c.execute("INSERT INTO chat_messages (meeting_id, role, content, created_at) VALUES (?, ?, ?, ?)",
              (meeting_id, role, content, time.time()))
    c.commit()

def get_chat_messages(meeting_id, limit: int = 50):
    if meeting_id:
        rows = conn().execute(
            "SELECT role, content, created_at FROM chat_messages WHERE meeting_id=? "
            "ORDER BY id DESC LIMIT ?", (meeting_id, limit)).fetchall()
    else:
        rows = conn().execute(
            "SELECT role, content, created_at FROM chat_messages WHERE meeting_id IS NULL "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]
