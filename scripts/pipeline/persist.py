# -*- coding: utf-8 -*-
"""
scripts/pipeline/persist.py
起居注 · Persister（落盘史官）

职责：将流水线各环节产物（六部奏本 / 通政奏报 / 驿传行单 / 弹章）统一
     合并写入 workbench/data.json（卷宗库），并同步嵌入 index.html，
     保证「单点写入、顺序受控」——所有板块数据同一处落盘，杜绝并发写坏。

产出更新：
  - data.json.blogger_picks / independent_blogger_picks ← 六部奏本 picks
  - data.json.hotspots / forward_events / market_diagnosis / consensus ... ← 通政奏报+上游引擎
  - data.json.pipeline_alert ← 都察院弹章告警
  - data.json.pipeline_state_snapshot ← 内阁档册摘要（前端体检用）
"""

import os
import sys
import json
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(BASE_DIR, 'scripts')
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, BASE_DIR)

DATA_PATH = os.path.join(BASE_DIR, 'workbench', 'data.json')


def _load_data():
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_data(data):
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record(data, brief=None, dispatch_log=None, liubu_report=None, report=None, pipe=None, quiet=False):
    """
    起居注主流程：合并各环节产物写入 data.json 并嵌入 index.html。

    参数均允许为 None（容错：未产出环节不覆盖，保留旧值）。
    """
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    now_ms = int(datetime.datetime.now().timestamp() * 1000)

    # 1. 六部奏本 → blogger_picks
    if liubu_report and liubu_report.get('picks'):
        data['blogger_picks'] = liubu_report['picks']
        data['independent_blogger_picks'] = liubu_report['picks']
        data['picks_updated_at'] = now_str

    # 1.0 引擎状态（前端状态条数据源：llm/offline）
    try:
        import runtime_config as _rc2
        data['research_engine'] = 'llm' if _rc2.LLM_API_KEY else 'offline'
    except Exception:
        pass

    # 1.1 统一市场状态机（趋势/量能/情绪/风格）— 全站共享
    try:
        from analysis.regime import compute_regime
        try:
            from analysis.event_factors import factor_snapshot
            _efs = factor_snapshot(since_hours=24)
        except Exception:
            _efs = []
        data['market_regime'] = compute_regime(
            brief.get('market_quotes') or {}, _efs)
    except Exception as e:
        if not quiet:
            print(f'[起居注] regime计算提示：{e}')

    # 1.5 通政奏报 → 热点/前瞻/诊断/共识/机会看板/分时（补齐各板块，避免停留在旧引擎时间点）
    if brief:
        # 阶段四：hotspots 为空列表也要覆盖——0条强/中相关时诚实空缺，
        # 不得残留上一轮旧热点冒充最新（用户诚实协议）
        if brief.get('hotspots') is not None:
            hs = brief['hotspots']
            # 起居注复核：门下省 VETO 的热点兜底剔除（防旧引擎/直接跑老脚本残留污染）
            try:
                import menxia
                hs = [h for h in hs
                      if menxia.relevance_check(h.get('title', ''), h.get('summary', ''),
                                                h.get('category', ''))[1] != 'VETO']
            except Exception:
                pass
            data['hotspots'] = hs
        # 阶段四：分层统计透传前端（过滤计数条数据源）
        if brief.get('news_tiers') is not None:
            data['news_tiers'] = brief['news_tiers']
        # 双击离线版兜底（用户 2026-08-29）：卖监控快照/企微状态/阈值随快照入库
        try:
            import runtime_config as _rc
            sa = _rc._load_json(_rc.SELL_ALERTS_PATH, {}) or {}
            data['sell_alerts'] = {'count': len(sa.get('sent', {}) or {}),
                                   'history': (sa.get('history', []) or [])[-20:]}
            _cfg = _rc._load_json(os.path.join(_rc.CONFIG_DIR, 'system_params.json'), {}) or {}
            _th = {'strong_take_pct': 2.5, 'watch_take_pct': 1.0,
                   'stop_loss_pct': -3.0, 'max_hold_days': 3}
            _th.update((_cfg.get('categories', {}).get('sell_monitor', {}).get('params', {}) or {}))
            data['sell_thresholds'] = _th
            _envp = os.path.join(_rc.BASE_DIR, '.env')
            _wecom = False
            if os.path.exists(_envp):
                with open(_envp, 'r', encoding='utf-8') as _f:
                    for _ln in _f:
                        if _ln.strip().startswith('WECOM_WEBHOOK_URL=') and _ln.split('=', 1)[1].strip():
                            _wecom = True
                            break
            data['wecom_configured'] = _wecom
        except Exception:
            pass
        try:
            from datasource import db as _hdb
            _today = datetime.datetime.now().strftime('%Y-%m-%d')
            _vl = _rc._load_json(_os.path.join(_rc.STATE_DIR, 'veto_log.json'), []) or []
            try:
                import json as _pj
                import os as _po
                _pf = _po.path.join(_po.path.dirname(_os.path.abspath(__file__)), '..', 'state', 'premarket_events.json')
                if _os.path.exists(_pf):
                    _pd = _pj.load(open(_pf, encoding='utf-8'))
                    if isinstance(_pd, dict) and isinstance(_pd.get('events'), list):
                        data['premarket_events'] = _pd
                        print('  [persist] 读盘前事件: ' + str(len(_pd['events'])) + ' 条 <- ' + _pf)
            except Exception as _pe:
                print('  [persist] 读盘前事件失败: ' + str(_pe))
            data['decision_gate'] = {
                'vetoes_today': [v for v in _vl if str(v.get('ts', '')).startswith(_today)][-20:],
                'recent_traces': _hdb.recent_traces(10),
            }
            _tf = _rc._load_json(_os.path.join(_rc.STATE_DIR, 'trade_feed.json'), {}) or {}
            data['trade_feed'] = (_tf.get('items') or [])[-10:]
        except Exception:
            pass
        if brief.get('vetoed_count') is not None:
            data['vetoed_count'] = brief['vetoed_count']
        if brief.get('forward_events'):
            data['forward_events'] = brief['forward_events']
        if brief.get('index_intraday'):
            data['index_intraday'] = brief['index_intraday']
        mq = brief.get('market_quotes')
        if mq:
            try:
                from auto_update_hotspots import (generate_dynamic_market_diagnosis,
                                                  generate_dynamic_consensus,
                                                  generate_dynamic_blogger_opportunity_board)
                # 数据驱动生成器必须吃到当日真实选股，否则看板会误报"无触发"
                picks_for_gen = (liubu_report or {}).get('picks') or None
                diag = generate_dynamic_market_diagnosis(mq, picks=picks_for_gen)
                if diag:
                    data['market_diagnosis'] = diag
                cons = generate_dynamic_consensus(mq, picks=picks_for_gen)
                if cons:
                    data['consensus'] = cons
                board = generate_dynamic_blogger_opportunity_board(mq, diag, cons, picks_for_gen)
                if board:
                    data['blogger_opportunity_board'] = board
            except Exception as e:
                if not quiet:
                    print(f'[起居注] 诊断/共识生成提示：{e}')

    # 2. 都察院弹章 → pipeline_alert
    if report is not None:
        fails = [i for i in report.get('impeachments', []) if i['severity'] == 'FAIL']
        warns = [i for i in report.get('impeachments', []) if i['severity'] == 'WARN']
        data['pipeline_alert'] = {
            'status': report['overall'],
            'score': report['score'],
            'reported_at': report['inspected_at'],
            'pipeline_id': report.get('pipeline_id'),
            'impeachment_count': len(fails),
            'warning_count': len(warns),
            'impeachments': fails,
            'warnings': warns,
            'checks': report.get('checks', []),
            'message': (
                f'都察院弹劾：{len(fails)} 项 FAIL、{len(warns)} 项 WARN，评分 {report["score"]}/100'
                if (fails or warns)
                else f'都察院监察通过：{report["score"]}/100'
            ),
        }

    # 3. 内阁档册摘要 → pipeline_state_snapshot（前端体检面板数据源）
    if pipe is not None:
        # 起居注执行时整链可能仍 RUNNING（都察院是最后一环），用 stages 推断最终状态
        stage_statuses = {v.get('status') for v in (pipe.get('stages') or {}).values()}
        if 'FAILED' in stage_statuses:
            final_status = 'FAILED'
        elif stage_statuses and stage_statuses <= {'DONE', 'SKIPPED'}:
            final_status = 'DONE'
        else:
            final_status = pipe.get('status', 'RUNNING')
        data['pipeline_state_snapshot'] = {
            'pipeline_id': pipe.get('pipeline_id'),
            'status': final_status,
            'trigger': pipe.get('trigger'),
            'created_at': pipe.get('created_at'),
            'finished_at': pipe.get('finished_at'),
            'duration_ms': pipe.get('duration_ms'),
            'updated_at': now_ms,
            'stages': {
                k: {'status': v.get('status'), 'duration_ms': v.get('duration_ms'),
                    'started_at': v.get('started_at'), 'finished_at': v.get('finished_at'),
                    'output': v.get('output'), 'detail': v.get('detail')}
                for k, v in (pipe.get('stages') or {}).items()
            },
        }

    # 顶层「最近一次成功时间」：仅整链 DONE 刷新，供前端时效失效判断（根治旧绿常驻假象）
    if pipe is not None and final_status == 'DONE':
        data['pipeline_last_success_at'] = now_ms

    data['updated_at'] = now_str
    _save_data(data)

    # 4. 嵌入 index.html（离线打开也有最新数据）
    embed_ok = True
    try:
        from auto_update_hotspots import embed_data_into_html
        embed_ok = embed_data_into_html(data)
    except Exception as e:
        embed_ok = False
        if not quiet:
            print(f'[起居注] 内嵌同步提示：{e}')

    if not quiet:
        print(f'[起居注] 卷宗库已更新：{DATA_PATH}（内嵌 {"成功" if embed_ok else "失败"}）')
    return data, embed_ok


