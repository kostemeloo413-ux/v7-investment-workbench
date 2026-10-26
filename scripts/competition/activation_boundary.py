# -*- coding: utf-8 -*-
"""Fail-closed final BUY boundary for a governed paper season.

This guard is deliberately limited to NEW ENTRY / BUY mutations.  It requires
the original decision timestamp (with an explicit timezone), binds account and
ledger identity to the single active season, compares the decision to that
season's governed activation time, and requires the current A-share market
session to be open.  A rejected BUY creates no order, fill, or position.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class BoundaryResult:
    allowed: bool
    reason: str
    decision_at: str | None = None
    activation_at: str | None = None
    checked_at: str | None = None
    season_id: str | None = None


def parse_explicit_timestamp(value: Any) -> datetime.datetime | None:
    """Parse only unambiguous, timezone-aware timestamps; never assume local."""
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI)


def decision_timestamp_from(*records: Any) -> Any:
    """Read an original decision timestamp; do not derive one from IDs or now."""
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("decision_timestamp", "timestamp"):
            value = record.get(key)
            if value is not None:
                return value
    return None


def _account_value(account: Any, key: str) -> Any:
    if isinstance(account, dict):
        return account.get(key)
    return getattr(account, key, None)


def evaluate_buy_boundary(
    decision_timestamp: Any,
    *,
    context: dict[str, Any] | None = None,
    account: Any = None,
    decision_season_ids: tuple[str, ...] | list[str] | None = None,
    ledger_root: str | os.PathLike[str] | None = None,
    safety_path: str | os.PathLike[str] | None = None,
    now: datetime.datetime | None = None,
    calendar_status_fn=None,
    market_phase_fn=None,
) -> BoundaryResult:
    """Return one deterministic BUY gate result without writing state.

    `context`, `now`, and the two function hooks exist for isolated tests.  Live
    callers omit them so the active identity and exchange session are read from
    the formal runtime contracts on every attempt.
    """
    decision = parse_explicit_timestamp(decision_timestamp)
    if decision is None:
        return BoundaryResult(False, "INVALID_DECISION_TIMESTAMP")

    try:
        if context is None:
            from competition.season_runtime import resolve_active_season_context
            context = resolve_active_season_context()
        if not isinstance(context, dict):
            return BoundaryResult(False, "ACTIVE_SEASON_CONTEXT_INVALID")
        season_id = context.get("season_id")
        identity = context.get("active_identity")
        if not isinstance(identity, dict):
            from competition.season_runtime import load_active_season_identity
            identity = load_active_season_identity()
        activation = parse_explicit_timestamp(identity.get("activated_at") if isinstance(identity, dict) else None)
        if activation is None:
            return BoundaryResult(False, "ACTIVE_SEASON_ACTIVATION_TIME_INVALID", decision_at=decision.isoformat(), season_id=season_id)

        checked = now if now is not None else datetime.datetime.now(SHANGHAI)
        if not isinstance(checked, datetime.datetime) or checked.tzinfo is None or checked.utcoffset() is None:
            return BoundaryResult(False, "INVALID_DECISION_TIMESTAMP", decision.isoformat(), activation.isoformat(), season_id=season_id)
        checked = checked.astimezone(SHANGHAI)
        if activation > checked or decision > checked:
            return BoundaryResult(False, "INVALID_DECISION_TIMESTAMP", decision.isoformat(), activation.isoformat(), checked.isoformat(), season_id)

        if account is not None:
            account_season = _account_value(account, "season_id")
            safety_season = _account_value(account, "_safety_season_id")
            if account_season != season_id:
                return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)
            if safety_season is not None and safety_season != season_id:
                return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)
            account_root = _account_value(account, "_safety_root")
            if account_root is not None and os.path.abspath(str(account_root)) != os.path.abspath(str(context.get("season_root", ""))):
                return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)

        if decision_season_ids is not None:
            if not isinstance(decision_season_ids, (tuple, list)):
                return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)
            if any(value is not None and value != season_id for value in decision_season_ids):
                return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)

        if ledger_root is not None:
            root = Path(ledger_root).resolve()
            account_id = _account_value(account, "participant_id") if account is not None else None
            aiq_root = Path(context.get("ai_quant_paper_root", "")).resolve()
            if root == aiq_root:
                pass
            elif account_id:
                expected = (Path(context.get("competition_ledger_root", "")) / str(account_id)).resolve()
                if root != expected:
                    return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)
            else:
                return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)
        if safety_path is not None and os.path.abspath(str(safety_path)) != os.path.abspath(str(context.get("governance_safety_path", ""))):
            return BoundaryResult(False, "MIXED_SEASON_STATE", decision.isoformat(), activation.isoformat(), season_id=season_id)

        if decision < activation:
            return BoundaryResult(False, "STALE_PRE_ACTIVATION_DECISION", decision.isoformat(), activation.isoformat(), season_id=season_id)

        if calendar_status_fn is None or market_phase_fn is None:
            import runtime_config as rc
            calendar_status_fn = calendar_status_fn or rc.trading_calendar_status
            market_phase_fn = market_phase_fn or rc.market_phase
        calendar = calendar_status_fn(checked.date())
        if not isinstance(calendar, dict) or calendar.get("status") != "VALID" or calendar.get("trading_day") is not True:
            return BoundaryResult(False, "MARKET_SESSION_BLOCK", decision.isoformat(), activation.isoformat(), checked.isoformat(), season_id)
        if market_phase_fn(checked) != "open":
            return BoundaryResult(False, "MARKET_SESSION_BLOCK", decision.isoformat(), activation.isoformat(), checked.isoformat(), season_id)
        return BoundaryResult(True, "PASS", decision.isoformat(), activation.isoformat(), checked.isoformat(), season_id)
    except Exception:
        return BoundaryResult(False, "ACTIVE_SEASON_CONTEXT_INVALID")


def check_new_entry_boundary(decision_timestamp: Any, **kwargs) -> BoundaryResult:
    """Production wrapper: exceptions and malformed state always block BUY."""
    try:
        result = evaluate_buy_boundary(decision_timestamp, **kwargs)
        if not result.allowed:
            return result
        checked = parse_explicit_timestamp(result.checked_at)
        decision = parse_explicit_timestamp(result.decision_at)
        if decision.date() != checked.date():
            return BoundaryResult(False, 'STALE_PRIOR_TRADING_DATE_DECISION', result.decision_at, result.activation_at, result.checked_at, result.season_id)
        from competition.season_runtime import resolve_active_season_context
        from competition.data_admission import latest_completed_data_admission
        context = kwargs.get('context') or resolve_active_season_context()
        admitted, reason = latest_completed_data_admission(context, checked)
        if not admitted:
            return BoundaryResult(False, reason, result.decision_at, result.activation_at, result.checked_at, result.season_id)
        return result
    except Exception:
        return BoundaryResult(False, "ACTIVE_SEASON_CONTEXT_INVALID")
