import datetime as dt
import pathlib
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scripts.datasource.market_data_service import (
    FieldRequirement,
    FreshnessPolicy,
    MarketDataService,
    RetryPolicy,
    SessionPoint,
)
from scripts.datasource.provider_adapters import DailyBar, ProviderResult
from scripts.datasource.provider_adapters import HiThinkAdapter, TencentAdapter


SH = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 9, 23, 10, 0, tzinfo=SH)
PRICE_UNITS = {"last_price": "CNY/share", "prev_price": "CNY/share",
               "volume": "share", "turnover": "CNY"}
BAR_UNITS = {"open_price": "CNY/share", "high_price": "CNY/share",
             "low_price": "CNY/share", "close_price": "CNY/share",
             "volume": "share", "turnover": "CNY"}


class FakeClock:
    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def sleep(self, seconds):
        self.current += dt.timedelta(seconds=seconds)


class FakeCalendar:
    def __init__(self, phase="CONTINUOUS", trading_day=True,
                 trade_date="2026-09-23", dates=("2026-09-22", "2026-09-23")):
        self.phase = phase
        self.trading_day = trading_day
        self.trade_date = trade_date
        self.dates = tuple(dates)

    def session_at(self, timestamp):
        return SessionPoint(self.phase, self.trading_day, self.trade_date, "VALID")

    def trading_dates(self, start, end):
        return self.dates


class FakeProvider:
    def __init__(self, provider, snapshots=(), histories=()):
        self.provider = provider
        self.snapshots = list(snapshots)
        self.histories = list(histories)
        self.snapshot_calls = 0
        self.history_calls = 0

    @staticmethod
    def _pop(queue):
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def fetch_snapshot(self, symbol):
        self.snapshot_calls += 1
        return self._pop(self.snapshots)

    def fetch_daily_history(self, symbol, *args, **kwargs):
        self.history_calls += 1
        return self._pop(self.histories)


class StaticHttpResponse:
    def __init__(self, *, payload=None, content=None, status=200):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class StaticTransport:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.response


def tencent_live_payload(timestamp="20260923100000", *, volume="1200", turnover="15500"):
    fields = [""] * 38
    fields[1:6] = ["贵州茅台", "600519", "1252.81", "1253.80", "1255.03"]
    fields[30:33] = [timestamp, "-0.99", "-0.08"]
    fields[36:38] = [volume, turnover]
    return ('v_sh600519="' + "~".join(fields) + '";').encode("gb18030")


def snapshot(provider="hithink", *, observed=NOW, status="VALID", request="req-1",
             fields=None, units=None, request_status="SUCCESS", trade_date="2026-09-23"):
    return ProviderResult(
        canonical_symbol="600519.SH", exchange="SH", provider=provider,
        observed_at=observed.isoformat() if isinstance(observed, dt.datetime) else observed,
        trade_date=trade_date, source_request_id=request, data_status=status,
        value_fields=fields or {"last_price": 1500.0, "prev_price": 1498.0,
                                "volume": 1000.0, "turnover": 1500000.0},
        field_units=units or dict(PRICE_UNITS), request_status=request_status)


def daily_result(provider="hithink", dates=("2026-09-22", "2026-09-23"), *,
                 adjust="forward", request_status="SUCCESS", status="VALID",
                 observed=NOW, bars=None):
    bars = bars if bars is not None else tuple(DailyBar(day, {
        "open_price": 10.0, "high_price": 11.0, "low_price": 9.0,
        "close_price": 10.5, "volume": 10000.0, "turnover": 105000.0,
    }, dict(BAR_UNITS)) for day in dates)
    return ProviderResult(
        canonical_symbol="600519.SH", exchange="SH", provider=provider,
        observed_at=observed.isoformat() if isinstance(observed, dt.datetime) else observed,
        trade_date=None, source_request_id=f"{provider}-history-1", data_status=status,
        adjustment_mode=adjust, bars=tuple(bars), request_status=request_status)


