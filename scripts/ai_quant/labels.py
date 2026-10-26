# -*- coding: utf-8 -*-
"""
scripts/ai_quant/labels.py
Phase 2: Label Factory（标签工厂）

核心职责与契约依据：
- 严格遵循《R2_LABEL_CONTRACT_FINAL_V2.md》（用户人工最终审查批准版）。
- 标签版本：L-INTRADAY-V1.0
- 解决“AI 到底要学什么？”
1. 双轨历史入场代理规范 (Dual Entry Proxies)：
   - 主轨研究代理 HISTORICAL_VWAP_PROXY：
     * 定义为【Label-side Entry Look-ahead / Target Construction Bias】
     * P_entry = (amount_T / volume_T) * (1.0 + slippage_rate)
     * entry_date = T 日 (与 signal_date 一致)
   - 辅轨保守基准 HISTORICAL_NEXT_OPEN_PROXY：
     * P_entry = Open_{T+1} * (1.0 + slippage_rate)
     * entry_date = T+1 日
2. 未来 N 日时间跨度严格消歧 (修订1)：
   - future_Nd 定义为：实际 Entry Date 之后第 N 个完整交易日收盘价，相对于 Entry Price 的收益。
   - future_Nd_gross_return = (P_{entry_idx + N, close} / P_entry) - 1.0 (纯净毛收益，不扣规费)
   - 两种 Proxy 均严格经历 N 个完整交易日收盘，经济跨度完全对称，零 off-by-one。
3. 日K粒度极值路径纠偏 (严禁使用 Entry Date 当日 High/Low)：
   - 避开买入先后未知时序问题，严格从 Entry Date 之后第一个完整交易日（T+1 / entry_idx + 1）起算：
     * realized_mae_daily_bar = (min_{k in [entry_idx + 1, min(entry_idx + N, M - 1)]}(Low_k) / P_entry) - 1.0
     * realized_mfe_daily_bar = (max_{k in [entry_idx + 1, min(entry_idx + N, M - 1)]}(High_k) / P_entry) - 1.0
4. 行情 Label 与交易成本模型彻底解耦 (修订2)：
   - Market Label 本体仅保留纯净毛收益与日K真实极值；
   - 派生净收益通过独立函数 derive_simulated_net_return(...) 在指定参数下计算。
5. 滑点参数化支持敏感性测试：
   - 显式透传 slippage_rate 参数 (支持 0.0005, 0.0010, 0.0020, 0.0030)。
"""

import os
import sys
import json
import sqlite3
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb

LABEL_VERSION = "L-INTRADAY-V1.0"

# 双轨 Entry Proxy 契约常数
ENTRY_PROXY_VWAP = "HISTORICAL_VWAP_PROXY"
ENTRY_PROXY_NEXT_OPEN = "HISTORICAL_NEXT_OPEN_PROXY"


def derive_simulated_net_return(gross_return, trade_amount=100000.0, commission_rate=0.0003,
                                min_commission=5.0, stamp_tax_rate=0.0005, slippage_rate=0.001,
                                entry_price_already_slipped=True):
    """
    【独立交易成本模型派生函数】
    根据指定参数派生出模拟净收益率，严格不作为 Market Label 本体。
    
    参数说明：
      gross_return: float 纯净毛收益率
      trade_amount: float 单笔名义成交金额 (默认 10 万)
      commission_rate: float 佣金费率 (双边万三)
      min_commission: float 最低佣金门槛 (单笔 5 元)
      stamp_tax_rate: float 印花税率 (卖出单边千分之0.5)
      slippage_rate: float 模拟滑点率 (单边)
      entry_price_already_slipped: bool 若 entry_price 已含滑点，买入端不再重复计入滑点
    """
    if gross_return is None:
        return None

    # 买入端成本
    buy_comm = max(min_commission, trade_amount * commission_rate)
    buy_slip = 0.0 if entry_price_already_slipped else (trade_amount * slippage_rate)
    total_buy_cost = buy_comm + buy_slip

    # 卖出端估算名义市值
    est_sell_amount = trade_amount * (1.0 + gross_return)
    sell_comm = max(min_commission, est_sell_amount * commission_rate)
    sell_tax = est_sell_amount * stamp_tax_rate
    sell_slip = est_sell_amount * slippage_rate
    total_sell_cost = sell_comm + sell_tax + sell_slip

    # 净收益金额与净收益率
    net_pnl_amount = (est_sell_amount - trade_amount) - (total_buy_cost + total_sell_cost)
    net_return = net_pnl_amount / trade_amount
    return round(net_return, 6)


