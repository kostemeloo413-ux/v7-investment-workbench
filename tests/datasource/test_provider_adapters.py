import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from scripts.datasource.provider_adapters import (
    HITHINK_HISTORICAL_PATH,
    HITHINK_SNAPSHOT_PATH,
    HiThinkAdapter,
    TencentAdapter,
    canonicalize_symbol,
)


class FakeResponse:
    def __init__(self, payload=None, status=200, content=None):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, url, *, params, headers, timeout):
        self.calls.append((url, params, headers, timeout))
        if self.error:
            raise self.error
        return self.response


def snapshot_payload(**overrides):
    data = {
        "timestamp": 1780138800000,
        "item": [{
            "thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
            "last_price": 1500.0, "price_change": 2.0,
            "price_change_ratio_pct": 0.13, "open_price": 1498.0,
            "high_price": 1502.0, "low_price": 1490.0,
            "prev_price": 1498.0, "volume": 123456,
            "turnover": 185000000.0,
        }],
    }
    data.update(overrides)
    return {"code": 0, "message": "success", "request_id": "trace-1", "data": data}


def history_payload(**overrides):
    data = {
        "timestamp": 1780138800000,
        "thscode": "600519.SH",
        "adjust": "forward",
        "item": [{"date_ms": 1780070400000, "open_price": 1498.0,
                  "high_price": 1510.0, "low_price": 1490.0,
                  "close_price": 1500.0, "volume": 123456,
                  "turnover": 185000000.0}],
    }
    data.update(overrides)
    return {"code": 0, "message": "success", "request_id": "history-1", "data": data}


def tencent_quote_bytes(*, symbol="600519", name="贵州茅台", timestamp="20260923100000",
                        volume="1234", turnover="18500", market="sh"):
    fields = [""] * 38
    fields[1] = name
    fields[2] = symbol
    fields[3:6] = ["1500.0", "1498.0", "1499.0"]
    fields[30] = timestamp
    fields[31] = "2.0"
    fields[32] = "0.13"
    fields[36] = volume
    fields[37] = turnover
    line = f'v_{market}{symbol}="' + "~".join(fields) + '";'
    return line.encode("gbk")


def test_symbol_normalization_accepts_provider_forms():
    assert canonicalize_symbol("sh600519") == ("600519.SH", "SH")
    assert canonicalize_symbol("000001.sz") == ("000001.SZ", "SZ")


def test_hithink_snapshot_normalizes_quote_and_auth_without_leaking_key():
    key = "test-secret-do-not-print"
    transport = FakeTransport(FakeResponse(snapshot_payload()))
    result = HiThinkAdapter(api_key=key, transport=transport).fetch_snapshot("sh600519")
    assert result.data_status == "VALID"
    assert result.canonical_symbol == "600519.SH"
    assert result.provider == "hithink"
    assert result.source_request_id == "trace-1"
    assert result.value_fields["volume"] == 123456
    assert result.field_units["volume"] == "share"
    assert transport.calls[0][0].endswith(HITHINK_SNAPSHOT_PATH)
    assert transport.calls[0][2] == {"X-api-key": key}
    assert key not in repr(result.to_dict())


def test_hithink_daily_k_requires_explicit_adjust_and_normalizes_bar():
    transport = FakeTransport(FakeResponse(history_payload()))
    result = HiThinkAdapter(api_key="offline-test", transport=transport).fetch_daily_history(
        "600519.SH", 1780000000000, 1780138800000, adjust="forward")
    assert result.data_status == "VALID"
    assert result.adjustment_mode == "forward"
    assert len(result.bars) == 1
    assert result.bars[0].field_units["volume"] == "share"
    assert result.bars[0].field_units["turnover"] == "CNY"
    assert transport.calls[0][0].endswith(HITHINK_HISTORICAL_PATH)
    assert transport.calls[0][1]["adjust"] == "forward"
    assert transport.calls[0][1]["interval"] == "1d"


