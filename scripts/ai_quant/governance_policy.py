# -*- coding: utf-8 -*-
"""
scripts/ai_quant/governance_policy.py
V7 治理与安全基础设施：授权写入策略与 HALT 熔断合同 (Authorized Writer & HALT Contract)

【最高工程与安全契约】：
1. 废弃简单哈希判定：正式废除 "market.db changed = CRITICAL" 的僵化教条；
2. 建立 Authorized DB Writer Policy：
   - 明确谁能写、写哪张表、什么时间窗口、哪种 SQL 操作；
   - 只有未授权写入才记录 Critical Event 或触发阻断；
3. 建立统一 HALT 合同：
   - 触发 Critical 违规时：NEW_ENTRY_DISABLED = TRUE, RISK_EXIT_ENABLED = TRUE；
   - 严禁因为 HALT 就禁止已有仓位止损止盈或禁止必要 Ledger 写入；
4. 建立最小化 Event Trace 骨架：
   - 生成并在各层透传全局一致的 correlation_id，统一 signal -> plan -> order -> fill 审计链。
"""

import os
import json
import datetime
import time
from ai_quant.storage_utils import (
    atomic_write_json, load_json_safe, strict_load_json, CriticalStateStatus,
    HALT_REQUIRED_FIELDS, HALT_FIELD_TYPES
)

DEFAULT_HALT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'state', 'governance_halt.json'
)

# 授权数据库写入者策略注册表
AUTHORIZED_DB_WRITERS = {
    'daily_k_updater': {
        'module': 'scripts.datasource.providers',
        'database': 'market.db',
        'tables': ['daily_kline', 'sync_meta'],
        'operations': ['INSERT', 'UPDATE'],
        'allowed_context': 'daily_review',
        'allowed_time_window': '16:00-18:00'
    },
    'kline_writer': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['daily_kline'],
        'operations': ['INSERT', 'UPDATE'],
        'allowed_context': 'upsert_klines',
        'allowed_time_window': 'ALL'
    },
    'news_persister': {
        'module': 'scripts.pipeline.persist',
        'database': 'market.db',
        'tables': ['news', 'event_factor'],
        'operations': ['INSERT'],
        'allowed_context': 'pipeline_persist',
        'allowed_time_window': 'ALL'
    },
    'event_factor_writer': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['event_factor'],
        'operations': ['INSERT', 'UPDATE'],
        'allowed_context': 'upsert_event_factor',
        'allowed_time_window': 'ALL'
    },
    'meta_syncer': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['sync_meta'],
        'operations': ['INSERT', 'UPDATE'],
        'allowed_context': 'set_meta',
        'allowed_time_window': 'ALL'
    },
    'event_calendar_persister': {
        'module': 'scripts.analysis.event_calendar',
        'database': 'market.db',
        'tables': ['event_calendar'],
        'operations': ['INSERT'],
        'allowed_context': 'persist_items',
        'allowed_time_window': 'ALL'
    },
    'signal_logger': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['signals'],
        'operations': ['INSERT'],
        'allowed_context': 'insert_signal',
        'allowed_time_window': 'ALL'
    },
    'ai_quant_ranker': {
        'module': 'scripts.ai_quant.ranking',
        'database': 'market.db',
        'tables': ['ai_quant_predictions'],
        'operations': ['INSERT'],
        'allowed_context': 'intraday_cycle',
        'allowed_time_window': '09:00-15:30'
    },
    'trade_plan_generator': {
        'module': 'scripts.ai_quant.trade_plan',
        'database': 'market.db',
        'tables': ['ai_quant_trade_plans'],
        'operations': ['INSERT'],
        'allowed_context': 'generate_trade_plan',
        'allowed_time_window': '09:00-15:30'
    },
    'instrument_syncer': {
        'module': 'scripts.ai_quant.universe',
        'database': 'market.db',
        'tables': ['instrument_master'],
        'operations': ['INSERT', 'UPDATE'],
        'allowed_context': 'sync_instrument_master',
        'allowed_time_window': 'MANUAL_MAINTENANCE'
    },
    'decision_trace_logger': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['decision_trace'],
        'operations': ['INSERT', 'UPDATE'],
        'allowed_context': 'decision_trace_management',
        'allowed_time_window': 'ALL'
    },
    'db_init': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['daily_kline', 'news', 'signals', 'sync_meta', 'event_factor', 'decision_trace', 'event_calendar', 'ai_quant_predictions', 'instrument_master', 'ai_quant_trade_plans'],
        'operations': ['CREATE', 'INDEX'],
        'allowed_context': 'db_init',
        'allowed_time_window': 'ALL'
    },
    'schema_migration': {
        'module': 'scripts.datasource.db',
        'database': 'market.db',
        'tables': ['event_factor', 'decision_trace', 'ai_quant_trade_plans'],
        'operations': ['ALTER'],
        'allowed_context': 'schema_migration',
        'allowed_time_window': 'ALL'
    }
}


