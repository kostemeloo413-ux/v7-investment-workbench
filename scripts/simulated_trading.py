# -*- coding: utf-8 -*-
"""
scripts/simulated_trading.py
AI 模拟盘引擎 + 沪深主板大池联动 + 本地交易账本锁存（v2.0 阶段3 核心升级）

6 位博主/AI 角色在 100 万虚拟资金上模拟交易：
- 与 3103 家沪深主板大池量化选股（scan_mainboard_picks.py）完全打通！
- 每日开盘/盘中/盘后运行：
  1. 从活动赛季 participant ledger Replay 重建真实账户状态（旧 data/trades 仅作归档证据）；
  2. 检查现有持仓止盈止损（执行卖出，资金回流现金）；
  3. 动态读取 3103 家主板大池最新选股信号（blogger_picks），符合契约自动买入建仓/调仓；
  4. 重新计算账户净值、持仓市值与浮动收益；
  5. 交易流水追加落盘到活动赛季 participant ledger；
  6. 重新生成并比对 SHA-256 manifest.json（防篡改校验）；
  7. 同步写入 data/simulation_state.json、workbench/data.json 并安全注入 workbench/index.html。
"""

import os
import sys
import json
import hashlib
import datetime
import urllib.request

# Windows 终端编码保护
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_DIR = os.path.join(BASE, 'data', 'trades')
STATE_PATH = os.path.join(BASE, 'data', 'simulation_state.json')
DATA_PATH = os.path.join(BASE, 'workbench', 'data.json')
HTML_PATH = os.path.join(BASE, 'workbench', 'index.html')

PARTICIPANT_TO_LEGACY = {
    'P-HANSHU': 'hanshu',
    'P-YINWENTAO': 'yintwentao',
    'P-LIAO-XIAOAI': 'liao-xiaoai',
    'P-COOL': 'cool',
    'P-RULE-QUANT': 'ai-alpha',
}
LEGACY_TO_PARTICIPANT = {v: k for k, v in PARTICIPANT_TO_LEGACY.items()}

INITIAL_CAPITAL = 100000.0  # 每位博主 10 万元虚拟资金 (总资金池 60 万元)

# 止盈止损与最大持仓配置（严格对齐各博主 Skill 契约）
BLOGGER_CONFIGS = {
    'hanshu': {
        'name': '韩叔 (趣聊财经)',
        'max_positions': 2,
        'buy_ratio': 0.25,        # 荡秋千单次 25% 仓位，预留 25% 预备队
        'stop_loss': -0.035,       # 绝活生命线 -3.5% 止损
        'take_profit': 0.060,      # 荡秋千远离5天线 +6.0% 止盈
        'trailing': None,
    },
    'yintwentao': {
        'name': '殷文涛 (梵爹)',
        'max_positions': 2,
        'buy_ratio': 0.30,        # 阵营抱团单次 30% 仓位
        'stop_loss': -0.035,       # 大阳线底破位 -3.5% 止损
        'take_profit': 0.085,      # 波段主升 +8.5% 止盈
        'trailing': None,
    },
    'ai-alpha': {
        'name': 'AI-Alpha 智脑',
        'max_positions': 3,
        'buy_ratio': 0.20,        # 动量多因子单次 20% 仓位
        'stop_loss': -0.045,       # 成本线刚性 -4.5% 止损
        'take_profit': None,       # 不设固定止盈
        'trailing': 0.080,         # ATR(14)×2.2 动态追踪回撤 8.0% 止盈
    },
    'liao-xiaoai': {
        'name': '廖小爱',
        'max_positions': 2,
        'buy_ratio': 0.30,        # 3·3·1 模型首批 30% 底仓，预留 30% 深蹲无限子弹
        'stop_loss': -0.060,       # 价值防守 -6.0% 止损 (逻辑破坏清仓)
        'take_profit': 0.120,      # 产业周期兑现 +12.0% 止盈
        'trailing': None,
    },
    'cool': {
        'name': '酷儿 (Cool)',
        'max_positions': 2,
        'buy_ratio': 0.20,        # 假跌破绝杀单次 20% 仓位
        'stop_loss': -0.030,       # 假跌破失效 -3.0% 刚性硬止损
        'take_profit': 0.090,      # 严格 3:1 盈亏比 +9.0% 止盈
        'trailing': None,
    },
    'anti-demo': {
        'name': '极短线情绪反指哨兵',
        'max_positions': 0,        # 纯风险监控，不主动做多
        'buy_ratio': 0.0,
        'stop_loss': -0.020,
        'take_profit': 0.040,
        'trailing': None,
    }
}

COMMISSION_RATE = 0.0003   # 佣金万三
COMMISSION_MIN = 5.0       # 最低5元
STAMP_TAX = 0.0005         # 卖出印花税 0.05%
SLIPPAGE = 0.001           # 双边滑点 0.1%


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def today_str():
    return datetime.datetime.now().strftime('%Y-%m-%d')


def load_active_season_safety():
    """Load active-season permission state; missing/invalid state fails closed."""
    safety = {
        'season_id': None,
        '_safety_season_id': None,
        '_safety_root': None,
        'new_entry_disabled': True,
        'execution_state_safe': False,
        'trading_enabled': False,
        'real_trading': False,
    }
    try:
        from competition.season_runtime import resolve_active_season_context
        from ai_quant.storage_utils import strict_load_json
        context = resolve_active_season_context()
        result = strict_load_json(context['governance_safety_path'], expected_type=dict)
        if not result.is_valid:
            return safety
        payload = result.value
        if payload.get('season_id') != context['season_id'] or payload.get('real_trading') is not False:
            return safety
        for key in ('new_entry_disabled', 'execution_state_safe', 'trading_enabled'):
            if not isinstance(payload.get(key), bool):
                return safety
        safety.update({
            'season_id': context['season_id'],
            '_safety_season_id': context['season_id'],
            '_safety_root': context['season_root'],
            'new_entry_disabled': payload['new_entry_disabled'],
            'execution_state_safe': payload['execution_state_safe'],
            'trading_enabled': payload['trading_enabled'],
            'real_trading': False,
        })
    except Exception:
        return safety
    return safety


def _buy_safety_check(account, price, ratio):
    """Preview and enforce deterministic BUY permission before execute_buy."""
    from analysis.decision_gate import deterministic_execution_safety
    try:
        buy_price = round(float(price) * (1 + SLIPPAGE), 4)
        budget = float(account.get('cash') or 0) * float(ratio)
        shares = int(budget / buy_price / 100) * 100 if buy_price > 0 else 0
    except (TypeError, ValueError, ZeroDivisionError):
        buy_price, shares = 0.0, 0
    return deterministic_execution_safety(
        action='BUY',
        account=account,
        positions=account.get('positions'),
        shares=shares,
        price=buy_price,
        real_trading=bool(account.get('real_trading', True)),
        new_entry_disabled=bool(account.get('new_entry_disabled', True)),
        execution_state_safe=bool(account.get('execution_state_safe', False)),
        trading_enabled=bool(account.get('trading_enabled', False)),
    )


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def format_symbol(code):
    """将6位纯数字代码转为腾讯行情标准带前缀代码"""
    code_str = str(code).strip()
    if code_str.startswith(('sh', 'sz')):
        return code_str
    if code_str.startswith(('600', '601', '603', '605')):
        return f'sh{code_str}'
    return f'sz{code_str}'


def new_account(bid):
    cfg = BLOGGER_CONFIGS.get(bid, {})
    account = {
        'blogger_id': bid,
        'blogger_name': cfg.get('name', bid),
        'cash': INITIAL_CAPITAL,
        'positions': [],   # {symbol, name, code, shares, cost_price, high_since_buy, buy_date}
        'trade_ids': [],
        'total_pnl': 0.0,
    }
    account.update(load_active_season_safety())
    return account


