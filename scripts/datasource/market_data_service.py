# -*- coding: utf-8 -*-
"""Isolated provider arbitration and market-data validation for V7 P1B.

This module is intentionally not wired to production callers and has no
database dependency. Freshness limits are mandatory injected policy; the
included policy model defaults to not-approved, so a successful isolated
quality validation cannot authorize a production decision by itself.
"""

from __future__ import annotations

import datetime as dt
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from scripts.datasource.provider_adapters import (
    DailyBar,
    ProviderResult,
    canonicalize_symbol,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DATA_STATUSES = frozenset({"VALID", "STALE", "MISSING", "INVALID", "UNKNOWN"})
REQUEST_STATUSES = frozenset({
    "SUCCESS", "CREDENTIAL_MISSING", "AUTHENTICATION_FAILED",
    "PERMISSION_DENIED", "RATE_LIMITED", "TRANSPORT_ERROR", "TIMEOUT",
    "HTTP_ERROR", "INVALID_RESPONSE", "PROVIDER_ERROR",
})
SNAPSHOT_PROJECTION_FIELDS = frozenset({
    "name", "last_price", "prev_price", "open_price", "high_price", "low_price",
    "close_price", "price_change", "price_change_ratio_pct", "volume", "turnover",
    "turnover_ratio", "pe", "mktcap_yi",
})


@dataclass(frozen=True)
class FieldRequirement:
    name: str
    fields: frozenset[str]
    kind: str = "snapshot"
    min_bars: int = 0


FIELD_REQUIREMENTS: Mapping[str, FieldRequirement] = {
    "PRICE_ONLY": FieldRequirement("PRICE_ONLY", frozenset({"last_price"})),
    "PRICE_AND_PREVIOUS_CLOSE": FieldRequirement(
        "PRICE_AND_PREVIOUS_CLOSE", frozenset({"last_price", "prev_price"})),
    "PRICE_VOLUME_AMOUNT": FieldRequirement(
        "PRICE_VOLUME_AMOUNT", frozenset({"last_price", "volume", "turnover"})),
    "PRICE_TURNOVER": FieldRequirement(
        "PRICE_TURNOVER", frozenset({"last_price", "turnover"})),
    "DAILY_OHLCV": FieldRequirement(
        "DAILY_OHLCV", frozenset({"open_price", "high_price", "low_price", "close_price", "volume"}), "history"),
    "AIQ_FEATURE_HISTORY": FieldRequirement(
        "AIQ_FEATURE_HISTORY", frozenset({"open_price", "high_price", "low_price", "close_price", "volume", "turnover"}), "history", 25),
}


@dataclass(frozen=True)
class FreshnessPolicy:
    """Session-specific quote limits. Values are proposals unless approved."""

    max_age_seconds_by_phase: Mapping[str, float]
    approved_for_decisions: bool = False
    policy_id: str = "PROPOSED_UNAPPROVED"

    def __post_init__(self):
        if not self.max_age_seconds_by_phase:
            raise ValueError("FRESHNESS_LIMITS_REQUIRED")
        for phase, seconds in self.max_age_seconds_by_phase.items():
            if not phase or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError("FRESHNESS_LIMIT_INVALID")
            if not math.isfinite(float(seconds)) or seconds <= 0:
                raise ValueError("FRESHNESS_LIMIT_INVALID")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_provider: int = 2
    backoff_seconds: float = 0.25

    def __post_init__(self):
        if self.max_attempts_per_provider not in (1, 2):
            raise ValueError("RETRY_LIMIT_MUST_BE_BOUNDED_TO_ONE_OR_TWO")
        if (isinstance(self.backoff_seconds, bool) or
                not isinstance(self.backoff_seconds, (int, float)) or
                not math.isfinite(float(self.backoff_seconds)) or
                self.backoff_seconds < 0):
            raise ValueError("RETRY_BACKOFF_INVALID")


@dataclass(frozen=True)
class SessionPoint:
    phase: str
    is_trading_day: bool
    trade_date: Optional[str]
    calendar_status: str = "VALID"


class Provider(Protocol):
    provider: str

    def fetch_snapshot(self, symbol: str) -> ProviderResult: ...

    def fetch_daily_history(self, symbol: str, *args, **kwargs) -> ProviderResult: ...


class SessionCalendar(Protocol):
    def session_at(self, timestamp: dt.datetime) -> SessionPoint: ...

    def trading_dates(self, start: dt.date, end: dt.date) -> Sequence[str]: ...


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    request_type: str
    attempt_number: int
    trace_id: str
    request_status: str
    data_status: str
    reason: Optional[str]
    requested_at: str
    received_at: str


@dataclass(frozen=True)
class MarketDataResult:
    schema_version: str
    correlation_id: str
    consumer: str
    request_type: str
    consumer_requirement: str
    required_fields: tuple[str, ...]
    canonical_symbol: str
    exchange: str
    request_status: str
    data_status: str
    provider: Optional[str]
    source_request_id: Optional[str]
    requested_at: str
    received_at: str
    observed_at: Optional[str]
    trade_date: Optional[str]
    session_phase: Optional[str]
    age_seconds: Optional[float]
    value_fields: Mapping[str, Any] = field(default_factory=dict)
    field_units: Mapping[str, str] = field(default_factory=dict)
    unavailable_fields: tuple[str, ...] = ()
    adjustment_mode: str = "not_applicable"
    bars: tuple[DailyBar, ...] = ()
    attempts: tuple[ProviderAttempt, ...] = ()
    fallback_reason: Optional[str] = None
    error_reason: Optional[str] = None
    policy_id: str = "PROPOSED_UNAPPROVED"
    production_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bars"] = [asdict(bar) for bar in self.bars]
        return value


class V7RuntimeSessionCalendar:
    """Adapter over the existing strict runtime calendar; does not own a calendar."""

    def __init__(self, calendar_status_fn: Callable[[dt.date], Mapping[str, Any]],
                 market_phase_fn: Callable[[dt.datetime], str],
                 trading_dates_fn: Callable[[dt.date, dt.date], Sequence[str]]):
        self._calendar_status_fn = calendar_status_fn
        self._market_phase_fn = market_phase_fn
        self._trading_dates_fn = trading_dates_fn

    def session_at(self, timestamp: dt.datetime) -> SessionPoint:
        local = _aware_shanghai(timestamp)
        if local is None:
            return SessionPoint("UNKNOWN", False, None, "INVALID_TIMESTAMP")
        status = self._calendar_status_fn(local.date())
        status_name = str(status.get("status", "UNKNOWN")).upper()
        if status_name != "VALID":
            return SessionPoint("UNKNOWN", False, local.date().isoformat(), status_name)
        is_trading_day = bool(status.get("trading_day"))
        if not is_trading_day:
            return SessionPoint("NON_TRADING", False, local.date().isoformat())
        phase = str(self._market_phase_fn(local)).lower()
        phase_map = {"pre": "PRE_OPEN", "open": "CONTINUOUS", "break": "LUNCH_BREAK"}
        normalized = phase_map.get(phase, "AFTER_HOURS" if phase == "closed" else "UNKNOWN")
        return SessionPoint(normalized, True, local.date().isoformat(), status_name)

    def trading_dates(self, start: dt.date, end: dt.date) -> Sequence[str]:
        values = self._trading_dates_fn(start, end)
        return tuple(_date_text(value) for value in values)


def _aware_shanghai(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(SHANGHAI)


def _date_text(value: Any) -> str:
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return value.isoformat()
    return dt.date.fromisoformat(str(value)).isoformat()


def _safe_trace(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    # Provider-controlled IDs are data, not trusted log messages.
    import re
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return None


def _requirement(value: str | FieldRequirement, kind: str) -> FieldRequirement:
    if isinstance(value, FieldRequirement):
        result = value
    else:
        try:
            result = FIELD_REQUIREMENTS[value]
        except KeyError as exc:
            raise ValueError("UNKNOWN_FIELD_REQUIREMENT") from exc
    if result.kind != kind:
        raise ValueError("FIELD_REQUIREMENT_KIND_MISMATCH")
    return result


def _unit_for(field_name: str) -> Optional[str]:
    return {
        "name": "text",
        "last_price": "CNY/share", "prev_price": "CNY/share",
        "open_price": "CNY/share", "high_price": "CNY/share",
        "low_price": "CNY/share", "close_price": "CNY/share",
        "price_change": "CNY/share", "price_change_ratio_pct": "percent",
        "volume": "share", "turnover": "CNY",
        "turnover_ratio": "percent", "pe": "ratio",
        "mktcap_yi": "CNY_100M",
    }.get(field_name)


def _valid_field(field_name: str, value: Any) -> bool:
    if field_name == "name":
        return isinstance(value, str) and bool(value.strip())
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    numeric = float(value)
    if not math.isfinite(numeric):
        return False
    if field_name in {"last_price", "prev_price", "open_price", "high_price", "low_price", "close_price"}:
        return numeric > 0
    if field_name in {"volume", "turnover"}:
        return numeric >= 0
    return True


class MarketDataService:
    """Bounded HiThink-primary/Tencent-secondary failover, validation only."""

    _RETRYABLE_REQUEST_STATUS = frozenset({"TRANSPORT_ERROR", "TIMEOUT"})

    def __init__(self, providers: Sequence[Provider], *, clock: Callable[[], dt.datetime],
                 calendar: SessionCalendar, freshness_policy: FreshnessPolicy,
                 retry_policy: RetryPolicy = RetryPolicy(), sleeper: Callable[[float], None] = time.sleep):
        if len(providers) != 2 or [p.provider for p in providers] != ["hithink", "tencent"]:
            raise ValueError("PROVIDER_ORDER_MUST_BE_HITHINK_THEN_TENCENT")
        if not callable(clock) or not callable(sleeper):
            raise ValueError("CLOCK_AND_SLEEPER_REQUIRED")
        self._providers = tuple(providers)
        self._clock = clock
        self._calendar = calendar
        self._freshness = freshness_policy
        self._retry = retry_policy
        self._sleeper = sleeper

    def _now(self) -> dt.datetime:
        result = self._clock()
        normalized = _aware_shanghai(result)
        if normalized is None:
            raise ValueError("CLOCK_MUST_RETURN_AWARE_DATETIME")
        return normalized

    def _call(self, provider: Provider, call: Callable[[], ProviderResult],
              correlation_id: str, requirement_name: str,
              validator: Callable[[ProviderResult, dt.datetime], tuple[str, Optional[str], dict[str, Any]]],
              attempts: list[ProviderAttempt], *, request_type: str) -> tuple[Optional[ProviderResult], str, Optional[str], dict[str, Any]]:
        final_result = None
        final_quality, final_reason, final_meta = "UNKNOWN", "PROVIDER_RESULT_UNAVAILABLE", {}
        for number in range(1, self._retry.max_attempts_per_provider + 1):
            requested = self._now()
            try:
                result = call()
                if not isinstance(result, ProviderResult):
                    raise TypeError("PROVIDER_RESULT_TYPE_INVALID")
            except Exception as exc:
                safe_name = type(exc).__name__ if type(exc).__name__.isalnum() else "PROVIDER_EXCEPTION"
                result = ProviderResult(
                    canonical_symbol="", exchange=None, provider=provider.provider,
                    observed_at=None, trade_date=None,
                    source_request_id=f"local-{uuid.uuid4().hex}",
                    data_status="UNKNOWN", request_status="TIMEOUT" if "timeout" in safe_name.lower() else "TRANSPORT_ERROR",
                    error_code=safe_name, error_reason="PROVIDER_CALL_FAILED")
            received = self._now()
            quality, reason, meta = validator(result, received)
            request_status = result.request_status if result.request_status in REQUEST_STATUSES else "INVALID_RESPONSE"
            # Use a local trace here; upstream-controlled request-id text is
            # not trusted for log/evidence serialization.
            trace_id = f"local-{provider.provider}-{correlation_id[:12]}-{number}"
            attempts.append(ProviderAttempt(
                provider=provider.provider, request_type=request_type,
                attempt_number=number, trace_id=trace_id,
                request_status=request_status, data_status=quality, reason=reason or result.error_reason,
                requested_at=requested.isoformat(timespec="milliseconds"),
                received_at=received.isoformat(timespec="milliseconds")))
            final_result, final_quality, final_reason, final_meta = result, quality, reason, meta
            if request_status == "SUCCESS" and quality == "VALID":
                return result, quality, reason, {**meta, "requested_at": requested, "received_at": received}
            retryable_http_5xx = (
                request_status == "HTTP_ERROR" and isinstance(result.error_code, str)
                and result.error_code.startswith("HTTP_5")
            )
            retryable_transport = request_status in self._RETRYABLE_REQUEST_STATUS
            if number < self._retry.max_attempts_per_provider and (retryable_transport or retryable_http_5xx):
                self._sleeper(float(self._retry.backoff_seconds))
                continue
            break
        if final_result is None:
            return None, "UNKNOWN", "PROVIDER_RESULT_UNAVAILABLE", {}
        return final_result, final_quality, final_reason, {**final_meta, "requested_at": requested, "received_at": received}

    def get_snapshot(self, symbol: str, requirement: str | FieldRequirement,
                     *, consumer: str,
                     include_fields: Sequence[str] = ()) -> MarketDataResult:
        req = _requirement(requirement, "snapshot")
        include = tuple(dict.fromkeys(str(name) for name in include_fields))
        if any(not name or name in req.fields or name not in SNAPSHOT_PROJECTION_FIELDS
               for name in include):
            raise ValueError("OPTIONAL_FIELD_PROJECTION_INVALID")
        correlation = uuid.uuid4().hex
        now = self._now()
        try:
            canonical, exchange = canonicalize_symbol(symbol)
        except ValueError as exc:
            return self._empty_result(correlation, req.name, str(symbol), None,
                                      "SUCCESS", "INVALID", "INVALID_SYMBOL", now, now,
                                      "not_applicable", consumer, "snapshot", tuple(sorted(req.fields)))
        attempts: list[ProviderAttempt] = []
        fallback_reason = None
        last: Optional[ProviderResult] = None
        final_quality, final_reason, final_meta = "UNKNOWN", "NO_PROVIDER_RESULT", {}
        for index, provider in enumerate(self._providers):
            result, quality, reason, meta = self._call(
                provider, lambda p=provider: p.fetch_snapshot(canonical), correlation,
                req.name, lambda r, received: self._validate_snapshot(r, canonical, req, received),
                attempts, request_type="snapshot")
            if result is not None:
                last, final_quality, final_reason, final_meta = result, quality, reason, meta
            if result is not None and result.request_status == "SUCCESS" and quality == "VALID":
                return self._from_provider(correlation, req.name, result, quality, fallback_reason,
                                           final_meta, tuple(attempts), consumer,
                                           request_type="snapshot", required_fields=tuple(sorted(req.fields)),
                                           include_fields=include)
            if index == 0:
                fallback_reason = (reason or (result.error_reason if result else None)
                                   or (result.error_code if result else None)
                                   or (result.data_status if result else None)
                                   or "PRIMARY_NO_RESULT")
        request_status = last.request_status if last and last.request_status in REQUEST_STATUSES else "UNKNOWN"
        requested = final_meta.get("requested_at", now)
        received = final_meta.get("received_at", now)
        return self._from_provider(correlation, req.name, last, final_quality,
            fallback_reason, final_meta, tuple(attempts), consumer,
            request_type="snapshot", required_fields=tuple(sorted(req.fields)),
            request_status=request_status, error_reason=final_reason,
            requested_fallback=requested, received_fallback=received,
            include_fields=include)

    def _validate_snapshot(self, result: ProviderResult, symbol: str,
                           requirement: FieldRequirement, received: dt.datetime):
        if result.request_status != "SUCCESS":
            return "UNKNOWN", result.error_reason or result.request_status, {}
        if result.data_status not in DATA_STATUSES:
            return "INVALID", "DATA_STATUS_UNSUPPORTED", {}
        if result.data_status in ("INVALID", "MISSING"):
            return result.data_status, result.error_reason or result.error_code, {}
        if result.data_status == "STALE":
            return "STALE", result.error_reason or "PROVIDER_MARKED_STALE", {}
        if result.canonical_symbol != symbol:
            return "INVALID", "SYMBOL_MISMATCH", {}
        expected_exchange = symbol.rsplit(".", 1)[-1]
        if result.exchange != expected_exchange:
            return "INVALID", "EXCHANGE_MISMATCH", {}
        observed = _aware_shanghai(result.observed_at)
        if observed is None:
            return "UNKNOWN", "OBSERVED_AT_MISSING_OR_INVALID", {}
        if observed > received:
            return "INVALID", "OBSERVED_AT_IN_FUTURE", {"observed": observed}
        if result.trade_date != observed.date().isoformat():
            return "INVALID", "TRADE_DATE_OBSERVED_AT_MISMATCH", {"observed": observed}
        try:
            session = self._calendar.session_at(received)
        except Exception:
            return "UNKNOWN", "SESSION_CALENDAR_UNAVAILABLE", {"observed": observed}
        if not isinstance(session, SessionPoint) or session.calendar_status != "VALID":
            return "UNKNOWN", "SESSION_CALENDAR_INVALID", {"observed": observed}
        if not session.is_trading_day or session.phase == "NON_TRADING":
            return "STALE", "NON_TRADING_SESSION", {"observed": observed, "session": session}
        if session.phase == "UNKNOWN":
            return "UNKNOWN", "SESSION_UNKNOWN", {"observed": observed, "session": session}
        if session.trade_date and result.trade_date != session.trade_date:
            return "STALE", "QUOTE_TRADE_DATE_MISMATCH", {"observed": observed, "session": session}
        limit = self._freshness.max_age_seconds_by_phase.get(session.phase)
        if limit is None:
            return "UNKNOWN", "FRESHNESS_LIMIT_NOT_CONFIGURED_FOR_SESSION", {"observed": observed, "session": session}
        age = (received - observed).total_seconds()
        if age > float(limit):
            return "STALE", "QUOTE_EXPIRED", {"observed": observed, "session": session, "age": age}
        if not isinstance(result.value_fields, Mapping) or not isinstance(result.field_units, Mapping):
            return "INVALID", "VALUE_FIELDS_INVALID", {"observed": observed, "session": session, "age": age}
        for field_name in requirement.fields:
            value = result.value_fields.get(field_name)
            if not _valid_field(field_name, value):
                status = "MISSING" if value is None else "INVALID"
                return status, f"REQUIRED_FIELD_{field_name.upper()}_UNAVAILABLE", {"observed": observed, "session": session, "age": age}
            expected_unit = _unit_for(field_name)
            if expected_unit and result.field_units.get(field_name) != expected_unit:
                return "INVALID", f"FIELD_UNIT_{field_name.upper()}_MISMATCH", {"observed": observed, "session": session, "age": age}
        if "last_price" in requirement.fields and not _valid_field("last_price", result.value_fields.get("last_price")):
            return "INVALID", "LAST_PRICE_INVALID", {"observed": observed, "session": session, "age": age}
        return "VALID", None, {"observed": observed, "session": session, "age": age}

    def get_daily_history(self, symbol: str, start: str, end: str, *, adjust: str,
                          requirement: str | FieldRequirement = "DAILY_OHLCV",
                          expected_trade_dates: Optional[Sequence[str]] = None,
                          min_bars: Optional[int] = None,
                          consumer: str) -> MarketDataResult:
        req = _requirement(requirement, "history")
        correlation = uuid.uuid4().hex
        now = self._now()
        try:
            canonical, exchange = canonicalize_symbol(symbol)
            start_date, end_date = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
            if end_date < start_date or adjust not in {"none", "forward", "backward"}:
                raise ValueError
        except (ValueError, TypeError):
            return self._empty_result(correlation, req.name, str(symbol), None,
                                      "SUCCESS", "INVALID", "HISTORY_REQUEST_INVALID", now, now,
                                      adjust, consumer, "history", tuple(sorted(req.fields)))
        try:
            expected = tuple(_date_text(x) for x in expected_trade_dates) if expected_trade_dates is not None else tuple(
                _date_text(x) for x in self._calendar.trading_dates(start_date, end_date))
        except Exception:
            expected = ()
        if not expected:
            return self._empty_result(correlation, req.name, canonical, exchange,
                "SUCCESS", "UNKNOWN", "EXPECTED_TRADING_DATES_UNAVAILABLE", now, now,
                adjust, consumer, "history", tuple(sorted(req.fields)))
        needed = max(req.min_bars, min_bars or 0)
        attempts: list[ProviderAttempt] = []
        fallback_reason = None
        last = None
        final_quality, final_reason, final_meta = "UNKNOWN", "NO_PROVIDER_RESULT", {}
        for index, provider in enumerate(self._providers):
            call = lambda p=provider: self._fetch_history(p, canonical, start_date, end_date, adjust)
            result, quality, reason, meta = self._call(
                provider, call, correlation, req.name,
                lambda r, received: self._validate_history(r, canonical, start_date, end_date,
                    adjust, req, expected, needed, received), attempts,
                request_type="history")
            if result is not None:
                last, final_quality, final_reason, final_meta = result, quality, reason, meta
            if result is not None and result.request_status == "SUCCESS" and quality == "VALID":
                return self._from_provider(correlation, req.name, result, quality, fallback_reason,
                    final_meta, tuple(attempts), consumer,
                    request_type="history", required_fields=tuple(sorted(req.fields)))
            if index == 0:
                fallback_reason = (reason or (result.error_reason if result else None)
                                   or (result.error_code if result else None)
                                   or (result.data_status if result else None)
                                   or "PRIMARY_NO_RESULT")
        return self._from_provider(correlation, req.name, last, final_quality,
            fallback_reason, final_meta, tuple(attempts), consumer,
            request_type="history", required_fields=tuple(sorted(req.fields)),
            request_status=last.request_status if last and last.request_status in REQUEST_STATUSES else "UNKNOWN",
            error_reason=final_reason,
            requested_fallback=final_meta.get("requested_at", now),
            received_fallback=final_meta.get("received_at", now))

    @staticmethod
    def _fetch_history(provider: Provider, symbol: str, start: dt.date,
                       end: dt.date, adjust: str) -> ProviderResult:
        if provider.provider == "hithink":
            start_ms = int(dt.datetime.combine(start, dt.time(), SHANGHAI).timestamp() * 1000)
            end_ms = int(dt.datetime.combine(end, dt.time(23, 59, 59), SHANGHAI).timestamp() * 1000)
            return provider.fetch_daily_history(symbol, start_ms, end_ms, adjust=adjust)
        return provider.fetch_daily_history(symbol, start.isoformat(), end.isoformat())

    def _validate_history(self, result: ProviderResult, symbol: str,
                          start: dt.date, end: dt.date, adjust: str,
                          requirement: FieldRequirement, expected: Sequence[str],
                          min_bars: int, received: dt.datetime):
        if result.request_status != "SUCCESS":
            return "UNKNOWN", result.error_reason or result.request_status, {}
        if result.data_status not in DATA_STATUSES:
            return "INVALID", "DATA_STATUS_UNSUPPORTED", {}
        if result.data_status in ("INVALID", "MISSING"):
            return result.data_status, result.error_reason or result.error_code, {}
        if result.data_status == "STALE":
            return "STALE", result.error_reason or "PROVIDER_MARKED_STALE", {}
        if result.canonical_symbol != symbol:
            return "INVALID", "SYMBOL_MISMATCH", {}
        expected_exchange = symbol.rsplit(".", 1)[-1]
        if result.exchange != expected_exchange:
            return "INVALID", "EXCHANGE_MISMATCH", {}
        if result.adjustment_mode != adjust:
            return "INVALID", "ADJUSTMENT_MODE_MISMATCH", {}
        observed = _aware_shanghai(result.observed_at)
        # Historical providers may expose per-bar trade dates but no response
        # observation timestamp. Never fabricate one from request time; the
        # validated dated bars plus local received_at are the available proof.
        if observed is not None and observed > received:
            return "INVALID", "OBSERVED_AT_IN_FUTURE", {"observed": observed}
        if observed is not None and (observed.date() < start or observed.date() > received.date()):
            return "INVALID", "HISTORY_RESPONSE_TIMESTAMP_INVALID", {"observed": observed}
        if not result.bars:
            return "MISSING", "HISTORY_BARS_MISSING", {}
        dates = []
        for bar in result.bars:
            try:
                day = dt.date.fromisoformat(bar.trade_date)
            except (TypeError, ValueError):
                return "INVALID", "BAR_DATE_INVALID", {}
            if day < start or day > end:
                return "INVALID", "BAR_OUTSIDE_REQUEST", {}
            dates.append(day.isoformat())
            for field_name in requirement.fields:
                value = bar.value_fields.get(field_name)
                if not _valid_field(field_name, value):
                    status = "MISSING" if value is None else "INVALID"
                    return status, f"BAR_FIELD_{field_name.upper()}_UNAVAILABLE", {}
                unit = _unit_for(field_name)
                if unit and bar.field_units.get(field_name) != unit:
                    return "INVALID", f"BAR_UNIT_{field_name.upper()}_MISMATCH", {}
            o, h, low, close = (bar.value_fields.get(x) for x in
                ("open_price", "high_price", "low_price", "close_price"))
            if (not all(_valid_field(x, y) for x, y in zip(
                    ("open_price", "high_price", "low_price", "close_price"), (o, h, low, close))) or
                    h < max(o, low, close) or low > min(o, h, close)):
                return "INVALID", "BAR_OHLC_INVALID", {}
        if len(set(dates)) != len(dates):
            return "INVALID", "DUPLICATE_BAR_DATE", {}
        if dates != sorted(dates):
            return "INVALID", "BAR_ORDER_INVALID", {}
        if len(dates) < min_bars:
            return "MISSING", "HISTORY_WINDOW_TOO_SHORT", {}
        if not set(expected).issubset(set(dates)):
            return "MISSING", "HISTORY_TRADING_DATE_GAP", {}
        return "VALID", None, {"observed": observed} if observed else {}

    def _from_provider(self, correlation: str, requirement_name: str,
                       result: Optional[ProviderResult], data_status: str,
                       fallback_reason: Optional[str], meta: Mapping[str, Any],
                       attempts: tuple[ProviderAttempt, ...], consumer: str,
                       *, request_type: str, required_fields: tuple[str, ...],
                       request_status: Optional[str] = None,
                       error_reason: Optional[str] = None,
                       requested_fallback: Optional[dt.datetime] = None,
                       received_fallback: Optional[dt.datetime] = None,
                       include_fields: Sequence[str] = ()) -> MarketDataResult:
        result = result or ProviderResult("", None, "unknown", None, None, None,
            "UNKNOWN", request_status="INVALID_RESPONSE")
        observed = _aware_shanghai(result.observed_at)
        requested = meta.get("requested_at") or requested_fallback or self._now()
        received = meta.get("received_at") or received_fallback or self._now()
        age = meta.get("age")
        request_status = request_status or result.request_status
        eligible = bool(data_status == "VALID" and self._freshness.approved_for_decisions)
        required = frozenset(required_fields)
        projected_unavailable: tuple[str, ...] = ()
        if data_status == "VALID":
            projected = required.union(include_fields)
            accepted_values = {}
            accepted_units = {}
            missing_optional = []
            for key in projected:
                value = result.value_fields.get(key)
                unit = result.field_units.get(key)
                expected_unit = _unit_for(key)
                if not _valid_field(key, value) or (expected_unit and unit != expected_unit):
                    if key in include_fields:
                        missing_optional.append(key)
                    continue
                accepted_values[key] = value
                if unit is not None:
                    accepted_units[key] = unit
            projected_unavailable = tuple(sorted(set(missing_optional)))
            accepted_bars = tuple(DailyBar(
                bar.trade_date,
                {key: value for key, value in bar.value_fields.items() if key in required},
                {key: value for key, value in bar.field_units.items() if key in required},
                tuple(key for key in bar.unavailable_fields if key in required),
            ) for bar in result.bars)
        else:
            accepted_values, accepted_units, accepted_bars = {}, {}, ()
        return MarketDataResult(
            schema_version="market-evidence-v1.1",
            correlation_id=correlation, consumer=consumer, request_type=request_type,
            consumer_requirement=requirement_name, required_fields=required_fields,
            canonical_symbol=result.canonical_symbol, exchange=result.exchange or "UNKNOWN",
            request_status=request_status if request_status in REQUEST_STATUSES else "INVALID_RESPONSE",
            data_status=data_status if data_status in DATA_STATUSES else "UNKNOWN",
            provider=result.provider,
            source_request_id=(f"local-{result.provider}-{correlation[:12]}" if result.provider else None),
            requested_at=requested.isoformat(timespec="milliseconds"),
            received_at=received.isoformat(timespec="milliseconds"),
            observed_at=observed.isoformat(timespec="milliseconds") if observed else None,
            trade_date=result.trade_date, session_phase=getattr(meta.get("session"), "phase", None),
            age_seconds=age,
            value_fields=accepted_values,
            field_units=accepted_units,
            unavailable_fields=projected_unavailable,
            adjustment_mode=result.adjustment_mode,
            bars=accepted_bars,
            attempts=attempts, fallback_reason=fallback_reason,
            error_reason=error_reason or (None if data_status == "VALID" else result.error_reason),
            policy_id=self._freshness.policy_id, production_eligible=eligible)

    def _empty_result(self, correlation, requirement, symbol, exchange,
                      request_status, data_status, reason, requested, received,
                      adjustment, consumer, request_type, required_fields):
        return MarketDataResult(
            schema_version="market-evidence-v1.1",
            correlation_id=correlation, consumer=consumer, request_type=request_type,
            consumer_requirement=requirement, required_fields=required_fields,
            canonical_symbol=symbol, exchange=exchange or "UNKNOWN",
            request_status=request_status, data_status=data_status, provider=None,
            source_request_id=None, requested_at=requested.isoformat(),
            received_at=received.isoformat(), observed_at=None, trade_date=None,
            session_phase=None, age_seconds=None, adjustment_mode=adjustment,
            error_reason=reason, policy_id=self._freshness.policy_id,
            production_eligible=False)
