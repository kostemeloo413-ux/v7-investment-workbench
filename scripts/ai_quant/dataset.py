# -*- coding: utf-8 -*-
"""
scripts/ai_quant/dataset.py
Phase 3: Dataset Builder（研究数据集构建器）

核心职责：
1. 将 Point-in-Time 股票池 (eligible_universe)、特征工厂 (features) 与标签工厂 (labels) 严格按时序对齐；
2. 构建标准机器学习样本矩阵（X: 特征矩阵, y: 标签向量/字典, groups: 截面时间分组）；
3. 严格执行时间截面纪律（按 trade_date 分组，供 LightGBM Ranker 等按截面排序）；
4. 支持双轨 Entry Proxy（entry_proxy_mode: 'vwap' / 'next_open'）与参数化滑点构建；
5. Target Mapping 严格对齐 R2 契约：纯净毛收益 target_gross_ret_5d 与 realized_mae_daily_bar / realized_mfe_daily_bar。
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
from ai_quant.universe import eligible_universe
from ai_quant.features import FeatureFactory, ALL_FEATURE_COLUMNS, FEATURE_VERSION
from ai_quant.labels import calculate_single_stock_labels, LABEL_VERSION

DATASET_DIR = os.path.join(_BASE, 'data', 'ai_quant_research')


class DatasetBuilder:
    def __init__(self):
        self.ff = FeatureFactory()
        os.makedirs(DATASET_DIR, exist_ok=True)

    def build_dataset(self, start_date='20240101', end_date='20260825', sample_symbols=None,
                      horizon=5, min_bars=60, entry_proxy_mode='vwap', slippage_rate=0.001):
        """
        构建结构化研究数据集：
        - start_date ~ end_date: 样本截面区间
        - sample_symbols: 抽样股票列表（若为 None 则使用 eligible_universe）
        - horizon: 默认对齐 5 日目标
        - entry_proxy_mode: 'vwap' (主轨 HISTORICAL_VWAP_PROXY) 或 'next_open' (辅轨)
        - slippage_rate: float 滑点参数
        - 仅纳入 is_mature == True 的成熟样本
        返回：
          records: list of dict, 每个样本包含全套特征 X 与标签 y
        """
        # 1. 确定目标标的
        if sample_symbols is None:
            # 取基准日合规池
            u = eligible_universe(end_date, min_list_days=min_bars)
            symbols = [s['symbol'] for s in u]
        else:
            symbols = [str(s).strip().zfill(6) for s in sample_symbols]

        print(f"[dataset] 启动数据集构建: 涵盖 {len(symbols)} 只标的, 截面范围 {start_date} ~ {end_date}, "
              f"Proxy={entry_proxy_mode}, 滑点={slippage_rate}")

        dataset = []
        for idx, sym in enumerate(symbols):
            klines = hdb.get_kline(sym)
            if not klines or len(klines) < 30:
                continue

            n_bars = len(klines)
            for i in range(20, n_bars):
                t_date = str(klines[i][0])
                if t_date < str(start_date) or t_date > str(end_date):
                    continue

                # 提取未来标签（支持双轨 Entry Proxy 与参数化滑点，成熟度硬过滤）
                lbl = calculate_single_stock_labels(klines, i, horizons=(1, 2, 3, 5),
                                                    entry_proxy_mode=entry_proxy_mode,
                                                    slippage_rate=slippage_rate)
                if not lbl or not lbl.get('is_mature'):
                    continue

                # 提取当日特征
                feats = self.ff.extract_features_for_stock(sym, i, klines=klines)
                if not feats:
                    continue

                # 合并为样本行 (契约规范对齐：纯净毛收益与规范事实极值)
                record = {
                    'symbol': sym,
                    'trade_date': t_date,
                    'target_gross_ret_5d': lbl[f'future_{horizon}d_gross_return'],
                    'target_is_profitable': lbl['is_profitable_5d'],
                    'realized_mae_daily_bar': lbl['realized_mae_daily_bar'],
                    'realized_mfe_daily_bar': lbl['realized_mfe_daily_bar'],
                    'target_is_risk': lbl['is_risk_triggered'],
                    'entry_proxy_type': lbl['entry_proxy_type'],
                    'slippage_param': lbl['slippage_param'],
                    'label_version': lbl['label_version'],
                }
                # 注入所有特征列
                for col in ALL_FEATURE_COLUMNS:
                    record[col] = feats.get(col, 0.0)

                dataset.append(record)

            if (idx + 1) % 20 == 0:
                print(f"  ...已处理 {idx + 1}/{len(symbols)} 只标的，当前样本量: {len(dataset)}")

        print(f"[dataset] 数据集构建完毕！累计成熟样本行数: {len(dataset)}")
        return dataset


if __name__ == '__main__':
    print("=== 测试 DatasetBuilder 轻量抽样构建 ===")
    builder = DatasetBuilder()
    # 抽样 2 只知名标的构建轻量样本测试接口
    sample_stocks = ['000001', '600519']
    data = builder.build_dataset(start_date='20260101', end_date='20260301',
                                 sample_symbols=sample_stocks, entry_proxy_mode='vwap')
    print(f"抽样构建成功: {len(data)} 条训练特征行")
    if data:
        print("样本第 1 行特征与目标预览:")
        print(json.dumps(data[0], ensure_ascii=False, indent=2))
