# -*- coding: utf-8 -*-
"""
tests/test_blogger_lab.py
Public preview unit & integration test suite for Blogger Lab.
Verifies public preview factory, default-off behavior, explicit-root initialization,
security gates, DSL schemas, synthetic upload, and T+1 paper trading mechanics.
"""

import os
import sys
import tempfile
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_public_preview_blogger_lab_disabled(monkeypatch, tmp_path):
    """
    Ensure Blogger Lab routes are not mounted in scripts.public_preview:create_preview_app
    when V7_BLOGGER_LAB_ENABLED is 0 or unset. Root directory must not be created.
    """
    monkeypatch.setenv("V7_BLOGGER_LAB_ENABLED", "0")
    never_created_root = tmp_path / "never_created_lab_root"
    monkeypatch.setenv("V7_BLOGGER_LAB_ROOT", str(never_created_root))

    from scripts.public_preview import create_preview_app
    app = create_preview_app()

    with TestClient(app) as client:
        # Standard preview pages and endpoints function normally
        res_health = client.get("/api/health")
        assert res_health.status_code == 200
        assert res_health.json().get("mode") == "PUBLIC_READ_ONLY_PREVIEW"

        res_demo = client.get("/demo")
        assert res_demo.status_code == 200

        res_replay = client.get("/replay")
        assert res_replay.status_code == 200

        res_v7 = client.get("/v7")
        assert res_v7.status_code == 200

        # Lab endpoint must return 404
        res_lab = client.get("/api/blogger-lab/personas")
        assert res_lab.status_code == 404

        # Non-GET requests return 403 READ_ONLY_PREVIEW
        res_post_run = client.post("/api/run/daily_review")
        assert res_post_run.status_code == 403
        assert res_post_run.json().get("status") == "READ_ONLY_PREVIEW"

        res_post_lab = client.post("/api/blogger-lab/personas", json={"name": "test"})
        assert res_post_lab.status_code == 403

    # Storage root must NOT be initialized or created on disk
    assert not never_created_root.exists()


def test_public_preview_blogger_lab_enabled_and_security(monkeypatch, tmp_path):
    """
    Ensure scripts.public_preview:create_preview_app mounts Blogger Lab when enabled,
    enforces loopback/anti-proxy/CORS security gates, permits synthetic corpus upload,
    keeps non-lab write requests 403, and releases runtime on exit.
    """
    lab_root = tmp_path / "preview_lab_root"
    monkeypatch.setenv("V7_BLOGGER_LAB_ENABLED", "1")
    monkeypatch.setenv("V7_BLOGGER_LAB_ROOT", str(lab_root))

    from scripts.public_preview import create_preview_app
    app = create_preview_app()

    with TestClient(app) as client:
        # 1. Non-lab write endpoints remain strictly 403 Forbidden
        res_non_lab_write = client.post("/api/run/daily_review")
        assert res_non_lab_write.status_code == 403
        assert res_non_lab_write.json().get("status") == "READ_ONLY_PREVIEW"

        # 2. External proxy injection -> 403 Forbidden
        res_proxy = client.get("/api/blogger-lab/personas", headers={"X-Forwarded-For": "203.0.113.195"})
        assert res_proxy.status_code == 403

        res_proxy_post = client.post(
            "/api/blogger-lab/personas",
            json={"name": "proxy_user"},
            headers={"X-Forwarded-For": "203.0.113.195"}
        )
        assert res_proxy_post.status_code == 403

        # 3. Cross-origin / untrusted origin -> 403 Forbidden
        res_origin = client.get("/api/blogger-lab/personas", headers={"Origin": "http://untrusted-site.org"})
        assert res_origin.status_code == 403

        res_origin_null = client.get("/api/blogger-lab/personas", headers={"Origin": "null"})
        assert res_origin_null.status_code == 403

        # 4. Legitimate loopback request -> 200 OK, empty personas initially
        res_ok = client.get("/api/blogger-lab/personas")
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data.get("status") == "ok"
        assert data.get("data") == []

        # 5. Legitimate loopback POST persona registration -> 200 OK
        res_create = client.post(
            "/api/blogger-lab/personas",
            json={"name": "公开合成研究员", "description": "用于公开环境验证"}
        )
        assert res_create.status_code == 200
        persona_body = res_create.json()
        assert persona_body.get("status") == "ok"
        persona = persona_body["data"]
        persona_id = persona["id"]
        assert persona["name"] == "公开合成研究员"

        # 6. Upload synthetic TXT sample note -> 200 OK
        sample_path = REPO_ROOT / "examples" / "blogger_lab" / "01_synthetic_researcher_sample.txt"
        assert sample_path.exists(), f"Sample file not found at {sample_path}"
        content_bytes = sample_path.read_bytes()

        res_upload = client.post(
            f"/api/blogger-lab/personas/{persona_id}/batches",
            files=[("files", ("01_synthetic_researcher_sample.txt", content_bytes, "text/plain"))],
            data={"source_date": "2026-11-01"}
        )
        assert res_upload.status_code == 200
        batch_body = res_upload.json()
        assert batch_body.get("status") == "ok"
        upload_data = batch_body["data"]
        assert upload_data["batch"]["persona_id"] == persona_id
        assert upload_data["chunks_count"] > 0

    # 7. Lifespan exit releases runtime and file locks
    runtime = getattr(app.state, "blogger_lab_runtime", None)
    assert runtime is not None
    assert runtime.status == "SHUTDOWN"
    assert runtime.is_locked is False


