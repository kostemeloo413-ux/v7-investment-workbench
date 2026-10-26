# -*- coding: utf-8 -*-
"""
tests/v7_governance/test_v7_safe_strict_state_r2.py
V7-G0C-SAFE-STRICT-STATE-R2 关键状态严格读取、生命周期与原件保全专项测试套件

【覆盖范围】：
- R2-T01: account VALID + positions MISSING -> BLOCK；positions.json 不得自动创建；can_new_entry=False
- R2-T02: account MISSING + positions VALID -> BLOCK；不得恢复默认 100 万账户
- R2-T03: existing account + registry MISSING -> RECOVERY_REQUIRED / BLOCK
- R2-T04: governance_halt.json={} -> new_entry_disabled=True, execution_state_safe=False (Fail Closed)
- R2-T05: HALT 字段类型错误 (str 代替 bool / list) -> FAIL CLOSED
- R2-T06: registry={} -> registry_corrupted=True; state=RECOVERY_REQUIRED; begin_execution BLOCK
- R2-T07: registry={"records": []} / {"records": "abc"} -> SCHEMA_ERROR / BLOCK
- R2-T08: HALT={BROKEN; 初始化后 trigger_halt(...) -> 原文件 bytes 完全不变 (原件保全)
- R2-T09: 对实际存在的其它 HALT 写路径 (unsafe, safe, clear, save) 验证 -> 原文件 bytes 不变
- R2-T10: 全新隔离目录、全部关键文件 MISSING 且无经济历史 -> 明确 new-account initialization PASS
- R2-T11: account.json 关键数值字段类型非法 ("cash": "oops") -> SCHEMA_ERROR / BLOCK
- R2-T12: positions.json 元素类型非法 ([123], [{}], shares="many") -> SCHEMA_ERROR / BLOCK
- R2-T13: 存在成交流水经济历史但账户/持仓缺失 -> 拒绝作为全新账户初始化并刚性阻断

【隔离纪律】：
所有测试在独立临时沙箱目录中进行，严禁触碰或破坏真实生产数据。
"""

import os
import sys
import json
import shutil
import tempfile
import unittest

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.storage_utils import (
    strict_load_json, strict_read_jsonl, CriticalStateStatus, atomic_write_json
)
from ai_quant.governance_policy import GovernanceSafetyPolicy
from ai_quant.idempotency_registry import IdempotencyRegistry, ExecutionState
from ai_quant.paper_executor import PaperAccount, PaperExecutionEngine
from ai_quant.reconciliation import LedgerReconciliationEngine


