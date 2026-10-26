import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from scripts.datasource.market_data_service import FreshnessPolicy, MarketDataService, RetryPolicy, SessionPoint
from scripts.datasource.provider_adapters import ProviderResult
from scripts.datasource.scanner_market_data import ScanBudget, build_scanner_preview
from scripts.datasource.scanner_parity import compare_legacy_quotes


SH = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 9, 23, 10, 0, tzinfo=SH)
UNITS = {
    "name": "text", "last_price": "CNY/share", "prev_price": "CNY/share",
    "price_change": "CNY/share", "price_change_ratio_pct": "percent",
    "volume": "share", "turnover": "CNY", "turnover_ratio": "percent",
    "pe": "ratio", "mktcap_yi": "CNY_100M",
}


class Clock:
    def __call__(self):
        return NOW


class Calendar:
    def session_at(self, when):
        return SessionPoint("CONTINUOUS", True, when.date().isoformat(), "VALID")

    def trading_dates(self, start, end):
        return ("2026-09-23",)


class Provider:
    def __init__(self, name, result=None):
        self.provider = name
        self.result = result
        self.calls = []

    def fetch_snapshot(self, symbol):
        self.calls.append(symbol)
        if self.result is None:
            raise AssertionError("unexpected fake-provider request")
        return self.result

    def fetch_daily_history(self, *args, **kwargs):
        raise AssertionError("parity preview must not fetch history")


def provider_result(provider="hithink", *, values=None, observed=NOW - dt.timedelta(seconds=5),
                    symbol="600519.SH", status="VALID", request_status="SUCCESS"):
    defaults = {
        "name": "贵州茅台", "last_price": 1500.0, "prev_price": 1498.0,
        "price_change": 2.0, "price_change_ratio_pct": 0.13,
        "volume": 123456.0, "turnover": 185000000.0,
        "turnover_ratio": 2.0, "pe": 25.0, "mktcap_yi": 18000.0,
    }
    values = dict(defaults if values is None else values)
    return ProviderResult(
        canonical_symbol=symbol, exchange=symbol.split(".")[-1], provider=provider,
        observed_at=observed.isoformat() if observed else None,
        trade_date=observed.date().isoformat() if observed else None,
        source_request_id=f"fake-{provider}-request", data_status=status,
        value_fields=values, field_units={k: UNITS[k] for k in values if k in UNITS},
        unavailable_fields=tuple(k for k in UNITS if k not in values),
        request_status=request_status,
    )


def preview_with(primary_result=None, secondary_result=None, *, symbols=("600519.SH",)):
    primary = Provider("hithink", primary_result)
    secondary = Provider("tencent", secondary_result)
    service = MarketDataService(
        [primary, secondary], clock=Clock(), calendar=Calendar(),
        freshness_policy=FreshnessPolicy({"CONTINUOUS": 90}, approved_for_decisions=False,
                                         policy_id="TEST_ONLY"),
        retry_policy=RetryPolicy(max_attempts_per_provider=1, backoff_seconds=0),
        sleeper=lambda _: None,
    )
    return build_scanner_preview(symbols, service, clock=Clock(), calendar=Calendar(),
                                 budget=ScanBudget(max_symbols=20, max_provider_attempts=80))


def legacy_fixture(quote, *, changes=None):
    values = {
        "name": "贵州茅台", "code": "600519", "price": 1500.0,
        "change_pct": 0.13, "volume": 123456.0,
        "amount_wan": 18500.0, "amount_yi": 1.85,
        "turnover_ratio": 2.0, "pe": 25.0, "mktcap_yi": 18000.0,
    }
    if changes:
        values.update(changes)
    return {
        "canonical_symbol": quote.canonical_symbol,
        "provider": quote.provider,
        "observed_at": quote.observed_at,
        "fields": values,
        "field_units": {"price": "CNY/share", "change_pct": "percent",
                        "amount_wan": "CNY_10000", "amount_yi": "CNY_100M",
                        "pe": "ratio", "mktcap_yi": "CNY_100M",
                        "turnover_ratio": "percent"},
        "raw_fields": {1: "贵州茅台", 2: "600519", 3: "1500", 4: "1498",
                        6: "1234", 30: "20260923100000", 32: "0.13",
                        37: "18500", 38: "2.0", 39: "25", 45: "18000"},
    }


def _row(rows, name):
    return next(row for row in rows if row["legacy_field"] == name)


