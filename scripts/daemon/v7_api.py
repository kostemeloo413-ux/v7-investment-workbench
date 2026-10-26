# -*- coding: utf-8 -*-
"""
scripts/daemon/v7_api.py
Phase UI-2: V7 前端只读数据聚合层 (Read-Only View Model Aggregation Layer)

【最高设计与架构原则】：
1. 纯只读 (GET ONLY)：本模块所有端点仅支持 GET 方法，严禁提供 POST/PUT/PATCH/DELETE，严禁业务状态变更；
2. 彻底解耦：前端 UI 不直接理解底层 SQLite schema、JSONL 或复杂研究文件树，由本模块转化为稳定 View Model；
3. 标准 Envelope 封装：统一包装 schema_version, generated_at, data_freshness, status, warnings, data；
4. 容错与局部降级 (Failure Isolation)：个别数据源故障不抛 500，标记局部 DEGRADED 并输出警告；
5. 真实性保障：缺失数据诚实返回 null / []，禁止编造假数据；区分真实 0 值 vs 数据不存在 vs 过期；
6. 严格白名单与路径穿越防御：研报接口只允许通过安全 report_id 访问白名单文件，严禁任意文件读取；
7. 高性能只读缓存：针对静态元数据、大文件与 SQLite 查询实施内存 LRU/mtime 缓存与尾部读取 (Tail Read)。
"""

import os
import sys
import json
import time
import math
import datetime
import sqlite3
import re
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb
from ai_quant.storage_utils import strict_load_json
from daemon.v7_market_news import build_market_view, build_news_view
from daemon.v7_picks import _read_gate_status, build_ai_quant_evidence, build_picks_view, build_stock_view

v7_router = APIRouter(prefix="/api/v7", tags=["v7_view_models"])

SCHEMA_VERSION = "v7.1"

# 路径常量定义
MARKET_DB_PATH = os.path.join(_BASE, 'data', 'market', 'market.db')
REPORTS_DIR = os.path.join(_BASE, 'docs', 'v7_review')
MODELS_MANIFEST_PATH = os.path.join(_BASE, 'models', 'ai_quant', 'production_manifest.json')
HEARTBEAT_PATH = os.path.join(_BASE, 'state', 'heartbeat.json')


def _active_season_context_or_warning(warnings):
    """Resolve the one active namespace; APIs never fall back to legacy economics."""
    try:
        from competition.season_runtime import resolve_active_season_context
        return resolve_active_season_context()
    except Exception as exc:
        warnings.append(f'active season context unavailable: {exc}')
        return None


def _strict_active_json(path, expected_type, warnings, label):
    result = strict_load_json(path, expected_type=expected_type)
    if not result.is_valid:
        warnings.append(f'{label} unavailable: {result.status}')
        return None
    return result.value


def _active_safety_view(context, warnings):
    """Read active-season governance without inventing an ALLOW/HALT value."""
    path = context.get('governance_safety_path')
    safety = _strict_active_json(path, dict, warnings, 'active-season governance') if path else None
    if safety is None:
        return {
            'status': 'UNAVAILABLE', 'halt_status': 'UNKNOWN',
            'execution_state_safe': None, 'new_entry_disabled': None,
            'risk_exit_enabled': None, 'trading_enabled': None,
            'real_trading': None, 'halt_reasons': None,
        }
    required = ('execution_state_safe', 'new_entry_disabled', 'risk_exit_enabled',
                'trading_enabled', 'real_trading')
    valid = all(isinstance(safety.get(key), bool) for key in required)
    if not valid:
        warnings.append('active-season governance fields incomplete or invalid')
    # The current governance schema records entry/safety flags and historical
    # halt events, but does not define a current active-halt boolean.
    explicit_halt = safety.get('halt_status')
    halt_status = explicit_halt if isinstance(explicit_halt, bool) else 'UNKNOWN'
    return {
        'status': 'VERIFIED' if valid else 'UNAVAILABLE',
        'halt_status': halt_status,
        'execution_state_safe': safety.get('execution_state_safe') if isinstance(safety.get('execution_state_safe'), bool) else None,
        'new_entry_disabled': safety.get('new_entry_disabled') if isinstance(safety.get('new_entry_disabled'), bool) else None,
        'risk_exit_enabled': safety.get('risk_exit_enabled') if isinstance(safety.get('risk_exit_enabled'), bool) else None,
        'trading_enabled': safety.get('trading_enabled') if isinstance(safety.get('trading_enabled'), bool) else None,
        'real_trading': safety.get('real_trading') if isinstance(safety.get('real_trading'), bool) else None,
        'halt_reasons': safety.get('halt_reasons') if isinstance(safety.get('halt_reasons'), list) else None,
    }


def _business_health_view(alive, heartbeat, warnings):
    """Process heartbeat is liveness evidence, never business-health proof."""
    epoch = alive.get('epoch') if isinstance(alive, dict) else None
    try:
        age = max(0.0, round(time.time() - float(epoch), 1)) if epoch is not None else None
    except (TypeError, ValueError):
        age = None
    jobs = heartbeat.get('jobs') if isinstance(heartbeat, dict) else None
    known_failures = []
    if isinstance(jobs, dict):
        for job_name, record in jobs.items():
            if not isinstance(record, dict):
                continue
            result = record.get('result')
            if isinstance(result, str) and ('FAILED' in result.upper() or 'ERROR' in result.upper()):
                known_failures.append(job_name)
    if known_failures:
        status = 'DEGRADED'
    elif age is None or age > 600:
        status = 'STALE' if age is not None else 'UNKNOWN'
    else:
        # A fresh daemon marker proves only process liveness. There is no
        # single authoritative business-health reducer in the current contract.
        status = 'UNKNOWN'
        warnings.append('process heartbeat is not sufficient to assert business health')
    return {
        'business_status': status,
        'daemon_liveness': 'LIVE' if age is not None and age <= 600 else ('STALE' if age is not None else 'UNKNOWN'),
        'daemon_alive_age_seconds': age,
        'known_failed_jobs': known_failures,
        'source': 'state/daemon_alive.json + state/heartbeat.json',
    }

