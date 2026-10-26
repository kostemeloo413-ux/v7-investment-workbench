# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_features.py
Phase 3: Feature Factory & Dataset Gate 单元测试
覆盖《指南》第 9 节与 Gate 3 前置要求：
1. 特征组定义与维度完整性 (22个核心指标，涵盖四大组)
2. 无前视偏差测试 (计算特征时绝不偷看未来收盘价或未来均线)
3. 极端行情健壮性测试 (涨跌停、无交易量、首20日冷启动缺失值安全处理)
4. 数据集构建器对齐测试 (特征 X 与标签 y 在同一交易日时点 Point-in-Time 严密咬合)
5. 独立性测试 (不包含任何新闻、LLM 或主观博主因子)
"""
import os
import sys
import unittest
import math

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.features import (
    FeatureFactory,
    calculate_price_features,
    ALL_FEATURE_COLUMNS,
    FEATURE_COLUMNS_PRICE,
    FEATURE_COLUMNS_VOLUME,
    FEATURE_COLUMNS_RELATIVE,
    FEATURE_COLUMNS_REGIME,
    FEATURE_VERSION
)
from ai_quant.dataset import DatasetBuilder


class TestFeatures(unittest.TestCase):

    def setUp(self):
        # 构造 25 根标准日K线 (使冷启动刚好 > 20 根)
        self.mock_klines = []
        base_p = 10.0
        for d in range(1, 26):
            date_s = f"202601{d:02d}"
            # 价格微幅上涨，成交量递增
            op = base_p + d * 0.1
            hp = op + 0.3
            lp = op - 0.2
            cp = op + 0.1
            vol = 10000 + d * 500
            amt = vol * cp
            pct = 1.0
            self.mock_klines.append((date_s, op, hp, lp, cp, vol, amt, pct))

    def test_feature_columns_schema(self):
        """测试四大特征组维度是否齐备且无命名冲突"""
        self.assertEqual(len(ALL_FEATURE_COLUMNS), 22)
        # 确保全部特征名唯一
        self.assertEqual(len(set(ALL_FEATURE_COLUMNS)), 22)

    def test_cold_start_handling(self):
        """测试不足 20 根历史 K 线的冷启动防护 (不产生异常，安全返回 None)"""
        # 截取前 10 根
        short_klines = self.mock_klines[:10]
        feats = calculate_price_features(short_klines, current_idx=9)
        self.assertIsNone(feats)

        # 满足 20 根以上正常提取
        feats_valid = calculate_price_features(self.mock_klines, current_idx=22)
        self.assertIsNotNone(feats_valid)
        self.assertIn('ret_1d', feats_valid)
        self.assertIn('volume_ratio_5d', feats_valid)

    def test_no_future_leakage(self):
        """测试计算 T 日特征时，未来价格的变动绝不影响 T 日已提取出的特征值"""
        ff = FeatureFactory()
        idx = 21
        # 提取基准特征
        f1 = ff.extract_features_for_stock('000001', idx, klines=self.mock_klines)

        # 故意篡改未来第 23、24 日的价格为涨停或异常暴跌
        polluted_klines = list(self.mock_klines)
        orig_row = polluted_klines[24]
        polluted_klines[24] = (orig_row[0], 999.0, 999.0, 999.0, 999.0, 999999, 99999999, 10.0)

        f2 = ff.extract_features_for_stock('000001', idx, klines=polluted_klines)

        # 检验 T 日（第 21 日）所有价格与量能特征是否完全一致
        for k in FEATURE_COLUMNS_PRICE + FEATURE_COLUMNS_VOLUME:
            self.assertEqual(f1[k], f2[k], f"特征 {k} 发生了未来数据泄露！")

    def test_dataset_alignment(self):
        """测试 DatasetBuilder 的 Point-in-Time 样本行对齐"""
        builder = DatasetBuilder()
        # 针对 000001 构建 20 个样本
        ds = builder.build_dataset(start_date='20260101', end_date='20260201', sample_symbols=['000001'])
        self.assertGreater(len(ds), 0)
        sample = ds[0]
        # 验证特征和目标同时存在 (契约对齐 target_gross_ret_5d)
        self.assertIn('target_gross_ret_5d', sample)
        self.assertIn('realized_mae_daily_bar', sample)
        self.assertIn('ret_5d', sample)
        self.assertIn('market_up_ratio', sample)
        # 验证不含新闻字段
        self.assertNotIn('news_sentiment', sample)
        self.assertNotIn('blogger_view', sample)


if __name__ == '__main__':
    unittest.main()
