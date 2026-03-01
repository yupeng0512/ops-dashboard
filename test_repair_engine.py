"""
Minimal smoke test for the GEP Repair Engine.

Verifies the core logic loop: event → gene match → capsule record
without requiring Docker or a running server.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import repair_engine


def test_gene_loading():
    """Verify genes load from JSON files."""
    genes = repair_engine._load_genes()
    assert len(genes) >= 4, f"Expected ≥4 genes, got {len(genes)}"
    gene_ids = {g["id"] for g in genes}
    expected = {"gene_container_restart", "gene_container_unhealthy_restart",
                "gene_health_check_restart", "gene_disk_space_cleanup"}
    assert expected.issubset(gene_ids), f"Missing genes: {expected - gene_ids}"
    print(f"  ✓ Loaded {len(genes)} genes: {', '.join(gene_ids)}")


def test_signal_matching():
    """Verify signal matching logic."""
    event_container_stopped = {
        "project": "infohunter",
        "category": "container_stopped",
        "title": "容器 infohunter 已停止",
        "level": "critical",
    }
    gene = repair_engine.select_gene(event_container_stopped)
    assert gene is not None, "No gene matched for container_stopped"
    assert gene["id"] == "gene_container_restart", f"Wrong gene: {gene['id']}"
    print(f"  ✓ container_stopped → {gene['id']}")

    event_unhealthy = {
        "project": "trendradar",
        "category": "container_unhealthy",
        "title": "容器 trendradar 健康检查失败",
        "level": "warning",
    }
    gene2 = repair_engine.select_gene(event_unhealthy)
    assert gene2 is not None, "No gene matched for container_unhealthy"
    assert gene2["id"] == "gene_container_unhealthy_restart", f"Wrong gene: {gene2['id']}"
    print(f"  ✓ container_unhealthy → {gene2['id']}")

    event_health = {
        "project": "infohunter",
        "category": "connection_failed",
        "title": "infohunter 健康检查不可达",
        "level": "warning",
    }
    gene3 = repair_engine.select_gene(event_health)
    assert gene3 is not None, "No gene matched for connection_failed"
    assert gene3["id"] == "gene_health_check_restart", f"Wrong gene: {gene3['id']}"
    print(f"  ✓ connection_failed → {gene3['id']}")

    event_disk = {
        "project": "ops-dashboard",
        "category": "disk_space_low",
        "title": "磁盘空间不足",
        "level": "warning",
    }
    gene4 = repair_engine.select_gene(event_disk)
    assert gene4 is not None, "No gene matched for disk_space_low"
    assert gene4["id"] == "gene_disk_space_cleanup", f"Wrong gene: {gene4['id']}"
    print(f"  ✓ disk_space_low → {gene4['id']}")

    event_unmatched = {
        "project": "test",
        "category": "unknown_issue",
        "title": "Something random happened",
        "level": "info",
    }
    gene_none = repair_engine.select_gene(event_unmatched)
    assert gene_none is None, f"Unexpected match: {gene_none['id']}"
    print("  ✓ unknown signal → no match (correct)")


def test_cooldown():
    """Verify cooldown mechanism prevents rapid re-triggering."""
    repair_engine.COOLDOWN_TRACKER.clear()

    event = {
        "project": "test",
        "category": "container_stopped",
        "title": "容器 test-container 已停止",
        "level": "critical",
    }
    gene1 = repair_engine.select_gene(event)
    assert gene1 is not None
    repair_engine._record_cooldown(gene1["id"])

    gene2 = repair_engine.select_gene(event)
    assert gene2 is None, "Gene should be in cooldown"
    print("  ✓ Cooldown blocks re-selection")

    repair_engine.COOLDOWN_TRACKER.clear()


def test_capsule_recording():
    """Verify capsule recording to JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_path = repair_engine.CAPSULES_PATH
        repair_engine.CAPSULES_PATH = Path(tmpdir) / "test_capsules.jsonl"

        gene = {"id": "gene_test", "summary": "Test gene"}
        event = {"project": "test", "category": "test_event", "title": "Test"}
        result = {"status": "success", "duration_ms": 42, "output": "ok"}

        capsule = repair_engine.record_capsule(gene, event, result)

        assert capsule["type"] == "Capsule"
        assert capsule["gene_id"] == "gene_test"
        assert capsule["outcome"]["status"] == "success"
        assert capsule["confidence"] == 1.0
        assert "asset_id" in capsule
        assert capsule["asset_id"].startswith("sha256:")
        print(f"  ✓ Capsule recorded: {capsule['id']}")

        with open(repair_engine.CAPSULES_PATH) as f:
            lines = f.readlines()
        assert len(lines) == 1
        saved = json.loads(lines[0])
        assert saved["gene_id"] == "gene_test"
        print("  ✓ Capsule persisted to JSONL")

        repair_engine.CAPSULES_PATH = original_path


def test_container_name_extraction():
    """Verify container name extraction from event titles."""
    assert repair_engine._extract_container_name("容器 infohunter 已停止") == "infohunter"
    assert repair_engine._extract_container_name("容器 digital-twin-neo4j 健康检查失败") == "digital-twin-neo4j"
    assert repair_engine._extract_container_name("Container my-app stopped") == "my-app"
    assert repair_engine._extract_container_name("Random title") is None
    print("  ✓ Container name extraction works correctly")


def test_repair_stats():
    """Verify stats aggregation from capsule log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_path = repair_engine.CAPSULES_PATH
        repair_engine.CAPSULES_PATH = Path(tmpdir) / "test_capsules.jsonl"

        gene = {"id": "gene_test", "summary": "Test"}
        for i, status in enumerate(["success", "success", "failed"]):
            event = {"project": "p", "category": "c", "title": f"T{i}"}
            result = {"status": status, "duration_ms": 10, "output": ""}
            repair_engine.record_capsule(gene, event, result)

        stats = repair_engine.get_repair_stats()
        assert stats["total_attempts"] == 3
        assert stats["total_success"] == 2
        assert stats["total_failed"] == 1
        assert abs(stats["success_rate"] - 0.667) < 0.01
        assert stats["genes"]["gene_test"]["attempts"] == 3
        assert stats["genes"]["gene_test"]["successes"] == 2
        print(f"  ✓ Stats: {stats['total_attempts']} attempts, {stats['success_rate']:.1%} success rate")

        repair_engine.CAPSULES_PATH = original_path


def main():
    print("\n=== GEP Repair Engine Smoke Tests ===\n")

    tests = [
        ("Gene Loading", test_gene_loading),
        ("Signal Matching", test_signal_matching),
        ("Cooldown Mechanism", test_cooldown),
        ("Capsule Recording", test_capsule_recording),
        ("Container Name Extraction", test_container_name_extraction),
        ("Repair Stats", test_repair_stats),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"[{name}]")
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
        print()

    print(f"{'=' * 40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All tests passed! GEP repair loop verified.")
    return failed


if __name__ == "__main__":
    sys.exit(main())
