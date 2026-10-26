# -*- coding: utf-8 -*-
"""
scripts/ai_quant/models.py
Phase 4: 第一代机器学习模型（ML Models）

核心定位（《指南》第 10 节）：
1. 核心基准：以 LightGBM Ranker (LambdaMART 截面排序模型) 为主力；
   - 理由：全市场量化投资最终目标是选出排名前 5 的最优标的 (Top 5)，而不是纠结某股明天具体涨 4.2% 还是 4.6%；
   - 模型针对截面按 trade_date 自动生成 Query Group 结构；
   - 目标标签支持 5 日连续收益率及等级相关排序。
2. 对标基准 (Baseline Models)：
   - Logistic Regression / Ridge 线性排序基准，用于验证树模型非线性增量；
3. 模型管理与持久化：
   - 模型保存路径：models/ai_quant/{model_name}_{version}.txt 或 .pkl
   - 版本规范：MODEL_VERSION = "AIQ-V1.0"
   - 提供标准化的 predict_scores() 接口供后续推理调用。
"""

import os
import sys
import json
import joblib
import datetime
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.linear_model import Ridge

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ai_quant.features import ALL_FEATURE_COLUMNS, FEATURE_VERSION
from ai_quant.labels import LABEL_VERSION

MODEL_DIR = os.path.join(_BASE, 'models', 'ai_quant')
os.makedirs(MODEL_DIR, exist_ok=True)

MODEL_VERSION = "AIQ-V1.0"


class LightGBMRankerModel:
    """
    【主力排序模型：LightGBM LambdaMART】
    以每日交易日 (trade_date) 作为 Query Group，最大化截面 NDCG 排序质量。
    """
    def __init__(self, model_version=MODEL_VERSION, params=None):
        self.model_version = model_version
        self.feature_columns = list(ALL_FEATURE_COLUMNS)
        self.params = params or {
            'objective': 'lambdarank',
            'metric': 'ndcg',
            'ndcg_eval_at': [5, 10],
            'boosting_type': 'gbdt',
            'learning_rate': 0.05,
            'num_leaves': 31,
            'max_depth': 6,
            'min_data_in_leaf': 20,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'bagging_freq': 1,
            'verbosity': -1,
            'random_state': 2026,
            'n_estimators': 100
        }
        self.booster = None
        self.feature_importance_ = None

    def train(self, dataset_records, target_col='target_gross_ret_5d', val_records=None):
        """
        输入格式化数据集列表 (每个元素为 dict，包含所有特征和 trade_date)
        target_col: 默认对齐 R2 纯净毛收益 target_gross_ret_5d，兼容历史 target_ret_5d
        """
        if not dataset_records:
            raise ValueError("训练数据集不能为空！")

        df = pd.DataFrame(dataset_records)
        # 兼容 target_col
        if target_col not in df.columns and 'target_ret_5d' in df.columns:
            target_col = 'target_ret_5d'
        elif target_col not in df.columns and 'target_gross_ret_5d' in df.columns:
            target_col = 'target_gross_ret_5d'

        # 严格按日期时间排序生成 Query Groups
        df = df.sort_values(by=['trade_date', 'symbol']).reset_index(drop=True)

        X = df[self.feature_columns].astype(float)
        # 转换连续收益为 0~9 等级整数相关度 (LambdaMART 规范)
        # 按截面分位映射等级
        y_ret = df[target_col].astype(float)
        # 全局分箱 10 个等级
        y_ranked = pd.qcut(y_ret, q=10, labels=False, duplicates='drop')
        
        # 统计每个 trade_date 包含的样本数 (query group)
        groups = df.groupby('trade_date').size().values

        train_data = lgb.Dataset(X, label=y_ranked, group=groups, feature_name=self.feature_columns)

        val_data = None
        if val_records:
            df_val = pd.DataFrame(val_records).sort_values(by=['trade_date', 'symbol']).reset_index(drop=True)
            X_val = df_val[self.feature_columns].astype(float)
            y_val_ret = df_val[target_col].astype(float)
            y_val_ranked = pd.qcut(y_val_ret, q=10, labels=False, duplicates='drop')
            groups_val = df_val.groupby('trade_date').size().values
            val_data = lgb.Dataset(X_val, label=y_val_ranked, group=groups_val, reference=train_data)

        # 训练
        callbacks = [lgb.early_stopping(stopping_rounds=15, verbose=False)] if val_data else []
        self.booster = lgb.train(
            self.params,
            train_data,
            valid_sets=[val_data] if val_data else None,
            callbacks=callbacks
        )

        # 记录特征重要性
        imp = self.booster.feature_importance(importance_type='gain')
        self.feature_importance_ = dict(zip(self.feature_columns, imp))
        print(f"[LightGBMRanker] 训练完成！样本数={len(df)}, 截面数={len(groups)}")
        return self

    def predict_scores(self, feature_dicts):
        """
        输入一个或多个样本特征字典，输出各标的的模型综合排序得分（未缩放原始排序分）
        """
        if self.booster is None:
            raise RuntimeError("模型尚未训练或加载！")
        if not feature_dicts:
            return []

        df = pd.DataFrame(feature_dicts)
        for col in self.feature_columns:
            if col not in df.columns:
                df[col] = 0.0
        X = df[self.feature_columns].astype(float)
        scores = self.booster.predict(X)
        return scores.tolist()

    def save(self, filepath=None):
        fp = filepath or os.path.join(MODEL_DIR, f"lgb_ranker_{self.model_version}.txt")
        if self.booster is None:
            raise RuntimeError("无模型可保存")
        # 兼容 Windows 宽字符中文路径：优先用 model_to_string 并经 Python 写入
        model_str = self.booster.model_to_string()
        with open(fp, 'w', encoding='utf-8') as f:
            f.write(model_str)
        # 同时保存元数据
        meta_fp = fp.replace('.txt', '_meta.json')
        with open(meta_fp, 'w', encoding='utf-8') as f:
            json.dump({
                'model_version': self.model_version,
                'feature_version': FEATURE_VERSION,
                'label_version': LABEL_VERSION,
                'feature_columns': self.feature_columns,
                'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'feature_importance': self.feature_importance_
            }, f, ensure_ascii=False, indent=2)
        print(f"[LightGBMRanker] 模型已固化至: {fp}")
        return fp

    def load(self, filepath=None):
        fp = filepath or os.path.join(MODEL_DIR, f"lgb_ranker_{self.model_version}.txt")
        if not os.path.exists(fp):
            raise FileNotFoundError(f"找不到模型文件: {fp}")
        # 兼容 Windows 中文路径：从文本直接加载 Booster
        with open(fp, 'r', encoding='utf-8') as f:
            model_str = f.read()
        self.booster = lgb.Booster(model_str=model_str)
        meta_fp = fp.replace('.txt', '_meta.json')
        if os.path.exists(meta_fp):
            with open(meta_fp, 'r', encoding='utf-8') as f:
                meta = json.load(f)
                self.feature_importance_ = meta.get('feature_importance')
        print(f"[LightGBMRanker] 模型成功加载: {fp}")
        return self


