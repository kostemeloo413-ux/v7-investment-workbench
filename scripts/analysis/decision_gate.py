# -*- coding: utf-8 -*-
"""
scripts/analysis/decision_gate.py
G2.5 decision context and deterministic execution permission helpers.

evaluate_decision() exposes SOFT_RISK_OPINION context only. It must not
make investment-value vetoes, change Skill scores, or rewrite Skill ratios.
deterministic_execution_safety() is the separate execution-permission check
for deterministic account, transaction, market, and safety constraints.

Legacy threshold names and write_veto() remain only for compatibility with
historical callers; they are not used by the current decision path.
"""

import os
import sys
import json
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_BASE, os.path.join(_BASE, 'scripts')):
    if p not in sys.path:
        sys.path.insert(0, p)

import runtime_config as rc

DEFAULTS = {
    'boost_strong_positive': 15,       # legacy context threshold; no score boost
    'veto_negative_sentiment': -0.5,   # legacy context threshold; no veto
    'priced_in_penalty_threshold': 80, # context threshold; no score penalty
    'priced_in_penalty': 10,           # retained for config compatibility only
    'risk_off_discount': 0.8,          # retained for config compatibility; no ratio rewrite
    'max_total_position_pct': 80,      # context threshold; no universal position gate
    'max_industry_pct': 40,            # context threshold; no universal position gate
    'max_daily_new_buys': 3,           # context threshold; Skill policy owns execution intent
    'crowded_theme_after': 2,          # context threshold
    'crowded_theme_factor': 0.5,       # retained for config compatibility; no ratio rewrite
    'debate_veto_penalty': 25,         # legacy context threshold; no veto
}

_TH_CACHE = None


def thresholds():
    global _TH_CACHE
    if _TH_CACHE is None:
        th = dict(DEFAULTS)
        try:
            cfg = json.load(open(os.path.join(_BASE, 'config', 'system_params.json'),
                                 encoding='utf-8'))
            overlay = (cfg.get('categories', {}).get('decision_gate', {}).get('params', {}) or {})
            th.update({k: v for k, v in overlay.items() if k in th})
        except Exception:
            pass
        _TH_CACHE = th
    return _TH_CACHE


def reset_cache():
    global _TH_CACHE
    _TH_CACHE = None


def _net_factor_sentiment(symbol, since_hours=72):
    """衰减加权净情绪：Σ sentiment × 0.5^(age/half_life) × confidence/100。异常返回 None（诚实缺席）。"""
    try:
        from datasource import db as hdb
        rows = hdb.get_factors(symbol, since_hours=since_hours, limit=50) or []
        now = datetime.datetime.now()
        net, n = 0.0, 0
        snap = []
        for r in rows:
            if isinstance(r, tuple):
                continue
            pub = str(r.get('published_at') or '')
            try:
                t0 = datetime.datetime.strptime(pub[:19], '%Y-%m-%d %H:%M:%S')
                age_h = max(0.0, (now - t0).total_seconds() / 3600.0)
            except Exception:
                age_h = 0.0
            hl = max(1.0, float(r.get('half_life_h') or 24.0))
            w = 0.5 ** (age_h / hl)
            conf = float(r.get('confidence') or 50) / 100.0
            s = float(r.get('sentiment') or 0.0)
            net += s * w * conf
            n += 1
            snap.append({'event_type': r.get('event_type'), 'sentiment': s,
                         'weight': round(w * conf, 3), 'title': str(r.get('title') or '')[:40]})
        if n == 0:
            return None, []
        return round(net, 3), snap
    except Exception:
        return None, []


def _priced_in_for(symbol, name=''):
    """从 data.json 前瞻事件取该标的的 priced_in_score（最大值）；无则 None。"""
    try:
        d = rc._load_json(os.path.join(rc.DATA_DIR, '..', 'workbench', 'data.json'), {}) or {}
        best = None
        for e in (d.get('forward_events') or []):
            t = f"{e.get('title') or ''}{e.get('name') or ''}"
            pb = (e.get('playbook') or {}).get('priced_in_score')
            if pb is None:
                continue
            if (name and name in t) or (symbol and symbol.replace('sh', '').replace('sz', '') in t):
                best = max(best or 0, float(pb))
        return best
    except Exception:
        return None