# 研报白名单字典 (防路径穿越)
RESEARCH_REPORTS_WHITELIST = {
    'phase12_research_report': 'PHASE_12_RESEARCH_REPORT.md',
    'phase12_entry_gate': 'PHASE_12_ENTRY_GATE.md',
    'phase12_human_audit': 'PHASE_12_HUMAN_AUDIT.md',
    'phase13_production_gap_audit': 'PHASE_13_PRODUCTION_GAP_AUDIT.md',
    'phase13a_artifact_promotion': 'PHASE_13A_ARTIFACT_PROMOTION.md',
    'phase14_shadow_operational_report': 'PHASE_14_SHADOW_OPERATIONAL_REPORT.md',
    'phase14_5_risk_gate_human_audit': 'PHASE_14_5_RISK_GATE_HUMAN_AUDIT.md',
    'phase15_risk_calibration_research': 'PHASE_15_RISK_CALIBRATION_RESEARCH.md',
    'phase15_5_risk_contract_decision': 'PHASE_15_5_RISK_CONTRACT_DECISION.md',
    'phase16a_risk_contract_patch_report': 'PHASE_16A_RISK_CONTRACT_PATCH_REPORT.md',
    'phase16b_shadow_risk_validation': 'PHASE_16B_SHADOW_RISK_VALIDATION.md',
    'phase17_daily_report': 'PHASE_17_DAILY_REPORT.md',
    'phase17_paper_simulation_gate': 'PHASE_17_PAPER_SIMULATION_GATE.md',
    'phase18_baseline_freeze': 'PHASE_18_BASELINE_FREEZE.md',
    'phase18_forward_20d_interim_report': 'PHASE_18_FORWARD_20D_INTERIM_REPORT.md',
    'phase18_5_forward_integrity': 'PHASE_18_5_FORWARD_INTEGRITY_AND_PNL_ATTRIBUTION.md',
    'phase18f_genuine_forward_start_gate': 'PHASE_18F_GENUINE_FORWARD_START_GATE.md',
    'phase_ui_0_frontend_architecture_audit': 'PHASE_UI_0_FRONTEND_ARCHITECTURE_AUDIT.md',
    'phase_ui_1_v7_frontend_redesign_blueprint': 'PHASE_UI_1_V7_FRONTEND_REDESIGN_BLUEPRINT.md',
    'v7_ui_a1_overview': 'V7_UI_A1_LIGHT_SHELL_OVERVIEW_REPORT.md',
    'v7_ui_a2_market_news': 'V7_UI_A2_MARKET_NEWS_IMPLEMENTATION_REPORT.md',
    'v7_g6_p1c1_scanner': 'V7_G6_P1C1_SCANNER_INTEGRATION_REPORT.md',
    'v7_g6_p1c2_parity': 'V7_G6_P1C2_SCANNER_PARITY_REPORT.md',
    'new_season_architecture': 'V7_NEW_SEASON_20260930_ARCHITECTURE_DECISION_V1.md',
    'new_season_launch_admission': 'V7_NEW_SEASON_LAUNCH_DATA_ADMISSION_V1.md',
}

# 内存快速 mtime 缓存字典
_CACHE = {}


def _get_file_freshness(filepath: str, max_age_fresh_sec=30, max_age_live_sec=5) -> Dict[str, Any]:
    """计算单个数据源文件的新鲜度"""
    if not os.path.exists(filepath):
        return {'source_timestamp': None, 'age_seconds': None, 'freshness_status': 'UNAVAILABLE'}
    try:
        mtime = os.path.getmtime(filepath)
        now = time.time()
        age = max(0.0, round(now - mtime, 1))
        dt_str = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
        if age <= max_age_live_sec:
            status = 'LIVE'
        elif age <= max_age_fresh_sec:
            status = 'FRESH'
        else:
            status = 'STALE'
        return {'source_timestamp': dt_str, 'age_seconds': age, 'freshness_status': status}
    except Exception:
        return {'source_timestamp': None, 'age_seconds': None, 'freshness_status': 'UNAVAILABLE'}


def _load_json_cached(filepath: str, default=None) -> Any:
    """基于 mtime 的安全只读 JSON 缓存"""
    if not os.path.exists(filepath):
        return default
    try:
        mtime = os.path.getmtime(filepath)
        cache_key = f"json_{filepath}"
        if cache_key in _CACHE and _CACHE[cache_key]['mtime'] == mtime:
            return _CACHE[cache_key]['data']
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _CACHE[cache_key] = {'mtime': mtime, 'data': data}
        return data
    except Exception:
        return default


def _tail_jsonl(filepath: str, limit=50) -> List[Dict[str, Any]]:
    """高效读取 JSONL 文件末尾 N 行（避免全量扫描大文件）"""
    if not os.path.exists(filepath):
        return []
    records = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            line_str = line.strip()
            if line_str:
                try:
                    records.append(json.loads(line_str))
                except Exception:
                    continue
        return records
    except Exception:
        return []


def _make_envelope(data: Any, freshness_dict: Dict[str, Any], warnings: List[str] = None,
                   status: str = "OK") -> Dict[str, Any]:
    """统一标准响应 Envelope"""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "data_freshness": freshness_dict,
        "status": status,
        "warnings": warnings or [],
        "data": data
    }


