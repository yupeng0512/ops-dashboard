"""ops-dashboard - 统一运维告警面板"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from models import (
    EventCreate, EventUpdate,
    init_db, upsert_event, get_events, update_event_status,
    get_stats, get_project_summary,
)
from notifier import notify_new_event, send_daily_summary
from probes import run_probes, get_container_statuses

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("ops-dashboard")

PROBE_INTERVAL_SECONDS = 300
DAILY_SUMMARY_HOUR = 9


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized")

    probe_task = asyncio.create_task(_probe_loop())
    summary_task = asyncio.create_task(_daily_summary_loop())
    logger.info("Background tasks started")

    yield

    probe_task.cancel()
    summary_task.cancel()


app = FastAPI(title="Ops Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/events")
async def create_event(event: EventCreate):
    """接收事件上报"""
    row, is_new = upsert_event(event)
    if is_new:
        logger.info(f"New event: [{row['level']}] [{row['project']}] {row['title']}")
        try:
            notify_new_event(row)
        except Exception as e:
            logger.warning(f"Notification failed: {e}")
    return {"status": "ok", "is_new": is_new, "event": row}


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
    """统计数据"""
    return get_stats()


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
    """定时探测容器和健康端点"""
    while True:
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)
        try:
            events = await asyncio.to_thread(run_probes)
            for evt_data in events:
                event = EventCreate(**evt_data)
                row, is_new = upsert_event(event)
                if is_new:
                    logger.info(f"Probe event: [{row['level']}] [{row['project']}] {row['title']}")
                    try:
                        notify_new_event(row)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Probe loop error: {e}")


async def _daily_summary_loop():
    """每日 09:00 推送汇总"""
    while True:
        now = datetime.utcnow()
        target_hour = DAILY_SUMMARY_HOUR
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=9090, log_level="info")
