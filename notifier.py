"""告警推送模块 - 飞书 / 企微 webhook

所有配置实时从数据库读取，DB 优先 → ENV 兜底 → schema default。

支持以下告警场景：
- 即时告警：新 critical/warning 事件
- 自愈失败告警：GEP 修复失败后推送
- 升级告警：事件长时间未解决（超过阈值）
- 日志滞留告警：项目长时间无新事件更新但仍有 open 事件
- 每日汇总：每天推送统计概览
"""

import json
import logging
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError

from models import get_config, get_config_int, mark_notified, get_events, get_stats

logger = logging.getLogger("ops-dashboard.notifier")

LEVEL_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}

_escalation_tracker: dict[str, str] = {}


def notify_new_event(event: dict) -> None:
    """对新产生的 critical/warning 事件即时推送"""
    if event["level"] not in ("critical", "warning"):
        return

    cooldown = get_config_int("NOTIFY_COOLDOWN_HOURS", 1)

    if event.get("notified_at"):
        notified = datetime.fromisoformat(event["notified_at"].rstrip("Z"))
        if datetime.utcnow() - notified < timedelta(hours=cooldown):
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
    _broadcast(message, event_id=event["id"])


def notify_repair_failed(event: dict, capsule: dict) -> None:
    """自愈失败后推送告警"""
    gene_id = capsule.get("gene_id", "unknown")
    output = capsule.get("outcome", {}).get("output", "")[:300]
    duration = capsule.get("outcome", {}).get("duration_ms", 0)

    lines = [
        f"⚠️ [{event['project']}] 自愈失败，需人工介入",
        "",
        f"事件: {event.get('title', '')}",
        f"修复基因: {gene_id}",
        f"耗时: {duration}ms",
        f"失败原因: {output}",
        "",
        f"建议操作: {event.get('action_hint', '请手动检查')}",
        f"分类: {event.get('category', '')}",
        f"时间: {event.get('created_at', '')}",
    ]

    message = "\n".join(lines)
    _broadcast(message)
    logger.info(f"Repair failure notification sent for [{event.get('project')}] {event.get('title')}")


def check_stale_events() -> None:
    """检查长时间未解决的事件并推送升级告警"""
    open_events = get_events(status="open", limit=200)
    if not open_events:
        return

    stale_hours = get_config_int("EVENT_STALE_THRESHOLD_HOURS", 1)
    cooldown = get_config_int("NOTIFY_COOLDOWN_HOURS", 1)

    now = datetime.utcnow()
    threshold = timedelta(hours=stale_hours)
    stale_events = []

    for e in open_events:
        if e["level"] not in ("critical", "warning"):
            continue

        created = datetime.fromisoformat(e["created_at"].rstrip("Z"))
        duration = now - created

        if duration < threshold:
            continue

        tracker_key = f"stale:{e['id']}"
        last_escalated = _escalation_tracker.get(tracker_key)
        if last_escalated:
            last_time = datetime.fromisoformat(last_escalated)
            if now - last_time < timedelta(hours=cooldown):
                continue

        stale_events.append((e, duration))
        _escalation_tracker[tracker_key] = now.isoformat()

    if not stale_events:
        return

    lines = [
        f"⏰ 升级告警：{len(stale_events)} 个事件超时未解决",
        f"阈值: {stale_hours} 小时",
        "",
    ]

    for e, duration in stale_events:
        hours = duration.total_seconds() / 3600
        emoji = LEVEL_EMOJI.get(e["level"], "")
        lines.append(f"{emoji} [{e['project']}] {e['title']}")
        lines.append(f"   持续时间: {hours:.1f} 小时")
        if e.get("action_hint"):
            lines.append(f"   建议操作: {e['action_hint']}")
        lines.append("")

    message = "\n".join(lines)
    _broadcast(message)
    logger.info(f"Stale event escalation sent for {len(stale_events)} events")


