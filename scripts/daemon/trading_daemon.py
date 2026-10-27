# -*- coding: utf-8 -*-
"""
scripts/daemon/trading_daemon.py
常驻调度守护进程（阶段一改造核心）

职责（治"实时性差"的根子）：
1. 脱离 WorkBuddy 宿主 automation，用 APScheduler 在本机常驻执行全部定时任务，
   时间表在 config/schedule.yaml 中配置化（借鉴 day_run 设计）；
2. 容错机制：
   - coalesce + misfire_grace：错过一个时点只补最新一次；
   - catchup + skip_stale_minutes：开机过晚的任务若仍在有效窗口内则启动即补跑；
   - market_open_only：盘中任务自动避开午休/周末/节假日；
   - 连续失败计数达阈值 → 企微 force 告警；
3. 每次任务成功写心跳 state/heartbeat.json，每分钟写活体 state/daemon_alive.json
   （供独立看门狗 watchdog.py 判断进程是否假死）；
4. 内嵌 FastAPI Web 服务（见 app.py），点开程序即有实时工作台。

手动单跑某任务（调试/验收用）：
    python -m scripts.daemon.trading_daemon --once intraday_cycle
"""

import os
import sys
import json
import time
import argparse
import datetime
import threading
import traceback
from zoneinfo import ZoneInfo

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_BASE, os.path.join(_BASE, 'scripts'), os.path.join(_BASE, 'scripts', 'pipeline'),
          os.path.join(_BASE, 'scripts', 'notify'), os.path.join(_BASE, 'scripts', 'datasource')):
    if p not in sys.path:
        sys.path.insert(0, p)

import runtime_config as rc
from competition.season_runtime import startup_runtime_gate

try:
    import yaml
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError as e:
    print('缺少依赖:', e, '→ 请使用项目 .venv 运行')
    sys.exit(1)

FAIL_STATE_PATH = os.path.join(rc.STATE_DIR, 'job_failures.json')

# ---------------------------------------------------------------- 心跳与失败账本

def write_heartbeat(job=None, detail=''):
    hb = rc._load_json(rc.HEARTBEAT_PATH, {})
    hb['updated_at'] = rc.now_str()
    hb['pid'] = os.getpid()
    if job:
        hb.setdefault('jobs', {})
        hb['jobs'][job] = {
            'last_finished': rc.now_str(),
            'result': (detail or '')[:160],
        }
    rc._save_json(rc.HEARTBEAT_PATH, hb)


def mark_alive():
    rc._save_json(rc.ALIVE_PATH, {
        'schema_version': 'v7.1',
        'process': 'trading_daemon',
        'pid': os.getpid(),
        'ts': rc.now_str(),
        'epoch': time.time(),
        'heartbeat_at': rc.now_str(),
        'status': 'RUNNING'
    })


def _load_failures():
    return rc._load_json(FAIL_STATE_PATH, {})


def _save_failures(st):
    rc._save_json(FAIL_STATE_PATH, st)


# ---------------------------------------------------------------- 任务包装器

def run_with_guard(name, fn, structured_hold=None):
    """统一任务执行器：计时、异常捕获、连续失败告警、心跳回写。

    DATA_MUTATING jobs may return a structured HOLD without raising.  That is
    a valid, audited data-health result rather than a successful completion or
    a Python failure.  Keep it on a separate heartbeat channel and preserve
    the failure counter; normal PASS and exception behavior remain unchanged.
    """
    if structured_hold is None:
        structured_hold = classify_runtime_job(name) == 'DATA_MUTATING'
    t0 = time.time()
    print(f'[守护] ▶ {name} 开始 @ {rc.now_str()}')
    try:
        detail = fn() or 'OK'
        dt = time.time() - t0
        if structured_hold and isinstance(detail, dict) and detail.get('status') == 'HOLD':
            reason_codes = detail.get('reason_codes') or ['DATA_JOB_HOLD']
            hold_detail = json.dumps({
                'status': 'HOLD',
                'reason_codes': reason_codes,
                'detail': detail,
                'elapsed_seconds': round(dt, 3),
            }, ensure_ascii=False, separators=(',', ':'))
            print(f'[守护] ⏸ {name} HOLD ({dt:.1f}s): {hold_detail}')
            try:
                write_heartbeat(f'{name}:hold', hold_detail[:160])
            except Exception as heartbeat_error:
                # Observability failure must not turn a data HOLD into PASS
                # or into a synthetic Python task failure.
                print(f'[守护] {name} HOLD 心跳写入失败: {heartbeat_error}')
            return True, detail
        print(f'[守护] ✔ {name} 完成 ({dt:.1f}s): {detail}')
        st = _load_failures()
        if st.get(name, {}).get('count'):
            st[name]['count'] = 0
            _save_failures(st)
        write_heartbeat(name, f'{detail} ({dt:.1f}s)')
        return True, detail
    except Exception as e:
        dt = time.time() - t0
        err = f'{type(e).__name__}: {e}'
        tb = traceback.format_exc(limit=6)
        print(f'[守护] ✘ {name} 失败 ({dt:.1f}s): {err}\n{tb}')
        st = _load_failures()
        ent = st.get(name, {'count': 0})
        ent['count'] += 1
        ent['last_error'] = f'{rc.now_str()} {err}'[:300]
        st[name] = ent
        _save_failures(st)
        threshold = 3
        if ent['count'] >= threshold:
            from notify.wecom_bot import push
            push(f'任务[{name}]已连续失败{ent["count"]}次',
                 [ent['last_error'], '请打开工作台健康页排查'], level='alert', force=True)
            ent['count'] = 0          # 告警一次后重新计数，避免重复轰炸
            _save_failures(st)
        write_heartbeat(name, f'FAILED: {err}'[:160])
        return False, err


# ---------------------------------------------------------------- 内置任务实现

def _archive_news_from_brief():
    """把通政奏报里的真实快讯归档进 SQLite（去重幂等）。"""
    brief_path = os.path.join(rc.DATA_DIR, 'pipeline', 'news_brief.json')
    brief = rc._load_json(brief_path, None)
    if not brief:
        return 'brief缺失,无新闻可归档'
    news = brief.get('news') or brief.get('hotspots') or []
    items = [{
        'title': n.get('title', ''), 'content': n.get('content', ''),
        'time': n.get('time', ''), 'source': n.get('source', ''),
        'url': n.get('url', ''), 'category': n.get('category', ''),
        'subj_type': n.get('subj_type', ''), 'entity': n.get('entity', ''),
    } for n in news]
    from datasource import db as history_db
    total, new = history_db.insert_news(items)
    return f'新闻入库 新增{new}/{total}'