def test_hithink_business_errors_fail_closed():
    expected = [(2001, "AUTHENTICATION_FAILED"), (2003, "CAPABILITY_FORBIDDEN"),
                (2400, "PROVIDER_BUSINESS_ERROR"), (4001, "RATE_LIMITED")]
    for code, reason in expected:
        response = FakeResponse({"code": code, "message": "sensitive-like message", "request_id": "r"})
        result = HiThinkAdapter(api_key="offline-test", transport=FakeTransport(response)).fetch_snapshot("600519.SH")
        assert result.data_status == "UNKNOWN"
        assert result.request_status in {"AUTHENTICATION_FAILED", "PERMISSION_DENIED", "PROVIDER_ERROR", "RATE_LIMITED"}
        assert result.error_code == str(code)
        assert result.error_reason == reason
        assert result.request_status != "SUCCESS"
        assert "sensitive-like" not in repr(result.to_dict())


def test_hithink_missing_credentials_does_not_send_request():
    transport = FakeTransport(FakeResponse(snapshot_payload()))
    result = HiThinkAdapter(api_key="", transport=transport).fetch_snapshot("600519.SH")
    assert result.error_code == "AUTH_NOT_CONFIGURED"
    assert transport.calls == []


def test_hithink_missing_data_and_missing_item_are_not_success():
    no_data = HiThinkAdapter(api_key="x", transport=FakeTransport(
        FakeResponse({"code": 0, "request_id": "r"}))).fetch_snapshot("600519.SH")
    no_item = HiThinkAdapter(api_key="x", transport=FakeTransport(
        FakeResponse({"code": 0, "request_id": "r", "data": {"timestamp": 1780138800000, "item": []}})
    )).fetch_snapshot("600519.SH")
    assert no_data.data_status == "MISSING"
    assert no_item.data_status == "MISSING"


def test_hithink_wrong_symbol_and_bad_timestamp_rejected():
    wrong = snapshot_payload(item=[{"thscode": "000001.SZ", "last_price": 10.0}])
    bad_timestamp = snapshot_payload(timestamp="not-a-timestamp")
    assert HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(wrong))).fetch_snapshot(
        "600519.SH").data_status == "INVALID"
    assert HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(bad_timestamp))).fetch_snapshot(
        "600519.SH").error_code == "TIMESTAMP_INVALID"


def test_hithink_bad_field_type_and_adjustment_mismatch_rejected():
    payload = snapshot_payload()
    payload["data"]["item"][0]["last_price"] = "1500"
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(payload))).fetch_snapshot("600519.SH")
    assert result.data_status == "INVALID"
    adjust_payload = history_payload(adjust="none")
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(adjust_payload))).fetch_daily_history(
        "600519.SH", 1780000000000, 1780138800000, adjust="forward")
    assert result.error_code == "ADJUSTMENT_MISMATCH"


def test_hithink_invalid_ohlc_rejected():
    payload = history_payload(item=[{"date_ms": 1780070400000, "open_price": 1498,
        "high_price": 1400, "low_price": 1490, "close_price": 1500,
        "volume": 10, "turnover": 10000}])
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(payload))).fetch_daily_history(
        "600519.SH", 1780000000000, 1780138800000, adjust="forward")
    assert result.data_status == "INVALID"
    assert result.error_code == "OHLC_INVALID"


def test_hithink_rejects_bars_outside_requested_window_and_duplicate_dates():
    outside = history_payload(item=[{"date_ms": 1779000000000, "open_price": 1498,
        "high_price": 1510, "low_price": 1490, "close_price": 1500,
        "volume": 10, "turnover": 10000}])
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(outside))).fetch_daily_history(
        "600519.SH", 1780000000000, 1780138800000, adjust="forward")
    assert result.error_code == "BAR_DATE_OUTSIDE_REQUEST"
    repeated = history_payload(item=[history_payload()["data"]["item"][0]] * 2)
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(FakeResponse(repeated))).fetch_daily_history(
        "600519.SH", 1780000000000, 1780138800000, adjust="forward")
    assert result.error_code == "DUPLICATE_BAR_DATE"