class GovernanceSafetyPolicy:
    """
    【治理安全与熔断策略管理器】
    支持内存状态与磁盘 state/governance_halt.json 持久化协同。
    包含 execution_state_safe 门禁，确保在账本断裂或未恢复时刚性禁止任何新的经济行为。
    """
    def __init__(self, halt_path=DEFAULT_HALT_STATE_PATH, preserve_schema=False):
        self.halt_path = os.path.abspath(halt_path)
        self.preserve_schema = bool(preserve_schema)
        self._payload = {}
        self.new_entry_disabled = False
        self.risk_exit_enabled = True
        self.execution_state_safe = True
        self.halt_source_corrupted = False
        self.halt_corrupt_error = None
        self.halt_reasons = []
        self.load_halt_state()

    def load_halt_state(self):
        """
        从持久化文件加载 HALT 状态 (SAFE-STRICT-STATE-R2 强化):
        - 文件缺失: 视为未触发 HALT 的正常状态
        - 文件存在但损坏/0字节/非法结构/类型错误: 刚性 FAIL CLOSED (new_entry_disabled=True, execution_state_safe=False)
        - 锁存 halt_source_corrupted = True，坚决不覆盖磁盘原文件，保留现场审计证据
        """
        res = strict_load_json(
            self.halt_path, expected_type=dict,
            required_fields=HALT_REQUIRED_FIELDS,
            field_types=HALT_FIELD_TYPES
        )
        if res.is_missing:
            self._payload = {}
            self.halt_source_corrupted = False
            self.halt_corrupt_error = None
            return

        if res.is_corrupt:
            self.halt_source_corrupted = True
            self.halt_corrupt_error = res.error
            self.new_entry_disabled = True
            self.risk_exit_enabled = True
            self.execution_state_safe = False
            self.halt_reasons.append({
                'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'condition': 'CORRUPT_HALT_FILE',
                'reason': f"HALT状态文件存在但损坏或格式非法: {res.error}"
            })
            return

        data = res.value
        self._payload = dict(data)
        self.halt_source_corrupted = False
        self.halt_corrupt_error = None
        self.new_entry_disabled = data.get('new_entry_disabled', False)
        self.risk_exit_enabled = True # 始终保证 True
        self.execution_state_safe = data.get('execution_state_safe', True)
        self.halt_reasons = data.get('halt_reasons', [])

    def save_halt_state(self):
        """原子持久化 HALT 状态 (损坏原件保全: 损坏时严禁写盘覆盖)"""
        if self.halt_source_corrupted:
            return
        payload = dict(self._payload) if self.preserve_schema else {}
        payload.update({
            'schema_version': 'v7.2',
            'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'new_entry_disabled': self.new_entry_disabled,
            'risk_exit_enabled': self.risk_exit_enabled,
            'execution_state_safe': self.execution_state_safe,
            'total_halt_events': len(self.halt_reasons),
            'halt_reasons': self.halt_reasons
        })
        self._payload = payload
        atomic_write_json(self.halt_path, payload)

    def is_new_entry_disabled(self):
        """查询是否阻断新开仓 (优先读取持久化与内存)"""
        if os.path.exists(self.halt_path):
            self.load_halt_state()
        return self.new_entry_disabled or (not self.execution_state_safe) or self.halt_source_corrupted

    def is_risk_exit_enabled(self):
        """查询是否允许风控出场 (必须始终保证为 True)"""
        return self.risk_exit_enabled

    def is_execution_state_safe(self):
        """查询账本与执行状态是否安全"""
        if os.path.exists(self.halt_path):
            self.load_halt_state()
        return self.execution_state_safe and (not self.halt_source_corrupted)

    def set_execution_state_unsafe(self, reason):
        """当执行事务断裂或需要恢复时，标记状态不安全并落盘"""
        self.execution_state_safe = False
        self.trigger_halt('EXECUTION_STATE_UNSAFE', reason)

    def set_execution_state_safe(self):
        """当执行恢复完全闭环后，恢复安全状态 (损坏原件锁存为 True 时拒绝置安全或写盘)"""
        if self.halt_source_corrupted:
            return
        self.execution_state_safe = True
        self.save_halt_state()

    def validate_db_write(self, writer_id, table_name, operation):
        """
        校验向 market.db 的写操作是否合法授权：
        返回 (is_authorized: bool, msg: str)
        """
        if writer_id not in AUTHORIZED_DB_WRITERS:
            return False, f"UNAUTHORIZED_WRITER: 未知写入者 [{writer_id}] 试图写入表 [{table_name}]"

        writer_cfg = AUTHORIZED_DB_WRITERS[writer_id]
        if table_name not in writer_cfg['tables']:
            return False, f"UNAUTHORIZED_TABLE: 写入者 [{writer_id}] 无权写入表 [{table_name}]"

        if operation.upper() not in writer_cfg['operations']:
            return False, f"UNAUTHORIZED_OP: 写入者 [{writer_id}] 无权执行 [{operation}] 操作"

        return True, "AUTHORIZED"

    def trigger_halt(self, critical_condition, reason):
        """
        触发最高级紧急熔断并持久化落盘：
        NEW_ENTRY_DISABLED = TRUE
        RISK_EXIT_ENABLED = TRUE (必须始终保持为 True，允许出场)
        """
        self.new_entry_disabled = True
        self.risk_exit_enabled = True  # 刚性保证
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        halt_entry = {
            'timestamp': now_str,
            'condition': critical_condition,
            'reason': reason
        }
        self.halt_reasons.append(halt_entry)
        if not self.halt_source_corrupted:
            self.save_halt_state()
        return halt_entry

    def clear_halt_for_testing(self):
        if self.halt_source_corrupted:
            # 损坏原件保全：禁止清除或删除损坏文件，保留原始证据
            return
        self.new_entry_disabled = False
        self.risk_exit_enabled = True
        self.execution_state_safe = True
        self.halt_reasons = []
        if os.path.exists(self.halt_path):
            try:
                os.remove(self.halt_path)
            except OSError:
                pass

    @staticmethod
    def make_correlation_id(prefix="TRACE", symbol=None, trade_date=None):
        """生成全局一致的链路追踪 ID"""
        ts = int(time.time() * 1000)
        s_part = f"_{symbol}" if symbol else ""
        d_part = f"_{trade_date}" if trade_date else ""
        return f"{prefix}{d_part}{s_part}_{ts}"