# =====================================================================
# API 1: GET /api/v7/overview (Command Center 第一屏与顶部上下文)
# =====================================================================
@v7_router.get("/overview")
def get_v7_overview():
    warnings = []
    status = "OK"
    context = _active_season_context_or_warning(warnings)
    if context is None:
        return _make_envelope({'active_season': None, 'forward_epoch': None,
                               'account_matrix': None, 'current_positions': None,
                               'personal_portfolio': {'status': 'UNAVAILABLE', 'reason_code': 'PERSONAL_PORTFOLIO_UNAVAILABLE'},
                               'latest_ai_quant_signals': None,
                               'latest_execution_events': None, 'critical_alerts': None,
                               'system_health': {'business_status': 'UNKNOWN', 'daemon_liveness': 'UNKNOWN'}},
                              {'active_season_context': {'freshness_status': 'UNAVAILABLE'}}, warnings=warnings, status='DEGRADED')
    manifest_p = os.path.join(context['forward_root'], 'forward_epoch_manifest.json')
    acc_p = os.path.join(context['ai_quant_paper_root'], 'account.json')
    pos_p = os.path.join(context['ai_quant_paper_root'], 'positions.json')
    fills_p = os.path.join(context['ai_quant_paper_root'], 'fills.jsonl')
    manifest = _strict_active_json(manifest_p, dict, warnings, 'active Forward manifest')
    acc = _strict_active_json(acc_p, dict, warnings, 'active AI Quant account')
    positions = _strict_active_json(pos_p, list, warnings, 'active AI Quant positions')
    if manifest is None or acc is None or positions is None:
        status = 'DEGRADED'
    freshness_manifest, freshness_acc = _get_file_freshness(manifest_p), _get_file_freshness(acc_p)
    safety_view = _active_safety_view(context, warnings)
    if safety_view['status'] != 'VERIFIED':
        status = 'DEGRADED'

    # 4. Select one complete AI Quant pulse; never merge rows by id.
    aiq_view = build_ai_quant_evidence(MARKET_DB_PATH, current_trade_date=rc.today_str(),
                                       is_trading_day=rc.is_trading_day)
    latest_signals = aiq_view.get('predictions', [])
    if aiq_view.get('status') in ('STALE', 'UNAVAILABLE', 'EMPTY'):
        warnings.append(f"AI Quant prediction status: {aiq_view.get('classification')}")
        status = "DEGRADED"

    # 5. 最新执行事件
    recent_fills = _tail_jsonl(fills_p, limit=5)

    # 6. 看门狗状态 (统一权威源: rc.ALIVE_PATH -> state/daemon_alive.json)
    alive = _load_json_cached(rc.ALIVE_PATH, {})
    heartbeat = _load_json_cached(HEARTBEAT_PATH, {})
    health_view = _business_health_view(alive, heartbeat, warnings)
    if health_view['business_status'] in ('DEGRADED', 'STALE', 'UNKNOWN'):
        status = 'DEGRADED'

    manifest_status = manifest.get('status') if manifest else None
    gate_map = manifest.get('required_gates') if isinstance(manifest, dict) else None
    g6_status = (gate_map.get('G6_MARKET_COMPLETENESS', {}).get('status')
                 if isinstance(gate_map, dict) and isinstance(gate_map.get('G6_MARKET_COMPLETENESS'), dict)
                 else 'UNKNOWN')
    overview_data = {
        'active_season': {
            'season_id': context.get('season_id'),
            'status': context.get('manifest', {}).get('status') if isinstance(context.get('manifest'), dict) else None,
            'source': context.get('manifest_path'),
            'data_status': 'VALID' if context.get('season_id') else 'UNKNOWN',
        },
        'system_control': {
            'execution_mode': 'PAPER_SIMULATION',
            'observation_mode': manifest_status or 'UNKNOWN',
            'real_trading': safety_view['real_trading'],
            'production_ready': False,
            'research_validated': None,
            'parameters_frozen': None,
            'halt_status': safety_view['halt_status'],
            'safety': safety_view,
            'g6_status': g6_status,
        },
        'forward_epoch': {
            'epoch_id': manifest.get('forward_epoch_id') if manifest else None,
            'baseline_freeze_timestamp': manifest.get('baseline_timestamp') if manifest else None,
            'forward_day_count': manifest.get('forward_day_count') if manifest else None,
            'status': manifest_status or 'UNAVAILABLE',
            'forward_enabled': manifest.get('forward_enabled') if manifest else None,
            'genuine_forward_started': manifest.get('genuine_forward_started') if manifest else None,
            'source': manifest_p,
            'data_status': 'VALID' if manifest else 'UNAVAILABLE',
        },
        'account_matrix': {
            'scope': 'AI_QUANT_PAPER_ONLY',
            'account_id': acc.get('account_id') if acc else None,
            'initial_capital': acc.get('initial_capital') if acc else None,
            'total_equity': acc.get('total_equity') if acc else None,
            'cash': acc.get('cash') if acc else None,
            'market_value': acc.get('market_value') if acc else None,
            'daily_pnl': None,
            'cumulative_pnl': acc.get('net_pnl') if acc else None,
            'cumulative_pnl_pct': acc.get('net_pnl_pct') if acc else None,
            'max_drawdown_pct': acc.get('max_drawdown_pct') if acc else None,
            'gross_exposure_pct': (round((acc['market_value'] / max(1.0, acc['total_equity'])) * 100, 2)
                                   if acc and isinstance(acc.get('market_value'), (int, float))
                                   and isinstance(acc.get('total_equity'), (int, float)) else None),
            'max_positions_limit': acc.get('max_positions') if acc else None,
            'single_position_cap_ratio': acc.get('max_single_position_ratio') if acc else None,
            'data_status': 'VALID' if acc else 'UNAVAILABLE',
            'source': acc_p,
        },
        'current_positions': positions,
        'personal_portfolio': {
            'status': 'UNAVAILABLE',
            'reason_code': 'PERSONAL_PORTFOLIO_UNAVAILABLE',
            'source': None,
        },
        'latest_ai_quant_signals': latest_signals,
        'latest_execution_events': recent_fills,
        'critical_alerts': None,
        'system_health': health_view,
        'market_opportunity': {'status': 'PARTIAL', 'api': '/api/v7/market',
                               'reason_code': 'SNAPSHOT_TIME_UNVERIFIED'},
        'news': {'status': 'PARTIAL', 'api': '/api/v7/news',
                 'reason_code': 'SOURCE_FRESHNESS_POLICY_UNVERIFIED'},
        'today_picks': {'status': 'READ_ONLY_LINKED', 'api': '/api/v7/picks',
                        'source_type': 'LEGACY_PRODUCTION_SCANNER',
                        'data_admission': 'NOT_YET_MIGRATED_TO_P1C3',
                        'execution_authorized': False},
        'ai_quant_prediction_status': aiq_view.get('classification'),
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    freshness_summary = {
        'active_season_id': context['season_id'],
        'account_freshness': freshness_acc,
        'manifest_freshness': freshness_manifest
    }
    return _make_envelope(overview_data, freshness_summary, warnings=warnings, status=status)


@v7_router.get("/market")
def get_v7_market():
    """Read persisted market display artifacts only; never refresh providers."""
    try:
        result = build_market_view()
        return _make_envelope(result['data'], result['data_freshness'],
                              warnings=result['warnings'], status=result['status'])
    except Exception as exc:
        return _make_envelope(
            {'indexes': [], 'market_breadth': {'status': 'UNAVAILABLE'},
             'sectors': {'status': 'UNAVAILABLE'}},
            {'freshness_status': 'UNAVAILABLE'},
            warnings=[f'market view unavailable: {type(exc).__name__}'], status='UNAVAILABLE')


@v7_router.get("/news")
def get_v7_news():
    """Read persisted News Officer artifacts only; never invoke collection."""
    try:
        result = build_news_view()
        return _make_envelope(result['data'], result['data_freshness'],
                              warnings=result['warnings'], status=result['status'])
    except Exception as exc:
        return _make_envelope(
            {'count': 0, 'hot_news': [], 'ordinary_news': [], 'all_news': []},
            {'freshness_status': 'UNAVAILABLE'},
            warnings=[f'news view unavailable: {type(exc).__name__}'], status='UNAVAILABLE')


@v7_router.get("/picks")
def get_v7_picks():
    """Read persisted role decisions and one AI Quant pulse; never scan/infer."""
    try:
        result = build_picks_view()
        return _make_envelope(result['data'], result['data_freshness'],
                              warnings=result['warnings'], status=result['status'])
    except Exception as exc:
        return _make_envelope({'participants': [], 'ai_quant_top5': {'status': 'UNAVAILABLE', 'predictions': []},
                               'risk_sentinel': {'status': 'UNAVAILABLE', 'capital': 0,
                                                 'trading_enabled': False, 'ranking_enabled': False},
                               'execution_authorized': False},
                              {'freshness_status': 'UNAVAILABLE'},
                              warnings=[f'picks view unavailable: {type(exc).__name__}'], status='UNAVAILABLE')


@v7_router.get("/stock/{symbol}")
def get_v7_stock(symbol: str):
    """Read persisted stock evidence only; no provider, scoring, or execution."""
    try:
        news = build_news_view()
        news_items = (news.get('data') or {}).get('all_news', [])
        result = build_stock_view(symbol, news_items=news_items)
        return _make_envelope(result.get('data'), {'freshness_status': result['status']},
                              warnings=result.get('warnings', []), status=result['status'])
    except Exception as exc:
        return _make_envelope(None, {'freshness_status': 'UNAVAILABLE'},
                              warnings=[f'stock view unavailable: {type(exc).__name__}'], status='UNAVAILABLE')


@v7_router.get("/data-center")
def get_v7_data_center():
    """Small, read-only metadata/schema summary; never calls a writer helper."""
    warnings = []
    exists = os.path.isfile(MARKET_DB_PATH)
    db_meta = {'path': 'data/market/market.db', 'exists': exists, 'size_bytes': None,
               'main_file_mtime': None, 'sqlite_mode': 'READ_ONLY', 'statistics_status': 'UNKNOWN'}
    tables = []
    if exists:
        try:
            stat = os.stat(MARKET_DB_PATH)
            db_meta['size_bytes'] = stat.st_size
            db_meta['main_file_mtime'] = datetime.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
            uri = 'file:' + os.path.abspath(MARKET_DB_PATH).replace('\\', '/') + '?mode=ro'
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                conn.execute('PRAGMA query_only=ON')
                tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                db_meta['tables_present'] = {name: name in tables for name in (
                    'daily_kline', 'instrument_master', 'news', 'sync_meta', 'ai_quant_predictions', 'ai_quant_trade_plans')}
                db_meta['statistics_status'] = 'SCHEMA_READ_ONLY; ROW_COUNTS_NOT_QUERIED'
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            warnings.append('market database metadata/schema read unavailable')
            db_meta['statistics_status'] = 'UNAVAILABLE'
    else:
        db_meta['statistics_status'] = 'UNAVAILABLE'
    context = _active_season_context_or_warning(warnings)
    g6 = 'UNKNOWN'
    if context:
        # G6 is a Forward-epoch gate, not a season-manifest field. Read the
        # active epoch manifest explicitly; never infer PASS from season state.
        manifest = {}
        forward_root = context.get('forward_root')
        if isinstance(forward_root, str) and forward_root:
            epoch_manifest = os.path.join(forward_root, 'forward_epoch_manifest.json')
            loaded = _strict_active_json(epoch_manifest, dict, warnings, 'active Forward manifest')
            if isinstance(loaded, dict):
                manifest = loaded
        gates = manifest.get('required_gates') if isinstance(manifest, dict) else {}
        gate = gates.get('G6_MARKET_COMPLETENESS') if isinstance(gates, dict) else None
        if isinstance(gate, dict):
            g6 = gate.get('status', 'UNKNOWN')
    if g6 == 'UNKNOWN':
        warnings.append('G6 gate status could not be read from active-season manifest')
    data = {
        'database': db_meta,
        'g6_status': g6,
        'scope': 'MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE',
        'completeness_claim': 'NOT_CLAIMED',
        'provider_routing': 'LEGACY_PRODUCTION_PATH; P1C3_NOT_STARTED',
        'p1c2_parity': 'PARTIAL',
        'p1c3': 'NOT_STARTED',
        'source_note': 'file mtime is only main-file metadata; it is not market observed_at or proof of current data freshness.',
    }
    return _make_envelope(data, {'freshness_status': 'METADATA_ONLY'}, warnings=warnings,
                          status='DEGRADED' if warnings else 'OK')


@v7_router.get("/data-sources")
def get_v7_data_sources():
    base = _BASE
    items = [
        {'id': 'hithink', 'name': 'HiThink', 'adapter': 'IMPLEMENTED' if os.path.isfile(os.path.join(base, 'scripts', 'datasource', 'provider_adapters.py')) else 'UNKNOWN', 'production_route': 'NOT_SWITCHED', 'credential_rotation': 'UNVERIFIED'},
        {'id': 'tencent', 'name': 'Tencent', 'adapter': 'IMPLEMENTED', 'production_route': 'LEGACY_SCANNER_ONLY', 'field_contract': 'EMPIRICAL_PARTIAL'},
        {'id': 'news', 'name': 'News Officer', 'artifact': 'AVAILABLE' if os.path.isfile(os.path.join(base, 'data', 'pipeline', 'news_brief.json')) else 'UNAVAILABLE', 'collection_triggered': False},
        {'id': 'market_db', 'name': 'market.db', 'metadata': 'AVAILABLE' if os.path.isfile(MARKET_DB_PATH) else 'UNAVAILABLE', 'g6': 'OPEN'},
        {'id': 'scanner', 'name': 'Scanner', 'production_route': 'LEGACY_DIRECT_TENCENT', 'p1c2': 'ISOLATED_PARTIAL_PARITY', 'p1c3': 'NOT_STARTED'},
        {'id': 'ai_quant', 'name': 'AI Quant', 'inference_triggered': False, 'prediction_status': 'READ_ONLY_PULSE_ONLY'},
    ]
    return _make_envelope({'sources': items}, {'freshness_status': 'CONFIGURATION_AND_ARTIFACT_METADATA'}, status='DEGRADED')


@v7_router.get("/integrations/llm")
def get_v7_llm_status():
    from urllib.parse import urlsplit
    host = urlsplit(getattr(rc, 'LLM_BASE_URL', '') or '').hostname
    model = getattr(rc, 'LLM_MODEL', None)
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,80}', model):
        model = None
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host):
        host = None
    data = {'configured': bool(getattr(rc, 'LLM_API_KEY', '')), 'provider_host': host,
            'model': model, 'connection_status': 'UNKNOWN',
            'last_error': 'UNKNOWN', 'secret_returned': False, 'write_enabled': False}
    return _make_envelope(data, {'freshness_status': 'PROCESS_CONFIGURATION_ONLY'}, status='DEGRADED')


