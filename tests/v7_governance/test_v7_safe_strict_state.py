# -*- coding: utf-8 -*-
"""
tests/v7_governance/test_v7_safe_strict_state.py
V7-G0C-SAFE-STRICT-STATE-R1 关键状态严格读取与 Fail-Closed 定向修复专项测试套件

【测试范围】：
- T01: 正常合法 HALT 读取与行为保持
- T02: 损坏 HALT 刚性 Fail-Closed (new_entry_disabled=True, execution_state_safe=False, 原文件不被覆盖)
- T03: 合法空 positions ([]) 正常通过 Startup 对账 (证明合法空仓 != 损坏空仓)
- T04: 损坏 positions 刚性拦截 Startup 对账 (status != PASS, can_new_entry != True)
- T05: 损坏 positions 刚性拦截 Daily 对账 (status != PASS)
- T06: 损坏 account 刚性拦截 Daily 对账 (不得出现 cash=0, equity=0 后假 PASS)
- T07: 损坏 registry 无法被当成空注册表，且刚性阻断新经济事务
- T08: fills.jsonl 坏行刚性拦截 Daily 对账
- T09: paper_trade_results.jsonl 坏行刚性拦截 Daily 对账
- T10: Critical State 损坏后 BUY 产生 0 经济效果 (cash不变, 无持仓增加, 无fill)
- T11: HALT 损坏后重启依然刚性禁止 new entry (绝不因重启恢复默认放行)
- T12: 正常合规全状态回归通过 (不误伤正常系统)

【隔离纪律】：
所有故障注入均在 isolated temp directory 中进行，零触碰生产数据。
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


class TestV7SafeStrictState(unittest.TestCase):

    def setUp(self):
        # 建立严格隔离的临时沙箱环境
        self.test_dir = tempfile.mkdtemp(prefix="strict_state_test_")
        self.halt_path = os.path.join(self.test_dir, 'governance_halt.json')
        self.account_path = os.path.join(self.test_dir, 'account.json')
        self.positions_path = os.path.join(self.test_dir, 'positions.json')
        self.registry_path = os.path.join(self.test_dir, 'processed_idempotency_registry.json')
        self.fills_path = os.path.join(self.test_dir, 'fills.jsonl')
        self.trade_results_path = os.path.join(self.test_dir, 'paper_trade_results.jsonl')

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
    # T01: 正常 HALT 读取与行为保持
    # -------------------------------------------------------------
    def test_t01_normal_halt(self):
        halt_data = {
            'schema_version': 'v7.2',
            'updated_at': '2026-09-16 10:00:00',
            'new_entry_disabled': True,
            'risk_exit_enabled': True,
            'execution_state_safe': True,
            'total_halt_events': 1,
            'halt_reasons': [{'timestamp': '2026-09-16 10:00:00', 'condition': 'TEST_HALT', 'reason': '测试熔断'}]
        }
        atomic_write_json(self.halt_path, halt_data)

        policy = GovernanceSafetyPolicy(halt_path=self.halt_path)
        self.assertTrue(policy.is_new_entry_disabled())
        self.assertTrue(policy.is_risk_exit_enabled())
        self.assertTrue(policy.is_execution_state_safe())
        self.assertEqual(len(policy.halt_reasons), 1)

    # -------------------------------------------------------------
    # T02: 损坏 HALT 刚性 Fail-Closed 且不覆盖损坏文件
    # -------------------------------------------------------------
    def test_t02_corrupt_halt_fail_closed(self):
        corrupt_content = "{BROKEN_SYNTAX_JSON_CORRUPTED"
        with open(self.halt_path, 'w', encoding='utf-8') as f:
            f.write(corrupt_content)

        policy = GovernanceSafetyPolicy(halt_path=self.halt_path)
        # 必须刚性 fail closed
        self.assertTrue(policy.is_new_entry_disabled())
        self.assertTrue(policy.is_risk_exit_enabled())
        self.assertFalse(policy.is_execution_state_safe())

        # 验证原文件未被覆盖重写 (保存原始现场证据)
        with open(self.halt_path, 'r', encoding='utf-8') as f:
            content_after = f.read()
        self.assertEqual(content_after, corrupt_content)

    # -------------------------------------------------------------
    # T03: 合法空 positions ([]) 正常通过 Startup 对账
    # -------------------------------------------------------------
    def test_t03_valid_empty_positions(self):
        self._create_valid_account_fixture()
        # 合法空列表
        atomic_write_json(self.positions_path, [])
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = reconciler.run_startup_reconciliation()

        self.assertEqual(res['status'], 'PASS')
        self.assertTrue(res['can_new_entry'])
        self.assertTrue(res['can_risk_exit'])
        self.assertEqual(len(res['findings']), 0)

    # -------------------------------------------------------------
    # T04: 损坏 positions 刚性拦截 Startup 对账
    # -------------------------------------------------------------
    def test_t04_corrupt_positions_startup_blocked(self):
        self._create_valid_account_fixture()
        # 写入损坏语法
        with open(self.positions_path, 'w', encoding='utf-8') as f:
            f.write("[{'corrupted_quote': 123, invalid_json")

        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = reconciler.run_startup_reconciliation()

        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        self.assertTrue(any('positions.json 损坏' in f for f in res['findings']))

    # -------------------------------------------------------------
    # T05: 损坏 positions 刚性拦截 Daily 对账
    # -------------------------------------------------------------
    def test_t05_corrupt_positions_daily_blocked(self):
        self._create_valid_account_fixture()
        # 写入非法类型（例如本应是 list，却是个 dict 或非 JSON 内容）
        with open(self.positions_path, 'w', encoding='utf-8') as f:
            f.write('{"type": "not_a_list"}')

        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = reconciler.run_daily_ledger_reconciliation()

        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        self.assertTrue(any('positions.json 损坏或格式非法' in f for f in res['findings']))

    # -------------------------------------------------------------
    # T06: 损坏 account 刚性拦截 Daily 对账 (拒绝假 PASS)
    # -------------------------------------------------------------
    def test_t06_corrupt_account_daily_blocked(self):
        # 损坏 account.json
        with open(self.account_path, 'w', encoding='utf-8') as f:
            f.write("{BROKEN_ACCOUNT_DATA_CRASH")
        atomic_write_json(self.positions_path, [])
        atomic_write_json(self.registry_path, {
            'schema_version': 'v7.2',
            'updated_at': '2026-09-16 10:00:00',
            'total_keys': 0,
            'records': {}
        })

        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = reconciler.run_daily_ledger_reconciliation()

        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        self.assertTrue(any('account.json 损坏或格式非法' in f for f in res['findings']))

    # -------------------------------------------------------------
    # T07: 损坏 registry 无法被当成空注册表，且刚性阻断新经济事务
    # -------------------------------------------------------------
    def test_t07_corrupt_registry_blocked(self):
        with open(self.registry_path, 'w', encoding='utf-8') as f:
            f.write("{MALFORMED_REGISTRY_JSON_CORRUPT")

        reg = IdempotencyRegistry(registry_path=self.registry_path)
        self.assertTrue(reg.registry_corrupted)

        # 必须认为处于未决安全状态，不能返回 False 误以为未处理
        self.assertTrue(reg.has_processed('PLAN_1', 'BUY', 'ACC_1'))
        self.assertEqual(reg.get_execution_state('PLAN_1', 'BUY', 'ACC_1'), ExecutionState.RECOVERY_REQUIRED)

        # 尝试开启新事务时刚性抛出异常拒绝
        with self.assertRaises(RuntimeError) as ctx:
            reg.begin_execution('PLAN_1', 'BUY', 'ACC_1')
        self.assertIn("注册表损坏拒绝开启新事务", str(ctx.exception))

        # 尝试 save() 时刚性抛出异常拒绝覆盖
        with self.assertRaises(RuntimeError) as ctx:
            reg.save()
        self.assertIn("禁止覆盖损坏的幂等注册表", str(ctx.exception))

    # -------------------------------------------------------------
    # T08: fills.jsonl 坏行刚性拦截 Daily 对账
    # -------------------------------------------------------------
    def test_t08_malformed_fills_jsonl_daily_blocked(self):
        self._create_valid_account_fixture()
        atomic_write_json(self.positions_path, [])

        # 注入格式损坏行到 fills.jsonl
        with open(self.fills_path, 'w', encoding='utf-8') as f:
            f.write('{"fill_id": "FILL_OK", "direction": "BUY"}\nBAD_CORRUPTED_LINE\n')

        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = reconciler.run_daily_ledger_reconciliation()

        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        self.assertTrue(any('fills.jsonl 检测到 1 行格式损坏' in f for f in res['findings']))

    # -------------------------------------------------------------
    # T09: paper_trade_results.jsonl 坏行刚性拦截 Daily 对账
    # -------------------------------------------------------------
    def test_t09_malformed_trade_results_daily_blocked(self):
        self._create_valid_account_fixture()
        atomic_write_json(self.positions_path, [])

        # 注入格式损坏行到 paper_trade_results.jsonl
        with open(self.trade_results_path, 'w', encoding='utf-8') as f:
            f.write('{"plan_id": "PLAN_1", "stock_code": "600519"}\nTRUNCATED_JSON_LINE_{"key":\n')

        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = reconciler.run_daily_ledger_reconciliation()

        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        self.assertTrue(any('paper_trade_results.jsonl 检测到 1 行格式损坏' in f for f in res['findings']))

    # -------------------------------------------------------------
    # T10: Critical State 损坏后 BUY 产生 0 经济效果
    # -------------------------------------------------------------
    def test_t10_critical_state_corrupt_buy_produces_zero_economic_effect(self):
        # 1. 建立具有 100 万现金的合法账户快照
        self._create_valid_account_fixture(cash=1000000.0, total_equity=1000000.0)

        # 2. 注入损坏的 positions.json
        with open(self.positions_path, 'w', encoding='utf-8') as f:
            f.write("{BROKEN_POSITIONS_CRITICAL_CORRUPT")

        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        self.assertTrue(engine.account.positions_corrupted)

        # 3. 准备买入决策
        mock_decision = {'symbol': '600519', 'name': '贵州茅台', 'position_size_pct': 0.2}
        mock_plan = {'plan_id': 'PLAN_600519_TEST', 'stop_loss': 1400.0, 'take_profit': 1600.0}

        # 4. 执行买入尝试
        ok, status, msg = engine.execute_buy(
            decision_item=mock_decision,
            trade_plan=mock_plan,
            current_price=1500.0,
            yesterday_close=1490.0,
            current_trade_date='2026-09-16'
        )

        # 5. 断言必须被刚性拒绝
        self.assertFalse(ok)
        self.assertEqual(status, 'RECONCILIATION_HALT')
        self.assertIn("损坏", msg)

        # 6. 断言产生 0 经济效果：
        # (a) 现金毫厘未扣
        self.assertEqual(engine.account.cash, 1000000.0)
        # (b) 持仓零增长
        self.assertEqual(len(engine.account.positions), 0)
        # (c) fills.jsonl 文件绝对不存在 (未产生任何成交流水)
        self.assertFalse(os.path.exists(engine.fills_path))
        # (d) 损坏的 positions.json 绝未被覆盖自毁
        with open(self.positions_path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertEqual(content, "{BROKEN_POSITIONS_CRITICAL_CORRUPT")

    # -------------------------------------------------------------
    # T11: HALT 损坏后重启依然刚性禁止 new entry
    # -------------------------------------------------------------
    def test_t11_corrupt_halt_restart_persists_disabled(self):
        # 模拟运行中 HALT 文件损坏
        with open(self.halt_path, 'w', encoding='utf-8') as f:
            f.write("{GARBAGE_HALT_DATA_CORRUPT_BYTES")

        # 实例 1 启动
        policy1 = GovernanceSafetyPolicy(halt_path=self.halt_path)
        self.assertTrue(policy1.is_new_entry_disabled())

        # 模拟系统重启，创建全新独立实例 2
        del policy1
        policy2 = GovernanceSafetyPolicy(halt_path=self.halt_path)

        # 刚性断言：重启后由于底层文件仍处于损坏未恢复状态，依然禁止新开仓
        self.assertTrue(policy2.is_new_entry_disabled())
        self.assertFalse(policy2.is_execution_state_safe())

    # -------------------------------------------------------------
    # T12: 正常合规全状态回归通过 (不误伤合规系统)
    # -------------------------------------------------------------
    def test_t12_normal_account_regression_passes(self):
        # 1. 正常账户
        self._create_valid_account_fixture(cash=600000.0, total_equity=1000000.0)

        # 2. 正常合法持仓 (价值 40 万，与 60 万现金恒等)
        pos = [{
            'symbol': '600519',
            'name': '贵州茅台',
            'plan_id': 'PLAN_600519_INIT',
            'shares': 200,
            'cost_price': 2000.0
        }]
        atomic_write_json(self.positions_path, pos)

        # 3. 正常注册表
        reg_payload = {
            'schema_version': 'v7.2',
            'updated_at': '2026-09-16 10:00:00',
            'total_keys': 1,
            'records': {
                'PLAN_600519_INIT:BUY:TEST-ACC-01': {
                    'status': 'COMMITTED',
                    'fill_id': 'FILL_001'
                }
            }
        }
        atomic_write_json(self.registry_path, reg_payload)

        # 4. 正常成交流水
        with open(self.fills_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'fill_id': 'FILL_001', 'plan_id': 'PLAN_600519_INIT', 'direction': 'BUY'}) + '\n')

        # 5. 执行 Startup 与 Daily 对账核算
        reconciler = LedgerReconciliationEngine(storage_dir=self.test_dir)
        startup_res = reconciler.run_startup_reconciliation()
        self.assertEqual(startup_res['status'], 'PASS')
        self.assertTrue(startup_res['can_new_entry'])

        daily_res = reconciler.run_daily_ledger_reconciliation(quotes_map={'600519': 2000.0})
        self.assertEqual(daily_res['status'], 'PASS')
        self.assertTrue(daily_res['can_new_entry'])
        self.assertEqual(daily_res['equity_discrepancy'], 0.0)


if __name__ == '__main__':
    unittest.main()
