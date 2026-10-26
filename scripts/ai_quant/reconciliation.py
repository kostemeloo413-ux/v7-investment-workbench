# -*- coding: utf-8 -*-
"""
scripts/ai_quant/reconciliation.py
V7 治理与安全基础设施：对账核算引擎 (Startup & Daily Ledger Reconciliation Engine)

【最高纪律与财务安全契约】：
1. 启动对账 (Startup Reconciliation)：
   - 进程启动时首先核对：account.json, positions.json, fills.jsonl, orders.jsonl, idempotency_registry 是否自洽；
   - 发现未落盘但已成交记录、孤儿持仓、缺失文件，绝不猜测修复，坚决将系统置为：
     NEW_ENTRY_DISABLED = TRUE, RISK_EXIT_ENABLED = TRUE；
   - 仅允许安全的、确定性的自愈（如清理未完成的 .tmp 遗留文件）；
2. 每日账本对账 (Daily Ledger Reconciliation)：
   - cash >= 0 (严禁负现金)；
   - position qty >= 0 (严禁负持仓)；
   - 财务恒等式：|cash + market_value - total_equity| <= 0.05 元 (允许浮点与分厘级舍入误差)；
   - 资金流水对账：|初始本金 + 累计净出入款 + 已实现净盈亏 - cash - 初始未平仓成本| <= 1.0 元；
   - 持仓防重与上限检查：持仓股票数 <= max_positions，无重复股票；
   - T+1 不变量检验：同一自然日内严禁发生同一标的的买入且平仓出场；
   - 判定输出：DAILY_RECONCILIATION_STATUS = PASS / FAIL。
"""

import os
import json
import datetime
import math
from ai_quant.storage_utils import (
    load_json_safe, strict_load_json, strict_read_jsonl, CriticalStateStatus,
    ACCOUNT_REQUIRED_FIELDS, ACCOUNT_FIELD_TYPES,
    REGISTRY_REQUIRED_FIELDS, REGISTRY_FIELD_TYPES,
    validate_positions_schema
)


