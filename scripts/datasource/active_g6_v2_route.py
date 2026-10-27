"""Active G6 universe routing through the approved Date-Effective V2 builder.

This module is orchestration only. It leaves the frozen V2 and G6 contracts
unchanged, captures target-date BaoStock evidence with a hard protocol-call
cap, and passes only a READY_CANDIDATE V2 expected set to the existing G6
provider/completeness route.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


V2_SOURCE_SHA256 = "87c1337b269b796348a105c9da381cf1b861b9297cd025d831cb7bd4c1ee1453"
G6_SOURCE_SHA256 = "7912c7a69bf9b187874b48be2c8d631b2b4db8c3720a080ee18750d0aa31ab97"
BOARD_OVERRIDE_RECORD_SHA256 = "c8afa85f395a4c13f8ecef41cc9fd0842da8dbf6b38029ab7881a6c28754639c"
MAX_BAOSTOCK_PROTOCOL_CALLS = 100
SHANGHAI = ZoneInfo("Asia/Shanghai")
APPROVED_BOARD_OVERRIDE_302132 = {
    "canonical_symbol": "302132.SZ",
    "board": "SZ_NON_MAINBOARD",
    "board_label": "SZ_CHINEXT",
    "effective_from": "20250217",
    "evidence_id": "SZSE_CODE_CHANGE_300114_TO_302132_AND_CHINEXT_RULES",
    "official_sources": [
        "https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-02-15/cedb693a-f5ee-4463-9682-ea33d406b569.PDF",
        "https://www.szse.cn/disclosure/notice/t20181210_563449.html",
    ],
    "basis": (
        "SZSE-hosted issuer notice identifies the 300114->302132 code change, "
        "effective 2025-02-17, and cites ChiNext rules; an SZSE notice separately "
        "lists former code 300114 in ChiNext index membership."
    ),
}


class ActiveG6RouteHold(RuntimeError):
    """Fail-closed result for incomplete source or invalid V2 evidence."""

    def __init__(self, reason: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details or {})


def _now() -> dt.datetime:
    return dt.datetime.now(SHANGHAI)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(payload))


def _consume(result: Any) -> tuple[list[str], list[dict[str, Any]], str, str]:
    fields = list(getattr(result, "fields", []) or [])
    result_code = str(getattr(result, "error_code", "UNKNOWN"))
    rows: list[dict[str, Any]] = []
    if result_code == "0":
        while result.next():
            rows.append(dict(zip(fields, result.get_row_data())))
    # A later SDK page can fail after a successful first page. Preserve its
    # final error instead of promoting the partial roster to SUCCESS.
    return fields, rows, str(getattr(result, "error_code", result_code)), str(getattr(result, "error_msg", ""))


class _ProtocolCapture:
    def __init__(self, raw_root: Path, max_calls: int):
        import baostock.util.socketutil as socketutil

        self.socketutil = socketutil
        self.original = socketutil.send_msg
        self.raw_root = raw_root
        self.max_calls = max_calls
        self.label = "unlabeled"
        self.calls: list[dict[str, Any]] = []

    def install(self) -> None:
        def wrapped(message: str) -> str:
            if len(self.calls) >= self.max_calls:
                raise RuntimeError("BAOSTOCK_PROTOCOL_CALL_LIMIT_REACHED")
            call = {
                "ordinal": len(self.calls) + 1,
                "operation": self.label,
                "request_time": _now().isoformat(timespec="milliseconds"),
                "request_sha256": _sha256_bytes(message.encode("utf-8")),
                "retry": False,
            }
            self.calls.append(call)
            try:
                response = self.original(message)
                body = (response or "").encode("utf-8", errors="strict")
                raw_name = f"provider_protocol_{call['ordinal']:03d}.bin"
                self.raw_root.mkdir(parents=True, exist_ok=True)
                (self.raw_root / raw_name).write_bytes(body)
                call.update({
                    "received_at": _now().isoformat(timespec="milliseconds"),
                    "response_bytes": len(body),
                    "response_sha256": _sha256_bytes(body),
                    "raw_file": raw_name,
                })
                return response
            except Exception as exc:
                call.update({
                    "received_at": _now().isoformat(timespec="milliseconds"),
                    "error_type": type(exc).__name__,
                })
                raise

        self.socketutil.send_msg = wrapped

    def restore(self) -> None:
        self.socketutil.send_msg = self.original

    @contextlib.contextmanager
    def operation(self, label: str):
        old = self.label
        self.label = label
        try:
            yield
        finally:
            self.label = old


def capture_target_date(trade_date: str, evidence_root: str | Path) -> dict[str, Any]:
    """Capture stock identity, dated status and daily bars once; no retries."""
    target = dt.datetime.strptime(str(trade_date).replace("-", ""), "%Y%m%d").date().isoformat()
    root = Path(evidence_root).resolve()
    raw_root = root / "raw"
    normalized_root = root / "normalized"
    normalized_root.mkdir(parents=True, exist_ok=True)

    import baostock as bs
    tap = _ProtocolCapture(raw_root, MAX_BAOSTOCK_PROTOCOL_CALLS)
    tap.install()
    logged_in = False
    capture: dict[str, Any] = {
        "provider": "BaoStock",
        "trade_date": target.replace("-", ""),
        "started_at": _now().isoformat(timespec="milliseconds"),
        "timezone": "Asia/Shanghai",
        "automatic_retry": False,
        "max_protocol_calls": MAX_BAOSTOCK_PROTOCOL_CALLS,
        "package_version": str(getattr(bs, "__version__", "unknown")),
        "sources": {},
        "protocol_calls": tap.calls,
    }
    try:
        with tap.operation("login"):
            login = bs.login()
        logged_in = str(getattr(login, "error_code", "")) == "0"
        capture["login"] = {
            "result_code": str(getattr(login, "error_code", "UNKNOWN")),
            "received_at": tap.calls[-1].get("received_at") if tap.calls else None,
            "success": logged_in,
        }
        if not logged_in:
            raise ActiveG6RouteHold("BAOSTOCK_LOGIN_FAILED")

        calls = (
            ("stock_basic", lambda: bs.query_stock_basic(), None),
            ("all_stock", lambda: bs.query_all_stock(day=target), target),
            ("daily", lambda: bs.query_daily_history_k_AStock(date=target), target),
        )
        row_sets: dict[str, list[dict[str, Any]]] = {}
        for name, query, requested_date in calls:
            start_index = len(tap.calls)
            with tap.operation(name):
                result = query()
            fields, rows, result_code, error_msg = _consume(result)
            protocol = tap.calls[start_index:]
            normalized = _json_bytes(rows)
            normalized_path = normalized_root / f"{name}.json"
            normalized_path.write_bytes(normalized)
            if name == "daily":
                unique = len({(str(row.get("code")), str(row.get("date"))) for row in rows})
                date_distribution: dict[str, int] = {}
                for row in rows:
                    date_text = str(row.get("date", ""))
                    date_distribution[date_text] = date_distribution.get(date_text, 0) + 1
            else:
                unique = len({str(row.get("code")) for row in rows})
                date_distribution = {}
            raw_parts = [
                (raw_root / call["raw_file"]).read_bytes()
                for call in protocol if call.get("raw_file")
            ]
            response_bundle = b"".join(len(part).to_bytes(8, "big") + part for part in raw_parts)
            response_sha = _sha256_bytes(response_bundle) if response_bundle else None
            observed = protocol[-1].get("received_at") if protocol else None
            evidence = {
                "provider": "BaoStock",
                "query_status": "SUCCESS" if result_code == "0" else "FAILED",
                "result_code": int(result_code) if result_code.isdigit() else result_code,
                "row_count": len(rows),
                "unique_count": unique,
                "trade_date": target.replace("-", "") if requested_date else None,
                "sha256": _sha256_bytes(normalized),
                "source_id": f"BaoStock.{name}:{target.replace('-', '')}:{_sha256_bytes(normalized)}",
                "observed_at": observed,
                "request_time": protocol[0].get("request_time") if protocol else None,
                "response_sha256": response_sha,
                "fields": fields,
                "normalized_path": str(normalized_path),
                "raw_protocol_responses": [
                    {key: call.get(key) for key in
                     ("ordinal", "request_sha256", "response_sha256", "response_bytes", "raw_file")}
                    for call in protocol
                ],
            }
            if name == "daily":
                evidence["trade_date_distribution"] = date_distribution
                evidence["target_date_row_count"] = date_distribution.get(target, 0) + date_distribution.get(target.replace("-", ""), 0)
            capture["sources"][name] = evidence
            row_sets[name] = rows
            if result_code != "0" or not rows:
                capture["stop_reason"] = f"PROVIDER_QUERY_FAILED_OR_EMPTY:{name}"
                break
    except ActiveG6RouteHold as exc:
        capture["stop_reason"] = exc.reason
    except Exception as exc:
        capture["stop_reason"] = f"CAPTURE_EXCEPTION:{type(exc).__name__}"
    finally:
        if logged_in:
            try:
                with tap.operation("logout"):
                    logout = bs.logout()
                capture["logout"] = {
                    "result_code": str(getattr(logout, "error_code", "UNKNOWN")),
                    "success": str(getattr(logout, "error_code", "")) == "0",
                    "received_at": tap.calls[-1].get("received_at") if tap.calls else None,
                }
            except Exception as exc:
                capture["logout"] = {"success": False, "error_type": type(exc).__name__}
        tap.restore()

    capture["protocol_call_count"] = len(tap.calls)
    capture["completed_at"] = _now().isoformat(timespec="milliseconds")
    required = {"stock_basic", "all_stock", "daily"}
    capture["capture_complete"] = (
        set(capture["sources"]) == required
        and all(capture["sources"][name]["query_status"] == "SUCCESS"
                and capture["sources"][name]["row_count"] > 0
                and capture["sources"][name]["row_count"] == capture["sources"][name]["unique_count"]
                for name in required)
        and capture["sources"]["daily"].get("trade_date_distribution") in ({target: capture["sources"]["daily"].get("row_count")}, {target.replace("-", ""): capture["sources"]["daily"].get("row_count")})
    )
    capture["rows"] = row_sets if "row_sets" in locals() else {}
    _write_json(root / "capture_manifest.json", {key: value for key, value in capture.items() if key != "rows"})
    if not capture["capture_complete"]:
        return capture
    return capture


def _approved_board_override():
    canonical = json.dumps(
        APPROVED_BOARD_OVERRIDE_302132, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence_sha = _sha256_bytes(canonical)
    if evidence_sha != BOARD_OVERRIDE_RECORD_SHA256:
        raise ActiveG6RouteHold("APPROVED_BOARD_OVERRIDE_EVIDENCE_HASH_DRIFT")
    return dict(APPROVED_BOARD_OVERRIDE_302132), evidence_sha


def build_v2_snapshot(capture: Mapping[str, Any], *, trade_date: str) -> dict[str, Any]:
    target = str(trade_date).replace("-", "")
    if not capture.get("capture_complete"):
        raise ActiveG6RouteHold("PROVIDER_CAPTURE_INCOMPLETE", details={"capture_status": capture.get("stop_reason")})
    source_path = Path(__file__).resolve().parents[2] / "scripts" / "datasource" / "date_effective_universe_v2.py"
    if _sha256_file(source_path) != V2_SOURCE_SHA256:
        raise ActiveG6RouteHold("FORMAL_V2_SOURCE_SHA_DRIFT")
    import datasource.date_effective_universe_v2 as v2

    source_evidence = {}
    for name, evidence in capture["sources"].items():
        source_evidence[name] = {
            key: evidence.get(key) for key in
            ("provider", "query_status", "result_code", "row_count", "unique_count",
             "trade_date", "sha256", "source_id", "observed_at", "response_sha256")
        }
    override, override_hash = _approved_board_override()
    original = v2._classify_mainboard
    try:
        def approved_classifier(symbol: str):
            if symbol == override["canonical_symbol"] and target >= override["effective_from"]:
                return override["board"]
            return original(symbol)
        v2._classify_mainboard = approved_classifier
        snapshot = v2.build_full_roster_snapshot_v2(
            stock_basic_rows=capture["rows"]["stock_basic"],
            all_stock_rows=capture["rows"]["all_stock"],
            daily_rows=capture["rows"]["daily"],
            trade_date=target,
            source_evidence=source_evidence,
            created_at=str(capture["completed_at"]),
        )
    finally:
        v2._classify_mainboard = original
    override_record = dict(override)
    override_record["evidence_record_sha256"] = override_hash
    snapshot["authoritative_board_overrides"] = [override_record]
    snapshot["capture_evidence_class"] = "REAL_PROVIDER_RESPONSE"
    snapshot["scope"] = str(snapshot["scope"]) + "; exact-code official board override is evidence-bound"
    from datasource.listing_exclusions import resolve_official_terminations
    resolve_official_terminations(snapshot, capture, target)
    snapshot.pop("snapshot_identity_sha256", None)
    snapshot["snapshot_identity_sha256"] = v2.canonical_json_sha256(snapshot)
    valid, reasons = v2.validate_full_roster_snapshot_v2(snapshot)
    if not valid or snapshot.get("snapshot_readiness") != "READY_CANDIDATE":
        raise ActiveG6RouteHold("V2_SNAPSHOT_NOT_READY", details={"validation_reasons": reasons,
            "snapshot_readiness": snapshot.get("snapshot_readiness"),
            "qualification_failure_counts": snapshot.get("qualification_failure_counts")})
    return snapshot


def prepare_g6_snapshot(snapshot: Mapping[str, Any], *, trade_date: str,
                        season_id: str, completeness_root: str | Path) -> tuple[dict[str, Any], list[str]]:
    """Project the validated V2 membership into the frozen G6 code schema."""
    target = str(trade_date).replace("-", "")
    valid, reasons = validate_v2_snapshot(snapshot, target)
    if not valid:
        raise ActiveG6RouteHold("V2_SNAPSHOT_VALIDATION_FAILED", details={"validation_reasons": reasons})
    codes = [str(symbol).split(".", 1)[0] for symbol in snapshot["expected_codes"]]
    if len(codes) != len(set(codes)) or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ActiveG6RouteHold("V2_TO_G6_CODE_PROJECTION_INVALID")
    g6_path = Path(__file__).resolve().with_name("completeness.py")
    if _sha256_file(g6_path) != G6_SOURCE_SHA256:
        raise ActiveG6RouteHold("FORMAL_G6_SOURCE_SHA_DRIFT")
    from datasource import completeness as g6
    root = Path(completeness_root)
    projection_path = root / "v2_universe_sources" / f"{target}.json"
    payload = {
        "schema_version": "v7-g6-v2-derived-source-projection-v1",
        "trade_date": target,
        "universe_scope": "DATE_EFFECTIVE_UNIVERSE_V2",
        "universe_version": "DATE_EFFECTIVE_UNIVERSE_V2:" + str(snapshot["snapshot_identity_sha256"]),
        "source_snapshot_identity_sha256": snapshot["snapshot_identity_sha256"],
        "source_members_hash": snapshot["members_hash"],
        "source_evidence": snapshot["source_evidence"],
        "updated_at": f"{target[:4]}-{target[4:6]}-{target[6:8]}",
        "updated_at_semantics": "trade-date effective-set date; provider observed_at values remain in source_evidence",
        "total_count": len(codes),
        "stocks": [{"code": code} for code in sorted(codes)],
    }
    _write_json(projection_path, payload)
    g6_snapshot = g6.build_universe_snapshot(
        target, codes, "DATE_EFFECTIVE_UNIVERSE_V2", payload["universe_version"], str(projection_path)
    )
    g6_snapshot["season_id"] = season_id
    return g6_snapshot, codes


def validate_v2_snapshot(snapshot: Mapping[str, Any], trade_date: str) -> tuple[bool, list[str]]:
    import datasource.date_effective_universe_v2 as v2
    valid, reasons = v2.validate_full_roster_snapshot_v2(snapshot)
    expected_date = str(trade_date).replace("-", "")
    if str(snapshot.get("trade_date", "")).replace("-", "") != expected_date:
        return False, list(reasons) + ["V2_TRADE_DATE_MISMATCH"]
    return valid, reasons


def archive_legacy_artifact(source: str | Path, archive_path: str | Path) -> str | None:
    """Copy the prior legacy artifact before canonical route output replaces it."""
    source_path = Path(source)
    if not source_path.is_file():
        return None
    target = Path(archive_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_sha = _sha256_file(source_path)
    if target.exists():
        if _sha256_file(target) != source_sha:
            target = target.with_name(f"{target.stem}.{source_sha[:12]}{target.suffix}")
    if not target.exists():
        shutil.copy2(source_path, target)
    if _sha256_file(target) != source_sha:
        raise ActiveG6RouteHold("LEGACY_ARTIFACT_ARCHIVE_HASH_MISMATCH")
    return source_sha