def _regime():
    try:
        d = rc._load_json(os.path.join(rc.DATA_DIR, '..', 'workbench', 'data.json'), {}) or {}
        return (d.get('market_regime') or {}).get('trend') or ''
    except Exception:
        return ''


def _industry_of(symbol, name=''):
    """粗行业归属：扫 menxia.THEME_MAP 的池内名单。"""
    try:
        from pipeline.menxia import THEME_MAP
        code = ''.join(ch for ch in str(symbol) if ch.isdigit())[-6:]
        for theme, codes in THEME_MAP.items():
            if code in codes:
                return theme
        for theme, codes in THEME_MAP.items():
            if name and any(n for n in [name]) and theme in str(name):
                return theme
    except Exception:
        pass
    return '其他'


def _industry_pcts(positions):
    """当前持仓的行业占比（按市值近似=成本价×股数）。"""
    total = 0.0
    by_ind = {}
    for p in (positions or []):
        v = float(p.get('cost_price') or 0) * float(p.get('shares') or 0)
        total += v
        ind = _industry_of(p.get('symbol'), p.get('name') or '')
        by_ind[ind] = by_ind.get(ind, 0.0) + v
    if total <= 0:
        return 0.0, {}
    return 100.0, {k: round(v, 2) for k, v in by_ind.items()}   # 占比用绝对额比较


def write_veto(entry):
    """否决审计日志（为什么没买，可追溯）。"""
    try:
        p = os.path.join(rc.STATE_DIR, 'veto_log.json')
        log = rc._load_json(p, []) or []
        entry['ts'] = rc.now_str()
        log.append(entry)
        rc._save_json(p, log[-200:])
    except Exception:
        pass


def evaluate_decision(symbol, name='', contract_score=60.0, *,
                      positions=None, account=None, blogger_id='',
                      today_buy_count=None, debate_penalty=None, factor_net=None,
                      priced_in=None, apply_st_cap=None):
    """
    Skill 决策的软风险上下文入口。

    本函数不再因情绪、辩论、priced-in、risk_off、拥挤或组合观点
    产生统一 VETO、分数改写或仓位比例改写。它只返回可审计的
    SOFT_RISK_OPINION；确定性执行安全由 deterministic_execution_safety()
    或 PaperExecutionEngine 的既有安全链负责。
    """
    th = thresholds()
    if factor_net is None:
        factor_net, factor_snap = _net_factor_sentiment(symbol)
    else:
        factor_snap = []
    regime = _regime()
    snapshot = {'symbol': symbol, 'name': name, 'contract_score': contract_score,
                'factor_net': factor_net, 'factor_detail': factor_snap[:6],
                'regime': regime, 'priced_in': priced_in,
                'debate_penalty': debate_penalty}
    soft = []
    if factor_net is not None and factor_net <= float(th['veto_negative_sentiment']):
        soft.append({'code': 'NEGATIVE_FACTOR_SENTIMENT', 'value': factor_net})
    if factor_net is not None and factor_net >= 0.5:
        soft.append({'code': 'POSITIVE_FACTOR_SENTIMENT', 'value': factor_net})
    if priced_in is not None and priced_in >= float(th['priced_in_penalty_threshold']):
        soft.append({'code': 'PRICED_IN_CONTEXT', 'value': priced_in})
    if debate_penalty is not None and debate_penalty > 0:
        soft.append({'code': 'BEAR_DEBATE_CONTEXT', 'value': debate_penalty})
    if regime == 'risk_off':
        soft.append({'code': 'RISK_OFF_CONTEXT', 'value': regime})

    positions = positions or []
    total_cost = sum(float(p.get('cost_price') or 0) * float(p.get('shares') or 0)
                     for p in positions)
    cash = float((account or {}).get('cash') or 0)
    total_equity = total_cost + cash
    if total_equity > 0:
        add_v = cash * 0.25
        new_pos_pct = (total_cost + add_v) / total_equity * 100
        if new_pos_pct > float(th['max_total_position_pct']):
            soft.append({'code': 'PORTFOLIO_EXPOSURE_CONTEXT', 'value': round(new_pos_pct, 1)})

        ind = _industry_of(symbol, name)
        ind_cost = sum(float(p.get('cost_price') or 0) * float(p.get('shares') or 0)
                       for p in positions if _industry_of(p.get('symbol'), p.get('name') or '') == ind)
        new_ind_pct = (ind_cost + add_v) / total_equity * 100
        if new_ind_pct > float(th['max_industry_pct']):
            soft.append({'code': 'INDUSTRY_EXPOSURE_CONTEXT', 'value': round(new_ind_pct, 1)})

    if today_buy_count is not None and today_buy_count >= int(th['max_daily_new_buys']):
        soft.append({'code': 'DAILY_OPPORTUNITY_CONTEXT', 'value': today_buy_count})
    try:
        ind = _industry_of(symbol, name)
        ind_positions = sum(1 for p in positions
                            if _industry_of(p.get('symbol'), p.get('name') or '') == ind)
        if ind_positions >= int(th['crowded_theme_after']):
            soft.append({'code': 'CROWDED_THEME_CONTEXT', 'value': ind_positions})
    except Exception:
        pass

    reasons = [f"SOFT_RISK_OPINION:{item['code']}" for item in soft]
    snapshot['soft_risk_context'] = soft
    return {'verdict': 'PASS', 'score_delta': 0.0, 'ratio_multiplier': 1.0,
            'reasons': reasons, 'soft_risk_context': soft, 'snapshot': snapshot,
            'thresholds': th}


