# -*- coding: utf-8 -*-
"""
scripts/ai_quant/paper_executor.py
Phase 17 & V7-G0C-R2: AI量化交易员模拟撮合执行引擎 (Paper Execution Engine)

【最高工程与业务纪律】：
1. 独立虚拟盘：1,000,000 RMB 初始资金，账户名 AIQ-PAPER-V1；
2. 绝对隔离：严禁写入 market.db，严禁污染六博主账本 (data/simulation_state.json) 与大乱斗账户；
3. TradePlan 真正不可变：TradePlan 为只读预测合同，本引擎零 UPDATE TradePlan，所有撮合与结算只写入独立 Ledger；
4. 仓位硬风控：max_positions = 3，单票上限 30% (30万元)，总仓上限 90% (保留 >= 10% 现金缓冲)；
5. A 股交易制度模拟：T+1 刚性锁仓、100 股整手、涨跌停阻断、停牌阻断；
6. T+1 Exit Pending 状态机：当日触发止损转为 T1_LOCKED，次日优先撮合；若次日跌停/停牌转为 LIMIT_BLOCKED/SUSPENSION_BLOCKED 挂起等待；
7. 日K日内时序模糊契约 (Intraday Ambiguity Contract)：单日若 High >= TP 且 Low <= SL，标记 INTRADAY_SEQUENCE_AMBIGUOUS，输出悲观与乐观收益界限；
8. V7-G0C-R2 崩溃恢复与执行事务契约：
   - 任何买卖撮合前，必须先执行 recover_incomplete_executions() 与 run_startup_reconciliation()；
   - 结构化两阶段事务流：PREPARED -> EFFECTS_WRITTEN -> COMMITTED；
   - 严禁将 PREPARED/PENDING 视为 NONE 重入；
   - 支持多崩溃注入点测试与跨实例恢复。
"""

import os
import sys
import json
import time
import math
import datetime
import uuid

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from competition.rules import AShareRules
from competition.costs import UnifiedCostModel
from ai_quant.storage_utils import (
    atomic_write_json, load_json_safe, atomic_rewrite_jsonl, strict_load_json, strict_read_jsonl, CriticalStateStatus,
    ACCOUNT_REQUIRED_FIELDS, ACCOUNT_FIELD_TYPES, validate_positions_schema
)
from ai_quant.idempotency_registry import IdempotencyRegistry, ExecutionState
from ai_quant.reconciliation import LedgerReconciliationEngine
from ai_quant.governance_policy import GovernanceSafetyPolicy

PAPER_ROOT = os.path.join(_BASE, 'data', 'ai_quant_research', 'paper_simulation')
ACCOUNT_PATH = os.path.join(PAPER_ROOT, 'account.json')
POSITIONS_PATH = os.path.join(PAPER_ROOT, 'positions.json')
ORDERS_PATH = os.path.join(PAPER_ROOT, 'orders.jsonl')
FILLS_PATH = os.path.join(PAPER_ROOT, 'fills.jsonl')
EQUITY_CURVE_PATH = os.path.join(PAPER_ROOT, 'equity_curve.jsonl')
TRADE_RESULTS_PATH = os.path.join(PAPER_ROOT, 'paper_trade_results.jsonl')

INITIAL_CAPITAL = 1000000.0
MAX_POSITIONS = 3
MAX_SINGLE_POSITION_RATIO = 0.30   # 单票最大资金比例 30% (30万)
MAX_EQUITY_RATIO = 0.90            # 全账户股票仓位上限 90%


