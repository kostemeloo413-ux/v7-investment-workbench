import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ai_quant.storage_utils import strict_load_json
from competition.activation_boundary import evaluate_buy_boundary
from competition.transaction_writer import TransactionIntegrityError, commit_strategy_transaction

SH = ZoneInfo("Asia/Shanghai")
SEASON = "V7-SEASON-20260930-V2"
EPOCH = "AIQ-FWD-V7-20260930-E2"
STRATEGY_IDS = (
    "P-LIAO-XIAOAI", "P-COOL", "P-YINWENTAO", "P-HANSHU", "P-RULE-QUANT",
)


class ActivationBoundaryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.competition_root = self.root / "competition_ledgers"
        self.season_root = self.root / "season"
        self.aiq_root = self.root / "ai_quant_paper"
        self.safety_path = self.season_root / "governance_halt.json"
        self.context = {
            "season_id": SEASON,
            "forward_epoch_id": EPOCH,
            "season_root": str(self.season_root),
            "competition_ledger_root": str(self.competition_root),
            "ai_quant_paper_root": str(self.aiq_root),
            "governance_safety_path": str(self.safety_path),
            "active_identity": {
                "active_season_id": SEASON,
                "activated_at": "2026-09-30T15:30:00+08:00",
            },
        }
        self.account = {
            "season_id": SEASON,
            "participant_id": STRATEGY_IDS[0],
            "account_id": "LIAO-XIAOAI-V2",
            "_safety_season_id": SEASON,
            "_safety_root": str(self.season_root),
            "cash": 100000.0,
            "positions": [],
        }

    def tearDown(self):
        self.temp.cleanup()

    def _writer_rejects_without_economic_files(
        self, timestamp, account=None, expected_reason=None,
        decision_season_id=None, market_phase="open",
    ):
        from competition import season_runtime
        from runtime_config import TradingCalendarStatus

        account = account or dict(self.account)
        events_root = self.competition_root / account["participant_id"]
        decision = {"participant_id": account["participant_id"]}
        if decision_season_id is not None:
            decision["season_id"] = decision_season_id
        original = season_runtime.resolve_active_season_context
        with patch.object(season_runtime, "resolve_active_season_context", return_value=self.context), \
             patch("runtime_config.trading_calendar_status", return_value={"status": TradingCalendarStatus.VALID, "trading_day": True}), \
             patch("runtime_config.market_phase", return_value=market_phase):
            with self.assertRaises(TransactionIntegrityError) as caught:
                commit_strategy_transaction(
                    str(events_root), account,
                    [{"symbol": "600519", "name": "Kweichow Moutai", "code": "600519", "shares": 100,
                      "cost_price": 100.0, "current_price": 100.0, "buy_date": "2026-09-30"}],
                    {"direction": "BUY", "symbol": "600519", "code": "600519", "name": "Kweichow Moutai",
                     "shares": 100, "price": 100.0, "date": "2026-09-30", "ts": "2026-09-30T16:00:00+08:00",
                     "commission": 5.0, "decision_timestamp": timestamp, "season_id": SEASON},
                    decision=decision,
                )
        self.assertIn(expected_reason, str(caught.exception))
        self.assertFalse((events_root / "orders.jsonl").exists())
        self.assertFalse((events_root / "fills.jsonl").exists())
        self.assertFalse((events_root / "account.json").exists())
        self.assertFalse((events_root / "positions.json").exists())

    def test_pre_activation_decision_rejected_at_writer(self):
        self._writer_rejects_without_economic_files(
            "2026-09-30T15:00:00+08:00", expected_reason="STALE_PRE_ACTIVATION_DECISION")

    def test_missing_timestamp_rejected_at_writer(self):
        self._writer_rejects_without_economic_files(None, expected_reason="INVALID_DECISION_TIMESTAMP")

    def test_malformed_timestamp_rejected_at_writer(self):
        self._writer_rejects_without_economic_files("not-a-timestamp", expected_reason="INVALID_DECISION_TIMESTAMP")

    def test_after_hours_decision_rejected_at_writer(self):
        self._writer_rejects_without_economic_files(
            "2026-09-30T15:45:00+08:00", expected_reason="MARKET_SESSION_BLOCK", market_phase="closed")

    def test_mixed_season_account_rejected_at_writer(self):
        account = dict(self.account, season_id="V7-SEASON-20260930-V1")
        self._writer_rejects_without_economic_files(
            "2026-09-30T15:45:00+08:00", account=account, expected_reason="MIXED_SEASON_STATE")

    def test_mixed_season_decision_rejected_at_writer(self):
        self._writer_rejects_without_economic_files(
            "2026-09-30T15:45:00+08:00", expected_reason="MIXED_SEASON_STATE",
            decision_season_id="V7-SEASON-20260930-V1")

    def test_boundary_parses_offset_and_fail_closes_naive_or_future(self):
        kwargs = {
            "context": self.context,
            "now": datetime(2026, 9, 30, 15, 50, tzinfo=SH),
            "calendar_status_fn": lambda _: {"status": "VALID", "trading_day": True},
            "market_phase_fn": lambda _: "open",
        }
        passed = evaluate_buy_boundary("2026-09-30T15:45:00+08:00", **kwargs)
        self.assertTrue(passed.allowed)
        naive = evaluate_buy_boundary("2026-09-30 15:45:00", **kwargs)
        self.assertFalse(naive.allowed)
        self.assertEqual(naive.reason, "INVALID_DECISION_TIMESTAMP")
        future = evaluate_buy_boundary("2026-09-30T16:00:00+08:00", **kwargs)
        self.assertFalse(future.allowed)
        self.assertEqual(future.reason, "INVALID_DECISION_TIMESTAMP")

    def test_all_five_strategy_buy_entries_reject_before_in_memory_mutation(self):
        from competition import season_runtime
        import simulated_trading

        with patch.object(season_runtime, "resolve_active_season_context", return_value=self.context):
            for participant_id in STRATEGY_IDS:
                with self.subTest(participant_id=participant_id):
                    account = {
                        **self.account,
                        "participant_id": participant_id,
                        "_season_ledger_root": str(self.competition_root / participant_id),
                        "positions": [],
                        "trade_ids": [],
                    }
                    before = copy.deepcopy(account)
                    result = simulated_trading.execute_buy(
                        account, participant_id, "sh600519", "Moutai", "600519",
                        100.0, 0.1, decision_timestamp="2026-09-30T15:00:00+08:00")
                    self.assertIsNone(result)
                    self.assertEqual(account, before)
                    ledger = Path(account["_season_ledger_root"])
                    self.assertFalse((ledger / "orders.jsonl").exists())
                    self.assertFalse((ledger / "fills.jsonl").exists())

    def test_aiq_paper_executor_rejects_before_order_fill_or_account_mutation(self):
        from competition import season_runtime
        from ai_quant.paper_executor import PaperExecutionEngine

        self.aiq_root.mkdir(parents=True)
        engine = PaperExecutionEngine(
            storage_dir=str(self.aiq_root), account_id="AIQ-PAPER-V2",
            initial_capital=100000.0, season_id=SEASON,
            governance_safety_path=str(self.safety_path), context=self.context,
            policy=SimpleNamespace(halt_path=str(self.safety_path)),
        )
        before_account = strict_load_json(self.aiq_root / "account.json", expected_type=dict).value
        before_positions = strict_load_json(self.aiq_root / "positions.json", expected_type=list).value
        with patch.object(season_runtime, "resolve_active_season_context", return_value=self.context):
            result = engine.execute_buy(
                {"symbol": "600519", "decision_timestamp": "2026-09-30T15:00:00+08:00"},
                {"plan_id": "stale-plan", "season_id": SEASON}, 100.0, 100.0,
                "20260930")
        self.assertEqual(result, (False, "STALE_PRE_ACTIVATION_DECISION", "STALE_PRE_ACTIVATION_DECISION"))
        self.assertEqual(strict_load_json(self.aiq_root / "account.json", expected_type=dict).value, before_account)
        self.assertEqual(strict_load_json(self.aiq_root / "positions.json", expected_type=list).value, before_positions)
        self.assertFalse((self.aiq_root / "orders.jsonl").exists())
        self.assertFalse((self.aiq_root / "fills.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