class MarketDataServiceTests(unittest.TestCase):
    def make_service(self, primary=(), secondary=(), *, phase="CONTINUOUS",
                     trading_day=True, max_age=120, retry=RetryPolicy(),
                     clock=None, calendar=None):
        self.clock = clock or FakeClock()
        self.primary = primary if isinstance(primary, FakeProvider) else FakeProvider("hithink", snapshots=primary)
        self.secondary = secondary if isinstance(secondary, FakeProvider) else FakeProvider("tencent", snapshots=secondary)
        return MarketDataService(
            [self.primary, self.secondary], clock=self.clock,
            calendar=calendar or FakeCalendar(phase=phase, trading_day=trading_day),
            freshness_policy=FreshnessPolicy({
                "CONTINUOUS": max_age, "PRE_OPEN": 300, "LUNCH_BREAK": 1800,
                "AFTER_HOURS": 900,
            }, approved_for_decisions=False, policy_id="TEST_PROPOSAL_ONLY"),
            retry_policy=retry, sleeper=self.clock.sleep)

    def quote(self, service, requirement="PRICE_ONLY"):
        return service.get_snapshot("600519.SH", requirement, consumer="isolated-test")

    def test_primary_valid_returns_trace_and_unapproved_policy_not_decision_eligible(self):
        service = self.make_service([snapshot()])
        result = self.quote(service)
        self.assertEqual((result.provider, result.data_status, result.request_status),
                         ("hithink", "VALID", "SUCCESS"))
        self.assertFalse(result.production_eligible)
        self.assertEqual(len(result.attempts), 1)
        self.assertIsNotNone(result.observed_at)
        self.assertEqual(result.requested_at, result.received_at)

    def test_primary_permission_failure_falls_back_once_without_key_retry(self):
        primary = snapshot(status="UNKNOWN", request_status="PERMISSION_DENIED",
                           request="secret-in-request-id")
        service = self.make_service([primary], [snapshot("tencent", request="safe-id")])
        result = self.quote(service)
        self.assertEqual(result.provider, "tencent")
        self.assertEqual([x.request_status for x in result.attempts],
                         ["PERMISSION_DENIED", "SUCCESS"])
        self.assertEqual(self.primary.snapshot_calls, 1)
        self.assertEqual(result.fallback_reason, "PERMISSION_DENIED")
        self.assertNotIn("secret", repr(result.to_dict()))

    def test_primary_stale_falls_back_to_valid_secondary(self):
        stale = snapshot(observed=NOW - dt.timedelta(seconds=400))
        result = self.quote(self.make_service([stale], [snapshot("tencent")]))
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].data_status, "STALE")

    def test_primary_http_429_is_not_retried_but_secondary_is_tried(self):
        limited = snapshot(status="UNKNOWN", request_status="RATE_LIMITED")
        service = self.make_service([limited], [snapshot("tencent")])
        result = self.quote(service)
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(self.primary.snapshot_calls, 1)

    def test_primary_authentication_failure_fails_over_without_repeating_key(self):
        auth = snapshot(status="UNKNOWN", request_status="AUTHENTICATION_FAILED")
        service = self.make_service([auth], [snapshot("tencent")])
        result = self.quote(service)
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(self.primary.snapshot_calls, 1)
        self.assertEqual(result.attempts[0].request_status, "AUTHENTICATION_FAILED")

    def test_wrong_symbol_is_invalid_and_fails_over(self):
        wrong = ProviderResult(**{**snapshot().__dict__, "canonical_symbol": "000001.SZ"})
        result = self.quote(self.make_service([wrong], [snapshot("tencent")]))
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].data_status, "INVALID")

    def test_required_missing_field_fails_over(self):
        primary = snapshot(fields={"last_price": 10.0}, units={"last_price": "CNY/share"})
        result = self.quote(self.make_service([primary], [snapshot("tencent")]), "PRICE_TURNOVER")
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].data_status, "MISSING")

    def test_both_sources_fail_closed(self):
        primary = snapshot(status="UNKNOWN", request_status="TRANSPORT_ERROR")
        secondary = snapshot("tencent", status="UNKNOWN", request_status="HTTP_ERROR")
        result = self.quote(self.make_service([primary], [secondary]))
        self.assertEqual(result.data_status, "UNKNOWN")
        self.assertFalse(result.production_eligible)
        self.assertEqual(len(result.attempts), 3)  # one bounded transport retry, then secondary

    def test_both_sources_stale_return_stale(self):
        old = NOW - dt.timedelta(seconds=500)
        result = self.quote(self.make_service([snapshot(observed=old)], [snapshot("tencent", observed=old)]))
        self.assertEqual(result.data_status, "STALE")
        self.assertEqual(len(result.attempts), 2)

    def test_secondary_missing_observation_timestamp_is_not_promoted(self):
        missing_time = snapshot("tencent", observed=None)
        result = self.quote(self.make_service(
            [snapshot(status="MISSING")], [missing_time]))
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.data_status, "UNKNOWN")
        self.assertFalse(result.production_eligible)

    def test_naive_or_invalid_timestamp_fails_over(self):
        naive = snapshot(observed="2026-09-23T10:00:00")
        result = self.quote(self.make_service([naive], [snapshot("tencent")]))
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].reason, "OBSERVED_AT_MISSING_OR_INVALID")

    def test_future_timestamp_is_invalid(self):
        future = snapshot(observed=NOW + dt.timedelta(seconds=1))
        result = self.quote(self.make_service([future], [snapshot("tencent")]))
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].data_status, "INVALID")

    def test_wrong_trade_date_is_stale(self):
        wrong_day = snapshot(trade_date="2026-09-22")
        result = self.quote(self.make_service([wrong_day], [snapshot("tencent")]))
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].reason, "TRADE_DATE_OBSERVED_AT_MISMATCH")

    def test_price_only_does_not_require_volume_or_turnover(self):
        price_only = snapshot(fields={"last_price": 100.0}, units={"last_price": "CNY/share"}, status="UNKNOWN")
        result = self.quote(self.make_service([price_only]))
        self.assertEqual(result.data_status, "VALID")
        self.assertEqual(result.provider, "hithink")
        self.assertEqual(set(result.value_fields), {"last_price"})

    def test_price_turnover_does_require_turnover(self):
        price_only = snapshot(fields={"last_price": 100.0}, units={"last_price": "CNY/share"}, status="UNKNOWN")
        result = self.quote(self.make_service([price_only], [snapshot("tencent")]), "PRICE_TURNOVER")
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.data_status, "VALID")

    def test_wrong_units_and_abnormal_prices_fail_over(self):
        bad_unit = snapshot(units={**PRICE_UNITS, "last_price": "unknown"})
        bad_price = snapshot(fields={**snapshot().value_fields, "last_price": -1})
        for bad, reason in ((bad_unit, "FIELD_UNIT_LAST_PRICE_MISMATCH"),
                            (bad_price, "INVALID")):
            result = self.quote(self.make_service([bad], [snapshot("tencent")]))
            self.assertEqual(result.provider, "tencent")
            self.assertIn(reason, (result.attempts[0].reason, result.attempts[0].data_status))

    def test_market_sessions_use_injected_context_and_phase_limits(self):
        for phase in ("PRE_OPEN", "LUNCH_BREAK"):
            result = self.quote(self.make_service([snapshot()], phase=phase))
            self.assertEqual(result.data_status, "VALID")
            self.assertEqual(result.session_phase, phase)
        closed = self.make_service([snapshot(status="MISSING")], [snapshot("tencent")],
                                   phase="NON_TRADING", trading_day=False)
        result = self.quote(closed)
        self.assertEqual(result.data_status, "STALE")
        self.assertEqual(result.attempts[-1].reason, "NON_TRADING_SESSION")

    def test_unknown_session_or_missing_phase_limit_fails_closed(self):
        service = self.make_service([snapshot()], calendar=FakeCalendar(phase="UNKNOWN"))
        self.assertEqual(self.quote(service).data_status, "UNKNOWN")

    def test_retry_is_bounded_and_uses_injected_sleeper(self):
        clock = FakeClock()
        transient = snapshot(status="UNKNOWN", request_status="TIMEOUT")
        primary = FakeProvider("hithink", snapshots=[transient, snapshot()])
        secondary = FakeProvider("tencent", snapshots=[])
        service = self.make_service(primary, secondary, clock=clock,
                                    retry=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=2))
        result = self.quote(service)
        self.assertEqual(result.provider, "hithink")
        self.assertEqual(primary.snapshot_calls, 2)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(clock.current, NOW + dt.timedelta(seconds=2))

    def test_retryable_http_5xx_has_one_bounded_retry(self):
        first = ProviderResult("600519.SH", "SH", "hithink", None, None, "r1",
            "UNKNOWN", error_code="HTTP_503", error_reason="HTTP_ERROR", request_status="HTTP_ERROR")
        primary = FakeProvider("hithink", snapshots=[first, snapshot()])
        service = self.make_service(primary, FakeProvider("tencent"),
            retry=RetryPolicy(max_attempts_per_provider=2, backoff_seconds=0))
        result = self.quote(service)
        self.assertEqual(result.provider, "hithink")
        self.assertEqual(primary.snapshot_calls, 2)

    def test_retry_policy_rejects_unbounded_attempts(self):
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts_per_provider=99)

    def test_historical_valid_window_accepted(self):
        service = self.make_service([ ], [ ], calendar=FakeCalendar(
            dates=("2026-09-22", "2026-09-23")))
        service._providers = (FakeProvider("hithink", histories=[daily_result()]), service._providers[1])
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
            adjust="forward", requirement="DAILY_OHLCV", consumer="history-test")
        self.assertEqual((result.provider, result.data_status, result.adjustment_mode),
                         ("hithink", "VALID", "forward"))
        self.assertEqual(len(result.bars), 2)

    def test_historical_window_uses_bar_dates_without_fabricating_observed_at(self):
        history = daily_result(observed=None)
        primary = FakeProvider("hithink", histories=[history])
        service = self.make_service(primary, FakeProvider("tencent"), calendar=FakeCalendar(
            dates=("2026-09-22", "2026-09-23")))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
            adjust="forward", requirement="DAILY_OHLCV", consumer="history-test")
        self.assertEqual(result.data_status, "VALID")
        self.assertIsNone(result.observed_at)
        self.assertEqual([bar.trade_date for bar in result.bars],
                         ["2026-09-22", "2026-09-23"])
        self.assertEqual(set(result.bars[0].value_fields), set(result.required_fields))

    def test_exchange_identity_mismatch_fails_over(self):
        wrong_exchange = ProviderResult(
            canonical_symbol="600519.SH", exchange="SZ", provider="hithink",
            observed_at=NOW.isoformat(), trade_date="2026-09-23",
            source_request_id="req", data_status="VALID",
            value_fields={"last_price": 1500},
            field_units={"last_price": "CNY/share"})
        service = self.make_service([wrong_exchange], [snapshot("tencent")])
        result = self.quote(service, "PRICE_ONLY")
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].reason, "EXCHANGE_MISMATCH")

    def test_historical_adjustment_mismatch_falls_back_then_fails_closed(self):
        primary = FakeProvider("hithink", histories=[daily_result(adjust="none")])
        secondary = FakeProvider("tencent", histories=[daily_result("tencent", adjust="none")])
        service = self.make_service(primary, secondary, calendar=FakeCalendar(
            dates=("2026-09-22", "2026-09-23")))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
            adjust="forward", consumer="history-test")
        self.assertEqual(result.data_status, "INVALID")
        self.assertEqual(result.attempts[0].reason, "ADJUSTMENT_MODE_MISMATCH")

    def test_historical_missing_trading_date_fails_over(self):
        primary = FakeProvider("hithink", histories=[daily_result(dates=("2026-09-23",))])
        secondary = FakeProvider("tencent", histories=[daily_result("tencent")])
        service = self.make_service(primary, secondary, calendar=FakeCalendar(
            dates=("2026-09-22", "2026-09-23")))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
            adjust="forward", consumer="history-test")
        self.assertEqual(result.provider, "tencent")
        self.assertEqual(result.attempts[0].reason, "HISTORY_TRADING_DATE_GAP")

    def test_historical_duplicate_dates_are_invalid(self):
        bars = daily_result().bars
        repeated = FakeProvider("hithink", histories=[daily_result(bars=(bars[0], bars[0]))])
        service = self.make_service(repeated, FakeProvider("tencent", histories=[daily_result("tencent")]),
                                    calendar=FakeCalendar(dates=("2026-09-22",)))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
            adjust="forward", expected_trade_dates=("2026-09-22",), consumer="history-test")
        self.assertEqual(result.attempts[0].reason, "DUPLICATE_BAR_DATE")

    def test_historical_short_window_and_missing_turnover_fail_closed(self):
        short = FakeProvider("hithink", histories=[daily_result(dates=("2026-09-22",))])
        service = self.make_service(short, FakeProvider("tencent", histories=[daily_result("tencent")]),
                                    calendar=FakeCalendar(dates=("2026-09-22",)))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
            adjust="forward", min_bars=2, consumer="history-test")
        self.assertEqual(result.attempts[0].reason, "HISTORY_WINDOW_TOO_SHORT")

        tencent_bar = DailyBar("2026-09-22", {"open_price": 10.0, "high_price": 11.0,
            "low_price": 9.0, "close_price": 10.5, "volume": 1000.0, "turnover": None},
            dict(BAR_UNITS), ("turnover",))
        primary = FakeProvider("hithink", histories=[daily_result(status="MISSING")])
        secondary = FakeProvider("tencent", histories=[daily_result("tencent", dates=("2026-09-22",), bars=(tencent_bar,))])
        service = self.make_service(primary, secondary, calendar=FakeCalendar(dates=("2026-09-22",)))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-22",
            adjust="forward", requirement=FieldRequirement("REQUIRE_TURNOVER",
                frozenset({"open_price", "high_price", "low_price", "close_price", "volume", "turnover"}),
                "history"), consumer="aiq-test")
        self.assertEqual(result.data_status, "MISSING")

    def test_missing_calendar_dates_returns_unknown(self):
        service = self.make_service([], [], calendar=FakeCalendar(dates=()))
        result = service.get_daily_history("600519.SH", "2026-09-22", "2026-09-23",
                                           adjust="forward", consumer="history-test")
        self.assertEqual(result.data_status, "UNKNOWN")

    def test_error_payloads_and_trace_ids_are_redacted(self):
        secret = "offline-fake-secret"
        malicious = snapshot(request=f"req-{secret}-tail")
        result = self.quote(self.make_service([malicious], [snapshot("tencent")]))
        self.assertNotIn(secret, repr(result.to_dict()))
        self.assertTrue(result.source_request_id.startswith("local-hithink-"))

    def test_service_has_no_sqlite_or_network_side_effect_dependency(self):
        import scripts.datasource.market_data_service as module
        self.assertFalse(hasattr(module, "sqlite3"))
        self.assertNotIn("scripts.datasource.db", sys.modules)

    def test_no_primary_or_secondary_result_is_unknown_not_fabricated(self):
        service = self.make_service(
            [RuntimeError("sensitive header=must-not-escape")],
            [snapshot("tencent", status="MISSING")])
        result = self.quote(service)
        self.assertNotIn("sensitive header", repr(result.to_dict()))
        self.assertNotIn("last_price", result.value_fields)
        self.assertNotEqual(result.data_status, "VALID")

    def test_real_adapter_chain_hithink_failure_tencent_valid_for_price_only(self):
        primary_transport = StaticTransport(StaticHttpResponse(payload={
            "code": 2003, "message": "redacted", "request_id": "safe"}))
        secondary_transport = StaticTransport(StaticHttpResponse(
            content=tencent_live_payload()))
        providers = [HiThinkAdapter(api_key="offline-fake", transport=primary_transport),
                     TencentAdapter(transport=secondary_transport,
                                    clock=lambda: NOW.timestamp())]
        service = MarketDataService(providers, clock=FakeClock(),
            calendar=FakeCalendar(),
            freshness_policy=FreshnessPolicy({"CONTINUOUS": 120},
                approved_for_decisions=False, policy_id="TEST_ONLY"),
            retry_policy=RetryPolicy(max_attempts_per_provider=1))
        result = service.get_snapshot("600519.SH", "PRICE_ONLY", consumer="isolated")
        self.assertEqual((result.provider, result.data_status), ("tencent", "VALID"))
        self.assertEqual(result.value_fields, {"last_price": 1252.81})
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(primary_transport.calls, 1)
        self.assertEqual(secondary_transport.calls, 1)
        self.assertFalse(result.production_eligible)

    def test_real_tencent_fallback_requires_verified_volume_and_turnover(self):
        primary_transport = StaticTransport(StaticHttpResponse(payload={"code": 2003}))
        secondary_transport = StaticTransport(StaticHttpResponse(
            content=tencent_live_payload()))
        service = MarketDataService([
            HiThinkAdapter(api_key="offline-fake", transport=primary_transport),
            TencentAdapter(transport=secondary_transport, clock=lambda: NOW.timestamp())],
            clock=FakeClock(), calendar=FakeCalendar(),
            freshness_policy=FreshnessPolicy({"CONTINUOUS": 120},
                approved_for_decisions=False, policy_id="TEST_ONLY"),
            retry_policy=RetryPolicy(max_attempts_per_provider=1))
        result = service.get_snapshot("600519.SH", "PRICE_VOLUME_AMOUNT", consumer="isolated")
        self.assertEqual((result.provider, result.data_status), ("tencent", "VALID"))
        self.assertEqual(result.value_fields["volume"], 120000)
        self.assertEqual(result.value_fields["turnover"], 155000000)

    def test_real_tencent_missing_timestamp_does_not_fallback_as_valid(self):
        primary_transport = StaticTransport(StaticHttpResponse(payload={"code": 2003}))
        secondary_transport = StaticTransport(StaticHttpResponse(
            content=tencent_live_payload(timestamp="")))
        service = MarketDataService([
            HiThinkAdapter(api_key="offline-fake", transport=primary_transport),
            TencentAdapter(transport=secondary_transport, clock=lambda: NOW.timestamp())],
            clock=FakeClock(), calendar=FakeCalendar(),
            freshness_policy=FreshnessPolicy({"CONTINUOUS": 120},
                approved_for_decisions=False, policy_id="TEST_ONLY"),
            retry_policy=RetryPolicy(max_attempts_per_provider=1))
        result = service.get_snapshot("600519.SH", "PRICE_ONLY", consumer="isolated")
        self.assertEqual(result.data_status, "UNKNOWN")
        self.assertFalse(result.production_eligible)
        self.assertEqual(result.attempts[-1].reason, "OBSERVED_AT_MISSING_OR_INVALID")

    def test_real_tencent_future_and_wrong_date_timestamps_fail_closed(self):
        for timestamp, expected_status in (("20260923100100", "INVALID"),
                                           ("20260922150000", "STALE")):
            primary_transport = StaticTransport(StaticHttpResponse(payload={"code": 2003}))
            secondary_transport = StaticTransport(StaticHttpResponse(
                content=tencent_live_payload(timestamp=timestamp)))
            service = MarketDataService([
                HiThinkAdapter(api_key="offline-fake", transport=primary_transport),
                TencentAdapter(transport=secondary_transport, clock=lambda: NOW.timestamp())],
                clock=FakeClock(), calendar=FakeCalendar(),
                freshness_policy=FreshnessPolicy({"CONTINUOUS": 120},
                    approved_for_decisions=False, policy_id="TEST_ONLY"),
                retry_policy=RetryPolicy(max_attempts_per_provider=1))
            result = service.get_snapshot("600519.SH", "PRICE_ONLY", consumer="isolated")
            self.assertEqual(result.data_status, expected_status)
            self.assertFalse(result.production_eligible)

    def test_real_tencent_daily_ohlcv_is_available_but_aiq_turnover_is_not(self):
        class DailyTransport:
            def __init__(self):
                self.calls = 0

            def __call__(self, *args, **kwargs):
                self.calls += 1
                return StaticHttpResponse(payload={"data": {"sh600519": {"qfqday": [[
                    "2026-09-23", "1255.03", "1253.54", "1271.50", "1251.00", "100"
                ]]}}})

        primary_transport = StaticTransport(StaticHttpResponse(payload={"code": 2003}))
        history_transport = DailyTransport()
        service = MarketDataService([
            HiThinkAdapter(api_key="offline-fake", transport=primary_transport),
            TencentAdapter(transport=history_transport, clock=lambda: NOW.timestamp())],
            clock=FakeClock(), calendar=FakeCalendar(dates=("2026-09-23",)),
            freshness_policy=FreshnessPolicy({"CONTINUOUS": 120},
                approved_for_decisions=False, policy_id="TEST_ONLY"),
            retry_policy=RetryPolicy(max_attempts_per_provider=1))
        daily = service.get_daily_history("600519.SH", "2026-09-23", "2026-09-23",
            adjust="forward", requirement="DAILY_OHLCV", consumer="isolated")
        self.assertEqual((daily.provider, daily.data_status), ("tencent", "VALID"))
        self.assertEqual(daily.bars[0].value_fields["volume"], 10000)
        self.assertEqual(daily.bars[0].field_units["volume"], "share")
        self.assertNotIn("turnover", daily.bars[0].value_fields)

        # A separate call requiring turnover cannot use the same row as a full
        # AIQ-feature-history bar; no estimated amount is synthesized.
        service._providers = (FakeProvider("hithink", histories=[daily_result(status="MISSING")]),
                              service._providers[1])
        aiq = service.get_daily_history("600519.SH", "2026-09-23", "2026-09-23",
            adjust="forward", requirement="AIQ_FEATURE_HISTORY", min_bars=1,
            expected_trade_dates=("2026-09-23",), consumer="isolated")
        self.assertNotEqual(aiq.data_status, "VALID")
        self.assertNotIn("turnover", aiq.bars[0].value_fields if aiq.bars else {})


if __name__ == "__main__":
    unittest.main()
