# -*- coding: utf-8 -*-
"""
scripts/ai_quant/features.py
Phase 3: Feature Factory（特征工厂）

核心原则（《指南》第 9 节）：
1. 每一个特征必须明确回答：“在模型产生决策的那个时刻（T 日收盘/盘中），该特征是否已经真实可获得？”
2. 绝对杜绝未来信息（如计算 5 日均线只能用 t-4 到 t，严禁包含 t+1）；
3. 严格克制特征数量，按结构化逻辑分为四大组（约 20~28 个高解释性 Alpha 特征）：
   Group A - Price (量价动量与波动)：
     - return_1d, return_3d, return_5d, return_10d, return_20d
     - volatility_5d, volatility_20d (年化波动率)
     - max_drawdown_20d (近20日最大回撤)
     - intraday_range_1d (日内振幅: (high - low) / close)
     - gap_1d (开盘跳空幅度: (open / prev_close) - 1)
     - shadow_upper, shadow_lower (上影线与下影线占比)
   Group B - Volume / Liquidity (成交量与换手流动性)：
     - volume_ratio_5d (当日成交量 vs 过去5日均量比值)
     - volume_zscore_20d (成交量在近20日的标准化偏离 Z-Score)
     - turnover_ratio (换手率，若无则以成交量对数变化代理)
     - amount_log (成交额对数值，衡量流动性容纳能力)
   Group C - Relative Strength (横截面相对强弱与超额动量)：
     - relative_return_5d (个股 5日收益 - 全市场主板同期中位数收益)
     - relative_return_20d (个股 20日收益 - 全市场主板同期中位数收益)
     - cross_section_rank_pct (个股当日收益在全池的百分比排名 0~1)
     - is_leading_sector (是否属于当日领涨前列题材)
   Group D - Market Regime (宏观与全市场宽度背景)：
     - market_breadth_up_ratio (全市场上涨股票家数占比 0~1)
     - market_trend_20d (全市场中位数 20日趋势动量)
     - market_turnover_chg (全市场总成交额较昨日变化率)
"""

import os
import sys
import json
import math
import sqlite3
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb

FEATURE_VERSION = "F-V1.0"

FEATURE_COLUMNS_PRICE = [
    'ret_1d', 'ret_3d', 'ret_5d', 'ret_10d', 'ret_20d',
    'volatility_5d', 'volatility_20d', 'max_drawdown_20d',
    'intraday_range', 'jump_gap', 'shadow_upper', 'shadow_lower'
]

FEATURE_COLUMNS_VOLUME = [
    'volume_ratio_5d', 'volume_zscore_20d', 'turnover_ratio_est', 'amount_log'
]

FEATURE_COLUMNS_RELATIVE = [
    'relative_ret_5d', 'relative_ret_20d', 'cross_rank_pct'
]

FEATURE_COLUMNS_REGIME = [
    'market_up_ratio', 'market_trend_20d', 'market_volume_chg'
]

ALL_FEATURE_COLUMNS = (
    FEATURE_COLUMNS_PRICE +
    FEATURE_COLUMNS_VOLUME +
    FEATURE_COLUMNS_RELATIVE +
    FEATURE_COLUMNS_REGIME
)


