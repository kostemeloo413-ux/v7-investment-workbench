"""Versioned, fail-closed DATE_EFFECTIVE_TRADABLE_UNIVERSE_V2 candidate builder.

This module consumes dated security-master, same-date status, and target-bar
evidence. It never enumerates numeric code ranges to invent candidate symbols.
The legacy frozen set is accepted only as a bounded replay seed; positive
identity and date-effective eligibility must come from the supplied records.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "v7-g6-date-effective-universe-v2"
POLICY_VERSION = "DATE_EFFECTIVE_TRADABLE_UNIVERSE_V2"
KNOWN_EXCLUSION_REASONS = {
    "DELISTED",
    "NOT_YET_LISTED",
    "ST",
    "SUSPENDED",
    "WRONG_SECURITY_TYPE_OR_BOARD",
    "WRONG_BOARD",
}
MAINBOARD_PREFIXES = {
    "SH": ("600", "601", "603", "605"),
    "SZ": ("000", "001", "002", "003"),
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def symbols_sha256(symbols: Iterable[str]) -> str:
    ordered = sorted(symbols)
    return sha256_bytes(("\n".join(ordered) + "\n").encode("utf-8"))


def _date(value: Any) -> str | None:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


def _provider_symbol(value: Any) -> str | None:
    if not isinstance(value, str) or "." not in value:
        return None
    exchange, code = value.split(".", 1)
    exchange = {"sh": "SH", "sz": "SZ"}.get(exchange.lower(), "")
    if exchange not in MAINBOARD_PREFIXES or len(code) != 6 or not code.isdigit():
        return None
    return f"{code}.{exchange}"


def _legacy_code(entry: Any) -> tuple[str | None, str | None]:
    if isinstance(entry, Mapping):
        raw = str(entry.get("code") or entry.get("symbol") or "").strip()
        ex = str(entry.get("exchange") or entry.get("market") or "").upper()
        if raw.lower().startswith(("sh.", "sz.")):
            return raw.split(".", 1)[1], raw[:2].upper()
        if raw.lower().startswith(("sh", "sz")) and raw[2:].isdigit():
            return raw[2:], raw[:2].upper()
        if "." in raw:
            code, suffix = raw.split(".", 1)
            return code, suffix.upper()
        return raw, ex or None
    raw = str(entry).strip()
    if raw.lower().startswith(("sh.", "sz.")):
        return raw.split(".", 1)[1], raw[:2].upper()
    if raw.lower().startswith(("sh", "sz")) and raw[2:].isdigit():
        return raw[2:], raw[:2].upper()
    if "." in raw:
        code, suffix = raw.split(".", 1)
        return code, suffix.upper()
    return raw, None


def _numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _valid_bar(row: Mapping[str, Any]) -> tuple[bool, str | None]:
    values = {name: _numeric(row.get(name)) for name in
              ("open", "high", "low", "close", "volume", "amount")}
    if any(value is None for value in values.values()):
        return False, "BAR_REQUIRED_NUMERIC_FIELD_MISSING_OR_INVALID"
    if values["open"] <= 0 or values["high"] <= 0 or values["low"] <= 0 or values["close"] <= 0:
        return False, "BAR_PRICE_NOT_POSITIVE"
    if values["high"] < max(values["open"], values["close"], values["low"]):
        return False, "BAR_HIGH_INCONSISTENT"
    if values["low"] > min(values["open"], values["close"], values["high"]):
        return False, "BAR_LOW_INCONSISTENT"
    if values["volume"] < 0 or values["amount"] < 0:
        return False, "BAR_VOLUME_OR_AMOUNT_NEGATIVE"
    if _numeric(row.get("pctChg")) is None:
        return False, "BAR_PCT_CHANGE_MISSING_OR_INVALID"
    return True, None


def _board_for(symbol: str) -> str | None:
    code, exchange = symbol.split(".")
    if code.startswith(MAINBOARD_PREFIXES[exchange]):
        return f"{exchange}_MAINBOARD"
    return None


def _index_records(rows: Iterable[Mapping[str, Any]], field: str,
                   symbol_fn) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = symbol_fn(row.get(field))
        if symbol:
            result[symbol].append(row)
    return result


def build_date_effective_universe_v2(
    *,
    legacy_expected_codes: list[Any],
    legacy_identity_hints: Mapping[str, str],
    stock_basic_rows: list[Mapping[str, Any]],
    dated_status_rows: list[Mapping[str, Any]],
    daily_rows: list[Mapping[str, Any]],
    trade_date: str,
    source_evidence: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build an auditable eligible set and retain every rejected legacy member.

    The explicit approval allows the 3031 positively proven eligible members
    to form a READY_CANDIDATE snapshot while unresolved legacy identities stay
    as qualification failures and never enter the expected set.
    """
    target = _date(trade_date)
    if target is None:
        raise ValueError("TRADE_DATE_INVALID")
    if not legacy_expected_codes:
        raise ValueError("LEGACY_SEED_EMPTY")
    if not source_evidence or any(not item.get("sha256") for item in source_evidence.values()):
        raise ValueError("SOURCE_EVIDENCE_IDENTITY_MISSING")

    basic_by_symbol = _index_records(stock_basic_rows, "code", _provider_symbol)
    status_by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in dated_status_rows:
        symbol = _provider_symbol(row.get("code"))
        if symbol:
            status_by_symbol[symbol].append(row)
    daily_by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        symbol = _provider_symbol(row.get("code"))
        if symbol and _date(row.get("date")) == target:
            daily_by_symbol[symbol].append(row)

    # The seed only bounds the replay cohort. Provider stock_basic supplies
    # positive symbol/exchange identity; legacy hints are retained solely to
    # identify quarantined failures when the provider has no identity row.
    resolved_seed: list[tuple[str | None, str, str | None]] = []
    seen_seed: set[str] = set()
    for entry in legacy_expected_codes:
        raw_code, raw_exchange = _legacy_code(entry)
        code = str(raw_code or "").replace(".", "")
        if code.lower().startswith(("sh", "sz")):
            code = code[2:]
        if code.isdigit():
            code = code.zfill(6)
        if len(code) != 6 or not code.isdigit():
            resolved_seed.append((None, str(entry), "LEGACY_CODE_INVALID"))
            continue
        provider_matches = sorted(
            symbol for symbol, rows in basic_by_symbol.items()
            if symbol.split(".")[0] == code
            and len(rows) == 1
            and str(rows[0].get("type", "")).strip() == "1"
            and _board_for(symbol) is not None
        )
        hinted = legacy_identity_hints.get(code)
        if raw_exchange in ("SH", "SZ"):
            provider_matches = [s for s in provider_matches if s.endswith("." + raw_exchange)]
        # A legacy symbol hint may disambiguate an old bare-code row only if
        # the dated provider security master independently contains that exact
        # canonical identity. A conflicting hint/provider identity stays HOLD.
        if hinted and hinted in provider_matches:
            symbol, issue = hinted, None
        elif hinted and provider_matches:
            symbol, issue = hinted, "IDENTITY_NOT_POSITIVELY_ESTABLISHED"
        elif len(provider_matches) == 1:
            symbol, issue = provider_matches[0], None
        elif not provider_matches and hinted:
            symbol, issue = hinted, "IDENTITY_NOT_POSITIVELY_ESTABLISHED"
        elif len(provider_matches) > 1:
            symbol, issue = hinted, "IDENTITY_AMBIGUOUS"
        else:
            symbol, issue = hinted, "IDENTITY_NOT_POSITIVELY_ESTABLISHED"
        if symbol and symbol in seen_seed:
            raise ValueError("LEGACY_SEED_DUPLICATE_CANONICAL_SYMBOL")
        if symbol:
            seen_seed.add(symbol)
        resolved_seed.append((symbol, code, issue))

    if len(resolved_seed) != len(legacy_expected_codes):
        raise ValueError("LEGACY_SEED_RECONCILIATION_FAILED")

    eligible: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()

    for symbol, code, seed_issue in resolved_seed:
        basic_rows = basic_by_symbol.get(symbol, []) if symbol else []
        market_rows = status_by_symbol.get(symbol, []) if symbol else []
        bars = daily_by_symbol.get(symbol, []) if symbol else []
        basic = basic_rows[0] if len(basic_rows) == 1 else None
        market = market_rows[0] if len(market_rows) == 1 else None
        bar = bars[0] if len(bars) == 1 else None
        reason: str | None = None
        details: list[str] = []
        exchange = symbol.split(".")[1] if symbol and "." in symbol else None
        board = _board_for(symbol) if symbol else None
        listing_date = _date((basic or {}).get("ipoDate"))
        delist_date = _date((basic or {}).get("outDate"))

        if seed_issue:
            reason = seed_issue
            details.append("NO_UNIQUE_PROVIDER_SECURITY_MASTER_IDENTITY")
        elif not symbol or not basic or len(basic_rows) != 1:
            reason = "IDENTITY_NOT_POSITIVELY_ESTABLISHED"
            details.append("STOCK_BASIC_IDENTITY_MISSING_OR_DUPLICATE")
        elif str(basic.get("type", "")).strip() != "1":
            reason = "WRONG_SECURITY_TYPE_OR_BOARD"
            details.append(f"stock_basic.type={basic.get('type')!r}")
        elif board is None:
            reason = "WRONG_BOARD"
            details.append("PROVIDER_IDENTIFIED_SYMBOL_OUTSIDE_SH_SZ_MAINBOARD_CODE_CLASS")
        elif listing_date is None:
            reason = "IDENTITY_NOT_POSITIVELY_ESTABLISHED"
            details.append("IPO_DATE_MISSING_OR_INVALID")
        elif listing_date > target:
            reason = "NOT_YET_LISTED"
            details.append(f"ipoDate={listing_date}")
        elif delist_date is not None and delist_date < target:
            reason = "DELISTED"
            details.append(f"outDate={delist_date}")
        elif market is None or len(market_rows) != 1:
            reason = "STATUS_EVIDENCE_UNKNOWN"
            details.append("TARGET_DATE_ALL_STOCK_IDENTITY_OR_STATUS_MISSING_OR_DUPLICATE")
        else:
            daily_st = str((bar or {}).get("isST", "")).strip()
            daily_trade_status = str((bar or {}).get("tradestatus", "")).strip()
            market_trade_status = str(
                market.get("tradeStatus", market.get("tradestatus", ""))
            ).strip()
            if daily_st == "1":
                reason = "ST"
                details.append("SAME_DATE_DAILY_ISST_1")
            elif daily_trade_status == "0" or market_trade_status == "0":
                reason = "SUSPENDED"
                details.append("SAME_DATE_TRADE_STATUS_0")
            elif daily_st != "0":
                reason = "STATUS_EVIDENCE_UNKNOWN"
                details.append("SAME_DATE_ST_STATUS_NOT_PROVEN_NOT_ST")
            elif daily_trade_status != "1" or market_trade_status != "1":
                reason = "STATUS_EVIDENCE_UNKNOWN"
                details.append("SAME_DATE_TRADING_STATUS_NOT_PROVEN")
            elif len(bars) == 0:
                reason = "TARGET_BAR_MISSING"
                details.append("TRADING_STATUS_1_BUT_NO_SAME_DATE_BAR")
            elif len(bars) != 1:
                reason = "INVALID_TARGET_BAR"
                details.append(f"TARGET_DATE_BAR_COUNT={len(bars)}")
            else:
                valid, bar_error = _valid_bar(bar)
                if not valid:
                    reason = "INVALID_TARGET_BAR"
                    details.append(bar_error or "BAR_INVALID")

        record = {
            "canonical_symbol": symbol,
            "legacy_code": code,
            "exchange": exchange,
            "board": board,
            "trade_date": target,
            "list_date": listing_date,
            "delist_date": delist_date,
            "st_status": "NOT_ST" if reason != "ST" and bar and str(bar.get("isST", "")).strip() == "0" else ("ST" if reason == "ST" else "UNKNOWN"),
            "suspension_status": "TRADING" if reason is None and bar else ("SUSPENDED" if reason == "SUSPENDED" else "UNKNOWN"),
            "qualification_status": "ELIGIBLE" if reason is None else "EXCLUDED_OR_HOLD",
            "qualification_reason": reason,
            "reason_details": details,
            "source_identity": {
                name: {
                    key: item.get(key)
                    for key in ("source_id", "sha256", "observed_at", "effective_date", "record_scope")
                    if item.get(key) is not None
                }
                for name, item in sorted(source_evidence.items())
            },
            "source_record_ids": {
                "stock_basic_code": (basic or {}).get("code"),
                "dated_status_code": (market or {}).get("code"),
                "daily_code": (bar or {}).get("code"),
                "daily_date": _date((bar or {}).get("date")),
            },
        }
        all_results.append(record)
        category_counts[reason or "ELIGIBLE"] += 1
        if reason is None:
            eligible.append(record)
        else:
            failures.append(record)

    members = sorted(r["canonical_symbol"] for r in eligible)
    if len(members) != len(set(members)):
        raise ValueError("V2_ELIGIBLE_DUPLICATE_CANONICAL_SYMBOL")
    member_hash = symbols_sha256(members)
    qualification_hash = canonical_json_sha256(failures)
    legacy_seed_reconciled = len(resolved_seed) == len(legacy_expected_codes)
    eligible_bars_complete = bool(eligible) and all(
        r["source_record_ids"]["daily_code"]
        and r["source_record_ids"]["daily_date"] == target
        and r["st_status"] == "NOT_ST"
        and r["suspension_status"] == "TRADING"
        for r in eligible
    )
    unresolved = [r for r in failures if r["qualification_reason"] not in KNOWN_EXCLUSION_REASONS]
    # Option B changes the expected cohort by removing positively proven
    # ineligible members, but it does not let missing identity/status/bar facts
    # silently shrink the snapshot. The approved, scoped cohort can be a
    # complete eligible-set candidate only when there are no unresolved rows.
    snapshot_readiness = (
        "READY_CANDIDATE"
        if legacy_seed_reconciled and eligible_bars_complete and not unresolved
        else "HOLD_UNRESOLVED_QUALIFICATION_OR_BAR_EVIDENCE"
    )
    summary_without_id = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "trade_date": target,
        "scope": "LEGACY_MAINBOARD_CANDIDATE_COHORT; SH/SZ mainboard identity positively checked; not a claim of all-exchange exhaustive market inventory",
        "legacy_seed_count": len(legacy_expected_codes),
        "expected_count": len(members),
        "members_hash": member_hash,
        "qualification_failure_count": len(failures),
        "qualification_evidence_sha256": qualification_hash,
        "source_evidence": {name: dict(item) for name, item in sorted(source_evidence.items())},
        "snapshot_readiness": snapshot_readiness,
        "eligible_bar_set_complete": eligible_bars_complete,
        "legacy_seed_reconciled": legacy_seed_reconciled,
        "unresolved_qualification_count": len(unresolved),
        "snapshot_readiness_reasons": sorted({r["qualification_reason"] for r in unresolved}),
    }
    identity_hash = canonical_json_sha256(summary_without_id)
    return {
        **summary_without_id,
        "snapshot_identity_sha256": identity_hash,
        "members": members,
        "eligible_records": eligible,
        "qualification_failures": failures,
        "qualification_failure_counts": dict(sorted(Counter(r["qualification_reason"] for r in failures).items())),
        "all_input_results": all_results,
    }


