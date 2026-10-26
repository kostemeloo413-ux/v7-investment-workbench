# -*- coding: utf-8 -*-
"""
scripts/analysis/sell_monitor.py
卖出五级监控 · 机器盯盘、人按纪律执行（借鉴成熟体系008篇的核心价值）

与模拟盘引擎(simulated_trading)的关系：
- 引擎负责"自动成交"(按各博主契约阈值真实买卖)；
- 本模块是"提醒层"：以比引擎更灵敏的档位在盘中持续扫描 simulation_state.json
  的持仓，把"该看了/该卖了"的信号推到微信——只读账本，绝不改单。

五级规则（config/system_params.json → categories.sell_monitor 可调，不写死）：
  L1 冲高强推   浮盈 ≥ strong_take_pct(默认+2.5%)            🚨 卖出强提醒
  L2 冲高关注   浮盈 ∈ [watch_take_pct, strong_take_pct)      👀 关注提示
  L3 纪律止损   浮亏 ≤ stop_loss_pct(默认-3.0%)               🚨 无条件止损提醒
  L4 T1未冲高   昨日买入(T+1首个可卖日)且10:03后仍无起色       ⏰ 择机处理提示
  L5 超期持仓   持有交易日 > max_hold_days(默认3天)            📅 强制决策提醒

降噪铁律：同一持仓同一等级每天只提醒一次；linger 追加提醒仅一次。
"""

import os
import sys
import json
import time
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

SIM_STATE = os.path.join(rc.DATA_DIR, 'simulation_state.json')
PARAMS_PATH = os.path.join(rc.CONFIG_DIR, 'system_params.json')

DEFAULTS = {
    'strong_take_pct': 2.5,
    'watch_take_pct': 1.0,
    'stop_loss_pct': -3.0,
    't1_check_hhmm': 1003,
    'max_hold_days': 3,
    'watch_first_hour_skip_min': 30,   # 开盘后前N分钟不推L2/L4噪音
}


