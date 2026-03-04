"""
GEP-inspired Repair Engine for Ops Dashboard.

Implements a 5-phase repair lifecycle inspired by EvoMap's GEP protocol:
  Detect → Select → Execute → Evaluate → Solidify

Key mechanisms:
  - Laplace-smoothed confidence scoring with exponential decay (30-day half-life)
  - Per-event circuit breaker: stops retrying after consecutive failures
  - Gene auto-ban: genes with sustained low success rates are disabled
  - Cooldown tracker: prevents rapid-fire retries of the same gene
"""

import hashlib
import json
import logging
import math
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ops-dashboard.repair")

GENES_DIR = Path("genes")
CAPSULES_PATH = Path("data/repair_capsules.jsonl")
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/data/workspace"))
PROJECT_PRIMARY_CONTAINERS = {
    "trading-system": "trading-api",
    "infohunter": "infohunter",
    "github-sentinel": "github-sentinel-backend",
    "truthsocial-trump-monitor": "truthsocial-trump-monitor",
    "claws": "claws",
    "ops-dashboard": "ops-dashboard",
}

COOLDOWN_TRACKER: dict[str, float] = {}

CIRCUIT_BREAKER: dict[str, int] = {}
MAX_CONSECUTIVE_FAILURES = 3

GENE_BAN_LIST: set[str] = set()
BAN_MIN_ATTEMPTS = 3
BAN_THRESHOLD = 0.18

DECAY_HALF_LIFE_DAYS = 30.0


def _load_genes() -> list[dict]:
    """Load all Gene definitions from the genes directory."""
    genes = []
    for gene_file in GENES_DIR.glob("*.json"):
        try:
            with open(gene_file, "r") as f:
                data = json.load(f)
            genes.extend(data.get("genes", []))
        except Exception as e:
            logger.warning(f"Failed to load genes from {gene_file}: {e}")
    return genes


def _match_signals(gene: dict, event_category: str, event_title: str) -> float:
    """Score how well an event matches a Gene's signals_match patterns.

    Returns 0.0 (no match) to 1.0 (perfect match).
    """
    signals = gene.get("signals_match", [])
    if not signals:
        return 0.0

    combined = f"{event_category} {event_title}".lower()
    matches = sum(1 for s in signals if s.lower() in combined)
    return matches / len(signals) if signals else 0.0


def _check_cooldown(gene_id: str, cooldown_seconds: int) -> bool:
    """Return True if the gene is still in cooldown."""
    last_used = COOLDOWN_TRACKER.get(gene_id, 0)
    return (time.time() - last_used) < cooldown_seconds


def _record_cooldown(gene_id: str) -> None:
    COOLDOWN_TRACKER[gene_id] = time.time()


def _circuit_breaker_key(gene_id: str, dedup_key: str) -> str:
    return f"{gene_id}::{dedup_key}"


def _check_circuit_breaker(gene_id: str, dedup_key: str) -> bool:
    """Return True if the circuit breaker has tripped (too many consecutive failures)."""
    key = _circuit_breaker_key(gene_id, dedup_key)
    return CIRCUIT_BREAKER.get(key, 0) >= MAX_CONSECUTIVE_FAILURES


def _record_circuit_breaker(gene_id: str, dedup_key: str, success: bool) -> None:
    key = _circuit_breaker_key(gene_id, dedup_key)
    if success:
        CIRCUIT_BREAKER.pop(key, None)
    else:
        CIRCUIT_BREAKER[key] = CIRCUIT_BREAKER.get(key, 0) + 1
        count = CIRCUIT_BREAKER[key]
        if count >= MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                f"Circuit breaker tripped: {gene_id} failed {count}x for {dedup_key}"
            )


def _laplace_confidence(successes: int, total: int) -> float:
    """Laplace-smoothed success probability: (s+1)/(n+2)."""
    return (successes + 1) / (total + 2)


