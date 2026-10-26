# -*- coding: utf-8 -*-
"""
scripts/ai_quant/ranking.py
Phase 6: AI Top 5 动态排名引擎（Top 5 Ranking Engine）

核心职责（《指南》第 1.3 & 1.4 & 12 节）：
1. 输入：全市场合规股票池 eligible_universe(t)；
2. A股交易可行性过滤（客观过滤，不掺人工主观偏见）：
   - 剔除停牌、ST/退市；
   - 剔除极端流动性匮乏（5日均成交额 < 2000万元）；
3. 特征与模型推理：
   - 调用 FeatureFactory 提取 22 维标准特征；
   - 调用 AIQuantPredictor 输出上涨概率、预期收益、风险 MAE、右侧确认分；
4. 综合排序输出 Top 5：
   - 排序目标：以预期风险调整后超额收益综合排定全市场前 5；
   - 铁律：已持仓股票允许继续出现在 Top 5；
   - Top 5 是全市场状态排名，不等于买入清单；
5. 结果落盘与留痕：
   - 写入 SQLite 的 ai_quant_predictions 表；
   - 供 AI Decision 与前端 Alpha 竞技场消费。
"""

import os
import sys
import json
import sqlite3
import datetime
import math

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb
from ai_quant.universe import eligible_universe
from ai_quant.features import FeatureFactory, ALL_FEATURE_COLUMNS
from ai_quant.production_input import QualifiedQFQInput, ProductionInputError
from ai_quant.predict import AIQuantPredictor
from ai_quant.governance_policy import GovernanceSafetyPolicy

_GOVERNANCE_POLICY = GovernanceSafetyPolicy()

