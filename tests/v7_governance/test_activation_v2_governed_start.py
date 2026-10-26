import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ai_quant.storage_utils import atomic_write_json, strict_load_json
from competition import season_governance, season_runtime


SEASON = "V7-SEASON-20260930-V2"
EPOCH = "AIQ-FWD-V7-20260930-E2"
PARTICIPANTS = (
    ("P-LIAO-XIAOAI", "Liao Xiao Ai", "LIAO-XIAOAI-V2"),
    ("P-COOL", "Cool", "COOL-V2"),
    ("P-YINWENTAO", "Yin Wentao", "YINWENTAO-V2"),
    ("P-HANSHU", "Han Shu", "HANSHU-V2"),
    ("P-RULE-QUANT", "Rule Quant", "AI-ALPHA-V2"),
    ("P-AI-QUANT", "AI Quant", "AIQ-PAPER-V2"),
    ("P-RISK-SENTINEL", "Risk Sentinel", "RISK-SENTINEL-V2"),
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GovernedV2SeasonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.old_root = self.base / "state" / "seasons" / "V7-SEASON-20260917-V1"
        self.old_root.mkdir(parents=True)
        participants = []
        for pid, name, account_id in PARTICIPANTS:
            participants.append({
                "participant_id": pid, "display_name": name,
                "new_account_id": account_id,
                "season_id": "V7-SEASON-20260917-V1",
            })
        atomic_write_json(self.old_root / "season_manifest.json", {
            "season_id": "V7-SEASON-20260917-V1",
            "forward_epoch_id": "AIQ-FWD-V7-20260917-E1",
            "participants": participants,
        })
        self.old_manifest_sha = _sha(self.old_root / "season_manifest.json")

    def tearDown(self):
        self.temp.cleanup()

    def _initialize(self):
        return season_governance.initialize_new_season(
            explicit_initialize=True, base_dir=str(self.base),
            season_id=SEASON, epoch_id=EPOCH,
        )

    def _write_pointer_fixture(self, *, pointer_season=SEASON, context_season=SEASON):
        root = self.base / "state" / "seasons" / pointer_season
        root.mkdir(parents=True, exist_ok=True)
        root_text = str(root.resolve())
        atomic_write_json(root / "active_season_context.json", {
            "schema_version": "v7-season-context-v1",
            "active_season_id": context_season,
            "state_root": root_text,
            "forward_epoch_id": EPOCH,
            "mode": "PAPER/SIMULATION", "real_trading": False,
        })
        atomic_write_json(root / "season_manifest.json", {
            "season_id": pointer_season, "forward_epoch_id": EPOCH,
            "state_root": root_text, "mode": "PAPER/SIMULATION", "real_trading": False,
        })
        pointer = {
            "schema_version": season_runtime.ACTIVE_IDENTITY_SCHEMA,
            "active_season_id": pointer_season, "season_root": root_text,
            "forward_epoch_id": EPOCH, "mode": "PAPER/SIMULATION",
            "real_trading": False, "activated_at": "2026-09-30T16:00:00+08:00",
            "evidence_ref": "isolated-test#sha256=" + "0" * 64,
        }
        atomic_write_json(self.base / "state" / "active_season.json", pointer)
        return pointer

    def test_active_pointer_strict_load_and_context_binding(self):
        self._write_pointer_fixture()
        resolved = season_runtime.resolve_active_season_context(base_dir=str(self.base))
        self.assertEqual(resolved["season_id"], SEASON)
        self.assertEqual(resolved["forward_epoch_id"], EPOCH)

    def test_corrupted_active_pointer_fails_closed(self):
        pointer = self._write_pointer_fixture()
        pointer.pop("activated_at")
        atomic_write_json(self.base / "state" / "active_season.json", pointer)
        with self.assertRaises(season_runtime.RuntimeStateError):
            season_runtime.load_active_season_identity(base_dir=str(self.base))

    def test_active_season_context_mismatch_fails_closed(self):
        self._write_pointer_fixture(context_season="V7-SEASON-20260930-V1")
        with self.assertRaises(season_runtime.RuntimeStateError):
            season_runtime.resolve_active_season_context(base_dir=str(self.base))

    def test_clean_v2_init_is_independent_day_zero_and_duplicate_refused(self):
        context = self._initialize()
        root = Path(context["season_root"])
        self.assertEqual(root, self.base / "state" / "seasons" / SEASON)
        self.assertEqual(_sha(self.old_root / "season_manifest.json"), self.old_manifest_sha)
        manifest = json.loads((root / "season_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["season_id"], SEASON)
        self.assertEqual(manifest["forward_epoch_id"], EPOCH)
        self.assertFalse(manifest["historical_economic_state_inherited"])
        self.assertFalse((root / "orders.jsonl").exists())

        arena = strict_load_json(root / "competition_accounts.json", expected_type=dict)
        self.assertTrue(arena.is_valid)
        accounts = arena.value["accounts"]
        return_accounts = [a for a in accounts.values() if a.get("return_bearing") is True]
        self.assertEqual(len(return_accounts), 6)
        self.assertTrue(all(a["season_id"] == SEASON and a["cash"] == 100000.0 for a in return_accounts))
        self.assertTrue(all(a.get("positions", []) == [] and a.get("trade_count", 0) == 0 for a in return_accounts))
        sentinel = accounts["P-RISK-SENTINEL"]
        self.assertEqual(sentinel["initial_capital"], 0.0)
        self.assertFalse(sentinel["return_bearing"])
        self.assertFalse(sentinel["trading_account"])
        self.assertFalse(sentinel["ranking_enabled"])

        aiq = json.loads((root / "ai_quant_paper" / "account.json").read_text(encoding="utf-8"))
        self.assertEqual(aiq["account_id"], "AIQ-PAPER-V2")
        self.assertEqual(aiq["season_id"], SEASON)
        self.assertEqual(aiq["initial_capital"], aiq["cash"])
        self.assertEqual(aiq["cash"], 100000.0)
        self.assertEqual(json.loads((root / "ai_quant_paper" / "positions.json").read_text(encoding="utf-8")), [])

        forward = json.loads((root / "forward" / "forward_epoch_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(forward["season_id"], SEASON)
        self.assertEqual(forward["forward_epoch_id"], EPOCH)
        self.assertEqual(forward["forward_day_count"], 0)
        self.assertEqual(forward["qualified_trading_dates"], [])
        self.assertFalse(forward["forward_enabled"])
        self.assertFalse(forward["genuine_forward_started"])
        self.assertNotIn("20260930", forward["qualified_trading_dates"])

        gate_path = root / "gate_state.json"
        gate_sha = _sha(gate_path)
        with self.assertRaises(season_runtime.RuntimeStateError):
            season_governance.write_gate_state(
                context, gate_a="CLOSED/PASS", g4="PASS", g5="PASS",
                g6="OPEN/P0", gate_b="CLOSED/PASS", evidence_refs=["unused"],
                explicit_writeback=True,
            )
        self.assertEqual(_sha(gate_path), gate_sha)
        with self.assertRaises(season_runtime.RuntimeStateError):
            season_governance.initialize_new_season(
                explicit_initialize=True, base_dir=str(self.base),
                season_id=SEASON, epoch_id=EPOCH,
            )


if __name__ == "__main__":
    unittest.main()
