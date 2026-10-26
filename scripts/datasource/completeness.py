# -*- coding: utf-8 -*-
"""G6 target-date market-data completeness contract.

This module is deliberately small and data-domain scoped.  It does not select
stocks for trading and it does not grant economic execution permission.  It
freezes the data universe, validates target-date bars after the authorized
database writer runs, records terminal failures, and emits evidence that the
Forward consumer can verify without trusting a bare ``status=PASS``.
"""

import datetime as _dt
import hashlib
import json
import math
import os
import re
import time


SCHEMA_VERSION = "v7-g6-market-completeness-v1"
UNIVERSE_SNAPSHOT_SCHEMA_VERSION = "v7-g6-universe-snapshot-v1"
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_DELAY_SEC = 0.0
DEFAULT_MAX_EVIDENCE_AGE_HOURS = 72
CODE_RE = re.compile(r"^\d{6}$")


class CompletenessContractError(ValueError):
    """Raised when a G6 contract or evidence artifact is not valid."""


def _now_iso(clock=None):
    value = clock() if clock else _dt.datetime.now(_dt.timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.isoformat()


def _parse_aware(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def normalize_trade_date(value):
    text = str(value).replace("-", "")
    if not re.fullmatch(r"\d{8}", text):
        raise CompletenessContractError("trade_date must be YYYYMMDD")
    _dt.datetime.strptime(text, "%Y%m%d")
    return text


def canonicalize_codes(codes):
    if not isinstance(codes, (list, tuple, set)):
        raise CompletenessContractError("expected_codes must be a collection")
    result = sorted({str(code).strip() for code in codes})
    if not result or any(not CODE_RE.fullmatch(code) for code in result):
        raise CompletenessContractError("expected_codes contains an invalid code")
    return result


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def sha256_codes(codes):
    canonical = canonicalize_codes(codes) if codes else []
    return sha256_bytes(("\n".join(canonical) + "\n").encode("utf-8"))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_universe_updated_at(value):
    if not isinstance(value, str) or not value.strip():
        raise CompletenessContractError("universe updated_at is required")
    text = value.strip().replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        try:
            return _dt.datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise CompletenessContractError("universe updated_at is invalid") from exc


def _source_codes(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("stocks"), list):
        raise CompletenessContractError("universe source stocks is invalid")
    codes = []
    for item in payload["stocks"]:
        if not isinstance(item, dict):
            raise CompletenessContractError("universe source stock entry is invalid")
        raw = item.get("code") or item.get("symbol") or item.get("full_code")
        digits = "".join(ch for ch in str(raw) if ch.isdigit())
        if len(digits) != 6:
            raise CompletenessContractError("universe source contains invalid code")
        codes.append(digits)
    return codes


def validate_universe_source_file(source_path, expected_codes=None,
                                  expected_sha256=None, expected_total_count=None,
                                  expected_updated_at=None):
    reasons = []
    if not isinstance(source_path, str) or not os.path.isfile(source_path):
        return False, ["UNIVERSE_SOURCE_MISSING"]
    try:
        payload = _json_load(source_path)
        raw_codes = _source_codes(payload)
        canonical_codes = canonicalize_codes(raw_codes)
        updated_at = _parse_universe_updated_at(payload.get("updated_at"))
    except (OSError, json.JSONDecodeError, CompletenessContractError):
        return False, ["UNIVERSE_SOURCE_INVALID"]
    total_count = payload.get("total_count")
    if not isinstance(total_count, int) or total_count <= 0:
        reasons.append("UNIVERSE_SOURCE_TOTAL_COUNT_INVALID")
    elif total_count != len(raw_codes) or total_count != len(canonical_codes):
        reasons.append("UNIVERSE_SOURCE_TOTAL_COUNT_MISMATCH")
    if len(raw_codes) != len(set(raw_codes)):
        reasons.append("UNIVERSE_SOURCE_DUPLICATE_CODES")
    if expected_codes is not None:
        try:
            if canonical_codes != canonicalize_codes(expected_codes):
                reasons.append("UNIVERSE_SOURCE_CODES_MISMATCH")
        except CompletenessContractError:
            reasons.append("UNIVERSE_SOURCE_CODES_INVALID")
    actual_sha = sha256_file(source_path)
    if expected_sha256 is not None and actual_sha != expected_sha256:
        reasons.append("UNIVERSE_SOURCE_SHA256_MISMATCH")
    if expected_total_count is not None and total_count != expected_total_count:
        reasons.append("UNIVERSE_SOURCE_SNAPSHOT_COUNT_MISMATCH")
    if expected_updated_at is not None and payload.get("updated_at") != expected_updated_at:
        reasons.append("UNIVERSE_SOURCE_UPDATED_AT_MISMATCH")
    return not reasons, reasons


def atomic_json_write(path, payload):
    """Use the project's existing atomic JSON writer."""
    from ai_quant.storage_utils import atomic_write_json

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    atomic_write_json(path, payload)


def _json_load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def universe_snapshot_path(completeness_root, trade_date):
    return os.path.join(completeness_root, "universe_snapshots", f"{normalize_trade_date(trade_date)}.json")


def failure_record_path(completeness_root, trade_date):
    return os.path.join(completeness_root, "failure_records", f"{normalize_trade_date(trade_date)}.json")


def evidence_path(completeness_root, trade_date):
    return os.path.join(completeness_root, f"{normalize_trade_date(trade_date)}.json")


def build_universe_snapshot(trade_date, expected_codes, universe_scope,
                            universe_version, source_path, clock=None):
    trade_date = normalize_trade_date(trade_date)
    codes = canonicalize_codes(expected_codes)
    if not isinstance(universe_scope, str) or not universe_scope:
        raise CompletenessContractError("universe_scope is required")
    if not isinstance(universe_version, str) or not universe_version:
        raise CompletenessContractError("universe_version is required")
    if not isinstance(source_path, str) or not source_path:
        raise CompletenessContractError("source_path is required")
    if not os.path.isfile(source_path):
        raise CompletenessContractError("universe source path is missing")
    try:
        source_payload = _json_load(source_path)
        source_codes = _source_codes(source_payload)
        source_updated_at = source_payload.get("updated_at")
        source_updated_dt = _parse_universe_updated_at(source_updated_at)
    except (OSError, json.JSONDecodeError, CompletenessContractError) as exc:
        raise CompletenessContractError("universe source file is invalid") from exc
    if source_payload.get("total_count") != len(source_codes) or \
            len(source_codes) != len(set(source_codes)) or \
            canonicalize_codes(source_codes) != codes:
        raise CompletenessContractError("universe source count or code set mismatch")
    if source_updated_dt.date() > _dt.datetime.strptime(trade_date, "%Y%m%d").date():
        raise CompletenessContractError("universe source is newer than target trade date")
    source_sha = sha256_file(source_path)
    return {
        "schema_version": UNIVERSE_SNAPSHOT_SCHEMA_VERSION,
        "trade_date": trade_date,
        "universe_scope": universe_scope,
        "universe_version": universe_version,
        "universe_sha256": source_sha,
        "source_total_count": source_payload["total_count"],
        "source_unique_count": len(set(source_codes)),
        "source_updated_at": source_updated_at,
        "expected_codes": codes,
        "expected_codes_sha256": sha256_codes(codes),
        "expected_count": len(codes),
        "created_at": _now_iso(clock),
        "source_path": os.path.abspath(source_path),
    }


def validate_universe_snapshot(snapshot, trade_date=None):
    if not isinstance(snapshot, dict):
        return False, ["UNIVERSE_SNAPSHOT_NOT_OBJECT"]
    required = ("schema_version", "trade_date", "universe_scope", "universe_version",
                "universe_sha256", "source_total_count", "source_unique_count",
                "source_updated_at", "expected_codes", "expected_codes_sha256",
                "expected_count", "created_at", "source_path")
    missing = [key for key in required if key not in snapshot]
    if missing:
        return False, ["UNIVERSE_SNAPSHOT_MISSING:" + ",".join(missing)]
    reasons = []
    if snapshot.get("schema_version") != UNIVERSE_SNAPSHOT_SCHEMA_VERSION:
        reasons.append("UNIVERSE_SNAPSHOT_SCHEMA_MISMATCH")
    try:
        normalized_date = normalize_trade_date(snapshot.get("trade_date"))
    except (TypeError, ValueError):
        normalized_date = None
        reasons.append("UNIVERSE_SNAPSHOT_DATE_INVALID")
    if trade_date is not None and normalized_date != normalize_trade_date(trade_date):
        reasons.append("UNIVERSE_SNAPSHOT_DATE_MISMATCH")
    try:
        codes = canonicalize_codes(snapshot.get("expected_codes"))
    except (TypeError, ValueError):
        codes = []
        reasons.append("UNIVERSE_SNAPSHOT_CODES_INVALID")
    if codes and snapshot.get("expected_codes") != codes:
        reasons.append("UNIVERSE_SNAPSHOT_CODES_NOT_CANONICAL")
    if snapshot.get("expected_count") != len(codes) or not isinstance(snapshot.get("expected_count"), int):
        reasons.append("UNIVERSE_SNAPSHOT_COUNT_MISMATCH")
    if not isinstance(snapshot.get("source_total_count"), int) or \
            not isinstance(snapshot.get("source_unique_count"), int) or \
            snapshot.get("source_total_count") != snapshot.get("source_unique_count") or \
            snapshot.get("source_total_count") != len(codes):
        reasons.append("UNIVERSE_SNAPSHOT_SOURCE_COUNT_MISMATCH")
    if codes and snapshot.get("expected_codes_sha256") != sha256_codes(codes):
        reasons.append("UNIVERSE_SNAPSHOT_CODES_HASH_MISMATCH")
    try:
        _parse_aware(snapshot.get("created_at"))
    except (TypeError, ValueError):
        reasons.append("UNIVERSE_SNAPSHOT_CREATED_AT_INVALID")
    try:
        _parse_universe_updated_at(snapshot.get("source_updated_at"))
    except CompletenessContractError:
        reasons.append("UNIVERSE_SNAPSHOT_SOURCE_UPDATED_AT_INVALID")
    for key in ("universe_scope", "universe_version", "universe_sha256", "source_path"):
        if not isinstance(snapshot.get(key), str) or not snapshot[key]:
            reasons.append("UNIVERSE_SNAPSHOT_" + key.upper() + "_INVALID")
    source_ok, source_reasons = validate_universe_source_file(
        snapshot.get("source_path"), expected_codes=codes,
        expected_sha256=snapshot.get("universe_sha256"),
        expected_total_count=snapshot.get("source_total_count"),
        expected_updated_at=snapshot.get("source_updated_at"),
    )
    if not source_ok:
        reasons.extend(source_reasons)
    return not reasons, reasons


def persist_universe_snapshot(path, snapshot):
    valid, reasons = validate_universe_snapshot(snapshot)
    if not valid:
        raise CompletenessContractError(";".join(reasons))
    atomic_json_write(path, snapshot)
    return path


def load_universe_snapshot(path, trade_date=None):
    if not os.path.isfile(path):
        raise CompletenessContractError("UNIVERSE_SNAPSHOT_MISSING")
    snapshot = _json_load(path)
    valid, reasons = validate_universe_snapshot(snapshot, trade_date)
    if not valid:
        raise CompletenessContractError(";".join(reasons))
    return snapshot


def _row_value(row, key, index=None):
    if isinstance(row, dict):
        return row.get(key)
    if index is not None and isinstance(row, (tuple, list)) and len(row) > index:
        return row[index]
    return None


def _row_source(row):
    if isinstance(row, dict):
        return row.get("source")
    if isinstance(row, (tuple, list)) and len(row) > 8:
        return row[8]
    return None


def validate_target_bar(code, trade_date, row, require_source=True):
    """Validate one database bar; tuple rows match datasource.db.get_kline."""
    trade_date = normalize_trade_date(trade_date)
    expected_code = str(code).strip()
    if isinstance(row, dict) and row.get("code") is not None and str(row.get("code")) != expected_code:
        return False, "CODE_MISMATCH"
    row_date = _row_value(row, "trade_date", 0)
    if row_date is None or normalize_trade_date(row_date) != trade_date:
        return False, "TARGET_DATE_MISSING_OR_MISMATCH"
    source = _row_source(row)
    if require_source and (not isinstance(source, str) or not source.strip() or source == "unknown"):
        return False, "DATABASE_SOURCE_MISSING"
    values = {
        "open": _row_value(row, "open", 1),
        "high": _row_value(row, "high", 2),
        "low": _row_value(row, "low", 3),
        "close": _row_value(row, "close", 4),
        "volume": _row_value(row, "volume", 5),
        "amount": _row_value(row, "amount", 6),
    }
    if any(value is None or not isinstance(value, (int, float)) or not math.isfinite(float(value))
           for value in values.values()):
        return False, "OHLCV_NULL_OR_NON_FINITE"
    if any(float(values[key]) <= 0 for key in ("open", "high", "low", "close")):
        return False, "OHLC_NONPOSITIVE"
    if float(values["volume"]) < 0 or float(values["amount"]) < 0:
        return False, "VOLUME_OR_AMOUNT_NEGATIVE"
    if float(values["high"]) < max(float(values["open"]), float(values["close"]), float(values["low"])):
        return False, "OHLC_INCONSISTENT"
    if float(values["low"]) > min(float(values["open"]), float(values["close"]), float(values["high"])):
        return False, "OHLC_INCONSISTENT"
    return True, "TARGET_DATE_BAR_VALID"


def validate_target_rows(code, trade_date, rows, require_source=True):
    if not isinstance(rows, (list, tuple)) or not rows:
        return False, "TARGET_DATE_BAR_MISSING"
    matching = []
    for row in rows:
        try:
            if normalize_trade_date(_row_value(row, "trade_date", 0)) == normalize_trade_date(trade_date):
                matching.append(row)
        except (TypeError, ValueError):
            continue
    if len(matching) != 1:
        return False, "TARGET_DATE_BAR_DUPLICATE_OR_MISSING"
    return validate_target_bar(code, trade_date, matching[0], require_source=require_source)


def _normalise_fetch_result(result):
    if isinstance(result, dict):
        rows = result.get("rows", [])
        provider_attempts = result.get("provider_attempts", [])
        source = result.get("source")
    else:
        rows = result or []
        provider_attempts = []
        source = None
    if not isinstance(rows, (list, tuple)):
        rows = []
    if not isinstance(provider_attempts, list):
        provider_attempts = [str(provider_attempts)]
    return list(rows), [str(item) for item in provider_attempts], source


def _load_failure_records(path):
    if not os.path.isfile(path):
        return {}
    value = _json_load(path)
    if not isinstance(value, dict):
        raise CompletenessContractError("FAILURE_RECORDS_NOT_OBJECT")
    return value


def _persist_failures(path, records):
    atomic_json_write(path, records)


def _failure_record(trade_date, code, attempts, provider_attempts, reason, clock, status="FAILED"):
    return {
        "trade_date": normalize_trade_date(trade_date),
        "symbol": str(code),
        "attempt_count": attempts,
        "provider_attempts": provider_attempts,
        "failure_reason": reason,
        "last_attempt_at": _now_iso(clock),
        "final_status": status,
    }


def _is_trading_day(value, trading_day_fn):
    if trading_day_fn is None:
        return True
    try:
        return bool(trading_day_fn(_dt.datetime.strptime(value, "%Y%m%d").date()))
    except TypeError:
        return bool(trading_day_fn(value))


def collect_target_date(snapshot, completeness_root, fetch_history, read_target_rows,
                        write_rows=None, trading_day_fn=None, max_attempts=DEFAULT_MAX_ATTEMPTS,
                        retry_delay_sec=DEFAULT_RETRY_DELAY_SEC, clock=None,
                        failure_records=None):
    """Collect and revalidate one frozen universe for one target date.

    ``fetch_history`` may return rows or ``{"rows": ..., "source": ...}``.
    ``write_rows`` is the authorized writer seam used by tests; production's
    provider already writes through ``datasource.db.upsert_klines``.
    """
    ok, snapshot_reasons = validate_universe_snapshot(snapshot)
    if not ok:
        raise CompletenessContractError(";".join(snapshot_reasons))
    trade_date = snapshot["trade_date"]
    if not _is_trading_day(trade_date, trading_day_fn):
        return build_completeness_evidence(
            snapshot, completeness_root, valid_symbols=[], failed_symbols=[], missing_symbols=snapshot["expected_codes"],
            retry_complete=True, fallback_complete=True, source_summary={},
            failure_reasons=["NON_TRADING_DATE"], database_validation_timestamp=_now_iso(clock), clock=clock,
            force_status="HOLD")
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise CompletenessContractError("max_attempts must be >= 1")
    failure_path = failure_record_path(completeness_root, trade_date)
    records = dict(failure_records or _load_failure_records(failure_path))
    valid_symbols = []
    source_summary = {}
    source_by_symbol = {}
    failure_reasons = []
    for code in snapshot["expected_codes"]:
        attempts = 0
        providers_seen = []
        passed = False
        last_reason = "TARGET_DATE_BAR_MISSING"
        while attempts < max_attempts and not passed:
            attempts += 1
            try:
                raw = fetch_history(code, trade_date)
                rows, provider_attempts, source = _normalise_fetch_result(raw)
                providers_seen.extend(provider_attempts)
                if rows and write_rows is not None:
                    write_rows(code, rows, source=source or "unknown")
                persisted_rows = read_target_rows(code, trade_date)
                passed, last_reason = validate_target_rows(code, trade_date, persisted_rows, require_source=True)
                if passed:
                    valid_symbols.append(code)
                    persisted_sources = {_row_source(row) for row in persisted_rows
                                         if _row_source(row) and
                                         normalize_trade_date(_row_value(row, "trade_date", 0)) == trade_date}
                    if len(persisted_sources) != 1:
                        passed = False
                        last_reason = "DATABASE_SOURCE_AMBIGUOUS"
                        valid_symbols.pop()
                        continue
                    source_name = next(iter(persisted_sources))
                    source_by_symbol[code] = source_name
                    source_summary[source_name] = source_summary.get(source_name, 0) + 1
                    records.pop(code, None)
                    break
            except KeyboardInterrupt:
                record = _failure_record(
                    trade_date, code, attempts, providers_seen or ["unknown"],
                    "INTERRUPTED", clock, status="INTERRUPTED"
                )
                records[code] = record
                _persist_failures(failure_path, records)
                raise
            except Exception as exc:
                last_reason = "COLLECTION_EXCEPTION:" + type(exc).__name__
            if not passed and retry_delay_sec:
                time.sleep(retry_delay_sec)
        if not passed:
            record = _failure_record(trade_date, code, attempts, providers_seen or ["unknown"], last_reason, clock)
            records[code] = record
            failure_reasons.append(f"{code}:{last_reason}")
        _persist_failures(failure_path, records)
    failed_symbols = sorted(records.keys())
    missing_symbols = sorted(set(snapshot["expected_codes"]) - set(valid_symbols))
    fallback_complete = not any("FALLBACK_INCOMPLETE" in reason for reason in failure_reasons)
    evidence = build_completeness_evidence(
        snapshot, completeness_root, valid_symbols=valid_symbols,
        failed_symbols=failed_symbols, missing_symbols=missing_symbols,
        retry_complete=True, fallback_complete=fallback_complete,
        source_summary=source_summary, source_by_symbol=source_by_symbol,
        failure_reasons=failure_reasons,
        database_validation_timestamp=_now_iso(clock), clock=clock,
    )
    atomic_json_write(evidence_path(completeness_root, trade_date), evidence)
    return evidence


def build_completeness_evidence(snapshot, completeness_root, valid_symbols, failed_symbols,
                                missing_symbols, retry_complete, fallback_complete,
                                source_summary, failure_reasons,
                                database_validation_timestamp, clock=None, force_status=None,
                                source_by_symbol=None):
    valid_symbols = canonicalize_codes(valid_symbols) if valid_symbols else []
    failed_symbols = canonicalize_codes(failed_symbols) if failed_symbols else []
    missing_symbols = canonicalize_codes(missing_symbols) if missing_symbols else []
    expected = set(snapshot["expected_codes"])
    status = force_status or (
        "PASS" if snapshot["expected_count"] > 0 and set(valid_symbols) == expected and
        not failed_symbols and not missing_symbols and retry_complete and fallback_complete else "HOLD")
    return {
        "schema_version": SCHEMA_VERSION,
        "season_id": snapshot.get("season_id"),
        "trade_date": snapshot["trade_date"],
        "status": status,
        "generated_at": _now_iso(clock),
        "expected_universe_count": snapshot["expected_count"],
        "successful_symbol_count": len(valid_symbols),
        "failed_symbol_count": len(failed_symbols),
        "missing_symbol_count": len(missing_symbols),
        "retry_complete": bool(retry_complete),
        "fallback_complete": bool(fallback_complete),
        "universe_scope": snapshot["universe_scope"],
        "universe_version": snapshot["universe_version"],
        "universe_sha256": snapshot["universe_sha256"],
        "expected_codes_sha256": snapshot["expected_codes_sha256"],
        "valid_symbols_sha256": sha256_codes(valid_symbols),
        "failed_symbols_sha256": sha256_codes(failed_symbols),
        "missing_symbols_sha256": sha256_codes(missing_symbols),
        "valid_symbols": valid_symbols,
        "failed_symbols": failed_symbols,
        "missing_symbols": missing_symbols,
        "database_validation_timestamp": database_validation_timestamp,
        "source_summary": dict(source_summary or {}),
        "source_by_symbol": dict(source_by_symbol or {}),
        "failure_reasons": list(failure_reasons or []),
        "universe_snapshot_path": universe_snapshot_path(completeness_root, snapshot["trade_date"]),
    }


def validate_completeness_evidence(evidence, snapshot, season_id, trade_date,
                                   now=None, max_age_hours=DEFAULT_MAX_EVIDENCE_AGE_HOURS):
    reasons = []
    if not isinstance(evidence, dict):
        return False, ["EVIDENCE_NOT_OBJECT"]
    if evidence.get("schema_version") != SCHEMA_VERSION:
        reasons.append("EVIDENCE_SCHEMA_MISMATCH")
    snapshot_ok, snapshot_reasons = validate_universe_snapshot(snapshot, trade_date)
    if not snapshot_ok:
        reasons.extend(snapshot_reasons)
    expected_date = normalize_trade_date(trade_date)
    if evidence.get("season_id") != season_id:
        reasons.append("EVIDENCE_SEASON_MISMATCH")
    if evidence.get("trade_date") != expected_date:
        reasons.append("EVIDENCE_DATE_MISMATCH")
    if evidence.get("status") != "PASS":
        reasons.append("EVIDENCE_STATUS_NOT_PASS")
    required = ("generated_at", "database_validation_timestamp", "universe_scope", "universe_version",
                "universe_sha256", "expected_codes_sha256", "valid_symbols_sha256",
                "failed_symbols_sha256", "missing_symbols_sha256", "source_summary",
                "source_by_symbol", "failure_reasons")
    for key in required:
        if key not in evidence:
            reasons.append("EVIDENCE_MISSING:" + key)
    for key in ("generated_at", "database_validation_timestamp"):
        try:
            parsed = _parse_aware(evidence.get(key))
            if now is not None and (now - parsed).total_seconds() > max_age_hours * 3600:
                reasons.append("EVIDENCE_EXPIRED")
        except (TypeError, ValueError):
            reasons.append("EVIDENCE_TIMESTAMP_INVALID:" + key)
    expected_codes = list(snapshot.get("expected_codes", [])) if isinstance(snapshot, dict) else []
    valid = evidence.get("valid_symbols")
    failed = evidence.get("failed_symbols")
    missing = evidence.get("missing_symbols")
    for name, values in (("valid", valid), ("failed", failed), ("missing", missing)):
        if not isinstance(values, list):
            reasons.append("EVIDENCE_" + name.upper() + "_SYMBOLS_INVALID")
        elif values:
            try:
                canonicalize_codes(values)
            except (TypeError, ValueError):
                reasons.append("EVIDENCE_" + name.upper() + "_SYMBOLS_INVALID")
    if isinstance(valid, list) and evidence.get("valid_symbols_sha256") != sha256_codes(valid):
        reasons.append("EVIDENCE_VALID_SYMBOLS_HASH_MISMATCH")
    if isinstance(failed, list) and evidence.get("failed_symbols_sha256") != sha256_codes(failed):
        reasons.append("EVIDENCE_FAILED_SYMBOLS_HASH_MISMATCH")
    if isinstance(missing, list) and evidence.get("missing_symbols_sha256") != sha256_codes(missing):
        reasons.append("EVIDENCE_MISSING_SYMBOLS_HASH_MISMATCH")
    if evidence.get("expected_codes_sha256") != snapshot.get("expected_codes_sha256"):
        reasons.append("EVIDENCE_EXPECTED_CODES_HASH_MISMATCH")
    if evidence.get("universe_sha256") != snapshot.get("universe_sha256"):
        reasons.append("EVIDENCE_UNIVERSE_HASH_MISMATCH")
    if evidence.get("universe_scope") != snapshot.get("universe_scope") or evidence.get("universe_version") != snapshot.get("universe_version"):
        reasons.append("EVIDENCE_UNIVERSE_IDENTITY_MISMATCH")
    numeric_fields = ("expected_universe_count", "successful_symbol_count", "failed_symbol_count", "missing_symbol_count")
    if any(not isinstance(evidence.get(key), int) or evidence[key] < 0 for key in numeric_fields):
        reasons.append("EVIDENCE_COUNTS_INVALID")
    elif evidence["expected_universe_count"] != len(expected_codes) or evidence["expected_universe_count"] <= 0:
        reasons.append("EVIDENCE_EXPECTED_COUNT_MISMATCH")
    if isinstance(valid, list) and isinstance(evidence.get("successful_symbol_count"), int) and \
            evidence["successful_symbol_count"] != len(valid):
        reasons.append("EVIDENCE_SUCCESSFUL_COUNT_MISMATCH")
    if isinstance(failed, list) and isinstance(evidence.get("failed_symbol_count"), int) and \
            evidence["failed_symbol_count"] != len(failed):
        reasons.append("EVIDENCE_FAILED_COUNT_MISMATCH")
    if isinstance(missing, list) and isinstance(evidence.get("missing_symbol_count"), int) and \
            evidence["missing_symbol_count"] != len(missing):
        reasons.append("EVIDENCE_MISSING_COUNT_MISMATCH")
    if isinstance(valid, list) and set(valid) != set(expected_codes):
        reasons.append("EVIDENCE_VALID_SET_MISMATCH")
    if isinstance(failed, list) and failed:
        reasons.append("EVIDENCE_FAILED_SYMBOLS_PRESENT")
    if isinstance(missing, list) and missing:
        reasons.append("EVIDENCE_MISSING_SYMBOLS_PRESENT")
    if not isinstance(evidence.get("retry_complete"), bool) or not evidence.get("retry_complete"):
        reasons.append("EVIDENCE_RETRY_INCOMPLETE")
    if not isinstance(evidence.get("fallback_complete"), bool) or not evidence.get("fallback_complete"):
        reasons.append("EVIDENCE_FALLBACK_INCOMPLETE")
    source_by_symbol = evidence.get("source_by_symbol")
    if not isinstance(source_by_symbol, dict):
        reasons.append("EVIDENCE_SOURCE_BY_SYMBOL_INVALID")
    elif set(source_by_symbol) != set(valid or []):
        reasons.append("EVIDENCE_SOURCE_SYMBOL_SET_MISMATCH")
    elif any(not isinstance(value, str) or not value.strip() or value == "unknown"
             for value in source_by_symbol.values()):
        reasons.append("EVIDENCE_SOURCE_VALUE_INVALID")
    if not isinstance(evidence.get("source_summary"), dict) or not isinstance(evidence.get("failure_reasons"), list):
        reasons.append("EVIDENCE_SOURCE_OR_FAILURE_SHAPE_INVALID")
    elif isinstance(source_by_symbol, dict):
        expected_summary = {}
        for value in source_by_symbol.values():
            expected_summary[value] = expected_summary.get(value, 0) + 1
        if evidence.get("source_summary") != expected_summary:
            reasons.append("EVIDENCE_SOURCE_SUMMARY_MISMATCH")
    return not reasons, list(dict.fromkeys(reasons))