class TestV7SafeStrictStateR2(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="strict_state_r2_")
        self.halt_path = os.path.join(self.test_dir, 'governance_halt.json')
        self.account_path = os.path.join(self.test_dir, 'account.json')
        self.positions_path = os.path.join(self.test_dir, 'positions.json')
        self.registry_path = os.path.join(self.test_dir, 'processed_idempotency_registry.json')
        self.fills_path = os.path.join(self.test_dir, 'fills.jsonl')
        self.orders_path = os.path.join(self.test_dir, 'orders.jsonl')
        self.trade_results_path = os.path.join(self.test_dir, 'paper_trade_results.jsonl')
        self.equity_curve_path = os.path.join(self.test_dir, 'equity_curve.jsonl')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_valid_account_fixture(self, cash=1000000.0, total_equity=1000000.0):
        d = {
            'account_id': 'TEST-ACC-01',
            'initial_capital': 1000000.0,
            'cash': cash,
            'market_value': total_equity - cash,
            'total_equity': total_equity,
            'net_pnl': total_equity - 1000000.0,
            'net_pnl_pct': (total_equity - 1000000.0) / 10000.0,
            'peak_equity': 1000000.0,
            'max_drawdown_pct': 0.0,
            'max_positions': 3,
            'max_single_position_ratio': 0.3,
            'max_equity_ratio': 0.9,
            'updated_at': '2026-09-16 10:00:00'
        }
        atomic_write_json(self.account_path, d)
        atomic_write_json(self.registry_path, {
            'schema_version': 'v7.2',
            'updated_at': '2026-09-16 10:00:00',
            'total_keys': 0,
            'records': {}
        })
        return d

    # -------------------------------------------------------------
    # R2-T01: account VALID + positions MISSING -> BLOCK, positions.json 不自动创建
    # -------------------------------------------------------------
    def test_r2_t01_account_valid_positions_missing_blocked(self):
        self._create_valid_account_fixture()
        if os.path.exists(self.positions_path):
            os.remove(self.positions_path)

        acc = PaperAccount(storage_dir=self.test_dir)
        self.assertTrue(acc.positions_corrupted)
        self.assertIn("CRITICAL_STATE_MISSING", acc.corrupt_reason)
        # 严禁自动创建 positions.json 并置 []
        self.assertFalse(os.path.exists(self.positions_path))

        # save 必须被刚性拒绝
        with self.assertRaises(RuntimeError):
            acc.save()

        # 对账必须 FAIL
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'FAIL')
        self.assertFalse(startup_res['can_new_entry'])

        daily_res = reconciler.run_daily_ledger_reconciliation()
        self.assertEqual(daily_res['status'], 'FAIL')
        self.assertFalse(daily_res['can_new_entry'])

        # 执行引擎必须阻断买入且零经济效果
        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        ok, status, msg = engine.execute_buy(
            decision_item={'symbol': '600519', 'name': '贵州茅台'},
            trade_plan={'plan_id': 'PLAN_1'},
            current_price=1500.0, yesterday_close=1490.0, current_trade_date='2026-09-16'
        )
        self.assertFalse(ok)
        self.assertEqual(status, 'RECONCILIATION_HALT')
        self.assertFalse(os.path.exists(self.positions_path))

    # -------------------------------------------------------------
    # R2-T02: account MISSING + positions VALID -> BLOCK, 不恢复默认 100 万本金
    # -------------------------------------------------------------
    def test_r2_t02_account_missing_positions_valid_blocked(self):
        if os.path.exists(self.account_path):
            os.remove(self.account_path)

        pos = [{
            'symbol': '600519',
            'name': '贵州茅台',
            'plan_id': 'PLAN_1',
            'shares': 100,
            'cost_price': 1500.0
        }]
        atomic_write_json(self.positions_path, pos)

        acc = PaperAccount(storage_dir=self.test_dir)
        self.assertTrue(acc.account_corrupted)
        self.assertIn("CRITICAL_STATE_MISSING", acc.corrupt_reason)
        # 不得自动生成 account.json
        self.assertFalse(os.path.exists(self.account_path))

        with self.assertRaises(RuntimeError):
            acc.save()

        # 对账必须 FAIL
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'FAIL')
        self.assertFalse(startup_res['can_new_entry'])

    # -------------------------------------------------------------
    # R2-T03: existing account + registry MISSING -> RECOVERY_REQUIRED / BLOCK
    # -------------------------------------------------------------
    def test_r2_t03_existing_account_registry_missing_blocked(self):
        # 建立已有合法账户与持仓快照，但故意不创建 registry
        self._create_valid_account_fixture()
        atomic_write_json(self.positions_path, [])
        if os.path.exists(self.registry_path):
            os.remove(self.registry_path)

        reg = IdempotencyRegistry(registry_path=self.registry_path)
        self.assertTrue(reg.registry_corrupted)
        self.assertIn("CRITICAL_STATE_MISSING", reg.corrupt_error)

        # 状态查询必须为 RECOVERY_REQUIRED
        self.assertEqual(reg.get_execution_state('PLAN_1', 'BUY', 'ACC_1'), ExecutionState.RECOVERY_REQUIRED)

        # begin_execution 和 save 必须抛出异常
        with self.assertRaises(RuntimeError):
            reg.begin_execution('PLAN_1', 'BUY', 'ACC_1')

        with self.assertRaises(RuntimeError):
            reg.save()

        # Startup 与 Daily 对账必须 FAIL
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'FAIL')
        self.assertFalse(startup_res['can_new_entry'])
        self.assertTrue(any('idempotency_registry' in f for f in startup_res['findings']))

        daily_res = reconciler.run_daily_ledger_reconciliation()
        self.assertEqual(daily_res['status'], 'FAIL')
        self.assertFalse(daily_res['can_new_entry'])

    # -------------------------------------------------------------
    # R2-T04: governance_halt.json={} -> Fail Closed
    # -------------------------------------------------------------
    def test_r2_t04_governance_halt_empty_dict_fail_closed(self):
        with open(self.halt_path, 'w', encoding='utf-8') as f:
            f.write("{}")

        policy = GovernanceSafetyPolicy(halt_path=self.halt_path)
        self.assertTrue(policy.is_new_entry_disabled())
        self.assertFalse(policy.is_execution_state_safe())
        self.assertTrue(policy.is_risk_exit_enabled())
        self.assertTrue(policy.halt_source_corrupted)

        # 原文件未被重写
        with open(self.halt_path, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), "{}")

    # -------------------------------------------------------------
    # R2-T05: HALT 字段类型错误 -> FAIL CLOSED
    # -------------------------------------------------------------
    def test_r2_t05_governance_halt_wrong_field_types_fail_closed(self):
        cases = [
            {'new_entry_disabled': "true", 'risk_exit_enabled': True, 'execution_state_safe': True, 'halt_reasons': []},
            {'new_entry_disabled': False, 'risk_exit_enabled': True, 'execution_state_safe': "yes", 'halt_reasons': []},
            {'new_entry_disabled': False, 'risk_exit_enabled': True, 'execution_state_safe': True, 'halt_reasons': "not_a_list"},
        ]
        for idx, payload in enumerate(cases):
            with open(self.halt_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f)

            policy = GovernanceSafetyPolicy(halt_path=self.halt_path)
            self.assertTrue(policy.is_new_entry_disabled(), f"case {idx} must disable new entry")
            self.assertFalse(policy.is_execution_state_safe(), f"case {idx} must be execution unsafe")
            self.assertTrue(policy.halt_source_corrupted, f"case {idx} must latch source corrupted")

    # -------------------------------------------------------------
    # R2-T06: registry={} -> registry_corrupted=True, state=RECOVERY_REQUIRED
    # -------------------------------------------------------------
    def test_r2_t06_registry_empty_dict_blocked(self):
        with open(self.registry_path, 'w', encoding='utf-8') as f:
            f.write("{}")

        reg = IdempotencyRegistry(registry_path=self.registry_path)
        self.assertTrue(reg.registry_corrupted)
        self.assertEqual(reg.get_execution_state('PLAN_1', 'BUY', 'ACC_1'), ExecutionState.RECOVERY_REQUIRED)

        with self.assertRaises(RuntimeError):
            reg.begin_execution('PLAN_1', 'BUY', 'ACC_1')

        with self.assertRaises(RuntimeError):
            reg.save()

    # -------------------------------------------------------------
    # R2-T07: registry 缺少合法 records (如 records 为 list 或 str)
    # -------------------------------------------------------------
    def test_r2_t07_registry_wrong_records_type_blocked(self):
        cases = ['{"records": []}', '{"records": "abc"}']
        for content in cases:
            with open(self.registry_path, 'w', encoding='utf-8') as f:
                f.write(content)

            reg = IdempotencyRegistry(registry_path=self.registry_path)
            self.assertTrue(reg.registry_corrupted)
            self.assertEqual(reg.get_execution_state('PLAN_1', 'BUY', 'ACC_1'), ExecutionState.RECOVERY_REQUIRED)

            with self.assertRaises(RuntimeError):
                reg.begin_execution('PLAN_1', 'BUY', 'ACC_1')

            with self.assertRaises(RuntimeError):
                reg.save()

    # -------------------------------------------------------------
    # R2-T08: HALT={BROKEN; 初始化后 trigger_halt(...) -> 原文件 bytes 完全不变
    # -------------------------------------------------------------
    def test_r2_t08_corrupt_halt_trigger_halt_preserves_bytes(self):
        corrupt_raw = b"{BROKEN_HALT_BYTES_XYZ_987654321"
        with open(self.halt_path, 'wb') as f:
            f.write(corrupt_raw)

        policy = GovernanceSafetyPolicy(halt_path=self.halt_path)
        self.assertTrue(policy.halt_source_corrupted)

        # 尝试触发新的熔断
        policy.trigger_halt("CRITICAL_SUBSEQUENT_FAILURE", "测试尝试写入熔断")

        # 原文件 bytes 必须毫厘未变
        with open(self.halt_path, 'rb') as f:
            bytes_after = f.read()
        self.assertEqual(bytes_after, corrupt_raw)

    # -------------------------------------------------------------
    # R2-T09: 对实际存在的其它 HALT 写路径验证 -> 原文件 bytes 不变
    # -------------------------------------------------------------
    def test_r2_t09_corrupt_halt_other_write_paths_preserve_bytes(self):
        corrupt_raw = b'{"bad_schema": 12345}'
        with open(self.halt_path, 'wb') as f:
            f.write(corrupt_raw)

        policy = GovernanceSafetyPolicy(halt_path=self.halt_path)
        self.assertTrue(policy.halt_source_corrupted)

        # 尝试全部其它修改接口
        policy.set_execution_state_unsafe("测试触发 unsafe")
        policy.set_execution_state_safe()
        policy.clear_halt_for_testing()
        policy.save_halt_state()

        # 原文件必须毫厘未变且未被删除
        self.assertTrue(os.path.exists(self.halt_path))
        with open(self.halt_path, 'rb') as f:
            bytes_after = f.read()
        self.assertEqual(bytes_after, corrupt_raw)

    # -------------------------------------------------------------
    # R2-T10: 全新隔离目录、全部关键文件 MISSING 且无经济历史 -> new-account PASS
    # -------------------------------------------------------------
    def test_r2_t10_new_account_clean_initialization_pass(self):
        # 确保无任何文件
        self.assertFalse(os.path.exists(self.account_path))
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertFalse(os.path.exists(self.registry_path))
        self.assertFalse(os.path.exists(self.fills_path))

        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        self.assertFalse(engine.account.account_corrupted)
        self.assertFalse(engine.account.positions_corrupted)
        self.assertFalse(engine.idempotency_registry.registry_corrupted)

        # 验证初始文件已生成并合法
        self.assertTrue(os.path.exists(self.account_path))
        self.assertTrue(os.path.exists(self.positions_path))
        self.assertEqual(engine.account.cash, 1000000.0)
        self.assertEqual(engine.account.positions, [])

        # 启动对账必须顺利通过
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'PASS')
        self.assertTrue(startup_res['can_new_entry'])

    # -------------------------------------------------------------
    # R2-T11: account.json 关键数值字段类型非法 ("cash": "oops") -> BLOCK
    # -------------------------------------------------------------
    def test_r2_t11_account_wrong_numeric_type_blocked(self):
        d = {
            'account_id': 'TEST-ACC-01',
            'initial_capital': 1000000.0,
            'cash': "oops",  # 非法字符串
            'market_value': 0.0,
            'total_equity': 1000000.0,
            'updated_at': '2026-09-16 10:00:00'
        }
        atomic_write_json(self.account_path, d)
        atomic_write_json(self.positions_path, [])
        atomic_write_json(self.registry_path, {'records': {}})

        acc = PaperAccount(storage_dir=self.test_dir)
        self.assertTrue(acc.account_corrupted)
        self.assertIn("类型错误", acc.corrupt_reason)

        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        ok, status, msg = engine.execute_buy(
            decision_item={'symbol': '600519', 'name': '贵州茅台'},
            trade_plan={'plan_id': 'PLAN_1'},
            current_price=1500.0, yesterday_close=1490.0, current_trade_date='2026-09-16'
        )
        self.assertFalse(ok)
        self.assertEqual(status, 'RECONCILIATION_HALT')

    # -------------------------------------------------------------
    # R2-T12: positions.json 元素类型非法 ([123], [{}], shares="many") -> BLOCK
    # -------------------------------------------------------------
    def test_r2_t12_positions_wrong_element_type_blocked(self):
        self._create_valid_account_fixture()

        bad_positions = [
            [123],
            [{}],
            [{'symbol': '600519', 'shares': 'many'}]
        ]
        for pos_data in bad_positions:
            atomic_write_json(self.positions_path, pos_data)

            acc = PaperAccount(storage_dir=self.test_dir)
            self.assertTrue(acc.positions_corrupted)

            reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
            startup_res = reconciler.run_startup_reconciliation()
            self.assertEqual(startup_res['status'], 'FAIL')

    # -------------------------------------------------------------
    # R2-T13: 存在交易历史但账户/持仓缺失 -> 拒绝作为新账户初始化
    # -------------------------------------------------------------
    def test_r2_t13_economic_history_with_missing_account_or_positions_blocked(self):
        # 写入成交流水历史，但无 account.json 和 positions.json
        with open(self.fills_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'fill_id': 'FILL_001', 'symbol': '600519', 'direction': 'BUY'}) + '\n')

        acc = PaperAccount(storage_dir=self.test_dir)
        self.assertTrue(acc.account_corrupted)
        self.assertTrue(acc.positions_corrupted)
        self.assertIn("检测到交易流水或订单历史存在", acc.corrupt_reason)
        # 严禁自动生成初始 account 和 positions
        self.assertFalse(os.path.exists(self.account_path))
        self.assertFalse(os.path.exists(self.positions_path))

    # -------------------------------------------------------------
    # FIX1-T14: account MISSING + positions MISSING + registry EXISTS -> BLOCK
    # -------------------------------------------------------------
    def test_fix1_t14_account_missing_positions_missing_registry_exists_blocked(self):
        # 确保 account.json 与 positions.json 缺失，且无经济流水
        self.assertFalse(os.path.exists(self.account_path))
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertFalse(os.path.exists(self.fills_path))
        self.assertFalse(os.path.exists(self.orders_path))
        self.assertFalse(os.path.exists(self.trade_results_path))

        # 写入已存在的合法注册表
        atomic_write_json(self.registry_path, {
            'schema_version': 'v7.2',
            'updated_at': '2026-09-16 10:00:00',
            'total_keys': 0,
            'records': {}
        })

        acc = PaperAccount(storage_dir=self.test_dir)
        self.assertTrue(acc.account_corrupted)
        self.assertTrue(acc.positions_corrupted)
        self.assertIn("CRITICAL_STATE_MISSING", acc.corrupt_reason)
        self.assertIn("幂等注册表已存在", acc.corrupt_reason)

        # 严禁自动创建 account.json 与 positions.json
        self.assertFalse(os.path.exists(self.account_path))
        self.assertFalse(os.path.exists(self.positions_path))

        # save 必须刚性抛出异常
        with self.assertRaises(RuntimeError):
            acc.save()

        # Startup 对账必须 FAIL，且 can_new_entry 为 False
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'FAIL')
        self.assertFalse(startup_res['can_new_entry'])

    # -------------------------------------------------------------
    # FIX1-T15: registry MISSING + positions EXISTS + account MISSING -> RECOVERY_REQUIRED / BLOCK
    # -------------------------------------------------------------
    def test_fix1_t15_registry_missing_positions_exists_blocked(self):
        # 确保 registry 与 account 缺失
        if os.path.exists(self.registry_path):
            os.remove(self.registry_path)
        if os.path.exists(self.account_path):
            os.remove(self.account_path)

        # 写入存在的 positions.json
        pos = [{
            'symbol': '600519',
            'name': '贵州茅台',
            'plan_id': 'PLAN_1',
            'shares': 100,
            'cost_price': 1500.0
        }]
        atomic_write_json(self.positions_path, pos)

        reg = IdempotencyRegistry(registry_path=self.registry_path)
        self.assertTrue(reg.registry_corrupted)
        self.assertIn("CRITICAL_STATE_MISSING", reg.corrupt_error)
        self.assertEqual(reg.get_execution_state('PLAN_1', 'BUY', 'ACC_1'), ExecutionState.RECOVERY_REQUIRED)

        with self.assertRaises(RuntimeError):
            reg.begin_execution('PLAN_1', 'BUY', 'ACC_1')

        with self.assertRaises(RuntimeError):
            reg.save()

    # -------------------------------------------------------------
    # FIX1-T16: 真正纯净全新环境 -> 允许初始化，全链路 PASS
    # -------------------------------------------------------------
    def test_fix1_t16_true_new_account_initialization_pass(self):
        # 确保三者皆空，无历史
        self.assertFalse(os.path.exists(self.account_path))
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertFalse(os.path.exists(self.registry_path))
        self.assertFalse(os.path.exists(self.fills_path))
        self.assertFalse(os.path.exists(self.orders_path))

        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        self.assertFalse(engine.account.account_corrupted)
        self.assertFalse(engine.account.positions_corrupted)
        self.assertFalse(engine.idempotency_registry.registry_corrupted)

        # 验证三个基础关键文件均已原子创建
        self.assertTrue(os.path.exists(self.account_path))
        self.assertTrue(os.path.exists(self.positions_path))
        self.assertTrue(os.path.exists(self.registry_path))

        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'PASS')
        self.assertTrue(startup_res['can_new_entry'])

    # -------------------------------------------------------------
    # FIX2-T17/T18: zero-byte lifecycle artifacts are surviving state
    # -------------------------------------------------------------
    def _assert_zero_byte_lifecycle_artifact_blocks_fresh_account(self, artifact_path):
        with open(artifact_path, 'wb'):
            pass

        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        self.assertTrue(engine.account.account_corrupted)
        self.assertTrue(engine.account.positions_corrupted)
        self.assertIn('CRITICAL_STATE_MISSING', engine.account.corrupt_reason)
        self.assertFalse(os.path.exists(self.account_path))
        self.assertFalse(os.path.exists(self.positions_path))
        self.assertTrue(engine.idempotency_registry.registry_corrupted)
        self.assertIn('CRITICAL_STATE_MISSING', engine.idempotency_registry.corrupt_error)
        self.assertEqual(
            engine.idempotency_registry.get_execution_state('PLAN_1', 'BUY', 'ACC_1'),
            ExecutionState.RECOVERY_REQUIRED
        )
        with self.assertRaises(RuntimeError):
            engine.idempotency_registry.begin_execution('PLAN_1', 'BUY', 'ACC_1')
        with self.assertRaises(RuntimeError):
            engine.idempotency_registry.save()

        startup_res = LedgerReconciliationEngine(storage_dir=self.test_dir).run_startup_reconciliation()
        self.assertNotEqual(startup_res['status'], 'PASS')
        self.assertFalse(startup_res['can_new_entry'])

        ok, status, _ = engine.execute_buy(
            decision_item={'symbol': '600519', 'name': '贵州茅台'},
            trade_plan={'plan_id': 'FIX2_ZERO_BYTE'},
            current_price=1500.0, yesterday_close=1490.0,
            current_trade_date='2026-09-16'
        )
        self.assertFalse(ok)
        self.assertEqual(status, 'RECONCILIATION_HALT')

    def test_fix2_t17_zero_byte_fills_surviving_state_blocked(self):
        self._assert_zero_byte_lifecycle_artifact_blocks_fresh_account(self.fills_path)

    def test_fix2_t18_zero_byte_equity_curve_surviving_state_blocks_execution(self):
        self._assert_zero_byte_lifecycle_artifact_blocks_fresh_account(self.equity_curve_path)
        reg = IdempotencyRegistry(registry_path=self.registry_path)
        self.assertTrue(reg.registry_corrupted)
        self.assertIn('CRITICAL_STATE_MISSING', reg.corrupt_error)
        self.assertEqual(
            reg.get_execution_state('PLAN_1', 'BUY', 'ACC_1'),
            ExecutionState.RECOVERY_REQUIRED
        )
        with self.assertRaises(RuntimeError):
            reg.begin_execution('PLAN_1', 'BUY', 'ACC_1')
        with self.assertRaises(RuntimeError):
            reg.save()

    def test_fix2_zero_byte_each_lifecycle_artifact_blocks_fresh_account(self):
        for artifact_name in (
            'fills.jsonl',
            'paper_trade_results.jsonl',
            'orders.jsonl',
            'equity_curve.jsonl',
        ):
            with self.subTest(artifact=artifact_name):
                for path in (
                    self.account_path,
                    self.positions_path,
                    self.registry_path,
                    self.fills_path,
                    self.orders_path,
                    self.trade_results_path,
                    self.equity_curve_path,
                ):
                    if os.path.exists(path):
                        os.remove(path)
                artifact_path = os.path.join(self.test_dir, artifact_name)
                self._assert_zero_byte_lifecycle_artifact_blocks_fresh_account(artifact_path)


if __name__ == '__main__':
    unittest.main()
