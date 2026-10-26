# -*- coding: utf-8 -*-
"""
scripts/ai_quant/trade_plan.py
Phase 8: AI 量化交易计划合同与冻结引擎（Trade Plan / Trade Contract）

核心职责（《指南》第 1.10 & 14 节）：
1. 买入前一次性生成 Trade Contract：
   - 当 AI Decision == BUY，在下单前必须确定：Entry Price、Position Size、Stop Loss、Take Profit、Expected Holding Days；
2. 买入后永久冻结（Frozen）：
   - 写入 SQLite 的 ai_quant_trade_plans 表，标记 is_frozen = 1；
   - 禁止因浮亏临时放大止损，禁止根据事后结果修改计划；
3. 版本号永久锁死：
   - 记录 model_version、feature_version、label_version、decision_version，支持事后审计与追溯。
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
from ai_quant.models import MODEL_VERSION
from ai_quant.features import FEATURE_VERSION
from ai_quant.labels import LABEL_VERSION
from ai_quant.decision import DECISION_VERSION
from ai_quant.governance_policy import GovernanceSafetyPolicy

_GOVERNANCE_POLICY = GovernanceSafetyPolicy()

SCHEMA_TRADE_PLANS = """
CREATE TABLE IF NOT EXISTS ai_quant_trade_plans (
    plan_id TEXT PRIMARY KEY,           -- 计划唯一编号 'PLAN_20260905_000001_1'
    correlation_id TEXT,                -- 全局链路追踪 ID (P1-2 真正持久化)
    competition_id TEXT NOT NULL,       -- 赛季编号，如 'SEASON_001'
    player_id TEXT NOT NULL,            -- 'AI_QUANT'
    symbol TEXT NOT NULL,               -- 标的代码 '000001'
    name TEXT NOT NULL,                 -- 标的名称
    signal_time TEXT NOT NULL,          -- 信号生成时间
    entry_price REAL NOT NULL,          -- 计划买入价格
    position_size REAL NOT NULL,        -- 建议仓位比例 (0~1)
    stop_loss REAL NOT NULL,            -- 止损价格
    stop_loss_pct REAL NOT NULL,        -- 止损幅度 (例如 -3.5%)
    take_profit REAL NOT NULL,          -- 止盈价格
    take_profit_pct REAL NOT NULL,      -- 止盈幅度 (例如 +6.8%)
    expected_holding_days INTEGER NOT NULL, -- 预计持有天数
    up_probability REAL,                -- 上涨概率
    expected_return REAL,               -- 预期收益
    expected_mae REAL,                  -- 预期不利波动
    right_side_score INTEGER,           -- 右侧确认分
    model_version TEXT NOT NULL,        -- 模型版本
    feature_version TEXT NOT NULL,      -- 特征版本
    label_version TEXT NOT NULL,        -- 标签版本
    decision_version TEXT NOT NULL,     -- 决策版本
    is_frozen INTEGER DEFAULT 1,        -- 是否已冻结 (1=是, 严禁事后修改)
    status TEXT DEFAULT 'ACTIVE',       -- 'ACTIVE' (持仓中), 'CLOSED' (已了结)
    close_time TEXT,                    -- 平仓时间
    close_price REAL,                   -- 平仓价格
    final_pnl_pct REAL                  -- 最终盈亏百分比
);
CREATE INDEX IF NOT EXISTS idx_plan_sym ON ai_quant_trade_plans(symbol);
CREATE INDEX IF NOT EXISTS idx_plan_season ON ai_quant_trade_plans(competition_id);
"""


def init_trade_plan_table(writer_id='db_init'):
    is_auth, msg = _GOVERNANCE_POLICY.validate_db_write(writer_id, 'ai_quant_trade_plans', 'CREATE')
    if not is_auth:
        _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
        raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

    with hdb._connect() as conn:
        conn.executescript(SCHEMA_TRADE_PLANS)
        # 平滑迁移：若 correlation_id 列尚不存在，安全追加该 nullable 列 (P1-2)
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(ai_quant_trade_plans)")
            cols = [r[1] for r in cur.fetchall()]
            if 'correlation_id' not in cols:
                is_m_auth, m_msg = _GOVERNANCE_POLICY.validate_db_write('schema_migration', 'ai_quant_trade_plans', 'ALTER')
                if not is_m_auth:
                    _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', m_msg)
                    raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {m_msg}")
                conn.execute("ALTER TABLE ai_quant_trade_plans ADD COLUMN correlation_id TEXT")
        except Exception as e:
            if isinstance(e, PermissionError):
                raise
            pass


class TradePlanEngine:
    def __init__(self, default_competition_id="SEASON_001", initialize_schema=True, persist_plans=True):
        if initialize_schema:
            init_trade_plan_table()
        self.persist_plans = persist_plans
        self.default_competition_id = default_competition_id

    def generate_trade_plan(self, decision_item, current_price, competition_id=None):
        """
        为 APPROVED BUY 的候选标的生成冻结交易计划合同。
        decision_item: dict (包含 symbol, name, up_probability, expected_return, expected_mae, right_side_score, expected_holding_days 等)
        current_price: float 实时基准价格
        """
        if decision_item.get('decision') != 'BUY':
            return None

        sym = decision_item['symbol']
        name = decision_item.get('name', sym)
        now = datetime.datetime.now()
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        date_tag = now.strftime('%Y%m%d_%H%M%S')
        plan_id = f"PLAN_{sym}_{date_tag}"
        comp_id = competition_id or self.default_competition_id
        correlation_id = decision_item.get('correlation_id') or GovernanceSafetyPolicy.make_correlation_id(prefix="TRACE", symbol=sym, trade_date=now.strftime('%Y%m%d'))

        entry_p = float(current_price)
        if entry_p <= 0:
            return None

        # 动态科学定锚止损点与止盈点：
        # 1. 止损点：依据预期 MAE (最大不利波动) 动态设定，下浮 MAE 的 1.15 倍安全垫，且风控底线刚性锁定在 -3.0% ~ -5.5% 之间
        mae_raw = abs(float(decision_item.get('predicted_mae', decision_item.get('expected_mae', -0.03))))
        stop_pct = -max(0.030, min(0.055, mae_raw * 1.15))
        stop_loss_price = round(entry_p * (1.0 + stop_pct), 2)

        # 2. 止盈点：依据预期收益设定，保持盈亏比 >= 1.5:1，止盈幅度锁定在 +5.5% ~ +12.0%
        exp_ret = float(decision_item.get('expected_return', 0.05))
        tp_pct = max(0.055, min(0.12, exp_ret * 1.6))
        take_profit_price = round(entry_p * (1.0 + tp_pct), 2)

        # 3. 仓位比例
        pos_size = float(decision_item.get('position_size_pct', 0.20))
        holding_days = int(decision_item.get('expected_holding_days', 2))

        pred_mae = float(decision_item.get('predicted_mae', decision_item.get('expected_mae', -0.03)))
        pred_mfe = float(decision_item.get('predicted_mfe', 0.055))
        rr_ratio = float(decision_item.get('risk_reward_ratio', decision_item.get('pl_ratio', 1.0)))

        plan = {
            'plan_id': plan_id,
            'correlation_id': correlation_id,
            'competition_id': comp_id,
            'player_id': 'AI_QUANT',
            'symbol': sym,
            'name': name,
            'signal_time': now_str,
            'entry_price': round(entry_p, 4),
            'position_size': round(pos_size, 4),
            'stop_loss': stop_loss_price,
            'stop_loss_pct': round(stop_pct * 100, 2),
            'take_profit': take_profit_price,
            'take_profit_pct': round(tp_pct * 100, 2),
            'expected_holding_days': holding_days,
            'up_probability': float(decision_item.get('up_probability', 0.5)),
            'expected_return': float(decision_item.get('expected_return', 0.0)),
            'predicted_mae': pred_mae,
            'predicted_mfe': pred_mfe,
            'risk_reward_ratio': rr_ratio,
            'expected_mae': pred_mae,  # 保持旧键名兼容数据库 schema
            'right_side_score': int(decision_item.get('right_side_score', 50)),
            'model_version': MODEL_VERSION,
            'feature_version': FEATURE_VERSION,
            'label_version': LABEL_VERSION,
            'decision_version': DECISION_VERSION,
            'is_frozen': 1,
            'status': 'ACTIVE'
        }

        # 4. 立即冻结持久化至 SQLite
        if self.persist_plans:
            self._save_plan_to_db(plan)
        return plan

    def _save_plan_to_db(self, plan):
        # 运行时写授权校验 (P1-1)
        is_auth, msg = _GOVERNANCE_POLICY.validate_db_write('trade_plan_generator', 'ai_quant_trade_plans', 'INSERT')
        if not is_auth:
            _GOVERNANCE_POLICY.trigger_halt('UNAUTHORIZED_DB_WRITE', msg)
            raise PermissionError(f"[DB_SECURITY_HALT] 阻断未授权写入: {msg}")

        query = """
            INSERT INTO ai_quant_trade_plans (
                plan_id, correlation_id, competition_id, player_id, symbol, name,
                signal_time, entry_price, position_size, stop_loss, stop_loss_pct,
                take_profit, take_profit_pct, expected_holding_days,
                up_probability, expected_return, expected_mae, right_side_score,
                model_version, feature_version, label_version, decision_version,
                is_frozen, status
            ) VALUES (
                :plan_id, :correlation_id, :competition_id, :player_id, :symbol, :name,
                :signal_time, :entry_price, :position_size, :stop_loss, :stop_loss_pct,
                :take_profit, :take_profit_pct, :expected_holding_days,
                :up_probability, :expected_return, :expected_mae, :right_side_score,
                :model_version, :feature_version, :label_version, :decision_version,
                :is_frozen, :status
            ) ON CONFLICT(plan_id) DO NOTHING;
        """
        with hdb._connect() as conn:
            conn.execute(query, plan)
        print(f"[trade_plan] 交易合同已生成并锁定: {plan['plan_id']} ({plan['name']} 止损:{plan['stop_loss']} 止盈:{plan['take_profit']})")

    def get_active_plan(self, symbol, competition_id=None):
        comp_id = competition_id or self.default_competition_id
        query = """
            SELECT * FROM ai_quant_trade_plans
            WHERE symbol = ? AND competition_id = ? AND status = 'ACTIVE'
            ORDER BY signal_time DESC LIMIT 1
        """
        with hdb._connect() as conn:
            cursor = conn.cursor()
            row = cursor.execute(query, (str(symbol).zfill(6), comp_id)).fetchone()
            if not row:
                return None
            cols = [col[0] for col in cursor.description]
            return dict(zip(cols, row))


if __name__ == '__main__':
    print("=== 测试 TradePlanEngine 交易合同生成与冻结 ===")
    engine = TradePlanEngine()
    mock_buy_decision = {
        'decision': 'BUY',
        'symbol': '000001',
        'name': '平安银行',
        'up_probability': 0.65,
        'expected_return': 0.058,
        'expected_mae': -0.026,
        'right_side_score': 82,
        'position_size_pct': 0.25,
        'expected_holding_days': 3
    }
    plan = engine.generate_trade_plan(mock_buy_decision, current_price=11.85)
    print("\n生成的冻结合同详情:")
    print(json.dumps(plan, ensure_ascii=False, indent=2))

    # 验证读取
    active = engine.get_active_plan('000001')
    print("\n从数据库读取的生效中合同:")
    print(f"  Plan ID: {active['plan_id']} | 标的: {active['name']} | 状态: {active['status']} | 冻结: {active['is_frozen']}")
