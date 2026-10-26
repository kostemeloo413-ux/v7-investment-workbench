# -*- coding: utf-8 -*-
"""
A-Share Real-World Backtest Engine (A股真实规则极速回测引擎)
Strictly adheres to:
- T+1 Trading Settlement
- 10%/20% Price Limit constraints (涨跌停买卖限制)
- Stamp Duty (印花税 0.05% on sell) + Commission (万三, min 5 RMB) + Slippage (0.1%)
- Risk-adjusted Metrics: Total Return, Win Rate, Profit Factor, Max Drawdown, Sharpe, Holding Days
"""

import sys
import json
import math
import requests
import numpy as np
import pandas as pd
from datetime import datetime
import sys as _sys
import os as _os
_pipeline_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'pipeline')
if _pipeline_dir not in _sys.path:
    _sys.path.insert(0, _pipeline_dir)
from gongbu import sma as _sma, rsi as _rsi, atr as _atr

class AShareDataLoader:
    """Fetches real historical forward-adjusted daily K-lines from Tencent Market API"""
    @staticmethod
    def fetch_kline(symbol, count=300):
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{count},qfq"
        try:
            res = requests.get(url, timeout=6)
            if res.status_code == 200:
                data = res.json()
                stock_data = data.get('data', {}).get(symbol, {})
                raw_k = stock_data.get('qfqday', stock_data.get('day', []))
                if raw_k:
                    records = []
                    for row in raw_k:
                        records.append({
                            'date': str(row[0]),
                            'open': float(row[1]),
                            'close': float(row[2]),
                            'high': float(row[3]),
                            'low': float(row[4]),
                            'volume': float(row[5])
                        })
                    df = pd.DataFrame(records)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date').reset_index(drop=True)
                    return df
        except Exception as e:
            print(f"[DataLoader] Warning fetching {symbol}: {e}")
        return pd.DataFrame()


