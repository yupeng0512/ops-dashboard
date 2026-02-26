"""数据模型 + 数据库初始化"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

DB_PATH = Path("data/ops_events.db")


class EventCreate(BaseModel):
    project: str
    level: str = Field(pattern="^(critical|warning|info)$")
    category: str
    title: str
    detail: str = ""
    action_hint: str = ""
    dedup_key: str = ""


class EventUpdate(BaseModel):
    status: str = Field(pattern="^(open|acknowledged|resolved)$")


class EventResponse(BaseModel):
    id: int
    project: str
    level: str
    category: str
    title: str
    detail: str
    action_hint: str
    status: str
    resolved_at: Optional[str]
    notified_at: Optional[str]
    created_at: str
    updated_at: str
    dedup_key: str


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ops_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            level TEXT NOT NULL CHECK(level IN ('critical', 'warning', 'info')),
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT DEFAULT '',
            action_hint TEXT DEFAULT '',
            status TEXT DEFAULT 'open' CHECK(status IN ('open', 'acknowledged', 'resolved')),
            resolved_at DATETIME,
            notified_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            dedup_key TEXT UNIQUE
        );

        CREATE INDEX IF NOT EXISTS idx_events_status ON ops_events(status, level);
        CREATE INDEX IF NOT EXISTS idx_events_project ON ops_events(project, created_at);
    """)
    conn.close()


def upsert_event(event: EventCreate) -> tuple[dict, bool]:
    """插入或更新事件。返回 (event_row, is_new)"""
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    dedup_key = event.dedup_key or f"{event.project}:{event.category}:{event.title[:50]}"

    existing = conn.execute(
        "SELECT id, status FROM ops_events WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()

    if existing:
        if existing["status"] == "resolved":
            conn.execute("DELETE FROM ops_events WHERE id = ?", (existing["id"],))
        else:
            conn.execute(
                "UPDATE ops_events SET detail=?, action_hint=?, updated_at=? WHERE id=?",
                (event.detail, event.action_hint, now, existing["id"]),
            )
            row = conn.execute("SELECT * FROM ops_events WHERE id=?", (existing["id"],)).fetchone()
            conn.commit()
            conn.close()
            return dict(row), False

    conn.execute(
        """INSERT INTO ops_events (project, level, category, title, detail, action_hint, dedup_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event.project, event.level, event.category, event.title,
         event.detail, event.action_hint, dedup_key, now, now),
    )
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute("SELECT * FROM ops_events WHERE id=?", (row_id,)).fetchone()
    conn.commit()
    conn.close()
    return dict(row), True


def get_events(
    project: Optional[str] = None,
    level: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    conn = get_db()
    query = "SELECT * FROM ops_events WHERE 1=1"
    params: list = []

    if project:
        query += " AND project = ?"
        params.append(project)
    if level:
        query += " AND level = ?"
        params.append(level)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_event_status(event_id: int, new_status: str) -> Optional[dict]:
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    resolved_at = now if new_status == "resolved" else None

    conn.execute(
        "UPDATE ops_events SET status=?, resolved_at=?, updated_at=? WHERE id=?",
        (new_status, resolved_at, now, event_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM ops_events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_stats() -> dict:
    conn = get_db()
    rows = conn.execute(
        "SELECT level, COUNT(*) as cnt FROM ops_events WHERE status='open' GROUP BY level"
    ).fetchall()
    conn.close()
    stats = {"critical": 0, "warning": 0, "info": 0}
    for r in rows:
        stats[r["level"]] = r["cnt"]
    stats["total_open"] = sum(stats.values())
    return stats


def get_project_summary() -> list[dict]:
    conn = get_db()
    rows = conn.execute("""
        SELECT project,
               SUM(CASE WHEN status='open' AND level='critical' THEN 1 ELSE 0 END) as critical_count,
               SUM(CASE WHEN status='open' AND level='warning' THEN 1 ELSE 0 END) as warning_count,
               SUM(CASE WHEN status='open' AND level='info' THEN 1 ELSE 0 END) as info_count,
               MAX(created_at) as latest_event_at
        FROM ops_events
        GROUP BY project
        ORDER BY critical_count DESC, warning_count DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_notified(event_id: int) -> None:
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute("UPDATE ops_events SET notified_at=? WHERE id=?", (now, event_id))
    conn.commit()
    conn.close()
