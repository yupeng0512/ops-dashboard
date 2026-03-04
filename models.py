"""数据模型 + 数据库初始化 + 动态配置管理"""

import os
import re
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


# ---------------------------------------------------------------------------
# 配置项定义：key -> (label, type, default_env_key, fallback_default, description)
# type: "str" | "int" | "bool"
# ---------------------------------------------------------------------------
CONFIG_SCHEMA: dict[str, dict] = {
    "FEISHU_WEBHOOK_URL": {
        "label": "飞书 Webhook URL",
        "type": "str",
        "group": "notification",
        "description": "飞书机器人 Webhook 地址",
    },
    "WEWORK_WEBHOOK_URL": {
        "label": "企微 Webhook URL",
        "type": "str",
        "group": "notification",
        "description": "企业微信机器人 Webhook 地址",
    },
    "NOTIFY_COOLDOWN_HOURS": {
        "label": "推送冷却时间 (小时)",
        "type": "int",
        "group": "notification",
        "default": "1",
        "description": "同一事件推送间隔，避免重复告警",
    },
    "EVENT_STALE_THRESHOLD_HOURS": {
        "label": "事件超时阈值 (小时)",
        "type": "int",
        "group": "escalation",
        "default": "1",
        "description": "事件超过此时间未解决则触发升级告警",
    },
    "LOG_STALE_THRESHOLD_HOURS": {
        "label": "日志滞留阈值 (小时)",
        "type": "int",
        "group": "escalation",
        "default": "2",
        "description": "项目超过此时间无更新但仍有 open 事件则告警",
    },
    "ESCALATION_CHECK_INTERVAL": {
        "label": "升级检查间隔 (秒)",
        "type": "int",
        "group": "escalation",
        "default": "600",
        "description": "后台定时检查超时事件和日志滞留的间隔",
    },
    "PROBE_INTERVAL_SECONDS": {
        "label": "探测间隔 (秒)",
        "type": "int",
        "group": "probe",
        "default": "300",
        "description": "容器和健康端点探测的执行间隔",
    },
    "MUTED_PROJECTS": {
        "label": "静默项目列表",
        "type": "str",
        "group": "probe",
        "default": "digital-twin",
        "description": "逗号分隔。命中的项目事件将被静默（不入 open 队列）",
    },
    "TRANSIENT_FAILURE_THRESHOLD": {
        "label": "瞬时故障告警阈值",
        "type": "int",
        "group": "probe",
        "default": "3",
        "description": "同一 dedup_key 连续失败达到阈值才生成告警",
    },
    "TRANSIENT_FAILURE_WINDOW_MINUTES": {
        "label": "瞬时故障统计窗口 (分钟)",
        "type": "int",
        "group": "probe",
        "default": "30",
        "description": "超过窗口后重置瞬时故障计数",
    },
    "DAILY_SUMMARY_HOUR": {
        "label": "日报推送时间 (UTC 小时)",
        "type": "int",
        "group": "notification",
        "default": "9",
        "description": "每日汇总推送的 UTC 小时数 (0-23)",
    },
}


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

        CREATE TABLE IF NOT EXISTS ops_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
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


def auto_resolve_by_dedup_key(dedup_key: str) -> int:
    """Auto-resolve open events matching a dedup_key. Returns count resolved."""
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    cursor = conn.execute(
        "UPDATE ops_events SET status='resolved', resolved_at=?, updated_at=? "
        "WHERE dedup_key=? AND status='open'",
        (now, now, dedup_key),
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


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


def _parse_iso_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.rstrip("Z"))


def _normalize_signal_key(row: dict) -> str:
    dedup_key = row.get("dedup_key", "") or ""
    if dedup_key:
        parts = dedup_key.split(":")
        parts[-1] = re.sub(r"_(degraded|recovered)$", "", parts[-1])
        return ":".join(parts)

    title = row.get("title", "") or ""
    title = title.replace(" 连续失败", "").replace(" 已恢复", "")
    return f"{row.get('project', '')}:{title}"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * p)
    return ordered[idx]


def _mttr_trend(mttr_series: list[float]) -> tuple[str, float]:
    if len(mttr_series) < 4:
        return "stable", 0.0

    recent = mttr_series[-3:]
    if len(mttr_series) >= 6:
        prev = mttr_series[-6:-3]
    else:
        prev = mttr_series[:-3]
    if not prev:
        return "stable", 0.0

    recent_avg = sum(recent) / len(recent)
    prev_avg = sum(prev) / len(prev)
    if prev_avg <= 0:
        return "stable", 0.0

    delta_pct = (recent_avg - prev_avg) / prev_avg
    if delta_pct <= -0.15:
        return "improving", round(delta_pct, 3)
    if delta_pct >= 0.15:
        return "worsening", round(delta_pct, 3)
    return "stable", round(delta_pct, 3)


