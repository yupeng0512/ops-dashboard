"""
ops_reporter - 轻量运维事件上报模块

零外部依赖（仅用标准库），各项目复制此文件到自己的 src/ 下即可。
失败时静默不影响主流程。

用法:
    from ops_reporter import report_event

    report_event(
        project="infohunter",
        level="warning",
        category="auth_expired",
        title="YouTube OAuth Token 已过期",
        detail="刷新失败: Token has been expired or revoked",
        action_hint="执行: curl http://localhost:6003/api/youtube/oauth/authorize",
        dedup_key="infohunter:youtube_token_expired",
    )
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

OPS_DASHBOARD_URL = os.getenv("OPS_DASHBOARD_URL", "http://ops-dashboard:9090")
OPS_EVENTS_LOG = os.getenv("OPS_EVENTS_LOG", "logs/ops_events.jsonl")

_VALID_LEVELS = ("critical", "warning", "info")


def report_event(
    project: str,
    level: str,
    category: str,
    title: str,
    detail: str = "",
    action_hint: str = "",
    dedup_key: str = "",
) -> None:
    """上报运维事件（异步非阻塞，失败静默）"""
    if level not in _VALID_LEVELS:
        return

    payload = {
        "project": project,
        "level": level,
        "category": category,
        "title": title,
        "detail": detail[:2000],
        "action_hint": action_hint,
        "dedup_key": dedup_key or f"{project}:{category}:{title[:50]}",
    }

    t = threading.Thread(target=_send, args=(payload,), daemon=True)
    t.start()


def _send(payload: dict) -> None:
    _write_local(payload)
    _post_remote(payload)


def _write_local(payload: dict) -> None:
    """写入本地 JSONL 备份"""
    try:
        log_path = Path(OPS_EVENTS_LOG)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {**payload, "timestamp": datetime.utcnow().isoformat() + "Z"}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _post_remote(payload: dict) -> None:
    """POST 到 ops-dashboard"""
    try:
        url = f"{OPS_DASHBOARD_URL}/api/events"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as resp:
            resp.read()
    except (URLError, OSError, Exception):
        pass