def test_transport_exception_never_exposes_exception_text_or_key():
    secret = "a-test-api-key"
    result = HiThinkAdapter(api_key=secret,
        transport=FakeTransport(error=RuntimeError("header=" + secret))).fetch_snapshot("600519.SH")
    rendered = repr(result.to_dict())
    assert result.data_status == "UNKNOWN"
    assert result.request_status == "TRANSPORT_ERROR"
    assert secret not in rendered
    assert "header=" not in rendered


def test_hithink_chinese_security_name_is_utf8_safe():
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(
        FakeResponse(snapshot_payload()))).fetch_snapshot("600519.SH")
    assert result.value_fields["name"] == "贵州茅台"
    assert "贵州茅台" in json.dumps(result.to_dict(), ensure_ascii=False)


def test_tencent_snapshot_normalizes_live_verified_timestamp_and_units():
    transport = FakeTransport(FakeResponse(status=200, content=tencent_quote_bytes()))
    result = TencentAdapter(transport=transport, clock=lambda: 1780138800).fetch_snapshot("600519.SH")
    assert result.provider == "tencent"
    assert result.data_status == "VALID"
    assert result.canonical_symbol == "600519.SH"
    assert result.observed_at == "2026-09-23T10:00:00+08:00"
    assert result.trade_date == "2026-09-23"
    assert result.value_fields["name"] == "贵州茅台"
    assert result.value_fields["volume"] == 123400
    assert result.field_units["volume"] == "share"
    assert result.value_fields["turnover"] == 185000000
    assert result.field_units["turnover"] == "CNY"
    assert "observed_at" not in result.unavailable_fields
    assert "贵州茅台".encode("gbk") in tencent_quote_bytes()


def test_tencent_missing_time_stays_unknown_and_malformed_time_is_invalid():
    missing = TencentAdapter(transport=FakeTransport(FakeResponse(
        status=200, content=tencent_quote_bytes(timestamp="")))).fetch_snapshot("600519.SH")
    malformed = TencentAdapter(transport=FakeTransport(FakeResponse(
        status=200, content=tencent_quote_bytes(timestamp="2026-09-23 10:00:00")))).fetch_snapshot("600519.SH")
    assert missing.data_status == "UNKNOWN"
    assert missing.observed_at is None
    assert "observed_at" in missing.unavailable_fields
    assert malformed.data_status == "INVALID"
    assert malformed.error_code == "QUOTE_TIMESTAMP_INVALID"


def test_tencent_snapshot_rejects_wrong_symbol_and_bad_numeric_fields():
    wrong_fields = tencent_quote_bytes().decode("gbk").replace("~600519~", "~000001~")
    wrong = TencentAdapter(transport=FakeTransport(FakeResponse(
        status=200, content=wrong_fields.encode("gbk")))).fetch_snapshot("600519.SH")
    bad_fields = tencent_quote_bytes(volume="-1")
    bad = TencentAdapter(transport=FakeTransport(FakeResponse(
        status=200, content=bad_fields))).fetch_snapshot("600519.SH")
    assert wrong.data_status == "INVALID"
    assert bad.data_status == "INVALID"


def test_tencent_timestamp_parser_rejects_impossible_calendar_time():
    result = TencentAdapter(transport=FakeTransport(FakeResponse(
        status=200, content=tencent_quote_bytes(timestamp="20261340120000")))).fetch_snapshot("600519.SH")
    assert result.data_status == "INVALID"
    assert result.error_code == "QUOTE_TIMESTAMP_INVALID"


def test_tencent_sz_market_code_is_normalized_and_name_keeps_gb18030_text():
    result = TencentAdapter(transport=FakeTransport(FakeResponse(
        status=200, content=tencent_quote_bytes(symbol="000001", name="平安银行",
                                                  market="sz")))).fetch_snapshot("000001.SZ")
    assert result.canonical_symbol == "000001.SZ"
    assert result.exchange == "SZ"
    assert result.value_fields["name"] == "平安银行"
    assert result.observed_at.endswith("+08:00")


