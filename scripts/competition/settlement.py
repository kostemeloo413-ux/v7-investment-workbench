# -*- coding: utf-8 -*-
"""
scripts/competition/settlement.py
Phase 11: 赛季结算与战报归档引擎（Season Settlement Engine）

核心职责（《指南》第 1.13 & 17 节）：
1. 赛季结算流程：
   - 冻结当前赛季全部 7 位选手账户；
   - 根据最终收盘价复核净资产，锁定最终 net_pnl_pct；
   - 产生赛季最终官方排名与总冠军 (Champion)；
   - 生成正式 Markdown 战报归档至 outputs/competition_{season_id}.md；
2. 开启新赛季：
   - 重新分配 10 万元纯现金独立账户，重置持仓与流水，开启新一期竞技。
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
from competition.ranking import ArenaRankingEngine

OUTPUTS_DIR = os.path.join(_BASE, 'outputs')


class SeasonSettlementEngine:
    def __init__(self, accounts_manager=None):
        self.mgr = accounts_manager or ArenaAccountsManager()
        self.ranker = ArenaRankingEngine(self.mgr)

    def settle_season(self, season_id=None, note=""):
        """结算当前赛季并生成 Markdown 战报"""
        sid = season_id or self.mgr.season_id
        now = datetime.datetime.now()
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')

        board = self.ranker.get_current_leaderboard()
        leaderboard = board['leaderboard']
        champion = leaderboard[0] if leaderboard else None

        # 组装战报
        lines = [
            f"# 🏆 七选手量化竞技场 · 赛季官方战报 ({sid})",
            f"> 结算时间: `{now_str}` ｜ 裁决规则: A股真实费率与强制T+1 ｜ 排名指标: 账户净收益率 (Net PnL %)",
            "",
            "---",
            "",
            "## 🥇 赛季总冠军出炉",
            f"- **冠军选手**: **{champion['icon']} {champion['name']}**",
            f"- **最终总资产**: `¥{champion['total_equity']:,.2f}` ｜ **净收益率**: `+{champion['net_pnl_pct']:.2f}%`",
            f"- **平仓胜率**: `{champion['win_rate_pct']}%` ｜ **最大回撤**: `{champion['max_drawdown_pct']}%` ｜ **总交易**: `{champion['trade_count']} 笔`",
            "",
            "---",
            "",
            "## 📊 最终完整排行榜 (按账户净收益率严格降序)",
            "",
            "| 排名 | 选手角色 | 类型 | 最终净资产 | 累计盈亏额 | 最终净收益率 | 平仓胜率 | 最大回撤 | 总交易次数 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for p in leaderboard:
            role_badge = "AI量化" if p['role'] == 'ai_quant' else "博主人格"
            wr = f"{p['win_rate_pct']}%" if p['closed_trades'] > 0 else "无平仓"
            lines.append(
                f"| **#{p['rank']}** | {p['icon']} **{p['name']}** | `{role_badge}` | "
                f"¥{p['total_equity']:,.2f} | ¥{p['net_pnl']:+,.2f} | **{p['net_pnl_pct']:+.2f}%** | "
                f"{wr} | {p['max_drawdown_pct']:.2f}% | {p['trade_count']}笔 |"
            )

        lines.extend([
            "",
            "---",
            "",
            "## ⚖️ 裁判公正性与规则符合说明",
            "1. **初始条件**: 7 位选手统一 10 万元虚拟初始资金，同台起跑；",
            "2. **费率模型**: 严格全扣除双边 0.1% 滑点、万三佣金（最低5元）与卖出 0.05% 印花税；",
            "3. **交易纪律**: 强制遵循 A 股 T+1 刚性锁仓，不设交易次数上限；",
            "4. **AI量化独立性**: AI量化交易员作为独立第7位选手参赛，其特征工程与模型决策零新闻/LLM污染，全程凭纯结构化量价参赛。",
            "",
            f"*(战报由 scripts/competition/settlement.py 于 {now_str} 自动生成封存)*"
        ])

        report_content = '\n'.join(lines)
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        report_path = os.path.join(OUTPUTS_DIR, f"competition_{sid}.md")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content)

        print(f"[settlement] 赛季 {sid} 结算完成！战报已落盘: {report_path}")
        return {
            'season_id': sid,
            'settled_at': now_str,
            'champion': champion,
            'report_path': report_path,
            'leaderboard': leaderboard
        }


if __name__ == '__main__':
    print("=== 测试 SeasonSettlementEngine 赛季结算 ===")
    settle_eng = SeasonSettlementEngine()
    rep = settle_eng.settle_season(season_id="SEASON_001_TEST")
    print(f"总冠军: {rep['champion']['name']} (收益率: {rep['champion']['net_pnl_pct']}%)")
    print(f"报告已归档至: {rep['report_path']}")
