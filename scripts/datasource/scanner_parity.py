"""Pure, bounded legacy-versus-provider-neutral quote evidence comparison.

Inputs must be supplied by an isolated caller; this module never fetches,
imports the production scanner, persists data, or scores a strategy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from scripts.datasource.provider_adapters import canonicalize_symbol
from scripts.datasource.scanner_market_data import ScannerPreview, ScannerQuoteEvidence


MAX_PARITY_SYMBOLS = 20
DIFFERENCE_TYPES = frozenset({
    "MATCH", "UNIT_RECONCILED", "LEGACY_UNVERIFIED", "NEW_FIELD_MISSING",
    "LEGACY_FIELD_MISSING", "TIME_BASIS_MISMATCH", "NOT_COMPARABLE",
})

# old output name, old raw Tencent index, new normalized name, new raw Tencent
# index (where known), comparison units, decision impact, note.
FIELD_MAP = (
    ("name", 1, "name", 1, "text", True,
     "GB18030/GBK name decoding must preserve provider text"),
    ("code", 2, "canonical_symbol", 2, "canonical symbol", True,
     "legacy exchange suffix is inferred from a limited code-prefix list"),
    ("price", 3, "price", 3, "CNY/share", True,
     "only compare values with the same symbol and quote observation time"),
    ("prev_price", 4, "prev_price", 4, "CNY/share", False,
     "legacy scanner receives the wire field but drops it from quote output"),
    ("change_pct", 32, "change_pct", 32, "percent", True,
     "old scan does not preserve provider observed_at"),
    ("volume", 6, "volume", 36, "share", True,
     "legacy index 6 and normalized adapter index 36 differ; legacy unit is unverified"),
    ("amount_wan", 37, "amount_wan", 37, "CNY_10000", True,
     "new evidence is the MarketDataService CNY turnover projected back to CNY_10000; field-37 unit is limited empirical evidence"),
    ("amount_yi", 37, "amount_yi", 37, "CNY_100M", True,
     "legacy derived/rounded from amount_wan; only compare under same-time field-37 evidence"),
    ("turnover_ratio", 38, "turnover_ratio", 38, "percent", True,
     "legacy float-or-zero mapping is not source/unit proof; current Tencent adapter omits it"),
    ("pe", 39, "pe", 39, "ratio", True,
     "legacy positional assumption; provider-neutral value is optional and often absent"),
    ("mktcap_yi", 45, "mktcap_yi", 45, "CNY_100M", True,
     "legacy positional assumption; provider-neutral value is optional and often absent"),
    ("observed_at", 30, "observed_at", 30, "Asia/Shanghai timestamp", True,
     "legacy scanner does not retain field 30; scan time is not quote time"),
)


def _field(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _legacy_missing(legacy_fields: Mapping[str, Any], legacy_raw: Mapping[Any, Any],
                    name: str, raw_index: int) -> bool:
    raw_key = raw_index if raw_index in legacy_raw else str(raw_index)
    if raw_key in legacy_raw:
        return legacy_raw[raw_key] is None or str(legacy_raw[raw_key]).strip() == ""
    return name not in legacy_fields or legacy_fields.get(name) is None


def _compare_one(legacy: Mapping[str, Any], quote: ScannerQuoteEvidence,
                 spec: tuple[str, int, str, int, str, bool, str]) -> dict[str, Any]:
    old_name, old_index, new_name, new_index, units, affects_decision, note = spec
    legacy_fields = legacy.get("fields", legacy)
    raw_fields = legacy.get("raw_fields", {})
    legacy_units = legacy.get("field_units", {})
    old_missing = _legacy_missing(legacy_fields, raw_fields, old_name, old_index)
    old_value = legacy_fields.get(old_name) if isinstance(legacy_fields, Mapping) else None
    new_value = quote.observed_at if new_name == "observed_at" else (
        quote.canonical_symbol if new_name == "canonical_symbol" else quote.fields.get(new_name))
    old_unit = legacy_units.get(old_name) if isinstance(legacy_units, Mapping) else None
    new_unit = "canonical symbol" if new_name == "canonical_symbol" else (
        "Asia/Shanghai timestamp" if new_name == "observed_at" else quote.field_units.get(new_name))
    old_provider = legacy.get("provider")
    old_observed = legacy.get("observed_at")
    new_provider, new_observed = quote.provider, quote.observed_at
    values_equivalent: bool | None = None
    conversion = None

    try:
        legacy_symbol, _ = canonicalize_symbol(str(legacy.get("canonical_symbol") or
                                                   legacy.get("symbol") or ""))
    except ValueError:
        legacy_symbol = None
    if legacy_symbol != quote.canonical_symbol:
        kind = "NOT_COMPARABLE"
        note = "canonical symbol identity mismatch or missing"
    elif old_missing:
        kind = "LEGACY_FIELD_MISSING"
    elif new_value is None:
        kind = "NEW_FIELD_MISSING"
    elif old_name == "volume":
        kind = "LEGACY_UNVERIFIED"
        note = "legacy uses raw field 6; P1B normalized Tencent uses raw field 36 (lots->shares empirical evidence); legacy parsed unit/raw semantics were not retained"
    elif old_name in {"amount_wan", "amount_yi"}:
        if old_provider != "tencent" or quote.provider != "tencent":
            kind = "NOT_COMPARABLE"
            note = "field-37 conversion is provider-specific; both observations must be Tencent"
        elif not old_observed or not new_observed or old_observed != new_observed:
            kind = "TIME_BASIS_MISMATCH"
        elif old_name == "amount_wan" and old_unit == "CNY_10000" and new_unit == "CNY_10000":
            old_number, new_number = _number(old_value), _number(new_value)
            conversion = "provider-neutral CNY turnover / 10000 -> ScannerQuoteEvidence amount_wan"
            values_equivalent = (math.isclose(old_number, new_number,
                                              rel_tol=1e-9, abs_tol=0.01)
                                 if old_number is not None and new_number is not None else None)
            kind = "UNIT_RECONCILED"
        elif old_name == "amount_yi" and old_unit == "CNY_100M" and new_unit == "CNY_100M":
            old_number, new_number = _number(old_value), _number(new_value)
            values_equivalent = (math.isclose(old_number, new_number, rel_tol=1e-9, abs_tol=0.005)
                                 if old_number is not None and new_number is not None else None)
            kind = "UNIT_RECONCILED"
        else:
            kind = "LEGACY_UNVERIFIED"
            note = "legacy amount unit was not explicitly evidenced by the supplied legacy record"
    elif old_name == "observed_at":
        kind = "LEGACY_FIELD_MISSING"
        note = "legacy scanner does not retain this provider timestamp; its scan timestamp cannot substitute"
    elif not old_observed or not new_observed or old_observed != new_observed:
        kind = "TIME_BASIS_MISMATCH"
    elif old_unit is None or old_unit != new_unit:
        kind = "LEGACY_UNVERIFIED"
    else:
        old_number, new_number = _number(old_value), _number(new_value)
        if old_number is not None and new_number is not None:
            values_equivalent = math.isclose(old_number, new_number, rel_tol=1e-9, abs_tol=1e-9)
            kind = "MATCH" if values_equivalent else "NOT_COMPARABLE"
        else:
            kind = "MATCH" if old_value == new_value else "NOT_COMPARABLE"

    return {
        "symbol": quote.canonical_symbol,
        "legacy_field": old_name,
        "legacy_wire_index": old_index,
        "legacy_value": old_value,
        "legacy_unit": old_unit,
        "legacy_provider": old_provider,
        "legacy_observed_at": old_observed,
        "new_field": new_name,
        "new_wire_index": new_index,
        "new_value": new_value,
        "new_unit": new_unit,
        "new_provider": new_provider,
        "new_observed_at": new_observed,
        "difference_type": kind,
        "values_equivalent": values_equivalent,
        "conversion": conversion,
        "affects_scoring": affects_decision,
        "affects_buy_condition": affects_decision,
        "evidence_note": note,
    }


def compare_legacy_quotes(legacy_quotes: Sequence[Mapping[str, Any]],
                          preview: ScannerPreview) -> tuple[dict[str, Any], ...]:
    """Compare explicitly supplied legacy fixtures with a bounded preview.

    Legacy fixtures should include raw positional fields and provenance when
    available. Missing provenance is classified as unverified/time-mismatched,
    never silently inferred from scan/request/receive time.
    """
    if isinstance(legacy_quotes, (str, bytes)) or len(legacy_quotes) > MAX_PARITY_SYMBOLS:
        raise ValueError("PARITY_SYMBOL_BUDGET_EXCEEDED")
    if len(preview.quotes) > MAX_PARITY_SYMBOLS:
        raise ValueError("PARITY_SYMBOL_BUDGET_EXCEEDED")
    new_by_symbol = {quote.canonical_symbol: quote for quote in preview.quotes}
    if len(new_by_symbol) != len(preview.quotes):
        raise ValueError("DUPLICATE_PREVIEW_SYMBOL")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for legacy in legacy_quotes:
        if not isinstance(legacy, Mapping):
            raise ValueError("LEGACY_QUOTE_MAPPING_REQUIRED")
        try:
            symbol, _ = canonicalize_symbol(str(legacy.get("canonical_symbol") or
                                                  legacy.get("symbol") or ""))
        except ValueError:
            symbol = ""
        if not symbol or symbol in seen:
            raise ValueError("LEGACY_SYMBOL_INVALID_OR_DUPLICATE")
        seen.add(symbol)
        quote = new_by_symbol.get(symbol)
        if quote is None:
            # Represent an unmatched legacy row without inventing a new value.
            for spec in FIELD_MAP:
                rows.append({
                    "symbol": symbol, "legacy_field": spec[0], "legacy_wire_index": spec[1],
                    "legacy_value": (legacy.get("fields", legacy).get(spec[0])
                                     if isinstance(legacy.get("fields", legacy), Mapping) else None),
                    "legacy_unit": legacy.get("field_units", {}).get(spec[0]),
                    "legacy_provider": legacy.get("provider"),
                    "legacy_observed_at": legacy.get("observed_at"),
                    "new_field": spec[2], "new_wire_index": spec[3], "new_value": None,
                    "new_unit": None, "new_provider": None, "new_observed_at": None,
                    "difference_type": "NOT_COMPARABLE", "values_equivalent": None,
                    "conversion": None, "affects_scoring": spec[5],
                    "affects_buy_condition": spec[5],
                    "evidence_note": "symbol has no matched provider-neutral row; missing versus mis-keyed evidence cannot be distinguished",
                })
            continue
        rows.extend(_compare_one(legacy, quote, spec) for spec in FIELD_MAP)
    return tuple(rows)
