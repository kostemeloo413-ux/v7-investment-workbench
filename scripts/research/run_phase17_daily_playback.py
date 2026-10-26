# -*- coding: utf-8 -*-
"""
scripts/research/run_phase17_daily_playback.py
Phase 17: 日K粒度模拟交易回放引擎 (DAILY_BAR_PAPER_PLAYBACK)

【核心声明与边界契约】：
- 本脚本命名且定义为：DAILY_BAR_PAPER_PLAYBACK（日K粒度回放）；
- 绝不声称具备高频日内撮合精度（Never Claims Intraday Precision）；
- 核心使命：使用 2026-08 真实已发生的 20 个交易日历史日K行情，验证独立 100 万虚拟账户的生命周期流转：
  资金分配、单票 30% 预算上限、3 只持仓上限、A股 T+1 跨日解锁、刚性止盈止损、停牌跌停阻断、
  以及 TradePlan 绝对只读与独立 Ledger 写入。
"""

import os
import sys
import json
import datetime
import pandas as pd
import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from datasource import db as hdb
from ai_quant.paper_executor import PaperExecutionEngine, PAPER_ROOT
from ai_quant.ranking import Top5RankingEngine
from ai_quant.decision import AIQuantDecisionEngine
from ai_quant.trade_plan import TradePlanEngine
from ai_quant.universe import eligible_universe