def _thresholds():
    th = dict(DEFAULTS)
    try:
        with open(PARAMS_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        overlay = (cfg.get('categories', {}).get('sell_monitor', {})
                   .get('params', {}))
        th.update({k: v for k, v in overlay.items() if k in DEFAULTS})
    except Exception:
        pass
    return th


def _is_market_closed_now():
    """非交易日（非工作日 / 早 9:00 前 / 夜 15:30 后）返回 True，提醒自动跳过（不重复推送）。"""
    now = datetime.datetime.now()
    if now.weekday() >= 5:   # 周六日
        return True
    hm = now.hour * 100 + now.minute
    if hm < 915 or hm > 1530:
        return True
    return False


def _alert_state():
    st = rc._load_json(rc.SELL_ALERTS_PATH, {})
    today = datetime.date.today().strftime('%Y-%m-%d')
    if st.get('date') != today:
        st = {'date': today, 'sent': {}}
    return st


def _hold_trading_days(buy_date):
    """粗略交易日数（自然日差-周末），够用于超期提醒。"""
    try:
        d0 = datetime.datetime.strptime(str(buy_date)[:10], '%Y-%m-%d').date()
    except Exception:
        return 0
    days = (datetime.date.today() - d0).days
    weeks = days // 7
    return max(days - weeks * 2, 0)


def scan_positions(force=False):
    """
    盘中扫描模拟盘全部持仓并推送提醒。
    force=True 时忽略每日一次的降噪去重（供手动触发验证用）。
    返回摘要字符串（写入心跳）。
    """
    state = rc._load_json(SIM_STATE, {})
    accounts = state.get('accounts') or {}
    positions = []
    for bid, acc in accounts.items():
        for p in acc.get('positions', []):
            p['_blogger'] = acc.get('blogger_name', bid)
            p['_blogger_id'] = bid
            positions.append(p)

    if not positions:
        return '无持仓,无需监控'

    if _is_market_closed_now():
        return '非交易时段,本轮跳过(避免周末/盘后误推)'

    from datasource.providers import get_realtime_quotes
    quotes = get_realtime_quotes([p['symbol'] for p in positions])
    if not quotes:
        return '行情拉取失败,本轮跳过(不提醒不误报)'

    th = _thresholds()
    st = _alert_state()
    alerts = []

    now_hhmm = datetime.datetime.now().hour * 100 + datetime.datetime.now().minute
    open_minutes_ago = None   # 不精确到开盘时刻,用时段粗判即可

    for p in positions:
        q = quotes.get(p['symbol'])
        if not q or q.get('price', 0) <= 0:
            continue
        cost = p.get('cost_price') or 1
        price = q['price']
        pct = round((price / cost - 1) * 100, 2)
        name = p.get('name', p['symbol'])
        who = p.get('_blogger', '')
        buy_date = str(p.get('buy_date', ''))[:10]
        base = f'{who}｜{name}({p["symbol"]}) 现{price} 成本{cost} {pct:+.2f}%'

        fired = []
        if pct >= th['strong_take_pct']:
            fired.append((f'L1冲高_{p["symbol"]}',
                          f'🚨【冲高强推】{base} ≥+{th["strong_take_pct"]}% → 按纪律分批卖出兑现'))
        elif pct >= th['watch_take_pct']:
            if now_hhmm >= 945:                     # 避开开盘半小时的普通波动噪音
                fired.append((f'L2关注_{p["symbol"]}',
                              f'👀【冲高关注】{base} 达L2区间 → 准备止盈预案'))
        if pct <= th['stop_loss_pct']:
            fired.append((f'L3止损_{p["symbol"]}',
                          f'🚨【纪律止损】{base} ≤{th["stop_loss_pct"]}% → 无条件执行,不硬扛'))

        if buy_date == str(_prev_trading_day()) and now_hhmm >= th['t1_check_hhmm'] and pct < th['watch_take_pct']:
            fired.append((f'L4T1未动_{p["symbol"]}',
                          f'⏰【T1择机】{base} 为昨日买入今日首个可卖日且走势平淡 → 观察是否择机了结'))

        hd = _hold_trading_days(buy_date)
        if hd > th['max_hold_days']:
            fired.append((f'L5超期_{p["symbol"]}',
                          f'📅【超期强制决策】{base} 已持有约{hd}个交易日 → 必须给出继续持有理由或离场'))

        # L6 买入理由消失（阶段五·任务G）：开仓利好因子被反向事件证伪
        try:
            from analysis.attribution import reason_gone_check
            _gone = reason_gone_check(p.get('_blogger_id'), p['symbol'])
            if _gone:
                fired.append((f'L6理由消失_{p["symbol"]}',
                              f'🔻【买入理由消失】{base} {_gone} → 建议离场或给出新持有依据'))
        except Exception:
            pass  # 无快照/查询异常时诚实跳过

        for key, text in fired:
            if not force and key in st['sent']:
                continue
            st['sent'][key] = rc.now_str()
            st.setdefault('history', []).append({'ts': rc.now_str(), 'key': key, 'text': text})
            st['history'] = st['history'][-100:]      # 只保留最近100条
            alerts.append(text)

    if alerts:
        from notify.wecom_bot import push
        push('【盘中卖出监控】触发 %d 条' % len(alerts), alerts[:8],
             level=('warn' if any('🚨' in a for a in alerts) else 'info'))
    rc._save_json(rc.SELL_ALERTS_PATH, st)

    return f'持仓{len(positions)}只 新提醒{len(alerts)}条'


def _prev_trading_day():
    d = datetime.date.today() - datetime.timedelta(days=1)
    while not rc.is_trading_day(d):
        d -= datetime.timedelta(days=1)
    return d


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='卖出五级监控手动扫描')
    ap.add_argument('--force', action='store_true', help='忽略当日去重,强制重发')
    args = ap.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print('阈值:', _thresholds())
    print(scan_positions(force=args.force))
