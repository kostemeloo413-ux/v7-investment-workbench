# -*- coding: utf-8 -*-
"""
scripts/competition/ranking.py
Phase 11: 七选手竞技场排行榜引擎（Arena Ranking Engine）

核心职责（《指南》第 1.11 & 1.12 节）：
1. 比赛第一排名指标（绝对原则）：
   > 【账户净收益率】(net_pnl_pct) 为唯一第一指标。
2. 多维画像保留：
   - 最大回撤 (max_drawdown_pct)、平仓胜率 (win_rate)、交易次数 (trade_count)、总资产等作为多维辅助，不改变冠军归属；
3. 不限制交易次数：
   - 彻底解除任何交易次数封顶。
"""

import os
import sys
import json
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from competition.accounts import ArenaAccountsManager, INITIAL_CAPITAL


class ArenaRankingEngine:
    def __init__(self, accounts_manager=None):
        self.mgr = accounts_manager or ArenaAccountsManager()

    def get_current_leaderboard(self):
        """
        根据当前 7 位选手账户状态计算最新排名。
        返回排序后的选手列表，每个元素包含：
          rank, player_id, name, icon, role, total_equity, net_pnl, net_pnl_pct, max_drawdown_pct, trade_count, win_rate
        """
        accs = self.mgr.accounts
        leaderboard = []

        for pid, a in accs.items():
            sells = a.get('win_count', 0) + a.get('loss_count', 0)
            wr = round(a.get('win_count', 0) / sells * 100.0, 1) if sells > 0 else 0.0
            item = {
                'player_id': pid,
                'name': a['name'],
                'icon': a['icon'],
                'role': a['role'],
                'total_equity': a['total_equity'],
                'cash': a['cash'],
                'market_value': a['market_value'],
                'net_pnl': a['net_pnl'],
                'net_pnl_pct': a['net_pnl_pct'],
                'max_drawdown_pct': a.get('max_drawdown_pct', 0.0),
                'trade_count': a.get('trade_count', 0),
                'closed_trades': sells,
                'win_rate_pct': wr,
                'positions_count': len(a.get('positions', []))
            }
            leaderboard.append(item)

        # 唯一第一排名指标：net_pnl_pct 降序
        leaderboard.sort(key=lambda x: x['net_pnl_pct'], reverse=True)

        for i, item in enumerate(leaderboard, start=1):
            item['rank'] = i

        return {
            'season_id': self.mgr.season_id,
            'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'ranking_metric': 'net_pnl_pct (账户净收益率)',
            'players_count': len(leaderboard),
            'leaderboard': leaderboard
        }


if __name__ == '__main__':
    print("=== 测试 ArenaRankingEngine 排行榜输出 ===")
    rank_eng = ArenaRankingEngine()
    board = rank_eng.get_current_leaderboard()
    print(f"\n当前赛季: {board['season_id']} | 排名指标: {board['ranking_metric']}")
    print("-" * 65)
    print(f"{'排名':<4} | {'选手':<16} | {'总资产':<12} | {'净收益率':<10} | {'最大回撤':<8} | {'交易数'}")
    print("-" * 65)
    for p in board['leaderboard']:
        print(f"#{p['rank']:<3} | {p['icon']} {p['name']:<14} | ¥{p['total_equity']:<10,.2f} | {p['net_pnl_pct']:>6.2f}%    | {p['max_drawdown_pct']:>5.2f}%   | {p['trade_count']}笔")
    print("-" * 65)