def _active_runtime_context():
    """Resolve active season economic storage; never fall back to legacy files."""
    from competition.season_runtime import resolve_active_season_context
    return resolve_active_season_context()


def _account_from_season_ledger(pid, state, context):
    """Adapt a season ledger to the legacy in-memory consumer shape."""
    bid = PARTICIPANT_TO_LEGACY[pid]
    account_doc = state['account']
    account = new_account(bid)
    if account.get('_safety_season_id') != context['season_id'] or \
            os.path.abspath(str(account.get('_safety_root'))) != os.path.abspath(context['season_root']) or \
            account_doc.get('season_id') != context['season_id'] or \
            os.path.commonpath((os.path.abspath(state['root']), os.path.abspath(context['competition_ledger_root']))) != \
            os.path.abspath(context['competition_ledger_root']):
        raise RuntimeError('active account/safety/ledger season mismatch')
    account.update({
        'participant_id': pid,
        'account_id': account_doc['account_id'],
        'season_id': account_doc['season_id'],
        'blogger_name': account_doc.get('name', BLOGGER_CONFIGS[bid]['name']),
        'cash': float(account_doc.get('cash', 0.0)),
        'positions': list(state['positions']),
        'total_pnl': float(account_doc.get('net_pnl', 0.0)),
        '_season_ledger_root': state['root'],
        '_season_ledger_state': state,
    })
    # Existing execution code treats this as the append-only in-memory trade list.
    account['trade_ids'] = list(state.get('fills', []))
    return account


def load_accounts_from_ledgers():
    """Replay the active season ledgers; legacy ``data/trades`` is archive-only."""
    context = _active_runtime_context()
    from competition.season_runtime import load_strategy_ledgers
    loaded = load_strategy_ledgers(context)
    accounts = {bid: _account_from_season_ledger(pid, loaded[pid], context)
                for pid, bid in PARTICIPANT_TO_LEGACY.items()}
    # Risk Sentinel is observation-only and deliberately has no economic ledger.
    sentinel = new_account('anti-demo')
    sentinel.update({
        'participant_id': 'P-RISK-SENTINEL',
        'account_id': 'RISK-SENTINEL-V1',
        'season_id': context['season_id'],
        'cash': 0.0,
        'positions': [],
        'trade_ids': [],
        'total_pnl': 0.0,
        'blogger_name': '反指哨兵',
    })
    accounts['anti-demo'] = sentinel
    return accounts


def _persist_active_strategy_ledgers(accounts):
    """Commit new runtime trades through the strategy transaction writer."""
    context = _active_runtime_context()
    from competition.season_runtime import load_strategy_ledgers
    from competition.transaction_writer import commit_strategy_transaction, TransactionIntegrityError
    existing = load_strategy_ledgers(context)
    for bid, pid in LEGACY_TO_PARTICIPANT.items():
        if bid not in accounts:
            continue
        acc = accounts[bid]
        state = existing[pid]
        # Build each post-trade effect from the prior committed snapshot.  The
        # old implementation passed the cycle-final account/positions to every
        # transaction, making intermediate equity snapshots lie when two trades
        # occurred in one cycle.
        account = dict(state['account'])
        positions = [dict(p) for p in state.get('positions', [])]
        previous_fills = len(state.get('fills', []))
        current_trades = list(acc.get('trade_ids', []))
        if len(current_trades) < previous_fills:
            raise TransactionIntegrityError(f'{pid} in-memory trade history regressed')
        for trade in current_trades[previous_fills:]:
            if not isinstance(trade, dict):
                raise TransactionIntegrityError(f'{pid} trade event is not an object')
            direction = str(trade.get('direction', '')).upper()
            symbol = trade.get('symbol')
            shares = int(trade.get('shares', 0))
            if not symbol or shares <= 0 or direction not in ('BUY', 'SELL'):
                raise TransactionIntegrityError(f'{pid} invalid transaction event: {trade}')
            if direction == 'BUY':
                positions.append({
                    'symbol': symbol,
                    'name': trade.get('name', symbol),
                    'code': trade.get('code', symbol),
                    'plan_id': trade.get('plan_id'),
                    'correlation_id': trade.get('correlation_id'),
                    'season_id': context['season_id'],
                    'participant_id': pid,
                    'account_id': state['account']['account_id'],
                    'shares': shares,
                    'cost_price': float(trade['price']),
                    'current_price': float(trade['price']),
                    'buy_date': trade.get('date'),
                })
            else:
                candidates = [p for p in positions if p.get('symbol') == symbol and int(p.get('shares', 0)) >= shares]
                if len(candidates) != 1:
                    raise TransactionIntegrityError(f'{pid} SELL position match ambiguous: {symbol}')
                position = candidates[0]
                trade.setdefault('entry_transaction_id', position.get('entry_transaction_id'))
                trade.setdefault('entry_price', position.get('cost_price'))
                trade.setdefault('entry_date', position.get('buy_date'))
                if not trade.get('entry_transaction_id'):
                    raise TransactionIntegrityError(f'{pid} SELL missing entry transaction reference')
                position['shares'] = int(position.get('shares', 0)) - shares
                if position['shares'] == 0:
                    positions.remove(position)
            net_cash_effect = trade.get('net_cash_effect')
            if net_cash_effect is None:
                gross = shares * float(trade.get('price', 0.0))
                fees = float(trade.get('commission', trade.get('fee', 0.0))) + float(trade.get('stamp_tax', 0.0))
                net_cash_effect = -(gross + fees) if direction == 'BUY' else gross - fees
            account['cash'] = round(float(account.get('cash', 0.0)) + float(net_cash_effect), 2)
            market_value = sum(
                int(p.get('shares', 0)) * float(p.get('current_price', p.get('cost_price', 0.0)))
                for p in positions
            )
            account['market_value'] = round(market_value, 2)
            account['total_equity'] = round(float(account['cash']) + market_value, 2)
            account['net_pnl'] = round(account['total_equity'] - float(account.get('initial_capital', INITIAL_CAPITAL)), 2)
            account['net_pnl_pct'] = round(account['net_pnl'] / float(account.get('initial_capital', INITIAL_CAPITAL)) * 100, 4)
            decision = trade.get('skill_decision') if isinstance(trade, dict) else None
            result = commit_strategy_transaction(
                state['root'], dict(account), [dict(p) for p in positions], trade, decision=decision
            )
            if isinstance(trade, dict):
                for key in ('transaction_id', 'order_id', 'fill_id', 'decision_id', 'idempotency_key'):
                    if result.get(key):
                        trade[key] = result[key]
    # Arena remains a derived view and is refreshed only after participant
    # ledger transactions have committed successfully.
    from competition.season_runtime import build_derived_arena_snapshot
    build_derived_arena_snapshot(context, persist=True)


def fetch_prices(symbols):
    """拉取腾讯实时行情（GBK 编码）"""
    from datasource.provider_adapters import TencentAdapter
    if not symbols:
        return {}
    clean_syms = list(set([s for s in symbols if s and len(s) >= 6]))
    if not clean_syms:
        return {}
    url = 'https://qt.gtimg.cn/q=' + ','.join(clean_syms) + '&_=' + str(int(datetime.datetime.now().timestamp() * 1000))
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                content = resp.read().decode('gbk', errors='replace')
            prices = {}
            for line in content.strip().split(';'):
                line = line.strip()
                if '="' not in line:
                    continue
                var_name = line.split('=')[0].strip()
                raw = line.split('="', 1)[1].rstrip('"')
                arr = raw.split('~')
                if len(arr) >= 40 and arr[1]:
                    sym = var_name.replace('v_', '')
                    observed = TencentAdapter._quote_observed_at(arr[30])
                    prices[sym] = {
                        'name': arr[1],
                        'code': arr[2],
                        'price': float(arr[3]) if arr[3] else 0.0,
                        'pct': float(arr[32]) if arr[32] else 0.0,
                        'amount_wan': float(arr[37]) if len(arr) > 37 and arr[37] else 0.0,
                        'observed_at': observed.isoformat() if observed else None,
                        'provider': 'Tencent',
                        'response_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                        'raw_payload': raw,
                    }
            return prices
        except Exception:
            continue
    return {}


