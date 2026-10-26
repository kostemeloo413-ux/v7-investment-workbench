# -*- coding: utf-8 -*-
"""
scripts/ai_quant/idempotency_registry.py
V7 治理与安全基础设施：执行事务日志与崩溃恢复注册表 (Execution Transaction Journal & Crash-Safe Recovery Registry)

【最高工程与安全契约】：
1. 避免崩溃重启导致的同一信号/同一合同重复买入或重复平仓；
2. 权威唯一键：idempotency_key = f"{tradeplan_id}:{action}:{account_id}"；
3. 严格 Execution State Machine 流转：
   - NONE: 未执行；
   - PREPARED (或 PENDING): 准备执行，记录快照事实，禁止重入与再次撮合；
   - EFFECTS_WRITTEN: 账户/持仓等关键快照已成功持久化落盘；
   - COMMITTED: 事务完全提交并闭环终态；
   - RECOVERY_REQUIRED: 崩溃恢复检测到状态断裂，挂起并等待人工审计。
4. 结构化 Execution Journal：完整保存 idempotency_key, tradeplan_id, account_id, symbol,
   action, qty, price, gross_amount, fees, net_amount, pre_cash, expected_post_cash,
   pre_position_qty, expected_post_position_qty, correlation_id, state 等。
5. 明确拒绝把 PREPARED/PENDING 当成 NONE。
"""

import os
import json
import datetime
from ai_quant.storage_utils import (
    atomic_write_json, load_json_safe, strict_load_json, CriticalStateStatus,
    REGISTRY_REQUIRED_FIELDS, REGISTRY_FIELD_TYPES
)

DEFAULT_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'ai_quant_research', 'paper_simulation', 'processed_idempotency_registry.json'
)


class ExecutionState:
    NONE = 'NONE'
    PREPARED = 'PREPARED'
    PENDING = 'PENDING'  # 兼容同义
    EFFECTS_WRITTEN = 'EFFECTS_WRITTEN'
    COMMITTED = 'COMMITTED'
    RECOVERY_REQUIRED = 'RECOVERY_REQUIRED'


