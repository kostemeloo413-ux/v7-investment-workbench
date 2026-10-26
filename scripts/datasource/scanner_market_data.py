# -*- coding: utf-8 -*-
"""Bounded, non-persistent provider-neutral scanner evidence preview.

This bridge is deliberately not imported by the legacy production scanner or
blogger pipeline. A caller must explicitly create a preview with a small
symbol set and an injected MarketDataService. It collects one shared evidence
record per symbol; it does not score, select, authorize, or execute trades.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from scripts.datasource.market_data_service import (
    FieldRequirement,
    MarketDataResult,
    MarketDataService,
    SessionCalendar,
    SessionPoint,
)
from scripts.datasource.provider_adapters import canonicalize_symbol


SHANGHAI = ZoneInfo("Asia/Shanghai")
SCANNER_BASE_REQUIREMENT = FieldRequirement(
    name="SCANNER_SHARED_QUOTE",
    fields=frozenset({"name", "last_price", "prev_price", "price_change_ratio_pct"}),
)
SCANNER_OPTIONAL_FIELDS = (
    "price_change", "volume", "turnover", "turnover_ratio", "pe", "mktcap_yi",
)
MAX_PREVIEW_SYMBOLS = 20
MAX_PROVIDER_ATTEMPTS_PER_SYMBOL = 4  # 2 bounded attempts × 2 configured providers.

PARTICIPANT_REQUIRED_FACTS: Mapping[str, tuple[str, ...]] = {
    # These are evidence-presence checks, not copied or expanded strategy rules.
    # The structural/context fields intentionally remain unavailable in a quote-only preview.
    "P-RULE-QUANT": ("price", "name", "mktcap_yi", "pe", "change_pct",
                     "turnover_ratio", "amount_wan"),
    "P-HANSHU": ("price", "name", "mktcap_yi", "pe", "change_pct",
                 "turnover_ratio", "amount_wan", "ma5", "ma20",
                 "daily_support_evidence"),
    "P-YINWENTAO": ("price", "name", "change_pct", "amount_wan",
                    "turnover_ratio", "market_breadth", "rotation_context"),
    "P-LIAO-XIAOAI": ("price", "name", "mktcap_yi", "pe", "change_pct",
                      "turnover_ratio", "fundamental_evidence_if_claimed"),
    "P-COOL": ("price", "change_pct", "turnover_ratio", "amount_wan",
               "ma20", "daily_structure", "intraday_structure"),
    "P-RISK-SENTINEL": ("change_pct", "turnover_ratio", "market_risk_evidence",
                        "alert_reason_evidence"),
}


@dataclass(frozen=True)
class ScanBudget:
    max_symbols: int = MAX_PREVIEW_SYMBOLS
    max_provider_attempts: int = MAX_PREVIEW_SYMBOLS * MAX_PROVIDER_ATTEMPTS_PER_SYMBOL

    def __post_init__(self) -> None:
        if isinstance(self.max_symbols, bool) or not 1 <= self.max_symbols <= MAX_PREVIEW_SYMBOLS:
            raise ValueError("PREVIEW_SYMBOL_BUDGET_OUT_OF_RANGE")
        if (isinstance(self.max_provider_attempts, bool)
                or self.max_provider_attempts < self.max_symbols
                or self.max_provider_attempts > self.max_symbols * MAX_PROVIDER_ATTEMPTS_PER_SYMBOL):
            raise ValueError("PREVIEW_PROVIDER_BUDGET_OUT_OF_RANGE")


@dataclass(frozen=True)
class ScannerQuoteEvidence:
    evidence_id: str
    scan_id: str
    canonical_symbol: str
    exchange: str
    provider_reported_symbol: str
    provider: str | None
    source_request_id: str | None
    observed_at: str | None
    trade_date: str | None
    requested_at: str
    received_at: str
    data_status: str
    decision_at: str
    decision_status: str
    decision_reason: str | None
    fields: Mapping[str, Any]
    field_units: Mapping[str, str]
    unavailable_fields: tuple[str, ...]
    provider_attempts: tuple[Mapping[str, Any], ...]
    participant_missing_facts: Mapping[str, tuple[str, ...]]
    decision_authorized: bool = False

    def revalidate_for_decision(self, decision_at: dt.datetime,
                                calendar: SessionCalendar) -> "ScannerQuoteEvidence":
        """Re-evaluate the saved observation at the consumer's actual use time."""
        status, reason = _decision_quality_values(
            self.data_status, self.observed_at, self.trade_date, decision_at, calendar)
        return replace(self,
                       decision_at=_aware(decision_at).isoformat(timespec="milliseconds"),
                       decision_status=status,
                       decision_reason=reason,
                       decision_authorized=False)

    def consumer_view(self, participant_id: str, *,
                      decision_at: dt.datetime | None = None,
                      calendar: SessionCalendar | None = None) -> Mapping[str, Any]:
        """Expose separate quote, evidence, rule-evaluation, and execution layers."""
        if participant_id not in PARTICIPANT_REQUIRED_FACTS:
            raise ValueError("UNKNOWN_SCANNER_PARTICIPANT")
        if decision_at is not None and calendar is None:
            raise ValueError("DECISION_CALENDAR_REQUIRED")
        use_status = self.decision_status
        use_reason = self.decision_reason
        if decision_at is not None and calendar is not None:
            validated = self.revalidate_for_decision(decision_at, calendar)
            use_status, use_reason = validated.decision_status, validated.decision_reason
        missing = self.participant_missing_facts[participant_id]
        sentinel = participant_id == "P-RISK-SENTINEL"
        return {
            "participant_id": participant_id,
            "scan_id": self.scan_id,
            "evidence_id": self.evidence_id,
            "symbol": self.canonical_symbol,
            "provider": self.provider,
            "observed_at": self.observed_at,
            "decision_at": (_aware(decision_at).isoformat(timespec="milliseconds")
                            if decision_at is not None else self.decision_at),
            "QUOTE_VALID": use_status == "VALID" and self.data_status == "VALID",
            # Compatibility for existing P1C1 consumers: data_status reflects
            # validity at this consumer's decision time, while fetched_data_status
            # preserves the provider/service verdict at collection time.
            "data_status": use_status,
            "fetched_data_status": self.data_status,
            "quote_status": use_status,
            "quote_reason": use_reason,
            "REQUIRED_FACTS_COMPLETE": not missing,
            "required_facts_missing": missing,
            "SKILL_RULES_SATISFIED": "NOT_APPLICABLE_NO_TRADE_RULE" if sentinel else "NOT_EVALUATED",
            "EXECUTION_AUTHORIZED": False,
            "fields": dict(self.fields),
            "field_units": dict(self.field_units),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "scan_id": self.scan_id,
            "canonical_symbol": self.canonical_symbol,
            "exchange": self.exchange,
            "provider_reported_symbol": self.provider_reported_symbol,
            "provider": self.provider,
            "source_request_id": self.source_request_id,
            "observed_at": self.observed_at,
            "trade_date": self.trade_date,
            "requested_at": self.requested_at,
            "received_at": self.received_at,
            "data_status": self.data_status,
            "decision_at": self.decision_at,
            "decision_status": self.decision_status,
            "decision_reason": self.decision_reason,
            "fields": dict(self.fields),
            "field_units": dict(self.field_units),
            "unavailable_fields": list(self.unavailable_fields),
            "provider_attempts": [dict(item) for item in self.provider_attempts],
            "participant_missing_facts": {
                key: list(value) for key, value in self.participant_missing_facts.items()
            },
            "decision_authorized": False,
        }