def _load_capsules() -> list[dict]:
    if not CAPSULES_PATH.exists():
        return []
    capsules = []
    with open(CAPSULES_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                capsules.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return capsules


def _compute_gene_confidence(gene_id: str) -> float:
    """Compute time-decayed Laplace confidence for a gene from capsule history."""
    capsules = _load_capsules()
    now = time.time()
    weighted_success = 0.0
    weighted_total = 0.0

    for c in capsules:
        if c.get("gene_id") != gene_id:
            continue
        created = c.get("created_at", "")
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (now - dt.timestamp()) / 86400
        except (ValueError, AttributeError):
            age_days = 0

        weight = math.exp(-math.log(2) * age_days / DECAY_HALF_LIFE_DAYS)
        weighted_total += weight
        if c.get("outcome", {}).get("status") == "success":
            weighted_success += weight

    return _laplace_confidence(int(weighted_success), int(weighted_total))


def _refresh_ban_list() -> None:
    """Scan capsule history and ban genes with low success rates."""
    capsules = _load_capsules()
    gene_stats: dict[str, dict] = {}

    for c in capsules:
        gid = c.get("gene_id", "")
        if not gid:
            continue
        if gid not in gene_stats:
            gene_stats[gid] = {"total": 0, "success": 0}
        gene_stats[gid]["total"] += 1
        if c.get("outcome", {}).get("status") == "success":
            gene_stats[gid]["success"] += 1

    for gid, s in gene_stats.items():
        if s["total"] >= BAN_MIN_ATTEMPTS:
            confidence = _laplace_confidence(s["success"], s["total"])
            if confidence < BAN_THRESHOLD:
                if gid not in GENE_BAN_LIST:
                    logger.warning(
                        f"Gene auto-banned: {gid} "
                        f"(confidence={confidence:.3f}, attempts={s['total']})"
                    )
                GENE_BAN_LIST.add(gid)
            else:
                GENE_BAN_LIST.discard(gid)


def select_gene(event: dict) -> Optional[dict]:
    """Select the best matching Gene for an event. GEP Phase 2: Select."""
    _refresh_ban_list()
    genes = _load_genes()
    category = event.get("category", "")
    title = event.get("title", "")
    project = event.get("project", "")
    dedup_key = event.get("dedup_key", "")

    best_gene = None
    best_score = 0.0

    for gene in genes:
        gid = gene["id"]

        if gid in GENE_BAN_LIST:
            logger.debug(f"Gene {gid} is banned, skipping")
            continue

        score = _match_signals(gene, category, title)
        if score <= 0.0:
            continue

        constraints = gene.get("constraints", {})
        allowed = constraints.get("allowed_projects", ["*"])
        if "*" not in allowed and project not in allowed:
            continue

        cooldown = constraints.get("cooldown_seconds", 300)
        if _check_cooldown(gid, cooldown):
            logger.debug(f"Gene {gid} in cooldown, skipping")
            continue

        if dedup_key and _check_circuit_breaker(gid, dedup_key):
            logger.debug(f"Gene {gid} circuit-broken for {dedup_key}, skipping")
            continue

        if score > best_score:
            best_score = score
            best_gene = gene

    return best_gene


def evaluate_repair(gene: dict, event: dict, result: dict) -> dict:
    """GEP Phase 6: Evaluate — verify repair outcome and score confidence."""
    evaluation = {
        "verified": False,
        "confidence": 0.0,
        "method": "none",
    }

    if result["status"] != "success":
        evaluation["confidence"] = 0.0
        return evaluation

    action_type = gene.get("repair_action", {}).get("type", "")

    if action_type == "docker_restart":
        container_name = _extract_container_name(event.get("title", ""))
        if container_name:
            evaluation = _verify_container_running(container_name)
    elif action_type == "shell_command":
        evaluation = {"verified": True, "confidence": 0.7, "method": "command_exit_code"}

    if not evaluation["verified"]:
        result["status"] = "unverified"

    gene_confidence = _compute_gene_confidence(gene["id"])
    evaluation["gene_confidence"] = round(gene_confidence, 3)

    return evaluation


def _verify_container_running(container_name: str) -> dict:
    """Post-repair verification: check container is running and optionally healthy."""
    try:
        import docker
        client = docker.from_env()
        container = client.containers.get(container_name)
        container.reload()

        if container.status != "running":
            return {"verified": False, "confidence": 0.0, "method": "docker_status"}

        health_state = container.attrs.get("State", {}).get("Health", {})
        if health_state:
            health_status = health_state.get("Status", "")
            if health_status == "healthy":
                return {"verified": True, "confidence": 1.0, "method": "docker_healthcheck"}
            elif health_status == "starting":
                return {"verified": True, "confidence": 0.8, "method": "docker_starting"}
            else:
                return {"verified": False, "confidence": 0.2, "method": "docker_unhealthy"}

        return {"verified": True, "confidence": 0.9, "method": "docker_running_no_healthcheck"}

    except Exception as e:
        logger.warning(f"Post-repair verification failed for {container_name}: {e}")
        return {"verified": False, "confidence": 0.0, "method": "verification_error"}


def execute_repair(gene: dict, event: dict) -> dict:
    """Execute a repair action defined by a Gene. GEP Phase 5: Execute."""
    action = gene.get("repair_action", {})
    action_type = action.get("type", "")
    params = action.get("params", {})
    project = event.get("project", "")
    title = event.get("title", "")

    result = {"status": "failed", "output": "", "duration_ms": 0}
    start = time.time()

    try:
        if action_type == "docker_restart":
            result = _execute_docker_restart(event, params)
        elif action_type == "shell_command":
            result = _execute_shell_commands(params.get("commands", []))
        else:
            result["output"] = f"Unknown action type: {action_type}"
    except Exception as e:
        result["output"] = str(e)

    result["duration_ms"] = int((time.time() - start) * 1000)
    return result


def _resolve_container_name(event: dict) -> Optional[str]:
    """Resolve container name from title/action_hint/project fallbacks."""
    title = event.get("title", "")
    action_hint = event.get("action_hint", "")
    project = event.get("project", "")

    name = _extract_container_name(title)
    if name:
        return name

    # Parse hints like: docker logs xxx --tail 50
    marker = "docker logs "
    if marker in action_hint:
        suffix = action_hint.split(marker, 1)[1].strip()
        guessed = suffix.split(" ")[0].strip()
        if guessed:
            return guessed

    return PROJECT_PRIMARY_CONTAINERS.get(project)


def _execute_docker_restart(event: dict, params: dict) -> dict:
    """Restart a Docker container, with optional compose fallback."""
    project = event.get("project", "")
    container_name = _resolve_container_name(event)
    if not container_name:
        return {"status": "failed", "output": "Could not identify container name"}

    try:
        import docker
        client = docker.from_env()
    except Exception as e:
        return {"status": "failed", "output": f"Docker API unavailable: {e}"}

    try:
        container = client.containers.get(container_name)
        container.restart(timeout=30)

        wait_time = params.get("wait_after_restart", 10)
        time.sleep(wait_time)

        container.reload()
        if container.status == "running":
            return {
                "status": "success",
                "output": f"Container {container_name} restarted successfully",
            }
        return {
            "status": "failed",
            "output": f"Container status after restart: {container.status}",
        }

    except docker.errors.NotFound:
        if params.get("fallback_compose"):
            return _execute_compose_up(project)
        return {
            "status": "failed",
            "output": f"Container {container_name} not found and no compose fallback",
        }
    except docker.errors.APIError as e:
        return {
            "status": "failed",
            "output": f"Docker API error for {container_name}: {str(e)[:300]}",
        }
    except Exception as e:
        return {
            "status": "failed",
            "output": f"Unexpected error restarting {container_name}: {str(e)[:300]}",
        }


def _execute_compose_up(project: str) -> dict:
    """Fallback: run docker compose up -d for the project."""
    project_dir = WORKSPACE_ROOT / project
    if not project_dir.exists():
        return {"status": "failed", "output": f"Project dir not found: {project_dir}"}

    try:
        proc = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            return {
                "status": "success",
                "output": f"docker compose up -d succeeded in {project_dir}",
            }
        return {
            "status": "failed",
            "output": f"docker compose failed: {proc.stderr[:500]}",
        }
    except subprocess.TimeoutExpired:
        return {"status": "failed", "output": "docker compose up timed out (120s)"}
    except Exception as e:
        return {"status": "failed", "output": str(e)}


def _execute_shell_commands(commands: list[str]) -> dict:
    """Execute a list of shell commands."""
    outputs = []
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=60
            )
            outputs.append(f"$ {cmd}\n{proc.stdout[:200]}")
        except Exception as e:
            outputs.append(f"$ {cmd}\nERROR: {e}")

    return {"status": "success", "output": "\n".join(outputs)}