class PaperAccount:
    """【独立虚拟账户数据模型】"""
    def __init__(self, storage_dir=PAPER_ROOT, account_id=None,
                 initial_capital=None, season_id=None):
        self.storage_dir = storage_dir
        self.account_path = os.path.join(storage_dir, 'account.json')
        self.positions_path = os.path.join(storage_dir, 'positions.json')
        self.account_id = account_id or "AIQ-PAPER-V1"
        self.expected_account_id = account_id
        self.initial_capital = float(initial_capital if initial_capital is not None else INITIAL_CAPITAL)
        self.expected_initial_capital = initial_capital
        self.season_id = season_id
        self.participant_id = 'P-AI-QUANT' if season_id else None
        self.expected_season_id = season_id
        self.cash = self.initial_capital
        self.market_value = 0.0
        self.total_equity = self.initial_capital
        self.net_pnl = 0.0
        self.net_pnl_pct = 0.0
        self.peak_equity = self.initial_capital
        self.max_drawdown_pct = 0.0
        self.max_positions = MAX_POSITIONS
        self.max_single_position_ratio = MAX_SINGLE_POSITION_RATIO
        self.max_equity_ratio = MAX_EQUITY_RATIO
        self.positions = []
        self.account_corrupted = False
        self.positions_corrupted = False
        self.corrupt_reason = None
        self.load_or_init()

    def load_or_init(self):
        """
        加载或初始化账户与持仓 (SAFE-STRICT-STATE-R2 强化):
        - 全新环境初始化: account.json 与 positions.json 均缺失，且无任何交易/订单历史时，创建初始账户并原子落盘
        - 单边缺失阻断:
          * account VALID + positions MISSING: 严禁自动写空持仓，标记 positions_corrupted = True，禁止 save 覆盖
          * account MISSING + positions VALID: 严禁恢复默认 100 万本金，标记 account_corrupted = True，禁止 save 覆盖
        - 关键状态损坏: 严格校验数值字段类型，不凭空猜测，刚性标记 corrupted 并阻断
        """
        os.makedirs(self.storage_dir, exist_ok=True)
        acc_res = strict_load_json(
            self.account_path, expected_type=dict,
            required_fields=ACCOUNT_REQUIRED_FIELDS,
            field_types=ACCOUNT_FIELD_TYPES
        )
        pos_res = strict_load_json(
            self.positions_path, expected_type=list,
            validator=validate_positions_schema
        )

        reg_path = os.path.join(self.storage_dir, 'processed_idempotency_registry.json')
        fills_path = os.path.join(self.storage_dir, 'fills.jsonl')
        trades_path = os.path.join(self.storage_dir, 'paper_trade_results.jsonl')
        orders_path = os.path.join(self.storage_dir, 'orders.jsonl')
        equity_path = os.path.join(self.storage_dir, 'equity_curve.jsonl')
        reg_exists = os.path.exists(reg_path)
        # Physical existence is surviving lifecycle evidence, even when a JSONL
        # file is zero bytes after a legitimate atomic rewrite/cleanup.
        has_economic_history = any(
            os.path.exists(p)
            for p in (fills_path, trades_path, orders_path, equity_path)
        )

        # 1. 检查是否为两文件均缺失
        if acc_res.is_missing and pos_res.is_missing:
            if has_economic_history:
                self.account_corrupted = True
                self.positions_corrupted = True
                self.corrupt_reason = "CRITICAL_STATE_MISSING: 检测到交易流水或订单历史存在，但账户与持仓物理文件缺失，拒绝作为全新账户初始化"
                return
            if reg_exists:
                self.account_corrupted = True
                self.positions_corrupted = True
                self.corrupt_reason = "CRITICAL_STATE_MISSING: account.json 与 positions.json 物理缺失，但幂等注册表已存在 (现有账户生命周期残缺)，拒绝作为全新账户初始化"
                return
            # 明确真正全新环境初始化 (account, positions, registry 均缺失且无历史)
            self.save()
            atomic_write_json(reg_path, {
                'schema_version': 'v7.2',
                'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                **({'account_id': self.account_id} if self.season_id is not None else {}),
                **({'season_id': self.season_id} if self.season_id is not None else {}),
                **({'participant_id': self.participant_id} if self.participant_id else {}),
                'total_keys': 0,
                'records': {}
            })
            return

        # 2. 检查 account.json 损坏/类型错误/字段残缺
        if acc_res.is_corrupt:
            self.account_corrupted = True
            self.corrupt_reason = f"account.json 损坏或格式非法: {acc_res.error}"
            return

        # 3. 检查 positions.json 损坏/类型错误/结构非法
        if pos_res.is_corrupt:
            self.positions_corrupted = True
            self.corrupt_reason = f"positions.json 损坏或格式非法: {pos_res.error}"
            return

        # 4. 单边缺失严格阻断 (BLOCKER-01)
        if acc_res.is_valid and pos_res.is_missing:
            self.positions_corrupted = True
            self.corrupt_reason = "CRITICAL_STATE_MISSING: account.json 存在但 positions.json 物理缺失 (单边缺失，严禁自动补空仓)"
            return

        if acc_res.is_missing and pos_res.is_valid:
            self.account_corrupted = True
            self.corrupt_reason = "CRITICAL_STATE_MISSING: positions.json 存在但 account.json 物理缺失 (单边缺失，严禁恢复默认初始本金)"
            return

        # 5. 双方均合规存在，正常解析
        if acc_res.is_valid and pos_res.is_valid:
            d = acc_res.value
            if self.expected_account_id is not None and d.get('account_id') != self.expected_account_id:
                self.account_corrupted = True
                self.corrupt_reason = f"account identity mismatch: expected {self.expected_account_id}, got {d.get('account_id')}"
                return
            if self.expected_season_id is not None and d.get('season_id') != self.expected_season_id:
                self.account_corrupted = True
                self.corrupt_reason = f"season identity mismatch: expected {self.expected_season_id}, got {d.get('season_id')}"
                return
            if self.expected_initial_capital is not None and abs(float(d.get('initial_capital', -1)) - float(self.expected_initial_capital)) > 0.05:
                self.account_corrupted = True
                self.corrupt_reason = f"initial capital mismatch: expected {self.expected_initial_capital}, got {d.get('initial_capital')}"
                return
            self.account_id = d.get('account_id', self.account_id)
            self.season_id = d.get('season_id', self.season_id)
            self.initial_capital = float(d.get('initial_capital', self.initial_capital))
            self.cash = float(d.get('cash', self.cash))
            self.market_value = float(d.get('market_value', self.market_value))
            self.total_equity = float(d.get('total_equity', self.total_equity))
            self.net_pnl = float(d.get('net_pnl', self.net_pnl))
            self.net_pnl_pct = float(d.get('net_pnl_pct', self.net_pnl_pct))
            self.peak_equity = float(d.get('peak_equity', self.peak_equity))
            self.max_drawdown_pct = float(d.get('max_drawdown_pct', self.max_drawdown_pct))
            self.positions = pos_res.value

    def save(self, crash_hook=None):
        """
        保存账户与持仓快照。
        crash_hook: 允许测试注入崩溃测试点 (如 'crash_after_account_save')
        """
        if self.account_corrupted or self.positions_corrupted:
            raise RuntimeError(f"[FAIL_CLOSED_LOCK] 禁止覆盖损坏的关键状态数据！原因: {self.corrupt_reason}")

        d = {
            'account_id': self.account_id,
            **({'participant_id': self.participant_id} if self.participant_id else {}),
            **({'season_id': self.season_id} if self.season_id is not None else {}),
            'initial_capital': round(self.initial_capital, 2),
            'cash': round(self.cash, 2),
            'market_value': round(self.market_value, 2),
            'total_equity': round(self.total_equity, 2),
            'net_pnl': round(self.net_pnl, 2),
            'net_pnl_pct': round(self.net_pnl_pct, 4),
            'peak_equity': round(self.peak_equity, 2),
            'max_drawdown_pct': round(self.max_drawdown_pct, 4),
            'max_positions': self.max_positions,
            'max_single_position_ratio': self.max_single_position_ratio,
            'max_equity_ratio': self.max_equity_ratio,
            'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        # 单文件分别原子写
        atomic_write_json(self.account_path, d)
        if crash_hook == 'crash_after_account_save':
            raise RuntimeError("CRASH_INJECTION: crash_after_account_save")

        atomic_write_json(self.positions_path, self.positions)
        if crash_hook == 'crash_after_positions_save':
            raise RuntimeError("CRASH_INJECTION: crash_after_positions_save")

    def update_market_value(self, quotes_map):
        """更新持仓最新市值与账户总资产"""
        mv = 0.0
        for p in self.positions:
            sym = p['symbol']
            shares = int(p.get('shares', 0))
            cur_p = float(quotes_map.get(sym, p.get('cost_price', 0.0)))
            p['current_price'] = cur_p
            p['position_value'] = round(shares * cur_p, 2)
            p['unrealized_pnl'] = round((cur_p - p['cost_price']) * shares, 2)
            p['unrealized_pnl_pct'] = round((cur_p / p['cost_price'] - 1.0) * 100, 2) if p['cost_price'] > 0 else 0.0
            mv += p['position_value']

        self.market_value = round(mv, 2)
        self.total_equity = round(self.cash + self.market_value, 2)
        self.net_pnl = round(self.total_equity - self.initial_capital, 2)
        self.net_pnl_pct = round((self.net_pnl / self.initial_capital) * 100, 4)
        if self.total_equity > self.peak_equity:
            self.peak_equity = self.total_equity
        if self.peak_equity > 0:
            dd = (self.peak_equity - self.total_equity) / self.peak_equity
            self.max_drawdown_pct = max(self.max_drawdown_pct, round(dd * 100, 4))
        self.save()


class PaperExecutionEngine:
    """
    【AI量化独立模拟交易执行引擎】
    """
    def __init__(self, storage_dir=PAPER_ROOT, cost_model=None, policy=None,
                 account_id=None, initial_capital=None, season_id=None,
                 governance_safety_path=None, context=None):
        if context is not None:
            account_id = account_id or context.get('ai_quant_account_id') or context.get('account_id') or 'AIQ-PAPER-V2'
            initial_capital = initial_capital if initial_capital is not None else context.get('ai_quant_initial_capital', 100000.0)
            season_id = season_id or context.get('season_id')
            governance_safety_path = governance_safety_path or context.get('governance_safety_path')
        self.storage_dir = storage_dir
        self.account = PaperAccount(
            storage_dir=storage_dir,
            account_id=account_id,
            initial_capital=initial_capital,
            season_id=season_id,
        )
        self.cost_model = cost_model or UnifiedCostModel()
        self.orders_path = os.path.join(storage_dir, 'orders.jsonl')
        self.fills_path = os.path.join(storage_dir, 'fills.jsonl')
        self.equity_curve_path = os.path.join(storage_dir, 'equity_curve.jsonl')
        self.trade_results_path = os.path.join(storage_dir, 'paper_trade_results.jsonl')
        self.idempotency_registry_path = os.path.join(storage_dir, 'processed_idempotency_registry.json')
        self.idempotency_registry = IdempotencyRegistry(registry_path=self.idempotency_registry_path)
        self.identity_mismatch = False
        if season_id is not None and not self.idempotency_registry.registry_corrupted:
            reg_probe = strict_load_json(self.idempotency_registry_path, expected_type=dict)
            if reg_probe.is_valid:
                for key, expected in (('season_id', season_id), ('account_id', account_id)):
                    if reg_probe.value.get(key) != expected:
                        self.identity_mismatch = True
                        self.idempotency_registry.registry_corrupted = True
                        self.idempotency_registry.corrupt_error = f'registry {key} mismatch'
                        break
        self.reconciler = LedgerReconciliationEngine(storage_dir=storage_dir)
        if policy is not None:
            self.policy = policy
        elif governance_safety_path or storage_dir != PAPER_ROOT:
            self.policy = GovernanceSafetyPolicy(
                halt_path=governance_safety_path or os.path.join(storage_dir, 'governance_halt.json'),
                preserve_schema=bool(governance_safety_path or season_id)
            )
        else:
            self.policy = GovernanceSafetyPolicy()
        self.startup_reconciled = False
        self.recovered = False
        self.crash_hook = None  # 允许测试注入崩溃点
        self.quote_provider = None  # Forward binds the approved live evidence adapter.
        self._last_verified_quotes = {}
        self.season_id = season_id
        self.participant_id = 'P-AI-QUANT' if season_id else None
        os.makedirs(storage_dir, exist_ok=True)

    def _quote_at_final_use(self, symbol, action, price, previous_close, trade_date):
        if self.quote_provider is None:
            return True
        if not self.quote_provider.final_guard(symbol, action, price, previous_close, trade_date):
            return False
        q = self.quote_provider.valid_quote(symbol)
        if q is None:
            return False
        self._last_verified_quotes[symbol] = {
            'provider':q['provider'], 'source_time':q['observed_at'],
            'requested_at':q['requested_at'], 'received_at':q['received_at'],
            'response_sha256':q['response_sha256'], 'market_date':q['market_date'],
            'market_price':q['price'], 'previous_close':q['previous_close'],
            'price_unit':q['price_unit'],
            'final_checked_at':self.quote_provider.clock().isoformat(),
        }
        return True

    def refresh_market_value(self, quotes_map):
        fresh = True
        if self.quote_provider is not None:
            quotes_map = self.quote_provider.get('quotes_map', {})
            for p in self.account.positions:
                if p['symbol'] not in quotes_map:
                    # Retain a previously recorded valuation, never call it a
                    # fresh quote or make it available to the execution guard.
                    previous_mark = p.get('current_price')
                    if (not isinstance(previous_mark, (int,float)) or not math.isfinite(previous_mark)
                            or previous_mark <= 0 or p.get('position_value') is None
                            or abs(p['position_value'] - round(p['shares'] * previous_mark, 2)) > 0.01):
                        return False
                    quotes_map[p['symbol']] = previous_mark
                    fresh = False
        self.account.update_market_value(quotes_map)
        return fresh

    def _append_jsonl(self, filepath, record):
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def log_order(self, order_record):
        # Rejected orders are still transaction trace evidence. Omitting their
        # identity corrupts subsequent reconciliation for an existing plan.
        if self.season_id is not None:
            for key, value in (('season_id', self.season_id),
                               ('participant_id', self.participant_id),
                               ('account_id', self.account.account_id)):
                if key in order_record and order_record[key] != value:
                    raise RuntimeError('Order trace identity mismatch: ' + key)
                order_record[key] = value
        if order_record.get('status') == 'FILLED' and order_record.get('symbol') in self._last_verified_quotes:
            order_record['execution_quote_evidence'] = dict(self._last_verified_quotes[order_record['symbol']])
        self._append_jsonl(self.orders_path, order_record)

    def log_fill(self, fill_record):
        if fill_record.get('symbol') in self._last_verified_quotes:
            fill_record['execution_quote_evidence'] = dict(self._last_verified_quotes[fill_record['symbol']])
        self._append_jsonl(self.fills_path, fill_record)

    def log_trade_result(self, trade_result):
        self._append_jsonl(self.trade_results_path, trade_result)

    def log_equity_snapshot(self, trade_date=None):
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rec = {
            'timestamp': now_str,
            'trade_date': trade_date or datetime.datetime.now().strftime('%Y%m%d'),
            'total_equity': self.account.total_equity,
            'cash': self.account.cash,
            'market_value': self.account.market_value,
            'net_pnl': self.account.net_pnl,
            'net_pnl_pct': self.account.net_pnl_pct,
            'positions_count': len(self.account.positions),
            'max_drawdown_pct': self.account.max_drawdown_pct
        }
        self._append_jsonl(self.equity_curve_path, rec)

    def _clean_orphan_sell_records(self, plan_id):
        """崩溃恢复：使用原子重写安全清理未完成事务的孤儿 SELL fill 与 trade result (FIX-1)"""
        if os.path.exists(self.fills_path):
            fills = self.reconciler._read_jsonl(self.fills_path)
            clean_fills = [f for f in fills if not (f.get('plan_id') == plan_id and f.get('direction') == 'SELL')]
            atomic_rewrite_jsonl(self.fills_path, clean_fills)

        if os.path.exists(self.trade_results_path):
            results = self.reconciler._read_jsonl(self.trade_results_path)
            clean_results = [r for r in results if r.get('plan_id') != plan_id]
            atomic_rewrite_jsonl(self.trade_results_path, clean_results)

    # -------------------------------------------------------------
    # 崩溃恢复引擎 (Execution Recovery Engine)
    # -------------------------------------------------------------
    def recover_incomplete_executions(self):
        """
        P0-1 & P0-2: 启动前扫描所有未完全 COMMITTED 的不完整执行事务并执行确定性自愈或标记熔断。
        返回: dict {'recovered_count': int, 'halt_count': int, 'findings': list}
        """
        incompletes = self.idempotency_registry.get_incomplete_executions()
        findings = []
        recovered_count = 0
        halt_count = 0

        if not incompletes:
            self.recovered = True
            return {'recovered_count': 0, 'halt_count': 0, 'findings': ['无未完成执行事务']}

        # 重新拉取物理磁盘快照
        self.account.load_or_init()
        if self.identity_mismatch or self.account.account_corrupted or self.account.positions_corrupted:
            msg = f"物理快照损坏禁止执行恢复自愈: {self.account.corrupt_reason}"
            findings.append(msg)
            for inc in incompletes:
                self.idempotency_registry.mark_recovery_required(
                    inc['tradeplan_id'], inc['action'], inc['account_id'],
                    reason=msg
                )
            self.policy.set_execution_state_unsafe(msg)
            halt_count += len(incompletes)
            self.recovered = True
            return {'recovered_count': recovered_count, 'halt_count': halt_count, 'findings': findings}

        fills_res = strict_read_jsonl(self.fills_path)
        trades_res = strict_read_jsonl(self.trade_results_path)

        if fills_res.malformed_lines > 0 or trades_res.malformed_lines > 0:
            msg = f"账本文件存在格式损坏行 (fills坏行={fills_res.malformed_lines}, trades坏行={trades_res.malformed_lines})，禁止自动推断"
            findings.append(msg)
            for inc in incompletes:
                self.idempotency_registry.mark_recovery_required(
                    inc['tradeplan_id'], inc['action'], inc['account_id'],
                    reason=f"账本文件损坏禁止自动恢复: {msg}"
                )
            self.policy.set_execution_state_unsafe(msg)
            halt_count += len(incompletes)
            self.recovered = True
            return {'recovered_count': recovered_count, 'halt_count': halt_count, 'findings': findings}

        fills = fills_res.value
        trade_results = trades_res.value
        fill_plan_ids = {f.get('plan_id'): f for f in fills}
        trade_result_plan_ids = {t.get('plan_id'): t for t in trade_results}
        active_pos_map = {p.get('plan_id'): p for p in self.account.positions}

        for inc in incompletes:
            pid = inc['tradeplan_id']
            action = inc['action']
            acc_id = inc['account_id']
            st = inc.get('status') or inc.get('state')
            expected_cash = inc.get('expected_post_cash')
            pre_cash = inc.get('pre_cash')

            if action == 'SELL':
                has_sell_fill = pid in fill_plan_ids and fill_plan_ids[pid].get('direction') == 'SELL'
                has_trade_result = pid in trade_result_plan_ids
                is_pos_still_active = pid in active_pos_map

                # 场景 A: 经济效果完全未发生 (无 fill, 无 trade_result, 现金未增加, 持仓仍在)
                if not has_sell_fill and not has_trade_result and is_pos_still_active:
                    if pre_cash is not None and abs(self.account.cash - pre_cash) < 0.05:
                        # 安全回滚到 NONE，允许正常再次平仓
                        self.idempotency_registry.rollback_to_none(pid, action, acc_id)
                        findings.append(f"事务 {pid} (SELL) 确认经济效果尚未发生，已安全回滚至 NONE")
                        recovered_count += 1
                        continue

                # 场景 A2: 在写入 fill 或 trade_result 之后、但 account.save() 执行前崩溃
                # 此时 account.cash 与 positions 依然完全是旧状态，经济效果未生效，但留下了孤儿 fill / trade_result
                if (has_sell_fill or has_trade_result) and is_pos_still_active:
                    if pre_cash is not None and abs(self.account.cash - pre_cash) < 0.05:
                        # 清理孤儿 fill 与 trade_result，并安全回滚注册表至 NONE，使次轮能正常平仓闭环
                        self._clean_orphan_sell_records(pid)
                        self.idempotency_registry.rollback_to_none(pid, action, acc_id)
                        findings.append(f"事务 {pid} (SELL) 确认现金未发生实际划转(旧状态完整)，已清理未完成日志并回滚至 NONE")
                        recovered_count += 1
                        continue

                # 场景 B: 经济效果已完整发生 (有 fill, 有 trade_result, 持仓已不在, 现金已增加)
                if has_sell_fill and has_trade_result and not is_pos_still_active:
                    # 补齐 COMMITTED 终态
                    fill_id = fill_plan_ids[pid].get('fill_id')
                    self.idempotency_registry.commit_execution(
                        pid, action, acc_id, fill_id=fill_id, symbol=inc.get('symbol'),
                        extra_meta={'recovered_commit': True}
                    )
                    findings.append(f"事务 {pid} (SELL) 确认经济效果已完整落地，已自动补齐 COMMITTED")
                    recovered_count += 1
                    continue

                # 场景 C: 半提交断裂 (如：account.cash 已经增加了，但 positions.json 仍包含旧持仓，且 fill/trade_result 已写)
                if has_sell_fill and has_trade_result and is_pos_still_active:
                    if expected_cash is not None and abs(self.account.cash - expected_cash) < 0.05:
                        # 确定事实：现金确实已加上，仅 positions 漏删。安全完成 positions 消除自愈
                        pos_to_del = active_pos_map[pid]
                        self.account.positions.remove(pos_to_del)
                        self.account.save()
                        fill_id = fill_plan_ids[pid].get('fill_id')
                        self.idempotency_registry.commit_execution(
                            pid, action, acc_id, fill_id=fill_id, symbol=inc.get('symbol'),
                            extra_meta={'recovered_from_half_commit': True}
                        )
                        findings.append(f"事务 {pid} (SELL) 处于半提交状态(现金已加但持仓滞留)，已安全自愈持仓并提交 COMMITTED")
                        recovered_count += 1
                        continue

                # 场景 D: 状态无法完全推断或严重失歧 -> 刚性触发 RECOVERY_REQUIRED 与 HALT
                self.idempotency_registry.mark_recovery_required(pid, action, acc_id, reason="SELL 执行状态与磁盘快照断裂，需人工核对")
                self.policy.set_execution_state_unsafe(f"检测到断裂未恢复事务 {pid}: SELL")
                findings.append(f"事务 {pid} (SELL) 处于断裂状态，已标记 RECOVERY_REQUIRED 并触发系统熔断")
                halt_count += 1

            elif action == 'BUY':
                has_buy_fill = pid in fill_plan_ids and fill_plan_ids[pid].get('direction') == 'BUY'
                is_pos_active = pid in active_pos_map

                # 场景 A: 经济效果未发生 (无 fill, 持仓无)
                if not has_buy_fill and not is_pos_active:
                    self.idempotency_registry.rollback_to_none(pid, action, acc_id)
                    findings.append(f"事务 {pid} (BUY) 确认无经济效果，安全回滚至 NONE")
                    recovered_count += 1
                    continue

                # 场景 B: 经济效果已完整发生 (有 fill, 持仓已有)
                if has_buy_fill and is_pos_active:
                    fill_id = fill_plan_ids[pid].get('fill_id')
                    self.idempotency_registry.commit_execution(
                        pid, action, acc_id, fill_id=fill_id, symbol=inc.get('symbol'),
                        extra_meta={'recovered_commit': True}
                    )
                    findings.append(f"事务 {pid} (BUY) 确认已完整建仓，自动补齐 COMMITTED")
                    recovered_count += 1
                    continue

                # 场景 C: 异常断裂 (有 fill 但无持仓，或反之)
                self.idempotency_registry.mark_recovery_required(pid, action, acc_id, reason="BUY 执行状态与持仓断裂，需人工核对")
                self.policy.set_execution_state_unsafe(f"检测到断裂未恢复事务 {pid}: BUY")
                findings.append(f"事务 {pid} (BUY) 处于断裂状态，标记 RECOVERY_REQUIRED 并熔断")
                halt_count += 1

        self.recovered = True
        return {'recovered_count': recovered_count, 'halt_count': halt_count, 'findings': findings}

    # -------------------------------------------------------------
    # 周期前置检查与前置对账门禁 (Recovery & Reconciliation before ANY Action)
    # -------------------------------------------------------------
    def ensure_pre_cycle_safety(self):
        """
        P0-2: 在任何平仓 (Exits) 或买入 (Entries) 之前，必须按序完成：
        0. 检查底层 account/positions 物理文件完整性
        1. recover_incomplete_executions()
        2. run_startup_reconciliation()
        3. 检查 execution_state_safe
        """
        # 0. 账户与持仓物理状态损坏硬检 (SAFE-STRICT-STATE-R2)
        if self.account.account_corrupted or self.account.positions_corrupted:
            msg = f"账户或持仓物理文件损坏 (account={self.account.account_corrupted}, pos={self.account.positions_corrupted}): {self.account.corrupt_reason}"
            self.policy.set_execution_state_unsafe(msg)
            return False, msg

        if self.idempotency_registry.registry_corrupted:
            msg = f"幂等注册表损坏或状态非法: {self.idempotency_registry.corrupt_error}"
            self.policy.set_execution_state_unsafe(msg)
            return False, msg

        # 1. 先做事务恢复
        rec_info = self.recover_incomplete_executions()
        if rec_info['halt_count'] > 0:
            return False, f"事务恢复发现 {rec_info['halt_count']} 个严重断裂，系统已熔断: {rec_info['findings']}"

        # 2. 启动自检对账
        rec_res = self.reconciler.run_startup_reconciliation()
        if not rec_res['can_new_entry']:
            self.policy.set_execution_state_unsafe(str(rec_res['findings']))
            return False, f"启动自检对账失败: {'; '.join(rec_res['findings'])}"

        # 3. 检查执行安全状态门禁
        if not self.policy.is_execution_state_safe():
            return False, "系统执行安全状态为 UNSAFE，拒绝进行任何新的经济行为"

        self.startup_reconciled = True
        return True, "SAFE"

    # -------------------------------------------------------------
    # 买入执行链路
    # -------------------------------------------------------------
    def execute_buy(self, decision_item, trade_plan, current_price, yesterday_close,
                    current_trade_date, open_price=None, low_price=None,
                    is_st=False, board='MAIN', is_suspended=False):
        """
        根据 TradePlan 和决策项执行模拟买入：
        - 前置检查前置安全 (Recovery & Reconciliation)
        - 校验持仓上限 (3只)
        - 校验单票 30% 预算上限
        - 校验 90% 总仓上限
        - 校验 A 股裁判规则 (一字涨停、停牌)
        - 扣减现金并增加持仓，写入 independent orders & fills ledger
        """
        # Final activation/session boundary runs before recovery audit logging,
        # order creation, account mutation, or any other economic write.
        if not self._quote_at_final_use(str(decision_item['symbol']).zfill(6), 'BUY',
                                       current_price, yesterday_close, current_trade_date):
            return False, 'MARKET_INPUT_UNAVAILABLE', 'Current verified quote unavailable at final use'
        from competition.activation_boundary import check_new_entry_boundary, decision_timestamp_from
        boundary = check_new_entry_boundary(
            decision_timestamp_from(decision_item), account=self.account,
            decision_season_ids=[
                decision_item.get('season_id'), trade_plan.get('season_id')
            ],
            ledger_root=self.storage_dir,
            safety_path=getattr(self.policy, 'halt_path', None),
        )
        if not boundary.allowed:
            return False, boundary.reason, boundary.reason

        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sym = str(decision_item['symbol']).zfill(6)
        name = decision_item.get('name', sym)
        plan_id = trade_plan.get('plan_id', f"PLAN_{sym}_{current_trade_date}")
        correlation_id = trade_plan.get('correlation_id') or self.policy.make_correlation_id(prefix="TRACE", symbol=sym, trade_date=current_trade_date)

        # 0. 前置安全门禁与对账 (P0-2)
        safe_ok, safe_msg = self.ensure_pre_cycle_safety()
        if not safe_ok:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'RECONCILIATION_HALT', 'reason': safe_msg,
                'timestamp': now_str
            })
            return False, 'RECONCILIATION_HALT', safe_msg

        # 0.1 账户与持仓物理损坏二次刚性硬闸 (SAFE-STRICT-STATE-R2)
        if self.account.account_corrupted or self.account.positions_corrupted or self.idempotency_registry.registry_corrupted:
            msg = f"关键状态损坏拒绝开仓: acc={self.account.account_corrupted}, pos={self.account.positions_corrupted}, reg={self.idempotency_registry.registry_corrupted}"
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'RECONCILIATION_HALT', 'reason': msg,
                'timestamp': now_str
            })
            return False, 'RECONCILIATION_HALT', msg

        # 0.1 全局持久化熔断门禁 (NEW_ENTRY_DISABLED)
        if self.policy.is_new_entry_disabled():
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'GOVERNANCE_HALT', 'reason': '全局治理熔断生效 (NEW_ENTRY_DISABLED=True)，阻断新开仓',
                'timestamp': now_str
            })
            return False, 'GOVERNANCE_HALT', '全局治理熔断生效，阻断新开仓'

        # 1. 事务状态机检查 (P0-1)
        exec_state = self.idempotency_registry.get_execution_state(plan_id, 'BUY', self.account.account_id)
        if exec_state == ExecutionState.COMMITTED:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'STRATEGY_REJECTED', 'reason': f'幂等拦截：合同 {plan_id} 已成交建仓 (COMMITTED)',
                'timestamp': now_str
            })
            return False, 'STRATEGY_REJECTED', '幂等拦截：合同已成交建仓'
        elif exec_state in (ExecutionState.PREPARED, ExecutionState.EFFECTS_WRITTEN, ExecutionState.RECOVERY_REQUIRED):
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'RECOVERY_REQUIRED', 'reason': f'事务拦截：合同 {plan_id} 正处于 {exec_state} 状态，禁止自动重复买入',
                'timestamp': now_str
            })
            return False, 'RECOVERY_REQUIRED', f'事务拦截：处于 {exec_state} 状态'

        # 2. 检查是否已经在持仓中
        holding_syms = {p['symbol'] for p in self.account.positions}
        if sym in holding_syms:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'STRATEGY_REJECTED', 'reason': '已在持仓组合中，不重复建仓',
                'timestamp': now_str
            })
            return False, 'STRATEGY_REJECTED', '已在持仓组合中'

        # 3. 检查持仓数量上限
        if len(self.account.positions) >= self.account.max_positions:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'STRATEGY_REJECTED', 'reason': f'持仓数量达上限({self.account.max_positions}只)',
                'timestamp': now_str
            })
            return False, 'STRATEGY_REJECTED', '持仓数量达上限'

        # 4. 检查停牌
        if is_suspended:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'SUSPENSION_BLOCKED', 'reason': '标的停牌，无法买入',
                'timestamp': now_str
            })
            return False, 'SUSPENSION_BLOCKED', '标的停牌，无法买入'

        # 5. 检查涨停板阻断 (区分 STRATEGY_REJECTED vs LIMIT_UP_BLOCKED)
        limit_up, _ = AShareRules.get_price_limits(yesterday_close, is_st=is_st, board=board)
        if float(current_price) >= limit_up:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'LIMIT_UP_BLOCKED', 'reason': '触及涨停板无法买入',
                'timestamp': now_str
            })
            return False, 'LIMIT_UP_BLOCKED', '触及涨停板无法买入'

        # 6. 计算买入仓位预算 (基于 100 万本金与账户总额约束)
        pos_ratio = float(decision_item.get('position_size_pct', 0.20))
        budget_target = self.account.initial_capital * min(self.account.max_single_position_ratio, pos_ratio)

        # 检查账户总仓上限 90% (MAX_EQUITY_RATIO)
        max_allowed_market_value = self.account.total_equity * self.account.max_equity_ratio
        current_mv = self.account.market_value
        if current_mv + budget_target > max_allowed_market_value:
            budget_target = max(0.0, max_allowed_market_value - current_mv)

        # 检查可用现金约束
        budget = min(budget_target, self.account.cash)
        if budget < 5000.0:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'STRATEGY_REJECTED', 'reason': '可用现金或仓位额度不足',
                'timestamp': now_str
            })
            return False, 'STRATEGY_REJECTED', '可用现金或仓位额度不足'

        # 7. 计算滑点后价格与 100 股整手
        raw_price = float(current_price)
        exec_price_raw = raw_price * (1.0 + self.cost_model.slippage_rate)
        shares = AShareRules.calculate_lot_shares(budget, exec_price_raw)
        if shares < 100:
            self.log_order({
                'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                'status': 'STRATEGY_REJECTED', 'reason': '预算不足买入最小1手(100股)',
                'timestamp': now_str
            })
            return False, 'STRATEGY_REJECTED', '预算不足买入1手'

        exec_p, gross_amt, comm, total_cash = self.cost_model.calculate_buy_cost(raw_price, shares)
        if total_cash > self.account.cash:
            shares -= 100
            if shares < 100:
                self.log_order({
                    'order_id': f"ORD_BUY_{sym}_{int(time.time()*1000)}",
                    'plan_id': plan_id, 'symbol': sym, 'direction': 'BUY',
                    'status': 'STRATEGY_REJECTED', 'reason': '扣费后现金不足1手',
                    'timestamp': now_str
                })
                return False, 'STRATEGY_REJECTED', '现金不足1手'
            exec_p, gross_amt, comm, total_cash = self.cost_model.calculate_buy_cost(raw_price, shares)

        pre_cash_val = self.account.cash
        post_cash_val = pre_cash_val - total_cash

        # 8. 启动执行事务 (P0-1: begin_execution -> PREPARED)
        if not self._quote_at_final_use(sym, 'BUY', current_price, yesterday_close, current_trade_date):
            return False, 'MARKET_INPUT_UNAVAILABLE', 'Quote expired before BUY transaction'
        participant_id = self.participant_id
        decision_id = decision_item.get('decision_id') or trade_plan.get('decision_id') or plan_id
        order_id = f"ORD_BUY_{uuid.uuid4().hex}"
        tx_record = self.idempotency_registry.begin_execution(
            tradeplan_id=plan_id, action='BUY', account_id=self.account.account_id,
            symbol=sym, qty=shares, price=exec_p, gross_amount=gross_amt,
            fees=comm, net_amount=total_cash, pre_cash=pre_cash_val,
            expected_post_cash=post_cash_val, pre_position_qty=0,
            expected_post_position_qty=shares, correlation_id=correlation_id,
            order_id=order_id, decision_id=decision_id, season_id=self.account.season_id,
            participant_id=participant_id,
            extra_meta={'budget': budget, 'price': exec_p}
        )
        transaction_id = tx_record['transaction_id']

        if self.crash_hook == 'crash_buy_after_prepared':
            raise RuntimeError("CRASH_INJECTION: crash_buy_after_prepared")

        # 9. 写 Order then Fill 留痕
        fill_id = f"FILL_BUY_{uuid.uuid4().hex}"
        self.log_order({
            'order_id': order_id, 'transaction_id': transaction_id, 'decision_id': decision_id,
            'plan_id': plan_id, 'season_id': self.account.season_id,
            'participant_id': participant_id, 'account_id': self.account.account_id, 'symbol': sym, 'direction': 'BUY',
            'requested_shares': shares, 'requested_price': exec_p,
            'created_at': now_str, 'status': 'FILLED', 'correlation_id': correlation_id,
        })
        fill_record = {
            'fill_id': fill_id,
            'order_id': order_id,
            'transaction_id': transaction_id,
            'decision_id': decision_id,
            'season_id': self.account.season_id,
            'participant_id': participant_id,
            'account_id': self.account.account_id,
            'plan_id': plan_id,
            'correlation_id': correlation_id,
            'direction': 'BUY',
            'symbol': sym,
            'name': name,
            'timestamp': now_str,
            'trade_date': current_trade_date,
            'price': exec_p,
            'shares': shares,
            'gross_amount': gross_amt,
            'commission': comm,
            'stamp_tax': 0.0,
            'simulated_slippage': round(gross_amt * self.cost_model.slippage_rate, 2),
            'total_cash_deducted': total_cash,
            'net_cash_effect': -total_cash,
            'executed_at': now_str,
            'risk_reward_ratio': trade_plan.get('risk_reward_ratio', 1.1),
            'predicted_mfe': trade_plan.get('predicted_mfe', 0.05),
            'predicted_mae': trade_plan.get('predicted_mae', -0.03),
        }
        self.log_fill(fill_record)

        if self.crash_hook == 'crash_buy_after_fill':
            raise RuntimeError("CRASH_INJECTION: crash_buy_after_fill")

        # 10. 更新内存并落盘
        self.account.cash -= total_cash
        new_pos = {
            'symbol': sym,
            'name': name,
            'plan_id': plan_id,
            'correlation_id': correlation_id,
            'entry_transaction_id': transaction_id,
            'entry_order_id': order_id,
            'entry_fill_id': fill_id,
            'entry_decision_id': decision_id,
            'season_id': self.account.season_id,
            'participant_id': participant_id,
            'account_id': self.account.account_id,
            'shares': shares,
            'cost_price': exec_p,
            'buy_date': current_trade_date,
            'buy_timestamp': now_str,
            'stop_loss': float(trade_plan.get('stop_loss', exec_p * 0.95)),
            'take_profit': float(trade_plan.get('take_profit', exec_p * 1.06)),
            'expected_holding_days': int(trade_plan.get('expected_holding_days', 3)),
            'holding_days': 0,
            'pending_exit': False,
            'exit_reason': None,
            'exit_status': None
        }
        self.account.positions.append(new_pos)
        self.account.save(crash_hook=self.crash_hook)

        # 11. 标记 EFFECTS_WRITTEN -> COMMITTED
        self.idempotency_registry.mark_effects_written(plan_id, 'BUY', self.account.account_id)

        if self.crash_hook == 'crash_buy_before_commit':
            raise RuntimeError("CRASH_INJECTION: crash_buy_before_commit")

        self.idempotency_registry.commit_execution(
            tradeplan_id=plan_id, action='BUY', account_id=self.account.account_id,
            fill_id=fill_id, order_id=order_id, transaction_id=transaction_id, symbol=sym,
            extra_meta={'shares': shares, 'price': exec_p}
        )
        self.refresh_market_value({sym: exec_p})
        self.log_equity_snapshot(trade_date=current_trade_date)
        return True, 'ORDER_FILLED', f'成功买入 {name} {shares}股 @ {exec_p}'

    # -------------------------------------------------------------
    # 持仓监控与卖出平仓链路 (含 T+1 Pending Exit 状态机)
    # -------------------------------------------------------------
    def monitor_and_execute_exits(self, quotes_map, current_trade_date, daily_bars_map=None):
        """
        每周期监控所有持仓，执行止盈/止损/到期平仓：
        - 前置进行恢复与对账 (P0-2)；
        - 若买入当日触发止损：标记 EXIT_STATUS = T1_LOCKED，转为 pending_exit 挂起等待次日；
        - 若次日遭遇跌停或停牌：标记 LIMIT_BLOCKED 或 SUSPENSION_BLOCKED 挂起；
        - 事务状态机控制：NONE -> PREPARED -> EFFECTS_WRITTEN -> COMMITTED，杜绝重复卖出与二次现金记账。
        """
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        exited_positions = []
        ambiguous_exits = []
        committed_sells = []

        # 0. 周期前置安全门禁与恢复 (P0-2)
        safe_ok, safe_msg = self.ensure_pre_cycle_safety()
        if not safe_ok:
            # 若处于 UNSAFE 状态，绝不盲目执行平仓
            return exited_positions, ambiguous_exits

        for p in list(self.account.positions):
            sym = p['symbol']
            shares = int(p['shares'])
            if self.quote_provider is not None:
                quote = self.quote_provider.valid_quote(sym)
                if quote is None:
                    continue  # No current quote, no exit trigger and no fabricated fill.
                cur_p = quote['price']
                yc = quote['previous_close']
                # Fresh quote evidence cannot override a known suspension.
                is_suspended = bool(p.get('is_suspended', False))
            else:
                cur_p = float(quotes_map.get(sym, p['cost_price']))
                yc = float(p.get('yesterday_close', cur_p))
                is_suspended = p.get('is_suspended', False)
            is_st = p.get('is_st', False)
            board = p.get('board', 'MAIN')
            plan_id = p['plan_id']
            if not p.get('entry_transaction_id'):
                self.policy.set_execution_state_unsafe(
                    f'active position {sym}/{plan_id} missing entry_transaction_id'
                )
                self.log_order({
                    'order_id': f"ORD_SELL_{uuid.uuid4().hex}",
                    'plan_id': plan_id, 'symbol': sym, 'direction': 'SELL',
                    'status': 'RECOVERY_REQUIRED',
                    'reason': 'missing entry_transaction_id under active-season contract',
                    'timestamp': now_str,
                })
                continue

            # 1. 检查是否触发日K时序模糊性契约 (Daily Bar Ambiguity)
            if daily_bars_map and sym in daily_bars_map:
                bar = daily_bars_map[sym]
                high_p = float(bar.get('high', cur_p))
                low_p = float(bar.get('low', cur_p))
                if high_p >= p['take_profit'] and low_p <= p['stop_loss']:
                    p['ambiguous_dual_hit'] = True
                    ambiguous_exits.append({
                        'symbol': sym,
                        'plan_id': p['plan_id'],
                        'trade_date': current_trade_date,
                        'high': high_p,
                        'low': low_p,
                        'stop_loss': p['stop_loss'],
                        'take_profit': p['take_profit'],
                        'status': 'INTRADAY_SEQUENCE_AMBIGUOUS'
                    })

            # 2. 判断是否触发平仓条件
            should_exit = False
            exit_reason = p.get('exit_reason')

            if p.get('pending_exit'):
                should_exit = True
            elif cur_p <= p['stop_loss']:
                should_exit = True
                exit_reason = 'STOP_LOSS'
            elif cur_p >= p['take_profit']:
                should_exit = True
                exit_reason = 'TAKE_PROFIT'
            elif p.get('holding_days', 0) >= p.get('expected_holding_days', 3):
                should_exit = True
                exit_reason = 'TIME_EXIT'

            if not should_exit:
                continue

            # 3. 校验 T+1 锁仓约束
            can_t1, avail_shares, msg_t1 = AShareRules.validate_t1_sell(p, current_trade_date)
            if not can_t1:
                p['pending_exit'] = True
                p['exit_reason'] = exit_reason
                p['exit_status'] = 'T1_LOCKED'
                self.log_order({
                    'order_id': f"ORD_SELL_{sym}_{int(time.time()*1000)}",
                    'plan_id': p['plan_id'], 'symbol': sym, 'direction': 'SELL',
                    'season_id': self.account.season_id,
                    'participant_id': self.participant_id,
                    'account_id': self.account.account_id,
                    'status': 'T1_LOCKED', 'reason': f'触发{exit_reason}但受T+1锁仓挂起等待次日',
                    'timestamp': now_str
                })
                continue

            # 4. 校验停牌与跌停阻断 (必须区分 SUSPENSION_BLOCKED vs LIMIT_DOWN_BLOCKED)
            if is_suspended:
                p['pending_exit'] = True
                p['exit_reason'] = exit_reason
                p['exit_status'] = 'SUSPENSION_BLOCKED'
                self.log_order({
                    'order_id': f"ORD_SELL_{sym}_{int(time.time()*1000)}",
                    'plan_id': p['plan_id'], 'symbol': sym, 'direction': 'SELL',
                    'status': 'SUSPENSION_BLOCKED', 'reason': '标的停牌，卖单挂起等待复牌',
                    'timestamp': now_str
                })
                continue

            _, limit_down = AShareRules.get_price_limits(yc, is_st=is_st, board=board)
            if cur_p <= limit_down:
                p['pending_exit'] = True
                p['exit_reason'] = exit_reason
                p['exit_status'] = 'LIMIT_DOWN_BLOCKED'
                self.log_order({
                    'order_id': f"ORD_SELL_{sym}_{int(time.time()*1000)}",
                    'plan_id': p['plan_id'], 'symbol': sym, 'direction': 'SELL',
                    'status': 'LIMIT_DOWN_BLOCKED', 'reason': '跌停板封死无法成交，卖单挂起等待开板',
                    'timestamp': now_str
                })
                continue

            # 5. 前置状态机检查 (P0-1 & P0-2)
            exec_state = self.idempotency_registry.get_execution_state(plan_id, 'SELL', self.account.account_id)
            if exec_state == ExecutionState.COMMITTED:
                self.log_order({
                    'order_id': f"ORD_SELL_{sym}_{int(time.time()*1000)}",
                    'plan_id': plan_id, 'symbol': sym, 'direction': 'SELL',
                    'status': 'ALREADY_PROCESSED', 'reason': f'幂等拦截：合同 {plan_id} 卖出平仓已处于 COMMITTED',
                    'timestamp': now_str
                })
                if p in self.account.positions:
                    exited_positions.append(p)
                continue
            elif exec_state in (ExecutionState.PREPARED, ExecutionState.EFFECTS_WRITTEN, ExecutionState.RECOVERY_REQUIRED):
                self.log_order({
                    'order_id': f"ORD_SELL_{sym}_{int(time.time()*1000)}",
                    'plan_id': plan_id, 'symbol': sym, 'direction': 'SELL',
                    'status': 'RECOVERY_REQUIRED', 'reason': f'事务拦截：合同 {plan_id} 处于 {exec_state} 状态，禁止自动重复卖出',
                    'timestamp': now_str
                })
                continue

            # 6. 计算撮合款项
            if not self._quote_at_final_use(sym, 'SELL', cur_p, yc, current_trade_date):
                continue
            buy_cost_total = p['cost_price'] * shares
            exec_p, gross_rev, comm, stamp, net_proc, net_pnl, pnl_pct = self.cost_model.calculate_sell_proceeds(
                cur_p, shares, buy_cost_total
            )

            pre_cash_val = self.account.cash
            post_cash_val = pre_cash_val + net_proc

            # 7. 开始 SELL 执行事务 (P0-1: begin_execution -> PREPARED)
            correlation_id = p.get('correlation_id', plan_id)
            participant_id = self.participant_id
            decision_id = p.get('entry_decision_id') or p.get('decision_id') or plan_id
            order_id = f"ORD_SELL_{uuid.uuid4().hex}"
            tx_record = self.idempotency_registry.begin_execution(
                tradeplan_id=plan_id, action='SELL', account_id=self.account.account_id,
                symbol=sym, qty=shares, price=exec_p, gross_amount=gross_rev,
                fees=(comm + stamp), net_amount=net_proc, pre_cash=pre_cash_val,
                expected_post_cash=post_cash_val, pre_position_qty=shares,
                expected_post_position_qty=0, correlation_id=correlation_id,
                order_id=order_id, decision_id=decision_id, season_id=self.account.season_id,
                participant_id=participant_id,
                extra_meta={'exit_reason': exit_reason, 'shares': shares, 'pnl': net_pnl}
            )
            transaction_id = tx_record['transaction_id']

            if self.crash_hook == 'crash_sell_after_prepared':
                raise RuntimeError("CRASH_INJECTION: crash_sell_after_prepared")

            # 8. 生成 Order, Fill 与 Trade Result 留痕
            fill_id = f"FILL_SELL_{uuid.uuid4().hex}"
            self.log_order({
                'order_id': order_id, 'transaction_id': transaction_id, 'decision_id': decision_id,
                'plan_id': plan_id, 'season_id': self.account.season_id,
                'participant_id': participant_id, 'account_id': self.account.account_id, 'symbol': sym, 'direction': 'SELL',
                'requested_shares': shares, 'requested_price': exec_p,
                'created_at': now_str, 'status': 'FILLED', 'correlation_id': correlation_id,
            })
            fill_record = {
                'fill_id': fill_id,
                'order_id': order_id,
                'transaction_id': transaction_id,
                'decision_id': decision_id,
                'season_id': self.account.season_id,
                'participant_id': participant_id,
                'account_id': self.account.account_id,
                'plan_id': plan_id,
                'correlation_id': correlation_id,
                'direction': 'SELL',
                'symbol': sym,
                'name': p['name'],
                'timestamp': now_str,
                'trade_date': current_trade_date,
                'price': exec_p,
                'shares': shares,
                'gross_revenue': gross_rev,
                'commission': comm,
                'stamp_tax': stamp,
                'simulated_slippage': round(gross_rev * self.cost_model.slippage_rate, 2),
                'net_proceeds': net_proc,
                'net_cash_effect': net_proc,
                'executed_at': now_str,
                'exit_reason': exit_reason
            }
            self.log_fill(fill_record)

            if self.crash_hook == 'crash_sell_after_fill':
                raise RuntimeError("CRASH_INJECTION: crash_sell_after_fill")

            trade_res = {
                'transaction_id': transaction_id,
                'open_transaction_id': p['entry_transaction_id'],
                'order_id': order_id,
                'fill_id': fill_id,
                'open_order_id': p.get('entry_order_id'),
                'open_fill_id': p.get('entry_fill_id'),
                'decision_id': decision_id,
                'season_id': self.account.season_id,
                'participant_id': participant_id,
                'account_id': self.account.account_id,
                'symbol': sym,
                'plan_id': plan_id,
                'correlation_id': correlation_id,
                'stock_code': sym,
                'name': p['name'],
                'buy_date': p['buy_date'],
                'exit_date': current_trade_date,
                'entry_price': p['cost_price'],
                'exit_price': exec_p,
                'shares': shares,
                'profit_loss': net_pnl,
                'return_pct': pnl_pct,
                'holding_days': p.get('holding_days', 1),
                'exit_reason': exit_reason,
                'exit_status': 'POSITION_CLOSED',
                'timestamp': now_str
            }
            self.log_trade_result(trade_res)

            if self.crash_hook == 'crash_sell_after_trade_result':
                raise RuntimeError("CRASH_INJECTION: crash_sell_after_trade_result")

            # 9. 内存更新并落盘
            self.account.cash += net_proc
            p['exit_reason'] = exit_reason
            p['exit_status'] = 'POSITION_CLOSED'
            p['exit_price'] = exec_p
            exited_positions.append(p)

            committed_sells.append({
                'plan_id': plan_id, 'transaction_id': transaction_id, 'order_id': order_id,
                'fill_id': fill_id, 'symbol': sym,
                'shares': shares, 'price': exec_p, 'pnl': net_pnl
            })

        # 10. 持仓列表移除已平仓记录并原子持久化
        for ep in exited_positions:
            if ep in self.account.positions:
                self.account.positions.remove(ep)

        self.account.save(crash_hook=self.crash_hook)

        # 11. 标记 EFFECTS_WRITTEN
        for cs in committed_sells:
            self.idempotency_registry.mark_effects_written(cs['plan_id'], 'SELL', self.account.account_id)

        if self.crash_hook == 'crash_sell_before_commit':
            raise RuntimeError("CRASH_INJECTION: crash_sell_before_commit")

        # 12. 提交 COMMITTED
        for cs in committed_sells:
            self.idempotency_registry.commit_execution(
                tradeplan_id=cs['plan_id'], action='SELL', account_id=self.account.account_id,
                fill_id=cs['fill_id'], order_id=cs['order_id'], transaction_id=cs['transaction_id'], symbol=cs['symbol'],
                extra_meta={'shares': cs['shares'], 'price': cs['price'], 'pnl': cs['pnl']}
            )
            self.refresh_market_value({cs['symbol']: cs['price']})
            self.log_equity_snapshot(trade_date=current_trade_date)

        return exited_positions, ambiguous_exits


def run_paper_intraday_cycle(quotes_map, current_trade_date, candidate_pool=None):
    """供调度器调用的完整模拟交易单周期驱动入口"""
    engine = PaperExecutionEngine()
    engine.account.update_market_value(quotes_map)
    # 1. 恢复与对账 -> 检查平仓
    exited, ambig = engine.monitor_and_execute_exits(quotes_map, current_trade_date)
    # 2. 刷新快照
    engine.log_equity_snapshot(trade_date=current_trade_date)
    return {
        'status': 'SUCCESS',
        'exited_count': len(exited),
        'ambiguous_count': len(ambig),
        'total_equity': engine.account.total_equity,
        'cash': engine.account.cash,
        'market_value': engine.account.market_value
    }