@dataclass(frozen=True)
class ScannerPreview:
    scan_id: str
    scan_requested_at: str
    scan_completed_at: str
    quotes: tuple[ScannerQuoteEvidence, ...]
    total_provider_attempts: int
    production_path_changed: bool = False
    persisted: bool = False

    def evidence_for_participant(self, participant_id: str, *,
                                 decision_at: dt.datetime | None = None,
                                 calendar: SessionCalendar | None = None) -> tuple[Mapping[str, Any], ...]:
        """Return references to the same per-symbol scan facts for each consumer."""
        if participant_id not in PARTICIPANT_REQUIRED_FACTS:
            raise ValueError("UNKNOWN_SCANNER_PARTICIPANT")
        if decision_at is not None and calendar is None:
            raise ValueError("DECISION_CALENDAR_REQUIRED")
        return tuple(quote.consumer_view(participant_id, decision_at=decision_at,
                                         calendar=calendar)
                     for quote in self.quotes)


def _aware(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_ASIA_SHANGHAI_TIME_REQUIRED")
    return value.astimezone(SHANGHAI)


def _evidence_id(scan_id: str, symbol: str, correlation_id: str) -> str:
    raw = f"{scan_id}|{symbol}|{correlation_id}".encode("utf-8")
    return "scan-evidence-" + hashlib.sha256(raw).hexdigest()[:24]


def _scanner_fields(result: MarketDataResult, requested_symbol: str) -> tuple[dict[str, Any], dict[str, str], tuple[str, ...]]:
    source = result.value_fields if result.data_status == "VALID" else {}
    units = result.field_units if result.data_status == "VALID" else {}
    last = source.get("last_price")
    name = source.get("name")
    change_pct = source.get("price_change_ratio_pct")
    turnover = source.get("turnover")
    values = {
        # Existing scanner-compatible names; unavailable facts remain None.
        "code": requested_symbol[:6],
        "name": name,
        "symbol": requested_symbol,
        "full_code": requested_symbol,
        "market": requested_symbol.rsplit(".", 1)[-1].lower(),
        "price": last,
        "prev_price": source.get("prev_price"),
        "change": source.get("price_change"),
        "change_pct": change_pct,
        "volume": source.get("volume"),
        "amount_wan": float(turnover) / 10_000 if isinstance(turnover, (int, float)) else None,
        "amount_yi": float(turnover) / 100_000_000 if isinstance(turnover, (int, float)) else None,
        "turnover_ratio": source.get("turnover_ratio"),
        "pe": source.get("pe"),
        "mktcap_yi": source.get("mktcap_yi"),
    }
    mapped_units = {
        "price": units.get("last_price"),
        "prev_price": units.get("prev_price"),
        "change": units.get("price_change"),
        "change_pct": units.get("price_change_ratio_pct"),
        "volume": units.get("volume"),
        "amount_wan": "CNY_10000" if values["amount_wan"] is not None else None,
        "amount_yi": "CNY_100M" if values["amount_yi"] is not None else None,
        "turnover_ratio": units.get("turnover_ratio"),
        "pe": units.get("pe"),
        "mktcap_yi": units.get("mktcap_yi"),
    }
    unavailable = set(result.unavailable_fields)
    for key, value in values.items():
        if key not in {"code", "symbol", "full_code", "market"} and value is None:
            unavailable.add(key)
    if result.data_status != "VALID":
        unavailable.update(("name", "price", "prev_price", "change", "change_pct",
                            "volume", "amount_wan", "amount_yi", "turnover_ratio", "pe", "mktcap_yi"))
    return values, {key: value for key, value in mapped_units.items() if value is not None}, tuple(sorted(unavailable))


def _decision_quality(result: MarketDataResult, decision_at: dt.datetime,
                      calendar: SessionCalendar) -> tuple[str, str | None]:
    return _decision_quality_values(result.data_status, result.observed_at,
                                    result.trade_date, decision_at, calendar,
                                    result.error_reason)


def _decision_quality_values(data_status: str, observed_at: str | None,
                             trade_date: str | None, decision_at: dt.datetime,
                             calendar: SessionCalendar,
                             error_reason: str | None = None) -> tuple[str, str | None]:
    if data_status != "VALID":
        return data_status, error_reason or "QUOTE_NOT_VALID_AT_FETCH"
    try:
        observed = _aware(dt.datetime.fromisoformat(observed_at or ""))
        decision = _aware(decision_at)
        session = calendar.session_at(decision)
    except (TypeError, ValueError, OverflowError):
        return "UNKNOWN", "DECISION_TIME_OR_OBSERVED_TIME_INVALID"
    if not isinstance(session, SessionPoint) or session.calendar_status != "VALID":
        return "UNKNOWN", "SESSION_CALENDAR_INVALID"
    if not session.is_trading_day or session.phase != "CONTINUOUS":
        return "STALE", "BUY_DECISION_NOT_ALLOWED_OUTSIDE_CONTINUOUS_SESSION"
    if observed > decision:
        return "INVALID", "OBSERVED_AT_AFTER_DECISION_AT"
    if trade_date != observed.date().isoformat() or session.trade_date != trade_date:
        return "STALE", "QUOTE_TRADE_DATE_NOT_CURRENT_DECISION_DATE"
    age = (decision - observed).total_seconds()
    if age < 0:
        return "INVALID", "OBSERVED_AT_AFTER_DECISION_AT"
    if age > 90:
        return "STALE", "QUOTE_EXCEEDS_90_SECOND_DECISION_FRESHNESS"
    return "VALID", None


def build_scanner_preview(
    symbols: Sequence[str],
    service: MarketDataService,
    *,
    clock: Callable[[], dt.datetime],
    calendar: SessionCalendar,
    budget: ScanBudget = ScanBudget(),
) -> ScannerPreview:
    """Fetch a bounded fake/injected preview; never persists or scores trades."""
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
        raise ValueError("SYMBOL_SEQUENCE_REQUIRED")
    if len(symbols) > budget.max_symbols:
        raise ValueError("PREVIEW_SYMBOL_BUDGET_EXCEEDED")
    if len(symbols) * MAX_PROVIDER_ATTEMPTS_PER_SYMBOL > budget.max_provider_attempts:
        raise ValueError("PREVIEW_PROVIDER_CALL_BUDGET_EXCEEDED")
    normalized = [canonicalize_symbol(item)[0] for item in symbols]
    if len(set(normalized)) != len(normalized):
        raise ValueError("DUPLICATE_PREVIEW_SYMBOL")
    scan_id = "scan-" + uuid.uuid4().hex
    requested_at = _aware(clock()).isoformat(timespec="milliseconds")
    quotes = []
    attempt_count = 0
    for symbol in normalized:
        try:
            result = service.get_snapshot(
                symbol, SCANNER_BASE_REQUIREMENT, consumer="six-blogger-scanner-preview",
                include_fields=SCANNER_OPTIONAL_FIELDS)
        except Exception:
            # Never attach provider exception text, which could contain headers.
            raise RuntimeError("SCANNER_PREVIEW_SERVICE_CALL_FAILED") from None
        attempt_count += len(result.attempts)
        if attempt_count > budget.max_provider_attempts:
            raise RuntimeError("SCANNER_PROVIDER_CALL_BUDGET_EXCEEDED")
        decision_at = _aware(clock())
        decision_status, decision_reason = _decision_quality(result, decision_at, calendar)
        fields, units, unavailable = _scanner_fields(result, symbol)
        missing_by_participant = {
            participant: tuple(name for name in required if fields.get(name) is None)
            for participant, required in PARTICIPANT_REQUIRED_FACTS.items()
        }
        quotes.append(ScannerQuoteEvidence(
            evidence_id=_evidence_id(scan_id, symbol, result.correlation_id),
            scan_id=scan_id,
            canonical_symbol=symbol,
            exchange=symbol.rsplit(".", 1)[-1],
            provider_reported_symbol=result.canonical_symbol,
            provider=result.provider,
            source_request_id=result.source_request_id,
            observed_at=result.observed_at,
            trade_date=result.trade_date,
            requested_at=result.requested_at,
            received_at=result.received_at,
            data_status=result.data_status,
            decision_at=decision_at.isoformat(timespec="milliseconds"),
            decision_status=decision_status,
            decision_reason=decision_reason,
            fields=fields,
            field_units=units,
            unavailable_fields=unavailable,
            provider_attempts=tuple({
                "provider": item.provider,
                "attempt_number": item.attempt_number,
                "trace_id": item.trace_id,
                "request_status": item.request_status,
                "data_status": item.data_status,
                "reason": item.reason,
                "requested_at": item.requested_at,
                "received_at": item.received_at,
            } for item in result.attempts),
            participant_missing_facts=missing_by_participant,
        ))
    completed_at = _aware(clock()).isoformat(timespec="milliseconds")
    return ScannerPreview(scan_id, requested_at, completed_at, tuple(quotes), attempt_count)