def quote_is_usable(quote, symbol, *, for_buy=True, now=None):
    """Enforce the approved Sept23 90-second source-time rule at final use.

    Close settlement may consume a same-day post-close quote explicitly as
    close evidence. It is never relabelled an intraday quote or an entry quote.
    """
    import math
    from competition.activation_boundary import parse_explicit_timestamp
    import runtime_config as rc
    try:
        checked = now or _execution_local_timestamp()
        observed = parse_explicit_timestamp(quote.get('observed_at'))
        if observed is None or checked is None or checked.tzinfo is None:
            return False
        if quote.get('provider') != 'Tencent' or quote.get('code') != symbol[-6:] or not quote.get('response_sha256'):
            return False
        raw=quote.get('raw_payload')
        if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=quote['response_sha256']:
            return False
        fields=raw.split('~')
        from datasource.provider_adapters import TencentAdapter
        if len(fields)<40 or fields[2]!=quote['code'] or TencentAdapter._quote_observed_at(fields[30])!=observed or float(fields[3])!=float(quote['price']):
            return False
        price=float(quote.get('price',0))
        if not math.isfinite(price) or price<=0 or observed.date()!=checked.date() or observed>checked:
            return False
        calendar=rc.trading_calendar_status(checked.date())
        if calendar.get('status')!='VALID' or calendar.get('trading_day') is not True:
            return False
        phase=rc.market_phase(checked)
        if phase=='open':
            return rc.market_phase(observed)=='open' and observed.time().replace(tzinfo=None)>datetime.time(9,30) and (checked-observed).total_seconds()<=90
        return not for_buy and phase=='closed' and checked.time().replace(tzinfo=None)>=datetime.time(15,0) and observed.time().replace(tzinfo=None)>=datetime.time(15,0)
    except Exception:
        return False



def record_trade_feed(direction, blogger_name, name, sym, shares, price, extra='', price_type=''):
    """阶段五：成交动态流水（买卖监控卡数据源）。最近50条，失败不影响交易。
    阶段六：带 price_type（intraday/close）供前端口径徽章判定。"""
    try:
        p = os.path.join(BASE, 'state', 'trade_feed.json')
        items = []
        if os.path.exists(p):
            items = (json.load(open(p, encoding='utf-8')) or {}).get('items', [])
        rec = {'ts': now_str(), 'direction': direction, 'blogger': blogger_name,
               'name': name, 'symbol': sym, 'shares': shares, 'price': price,
               'extra': str(extra)[:60]}
        if price_type:
            rec['price_type'] = price_type
        items.append(rec)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'items': items[-50:]}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def calc_buy_fee(amount):
    return max(amount * COMMISSION_RATE, COMMISSION_MIN)


def calc_sell_fee(amount):
    return max(amount * COMMISSION_RATE, COMMISSION_MIN) + amount * STAMP_TAX


def _annotate_strategy_trade(trade, account, blogger_id, decision=None, position=None):
    """Attach stable transaction identity before the ledger writer sees a trade."""
    if not trade:
        return trade
    decision = decision or {}
    participant_id = account.get('participant_id') or LEGACY_TO_PARTICIPANT.get(blogger_id)
    trade['participant_id'] = participant_id
    if not account.get('season_id'):
        raise RuntimeError('trade season identity missing')
    trade['season_id'] = account['season_id']
    trade['account_id'] = account.get('account_id')
    if decision.get('decision_id'):
        trade['decision_id'] = decision['decision_id']
        trade['skill_version'] = decision.get('skill_version', '')
    elif position and position.get('plan_id'):
        trade['decision_id'] = f"{position['plan_id']}:{trade.get('direction', 'SELL')}"
    else:
        trade['decision_id'] = f"LEGACY-{participant_id}-{trade.get('direction')}-{trade.get('symbol')}-{trade.get('date')}"
    trade['idempotency_key'] = f"{trade['season_id']}:{participant_id}:{trade['decision_id']}:{trade.get('direction')}"
    if position:
        trade['entry_price'] = position.get('cost_price')
        trade['entry_date'] = position.get('buy_date')
        trade['buy_date'] = position.get('buy_date')
        trade['entry_transaction_id'] = position.get('entry_transaction_id')
        trade['entry_order_id'] = position.get('entry_order_id')
        trade['entry_fill_id'] = position.get('entry_fill_id')
        trade['entry_decision_id'] = position.get('entry_decision_id')
        trade['plan_id'] = position.get('plan_id')
    return trade


def execute_buy(account, blogger_id, sym, name, code, price, ratio, reason='主板大池量化建仓',
                price_type='close', decision_timestamp=None, decision_season_id=None):
    """按仓位比例建仓（整百股），记录交易。price_type: 'intraday'=盘中实时价 / 'close'=收盘结算价"""
    try:
        context = _active_runtime_context()
        if account.get('season_id') != context['season_id'] or \
                account.get('_safety_season_id') != context['season_id'] or \
                os.path.abspath(str(account.get('_safety_root'))) != os.path.abspath(context['season_root']) or \
                os.path.commonpath((os.path.abspath(str(account.get('_season_ledger_root'))),
                                    os.path.abspath(context['competition_ledger_root']))) != \
                os.path.abspath(context['competition_ledger_root']):
            return None
    except (OSError, ValueError, RuntimeError, TypeError):
        return None
    from competition.activation_boundary import check_new_entry_boundary
    boundary = check_new_entry_boundary(
        decision_timestamp, account=account,
        decision_season_ids=[decision_season_id],
        ledger_root=account.get('_season_ledger_root'),
    )
    if not boundary.allowed:
        print(f"  [BUY边界阻断] {blogger_id}/{sym}: {boundary.reason}")
        return None
    # A pick's cached price is signal context, never a current fill quote.
    quote = fetch_prices([sym]).get(sym)
    if not quote_is_usable(quote, sym):
        print(f"  [行情准入阻断] {blogger_id}/{sym}: missing/stale source quote")
        return None
    price = float(quote['price'])
    budget = account['cash'] * ratio
    buy_price = round(price * (1 + SLIPPAGE), 4)
    shares = int(budget / buy_price / 100) * 100
    if shares <= 0:
        return None
    amount = shares * buy_price
    fee = calc_buy_fee(amount)
    total_cost = amount + fee
    if total_cost > account['cash']:
        return None
    from analysis.decision_gate import deterministic_execution_safety
    safety = deterministic_execution_safety(
        action='BUY', account=account, positions=account.get('positions'),
        shares=shares, price=buy_price,
        real_trading=bool(account.get('real_trading', True)),
        new_entry_disabled=bool(account.get('new_entry_disabled', True)),
        execution_state_safe=bool(account.get('execution_state_safe', False)),
        trading_enabled=bool(account.get('trading_enabled', False)),
    )
    if safety['verdict'] == 'BLOCK':
        return None
    account['cash'] -= total_cost
    account['positions'].append({
        'symbol': sym,
        'name': name,
        'code': code,
        'shares': shares,
        'cost_price': buy_price,
        'high_since_buy': buy_price,
        'buy_date': today_str(),
    })
    return {
        'direction': 'BUY',
        'date': today_str(),
        'ts': now_str(),
        'price_type': price_type,
        'symbol': sym,
        'code': code,
        'name': name,
        'shares': shares,
        'price': buy_price,
        'fee': round(fee, 2),
        'reason': reason,
        'commission': round(fee, 4),
        'stamp_tax': 0.0,
        'gross_amount': round(amount, 4),
        'net_cash_effect': round(-total_cost, 4),
        'decision_timestamp': decision_timestamp,
        'decision_season_id': decision_season_id,
        'quote_evidence': quote,
    }


