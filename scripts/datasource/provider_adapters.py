# -*- coding: utf-8 -*-
"""Provider-neutral, non-persistent market-data adapters.

This module is deliberately not wired into production callers.  It performs
fetch/normalization only; database persistence, freshness arbitration, retries,
and cross-provider fallback belong to separately reviewed work.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
HITHINK_BASE_URL = "https://fuyao.aicubes.cn"
HITHINK_SNAPSHOT_PATH = "/api/a-share/prices/snapshot"
HITHINK_HISTORICAL_PATH = "/api/a-share/prices/historical"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


Transport = Callable[..., HttpResponse]


@dataclass(frozen=True)
class DailyBar:
    trade_date: str
    value_fields: Mapping[str, Optional[float]]
    field_units: Mapping[str, str]
    unavailable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderResult:
    canonical_symbol: str
    exchange: Optional[str]
    provider: str
    observed_at: Optional[str]
    trade_date: Optional[str]
    source_request_id: Optional[str]
    data_status: str
    value_fields: Mapping[str, Any] = field(default_factory=dict)
    field_units: Mapping[str, str] = field(default_factory=dict)
    adjustment_mode: str = "not_applicable"
    error_code: Optional[str] = None
    error_reason: Optional[str] = None
    unavailable_fields: tuple[str, ...] = ()
    bars: tuple[DailyBar, ...] = ()
    # Transport/business outcome is deliberately orthogonal to data quality.
    # Defaults preserve construction compatibility for the P1A fixtures.
    request_status: str = "SUCCESS"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe result. Credentials are never part of this model."""
        result = asdict(self)
        result["bars"] = [asdict(bar) for bar in self.bars]
        return result


def canonicalize_symbol(symbol: str) -> tuple[str, str]:
    """Normalize a mainland A-share symbol to ``NNNNNN.SH|SZ``."""
    if not isinstance(symbol, str):
        raise ValueError("INVALID_SYMBOL")
    value = symbol.strip().upper()
    match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ))?", value)
    if match:
        code, suffix = match.groups()
        exchange = suffix or ("SH" if code.startswith(("5", "6", "9")) else "SZ")
        if exchange == "SH" and not code.startswith(("5", "6", "9")):
            raise ValueError("SYMBOL_EXCHANGE_MISMATCH")
        if exchange == "SZ" and code.startswith(("5", "6", "9")):
            raise ValueError("SYMBOL_EXCHANGE_MISMATCH")
        return f"{code}.{exchange}", exchange
    match = re.fullmatch(r"(SH|SZ)(\d{6})", value)
    if match:
        exchange, code = match.groups()
        return canonicalize_symbol(f"{code}.{exchange}")
    raise ValueError("INVALID_SYMBOL")


def _default_transport(url: str, *, params=None, headers=None, timeout=8):
    # Lazy import keeps offline tests and import-time behavior independent of
    # HTTP libraries. Request exceptions are caught without formatting headers.
    import requests

    return requests.get(url, params=params, headers=headers, timeout=timeout)


def _request_id() -> str:
    return "client-" + uuid.uuid4().hex


