import datetime as dt
import pathlib
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.datasource.market_data_service import FreshnessPolicy, MarketDataService, RetryPolicy, SessionPoint
from scripts.datasource.provider_adapters import ProviderResult
from scripts.datasource.scanner_market_data import (
    PARTICIPANT_REQUIRED_FACTS,
    ScanBudget,
    build_scanner_preview,
)


SH = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 9, 23, 10, 0, tzinfo=SH)
UNITS = {
    "name": "text", "last_price": "CNY/share", "prev_price": "CNY/share",
    "price_change": "CNY/share", "price_change_ratio_pct": "percent",
    "volume": "share", "turnover": "CNY", "turnover_ratio": "percent",
    "pe": "ratio", "mktcap_yi": "CNY_100M",
}


def _assert_raises_regex(expected, pattern, call):
    try:
        call()
    except expected as exc:
        assert pattern in str(exc), f"expected {pattern!r}, got {exc!r}"
    else:
        raise AssertionError(f"expected {expected.__name__} containing {pattern!r}")


class FakeClock:
    def __init__(self, values=None):
        self.values = list(values or [NOW] * 20)
        self.last = self.values[-1]

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class FakeCalendar:
    def __init__(self, phase="CONTINUOUS", trading=True, trade_date="2026-09-23"):
        self.phase, self.trading, self.trade_date = phase, trading, trade_date

    def session_at(self, timestamp):
        return SessionPoint(self.phase, self.trading, self.trade_date, "VALID")

    def trading_dates(self, start, end):
        return ("2026-09-23",)


class FakeProvider:
    def __init__(self, provider, results):
        self.provider = provider
        self.results = list(results)
        self.calls = []

    def fetch_snapshot(self, symbol):
        self.calls.append(symbol)
        item = self.results.pop(0)
        return item

    def fetch_daily_history(self, *args, **kwargs):
        raise AssertionError("scanner preview must not request history")


def result(provider="hithink", *, symbol="600519.SH", observed=NOW - dt.timedelta(seconds=5),
           status="VALID", fields=None, units=None, request_status="SUCCESS", reason=None):
    return ProviderResult(
        canonical_symbol=symbol,
        exchange=symbol.rsplit(".", 1)[-1],
        provider=provider,
        observed_at=observed.isoformat() if observed else None,
        trade_date=observed.date().isoformat() if observed else None,
        source_request_id=f"{provider}-local-id",
        data_status=status,
        value_fields=fields if fields is not None else {
            "name": "贵州茅台", "last_price": 1500.0, "prev_price": 1498.0,
            "price_change": 2.0, "price_change_ratio_pct": 0.13,
            "volume": 123456.0, "turnover": 185000000.0,
            "turnover_ratio": 2.0, "pe": 25.0, "mktcap_yi": 18000.0,
        },
        field_units=units if units is not None else dict(UNITS),
        request_status=request_status,
        error_reason=reason,
    )


def make_service(primary, secondary, clock=None, calendar=None):
    return MarketDataService(
        [primary, secondary], clock=clock or FakeClock(),
        calendar=calendar or FakeCalendar(),
        freshness_policy=FreshnessPolicy({"CONTINUOUS": 90, "LUNCH_BREAK": 1800,
                                          "PRE_OPEN": 300, "AFTER_HOURS": 900},
                                         approved_for_decisions=False,
                                         policy_id="TEST_ONLY_90S"),
        retry_policy=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=0),
        sleeper=lambda _seconds: None,
    )


def test_hithink_is_primary_and_normalized_legacy_fields_are_mapped():
    primary, secondary = FakeProvider("hithink", [result()]), FakeProvider("tencent", [result("tencent")])
    preview = build_scanner_preview(["sh600519"], make_service(primary, secondary),
                                    clock=FakeClock(), calendar=FakeCalendar())
    quote = preview.quotes[0]
    assert quote.provider == "hithink"
    assert quote.decision_status == "VALID"
    assert quote.fields["price"] == 1500.0
    assert quote.fields["amount_wan"] == 18500.0
    assert quote.fields["amount_yi"] == 1.85
    assert quote.fields["volume"] == 123456.0
    assert secondary.calls == []


def test_primary_failure_falls_back_to_tencent_with_trace():
    primary = FakeProvider("hithink", [result(status="UNKNOWN", request_status="PERMISSION_DENIED")])
    secondary = FakeProvider("tencent", [result("tencent")])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar()).quotes[0]
    assert quote.provider == "tencent"
    assert [attempt["provider"] for attempt in quote.provider_attempts] == ["hithink", "tencent"]