def execute_sell(account, blogger_id, sym, name, code, shares, price, reason, price_type='close'):
    """卖出（整百股），记录交易并计算净收益。price_type: 'intraday'=盘中实时价 / 'close'=收盘结算价"""
    quote = fetch_prices([sym]).get(sym)
    if not quote_is_usable(quote, sym, for_buy=False):
        print(f"  [风险退出行情不可用] {blogger_id}/{sym}: retain position; no fill")
        return None
    price = float(quote['price'])
    sell_price = round(price * (1 - SLIPPAGE), 4)
    amount = shares * sell_price
    fee = calc_sell_fee(amount)
    proceeds = amount - fee
    account['cash'] += proceeds
    return {
        'direction': 'SELL',
        'date': today_str(),
        'ts': now_str(),
        'price_type': price_type,
        'symbol': sym,
        'code': code,
        'name': name,
        'shares': shares,
        'price': sell_price,
        'fee': round(fee, 2),
        'reason': reason,
        'commission': round(max(amount * COMMISSION_RATE, COMMISSION_MIN), 4),
        'stamp_tax': round(amount * STAMP_TAX, 4),
        'gross_amount': round(amount, 4),
        'net_cash_effect': round(proceeds, 4),
        'quote_evidence': quote,
    }


def run_simulation(force_picks_scan=False, reset_to_mainboard=False, allow_trading=False, price_type='close'):
    """
    运行 AI 模拟盘：
    - 支持从 3103 家主板大池动态读取最新选股信号进行买入建仓；
    - 执行持仓止盈止损；
    - 账本加密锁存与 SHA-256 清单比对。
    阶段六（9:55 事故根治）：allow_trading 默认 False —— 只刷市值不写账本。
    仅 15:08 收盘结算与 execute_intraday 显式传 True；宁可少交易，不可多交易。
    """
    print(f'[{now_str()}] 🚀 AI 模拟盘引擎启动 (3103家主板大池联动版, 交易={"允许" if allow_trading else "只读"})...')
    active_context = _active_runtime_context()

    # 如果需要重置至纯正主板或首次运行，先清理旧账本
    if reset_to_mainboard:
        raise RuntimeError('active season reset_to_mainboard is forbidden; legacy data/trades is read-only archive')

    accounts = load_accounts_from_ledgers()

    # 1. 获取 3103 家主板大池的最新选股结果
    picks_list = []
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, 'r', encoding='utf-8') as f:
                data_json = json.load(f)
                picks_list = data_json.get('blogger_picks', [])
        except Exception:
            picks_list = []

    if not picks_list or force_picks_scan:
        try:
            from scan_mainboard_picks import scan_and_score_all
            picks_list = scan_and_score_all()
        except Exception as e:
            print(f'  [大池扫描提示] {e}')

    picks_by_blogger = {p.get('blogger_id'): p for p in picks_list if p.get('blogger_id')}

    # 2. 收集需要拉取实时行情的所有标的代码
    symbols_needed = set()
    for acc in accounts.values():
        for pos in acc['positions']:
            symbols_needed.add(pos['symbol'])
    for p in picks_list:
        if p.get('triggered') and p.get('stock_code') and p.get('stock_code') != '无':
            symbols_needed.add(format_symbol(p['stock_code']))

    prices = fetch_prices(list(symbols_needed))
    print(f'  [实时行情] 成功获取 {len(prices)} 个标的最新盘面数据')

    # 3. 检查现有持仓的止盈止损（卖出阶段，严格遵守 A 股 T+1 制度）
    for bid, acc in accounts.items():
        cfg = BLOGGER_CONFIGS.get(bid, {})
        remaining = []
        for pos in acc['positions']:
            # A 股 T+1 刚性约束：当天买入的股票当日锁定，次交易日起方可卖出
            buy_date = pos.get('buy_date', today_str())
            if buy_date >= today_str():
                remaining.append(pos)
                continue

            q = prices.get(pos['symbol'])
            if not quote_is_usable(q, pos['symbol'], for_buy=False):
                remaining.append(pos)
                continue
            price = q['price']
            # 阶段六验收修复④：15:08 结算同样使用持久化高点（与盘中口径一致）
            try:
                _hkey = f'{bid}+{pos["symbol"]}'
                _persisted_high = _load_position_highs().get(_hkey)
                if _persisted_high:
                    pos['high_since_buy'] = max(pos.get('high_since_buy', pos['cost_price']), float(_persisted_high), price)
                else:
                    pos['high_since_buy'] = max(pos.get('high_since_buy', pos['cost_price']), price)
            except Exception:
                pos['high_since_buy'] = max(pos.get('high_since_buy', pos['cost_price']), price)
            pnl_pct = (price - pos['cost_price']) / pos['cost_price']

            reason = None
            if cfg.get('take_profit') is not None and pnl_pct >= cfg['take_profit']:
                reason = f'触发固定止盈 +{cfg["take_profit"]*100:.1f}%'
            elif cfg.get('take_profit') is None and cfg.get('trailing'):
                drawdown = (pos['high_since_buy'] - price) / pos['high_since_buy']
                if drawdown >= cfg['trailing']:
                    reason = f'触发 ATR 动态追踪止盈（自高点回撤 {drawdown*100:.1f}%）'
            elif pnl_pct <= cfg.get('stop_loss', -0.05):
                reason = f'触发纪律止损 {cfg["stop_loss"]*100:.1f}%'

            if reason:
                if not allow_trading:
                    print(f'    [只读] {pos["name"]} 触发「{reason}」但本轮不交易（allow_trading=False）')
                    remaining.append(pos)
                    continue
                t = execute_sell(acc, bid, pos['symbol'], pos['name'], pos['code'], pos['shares'], price, reason,
                                 price_type=price_type)
                if t:
                    # ===== 阶段五·任务E：平仓自动归因（关联开仓快照）=====
                    try:
                        from analysis.attribution import attribute_close
                        _pnl_pct = (price - pos['cost_price']) / pos['cost_price'] * 100
                        _attr = attribute_close(bid, pos['symbol'], _pnl_pct)
                        print(f"    🧾 平仓归因: {pos['name']} {_attr.get('label')}")
                    except Exception as _ae:
                        print(f'    [归因失败] {type(_ae).__name__}: {str(_ae)[:60]}')
                    buy_cost = pos['shares'] * pos['cost_price']
                    sell_proceeds = t['price'] * t['shares']
                    net_pnl = sell_proceeds - t['fee'] - buy_cost
                    acc['total_pnl'] += net_pnl
                    t['net_pnl'] = round(net_pnl, 2)
                    t['pnl_pct'] = round(net_pnl / buy_cost * 100, 2)
                    _annotate_strategy_trade(t, acc, bid, position=pos)
                    acc['trade_ids'].append(t)
                    print(f'    🔻 {acc["blogger_name"]} 卖出 {pos["name"]}（{pos["code"]}）: {reason} @ {t["price"]}，净收益 {t["net_pnl"]}元')
                    record_trade_feed('SELL', acc['blogger_name'], pos['name'], pos['symbol'], t['shares'], t['price'], reason)
                    continue
            remaining.append(pos)
        acc['positions'] = remaining

    # 4. 动态承接 3103 家主板大池选股信号（买入建仓/调仓阶段）
    # 阶段六：allow_trading=False 时整段跳过（只读模式不建仓）
    print('  [大池买入检查] 正在根据 3100+ 家主板大池最新信号匹配建仓机会...')
    for bid, acc in accounts.items():
        if not allow_trading:
            break  # 只读模式：不建仓
        cfg = BLOGGER_CONFIGS.get(bid, {})
        max_pos = cfg.get('max_positions', 2)
        if max_pos <= 0:
            continue  # 如反指哨兵不主动买入

        # 检查是否还有持仓名额
        if len(acc['positions']) >= max_pos:
            continue

        pick = picks_by_blogger.get(bid)
        if not pick or pick.get('stock_code') == '无':
            continue

        decision = pick.get('skill_decision')
        if decision is not None:
            if decision.get('action') != 'BUY':
                continue
        else:
            if not pick.get('triggered'):
                continue
            sig = str(pick.get('signal', '')).upper()
            if 'BUY' not in sig and '低吸' not in sig and '建仓' not in sig and '突破' not in sig and '绝杀' not in sig:
                continue

        code = pick['stock_code']
        name = pick['stock_name'].split(' ')[0].split('(')[0]  # 提取干净名称
        sym = format_symbol(code)

        # 检查是否已经持有
        if any(p['symbol'] == sym or p['code'] == code for p in acc['positions']):
            continue

        # 盘中按实际价买入：优先使用博主信号触发时记录的实时价 current_price
        # （auto_update_hotspots 每 5 分钟刷新 blogger_picks，current_price = 触发那一刻的真实盘价）
        # 这样记录的买入价 = 交易时段信号触发时的实际价，而非收盘附近价
        price = float(pick.get('current_price') or 0.0)
        if price <= 0:
            q = prices.get(sym)
            price = q['price'] if q and q['price'] > 0 else 0.0
        if price <= 0:
            continue

        ratio = cfg.get('buy_ratio', 0.25)
        reason = f"3100+主板大池选股：{pick.get('trigger_reason', '符合Skill契约')[:40]}"

        # ===== G2.5：Skill 决策后只附加软风险上下文，不改写 action/ratio =====
        gate = {'verdict': 'PASS', 'reasons': ['门缺席放行'], 'snapshot': {}, 'thresholds': {}}
        try:
            from analysis.decision_gate import evaluate_decision
            contract_score = float(pick.get('score') or pick.get('quant_score') or 60.0)
            gate = evaluate_decision(symbol=sym, name=name, contract_score=contract_score,
                                     positions=acc.get('positions'), account=acc,
                                     blogger_id=bid)
            if gate.get('reasons'):
                reason = reason + ' | 软风险:' + '+'.join(gate['reasons'])[:46]
        except Exception as _ge:
            gate = {'verdict': 'PASS', 'reasons': [f'门异常放行({type(_ge).__name__})'],
                    'snapshot': {}, 'thresholds': {}}
            print(f'    [决策门异常降级] {type(_ge).__name__}: {str(_ge)[:60]}')

        safety = _buy_safety_check(acc, price, ratio)
        if safety['verdict'] == 'BLOCK':
            print(f"    [安全阻断] {bid}/{sym}: {','.join(safety['hard_blocks'])}")
            continue
        decision_timestamp = decision.get('timestamp') if isinstance(decision, dict) else pick.get('timestamp')
        t = execute_buy(acc, bid, sym, name, code, price, ratio, reason,
                        price_type=price_type, decision_timestamp=decision_timestamp,
                        decision_season_id=(decision.get('season_id') if isinstance(decision, dict) else pick.get('season_id')))
        if t:
            # ===== 阶段五·任务E：决策快照同步留痕（下单事务内，禁止事后补）=====
            try:
                from datasource import db as _hdb
                import json as _json
                _hdb.init_decision_trace()
                _hdb.insert_decision_trace({
                    'ts': now_str(), 'trade_date': today_str(), 'blogger_id': bid,
                    'participant_id': decision.get('participant_id') if decision else '',
                    'decision_id': decision.get('decision_id') if decision else '',
                    'skill_version': decision.get('skill_version') if decision else '',
                    'original_action': decision.get('action') if decision else 'BUY',
                    'candidate_evidence': decision.get('candidate_evidence') if decision else {},
                    'rules_triggered': decision.get('skill_rules_triggered') if decision else [],
                    'rules_failed': decision.get('skill_rules_failed') if decision else [],
                    'position_plan': decision.get('position_plan') if decision else {},
                    'exit_plan': decision.get('exit_plan') if decision else {},
                    'soft_risk_context': decision.get('soft_risk_context') if decision else [],
                    'safety_verdict': safety.get('verdict'),
                    'safety_reason': safety.get('hard_blocks', []),
                    'symbol': sym, 'name': name, 'action': 'BUY',
                    'contract_score': float(pick.get('score') or pick.get('quant_score') or 60.0),
                    'gate_verdict': gate.get('verdict'),
                    'gate_adjustments': ';'.join(gate.get('reasons', []))[:200],
                    'factor_snapshot': _json.dumps(gate.get('snapshot', {}), ensure_ascii=False, default=str)[:1500],
                    'regime': (gate.get('snapshot') or {}).get('regime', ''),
                    'priced_in': (gate.get('snapshot') or {}).get('priced_in'),
                    'debate_penalty': (gate.get('snapshot') or {}).get('debate_penalty'),
                    'price': t.get('price'), 'ratio': ratio, 'reason': reason[:200],
                })
            except Exception as _te:
                print(f'    [决策快照写入失败] {type(_te).__name__}: {str(_te)[:60]}')
            _annotate_strategy_trade(t, acc, bid, decision=decision)
            acc['trade_ids'].append(t)
            print(f'    ✨ 🟢 {acc["blogger_name"]} 自动买入主板标的: {name}（{code}） {t["shares"]}股 @ {t["price"]}元 (占用现金 {t["shares"]*t["price"]:.2f}元)')
            record_trade_feed('BUY', acc['blogger_name'], name, sym, t['shares'], t['price'], reason)

    # 5. 汇总账户状态与净值
    summary = {'updated_at': now_str(), 'capital_per_blogger': INITIAL_CAPITAL, 'accounts': {}}
    for bid, acc in accounts.items():
        market_value = 0.0
        positions_view = []
        for pos in acc['positions']:
            q = prices.get(pos['symbol'])
            price = q['price'] if q and q['price'] > 0 else pos['cost_price']
            mv = pos['shares'] * price
            market_value += mv
            positions_view.append({
                'symbol': pos['symbol'],
                'name': pos['name'],
                'code': pos['code'],
                'shares': pos['shares'],
                'cost_price': round(pos['cost_price'], 4),
                'current_price': round(price, 4),
                'market_value': round(mv, 2),
                'pnl_pct': round((price - pos['cost_price']) / pos['cost_price'] * 100, 2),
                'buy_date': pos.get('buy_date', today_str()),
            })
        total_value = acc['cash'] + market_value
        pnl = total_value - INITIAL_CAPITAL
        summary['accounts'][bid] = {
            'blogger_id': bid,
            'blogger_name': acc['blogger_name'],
            'cash': round(acc['cash'], 2),
            'market_value': round(market_value, 2),
            'total_value': round(total_value, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl / INITIAL_CAPITAL * 100, 2),
            'positions': positions_view,
            'trades': acc['trade_ids'],
            'rules': {
                'stop_loss': BLOGGER_CONFIGS[bid].get('stop_loss'),
                'take_profit': BLOGGER_CONFIGS[bid].get('take_profit'),
                'trailing': BLOGGER_CONFIGS[bid].get('trailing'),
            },
        }

    # 6. 账本落盘与 SHA-256 清单防篡改锁存（阶段六：只读模式不写账本，保 SHA 稳定）
    ledger_status = {'status': 'OK', 'checked_at': now_str(), 'files': {}}
    if allow_trading:
        _persist_active_strategy_ledgers(accounts)
        for bid, acc in accounts.items():
            if bid in LEGACY_TO_PARTICIPANT:
                ledger_status['files'][bid] = {
                    'trades': len(acc['trade_ids']),
                    'path': os.path.join(active_context['competition_ledger_root'], LEGACY_TO_PARTICIPANT[bid], 'fills.jsonl'),
                }
    else:
        # 只读模式：报告 active season ledger 状态，不读取 legacy data/trades
        for bid in accounts:
            if bid in LEGACY_TO_PARTICIPANT:
                ledger_status['files'][bid] = {
                    'trades': len(accounts[bid]['trade_ids']),
                    'path': os.path.join(active_context['competition_ledger_root'], LEGACY_TO_PARTICIPANT[bid], 'fills.jsonl'),
                }

    summary['ledger_status'] = ledger_status
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 7. 合并进 workbench/data.json
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}
        data['simulation'] = {
            'updated_at': summary['updated_at'],
            'capital_per_blogger': INITIAL_CAPITAL,
            'accounts': summary['accounts'],
        }
        data['ledger_status'] = ledger_status
        with open(DATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # 8. 同步更新 index.html 内嵌
    try:
        from auto_update_hotspots import embed_data_into_html
        embed_data_into_html(data)
    except Exception as e:
        print(f'  [HTML内嵌提示] {e}')

    active_pos_cnt = sum(len(v['positions']) for v in summary['accounts'].values())
    total_trades_cnt = sum(len(v['trades']) for v in summary['accounts'].values())
    print(f'[{now_str()}] ✅ AI 模拟盘更新完成：共 {active_pos_cnt} 个主板持仓标的，累计 {total_trades_cnt} 笔交易，SHA-256 账本验证状态 = {ledger_status["status"]}')
    return summary


def reset_all_accounts_to_clean_state():
    """
    全量重置各博主账户至初始纯净状态：
    - 本金统一为 10 万元 (¥100,000.00)
    - 0 持仓 (100% 现金)，0 笔交易，收益率 0.00%
    - 重新生成全新账本文件及 SHA-256 manifest.json 清单
    - 同步落盘 simulation_state.json、data.json 并注入 index.html
    """
    _active_runtime_context()
    raise RuntimeError(
        'reset_all_accounts_to_clean_state is LEGACY/MANUAL MAINTENANCE ONLY; '
        'active season economic reset is forbidden'
    )

    # Legacy implementation retained below for historical/manual namespaces.
    print(f'[{now_str()}] 🔄 执行 AI 模拟盘全量平仓与 10 万元本金初始化重置...')
    os.makedirs(TRADES_DIR, exist_ok=True)

    summary = {'updated_at': now_str(), 'capital_per_blogger': INITIAL_CAPITAL, 'accounts': {}}
    ledger_status = {'status': 'OK', 'checked_at': now_str(), 'files': {}}

    for bid, cfg in BLOGGER_CONFIGS.items():
        # 1. 写入干净的 10 万元初始账本
        ledger = {
            'blogger_id': bid,
            'blogger_name': cfg['name'],
            'initial_capital': INITIAL_CAPITAL,
            'trades': [],
        }
        path = os.path.join(TRADES_DIR, bid + '.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        digest = sha256_of_file(path)
        ledger_status['files'][bid] = {'trades': 0, 'sha256': digest[:16]}

        summary['accounts'][bid] = {
            'blogger_id': bid,
            'blogger_name': cfg['name'],
            'cash': INITIAL_CAPITAL,
            'market_value': 0.0,
            'total_value': INITIAL_CAPITAL,
            'pnl': 0.0,
            'pnl_pct': 0.0,
            'positions': [],
            'trades': [],
            'rules': {
                'stop_loss': cfg.get('stop_loss'),
                'take_profit': cfg.get('take_profit'),
                'trailing': cfg.get('trailing'),
            },
        }

    # 2. 生成全新的 manifest.json
    manifest_path = os.path.join(TRADES_DIR, 'manifest.json')
    new_manifest = {
        'generated_at': now_str(),
        'files': ledger_status['files'],
    }
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(new_manifest, f, ensure_ascii=False, indent=2)

    summary['ledger_status'] = ledger_status
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 3. 同步至 workbench/data.json
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}
        data['simulation'] = {
            'updated_at': summary['updated_at'],
            'capital_per_blogger': INITIAL_CAPITAL,
            'accounts': summary['accounts'],
        }
        data['ledger_status'] = ledger_status
        with open(DATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # 4. 同步至 index.html 内嵌
    try:
        from auto_update_hotspots import embed_data_into_html
        embed_data_into_html(data)
    except Exception as e:
        print(f'  [HTML内嵌提示] {e}')

    print(f'[{now_str()}] 🎉 重置完成！6 位博主均已平仓为 10 万元纯现金 (¥100,000.00)，SHA-256 账本已锁存，明日开盘重新起跑！')
    return summary


# =========================================================================
# 阶段六·B1：execute_intraday — 盘中实时交易执行器
# =========================================================================

_TRADE_LOCK = None  # msvcrt 文件锁（与 daemon.lock 同模式，单例守卫）

def _acquire_trade_lock(timeout=0):
    """抢 simulation.lock 排他锁。返回 lock file 句柄（失败 None）。"""
    import msvcrt
    lock_path = os.path.join(BASE, 'state', 'simulation.lock')
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    f = open(lock_path, 'a+b')
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


def _load_intraday_config():
    """从 system_params.json 读盘中交易参数，缺省走保守默认。"""
    cfg = {
        'enabled': True,
        'trade_windows': [['09:37', '11:28'], ['13:05', '14:55']],
        'cooldown_minutes': 30,
        'signal_confirm_minutes': 0,
        'execute_levels': {
            'contract_stop': True, 'contract_take_profit': True,
            'trailing': True, 'L1_L2_L4_L5': 'alert_only'
        },
        'limit_up_block_buy': True,
    }
    try:
        p = os.path.join(BASE, 'config', 'system_params.json')
        d = json.load(open(p, encoding='utf-8'))
        c = d.get('categories', {}).get('intraday_trading', {}).get('params', {}) or {}
        cfg.update({k: v for k, v in c.items() if k in cfg})
    except Exception:
        pass
    return cfg


def _execution_local_timestamp(value=None):
    """Return an aware Asia/Shanghai timestamp or None.

    Direct callers may inject a clock only as an explicit, timezone-aware
    value for isolated tests.  Naive values are rejected rather than being
    interpreted using the Windows host timezone.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('Asia/Shanghai')
    if value is None:
        return datetime.datetime.now(tz)
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        return None
    local = value.astimezone(tz)
    if local.utcoffset() != datetime.timedelta(hours=8):
        return None
    return local


def _in_trade_window(cfg, current_timestamp=None):
    """当前时间是否在合法交易窗口内（午休静默）。"""
    local = _execution_local_timestamp(current_timestamp)
    if local is None:
        return False
    now_t = local.strftime('%H:%M')
    for start, end in cfg.get('trade_windows', []):
        if start <= now_t <= end:
            return True
    return False


def _load_intraday_executed():
    """当日已执行防重复表 {date: {blogger_id+symbol+direction: 1}}"""
    p = os.path.join(BASE, 'state', 'intraday_executed.json')
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding='utf-8')) or {}
    except Exception:
        return {}


def _save_intraday_executed(d):
    p = os.path.join(BASE, 'state', 'intraday_executed.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def _today_buy_count(accounts, blogger_id):
    """阶段六·B2：按账本统计当日 BUY 笔数（替代旧的结算批次计数）。"""
    cnt = 0
    for t in accounts[blogger_id].get('trade_ids', []):
        if t.get('direction') == 'BUY' and t.get('date') == today_str():
            cnt += 1
    return cnt


def _cooldown_pass(blogger_id, symbol, minutes, cache):
    """同博主同标的冷却期：当前时间 - 上次成交 >= minutes 才放行。"""
    from datetime import datetime as _dt
    key = f'{blogger_id}+{symbol}'
    last_ts = (cache.get(today_str()) or {}).get(key)
    if not last_ts:
        return True
    try:
        last = _dt.strptime(last_ts, '%Y-%m-%d %H:%M:%S')
        delta = (_dt.now() - last).total_seconds() / 60.0
        return delta >= minutes
    except Exception:
        return True


def _record_executed(blogger_id, symbol, cache):
    cache.setdefault(today_str(), {})[f'{blogger_id}+{symbol}'] = now_str()
    return cache


def _load_position_highs():
    """阶段六验收修复④：盘中 Replay 会把 high_since_buy 重置为成本价，
    导致 ai-alpha 追踪止盈(trailing)跨轮失忆。用 state 文件持久化持仓高点。"""
    p = os.path.join(BASE, 'state', 'position_highs.json')
    try:
        return json.load(open(p, encoding='utf-8')) or {}
    except Exception:
        return {}


def _save_position_highs(d):
    p = os.path.join(BASE, 'state', 'position_highs.json')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def execute_intraday(force=False, current_timestamp=None, calendar_path=None):
    """
    阶段六·B1+B2：盘中每 5 分钟执行的实时交易函数。
    - 卖出：契约止盈/止损/追踪线盘中触发即按实时价±滑点成交
    - 买入：六部选股 triggered + 决策门过 + 资金/仓位/日计数/冷却/涨停保护
    - 时段闸门、并发锁、当日防重复、T+1 校验
    返回摘要字符串（写入心跳）。失败降级：不报错停摆。
    """
    # Defense in depth for direct production callers.  ``force`` is retained
    # for compatibility with controlled callers, but it never bypasses the
    # canonical exchange calendar or legal clock window.
    try:
        import runtime_config as _runtime_config
        # A production caller cannot manufacture the exchange clock by
        # passing ``current_timestamp``.  Tests use the explicit private
        # clock seam (patching _execution_local_timestamp) in an isolated
        # namespace; live execution always reads the host clock here.
        if current_timestamp is not None:
            return '禁止外部时钟注入，使用交易所运行时钟'
        check_dt = _execution_local_timestamp()
        if check_dt is None:
            return '无效交易时区时间，禁止盘中交易'
        canonical_calendar = os.path.realpath(
            _runtime_config.A_SHARE_TRADING_CALENDAR_JSON)
        requested_calendar = os.path.realpath(
            calendar_path or _runtime_config.A_SHARE_TRADING_CALENDAR_JSON)
        if requested_calendar != canonical_calendar:
            return '非canonical A股交易日历，禁止盘中交易'
        calendar_status = _runtime_config.trading_calendar_status(
            check_dt.date(), path=canonical_calendar)
        if calendar_status.get('status') != _runtime_config.TradingCalendarStatus.VALID:
            return 'A股交易日历不可用，禁止盘中交易'
        if not calendar_status.get('trading_day'):
            return '非A股交易日，禁止盘中交易'
    except Exception as exc:
        return f'A股交易日历校验失败:{type(exc).__name__}'

    cfg = _load_intraday_config()
    if not cfg.get('enabled', True):
        return '盘中交易已禁用'

    if not _in_trade_window(cfg, check_dt):
        return f'非交易窗口跳过(now={check_dt.strftime("%H:%M")})'

    # 并发锁
    lf = _acquire_trade_lock()
    if lf is None:
        return 'simulation.lock 抢锁失败,本轮跳过'

    try:
        accounts = load_accounts_from_ledgers()
        # 行情拉取
        syms = [p['symbol'] for acc in accounts.values() for p in acc['positions']]
        prices = fetch_prices(syms)
        if not prices and syms:
            return f'行情拉取失败(持仓{len(syms)}只),本轮跳过(诚实不交易)'

        executed = _load_intraday_executed()
        highs = _load_position_highs()
        actions = []  # (direction, blogger, name, sym, price, reason)
        daily_buy_cnt = {bid: _today_buy_count(accounts, bid) for bid in accounts}

        # ============ 卖出段 ============
        for bid, acc in accounts.items():
            cfg_b = BLOGGER_CONFIGS.get(bid, {})
            for pos in list(acc['positions']):
                # T+1：当日买入锁定
                if pos.get('buy_date', '') >= today_str():
                    continue
                q = prices.get(pos['symbol'])
                if not quote_is_usable(q, pos['symbol'], for_buy=False):
                    continue
                price = q['price']
                pnl_pct = (price - pos['cost_price']) / pos['cost_price']
                # 验收修复④：高点持久化（跨轮记忆，Replay 不再失忆）
                hkey = f'{bid}+{pos["symbol"]}'
                pos['high_since_buy'] = max(float(highs.get(hkey) or pos.get('high_since_buy') or pos['cost_price']), price)
                highs[hkey] = pos['high_since_buy']

                reason = None
                if cfg_b.get('take_profit') is not None and pnl_pct >= cfg_b['take_profit']:
                    reason = f'盘中触发固定止盈 +{cfg_b["take_profit"]*100:.1f}%'
                elif cfg_b.get('take_profit') is None and cfg_b.get('trailing'):
                    drawdown = (pos['high_since_buy'] - price) / pos['high_since_buy']
                    if drawdown >= cfg_b['trailing']:
                        reason = f'盘中触发 ATR 动态追踪止盈（自高点回撤 {drawdown*100:.1f}%）'
                elif pnl_pct <= cfg_b.get('stop_loss', -0.05):
                    reason = f'盘中触发纪律止损 {cfg_b["stop_loss"]*100:.1f}%'
                if not reason:
                    continue

                # 当日同标的不重复（卖出侧按 blogger+symbol+date 一次）
                key = f'{bid}+{pos["symbol"]}+SELL'
                if executed.get(today_str(), {}).get(key):
                    continue

                t = execute_sell(acc, bid, pos['symbol'], pos['name'], pos['code'],
                                 pos['shares'], price, reason, price_type='intraday')
                if t:
                    try:
                        from analysis.attribution import attribute_close
                        attr = attribute_close(bid, pos['symbol'],
                                               (price - pos['cost_price']) / pos['cost_price'] * 100)
                        attr_lbl = attr.get('label', '')
                    except Exception:
                        attr_lbl = '归因失败'
                    buy_cost = pos['shares'] * pos['cost_price']
                    net_pnl = t['shares'] * t['price'] - t['fee'] - buy_cost
                    t['net_pnl'] = round(net_pnl, 2)
                    t['pnl_pct'] = round(net_pnl / buy_cost * 100, 2)
                    _annotate_strategy_trade(t, acc, bid, position=pos)
                    acc['trade_ids'].append(t)
                    record_trade_feed('SELL', acc['blogger_name'], pos['name'],
                                      pos['symbol'], t['shares'], t['price'],
                                      f'{reason} | 归因:{attr_lbl}', price_type='intraday')
                    executed = _record_executed(bid, pos['symbol'], executed)
                    executed[today_str()][key] = now_str()
                    highs.pop(hkey, None)  # 平仓清除高点记忆
                    actions.append(('SELL', acc['blogger_name'], pos['name'], t['price'], reason))

        # ============ 买入段 ============
        picks_list = []
        try:
            if os.path.exists(DATA_PATH):
                with open(DATA_PATH, 'r', encoding='utf-8') as f:
                    picks_list = json.load(f).get('blogger_picks', []) or []
        except Exception:
            pass
        picks_by_blogger = {p.get('blogger_id'): p for p in picks_list if p.get('blogger_id')}

        for bid, acc in accounts.items():
            cfg_b = BLOGGER_CONFIGS.get(bid, {})
            max_pos = cfg_b.get('max_positions', 2)
            if max_pos <= 0 or len(acc['positions']) >= max_pos:
                continue
            # 日机会数属于各 Skill Contract，不是统一主观投资门
            if daily_buy_cnt.get(bid, 0) >= int(cfg_b.get('max_daily_buys', 3)):
                continue
            pick = picks_by_blogger.get(bid)
            if not pick or pick.get('stock_code') in (None, '', '无'):
                continue
            decision = pick.get('skill_decision')
            if decision is not None:
                if decision.get('action') != 'BUY':
                    continue
            else:
                if not pick.get('triggered'):
                    continue
                sig = str(pick.get('signal', '')).upper()
                if not any(k in sig for k in ('BUY', '低吸', '建仓', '突破', '绝杀')):
                    continue
            code = pick['stock_code']
            name = pick.get('stock_name', '').split(' ')[0].split('(')[0]
            sym = format_symbol(code)
            if any(p['symbol'] == sym or p['code'] == code for p in acc['positions']):
                continue
            # 冷却
            if not _cooldown_pass(bid, sym, cfg.get('cooldown_minutes', 30), executed):
                continue
            price = float(pick.get('current_price') or prices.get(sym, {}).get('price') or 0)
            if price <= 0:
                continue
            # 涨停保护（阶段六验收修复：原引用卖出循环遗留的 q 变量，存在 NameError 与错标的风险）
            qq = prices.get(sym) or {}
            if cfg.get('limit_up_block_buy', True) and qq.get('pct', 0) >= 9.5:
                continue

            # G2.5：只记录 soft-risk context；不得中央 VETO 或改写 Skill 仓位
            gate = {
                'verdict': 'PASS',
                'reasons': [],
                'soft_risk_context': [],
                'snapshot': {},
                'thresholds': {},
            }
            try:
                from analysis.decision_gate import evaluate_decision
                contract_score = float(pick.get('score') or pick.get('quant_score') or 60.0)
                gate = evaluate_decision(symbol=sym, name=name, contract_score=contract_score,
                                         positions=acc.get('positions'), account=acc, blogger_id=bid)
                ratio = cfg_b.get('buy_ratio', 0.25)
            except Exception:
                gate['reasons'] = ['SOFT_RISK_CONTEXT_UNAVAILABLE']
                ratio = cfg_b.get('buy_ratio', 0.25)

            safety = _buy_safety_check(acc, price, ratio)
            if safety['verdict'] == 'BLOCK':
                print(f"    [安全阻断] {bid}/{sym}: {','.join(safety['hard_blocks'])}")
                continue
            t = execute_buy(acc, bid, sym, name, code, price, ratio,
                            f'盘中选股: {pick.get("trigger_reason", "契约触发")[:30]} | 门:{"+".join(gate.get("reasons", []))[:30]}',
                            price_type='intraday',
                            decision_timestamp=(decision.get('timestamp') if isinstance(decision, dict) else pick.get('timestamp')),
                            decision_season_id=(decision.get('season_id') if isinstance(decision, dict) else pick.get('season_id')))
            if t:
                try:
                    from datasource import db as _hdb
                    _hdb.init_decision_trace()
                    _hdb.insert_decision_trace({
                        'ts': now_str(), 'trade_date': today_str(), 'blogger_id': bid,
                        'participant_id': decision.get('participant_id') if decision else '',
                        'decision_id': decision.get('decision_id') if decision else '',
                        'skill_version': decision.get('skill_version') if decision else '',
                        'original_action': decision.get('action') if decision else 'BUY',
                        'candidate_evidence': decision.get('candidate_evidence') if decision else {},
                        'rules_triggered': decision.get('skill_rules_triggered') if decision else [],
                        'rules_failed': decision.get('skill_rules_failed') if decision else [],
                        'position_plan': decision.get('position_plan') if decision else {},
                        'exit_plan': decision.get('exit_plan') if decision else {},
                        'soft_risk_context': decision.get('soft_risk_context') if decision else [],
                        'safety_verdict': safety.get('verdict'),
                        'safety_reason': safety.get('hard_blocks', []),
                        'symbol': sym, 'name': name, 'action': 'BUY',
                        'contract_score': float(pick.get('score') or 60.0),
                        'gate_verdict': gate.get('verdict'),
                        'gate_adjustments': ';'.join(gate.get('reasons', []))[:200],
                        'price': t.get('price'), 'ratio': ratio,
                        'reason': (t.get('reason') or '')[:200],
                    })
                except Exception:
                    pass
                _annotate_strategy_trade(t, acc, bid, decision=decision)
                acc['trade_ids'].append(t)
                highs[f'{bid}+{sym}'] = t['price']
                record_trade_feed('BUY', acc['blogger_name'], name, sym, t['shares'], t['price'],
                                  t.get('reason', '')[:60], price_type='intraday')
                executed = _record_executed(bid, sym, executed)
                actions.append(('BUY', acc['blogger_name'], name, t['price'], '选股触发+门过'))
                daily_buy_cnt[bid] = daily_buy_cnt.get(bid, 0) + 1

        _save_intraday_executed(executed)
        _save_position_highs(highs)
        # 阶段六验收修复③（双执行根治）：本函数已完成全部交易判定与内存账本更新，
        # 此处**自己写盘**账本+manifest；随后的 run_simulation 只读刷市值——
        # 严禁在此传 allow_trading=True：旧账本 Replay 看不到本函数刚成交的单，
        # 会把同一信号再执行一遍（09-03 湖南白银 trace/feed 双写事故根因）。
        if actions:
            _persist_active_strategy_ledgers(accounts)
        # 只读刷市值 + summary + data.json + HTML 内嵌（replay 到最新账本，买入段被"已持有"挡住）
        run_simulation(force_picks_scan=False, reset_to_mainboard=False,
                       allow_trading=False)
        return f'盘中执行: 成交{len(actions)}笔(买{sum(1 for a in actions if a[0]=="BUY")}/卖{sum(1 for a in actions if a[0]=="SELL")})'
    except Exception as e:
        return f'盘中执行异常: {type(e).__name__}: {str(e)[:80]}'
    finally:
        try:
            import msvcrt
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
            lf.close()
        except Exception:
            pass


def audit_ledger():
    """阶段六·B4 对账命令：打印当日成交清单 + 账本 SHA256（只读）。"""
    print(f'\n[{now_str()}] 账本对账审计：')
    intraday = []
    context = _active_runtime_context()
    accounts = load_accounts_from_ledgers()
    for bid in BLOGGER_CONFIGS:
        participant_id = LEGACY_TO_PARTICIPANT.get(bid)
        if not participant_id:
            continue
        path = os.path.join(context['competition_ledger_root'], participant_id, 'fills.jsonl')
        if not os.path.exists(path):
            continue
        try:
            rows = []
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    if line.strip():
                        rows.append(json.loads(line))
        except Exception as e:
            print(f'  {bid}: 读取失败 {e}')
            continue
        digest = sha256_of_file(path)
        print(f'  {bid}: {len(rows)}笔 sha256={digest[:16]}')
        for t in rows:
            if str(t.get('date', t.get('timestamp', '')))[:10] == today_str():
                intraday.append((bid, t))
    print(f'\n当日({today_str()})成交共{len(intraday)}笔:')
    for bid, t in intraday:
        direction = t.get('direction', t.get('action', '?'))
        print(f'  [{direction}] {bid} {t.get("name", t.get("symbol"))} {t.get("shares")}股 @ {t.get("price")} '
              f'({t.get("price_type", "close")}={t.get("ts", t.get("timestamp", ""))}) {t.get("reason", "")[:30]}')
    print(f'\n活动赛季: {context["season_id"]}; 账户数={len(accounts)}')


if __name__ == '__main__':
    if '--reset' in sys.argv or '-r' in sys.argv:
        reset_all_accounts_to_clean_state()
    elif '--audit' in sys.argv:
        audit_ledger()
    elif '--intraday' in sys.argv:
        force = '--force' in sys.argv
        print(execute_intraday(force=force))
    else:
        run_simulation(force_picks_scan=False, reset_to_mainboard=False)
