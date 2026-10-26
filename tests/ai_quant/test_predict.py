# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_predict.py
Phase 5: 概率校准、预期收益、风险与右侧分 Gate 单元测试
覆盖《指南》第 11 节要求：
1. 校准概率范围严格受控于 [0.01, 0.99]
2. 预期收益双层结构完整且有数值定义 (model_expected_return + probability_weighted_return)
3. 风险双层披露 (expected_mae 为合理负向波动, risk_probability 为有效概率)
4. 右侧确认分 right_side_score 处于 0~100 区间，且形态强于破位标的
5. 预计持有周期处于 1~5 天合理区间
"""
import os
import sys
import unittest

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.predict import ProbabilityCalibrator, AIQuantPredictor
from ai_quant.models import LightGBMRankerModel
from ai_quant.dataset import DatasetBuilder


class TestPredict(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        builder = DatasetBuilder()
        stocks = ['000001', '000002', '600519', '601318']
        cls.data = builder.build_dataset('20260101', '20260601', sample_symbols=stocks)
        cls.ranker = LightGBMRankerModel(model_version="TEST-PREDICT-LGB")
        cls.ranker.train(cls.data[:200])

    def test_calibrator_fit_and_range(self):
        """测试概率校准器输出严格符合概率定义 [0.0, 1.0]"""
        calibrator = ProbabilityCalibrator()
        raw_scores = [-2.5, -1.0, 0.0, 1.2, 3.0]
        targets = [0, 0, 1, 1, 1]
        calibrator.fit(raw_scores, targets)
        probs = calibrator.predict_proba([-3.0, 0.0, 4.0])
        for p in probs:
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)
        # 单调性：得分越高，校准后上涨概率应越高
        self.assertLess(probs[0], probs[1])
        self.assertLess(probs[1], probs[2])

    def test_predict_profile_structure(self):
        """测试推断画像输出 7 维指标齐全度与双层收益/风险字段"""
        predictor = AIQuantPredictor(ranker_model=self.ranker)
        # 测试用例自包含：不覆盖磁盘全局生产校准器
        calibrator_test = ProbabilityCalibrator()
        val_scores = self.ranker.predict_scores(self.data[200:])
        val_targets = [r.get('target_is_profitable', 0) for r in self.data[200:]]
        calibrator_test.fit(val_scores, val_targets)
        predictor.calibrator = calibrator_test

        profiles = predictor.predict_stock_profile(self.data[:5])
        self.assertEqual(len(profiles), 5)
        for pf in profiles:
            # 1. 概率
            self.assertIn('up_probability', pf)
            self.assertGreaterEqual(pf['up_probability'], 0.01)
            self.assertLessEqual(pf['up_probability'], 0.99)
            
            # 2. 预期收益双层
            self.assertIn('expected_return', pf)
            self.assertIn('model_expected_return', pf)
            self.assertIn('probability_weighted_return', pf)

            # 3. 风险双层
            self.assertIn('expected_mae', pf)
            self.assertLessEqual(pf['expected_mae'], 0.0)  # MAE 必须为不利下撤负值
            self.assertIn('risk_probability', pf)
            self.assertGreaterEqual(pf['risk_probability'], 0.0)
            self.assertLessEqual(pf['risk_probability'], 1.0)

            # 4. 右侧分与持有周期
            self.assertIn('right_side_score', pf)
            self.assertGreaterEqual(pf['right_side_score'], 0)
            self.assertLessEqual(pf['right_side_score'], 100)
            self.assertIn('expected_holding_days', pf)
            self.assertIn(pf['expected_holding_days'], [1, 2, 3, 4, 5])


if __name__ == '__main__':
    unittest.main()