def test_both_sources_unavailable_do_not_synthesize_prices():
    primary = FakeProvider("hithink", [result(status="UNKNOWN", request_status="TRANSPORT_ERROR")])
    secondary = FakeProvider("tencent", [result("tencent", status="UNKNOWN", request_status="TRANSPORT_ERROR")])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar()).quotes[0]
    assert quote.decision_status == "UNKNOWN"
    assert quote.fields["price"] is None
    assert quote.fields["amount_wan"] is None
    assert quote.decision_authorized is False


def test_missing_turnover_preserves_price_but_blocks_turnover_dependent_rules():
    fields = {"name": "贵州茅台", "last_price": 1500.0, "prev_price": 1498.0,
              "price_change_ratio_pct": 0.13}
    units = {key: UNITS[key] for key in fields}
    primary = FakeProvider("hithink", [result(fields=fields, units=units)])
    secondary = FakeProvider("tencent", [result("tencent", fields=fields, units=units)])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar()).quotes[0]
    assert quote.data_status == "VALID"  # required display/price facts are valid
    assert quote.fields["price"] == 1500.0
    assert quote.fields["amount_wan"] is None
    assert "amount_wan" in quote.participant_missing_facts["P-RULE-QUANT"]
    assert "turnover_ratio" in quote.participant_missing_facts["P-COOL"]


def test_quote_valid_at_receive_but_expired_at_consumer_use_is_stale():
    clock = FakeClock([NOW, NOW, NOW, NOW + dt.timedelta(seconds=1),
                       NOW + dt.timedelta(seconds=100), NOW + dt.timedelta(seconds=101)])
    primary = FakeProvider("hithink", [result(observed=NOW)])
    secondary = FakeProvider("tencent", [result("tencent", observed=NOW)])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary, clock),
                                  clock=clock, calendar=FakeCalendar()).quotes[0]
    assert quote.data_status == "VALID"
    assert quote.decision_status == "STALE"
    assert quote.decision_reason == "QUOTE_EXCEEDS_90_SECOND_DECISION_FRESHNESS"


def test_symbol_mismatch_is_rejected_then_secondary_is_tried():
    primary = FakeProvider("hithink", [result(symbol="000001.SZ")])
    secondary = FakeProvider("tencent", [result("tencent")])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar()).quotes[0]
    assert quote.provider == "tencent"
    assert quote.provider_attempts[0]["data_status"] == "INVALID"


def test_chinese_name_is_preserved_and_unknown_strategy_fields_remain_absent():
    primary = FakeProvider("hithink", [result()])
    secondary = FakeProvider("tencent", [])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar()).quotes[0]
    assert quote.fields["name"] == "贵州茅台"
    assert quote.fields["pe"] == 25.0
    assert quote.fields.get("ma20") is None
    assert quote.fields.get("intraday_structure") is None


def test_preview_is_bounded_and_rejects_full_market_request_before_provider_calls():
    primary = FakeProvider("hithink", [])
    secondary = FakeProvider("tencent", [])
    service = make_service(primary, secondary)
    _assert_raises_regex(ValueError, "PREVIEW_SYMBOL_BUDGET_EXCEEDED", lambda:
        build_scanner_preview(["600519.SH"] * 21, service, clock=FakeClock(), calendar=FakeCalendar()))
    assert primary.calls == [] and secondary.calls == []
    _assert_raises_regex(ValueError, "PREVIEW_PROVIDER_CALL_BUDGET_EXCEEDED", lambda:
        build_scanner_preview(["600519.SH", "000001.SZ"], service, clock=FakeClock(),
                              calendar=FakeCalendar(), budget=ScanBudget(2, 4)))
    assert primary.calls == [] and secondary.calls == []


def test_single_scan_shares_same_evidence_ids_across_six_roles():
    primary = FakeProvider("hithink", [result(), result(symbol="000001.SZ")])
    secondary = FakeProvider("tencent", [])
    # Use matching second-symbol facts while preserving a unique per-symbol identity.
    primary.results[1] = result(symbol="000001.SZ", fields={
        "name": "平安银行", "last_price": 11.5, "prev_price": 11.6,
        "price_change_ratio_pct": -0.86,
    }, units={"name": "text", "last_price": "CNY/share", "prev_price": "CNY/share",
             "price_change_ratio_pct": "percent"})
    preview = build_scanner_preview(["600519.SH", "000001.SZ"], make_service(primary, secondary),
                                   clock=FakeClock(), calendar=FakeCalendar())
    all_views = [preview.evidence_for_participant(pid) for pid in PARTICIPANT_REQUIRED_FACTS]
    assert len({view[0]["evidence_id"] for view in all_views}) == 1
    assert all(view[0]["scan_id"] == preview.scan_id for view in all_views)
    assert all(view[0]["fields"] == all_views[0][0]["fields"] for view in all_views)
    assert len(primary.calls) == 2


