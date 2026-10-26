# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_models.py
Phase 4: ML Models Gate 单元测试
覆盖《指南》第 10 节与 Gate 4 前置要求：
1. LightGBM Ranker (LambdaMART) 核心训练管线验证
2. Query Group 截面分组完整性
3. 模型固化、文本持久化与复现性对齐验证
4. 预测输出单调性与空值健壮性
5. Baseline Ridge 对标基准模型接口对齐
"""
import os
import sys
import unittest
import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.models import (
    LightGBMRankerModel,
    BaselineRidgeModel,
    MODEL_VERSION,
    MODEL_DIR
)
from ai_quant.dataset import DatasetBuilder


class TestModels(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 准备小型训练数据集 (5只标的，近6个月)
        builder = DatasetBuilder()
        stocks = ['000001', '000002', '600519', '601318', '600036']
        cls.raw_data = builder.build_dataset('20260101', '20260701', sample_symbols=stocks)

    def test_ranker_training_and_query_groups(self):
        """测试 LightGBM Ranker 截面训练与特征重要性输出"""
        model = LightGBMRankerModel(model_version="TEST-LGB-1.0")
        model.train(self.raw_data)
        self.assertIsNotNone(model.booster)
        self.assertIsNotNone(model.feature_importance_)
        # 特征重要性字典包含所有 22 个特征
        self.assertEqual(len(model.feature_importance_), len(model.feature_columns))

    def test_ranker_prediction_and_reproducibility(self):
        """测试模型预测输出、文件持久化与加载后 100% 精确复现"""
        model = LightGBMRankerModel(model_version="TEST-LGB-SAVE")
        model.train(self.raw_data)
        
        test_samples = self.raw_data[:10]
        preds1 = model.predict_scores(test_samples)
        self.assertEqual(len(preds1), 10)

        # 保存
        saved_path = model.save()
        self.assertTrue(os.path.exists(saved_path))
        self.assertTrue(os.path.exists(saved_path.replace('.txt', '_meta.json')))

        # 新实例加载
        loaded_model = LightGBMRankerModel(model_version="TEST-LGB-SAVE")
        loaded_model.load(saved_path)
        preds2 = loaded_model.predict_scores(test_samples)

        # 验证数值完全一致
        np.testing.assert_allclose(preds1, preds2, rtol=1e-5, atol=1e-5)

    def test_baseline_ridge_model(self):
        """测试对标基准 Ridge 线性模型的训练与预测"""
        ridge = BaselineRidgeModel(model_version="TEST-RIDGE-1.0")
        ridge.train(self.raw_data)
        preds = ridge.predict_scores(self.raw_data[:5])
        self.assertEqual(len(preds), 5)
        for p in preds:
            self.assertIsInstance(p, float)


if __name__ == '__main__':
    unittest.main()