def _timestamp_ms(value: Any) -> Optional[_datetime.datetime]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value <= 0:
        return None
    try:
        return _datetime.datetime.fromtimestamp(float(value) / 1000.0, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(dt: Optional[_datetime.datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _http_json(transport: Transport, url: str, *, params, headers, timeout):
    try:
        response = transport(url, params=params, headers=headers, timeout=timeout)
    except Exception as exc:  # Never include exception text/request headers.
        return None, None, type(exc).__name__, "TRANSPORT_ERROR"
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return None, None, "INVALID_HTTP_RESPONSE", "INVALID_RESPONSE"
    if status < 200 or status >= 300:
        return status, None, f"HTTP_{status}", "HTTP_ERROR"
    try:
        payload = response.json()
    except Exception as exc:
        return status, None, type(exc).__name__, "INVALID_JSON"
    if not isinstance(payload, dict):
        return status, None, "JSON_OBJECT_REQUIRED", "INVALID_RESPONSE"
    return status, payload, None, None


def _safe_error_result(symbol: str, exchange: Optional[str], provider: str,
                       request_id: str, code: str, reason: str,
                       adjustment_mode="not_applicable",
                       request_status: Optional[str] = None) -> ProviderResult:
    if request_status is None:
        if code == "AUTH_NOT_CONFIGURED":
            request_status = "CREDENTIAL_MISSING"
        elif code in ("2001", "AUTHENTICATION_FAILED"):
            request_status = "AUTHENTICATION_FAILED"
        elif code in ("2003", "CAPABILITY_FORBIDDEN"):
            request_status = "PERMISSION_DENIED"
        elif code in ("4001", "HTTP_429"):
            request_status = "RATE_LIMITED"
        elif reason == "TRANSPORT_ERROR":
            request_status = "TIMEOUT" if "timeout" in code.lower() else "TRANSPORT_ERROR"
        elif reason == "HTTP_ERROR":
            request_status = "HTTP_ERROR"
        elif reason == "INVALID_JSON" or code in ("INVALID_HTTP_RESPONSE", "JSON_OBJECT_REQUIRED", "BUSINESS_CODE_INVALID"):
            request_status = "INVALID_RESPONSE"
        elif reason == "PROVIDER_BUSINESS_ERROR":
            request_status = "PROVIDER_ERROR"
        else:
            request_status = "SUCCESS"
    if request_status != "SUCCESS":
        data_status = "UNKNOWN"
    elif reason == "MISSING":
        data_status = "MISSING"
    elif reason == "INVALID":
        data_status = "INVALID"
    else:
        data_status = "UNKNOWN"
    return ProviderResult(
        canonical_symbol=symbol,
        exchange=exchange,
        provider=provider,
        observed_at=None,
        trade_date=None,
        source_request_id=request_id,
        data_status=data_status,
        adjustment_mode=adjustment_mode,
        error_code=code,
        error_reason=reason,
        request_status=request_status,
    )


class HiThinkAdapter:
    provider = "hithink"

    def __init__(self, api_key: Optional[str] = None,
                 transport: Optional[Transport] = None, timeout: float = 8.0):
        # Do not load dotenv here: application bootstrap owns environment setup.
        self._api_key = api_key if api_key is not None else os.environ.get(
            "HITHINK_FINANCE_API_KEY", "")
        self._transport = transport or _default_transport
        self._timeout = timeout

    def _call(self, path: str, params: Mapping[str, Any], symbol: str,
              exchange: str, adjustment_mode="not_applicable"):
        request_id = _request_id()
        if not self._api_key:
            return None, _safe_error_result(symbol, exchange, self.provider,
                request_id, "AUTH_NOT_CONFIGURED", "API_KEY_MISSING", adjustment_mode)
        status, payload, detail, failure = _http_json(
            self._transport, HITHINK_BASE_URL + path, params=dict(params),
            headers={"X-api-key": self._api_key}, timeout=self._timeout)
        if failure:
            return None, _safe_error_result(symbol, exchange, self.provider,
                request_id, detail or failure, failure, adjustment_mode)
        candidate_request_id = payload.get("request_id")
        if isinstance(candidate_request_id, str) and candidate_request_id.strip():
            # A provider-controlled identifier is never allowed to echo a
            # credential or arbitrary response text into evidence.
            if self._api_key and self._api_key in candidate_request_id:
                candidate_request_id = None
            if candidate_request_id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", candidate_request_id):
                request_id = candidate_request_id
        code = payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            return None, _safe_error_result(symbol, exchange, self.provider,
                request_id, "BUSINESS_CODE_INVALID", "BUSINESS_CODE_INVALID", adjustment_mode)
        if code != 0:
            # Do not copy arbitrary server messages into logs or result details.
            reason = {2001: "AUTHENTICATION_FAILED", 2003: "CAPABILITY_FORBIDDEN",
                      4001: "RATE_LIMITED"}.get(
                code, "PROVIDER_BUSINESS_ERROR")
            request_status = {2001: "AUTHENTICATION_FAILED", 2003: "PERMISSION_DENIED",
                              4001: "RATE_LIMITED"}.get(code, "PROVIDER_ERROR")
            return None, _safe_error_result(symbol, exchange, self.provider,
                request_id, str(code), reason, adjustment_mode, request_status)
        data = payload.get("data")
        if not isinstance(data, dict):
            return None, _safe_error_result(symbol, exchange, self.provider,
                request_id, "DATA_MISSING", "MISSING", adjustment_mode)
        return (data, request_id), None

    def fetch_snapshot(self, symbol: str) -> ProviderResult:
        try:
            canonical, exchange = canonicalize_symbol(symbol)
        except ValueError as exc:
            return _safe_error_result(str(symbol), None, self.provider,
                _request_id(), str(exc), "INVALID")
        data_pair, error = self._call(HITHINK_SNAPSHOT_PATH,
            {"thscodes": canonical}, canonical, exchange)
        if error:
            return error
        data, request_id = data_pair
        items = data.get("item")
        if not isinstance(items, list) or not items:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "ITEM_MISSING", "MISSING")
        matching = []
        for item in items:
            if isinstance(item, dict) and item.get("thscode") == canonical:
                matching.append(item)
        if len(matching) != 1:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "SYMBOL_MISMATCH", "INVALID")
        item = matching[0]
        observed = _timestamp_ms(data.get("timestamp"))
        if observed is None:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "TIMESTAMP_INVALID", "INVALID")
        mapping = {
            "name": "name", "last_price": "last_price", "price_change": "price_change",
            "price_change_ratio_pct": "price_change_ratio_pct",
            "open_price": "open_price", "high_price": "high_price",
            "low_price": "low_price", "prev_price": "prev_price",
            "volume": "volume", "turnover": "turnover",
        }
        units = {"name": "text", "last_price": "CNY/share", "price_change": "CNY/share",
            "price_change_ratio_pct": "percent", "open_price": "CNY/share",
            "high_price": "CNY/share", "low_price": "CNY/share",
            "prev_price": "CNY/share", "volume": "share", "turnover": "CNY"}
        values, unavailable = {}, []
        for output, source in mapping.items():
            value = item.get(source)
            if output == "name":
                if isinstance(value, str) and value.strip():
                    values[output] = value
                else:
                    unavailable.append(output)
                continue
            parsed = _number(value)
            if parsed is None:
                unavailable.append(output)
            else:
                values[output] = parsed
        if values.get("last_price", 0) <= 0:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "LAST_PRICE_INVALID", "INVALID")
        if any(values.get(k, 0) < 0 for k in ("volume", "turnover")):
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "NEGATIVE_MARKET_FIELD", "INVALID")
        status = "UNKNOWN" if unavailable else "VALID"
        return ProviderResult(canonical, exchange, self.provider, _iso(observed),
            observed.date().isoformat(), request_id, status, values, units,
            unavailable_fields=tuple(unavailable))

    def fetch_daily_history(self, symbol: str, start_ms: int, end_ms: int,
                            *, adjust: str) -> ProviderResult:
        try:
            canonical, exchange = canonicalize_symbol(symbol)
        except ValueError as exc:
            return _safe_error_result(str(symbol), None, self.provider,
                _request_id(), str(exc), "INVALID", str(adjust))
        if adjust not in ("none", "forward", "backward"):
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "ADJUST_INVALID", "INVALID", str(adjust))
        if (isinstance(start_ms, bool) or isinstance(end_ms, bool) or
            not isinstance(start_ms, int) or not isinstance(end_ms, int) or
            start_ms <= 0 or end_ms < start_ms):
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "DATE_WINDOW_INVALID", "INVALID", adjust)
        start_dt, end_dt = _timestamp_ms(start_ms), _timestamp_ms(end_ms)
        if start_dt is None or end_dt is None:
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "DATE_WINDOW_INVALID", "INVALID", adjust)
        try:
            ten_year_limit = start_dt.replace(year=start_dt.year + 10)
        except ValueError:  # Feb 29 -> the last valid day in the target year.
            ten_year_limit = start_dt.replace(year=start_dt.year + 10, day=28)
        if end_dt > ten_year_limit:
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "DATE_WINDOW_TOO_LONG", "INVALID", adjust)
        data_pair, error = self._call(HITHINK_HISTORICAL_PATH,
            {"thscode": canonical, "interval": "1d", "start": start_ms,
             "end": end_ms, "adjust": adjust}, canonical, exchange, adjust)
        if error:
            return error
        data, request_id = data_pair
        if data.get("thscode") != canonical:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "SYMBOL_MISMATCH", "INVALID", adjust)
        returned_adjust = data.get("adjust")
        if returned_adjust != adjust:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "ADJUSTMENT_MISMATCH", "INVALID", adjust)
        observed = _timestamp_ms(data.get("timestamp"))
        items = data.get("item")
        if observed is None or not isinstance(items, list):
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, "HISTORICAL_DATA_INVALID", "INVALID", adjust)
        bars, errors, seen_dates = [], [], set()
        for item in items:
            if not isinstance(item, dict):
                errors.append("BAR_NOT_OBJECT")
                continue
            date = _timestamp_ms(item.get("date_ms"))
            if date is None:
                errors.append("BAR_DATE_INVALID")
                continue
            bar_date = date.date().isoformat()
            if bar_date < start_dt.date().isoformat() or bar_date > end_dt.date().isoformat():
                errors.append("BAR_DATE_OUTSIDE_REQUEST")
                continue
            if bar_date in seen_dates:
                errors.append("DUPLICATE_BAR_DATE")
                continue
            seen_dates.add(bar_date)
            values = {}
            for key in ("open_price", "high_price", "low_price", "close_price", "volume", "turnover"):
                number = _number(item.get(key))
                if number is None:
                    errors.append("BAR_FIELD_INVALID")
                    break
                values[key] = number
            else:
                o, h, low, close = (values[k] for k in
                    ("open_price", "high_price", "low_price", "close_price"))
                if min(o, h, low, close) <= 0 or h < max(o, low, close) or low > min(o, h, close):
                    errors.append("OHLC_INVALID")
                    continue
                if values["volume"] < 0 or values["turnover"] < 0:
                    errors.append("NEGATIVE_VOLUME_OR_TURNOVER")
                    continue
                bars.append(DailyBar(bar_date, values,
                    {"open_price": "CNY/share", "high_price": "CNY/share",
                     "low_price": "CNY/share", "close_price": "CNY/share",
                     "volume": "share", "turnover": "CNY"}))
        if errors:
            return _safe_error_result(canonical, exchange, self.provider,
                request_id, errors[0], "INVALID", adjust)
        if not bars:
            return ProviderResult(canonical, exchange, self.provider, _iso(observed),
                None, request_id, "MISSING", {}, {}, adjust,
                "NO_BARS", "NO_HISTORICAL_BARS")
        return ProviderResult(canonical, exchange, self.provider, _iso(observed),
            None, request_id, "VALID", {}, {}, adjust, bars=tuple(bars))