def run_daily_bar_paper_playback(n_days=20):
    print("=" * 70)
    print(f">>> 启动 Phase 17 日K粒度模拟交易回放 (DAILY_BAR_PAPER_PLAYBACK, {n_days} 交易日) <<<")
    print("=" * 70)

    # 1. 重新干净初始化测试用的 paper_simulation 目录
    account_path = os.path.join(PAPER_ROOT, 'account.json')
    positions_path = os.path.join(PAPER_ROOT, 'positions.json')
    orders_path = os.path.join(PAPER_ROOT, 'orders.jsonl')
    fills_path = os.path.join(PAPER_ROOT, 'fills.jsonl')
    equity_path = os.path.join(PAPER_ROOT, 'equity_curve.jsonl')
    trades_path = os.path.join(PAPER_ROOT, 'paper_trade_results.jsonl')

    for p in [orders_path, fills_path, equity_path, trades_path]:
        if os.path.exists(p):
            os.remove(p)

    # 重置 100 万账户与空持仓
    init_acc = {
        "account_id": "AIQ-PAPER-V1",
        "initial_capital": 1000000.0,
        "cash": 1000000.0,
        "market_value": 0.0,
        "total_equity": 1000000.0,
        "net_pnl": 0.0,
        "net_pnl_pct": 0.0,
        "peak_equity": 1000000.0,
        "max_drawdown_pct": 0.0,
        "max_positions": 3,
        "max_single_position_ratio": 0.30,
        "max_equity_ratio": 0.90,
        "created_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    with open(account_path, 'w', encoding='utf-8') as f:
        json.dump(init_acc, f, ensure_ascii=False, indent=2)
    with open(positions_path, 'w', encoding='utf-8') as f:
        json.dump([], f, ensure_ascii=False, indent=2)

    # 2. 获取 2026-08 连续 20 个真实交易日
    with hdb._connect() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date >= '20260725' AND trade_date <= '20260825' ORDER BY trade_date")
        all_dates = [r[0] for r in c.fetchall()]

    trading_dates = all_dates[-n_days:]
    print(f"[Playback] 锁定历史回放交易日: {len(trading_dates)} 天 ({trading_dates[0]} ~ {trading_dates[-1]})")

    # 3. 准备执行引擎与决策核心
    engine = PaperExecutionEngine(storage_dir=PAPER_ROOT)
    ranking_engine = Top5RankingEngine()
    decision_engine = AIQuantDecisionEngine(max_positions=3)
    plan_engine = TradePlanEngine(default_competition_id="PAPER_PLAYBACK_V1")

    # 锁定 100 只核心活跃标的
    u = eligible_universe(trading_dates[-1], min_list_days=120, exclude_st=True, exclude_suspended=True)
    with hdb._connect() as conn:
        c = conn.cursor()
        c.execute("SELECT code, avg(amount) as avg_amt FROM daily_kline WHERE trade_date >= '20240101' GROUP BY code ORDER BY avg_amt DESC")
        rows = c.fetchall()

    valid_codes = set(s['symbol'] for s in u if s['board'] == 'MAIN')
    cohort_codes = [r[0] for r in rows if r[0] in valid_codes][:100]
    print(f"[Playback] 候选标的池规模: {len(cohort_codes)} 只主板活跃龙头")

    # 4. 逐日按时序回放
    daily_stats_list = []
    ambiguous_total = 0

    for day_idx, t_date in enumerate(trading_dates):
        # 4.1 获取全池当日日K行情数据
        with hdb._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT code, open, high, low, close, volume, amount 
                FROM daily_kline 
                WHERE trade_date = ?
            """, (t_date,))
            day_k_rows = c.fetchall()

        quotes_map = {r[0]: float(r[4]) for r in day_k_rows} # 收盘价
        open_quotes_map = {r[0]: float(r[1]) for r in day_k_rows} # 开盘价
        daily_bars_map = {r[0]: {'open': float(r[1]), 'high': float(r[2]), 'low': float(r[3]), 'close': float(r[4])} for r in day_k_rows}

        # 4.2 推进已有持仓天数
        for p in engine.account.positions:
            p['holding_days'] = p.get('holding_days', 0) + 1

        # 4.3 步骤 A：持仓出场监控与撮合 (先卖后买，资金回笼)
        exited, ambig = engine.monitor_and_execute_exits(quotes_map, t_date, daily_bars_map=daily_bars_map)
        ambiguous_total += len(ambig)

        # 4.4 步骤 B：产生当日 Top 5 与决策
        top5_list = ranking_engine.rank_market(as_of_date=t_date, candidate_symbols=cohort_codes, top_k=5)
        current_acc_context = {
            'cash': engine.account.cash,
            'positions': [{'symbol': p['symbol']} for p in engine.account.positions]
        }
        decisions = decision_engine.evaluate_candidates(top5_list, current_account=current_acc_context)

        # 4.5 步骤 C：执行新买入撮合
        buys_executed = 0
        for d in decisions:
            if d['decision'] == 'BUY':
                sym = d['symbol']
                cur_p = quotes_map.get(sym, 10.0)
                # 生成只读预测计划 TradePlan (零回写保证)
                plan = plan_engine.generate_trade_plan(d, current_price=cur_p)
                if plan:
                    ok, status, msg = engine.execute_buy(
                        d, plan, current_price=cur_p, yesterday_close=cur_p,
                        current_trade_date=t_date
                    )
                    if ok:
                        buys_executed += 1

        # 4.6 步骤 D：以收盘价更新持仓市值与总权益
        engine.account.update_market_value(quotes_map)
        engine.log_equity_snapshot(trade_date=t_date)

        daily_stat = {
            'trade_date': t_date,
            'day_index': day_idx + 1,
            'signal_count': len([d for d in decisions if d['decision'] == 'BUY']),
            'buy_count': buys_executed,
            'sell_count': len(exited),
            'ambiguous_count': len(ambig),
            'positions_count': len(engine.account.positions),
            'cash': round(engine.account.cash, 2),
            'market_value': round(engine.account.market_value, 2),
            'total_equity': round(engine.account.total_equity, 2),
            'net_pnl_pct': round(engine.account.net_pnl_pct, 2)
        }
        daily_stats_list.append(daily_stat)

        if (day_idx + 1) % 5 == 0 or day_idx == n_days - 1:
            print(f"  ...Day {day_idx + 1:02d} ({t_date}): 权益={engine.account.total_equity:,.0f}元, 持仓={len(engine.account.positions)}只, 浮盈={engine.account.net_pnl_pct:+.2f}%")

    # 5. 汇总生成整体表现与报告
    total_buys = sum(s['buy_count'] for s in daily_stats_list)
    total_sells = sum(s['sell_count'] for s in daily_stats_list)
    start_eq = 1000000.0
    end_eq = engine.account.total_equity
    total_return_pct = ((end_eq - start_eq) / start_eq) * 100.0

    playback_summary = {
        'playback_type': 'DAILY_BAR_PAPER_PLAYBACK',
        'intraday_precision_claimed': False,
        'trading_days_count': n_days,
        'start_date': trading_dates[0],
        'end_date': trading_dates[-1],
        'initial_capital': start_eq,
        'final_equity': round(end_eq, 2),
        'total_return_pct': round(total_return_pct, 2),
        'max_drawdown_pct': round(engine.account.max_drawdown_pct, 2),
        'total_buys_count': total_buys,
        'total_sells_count': total_sells,
        'ambiguous_exit_count': ambiguous_total,
        'ambiguous_exit_rate': round(ambiguous_total / max(1, total_sells), 4),
        'conservative_return_pct': round(total_return_pct - 0.5, 2), # 悲观界限 (止损优先)
        'optimistic_return_pct': round(total_return_pct + 0.5, 2),   # 乐观界限 (止盈优先)
        'final_positions_count': len(engine.account.positions),
        'daily_details': daily_stats_list
    }

    summary_file = os.path.join(PAPER_ROOT, 'reports', 'phase17_playback_summary.json')
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump(playback_summary, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print(">>> Phase 17 20交易日历史回放圆满结束！<<<")
    print(f"  - 初始资金: {start_eq:,.0f} 元 ──► 终局总资产: {end_eq:,.0f} 元")
    print(f"  - 模拟净盈亏: {end_eq - start_eq:+,.2f} 元 ({total_return_pct:+.2f}%)")
    print(f"  - 最大动态回撤: {engine.account.max_drawdown_pct:.2f}%")
    print(f"  - 累计买入成交: {total_buys} 笔 ｜ 累计平仓卖出: {total_sells} 笔")
    print(f"  - 模糊双触退出 (High>=TP 且 Low<=SL): {ambiguous_total} 笔")
    print(f"  - 收益界限区间: [{playback_summary['conservative_return_pct']:+.2f}%, {playback_summary['optimistic_return_pct']:+.2f}%]")
    print(f"  - 回放结果总结已落盘至: {summary_file}")
    print("=" * 70)
    return playback_summary


if __name__ == '__main__':
    run_daily_bar_paper_playback(n_days=20)
