"""ops-dashboard - 统一运维告警面板"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel as PydanticBaseModel

from models import (
    EventCreate, EventUpdate,
    init_db, upsert_event, get_events, update_event_status,
    auto_resolve_by_dedup_key,
    get_stats, get_project_summary,
    get_all_configs, get_config, get_config_int, set_config, delete_config,
)
from notifier import (
    notify_new_event, notify_repair_failed,
    send_daily_summary, check_stale_events, check_log_stale,
)
from probes import run_probes, get_container_statuses
from repair_engine import attempt_repair, get_repair_stats, _load_capsules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("ops-dashboard")

class ConfigUpdate(PydanticBaseModel):
    value: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized")

    probe_task = asyncio.create_task(_probe_loop())
    summary_task = asyncio.create_task(_daily_summary_loop())
    escalation_task = asyncio.create_task(_escalation_loop())
    logger.info("Background tasks started")

    yield

    probe_task.cancel()
    summary_task.cancel()
    escalation_task.cancel()


app = FastAPI(title="Ops Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/events")
async def create_event(event: EventCreate):
    """接收事件上报，自动尝试 GEP 修复"""
    row, is_new = upsert_event(event)
    repair_capsule = None

    if is_new:
        logger.info(f"New event: [{row['level']}] [{row['project']}] {row['title']}")
        try:
            notify_new_event(row)
        except Exception as e:
            logger.warning(f"Notification failed: {e}")

    if row["level"] in ("critical", "warning") and row["status"] == "open":
        try:
            repair_capsule = await asyncio.to_thread(attempt_repair, row)
            if repair_capsule:
                if repair_capsule["outcome"]["status"] == "success":
                    update_event_status(row["id"], "resolved")
                    logger.info(f"Auto-resolved event {row['id']} via {repair_capsule['gene_id']}")
                elif is_new:
                    try:
                        notify_repair_failed(row, repair_capsule)
                    except Exception as e:
                        logger.warning(f"Repair failure notification error: {e}")
        except Exception as e:
            logger.warning(f"Repair engine error: {e}")

    return {"status": "ok", "is_new": is_new, "event": row, "repair": repair_capsule}


@app.get("/api/events")
async def list_events(
    project: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    """查询事件列表"""
    return get_events(project=project, level=level, status=status, limit=limit)


@app.patch("/api/events/{event_id}")
async def patch_event(event_id: int, update: EventUpdate):
    """更新事件状态"""
    row = update_event_status(event_id, update.status)
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    logger.info(f"Event {event_id} -> {update.status}")
    return row


@app.get("/api/stats")
async def stats():
    """统计数据（含修复统计）"""
    event_stats = get_stats()
    repair = get_repair_stats()
    return {**event_stats, "repair": repair}


@app.get("/api/repair/stats")
async def repair_stats():
    """GEP 修复统计"""
    return get_repair_stats()


@app.post("/api/repair/trigger/{event_id}")
async def trigger_repair(event_id: int):
    """手动对指定事件触发一次 GEP 修复"""
    from models import get_db
    conn = get_db()
    row = conn.execute("SELECT * FROM ops_events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")

    event = dict(row)
    if event["status"] != "open":
        return {"status": "skip", "reason": f"Event is {event['status']}, not open"}

    capsule = await asyncio.to_thread(attempt_repair, event)
    if not capsule:
        return {"status": "no_match", "reason": "No Gene matched (cooldown/banned/circuit-broken)"}

    if capsule["outcome"]["status"] == "success":
        update_event_status(event_id, "resolved")
        logger.info(f"Manual repair resolved event {event_id} via {capsule['gene_id']}")

    return {"status": "ok", "capsule": capsule}


@app.get("/api/repair/capsules")
async def list_capsules(
    gene_id: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    """查询修复历史（Capsule 列表）"""
    capsules = _load_capsules()
    if gene_id:
        capsules = [c for c in capsules if c.get("gene_id") == gene_id]
    if project:
        capsules = [c for c in capsules if c.get("project") == project]
    return capsules[-limit:]


@app.get("/api/config")
async def list_config():
    """获取所有配置项"""
    return get_all_configs()


@app.put("/api/config/{key}")
async def update_config(key: str, body: ConfigUpdate):
    """更新配置项"""
    try:
        result = set_config(key, body.value)
        logger.info(f"Config updated: {key}")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/config/{key}")
async def reset_config(key: str):
    """删除数据库配置，恢复 ENV 兜底值"""
    delete_config(key)
    logger.info(f"Config reset to ENV fallback: {key}")
    return {"status": "ok", "key": key}


@app.post("/api/config/test-notify")
async def test_notify():
    """发送测试推送，验证当前 Webhook 配置是否生效"""
    from notifier import _send_feishu_dynamic, _send_wework_dynamic
    results = {}
    feishu_url = get_config("FEISHU_WEBHOOK_URL")
    wework_url = get_config("WEWORK_WEBHOOK_URL")

    test_msg = "\n".join([
        "🧪 OPS Dashboard 推送测试",
        "",
        "这是一条测试消息，验证 Webhook 连通性。",
        f"时间: {datetime.utcnow().isoformat()}Z",
        "",
        "✅ 收到此消息说明配置正常！",
    ])

    if feishu_url:
        results["feishu"] = _send_feishu_dynamic(test_msg, feishu_url)
    else:
        results["feishu"] = None

    if wework_url:
        results["wework"] = _send_wework_dynamic(test_msg, wework_url)
    else:
        results["wework"] = None

    return results


@app.post("/api/probe")
async def manual_probe():
    """手动触发一次探测（不用等 5 分钟的定时循环）"""
    events, recovered_keys = await asyncio.to_thread(run_probes)

    resolved_count = 0
    for key in recovered_keys:
        resolved_count += auto_resolve_by_dedup_key(key)

    new_count = 0
    repair_count = 0
    for evt_data in events:
        event = EventCreate(**evt_data)
        row, is_new = upsert_event(event)
        if is_new:
            new_count += 1
            logger.info(f"Manual probe event: [{row['level']}] [{row['project']}] {row['title']}")

        if row["level"] in ("critical", "warning") and row["status"] == "open":
            try:
                capsule = await asyncio.to_thread(attempt_repair, row)
                if capsule:
                    repair_count += 1
                    if capsule["outcome"]["status"] == "success":
                        update_event_status(row["id"], "resolved")
                        resolved_count += 1
            except Exception as e:
                logger.warning(f"Manual probe repair error: {e}")

    return {
        "status": "ok",
        "new_events": new_count,
        "repair_attempts": repair_count,
        "auto_resolved": resolved_count,
        "total_anomalies": len(events),
        "total_recovered": len(recovered_keys),
    }


@app.get("/api/projects")
async def projects():
    """项目汇总状态（事件 + 容器）"""
    summaries = get_project_summary()
    containers = get_container_statuses()

    container_map = {c["project"]: c["containers"] for c in containers}

    all_project_names = set(s["project"] for s in summaries) | set(container_map.keys())

    result = []
    for name in sorted(all_project_names):
        summary = next((s for s in summaries if s["project"] == name), None)
        result.append({
            "project": name,
            "critical_count": summary["critical_count"] if summary else 0,
            "warning_count": summary["warning_count"] if summary else 0,
            "info_count": summary["info_count"] if summary else 0,
            "latest_event_at": summary["latest_event_at"] if summary else None,
            "containers": container_map.get(name, []),
        })

    return result


async def _probe_loop():
    """定时探测容器和健康端点，自动关闭已恢复的事件，对持续异常尝试修复"""
    while True:
        interval = get_config_int("PROBE_INTERVAL_SECONDS", 300)
        await asyncio.sleep(interval)
        try:
            events, recovered_keys = await asyncio.to_thread(run_probes)

            for key in recovered_keys:
                resolved_count = auto_resolve_by_dedup_key(key)
                if resolved_count > 0:
                    logger.info(f"Auto-resolved {resolved_count} event(s) for recovered key: {key}")

            for evt_data in events:
                event = EventCreate(**evt_data)
                row, is_new = upsert_event(event)
                if is_new:
                    logger.info(f"Probe event: [{row['level']}] [{row['project']}] {row['title']}")
                    try:
                        notify_new_event(row)
                    except Exception:
                        pass

                if row["level"] in ("critical", "warning") and row["status"] == "open":
                    try:
                        capsule = await asyncio.to_thread(attempt_repair, row)
                        if capsule:
                            if capsule["outcome"]["status"] == "success":
                                update_event_status(row["id"], "resolved")
                                logger.info(f"Auto-repair: {capsule['gene_id']} resolved event {row['id']}")
                            elif is_new:
                                try:
                                    notify_repair_failed(row, capsule)
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.warning(f"Probe repair error: {e}")
        except Exception as e:
            logger.error(f"Probe loop error: {e}")


async def _daily_summary_loop():
    """每日推送汇总（时间可动态配置）"""
    while True:
        now = datetime.utcnow()
        target_hour = get_config_int("DAILY_SUMMARY_HOUR", 9)
        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if now.hour >= target_hour:
            next_run = next_run.replace(day=now.day + 1)

        wait_seconds = (next_run - now).total_seconds()
        logger.info(f"Daily summary scheduled in {wait_seconds/3600:.1f}h")
        await asyncio.sleep(wait_seconds)

        try:
            send_daily_summary()
            logger.info("Daily summary sent")
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")


async def _escalation_loop():
    """定时检查超时未解决事件 + 日志滞留"""
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(check_stale_events)
        except Exception as e:
            logger.error(f"Stale events check error: {e}")

        try:
            await asyncio.to_thread(check_log_stale)
        except Exception as e:
            logger.error(f"Log stale check error: {e}")

        interval = get_config_int("ESCALATION_CHECK_INTERVAL", 600)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=9090, log_level="info")