def test_tencent_daily_normalizes_volume_and_keeps_turnover_unavailable():
    payload = {"data": {"sh600519": {"qfqday": [["2026-06-01", "1498", "1500", "1510", "1490", "12.5"]]}}}
    transport = FakeTransport(FakeResponse(payload))
    result = TencentAdapter(transport=transport, clock=lambda: 1780138800).fetch_daily_history(
        "600519.SH", "2026-06-01", "2026-06-02")
    assert result.adjustment_mode == "forward"
    assert result.data_status == "UNKNOWN"
    assert result.bars[0].value_fields["volume"] == 1250
    assert result.bars[0].value_fields["turnover"] is None
    assert result.bars[0].field_units["volume"] == "share"
    assert result.bars[0].unavailable_fields == ("turnover",)
    assert {"turnover", "observed_at"}.issubset(result.unavailable_fields)
    assert result.observed_at is None


def test_tencent_daily_malformed_volume_fails_closed():
    payload = {"data": {"sh600519": {"qfqday": [[
        "2026-06-01", "1498", "1500", "1510", "1490", "not-a-verified-unit"
    ]]}}}
    result = TencentAdapter(transport=FakeTransport(FakeResponse(payload))).fetch_daily_history(
        "600519.SH", "2026-06-01", "2026-06-02")
    assert result.data_status == "INVALID"
    assert result.error_code == "BAR_VOLUME_INVALID"


def test_tencent_daily_unadjusted_response_is_labeled_none():
    payload = {"data": {"sh600519": {"day": [["2026-06-01", "1498", "1500", "1510", "1490", "12"]]}}}
    result = TencentAdapter(transport=FakeTransport(FakeResponse(payload))).fetch_daily_history(
        "600519.SH", "2026-06-01", "2026-06-02")
    assert result.adjustment_mode == "none"


def test_tencent_rejects_invalid_ohlc_and_malformed_known_quote_field():
    bad_bar = {"data": {"sh600519": {"qfqday": [["2026-06-01", "1498", "1500", "1400", "1490", "12"]]}}}
    result = TencentAdapter(transport=FakeTransport(FakeResponse(bad_bar))).fetch_daily_history(
        "600519.SH", "2026-06-01", "2026-06-02")
    assert result.data_status == "INVALID"
    fields = [""] * 38
    fields[2] = "600519"
    fields[3:6] = ["1500", "1498", "1499"]
    fields[30] = "20260923100000"
    fields[3] = "not-a-price"
    fields[31:33] = ["2", "0.1"]
    fields[36:38] = ["100", "1000"]
    raw = ('v_sh600519="' + "~".join(fields) + '";').encode("gbk")
    bad_quote = TencentAdapter(transport=FakeTransport(FakeResponse(status=200, content=raw)))
    assert bad_quote.fetch_snapshot("600519.SH").error_code == "QUOTE_FIELDS_INVALID"


def test_tencent_daily_rejects_invalid_volume_and_preserves_adjustment_label():
    payload = {"data": {"sh600519": {"qfqday": [[
        "2026-06-01", "1498", "1500", "1510", "1490", "-1"
    ]]}}}
    result = TencentAdapter(transport=FakeTransport(FakeResponse(payload))).fetch_daily_history(
        "600519.SH", "2026-06-01", "2026-06-02")
    assert result.data_status == "INVALID"
    assert result.adjustment_mode == "forward"


def test_http_failure_is_reported_without_response_body():
    response = FakeResponse({"secret": "must not leak"}, status=403)
    result = HiThinkAdapter(api_key="x", transport=FakeTransport(response)).fetch_snapshot("600519.SH")
    assert result.error_code == "HTTP_403"
    assert result.request_status == "HTTP_ERROR"
    assert "must not leak" not in repr(result.to_dict())


if __name__ == "__main__":
    tests = [(name, func) for name, func in globals().items()
             if name.startswith("test_") and callable(func)]
    for name, test in tests:
        test()
    print(f"provider adapter isolated tests: {len(tests)}/{len(tests)} passed")