class TencentAdapter:
    """Adapter over Tencent's existing qt.gtimg.cn and fqkline protocols.

    This is an isolated fetch/normalize entry point, not production routing.
    Tencent's public positional protocol has no first-party schema located by
    this project. The live-verified fields are parsed conservatively: quote
    field 30 as an Asia/Shanghai wall-clock observation time, field 36 as lots,
    field 37 as CNY ten-thousands, and daily-row field 5 as lots. These unit
    mappings are supported by bounded live samples, cross-endpoint consistency,
    and community protocol references, not a Tencent first-party guarantee.
    All accepted units are normalized to shares and CNY. This adapter remains
    isolated and is not wired into production routing.
    """

    provider = "tencent"

    def __init__(self, transport: Optional[Transport] = None, timeout: float = 8.0,
                 clock: Callable[[], float] = time.time):
        self._transport = transport or _default_transport
        self._timeout = timeout
        self._clock = clock

    @staticmethod
    def _tencent_code(canonical: str) -> str:
        code, exchange = canonical.split(".")
        return exchange.lower() + code

    @staticmethod
    def _quote_observed_at(value: str) -> Optional[_datetime.datetime]:
        """Parse Tencent's YYYYMMDDhhmmss market-local quote timestamp.

        The wire field carries no UTC offset. Tencent's A-share quote endpoint
        is interpreted in exchange-local Asia/Shanghai time; this assumption is
        explicit here and the MarketDataService still validates date/session/
        freshness before a consumer can accept the quote.
        """
        if not isinstance(value, str) or not re.fullmatch(r"\d{14}", value):
            return None
        try:
            parsed = _datetime.datetime.strptime(value, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=SHANGHAI)

    def fetch_snapshot(self, symbol: str) -> ProviderResult:
        try:
            canonical, exchange = canonicalize_symbol(symbol)
        except ValueError as exc:
            return _safe_error_result(str(symbol), None, self.provider,
                _request_id(), str(exc), "INVALID")
        if exchange not in ("SH", "SZ"):
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "EXCHANGE_UNSUPPORTED", "INVALID")
        local_id = "tencent-client-" + uuid.uuid4().hex
        tencent_symbol = self._tencent_code(canonical)
        url = TENCENT_QUOTE_URL + tencent_symbol
        params = {"_": str(int(self._clock() * 1000))}
        try:
            response = self._transport(url, params=params,
                headers={"User-Agent": "Mozilla/5.0"}, timeout=self._timeout)
            status = getattr(response, "status_code", None)
            if status != 200:
                return _safe_error_result(canonical, exchange, self.provider,
                    local_id, f"HTTP_{status}", "HTTP_ERROR")
            raw = getattr(response, "content", None)
            if raw is None:
                raw = getattr(response, "text", "").encode("gb18030", errors="replace")
            text = raw.decode("gb18030", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception as exc:
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, type(exc).__name__, "TRANSPORT_ERROR")
        target_variable = "v_" + tencent_symbol
        found = []
        for statement in text.strip().split(";"):
            if "=" not in statement:
                continue
            name, raw_value = statement.split("=", 1)
            if name.strip() != target_variable:
                continue
            quoted = re.search(r'"(.*)"', raw_value)
            if quoted:
                found.append(quoted.group(1).split("~"))
        if len(found) != 1:
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, "QUOTE_MISSING_OR_DUPLICATE", "MISSING")
        fields = found[0]
        if len(fields) <= 37 or fields[2] != canonical[:6]:
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, "QUOTE_SHAPE_OR_SYMBOL_INVALID", "INVALID")
        try:
            observed = self._quote_observed_at(fields[30])
            if fields[30] and observed is None:
                return _safe_error_result(canonical, exchange, self.provider,
                    local_id, "QUOTE_TIMESTAMP_INVALID", "INVALID")
            mapped = {"last_price": fields[3], "prev_price": fields[4],
                "open_price": fields[5], "price_change": fields[31],
                "price_change_ratio_pct": fields[32], "volume": fields[36],
                "turnover": fields[37]}
            values, unavailable = ({"name": fields[1]} if fields[1] else {}), []
            if observed is None:
                unavailable.append("observed_at")
            if not fields[1]:
                unavailable.append("name")
            for key, raw_value in mapped.items():
                parsed = _number(float(raw_value)) if raw_value else None
                if parsed is None:
                    unavailable.append(key)
                else:
                    if key == "volume":
                        # The live-checked Tencent field is in lots; V7's
                        # provider-neutral contract uses shares.
                        values[key] = parsed * 100.0
                    elif key == "turnover":
                        # The live-checked field is in CNY 10,000s.
                        values[key] = parsed * 10000.0
                    else:
                        values[key] = parsed
            if values.get("last_price", 0) <= 0:
                raise ValueError("LAST_PRICE_INVALID")
            if any(values.get(key, 0) < 0 for key in ("volume", "turnover")):
                raise ValueError("NEGATIVE_MARKET_FIELD")
        except (ValueError, TypeError, OverflowError):
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, "QUOTE_FIELDS_INVALID", "INVALID")
        units = {"name": "text", "last_price": "CNY/share", "prev_price": "CNY/share",
            "open_price": "CNY/share", "price_change": "CNY/share",
            "price_change_ratio_pct": "percent", "volume": "share", "turnover": "CNY"}
        status = "UNKNOWN" if unavailable else "VALID"
        return ProviderResult(canonical, exchange, self.provider, _iso(observed),
            observed.date().isoformat() if observed else None, local_id, status, values, units,
            unavailable_fields=tuple(unavailable))

    def fetch_daily_history(self, symbol: str, start: str, end: str) -> ProviderResult:
        try:
            canonical, exchange = canonicalize_symbol(symbol)
        except ValueError as exc:
            return _safe_error_result(str(symbol), None, self.provider,
                _request_id(), str(exc), "INVALID", "forward")
        if exchange not in ("SH", "SZ"):
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "EXCHANGE_UNSUPPORTED", "INVALID", "forward")
        try:
            start_date = _datetime.date.fromisoformat(start)
            end_date = _datetime.date.fromisoformat(end)
            if end_date < start_date:
                raise ValueError
        except (TypeError, ValueError):
            return _safe_error_result(canonical, exchange, self.provider,
                _request_id(), "DATE_RANGE_INVALID", "INVALID", "forward")
        local_id = "tencent-client-" + uuid.uuid4().hex
        tencent_symbol = self._tencent_code(canonical)
        params = {"param": f"{tencent_symbol},day,{start_date.isoformat()},{end_date.isoformat()},640,qfq"}
        try:
            response = self._transport(TENCENT_DAILY_URL, params=params,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=self._timeout)
            status = getattr(response, "status_code", None)
            if status != 200:
                return _safe_error_result(canonical, exchange, self.provider,
                    local_id, f"HTTP_{status}", "HTTP_ERROR", "forward")
            payload = response.json()
        except Exception as exc:
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, type(exc).__name__, "TRANSPORT_ERROR", "forward")
        if not isinstance(payload, dict):
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, "JSON_OBJECT_REQUIRED", "INVALID", "forward")
        node = payload.get("data", {}).get(tencent_symbol) if isinstance(payload.get("data"), dict) else None
        if not isinstance(node, dict):
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, "DAILY_NODE_MISSING", "MISSING", "forward")
        key = "qfqday" if "qfqday" in node else "day" if "day" in node else None
        if key is None or not isinstance(node.get(key), list):
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, "DAILY_ROWS_MISSING", "MISSING", "forward")
        adjustment = "forward" if key == "qfqday" else "none"
        bars = []
        try:
            for row in node[key]:
                if not isinstance(row, list) or len(row) < 6:
                    raise ValueError("BAR_SHAPE_INVALID")
                day = _datetime.date.fromisoformat(str(row[0]))
                raw = [_number(float(value)) for value in row[1:5]]
                if any(value is None for value in raw):
                    raise ValueError("BAR_FIELD_INVALID")
                open_, close, high, low = raw
                if min(open_, close, high, low) <= 0 or high < max(open_, close, low) or low > min(open_, close, high):
                    raise ValueError("OHLC_INVALID")
                try:
                    volume_raw = float(row[5]) if row[5] not in (None, "") else None
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("BAR_VOLUME_INVALID") from None
                volume_lots = _number(volume_raw) if volume_raw is not None else None
                fields = {"open_price": open_, "high_price": high,
                    "low_price": low, "close_price": close,
                    "volume": volume_lots * 100.0 if volume_lots is not None else None,
                    "turnover": None}
                if volume_lots is None or volume_lots < 0:
                    raise ValueError("BAR_VOLUME_INVALID")
                bars.append(DailyBar(day.isoformat(), fields,
                    {"open_price": "CNY/share", "high_price": "CNY/share",
                     "low_price": "CNY/share", "close_price": "CNY/share",
                     "volume": "share"}, ("turnover",)))
        except (ValueError, TypeError, OverflowError) as exc:
            # Only stable reason codes are emitted, never raw provider content.
            reason = str(exc) if str(exc).isupper() else "BAR_INVALID"
            return _safe_error_result(canonical, exchange, self.provider,
                local_id, reason, "INVALID", adjustment)
        if not bars:
            return ProviderResult(canonical, exchange, self.provider, None,
                None, local_id, "MISSING", {}, {}, adjustment,
                "NO_BARS", "NO_HISTORICAL_BARS")
        return ProviderResult(canonical, exchange, self.provider, None,
            None, local_id, "UNKNOWN", {}, {}, adjustment,
            error_reason="TURNOVER_NOT_EXPOSED_BY_DAILY_K_RESPONSE",
            bars=tuple(bars), unavailable_fields=("turnover", "observed_at"))
