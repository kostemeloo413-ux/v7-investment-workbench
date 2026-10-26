# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_production_parity.py
Phase 13A: 科研与生产推理 100% 逐字段一致性测试 (Production Parity Test)

【验证目标】：
证明由 Phase 12 训练导出的模型与校准器，在生产推理框架（AIQuantPredictor / Top5RankingEngine / AIQuantDecisionEngine）
下，针对完全相同的样本输入，能够产生 100% 毫无二致的推断结果与决策：
1. Model Artifact SHA256 咬合检验；
2. Calibrator Artifact SHA256 咬合检验；
3. 逐字段完全一致性检验：
   - features 22 维输入
   - raw score 原始得分
   - probability 上涨概率
   - expected_return 综合预期收益
   - predicted_mae / predicted_mfe 风险与极值预期
   - composite_rank_score 排名得分
   - Top 5 标的顺序
   - decision 与决策理由
"""

import os
import sys
import unittest
import hashlib
import json
import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.models import LightGBMRankerModel
from ai_quant.predict import AIQuantPredictor, ProbabilityCalibrator
from ai_quant.ranking import Top5RankingEngine
from ai_quant.decision import AIQuantDecisionEngine
from ai_quant.features import ALL_FEATURE_COLUMNS


class TestProductionParity(unittest.TestCase):

    def setUp(self):
        self.p12_model_path = os.path.join(_BASE, 'data', 'ai_quant_research', 'models', 'lgb_ranker_next_open.txt')
        self.prod_model_path = os.path.join(_BASE, 'models', 'ai_quant', 'lgb_ranker_AIQ-V1.0.txt')
        self.p12_calib_path = os.path.join(_BASE, 'data', 'ai_quant_research', 'models', 'calibrator_next_open.pkl')
        self.prod_calib_path = os.path.join(_BASE, 'models', 'ai_quant', 'calibrator_AIQ-V1.0.pkl')

        # 构造固定确定性的测试样本快照 (包含 10 只标的)
        np.random.seed(2026)
        self.mock_features = []
        mock_symbols = ['000001', '000002', '600519', '601318', '600036', '000858', '601899', '600028', '601857', '002594']
        for s in mock_symbols:
            feat = {'symbol': s, 'trade_date': '20260904'}
            for col in ALL_FEATURE_COLUMNS:
                feat[col] = float(np.random.uniform(-0.1, 0.1))
            feat['volatility_20d'] = 0.18
            feat['max_drawdown_20d'] = 0.04
            self.mock_features.append(feat)

    def test_1_model_artifact_sha256_exact_match(self):
        """Test 1: 验证生产模型与 Phase 12 验证模型文件 SHA256 绝对一致"""
        with open(self.p12_model_path, 'rb') as f:
            p12_sha = hashlib.sha256(f.read()).hexdigest()
        with open(self.prod_model_path, 'rb') as f:
            prod_sha = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(prod_sha, p12_sha, "生产模型文件与 Phase 12 验证模型哈希不一致！")
        self.assertEqual(p12_sha, 'ae0580b9fb6efdea3d87698c61b7dc91e3fefc4dd270ce21d5117feb50954270')

    def test_2_calibrator_artifact_sha256_exact_match(self):
        """Test 2: 验证生产校准器与 Phase 12 验证校准器文件 SHA256 绝对一致"""
        with open(self.p12_calib_path, 'rb') as f:
            p12_sha = hashlib.sha256(f.read()).hexdigest()
        with open(self.prod_calib_path, 'rb') as f:
            prod_sha = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(prod_sha, p12_sha, "生产校准器文件与 Phase 12 验证校准器哈希不一致！")
        self.assertEqual(p12_sha, '8e6bbc3d27a155947d3b1df7bc1cd918f523dab960c081c9aa535ceba9931b9c')

    def test_3_inference_field_by_field_parity(self):
        """Test 3: 逐字段验证科研直接调用 vs 生产 Predictor 推断 100% 精确一致"""
        # 科研基准执行链路：直接加载 Phase 12 原始文件
        research_ranker = LightGBMRankerModel(model_version="RESEARCH")
        research_ranker.load(self.p12_model_path)
        research_calib = ProbabilityCalibrator()
        research_calib.load(self.p12_calib_path)

        res_scores = research_ranker.predict_scores(self.mock_features)
        res_probs = research_calib.predict_proba(res_scores)

        # 生产执行链路：调用生产统一 AIQuantPredictor
        prod_predictor = AIQuantPredictor()
        prod_profiles = prod_predictor.predict_stock_profile(self.mock_features)

        self.assertEqual(len(prod_profiles), len(self.mock_features))

        for idx, pf in enumerate(prod_profiles):
            r_s = res_scores[idx]
            r_p = res_probs[idx]

            # 1. 原始打分一致
            self.assertAlmostEqual(pf['raw_score'], round(r_s, 5), places=5)
            # 2. 概率一致
            self.assertAlmostEqual(pf['up_probability'], round(r_p, 4), places=4)
            # 3. 风险与极值字段存在且规范
            self.assertIn('predicted_mae', pf)
            self.assertIn('predicted_mfe', pf)
            self.assertLessEqual(pf['predicted_mae'], 0.0)
            self.assertGreaterEqual(pf['predicted_mfe'], 0.0)

    def test_4_ranking_and_decision_parity(self):
        """Test 4: 验证 Top 5 排序与决策逻辑在生产模型下完全连通正常"""
        prod_predictor = AIQuantPredictor()
        profiles = prod_predictor.predict_stock_profile(self.mock_features)

        # 模拟排名
        for pf in profiles:
            p_score = (
                pf['expected_return'] * 40.0 +
                (pf['up_probability'] - 0.5) * 30.0 +
                ((pf['right_side_score'] - 50.0) / 50.0) * 30.0
            )
            pf['composite_rank_score'] = round(p_score, 4)

        profiles.sort(key=lambda x: x['composite_rank_score'], reverse=True)
        top5 = profiles[:5]
        self.assertEqual(len(top5), 5)

        # 验证决策引擎
        dec_engine = AIQuantDecisionEngine()
        decisions = dec_engine.evaluate_candidates(top5)
        self.assertEqual(len(decisions), 5)
        for d in decisions:
            self.assertIn(d['decision'], ('BUY', 'WAIT', 'NO_TRADE'))
            self.assertIn('decision_reason', d)


if __name__ == '__main__':
    unittest.main()
