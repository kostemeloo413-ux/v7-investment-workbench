# -*- coding: utf-8 -*-
"""
scripts/competition/accounts.py
Phase 11: 7 选手独立虚拟账户管理器（Arena Accounts Manager）

核心职责（《指南》第 1.11 & 17 节）：
1. 7 位选手物理隔离：
   - 6 个博主：hanshu, yintwentao, liao-xiaoai, cool, ai-alpha, anti-demo
   - 1 个 AI量化：ai-quant
2. 初始条件绝对公平：
   - 统一初始本金：100,000.0 元
   - 独立 cash、独立 positions、独立 trade_history；
3. 持久化存储：
   - 保存在 state/competition_accounts.json 中，支持赛季开账与重置。
"""

import os
import sys
import json
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from simulated_trading import BLOGGER_CONFIGS
from ai_quant.storage_utils import atomic_write_json, strict_load_json, CriticalStateStatus

ARENA_STATE_PATH = os.path.join(_BASE, 'state', 'competition_accounts.json')
INITIAL_CAPITAL = 100000.0

SEVEN_PLAYERS = {
    'hanshu': {'name': '韩叔 (趣聊财经)', 'role': 'blogger', 'icon': '🎯'},
    'yintwentao': {'name': '殷文涛 (梵爹)', 'role': 'blogger', 'icon': '🔥'},
    'liao-xiaoai': {'name': '廖小爱', 'role': 'blogger', 'icon': '🛡️'},
    'cool': {'name': '酷儿 (Cool)', 'role': 'blogger', 'icon': '⚡'},
    'ai-alpha': {'name': 'AI-Alpha 智脑', 'role': 'blogger', 'icon': '🧠'},
    'anti-demo': {'name': '极短线情绪反指哨兵', 'role': 'blogger', 'icon': '🚨'},
    'ai-quant': {'name': 'AI量化交易员', 'role': 'ai_quant', 'icon': '🤖'},
}