def _record_data_job_health(context, trade_date, completeness_result, reason_codes=None):
    """Persist a traceable DATA-domain HOLD without touching economic safety."""
    from datasource.completeness import atomic_json_write

    completeness_root = os.path.join(context['season_root'], 'market_data_completeness')
    date_text = str(trade_date).replace('-', '')
    reasons = list(reason_codes or [])
    if not reasons and isinstance(completeness_result, dict):
        reasons = list(completeness_result.get('failure_reasons') or [])
    health = {
        'schema_version': 'v7-g6-market-data-health-v1',
        'season_id': context.get('season_id'),
        'trade_date': date_text,
        'status': 'HOLD',
        'generated_at': datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'completeness_status': completeness_result.get('status') if isinstance(completeness_result, dict) else 'HOLD',
        'reason_codes': reasons or ['DAILY_MARKET_COMPLETENESS_HOLD'],
        'evidence_path': os.path.join(completeness_root, f'{date_text}.json'),
        'economic_execution_impact': 'NONE',
        'operational_alert': True,
    }
    health_path = os.path.join(completeness_root, 'health', f'{date_text}.json')
    atomic_json_write(health_path, health)
    detail = json.dumps(health, ensure_ascii=False, separators=(',', ':'))
    write_heartbeat('daily_review:data_health', detail)
    try:
        from notify.wecom_bot import push
        push(f'G6行情完整性HOLD: {date_text}', health['reason_codes'],
             level='alert', force=True)
    except Exception as exc:
        print(f'[daily_review] G6 操作告警发送失败: {exc}')
    return health_path, health


def precheck_briefing():
    """09:25 开市预检 + 每日第一条盘前简报。"""
    from notify.wecom_bot import daily_brief
    from datasource import providers as prov
    lines = []
    q = prov.get_realtime_quotes(['sh000001', 'sz399001', 'sz399006'])
    sh = q.get('sh000001')
    lines.append(f'上证指数(昨收) {sh["price"] if sh else "获取失败"}' if sh else '⚠ 行情接口拉取失败')
    from datasource import db as history_db
    n_stk, lo, hi = history_db.kline_coverage()
    ns = history_db.recent_news(limit=5)
    sim = rc._load_json(os.path.join(rc.DATA_DIR, 'simulation_state.json'), {}) \
        .get('accounts', {})
    total_pnl = sum(a.get('pnl_pct', 0) for a in sim.values()) / max(len(sim), 1)
    lines.append(f'历史库覆盖 {n_stk} 只 ({lo}~{hi})')
    if ns:
        lines.append('隔夜要闻TOP: ' + ns[0]['title'][:40])
    lines.append(f'模拟盘平均收益 {total_pnl:+.2f}% | 系统就绪,9:30开盘')
    ok, detail = daily_brief(lines)
    return f'简报推送={"成功" if ok else detail}'


def intraday_cycle():
    """盘中核心周期：朝廷流水线全链 + 新闻归档 + 卖出五级监控。"""
    results = []

    # 0) LLM 闸门（阶段四降频）：距上次深度研判不足 N 分钟 → 本轮 LLM 待机，
    #    行情/新闻归档照常。强信号检测（批量评分）不受闸门限制。
    try:
        gate = rc._load_json(os.path.join(_BASE, 'state', 'llm_gate.json'), {}) or {}
        try:
            min_iv = int(load_schedule().get('llm_min_interval_min', 15))
        except Exception:
            min_iv = 15
        last = gate.get('last_llm_run', '')
        gate_open = True
        if last:
            lt = datetime.datetime.strptime(last[:19], '%Y-%m-%d %H:%M:%S')
            if (datetime.datetime.now() - lt).total_seconds() < min_iv * 60:
                gate_open = False
        os.environ['NEWS_LLM_GATE'] = 'open' if gate_open else 'closed'
        if not gate_open:
            results.append(f'LLM闸门=待机(<{min_iv}分钟)')
    except Exception:
        os.environ['NEWS_LLM_GATE'] = 'open'

    # 1) 流水线全链（通政司→驿传→六部→起居注→都察院）
    sys.path.insert(0, os.path.join(_BASE, 'scripts', 'pipeline'))
    import orchestrator
    overall, rep = orchestrator.run_full_pipeline(trigger='daemon')
    results.append(f'流水线={overall}')
    if overall == 'FAILED':
        raise RuntimeError('run_full_pipeline 整体失败')

    # 2) 快讯归档 SQLite
    results.append(_archive_news_from_brief())

    # 3) 卖出五级监控（仅盘中且模拟盘有持仓才有意义）
    try:
        from analysis.sell_monitor import scan_positions
        r = scan_positions()
        results.append(f'卖出监控:{r}')
    except Exception as e:
        # 监控失败不推翻本轮数据成果，但要留痕
        results.append(f'卖出监控异常:{type(e).__name__}')

    # 3.5) 阶段六·盘中实时执行：契约止盈/止损/追踪 + 选股买入（时段闸门+锁+防重复内置）
    try:
        from simulated_trading import execute_intraday
        r = execute_intraday()
        results.append(f'盘中执行:{r}')
    except Exception as e:
        results.append(f'盘中执行异常:{type(e).__name__}')

    # 3.6) G3 Forward branch: the Start Gate must pass before importing or
    # instantiating the runner. HOLD is diagnostic-only and writes no telemetry.
    results.append(forward_runtime_branch())

    return ' | '.join(results)


def forward_runtime_branch(context=None, current_timestamp=None):
    """Gate the Forward runner without allowing legacy or HOLD execution."""
    try:
        from ai_quant.forward_start_gate import evaluate_forward_start_gate
        gate = evaluate_forward_start_gate(context=context, current_timestamp=current_timestamp)
        if not gate.get('pass'):
            return 'AI-Quant Forward=HOLD:' + ','.join(gate.get('reason_codes', []))
        from ai_quant.forward_runner import execute_forward_paper_cycle
        return execute_forward_paper_cycle(context=context, current_timestamp=current_timestamp)
    except Exception as exc:
        return f'AI-Quant Forward=HOLD:FORWARD_GATE_ERROR:{type(exc).__name__}'


