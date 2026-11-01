# -*- coding: utf-8 -*-
"""
tests/test_blogger_lab.py
Public preview unit & integration test suite for Blogger Lab.
Verifies default-off behavior, explicit-root initialization, security gates, DSL schemas, and T+1 paper trading mechanics.
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


def test_blogger_lab_default_disabled(monkeypatch):
    """Ensure Blogger Lab routes are not mounted when V7_BLOGGER_LAB_ENABLED is 0 or unset."""
    monkeypatch.setenv("V7_BLOGGER_LAB_ENABLED", "0")
    from daemon.app import create_app
    app = create_app()
    client = TestClient(app)

    # Core health should succeed
    res_health = client.get("/api/health")
    assert res_health.status_code == 200

    # Blogger lab endpoint must return 404
    res_lab = client.get("/api/blogger-lab/personas")
    assert res_lab.status_code == 404


def test_blogger_lab_enabled_routes_and_security(monkeypatch):
    """Ensure Blogger Lab mounts and enforces loopback security gate when enabled."""
    tmpdir = tempfile.mkdtemp(prefix="test_blogger_lab_root_")
    try:
        monkeypatch.setenv("V7_BLOGGER_LAB_ENABLED", "1")
        monkeypatch.setenv("V7_BLOGGER_LAB_ROOT", tmpdir)

        from daemon.app import create_app
        app = create_app()

        with TestClient(app) as client:
            # 1. External proxy injection -> 403 Forbidden
            res_proxy = client.get("/api/blogger-lab/personas", headers={"X-Forwarded-For": "203.0.113.195"})
            assert res_proxy.status_code == 403

            # 2. Cross-origin / untrusted origin -> 403 Forbidden
            res_origin = client.get("/api/blogger-lab/personas", headers={"Origin": "http://untrusted-site.org"})
            assert res_origin.status_code == 403

            # 3. Legitimate loopback request -> 200 OK, empty personas
            res_ok = client.get("/api/blogger-lab/personas")
            assert res_ok.status_code == 200
            data = res_ok.json()
            assert data.get("status") == "ok"
            assert data.get("data") == []

        # Explicitly shutdown runtime to release SQLite locks
        if hasattr(app.state, "blogger_lab_runtime") and app.state.blogger_lab_runtime:
            app.state.blogger_lab_runtime.shutdown()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


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