class ArenaAccountsManager:
    def __init__(self, season_id="SEASON_001", state_path=ARENA_STATE_PATH,
                 player_specs=None, explicit_initialize=None):
        self.season_id = season_id
        self.state_path = os.path.abspath(state_path)
        self.player_specs = player_specs or SEVEN_PLAYERS
        # Legacy/unit-test namespaces may explicitly initialize. The active
        # V7 season is strict by default: missing/corrupt state is not a reset.
        self.explicit_initialize = (
            not season_id.startswith('V7-SEASON-')
            if explicit_initialize is None else bool(explicit_initialize)
        )
        self.accounts = {}
        self.load_or_init()

    def load_or_init(self):
        result = strict_load_json(self.state_path, expected_type=dict)
        if result.status == CriticalStateStatus.VALID:
            d = result.value
            if d.get('season_id') != self.season_id:
                raise RuntimeError(f'Arena season mismatch: {self.state_path}')
            if not isinstance(d.get('accounts'), dict):
                raise RuntimeError(f'Arena schema error: accounts must be object')
            self.accounts = d['accounts']
            return
        if result.status != CriticalStateStatus.MISSING:
            raise RuntimeError(f'Arena critical state {result.status}: {result.error}')
        if not self.explicit_initialize:
            raise RuntimeError(
                f'Arena state missing in normal runtime: {self.state_path}; '
                'explicit initialization is required'
            )
        self.reset_season(self.season_id)

    def reset_season(self, season_id=None):
        """重置赛季账户为 10 万元现金净值"""
        if season_id:
            self.season_id = season_id
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.accounts = {}
        for pid, pmeta in self.player_specs.items():
            capital = float(pmeta.get('initial_capital', INITIAL_CAPITAL))
            self.accounts[pid] = {
                'player_id': pid,
                'account_id': pmeta.get('account_id', f'{self.season_id}:{pid}'),
                'season_id': self.season_id,
                'name': pmeta['name'],
                'role': pmeta['role'],
                'icon': pmeta['icon'],
                'return_bearing': bool(pmeta.get('return_bearing', True)),
                'ranking_enabled': bool(pmeta.get('ranking_enabled', True)),
                'trading_account': bool(pmeta.get('trading_account', True)),
                'initial_capital': capital,
                'cash': capital,
                'market_value': 0.0,
                'total_equity': capital,
                'net_pnl': 0.0,
                'net_pnl_pct': 0.0,
                'peak_equity': capital,
                'max_drawdown_pct': 0.0,
                'positions': [],    # [{symbol, name, shares, cost_price, buy_date, fee}...]
                'trade_count': 0,
                'win_count': 0,
                'loss_count': 0,
                'updated_at': now_str
            }
        self.save()

    def update_positions_quotes(self, quotes_map):
        """根据最新盘面价格刷新市值与浮动盈亏"""
        for pid, acc in self.accounts.items():
            mv = 0.0
            for pos in acc.get('positions', []):
                sym = pos['symbol']
                q = quotes_map.get(sym) or quotes_map.get(f"sh{sym}") or quotes_map.get(f"sz{sym}")
                if q and float(q.get('price', 0)) > 0:
                    pos['current_price'] = float(q['price'])
                cur_p = pos.get('current_price', pos.get('cost_price', 0))
                mv += pos['shares'] * cur_p
            acc['market_value'] = round(mv, 2)
            acc['total_equity'] = round(acc['cash'] + mv, 2)
            baseline = float(acc.get('initial_capital', INITIAL_CAPITAL))
            acc['net_pnl'] = round(acc['total_equity'] - baseline, 2)
            acc['net_pnl_pct'] = round((acc['net_pnl'] / baseline) * 100.0, 2) if baseline else 0.0
            # 更新回撤
            if acc['total_equity'] > acc.get('peak_equity', INITIAL_CAPITAL):
                acc['peak_equity'] = acc['total_equity']
            peak = acc['peak_equity']
            dd = round((peak - acc['total_equity']) / peak * 100.0, 2) if peak > 0 else 0.0
            if dd > acc.get('max_drawdown_pct', 0.0):
                acc['max_drawdown_pct'] = dd
        self.save()

    def apply_fill(self, player_id, fill_record):
        """将一笔成功的成交应用到指定选手的账户中"""
        acc = self.accounts.get(player_id)
        if not acc:
            return False, "未知选手"

        direction = fill_record['direction']
        sym = fill_record['symbol']
        shares = fill_record['shares']
        price = fill_record['price']

        if direction == 'BUY':
            cost_total = fill_record['total_cash_required']
            if cost_total > acc['cash']:
                return False, "现金不足"
            acc['cash'] = round(acc['cash'] - cost_total, 2)
            acc['positions'].append({
                'symbol': sym,
                'name': fill_record.get('name', sym),
                'shares': shares,
                'cost_price': price,
                'fee': fill_record.get('commission', 5.0),
                'buy_date': fill_record.get('trade_date') or datetime.datetime.now().strftime('%Y%m%d'),
                'current_price': price
            })
            acc['trade_count'] += 1

        elif direction == 'SELL':
            # 找到并移除持仓
            found = False
            for pos in list(acc['positions']):
                if pos['symbol'] == sym and pos['shares'] == shares:
                    acc['positions'].remove(pos)
                    found = True
                    break
            if not found:
                # 兼容部分卖出
                for pos in list(acc['positions']):
                    if pos['symbol'] == sym and pos['shares'] >= shares:
                        pos['shares'] -= shares
                        if pos['shares'] == 0:
                            acc['positions'].remove(pos)
                        found = True
                        break
            acc['cash'] = round(acc['cash'] + fill_record['net_proceeds'], 2)
            acc['trade_count'] += 1
            if fill_record.get('net_pnl', 0) > 0:
                acc['win_count'] += 1
            else:
                acc['loss_count'] += 1

        self.save()
        return True, "账户更新成功"

    def save(self):
        if not self.explicit_initialize and not os.path.exists(self.state_path):
            raise RuntimeError('Arena save blocked: missing state requires explicit initialization')
        atomic_write_json(self.state_path, {
            'season_id': self.season_id,
            'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'snapshot_type': 'DERIVED_ARENA_SNAPSHOT',
            'accounts': self.accounts
        })

    def rebuild_from_season_ledgers(self, persist=False):
        """Build the active Arena view from season ledgers, never vice versa."""
        from competition.season_runtime import build_derived_arena_snapshot
        context = build_derived_arena_snapshot(persist=persist)
        self.accounts = context['accounts']
        self.season_id = context['season_id']
        return context


if __name__ == '__main__':
    mgr = ArenaAccountsManager(season_id="TEST_S1")
    print(f"7位选手账户初始化成功 (共 {len(mgr.accounts)} 位选手):")
    for pid, a in mgr.accounts.items():
        print(f"  {a['icon']} {a['name']:<18} | 现金: ¥{a['cash']:,.2f} | 净资产: ¥{a['total_equity']:,.2f}")