def close_settlement():
    """15:08 收盘结算：以收盘价复核止盈止损 + L4/L5兜底 + 收盘简报推送（阶段六·职能收缩）。"""
    from notify.wecom_bot import close_brief
    import simulated_trading as st
    # 阶段六：15:08 仍是 allow_trading=True 的唯一入口之一（price_type='close'）
    summary = st.run_simulation(force_picks_scan=False, reset_to_mainboard=False,
                                allow_trading=True, price_type='close')

    accs = summary.get('accounts', {})
    lines = []
    ranked = sorted(accs.values(), key=lambda a: -a.get('pnl_pct', 0))
    for a in ranked:
        lines.append(f'{a["blogger_name"]}: {a["total_value"]:,.0f}元 '
                     f'({a["pnl_pct"]:+.2f}%) 持仓{len(a.get("positions", []))}只')
    trade_cnt = sum(len(v.get("trades", [])) for v in accs.values())
    lines.append(f'累计交易 {trade_cnt} 笔 · 账本SHA256已锁存')
    ok, detail = close_brief(lines)

    # G2.6: active-season strategy ledgers + AIQ + derived Arena snapshot.
    # The legacy global HALT and data/trades are never used as active inputs.
    rec_msg = '对账未执行'
    try:
        from competition.season_runtime import (
            resolve_active_season_context,
            resolve_daily_reconciliation_path,
            run_daily_reconciliation,
        )
        from ai_quant.storage_utils import atomic_write_json
        context = resolve_active_season_context()
        rec_report = run_daily_reconciliation(context)
        completed_at = datetime.datetime.now(ZoneInfo('Asia/Shanghai'))
        rec_report_path = resolve_daily_reconciliation_path(context)
        atomic_write_json(rec_report_path, {
            'status': rec_report.get('status'),
            'overall': {'status': rec_report.get('status')},
            'strategy': {'status': rec_report.get('strategy', {}).get('status'),
                         'findings': rec_report.get('strategy', {}).get('findings', [])},
            'ai_quant': {'status': rec_report.get('ai_quant', {}).get('status'),
                         'findings': rec_report.get('ai_quant', {}).get('findings', [])},
            'arena': {'status': rec_report.get('arena', {}).get('status'),
                      'findings': rec_report.get('arena', {}).get('findings', [])},
            'findings': rec_report.get('findings', []),
            'season_id': context['season_id'],
            'trade_date': completed_at.strftime('%Y%m%d'),
            'completed_at': completed_at.isoformat(),
            'updated_at': completed_at.isoformat(),
        })
        rec_status = rec_report.get('status', 'UNKNOWN')
        rec_msg = f"active-season对账={rec_status}"
        if rec_status != 'PASS':
            startup_runtime_gate(update_safety=True)
            print('[close_settlement] active-season reconciliation failed; entry remains disabled')
    except Exception as e:
        rec_msg = f"active-season对账异常:{type(e).__name__}"
        print(f"[close_settlement] 每日对账执行异常: {e}")
        # Fail-safe in the active season only; legacy state/governance_halt.json
        # is historical and must remain untouched.
        startup_runtime_gate(update_safety=True)

    return f'结算完成 推送={"成功" if ok else detail} | {rec_msg}'


def daily_review():
    return _daily_review()


def daily_data_recovery():
    """DATA_MUTATING only; current completed date, bounded and resumable."""
    from datasource.daily_recovery import recover_backlog
    result = recover_backlog()
    write_heartbeat('daily_data_recovery:result', json.dumps(result, ensure_ascii=False))
    return result