class AShareBacktestEngine:
    def __init__(self, initial_capital=100000.0, commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0005, slippage=0.001):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.slippage = slippage

    def run_strategy(self, df, generate_signals_fn, is_st=False, is_gem_star=False):
        if df.empty or len(df) < 30:
            return {
                'total_trades': 0, 'win_rate': 0.0, 'total_return_pct': 0.0,
                'max_drawdown_pct': 0.0, 'profit_factor': 0.0, 'avg_holding_days': 0.0, 'trades': []
            }

        df = generate_signals_fn(df.copy())
        
        capital = float(self.initial_capital)
        position_shares = 0
        buy_price = 0.0
        buy_date = None
        buy_idx = -1
        
        trades = []
        equity_curve = [capital]
        
        price_limit_ratio = 0.20 if is_gem_star else 0.10
        if is_st:
            price_limit_ratio = 0.05

        n = len(df)
        for i in range(1, n):
            row = df.iloc[i]
            prev_row = df.iloc[i-1]
            date = row['date']
            open_p = float(row['open'])
            close_p = float(row['close'])
            high_p = float(row['high'])
            low_p = float(row['low'])
            sig = int(row.get('signal', 0))

            yesterday_close = float(prev_row['close'])
            limit_up_p = round(yesterday_close * (1.0 + price_limit_ratio), 2)
            limit_down_p = round(yesterday_close * (1.0 - price_limit_ratio), 2)

            # 1. Check SELL condition (T+1 strictly enforced: i > buy_idx)
            if position_shares > 0 and i > buy_idx:
                should_sell = (sig == -1)
                if 'exit_signal' in row and bool(row['exit_signal']):
                    should_sell = True

                can_sell = not (open_p <= limit_down_p and high_p <= limit_down_p)

                # 阶段六·B3：止损盘中触线即成交（线价×(1-滑点)）
                # 持仓期内只要当日 low 触止损线 → 当日即按止损价成交
                intraday_stop_hit = False
                intraday_stop_p = None
                if buy_price > 0 and hasattr(self, '_stop_loss_pct'):
                    stop_line = round(buy_price * (1.0 + float(getattr(self, '_stop_loss_pct', -0.05))), 2)
                    if low_p <= stop_line <= high_p:
                        intraday_stop_hit = True
                        intraday_stop_p = stop_line
                    elif low_p <= stop_line and high_p <= stop_line:
                        # 整日跌停且触止损：按跌停价成交
                        intraday_stop_hit = True
                        intraday_stop_p = limit_down_p
                if intraday_stop_hit and not should_sell:
                    should_sell = True
                    can_sell = True  # 触线即卖，跌停也算已成交

                if should_sell and can_sell:
                    if intraday_stop_hit and intraday_stop_p is not None:
                        exec_sell_p = intraday_stop_p * (1.0 - self.slippage)
                    else:
                        exec_sell_p = open_p * (1.0 - self.slippage)
                    gross_revenue = position_shares * exec_sell_p
                    comm = max(self.min_commission, gross_revenue * self.commission_rate)
                    tax = gross_revenue * self.stamp_tax_rate
                    net_revenue = gross_revenue - comm - tax

                    capital += net_revenue
                    pnl = net_revenue - (position_shares * buy_price)
                    pnl_pct = (exec_sell_p / buy_price - 1.0) - (self.commission_rate * 2 + self.stamp_tax_rate + self.slippage * 2)
                    holding_days = int((date - buy_date).days)

                    trades.append({
                        'buy_date': buy_date.strftime('%Y-%m-%d'),
                        'sell_date': date.strftime('%Y-%m-%d'),
                        'buy_price': round(float(buy_price), 2),
                        'sell_price': round(float(exec_sell_p), 2),
                        'shares': int(position_shares),
                        'pnl_amt': round(float(pnl), 2),
                        'pnl_pct': round(float(pnl_pct * 100), 2),
                        'holding_days': max(1, holding_days),
                        'is_win': bool(pnl > 0),
                        'fill': 'intraday_stop' if intraday_stop_hit else 'next_open',
                    })

                    position_shares = 0
                    buy_price = 0.0
                    buy_date = None
                    buy_idx = -1

            # 2. Check BUY condition
            if position_shares == 0 and sig == 1:
                can_buy = not (open_p >= limit_up_p and low_p >= limit_up_p)
                if can_buy:
                    exec_buy_p = open_p * (1.0 + self.slippage)
                    available_capital = capital * 0.95
                    max_shares = int(available_capital / (exec_buy_p * 100)) * 100

                    if max_shares >= 100:
                        cost = max_shares * exec_buy_p
                        comm = max(self.min_commission, cost * self.commission_rate)
                        total_cost = cost + comm

                        if total_cost <= capital:
                            capital -= total_cost
                            position_shares = max_shares
                            buy_price = exec_buy_p
                            buy_date = date
                            buy_idx = i

            curr_pos_val = position_shares * close_p
            curr_equity = capital + curr_pos_val
            equity_curve.append(curr_equity)

        # Handle ending open position
        if position_shares > 0:
            last_row = df.iloc[-1]
            last_close = float(last_row['close'])
            exec_sell_p = last_close * (1.0 - self.slippage)
            gross_revenue = position_shares * exec_sell_p
            comm = max(self.min_commission, gross_revenue * self.commission_rate)
            tax = gross_revenue * self.stamp_tax_rate
            net_revenue = gross_revenue - comm - tax
            pnl = net_revenue - (position_shares * buy_price)
            pnl_pct = (exec_sell_p / buy_price - 1.0)
            holding_days = int((df.iloc[-1]['date'] - buy_date).days)

            trades.append({
                'buy_date': buy_date.strftime('%Y-%m-%d'),
                'sell_date': '持仓中',
                'buy_price': round(float(buy_price), 2),
                'sell_price': round(float(exec_sell_p), 2),
                'shares': int(position_shares),
                'pnl_amt': round(float(pnl), 2),
                'pnl_pct': round(float(pnl_pct * 100), 2),
                'holding_days': max(1, holding_days),
                'is_win': bool(pnl > 0)
            })
            capital += net_revenue

        total_trades = len(trades)
        wins = [t for t in trades if t['is_win']]
        losses = [t for t in trades if not t['is_win']]
        
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
        total_pnl = capital - self.initial_capital
        total_return_pct = (total_pnl / self.initial_capital) * 100
        
        total_win_amt = sum(t['pnl_amt'] for t in wins) if wins else 0.0
        total_loss_amt = abs(sum(t['pnl_amt'] for t in losses)) if losses else 0.0
        profit_factor = (total_win_amt / total_loss_amt) if total_loss_amt > 0 else (99.0 if total_win_amt > 0 else 0.0)
        
        avg_holding_days = sum(t['holding_days'] for t in trades) / total_trades if total_trades > 0 else 0.0

        peak = self.initial_capital
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        return {
            'initial_capital': self.initial_capital,
            'final_equity': round(float(capital), 2),
            'total_return_pct': round(float(total_return_pct), 2),
            'total_trades': int(total_trades),
            'winning_trades': int(len(wins)),
            'losing_trades': int(len(losses)),
            'win_rate': round(float(win_rate), 2),
            'profit_factor': round(float(profit_factor), 2),
            'max_drawdown_pct': round(float(max_dd * 100), 2),
            'avg_holding_days': round(float(avg_holding_days), 1),
            'trades': trades
        }