def write_failure_snapshot(trigger='automation', pipeline_id=None, error='', quiet=False):
    """兜底：流水线若连第一步都崩（如 orchestrator 未捕获异常、或宿主层中断），
    把内阁档册标记为 FAILED，绝不残留上一轮旧 DONE 绿灯，根治「绿灯假象」。"""
    try:
        now = datetime.datetime.now()
        now_ms = int(now.timestamp() * 1000)
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        pid = pipeline_id or ('FAIL_' + now_str.replace(' ', '_').replace(':', ''))
        data = _load_data()
        data['pipeline_state_snapshot'] = {
            'pipeline_id': pid,
            'status': 'FAILED',
            'trigger': trigger,
            'created_at': now_str,
            'finished_at': now_str,
            'duration_ms': None,
            'updated_at': now_ms,
            'stages': {},
            'fatal_error': (error or '')[:600],
        }
        data['updated_at'] = now_str
        _save_data(data)
        try:
            from auto_update_hotspots import embed_data_into_html
            embed_data_into_html(data)
        except Exception:
            pass
        if not quiet:
            print(f'[起居注] 已写入失败快照：{pid}')
    except Exception as e:
        if not quiet:
            print(f'[起居注] 失败快照写入异常：{e}')


if __name__ == '__main__':
    from blogger_workers import load_liubu
    from inspector import load_report
    from pipeline_state import get_latest_pipeline
    lb = load_liubu()
    rep = load_report()
    pipe = get_latest_pipeline()
    d = _load_data()
    record(d, liubu_report=lb, report=rep, pipe=pipe)