@v7_router.get("/integrations/wecom")
def get_v7_wecom_status():
    push_log = _load_json_cached(rc.PUSH_LOG_PATH, {})
    sent = push_log.get('sent') if isinstance(push_log, dict) and isinstance(push_log.get('sent'), list) else []
    last = sent[-1] if sent and isinstance(sent[-1], dict) else {}
    timestamp = last.get('time')
    if not isinstance(timestamp, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', timestamp):
        timestamp = None
    level = last.get('level')
    if level not in ('info', 'ok', 'warn', 'alert'):
        level = None
    notification_status = last.get('status')
    if notification_status not in ('OK', 'FAILED', 'SUCCESS', 'ERROR'):
        notification_status = 'UNKNOWN'
    safe_last = {'time': timestamp, 'level': level, 'status': notification_status}
    data = {'configured': bool(getattr(rc, 'WECOM_WEBHOOK_URL', '')),
            'quota_used': push_log.get('count') if isinstance(push_log, dict) and isinstance(push_log.get('count'), int) else None,
            'quota_limit': getattr(rc, 'PUSH_DAILY_LIMIT', None), 'last_notification': safe_last,
            'secret_returned': False, 'write_enabled': False}
    return _make_envelope(data, {'freshness_status': _get_file_freshness(rc.PUSH_LOG_PATH).get('freshness_status')},
                          status='DEGRADED' if safe_last.get('status') == 'UNKNOWN' else 'OK')


# =====================================================================
# API 2: GET /api/v7/ai-quant (策略实验室、特征重要性与截面打分)
# =====================================================================
@v7_router.get("/ai-quant")
def get_v7_ai_quant():
    warnings = []
    status = "OK"

    # 1. 读取模型生产清单
    manifest = _load_json_cached(MODELS_MANIFEST_PATH, {})
    freshness = _get_file_freshness(MODELS_MANIFEST_PATH)

    # 2. 读取模型元数据 (特征重要性)
    meta_p = os.path.join(_BASE, 'models', 'ai_quant', 'lgb_ranker_AIQ-V1.0_meta.json')
    meta = _load_json_cached(meta_p, {})

    feat_importance = meta.get('feature_importance', {})
    sorted_feats = [{'feature': k, 'importance_gain': round(float(v), 2)}
                    for k, v in sorted(feat_importance.items(), key=lambda x: x[1], reverse=True)]

    # 3. Only expose one complete pulse; date/time/model identity are retained.
    aiq_view = build_ai_quant_evidence(MARKET_DB_PATH, current_trade_date=rc.today_str(),
                                       is_trading_day=rc.is_trading_day)
    predictions_table = aiq_view.get('predictions', [])
    if aiq_view.get('status') != 'NORMAL':
        warnings.append(f"AI Quant prediction status: {aiq_view.get('classification')}")
        status = "DEGRADED"

    quant_data = {
        'model_metadata': {
            'model_name': manifest.get('model', {}).get('model_name', 'LightGBM LambdaMART Ranker'),
            'model_version': manifest.get('model', {}).get('model_version', 'PH12-LGB-OPEN'),
            'model_sha256': manifest.get('model', {}).get('sha256'),
            'feature_count': len(sorted_feats),
            'training_sample_count': manifest.get('model', {}).get('sample_count', 69877),
            'calibrator_version': manifest.get('calibrator', {}).get('calibrator_name', 'Platt Scaling')
        },
        'feature_importance_ranking': sorted_feats,
        'recent_predictions': predictions_table,
        'prediction_status': aiq_view.get('status'),
        'prediction_classification': aiq_view.get('classification'),
        'prediction_pulse': aiq_view.get('pulse'),
        'risk_contract': {
            'formula': 'predicted_mfe / abs(predicted_mae)',
            'min_pl_ratio': 1.1,
            'min_up_prob': 0.50,
            'min_right_score': 60
        }
    }
    return _make_envelope(quant_data, freshness, warnings=warnings, status=status)


# =====================================================================
# API 3: GET /api/v7/paper (100 万独立虚拟盘、持仓、流水与净值曲线)
# =====================================================================
@v7_router.get("/paper")
def get_v7_paper():
    warnings = []
    status = "OK"
    context = _active_season_context_or_warning(warnings)
    if context is None:
        return _make_envelope({'account_id': None, 'capital_summary': None, 'positions': None,
                               'recent_orders': [], 'recent_fills': [], 'recent_closed_trades': [], 'equity_curve': []},
                              {'freshness_status': 'UNAVAILABLE'}, warnings=warnings, status='DEGRADED')
    paper_root = context['ai_quant_paper_root']
    acc_p = os.path.join(paper_root, 'account.json')
    acc = _strict_active_json(acc_p, dict, warnings, 'active AI Quant account')
    freshness = _get_file_freshness(acc_p)
    positions = _strict_active_json(os.path.join(paper_root, 'positions.json'), list, warnings, 'active AI Quant positions')
    if acc is None or positions is None:
        status = 'DEGRADED'
    orders = _tail_jsonl(os.path.join(paper_root, 'orders.jsonl'), limit=30)
    fills = _tail_jsonl(os.path.join(paper_root, 'fills.jsonl'), limit=30)
    closed_trades = _tail_jsonl(os.path.join(paper_root, 'paper_trade_results.jsonl'), limit=30)
    equity_curve = _tail_jsonl(os.path.join(paper_root, 'equity_curve.jsonl'), limit=100)

    paper_data = {
        'season_id': context['season_id'],
        'account_id': acc.get('account_id') if acc else None,
        'capital_summary': {
            'initial_capital': acc.get('initial_capital') if acc else None,
            'cash': acc.get('cash') if acc else None,
            'market_value': acc.get('market_value') if acc else None,
            'total_equity': acc.get('total_equity') if acc else None,
            'net_pnl': acc.get('net_pnl') if acc else None,
            'net_pnl_pct': acc.get('net_pnl_pct') if acc else None,
            'peak_equity': acc.get('peak_equity') if acc else None,
            'max_drawdown_pct': acc.get('max_drawdown_pct') if acc else None
        },
        'positions': positions,
        'recent_orders': orders,
        'recent_fills': fills,
        'recent_closed_trades': closed_trades,
        'equity_curve': equity_curve
    }
    return _make_envelope(paper_data, freshness, warnings=warnings, status=status)


# =====================================================================
# API 4: GET /api/v7/forward (真实前向观测数据、天数计数与每日研报清单)
# =====================================================================
@v7_router.get("/forward")
def get_v7_forward():
    warnings = []
    status = "OK"
    context = _active_season_context_or_warning(warnings)
    if context is None:
        return _make_envelope({'forward_epoch_id': None, 'forward_day_count': None, 'observation_status': 'UNAVAILABLE',
                               'is_genuine_forward': False, 'daily_reports_list': [], 'recent_telemetry': []},
                              {'freshness_status': 'UNAVAILABLE'}, warnings=warnings, status='DEGRADED')
    manifest_p = os.path.join(context['forward_root'], 'forward_epoch_manifest.json')
    manifest = _strict_active_json(manifest_p, dict, warnings, 'active Forward manifest')
    freshness = _get_file_freshness(manifest_p)
    if manifest is None:
        status = 'DEGRADED'
        manifest, day_count = {}, None
    else:
        day_count = manifest.get('forward_day_count')

    # 扫描已生成的每日研报列表 (仅限白名单前缀 PHASE18F_FORWARD_)
    daily_reports = []
    fwd_reports_dir = os.path.join(context['forward_root'], 'reports')
    if os.path.exists(fwd_reports_dir):
        for fn in sorted(os.listdir(fwd_reports_dir)):
            if fn.startswith('PHASE18F_FORWARD_') and fn.endswith('.md'):
                td_str = fn.replace('PHASE18F_FORWARD_', '').replace('.md', '')
                daily_reports.append({
                    'report_name': fn,
                    'trade_date': td_str,
                    'file_size': os.path.getsize(os.path.join(fwd_reports_dir, fn))
                })

    # 读取遥测流水 (最近 20 条)
    telemetry_records = []
    telem_dir = os.path.join(context['forward_root'], 'telemetry')
    if os.path.exists(telem_dir):
        for t_fn in sorted(os.listdir(telem_dir))[-3:]:
            telemetry_records.extend(_tail_jsonl(os.path.join(telem_dir, t_fn), limit=10))

    forward_data = {
        'season_id': context['season_id'],
        'forward_epoch_id': manifest.get('forward_epoch_id'),
        'baseline_freeze_timestamp': manifest.get('baseline_timestamp'),
        'forward_day_count': day_count,
        'observation_status': manifest.get('status', 'UNAVAILABLE'),
        'forward_enabled': manifest.get('forward_enabled') if isinstance(manifest.get('forward_enabled'), bool) else None,
        'genuine_forward_started': manifest.get('genuine_forward_started') if isinstance(manifest.get('genuine_forward_started'), bool) else None,
        'required_gates': manifest.get('required_gates') if isinstance(manifest.get('required_gates'), dict) else None,
        'gate_b_status': _read_gate_status(),
        'is_genuine_forward': bool(manifest.get('genuine_forward_started') and manifest.get('status') == 'ACTIVE'),
        'stop_execution_gap_stats': {
            'mean_gap_pct': None,
            'worst_gap_pct': None
        },
        'risk_underprediction_stats': {
            'underprediction_rate_pct': None,
            'overprediction_rate_pct': None
        },
        'daily_reports_list': daily_reports,
        'recent_telemetry': telemetry_records
    }
    return _make_envelope(forward_data, freshness, warnings=warnings, status=status)


# =====================================================================
# API 5: GET /api/v7/trade-plans (只读交易计划合同库)
# =====================================================================
@v7_router.get("/trade-plans")
def get_v7_trade_plans(status_filter: Optional[str] = Query(None, description="ACTIVE 或 CLOSED"),
                       symbol_filter: Optional[str] = Query(None, description="股票代码"),
                       limit: int = Query(50, ge=1, le=100)):
    warnings = []
    status = "OK"

    plans = []
    try:
        with sqlite3.connect(f"file:{MARKET_DB_PATH}?mode=ro", uri=True) as conn:
            cursor = conn.cursor()
            query = """
                SELECT plan_id, competition_id, player_id, symbol, name,
                       signal_time, entry_price, position_size, stop_loss, stop_loss_pct,
                       take_profit, take_profit_pct, expected_holding_days,
                       up_probability, expected_return, expected_mae, right_side_score,
                       model_version, is_frozen, status
                FROM ai_quant_trade_plans
                WHERE 1=1
            """
            params = []
            if status_filter:
                query += " AND status = ?"
                params.append(status_filter.upper())
            if symbol_filter:
                query += " AND symbol = ?"
                params.append(str(symbol_filter).zfill(6))

            query += " ORDER BY signal_time DESC LIMIT ?"
            params.append(limit)

            rows = cursor.execute(query, tuple(params)).fetchall()
            for r in rows:
                plans.append({
                    'plan_id': r[0], 'competition_id': r[1], 'player_id': r[2],
                    'symbol': r[3], 'name': r[4], 'signal_time': r[5],
                    'entry_price': r[6], 'position_size': r[7], 'stop_loss': r[8],
                    'stop_loss_pct': r[9], 'take_profit': r[10], 'take_profit_pct': r[11],
                    'expected_holding_days': r[12], 'up_probability': r[13],
                    'expected_return': r[14], 'predicted_mae': r[15], 'right_side_score': r[16],
                    'model_version': r[17], 'is_frozen': r[18], 'status': r[19],
                    'season_id': None, 'season_qualification': 'UNVERIFIED',
                    'order_status': 'NOT_INFERRED_FROM_PLAN', 'fill_status': 'NOT_INFERRED_FROM_PLAN'
                })
    except Exception as e:
        warnings.append(f"只读查询 TradePlans 异常: {e}")
        status = "DEGRADED"

    freshness = _get_file_freshness(MARKET_DB_PATH)
    return _make_envelope({'total_records': len(plans), 'plans': plans}, freshness, warnings=warnings, status=status)


# =====================================================================
# API 6: GET /api/v7/arena (七选手量化大乱斗与排行榜)
# =====================================================================
@v7_router.get("/arena")
def get_v7_arena():
    from daemon.v7_arena import build_arena_view
    warnings = []
    context = _active_season_context_or_warning(warnings)
    if context is None:
        result = _make_envelope({'season_id': None, 'leaderboard': [], 'players_count': None},
                                {'freshness_status': 'UNKNOWN', 'stale': True}, warnings, 'DEGRADED')
    else:
        result = build_arena_view(context)
    return JSONResponse(result, headers={'Cache-Control': 'no-store, max-age=0'})


@v7_router.get("/arena/{participant_id}")
def get_v7_arena_account(participant_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    from daemon.v7_arena import build_arena_view
    warnings = []
    context = _active_season_context_or_warning(warnings)
    if context is None:
        return JSONResponse(_make_envelope(None, {'freshness_status': 'UNKNOWN', 'stale': True}, warnings, 'DEGRADED'),
                            headers={'Cache-Control': 'no-store, max-age=0'})
    allowed = {p['participant_id'] for p in context['manifest'].get('participants', [])
               if p.get('return_bearing') and p.get('ranking_enabled')}
    if participant_id not in allowed:
        raise HTTPException(404, 'Unknown ranking participant')
    result = build_arena_view(context, participant_id=participant_id, offset=offset, limit=limit)
    return JSONResponse(result, headers={'Cache-Control': 'no-store, max-age=0'})


# =====================================================================
# API 7: GET /api/v7/research (研报清单与正文白名单只读读取)
# =====================================================================
@v7_router.get("/research")
def get_v7_research(report_id: Optional[str] = Query(None, description="报告唯一标识")):
    # 场景 A: 请求报告列表
    if not report_id:
        report_list = []
        for rid, fn in RESEARCH_REPORTS_WHITELIST.items():
            fp = os.path.join(REPORTS_DIR, fn)
            if os.path.exists(fp):
                report_list.append({
                    'report_id': rid,
                    'file_name': fn,
                    'file_size': os.path.getsize(fp),
                    'updated_at': datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S')
                })
        freshness = {'source_timestamp': rc.now_str(), 'age_seconds': 0.0, 'freshness_status': 'LIVE'}
        return _make_envelope({'reports_count': len(report_list), 'reports': report_list}, freshness)

    # 场景 B: 读取指定报告正文 (严格白名单防路径穿越)
    clean_rid = str(report_id).strip().lower()
    if clean_rid not in RESEARCH_REPORTS_WHITELIST:
        raise HTTPException(status_code=404, detail=f"报告未在安全白名单中: {report_id}")

    target_fn = RESEARCH_REPORTS_WHITELIST[clean_rid]
    target_fp = os.path.join(REPORTS_DIR, target_fn)

    # 物理防穿越安全检查
    real_target = os.path.realpath(target_fp)
    real_dir = os.path.realpath(REPORTS_DIR)
    if not real_target.startswith(real_dir):
        raise HTTPException(status_code=403, detail="非法路径穿越访问拒绝！")

    if not os.path.exists(target_fp):
        raise HTTPException(status_code=404, detail=f"报告文件不存在: {target_fn}")

    try:
        with open(target_fp, 'r', encoding='utf-8') as f:
            content = f.read()
        freshness = _get_file_freshness(target_fp)
        data_payload = {
            'report_id': clean_rid,
            'file_name': target_fn,
            'markdown_content': content
        }
        return _make_envelope(data_payload, freshness)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取报告正文失败: {e}")


# =====================================================================
# API 8: GET /api/v7/system (系统状态、看门狗健康度与参数版本日志)
# =====================================================================
@v7_router.get("/system")
def get_v7_system():
    warnings = []
    status = "OK"

    context = _active_season_context_or_warning(warnings)
    if context is None:
        safety_view = {'status': 'UNAVAILABLE', 'halt_status': 'UNKNOWN',
                       'execution_state_safe': None, 'new_entry_disabled': None,
                       'risk_exit_enabled': None, 'trading_enabled': None,
                       'real_trading': None, 'halt_reasons': None}
    else:
        safety_view = _active_safety_view(context, warnings)
    if safety_view['status'] != 'VERIFIED':
        status = 'DEGRADED'

    # 看门狗与心跳 (统一权威源: rc.ALIVE_PATH -> state/daemon_alive.json)
    alive = _load_json_cached(rc.ALIVE_PATH, {})
    hb = _load_json_cached(HEARTBEAT_PATH, {})
    freshness = _get_file_freshness(HEARTBEAT_PATH)

    now = time.time()
    alive_age_sec = round(now - float(alive['epoch']), 1) if alive.get('epoch') else None
    business_health = _business_health_view(alive, hb, warnings)
    if business_health['business_status'] != 'HEALTHY':
        status = 'DEGRADED'

    # 模型生产凭证
    manifest = _load_json_cached(MODELS_MANIFEST_PATH, {})

    system_data = {
        'daemon_health': {
            'healthy': None,
            'business_status': business_health['business_status'],
            'daemon_liveness': business_health['daemon_liveness'],
            'pid': alive.get('pid'),
            'alive_age_seconds': alive_age_sec,
            'last_heartbeat': hb.get('timestamp')
        },
        'system_invariants': {
            'parameters_frozen': None,
            'observation_mode': 'UNKNOWN',
            'execution_mode': 'PAPER_SIMULATION',
            'real_trading_allowed': safety_view['real_trading'],
            'research_validated': None,
            'governance': safety_view,
            'season_id': context.get('season_id') if context else None,
        },
        'production_artifacts': {
            'model_sha256': manifest.get('model', {}).get('sha256'),
            'calibrator_sha256': manifest.get('calibrator', {}).get('sha256'),
            'model_version': manifest.get('model', {}).get('model_version'),
            'promotion_time': manifest.get('promotion_timestamp')
        },
        'frozen_risk_parameters': {
            'risk_reward_formula': 'predicted_mfe / abs(predicted_mae)',
            'min_pl_ratio': 1.1,
            'max_positions': 3,
            'max_single_position_ratio': 0.30,
            'max_equity_ratio': 0.90,
            'commission_rate': 0.0003,
            'stamp_tax_rate': 0.0005,
            'simulated_slippage': 0.0010
        }
    }
    return _make_envelope(system_data, freshness, warnings=warnings, status=status)