def check_log_stale() -> None:
    """检查日志滞留 - 项目有 open 事件但长时间无更新"""
    open_events = get_events(status="open", limit=200)
    if not open_events:
        return

    log_stale_hours = get_config_int("LOG_STALE_THRESHOLD_HOURS", 2)
    cooldown = get_config_int("NOTIFY_COOLDOWN_HOURS", 1)

    now = datetime.utcnow()
    threshold = timedelta(hours=log_stale_hours)

    project_events: dict[str, list[dict]] = {}
    for e in open_events:
        project_events.setdefault(e["project"], []).append(e)

    stale_projects = []
    for project, events in project_events.items():
        latest_update = max(
            datetime.fromisoformat(e["updated_at"].rstrip("Z")) for e in events
        )
        staleness = now - latest_update

        if staleness < threshold:
            continue

        tracker_key = f"log_stale:{project}"
        last_notified = _escalation_tracker.get(tracker_key)
        if last_notified:
            last_time = datetime.fromisoformat(last_notified)
            if now - last_time < timedelta(hours=cooldown):
                continue

        critical_count = sum(1 for e in events if e["level"] == "critical")
        warning_count = sum(1 for e in events if e["level"] == "warning")
        stale_projects.append({
            "project": project,
            "event_count": len(events),
            "critical": critical_count,
            "warning": warning_count,
            "stale_hours": staleness.total_seconds() / 3600,
            "events": events,
        })
        _escalation_tracker[tracker_key] = now.isoformat()

    if not stale_projects:
        return

    lines = [
        f"📋 日志滞留告警：{len(stale_projects)} 个项目事件长时间无更新",
        f"阈值: {log_stale_hours} 小时",
        "",
    ]

    for p in stale_projects:
        lines.append(f"📌 {p['project']}")
        lines.append(f"   滞留时间: {p['stale_hours']:.1f} 小时")
        lines.append(f"   Open 事件: {p['event_count']} 条 (🔴{p['critical']} 🟡{p['warning']})")
        for e in p["events"][:3]:
            emoji = LEVEL_EMOJI.get(e["level"], "")
            lines.append(f"   {emoji} {e['title']}")
        if len(p["events"]) > 3:
            lines.append(f"   ... 还有 {len(p['events']) - 3} 条")
        lines.append("")

    message = "\n".join(lines)
    _broadcast(message)
    logger.info(f"Log stale notification sent for {len(stale_projects)} projects")


def send_daily_summary() -> None:
    """每日汇总推送"""
    stats = get_stats()
    if stats["total_open"] == 0:
        return

    stale_hours = get_config_int("EVENT_STALE_THRESHOLD_HOURS", 1)
    open_events = get_events(status="open", limit=50)

    lines = ["📋 运维告警日报", ""]
    lines.append(f"当前 Open 事件: {stats['total_open']} 条")
    lines.append(f"  🔴 Critical: {stats['critical']}")
    lines.append(f"  🟡 Warning: {stats['warning']}")
    lines.append(f"  🔵 Info: {stats['info']}")
    lines.append("")

    now = datetime.utcnow()
    stale_count = 0
    for e in open_events:
        if e["level"] in ("critical", "warning"):
            created = datetime.fromisoformat(e["created_at"].rstrip("Z"))
            if (now - created).total_seconds() > stale_hours * 3600:
                stale_count += 1

    if stale_count > 0:
        lines.append(f"⏰ 超时未解决: {stale_count} 条")
        lines.append("")

    if stats["critical"] > 0 or stats["warning"] > 0:
        lines.append("--- 需关注事件 ---")
        for e in open_events:
            if e["level"] in ("critical", "warning"):
                emoji = LEVEL_EMOJI.get(e["level"], "")
                created = datetime.fromisoformat(e["created_at"].rstrip("Z"))
                hours = (now - created).total_seconds() / 3600
                age_tag = f" [{hours:.0f}h]" if hours >= 1 else ""
                lines.append(f"{emoji} [{e['project']}] {e['title']}{age_tag}")
                if e.get("action_hint"):
                    lines.append(f"   → {e['action_hint']}")

    message = "\n".join(lines)
    _broadcast(message)


# ---------------------------------------------------------------------------
# 底层推送
# ---------------------------------------------------------------------------

def _broadcast(message: str, event_id: int | None = None) -> None:
    """统一广播消息到所有已配置的渠道（实时读取配置）"""
    feishu_url = get_config("FEISHU_WEBHOOK_URL")
    wework_url = get_config("WEWORK_WEBHOOK_URL")

    sent = False
    if feishu_url:
        sent = _send_feishu_dynamic(message, feishu_url) or sent
    if wework_url:
        sent = _send_wework_dynamic(message, wework_url) or sent

    if sent and event_id is not None:
        mark_notified(event_id)


def _send_feishu_dynamic(message: str, url: str) -> bool:
    payload = {"msg_type": "text", "content": {"text": message}}
    return _post_webhook(url, payload)


def _send_wework_dynamic(message: str, url: str) -> bool:
    if len(message.encode("utf-8")) > 4000:
        message = message[:1800] + "\n...(已截断)"
    payload = {"msgtype": "text", "text": {"content": message}}
    return _post_webhook(url, payload)


def _post_webhook(url: str, payload: dict) -> bool:
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            body = resp.read()
            try:
                result = json.loads(body)
                code = result.get("code", result.get("StatusCode", -1))
                if code != 0:
                    logger.warning(f"Webhook returned non-zero: {result}")
                    return False
            except (json.JSONDecodeError, ValueError):
                pass
        return True
    except (URLError, OSError) as e:
        logger.warning(f"Webhook push failed: {e}")
        return False
