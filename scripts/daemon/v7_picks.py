# -*- coding: utf-8 -*-
"""Read-only V7 picks and stock-evidence projections.

This module deliberately does not invoke the scanner, Skills, AI Quant inference,
providers, or any write-capable database helper. Legacy persisted picks are
display evidence only and never become execution authorization.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PICKS_PATH = BASE_DIR / "workbench" / "data.json"
DEFAULT_MARKET_DB = BASE_DIR / "data" / "market" / "market.db"
DEFAULT_CONTROL_PATH = BASE_DIR / "docs" / "v7_control" / "CURRENT_TASK.md"
SHANGHAI = ZoneInfo("Asia/Shanghai")
ROLE_ORDER = (
    "P-LIAO-XIAOAI",
    "P-COOL",
    "P-YINWENTAO",
    "P-HANSHU",
    "P-RULE-QUANT",
    "P-AI-QUANT",
)
ROLE_NAMES = {
    "P-LIAO-XIAOAI": "廖小爱",
    "P-COOL": "酷儿",
    "P-YINWENTAO": "殷文涛",
    "P-HANSHU": "韩叔",
    "P-RULE-QUANT": "规则量化派",
    "P-AI-QUANT": "AI Quant",
}
LEGACY_IDS = {
    "liao-xiaoai": "P-LIAO-XIAOAI",
    "cool": "P-COOL",
    "yintwentao": "P-YINWENTAO",
    "hanshu": "P-HANSHU",
    "ai-alpha": "P-RULE-QUANT",
    "anti-demo": "P-RISK-SENTINEL",
}
LEGACY_SOURCE = "LEGACY_PRODUCTION_SCANNER"
LEGACY_ADMISSION = "NOT_YET_MIGRATED_TO_P1C3"


def _load_json(path: str | Path) -> tuple[Any | None, str | None]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            return json.load(stream), None
    except FileNotFoundError:
        return None, "FILE_MISSING"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "FILE_UNREADABLE_OR_INVALID"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        # The legacy scanner records local wall time without an offset. This
        # assumption is used only to derive a display trade_date; it is not an
        # observed market timestamp or quote-freshness proof.
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _six_digit_code(value: Any) -> str | None:
    if not isinstance(value, str):
        if isinstance(value, int):
            value = f"{value:06d}"
        else:
            return None
    text = value.strip().upper()
    match = re.fullmatch(r"(?:(?:SH|SZ|BJ)[.]?)?(\d{6})(?:\.(?:SH|SZ|BJ))?", text)
    return match.group(1) if match else None


def _participant_id(item: dict[str, Any]) -> str | None:
    participant = item.get("participant_id")
    if participant in ROLE_NAMES or participant == "P-RISK-SENTINEL":
        return participant
    legacy = item.get("blogger_id")
    return LEGACY_IDS.get(legacy) if isinstance(legacy, str) else None


def _decision_for(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("skill_decision")
    return value if isinstance(value, dict) else {}


def _recorded_scan_evidence(item: dict[str, Any], role_id: str,
                            scan_at: str | None, current_day: bool) -> dict[str, Any]:
    """Project retained scan records, never provider observation/admission proof."""
    decision = _decision_for(item)
    freshness = decision.get("data_freshness")
    freshness = freshness if isinstance(freshness, dict) else {}
    refs = decision.get("evidence_refs")
    refs = refs if isinstance(refs, list) else []
    candidate = decision.get("candidate_evidence")
    candidate = candidate if isinstance(candidate, dict) else {}
    code = _six_digit_code(item.get("stock_code"))
    identities = [decision.get("symbol"), candidate.get("symbol"), candidate.get("code")]
    mismatch = (decision.get("participant_id") not in (None, role_id)
                or any(value is not None and _six_digit_code(value) != code for value in identities))
    identity_complete = bool(code and decision.get("participant_id") == role_id
                             and decision.get("symbol") and candidate.get("symbol") and candidate.get("code"))
    recorded_at = _parse_timestamp(freshness.get("scan_timestamp"))
    scan = _parse_timestamp(scan_at)
    reference_match = bool(code and recorded_at and any(
        isinstance(ref, str) and ref.startswith(f"tencent_quote:{code}:")
        and _parse_timestamp(ref.split(":", 2)[2]) == recorded_at for ref in refs))
    reasons = []
    if mismatch:
        reasons.append("SCAN_EVIDENCE_IDENTITY_MISMATCH")
    if not identity_complete:
        reasons.append("SCAN_EVIDENCE_IDENTITY_INCOMPLETE")
    if not (current_day and scan and recorded_at and recorded_at.date() == scan.date()
            and recorded_at <= scan):
        reasons.append("SCAN_EVIDENCE_DATE_NOT_CURRENT_OR_UNBOUND")
    if freshness.get("source") != "Tencent realtime quote" or not reference_match:
        reasons.append("SCAN_SOURCE_REFERENCE_NOT_BOUND")
    if freshness.get("stale") is True or freshness.get("error") or freshness.get("error_message"):
        reasons.append("SCAN_SOURCE_STALE_OR_ERROR")
    matched = not reasons
    return {"status": "RECORDED_CURRENT_SCAN" if matched else "UNVERIFIED",
            "identity_status": "MISMATCH" if mismatch else ("MATCH" if identity_complete else "PARTIAL"),
            "source_name": "腾讯行情记录" if matched else None,
            "scan_at": recorded_at.isoformat() if matched else None,
            "source_record": freshness, "evidence_refs": refs, "reason_codes": reasons}


def _normalise_pick(item: dict[str, Any], *, role_id: str, scan_at: str | None,
                    current_day: bool, include_scan_evidence: bool = False) -> dict[str, Any]:
    decision = _decision_for(item)
    code = _six_digit_code(item.get("stock_code") or decision.get("symbol"))
    raw_action = decision.get("action")
    action = raw_action if isinstance(raw_action, str) and raw_action else "UNKNOWN"
    evidence = decision.get("candidate_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    triggered = decision.get("skill_rules_triggered")
    failed = decision.get("skill_rules_failed")
    return {
        "participant_id": role_id,
        "display_name": ROLE_NAMES.get(role_id, str(item.get("display_name") or "UNKNOWN")),
        "role_type": "ML_QUANT" if role_id == "P-AI-QUANT" else "RETURN_BEARING_ROLE",
        "symbol": code,
        "stock_name": item.get("stock_name") if isinstance(item.get("stock_name"), str) else None,
        "action": action,
        "signal": item.get("signal") if isinstance(item.get("signal"), str) else None,
        "decision_reason": decision.get("decision_reason") if isinstance(decision.get("decision_reason"), str) else (item.get("trigger_reason") if isinstance(item.get("trigger_reason"), str) else None),
        "skill_rules_triggered": triggered if isinstance(triggered, list) else [],
        "skill_rules_failed": failed if isinstance(failed, list) else [],
        "candidate_evidence": evidence,
        "position_plan": decision.get("position_plan") if isinstance(decision.get("position_plan"), dict) else None,
        "exit_plan": decision.get("exit_plan") if isinstance(decision.get("exit_plan"), dict) else None,
        "holding_horizon": (decision.get("holding_horizon") if include_scan_evidence
                            else decision.get("holding_horizon") or item.get("horizon")),
        "skill_version": decision.get("skill_version"),
        "decision_id": decision.get("decision_id"),
        "decision_at": decision.get("timestamp"),
        "scan_timestamp": scan_at,
        "scan_date_qualification": "CURRENT_TRADING_DATE" if current_day else "HISTORICAL_OR_NON_TRADING_DATE",
        "source_type": LEGACY_SOURCE,
        "data_admission": LEGACY_ADMISSION,
        "data_status": "UNKNOWN",
        "observed_at": None,
        "legacy_field_provenance": True,
        "execution_authorized": False,
        "execution_status": "NOT_AUTHORIZED",
        **({"scan_evidence": _recorded_scan_evidence(item, role_id, scan_at, current_day)}
           if include_scan_evidence else {}),
    }


def _read_prediction_pulses(db_path: str | Path) -> tuple[list[dict[str, Any]], str | None]:
    path = Path(db_path)
    if not path.exists():
        return [], "MARKET_DB_UNAVAILABLE"
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_quant_predictions'"
        ).fetchone()
        if not exists:
            return [], "PREDICTION_TABLE_UNAVAILABLE"
        grouped = conn.execute("""
            SELECT trade_date, predict_time, model_version, COUNT(*)
              FROM ai_quant_predictions
             GROUP BY trade_date, predict_time, model_version
             ORDER BY trade_date DESC, predict_time DESC, model_version DESC
        """).fetchall()
        pulses: list[dict[str, Any]] = []
        for trade_date, predict_time, model_version, count in grouped:
            rows = conn.execute("""
                SELECT symbol, name, rank, raw_score, up_probability, expected_return,
                       expected_mae, right_side_score, expected_holding_days,
                       predict_time, trade_date, model_version
                  FROM ai_quant_predictions
                 WHERE trade_date=? AND predict_time=? AND model_version=?
                 ORDER BY rank ASC, symbol ASC
            """, (trade_date, predict_time, model_version)).fetchall()
            ranks = [row[2] for row in rows]
            symbols = [str(row[0]) for row in rows]
            complete = (count == 5 and len(rows) == 5 and set(ranks) == {1, 2, 3, 4, 5}
                        and len(set(ranks)) == 5 and len(set(symbols)) == 5)
            predictions = [{
                "symbol": _six_digit_code(str(row[0])), "name": row[1], "rank": row[2],
                "raw_score": row[3], "up_probability": row[4],
                "expected_return": row[5], "predicted_mae": row[6],
                "right_side_score": row[7], "expected_holding_days": row[8],
                "predict_time": row[9], "trade_date": row[10], "model_version": row[11],
            } for row in rows]
            pulses.append({
                "trade_date": str(trade_date), "predict_time": str(predict_time),
                "model_version": str(model_version), "row_count": int(count),
                "complete": complete, "predictions": predictions,
            })
        return pulses, None
    except sqlite3.Error:
        return [], "PREDICTION_READ_FAILED"
    finally:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()


def build_ai_quant_evidence(db_path: str | Path = DEFAULT_MARKET_DB, *,
                            current_trade_date: str | date | None = None,
                            is_trading_day: Callable[[date], bool] | None = None) -> dict[str, Any]:
    """Select exactly one complete prediction pulse; never merge by row id."""
    if isinstance(current_trade_date, date):
        current_day = current_trade_date.strftime("%Y%m%d")
    elif isinstance(current_trade_date, str):
        current_day = current_trade_date.replace("-", "")
    else:
        current_day = datetime.now(SHANGHAI).strftime("%Y%m%d")
    try:
        target_date = datetime.strptime(current_day, "%Y%m%d").date()
    except ValueError:
        return {"status": "UNAVAILABLE", "classification": "NO_VALID_CURRENT_DATE", "predictions": [], "pulse": None}
    if is_trading_day is None:
        try:
            import runtime_config as rc
            is_trading_day = rc.is_trading_day
        except Exception:
            is_trading_day = lambda _day: False
    is_current_session = bool(is_trading_day(target_date))
    pulses, error = _read_prediction_pulses(db_path)
    if error:
        return {"status": "UNAVAILABLE", "classification": error, "predictions": [], "pulse": None}
    current = next((p for p in pulses if p["trade_date"].replace("-", "") == current_day and p["complete"]), None) if is_current_session else None
    if current:
        return {"status": "NORMAL", "classification": "CURRENT_DAY_TOP5", "predictions": current["predictions"], "pulse": {k: current[k] for k in ("trade_date", "predict_time", "model_version", "row_count")}}
    historical = next((p for p in pulses if p["complete"]), None)
    if historical:
        return {"status": "STALE", "classification": "LATEST_HISTORICAL_PREDICTION", "predictions": historical["predictions"], "pulse": {k: historical[k] for k in ("trade_date", "predict_time", "model_version", "row_count")}}
    return {"status": "EMPTY", "classification": "NO_COMPLETE_PREDICTION_PULSE", "predictions": [], "pulse": None}


def _read_gate_status(control_path: str | Path = DEFAULT_CONTROL_PATH, gate: str = "GATE_B") -> str:
    """Read governed active-season gate truth; Markdown is never authority."""
    del control_path  # Human-facing control notes cannot grant runtime permission.
    try:
        from competition.season_runtime import resolve_active_season_context
        from ai_quant.storage_utils import strict_load_json
        context = resolve_active_season_context()
        result = strict_load_json(Path(context['season_root']) / 'gate_state.json', expected_type=dict)
        if not result.is_valid:
            return 'UNKNOWN'
        state = result.value
        if (state.get('schema_version') != 'v7-gate-decision-v1'
                or state.get('season_id') != context['season_id']
                or state.get('mode') != 'PAPER/SIMULATION'
                or state.get('real_trading') is not False
                or not isinstance(state.get('evidence_refs'), list)
                or not state['evidence_refs']):
            return 'UNKNOWN'
        if gate == 'GATE_B':
            if (state.get('gate_a') != 'CLOSED/PASS' or state.get('g4') != 'PASS'
                    or state.get('g5') != 'PASS' or state.get('g6') != 'CLOSED/PASS'
                    or state.get('gate_b') != 'CLOSED/PASS'):
                return 'HOLD'
            return 'CLOSED/PASS'
        return str(state.get(gate.lower(), 'UNKNOWN'))
    except (ImportError, KeyError, OSError, ValueError, RuntimeError, TypeError):
        return 'UNKNOWN'


def build_picks_view(picks_path: str | Path = DEFAULT_PICKS_PATH,
                     db_path: str | Path = DEFAULT_MARKET_DB,
                     control_path: str | Path = DEFAULT_CONTROL_PATH, *,
                     now: datetime | None = None,
                     current_trade_date: str | date | None = None,
                     is_trading_day: Callable[[date], bool] | None = None,
                     gate_b_status: str | None = None,
                     g6_status: str = "OPEN") -> dict[str, Any]:
    now = now or datetime.now(SHANGHAI)
    now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    if isinstance(current_trade_date, date):
        current_day = current_trade_date.strftime("%Y%m%d")
    elif isinstance(current_trade_date, str):
        current_day = current_trade_date.replace("-", "")
    else:
        current_day = now.strftime("%Y%m%d")
    if is_trading_day is None:
        try:
            import runtime_config as rc
            is_trading_day = rc.is_trading_day
        except Exception:
            is_trading_day = lambda _day: False
    parsed_current = None
    try:
        parsed_current = datetime.strptime(current_day, "%Y%m%d").date()
    except ValueError:
        pass
    trading_day = bool(parsed_current and is_trading_day(parsed_current))
    raw, error = _load_json(picks_path)
    warnings: list[str] = []
    if error or not isinstance(raw, dict):
        unavailable = "INVALID" if error == "FILE_UNREADABLE_OR_INVALID" or (error is None and not isinstance(raw, dict)) else "UNAVAILABLE"
        return {"status": unavailable, "warnings": [error or "PICKS_ROOT_INVALID"], "data_freshness": {"trade_date": None},
                "data": {"as_of": None, "trade_date": None, "source": "workbench/data.json", "source_type": LEGACY_SOURCE,
                         "data_status": unavailable, "participants": [
                             {"participant_id": role, "display_name": ROLE_NAMES[role], "role_type": "RETURN_BEARING_ROLE",
                              "action": "UNAVAILABLE", "data_status": unavailable, "execution_authorized": False}
                             for role in ROLE_ORDER if role != "P-AI-QUANT"
                         ] + [{"participant_id": "P-AI-QUANT", "display_name": ROLE_NAMES["P-AI-QUANT"],
                               "role_type": "ML_QUANT", "action": "UNAVAILABLE", "data_status": unavailable,
                               "execution_authorized": False}],
                         "ai_quant_top5": {"status": "UNAVAILABLE", "predictions": [], "pulse": None},
                         "risk_sentinel": {"status": "UNAVAILABLE", "trading_enabled": False, "ranking_enabled": False},
                          "gate_b_status": _read_gate_status(control_path), "g6_status": g6_status,
                         "execution_authorized": False}}
    rows = raw.get("blogger_picks")
    if not isinstance(rows, list):
        rows = []
        warnings.append("PERSISTED_BLOGGER_PICKS_MISSING")
    scan_value = raw.get("picks_updated_at")
    scan_dt = _parse_timestamp(scan_value)
    scan_at = scan_dt.isoformat() if scan_dt else None
    trade_date = scan_dt.strftime("%Y%m%d") if scan_dt else None
    future_scan = bool(scan_dt and scan_dt > now)
    current_scan = bool(trade_date == current_day and trading_day and not future_scan)
    historical_scan = bool(trade_date and trade_date != current_day)
    if not scan_dt:
        warnings.append("PICKS_SCAN_TIMESTAMP_UNAVAILABLE")
    if future_scan:
        warnings.append("PICKS_SCAN_TIMESTAMP_IN_FUTURE")
    by_role: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLE_ORDER if role != "P-AI-QUANT"}
    sentinel_items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role_id = _participant_id(row)
        if role_id == "P-RISK-SENTINEL":
            sentinel_items.append(row)
        elif role_id in by_role:
            by_role[role_id].append(row)
    participants = []
    for role_id in ROLE_ORDER:
        if role_id == "P-AI-QUANT":
            continue
        items = by_role[role_id]
        if len(items) > 1:
            warnings.append(f"DUPLICATE_ROLE_RECORD:{role_id}")
        participants.append(_normalise_pick(items[0], role_id=role_id, scan_at=scan_at,
                                            current_day=current_scan, include_scan_evidence=True) if items else {
            "participant_id": role_id, "display_name": ROLE_NAMES[role_id],
            "role_type": "RETURN_BEARING_ROLE", "action": "UNAVAILABLE",
            "data_status": "EMPTY", "source_type": LEGACY_SOURCE,
            "data_admission": LEGACY_ADMISSION, "execution_authorized": False,
        })
    sentinel = None
    if sentinel_items:
        item = sentinel_items[0]
        decision = _decision_for(item)
        sentinel = {
            "participant_id": "P-RISK-SENTINEL", "display_name": "反指哨兵",
            "role_type": "RISK_OBSERVER", "status": "AVAILABLE",
            "alert_triggered": str(item.get("signal") or "").upper() == "RISK_ALERT",
            "signal": item.get("signal") if isinstance(item.get("signal"), str) else None,
            "symbol": _six_digit_code(item.get("stock_code") or decision.get("symbol")),
            "stock_name": item.get("stock_name"),
            "reason": decision.get("decision_reason") or item.get("trigger_reason"),
            "risk_evidence": decision.get("candidate_evidence") if isinstance(decision.get("candidate_evidence"), dict) else {},
            "decision_id": decision.get("decision_id"), "decision_at": decision.get("timestamp"),
            "source_type": LEGACY_SOURCE, "data_status": "UNKNOWN",
            "capital": 0, "trading_enabled": False, "ranking_enabled": False,
        }
    else:
        sentinel = {"participant_id": "P-RISK-SENTINEL", "display_name": "反指哨兵", "role_type": "RISK_OBSERVER",
                    "status": "EMPTY", "alert_triggered": False, "capital": 0,
                    "trading_enabled": False, "ranking_enabled": False}
    if historical_scan or (trade_date == current_day and not trading_day) or not current_scan:
        warnings.append("LEGACY_SCAN_NOT_CURRENT_ACTIONABLE_EVIDENCE")
    aiq = build_ai_quant_evidence(db_path, current_trade_date=current_day, is_trading_day=is_trading_day)
    aiq_participant = {
        "participant_id": "P-AI-QUANT", "display_name": ROLE_NAMES["P-AI-QUANT"],
        "role_type": "ML_QUANT", "action": "PREDICTION_EVIDENCE" if aiq.get("predictions") else "UNAVAILABLE",
        "data_status": aiq.get("status", "UNAVAILABLE"), "source_type": "MARKET_DB_READONLY_PREDICTION_PULSE",
        "pulse": aiq.get("pulse"), "execution_authorized": False,
    }
    participants.append(aiq_participant)
    if aiq["classification"] == "LATEST_HISTORICAL_PREDICTION":
        warnings.append("AI_QUANT_PREDICTION_IS_HISTORICAL")
    if aiq["status"] == "UNAVAILABLE":
        warnings.append(aiq["classification"])
    if future_scan:
        overall = "INVALID"
    elif historical_scan:
        overall = "STALE"
    elif current_scan:
        # Legacy scanner values have no retained provider observed_at and are
        # not P1C3-admitted, even when their recorded scan date is today's.
        overall = "DEGRADED"
    else:
        overall = "EMPTY" if not any(by_role.values()) else "STALE"
    role_opinions = [p["action"] for p in participants
                     if p.get("participant_id") != "P-AI-QUANT"
                     and p.get("action") not in ("UNAVAILABLE", "UNKNOWN")]
    opinion_counts = {action: role_opinions.count(action) for action in sorted(set(role_opinions))}
    gate = _read_gate_status(control_path)
    data = {
        "as_of": scan_at, "trade_date": trade_date,
        "scan_date_status": "CURRENT_TRADING_DATE" if current_scan else ("HISTORICAL" if historical_scan else "UNQUALIFIED"),
        "source": "workbench/data.json", "source_type": LEGACY_SOURCE,
        "data_admission": LEGACY_ADMISSION, "data_status": overall,
        "quote_observed_at": None, "quote_status": "UNKNOWN",
        "participants": participants,
        "opinion_counts": opinion_counts,
        "ai_quant_top5": aiq,
        "risk_sentinel": sentinel,
        "gate_b_status": gate, "g6_status": g6_status,
        "execution_authorized": False,
    }
    if gate != "CLOSED/PASS" and gate != "PASS":
        warnings.append("GATE_B_NOT_PASSED_EXECUTION_NOT_AUTHORIZED")
    return {"status": overall, "warnings": warnings,
            "data_freshness": {"trade_date": trade_date, "scan_timestamp": scan_at,
                               "observed_at": None, "freshness_status": "UNKNOWN"}, "data": data}


def _read_market_identity_and_bar(db_path: str | Path, code: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    path = Path(db_path)
    if not path.exists():
        return None, None, "MARKET_DB_UNAVAILABLE"
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        identity = None
        if "instrument_master" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(instrument_master)")}
            if {"symbol", "name"}.issubset(cols):
                select_cols = [c for c in ("symbol", "name", "full_symbol", "exchange", "board", "list_date", "delist_date", "is_st", "is_suspended", "updated_at") if c in cols]
                qcols = ",".join('"' + c + '"' for c in select_cols)
                row = conn.execute(f"SELECT {qcols} FROM instrument_master WHERE symbol=? OR full_symbol=? LIMIT 1", (code, code)).fetchone() if "full_symbol" in cols else conn.execute(f"SELECT {qcols} FROM instrument_master WHERE symbol=? LIMIT 1", (code,)).fetchone()
                if row:
                    identity = dict(zip(select_cols, row))
        bar = None
        if "daily_kline" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_kline)")}
            required = {"code", "trade_date", "close", "source"}
            if required.issubset(cols):
                select_cols = [c for c in ("code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "source", "updated_at") if c in cols]
                qcols = ",".join('"' + c + '"' for c in select_cols)
                candidates = (code, "sh" + code, "sz" + code, "bj" + code)
                placeholders = ",".join("?" for _ in candidates)
                row = conn.execute(f"SELECT {qcols} FROM daily_kline WHERE code IN ({placeholders}) ORDER BY trade_date DESC LIMIT 1", candidates).fetchone()
                if row:
                    bar = dict(zip(select_cols, row))
        return identity, bar, None
    except sqlite3.Error:
        return None, None, "MARKET_DB_READ_FAILED"
    finally:
        if conn is not None:
            try:
                conn.rollback()
            finally:
                conn.close()


def _direct_news_matches(news_items: list[dict[str, Any]], code: str, name: str | None) -> list[dict[str, Any]]:
    matches = []
    code_pattern = re.compile(rf"(?<!\d){re.escape(code)}(?!\d)")
    for item in news_items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(key) or "") for key in ("title", "summary", "content"))
        entities = item.get("entities") if isinstance(item.get("entities"), list) else []
        entity_tokens = {str(value).strip().casefold() for value in entities if isinstance(value, str)}
        exact_entity = code.casefold() in entity_tokens or bool(name and name.casefold() in entity_tokens)
        direct_text = bool(code_pattern.search(text) or (name and name in text))
        if exact_entity or direct_text:
            record = {key: item.get(key) for key in (
                "id", "title", "summary", "content", "source", "category", "themes", "url",
                "published_at", "published_at_raw", "published_at_status", "is_hot", "ai_analysis",
                "entities", "source_record") if key in item}
            raw_url = record.get("url")
            try:
                parsed = urlsplit(raw_url) if isinstance(raw_url, str) else None
                if not parsed or parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    record["url"] = None
            except ValueError:
                record["url"] = None
            matches.append(record)
    return matches


def build_stock_view(symbol: str, picks_path: str | Path = DEFAULT_PICKS_PATH,
                     db_path: str | Path = DEFAULT_MARKET_DB, *,
                     news_items: list[dict[str, Any]] | None = None,
                     current_trade_date: str | date | None = None,
                     is_trading_day: Callable[[date], bool] | None = None,
                     gate_b_status: str | None = None,
                     g6_status: str = "OPEN") -> dict[str, Any]:
    code = _six_digit_code(symbol)
    if not code or not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol.strip()):
        return {"status": "INVALID", "warnings": ["SYMBOL_MUST_BE_SIX_DIGIT_A_SHARE_CODE"], "data": None}
    pick_result = build_picks_view(picks_path, db_path, current_trade_date=current_trade_date,
                                   is_trading_day=is_trading_day, gate_b_status=gate_b_status, g6_status=g6_status)
    pick_data = pick_result["data"]
    all_rows, _ = _load_json(picks_path)
    rows = all_rows.get("blogger_picks", []) if isinstance(all_rows, dict) else []
    matched: list[tuple[str, dict[str, Any]]] = []
    sentinel_source = None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or _six_digit_code(row.get("stock_code")) != code:
            continue
        role = _participant_id(row)
        if role == "P-RISK-SENTINEL":
            sentinel_source = row
        elif role in ROLE_NAMES:
            matched.append((role, row))
    identity, bar, db_error = _read_market_identity_and_bar(db_path, code)
    selected_name = next((row.get("stock_name") for _, row in matched if isinstance(row.get("stock_name"), str)), None)
    if not selected_name and isinstance(identity, dict):
        selected_name = identity.get("name")
    source_kind = LEGACY_SOURCE if matched else ("LOCAL_SQLITE_RESEARCH_BAR" if bar else "UNAVAILABLE")
    evidence: dict[str, Any]
    if matched:
        evidence = _decision_for(matched[0][1]).get("candidate_evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        raw_price = matched[0][1].get("current_price")
        if "price" not in evidence and isinstance(raw_price, (int, float)):
            evidence = {**evidence, "price": raw_price}
        evidence_status = "UNKNOWN"
        evidence_source_note = "LEGACY_FIELD_PROVENANCE; source observed_at/units are not retained or verified"
        market_time = None
        market_date = pick_data.get("trade_date")
    elif bar:
        evidence = {key: bar.get(key) for key in ("open", "high", "low", "close", "volume", "amount", "pct_chg") if key in bar}
        evidence_status = "STALE"
        evidence_source_note = "Historical daily_kline only; not a live quote. Amount/volume units are not asserted here."
        market_time = None
        market_date = bar.get("trade_date")
    else:
        evidence, evidence_status, evidence_source_note = {}, "UNAVAILABLE", db_error or "NO_LOCAL_QUOTE_EVIDENCE"
        market_time = None
        market_date = None
    current_day = (current_trade_date.strftime("%Y%m%d") if isinstance(current_trade_date, date)
                   else str(current_trade_date).replace("-", "") if isinstance(current_trade_date, str)
                   else datetime.now(SHANGHAI).strftime("%Y%m%d"))
    aiq = build_ai_quant_evidence(db_path, current_trade_date=current_day, is_trading_day=is_trading_day)
    current_aiq = next((row for row in aiq.get("predictions", []) if row.get("symbol") == code), None) if aiq.get("classification") == "CURRENT_DAY_TOP5" else None
    persona_views = []
    for role_id in ("P-RULE-QUANT", "P-HANSHU", "P-YINWENTAO", "P-LIAO-XIAOAI", "P-COOL"):
        source_row = next((row for role, row in matched if role == role_id), None)
        if source_row is None:
            persona_views.append({"participant_id": role_id, "display_name": ROLE_NAMES[role_id], "status": "NO_CURRENT_PERSONA_VIEW"})
            continue
        projection = _normalise_pick(source_row, role_id=role_id, scan_at=pick_data.get("as_of"),
                                     current_day=pick_data.get("scan_date_status") == "CURRENT_TRADING_DATE")
        decision = _decision_for(source_row)
        persona_views.append({**projection,
            "skill_rules_triggered": decision.get("skill_rules_triggered") if isinstance(decision.get("skill_rules_triggered"), list) else [],
            "skill_rules_failed": decision.get("skill_rules_failed") if isinstance(decision.get("skill_rules_failed"), list) else [],
            "evidence_refs": decision.get("evidence_refs") if isinstance(decision.get("evidence_refs"), list) else [],
        })
    sentinel_decision = _decision_for(sentinel_source) if sentinel_source else {}
    sentinel = {
        "participant_id": "P-RISK-SENTINEL", "display_name": "反指哨兵", "role_type": "RISK_OBSERVER",
        "status": "AVAILABLE" if sentinel_source else "NO_MATCHING_RISK_ALERT",
        "alert_triggered": bool(sentinel_source and str(sentinel_source.get("signal") or "").upper() == "RISK_ALERT"),
        "signal": sentinel_source.get("signal") if sentinel_source else None,
        "reason": (sentinel_decision.get("decision_reason") or sentinel_source.get("trigger_reason")) if sentinel_source else None,
        "risk_evidence": sentinel_decision.get("candidate_evidence") if isinstance(sentinel_decision.get("candidate_evidence"), dict) else {},
        "capital": 0, "trading_enabled": False, "ranking_enabled": False,
    }
    related_news = _direct_news_matches(news_items or [], code, selected_name)
    news_status = "AVAILABLE" if related_news else "EMPTY"
    found_evidence = bool(matched or bar or identity or related_news or current_aiq)
    overall = "DEGRADED" if matched else ("STALE" if bar else ("NORMAL" if found_evidence else "EMPTY"))
    warnings = []
    if matched:
        warnings.append("LEGACY_DATA_NOT_P1C3_ADMITTED; no provider observed_at or verified unit provenance")
    if not related_news:
        warnings.append("NO_DIRECT_NEWS_MATCH")
    if not current_aiq:
        warnings.append("NO_CURRENT_AI_QUANT_EVIDENCE")
    data = {
        "identity": {"symbol": code, "name": selected_name, "exchange": identity.get("exchange") if identity else None,
                     "board": identity.get("board") if identity else None,
                     "source": "persisted pick / instrument_master" if (matched or identity) else None,
                     "status": "AVAILABLE" if (selected_name or identity) else "PARTIAL",
                     "date_effective_qualification": "UNVERIFIED"},
        "quote_evidence": {"status": evidence_status, "source_type": source_kind,
                           "source_admission": LEGACY_ADMISSION if matched else "RESEARCH_ONLY",
                           "value_fields": evidence, "observed_at": None, "trade_date": market_date,
                           "source_note": evidence_source_note, "field_units": "UNVERIFIED_FOR_LEGACY_FIELDS" if matched else "NOT_ASSERTED",
                           "execution_eligible": False},
        "persona_views": persona_views,
        "ai_quant": {"status": "AVAILABLE" if current_aiq else "NO_CURRENT_AI_QUANT_EVIDENCE",
                      "classification": "CURRENT_DAY_TOP5" if current_aiq else aiq.get("classification"),
                      "prediction": current_aiq},
        "risk_sentinel": sentinel,
        "news": {"status": news_status, "reason_code": None if related_news else "NO_DIRECT_NEWS_MATCH", "items": related_news},
        "data_health": {"status": overall, "warnings": warnings, "gate_b_status": pick_data.get("gate_b_status"),
                        "g6_status": g6_status, "execution_authorized": False},
        "execution_authorized": False,
    }
    return {"status": overall, "warnings": warnings, "data": data}