class BaselineRidgeModel:
    """
    【对标基准模型：Ridge 线性排序】
    用于验证 LightGBM 树模型的非线性增量超额表现。
    """
    def __init__(self, model_version="BASE-RIDGE-V1.0", alpha=1.0):
        self.model_version = model_version
        self.feature_columns = list(ALL_FEATURE_COLUMNS)
        self.model = Ridge(alpha=alpha, random_state=2026)

    def train(self, dataset_records, target_col='target_gross_ret_5d'):
        df = pd.DataFrame(dataset_records)
        if target_col not in df.columns and 'target_ret_5d' in df.columns:
            target_col = 'target_ret_5d'
        elif target_col not in df.columns and 'target_gross_ret_5d' in df.columns:
            target_col = 'target_gross_ret_5d'
        X = df[self.feature_columns].fillna(0.0).astype(float)
        y = df[target_col].astype(float)
        self.model.fit(X, y)
        print(f"[BaselineRidge] 训练完成！样本数={len(df)}")
        return self

    def predict_scores(self, feature_dicts):
        if not feature_dicts:
            return []
        df = pd.DataFrame(feature_dicts)
        for col in self.feature_columns:
            if col not in df.columns:
                df[col] = 0.0
        X = df[self.feature_columns].fillna(0.0).astype(float)
        return self.model.predict(X).tolist()

    def save(self, filepath=None):
        fp = filepath or os.path.join(MODEL_DIR, f"ridge_baseline_{self.model_version}.pkl")
        joblib.dump(self.model, fp)
        return fp

    def load(self, filepath=None):
        fp = filepath or os.path.join(MODEL_DIR, f"ridge_baseline_{self.model_version}.pkl")
        self.model = joblib.load(fp)
        return self


if __name__ == '__main__':
    print("=== 测试 LightGBMRankerModel 模型训练与持久化 ===")
    from ai_quant.dataset import DatasetBuilder
    builder = DatasetBuilder()
    stocks = ['000001', '000002', '600519', '601318', '600036', '000858', '601899', '600028', '601857']
    ds = builder.build_dataset('20250601', '20260815', sample_symbols=stocks)
    print(f"提取训练样本数: {len(ds)}")

    # 划分训练集和测试集 (按时间截面拆分)
    train_ds = [d for d in ds if d['trade_date'] <= '20260601']
    test_ds = [d for d in ds if d['trade_date'] > '20260601']
    print(f"时序拆分: 训练集={len(train_ds)}, 样本外OOS={len(test_ds)}")

    # 1. 训练主力 Ranker
    ranker = LightGBMRankerModel()
    ranker.train(train_ds, val_records=test_ds)
    m_path = ranker.save()

    # 2. 预测与评估
    test_scores = ranker.predict_scores(test_ds)
    print(f"OOS 预测成功，得分样本前 5: {[round(s, 3) for s in test_scores[:5]]}")

    # 3. 重新加载测试
    new_ranker = LightGBMRankerModel()
    new_ranker.load(m_path)
    re_scores = new_ranker.predict_scores(test_ds[:5])
    print(f"加载后复测得分前 5: {[round(s, 3) for s in re_scores]}")
    assert np.allclose(test_scores[:5], re_scores), "模型加载前后预测不一致！"
    print("✅ 模型持久化与复现性验证完美通过！")