def deterministic_execution_safety(*, action, account=None, positions=None, shares=0,
                                   price=0, real_trading=False,
                                   new_entry_disabled=False, execution_state_safe=True,
                                   trading_enabled=True,
                                   idempotency_duplicate=False, t1_allowed=True,
                                   market_open=True, is_suspended=False,
                                   limit_impossible=False, lot_size=100):
    """仅执行确定性许可检查；不读取或判断任何投资价值观点。"""
    action = str(action or '').upper()
    blocks = []
    if real_trading:
        blocks.append('REAL_TRADING_DISABLED_INVARIANT')
    if action not in ('BUY', 'SELL'):
        blocks.append('INVALID_ACTION')
    if action == 'BUY' and new_entry_disabled:
        blocks.append('NEW_ENTRY_DISABLED')
    if action == 'BUY' and not trading_enabled:
        blocks.append('TRADING_DISABLED')
    if not execution_state_safe:
        blocks.append('EXECUTION_STATE_UNSAFE')
    if idempotency_duplicate:
        blocks.append('IDEMPOTENCY_DUPLICATE')
    if not market_open:
        blocks.append('MARKET_CLOSED')
    if is_suspended:
        blocks.append('SUSPENSION')
    if limit_impossible:
        blocks.append('LEGAL_LIMIT_EXECUTION_IMPOSSIBLE')
    qty = 0
    if action in ('BUY', 'SELL'):
        try:
            qty = int(shares)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0 or qty % int(lot_size) != 0:
            blocks.append('INVALID_LOT_SIZE')
        try:
            if float(price) <= 0:
                blocks.append('INVALID_PRICE')
        except (TypeError, ValueError):
            blocks.append('INVALID_PRICE')
    if action == 'BUY':
        cash = float((account or {}).get('cash') or 0)
        try:
            notional = float(price) * qty
        except (TypeError, ValueError):
            notional = 0.0
        if notional > cash:
            blocks.append('CASH_INSUFFICIENT')
        if (account or {}).get('trading_account') is False:
            blocks.append('NON_TRADING_ACCOUNT')
    if action == 'SELL':
        if not t1_allowed:
            blocks.append('T_PLUS_ONE_RESTRICTION')
        available = sum(int(p.get('shares') or 0) for p in (positions or []))
        if qty > available:
            blocks.append('SELL_QUANTITY_EXCEEDS_POSITION')
    return {
        'verdict': 'BLOCK' if blocks else 'ALLOW',
        'hard_blocks': blocks,
        'authority': 'DETERMINISTIC_EXECUTION_SAFETY',
    }
