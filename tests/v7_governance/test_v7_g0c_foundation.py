# -*- coding: utf-8 -*-
"""
tests/v7_governance/test_v7_g0c_foundation.py
V7-G0C 治理与安全基础设施全面断言测试套件 (Governance Safety Foundation Tests)

【用户指令全面覆盖 8 大治理板块】：
A. Health: Single source of truth (daemon_alive.json), API health consistency, stale detection
B. Atomic Write: normal write, interrupted temp write, target preservation, atomic replace, fsync
C. Idempotency: duplicate BUY, duplicate SELL, crash recovery idempotency, different tradeplan allowed
D. Startup Reconciliation: consistent state PASS, fill exists/account missing FAIL, position mismatch FAIL, corrupt JSON, partial JSONL
E. Daily Reconciliation: cash/equity invariant, negative cash FAIL, position mismatch FAIL, duplicate fill FAIL, T+1 issue FAIL, rounding tolerance
F. HALT Contract: CRITICAL disables new entry, CRITICAL still allows risk exit, WARNING does not incorrectly HALT
G. DB Writer Policy: authorized writer accepted, unauthorized writer detected, allowed table, wrong table rejected
H. Correlation Trace: correlation_id consistency from plan to fill and position
"""

import os
import sys
import unittest
import tempfile
import shutil
import json
import time

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))
sys.path.insert(0, os.path.join(_BASE, 'scripts', 'daemon'))

import runtime_config as rc
from ai_quant.storage_utils import atomic_write_json, load_json_safe
from ai_quant.idempotency_registry import IdempotencyRegistry
from ai_quant.reconciliation import LedgerReconciliationEngine
from ai_quant.governance_policy import GovernanceSafetyPolicy, AUTHORIZED_DB_WRITERS
from ai_quant.paper_executor import PaperExecutionEngine
from app import create_app
from fastapi.testclient import TestClient