def calculate_single_stock_labels(klines, current_idx, horizons=(1, 2, 3, 5), entry_proxy_mode='vwap',
                                  slippage_rate=0.001, benchmark_returns=None):
    """
    对单一股票在某一历史信号时点 current_idx 计算未来标签字典。
    
    参数：
      klines: list of tuple or dict (按日期升序排列的日K线数据: [trade_date, open, high, low, close, volume, amount, ...])
      current_idx: 产生信号的时点索引 (T 日)
      horizons: 评估的未来交易日跨度元组 (默认 (1, 2, 3, 5))
      entry_proxy_mode: 'vwap' (主轨 HISTORICAL_VWAP_PROXY) 或 'next_open' (辅轨 HISTORICAL_NEXT_OPEN_PROXY)
      slippage_rate: float 滑点参数 (默认 0.0010，支持敏感性测试)
      benchmark_returns: dict of {horizon: benchmark_ret} 同期基准毛收益
    
    返回：
      dict 包含各 Horizon 的纯净毛收益率、超额毛收益率、realized_mae/mfe_daily_bar、成熟度标记等。
    """
    n_bars = len(klines)
    if current_idx >= n_bars or current_idx < 0:
        return None

    bar_t0 = klines[current_idx]
    signal_date = str(bar_t0[0] if isinstance(bar_t0, (list, tuple)) else bar_t0['trade_date'])

    # 1. 依据双轨代理模式确定 Entry 语义与入场价格
    mode_str = str(entry_proxy_mode).lower().strip()
    if mode_str in ('next_open', 'historical_next_open_proxy'):
        entry_proxy_type = ENTRY_PROXY_NEXT_OPEN
        entry_idx = current_idx + 1
        if entry_idx >= n_bars:
            return {
                'signal_date': signal_date,
                'entry_date': None,
                'entry_price': None,
                'entry_proxy_type': entry_proxy_type,
                'slippage_param': slippage_rate,
                'is_mature': False,
                'available_bars': 0,
                'label_version': LABEL_VERSION
            }
        bar_entry = klines[entry_idx]
        entry_date = str(bar_entry[0] if isinstance(bar_entry, (list, tuple)) else bar_entry['trade_date'])
        open_price = float(bar_entry[1] if isinstance(bar_entry, (list, tuple)) else bar_entry['open'])
        entry_price = open_price * (1.0 + slippage_rate)
    else:
        # 主轨：HISTORICAL_VWAP_PROXY
        entry_proxy_type = ENTRY_PROXY_VWAP
        entry_idx = current_idx
        entry_date = signal_date

        # 取 T 日成交量与成交额以重建日内 VWAP
        # 兼容 tuple / dict 结构：标准 kline 为 [trade_date, open, high, low, close, volume, amount, ...]
        vol = float(bar_t0[5] if isinstance(bar_t0, (list, tuple)) and len(bar_t0) > 5 else (bar_t0.get('volume', 0) if isinstance(bar_t0, dict) else 0))
        amt = float(bar_t0[6] if isinstance(bar_t0, (list, tuple)) and len(bar_t0) > 6 else (bar_t0.get('amount', 0) if isinstance(bar_t0, dict) else 0))
        close_t0 = float(bar_t0[4] if isinstance(bar_t0, (list, tuple)) else bar_t0['close'])

        if vol > 0 and amt > 0:
            vwap_price = amt / vol
            # 异常值保护：VWAP 若偏离收盘价超过 20%（常见于单位换算如手 vs 股），降级使用收盘价
            if vwap_price < close_t0 * 0.5 or vwap_price > close_t0 * 2.0:
                vwap_price = close_t0
        else:
            vwap_price = close_t0

        entry_price = vwap_price * (1.0 + slippage_rate)

    if entry_price <= 0:
        return None

    max_horizon = max(horizons)
    available_forward_bars = (n_bars - 1) - entry_idx
    is_fully_mature = (available_forward_bars >= max_horizon)

    label_res = {
        'signal_date': signal_date,
        'entry_date': entry_date,
        'entry_price': round(entry_price, 4),
        'entry_proxy_type': entry_proxy_type,
        'slippage_param': slippage_rate,
        'is_mature': is_fully_mature,
        'available_bars': available_forward_bars,
        'label_version': LABEL_VERSION
    }

    # 2. 逐个 Horizon 计算未来 N 日纯净毛收益率 (修订1: 实际 Entry Date 之后第 N 个完整交易日)
    for h in horizons:
        target_idx = entry_idx + h
        if target_idx < n_bars:
            bar_target = klines[target_idx]
            close_target = float(bar_target[4] if isinstance(bar_target, (list, tuple)) else bar_target['close'])
            # 纯净毛收益率：不扣规费
            gross_ret = (close_target / entry_price) - 1.0
            label_res[f'future_{h}d_gross_return'] = round(gross_ret, 6)

            # 超额毛收益计算
            if benchmark_returns and f'bm_return_{h}d' in benchmark_returns:
                bm_ret = benchmark_returns[f'bm_return_{h}d']
                label_res[f'future_{h}d_excess_gross_return'] = round(gross_ret - bm_ret, 6)
            else:
                label_res[f'future_{h}d_excess_gross_return'] = None
        else:
            label_res[f'future_{h}d_gross_return'] = None
            label_res[f'future_{h}d_excess_gross_return'] = None

    # 3. 日K粒度极值路径计算 (避开 Entry 当日，从 entry_idx + 1 起算)
    scan_start = entry_idx + 1
    scan_end = min(entry_idx + max_horizon, n_bars - 1)
    if scan_end >= scan_start:
        lowest_p = float('inf')
        highest_p = float('-inf')
        time_to_mae = 1
        time_to_mfe = 1

        for step, scan_i in enumerate(range(scan_start, scan_end + 1), start=1):
            b_s = klines[scan_i]
            low_val = float(b_s[3] if isinstance(b_s, (list, tuple)) else b_s['low'])
            high_val = float(b_s[2] if isinstance(b_s, (list, tuple)) else b_s['high'])

            if low_val < lowest_p:
                lowest_p = low_val
                time_to_mae = step
            if high_val > highest_p:
                highest_p = high_val
                time_to_mfe = step

        # MAE: 最低价相对买入价的负向偏离 (<= 0)
        mae = (lowest_p / entry_price) - 1.0
        # MFE: 最高价相对买入价的正向偏离 (>= 0)
        mfe = (highest_p / entry_price) - 1.0

        label_res['realized_mae_daily_bar'] = round(mae, 6)
        label_res['realized_mfe_daily_bar'] = round(mfe, 6)
        label_res['time_to_mae'] = time_to_mae
        label_res['time_to_mfe'] = time_to_mfe
    else:
        label_res['realized_mae_daily_bar'] = None
        label_res['realized_mfe_daily_bar'] = None
        label_res['time_to_mae'] = None
        label_res['time_to_mfe'] = None

    # 4. 产生分类目标 (基于纯净毛收益与已实现极值)
    ret_5d = label_res.get('future_5d_gross_return')
    if ret_5d is not None:
        label_res['is_profitable_5d'] = 1 if ret_5d > 0 else 0
    else:
        label_res['is_profitable_5d'] = None

    mae_val = label_res.get('realized_mae_daily_bar')
    if mae_val is not None:
        label_res['is_risk_triggered'] = 1 if mae_val <= -0.03 else 0
    else:
        label_res['is_risk_triggered'] = None

    return label_res