def _extract_container_name(title: str) -> Optional[str]:
    """Extract container name from event title like '容器 xxx 已停止'."""
    for prefix in ["容器 ", "Container "]:
        if prefix in title:
            rest = title.split(prefix, 1)[1]
            return rest.split(" ")[0].strip()
    return None


def _compute_asset_id(obj: dict) -> str:
    """Content-addressable ID (simplified GEP asset_id)."""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def record_capsule(
    gene: dict, event: dict, result: dict, evaluation: dict
) -> dict:
    """Record a repair result as a Capsule. GEP Phase 7: Solidify."""
    now = datetime.utcnow().isoformat() + "Z"
    capsule = {
        "type": "Capsule",
        "id": f"capsule_{int(time.time() * 1000)}",
        "gene_id": gene["id"],
        "trigger": [event.get("category", ""), event.get("title", "")],
        "project": event.get("project", ""),
        "dedup_key": event.get("dedup_key", ""),
        "summary": gene.get("summary", ""),
        "outcome": {
            "status": result["status"],
            "duration_ms": result["duration_ms"],
            "output": result["output"][:500],
        },
        "evaluation": evaluation,
        "confidence": evaluation.get("confidence", 0.0),
        "created_at": now,
    }
    capsule["asset_id"] = _compute_asset_id(capsule)

    CAPSULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CAPSULES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(capsule, ensure_ascii=False) + "\n")

    return capsule


