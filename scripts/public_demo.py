"""Offline hypothetical orders over a tiny, explicitly historical market sample."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
os.environ['V7_PUBLIC_PREVIEW'] = '1'
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))

from competition.accounts import ArenaAccountsManager
from competition.fills import SimulationFillEngine

SAMPLE_PATH = ROOT / 'examples' / 'historical_bars.json'
SCENARIO_ID = 'PUBLIC-HISTORICAL-DEMO'
PLAYER = 'demo'


def load_sample():
    raw = SAMPLE_PATH.read_bytes()
    sample = json.loads(raw)
    bars = sample['bars']
    if sample['price_basis'] != 'UNADJUSTED' or len(bars) != 3:
        raise ValueError('DEMO_SAMPLE_SCHEMA_INVALID')
    dates = [bar['trade_date'] for bar in bars]
    if dates != sorted(set(dates)):
        raise ValueError('DEMO_SAMPLE_DATES_INVALID')
    for bar in bars:
        values = [float(bar[key]) for key in ('open', 'high', 'low', 'close')]
        if not all(value > 0 for value in values) or not (values[2] <= min(values[0], values[3]) <= max(values[0], values[3]) <= values[1]):
            raise ValueError('DEMO_SAMPLE_OHLC_INVALID')
    return sample, hashlib.sha256(raw).hexdigest()


def run_demo():
    """Replay with the project's actual fill/cost/rule/account implementations.

    The BUY is an explicit hypothetical instruction, not an inferred strategy
    signal. Daily closing prices are a research proxy, not intraday quotes.
    No governed production season or forward-day state is initialized.
    """
    sample, sample_hash = load_sample()
    baseline, entry, exit_bar = sample['bars']
    clock_value = dt.datetime.strptime(entry['trade_date'] + '150000', '%Y%m%d%H%M%S')
    engine = SimulationFillEngine(clock=lambda: clock_value)
    specs = {PLAYER: {'name': '公开回放示例账户', 'role': 'hypothetical_demo', 'icon': 'D', 'initial_capital': 100000.0}}
    with tempfile.TemporaryDirectory(prefix='v7-public-demo-') as directory:
        manager = ArenaAccountsManager(
            season_id=SCENARIO_ID, state_path=str(Path(directory) / 'account.json'),
            player_specs=specs, explicit_initialize=True,
        )
        ok, buy, message = engine.try_fill_buy(
            sample['symbol'], sample['name'], entry['close'], budget=20000,
            yesterday_close=baseline['close'], open_p=entry['open'], low_p=entry['low'],
            reason='HYPOTHETICAL_ORDER_AT_HISTORICAL_CLOSE_PROXY',
        )
        if not ok:
            raise ValueError('DEMO_BUY_REJECTED:' + message)
        applied, _ = manager.apply_fill(PLAYER, buy)
        if not applied:
            raise ValueError('DEMO_ACCOUNT_REJECTED_BUY')
        position = copy.deepcopy(manager.accounts[PLAYER]['positions'][0])
        blocked, _, t1_reason = engine.try_fill_sell(
            position, entry['close'], baseline['close'], current_date=entry['trade_date'],
            reason='DEMO_SAME_DAY_EXIT_ATTEMPT',
        )
        if blocked:
            raise ValueError('DEMO_T1_PROTECTION_FAILED')
        clock_value = dt.datetime.strptime(exit_bar['trade_date'] + '150000', '%Y%m%d%H%M%S')
        ok, sell, message = engine.try_fill_sell(
            position, exit_bar['close'], entry['close'], current_date=exit_bar['trade_date'],
            open_p=exit_bar['open'], high_p=exit_bar['high'],
            reason='HYPOTHETICAL_NEXT_SESSION_EXIT_AT_CLOSE_PROXY',
        )
        if not ok:
            raise ValueError('DEMO_SELL_REJECTED:' + message)
        applied, _ = manager.apply_fill(PLAYER, sell)
        if not applied:
            raise ValueError('DEMO_ACCOUNT_REJECTED_SELL')
        # This demo ledger records calculated hypothetical fills only. The
        # production season transaction writer and activation gates are not run.
        ledger_path = Path(directory) / 'demo_ledger.jsonl'
        with ledger_path.open('x', encoding='utf-8') as stream:
            for fill in (buy, sell):
                stream.write(json.dumps(fill, ensure_ascii=False, sort_keys=True) + '\n')
        ledger = [json.loads(line) for line in ledger_path.read_text(encoding='utf-8').splitlines()]
        final_cash = manager.accounts[PLAYER]['cash']
        replay_cash = round(100000.0 - ledger[0]['total_cash_required'] + ledger[1]['net_proceeds'], 2)
        reconciled = final_cash == replay_cash and not manager.accounts[PLAYER]['positions']
        if not reconciled:
            raise ValueError('DEMO_LEDGER_RECONCILIATION_FAILED')
        summary = {
            'initial_capital': 100000.0, 'final_cash': final_cash,
            'hypothetical_net_pnl': round(final_cash - 100000.0, 2),
            'positions_count': 0, 'fills_count': 2, 't1_blocked': True,
            'ledger_reconciliation': 'PASS',
        }
    return {
        'mode': 'HISTORICAL_HYPOTHETICAL_DEMO', 'not_live_trading': True,
        'market_sample': sample, 'sample_sha256': sample_hash,
        'scenario': '预设假设订单 → 真实撮合规则 → 同日卖出拒绝 → 下一交易日退出 → 账本核对',
        'entry_proxy': 'DAILY_CLOSE_PLUS_CONFIGURED_SLIPPAGE',
        'signal_origin': 'USER_SCENARIO_NOT_STRATEGY_SELECTION',
        't1_reason': t1_reason, 'fills': ledger, 'summary': summary,
        'scope': 'Core fill/rule/cost/account demonstration; no LLM, model inference, live feed, production season or Forward acceptance.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='print the complete hypothetical replay')
    args = parser.parse_args()
    result = run_demo()
    print(json.dumps(result if args.json else {
        'status': 'PASS', 'mode': result['mode'], **result['summary'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
