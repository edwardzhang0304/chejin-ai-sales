"""Real incident and resource producers -> restart -> sole desktop ZIP.

Synthetic original-send fixture and historical volume are boundary setup, not a
claim that real WeChat sent anything. No live account or network is involved.
"""

import hashlib
import json
import os
import time
import zipfile
from dataclasses import replace

from PIL import Image

from chejin_worker_client import incident_evidence
from chejin_worker_client import diagnostic_bundle as bundle
from chejin_worker_client.diagnostic_bundle import Collector
from test_failure_evidence_boundaries import home
from test_performance_evidence import FAST, monitors, start
from test_performance_review_closure import desktop


def test_current_incident_and_unknown_proof_precede_normal_performance(
    home, monitors, monkeypatch
):
    # Actual sampler produces normal records, then actually stops and restarts.
    policy = replace(FAST, cpu_percent=10000, growth_bytes=2**40)
    monitor = start(home, monitors, policy)
    time.sleep(0.6)
    monitor.close()
    monitor = start(home, monitors, policy)
    time.sleep(0.6)
    monitor.close()

    # Retained original-send boundary fixture, never a physical send.
    proof = home / "transactions/hook/sends/synthetic-action"
    proof.mkdir(parents=True)
    (proof / "intent.json").write_text(
        json.dumps({"reply_action_id": "synthetic-action", "task_id": "synthetic-task"})
    )
    (proof / "outcome.json").write_text(
        json.dumps({"result": "unknown", "automatic_resend_allowed": False})
    )
    scene = home / "artifacts/tasks/synthetic-task/screenshots/fault.png"
    scene.parent.mkdir(parents=True)
    Image.new("RGB", (30, 30), "blue").save(scene)
    try:
        raise RuntimeError("SYNTHETIC_RESOURCE_FAULT")
    except RuntimeError as exc:
        request = incident_evidence.schedule_incident(
            event="synthetic_resource_fault",
            error_code=str(exc),
            message="controlled isolated fault",
            task_id="synthetic-task",
            metadata={"screenshot_path": str(scene), "error_type": type(exc).__name__},
        )
    path = incident_evidence.wait_for_incident(request["incident_id"], timeout=10)
    assert path and path.suffix == ".zip"
    incident_evidence.stop_incident_worker(wait=True)
    # Old directory volume may be newer by mtime; it is not current fault fact.
    for n in range(450):
        old = path.parent / f"INC-historical-{n}.zip"
        with zipfile.ZipFile(old, "w") as z:
            z.writestr("manifest.json", json.dumps({"padding": "o" * 12000}))
        os.utime(old, (time.time() + 100, time.time() + 100))
    originals = [path, scene, *proof.glob("*.json"), home / "worker_client.sqlite3"]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
    full = desktop(home, monkeypatch)
    prefix = "incidents/current/" + request["incident_id"] + "/"
    essentials = [
        prefix + "manifest.json",
        prefix + "occurrences/initial.json",
        prefix + "evidence-index/initial.json",
    ]
    assert all(x in full for x in essentials)
    evidence = json.loads(full[prefix + "evidence-index/initial.json"])
    scenes = [prefix + n for n in evidence["origin_screenshots"]]
    assert scenes and all(n in full for n in scenes)
    # Exact transformed size before optional PE plus a small remainder. Restoring
    # the old call order spends this on PE and must lose current fault members.
    budget = 512
    for name, raw in full.items():
        if name.startswith("performance/"):
            break
        budget += len(raw)
    limited = desktop(home, monkeypatch, max_bytes=budget)
    assert all(x in limited for x in essentials)
    assert all(n in limited for n in scenes)
    assert "receipts/hook/sends/synthetic-action/intent.json" in limited
    assert "receipts/hook/sends/synthetic-action/outcome.json" in limited
    manifest = json.loads(limited["收集清单.json"])
    assert any(
        e.get("reason") == "segment_not_exported" and "first" in e and "last" in e
        for e in manifest["sources"]
    )
    with monkeypatch.context() as patch:
        patch.setattr(Collector, "priority_incidents", lambda *args: None)
        old_order = desktop(home, patch, max_bytes=budget)
    assert not all(x in old_order for x in essentials)
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals} == before


def test_priority_incident_reference_and_budget_boundaries(tmp_path, monkeypatch):
    root = tmp_path / "artifacts/incidents"
    root.mkdir(parents=True)
    (root / "latest.json").write_text(
        json.dumps({"incident_id": "INC-current", "path": "/ignored/untrusted/path"})
    )
    for incident in ("INC-current", "INC-linked", "INC-unrelated"):
        with zipfile.ZipFile(root / (incident + ".zip"), "w") as z:
            z.writestr("manifest.json", "{}")
    collector = Collector(tmp_path, tmp_path / "selection.zip")
    collector.latest_logs = [
        {"task_id": "task", "metadata": {"incident_id": "INC-linked"}}
    ]
    with zipfile.ZipFile(collector.destination, "w") as z:
        collector.priority_incidents(z)
    assert "incidents/current/INC-current/manifest.json" in collector.members
    assert "incidents/current/INC-linked/manifest.json" in collector.members
    assert not any("INC-unrelated" in n for n in collector.members)
    for limit in ("time", "files"):
        collector = Collector(
            tmp_path,
            tmp_path / (limit + ".zip"),
            max_seconds=0 if limit == "time" else 45,
        )
        if limit == "files":
            monkeypatch.setattr(bundle, "MAX_FILES", 0)
        with zipfile.ZipFile(collector.destination, "w") as z:
            collector.priority_incidents(z)
        assert not collector.members
        assert any(
            e.get("reason") == "priority_time_or_file_limit" for e in collector.entries
        )


def test_priority_incident_missing_reference_is_partial(tmp_path):
    collector = Collector(tmp_path, tmp_path / "missing.zip")
    collector.latest_logs = [{"metadata": {"incident_id": "INC-missing"}}]
    with zipfile.ZipFile(collector.destination, "w") as z:
        collector.priority_incidents(z)
    assert any(
        e["status"] == "read_failed"
        and e.get("reason") == "referenced_incident_unavailable_or_pending"
        for e in collector.entries
    )