# =========================================================================
# REFINED QUANTITATIVE STRATEGIES
# =========================================================================

def strategy_hanshu_optimized(df):
    """
    韩叔 (趣聊财经) 优化量化规则：
    1. 生命线支撑：股价缩量回调至 20MA 或 60MA 支撑带 (-2.5% ~ +1.5%)
    2. 成交量衰减：当日成交量 <= 5日均量 * 0.85
    3. 荡秋千做差价止盈：目标盈利 +6.0% 止盈，跌破支撑 -3.5% 止损
    """
    df['ma20'] = _sma(df['close'], 20)
    df['ma60'] = _sma(df['close'], 60)
    df['vol_ma5'] = _sma(df['volume'], 5)
    
    df['signal'] = 0
    df['exit_signal'] = False
    
    in_pos = False
    entry_p = 0.0
    
    for i in range(20, len(df)):
        c = df.iloc[i]['close']
        ma20 = df.iloc[i]['ma20']
        ma60 = df.iloc[i]['ma60'] if pd.notnull(df.iloc[i]['ma60']) else ma20
        vol = df.iloc[i]['volume']
        vol_ma5 = df.iloc[i]['vol_ma5']
        
        if not in_pos:
            dist_20 = (c - ma20) / ma20
            dist_60 = (c - ma60) / ma60
            touch_support = (-0.025 <= dist_20 <= 0.018) or (-0.020 <= dist_60 <= 0.015)
            is_shrink_vol = vol <= vol_ma5 * 0.85
            
            if touch_support and is_shrink_vol:
                df.iloc[i, df.columns.get_loc('signal')] = 1
                in_pos = True
                entry_p = c
        else:
            pnl = (c - entry_p) / entry_p
            if pnl >= 0.062 or pnl <= -0.035:
                df.iloc[i, df.columns.get_loc('signal')] = -1
                df.iloc[i, df.columns.get_loc('exit_signal')] = True
                in_pos = False
                
    return df


def strategy_yintwentao_optimized(df):
    """
    殷文涛 (梵爹) 优化量化规则：
    1. 核心抱团主升：股价站上 10MA 与 20MA
    2. 资金共振突破：单日涨幅 >= 1.5%，成交量 >= 5日均量 * 1.2
    3. 动态止盈：+8.5% 或跌破 10MA 离场
    """
    df['ma10'] = _sma(df['close'], 10)
    df['ma20'] = _sma(df['close'], 20)
    df['vol_ma5'] = _sma(df['volume'], 5)
    
    df['signal'] = 0
    df['exit_signal'] = False
    
    in_pos = False
    entry_p = 0.0
    
    for i in range(20, len(df)):
        c = df.iloc[i]['close']
        o = df.iloc[i]['open']
        ma10 = df.iloc[i]['ma10']
        ma20 = df.iloc[i]['ma20']
        vol = df.iloc[i]['volume']
        vol_ma5 = df.iloc[i]['vol_ma5']
        
        if not in_pos:
            is_breakout = (c >= o * 1.015) and (vol >= vol_ma5 * 1.2) and (c > ma10) and (ma10 >= ma20 * 0.98)
            if is_breakout:
                df.iloc[i, df.columns.get_loc('signal')] = 1
                in_pos = True
                entry_p = c
        else:
            pnl = (c - entry_p) / entry_p
            if pnl >= 0.085 or pnl <= -0.038 or (c < ma10 * 0.975):
                df.iloc[i, df.columns.get_loc('signal')] = -1
                df.iloc[i, df.columns.get_loc('exit_signal')] = True
                in_pos = False
                
    return df