class ScannerParityP1C2Tests(unittest.TestCase):
    def test_primary_success_is_used_and_secondary_is_not_called(self):
        primary, secondary = Provider("hithink", provider_result()), Provider("tencent")
        service = MarketDataService([primary, secondary], clock=Clock(), calendar=Calendar(),
                                    freshness_policy=FreshnessPolicy({"CONTINUOUS": 90}),
                                    retry_policy=RetryPolicy(max_attempts_per_provider=1),
                                    sleeper=lambda _: None)
        preview = build_scanner_preview(["600519.SH"], service, clock=Clock(), calendar=Calendar())
        self.assertEqual(preview.quotes[0].provider, "hithink")
        self.assertEqual(primary.calls, ["600519.SH"])
        self.assertEqual(secondary.calls, [])

    def test_primary_failure_falls_back_to_fake_tencent_only(self):
        bad = provider_result(status="UNKNOWN", request_status="PERMISSION_DENIED")
        good = provider_result("tencent")
        preview = preview_with(bad, good)
        self.assertEqual(preview.quotes[0].provider, "tencent")
        self.assertEqual([x["provider"] for x in preview.quotes[0].provider_attempts],
                         ["hithink", "tencent"])

    def test_field_indexes_document_legacy_volume_mismatch_and_amount_conversion(self):
        preview = preview_with(None, provider_result("tencent"))
        quote = preview.quotes[0]
        rows = compare_legacy_quotes([legacy_fixture(quote)], preview)
        self.assertEqual(_row(rows, "volume")["legacy_wire_index"], 6)
        self.assertEqual(_row(rows, "volume")["new_wire_index"], 36)
        self.assertEqual(_row(rows, "volume")["difference_type"], "LEGACY_UNVERIFIED")
        amount = _row(rows, "amount_wan")
        self.assertEqual(amount["difference_type"], "UNIT_RECONCILED")
        self.assertTrue(amount["values_equivalent"])
        self.assertEqual(amount["new_value"], 18500.0)
        self.assertEqual(amount["new_unit"], "CNY_10000")

    def test_old_empty_field_parsed_as_zero_is_reported_missing(self):
        quote = preview_with(provider_result()).quotes[0]
        legacy = legacy_fixture(quote, changes={"volume": 0.0})
        legacy["raw_fields"][6] = ""
        row = _row(compare_legacy_quotes([legacy], preview_with(provider_result())), "volume")
        self.assertEqual(row["legacy_value"], 0.0)
        self.assertEqual(row["difference_type"], "LEGACY_FIELD_MISSING")

    def test_missing_new_turnover_ratio_is_not_replaced_by_legacy_zero_or_value(self):
        values = {"name": "贵州茅台", "last_price": 1500.0, "prev_price": 1498.0,
                  "price_change_ratio_pct": 0.13}
        quote_preview = preview_with(provider_result(values=values))
        legacy = legacy_fixture(quote_preview.quotes[0])
        row = _row(compare_legacy_quotes([legacy], quote_preview), "turnover_ratio")
        self.assertEqual(row["difference_type"], "NEW_FIELD_MISSING")

    def test_pe_and_market_cap_are_missing_when_provider_did_not_supply_them(self):
        values = {"name": "贵州茅台", "last_price": 1500.0, "prev_price": 1498.0,
                  "price_change_ratio_pct": 0.13}
        preview = preview_with(provider_result(values=values))
        rows = compare_legacy_quotes([legacy_fixture(preview.quotes[0])], preview)
        self.assertEqual(_row(rows, "pe")["difference_type"], "NEW_FIELD_MISSING")
        self.assertEqual(_row(rows, "mktcap_yi")["difference_type"], "NEW_FIELD_MISSING")

    def test_legacy_observation_time_absent_is_never_replaced_by_scan_time(self):
        quote = preview_with(provider_result()).quotes[0]
        legacy = legacy_fixture(quote)
        legacy.pop("observed_at")
        rows = compare_legacy_quotes([legacy], preview_with(provider_result()))
        self.assertEqual(_row(rows, "observed_at")["difference_type"], "LEGACY_FIELD_MISSING")
        self.assertEqual(_row(rows, "price")["difference_type"], "TIME_BASIS_MISMATCH")

    def test_different_quote_times_block_numeric_parity_claim(self):
        old = NOW - dt.timedelta(seconds=60)
        legacy = {"canonical_symbol": "600519.SH", "provider": "tencent",
                  "observed_at": old.isoformat(), "fields": {"price": 1500.0},
                  "field_units": {"price": "CNY/share"}, "raw_fields": {3: "1500"}}
        preview = preview_with(provider_result("hithink"))
        row = _row(compare_legacy_quotes([legacy], preview), "price")
        self.assertEqual(row["difference_type"], "TIME_BASIS_MISMATCH")
        self.assertIsNone(row["values_equivalent"])

    def test_unverified_amount_unit_does_not_get_reconciled(self):
        quote = preview_with(provider_result("tencent")).quotes[0]
        legacy = legacy_fixture(quote)
        legacy["field_units"].pop("amount_wan")
        row = _row(compare_legacy_quotes([legacy], preview_with(provider_result("tencent"))),
                   "amount_wan")
        self.assertEqual(row["difference_type"], "LEGACY_UNVERIFIED")

    def test_unmatched_symbol_is_not_comparable_or_silently_joined(self):
        preview = preview_with(provider_result())
        legacy = legacy_fixture(preview.quotes[0])
        legacy["canonical_symbol"] = "000001.SZ"
        rows = compare_legacy_quotes([legacy], preview)
        self.assertTrue(all(row["difference_type"] == "NOT_COMPARABLE" for row in rows))
        self.assertTrue(all(row["symbol"] == "000001.SZ" for row in rows))

    def test_comparator_rejects_more_than_twenty_and_duplicate_symbols(self):
        from dataclasses import replace
        preview = preview_with(provider_result())
        with self.assertRaisesRegex(ValueError, "PARITY_SYMBOL_BUDGET_EXCEEDED"):
            compare_legacy_quotes([{}] * 21, preview)
        dup_preview = replace(preview, quotes=(preview.quotes[0], preview.quotes[0]))
        with self.assertRaisesRegex(ValueError, "DUPLICATE_PREVIEW_SYMBOL"):
            compare_legacy_quotes([], dup_preview)

    def test_consumer_layers_are_distinct_and_never_authorize_execution(self):
        quote = preview_with(provider_result()).quotes[0]
        view = quote.consumer_view("P-HANSHU", decision_at=NOW, calendar=Calendar())
        self.assertTrue(view["QUOTE_VALID"])
        self.assertFalse(view["REQUIRED_FACTS_COMPLETE"])
        self.assertEqual(view["SKILL_RULES_SATISFIED"], "NOT_EVALUATED")
        self.assertFalse(view["EXECUTION_AUTHORIZED"])

    def test_hanshu_ma20_missing_remains_no_signal_evidence_gap(self):
        quote = preview_with(provider_result()).quotes[0]
        view = quote.consumer_view("P-HANSHU", decision_at=NOW, calendar=Calendar())
        self.assertIn("ma20", view["required_facts_missing"])
        self.assertIn("ma5", view["required_facts_missing"])
        self.assertFalse(view["REQUIRED_FACTS_COMPLETE"])
        self.assertFalse(view["EXECUTION_AUTHORIZED"])

    def test_cool_intraday_and_daily_structure_missing_remain_unavailable(self):
        quote = preview_with(provider_result()).quotes[0]
        view = quote.consumer_view("P-COOL", decision_at=NOW, calendar=Calendar())
        self.assertIn("intraday_structure", view["required_facts_missing"])
        self.assertIn("daily_structure", view["required_facts_missing"])
        self.assertFalse(view["REQUIRED_FACTS_COMPLETE"])

    def test_sentinel_is_non_trading_non_ranking_observer(self):
        quote = preview_with(provider_result()).quotes[0]
        view = quote.consumer_view("P-RISK-SENTINEL", decision_at=NOW, calendar=Calendar())
        self.assertEqual(view["SKILL_RULES_SATISFIED"], "NOT_APPLICABLE_NO_TRADE_RULE")
        self.assertFalse(view["EXECUTION_AUTHORIZED"])
        self.assertIn("market_risk_evidence", view["required_facts_missing"])

    def test_quote_valid_at_scan_but_expired_at_consumer_use_is_stale(self):
        quote = preview_with(provider_result()).quotes[0]
        view = quote.consumer_view("P-YINWENTAO", decision_at=NOW + dt.timedelta(seconds=96),
                                   calendar=Calendar())
        self.assertFalse(view["QUOTE_VALID"])
        self.assertEqual(view["quote_status"], "STALE")
        self.assertEqual(view["quote_reason"], "QUOTE_EXCEEDS_90_SECOND_DECISION_FRESHNESS")
        self.assertFalse(view["EXECUTION_AUTHORIZED"])

    def test_many_participants_reference_same_scan_and_evidence(self):
        preview = preview_with(provider_result())
        views = [preview.quotes[0].consumer_view(pid, decision_at=NOW, calendar=Calendar())
                 for pid in ("P-RULE-QUANT", "P-HANSHU", "P-YINWENTAO", "P-LIAO-XIAOAI", "P-COOL")]
        self.assertEqual({x["scan_id"] for x in views}, {preview.scan_id})
        self.assertEqual({x["evidence_id"] for x in views}, {preview.quotes[0].evidence_id})
        self.assertTrue(all(x["fields"] == views[0]["fields"] for x in views))

    def test_two_provider_failure_stays_unavailable_not_synthetic(self):
        preview = preview_with(
            provider_result(status="UNKNOWN", request_status="TRANSPORT_ERROR"),
            provider_result("tencent", status="UNKNOWN", request_status="TRANSPORT_ERROR"))
        quote = preview.quotes[0]
        self.assertNotEqual(quote.data_status, "VALID")
        self.assertIsNone(quote.fields["price"])
        self.assertIsNone(quote.fields["amount_wan"])

    def test_no_production_scanner_or_persistence_import_in_comparator(self):
        from pathlib import Path
        source_root = Path(__file__).resolve().parents[2]
        source = (source_root / "scripts/datasource/scanner_parity.py").read_text(encoding="utf-8")
        scanner = (source_root / "scripts/scan_mainboard_picks.py").read_text(encoding="utf-8")
        self.assertNotIn("scan_and_score_all", source)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("sqlite", source.lower())
        self.assertIn("def scan_and_score_all(persist=True):", scanner)


if __name__ == "__main__":
    unittest.main()