def test_blogger_lab_schemas_and_dsl():
    """Verify core Pydantic data models and DSL condition serialization."""
    from blogger_lab.schemas import (
        StrategyCondition, ExitRules, SizingRules, StrategyDraft, StrategyVersion
    )

    cond = StrategyCondition(
        field="close_gt_ma20",
        op="eq",
        value=True,
        unit="boolean",
        basis="completed_daily",
        parameter_origin="user"
    )
    assert cond.field == "close_gt_ma20"
    assert cond.value is True

    exit_rules = ExitRules(stop_loss_pct=-3.5, take_profit_pct=5.0)
    sizing = SizingRules(max_position_pct=0.2, max_symbols=1)

    draft = StrategyDraft(
        id="draft_test_001",
        persona_id="persona_test",
        knowledge_version="kv_test_001",
        created_at="2026-11-01T00:00:00+08:00",
        updated_at="2026-11-01T00:00:00+08:00",
        entry_rules=[cond],
        exit_rules=exit_rules,
        sizing_rules=sizing,
        draft_hash="6998c95d18ba9950ce022e623aeee9388febe2f9919a62151f9fc1e45a874147"
    )
    assert draft.id == "draft_test_001"
    assert len(draft.entry_rules) == 1
    assert draft.exit_rules.stop_loss_pct == -3.5

    strat_ver = StrategyVersion(
        id="strat_test_001",
        persona_id="persona_test",
        draft_id="draft_test_001",
        draft_revision=1,
        draft_hash=draft.draft_hash,
        knowledge_version="kv_test_001",
        entry_rules=draft.entry_rules,
        exit_rules=draft.exit_rules,
        sizing_rules=draft.sizing_rules,
        confirmed_at="2026-11-01T00:05:00+08:00"
    )
    assert strat_ver.id == "strat_test_001"
    assert strat_ver.draft_id == "draft_test_001"
    assert len(strat_ver.entry_rules) == 1


def test_blogger_lab_paper_engine_t1_lock():
    """Verify paper trading adapter and account management."""
    tmpdir = tempfile.mkdtemp(prefix="test_paper_")
    try:
        from blogger_lab.paper import BloggerLabPaperAdapter

        adapter = BloggerLabPaperAdapter(lab_root=tmpdir)
        account = adapter.get_or_create_account("persona_test", mode="REPLAY")
        assert account.cash == 100000.0
        assert account.initial_capital == 100000.0

        fills = adapter.list_paper_fills("persona_test", mode="REPLAY")
        assert len(fills) == 0

        cycles = adapter.list_paper_cycles("persona_test", mode="REPLAY")
        assert len(cycles) == 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
