"""Minimal in-memory trust boundaries for isolated V7 M1 orchestration.

These contracts are review-only. They neither authenticate production providers
cryptographically nor persist plans/admission state. Production adapters remain
unwired until separately reviewed and authorized.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import copy
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Optional, Sequence


SYNTHETIC_FIXTURE_CLASS = "SYNTHETIC_TRUSTED_CONTRACT_FIXTURE_ONLY"
VALIDATION_CONTRACT_VERSION = "v7-trusted-validation-result-v1"
UNIVERSE_VALIDATOR_ID = "V7_DATE_EFFECTIVE_UNIVERSE_VALIDATOR"
GOLDEN_VALIDATOR_ID = "V7_AIQ_GOLDEN_FIXTURE_VALIDATOR"
AIQ_UNIVERSE_VALIDATOR_ID = "V7_AIQ_DATED_UNIVERSE_VALIDATOR"
PRICE_BASIS_VALIDATOR_ID = "V7_AIQ_PRICE_BASIS_VALIDATOR"
PLAN_SERVICE_ID = "V7_SYSTEM_PLAN_SERVICE"
PLAN_ISSUER_ID = "V7_SYSTEM_PLAN_ISSUER:M1-ISOLATED"
PLAN_SCHEMA_VERSION = "v7-system-plan-evidence-v2"
PLAN_CONTRACT_VERSION = "v7-system-plan-registry-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp.tzinfo is not None and stamp.utcoffset() is not None


@dataclass(frozen=True)
class TrustedValidationResult:
    validator_id: str
    contract_version: str
    status: str
    subject_identity: Mapping[str, Any]
    evidence_refs: tuple[str, ...]
    evidence_hash: str
    verified_at: str
    failure_reasons: tuple[str, ...] = ()
    evidence_class: str = "REAL_SOURCE_EVIDENCE"

    def payload(self) -> dict[str, Any]:
        return {
            "validator_id": self.validator_id,
            "contract_version": self.contract_version,
            "status": self.status,
            "subject_identity": dict(self.subject_identity),
            "evidence_refs": list(self.evidence_refs),
            "verified_at": self.verified_at,
            "failure_reasons": list(self.failure_reasons),
            "evidence_class": self.evidence_class,
        }


@dataclass(frozen=True)
class GoldenValidationResult(TrustedValidationResult):
    pass


@dataclass(frozen=True)
class UniverseValidationResult(TrustedValidationResult):
    pass


@dataclass(frozen=True)
class PriceBasisValidationResult(TrustedValidationResult):
    pass


def make_validation_result(
    result_type: type[TrustedValidationResult], *, validator_id: str,
    status: str, subject_identity: Mapping[str, Any], evidence_refs: Sequence[str],
    verified_at: str, failure_reasons: Sequence[str] = (),
    evidence_class: str = "REAL_SOURCE_EVIDENCE",
) -> TrustedValidationResult:
    """Build a content-bound result; callers still need an injected trusted validator."""
    provisional = result_type(
        validator_id=validator_id,
        contract_version=VALIDATION_CONTRACT_VERSION,
        status=status,
        subject_identity=dict(subject_identity),
        evidence_refs=tuple(str(ref) for ref in evidence_refs),
        evidence_hash="",
        verified_at=verified_at,
        failure_reasons=tuple(str(reason) for reason in failure_reasons),
        evidence_class=evidence_class,
    )
    return result_type(**{**asdict(provisional), "evidence_hash": sha256_json(provisional.payload())})


def verify_validation_result(
    result: Any, *, result_type: type[TrustedValidationResult],
    validator_id: str, expected_subject: Mapping[str, Any],
    allow_synthetic: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if type(result) is not result_type:
        return False, ["TRUSTED_VALIDATOR_RESULT_TYPE_INVALID"]
    if result.validator_id != validator_id:
        reasons.append("TRUSTED_VALIDATOR_ID_MISMATCH")
    if result.contract_version != VALIDATION_CONTRACT_VERSION:
        reasons.append("TRUSTED_VALIDATOR_CONTRACT_VERSION_MISMATCH")
    if result.status != "VERIFIED":
        reasons.append("TRUSTED_VALIDATOR_STATUS_NOT_VERIFIED")
    if dict(result.subject_identity) != dict(expected_subject):
        reasons.append("TRUSTED_VALIDATOR_SUBJECT_MISMATCH")
    if not result.evidence_refs or any(not isinstance(ref, str) or not ref.strip()
                                       for ref in result.evidence_refs):
        reasons.append("TRUSTED_VALIDATOR_EVIDENCE_REFS_MISSING")
    if not _aware_timestamp(result.verified_at):
        reasons.append("TRUSTED_VALIDATOR_VERIFIED_AT_INVALID")
    if result.failure_reasons:
        reasons.extend(result.failure_reasons)
    if result.evidence_class == SYNTHETIC_FIXTURE_CLASS:
        if not allow_synthetic:
            reasons.append("SYNTHETIC_VALIDATION_RESULT_NOT_REAL_ADMISSION")
    elif result.evidence_class != "REAL_SOURCE_EVIDENCE":
        reasons.append("TRUSTED_VALIDATOR_EVIDENCE_CLASS_UNKNOWN")
    try:
        expected_hash = sha256_json(result.payload())
    except (TypeError, ValueError):
        expected_hash = None
    if not result.evidence_hash or result.evidence_hash != expected_hash:
        reasons.append("TRUSTED_VALIDATOR_EVIDENCE_HASH_INVALID")
    return not reasons, list(dict.fromkeys(reasons))


class DateEffectiveUniverseValidator:
    """Validates one supplied dated snapshot and each candidate's membership.

    Real-source snapshots require `source_verifier`; synthetic fixtures are
    explicitly tagged and are useful only for isolated contract tests.
    """

    validator_id = UNIVERSE_VALIDATOR_ID
    contract_version = VALIDATION_CONTRACT_VERSION

    def __init__(self, source_verifier: Optional[Callable[[Mapping[str, Any]], bool]] = None):
        self._source_verifier = source_verifier
        self._snapshots: dict[str, Mapping[str, Any]] = {}

    def validate_snapshot(self, snapshot: Mapping[str, Any], trade_date: str) -> UniverseValidationResult:
        reasons: list[str] = []
        if not isinstance(snapshot, Mapping):
            snapshot = {}
            reasons.append("UNIVERSE_SNAPSHOT_MISSING")
        members = snapshot.get("members")
        date_value = snapshot.get("trade_date")
        if date_value != trade_date:
            reasons.append("UNIVERSE_SNAPSHOT_TRADE_DATE_MISMATCH")
        if not all(isinstance(snapshot.get(key), str) and snapshot.get(key).strip()
                   for key in ("scope", "version", "source_identity", "evidence_id")):
            reasons.append("UNIVERSE_SNAPSHOT_IDENTITY_MISSING")
        if not _aware_timestamp(snapshot.get("effective_at")):
            reasons.append("UNIVERSE_SNAPSHOT_EFFECTIVE_AT_INVALID")
        if not isinstance(members, list):
            members = []
            reasons.append("UNIVERSE_MEMBERS_INVALID")
        try:
            normalized_members = sorted((dict(member) for member in members),
                                         key=lambda item: (str(item.get("canonical_symbol", "")),
                                                           str(item.get("exchange", ""))))
            actual_hash = sha256_json(normalized_members)
        except (TypeError, ValueError):
            normalized_members = []
            actual_hash = ""
            reasons.append("UNIVERSE_MEMBERS_NOT_CANONICALIZABLE")
        if snapshot.get("members_count") != len(members):
            reasons.append("UNIVERSE_MEMBERS_COUNT_MISMATCH")
        if snapshot.get("members_hash") != actual_hash:
            reasons.append("UNIVERSE_MEMBERS_HASH_MISMATCH")
        seen: set[str] = set()
        for member in normalized_members:
            symbol = _identity(member.get("canonical_symbol"))
            exchange = _identity(member.get("exchange"))
            key = f"{symbol}|{exchange}" if symbol and exchange else ""
            if not key:
                reasons.append("UNIVERSE_MEMBER_SECURITY_IDENTITY_MISSING")
            elif key in seen:
                reasons.append("UNIVERSE_MEMBER_DUPLICATE_SECURITY_IDENTITY")
            seen.add(key)
            required = ("board", "listing_status", "st_status", "suspension_status",
                        "list_date", "delist_date")
            if any(name not in member for name in required):
                reasons.append("UNIVERSE_MEMBER_EFFECTIVE_STATUS_FIELDS_MISSING")
        evidence_class = snapshot.get("evidence_class", "REAL_SOURCE_EVIDENCE")
        if evidence_class == SYNTHETIC_FIXTURE_CLASS:
            pass
        elif not callable(self._source_verifier) or self._source_verifier(snapshot) is not True:
            reasons.append("UNIVERSE_SOURCE_PROVENANCE_UNVERIFIED")
        subject = {
            "trade_date": trade_date,
            "scope": snapshot.get("scope"),
            "version": snapshot.get("version"),
            "members_hash": snapshot.get("members_hash"),
            "evidence_id": snapshot.get("evidence_id"),
        }
        status = "VERIFIED" if not reasons else "HOLD"
        result = make_validation_result(
            UniverseValidationResult, validator_id=self.validator_id, status=status,
            subject_identity=subject,
            evidence_refs=[str(snapshot.get("evidence_id"))] if snapshot.get("evidence_id") else (),
            verified_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            failure_reasons=reasons, evidence_class=str(evidence_class),
        )
        snapshot_key = f"{trade_date}|{snapshot.get('evidence_id')}"
        self._snapshots[snapshot_key] = copy.deepcopy(dict(snapshot))
        self._active_snapshot_key = snapshot_key
        self._active_snapshot_result = result
        return result

    def validate_candidate(self, canonical_symbol: str, exchange: str,
                           trade_date: str) -> UniverseValidationResult:
        snapshot = getattr(self, "_active_snapshot_result", None)
        raw_snapshot = self._snapshots.get(getattr(self, "_active_snapshot_key", ""), {})
        reasons: list[str] = []
        refreshed = self.validate_snapshot(raw_snapshot, trade_date) if raw_snapshot else None
        if (not isinstance(snapshot, UniverseValidationResult) or snapshot.status != "VERIFIED"
                or not isinstance(refreshed, UniverseValidationResult)
                or refreshed.status != "VERIFIED"):
            reasons.append("DATE_EFFECTIVE_UNIVERSE_SNAPSHOT_NOT_VERIFIED")
        if raw_snapshot.get("trade_date") != trade_date:
            reasons.append("UNIVERSE_CANDIDATE_TRADE_DATE_MISMATCH")
        symbol = _identity(canonical_symbol)
        market = _identity(exchange)
        if not symbol or not market:
            reasons.append("UNIVERSE_CANDIDATE_IDENTITY_MISSING")
        matches = [member for member in raw_snapshot.get("members", [])
                   if isinstance(member, Mapping)
                   and _identity(member.get("canonical_symbol")) == symbol
                   and _identity(member.get("exchange")) == market]
        if len(matches) != 1:
            reasons.append("UNIVERSE_CANDIDATE_MEMBERSHIP_NOT_UNIQUE")
        else:
            member = matches[0]
            if member.get("listing_status") not in {"LISTED", "ACTIVE"}:
                reasons.append("UNIVERSE_CANDIDATE_LISTING_STATUS_NOT_ELIGIBLE")
            if member.get("st_status") != "NOT_ST":
                reasons.append("UNIVERSE_CANDIDATE_ST_STATUS_NOT_PROVEN_CLEAR")
            if member.get("suspension_status") != "NOT_SUSPENDED":
                reasons.append("UNIVERSE_CANDIDATE_SUSPENSION_STATUS_NOT_PROVEN_CLEAR")
            board = member.get("board")
            if not isinstance(board, str) or not board.strip():
                reasons.append("UNIVERSE_CANDIDATE_BOARD_MISSING")
            elif board.strip().upper() not in {"MAINBOARD", "MAIN_BOARD", "MAIN", "主板"}:
                reasons.append("UNIVERSE_CANDIDATE_BOARD_NOT_MAINBOARD")
            try:
                list_date = dt.date.fromisoformat(str(member.get("list_date")))
                if list_date.isoformat() != str(member.get("list_date")) or list_date > dt.date.fromisoformat(trade_date):
                    reasons.append("UNIVERSE_CANDIDATE_LIST_DATE_INVALID")
            except ValueError:
                reasons.append("UNIVERSE_CANDIDATE_LIST_DATE_UNKNOWN")
            delist = member.get("delist_date")
            if delist not in (None, ""):
                try:
                    delist_date = dt.date.fromisoformat(str(delist))
                    if delist_date.isoformat() != str(delist) or delist_date <= dt.date.fromisoformat(trade_date):
                        reasons.append("UNIVERSE_CANDIDATE_DELISTED_BY_TRADE_DATE")
                except ValueError:
                    reasons.append("UNIVERSE_CANDIDATE_DELIST_DATE_INVALID")
        source_id = raw_snapshot.get("evidence_id")
        subject = {
            "canonical_symbol": symbol,
            "exchange": market,
            "trade_date": trade_date,
            "snapshot_evidence_id": source_id,
            "snapshot_members_hash": raw_snapshot.get("members_hash"),
        }
        evidence_class = raw_snapshot.get("evidence_class", "REAL_SOURCE_EVIDENCE")
        return make_validation_result(
            UniverseValidationResult, validator_id=self.validator_id,
            status="VERIFIED" if not reasons else "HOLD",
            subject_identity=subject,
            evidence_refs=[str(source_id)] if source_id else (),
            verified_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            failure_reasons=reasons, evidence_class=str(evidence_class),
        )


class FixtureAiqValidationProvider:
    """Synthetic-only validator dependency for negative/contract tests."""

    evidence_class = SYNTHETIC_FIXTURE_CLASS

    def __init__(self, statuses: Optional[Mapping[str, str]] = None):
        self.statuses = dict(statuses or {})

    def _result(self, key: str, result_type: type[TrustedValidationResult],
                validator_id: str, subject: Mapping[str, Any]) -> TrustedValidationResult:
        status = self.statuses.get(key, "VERIFIED")
        return make_validation_result(
            result_type, validator_id=validator_id, status=status,
            subject_identity=subject, evidence_refs=[f"synthetic:{key}:evidence"],
            verified_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            failure_reasons=() if status == "VERIFIED" else (f"FIXTURE_{key.upper()}_HOLD",),
            evidence_class=self.evidence_class,
        )

    def validate_golden_fixture(self, subject: Mapping[str, Any]) -> GoldenValidationResult:
        return self._result("golden", GoldenValidationResult, GOLDEN_VALIDATOR_ID, subject)

    def validate_dated_universe(self, subject: Mapping[str, Any]) -> UniverseValidationResult:
        return self._result("universe", UniverseValidationResult, AIQ_UNIVERSE_VALIDATOR_ID, subject)

    def validate_price_basis(self, subject: Mapping[str, Any]) -> PriceBasisValidationResult:
        return self._result("price_basis", PriceBasisValidationResult, PRICE_BASIS_VALIDATOR_ID, subject)


@dataclass(frozen=True)
class PlanVerificationResult:
    validator_id: str
    contract_version: str
    status: str
    subject_identity: Mapping[str, Any]
    evidence_refs: tuple[str, ...]
    evidence_hash: str
    verified_at: str
    failure_reasons: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "validator_id": self.validator_id,
            "contract_version": self.contract_version,
            "status": self.status,
            "subject_identity": dict(self.subject_identity),
            "evidence_refs": list(self.evidence_refs),
            "verified_at": self.verified_at,
            "failure_reasons": list(self.failure_reasons),
        }


class SystemPlanIssuer:
    """Per-run system plan issuer with an in-memory issue/verify registry."""

    issuer_id = PLAN_ISSUER_ID
    service_id = PLAN_SERVICE_ID
    contract_version = PLAN_CONTRACT_VERSION

    def __init__(self) -> None:
        self._registry: dict[str, dict[str, Any]] = {}

    def issue_plan(self, binding: Mapping[str, Any], calendar_resolution: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(binding, Mapping) or not isinstance(calendar_resolution, Mapping):
            raise ValueError("SYSTEM_PLAN_ISSUE_INPUT_INVALID")
        required = (
            "season_id", "forward_epoch_id", "participant_id", "account_id",
            "canonical_symbol", "exchange", "decision_trade_date", "planned_trade_date",
            "source_evidence_id",
        )
        payload = {key: binding.get(key) for key in required}
        if any(not isinstance(value, str) or not value.strip() for value in payload.values()):
            raise ValueError("SYSTEM_PLAN_BINDING_FIELD_MISSING")
        if (calendar_resolution.get("validation_status") != "VALID"
                or calendar_resolution.get("confirmation_date") != payload["decision_trade_date"]
                or calendar_resolution.get("next_trade_date") != payload["planned_trade_date"]
                or not isinstance(calendar_resolution.get("calendar_evidence_id"), str)
                or not calendar_resolution.get("calendar_evidence_id")):
            raise ValueError("SYSTEM_PLAN_CALENDAR_NOT_VERIFIED")
        payload.update({
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_service_id": self.service_id,
            "issuer_identity": self.issuer_id,
            "evidence_class": binding.get("evidence_class", "REAL_SOURCE_EVIDENCE"),
            "confirmation_date": payload["decision_trade_date"],
            "planned_next_trade_date": payload["planned_trade_date"],
            "calendar_version": calendar_resolution.get("calendar_version"),
            "calendar_source": calendar_resolution.get("calendar_source"),
            "calendar_evidence_id": calendar_resolution.get("calendar_evidence_id"),
        })
        if not all(isinstance(payload.get(key), str) and payload.get(key).strip()
                   for key in ("calendar_version", "calendar_source", "calendar_evidence_id")):
            raise ValueError("SYSTEM_PLAN_CALENDAR_IDENTITY_MISSING")
        digest = sha256_json(payload)
        plan = {**payload, "plan_identity": canonical_json(payload),
                "plan_evidence_id": f"v7-system-plan-sha256:{digest}"}
        self._registry[plan["plan_evidence_id"]] = dict(plan)
        return dict(plan)

    def verify_plan(self, plan: Mapping[str, Any],
                    expected_binding: Optional[Mapping[str, Any]] = None) -> PlanVerificationResult:
        reasons: list[str] = []
        if not isinstance(plan, Mapping):
            plan = {}
            reasons.append("SYSTEM_PLAN_EVIDENCE_MISSING")
        plan_id = plan.get("plan_evidence_id")
        registered = self._registry.get(str(plan_id)) if isinstance(plan_id, str) else None
        if not registered or dict(plan) != registered:
            reasons.append("SYSTEM_PLAN_NOT_PRESENT_IN_ISSUER_REGISTRY")
        if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
            reasons.append("SYSTEM_PLAN_SCHEMA_VERSION_INVALID")
        if plan.get("plan_service_id") != self.service_id or plan.get("issuer_identity") != self.issuer_id:
            reasons.append("SYSTEM_PLAN_ISSUER_IDENTITY_INVALID")
        body = {key: value for key, value in plan.items()
                if key not in {"plan_identity", "plan_evidence_id"}}
        try:
            expected_id = f"v7-system-plan-sha256:{sha256_json(body)}"
        except (TypeError, ValueError):
            expected_id = ""
        if plan.get("plan_evidence_id") != expected_id:
            reasons.append("SYSTEM_PLAN_EVIDENCE_HASH_MISMATCH")
        if expected_binding is not None:
            for key, value in expected_binding.items():
                if plan.get(key) != value:
                    reasons.append(f"SYSTEM_PLAN_BINDING_MISMATCH:{key}")
        subject = {
            "plan_evidence_id": plan.get("plan_evidence_id"),
            "season_id": plan.get("season_id"),
            "forward_epoch_id": plan.get("forward_epoch_id"),
            "participant_id": plan.get("participant_id"),
            "account_id": plan.get("account_id"),
            "canonical_symbol": plan.get("canonical_symbol"),
            "exchange": plan.get("exchange"),
            "decision_trade_date": plan.get("decision_trade_date"),
            "planned_trade_date": plan.get("planned_trade_date"),
        }
        refs = (str(plan_id),) if isinstance(plan_id, str) and plan_id else ()
        verified_at = dt.datetime.now(dt.timezone.utc).isoformat()
        status = "VERIFIED" if not reasons else "HOLD"
        provisional = PlanVerificationResult(
            validator_id=self.issuer_id, contract_version=self.contract_version,
            status=status, subject_identity=subject, evidence_refs=refs,
            evidence_hash="", verified_at=verified_at, failure_reasons=tuple(reasons),
        )
        return PlanVerificationResult(**{**asdict(provisional),
                                         "evidence_hash": sha256_json(provisional.payload())})


def verify_plan_verification_result(result: Any, plan: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if type(result) is not PlanVerificationResult:
        return False, ["SYSTEM_PLAN_VERIFIER_RESULT_INVALID"]
    if result.validator_id != PLAN_ISSUER_ID:
        reasons.append("SYSTEM_PLAN_VERIFIER_ID_MISMATCH")
    if result.contract_version != PLAN_CONTRACT_VERSION:
        reasons.append("SYSTEM_PLAN_VERIFIER_CONTRACT_MISMATCH")
    if result.status != "VERIFIED":
        reasons.append("SYSTEM_PLAN_ISSUER_UNVERIFIED")
    if result.subject_identity.get("plan_evidence_id") != plan.get("plan_evidence_id"):
        reasons.append("SYSTEM_PLAN_VERIFIER_SUBJECT_MISMATCH")
    if not result.evidence_refs or not _aware_timestamp(result.verified_at):
        reasons.append("SYSTEM_PLAN_VERIFIER_EVIDENCE_MISSING")
    if result.failure_reasons:
        reasons.extend(result.failure_reasons)
    if result.evidence_hash != sha256_json(result.payload()):
        reasons.append("SYSTEM_PLAN_VERIFIER_RESULT_HASH_INVALID")
    return not reasons, list(dict.fromkeys(reasons))


def _identity(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper()
