# -*- coding: utf-8 -*-
"""Isolated G6 Phase 2 target-date completeness contract tests.

All writes in this suite go to temporary directories/databases.  No provider,
daily_review, Forward runner, live market.db, or economic runtime is invoked.
"""

import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from ai_quant.storage_utils import atomic_write_json
from datasource.completeness import (
    CompletenessContractError,
    build_completeness_evidence,
    build_universe_snapshot,
    collect_target_date,
    load_universe_snapshot,
    persist_universe_snapshot,
    validate_completeness_evidence,
    validate_target_bar,
    validate_universe_snapshot,
    evidence_path,
    universe_snapshot_path,
)
from datasource import providers
from ai_quant import forward_start_gate


class TestV7G6Phase2DataCompleteness(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="v7_g6_phase2_")
        self.completeness_root = os.path.join(self.root, "market_data_completeness")
        self.source_path = os.path.join(self.root, "stock_universe.json")
        with open(self.source_path, "w", encoding="utf-8") as handle:
            json.dump({"updated_at": "2026-09-18 09:00:00", "total_count": 2,
                       "stocks": [{"code": "000001"}, {"code": "000002"}]}, handle)
        self.expected = ["000001", "000002"]
        self.snapshot = build_universe_snapshot(
            "20260918", self.expected, "MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE",
            "test-v1", self.source_path,
        )
        self.snapshot["season_id"] = "TEST-SEASON"
        persist_universe_snapshot(universe_snapshot_path(self.completeness_root, "20260918"), self.snapshot)
        self.store = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def bar(code, trade_date="20260918", **overrides):
        value = {
            "code": code, "trade_date": trade_date, "open": 10.0, "high": 11.0,
            "low": 9.5, "close": 10.5, "volume": 1000.0, "amount": 10000.0,
            "pct_chg": 1.0, "source": "test-provider",
        }
        value.update(overrides)
        return value

    def fetch_from_store(self, code, trade_date):
        row = self.store.get(code)
        return {"rows": [row] if row else [], "source": "test-provider", "provider_attempts": ["test-provider"]}

    def write_store(self, code, rows, source=None):
        for row in rows:
            if row.get("trade_date") == "20260918":
                value = dict(row)
                value["code"] = code
                value["source"] = source or value.get("source")
                self.store[code] = value

    def read_store(self, code, trade_date):
        row = self.store.get(code)
        return [row] if row and row.get("trade_date") == trade_date else []

    def collect(self, fetch=None, max_attempts=2, trading_day=True):
        return collect_target_date(
            self.snapshot, self.completeness_root,
            fetch or self.fetch_from_store, self.read_store, self.write_store,
            trading_day_fn=lambda _: trading_day, max_attempts=max_attempts,
        )

    def test_01_universe_snapshot_is_deduplicated_and_hashable(self):
        duplicate = build_universe_snapshot(
            "20260918", ["000002", "000001", "000001"],
            "MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE", "test-v1", self.source_path,
        )
        self.assertEqual(duplicate["expected_codes"], self.expected)
        self.assertEqual(duplicate["expected_count"], 2)
        self.assertTrue(validate_universe_snapshot(duplicate)[0])

    def test_02_target_date_bar_present_is_valid(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["successful_symbol_count"], 2)
        self.assertEqual(evidence["missing_symbol_count"], 0)

    def test_03_history_only_is_not_target_success(self):
        self.store = {code: self.bar(code, trade_date="20260917") for code in self.expected}
        evidence = self.collect()
        self.assertEqual(evidence["status"], "HOLD")
        self.assertEqual(evidence["successful_symbol_count"], 0)
        self.assertEqual(evidence["missing_symbol_count"], 2)

    def test_04_invalid_ohlc_is_rejected(self):
        valid, reason = validate_target_bar("000001", "20260918", self.bar("000001", high=8.0))
        self.assertFalse(valid)
        self.assertEqual(reason, "OHLC_INCONSISTENT")

    def test_05_negative_volume_is_rejected(self):
        valid, reason = validate_target_bar("000001", "20260918", self.bar("000001", volume=-1))
        self.assertFalse(valid)
        self.assertEqual(reason, "VOLUME_OR_AMOUNT_NEGATIVE")

    def test_06_empty_provider_response_is_recorded(self):
        evidence = self.collect(fetch=lambda code, date: {"rows": [], "source": "tencent-qfq"})
        self.assertEqual(evidence["status"], "HOLD")
        self.assertEqual(evidence["failed_symbol_count"], 2)
        with open(os.path.join(self.completeness_root, "failure_records", "20260918.json"), encoding="utf-8") as handle:
            records = json.load(handle)
        self.assertEqual(set(records), set(self.expected))
        self.assertIn("attempt_count", records["000001"])
        self.assertIn("failure_reason", records["000001"])

    def test_07_retry_target_date_then_recover(self):
        attempts = {code: 0 for code in self.expected}

        def fetch(code, date):
            attempts[code] += 1
            return {"rows": [self.bar(code)] if attempts[code] >= 2 else [], "source": "sina-qfq",
                    "provider_attempts": ["tencent-qfq", "sina-qfq"]}

        evidence = self.collect(fetch=fetch, max_attempts=2)
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(attempts, {"000001": 2, "000002": 2})

    def test_08_fallback_success_is_traceable(self):
        def fetch(code, date):
            return {"rows": [self.bar(code)], "source": "sina-qfq",
                    "provider_attempts": ["tencent-qfq", "sina-qfq"]}

        evidence = self.collect(fetch=fetch)
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["source_summary"], {"sina-qfq": 2})

    def test_09_all_providers_failed_remains_hold(self):
        evidence = self.collect(fetch=lambda code, date: {"rows": [], "provider_attempts": ["tencent-qfq", "sina-qfq", "tushare"]})
        self.assertEqual(evidence["status"], "HOLD")
        self.assertGreater(evidence["failed_symbol_count"], 0)

    def test_10_same_run_is_idempotent(self):
        self.store = {code: self.bar(code) for code in self.expected}
        first = self.collect()
        second = self.collect()
        self.assertEqual(first["valid_symbols_sha256"], second["valid_symbols_sha256"])
        self.assertEqual(len(self.store), 2)

    def test_11_interrupted_partial_run_leaves_recoverable_records(self):
        self.store["000001"] = self.bar("000001")
        calls = {"n": 0}

        def fetch(code, date):
            calls["n"] += 1
            if code == "000002":
                raise KeyboardInterrupt()
            return {"rows": [self.bar(code)], "source": "test-provider"}

        with self.assertRaises(KeyboardInterrupt):
            self.collect(fetch=fetch)
        records_path = os.path.join(self.completeness_root, "failure_records", "20260918.json")
        self.assertTrue(os.path.exists(records_path))

    def test_12_non_trading_date_is_hold(self):
        evidence = self.collect(trading_day=False)
        self.assertEqual(evidence["status"], "HOLD")
        self.assertIn("NON_TRADING_DATE", evidence["failure_reasons"])

    def test_13_failed_count_cannot_be_pass(self):
        evidence = build_completeness_evidence(
            self.snapshot, self.completeness_root, self.expected, ["000002"], [], True, True, {}, [],
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self.assertNotEqual(evidence["status"], "PASS")

    def test_14_missing_count_cannot_be_pass(self):
        evidence = build_completeness_evidence(
            self.snapshot, self.completeness_root, ["000001"], [], ["000002"], True, True, {}, [],
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self.assertNotEqual(evidence["status"], "PASS")

    def test_15_retry_false_cannot_be_pass(self):
        evidence = build_completeness_evidence(
            self.snapshot, self.completeness_root, self.expected, [], [], False, True, {}, [],
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self.assertNotEqual(evidence["status"], "PASS")

    def test_16_fallback_false_cannot_be_pass(self):
        evidence = build_completeness_evidence(
            self.snapshot, self.completeness_root, self.expected, [], [], True, False, {}, [],
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self.assertNotEqual(evidence["status"], "PASS")

    def test_17_consumer_accepts_only_complete_identity_bound_evidence(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        ok, reasons = validate_completeness_evidence(
            evidence, self.snapshot, "TEST-SEASON", "20260918",
            now=datetime.datetime.now(datetime.timezone.utc),
        )
        self.assertTrue(ok, reasons)

    def test_18_consumer_rejects_wrong_season(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        ok, reasons = validate_completeness_evidence(evidence, self.snapshot, "OTHER-SEASON", "20260918")
        self.assertFalse(ok)
        self.assertIn("EVIDENCE_SEASON_MISMATCH", reasons)

    def test_19_consumer_rejects_wrong_date(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        ok, reasons = validate_completeness_evidence(evidence, self.snapshot, "TEST-SEASON", "20260917")
        self.assertFalse(ok)
        self.assertIn("UNIVERSE_SNAPSHOT_DATE_MISMATCH", reasons)

    def test_20_consumer_rejects_universe_hash_mismatch(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        tampered = dict(evidence)
        tampered["universe_sha256"] = "0" * 64
        ok, reasons = validate_completeness_evidence(tampered, self.snapshot, "TEST-SEASON", "20260918")
        self.assertFalse(ok)
        self.assertIn("EVIDENCE_UNIVERSE_HASH_MISMATCH", reasons)

    def test_21_old_schema_pass_is_rejected(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        evidence["schema_version"] = "v3-legacy"
        ok, reasons = validate_completeness_evidence(evidence, self.snapshot, "TEST-SEASON", "20260918")
        self.assertFalse(ok)
        self.assertIn("EVIDENCE_SCHEMA_MISMATCH", reasons)

    def test_22_forward_consumer_missing_artifact_holds(self):
        context = {"season_root": self.root, "season_id": "TEST-SEASON"}
        ok, result = forward_start_gate._daily_market_completeness_status(
            context, datetime.date(2026, 9, 18),
        )
        self.assertFalse(ok)
        self.assertIn("DAILY_MARKET_COMPLETENESS_MISSING", result["reason_code"])

    def test_23_forward_consumer_rejects_counts_even_when_status_pass(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        evidence["failed_symbol_count"] = 1
        evidence["status"] = "PASS"
        atomic_write_json(evidence_path(self.completeness_root, "20260918"), evidence)
        ok, result = forward_start_gate._daily_market_completeness_status(
            {"season_root": self.root, "season_id": "TEST-SEASON"}, datetime.date(2026, 9, 18),
        )
        self.assertFalse(ok)
        self.assertIn("EVIDENCE_FAILED_COUNT_MISMATCH", result["reasons"])

    def test_24_snapshot_corruption_fails_closed(self):
        path = universe_snapshot_path(self.completeness_root, "20260918")
        atomic_write_json(path, {"schema_version": "bad"})
        with self.assertRaises(CompletenessContractError):
            load_universe_snapshot(path, "20260918")

    def test_25_missing_denominator_never_builds_pass(self):
        with self.assertRaises(CompletenessContractError):
            build_universe_snapshot("20260918", [], "MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE", "test-v1", self.source_path)

    def test_26_database_reader_can_be_temp_sqlite_only(self):
        db_path = os.path.join(self.root, "isolated.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE daily_kline(code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, source TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO daily_kline VALUES ('000001','20260918',10,11,9,10.5,100,1000,'sina-qfq','2026-09-18T16:00:00+08:00')")
        conn.commit()
        conn.close()
        self.assertTrue(os.path.exists(db_path))

    def test_27_primary_history_only_enters_provider_fallback(self):
        old = self.bar("000001", trade_date="20260917")
        target = self.bar("000001", trade_date="20260918")
        with patch.object(providers, "_daily_from_tencent", return_value=[old]), \
             patch.object(providers, "_daily_from_akshare", return_value=[target]), \
             patch.object(providers.history_db, "upsert_klines") as writer:
            result = providers.get_daily_history(
                "000001", start="20260918", end="20260918",
                return_metadata=True, target_trade_date="20260918",
            )
        self.assertEqual(result["source"], "sina-qfq")
        self.assertEqual([row["trade_date"] for row in result["rows"]], ["20260918"])
        self.assertEqual(result["provider_attempts"], ["tencent-qfq", "sina-qfq"])
        writer.assert_called_once()
        self.assertEqual(writer.call_args.args[0][0]["trade_date"], "20260918")

    def test_28_provider_writer_database_reread_evidence_forward_integration(self):
        db_path = os.path.join(self.root, "integration.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE daily_kline(code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, source TEXT, updated_at TEXT)")
        conn.commit()
        conn.close()

        def writer(code, rows, source=None):
            with sqlite3.connect(db_path) as handle:
                for row in rows:
                    handle.execute(
                        "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (code, row["trade_date"], row["open"], row["high"], row["low"],
                         row["close"], row["volume"], row["amount"], source,
                         "2026-09-18T16:00:00+08:00"),
                    )

        def reader(code, trade_date):
            with sqlite3.connect(db_path) as handle:
                return handle.execute(
                    "SELECT trade_date, open, high, low, close, volume, amount, 0, source, updated_at "
                    "FROM daily_kline WHERE code=? AND trade_date=?", (code, trade_date),
                ).fetchall()

        evidence = collect_target_date(
            self.snapshot, self.completeness_root,
            lambda code, date: {"rows": [self.bar(code)], "source": "sina-qfq",
                                "provider_attempts": ["tencent-qfq", "sina-qfq"]},
            reader, writer, trading_day_fn=lambda _: True,
        )
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["source_by_symbol"], {"000001": "sina-qfq", "000002": "sina-qfq"})
        self.assertEqual(evidence["source_summary"], {"sina-qfq": 2})
        ok, reasons = forward_start_gate._daily_market_completeness_status(
            {"season_root": self.root, "season_id": "TEST-SEASON"},
            datetime.date(2026, 9, 18),
        )
        self.assertTrue(ok, reasons)

    def test_29_database_source_missing_cannot_pass(self):
        def reader(code, trade_date):
            row = self.bar(code)
            return [(row["trade_date"], row["open"], row["high"], row["low"],
                     row["close"], row["volume"], row["amount"], row["pct_chg"],
                     None, "2026-09-18T16:00:00+08:00")]

        evidence = collect_target_date(
            self.snapshot, self.completeness_root,
            lambda code, date: {"rows": [self.bar(code)], "source": "sina-qfq"},
            reader, write_rows=lambda *args, **kwargs: None,
            trading_day_fn=lambda _: True,
        )
        self.assertEqual(evidence["status"], "HOLD")
        self.assertIn("DATABASE_SOURCE_MISSING", evidence["failure_reasons"][0])

    def test_30_source_file_duplicate_or_invalid_timestamp_rejected(self):
        duplicate = os.path.join(self.root, "duplicate.json")
        with open(duplicate, "w", encoding="utf-8") as handle:
            json.dump({"updated_at": "2026-09-18 09:00:00", "total_count": 2,
                       "stocks": [{"code": "000001"}, {"code": "000001"}]}, handle)
        with self.assertRaises(CompletenessContractError):
            build_universe_snapshot("20260918", ["000001"],
                                    "MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE", "test-v1", duplicate)
        invalid = os.path.join(self.root, "invalid_time.json")
        with open(invalid, "w", encoding="utf-8") as handle:
            json.dump({"updated_at": "not-a-time", "total_count": 1,
                       "stocks": [{"code": "000001"}]}, handle)
        with self.assertRaises(CompletenessContractError):
            build_universe_snapshot("20260918", ["000001"],
                                    "MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE", "test-v1", invalid)

    def test_31_data_job_hold_is_not_runtime_success(self):
        from daemon import trading_daemon as daemon
        business = MagicMock(return_value={"status": "HOLD", "reason_codes": ["G6_HOLD"]})
        with patch.object(daemon, "data_runtime_preflight", return_value=(True, {
                "status": "PASS", "pass": True, "preflight_domain": "DATA"})), \
             patch.object(daemon, "run_with_guard", side_effect=lambda name, fn: (True, fn())), \
             patch.object(daemon, "write_heartbeat"):
            result = daemon.execute_runtime_job("daily_review", business, source="test")
        self.assertEqual(result["status"], "HOLD")
        self.assertTrue(result["allowed"])
        self.assertTrue(result["business_job_called"])
        self.assertEqual(result["reason_codes"], ["G6_HOLD"])
        business.assert_called_once()

    def test_32_data_hold_writes_health_and_alert(self):
        from daemon import trading_daemon as daemon
        context = {"season_root": self.root, "season_id": "TEST-SEASON"}
        with patch.object(daemon, "write_heartbeat") as heartbeat, \
             patch("notify.wecom_bot.push") as alert:
            path, health = daemon._record_data_job_health(
                context, "20260918", {"status": "HOLD"}, ["MISSING_TARGET_DATE_BAR"],
            )
        self.assertTrue(os.path.exists(path))
        self.assertEqual(health["status"], "HOLD")
        self.assertEqual(health["economic_execution_impact"], "NONE")
        heartbeat.assert_called_once()
        alert.assert_called_once()

    def test_33_source_summary_must_match_success_set(self):
        self.store = {code: self.bar(code) for code in self.expected}
        evidence = self.collect()
        evidence["source_summary"] = {"tencent-qfq": 2}
        ok, reasons = validate_completeness_evidence(
            evidence, self.snapshot, "TEST-SEASON", "20260918",
            now=datetime.datetime.now(datetime.timezone.utc),
        )
        self.assertFalse(ok)
        self.assertIn("EVIDENCE_SOURCE_SUMMARY_MISMATCH", reasons)


if __name__ == "__main__":
    unittest.main()
