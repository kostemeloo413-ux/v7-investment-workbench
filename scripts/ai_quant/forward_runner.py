# -*- coding: utf-8 -*-
"""
scripts/ai_quant/forward_runner.py
Phase 18: 真实向前 5 分钟 Paper Simulation 运行引擎 (Forward Paper Runner)

【核心职责与纪律】：
1. 真实时序推进：按照真实盘中 5 分钟离散网格，由 trading_daemon.py 调用；
2. 完整时序四步遥测：记录 signal_time, decision_time, order_time, fill_time 与 market_snapshot；
3. 严格遵循用户 Phase 18 指令：
   - 绝不产生真实订单，所有交易仅记录在 active-season Forward namespace；
   - 细致区分交易执行阻断分类：LIMIT_UP_BLOCKED, LIMIT_DOWN_BLOCKED, SUSPENSION_BLOCKED, T1_LOCKED,
     INSUFFICIENT_CASH, MAX_POSITION_BLOCKED, STRATEGY_REJECTED；
   - 驱动 PaperExecutionEngine 模拟真实出场与建仓；
   - 记录每日资产净值与回撤指标。
"""

import os
import sys
import json
import time
import math
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from datasource import db as hdb
from ai_quant.paper_executor import PaperExecutionEngine
from ai_quant.forward_start_gate import (
    evaluate_forward_start_gate,
    resolve_forward_root,
)
from ai_quant.live_quotes import TencentQuotesProvider