def market_breadth_v2(snapshot: Mapping[str, Any], daily_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Candidate-only breadth: exact frozen V2 denominator and matching bars."""
    expected = list(snapshot.get("members", []))
    target = str(snapshot.get("trade_date", ""))
    counts: Counter[str] = Counter()
    for row in daily_rows:
        symbol = _provider_symbol(row.get("code"))
        if symbol in expected and _date(row.get("date")) == target:
            counts[symbol] += 1
    missing = sorted(set(expected) - set(counts))
    duplicates = sorted(symbol for symbol, count in counts.items() if count != 1)
    target_rows = [row for row in daily_rows
                   if _provider_symbol(row.get("code")) in expected and _date(row.get("date")) == target]
    if missing or duplicates or len(target_rows) != len(expected):
        return {
            "status": "HOLD_INCOMPLETE_V2_BAR_DENOMINATOR",
            "expected_count": len(expected), "bar_available_count": len(target_rows),
            "missing_symbols": missing, "duplicate_symbols": duplicates,
            "up": None, "down": None, "flat": None, "market_up_ratio": None,
        }
    changes = [_numeric(row.get("pctChg")) for row in target_rows]
    if any(value is None for value in changes):
        return {
            "status": "HOLD_INVALID_CHANGE_FIELD",
            "expected_count": len(expected), "bar_available_count": len(target_rows),
            "missing_symbols": [], "duplicate_symbols": [],
            "up": None, "down": None, "flat": None, "market_up_ratio": None,
        }
    up = sum(value > 0 for value in changes)
    down = sum(value < 0 for value in changes)
    flat = sum(value == 0 for value in changes)
    return {
        "status": "CANDIDATE_ONLY_NOT_AIQUANT_ADMITTED",
        "expected_count": len(expected), "bar_available_count": len(target_rows),
        "missing_symbols": [], "duplicate_symbols": [],
        "up": up, "down": down, "flat": flat,
        "denominator": len(expected),
        "market_up_ratio": round(up / len(expected), 4) if expected else None,
        "members_hash": snapshot.get("members_hash"),
    }


def strict_completeness_candidate(snapshot: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Mirror the existing G6 all-members rule; never adds qualification exceptions."""
    expected = set(snapshot.get("members", []))
    valid = set(result.get("valid_symbols", []))
    failed = set(result.get("failed_symbols", []))
    missing = expected - valid
    if (valid != expected or missing or failed or result.get("retry_complete") is not True
            or result.get("fallback_complete") is not True or result.get("source_valid") is not True):
        return "HOLD"
    return "PASS_CANDIDATE_ONLY"


_NON_MAINBOARD_PREFIXES = {
    "SH": ("000", "688", "689", "900"),
    "SZ": ("200", "300", "301"),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORMAL_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ISOLATED_ROOT_MARKER = ".v7_isolated_output_root"


def _canonical_sh_sz_symbol(value: Any) -> str | None:
    if not isinstance(value, str) or "." not in value:
        return None
    first, second = value.strip().split(".", 1)
    if first.lower() in ("sh", "sz"):
        exchange = {"sh": "SH", "sz": "SZ"}[first.lower()]
        code = second
    else:
        code = first
        exchange = second.upper()
    if exchange not in ("SH", "SZ") or len(code) != 6 or not code.isdigit():
        return None
    return f"{code}.{exchange}"


def _classify_mainboard(symbol: str) -> str | None:
    code, exchange = symbol.split(".", 1)
    if code.startswith(MAINBOARD_PREFIXES.get(exchange, ())):
        return f"{exchange}_MAINBOARD"
    if code.startswith(_NON_MAINBOARD_PREFIXES.get(exchange, ())):
        return f"{exchange}_NON_MAINBOARD"
    return None


def _evidence_datetime(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("SOURCE_EVIDENCE_TIMESTAMP_MISSING")


def _capture_complete(source: Mapping[str, Any], *, trade_date: str | None,
                      actual_rows: int, actual_unique: int) -> bool:
    if source.get("provider") != "BaoStock":
        return False
    if source.get("query_status") != "SUCCESS" or source.get("result_code") != 0:
        return False
    if source.get("row_count") != actual_rows or source.get("unique_count") != actual_unique:
        return False
    if source.get("row_count") != source.get("unique_count"):
        return False
    if trade_date is not None and _date(source.get("trade_date")) != trade_date:
        return False
    payload_sha = str(source.get("sha256", ""))
    if not _SHA256_RE.fullmatch(payload_sha):
        return False
    if not isinstance(source.get("source_id"), str) or not source["source_id"].strip():
        return False
    try:
        _evidence_datetime(source.get("observed_at"))
    except ValueError:
        return False
    response_sha = source.get("response_sha256")
    return response_sha is None or bool(_SHA256_RE.fullmatch(str(response_sha)))


def build_full_roster_snapshot_v2(
    *,
    stock_basic_rows: list[Mapping[str, Any]],
    all_stock_rows: list[Mapping[str, Any]],
    daily_rows: list[Mapping[str, Any]],
    trade_date: str,
    source_evidence: Mapping[str, Mapping[str, Any]],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a BaoStock-capture-based full SH/SZ mainboard V2 candidate.

    Records are admitted only from positive stock_basic identity rows. Prefixes
    classify a positively returned equity into a board; they never generate a
    symbol. Same-date all_stock and daily evidence are required to admit a
    tradable member. This is a source-bound candidate, not a production PASS.
    """
    target = _date(trade_date)
    if target is None:
        raise ValueError("TRADE_DATE_INVALID")
    if not all(isinstance(rows, list) for rows in (stock_basic_rows, all_stock_rows, daily_rows)):
        raise ValueError("CAPTURE_ROWS_MUST_BE_LISTS")
    required_sources = {"stock_basic", "all_stock", "daily"}
    if set(source_evidence) != required_sources:
        raise ValueError("SOURCE_EVIDENCE_SET_MISMATCH")

    raw_basic_codes = [str(row.get("code", "")) for row in stock_basic_rows]
    raw_all_codes = [str(row.get("code", "")) for row in all_stock_rows]
    raw_daily_keys = [(str(row.get("code", "")), _date(row.get("date"))) for row in daily_rows]
    basic_unique = len(set(raw_basic_codes))
    all_unique = len(set(raw_all_codes))
    daily_unique = len(set(raw_daily_keys))
    source_capture_checks = {
        "stock_basic": _capture_complete(source_evidence["stock_basic"], trade_date=None,
                                          actual_rows=len(stock_basic_rows), actual_unique=basic_unique),
        "all_stock": _capture_complete(source_evidence["all_stock"], trade_date=target,
                                       actual_rows=len(all_stock_rows), actual_unique=all_unique),
        "daily": _capture_complete(source_evidence["daily"], trade_date=target,
                                   actual_rows=len(daily_rows), actual_unique=daily_unique),
    }

    basic_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in stock_basic_rows:
        symbol = _canonical_sh_sz_symbol(row.get("code"))
        if symbol:
            basic_rows[symbol].append(row)
    all_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in all_stock_rows:
        symbol = _canonical_sh_sz_symbol(row.get("code"))
        if symbol:
            all_rows[symbol].append(row)
    target_bars: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        symbol = _canonical_sh_sz_symbol(row.get("code"))
        if symbol and _date(row.get("date")) == target:
            target_bars[symbol].append(row)

    stock_identities: dict[str, Mapping[str, Any]] = {}
    out_of_scope = Counter()
    unclassified_codes: list[str] = []
    malformed_identity_count = 0
    for row in stock_basic_rows:
        code = row.get("code")
        if not isinstance(code, str) or not code.lower().startswith(("sh.", "sz.")):
            continue
        if str(row.get("type", "")).strip() != "1":
            continue
        symbol = _canonical_sh_sz_symbol(code)
        if not symbol:
            malformed_identity_count += 1
            continue
        board = _classify_mainboard(symbol)
        if board is None:
            unclassified_codes.append(symbol)
            continue
        if board not in ("SH_MAINBOARD", "SZ_MAINBOARD"):
            out_of_scope[board] += 1
            continue
        stock_identities[symbol] = row

    # A duplicate positive identity is not silently resolved by row order.
    duplicate_basic = sorted(symbol for symbol, rows in basic_rows.items() if len(rows) != 1
                             and any(str(row.get("type", "")).strip() == "1" for row in rows))
    date_identities: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    exclusions = Counter()
    unresolved_reasons = {
        "IDENTITY_DATE_UNKNOWN", "DUPLICATE_IDENTITY", "STATUS_EVIDENCE_MISSING",
        "STATUS_EVIDENCE_CONFLICT", "STATUS_UNKNOWN", "TARGET_BAR_MISSING",
        "INVALID_TARGET_BAR", "DUPLICATE_STATUS", "DUPLICATE_TARGET_BAR",
        "BOARD_IDENTITY_UNKNOWN",
    }

    for symbol, basic in sorted(stock_identities.items()):
        code, exchange = symbol.split(".")
        board = _classify_mainboard(symbol)
        ipo = _date(basic.get("ipoDate"))
        out = _date(basic.get("outDate"))
        result: dict[str, Any] = {
            "canonical_symbol": symbol,
            "exchange": exchange,
            "board": board,
            "trade_date": target,
            "ipo_date": ipo,
            "out_date": out,
            "identity_source_id": source_evidence["stock_basic"].get("source_id"),
            "identity_evidence_sha256": source_evidence["stock_basic"].get("sha256"),
            "identity_record": {key: basic.get(key) for key in ("code", "type", "ipoDate", "outDate")},
            "status_source_id": source_evidence["all_stock"].get("source_id"),
            "status_evidence_sha256": source_evidence["all_stock"].get("sha256"),
            "daily_source_id": source_evidence["daily"].get("source_id"),
            "daily_evidence_sha256": source_evidence["daily"].get("sha256"),
        }
        reason = None
        all_for_symbol = all_rows.get(symbol, [])
        bars = target_bars.get(symbol, [])
        if symbol in duplicate_basic:
            reason = "DUPLICATE_IDENTITY"
        elif ipo is None:
            reason = "IDENTITY_DATE_UNKNOWN"
        elif ipo > target:
            reason = "NOT_YET_LISTED"
        elif out is not None and out < target:
            reason = "DELISTED"
        elif len(all_for_symbol) == 0:
            reason = "STATUS_EVIDENCE_MISSING"
        elif len(all_for_symbol) != 1:
            reason = "DUPLICATE_STATUS"
        else:
            all_status = str(all_for_symbol[0].get("tradeStatus", "")).strip()
            result["all_stock_trade_status"] = all_status or None
            if all_status == "0":
                reason = "SUSPENDED"
            elif all_status != "1":
                reason = "STATUS_UNKNOWN"
            elif len(bars) == 0:
                reason = "TARGET_BAR_MISSING"
            elif len(bars) != 1:
                reason = "DUPLICATE_TARGET_BAR"
            else:
                bar = bars[0]
                result["daily_record"] = {key: bar.get(key) for key in
                                           ("code", "date", "open", "high", "low", "close", "volume", "amount", "pctChg", "isST", "tradestatus")}
                is_st = str(bar.get("isST", "")).strip()
                bar_status = str(bar.get("tradestatus", "")).strip()
                result["is_st"] = {"0": False, "1": True}.get(is_st)
                result["daily_trade_status"] = bar_status or None
                if is_st == "1":
                    reason = "ST"
                elif is_st != "0":
                    reason = "STATUS_UNKNOWN"
                elif bar_status == "0":
                    reason = "STATUS_EVIDENCE_CONFLICT"
                elif bar_status != "1":
                    reason = "STATUS_UNKNOWN"
                else:
                    valid, bar_reason = _valid_bar(bar)
                    if not valid:
                        reason = "INVALID_TARGET_BAR"
                        result["bar_validation_reason"] = bar_reason
        result["qualification_status"] = "ELIGIBLE" if reason is None else "QUALIFICATION_FAILURE"
        result["qualification_reason"] = reason
        if reason is None:
            result["daily_trade_status"] = "1"
            result["is_st"] = False
            date_identities.append(result)
        else:
            exclusions[reason] += 1
            failures.append(result)

    # Preserve positively returned stock identities whose board cannot be
    # established; they are never silently dropped or treated as non-mainboard.
    for symbol in sorted(unclassified_codes):
        basic_rows_for_symbol = basic_rows.get(symbol, [])
        basic = basic_rows_for_symbol[0] if len(basic_rows_for_symbol) == 1 else {}
        ipo = _date(basic.get("ipoDate"))
        out = _date(basic.get("outDate"))
        record = {
            "canonical_symbol": symbol,
            "exchange": symbol.split(".", 1)[1],
            "board": "UNKNOWN",
            "trade_date": target,
            "ipo_date": ipo,
            "out_date": out,
            "qualification_status": "QUALIFICATION_FAILURE",
            "qualification_reason": "BOARD_IDENTITY_UNKNOWN",
            "identity_source_id": source_evidence["stock_basic"].get("source_id"),
            "identity_evidence_sha256": source_evidence["stock_basic"].get("sha256"),
            "identity_record": {key: basic.get(key) for key in ("code", "type", "ipoDate", "outDate")},
            "status_source_id": source_evidence["all_stock"].get("source_id"),
            "status_evidence_sha256": source_evidence["all_stock"].get("sha256"),
            "daily_source_id": source_evidence["daily"].get("source_id"),
            "daily_evidence_sha256": source_evidence["daily"].get("sha256"),
            "reason_details": ["POSITIVE_STOCK_MASTER_ROW_HAS_NO_APPROVED_BOARD_CLASSIFICATION"],
        }
        if ipo is not None and ipo <= target and (out is None or out >= target):
            record["date_effective_identity_unresolved"] = True
        exclusions["BOARD_IDENTITY_UNKNOWN"] += 1
        failures.append(record)

    expected_codes = sorted(row["canonical_symbol"] for row in date_identities)
    if len(expected_codes) != len(set(expected_codes)):
        raise ValueError("DUPLICATE_ELIGIBLE_MEMBER")
    members_hash = symbols_sha256(expected_codes)
    qualification_hash = canonical_json_sha256(failures)
    master_scope_complete = (
        source_capture_checks["stock_basic"]
        and malformed_identity_count == 0
        and not unclassified_codes
        and not duplicate_basic
    )
    target_inputs_complete = source_capture_checks["all_stock"] and source_capture_checks["daily"]
    unresolved = [row for row in failures if row["qualification_reason"] in unresolved_reasons]
    all_expected_have_valid_bar = len(date_identities) == len(expected_codes) and all(
        row.get("qualification_status") == "ELIGIBLE"
        and _date(row.get("daily_record", {}).get("date")) == target
        for row in date_identities
    )
    roster_status = "PASS_CANDIDATE" if master_scope_complete else "HOLD"
    eligible_status = (
        "PASS_CANDIDATE"
        if master_scope_complete and target_inputs_complete and not unresolved and all_expected_have_valid_bar
        else "HOLD"
    )
    created = created_at or datetime.now(timezone.utc).isoformat()
    source_map = {key: dict(source_evidence[key]) for key in sorted(source_evidence)}
    positive_identity_records = []
    for symbol, basic in sorted(stock_identities.items()):
        ipo = _date(basic.get("ipoDate"))
        out = _date(basic.get("outDate"))
        if ipo is not None and ipo > target:
            date_status = "NOT_YET_LISTED"
        elif out is not None and out < target:
            date_status = "DELISTED_BEFORE_TRADE_DATE"
        elif ipo is not None:
            date_status = "IDENTITY_EFFECTIVE_ON_TRADE_DATE"
        else:
            date_status = "IDENTITY_DATE_UNKNOWN"
        positive_identity_records.append({
            "canonical_symbol": symbol,
            "exchange": symbol.split(".", 1)[1],
            "board": _classify_mainboard(symbol),
            "security_type": str(basic.get("type", "")).strip(),
            "ipo_date": ipo,
            "out_date": out,
            "trade_date": target,
            "date_effective_identity_status": date_status,
            "identity_source_id": source_map["stock_basic"].get("source_id"),
            "identity_evidence_sha256": source_map["stock_basic"].get("sha256"),
        })
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "trade_date": target,
        "scope": "POSITIVE_BAOSTOCK_SECURITY_MASTER; SH/SZ MAINBOARD; code prefixes classify returned type=1 identities only",
        "identity_source": source_map["stock_basic"]["source_id"],
        "status_source": source_map["all_stock"]["source_id"],
        "daily_source": source_map["daily"]["source_id"],
        "source_evidence": source_map,
        "source_capture_checks": source_capture_checks,
        "positive_sh_sz_stock_identity_count": sum(
            1 for row in stock_basic_rows
            if isinstance(row.get("code"), str) and row["code"].lower().startswith(("sh.", "sz."))
            and str(row.get("type", "")).strip() == "1"
        ),
        "full_positive_mainboard_identity_count": len(stock_identities),
        "positive_mainboard_identity_records": positive_identity_records,
        "out_of_scope_stock_type_counts": dict(sorted(out_of_scope.items())),
        "unclassified_positive_stock_count": len(unclassified_codes),
        "unclassified_positive_stock_symbols": sorted(unclassified_codes),
        "date_effective_unclassified_identity_count": sum(
            1 for symbol in unclassified_codes
            for row in basic_rows.get(symbol, [])
            if _date(row.get("ipoDate")) is not None and _date(row.get("ipoDate")) <= target
            and (_date(row.get("outDate")) is None or _date(row.get("outDate")) >= target)
        ),
        "date_effective_identity_count": sum(1 for row in stock_identities.values()
                                              if _date(row.get("ipoDate")) is not None
                                              and _date(row.get("ipoDate")) <= target
                                              and (_date(row.get("outDate")) is None or _date(row.get("outDate")) >= target)),
        "expected_codes": expected_codes,
        "expected_count": len(expected_codes),
        "members_hash": members_hash,
        "members": expected_codes,
        "member_records": date_identities,
        "qualification_failures": failures,
        "qualification_failure_count": len(failures),
        "qualification_failure_counts": dict(sorted(exclusions.items())),
        "qualification_evidence_sha256": qualification_hash,
        "roster_completeness": roster_status,
        "eligible_completeness": eligible_status,
        "snapshot_readiness": "READY_CANDIDATE" if eligible_status == "PASS_CANDIDATE" else "HOLD",
        "created_at": created,
    }
    snapshot["snapshot_identity_sha256"] = canonical_json_sha256(snapshot)
    return snapshot


def validate_full_roster_snapshot_v2(snapshot: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not isinstance(snapshot, Mapping):
        return False, ["SNAPSHOT_NOT_OBJECT"]
    required = ("schema_version", "policy_version", "trade_date", "scope", "source_evidence",
                "expected_codes", "expected_count", "members_hash", "members",
                "qualification_failures", "qualification_evidence_sha256", "roster_completeness",
                "eligible_completeness", "snapshot_readiness", "created_at", "snapshot_identity_sha256")
    reasons.extend("SNAPSHOT_MISSING:" + key for key in required if key not in snapshot)
    if reasons:
        return False, reasons
    if snapshot.get("schema_version") != SCHEMA_VERSION or snapshot.get("policy_version") != POLICY_VERSION:
        reasons.append("SNAPSHOT_VERSION_MISMATCH")
    target = _date(snapshot.get("trade_date"))
    if target is None or target != snapshot.get("trade_date"):
        reasons.append("SNAPSHOT_DATE_INVALID")
    codes = snapshot.get("expected_codes")
    if not isinstance(codes, list) or any(_canonical_sh_sz_symbol(code) != code for code in codes):
        reasons.append("SNAPSHOT_EXPECTED_CODES_INVALID")
        codes = []
    if codes != sorted(set(codes)):
        reasons.append("SNAPSHOT_EXPECTED_CODES_NOT_SORTED_UNIQUE")
    if snapshot.get("expected_count") != len(codes):
        reasons.append("SNAPSHOT_EXPECTED_COUNT_MISMATCH")
    if snapshot.get("members_hash") != symbols_sha256(codes):
        reasons.append("SNAPSHOT_MEMBERS_HASH_MISMATCH")
    members = snapshot.get("members")
    if not isinstance(members, list):
        reasons.append("SNAPSHOT_MEMBERS_INVALID")
        members = []
    elif members != codes:
        reasons.append("SNAPSHOT_MEMBER_RECORD_SET_MISMATCH")
    member_records = snapshot.get("member_records")
    if not isinstance(member_records, list) or sorted(
            row.get("canonical_symbol") for row in member_records if isinstance(row, Mapping)) != codes:
        reasons.append("SNAPSHOT_MEMBER_EVIDENCE_SET_MISMATCH")
    for row in member_records if isinstance(member_records, list) else []:
        if not isinstance(row, Mapping):
            reasons.append("SNAPSHOT_MEMBER_RECORD_INVALID")
            continue
        symbol = row.get("canonical_symbol")
        if (_canonical_sh_sz_symbol(symbol) != symbol
                or row.get("exchange") != symbol.split(".", 1)[1]
                or row.get("board") not in ("SH_MAINBOARD", "SZ_MAINBOARD")
                or row.get("trade_date") != target
                or row.get("qualification_status") != "ELIGIBLE"
                or row.get("is_st") is not False
                or row.get("daily_trade_status") != "1"):
            reasons.append("SNAPSHOT_MEMBER_IDENTITY_OR_STATUS_INVALID")
            continue
        bar = row.get("daily_record")
        if not isinstance(bar, Mapping) or _canonical_sh_sz_symbol(bar.get("code")) != symbol or _date(bar.get("date")) != target:
            reasons.append("SNAPSHOT_MEMBER_BAR_IDENTITY_OR_DATE_INVALID")
        elif not _valid_bar(bar)[0]:
            reasons.append("SNAPSHOT_MEMBER_BAR_INVALID")
    positive_master = snapshot.get("positive_mainboard_identity_records")
    if not isinstance(positive_master, list) or len(positive_master) != snapshot.get("full_positive_mainboard_identity_count"):
        reasons.append("SNAPSHOT_POSITIVE_MASTER_COUNT_INVALID")
    failures = snapshot.get("qualification_failures")
    if isinstance(failures, list):
        seen_failure = set()
        for row in failures:
            if not isinstance(row, Mapping) or not isinstance(row.get("qualification_reason"), str) or not row["qualification_reason"]:
                reasons.append("SNAPSHOT_QUALIFICATION_FAILURE_INVALID")
                continue
            symbol = row.get("canonical_symbol")
            if symbol and _canonical_sh_sz_symbol(symbol) != symbol:
                reasons.append("SNAPSHOT_QUALIFICATION_FAILURE_SYMBOL_INVALID")
            key = (symbol, row["qualification_reason"])
            if key in seen_failure:
                reasons.append("SNAPSHOT_QUALIFICATION_FAILURE_DUPLICATE")
            seen_failure.add(key)
    checks = snapshot.get("source_capture_checks")
    if not isinstance(checks, Mapping) or set(checks) != {"stock_basic", "all_stock", "daily"}:
        reasons.append("SNAPSHOT_SOURCE_CAPTURE_CHECKS_INVALID")
    elif any(value is not True for value in checks.values()):
        reasons.append("SNAPSHOT_SOURCE_CAPTURE_INCOMPLETE")
    known_exclusions = KNOWN_EXCLUSION_REASONS | {"BOARD_IDENTITY_UNKNOWN"}
    unresolved_count = sum(1 for row in (failures if isinstance(failures, list) else [])
                           if row.get("qualification_reason") not in known_exclusions)
    master_ready = bool(checks) and all(checks.values()) and snapshot.get("unclassified_positive_stock_count") == 0
    expected_roster_status = "PASS_CANDIDATE" if master_ready else "HOLD"
    if snapshot.get("roster_completeness") != expected_roster_status:
        reasons.append("SNAPSHOT_ROSTER_STATUS_INCONSISTENT")
    expected_eligible_status = (
        "PASS_CANDIDATE" if master_ready and unresolved_count == 0
        and len(codes) == len(member_records if isinstance(member_records, list) else []) else "HOLD"
    )
    if snapshot.get("eligible_completeness") != expected_eligible_status:
        reasons.append("SNAPSHOT_ELIGIBLE_STATUS_INCONSISTENT")
    expected_readiness = "READY_CANDIDATE" if expected_eligible_status == "PASS_CANDIDATE" else "HOLD"
    if snapshot.get("snapshot_readiness") != expected_readiness:
        reasons.append("SNAPSHOT_READINESS_INCONSISTENT")
    if not isinstance(failures, list):
        reasons.append("SNAPSHOT_FAILURES_INVALID")
    elif snapshot.get("qualification_evidence_sha256") != canonical_json_sha256(failures):
        reasons.append("SNAPSHOT_QUALIFICATION_HASH_MISMATCH")
    sources = snapshot.get("source_evidence")
    if not isinstance(sources, Mapping) or set(sources) != {"stock_basic", "all_stock", "daily"}:
        reasons.append("SNAPSHOT_SOURCE_SET_INVALID")
    else:
        for name, evidence in sources.items():
            if not isinstance(evidence, Mapping) or evidence.get("provider") != "BaoStock" or not _SHA256_RE.fullmatch(str(evidence.get("sha256", ""))):
                reasons.append("SNAPSHOT_SOURCE_INVALID:" + str(name))
    if not isinstance(snapshot.get("created_at"), str) or not snapshot["created_at"].strip():
        reasons.append("SNAPSHOT_CREATED_AT_INVALID")
    identity = dict(snapshot)
    stored_identity = identity.pop("snapshot_identity_sha256", None)
    if stored_identity != canonical_json_sha256(identity):
        reasons.append("SNAPSHOT_IDENTITY_HASH_MISMATCH")
    return not reasons, list(dict.fromkeys(reasons))


def write_snapshot_atomic_v2(snapshot: Mapping[str, Any], isolated_root: str | Path) -> str:
    """Persist only under a marker-protected isolated root, never formal state.

    A content-addressed filename preserves prior snapshots; identical writes
    are idempotent. The formal production root is explicitly rejected.
    """
    valid, reasons = validate_full_roster_snapshot_v2(snapshot)
    if not valid:
        raise ValueError("SNAPSHOT_INVALID:" + ";".join(reasons))
    root = Path(isolated_root).resolve()
    try:
        root.relative_to(_FORMAL_PROJECT_ROOT)
    except ValueError:
        pass
    else:
        raise PermissionError("FORMAL_PROJECT_OUTPUT_PROHIBITED")
    marker = root / _ISOLATED_ROOT_MARKER
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != "V7_ISOLATED_NON_PRODUCTION_OUTPUT_V1":
        raise PermissionError("ISOLATED_ROOT_MARKER_REQUIRED")
    target = str(snapshot["trade_date"])
    identity = str(snapshot["snapshot_identity_sha256"])
    destination = root / "snapshots" / f"{SCHEMA_VERSION}_{target}_{identity[:16]}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if destination.exists():
        existing = destination.read_bytes()
        if existing == payload:
            return str(destination)
        raise FileExistsError("CONTENT_ADDRESSED_SNAPSHOT_COLLISION")
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return str(destination)