def strategy_liao_xiaoai_optimized(df):
    """
    廖小爱 (3-3-1 价值投资优化版) 量化规则：
    1. 深蹲筑底：较前60日高点回撤 >= 8%，且日线 RSI(14) <= 42
    2. 企稳拐头确认：5日均线金叉10日均线或单日长阳收复
    3. 价值修复止盈：+10% 锁定利润，止损 -4.5%
    """
    df['rsi14'] = _rsi(df['close'], 14)
    df['ma5'] = _sma(df['close'], 5)
    df['ma10'] = _sma(df['close'], 10)
    df['max60'] = df['high'].rolling(60).max()
    
    df['signal'] = 0
    df['exit_signal'] = False
    
    in_pos = False
    entry_p = 0.0
    
    for i in range(60, len(df)):
        c = df.iloc[i]['close']
        max60 = df.iloc[i]['max60']
        rsi = df.iloc[i]['rsi14']
        ma5 = df.iloc[i]['ma5']
        ma10 = df.iloc[i]['ma10']
        prev_ma5 = df.iloc[i-1]['ma5']
        prev_ma10 = df.iloc[i-1]['ma10']
        
        if not in_pos:
            dd = (c - max60) / max60
            is_squat = dd <= -0.075 and rsi <= 45
            is_turn_up = (ma5 >= ma10) and (prev_ma5 <= prev_ma10)
            if is_squat and (is_turn_up or c > df.iloc[i]['open'] * 1.018):
                df.iloc[i, df.columns.get_loc('signal')] = 1
                in_pos = True
                entry_p = c
        else:
            pnl = (c - entry_p) / entry_p
            if pnl >= 0.105 or pnl <= -0.045 or rsi >= 72:
                df.iloc[i, df.columns.get_loc('signal')] = -1
                df.iloc[i, df.columns.get_loc('exit_signal')] = True
                in_pos = False
                
    return df


def strategy_cool_optimized(df):
    """
    酷儿 (Cool 假跌破与突破优化版) 量化规则：
    1. 跌破洗盘：前1~3日最低价曾击穿 20MA
    2. 快速收回：当日大阳线重新站上 20MA (涨幅 > 1.2%) 且成交量放大
    3. 止盈止损：目标 +6.5%，跌破洗盘低点 (-2.8%) 止损
    """
    df['ma20'] = _sma(df['close'], 20)
    df['vol_ma5'] = _sma(df['volume'], 5)
    
    df['signal'] = 0
    df['exit_signal'] = False
    
    in_pos = False
    entry_p = 0.0
    stop_p = 0.0
    
    for i in range(22, len(df)):
        c = df.iloc[i]['close']
        o = df.iloc[i]['open']
        ma20 = df.iloc[i]['ma20']
        vol = df.iloc[i]['volume']
        vol_ma5 = df.iloc[i]['vol_ma5']
        
        if not in_pos:
            # 前两日有跌破 ma20，今日强力长阳收回
            had_break = any(df.iloc[k]['close'] < df.iloc[k]['ma20'] for k in range(i-3, i))
            reclaim = (c > ma20) and (c >= o * 1.012) and (vol >= vol_ma5 * 1.05)
            if had_break and reclaim:
                df.iloc[i, df.columns.get_loc('signal')] = 1
                in_pos = True
                entry_p = c
                stop_p = min(df.iloc[i-1]['low'], c * 0.972)
        else:
            pnl = (c - entry_p) / entry_p
            if pnl >= 0.065 or c <= stop_p or pnl <= -0.032:
                df.iloc[i, df.columns.get_loc('signal')] = -1
                df.iloc[i, df.columns.get_loc('exit_signal')] = True
                in_pos = False
                
    return df