class ForwardPaperRunner:
    """
    【Phase 18 真实向前 5 分钟 Paper 执行器】
    """
    def __init__(self, storage_dir=None, context=None, persist_analysis=True):
        if context is None:
            from competition.season_runtime import resolve_active_season_context
            context = resolve_active_season_context()
        self.context = context
        self.storage_dir = os.path.abspath(storage_dir or context.get(
            'ai_quant_paper_root', os.path.join(context['season_root'], 'ai_quant_paper')))
        self.telemetry_dir = os.path.join(resolve_forward_root(context), 'telemetry')
        self.epoch_id = context.get('forward_epoch_id') or context.get('manifest', {}).get('forward_epoch_id')
        if not isinstance(self.epoch_id, str) or not self.epoch_id:
            raise RuntimeError('active Forward epoch identity missing; refusing legacy fallback')
        os.makedirs(self.telemetry_dir, exist_ok=True)
        self.engine = PaperExecutionEngine(
            storage_dir=self.storage_dir,
            account_id=context.get('ai_quant_account_id') or context.get('account_id') or 'AIQ-PAPER-V2',
            initial_capital=context.get('ai_quant_initial_capital', 100000.0),
            season_id=context['season_id'],
            governance_safety_path=context['governance_safety_path'],
            context=context,
        )
        # Entry research must never be a constructor dependency of risk exits.
        self.persist_analysis = persist_analysis
        self.predictor = self.ranking_engine = None
        self.decision_engine = self.plan_engine = None

    def _initialize_entry(self):
        if self.ranking_engine is None:
            from ai_quant.ranking import Top5RankingEngine
            self.ranking_engine = Top5RankingEngine(persist_predictions=self.persist_analysis)
            self.predictor = self.ranking_engine.predictor
        if self.decision_engine is None:
            from ai_quant.decision import AIQuantDecisionEngine
            self.decision_engine = AIQuantDecisionEngine(max_positions=3)
        if self.plan_engine is None:
            from ai_quant.trade_plan import TradePlanEngine
            self.plan_engine = TradePlanEngine(default_competition_id=self.epoch_id,
                                              initialize_schema=False,
                                              persist_plans=self.persist_analysis)

    def _mark_snapshot(self, quotes_map, telemetry):
        marked = self.engine.refresh_market_value(quotes_map)
        telemetry['market_value_status'] = ('CURRENT_VERIFIED' if marked
                                           else 'STALE_PRESERVED_NO_PRICE_SUBSTITUTE')
        account = self.engine.account
        telemetry['account_snapshot'] = {
            'total_equity': account.total_equity, 'cash': account.cash,
            'market_value': account.market_value, 'net_pnl': account.net_pnl,
            'net_pnl_pct': account.net_pnl_pct, 'positions_count': len(account.positions),
            'max_drawdown_pct': account.max_drawdown_pct,
        }

    def _refresh_holding_days(self, trade_date):
        """Age from the canonical calendar, never from completed-bar availability."""
        end = datetime.datetime.strptime(trade_date, '%Y%m%d').date()
        ages = {}
        for position in self.engine.account.positions:
            try:
                date = datetime.datetime.strptime(position['buy_date'].replace('-', ''), '%Y%m%d').date()
            except (KeyError, TypeError, ValueError, AttributeError):
                ages[position['symbol']] = 'UNKNOWN_INVALID_BUY_DATE'
                continue
            if not 0 <= (end-date).days <= 3700:
                ages[position['symbol']] = 'UNKNOWN_INVALID_BUY_DATE'
                continue
            count = 0
            valid = True
            while date < end:
                date += datetime.timedelta(days=1)
                try:
                    calendar = rc.trading_calendar_status(date)
                except Exception:
                    valid = False
                    break
                if calendar.get('status') != 'VALID':
                    valid = False
                    break
                count += int(calendar.get('trading_day') is True)
            if valid:
                position['holding_days'] = count
                ages[position['symbol']] = count
            else:
                ages[position['symbol']] = 'UNKNOWN_CALENDAR'
        return ages

    def run_5minute_cycle(self, as_of_date=None, quotes_provider=None, candidate_symbols=None):
        """
        单次 5 分钟节点驱动。
        记录：signal_time, decision_time, order_time, fill_time, market_snapshot, execution_status
        """
        cycle_start_t = time.time()
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        td = str(as_of_date or now.strftime('%Y%m%d')).replace('-', '')[:8]

        telemetry = {
            'timestamp': now_str,
            'trade_date': td,
            'signal_time': None,
            'decision_time': None,
            'order_time': None,
            'fill_time': None,
            'execution_status': 'SUCCESS',
            'entry_status': 'NOT_EVALUATED',
            'exit_check_status': 'NOT_EVALUATED',
            'execution_stats': {
                'signal_count': 0,
                'buy_decision_count': 0,
                'submitted_order_count': 0,
                'filled_order_count': 0,
                'blocked_order_count': 0,
                'unfilled_order_count': 0,
                'blocked_breakdown': {
                    'LIMIT_UP_BLOCKED': 0,
                    'LIMIT_DOWN_BLOCKED': 0,
                    'SUSPENSION_BLOCKED': 0,
                    'T1_LOCKED': 0,
                    'INSUFFICIENT_CASH': 0,
                    'MAX_POSITION_BLOCKED': 0,
                    'STRATEGY_REJECTED': 0
                }
            },
            'market_snapshot': {},
            'top5_results': [],
            'decisions': [],
            'orders': [],
            'fills': [],
            'exits': [],
            'account_snapshot': {}
        }

        try:
            # 1. 获取盘中最新行情快照 (Market Snapshot)
            if quotes_provider is not None:
                if not isinstance(quotes_provider, TencentQuotesProvider):
                    telemetry['execution_status'] = 'HOLD_MARKET_INPUT_UNVERIFIED'
                    return telemetry
                if td != now.strftime('%Y%m%d'):
                    telemetry['execution_status'] = 'HOLD_STALE_DECISION_DATE'
                    return telemetry
                calendar = rc.trading_calendar_status(now.date())
                if (calendar.get('status') != 'VALID' or calendar.get('trading_day') is not True
                        or rc.market_phase(now) != 'open' or now.time().replace(tzinfo=None) <= datetime.time(9,30)):
                    telemetry['execution_status'] = 'HOLD_MARKET_SESSION'
                    return telemetry
                self.engine.quote_provider = quotes_provider
                quotes_map = quotes_provider.get('quotes_map', {})
                daily_bars_map = quotes_provider.get('daily_bars_map', {})
                yesterday_closes = quotes_provider.get('yesterday_closes', {})
                suspended_set = set(quotes_provider.get('suspended_set', []))
            else:
                # Completed daily bars cannot supply a current execution quote.
                # Keep the existing input seam; do not fabricate an adapter.
                telemetry['execution_status'] = 'HOLD_MARKET_INPUT_UNAVAILABLE'
                telemetry['safety_message'] = 'Approved current quote input is not wired'
                return telemetry

            telemetry['market_snapshot'] = {
                'stocks_count': len(quotes_map),
                'sample_quotes': {s: quotes_map[s] for s in list(quotes_map.keys())[:5]}
            }

            # 1.5 步骤 0：周期启动前执行事务恢复与启动对账 (P0-2: Recovery & Reconciliation before ANY Action)
            safe_ok, safe_msg = self.engine.ensure_pre_cycle_safety()
            if not safe_ok:
                telemetry['execution_status'] = 'HALTED_FOR_SAFETY'
                telemetry['safety_message'] = safe_msg
                return telemetry

            # 2. 步骤 A：持仓监控与平仓撮合 (先卖后买，释放现金)
            order_t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            telemetry['order_time'] = order_t

            telemetry['holding_trading_days'] = self._refresh_holding_days(td)
            exited, ambig = self.engine.monitor_and_execute_exits(quotes_map, td, daily_bars_map=daily_bars_map)
            telemetry['exit_check_status'] = 'CHECKED'
            telemetry['exits'] = [
                {
                    'symbol': p['symbol'],
                    'name': p['name'],
                    'exit_reason': p['exit_reason'],
                    'exit_status': p.get('exit_status', 'POSITION_CLOSED')
                } for p in exited
            ]
            telemetry['quote_evidence'] = quotes_provider.evidence
            self._mark_snapshot(quotes_map, telemetry)
            if any(quotes_provider.valid_quote(p['symbol']) is None for p in self.engine.account.positions):
                telemetry['execution_status'] = 'HOLD_POSITION_MARKET_INPUT_UNAVAILABLE'
                telemetry['entry_status'] = 'BLOCKED_POSITION_QUOTES'
                telemetry['exit_check_status'] = 'PARTIAL_MISSING_QUOTES'
                return telemetry  # Existing exits ran safely; no new entry on stale portfolio values.

            # 统计平仓阻断情况
            for p in self.engine.account.positions:
                if p.get('pending_exit'):
                    st = p.get('exit_status', 'T1_LOCKED')
                    if st in telemetry['execution_stats']['blocked_breakdown']:
                        telemetry['execution_stats']['blocked_breakdown'][st] += 1
                        telemetry['execution_stats']['blocked_order_count'] += 1

            # History/model failures block entries only, after exits and marks.
            try:
                self._initialize_entry()
                telemetry['history_cutoff'] = self.ranking_engine.input_provider.cutoff
                self.ranking_engine.input_provider.validate_as_of(td)
            except Exception as exc:
                telemetry['execution_status'] = 'HOLD_ENTRY_INPUT'
                telemetry['entry_status'] = 'BLOCKED_HISTORY_OR_MODEL'
                telemetry['entry_reason'] = str(exc)
                return telemetry
            telemetry['entry_status'] = 'ADMITTED'

            # 3. 步骤 B：特征提取与 Top 5 排序 (Signal Time)
            sig_t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            telemetry['signal_time'] = sig_t

            top5_list = self.ranking_engine.rank_market(as_of_date=td, candidate_symbols=candidate_symbols, top_k=5)
            quotes_provider.refresh([item['symbol'] for item in top5_list])
            quotes_map = quotes_provider.get('quotes_map', {})
            yesterday_closes = quotes_provider.get('yesterday_closes', {})
            telemetry['history_cutoff'] = self.ranking_engine.input_provider.cutoff
            telemetry['quote_evidence'] = quotes_provider.evidence
            telemetry['execution_stats']['signal_count'] = len(top5_list)
            telemetry['top5_results'] = [
                {
                    'rank': item['rank'],
                    'symbol': item['symbol'],
                    'name': item['name'],
                    'composite_rank_score': item['composite_rank_score'],
                    'up_probability': item['up_probability'],
                    'expected_return': item['expected_return'],
                    'predicted_mae': item.get('predicted_mae', item.get('expected_mae', -0.03)),
                    'predicted_mfe': item.get('predicted_mfe', 0.055)
                } for item in top5_list
            ]

            # 4. 步骤 C：独立决策与风控 (Decision Time)
            decision_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
            dec_t = decision_at.isoformat()
            telemetry['decision_time'] = dec_t

            current_acc_context = {
                'cash': self.engine.account.cash,
                'positions': [{'symbol': p['symbol']} for p in self.engine.account.positions]
            }
            decisions = self.decision_engine.evaluate_candidates(top5_list, current_account=current_acc_context)
            for decision in decisions:
                # Preserve the actual candidate evaluation time as an explicit
                # offset timestamp for the final Paper BUY boundary.
                decision['decision_timestamp'] = dec_t
            telemetry['decisions'] = [
                {
                    'symbol': d['symbol'],
                    'name': d['name'],
                    'decision': d['decision'],
                    'decision_timestamp': d['decision_timestamp'],
                    'decision_reason': d['decision_reason'],
                    'risk_reward_ratio': d.get('risk_reward_ratio', 1.0)
                } for d in decisions
            ]

            buy_cands = [d for d in decisions if d['decision'] == 'BUY']
            telemetry['execution_stats']['buy_decision_count'] = len(buy_cands)

            # 5. 步骤 D：模拟委托与撮合 (Fill Time)
            fill_t = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            telemetry['fill_time'] = fill_t

            for d in buy_cands:
                sym = d['symbol']
                telemetry['execution_stats']['submitted_order_count'] += 1
                cur_p = quotes_map.get(sym)
                yc = yesterday_closes.get(sym)
                if (not isinstance(cur_p, (int, float)) or not math.isfinite(cur_p) or cur_p <= 0
                        or not isinstance(yc, (int, float)) or not math.isfinite(yc) or yc <= 0):
                    telemetry['execution_stats']['blocked_order_count'] += 1
                    telemetry['orders'].append({'symbol': sym, 'status': 'MARKET_INPUT_UNAVAILABLE', 'reason': 'No valid actual quote/prior close; no fill'})
                    continue
                is_susp = sym in suspended_set

                # 只读生成预测计划合同 TradePlan
                plan = self.plan_engine.generate_trade_plan(d, current_price=cur_p)
                if not plan:
                    continue

                ok, status, msg = self.engine.execute_buy(
                    d, plan, current_price=cur_p, yesterday_close=yc,
                    current_trade_date=td, is_suspended=is_susp
                )

                if ok:
                    telemetry['execution_stats']['filled_order_count'] += 1
                    telemetry['fills'].append({'symbol': sym, 'price': cur_p, 'status': status})
                else:
                    telemetry['execution_stats']['blocked_order_count'] += 1
                    if status in telemetry['execution_stats']['blocked_breakdown']:
                        telemetry['execution_stats']['blocked_breakdown'][status] += 1
                    telemetry['orders'].append({'symbol': sym, 'status': status, 'reason': msg})

            # 统计策略级 WAIT 拒绝
            for d in decisions:
                if d['decision'] == 'WAIT':
                    if '持仓数量已达上限' in d.get('decision_reason', ''):
                        telemetry['execution_stats']['blocked_breakdown']['MAX_POSITION_BLOCKED'] += 1
                    elif '现金不足' in d.get('decision_reason', ''):
                        telemetry['execution_stats']['blocked_breakdown']['INSUFFICIENT_CASH'] += 1
                    elif '已在持仓' in d.get('decision_reason', ''):
                        telemetry['execution_stats']['blocked_breakdown']['STRATEGY_REJECTED'] += 1
                elif d['decision'] == 'NO_TRADE':
                    telemetry['execution_stats']['blocked_breakdown']['STRATEGY_REJECTED'] += 1

            # 6. 刷新市值并记录账户快照
            self._mark_snapshot(quotes_map, telemetry)
            telemetry['entry_status'] = 'EVALUATED'

        except Exception as e:
            telemetry['execution_status'] = 'ERROR'
            telemetry['error_message'] = str(e)
        finally:
            if telemetry['exit_check_status'] in ('CHECKED', 'PARTIAL_MISSING_QUOTES'):
                # Persist one valuation observation even when research is stale.
                try:
                    self.engine.log_equity_snapshot(trade_date=td)
                except Exception as exc:
                    telemetry['execution_status'] = 'ERROR_EQUITY_PERSISTENCE'
                    telemetry['error_message'] = str(exc)
            # Publish after all committed effects and marks, also on early
            # returns after a safe exit. Never project an interrupted ledger.
            if os.path.abspath(self.storage_dir) == os.path.abspath(self.context['ai_quant_paper_root']):
                try:
                    from competition.season_runtime import publish_reconciled_arena_snapshot
                    telemetry['arena_projection'] = publish_reconciled_arena_snapshot(self.context)
                except Exception as exc:
                    telemetry['execution_status'] = 'HALTED_ARENA_PROJECTION'
                    telemetry['arena_projection'] = {'status':'FAIL', 'error':str(exc)}
                    self.engine.policy.set_execution_state_unsafe('AIQ Arena projection: ' + str(exc))
            telemetry_file = os.path.join(self.telemetry_dir, f"forward_telemetry_{td}.jsonl")
            with open(telemetry_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(telemetry, ensure_ascii=False) + '\n')
            from datasource.completeness import atomic_json_write
            atomic_json_write(os.path.join(self.telemetry_dir, 'latest.json'), telemetry)

        return telemetry