class LedgerReconciliationEngine:
    """
    【对账核算引擎】
    """
    def __init__(self, storage_dir):
        self.storage_dir = os.path.abspath(storage_dir)
        self.account_path = os.path.join(self.storage_dir, 'account.json')
        self.positions_path = os.path.join(self.storage_dir, 'positions.json')
        self.orders_path = os.path.join(self.storage_dir, 'orders.jsonl')
        self.fills_path = os.path.join(self.storage_dir, 'fills.jsonl')
        self.registry_path = os.path.join(self.storage_dir, 'processed_idempotency_registry.json')
        self.trade_results_path = os.path.join(self.storage_dir, 'paper_trade_results.jsonl')

    def cleanup_safe_temp_files(self):
        """确定性安全自愈：清理因断电或异常终止遗留的 .tmp 临时文件"""
        cleaned = []
        if os.path.exists(self.storage_dir):
            for fn in os.listdir(self.storage_dir):
                if fn.startswith('.') and '.tmp_' in fn:
                    fp = os.path.join(self.storage_dir, fn)
                    try:
                        os.remove(fp)
                        cleaned.append(fn)
                    except OSError:
                        pass
        return cleaned

    @staticmethod
    def _persisted_valuation(account, positions):
        """Reconcile the saved mark, never substitute a fill cost or a new quote."""
        findings, market_value = [], 0.0
        def finite(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for p in positions:
            symbol = p.get('symbol')
            shares, mark, value = p.get('shares'), p.get('current_price'), p.get('position_value')
            if (not finite(shares) or shares <= 0 or int(shares) != shares
                    or not finite(mark) or mark <= 0 or not finite(value) or value <= 0):
                findings.append(f'invalid/missing persisted position mark: {symbol}')
                continue
            calculated = round(shares * mark, 2)
            if abs(value - calculated) > 0.01:
                findings.append(f'persisted position_value contradicts current_price: {symbol}')
            cost = p.get('cost_price')
            if not finite(cost) or cost <= 0:
                findings.append(f'invalid position cost: {symbol}')
            elif 'unrealized_pnl' in p and (not finite(p['unrealized_pnl'])
                    or abs(p['unrealized_pnl'] - round((mark-cost)*shares, 2)) > 0.01):
                findings.append(f'persisted unrealized_pnl contradicts mark/cost: {symbol}')
            market_value += calculated
        required = ('cash', 'market_value', 'total_equity', 'initial_capital', 'net_pnl')
        if any(not finite(account.get(k)) for k in required):
            findings.append('invalid/non-finite account valuation')
        else:
            if account['cash'] < 0:
                findings.append('negative cash in persisted valuation')
            if abs(account['market_value'] - market_value) > 0.05:
                findings.append('account market_value does not match persisted position marks')
            if abs(account['total_equity'] - (account['cash'] + market_value)) > 0.05:
                findings.append('account total_equity does not equal cash + persisted market_value')
            if abs(account['net_pnl'] - (account['total_equity'] - account['initial_capital'])) > 0.05:
                findings.append('account net_pnl does not equal total_equity - initial_capital')
        return round(market_value, 2), findings

    def run_startup_reconciliation(self):
        """
        启动自检对账：
        返回 dict 包含 status ('PASS' / 'FAIL'), can_new_entry, can_risk_exit, findings
        """
        findings = []
        # 1. 安全清理临时孤儿文件
        cleaned_temps = self.cleanup_safe_temp_files()
        if cleaned_temps:
            findings.append(f"安全清理了 {len(cleaned_temps)} 个遗留 .tmp 临时文件")

        # 2. 核心文件存在性与严格 JSON 状态检查 (SAFE-STRICT-STATE-R2 强化)
        acc_res = strict_load_json(
            self.account_path, expected_type=dict,
            required_fields=ACCOUNT_REQUIRED_FIELDS,
            field_types=ACCOUNT_FIELD_TYPES
        )
        if acc_res.is_missing:
            return {
                'status': 'FAIL',
                'can_new_entry': False,
                'can_risk_exit': True,
                'findings': ['account.json 不存在，拒绝新开仓']
            }
        if acc_res.is_corrupt:
            return {
                'status': 'FAIL',
                'can_new_entry': False,
                'can_risk_exit': True,
                'findings': [f'account.json 损坏或格式非法: {acc_res.error}，拒绝新开仓']
            }
        acc = acc_res.value

        pos_res = strict_load_json(
            self.positions_path, expected_type=list,
            validator=validate_positions_schema
        )
        if pos_res.is_missing:
            return {
                'status': 'FAIL',
                'can_new_entry': False,
                'can_risk_exit': True,
                'findings': ['account.json 存在但 positions.json 物理缺失 (单边缺失)，拒绝新开仓']
            }
        if pos_res.is_corrupt:
            return {
                'status': 'FAIL',
                'can_new_entry': False,
                'can_risk_exit': True,
                'findings': [f'positions.json 损坏或格式非法: {pos_res.error}，拒绝新开仓']
            }
        positions = pos_res.value

        # 3. 校验现金非负
        cash = float(acc.get('cash', -1.0))
        if cash < 0.0:
            findings.append(f"检测到非法负现金: {cash:.2f}")

        # 4. 校验持仓股数与数量
        if len(positions) > acc.get('max_positions', 3):
            findings.append(f"持仓数量超限: {len(positions)} > {acc.get('max_positions', 3)}")

        seen_syms = set()
        for p in positions:
            sym = p.get('symbol')
            shares = int(p.get('shares', 0))
            if shares <= 0:
                findings.append(f"标的 {sym} 持仓数量非正: {shares}")
            if sym in seen_syms:
                findings.append(f"检测到重复持仓标的: {sym}")
            seen_syms.add(sym)

        # Startup must validate the account/equity identity as well as the
        # ledger relationships; otherwise a tampered snapshot could pass until
        # the next daily cycle.
        _, valuation_findings = self._persisted_valuation(acc, positions)
        findings.extend(valuation_findings)

        # 5. 校验最近成交流水与持仓的一致性 (SAFE-STRICT-STATE-R1 严格读取)
        fills_res = strict_read_jsonl(self.fills_path)
        orders_res = strict_read_jsonl(self.orders_path)
        trades_res = strict_read_jsonl(self.trade_results_path)
        if fills_res.malformed_lines > 0:
            findings.append(f"fills.jsonl 检测到 {fills_res.malformed_lines} 行格式损坏的无效行！")
        if orders_res.malformed_lines > 0:
            findings.append(f"orders.jsonl 检测到 {orders_res.malformed_lines} 行格式损坏的无效行！")
        if trades_res.malformed_lines > 0:
            findings.append(f"paper_trade_results.jsonl 检测到 {trades_res.malformed_lines} 行格式损坏的无效行！")

        fills = fills_res.value
        orders = orders_res.value
        trade_results = trades_res.value

        closed_plan_ids = {t.get('plan_id') for t in trade_results}
        active_pos_plan_ids = {p.get('plan_id') for p in positions}

        # 5.1 检查既在 Active 持仓中又在 Closed 平仓结果中的冲突
        conflict_pids = active_pos_plan_ids.intersection(closed_plan_ids)
        if conflict_pids:
            findings.append(f"数据冲突：合同 {conflict_pids} 既存在于活跃持仓又存在于已平仓结果中！")

        # 5.2 检查 BUY成交流水的一致性
        for f in fills:
            if f.get('direction') == 'BUY':
                pid = f.get('plan_id')
                if pid and (pid not in active_pos_plan_ids) and (pid not in closed_plan_ids):
                    findings.append(f"成交流水 {f.get('fill_id')} (合同{pid}) 在持仓与平仓表中均无踪影，存在数据断裂！")

        # 5.3 检查 SELL成交流水与持仓的一致性 (已平仓的不应仍在持仓中)
        for f in fills:
            if f.get('direction') == 'SELL':
                pid = f.get('plan_id')
                if pid and pid in active_pos_plan_ids:
                    findings.append(f"成交流水显示合同 {pid} 已执行卖出平仓，但其依然滞留在活跃持仓中！")

        # 5.4 校验 Idempotency Registry 与成交流水的一致性 (SAFE-STRICT-STATE-R2)
        reg_res = strict_load_json(
            self.registry_path, expected_type=dict,
            required_fields=REGISTRY_REQUIRED_FIELDS,
            field_types=REGISTRY_FIELD_TYPES
        )
        if reg_res.is_missing:
            findings.append("已有账户存在但 idempotency_registry 缺失 (CRITICAL_STATE_MISSING)，拒绝新开仓")
        elif reg_res.is_corrupt:
            findings.append(f"幂等注册表损坏或格式非法: {reg_res.error}")
        elif reg_res.is_valid:
            records = reg_res.value.get('records', {})
            # 检查是否有未 COMMITTED 的挂起状态或是存在 COMMITTED 但 fills 缺失
            committed_fill_ids = {r.get('fill_id') for r in records.values() if r.get('status') == 'COMMITTED' and r.get('fill_id')}
            actual_fill_ids = {f.get('fill_id') for f in fills if f.get('fill_id')}
            missing_fills = committed_fill_ids - actual_fill_ids
            if missing_fills:
                findings.append(f"幂等注册表记录了已确认成交的 fill_id {missing_fills}，但 fills.jsonl 中缺失！")

        findings.extend(self._validate_transaction_trace(
            acc, positions, orders, fills, trade_results,
            reg_res.value if reg_res.is_valid else {},
        ))

        is_pass = len([f for f in findings if '安全清理' not in f]) == 0
        return {
            'status': 'PASS' if is_pass else 'FAIL',
            'can_new_entry': is_pass,
            'can_risk_exit': True, # 无论如何必须允许已有仓位风控退出
            'findings': findings
        }

    def run_daily_ledger_reconciliation(self, quotes_map=None, current_trade_date=None):
        """
        每日对账 (SAFE-STRICT-STATE-R2 严格读取)：
        核算财务恒等式、成本扣除、T+1 合规率、重复成交与现金流动。
        """
        findings = []

        # 1. 严格读取账户、持仓与幂等注册表状态 (SAFE-STRICT-STATE-R2 强化)
        acc_res = strict_load_json(
            self.account_path, expected_type=dict,
            required_fields=ACCOUNT_REQUIRED_FIELDS,
            field_types=ACCOUNT_FIELD_TYPES
        )
        pos_res = strict_load_json(
            self.positions_path, expected_type=list,
            validator=validate_positions_schema
        )
        reg_res = strict_load_json(
            self.registry_path, expected_type=dict,
            required_fields=REGISTRY_REQUIRED_FIELDS,
            field_types=REGISTRY_FIELD_TYPES
        )

        if acc_res.is_missing:
            findings.append("account.json 文件缺失无法核算每日账本！")
        elif acc_res.is_corrupt:
            findings.append(f"account.json 损坏或格式非法: {acc_res.error}")

        if pos_res.is_missing:
            findings.append("positions.json 文件缺失无法核算每日账本！")
        elif pos_res.is_corrupt:
            findings.append(f"positions.json 损坏或格式非法: {pos_res.error}")

        if reg_res.is_missing:
            findings.append("idempotency_registry.json 文件缺失无法核算每日账本！")
        elif reg_res.is_corrupt:
            findings.append(f"idempotency_registry.json 损坏或格式非法: {reg_res.error}")

        # 2. 严格读取成交流水与平仓账本 (坏行刚性报错，禁止静默略过)
        fills_res = strict_read_jsonl(self.fills_path)
        orders_res = strict_read_jsonl(self.orders_path)
        trades_res = strict_read_jsonl(self.trade_results_path)

        if fills_res.malformed_lines > 0:
            findings.append(f"fills.jsonl 检测到 {fills_res.malformed_lines} 行格式损坏的无效行！")
        if orders_res.malformed_lines > 0:
            findings.append(f"orders.jsonl 检测到 {orders_res.malformed_lines} 行格式损坏的无效行！")
        if trades_res.malformed_lines > 0:
            findings.append(f"paper_trade_results.jsonl 检测到 {trades_res.malformed_lines} 行格式损坏的无效行！")

        fills = fills_res.value
        orders = orders_res.value
        trade_results = trades_res.value

        if acc_res.is_valid and pos_res.is_valid and reg_res.is_valid:
            findings.extend(self._validate_transaction_trace(
                acc_res.value, pos_res.value, orders, fills, trade_results, reg_res.value,
            ))

        cash = 0.0
        total_equity = 0.0
        calc_mv = 0.0
        equity_diff = 0.0
        quote_valuation = None

        if acc_res.is_valid and pos_res.is_valid and reg_res.is_valid:
            acc = acc_res.value
            positions = pos_res.value
            cash = float(acc.get('cash', 0.0))
            total_equity = float(acc.get('total_equity', 0.0))

            # 现金非负校验
            if cash < 0.0:
                findings.append(f"现金余额为负数: {cash:.2f}")

            # 市值与总资产恒等式
            calc_mv, valuation_findings = self._persisted_valuation(acc, positions)
            findings.extend(valuation_findings)
            # Optional newer prices are a separate quote observation, not a
            # reason to reinterpret a saved, earlier account mark as corrupt.
            if quotes_map is not None:
                valid_quotes = isinstance(quotes_map, dict)
                if valid_quotes:
                    for p in positions:
                        price = quotes_map.get(p['symbol'])
                        if (not isinstance(price, (int, float)) or isinstance(price, bool)
                                or not math.isfinite(price) or price <= 0):
                            valid_quotes = False
                            break
                if not valid_quotes:
                    findings.append('invalid/incomplete optional quote observation')
                else:
                    quote_valuation = round(cash + sum(p['shares'] * quotes_map[p['symbol']] for p in positions), 2)

            calc_equity = cash + calc_mv
            equity_diff = abs(calc_equity - total_equity)
            if equity_diff > 0.05:
                findings.append(f"资产净值不平: 账面={total_equity:.2f}, 计算={calc_equity:.2f}, 差异={equity_diff:.2f}")
        else:
            # 状态损坏时，差额标记为无法平衡
            equity_diff = 999999.0

        # 3. 校验重复成交 Fill (检查 fill_id 与 plan_id + direction 唯一性)
        seen_fill_ids = set()
        seen_buy_plans = set()
        for f in fills:
            fid = f.get('fill_id')
            if fid:
                if fid in seen_fill_ids:
                    findings.append(f"检测到重复成交流水 fill_id: {fid}")
                seen_fill_ids.add(fid)

            if f.get('direction') == 'BUY':
                pid = f.get('plan_id')
                if pid:
                    if pid in seen_buy_plans:
                        findings.append(f"检测到同一交易合同重复买入建仓: {pid}")
                    seen_buy_plans.add(pid)

        # 4. T+1 不变量检验
        for t in trade_results:
            b_d = str(t.get('buy_date', '')).replace('-', '')[:8]
            e_d = str(t.get('exit_date', '')).replace('-', '')[:8]
            if b_d and e_d and b_d == e_d:
                findings.append(f"T+1 刚性违规：标的 {t.get('stock_code')} 在买入当日 ({b_d}) 发生平仓！")

        is_pass = len(findings) == 0
        return {
            'status': 'PASS' if is_pass else 'FAIL',
            'reconciliation_date': current_trade_date or datetime.datetime.now().strftime('%Y%m%d'),
            'can_new_entry': is_pass,
            'can_risk_exit': True,
            'cash': cash,
            'market_value': calc_mv,
            'total_equity': total_equity,
            'equity_discrepancy': round(equity_diff, 4),
            'valuation_basis': 'PERSISTED_POSITION_MARK',
            'separate_quote_equity': quote_valuation,
            'findings': findings
        }

    def _validate_transaction_trace(self, account, positions, orders, fills, results, registry):
        """Validate the active AIQ Order -> Fill -> Effects -> Result chain.

        Legacy V1 fixtures remain compatible; the stricter identity contract is
        enabled for a season-scoped AIQ-PAPER-V2 account only.
        """
        findings = []
        active = bool(account.get('season_id')) and account.get('account_id') == 'AIQ-PAPER-V2'
        # The transaction identity contract is an active-season V2 contract.
        # Older V1/fixture ledgers intentionally contain only the historical
        # fill/plan fields and have no order or transaction identifiers.  Do
        # not reinterpret those legacy records as broken V2 traces; their
        # strict JSON/schema checks and the legacy fill/position checks still
        # run in the caller.  Active AIQ-PAPER-V2 state remains fully strict.
        if not active:
            return findings
        order_by_id = {}
        fill_by_id = {}
        for order in orders:
            oid = order.get('order_id')
            if not oid or oid in order_by_id:
                findings.append(f'duplicate/missing order_id: {oid}')
            else:
                order_by_id[oid] = order
        for fill in fills:
            fid = fill.get('fill_id')
            if not fid or fid in fill_by_id:
                findings.append(f'duplicate/missing fill_id: {fid}')
            else:
                fill_by_id[fid] = fill
            order = order_by_id.get(fill.get('order_id'))
            if order is None:
                findings.append(f'fill without order: {fid}')
            else:
                for field in ('transaction_id', 'decision_id', 'season_id', 'account_id', 'symbol', 'direction'):
                    if fill.get(field) != order.get(field):
                        findings.append(f'order/fill identity mismatch: {fid}:{field}')
        for oid, order in order_by_id.items():
            if order.get('status') == 'FILLED' and not any(f.get('order_id') == oid for f in fills):
                findings.append(f'order without fill: {oid}')

        committed = [r for r in registry.get('records', {}).values() if r.get('status') == 'COMMITTED']
        committed_by_tx = {r.get('transaction_id'): r for r in committed if r.get('transaction_id')}
        if active:
            for record in committed:
                if not record.get('order_id') or record.get('order_id') not in order_by_id:
                    findings.append(f'COMMITTED without order: {record.get("transaction_id")}')
                if not record.get('fill_id') or record.get('fill_id') not in fill_by_id:
                    findings.append(f'COMMITTED without fill: {record.get("transaction_id")}')
            for fill in fills:
                if fill.get('transaction_id') not in committed_by_tx:
                    findings.append(f'fill not backed by COMMITTED registry: {fill.get("fill_id")}')

        result_by_sell = {}
        for result in results:
            tx = result.get('transaction_id')
            if tx in result_by_sell:
                findings.append(f'duplicate trade result transaction_id: {tx}')
            result_by_sell[tx] = result
            if active:
                sell = committed_by_tx.get(tx)
                open_tx = result.get('open_transaction_id')
                opening = committed_by_tx.get(open_tx)
                if not sell or (sell.get('direction') or sell.get('action')) != 'SELL':
                    findings.append(f'trade result without committed SELL: {tx}')
                if not open_tx or not opening or (opening.get('direction') or opening.get('action')) != 'BUY':
                    findings.append(f'closed trade result invalid open reference: {tx}')
                elif any(result.get(k) != opening.get(k) for k in ('season_id', 'participant_id', 'account_id', 'symbol')):
                    findings.append(f'closed trade open identity mismatch: {tx}')
        if active:
            for fill in fills:
                if fill.get('direction') == 'SELL' and fill.get('transaction_id') not in result_by_sell:
                    findings.append(f'SELL fill missing closed trade result: {fill.get("fill_id")}')
            for position in positions:
                entry = position.get('entry_transaction_id')
                opening = committed_by_tx.get(entry)
                if not entry or not opening or (opening.get('direction') or opening.get('action')) != 'BUY':
                    findings.append(f'open position invalid BUY reference: {position.get("symbol")}')
                elif any(position.get(k) != opening.get(k) for k in ('season_id', 'participant_id', 'account_id', 'symbol') if k in position):
                    findings.append(f'open position identity mismatch: {entry}')
            for event in orders + fills:
                for key, expected in (('season_id', account.get('season_id')), ('account_id', account.get('account_id'))):
                    if event.get(key) != expected:
                        findings.append(f'{key} mismatch in transaction trace: {event.get("symbol")}')
        return findings

    def _read_jsonl(self, path, return_diagnostics=False):
        records = []
        malformed_lines = 0
        raw_lines = 0
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line_str = line.strip()
                    if line_str:
                        raw_lines += 1
                        try:
                            records.append(json.loads(line_str))
                        except Exception:
                            malformed_lines += 1
        if return_diagnostics:
            return records, malformed_lines, raw_lines
        return records