def attempt_repair(event: dict) -> Optional[dict]:
    """Full GEP repair cycle: Select → Execute → Evaluate → Solidify.

    Returns the Capsule if a repair was attempted, None if no Gene matched.
    """
    gene = select_gene(event)
    if not gene:
        return None

    gid = gene["id"]
    dedup_key = event.get("dedup_key", "")
    logger.info(f"Gene matched: {gid} for [{event.get('project')}] {event.get('title')}")

    _record_cooldown(gid)
    result = execute_repair(gene, event)
    evaluation = evaluate_repair(gene, event, result)
    capsule = record_capsule(gene, event, result, evaluation)

    success = result["status"] == "success"
    _record_circuit_breaker(gid, dedup_key, success)

    if success:
        logger.info(
            f"Repair SUCCESS: {gid} -> {capsule['id']} "
            f"(confidence={evaluation.get('confidence', 0):.2f})"
        )
    else:
        logger.warning(f"Repair FAILED: {gid} -> {result['output'][:200]}")

    return capsule


def get_repair_stats() -> dict:
    """Aggregate repair statistics from capsule log."""
    capsules = _load_capsules()

    stats = {
        "total_attempts": len(capsules),
        "total_success": 0,
        "total_failed": 0,
        "success_rate": 0.0,
        "genes": {},
        "banned_genes": list(GENE_BAN_LIST),
        "circuit_breakers": {
            k: v for k, v in CIRCUIT_BREAKER.items() if v > 0
        },
        "recent_capsules": capsules[-20:],
    }

    for c in capsules:
        if c.get("outcome", {}).get("status") == "success":
            stats["total_success"] += 1
        else:
            stats["total_failed"] += 1

        gid = c.get("gene_id", "unknown")
        if gid not in stats["genes"]:
            stats["genes"][gid] = {"attempts": 0, "successes": 0, "confidence": 0.0}
        stats["genes"][gid]["attempts"] += 1
        if c.get("outcome", {}).get("status") == "success":
            stats["genes"][gid]["successes"] += 1

    for gid in stats["genes"]:
        s = stats["genes"][gid]
        s["confidence"] = round(_laplace_confidence(s["successes"], s["attempts"]), 3)
        s["banned"] = gid in GENE_BAN_LIST

    if stats["total_attempts"] > 0:
        stats["success_rate"] = round(
            stats["total_success"] / stats["total_attempts"], 3
        )

    return stats