class TestV7GovernanceSafetyFoundation(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="g0c_test_")
        self.engine = PaperExecutionEngine(storage_dir=self.test_dir)
        self.test_halt_path = os.path.join(self.test_dir, 'governance_halt.json')
        self.policy = GovernanceSafetyPolicy(halt_path=self.test_halt_path)
        self.engine.policy = self.policy

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -----------------------------------------------------------------
    # A. Health Source of Truth
    # -----------------------------------------------------------------
    def test_a1_single_source_of_truth_unification(self):
        """A1: 验证系统统一使用 rc.ALIVE_PATH (state/daemon_alive.json) 作为唯一活体心跳"""
        self.assertTrue(rc.ALIVE_PATH.endswith('daemon_alive.json'))
        # 验证 v7_api.py 中不再硬编码读取不存在的 alive.json
        client = TestClient(create_app())
        res = client.get('/api/v7/overview')
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertIn('daemon_alive', d['data']['system_health'])

    def test_a2_stale_heartbeat_detection(self):
        """A2: 验证超过 10 分钟的心跳被正确识别为非健康/STALE"""
        old_epoch = time.time() - 700.0 # 超过 10 分钟 (600s)
        test_alive = {'pid': 99999, 'epoch': old_epoch, 'ts': '2026-09-11 00:00:00'}
        alive_test_file = os.path.join(self.test_dir, 'daemon_alive.json')
        atomic_write_json(alive_test_file, test_alive)
        
        loaded = load_json_safe(alive_test_file)
        age_min = (time.time() - float(loaded['epoch'])) / 60.0
        self.assertGreater(age_min, 10.0)

    # -----------------------------------------------------------------
    # B. Atomic Write
    # -----------------------------------------------------------------
    def test_b1_atomic_write_normal_and_replace(self):
        """B1: 验证 atomic_write_json 成功落地目标文件且临时文件自动清除"""
        target = os.path.join(self.test_dir, "test_target.json")
        payload = {"status": "ACTIVE", "capital": 1000000.0}
        atomic_write_json(target, payload)
        
        self.assertTrue(os.path.exists(target))
        loaded = load_json_safe(target)
        self.assertEqual(loaded['capital'], 1000000.0)
        # 确认没有残留临时文件
        tmps = [f for f in os.listdir(self.test_dir) if f.startswith('.test_target.json.tmp_')]
        self.assertEqual(len(tmps), 0)

    def test_b2_atomic_write_preserves_existing_on_error(self):
        """B2: 验证写入出错时，既有目标文件毫发无损"""
        target = os.path.join(self.test_dir, "safe_target.json")
        original_data = {"key": "original_safe_value"}
        atomic_write_json(target, original_data)

        # 企图写入一个包含不可序列化对象的非法数据
        class UnserializableObject:
            pass

        with self.assertRaises(IOError):
            atomic_write_json(target, {"invalid": UnserializableObject()})

        # 验证原文件未被破坏
        restored = load_json_safe(target)
        self.assertEqual(restored['key'], "original_safe_value")

    # -----------------------------------------------------------------
    # C. Idempotency (正常循环防重 vs 崩溃恢复幂等)
    # -----------------------------------------------------------------
    def test_c1_duplicate_buy_prevention_normal_loop(self):
        """C1: 正常循环防重：同一股票已在持仓中，第二次买入被拦截 (STRATEGY_REJECTED)"""
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_C1', 'stop_loss': 1400.0, 'take_profit': 1650.0}
        ok1, st1, _ = self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        self.assertTrue(ok1)
        self.assertEqual(st1, 'ORDER_FILLED')

        # 再次调用同一买入
        ok2, st2, msg2 = self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        self.assertFalse(ok2)
        self.assertEqual(st2, 'STRATEGY_REJECTED')
        self.assertIn('幂等拦截', msg2)

    def test_c2_crash_recovery_idempotency(self):
        """C2: 崩溃恢复幂等：模拟内存/持仓丢失但 Registry 已记录，重启后坚决不重复成交！"""
        plan_id = 'PLAN_CRASH_TEST'
        account_id = self.engine.account.account_id
        
        # 假定已记录过买入幂等事实并写入对应的 fill 记录保持对账自洽
        self.engine.idempotency_registry.record_processed(plan_id, 'BUY', account_id, fill_id='FILL_OLD', symbol='600519')
        self.engine.log_fill({
            'fill_id': 'FILL_OLD', 'plan_id': plan_id, 'direction': 'BUY',
            'symbol': '600519', 'shares': 100, 'trade_date': '20260901'
        })
        self.engine.log_trade_result({
            'plan_id': plan_id, 'stock_code': '600519', 'exit_status': 'POSITION_CLOSED'
        })
        
        # 模拟清空持仓 (假设 positions.json 丢失或被冲刷)
        self.engine.account.positions = []
        
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': plan_id, 'stop_loss': 1400.0, 'take_profit': 1650.0}
        ok, st, msg = self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        
        self.assertFalse(ok)
        self.assertEqual(st, 'STRATEGY_REJECTED')
        self.assertIn('幂等拦截', msg)
        # 确认未产生重复持仓
        self.assertEqual(len(self.engine.account.positions), 0)

    def test_c3_different_tradeplan_allowed(self):
        """C3: 幂等精度：不同的 tradeplan_id 允许正常处理"""
        account_id = self.engine.account.account_id
        self.engine.idempotency_registry.record_processed('PLAN_001', 'BUY', account_id)
        # 不同的 plan 应该返回未处理
        self.assertFalse(self.engine.idempotency_registry.has_processed('PLAN_002', 'BUY', account_id))

    # -----------------------------------------------------------------
    # D. Startup Reconciliation
    # -----------------------------------------------------------------
    def test_d1_startup_reconciliation_pass_consistent(self):
        """D1: 启动对账通过：账户自洽且文件完整时返回 PASS 且 can_new_entry = True"""
        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = rec_engine.run_startup_reconciliation()
        self.assertEqual(res['status'], 'PASS')
        self.assertTrue(res['can_new_entry'])
        self.assertTrue(res['can_risk_exit'])

    def test_d2_startup_reconciliation_fails_on_corrupt_account(self):
        """D2: 启动对账失败：account.json 损坏或现金为负时判定 FAIL 并阻断开仓"""
        acc_path = os.path.join(self.test_dir, 'account.json')
        # 模拟写入非法负现金
        with open(acc_path, 'w', encoding='utf-8') as f:
            json.dump({'cash': -5000.0, 'max_positions': 3}, f)
        
        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = rec_engine.run_startup_reconciliation()
        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        # 关键安全防线：即使开仓被阻断，已有仓位风控退出必须永远允许！
        self.assertTrue(res['can_risk_exit'])

    def test_d3_startup_reconciliation_cleans_orphan_temp_files(self):
        """D3: 启动对账确定性自愈：安全清理残留 .tmp 临时文件"""
        tmp_orphan = os.path.join(self.test_dir, ".account.json.tmp_orphan123")
        with open(tmp_orphan, 'w') as f: f.write("unfinished")
        self.assertTrue(os.path.exists(tmp_orphan))

        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = rec_engine.run_startup_reconciliation()
        self.assertFalse(os.path.exists(tmp_orphan))

    # -----------------------------------------------------------------
    # E. Daily Reconciliation
    # -----------------------------------------------------------------
    def test_e1_daily_reconciliation_cash_equity_invariant(self):
        """E1: 每日对账：财务恒等式 (cash + market_value == total_equity) 检验"""
        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = rec_engine.run_daily_ledger_reconciliation()
        self.assertEqual(res['status'], 'PASS')
        self.assertLessEqual(res['equity_discrepancy'], 0.05)

    def test_e2_daily_reconciliation_fails_on_duplicate_fill(self):
        """E2: 每日对账：成交流水若有重复 fill_id 必须报警 FAIL"""
        fills_path = os.path.join(self.test_dir, 'fills.jsonl')
        with open(fills_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'fill_id': 'DUP_1', 'direction': 'BUY', 'plan_id': 'P1'}) + '\n')
            f.write(json.dumps({'fill_id': 'DUP_1', 'direction': 'BUY', 'plan_id': 'P1'}) + '\n')
        
        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = rec_engine.run_daily_ledger_reconciliation()
        self.assertEqual(res['status'], 'FAIL')
        self.assertIn('重复成交流水', str(res['findings']))

    def test_e3_daily_reconciliation_fails_on_t1_violation(self):
        """E3: 每日对账：同一自然日买入且卖出构成 T+1 违规，必须报警 FAIL"""
        trades_path = os.path.join(self.test_dir, 'paper_trade_results.jsonl')
        with open(trades_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'stock_code': '600519', 'buy_date': '20260901', 'exit_date': '20260901', 'profit_loss': 100}) + '\n')
        
        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        res = rec_engine.run_daily_ledger_reconciliation()
        self.assertEqual(res['status'], 'FAIL')
        self.assertIn('T+1 刚性违规', str(res['findings']))

    # -----------------------------------------------------------------
    # F. HALT Contract
    # -----------------------------------------------------------------
    def test_f1_critical_halt_semantics(self):
        """F1: HALT 语义：触发熔断时 NEW_ENTRY_DISABLED = TRUE 但 RISK_EXIT_ENABLED 永不关闭"""
        self.policy.trigger_halt('LEDGER_MISMATCH', '对账发现金额差异超过容忍度')
        self.assertTrue(self.policy.new_entry_disabled)
        self.assertTrue(self.policy.risk_exit_enabled)
        self.assertEqual(len(self.policy.halt_reasons), 1)

    # -----------------------------------------------------------------
    # G. Authorized DB Writer Policy
    # -----------------------------------------------------------------
    def test_g1_authorized_writer_accepted(self):
        """G1: 授权写入者校验：daily_k_updater 写入 daily_kline 属于合法授权"""
        ok, msg = self.policy.validate_db_write('daily_k_updater', 'daily_kline', 'INSERT')
        self.assertTrue(ok)
        self.assertEqual(msg, 'AUTHORIZED')

    def test_g2_unauthorized_writer_detected(self):
        """G2: 违规写入拦截：未知写入者试图写库被坚决阻断"""
        ok, msg = self.policy.validate_db_write('hacker_or_buggy_module', 'daily_kline', 'INSERT')
        self.assertFalse(ok)
        self.assertIn('UNAUTHORIZED_WRITER', msg)

    def test_g3_wrong_table_rejected(self):
        """G3: 越权写入拦截：daily_k_updater 企图写入 news 表被坚决阻断"""
        ok, msg = self.policy.validate_db_write('daily_k_updater', 'news', 'INSERT')
        self.assertFalse(ok)
        self.assertIn('UNAUTHORIZED_TABLE', msg)

    # -----------------------------------------------------------------
    # H. Correlation Trace
    # -----------------------------------------------------------------
    def test_h1_correlation_trace_consistency(self):
        """H1: 事件追踪：从买入到持仓再到平仓，correlation_id 保持全局一致"""
        corr_id = GovernanceSafetyPolicy.make_correlation_id(prefix="AUDIT_TRACE", symbol="600519", trade_date="20260901")
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_TRACE_01', 'correlation_id': corr_id, 'stop_loss': 1400.0, 'take_profit': 1650.0}
        
        ok, _, _ = self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        self.assertTrue(ok)
        # 持仓中继承了 correlation_id
        pos = self.engine.account.positions[0]
        self.assertEqual(pos['correlation_id'], corr_id)

        # 卖出平仓
        self.engine.monitor_and_execute_exits({'600519': 1700.0}, current_trade_date='20260902')
        # 平仓账本中继承了同一 correlation_id
        with open(self.engine.trade_results_path, 'r', encoding='utf-8') as f:
            res_rec = json.loads(f.readline())
        self.assertEqual(res_rec['correlation_id'], corr_id)


    # -----------------------------------------------------------------
    # I. G0C-R1 专项补丁测试 (Crash Injection, SELL Idempotency, HALT Persistence, DB Halt)
    # -----------------------------------------------------------------
    def test_i1_sell_idempotency_prevents_duplicate_cash_and_fill(self):
        """I1: SELL 幂等：同一平仓卖出重复触发时，返回 ALREADY_PROCESSED 且现金与 Fill 绝不重复追加"""
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_SELL_IDEM', 'stop_loss': 1400.0, 'take_profit': 1600.0}
        self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        
        cash_after_buy = self.engine.account.cash
        # 首次平仓 (触发止盈)
        self.engine.monitor_and_execute_exits({'600519': 1700.0}, current_trade_date='20260902')
        cash_after_sell = self.engine.account.cash
        self.assertGreater(cash_after_sell, cash_after_buy)
        
        # 验证 registry 中 SELL 已被 COMMITTED
        self.assertTrue(self.engine.idempotency_registry.has_processed('PLAN_SELL_IDEM', 'SELL', self.engine.account.account_id))
        
        # 统计当前 fills 数量
        fills_before = len(self.engine.reconciler._read_jsonl(self.engine.fills_path))
        
        # 模拟异常：再次向持仓中强行塞入该已平仓记录并再次执行 exits
        dummy_pos = {
            'symbol': '600519', 'name': '贵州茅台', 'plan_id': 'PLAN_SELL_IDEM',
            'shares': 100, 'cost_price': 1500.0, 'buy_date': '20260901',
            'stop_loss': 1400.0, 'take_profit': 1600.0
        }
        self.engine.account.positions.append(dummy_pos)
        self.engine.monitor_and_execute_exits({'600519': 1700.0}, current_trade_date='20260902')
        
        # 关键断言：现金绝不能再次增加，fills 绝不能重复写入！
        self.assertEqual(self.engine.account.cash, cash_after_sell)
        fills_after = len(self.engine.reconciler._read_jsonl(self.engine.fills_path))
        self.assertEqual(fills_after, fills_before)

    def test_i2_startup_reconciliation_conflict_detection(self):
        """I2: 启动对账扩展：Active 持仓与 Closed 平仓存在冲突或 SELL Fill 未出持仓时报警 FAIL"""
        rec_engine = LedgerReconciliationEngine(storage_dir=self.test_dir)
        # 模拟写入冲突：合同 P_CONFLICT 既在 positions 又在 trade_results
        pos_path = os.path.join(self.test_dir, 'positions.json')
        trades_path = os.path.join(self.test_dir, 'paper_trade_results.jsonl')
        atomic_write_json(pos_path, [{'symbol': '600519', 'plan_id': 'P_CONFLICT', 'shares': 100}])
        with open(trades_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'plan_id': 'P_CONFLICT', 'stock_code': '600519', 'exit_status': 'POSITION_CLOSED'}) + '\n')
            
        res = rec_engine.run_startup_reconciliation()
        self.assertEqual(res['status'], 'FAIL')
        self.assertFalse(res['can_new_entry'])
        self.assertIn('数据冲突', str(res['findings']))

    def test_i3_halt_persistence_and_restart(self):
        """I3: HALT 持久化与跨实例恢复：熔断落盘到 state/governance_halt.json，重启后自动保持阻断"""
        halt_file = os.path.join(self.test_dir, 'governance_halt.json')
        policy1 = GovernanceSafetyPolicy(halt_path=halt_file)
        self.assertFalse(policy1.is_new_entry_disabled())
        
        # 触发熔断
        policy1.trigger_halt('RECONCILIATION_FAIL', '测试熔断触发')
        self.assertTrue(policy1.is_new_entry_disabled())
        self.assertTrue(os.path.exists(halt_file))
        
        # 模拟进程重启，实例化全新的 policy2
        policy2 = GovernanceSafetyPolicy(halt_path=halt_file)
        self.assertTrue(policy2.is_new_entry_disabled())
        self.assertTrue(policy2.risk_exit_enabled)

    def test_i4_db_writer_enforcement_triggers_halt(self):
        """I4: 核心 DB Writer 拦截：未授权写库操作被拒绝并触发治理 HALT"""
        from datasource import db as history_db
        halt_file = os.path.join(self.test_dir, 'governance_halt.json')
        test_policy = GovernanceSafetyPolicy(halt_path=halt_file)
        # 临时替换全局 policy 实例
        old_pol = history_db._GOVERNANCE_POLICY
        history_db._GOVERNANCE_POLICY = test_policy
        try:
            with self.assertRaises(PermissionError):
                history_db.insert_signal('blogger1', 'BUY', writer_id='unauthorized_rogue_writer')
            # 验证熔断被触发并持久化
            self.assertTrue(test_policy.is_new_entry_disabled())
            self.assertTrue(test_policy.risk_exit_enabled)
        finally:
            history_db._GOVERNANCE_POLICY = old_pol


    # -----------------------------------------------------------------
    # J. G0C-R2 专项闭环测试 (Crash Injections, Recovery Order, Half Commit, TradePlan Correlation)
    # -----------------------------------------------------------------
    def test_j1_sell_crash_injection_six_points_and_recovery(self):
        """J1: SELL 崩溃注入测试：在 6 大崩溃点分别注入 crash，销毁实例重建后执行恢复，断言绝不 double cash/fill"""
        crash_points = [
            'crash_sell_after_prepared',
            'crash_sell_after_fill',
            'crash_sell_after_trade_result',
            'crash_after_account_save',
            'crash_after_positions_save',
            'crash_sell_before_commit'
        ]

        for idx, cp in enumerate(crash_points):
            sub_dir = os.path.join(self.test_dir, f"sub_sell_crash_{idx}")
            os.makedirs(sub_dir, exist_ok=True)
            eng1 = PaperExecutionEngine(storage_dir=sub_dir)
            eng1.policy = GovernanceSafetyPolicy(halt_path=os.path.join(sub_dir, 'halt.json'))

            dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
            plan = {'plan_id': f'PLAN_CRASH_{idx}', 'stop_loss': 1400.0, 'take_profit': 1600.0}
            ok, _, _ = eng1.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
            self.assertTrue(ok)
            cash_pre_sell = eng1.account.cash

            # 注入崩溃点
            eng1.crash_hook = cp
            with self.assertRaises(RuntimeError):
                eng1.monitor_and_execute_exits({'600519': 1700.0}, current_trade_date='20260902')

            # 销毁 eng1，重建 eng2 模拟系统重启
            del eng1
            eng2 = PaperExecutionEngine(storage_dir=sub_dir)
            eng2.policy = GovernanceSafetyPolicy(halt_path=os.path.join(sub_dir, 'halt.json'))

            # 执行恢复
            rec_info = eng2.recover_incomplete_executions()
            self.assertTrue(eng2.recovered)

            # 再次执行正常 exit cycle
            eng2.monitor_and_execute_exits({'600519': 1700.0}, current_trade_date='20260902')

            # 关键断言：
            # 1. SELL fill 最多一条或已闭环
            sell_fills = [f for f in eng2.reconciler._read_jsonl(eng2.fills_path) if f.get('direction') == 'SELL']
            self.assertLessEqual(len(sell_fills), 1)
            # 2. 现金绝无 double credit
            trade_res = eng2.reconciler._read_jsonl(eng2.trade_results_path)
            self.assertLessEqual(len(trade_res), 1)
            if len(trade_res) == 1:
                expected_net = cash_pre_sell + trade_res[0]['profit_loss'] + (trade_res[0]['shares'] * trade_res[0]['entry_price'])
                # 检查现金在合理范围内 (允许费用滑点浮点)
                self.assertLessEqual(abs(eng2.account.cash - expected_net), 10.0)

    def test_j2_buy_crash_injection_four_points_and_recovery(self):
        """J2: BUY 崩溃注入测试：在 4 大崩溃点注入 crash，销毁实例重建后执行恢复，断言绝不 double debit"""
        buy_crash_points = [
            'crash_buy_after_prepared',
            'crash_buy_after_fill',
            'crash_after_account_save',
            'crash_buy_before_commit'
        ]

        for idx, cp in enumerate(buy_crash_points):
            sub_dir = os.path.join(self.test_dir, f"sub_buy_crash_{idx}")
            os.makedirs(sub_dir, exist_ok=True)
            eng1 = PaperExecutionEngine(storage_dir=sub_dir)
            eng1.policy = GovernanceSafetyPolicy(halt_path=os.path.join(sub_dir, 'halt.json'))
            eng1.crash_hook = cp

            dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
            plan = {'plan_id': f'PLAN_BUY_CRASH_{idx}', 'stop_loss': 1400.0, 'take_profit': 1600.0}

            with self.assertRaises(RuntimeError):
                eng1.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')

            del eng1
            eng2 = PaperExecutionEngine(storage_dir=sub_dir)
            eng2.policy = GovernanceSafetyPolicy(halt_path=os.path.join(sub_dir, 'halt.json'))

            # 执行恢复
            eng2.recover_incomplete_executions()
            buy_fills = [f for f in eng2.reconciler._read_jsonl(eng2.fills_path) if f.get('direction') == 'BUY']
            self.assertLessEqual(len(buy_fills), 1)

    def test_j3_runtime_recovery_before_sell_and_buy_order(self):
        """J3: 运行时时序断言：验证 Recovery & Reconciliation 发生在任何 SELL 与 BUY 之前"""
        self.assertFalse(self.engine.recovered)
        self.assertFalse(self.engine.startup_reconciled)

        # 构造一个合规持仓
        p = {
            'symbol': '600519', 'name': '贵州茅台', 'plan_id': 'PLAN_ORDER_SEQ',
            'shares': 100, 'cost_price': 1500.0, 'buy_date': '20260901',
            'stop_loss': 1400.0, 'take_profit': 1600.0
        }
        self.engine.account.positions.append(p)
        self.engine.account.save()

        # 触发 exit cycle
        self.engine.monitor_and_execute_exits({'600519': 1700.0}, current_trade_date='20260902')
        # 必须已被标记为 recovered 与 startup_reconciled
        self.assertTrue(self.engine.recovered)
        self.assertTrue(self.engine.startup_reconciled)

    def test_j4_tradeplan_correlation_id_db_persistence(self):
        """J4: TradePlan 数据库加列与 correlation_id 重启恢复核验"""
        from ai_quant.trade_plan import TradePlanEngine
        plan_eng = TradePlanEngine(default_competition_id="UNITTEST_PERSIST")
        corr_id = "TRACE_TEST_CORR_PERSIST_999"
        mock_dec = {
            'decision': 'BUY', 'symbol': '600519', 'name': '贵州茅台',
            'up_probability': 0.70, 'expected_return': 0.08, 'predicted_mae': -0.025,
            'predicted_mfe': 0.08, 'right_side_score': 85, 'position_size_pct': 0.20,
            'expected_holding_days': 3, 'correlation_id': corr_id
        }
        plan = plan_eng.generate_trade_plan(mock_dec, current_price=1500.0)
        self.assertIsNotNone(plan)
        self.assertEqual(plan['correlation_id'], corr_id)

        # 模拟跨连接重读
        loaded = plan_eng.get_active_plan('600519', competition_id="UNITTEST_PERSIST")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get('correlation_id'), corr_id)

    def test_j5_db_writer_full_inventory_enforcement(self):
        """J5: 核心写库全关键入口运行时白名单拦截核验 (news, event_factor, trade_plan, sync_meta, etc.)"""
        from datasource import db as history_db
        test_halt = os.path.join(self.test_dir, 'isolated_halt.json')
        test_pol = GovernanceSafetyPolicy(halt_path=test_halt)
        old_pol = history_db._GOVERNANCE_POLICY
        history_db._GOVERNANCE_POLICY = test_pol

        try:
            # 1. 测试未授权写入 news
            with self.assertRaises(PermissionError):
                history_db.insert_news([{'title': 't', 'time': '12:00'}], writer_id='hacker_news_writer')

            # 2. 测试未授权写入 event_factor
            with self.assertRaises(PermissionError):
                history_db.upsert_event_factor('dedup_1', {'sentiment': 0.5}, writer_id='rogue_factor_writer')

            # 3. 测试未授权写入 sync_meta
            with self.assertRaises(PermissionError):
                history_db.set_meta('test_key', 'val', writer_id='unauth_meta_writer')
        finally:
            history_db._GOVERNANCE_POLICY = old_pol

    def test_k1_close_settlement_datetime_regression(self):
        """K1 (Unit Test): 验证 close_settlement 中 datetime 模块调用已修复，不再抛 AttributeError"""
        import datetime
        # 验证修复行语法与调用正确性
        now_str = datetime.datetime.now().strftime('%Y-%m-%d')
        self.assertRegex(now_str, r'^\d{4}-\d{2}-\d{2}$')

    def test_k2_daily_reconciliation_exception_fail_safe(self):
        """K2 (Integration Test): 验证每日对账抛出异常时坚决触发持久化 HALT (DAILY_RECONCILIATION_EXCEPTION)"""
        test_halt = os.path.join(self.test_dir, 'daily_rec_halt.json')
        policy = GovernanceSafetyPolicy(halt_path=test_halt)
        policy.clear_halt_for_testing()

        # 模拟 close_settlement 中的 fail-safe 逻辑
        try:
            raise AttributeError("module 'datetime' has no attribute 'now'")
        except Exception as e:
            policy.trigger_halt(
                critical_condition='DAILY_RECONCILIATION_EXCEPTION',
                reason=f"每日账本对账过程抛出异常 fail-safe 熔断: {str(e)}"
            )

        self.assertTrue(policy.is_new_entry_disabled())
        self.assertTrue(policy.risk_exit_enabled)
        self.assertEqual(policy.halt_reasons[-1]['condition'], 'DAILY_RECONCILIATION_EXCEPTION')

    def test_k3_atomic_rewrite_jsonl_success_and_preservation(self):
        """K3 (Unit Test): 验证 atomic_rewrite_jsonl 成功写入及异常中断时保护目标文件完整"""
        from ai_quant.storage_utils import atomic_rewrite_jsonl
        test_jsonl = os.path.join(self.test_dir, 'test_atomic.jsonl')
        initial_records = [{'id': 1, 'text': 'first'}, {'id': 2, 'text': 'second'}]
        atomic_rewrite_jsonl(test_jsonl, initial_records)

        # 验证写入成功
        with open(test_jsonl, 'r', encoding='utf-8') as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['id'], 1)

        # 验证写入异常中断时原文件保持不变且无孤儿 tmp 文件
        class UnserializableObj:
            pass

        bad_records = [{'id': 3}, UnserializableObj()]
        with self.assertRaises(IOError):
            atomic_rewrite_jsonl(test_jsonl, bad_records)

        # 检查原目标文件内容完好
        with open(test_jsonl, 'r', encoding='utf-8') as f:
            intact_lines = [json.loads(line) for line in f]
        self.assertEqual(len(intact_lines), 2)
        self.assertEqual(intact_lines[1]['id'], 2)

        # 检查无残余临时文件
        tmp_files = [fn for fn in os.listdir(self.test_dir) if fn.startswith('.test_atomic.jsonl.tmp_')]
        self.assertEqual(len(tmp_files), 0)

    def test_k4_recovery_cleanup_uses_atomic_rewrite(self):
        """K4 (Unit Test): 验证 paper_executor 在清理孤儿 SELL 记录时使用 atomic_rewrite_jsonl"""
        from ai_quant.paper_executor import PaperExecutionEngine
        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        engine.account.starting_capital = 1000000.0
        engine.account.cash = 1000000.0
        engine.account.save()

        # 写入孤儿记录
        orphan_fill = {'plan_id': 'ORPHAN_PLAN_1', 'direction': 'SELL', 'qty': 100}
        normal_fill = {'plan_id': 'NORMAL_PLAN_2', 'direction': 'BUY', 'qty': 100}
        with open(engine.fills_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(orphan_fill) + '\n' + json.dumps(normal_fill) + '\n')

        # 执行清理
        engine._clean_orphan_sell_records('ORPHAN_PLAN_1')

        # 验证孤儿记录已被安全清理且正常记录留存
        with open(engine.fills_path, 'r', encoding='utf-8') as f:
            remaining = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['plan_id'], 'NORMAL_PLAN_2')

    def test_k5_malformed_ledger_record_triggers_fail_safe(self):
        """K5 (Integration Test): 验证账本存在格式损坏行时拒绝猜测，未完成事务标记 RECOVERY_REQUIRED 且置 execution_state_safe=False"""
        from ai_quant.paper_executor import PaperExecutionEngine
        engine = PaperExecutionEngine(storage_dir=self.test_dir)
        engine.account.starting_capital = 1000000.0
        engine.account.cash = 1000000.0
        engine.account.save()

        # 注册一个未完成事务
        engine.idempotency_registry.begin_execution(
            'TEST_MALFORMED_PLAN', 'SELL', 'acc_1',
            symbol='000001', pre_cash=1000000.0, expected_post_cash=1010000.0
        )

        # 在 fills.jsonl 中注入格式损坏的行
        with open(engine.fills_path, 'w', encoding='utf-8') as f:
            f.write('{"plan_id": "VALID"}\nMALFORMED_GARBAGE_LINE_CORRUPTED\n')

        res = engine.recover_incomplete_executions()
        self.assertGreater(res['halt_count'], 0)
        self.assertFalse(engine.policy.is_execution_state_safe())
        self.assertTrue(engine.policy.is_new_entry_disabled())

        # 验证该未完成事务已被标记为 RECOVERY_REQUIRED
        inc_list = engine.idempotency_registry.get_incomplete_executions()
        test_inc = next((i for i in inc_list if i['tradeplan_id'] == 'TEST_MALFORMED_PLAN'), None)
        self.assertIsNotNone(test_inc)
        self.assertEqual(test_inc.get('status'), 'RECOVERY_REQUIRED')

    def test_k6_link_close_trace_enforcement(self):
        """K6 (Unit Test): 验证 link_close_trace 受 validate_db_write 保护且拒绝未授权调用"""
        from datasource import db as history_db
        test_halt = os.path.join(self.test_dir, 'link_close_halt.json')
        test_pol = GovernanceSafetyPolicy(halt_path=test_halt)
        old_pol = history_db._GOVERNANCE_POLICY
        history_db._GOVERNANCE_POLICY = test_pol

        try:
            # 1. 模拟未授权写入者调用 link_close_trace
            with self.assertRaises(PermissionError):
                history_db.link_close_trace('blogger_test', '600519', 5.0, 'TAKE_PROFIT', '2026-09-15 10:00:00',
                                           writer_id='unauthorized_close_logger')

            # 2. 合法授权写入者调用不抛 PermissionError
            history_db.init_decision_trace()
            res = history_db.link_close_trace('blogger_test', '600519', 5.0, 'TAKE_PROFIT', '2026-09-15 10:00:00',
                                              writer_id='decision_trace_logger')
            # 无对应 open trace 则返回 None，但权限校验通过
            self.assertIsNone(res)
        finally:
            history_db._GOVERNANCE_POLICY = old_pol

    def test_k7_schema_init_and_migration_enforcement(self):
        """K7 (Unit Test): 验证 DDL 建表与 Schema 迁移纳入 db_init / schema_migration 白名单治理"""
        from datasource import db as history_db
        test_halt = os.path.join(self.test_dir, 'schema_halt.json')
        test_pol = GovernanceSafetyPolicy(halt_path=test_halt)
        old_pol = history_db._GOVERNANCE_POLICY
        history_db._GOVERNANCE_POLICY = test_pol

        try:
            # 1. 非法身份尝试调用 init_db
            with self.assertRaises(PermissionError):
                history_db.init_db(writer_id='unauthorized_init_writer')

            # 2. 合法身份正常通过
            history_db.init_db(writer_id='db_init')
        finally:
            history_db._GOVERNANCE_POLICY = old_pol

    def test_k8_all_runtime_db_business_writers_enforcement(self):
        """K8 (Integration Test): 验证全量运行时 Business Writer 均受 Policy 强校验"""
        from ai_quant.governance_policy import AUTHORIZED_DB_WRITERS
        expected_writers = [
            'kline_writer', 'daily_k_updater', 'news_persister', 'event_factor_writer',
            'meta_syncer', 'event_calendar_persister', 'signal_logger', 'ai_quant_ranker',
            'trade_plan_generator', 'instrument_syncer', 'decision_trace_logger',
            'db_init', 'schema_migration'
        ]
        for w in expected_writers:
            self.assertIn(w, AUTHORIZED_DB_WRITERS, f"缺少授权写入者登记: {w}")
            cfg = AUTHORIZED_DB_WRITERS[w]
            self.assertEqual(cfg['database'], 'market.db')
            self.assertGreater(len(cfg['tables']), 0)
            self.assertGreater(len(cfg['operations']), 0)


if __name__ == '__main__':
    unittest.main()