def execute_forward_paper_cycle(context=None, current_timestamp=None):
    """Run only after the non-mutating Forward Start Gate allows it."""
    gate = evaluate_forward_start_gate(context=context, current_timestamp=current_timestamp)
    if not gate.get('pass'):
        return 'AI-Quant Forward=HOLD:' + ','.join(gate.get('reason_codes', []))
    runner = ForwardPaperRunner(context=context)
    provider = TencentQuotesProvider([p['symbol'] for p in runner.engine.account.positions])
    res = runner.run_5minute_cycle(quotes_provider=provider)
    status = res.get('execution_status', 'UNKNOWN')
    stats = res.get('execution_stats', {})
    acc = res.get('account_snapshot', {})
    equity = acc.get('total_equity')
    equity_text = f'{equity:,.0f}' if isinstance(equity, (int, float)) else 'UNKNOWN'
    return (f"AI-Quant(FORWARD_PAPER):{status}|Signal={stats.get('signal_count')}|"
            f"Filled={stats.get('filled_order_count')}|Blocked={stats.get('blocked_order_count')}|"
            f"Exited={len(res.get('exits', []))}|"
            f"ExitCheck={res.get('exit_check_status')}|Entry={res.get('entry_status')}|"
            f"Reason={res.get('entry_reason', '')}|Equity={equity_text}")


if __name__ == '__main__':
    print("=== 测试 ForwardPaperRunner 单次 5 分钟驱动 ===")
    r = execute_forward_paper_cycle()
    print("执行结果:", r)