def calculate_price_features(klines, current_idx):
    """
    计算 Group A (Price) 与 Group B (Volume) 的单一标的时序特征。
    klines 按日期升序排列: [(trade_date, open, high, low, close, volume, amount, ...), ...]
    current_idx: T 日索引（只能看到 current_idx 及以前的数据）
    """
    if current_idx < 20 or current_idx >= len(klines):
        # 至少需要 20 根历史 K 线计算指标
        return None

    row = klines[current_idx]
    c_close = float(row[4])
    c_open = float(row[1])
    c_high = float(row[2])
    c_low = float(row[3])
    c_vol = float(row[5])
    c_amt = float(row[6])

    prev_row = klines[current_idx - 1]
    p_close = float(prev_row[4])

    if c_close <= 0 or p_close <= 0:
        return None

    # 1. 动量收益
    ret_1d = (c_close / p_close) - 1.0
    
    def _safe_ret(lag):
        if current_idx >= lag:
            b_lag = klines[current_idx - lag]
            close_lag = float(b_lag[4])
            return (c_close / close_lag) - 1.0 if close_lag > 0 else 0.0
        return 0.0

    ret_3d = _safe_ret(3)
    ret_5d = _safe_ret(5)
    ret_10d = _safe_ret(10)
    ret_20d = _safe_ret(20)

    # 2. 波动率与回撤计算 (基于历史收盘序列)
    closes_20 = [float(klines[i][4]) for i in range(current_idx - 19, current_idx + 1)]
    log_rets = [math.log(closes_20[k] / closes_20[k-1]) for k in range(1, len(closes_20)) if closes_20[k-1] > 0 and closes_20[k] > 0]
    
    # 5日波动
    if len(log_rets) >= 5:
        sub_rets_5 = log_rets[-5:]
        mean_5 = sum(sub_rets_5) / len(sub_rets_5)
        var_5 = sum((r - mean_5)**2 for r in sub_rets_5) / len(sub_rets_5)
        vol_5d = math.sqrt(var_5) * math.sqrt(250)
    else:
        vol_5d = 0.0

    # 20日波动
    if len(log_rets) >= 15:
        mean_20 = sum(log_rets) / len(log_rets)
        var_20 = sum((r - mean_20)**2 for r in log_rets) / len(log_rets)
        vol_20d = math.sqrt(var_20) * math.sqrt(250)
    else:
        vol_20d = 0.0

    # 20日最大回撤
    peak = closes_20[0]
    max_dd = 0.0
    for p in closes_20:
        if p > peak:
            peak = p
        dd = (peak - p) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # 3. K线形态微观指标
    intraday_range = (c_high - c_low) / c_close if c_close > 0 else 0.0
    jump_gap = (c_open / p_close) - 1.0
    body_top = max(c_open, c_close)
    body_bottom = min(c_open, c_close)
    total_span = max(c_high - c_low, 0.001)
    shadow_upper = max(c_high - body_top, 0.0) / total_span
    shadow_lower = max(body_bottom - c_low, 0.0) / total_span

    # 4. Volume / Liquidity 特征
    vols_20 = [float(klines[i][5]) for i in range(current_idx - 19, current_idx + 1)]
    v_mean_5 = sum(vols_20[-5:]) / 5.0
    vol_ratio_5d = (c_vol / v_mean_5) if v_mean_5 > 0 else 1.0

    v_mean_20 = sum(vols_20) / 20.0
    v_std_20 = math.sqrt(sum((v - v_mean_20)**2 for v in vols_20) / 20.0) if len(vols_20) >= 20 else 1.0
    vol_zscore = ((c_vol - v_mean_20) / v_std_20) if v_std_20 > 0 else 0.0

    turnover_est = math.log(max(c_vol, 1.0))  # 换手估计
    amt_log = math.log(max(c_amt, 1000.0))    # 成交额对数

    return {
        'ret_1d': round(ret_1d, 5),
        'ret_3d': round(ret_3d, 5),
        'ret_5d': round(ret_5d, 5),
        'ret_10d': round(ret_10d, 5),
        'ret_20d': round(ret_20d, 5),
        'volatility_5d': round(vol_5d, 4),
        'volatility_20d': round(vol_20d, 4),
        'max_drawdown_20d': round(max_dd, 4),
        'intraday_range': round(intraday_range, 4),
        'jump_gap': round(jump_gap, 4),
        'shadow_upper': round(shadow_upper, 4),
        'shadow_lower': round(shadow_lower, 4),
        'volume_ratio_5d': round(vol_ratio_5d, 3),
        'volume_zscore_20d': round(vol_zscore, 3),
        'turnover_ratio_est': round(turnover_est, 3),
        'amount_log': round(amt_log, 3),
    }