def test_actual_consumer_can_revalidate_same_evidence_after_scan_completion():
    primary = FakeProvider("hithink", [result(observed=NOW)])
    secondary = FakeProvider("tencent", [])
    preview = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                    clock=FakeClock(), calendar=FakeCalendar())
    use_at = NOW + dt.timedelta(seconds=91)
    view = preview.evidence_for_participant("P-YINWENTAO", decision_at=use_at,
                                            calendar=FakeCalendar())[0]
    assert view["evidence_id"] == preview.quotes[0].evidence_id
    assert view["data_status"] == "STALE"
    assert view["fields"] == dict(preview.quotes[0].fields)


def test_all_participants_keep_unavailable_rule_fact_requirements():
    facts = {"name": "贵州茅台", "last_price": 1500.0, "prev_price": 1498.0,
             "price_change_ratio_pct": 0.13}
    primary = FakeProvider("hithink", [result(fields=facts,
                                               units={key: UNITS[key] for key in facts})])
    secondary = FakeProvider("tencent", [])
    quote = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar()).quotes[0]
    assert "mktcap_yi" in quote.participant_missing_facts["P-RULE-QUANT"]
    assert "turnover_ratio" in quote.participant_missing_facts["P-COOL"]
    assert "turnover_ratio" in quote.participant_missing_facts["P-HANSHU"]
    assert quote.decision_authorized is False


def test_non_continuous_and_non_trading_decisions_are_not_buy_eligible():
    primary = FakeProvider("hithink", [result(), result()])
    secondary = FakeProvider("tencent", [])
    lunch = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                  clock=FakeClock(), calendar=FakeCalendar("LUNCH_BREAK"))
    assert lunch.quotes[0].decision_status == "STALE"
    closed = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                   clock=FakeClock(), calendar=FakeCalendar("NON_TRADING", False))
    assert closed.quotes[0].decision_status == "STALE"


def test_output_contains_no_credentials_and_bridge_has_no_persistence_dependencies():
    primary = FakeProvider("hithink", [result()])
    secondary = FakeProvider("tencent", [])
    payload = build_scanner_preview(["600519.SH"], make_service(primary, secondary),
                                    clock=FakeClock(), calendar=FakeCalendar()).quotes[0].to_dict()
    rendered = repr(payload).lower()
    assert "authorization" not in rendered and "api_key" not in rendered
    source = (ROOT / "scripts/datasource/scanner_market_data.py").read_text(encoding="utf-8")
    assert "sqlite" not in source.lower() and "upsert_klines" not in source
    assert "persisted=False" in source or "persisted: bool = False" in source


def test_optional_projection_is_allowlisted_and_unit_checked():
    invalid = result(units={**UNITS, "turnover_ratio": "unknown-unit"})
    primary = FakeProvider("hithink", [invalid])
    secondary = FakeProvider("tencent", [])
    service = make_service(primary, secondary)
    quote = build_scanner_preview(["600519.SH"], service, clock=FakeClock(),
                                  calendar=FakeCalendar()).quotes[0]
    assert quote.fields["price"] == 1500.0
    assert quote.fields["turnover_ratio"] is None
    assert "turnover_ratio" in quote.unavailable_fields
    _assert_raises_regex(ValueError, "OPTIONAL_FIELD_PROJECTION_INVALID", lambda:
        service.get_snapshot("600519.SH", "PRICE_ONLY", consumer="test",
                             include_fields=("untrusted_provider_field",)))


def test_legacy_scanner_entry_and_frozen_no_signal_rules_remain_unchanged():
    source = (ROOT / "scripts/scan_mainboard_picks.py").read_text(encoding="utf-8")
    assert "def scan_and_score_all(persist=True):" in source
    assert "MA20_CONFIRMATION_UNAVAILABLE" in source and "action = 'NO_SIGNAL'" in source
    assert "INTRADAY_STRUCTURE_UNAVAILABLE" in source
    assert "def fetch_quotes_batch(symbols):" in source


def test_no_trade_or_scan_persistence_code_is_called_by_preview():
    source = (ROOT / "scripts/datasource/scanner_market_data.py").read_text(encoding="utf-8")
    assert "scan_and_score_all(" not in source
    assert "write_text(" not in source and "open(" not in source
    assert "decision_authorized: bool = False" in source


def load_tests(loader, tests, pattern):
    """Expose the contract-style function tests to stdlib unittest discovery."""
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
