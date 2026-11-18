"""SQLite storage. One connection per thread via thread local."""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

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
    audio_path TEXT DEFAULT '',
    category_id INTEGER
);
CREATE TABLE IF NOT EXISTS meeting_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
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
CREATE INDEX IF NOT EXISTS idx_segments_timeline ON segments(meeting_id, t0, id);
CREATE TABLE IF NOT EXISTS minutes_jobs (
    id TEXT PRIMARY KEY,
    meeting_id INTEGER NOT NULL UNIQUE REFERENCES meetings(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    strategy TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    original_digest TEXT NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    backend TEXT NOT NULL DEFAULT '',
    elapsed_sec REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_minutes_queue ON minutes_jobs(state, created_at);
CREATE TABLE IF NOT EXISTS minutes_blocks (
    job_id TEXT NOT NULL REFERENCES minutes_jobs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    PRIMARY KEY(job_id, ordinal)
);
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
    if "category_id" not in cols:
        c.execute("ALTER TABLE meetings ADD COLUMN category_id INTEGER")
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

def create_meeting(title: str, template_id: str, category_id=None) -> int:
    c = conn()
    clean = (title or "").strip() or "Untitled meeting"
    manual = 1 if clean != "Untitled meeting" else 0
    if category_id is not None and not c.execute(
            "SELECT 1 FROM meeting_categories WHERE id=?", (category_id,)).fetchone():
        category_id = None
    cur = c.execute(
        "INSERT INTO meetings (title, title_manual, created_at, template_id, category_id) VALUES (?, ?, ?, ?, ?)",
        (clean, manual, time.time(), template_id or "auto", category_id))
    c.commit()
    return cur.lastrowid

def end_meeting(meeting_id: int, duration_sec: float, audio_path: str = ""):
    c = conn()
    c.execute("UPDATE meetings SET ended_at=?, duration_sec=?, audio_path=? WHERE id=?",
              (time.time(), duration_sec, audio_path or "", meeting_id))
    c.commit()

def update_meeting(meeting_id: int, **fields):
    allowed = {"title", "title_manual", "notes_md", "summary_md", "summary_provider", "template_id", "attendees", "category_id"}
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

def write_local_minutes(meeting_id, markdown, original):
    """Compare and swap so an editor cannot silently replace a newer version."""
    with conn() as c:
        return c.execute("UPDATE meetings SET summary_md=? WHERE id=? AND summary_provider='local_minutes' "
                         "AND COALESCE(summary_md, '')=?", (markdown, meeting_id, original)).rowcount == 1


def get_meeting(meeting_id: int):
    row = conn().execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    return dict(row) if row else None


# Durable minutes queue. All changes are local to the meeting database.
def _digest(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def minutes_watermark(meeting_id):
    row = conn().execute('''SELECT notes_md,
        (SELECT COALESCE(MAX(id),0) FROM segments WHERE meeting_id=m.id),
        (SELECT COUNT(*) FROM segments WHERE meeting_id=m.id)
        FROM meetings m WHERE m.id=?''', (meeting_id,)).fetchone()
    return (_digest(row[0]), row[1], row[2]) if row else None


def iter_minutes_segments(meeting_id, upper_id, page_size=256):
    after = (-1, -1)
    while True:
        rows = conn().execute('''SELECT id,t0,t1,speaker,text FROM segments
            WHERE meeting_id=? AND id<=? AND (t0,id)>(?,?) ORDER BY t0,id LIMIT ?''',
                              (meeting_id, upper_id, *after, page_size)).fetchall()
        if not rows:
            return
        yield from map(dict, rows)
        after = rows[-1]['t0'], rows[-1]['id']


def enqueue_minutes(meeting_id, profile, strategy):
    c = conn()
    with c:
        c.execute('BEGIN IMMEDIATE')
        meeting = c.execute('SELECT ended_at,summary_md FROM meetings WHERE id=?', (meeting_id,)).fetchone()
        if not meeting or not meeting['ended_at']:
            return None
        job = get_minutes_job(meeting_id)
        if job and job['profile'] == profile and job['strategy'] == strategy and job['state'] not in ('done', 'conflict'):
            if job['state'] not in ('queued', 'running'):
                c.execute("UPDATE minutes_jobs SET state='queued',detail='',updated_at=? WHERE id=?",
                          (time.time(), job['id']))
            return get_minutes_job(meeting_id)
        now = time.time()
        c.execute('DELETE FROM minutes_jobs WHERE meeting_id=?', (meeting_id,))
        c.execute('''INSERT INTO minutes_jobs(id,meeting_id,profile,strategy,state,original_digest,created_at,updated_at)
                     VALUES(?,?,?,?,'queued',?,?,?)''',
                  ('minutes-' + uuid.uuid4().hex, meeting_id, profile, strategy, _digest(meeting['summary_md']), now, now))
        return get_minutes_job(meeting_id)


def get_minutes_job(meeting_id):
    row = conn().execute('SELECT * FROM minutes_jobs WHERE meeting_id=?', (meeting_id,)).fetchone()
    return dict(row) if row else None


def get_minutes_job_by_id(job_id):
    row = conn().execute('''SELECT j.*,m.title,
         (SELECT COUNT(*) FROM minutes_blocks b WHERE b.job_id=j.id AND b.state='done') AS completed,
         (SELECT COUNT(*) FROM minutes_blocks b WHERE b.job_id=j.id AND b.state='error') AS failed
         FROM minutes_jobs j JOIN meetings m ON m.id=j.meeting_id WHERE j.id=?''', (job_id,)).fetchone()
    return dict(row) if row else None


def list_minutes_jobs():
    rows = conn().execute('''SELECT j.id,j.meeting_id,j.state,j.profile,j.total,j.detail,m.title,
         (SELECT COUNT(*) FROM minutes_blocks b WHERE b.job_id=j.id AND b.state='done') AS completed,
         (SELECT COUNT(*) FROM minutes_blocks b WHERE b.job_id=j.id AND b.state='error') AS failed
         FROM minutes_jobs j JOIN meetings m ON m.id=j.meeting_id WHERE j.state!='done' ORDER BY j.created_at''')
    return [dict(row) for row in rows]


def next_minutes_job():
    row = conn().execute("SELECT * FROM minutes_jobs WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
    return dict(row) if row else None


def recover_minutes_jobs():
    with conn() as c:
        c.execute("UPDATE minutes_jobs SET state='queued',detail='' WHERE state='running'")


def set_minutes_state(job_id, state, detail='', expected=None):
    sql = 'UPDATE minutes_jobs SET state=?, detail=?, updated_at=? WHERE id=?'
    args = (state, detail, time.time(), job_id)
    if expected:
        sql += ' AND state=?'
        args += (expected,)
    with conn() as c:
        return c.execute(sql, args).rowcount == 1


def prepare_minutes(job_id, signature, total, watermark=None):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT signature FROM minutes_jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
        if not row:
            return False
        if row['signature'] != signature:
            c.execute('DELETE FROM minutes_blocks WHERE job_id=?', (job_id,))
        c.execute('UPDATE minutes_jobs SET signature=?,total=?,snapshot=? WHERE id=?',
                  (signature, total, json.dumps(watermark), job_id))
        return True


def save_minutes_block(job_id, ordinal, state, result, start, end):
    with conn() as c:
        return c.execute('''INSERT OR REPLACE INTO minutes_blocks(job_id,ordinal,state,result_json,start_sec,end_sec)
            SELECT id,?,?,?,?,? FROM minutes_jobs WHERE id=? AND state='running' ''',
                         (ordinal, state, json.dumps(result, ensure_ascii=False), start, end, job_id)).rowcount == 1


def load_minutes_blocks(job_id):
    rows = conn().execute('SELECT * FROM minutes_blocks WHERE job_id=? ORDER BY ordinal', (job_id,))
    return {row['ordinal']: {**dict(row), 'result': json.loads(row['result_json'] or 'null')} for row in rows}


def finish_minutes(job_id, render, backend, elapsed, detail, watermark, suggested_title=''):
    """Publish the title and draft together only if the source and draft still match."""
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        job = c.execute("SELECT * FROM minutes_jobs WHERE id=? AND state='running'", (job_id,)).fetchone()
        if not job:
            return False
        mid = job['meeting_id']
        meeting = get_meeting(mid)
        if not meeting or _digest(meeting['summary_md']) != job['original_digest'] or minutes_watermark(mid) != watermark:
            c.execute("UPDATE minutes_jobs SET state='conflict',detail=?,updated_at=? WHERE id=?",
                      ('A fonte ou a ata mudou. O rascunho existente foi preservado. Gere de novo para atualizar.', time.time(), job_id))
            return False
        if suggested_title and not meeting['title_manual']:
            meeting['title'] = suggested_title
        c.execute("UPDATE meetings SET title=?,summary_md=?,summary_provider='local_minutes' WHERE id=?",
                  (meeting['title'], render(meeting), mid))
        c.execute("UPDATE minutes_jobs SET state='done',backend=?,elapsed_sec=?,detail=?,updated_at=? WHERE id=?",
                  (backend, elapsed, detail, time.time(), job_id))
        return True

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
            "SELECT id, title, created_at, ended_at, duration_sec, template_id, category_id, "
            "substr(summary_md, 1, 200) AS preview FROM meetings "
            "WHERE title LIKE ? ESCAPE '\\' OR notes_md LIKE ? ESCAPE '\\' "
            "OR summary_md LIKE ? ESCAPE '\\' OR id IN ({}) "
            "ORDER BY created_at DESC LIMIT ?".format(
                ",".join(str(i) for i in ids) if ids else "-1"),
            (like, like, like, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT id, title, created_at, ended_at, duration_sec, template_id, category_id, "
            "substr(summary_md, 1, 200) AS preview FROM meetings "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

# Categories

def list_categories():
    rows = conn().execute("SELECT id, name FROM meeting_categories ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]

def create_category(name: str):
    clean = (name or "").strip()
    if not clean:
        return None
    c = conn()
    try:
        cur = c.execute("INSERT INTO meeting_categories (name) VALUES (?)", (clean,))
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None

def rename_category(category_id: int, name: str) -> bool:
    clean = (name or "").strip()
    if not clean:
        return False
    c = conn()
    try:
        cur = c.execute("UPDATE meeting_categories SET name=? WHERE id=?", (clean, category_id))
        c.commit()
        return cur.rowcount > 0
    except sqlite3.IntegrityError:
        return False

def delete_category(category_id: int):
    c = conn()
    c.execute("UPDATE meetings SET category_id=NULL WHERE category_id=?", (category_id,))
    c.execute("DELETE FROM meeting_categories WHERE id=?", (category_id,))
    c.commit()

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