def get_mttr_map(projects: Optional[set[str]] = None) -> dict[str, dict]:
    """Compute project-level MTTR from *_degraded -> *_recovered event pairs."""
    conn = get_db()
    if projects:
        placeholders = ",".join("?" for _ in projects)
        query = (
            "SELECT project, category, dedup_key, title, created_at "
            "FROM ops_events "
            "WHERE (category LIKE '%_degraded' OR category LIKE '%_recovered') "
            f"AND project IN ({placeholders}) "
            "ORDER BY created_at ASC"
        )
        rows = conn.execute(query, list(projects)).fetchall()
    else:
        rows = conn.execute(
            "SELECT project, category, dedup_key, title, created_at "
            "FROM ops_events "
            "WHERE category LIKE '%_degraded' OR category LIKE '%_recovered' "
            "ORDER BY created_at ASC"
        ).fetchall()
    conn.close()

    pending: dict[tuple[str, str], datetime] = {}
    durations_by_project: dict[str, list[float]] = {}

    for row in rows:
        event = dict(row)
        project = event.get("project", "")
        signal_key = _normalize_signal_key(event)
        cat = event.get("category", "")
        ts = _parse_iso_dt(event.get("created_at", ""))
        key = (project, signal_key)

        if cat.endswith("_degraded"):
            pending[key] = ts
            continue
        if cat.endswith("_recovered"):
            started = pending.pop(key, None)
            if not started:
                continue
            duration = (ts - started).total_seconds()
            if duration < 0:
                continue
            durations_by_project.setdefault(project, []).append(duration)

    result: dict[str, dict] = {}
    for project, series in durations_by_project.items():
        avg = sum(series) / len(series)
        trend, delta_pct = _mttr_trend(series)
        result[project] = {
            "sample_count": len(series),
            "last_mttr_seconds": round(series[-1], 2),
            "avg_mttr_seconds": round(avg, 2),
            "p95_mttr_seconds": round(_percentile(series, 0.95), 2),
            "trend": trend,
            "trend_delta_pct": delta_pct,
        }

    return result


def mark_notified(event_id: int) -> None:
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute("UPDATE ops_events SET notified_at=? WHERE id=?", (now, event_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 动态配置：DB 优先 → ENV 兜底 → schema default
# ---------------------------------------------------------------------------

def get_config(key: str) -> str:
    """读取单个配置。优先级：DB → ENV → schema default"""
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM ops_config WHERE key = ?", (key,)
    ).fetchone()
    conn.close()

    if row and row["value"] != "":
        return row["value"]

    env_val = os.getenv(key, "")
    if env_val:
        return env_val

    schema = CONFIG_SCHEMA.get(key, {})
    return schema.get("default", "")


def get_config_int(key: str, fallback: int = 0) -> int:
    val = get_config(key)
    try:
        return int(val) if val else fallback
    except (ValueError, TypeError):
        return fallback


def get_all_configs() -> list[dict]:
    """获取所有配置项（合并 schema + DB + ENV）"""
    conn = get_db()
    db_rows = conn.execute("SELECT key, value, updated_at FROM ops_config").fetchall()
    conn.close()

    db_map = {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in db_rows}

    result = []
    for key, schema in CONFIG_SCHEMA.items():
        db_entry = db_map.get(key)
        env_val = os.getenv(key, "")
        schema_default = schema.get("default", "")

        if db_entry and db_entry["value"] != "":
            effective = db_entry["value"]
            source = "db"
        elif env_val:
            effective = env_val
            source = "env"
        elif schema_default:
            effective = schema_default
            source = "default"
        else:
            effective = ""
            source = "none"

        is_secret = "webhook" in key.lower() or "secret" in key.lower() or "token" in key.lower()

        result.append({
            "key": key,
            "label": schema["label"],
            "type": schema["type"],
            "group": schema["group"],
            "description": schema["description"],
            "value": effective,
            "source": source,
            "is_secret": is_secret,
            "updated_at": db_entry["updated_at"] if db_entry else None,
        })

    return result


def set_config(key: str, value: str) -> dict:
    """写入配置到数据库"""
    if key not in CONFIG_SCHEMA:
        raise ValueError(f"Unknown config key: {key}")

    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        """INSERT INTO ops_config (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, now),
    )
    conn.commit()
    conn.close()
    return {"key": key, "value": value, "source": "db", "updated_at": now}


def delete_config(key: str) -> bool:
    """删除数据库中的配置，恢复使用 ENV 兜底"""
    conn = get_db()
    conn.execute("DELETE FROM ops_config WHERE key = ?", (key,))
    conn.commit()
    conn.close()
    return True
