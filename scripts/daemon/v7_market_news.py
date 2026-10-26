# -*- coding: utf-8 -*-
"""Read-only V7 market/news projections over existing News Officer artifacts.

This module never imports or invokes the News Officer or Provider functions.
It only parses persisted JSON and marks timestamp/provenance limitations.
"""

import json
import math
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEWS_BRIEF_PATH = os.path.join(BASE_DIR, "data", "pipeline", "news_brief.json")
TODAY_NEWS_POOL_PATH = os.path.join(BASE_DIR, "state", "today_news_pool.json")
INDEX_INTRADAY_CACHE_PATH = os.path.join(BASE_DIR, "state", "index_intraday_cache.json")
WORKBENCH_DATA_PATH = os.path.join(BASE_DIR, "workbench", "data.json")
PIPELINE_STATE_PATH = os.path.join(BASE_DIR, "data", "pipeline", "pipeline_state.json")
SHANGHAI = ZoneInfo("Asia/Shanghai")

INDEXES = (
    ("sh000001", "000001", "上证指数", "SH"),
    ("sz399001", "399001", "深证成指", "SZ"),
    ("sz399006", "399006", "创业板指", "SZ"),
)


def _now(value=None):
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def _parse_local_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _iso(value):
    return value.isoformat(timespec="seconds") if value else None


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value, None
    except FileNotFoundError:
        return None, "SOURCE_FILE_MISSING"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "SOURCE_FILE_INVALID"


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _format_bar_time(trade_date, hhmm):
    if not isinstance(trade_date, str) or len(trade_date) != 8 or not trade_date.isdigit():
        return None
    if not isinstance(hhmm, str) or len(hhmm) != 4 or not hhmm.isdigit():
        return None
    try:
        value = datetime.strptime(trade_date + hhmm, "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
    except ValueError:
        return None
    return _iso(value)


def _read_intraday(primary, cache_path, warnings):
    if isinstance(primary, dict) and primary:
        return primary, "NEWS_OFFICER_BRIEF", False
    cached, error = _load_json(cache_path)
    if isinstance(cached, dict) and cached:
        warnings.append("market intraday using persisted cache; cache values are not assumed fresh")
        return cached, "INDEX_INTRADAY_CACHE", True
    if error:
        warnings.append(f"index intraday cache: {error}")
    return {}, None, False


ENVIRONMENT_LABELS = {"bull": "偏强", "range": "震荡", "bear": "偏弱", "crisis": "偏弱"}
ENVIRONMENT_INPUTS = {"sh_pct": "sh000001", "sz_pct": "sz399001", "cyb_pct": "sz399006", "kcb_pct": "sh000688"}


def _producer_problem(value):
    """Explicit source failures never become a verified display observation."""
    if not isinstance(value, dict):
        return False
    if ("stale" in value and value["stale"] is not False) or value.get("error") or value.get("error_message"):
        return True
    if str(value.get("status", "")).upper() in {"STALE", "ERROR", "FAILED", "INVALID", "UNAVAILABLE", "DEGRADED", "HOLD"}:
        return True
    quality = value.get("data_quality")
    return isinstance(quality, dict) and _producer_problem(quality)


def build_market_environment(workbench_path=None, pipeline_path=None, brief=None, now=None):
    """Owner-approved mapping of existing trend; no new regime calculation or writes.

    Same-day, same completed pipeline and matching quote inputs are required.
    Generated as_of is a regime timestamp, never a provider quote timestamp.
    """
    current = _now(now)
    workbench_path = workbench_path or WORKBENCH_DATA_PATH
    pipeline_path = pipeline_path or PIPELINE_STATE_PATH
    saved, saved_error = _load_json(workbench_path)
    pipeline, pipeline_error = _load_json(pipeline_path)
    saved = saved if isinstance(saved, dict) else {}
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    brief = brief if isinstance(brief, dict) else {}
    regime = saved.get("market_regime")
    latest = pipeline.get("latest")
    snapshot = saved.get("pipeline_state_snapshot")
    # Raw regime, flags and pipeline identity remain inspectable even on HOLD.
    evidence = {"market_regime": regime, "pipeline_latest": latest,
                "pipeline_snapshot": snapshot, "brief_fetched_at": brief.get("fetched_at"),
                "producer_quality": (saved.get("market_diagnosis") or {}).get("data_quality")
                if isinstance(saved.get("market_diagnosis"), dict) else None}
    result = {"status": "UNVERIFIED", "classification": None, "display_classification": None,
              "observed_at": None, "inputs": None, "rule_version": "existing_market_regime_v1",
              "source": "scripts/analysis/regime.py::compute_regime via pipeline.persist.record",
              "source_refs": [workbench_path, pipeline_path, NEWS_BRIEF_PATH],
              "evidence": evidence, "reason_codes": []}

    def reject(code):
        result["reason_codes"].append(code)
        return result

    if saved_error or pipeline_error:
        return reject("ENVIRONMENT_SOURCE_UNAVAILABLE")
    if not isinstance(regime, dict) or not isinstance(regime.get("trend"), str) or regime["trend"] not in ENVIRONMENT_LABELS:
        return reject("INVALID_REGIME_TREND")
    observed = _parse_local_timestamp(regime.get("as_of"))
    if not observed or observed.date() != current.date() or observed > current:
        return reject("REGIME_DATE_UNVERIFIED")
    meta = regime.get("meta")
    keys = ("avg_pct", *ENVIRONMENT_INPUTS)
    if not isinstance(meta, dict) or any(_number(meta.get(key)) is None for key in keys):
        return reject("REGIME_INPUTS_INCOMPLETE")
    if any(_producer_problem(item) for item in (saved, regime, meta, brief, evidence["producer_quality"])):
        return reject("PRODUCER_STALE_OR_ERROR")
    if not isinstance(latest, dict) or not isinstance(snapshot, dict):
        return reject("PIPELINE_EVIDENCE_MISSING")
    if latest.get("status") != "DONE" or snapshot.get("status") != "DONE":
        return reject("PIPELINE_NOT_COMPLETED")
    if any(_producer_problem(item) for item in (latest, snapshot)):
        return reject("PIPELINE_STALE_OR_ERROR")
    pipeline_id = latest.get("pipeline_id")
    if not isinstance(pipeline_id, str) or not pipeline_id or pipeline_id != snapshot.get("pipeline_id"):
        return reject("PIPELINE_BATCH_MISMATCH")
    started = _parse_local_timestamp(latest.get("created_at"))
    finished = _parse_local_timestamp(latest.get("finished_at"))
    captured = _parse_local_timestamp(brief.get("fetched_at"))
    snapshot_started = _parse_local_timestamp(snapshot.get("created_at"))
    dates = (started, finished, captured, snapshot_started)
    if any(not value or value.date() != current.date() for value in dates):
        return reject("PIPELINE_DATE_UNVERIFIED")
    if not (started == snapshot_started and started <= captured <= observed <= finished <= current):
        return reject("PIPELINE_TIME_MISMATCH")
    for item in (latest, snapshot):
        stages = item.get("stages")
        if not isinstance(stages, dict) or any(not isinstance(stage, dict) or stage.get("status") not in {"DONE", "SKIPPED"}
                                               or _producer_problem(stage) for stage in stages.values()):
            return reject("PIPELINE_STAGE_FAILURE")
        if any((stages.get(key) or {}).get("status") != "DONE" for key in ("tongzhengsi", "qijuzhu")):
            return reject("MARKET_PRODUCER_NOT_COMPLETED")
    quotes = brief.get("market_quotes")
    quotes = quotes if isinstance(quotes, dict) else {}
    quote_evidence = {}
    for key, symbol in ENVIRONMENT_INPUTS.items():
        quote = quotes.get(symbol)
        if (not isinstance(quote, dict) or quote.get("code") != symbol[2:] or _producer_problem(quote) or _number(quote.get("price"), positive=True) is None
                or _number(quote.get("pct")) is None or round(quote["pct"], 3) != meta[key]):
            return reject("REGIME_QUOTE_INPUT_MISMATCH")
        quote_evidence[symbol] = {"pct": quote["pct"]}
    # Integrity check of the existing producer's rounded mean, not a classification rule.
    if not math.isclose(round(sum(quotes[s]["pct"] for s in ENVIRONMENT_INPUTS.values()) / 4, 3),
                        meta["avg_pct"], rel_tol=0, abs_tol=1e-9):
        return reject("REGIME_AVERAGE_MISMATCH")
    evidence["classification_input_evidence"] = quote_evidence
    result.update(status="VERIFIED", classification=regime["trend"],
                  display_classification=ENVIRONMENT_LABELS[regime["trend"]], observed_at=_iso(observed),
                  inputs={key: meta[key] for key in keys})
    return result


def build_market_view(brief_path=None, intraday_cache_path=None, now=None,
                      environment_source_path=None, pipeline_state_path=None):
    """Build the index view without triggering acquisition or database access."""
    current = _now(now)
    brief_path = brief_path or NEWS_BRIEF_PATH
    intraday_cache_path = intraday_cache_path or INDEX_INTRADAY_CACHE_PATH
    warnings = []
    brief, load_error = _load_json(brief_path)
    if load_error:
        warnings.append(f"news brief market source: {load_error}")
    if not isinstance(brief, dict):
        brief = {}

    captured = _parse_local_timestamp(brief.get("fetched_at"))
    quotes = brief.get("market_quotes") if isinstance(brief.get("market_quotes"), dict) else {}
    intraday, intraday_source, used_cache = _read_intraday(
        brief.get("index_intraday"), intraday_cache_path, warnings)

    index_rows = []
    for symbol, expected_code, display_name, exchange in INDEXES:
        raw_quote = quotes.get(symbol) if isinstance(quotes.get(symbol), dict) else {}
        raw_series = intraday.get(symbol) if isinstance(intraday.get(symbol), dict) else {}
        raw_code = str(raw_quote.get("code") or "")
        code_ok = raw_code == expected_code

        latest_price = _number(raw_quote.get("price"), positive=True) if code_ok else None
        previous_close = _number(raw_quote.get("pre_close"), positive=True) if code_ok else None
        change = _number(raw_quote.get("change")) if code_ok else None
        pct = _number(raw_quote.get("pct")) if code_ok else None
        if raw_quote and not code_ok:
            warnings.append(f"{symbol} quote code mismatch; snapshot fields withheld")

        market_date_raw = raw_series.get("date")
        market_date = (market_date_raw if isinstance(market_date_raw, str)
                       and len(market_date_raw) == 8 and market_date_raw.isdigit() else None)
        points = raw_series.get("points") if isinstance(raw_series.get("points"), list) else []
        clean_points = []
        for point in points[-390:]:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            hhmm, value = point[0], _number(point[1], positive=True)
            bar_at = _format_bar_time(market_date, hhmm)
            if value is None or bar_at is None:
                continue
            if datetime.fromisoformat(bar_at) > current.replace(second=59, microsecond=999999):
                continue
            clean_points.append({"time": hhmm, "bar_at": bar_at, "value": value})
        last_point = clean_points[-1] if clean_points else None
        # Neither capture time nor a separate index series proves this
        # snapshot's own trade date; only accept a quote-local date field.
        quote_date_raw = raw_quote.get("trade_date") or raw_quote.get("date")
        quote_date = None
        if isinstance(quote_date_raw, str) and len(quote_date_raw) == 8 and quote_date_raw.isdigit():
            try:
                quote_date = datetime.strptime(quote_date_raw, "%Y%m%d").date().isoformat()
            except ValueError:
                quote_date = None

        if raw_series.get("stale") is True or used_cache or (market_date and market_date != current.strftime("%Y%m%d")):
            series_status = "STALE"
        elif last_point:
            # A dated bar is verifiable historical evidence, but without an
            # approved UI freshness SLA its same-day age is not promoted to VALID.
            series_status = "UNKNOWN"
        elif raw_series:
            series_status = "INVALID"
        else:
            series_status = "MISSING"

        index_rows.append({
            "symbol": symbol,
            "code": expected_code,
            "name": str(raw_quote.get("name") or display_name),
            "exchange": exchange,
            "snapshot": {
                "last_price": latest_price,
                "previous_close": previous_close,
                "change": change,
                "change_percent": pct,
                "market_observed_at": None,
                "capture_started_at": _iso(captured),
                "market_date": quote_date,
                "provider": "tencent" if code_ok and raw_quote else None,
                "source": "auto_update_hotspots.fetch_market_quotes" if code_ok and raw_quote else None,
                "status": "UNKNOWN" if latest_price is not None else "MISSING",
                "unavailable_fields": ["market_observed_at", "volume", "amount_unit"],
            },
            "intraday": {
                "source": "Tencent web.ifzq.gtimg.cn minute endpoint" if clean_points and intraday_source == "NEWS_OFFICER_BRIEF" else intraday_source,
                "market_date": market_date,
                "points": clean_points,
                "last_price": last_point["value"] if last_point else None,
                "market_time": last_point["bar_at"] if last_point else None,
                "capture_completed_at": _parse_local_timestamp(raw_series.get("fetched_at")) and _iso(_parse_local_timestamp(raw_series.get("fetched_at"))),
                "status": series_status,
            },
        })

    has_any = any(row["snapshot"]["last_price"] is not None or row["intraday"]["points"] for row in index_rows)
    if not brief and not any(row["intraday"]["points"] for row in index_rows):
        status = "UNAVAILABLE"
    elif not has_any:
        status = "EMPTY"
    elif not captured or captured.date() != current.date():
        status = "STALE" if has_any else "EMPTY"
    else:
        # The existing quote artifact has a batch capture-start time, not a
        # provider quote timestamp. Do not promote it to real-time/fresh.
        status = "DEGRADED"
        warnings.append("provider quote observed_at is not persisted; quote snapshot is display-only with unverified freshness")

    if captured and captured > current:
        warnings.append("market capture timestamp is in the future; freshness cannot be verified")
        status = "DEGRADED"

    if any(row["intraday"]["points"] and row["intraday"]["status"] == "UNKNOWN" for row in index_rows):
        warnings.append("same-day intraday bars are shown with their bar time; no approved freshness threshold is applied")

    dates = [row["intraday"]["market_date"] for row in index_rows if row["intraday"]["market_date"]]
    return {
        "status": status,
        "warnings": warnings,
        "data_freshness": {
            "source_capture_started_at": _iso(captured),
            "capture_age_seconds": round((current - captured).total_seconds(), 1) if captured else None,
            "freshness_status": "UNVERIFIED_PROVIDER_TIME" if captured else "UNAVAILABLE",
            "market_date": max(dates) if dates else None,
        },
        "data": {
            "source": "data/pipeline/news_brief.json via News Officer; Tencent sources verified in existing code",
            "source_path": brief_path,
            "schema_version": brief.get("schema_version"),
            "capture_started_at": _iso(captured),
            "market_date": max(dates) if dates else None,
            "indexes": index_rows,
            "environment": build_market_environment(environment_source_path, pipeline_state_path, brief, current),
            "market_breadth": {"status": "UNAVAILABLE", "reason_code": "NO_VERIFIED_BREADTH_VIEW_MODEL"},
            "sectors": {"status": "UNAVAILABLE", "reason_code": "NO_VERIFIED_SECTOR_VIEW_MODEL"},
        },
    }


def _safe_http_url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))
    except (TypeError, ValueError):
        return None


