"""Pull 层 - 容器状态探测 + Health 端点轮询"""

import json
import logging
import os
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger("ops-dashboard.probes")

PROJECTS_CONFIG = [
    {
        "name": "github-sentinel",
        "containers": [
            "github-sentinel-backend",
            "github-sentinel-scheduler",
            "github-sentinel-frontend",
            "github-sentinel-mysql",
        ],
        "health_url": "http://github-sentinel-backend:8000/health",
    },
    {
        "name": "trendradar",
        "containers": ["trendradar", "trendradar-mcp"],
        "health_url": None,
    },
    {
        "name": "infohunter",
        "containers": ["infohunter"],
        "health_url": "http://infohunter:6002/api/health",
    },
    {
        "name": "truthsocial-trump-monitor",
        "containers": ["truthsocial-trump-monitor"],
        "health_url": "http://truthsocial-trump-monitor:8000/api/health",
    },
    {
        "name": "digital-twin",
        "containers": [
            "digital-twin-joplin-server",
            "digital-twin-opennotebook",
            "digital-twin-graphiti-mcp",
            "digital-twin-neo4j",
            "digital-twin-joplin-db",
        ],
        "health_url": None,
    },
    {
        "name": "mcp-gateway",
        "containers": ["mcp-gateway", "pinme-mcp"],
        "health_url": None,
    },
    {
        "name": "rsshub",
        "containers": ["rsshub"],
        "health_url": None,
    },
]


def run_probes() -> list[dict]:
    """执行所有探测，返回需要上报的事件列表"""
    events = []

    for project in PROJECTS_CONFIG:
        events.extend(_check_containers(project))

        if project.get("health_url"):
            evt = _check_health(project)
            if evt:
                events.append(evt)

    return events


def _check_containers(project: dict) -> list[dict]:
    """通过 Docker API 检查容器状态"""
    events = []
    try:
        import docker
        client = docker.from_env()

        for container_name in project["containers"]:
            try:
                container = client.containers.get(container_name)
                status = container.status
                health = "N/A"

                if container.attrs.get("State", {}).get("Health"):
                    health = container.attrs["State"]["Health"]["Status"]

                if status != "running":
                    events.append({
                        "project": project["name"],
                        "level": "critical",
                        "category": "container_stopped",
                        "title": f"容器 {container_name} 已停止",
                        "detail": f"状态: {status}",
                        "action_hint": f"docker start {container_name}",
                        "dedup_key": f"{project['name']}:container_stopped:{container_name}",
                    })
                elif health == "unhealthy":
                    log_tail = ""
                    health_log = container.attrs["State"]["Health"].get("Log", [])
                    if health_log:
                        last = health_log[-1]
                        log_tail = last.get("Output", "")[:300]
                    events.append({
                        "project": project["name"],
                        "level": "warning",
                        "category": "container_unhealthy",
                        "title": f"容器 {container_name} 健康检查失败",
                        "detail": log_tail,
                        "action_hint": f"docker logs {container_name} --tail 50",
                        "dedup_key": f"{project['name']}:container_unhealthy:{container_name}",
                    })
            except docker.errors.NotFound:
                events.append({
                    "project": project["name"],
                    "level": "critical",
                    "category": "container_stopped",
                    "title": f"容器 {container_name} 不存在",
                    "detail": "容器未找到，可能未启动或已被删除",
                    "action_hint": f"cd /data/workspace/{project['name']} && docker compose up -d",
                    "dedup_key": f"{project['name']}:container_stopped:{container_name}",
                })
            except Exception as e:
                logger.debug(f"Container check error for {container_name}: {e}")

    except ImportError:
        logger.warning("docker package not installed, skipping container probes")
    except Exception as e:
        logger.warning(f"Docker API error: {e}")

    return events


def _check_health(project: dict) -> dict | None:
    """HTTP 健康检查"""
    url = project["health_url"]
    if not url:
        return None

    try:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return None
            return {
                "project": project["name"],
                "level": "warning",
                "category": "connection_failed",
                "title": f"{project['name']} 健康检查返回 {resp.status}",
                "detail": f"URL: {url}",
                "action_hint": f"docker logs {project['containers'][0]} --tail 50",
                "dedup_key": f"{project['name']}:health_check_failed",
            }
    except (URLError, OSError) as e:
        return {
            "project": project["name"],
            "level": "warning",
            "category": "connection_failed",
            "title": f"{project['name']} 健康检查不可达",
            "detail": f"URL: {url}, Error: {str(e)[:200]}",
            "action_hint": f"docker logs {project['containers'][0]} --tail 50",
            "dedup_key": f"{project['name']}:health_check_failed",
        }


def get_container_statuses() -> list[dict]:
    """获取所有项目的容器运行状态（供前端展示）"""
    result = []
    try:
        import docker
        client = docker.from_env()

        for project in PROJECTS_CONFIG:
            containers = []
            for cname in project["containers"]:
                try:
                    c = client.containers.get(cname)
                    health = "N/A"
                    if c.attrs.get("State", {}).get("Health"):
                        health = c.attrs["State"]["Health"]["Status"]
                    containers.append({
                        "name": cname,
                        "status": c.status,
                        "health": health,
                    })
                except Exception:
                    containers.append({
                        "name": cname,
                        "status": "not_found",
                        "health": "N/A",
                    })
            result.append({
                "project": project["name"],
                "containers": containers,
            })
    except ImportError:
        for project in PROJECTS_CONFIG:
            result.append({
                "project": project["name"],
                "containers": [{"name": c, "status": "unknown", "health": "N/A"} for c in project["containers"]],
            })
    except Exception as e:
        logger.warning(f"Failed to get container statuses: {e}")

    return result