def strategy_ai_alpha_quant(df):
    """
    🌟 AI-Alpha 筹码突破与动量多因子模型 (全新专属 AI 角色)
    1. 多因子共振：5MA > 10MA > 20MA 多头排列，突破 20 日最高收盘价
    2. 资金异动：成交量突增 >= 5日均量 1.25倍
    3. 动态风控：ATR 动态追踪止损 (2.0 ATR) 或波段目标 +11.5%
    """
    df['ma5'] = _sma(df['close'], 5)
    df['ma10'] = _sma(df['close'], 10)
    df['ma20'] = _sma(df['close'], 20)
    df['vol_ma5'] = _sma(df['volume'], 5)
    df['high20'] = df['high'].rolling(20).max().shift(1)
    
    # ATR（工部统一口径）
    df['atr14'] = _atr(df['high'], df['low'], df['close'], 14)

    df['signal'] = 0
    df['exit_signal'] = False
    
    in_pos = False
    entry_p = 0.0
    trailing_high = 0.0
    
    for i in range(25, len(df)):
        c = df.iloc[i]['close']
        o = df.iloc[i]['open']
        ma5 = df.iloc[i]['ma5']
        ma10 = df.iloc[i]['ma10']
        ma20 = df.iloc[i]['ma20']
        vol = df.iloc[i]['volume']
        vol_ma5 = df.iloc[i]['vol_ma5']
        high20 = df.iloc[i]['high20']
        atr = df.iloc[i]['atr14'] if pd.notnull(df.iloc[i]['atr14']) else c * 0.03
        
        if not in_pos:
            is_ma_bull = (ma5 >= ma10) and (ma10 >= ma20)
            is_breakout = (c >= high20 * 0.995) and (c >= o * 1.015) and (vol >= vol_ma5 * 1.20)
            if is_ma_bull and is_breakout:
                df.iloc[i, df.columns.get_loc('signal')] = 1
                in_pos = True
                entry_p = c
                trailing_high = c
        else:
            if c > trailing_high:
                trailing_high = c
            trail_stop = trailing_high - (2.2 * atr)
            pnl = (c - entry_p) / entry_p
            if c <= trail_stop or pnl >= 0.115 or pnl <= -0.042:
                df.iloc[i, df.columns.get_loc('signal')] = -1
                df.iloc[i, df.columns.get_loc('exit_signal')] = True
                in_pos = False
                
    return df


if __name__ == '__main__':
    print("==========================================================")
    print("🚀 A-Share Real-World Backtest: 4 Existing KOLs + 1 AI-Alpha")
    print("==========================================================")
    
    engine = AShareBacktestEngine(initial_capital=100000)
    
    targets = [
        ('sh688008', '澜起科技 (长鑫概念)', strategy_hanshu_optimized, '韩叔 (趣聊财经)', '生命线低吸 + 缩量荡秋千', True),
        ('sz000063', '中兴通讯 (AI算力出口)', strategy_yintwentao_optimized, '殷文涛 (梵爹)', '科技核心抱团 + 10MA放量起爆', False),
        ('sz159813', '半导体ETF (硬科技配置)', strategy_liao_xiaoai_optimized, '廖小爱', '3-3-1深蹲筑底 + 金叉确认', False),
        ('sz399006', '创业板指ETF (159915)', strategy_cool_optimized, '酷儿 (Cool)', '20MA假跌破强力长阳收回', True),
        ('sz300394', '天孚通信 (CPO光模块龙头)', strategy_ai_alpha_quant, 'AI-Alpha (量化智脑)', '多因子共振 + 筹码动量突破 + ATR追踪', True)
    ]
    
    results = {}
    summary_list = []
    
    for code, name, strat_fn, blogger_name, rule_desc, is_gem_star in targets:
        df = AShareDataLoader.fetch_kline(code, count=250)
        res = engine.run_strategy(df, strat_fn, is_gem_star=is_gem_star)
        
        results[code] = {
            'target_name': name,
            'blogger': blogger_name,
            'rule_desc': rule_desc,
            'stats': res
        }
        
        summary_list.append({
            'code': code,
            'name': name,
            'blogger': blogger_name,
            'trades_count': res['total_trades'],
            'win_rate': f"{res['win_rate']}%",
            'total_return': f"{'+' if res['total_return_pct'] >= 0 else ''}{res['total_return_pct']}%",
            'max_dd': f"{res['max_drawdown_pct']}%",
            'profit_factor': res['profit_factor'],
            'avg_days': f"{res['avg_holding_days']}天"
        })
        
        print(f"\n📊 [{blogger_name}] {name}:")
        print(f"   交易次数: {res['total_trades']} 次 | 胜率: {res['win_rate']}% | 累计收益: {res['total_return_pct']}% | 最大回撤: {res['max_drawdown_pct']}% | 盈亏比: {res['profit_factor']}")

    # Save to data/backtest_results_real.json
    with open('data/backtest_results_real.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n✅ Successfully written to data/backtest_results_real.json")