def _canonical_dedupe_key(item):
    url = _safe_http_url(item.get("url"))
    if url:
        parsed = urlsplit(url)
        return "url:" + urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    title = " ".join(str(item.get("title") or "").split()).casefold()
    source = " ".join(str(item.get("source") or "").split()).casefold()
    recorded = str(item.get("time") or "")
    return f"fallback:{title}|{source}|{recorded}" if title else None


def _map_news_item(raw, *, is_hot=False):
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    raw_time = raw.get("time")
    parsed_time = _parse_local_timestamp(raw_time)
    url = _safe_http_url(raw.get("url"))
    meta = raw.get("blogger_views_meta")
    views = raw.get("blogger_views")
    if isinstance(meta, dict) and meta.get("model") and isinstance(views, dict):
        ai_analysis = {
            "status": "AVAILABLE",
            "generated_by": str(meta.get("model")),
            "perspectives": {str(k): str(v) for k, v in views.items() if isinstance(v, str)},
        }
    else:
        ai_analysis = {"status": "UNAVAILABLE", "reason_code": "NO_VERIFIABLE_AI_ANALYSIS_METADATA"}
    return {
        "id": str(raw.get("id")) if raw.get("id") is not None else None,
        "title": title.strip(),
        "summary": raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        "content": raw.get("content") if isinstance(raw.get("content"), str) else None,
        "source": raw.get("source") if isinstance(raw.get("source"), str) else None,
        "category": raw.get("category") if isinstance(raw.get("category"), str) else None,
        "themes": raw.get("themes") if isinstance(raw.get("themes"), list) else [],
        "entities": raw.get("entity") if isinstance(raw.get("entity"), list) else [],
        "url": url,
        "published_at": _iso(parsed_time),
        "published_at_raw": str(raw_time) if raw_time is not None else None,
        "published_at_status": "RECORDED_SOURCE_TIME_UNVERIFIED" if parsed_time else "UNAVAILABLE",
        "source_time_note": "News Officer 保存 item.time；缺失源 ctime 曾可能以采集机器当前时间回填。",
        "is_hot": bool(is_hot),
        "relevance": raw.get("relevance") if isinstance(raw.get("relevance"), dict) else None,
        "ai_analysis": ai_analysis,
        "source_record": "News Officer persisted item",
    }