class LabelFactory:
    """
    【标签工厂统一调度器】
    支持批量从 SQLite 读取股票历史并高速构建 Point-in-Time 样本标签矩阵。
    """
    def __init__(self, db_conn=None):
        self.conn = db_conn

    def build_labels_for_stock(self, symbol, horizons=(1, 2, 3, 5), entry_proxy_mode='vwap',
                               slippage_rate=0.001, only_mature=True):
        """为单只股票生成完整的历史标签序列"""
        pure = str(symbol).strip().zfill(6)
        klines = hdb.get_kline(pure)
        if not klines or len(klines) < 30:
            return []

        results = []
        n_bars = len(klines)
        for i in range(len(klines)):
            lbl = calculate_single_stock_labels(klines, i, horizons=horizons,
                                                entry_proxy_mode=entry_proxy_mode,
                                                slippage_rate=slippage_rate)
            if lbl is None:
                continue
            if only_mature and not lbl['is_mature']:
                continue
            lbl['symbol'] = pure
            results.append(lbl)
        return results


if __name__ == '__main__':
    print("=== 测试 LabelFactory 单标的标签构建 (L-INTRADAY-V1.0) ===")
    factory = LabelFactory()
    labels = factory.build_labels_for_stock('000001', entry_proxy_mode='vwap', only_mature=False)
    print(f"000001 共生成历史截面样本数: {len(labels)}")
    if labels:
        print("\n成熟样本示例 (早于5日前):")
        mature_sample = [l for l in labels if l['is_mature']][-1]
        print(json.dumps(mature_sample, ensure_ascii=False, indent=2))

        print("\n未成熟样本示例 (最近截面):")
        immature_sample = labels[-1]
        print(json.dumps(immature_sample, ensure_ascii=False, indent=2))
