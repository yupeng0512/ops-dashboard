"""告警推送模块 - 飞书 / 企微 webhook"""

import json
import os
import logging
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

from models import get_db, mark_notified, get_events, get_stats

logger = logging.getLogger("ops-dashboard.notifier")

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
WEWORK_WEBHOOK_URL = os.getenv("WEWORK_WEBHOOK_URL", "")
NOTIFY_COOLDOWN_HOURS = int(os.getenv("NOTIFY_COOLDOWN_HOURS", "24"))

LEVEL_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


def notify_new_event(event: dict) -> None:
    """对新产生的 critical/warning 事件即时推送"""
    if event["level"] not in ("critical", "warning"):
        return

    if event.get("notified_at"):
        notified = datetime.fromisoformat(event["notified_at"].rstrip("Z"))
        if datetime.utcnow() - notified < timedelta(hours=NOTIFY_COOLDOWN_HOURS):
            return

    emoji = LEVEL_EMOJI.get(event["level"], "")
    title = f"{emoji} [{event['project']}] {event['title']}"
    lines = [title, ""]
    if event.get("detail"):
        lines.append(f"详情: {event['detail'][:500]}")
    if event.get("action_hint"):
        lines.append(f"建议操作: {event['action_hint']}")
    lines.append(f"分类: {event['category']}")
    lines.append(f"时间: {event['created_at']}")

    message = "\n".join(lines)

    sent = False
    if FEISHU_WEBHOOK_URL:
        sent = _send_feishu(message) or sent
    if WEWORK_WEBHOOK_URL:
        sent = _send_wework(message) or sent

    if sent:
        mark_notified(event["id"])


def send_daily_summary() -> None:
    """每日汇总推送"""
    stats = get_stats()
    if stats["total_open"] == 0:
        return

    open_events = get_events(status="open", limit=50)

    lines = ["📋 运维告警日报", ""]
    lines.append(f"当前 Open 事件: {stats['total_open']} 条")
    lines.append(f"  🔴 Critical: {stats['critical']}")
    lines.append(f"  🟡 Warning: {stats['warning']}")
    lines.append(f"  🔵 Info: {stats['info']}")
    lines.append("")

    if stats["critical"] > 0 or stats["warning"] > 0:
        lines.append("--- 需关注事件 ---")
        for e in open_events:
            if e["level"] in ("critical", "warning"):
                emoji = LEVEL_EMOJI.get(e["level"], "")
                lines.append(f"{emoji} [{e['project']}] {e['title']}")
                if e.get("action_hint"):
                    lines.append(f"   → {e['action_hint']}")

    message = "\n".join(lines)

    if FEISHU_WEBHOOK_URL:
        _send_feishu(message)
    if WEWORK_WEBHOOK_URL:
        _send_wework(message)


def _send_feishu(message: str) -> bool:
    if not FEISHU_WEBHOOK_URL:
        return False
    payload = {"msg_type": "text", "content": {"text": message}}
    return _post_webhook(FEISHU_WEBHOOK_URL, payload)


def _send_wework(message: str) -> bool:
    if not WEWORK_WEBHOOK_URL:
        return False
    if len(message.encode("utf-8")) > 4000:
        message = message[:1800] + "\n...(已截断)"
    payload = {"msgtype": "text", "text": {"content": message}}
    return _post_webhook(WEWORK_WEBHOOK_URL, payload)


def _post_webhook(url: str, payload: dict) -> bool:
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except (URLError, OSError) as e:
        logger.warning(f"Webhook push failed: {e}")
        return False
