"""Real paper ledger in a temp directory; no production data or network use."""
import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from ai_quant.forward_runner import ForwardPaperRunner
from ai_quant.live_quotes import TencentQuotesProvider
from ai_quant.paper_executor import PaperExecutionEngine

NOW = dt.datetime.fromisoformat('2026-10-27T10:00:00+08:00')


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class ExitIndependenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.halt = self.root / 'governance_halt.json'
        self.halt.write_text(json.dumps(dict(execution_state_safe=True,
            new_entry_disabled=False, risk_exit_enabled=True, trading_enabled=True,
            real_trading=False, halt_reasons=[])), encoding='utf-8')
        self.context = dict(season_id='TEST-SEASON', season_root=str(self.root),
            forward_epoch_id='TEST-EPOCH', forward_root=str(self.root/'forward'),
            ai_quant_paper_root=str(self.root/'projection-other'),
            governance_safety_path=str(self.halt))
        self.engine = PaperExecutionEngine(storage_dir=str(self.root/'paper'),
            account_id='AIQ-PAPER-V2', initial_capital=100000,
            season_id='TEST-SEASON', governance_safety_path=str(self.halt))
        # A committed position, created through the unchanged executor.
        from competition.activation_boundary import BoundaryResult
        # Inject admission for fixture setup only; the exit cycle uses real guards.
        with patch('competition.activation_boundary.check_new_entry_boundary', return_value=BoundaryResult(True,'TEST_FIXTURE')):
            result = self.engine.execute_buy(
                dict(symbol='600000', name='TEST', decision_id='TEST-DECISION', position_size_pct=.2),
                dict(plan_id='TEST-PLAN', expected_holding_days=30, stop_loss=9, take_profit=10.5),
                10, 10, '20261026')
        self.assertTrue(result[0], result)

    def tearDown(self):
        self.temp.cleanup()

    def quote(self, price=10.8, old=False):
        observed = NOW - dt.timedelta(seconds=180 if old else 1)
        fields = ['']*40
        for i, v in {1:'ALPHA', 2:'600000', 3:str(price), 4:'10', 30:observed.strftime('%Y%m%d%H%M%S'), 36:'20000',37:'2000'}.items():
            fields[i] = v
        raw = '~'.join(fields)
        return dict(provider='Tencent', code='600000', price=price,
            observed_at=observed.isoformat(), raw_payload=raw,
            response_sha256=hashlib.sha256(raw.encode()).hexdigest())

    def cycle(self, *, price=10.8, missing=False, old=False, entry_error=None):
        provider = TencentQuotesProvider(['600000'], clock=lambda:NOW,
            fetcher=lambda symbols: {} if missing else {'sh600000':self.quote(price,old)})
        runner = ForwardPaperRunner(storage_dir=str(self.root/'paper'), context=self.context)
        error = entry_error or RuntimeError('AIQ_INPUT_STALE_COMPLETED_DATE')
        with patch('ai_quant.forward_runner.datetime.datetime', Clock), \
             patch('ai_quant.forward_runner.rc.trading_calendar_status', side_effect=lambda day:{'status':'VALID','trading_day':day.weekday()<5}), \
             patch('ai_quant.forward_runner.rc.market_phase', return_value='open'), \
             patch.object(runner, '_initialize_entry', side_effect=error) as entry:
            result = runner.run_5minute_cycle(quotes_provider=provider)
        return runner, result, entry

    def test_stale_history_cannot_prevent_committed_exit(self):
        runner, result, _ = self.cycle()
        self.assertEqual(result['exit_check_status'], 'CHECKED')
        self.assertEqual(result['entry_status'], 'BLOCKED_HISTORY_OR_MODEL')
        self.assertEqual(len(result['exits']), 1)
        self.assertEqual(len(runner.engine.account.positions), 0)
        self.assertGreater(result['account_snapshot']['total_equity'], 100000)
        self.assertTrue(runner.engine.ensure_pre_cycle_safety()[0])
        _, again, _ = self.cycle()
        self.assertEqual(again['exits'], [])
        fills = (self.root/'paper/fills.jsonl').read_text().splitlines()
        self.assertEqual(len(fills), 2)  # one actual test BUY and one SELL

    def test_missing_cache_does_not_load_before_exit(self):
        runner, result, entry = self.cycle(entry_error=FileNotFoundError('CURRENT.json'))
        self.assertEqual(len(result['exits']), 1)
        self.assertEqual(result['entry_reason'], 'CURRENT.json')
        entry.assert_called_once()

    def test_fresh_mark_survives_history_error(self):
        _, result, _ = self.cycle(price=10.1)
        self.assertEqual(result['exits'], [])
        self.assertEqual(result['market_value_status'], 'CURRENT_VERIFIED')
        self.assertGreater(result['account_snapshot']['market_value'], 0)

    def test_missing_and_stale_quotes_cannot_fill_or_enable_entry(self):
        for kw in [dict(missing=True),dict(old=True)]:
            with self.subTest(kw=kw):
                runner, result, entry = self.cycle(**kw)
                self.assertEqual(len(runner.engine.account.positions), 1)
                self.assertEqual(result['exit_check_status'], 'PARTIAL_MISSING_QUOTES')
                self.assertEqual(result['exits'], [])
                entry.assert_not_called()

    def test_t1_and_limit_down_still_block(self):
        runner, result, _ = self.cycle(price=9)
        self.assertEqual(result['exits'], [])
        self.assertEqual(runner.engine.account.positions[0]['exit_status'], 'LIMIT_DOWN_BLOCKED')
        p = runner.engine.account.positions[0]
        p['buy_date'] = '20261027'
        runner.engine.account.save()
        runner, result, _ = self.cycle(price=10.8)
        self.assertEqual(result['exits'], [])
        self.assertEqual(runner.engine.account.positions[0]['exit_status'], 'T1_LOCKED')

    def test_constructor_does_not_import_entry_research(self):
        import builtins
        original = builtins.__import__
        def guarded(name,*args,**kwargs):
            if name in ('ai_quant.ranking','ai_quant.predict','ai_quant.production_input'):
                raise AssertionError('entry research imported before exit check')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=guarded):
            ForwardPaperRunner(storage_dir=str(self.root/'paper'), context=self.context)

    def test_time_exit_is_aged_once_per_trading_day_without_daily_bars(self):
        runner = ForwardPaperRunner(storage_dir=str(self.root/'paper'), context=self.context)
        position = runner.engine.account.positions[0]
        position['buy_date'] = '20261023'  # Friday; Saturday and Sunday do not count.
        position['expected_holding_days'] = 2
        runner.engine.account.save()
        runner, result, _ = self.cycle(price=10.1)
        self.assertEqual(result['holding_trading_days']['600000'], 2)
        self.assertEqual(result['exits'][0]['exit_reason'], 'TIME_EXIT')

    def test_suspension_and_unsafe_state_are_not_bypassed(self):
        self.engine.account.positions[0]['is_suspended'] = True
        self.engine.account.save()
        runner, result, _ = self.cycle()
        self.assertEqual(result['exits'], [])
        self.assertEqual(runner.engine.account.positions[0]['exit_status'],'SUSPENSION_BLOCKED')
        runner.engine.policy.set_execution_state_unsafe('TEST_UNSAFE')
        _, result, entry = self.cycle()
        self.assertEqual(result['execution_status'],'HALTED_FOR_SAFETY')
        self.assertEqual(result['exit_check_status'],'NOT_EVALUATED')
        entry.assert_not_called()


if __name__ == '__main__':
    unittest.main()