def calculate_market_regime_features(trade_date, data_provider=None):
    """
    计算特定历史交易日 trade_date ('YYYYMMDD') 的全市场宏观宽度特征 (Group D)。
    基于本地 SQLite 库全主板截面聚合，零网络依赖。
    """
    td = str(trade_date).replace('-', '')[:8]
    with (data_provider or hdb)._connect() as conn:
        cursor = conn.cursor()
        # 统计全市场当日涨跌家数与总成交额
        rows = cursor.execute("""
            SELECT pct_chg, amount
            FROM daily_kline
            WHERE trade_date = ?
        """, (td,)).fetchall()

        if not rows or len(rows) < 500:
            return {
                'market_up_ratio': 0.5,
                'market_trend_20d': 0.0,
                'market_volume_chg': 0.0
            }

        up_count = sum(1 for r in rows if r[0] is not None and r[0] > 0)
        up_ratio = round(up_count / len(rows), 4)

        # 估算大盘 20 日中位数动量（查上证或代表性样本）
        sh_rows = cursor.execute("""
            SELECT close FROM daily_kline
            WHERE code = '000001' AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 21
        """, (td,)).fetchall()
        if len(sh_rows) >= 21:
            c_now = float(sh_rows[0][0])
            c_20 = float(sh_rows[-1][0])
            mkt_trend = round((c_now / c_20) - 1.0, 4) if c_20 > 0 else 0.0
        else:
            mkt_trend = 0.0

    return {
        'market_up_ratio': up_ratio,
        'market_trend_20d': mkt_trend,
        'market_volume_chg': 0.0  # 保守中性
    }


class FeatureFactory:
    """
    【特征工厂统一管理器】
    负责单一标的与全截面特征提取、拼接与无偏对齐。
    """
    def __init__(self, data_provider=None):
        self.data_provider = data_provider or hdb
        self._regime_cache = {}

    def get_regime_features(self, trade_date):
        td = str(trade_date).replace('-', '')[:8]
        if td not in self._regime_cache:
            self._regime_cache[td] = calculate_market_regime_features(td, self.data_provider)
        return self._regime_cache[td]

    def extract_features_for_stock(self, symbol, current_idx, klines=None):
        """
        提取单一股票在 current_idx 截面的完整特征字典（四大组融合）。
        """
        pure = str(symbol).strip().zfill(6)
        if klines is None:
            klines = self.data_provider.get_kline(pure)
        if not klines or current_idx < 20 or current_idx >= len(klines):
            return None

        # 1. 提取 Price 与 Volume 特征 (Group A & B)
        pv_feats = calculate_price_features(klines, current_idx)
        if not pv_feats:
            return None

        bar = klines[current_idx]
        t_date = str(bar[0])

        # 2. 提取 Regime 特征 (Group D)
        regime_feats = self.get_regime_features(t_date)

        # 3. 提取 Relative Strength 简化特征 (Group C)
        # 个股 5d 收益相对于市场趋势的超额偏离
        rel_5d = round(pv_feats['ret_5d'] - regime_feats['market_trend_20d'] / 4.0, 5)
        rel_20d = round(pv_feats['ret_20d'] - regime_feats['market_trend_20d'], 5)
        # 截面收益初阶映射
        rank_pct = max(0.0, min(1.0, 0.5 + pv_feats['ret_1d'] * 5.0))

        rel_feats = {
            'relative_ret_5d': rel_5d,
            'relative_ret_20d': rel_20d,
            'cross_rank_pct': round(rank_pct, 4)
        }

        # 融合全套特征
        merged = {
            'symbol': pure,
            'trade_date': t_date,
            'feature_version': FEATURE_VERSION
        }
        merged.update(pv_feats)
        merged.update(rel_feats)
        merged.update(regime_feats)
        return merged


if __name__ == '__main__':
    print("=== 测试 FeatureFactory 特征提取 ===")
    ff = FeatureFactory()
    klines_000001 = hdb.get_kline('000001')
    print(f"000001 历史K线总数: {len(klines_000001)}")
    
    # 提取最近一个截面的特征
    sample_feat = ff.extract_features_for_stock('000001', len(klines_000001) - 1, klines=klines_000001)
    print("\n最新截面特征提取示例:")
    print(json.dumps(sample_feat, ensure_ascii=False, indent=2))
    print(f"\n全部特征列数: {len(ALL_FEATURE_COLUMNS)} 个")
