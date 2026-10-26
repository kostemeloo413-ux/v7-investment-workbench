# -*- coding: utf-8 -*-
"""Isolated A3 read-only picks/stock View Model tests."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "scripts" / "daemon"))

from app import create_app
from daemon.v7_picks import build_ai_quant_evidence, build_picks_view, build_stock_view

SH = ZoneInfo("Asia/Shanghai")


def pick_row(pid, code="600519", action="BUY", failed=None):
    legacy = {"P-LIAO-XIAOAI": "liao-xiaoai", "P-COOL": "cool", "P-YINWENTAO": "yintwentao",
              "P-HANSHU": "hanshu", "P-RULE-QUANT": "ai-alpha", "P-RISK-SENTINEL": "anti-demo"}[pid]
    return {"participant_id": pid, "blogger_id": legacy, "stock_code": code,
            "stock_name": "贵州茅台", "signal": "RISK_ALERT" if pid == "P-RISK-SENTINEL" else None,
            "current_price": 1500.0, "skill_decision": {
                "action": action, "timestamp": "2026-09-23T16:00:00+08:00", "decision_id": pid + "-d1",
                "decision_reason": "观点文本", "candidate_evidence": {"price": 1500, "MA20": None},
                "skill_rules_triggered": ["rule-a"], "skill_rules_failed": failed or [],
                "position_plan": {"target_weight": 0.1}, "exit_plan": {"stop_loss": 1400},
                "holding_horizon": "5 sessions", "skill_version": "v2"}}


def create_db(path, pulses=()):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ai_quant_predictions (
          id INTEGER PRIMARY KEY, predict_time TEXT, trade_date TEXT, symbol TEXT, name TEXT,
          rank INTEGER, raw_score REAL, up_probability REAL, expected_return REAL,
          expected_mae REAL, right_side_score REAL, expected_holding_days INTEGER, model_version TEXT);
        CREATE TABLE instrument_master (
          symbol TEXT, name TEXT, full_symbol TEXT, exchange TEXT, board TEXT, list_date TEXT,
          delist_date TEXT, is_st INTEGER, is_suspended INTEGER, updated_at TEXT);
        CREATE TABLE daily_kline (
          code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
          amount REAL, pct_chg REAL, source TEXT, updated_at TEXT);
    """)
    conn.execute("INSERT INTO instrument_master VALUES ('600519','贵州茅台','600519.SH','SH','MAIN',NULL,NULL,0,0,'2026-09-23')")
    conn.execute("INSERT INTO daily_kline VALUES ('600519','20260922',1490,1510,1480,1500,100,200,'1','legacy','2026-09-22')")
    for trade_date, predict_time, model, nrows in pulses:
        for rank in range(1, nrows + 1):
            conn.execute("INSERT INTO ai_quant_predictions (predict_time,trade_date,symbol,name,rank,raw_score,up_probability,expected_return,expected_mae,right_side_score,expected_holding_days,model_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (predict_time, trade_date, f"{600519 + rank:06d}", "样本", rank, 0.8, .7, .05, -.03, 75, 5, model))
    conn.commit()
    conn.close()


class TestPicksAndStockReadModels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.picks = self.root / "data.json"
        self.db = self.root / "market.db"
        self.control = self.root / "CURRENT_TASK.md"
        self.control.write_text("GATE_B: OPEN\nG6: OPEN\n", encoding="utf-8")
        self.rows = [pick_row(pid) for pid in ("P-LIAO-XIAOAI", "P-COOL", "P-YINWENTAO", "P-HANSHU", "P-RULE-QUANT", "P-RISK-SENTINEL")]
        self.write_picks("2026-09-23 16:30:00")

    def tearDown(self):
        self.tmp.cleanup()

    def write_picks(self, timestamp, rows=None):
        self.picks.write_text(json.dumps({"picks_updated_at": timestamp, "blogger_picks": rows or self.rows}, ensure_ascii=False), encoding="utf-8")

    def view(self, **kwargs):
        return build_picks_view(self.picks, self.db, self.control, now=datetime(2026, 9, 23, 16, 40, tzinfo=SH),
                                current_trade_date="20260923", is_trading_day=lambda _d: True, **kwargs)

    def test_current_persisted_roles_are_six_and_not_execution_authorized(self):
        create_db(self.db)
        view = self.view()
        data = view["data"]
        self.assertEqual(len(data["participants"]), 6)
        self.assertEqual({p["participant_id"] for p in data["participants"]},
                         {"P-LIAO-XIAOAI", "P-COOL", "P-YINWENTAO", "P-HANSHU", "P-RULE-QUANT", "P-AI-QUANT"})
        self.assertTrue(all(p["execution_authorized"] is False for p in data["participants"]))
        self.assertEqual(data["participants"][0]["source_type"], "LEGACY_PRODUCTION_SCANNER")
        self.assertEqual(data["participants"][0]["data_admission"], "NOT_YET_MIGRATED_TO_P1C3")

    def test_old_scan_is_stale_historical_not_today(self):
        create_db(self.db)
        self.write_picks("2026-09-22 16:30:00")
        data = self.view()["data"]
        self.assertEqual(data["data_status"], "STALE")
        self.assertEqual(data["scan_date_status"], "HISTORICAL")

    def test_missing_and_corrupt_persisted_files_are_explicit(self):
        create_db(self.db)
        missing = build_picks_view(self.root / "absent.json", self.db, self.control,
                                   current_trade_date="20260923", is_trading_day=lambda _d: True)
        self.assertEqual(missing["status"], "UNAVAILABLE")
        self.picks.write_text("{broken", encoding="utf-8")
        corrupt = self.view()
        self.assertEqual(corrupt["status"], "INVALID")

    def test_han_and_cool_missing_evidence_remain_no_signal(self):
        create_db(self.db)
        rows = [pick_row("P-HANSHU", action="NO_SIGNAL", failed=["MA20_UNAVAILABLE"]),
                pick_row("P-COOL", action="NO_SIGNAL", failed=["INTRADAY_STRUCTURE_UNAVAILABLE"])]
        self.write_picks("2026-09-23 16:30:00", rows)
        participants = {p["participant_id"]: p for p in self.view()["data"]["participants"]}
        self.assertEqual(participants["P-HANSHU"]["action"], "NO_SIGNAL")
        self.assertIn("MA20_UNAVAILABLE", participants["P-HANSHU"]["skill_rules_failed"])
        self.assertEqual(participants["P-COOL"]["action"], "NO_SIGNAL")
        self.assertIn("INTRADAY_STRUCTURE_UNAVAILABLE", participants["P-COOL"]["skill_rules_failed"])

    def test_sentinel_never_enters_return_roles_or_has_trading_authority(self):
        create_db(self.db)
        data = self.view()["data"]
        sentinel = data["risk_sentinel"]
        self.assertNotIn("P-RISK-SENTINEL", {p["participant_id"] for p in data["participants"]})
        self.assertEqual((sentinel["capital"], sentinel["trading_enabled"], sentinel["ranking_enabled"]), (0, False, False))

    def test_latest_historical_prediction_pulse_stays_historical(self):
        create_db(self.db, [("20260915", "2026-09-15T10:15:42", "AIQ-V1.0", 5)])
        result = build_ai_quant_evidence(self.db, current_trade_date="20260923", is_trading_day=lambda _d: True)
        self.assertEqual(result["classification"], "LATEST_HISTORICAL_PREDICTION")
        self.assertEqual(result["pulse"]["trade_date"], "20260915")
        self.assertEqual(len(result["predictions"]), 5)

    def test_two_incomplete_pulses_are_not_combined(self):
        create_db(self.db, [("20260923", "2026-09-23T09:35:00", "M1", 2),
                            ("20260923", "2026-09-23T09:40:00", "M1", 3)])
        result = build_ai_quant_evidence(self.db, current_trade_date="20260923", is_trading_day=lambda _d: True)
        self.assertEqual(result["classification"], "NO_COMPLETE_PREDICTION_PULSE")
        self.assertEqual(result["predictions"], [])

    def test_stock_detail_returns_independent_personas_and_research_bar(self):
        create_db(self.db)
        result = build_stock_view("600519", self.picks, self.db, current_trade_date="20260923",
                                  is_trading_day=lambda _d: True,
                                  news_items=[{"id":"n1", "title":"贵州茅台公告", "entities":["600519"], "url":"javascript:alert(1)"}])
        self.assertEqual(result["data"]["identity"]["symbol"], "600519")
        self.assertFalse(result["data"]["execution_authorized"])
        self.assertTrue(result["data"]["quote_evidence"]["source_note"].startswith("LEGACY_FIELD_PROVENANCE"))
        self.assertEqual(result["data"]["news"]["status"], "AVAILABLE")
        self.assertIsNone(result["data"]["news"]["items"][0].get("url"))

    def test_invalid_and_no_evidence_symbols_are_distinguished(self):
        invalid = build_stock_view("../../", self.picks, self.db)
        absent = build_stock_view("123456", self.picks, self.db)
        self.assertEqual(invalid["status"], "INVALID")
        self.assertIn(absent["status"], ("EMPTY", "STALE"))
        self.assertFalse(absent["data"]["execution_authorized"])

    def test_direct_news_match_does_not_infer_from_unrelated_article(self):
        create_db(self.db)
        result = build_stock_view("600519", self.root / "missing.json", self.db,
                                  news_items=[{"title":"宏观行业消息", "entities":["银行"]}])
        self.assertEqual(result["data"]["news"]["reason_code"], "NO_DIRECT_NEWS_MATCH")

    def test_api_routes_are_read_only_and_expose_authorization_false(self):
        create_db(self.db)
        from daemon import v7_api
        original_picks, original_db = v7_api.build_picks_view, v7_api.MARKET_DB_PATH
        try:
            v7_api.MARKET_DB_PATH = str(self.db)
            v7_api.build_picks_view = lambda: build_picks_view(self.picks, self.db, self.control,
                now=datetime(2026,9,23,16,40,tzinfo=SH), current_trade_date="20260923", is_trading_day=lambda _d: True)
            client = TestClient(create_app())
            response = client.get("/api/v7/picks")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["data"]["execution_authorized"])
            self.assertEqual(client.get("/api/v7/stock/nope").status_code, 200)
        finally:
            v7_api.build_picks_view, v7_api.MARKET_DB_PATH = original_picks, original_db

    def test_picks_file_not_modified_by_read_model(self):
        create_db(self.db)
        before = self.picks.read_bytes()
        self.view()
        self.assertEqual(self.picks.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