SCHEMA_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS ai_quant_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    predict_time TEXT NOT NULL,         -- 'YYYY-MM-DD HH:MM:SS'
    trade_date TEXT NOT NULL,           -- 'YYYYMMDD'
    symbol TEXT NOT NULL,               -- 股票代码 '000001'
    name TEXT NOT NULL,                 -- 股票名称
    rank INTEGER NOT NULL,              -- 当期排名 (1~5)
    raw_score REAL,                     -- 模型裸打分
    up_probability REAL NOT NULL,       -- 校准上涨概率 (0~1)
    expected_return REAL NOT NULL,      -- 预期收益率
    model_expected_return REAL,         -- 模型预测收益
    probability_weighted_return REAL,   -- 概率加权收益
    expected_mae REAL NOT NULL,         -- 预期最大不利波动 MAE
    risk_probability REAL NOT NULL,     -- 风险概率
    right_side_score INTEGER NOT NULL,  -- 右侧确认分 (0~100)
    expected_holding_days INTEGER,      -- 建议持有天数 (1~5)
    model_version TEXT NOT NULL         -- 模型版本号
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON ai_quant_predictions(trade_date);
CREATE INDEX IF NOT EXISTS idx_pred_sym ON ai_quant_predictions(symbol);
"""


def init_prediction_table(writer_id='db_init'):
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'ai_quant_predictions', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")
    with hdb._connect() as conn:
        conn.executescript(SCHEMA_PREDICTIONS)


class Top5RankingEngine:
    """
    【Top 5 动态排名引擎】
    """
    def __init__(self, predictor=None, input_provider=None, persist_predictions=True):
        # Constructor is read-only: schema initialization belongs to the
        # existing authorized database initializer, never a ranking probe.
        self.input_provider = input_provider or QualifiedQFQInput()
        self.persist_predictions = persist_predictions
        self.ff = FeatureFactory(data_provider=self.input_provider)
        self.predictor = predictor or AIQuantPredictor()
        if self.predictor.ranker.booster is None or not self.predictor.calibrator.is_fitted:
            raise ProductionInputError('AIQ_FROZEN_MODEL_OR_CALIBRATOR_UNAVAILABLE')

    def rank_market(self, as_of_date=None, candidate_symbols=None, top_k=5):
        """
        对指定日期或当前盘中执行全市场筛选并输出 Top 5 候选标的画像。
        """
        now = datetime.datetime.now()
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        td = str(as_of_date or now.strftime('%Y%m%d')).replace('-', '')[:8]
        self.input_provider.validate_as_of(td)

        # 1. 获取合规股票池 (Point-in-Time)
        if candidate_symbols is None:
            pool = eligible_universe(td, min_list_days=60, exclude_st=True, exclude_suspended=True, data_provider=self.input_provider)
        else:
            cand_set = set(str(s).strip().zfill(6) for s in candidate_symbols)
            pool = [s for s in eligible_universe(td, min_list_days=60, exclude_st=True, data_provider=self.input_provider) if s['symbol'] in cand_set]

        if not pool:
            return []

        # 2. 提取特征并做流动性硬过滤
        valid_features = []
        self.input_audit = {'as_of_date':td, 'history_cutoff':self.input_provider.cutoff,
                            'eligible_pool_count':len(pool), 'excluded':{}, 'feature_rows':[]}
        stock_meta_map = {s['symbol']: s for s in pool}

        for item in pool:
            sym = item['symbol']
            klines = self.input_provider.get_kline(sym)
            if not klines or len(klines) < 25:
                self.input_audit['excluded'][sym] = 'LESS_THAN_25_VALID_BARS'
                continue

            # 找到 T 日索引
            target_idx = None
            for i in range(len(klines) - 1, -1, -1):
                if str(klines[i][0]) <= td:
                    target_idx = i
                    break

            if target_idx is None or target_idx < 20:
                continue

            # 客观流动性过滤：近5日均成交额 < 2000万元 (防微盘流动性枯竭)
            recent_amts = [float(klines[k][6]) for k in range(target_idx - 4, target_idx + 1)]
            if (sum(recent_amts) / 5.0) < 20000000.0:
                self.input_audit['excluded'][sym] = 'FIVE_DAY_ACTUAL_AMOUNT_LT_20M_CNY'
                continue

            feat = self.ff.extract_features_for_stock(sym, target_idx, klines=klines)
            if not feat or any(k not in feat or not isinstance(feat[k], (int,float))
                               or not math.isfinite(feat[k]) for k in ALL_FEATURE_COLUMNS):
                raise ProductionInputError('AIQ_FEATURE_NONFINITE_OR_MISSING:' + sym)
            if feat:
                valid_features.append(feat)
        self.input_audit['feature_rows'] = valid_features

        if not valid_features:
            return []

        # 3. 批量多维画像推断
        profiles = self.predictor.predict_stock_profile(valid_features)

        # 4. 综合排序评定 Top 5
        # 综合排序分：预期收益 (权重40%) + 校准上涨概率 (权重30%) + 右侧确认分 (权重30%)
        for pf in profiles:
            p_score = (
                pf['expected_return'] * 40.0 +
                (pf['up_probability'] - 0.5) * 30.0 +
                ((pf['right_side_score'] - 50.0) / 50.0) * 30.0
            )
            pf['composite_rank_score'] = round(p_score, 4)
            # 补齐标的名称与板块
            meta = stock_meta_map.get(pf['symbol'], {})
            pf['name'] = meta.get('name', pf['symbol'])
            pf['industry'] = meta.get('industry', '主板')
            pf['board'] = meta.get('board', 'MAIN')

        # 降序排列
        profiles.sort(key=lambda x: x['composite_rank_score'], reverse=True)
        top5_list = profiles[:top_k]

        # 赋予 rank 序号 (1~5)
        records_to_save = []
        for r_idx, pf in enumerate(top5_list, start=1):
            pf['rank'] = r_idx
            pred_mae = pf.get('predicted_mae', pf.get('expected_mae', -0.028))
            records_to_save.append((
                now_str, td, pf['symbol'], pf['name'], r_idx,
                pf['raw_score'], pf['up_probability'], pf['expected_return'],
                pf['model_expected_return'], pf['probability_weighted_return'],
                pred_mae, pf['risk_probability'],
                pf['right_side_score'], pf['expected_holding_days'],
                self.predictor.ranker.model_version
            ))

        # 5. 留痕写入 SQLite
        if not self.persist_predictions:
            return top5_list
        try:
            # 运行时写授权校验 (P1-1)
            is_auth, msg = _GOVERNANCE_POLICY.validate_db_write('ai_quant_ranker', 'ai_quant_predictions', 'INSERT')
            if not is_auth:
                _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
                raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

            with hdb._connect() as conn:
                conn.executemany("""
                    INSERT INTO ai_quant_predictions (
                        predict_time, trade_date, symbol, name, rank,
                        raw_score, up_probability, expected_return,
                        model_expected_return, probability_weighted_return,
                        expected_mae, risk_probability, right_side_score,
                        expected_holding_days, model_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, records_to_save)
        except Exception as e:
            print(f"[ranking] 保存 predictions 失败: {e}")

        return top5_list


if __name__ == '__main__':
    print("=== 测试 Top5RankingEngine 动态排名 ===")
    ranker = Top5RankingEngine()
    # 针对 10 只样本做排序测试
    stocks = ['000001', '000002', '600519', '601318', '600036', '000858', '601899', '600028', '601857', '002594']
    top5 = ranker.rank_market(as_of_date='20260904', candidate_symbols=stocks, top_k=5)
    print(f"\n成功输出 Top 5 榜单 (共 {len(top5)} 只):")
    for s in top5:
        print(f"  #{s['rank']} {s['name']} ({s['symbol']}) | 综合分:{s['composite_rank_score']} | "
              f"上涨概率:{s['up_probability_pct']}% | 预期收益:{s['expected_return_pct']}% | "
              f"MAE:{s['expected_mae_pct']}% | 右侧分:{s['right_side_score']} | 建议持有:{s['expected_holding_days']}天")