def _update_aiq_cache_after_g6(context, trade_date, capture_root):
    """Separate AIQ admission outcome; never rewrite G6 or economic safety."""
    from types import SimpleNamespace
    from datasource.completeness import atomic_json_write
    try:
        from ai_quant.cache_rollover import run, ROOT
        result = run(SimpleNamespace(
            trade_date=trade_date, capture_root=capture_root,
            output_root=ROOT / 'data/ai_quant/input_cache',
            collect_factors=True, factor_bundle=None, max_protocol_calls=20,
            cumulative_protocol_limit=0, daily_operations=True, publish=True))
    except Exception as exc:
        result = {'status': 'HOLD', 'reason': str(exc),
                  'error_type': type(exc).__name__, 'economic_writes': 0}
    result = dict(result, trade_date=trade_date, season_id=context['season_id'],
                  G6_status='PASS', domain='AIQ_CACHE_UPDATE',
                  generated_at=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
    try:
        root = os.path.join(context['season_root'], 'aiq_input_admission')
        name = datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S%f')
        path = os.path.join(root, trade_date, name + '.json')
        atomic_json_write(path, result)
        atomic_json_write(os.path.join(root, 'latest.json'), dict(result, receipt_path=path))
        write_heartbeat('daily_review:aiq_cache_update', json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        result['audit_write_warning'] = type(exc).__name__ + ':' + str(exc)
        print('[daily_review] AIQ cache audit warning:', exc)
    return result


def _daily_review():
    """16:30 收盘后：目标交易日完整性同步 + 数据健康简述。

    G6 requires a date-frozen candidate universe and database revalidation;
    this data job must not treat a non-empty historical response as success.
    """
    from datasource import db as history_db
    recovery = daily_data_recovery()
    if recovery.get('status') != 'PASS':
        return recovery
    target_trade_date = recovery['target_date']
    days = recovery.get('dates', [])
    res = days[-1]['data'] if days else {'status': 'ALREADY_VALID'}
    aiq_cache = days[-1]['aiq_cache'] if days else {'status': 'ALREADY_QUALIFIED_NO_WRITE'}
    n_stk, lo, hi = history_db.kline_coverage()

    # v4.1 A2：pos_neg_diff 挂 daily_review（防死数据）
    pn_msg = ''
    try:
        from analysis.event_factors import pos_neg_diff
        pn = pos_neg_diff()
        if pn:
            pn_msg = ' | 正负差90日均值已更新'
    except Exception as e:
        print(f'[daily_review] pos_neg_diff 失败: {e}')

    # v4.1 A3：因子存量周报（仅周五）
    factor_msg = ''
    try:
        import sqlite3
        dbp = os.path.normpath(os.path.join(BASE, 'data', 'market', 'market.db'))
        if os.path.exists(dbp):
            conn = sqlite3.connect(dbp)
            row = conn.execute("SELECT COUNT(*), MIN(published_at) FROM event_factor").fetchone()
            conn.close()
            n_total, lo_dt = row
            bsp = os.path.join(BASE, 'state', 'factor_baseline.json')
            base = rc._load_json(bsp, {})
            prev = base.get('count', 0)
            base['count'] = n_total
            base['last_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            os.makedirs(os.path.dirname(bsp), exist_ok=True)
            with open(bsp, 'w', encoding='utf-8') as f:
                json.dump(base, f, ensure_ascii=False, indent=2)
            # 逢周五 + 因子有增长 才追加报告
            if datetime.datetime.now().weekday() == 4 and n_total > prev:
                delta = n_total - prev
                try:
                    dt0 = datetime.datetime.strptime(lo_dt[:10], '%Y-%m-%d')
                    days_so_far = max((datetime.datetime.now() - dt0).days, 0)
                    days_left = max(14 - days_so_far, 0)
                except Exception:
                    days_left = -1
                left_msg = f'，距统计进化启动还差{days_left}天' if days_left > 0 else '，已满足统计进化启动条件'
                factor_msg = f' | 事件因子存量{n_total}条(周增{delta}){left_msg}'
                # 企微推送（未配置时静默）
                try:
                    from notify.wecom_bot import daily_brief
                    daily_brief([f'事件因子周报: 存量{n_total}条(周增{delta}){left_msg}'])
                except Exception:
                    pass
    except Exception as e:
        print(f'[daily_review] 因子存量周报失败: {e}')

    # 阶段五·任务F：事件因子权重审计与月度系统体检生成
    checkup_msg = ''
    try:
        from analysis.factor_weight_audit import run_factor_weight_audit
        from analysis.system_checkup import generate_system_checkup
        audit_res = run_factor_weight_audit()
        checkup_res = generate_system_checkup()
        checkup_msg = f' | 体检状态: {audit_res.get("status")}'
    except Exception as e:
        print(f'[daily_review] 因子权重审计/体检生成提示: {e}')

    return {
        'status': 'PASS',
        'reason_codes': [],
        'trade_date': target_trade_date,
        'completeness': res,
        'aiq_cache_update': aiq_cache,
        'coverage': {'symbols': n_stk, 'earliest': lo, 'latest': hi},
        'messages': [pn_msg, factor_msg, checkup_msg],
    }


def news_refresh():
    """7x24 新闻刷新（用户 2026-08-29 反馈：周末/盘后新闻频道不更新）。
    行情可以收盘，新闻不停——每20分钟拉一轮流水线；盘中自动跳过（intraday_cycle 5分钟已覆盖）。"""
    if rc.market_phase() == 'open':
        return 'skip(盘中由intraday_cycle每5分钟覆盖)'
    sys.path.insert(0, os.path.join(_BASE, 'scripts', 'pipeline'))
    import orchestrator
    overall, rep = orchestrator.run_full_pipeline(trigger='daemon_news')
    if overall == 'FAILED':
        raise RuntimeError('news_refresh 流水线失败')
    r = _archive_news_from_brief()
    return f'新闻刷新=PASS | {r}'


BUILTINS = {
    'precheck_briefing': precheck_briefing,
    'intraday_cycle': intraday_cycle,
    'news_refresh': news_refresh,
    'close_settlement': close_settlement,
    'daily_review': daily_review,
    'daily_data_recovery': daily_data_recovery,
}

# One policy table is the source of truth for every runtime entry point.  The
# business functions remain unchanged; this table only decides which safety
# preflight must run before they can be called.
RUNTIME_JOB_POLICY = {
    'precheck_briefing': 'INFORMATIONAL',
    'intraday_cycle': 'ECONOMIC_MUTATING',
    'news_refresh': 'INFORMATIONAL',
    'close_settlement': 'ECONOMIC_MUTATING',
    'daily_review': 'DATA_MUTATING',
    'daily_data_recovery': 'DATA_MUTATING',
}

MUTATING_RUNTIME_JOBS = {
    name for name, classification in RUNTIME_JOB_POLICY.items()
    if classification != 'INFORMATIONAL'
}
MUTATING_RUNTIME_JOBS.add('daemon_boot')


def classify_runtime_job(job_name):
    """Return the explicit runtime policy classification for a builtin."""
    if job_name == 'daemon_boot':
        return 'DAEMON_BOOT'
    return RUNTIME_JOB_POLICY.get(job_name)


def runtime_job_preflight(job_name):
    """Dispatch each runtime job to its domain-specific authority."""
    classification = classify_runtime_job(job_name)
    if classification is None:
        return False, {
            'status': 'FAIL', 'pass': False, 'job': job_name,
            'findings': ['UNKNOWN_RUNTIME_JOB'],
        }
    if classification == 'INFORMATIONAL':
        return True, {'status': 'BYPASS_READ_ONLY', 'job': job_name}
    if classification == 'DATA_MUTATING':
        return data_runtime_preflight(job_name)
    result = startup_runtime_gate(update_safety=True)
    return bool(result.get('pass')), result


def data_runtime_preflight(job_name):
    """Validate data-maintenance prerequisites without economic reconciliation.

    This authority intentionally does not inspect strategy/AIQ/Arena ledger
    consistency.  Data maintenance must remain available for recovery and G6
    evidence work even when an unrelated economic ledger is unhealthy.
    """
    classification = classify_runtime_job(job_name)
    findings = []
    context = None
    if classification != 'DATA_MUTATING':
        findings.append('INVALID_DATA_PREFLIGHT_CLASSIFICATION')
    if not os.path.isdir(rc.DATA_DIR):
        findings.append('DATA_ROOT_UNRESOLVABLE')
    if not os.path.isfile(rc.SCHEDULE_YAML):
        findings.append('SCHEDULE_PATH_UNRESOLVABLE')
    try:
        from competition.season_runtime import resolve_active_season_context, strict_load_json
        context = resolve_active_season_context()
        safety = strict_load_json(context['governance_safety_path'], expected_type=dict)
        if not safety.is_valid:
            findings.append(f'ACTIVE_SAFETY_{safety.status}')
        elif safety.value.get('real_trading') is not False:
            findings.append('REAL_TRADING_INVARIANT_VIOLATION')
    except Exception as exc:
        findings.append(f'DATA_CONTEXT_EXCEPTION:{type(exc).__name__}')
    try:
        from datasource import providers as data_provider
        from datasource import db as data_db
        if not callable(getattr(data_provider, 'sync_universe_daily', None)):
            findings.append('DATA_WRITER_CONTRACT_MISSING')
        if not callable(getattr(data_db, 'kline_coverage', None)):
            findings.append('DATA_HEALTH_READER_MISSING')
    except Exception as exc:
        findings.append(f'DATA_WRITER_IMPORT_EXCEPTION:{type(exc).__name__}')
    passed = not findings
    return passed, {
        'status': 'PASS' if passed else 'FAIL',
        'pass': passed,
        'job': job_name,
        'classification': classification,
        'preflight_domain': 'DATA',
        'season_id': context.get('season_id') if context else None,
        'findings': findings,
    }


def _runtime_local_timestamp(value=None):
    """Normalize a trusted aware timestamp to the exchange timezone.

    An externally injected naive datetime is rejected rather than interpreted
    using the Windows host timezone.  The production default is an aware local
    timestamp in Asia/Shanghai.
    """
    tz = ZoneInfo('Asia/Shanghai')
    if value is None:
        return datetime.datetime.now(tz)
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        return None
    return value.astimezone(tz)


def _close_settlement_window(schedule=None):
    """Resolve the configured close slot and stale window without fallback."""
    schedule = schedule if schedule is not None else load_schedule()
    jobs = schedule.get('jobs') if isinstance(schedule, dict) else None
    if not isinstance(jobs, dict):
        return None
    cfg = jobs.get('close_settlement')
    if not isinstance(cfg, dict):
        return None
    trigger = cfg.get('trigger')
    if not isinstance(trigger, str):
        return None
    parts = trigger.split()
    if len(parts) != 5:
        return None
    try:
        minute = int(parts[0])
        hour = int(parts[1])
        stale_minutes = float(cfg.get('skip_stale_minutes', 0))
    except (TypeError, ValueError):
        return None
    if not (0 <= minute <= 59 and 0 <= hour <= 23 and stale_minutes >= 0):
        return None
    # The current schedule is weekday based; the exchange calendar remains the
    # authority for actual trading-day eligibility.
    return hour, minute, stale_minutes


def runtime_market_session_preflight(job_name, current_timestamp=None,
                                     calendar_path=None, schedule=None):
    """Canonical A-share market-session hard gate for economic jobs.

    This gate is deliberately separate from economic reconciliation and from
    G4 data-maintenance preflight.  It is called by execute_runtime_job, so
    scheduler, catchup, --once, and localhost API share the same decision.
    """
    classification = classify_runtime_job(job_name)
    base = {
        'job': job_name,
        'market': 'A_SHARE',
        'timezone': 'Asia/Shanghai',
        'classification': classification,
        'preflight_domain': 'MARKET_SESSION',
        'allowed': False,
        'pass': False,
        'status': 'HOLD',
        'calendar_valid': False,
        'trading_day': False,
        'session': 'CLOSED',
        'reason_codes': [],
    }
    if classification != 'ECONOMIC_MUTATING':
        base['reason_codes'] = ['SESSION_POLICY_UNKNOWN']
        return base
    timestamp = _runtime_local_timestamp(current_timestamp)
    if timestamp is None:
        base['reason_codes'] = ['INVALID_TIMEZONE_AWARE_TIMESTAMP']
        return base
    base['timestamp'] = timestamp.isoformat()
    base['trade_date'] = timestamp.date().isoformat()
    calendar = rc.trading_calendar_status(timestamp.date(), path=calendar_path)
    if calendar.get('status') != rc.TradingCalendarStatus.VALID:
        base['reason_codes'] = [calendar.get('reason') or 'TRADING_CALENDAR_INVALID']
        return base
    base['calendar_valid'] = True
    base['calendar_version'] = calendar.get('version')
    base['trading_day'] = bool(calendar.get('trading_day'))
    if not base['trading_day']:
        base['reason_codes'] = ['NON_TRADING_DAY']
        return base
    base['session'] = rc.market_phase(timestamp, calendar_path=calendar_path).upper()
    if job_name == 'intraday_cycle':
        if base['session'] != 'OPEN':
            base['reason_codes'] = ['OUTSIDE_INTRADAY_SESSION']
            return base
    elif job_name == 'close_settlement':
        window = _close_settlement_window(schedule=schedule)
        if window is None:
            base['reason_codes'] = ['CLOSE_SETTLEMENT_SCHEDULE_INVALID']
            return base
        hour, minute, stale_minutes = window
        slot = timestamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
        stale_end = slot + datetime.timedelta(minutes=stale_minutes)
        base['close_slot'] = slot.isoformat()
        base['close_stale_end'] = stale_end.isoformat()
        if timestamp < slot:
            base['reason_codes'] = ['BEFORE_CLOSE_SETTLEMENT_WINDOW']
            return base
        if timestamp > stale_end:
            base['reason_codes'] = ['CLOSE_SETTLEMENT_STALE']
            return base
    else:
        base['reason_codes'] = ['SESSION_POLICY_UNKNOWN']
        return base
    base['allowed'] = True
    base['pass'] = True
    base['status'] = 'PASS'
    return base


def _parse_session_verdict_timestamp(value):
    """Parse a session verdict timestamp without accepting host-local time."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    local = parsed.astimezone(ZoneInfo('Asia/Shanghai'))
    # The verdict contract is explicitly emitted in the exchange timezone.
    if local.utcoffset() != datetime.timedelta(hours=8):
        return None
    return local


def validate_market_session_verdict(job_name, verdict, current_timestamp=None,
                                    schedule=None):
    """Strictly validate the producer's market-session PASS verdict.

    A boolean ``pass`` is intentionally insufficient.  Economic execution is
    permitted only when the complete, job-specific A-share session contract
    is present and internally consistent.  The validator is kept at the
    consumer boundary so every scheduler/catchup/once/API caller is covered.
    """
    if not isinstance(verdict, dict):
        return False, ['MARKET_SESSION_GATE_MALFORMED']

    required = {
        'pass': True,
        'allowed': True,
        'status': 'PASS',
        'job': job_name,
        'classification': 'ECONOMIC_MUTATING',
        'market': 'A_SHARE',
        'timezone': 'Asia/Shanghai',
        'calendar_valid': True,
        'trading_day': True,
    }
    for key, expected in required.items():
        if verdict.get(key) != expected or type(verdict.get(key)) is not type(expected):
            return False, ['MARKET_SESSION_GATE_MALFORMED']

    timestamp = _parse_session_verdict_timestamp(verdict.get('timestamp'))
    trade_date = verdict.get('trade_date')
    if timestamp is None or not isinstance(trade_date, str):
        return False, ['MARKET_SESSION_GATE_MALFORMED']
    try:
        parsed_trade_date = datetime.date.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return False, ['MARKET_SESSION_GATE_MALFORMED']
    if parsed_trade_date.isoformat() != trade_date or parsed_trade_date != timestamp.date():
        return False, ['MARKET_SESSION_GATE_MALFORMED']

    if current_timestamp is not None:
        expected_timestamp = _runtime_local_timestamp(current_timestamp)
        if expected_timestamp is None or expected_timestamp != timestamp:
            return False, ['MARKET_SESSION_GATE_MALFORMED']

    session = verdict.get('session')
    if job_name == 'intraday_cycle':
        if session != 'OPEN':
            return False, ['MARKET_SESSION_GATE_MALFORMED']
    elif job_name == 'close_settlement':
        # Close settlement is not an intraday OPEN job.  It must prove the
        # configured close slot and stale window independently.
        if session != 'CLOSED':
            return False, ['MARKET_SESSION_GATE_MALFORMED']
        window = _close_settlement_window(schedule=schedule)
        close_slot = _parse_session_verdict_timestamp(verdict.get('close_slot'))
        stale_end = _parse_session_verdict_timestamp(verdict.get('close_stale_end'))
        if window is None or close_slot is None or stale_end is None:
            return False, ['MARKET_SESSION_GATE_MALFORMED']
        hour, minute, stale_minutes = window
        expected_slot = timestamp.replace(hour=hour, minute=minute,
                                          second=0, microsecond=0)
        expected_end = expected_slot + datetime.timedelta(minutes=stale_minutes)
        if (close_slot != expected_slot or stale_end != expected_end or
                close_slot.date() != timestamp.date() or
                not (close_slot <= timestamp <= stale_end)):
            return False, ['MARKET_SESSION_GATE_MALFORMED']
    else:
        return False, ['MARKET_SESSION_GATE_MALFORMED']

    reason_codes = verdict.get('reason_codes', [])
    if not isinstance(reason_codes, list):
        return False, ['MARKET_SESSION_GATE_MALFORMED']
    return True, []


def _reason_codes(gate_result):
    values = gate_result.get('reason_codes') or gate_result.get('findings') or []
    if not isinstance(values, list):
        values = [values]
    return [str(value) for value in values] or ['RUNTIME_GATE_FAILED']


def _record_runtime_gate(job_name, source, classification, status, reason_codes,
                         business_job_called):
    """Expose gate decisions through the existing heartbeat/audit channel."""
    detail = json.dumps({
        'source': source,
        'classification': classification,
        'gate_status': status,
        'reason_codes': reason_codes,
        'business_job_called': bool(business_job_called),
    }, ensure_ascii=False, separators=(',', ':'))
    try:
        write_heartbeat(f'runtime_gate:{source}:{job_name}', detail)
    except Exception:
        # An observability failure must not turn a safe HOLD into an ALLOW.
        pass


def execute_runtime_job(job_name, fn=None, source='scheduler', current_timestamp=None,
                        calendar_path=None, schedule=None):
    """Canonical runtime invocation for scheduler, catchup, once, and API.

    The function deliberately separates gate status from business execution.
    A failed/malformed preflight returns HOLD and never calls the business
    callable.  Informational jobs are explicitly read-only and do not require
    the economic startup reconciliation.
    """
    classification = classify_runtime_job(job_name)
    business = fn or BUILTINS.get(job_name)
    if classification is None or business is None:
        result = {
            'job': job_name, 'source': source,
            'classification': classification or 'UNKNOWN',
            'allowed': False, 'status': 'HOLD',
            'reason_codes': ['UNKNOWN_RUNTIME_JOB'],
            'business_job_called': False,
        }
        _record_runtime_gate(job_name, source, result['classification'],
                             result['status'], result['reason_codes'], False)
        return result

    if classification == 'INFORMATIONAL':
        gate_ok, gate_result = True, {'status': 'BYPASS_READ_ONLY', 'pass': True}
    else:
        try:
            if classification == 'ECONOMIC_MUTATING':
                session_result = runtime_market_session_preflight(
                    job_name, current_timestamp=current_timestamp,
                    calendar_path=calendar_path, schedule=schedule)
                verdict_ok, verdict_reasons = validate_market_session_verdict(
                    job_name, session_result, current_timestamp=current_timestamp,
                    schedule=schedule)
                if not verdict_ok:
                    gate_ok = False
                    gate_result = {'status': 'FAIL', 'pass': False,
                                   'findings': verdict_reasons}
                elif not session_result.get('pass'):
                    gate_ok = False
                    gate_result = session_result
                else:
                    gate_ok, gate_result = runtime_job_preflight(job_name)
            else:
                gate_ok, gate_result = runtime_job_preflight(job_name)
        except Exception as exc:
            gate_ok = False
            gate_result = {
                'status': 'FAIL', 'pass': False,
                'findings': [f'RUNTIME_GATE_EXCEPTION:{type(exc).__name__}'],
            }

    if not isinstance(gate_result, dict) or not isinstance(gate_ok, bool):
        result = {
            'job': job_name, 'source': source, 'classification': classification,
            'allowed': False, 'status': 'HOLD',
            'reason_codes': ['RUNTIME_GATE_MALFORMED'],
            'business_job_called': False,
        }
        _record_runtime_gate(job_name, source, classification, result['status'],
                             result['reason_codes'], False)
        return result

    if not gate_ok:
        reason_codes = _reason_codes(gate_result)
        result = {
            'job': job_name, 'source': source, 'classification': classification,
            'allowed': False, 'status': 'HOLD', 'reason_codes': reason_codes,
            'business_job_called': False,
        }
        _record_runtime_gate(job_name, source, classification, result['status'],
                             reason_codes, False)
        return result

    # Only this line invokes business code.  It is intentionally after the
    # preflight and is the only normal path to run_with_guard.
    ok, detail = run_with_guard(job_name, business)
    if classification == 'DATA_MUTATING' and ok and isinstance(detail, dict) and \
            detail.get('status') == 'HOLD':
        reason_codes = detail.get('reason_codes') or ['DATA_JOB_HOLD']
        _record_runtime_gate(job_name, source, classification, 'HOLD', reason_codes, True)
        return {
            'job': job_name, 'source': source, 'classification': classification,
            'allowed': True, 'status': 'HOLD', 'reason_codes': reason_codes,
            'detail': detail, 'business_job_called': True,
        }
    result = {
        'job': job_name, 'source': source, 'classification': classification,
        'allowed': True, 'status': 'PASS' if ok else 'FAILED',
        'reason_codes': [] if ok else ['BUSINESS_JOB_FAILED'],
        'detail': detail, 'business_job_called': True,
    }
    _record_runtime_gate(job_name, source, classification, result['status'],
                         result['reason_codes'], True)
    return result


# ---------------------------------------------------------------- catchup 补跑判断

def _catchup_needed(cfg_job):
    """进程晚启/机器重启导致错过的任务：仍在有效窗口则补跑一次。"""
    if not cfg_job.get('catchup'):
        return False
    win_min = float(cfg_job.get('skip_stale_minutes', 0))
    if win_min <= 0:
        return False
    parts = cfg_job['trigger'].split()
    try:
        cron_m, cron_h = int(parts[0].split('-')[0]), int(parts[1].split('-')[0])
    except ValueError:
        return False          # 周期型触发器(如 */5)不做点位补跑判断
    hb = rc._load_json(rc.HEARTBEAT_PATH, {}).get('jobs', {}).get(cfg_job['builtin'])
    now = datetime.datetime.now()
    today_slot = now.replace(hour=cron_h, minute=cron_m, second=0, microsecond=0)
    if hb:
        last = datetime.datetime.strptime(hb['last_finished'], '%Y-%m-%d %H:%M:%S')
        if last >= today_slot:
            return False      # 今天该点位后已成功执行过
    minutes_late = (now - today_slot).total_seconds() / 60
    return 0 <= minutes_late <= win_min


# ---------------------------------------------------------------- main

def load_schedule():
    with open(rc.SCHEDULE_YAML, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def build_scheduler(conf, scheduler):
    registered = []
    for name, cfg in conf.get('jobs', {}).items():
        builtin = cfg.get('builtin')
        fn = BUILTINS.get(builtin)
        if not fn:
            print(f'[守护] 配置了未知builtin: {builtin}, 已忽略({name})')
            continue

        if cfg.get('market_open_only'):
            # 时段闸门：非连续竞价阶段静默跳过（不动心跳，视为计划内空转）
            def gated(n=builtin, f=fn, schedule_name=name):
                if rc.market_phase() != 'open':
                    return f'skip(当前{rc.market_phase()}时段)'
                return execute_runtime_job(
                    n, f, source=f'scheduler:{schedule_name}')
            target = gated
        else:
            def target(n=builtin, f=fn, schedule_name=name):
                return execute_runtime_job(
                    n, f, source=f'scheduler:{schedule_name}')

        scheduler.add_job(
            target, trigger=CronTrigger.from_crontab(cfg['trigger']), id=name,
            coalesce=True,
            misfire_grace_time=int(cfg.get('misfire_grace_minutes', 5)) * 60,
            max_instances=1,
        )
        registered.append(name)
    return registered


def run_acceptance_canary(conf=None):
    """Run real startup reconciliation and scheduler registration only.

    This intentionally does not initialize the history DB, start web/server or
    scheduler, execute catchup, import Forward runner, or call any business
    builtin.  It is the bounded G4 acceptance path.
    """
    global _SINGLETON_FD
    conf = conf or load_schedule()
    result = {
        'startup_gate': 'FAIL',
        'active_season': None,
        'execution_state_safe': False,
        'new_entry_disabled': True,
        'trading_enabled': False,
        'real_trading': False,
        'registered_jobs': [],
        'economic_jobs': [name for name, policy in RUNTIME_JOB_POLICY.items()
                          if policy == 'ECONOMIC_MUTATING'],
        'data_jobs': [name for name, policy in RUNTIME_JOB_POLICY.items()
                      if policy == 'DATA_MUTATING'],
        'informational_jobs': [name for name, policy in RUNTIME_JOB_POLICY.items()
                               if policy == 'INFORMATIONAL'],
        'scheduler_business_execution_count': 0,
        'forward_day': 0,
        'forward_enabled': False,
        'exit_status': 'FAIL',
    }
    lock = acquire_singleton('daemon')
    if lock is None:
        result['reason_codes'] = ['DAEMON_SINGLETON_HELD']
        return result
    _SINGLETON_FD = lock
    try:
        gate = runtime_job_preflight('daemon_boot')
        if not isinstance(gate, tuple) or len(gate) != 2:
            result['reason_codes'] = ['STARTUP_GATE_MALFORMED']
            return result
        gate_ok, gate_result = gate
        if not isinstance(gate_result, dict) or not isinstance(gate_ok, bool):
            result['reason_codes'] = ['STARTUP_GATE_MALFORMED']
            return result
        result['active_season'] = gate_result.get('season_id')
        result['execution_state_safe'] = bool(gate_result.get('execution_state_safe'))
        result['startup_gate'] = 'PASS' if gate_ok else 'FAIL'
        findings = gate_result.get('findings') or []
        result['reason_codes'] = [str(item) for item in findings]
        if not gate_ok:
            return result

        scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
        result['registered_jobs'] = build_scheduler(conf, scheduler)
        required = set(BUILTINS)
        if not required.issubset({cfg.get('builtin') for cfg in conf.get('jobs', {}).values()}):
            result['reason_codes'] = ['REQUIRED_JOB_NOT_REGISTERED']
            return result
        result['exit_status'] = 'PASS'
        return result
    except Exception as exc:
        result['reason_codes'] = [f'CANARY_EXCEPTION:{type(exc).__name__}']
        return result
    finally:
        try:
            _SINGLETON_FD.close()
        except Exception:
            pass
        _SINGLETON_FD = None


def start_web(port):
    """在同进程内以子线程启动 FastAPI/uvicorn。返回 uvicorn Server 实例。"""
    import uvicorn
    from daemon.app import create_app
    app = create_app()
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    return server


def acquire_singleton(name):
    """单例守卫（OS级文件锁）：state/<name>.lock 上的排他字节锁由本进程持有，
    第二实例加锁失败即退出。进程退出时操作系统自动释放。"""
    import msvcrt
    lock_path = os.path.join(rc.STATE_DIR, f'{name}.lock')
    os.makedirs(rc.STATE_DIR, exist_ok=True)
    f = open(lock_path, 'a+b')
    f.seek(0)
    try:
        # Fixed range also overlaps legacy EOF-offset locks (Windows PID <=10 digits).
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 16)
    except OSError:
        f.close()
        print(f'[守护] 检测到已运行实例（{name}.lock 被持有），本次启动退出')
        return None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()).encode('utf-8'))
    f.flush()
    return f


_SINGLETON_FD = None   # 持有句柄防GC提前释放锁


def _runtime_code_digest(code):
    import hashlib
    import types
    def shape(c):
        return [c.co_code.hex(), c.co_names, c.co_varnames, c.co_argcount,
                c.co_flags, [shape(x) if isinstance(x, types.CodeType) else repr(x)
                             for x in c.co_consts]]
    return hashlib.sha256(json.dumps(shape(code), ensure_ascii=False).encode('utf-8')).hexdigest()


def _record_resident_source_version(names):
    """Observe this process without importing a HOLD-gated Forward runner."""
    import hashlib
    from datasource.completeness import atomic_json_write
    targets = ['ranking', 'features', 'universe', 'forward_runner',
               'paper_executor', 'trade_plan', 'production_input', 'live_quotes',
               'cache_rollover']
    def digest(path):
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    modules = {}
    for target in targets:
        module = sys.modules.get('ai_quant.' + target)
        path = os.path.join(_BASE, 'scripts', 'ai_quant', target + '.py')
        modules[target] = {
            'source_path': path, 'source_sha256_at_startup': digest(path),
            'loaded': module is not None,
            'loaded_file': getattr(module, '__file__', None),
            'functions': {n: _runtime_code_digest(v.__code__)
                          for n, v in vars(module).items()
                          if hasattr(v, '__code__') and getattr(v, '__module__', '') == module.__name__}
                         if module is not None else {},
        }
    receipt = {'pid': os.getpid(), 'parent_pid': os.getppid(),
               'executable': sys.executable, 'observed_at': rc.now_str(),
               'daemon_source_path': os.path.abspath(__file__),
               'daemon_source_sha256': digest(__file__),
               'resident_function_digests': {f.__name__: _runtime_code_digest(f.__code__)
                    for f in [_daily_review, daily_data_recovery, _update_aiq_cache_after_g6, forward_runtime_branch]},
               'modules': modules, 'registered_jobs': names,
               'forward_runner_imported_for_audit': False,
               'economic_jobs_called_for_audit': False}
    receipt['data_recovery_sources'] = {
        name: digest(os.path.join(_BASE, 'scripts', 'datasource', name + '.py'))
        for name in ('daily_recovery', 'active_g6_v2_route', 'listing_exclusions', 'universe_trust_boundary')}
    atomic_json_write(os.path.join(rc.STATE_DIR, 'daemon_source_version.json'), receipt)


def main():
    global _SINGLETON_FD
    ap = argparse.ArgumentParser(description='交易工作台常驻守护进程')
    ap.add_argument('--once', choices=list(BUILTINS.keys()),
                    help='只执行指定内置任务一次后退出(调试/补跑)')
    ap.add_argument('--startup-check', '--acceptance-canary', action='store_true',
                    help='运行真实启动闸门与任务注册验收，不启动scheduler/业务任务')
    ap.add_argument('--no-browser', action='store_true', help='启动时不自动打开浏览器')
    args = ap.parse_args()

    conf = load_schedule()
    port = int(conf.get('port', rc.DAEMON_PORT))

    if args.startup_check:
        canary = run_acceptance_canary(conf)
        print(json.dumps(canary, ensure_ascii=False, sort_keys=True))
        sys.exit(0 if canary.get('exit_status') == 'PASS' else 2)

    # 手动单跑模式
    if args.once:
        result = execute_runtime_job(args.once, source='once')
        if result.get('status') == 'HOLD':
            print(f'[守护] 安全闸 HOLD，禁止 --once {args.once}: {result.get("reason_codes")}')
            sys.exit(2)
        sys.exit(0 if result.get('status') == 'PASS' else 1)

    _SINGLETON_FD = acquire_singleton('daemon')
    if _SINGLETON_FD is None:
        sys.exit(0)

    gate_ok, gate_result = runtime_job_preflight('daemon_boot')
    if not gate_ok:
        print(f'[守护] 启动安全闸失败，禁止启动 scheduler: {gate_result.get("findings")}')
        sys.exit(2)

    from datasource import db as history_db
    history_db.init_db()

    scheduler = BackgroundScheduler(timezone='Asia/Shanghai')
    names = build_scheduler(conf, scheduler)
    _record_resident_source_version(names)
    # 存活标记（看门狗据此判断假死）
    scheduler.add_job(mark_alive, IntervalTrigger(seconds=60), id='_alive',
                      coalesce=True, max_instances=1)

    server = start_web(port)
    scheduler.start()

    # 启动即补跑检查
    for name, cfg in conf.get('jobs', {}).items():
        try:
            if _catchup_needed(cfg):
                phase = rc.market_phase()
                if cfg.get('market_open_only') and phase != 'open':
                    continue
                builtin = cfg.get('builtin')
                fn = BUILTINS.get(builtin)
                if fn:
                    print(f'[守护] 检测到错过今日任务 {name}，启动补跑…')
                    threading.Thread(
                        target=lambda f=fn, n=builtin, schedule_name=name:
                            execute_runtime_job(
                                n, f, source=f'catchup:{schedule_name}'),
                        daemon=True).start()
        except Exception:
            continue

    # Startup recovery is data-only, bounded, and shares the natural job lock.
    # No historical economic task is replayed.
    threading.Thread(target=lambda: execute_runtime_job(
        'daily_data_recovery', source='startup:data_backlog'), daemon=True).start()

    write_heartbeat('daemon_boot', f'调度任务: {",".join(names)}')
    mark_alive()
    print(f'[守护] ✅ 交易工作台守护进程已启动 pid={os.getpid()} '
          f'Web=http://127.0.0.1:{port}/  任务表: {names}')

    if conf.get('auto_open_browser', True) and not args.no_browser:
        threading.Timer(1.8, lambda: os.startfile(f'http://127.0.0.1:{port}/')
                        if hasattr(os, 'startfile') else __import__('webbrowser')
                        .open(f'http://127.0.0.1:{port}/')).start()

    try:
        while server.started or not server.should_exit:
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.shutdown(wait=False)
        print('[守护] 守护进程退出')


if __name__ == '__main__':
    main()