def _merge_news_records(current_items, hot_items):
    merged = {}
    duplicate_count = 0
    for raw, is_hot in [(item, False) for item in current_items] + [(item, True) for item in hot_items]:
        mapped = _map_news_item(raw, is_hot=is_hot)
        if not mapped:
            continue
        key = _canonical_dedupe_key(raw)
        if not key:
            key = f"id:{mapped.get('id')}" if mapped.get("id") else None
        if not key:
            continue
        if key in merged:
            duplicate_count += 1
            merged[key]["is_hot"] = merged[key]["is_hot"] or mapped["is_hot"]
            continue
        merged[key] = mapped
    items = list(merged.values())
    items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return items, duplicate_count


def build_news_view(brief_path=None, pool_path=None, now=None):
    """Read News Officer output and a dated existing pool; never run collection."""
    current = _now(now)
    brief_path = brief_path or NEWS_BRIEF_PATH
    pool_path = pool_path or TODAY_NEWS_POOL_PATH
    warnings = []
    brief, brief_error = _load_json(brief_path)
    if brief_error:
        warnings.append(f"news brief: {brief_error}")
    if not isinstance(brief, dict):
        brief = {}
    pool, pool_error = _load_json(pool_path)
    if pool_error:
        warnings.append(f"today news pool: {pool_error}")
    if not isinstance(pool, dict):
        pool = {}

    fetched = _parse_local_timestamp(brief.get("fetched_at"))
    current_items = brief.get("news") if isinstance(brief.get("news"), list) else []
    hot_items = brief.get("hotspots") if isinstance(brief.get("hotspots"), list) else []
    source_mode = "NEWS_OFFICER_BRIEF"
    status = "DEGRADED"
    freshness_status = "UNVERIFIED_THRESHOLD"

    if not current_items and not hot_items:
        pool_items = pool.get("pool") if isinstance(pool.get("pool"), list) else []
        pool_date = pool.get("date")
        if pool_items:
            hot_items = pool_items
            source_mode = "DATED_NEWS_POOL_FALLBACK"
            fetched = None
            status = "DEGRADED" if pool_date == current.date().isoformat() else "STALE"
            freshness_status = "CAPTURE_TIME_UNAVAILABLE"
            warnings.append("news brief has no usable records; showing dated pool fallback with recorded item times")
        else:
            status = "EMPTY" if isinstance(brief.get("news"), list) or isinstance(brief.get("hotspots"), list) else "UNAVAILABLE"
            freshness_status = "UNAVAILABLE"

    items, duplicate_count = _merge_news_records(current_items, hot_items)
    if fetched and fetched.date() != current.date():
        status = "STALE"
        freshness_status = "STALE_SOURCE_CAPTURE_DATE"
    elif fetched and fetched > current:
        status = "DEGRADED"
        freshness_status = "FUTURE_CAPTURE_TIMESTAMP_INVALID"
        warnings.append("news fetched_at is in the future")
    elif not fetched and status not in ("EMPTY", "UNAVAILABLE", "STALE"):
        status = "DEGRADED"
        freshness_status = "CAPTURE_TIME_UNAVAILABLE"

    if duplicate_count:
        warnings.append(f"deduplicated {duplicate_count} repeated news record(s) by article URL or stable fallback key")
    if items and status == "EMPTY":
        status = "DEGRADED"
    hot_news = [item for item in items if item["is_hot"]]
    ordinary_news = [item for item in items if not item["is_hot"]]

    return {
        "status": status,
        "warnings": warnings,
        "data_freshness": {
            "source_capture_started_at": _iso(fetched),
            "capture_age_seconds": round((current - fetched).total_seconds(), 1) if fetched else None,
            "freshness_status": freshness_status,
        },
        "data": {
            "source": "News Officer persisted brief / dated today-news pool",
            "source_path": brief_path if source_mode == "NEWS_OFFICER_BRIEF" else pool_path,
            "source_mode": source_mode,
            "schema_version": brief.get("schema_version"),
            "capture_started_at": _iso(fetched),
            "source_collection_status": "NOT_PERSISTED_PER_SOURCE",
            "count": len(items),
            "duplicate_count": duplicate_count,
            "hot_news": hot_news,
            "ordinary_news": ordinary_news,
            "all_news": items,
        },
    }