class IdempotencyRegistry:
    """
    【崩溃安全执行事务注册表】
    记录所有执行中、已落盘、已提交的完整执行事务事实。
    """
    def __init__(self, registry_path=DEFAULT_REGISTRY_PATH):
        self.registry_path = os.path.abspath(registry_path)
        self.records = {}
        self.metadata = {}
        self.registry_corrupted = False
        self.corrupt_error = None
        self.load()

    def load(self):
        """严格加载幂等注册表 (SAFE-STRICT-STATE-R2 强化)"""
        res = strict_load_json(
            self.registry_path, expected_type=dict,
            required_fields=REGISTRY_REQUIRED_FIELDS,
            field_types=REGISTRY_FIELD_TYPES
        )
        if res.is_missing:
            # 检查同级目录下是否存在已有账户、持仓或已有经济历史
            parent_dir = os.path.dirname(self.registry_path)
            acc_path = os.path.join(parent_dir, 'account.json')
            pos_path = os.path.join(parent_dir, 'positions.json')
            fills_path = os.path.join(parent_dir, 'fills.jsonl')
            trades_path = os.path.join(parent_dir, 'paper_trade_results.jsonl')
            orders_path = os.path.join(parent_dir, 'orders.jsonl')
            equity_path = os.path.join(parent_dir, 'equity_curve.jsonl')

            existing_account = os.path.exists(acc_path)
            existing_positions = os.path.exists(pos_path)
            # Physical existence is surviving lifecycle evidence, even when a
            # JSONL file is zero bytes after a legitimate atomic rewrite/cleanup.
            has_history = any(
                os.path.exists(p)
                for p in (fills_path, trades_path, orders_path, equity_path)
            )

            if existing_account or existing_positions or has_history:
                self.registry_corrupted = True
                self.corrupt_error = "已有账户、持仓或交易历史存在，但幂等注册表缺失 (CRITICAL_STATE_MISSING)，拒绝作为空注册表初始化"
                self.records = {}
                return

            self.records = {}
            self.metadata = {}
            self.registry_corrupted = False
            self.corrupt_error = None
            return

        if res.is_corrupt:
            self.registry_corrupted = True
            self.corrupt_error = f"幂等注册表文件存在但损坏或格式非法: {res.error}"
            self.records = {}
            return

        self.records = res.value['records']
        self.metadata = {k: res.value[k] for k in ('season_id', 'participant_id', 'account_id', 'created_at') if k in res.value}
        self.registry_corrupted = False
        self.corrupt_error = None

    def save(self):
        if self.registry_corrupted:
            raise RuntimeError(f"[FAIL_CLOSED_LOCK] 禁止覆盖损坏的幂等注册表！原因: {self.corrupt_error}")
        payload = {
            'schema_version': 'v7.2',
            'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_keys': len(self.records),
            'records': self.records
        }
        for key in ('season_id', 'participant_id', 'account_id', 'created_at'):
            if key in self.metadata:
                payload[key] = self.metadata[key]
        atomic_write_json(self.registry_path, payload)

    @staticmethod
    def make_key(tradeplan_id, action, account_id):
        """生成确定性幂等键：禁止随机数"""
        return f"{str(tradeplan_id).strip()}:{str(action).upper().strip()}:{str(account_id).strip()}"

    def get_execution_state(self, tradeplan_id, action, account_id):
        """获取当前执行事务状态：NONE, PREPARED/PENDING, EFFECTS_WRITTEN, COMMITTED, RECOVERY_REQUIRED"""
        if self.registry_corrupted:
            # 注册表损坏时状态不可信，刚性返回 RECOVERY_REQUIRED 阻断重入
            return ExecutionState.RECOVERY_REQUIRED
        key = self.make_key(tradeplan_id, action, account_id)
        rec = self.records.get(key)
        if not rec:
            return ExecutionState.NONE
        st = rec.get('status') or rec.get('state') or ExecutionState.NONE
        if st == ExecutionState.PENDING:
            return ExecutionState.PREPARED
        return st

    def has_processed(self, tradeplan_id, action, account_id):
        """检查该合同动作是否已处于 COMMITTED 终态 (兼容既有 API)"""
        if self.registry_corrupted:
            # 注册表损坏时禁止误判为未执行
            return True
        st = self.get_execution_state(tradeplan_id, action, account_id)
        return st == ExecutionState.COMMITTED

    def get_record(self, tradeplan_id, action, account_id):
        """获取特定幂等事务记录"""
        key = self.make_key(tradeplan_id, action, account_id)
        return self.records.get(key)

    def begin_execution(self, tradeplan_id, action, account_id, symbol=None,
                        qty=0, price=0.0, gross_amount=0.0, fees=0.0, net_amount=0.0,
                        pre_cash=None, expected_post_cash=None,
                        pre_position_qty=None, expected_post_position_qty=None,
                        correlation_id=None, extra_meta=None, transaction_id=None,
                        order_id=None, decision_id=None, season_id=None, participant_id=None):
        """
        第一阶段：在经济效果发生前记录 PREPARED 事务日志，保存完整事实
        """
        if self.registry_corrupted:
            raise RuntimeError(f"[IDEMPOTENCY_CORRUPT] 注册表损坏拒绝开启新事务: {self.corrupt_error}")
        key = self.make_key(tradeplan_id, action, account_id)
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rec = {
            'idempotency_key': key,
            'tradeplan_id': str(tradeplan_id),
            'action': str(action).upper(),
            'account_id': str(account_id),
            'symbol': symbol,
            'qty': int(qty),
            'price': float(price),
            'gross_amount': float(gross_amount),
            'fees': float(fees),
            'net_amount': float(net_amount),
            'pre_cash': float(pre_cash) if pre_cash is not None else None,
            'expected_post_cash': float(expected_post_cash) if expected_post_cash is not None else None,
            'pre_position_qty': int(pre_position_qty) if pre_position_qty is not None else None,
            'expected_post_position_qty': int(expected_post_position_qty) if expected_post_position_qty is not None else None,
            'correlation_id': correlation_id or str(tradeplan_id),
            'transaction_id': transaction_id or f'TX-{str(tradeplan_id)}-{str(action).upper()}-{datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")}',
            **({'order_id': order_id} if order_id else {}),
            **({'decision_id': decision_id} if decision_id else {}),
            **({'season_id': season_id} if season_id else {}),
            **({'participant_id': participant_id} if participant_id else {}),
            'status': ExecutionState.PREPARED,
            'state': ExecutionState.PREPARED,
            'created_at': now_str,
            'updated_at': now_str,
            'meta': extra_meta or {}
        }
        self.records[key] = rec
        self.save()
        return rec

    def mark_pending(self, tradeplan_id, action, account_id, symbol=None, extra_meta=None):
        """兼容旧接口：标记 PREPARED/PENDING"""
        meta = extra_meta or {}
        return self.begin_execution(
            tradeplan_id=tradeplan_id, action=action, account_id=account_id,
            symbol=symbol, extra_meta=meta
        )

    def mark_effects_written(self, tradeplan_id, action, account_id, extra_meta=None):
        """
        阶段：底层账户与持仓快照已成功原子落盘，标记 EFFECTS_WRITTEN
        """
        key = self.make_key(tradeplan_id, action, account_id)
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        existing = self.records.get(key, {})
        existing['status'] = ExecutionState.EFFECTS_WRITTEN
        existing['state'] = ExecutionState.EFFECTS_WRITTEN
        existing['updated_at'] = now_str
        if extra_meta:
            existing.setdefault('meta', {}).update(extra_meta)
        self.records[key] = existing
        self.save()
        return existing

    def commit_execution(self, tradeplan_id, action, account_id, fill_id=None, symbol=None,
                         extra_meta=None, order_id=None, transaction_id=None):
        """
        最终阶段：确认事务提交闭环，标记 COMMITTED
        """
        key = self.make_key(tradeplan_id, action, account_id)
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        existing = self.records.get(key, {})
        existing['idempotency_key'] = key
        existing['tradeplan_id'] = str(tradeplan_id)
        existing['action'] = str(action).upper()
        existing['account_id'] = str(account_id)
        if fill_id:
            existing['fill_id'] = fill_id
        if order_id:
            existing['order_id'] = order_id
        if transaction_id:
            existing['transaction_id'] = transaction_id
        if symbol:
            existing['symbol'] = symbol
        existing['status'] = ExecutionState.COMMITTED
        existing['state'] = ExecutionState.COMMITTED
        existing['committed_at'] = now_str
        existing['updated_at'] = now_str
        if extra_meta:
            existing.setdefault('meta', {}).update(extra_meta)
        self.records[key] = existing
        self.save()
        return existing

    def mark_committed(self, tradeplan_id, action, account_id, fill_id=None, symbol=None, extra_meta=None):
        """兼容旧接口"""
        return self.commit_execution(tradeplan_id, action, account_id, fill_id=fill_id, symbol=symbol, extra_meta=extra_meta)

    def mark_recovery_required(self, tradeplan_id, action, account_id, reason="状态不一致需人工介入"):
        """标记需要人工恢复"""
        key = self.make_key(tradeplan_id, action, account_id)
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        existing = self.records.get(key, {})
        existing['status'] = ExecutionState.RECOVERY_REQUIRED
        existing['state'] = ExecutionState.RECOVERY_REQUIRED
        existing['recovery_reason'] = reason
        existing['updated_at'] = now_str
        self.records[key] = existing
        self.save()
        return existing

    def rollback_to_none(self, tradeplan_id, action, account_id):
        """确定未发生任何经济效果时，安全回滚注册表至 NONE"""
        key = self.make_key(tradeplan_id, action, account_id)
        if key in self.records:
            del self.records[key]
            self.save()

    def record_processed(self, tradeplan_id, action, account_id, fill_id=None, symbol=None, extra_meta=None):
        """兼容旧接口：直接提交 COMMITTED"""
        return self.commit_execution(tradeplan_id, action, account_id, fill_id=fill_id, symbol=symbol, extra_meta=extra_meta)

    def get_incomplete_executions(self):
        """获取所有未完全 COMMITTED 的记录 (PREPARED, EFFECTS_WRITTEN, RECOVERY_REQUIRED)"""
        incomplete = []
        for key, rec in self.records.items():
            st = rec.get('status') or rec.get('state')
            if st in (ExecutionState.PREPARED, ExecutionState.PENDING,
                      ExecutionState.EFFECTS_WRITTEN, ExecutionState.RECOVERY_REQUIRED):
                incomplete.append(rec)
        return incomplete

    def clear_for_testing(self):
        self.records = {}
        if os.path.exists(self.registry_path):
            try:
                os.remove(self.registry_path)
            except OSError:
                pass
