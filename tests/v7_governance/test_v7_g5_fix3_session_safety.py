# -*- coding: utf-8 -*-
"""G5 FIX3: strict session verdict and direct force boundary tests.

All business callables and all direct execution state are isolated.  No live
runtime job, market sync, BUY, or SELL is invoked by this module.
"""

import datetime
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for item in (ROOT, ROOT / 'scripts'):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from daemon import trading_daemon as daemon
import runtime_config as rc


def aware(value):
    return datetime.datetime.fromisoformat(value).replace(
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


class G5Fix3SessionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.schedule = {
            'jobs': {
                'close_settlement': {
                    'trigger': '8 15 * * 1-5',
                    'skip_stale_minutes': 240,
                }
            }
        }
        self.tmp = tempfile.TemporaryDirectory(prefix='v7_g5_fix3_')
        self.temp_calendar = Path(self.tmp.name) / 'calendar.json'
        shutil.copyfile(rc.A_SHARE_TRADING_CALENDAR_JSON, self.temp_calendar)
        self.addCleanup(self.tmp.cleanup)

    def _valid(self, job='intraday_cycle', timestamp=None):
        timestamp = timestamp or (
            '2026-09-21T15:08:00+08:00'
            if job == 'close_settlement' else '2026-09-21T10:00:00+08:00')
        verdict = {
            'job': job,
            'market': 'A_SHARE',
            'timezone': 'Asia/Shanghai',
            'classification': 'ECONOMIC_MUTATING',
            'preflight_domain': 'MARKET_SESSION',
            'allowed': True,
            'pass': True,
            'status': 'PASS',
            'calendar_valid': True,
            'trading_day': True,
            'timestamp': timestamp,
            'trade_date': timestamp[:10],
            'session': 'CLOSED' if job == 'close_settlement' else 'OPEN',
            'reason_codes': [],
        }
        if job == 'close_settlement':
            verdict['close_slot'] = '2026-09-21T15:08:00+08:00'
            verdict['close_stale_end'] = '2026-09-21T19:08:00+08:00'
        return verdict

    def _execute_with_verdict(self, verdict, job='intraday_cycle', timestamp=None):
        business = MagicMock(return_value='not-run')
        with patch.object(daemon, 'runtime_market_session_preflight',
                          return_value=verdict), \
             patch.object(daemon, 'runtime_job_preflight',
                          return_value=(True, {'status': 'PASS', 'pass': True})), \
             patch.object(daemon, 'run_with_guard',
                          side_effect=lambda name, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job(
                job, business, source='fix3-test',
                current_timestamp=timestamp,
                schedule=self.schedule)
        return result, business

    def _direct(self, timestamp, **kwargs):
        import simulated_trading as sim
        with patch.object(sim, '_execution_local_timestamp', return_value=aware(timestamp)):
            return sim.execute_intraday(force=True, **kwargs)

    def test_t01_pass_true_only_holds(self):
        result, business = self._execute_with_verdict({'pass': True})
        self.assertEqual(result['status'], 'HOLD')
        business.assert_not_called()

    def test_t02_allowed_false_holds(self):
        verdict = self._valid(); verdict['allowed'] = False
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t03_status_hold_holds(self):
        verdict = self._valid(); verdict['status'] = 'HOLD'
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t04_calendar_invalid_holds(self):
        verdict = self._valid(); verdict['calendar_valid'] = False
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t05_not_trading_day_holds(self):
        verdict = self._valid(); verdict['trading_day'] = False
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t06_wrong_job_holds(self):
        verdict = self._valid('close_settlement')
        result, business = self._execute_with_verdict(
            verdict, job='intraday_cycle', timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t07_wrong_classification_holds(self):
        verdict = self._valid(); verdict['classification'] = 'DATA_MUTATING'
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t08_wrong_session_holds(self):
        verdict = self._valid(); verdict['session'] = 'CLOSED'
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t09_wrong_timezone_holds(self):
        verdict = self._valid(); verdict['timezone'] = 'UTC'
        result, business = self._execute_with_verdict(verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t10_invalid_timestamp_holds(self):
        verdict = self._valid(); verdict['timestamp'] = 'not-a-time'
        result, business = self._execute_with_verdict(verdict)
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t11_malformed_close_window_holds(self):
        verdict = self._valid('close_settlement')
        verdict['close_stale_end'] = 'not-a-time'
        result, business = self._execute_with_verdict(
            verdict, job='close_settlement', timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t12_complete_intraday_verdict_allows_business(self):
        verdict = self._valid()
        result, business = self._execute_with_verdict(
            verdict, timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'PASS'); business.assert_called_once_with()

    def test_t13_complete_close_verdict_allows_business(self):
        verdict = self._valid('close_settlement')
        result, business = self._execute_with_verdict(
            verdict, job='close_settlement', timestamp=aware(verdict['timestamp']))
        self.assertEqual(result['status'], 'PASS'); business.assert_called_once_with()

    def test_t14_saturday_force_blocked(self):
        result = self._direct('2026-09-19T10:00:00')
        self.assertIn('非A股交易日', result)

    def test_t15_holiday_force_blocked(self):
        result = self._direct('2026-09-25T10:00:00')
        self.assertIn('非A股交易日', result)

    def test_t16_preopen_force_blocked(self):
        result = self._direct('2026-09-21T08:30:00')
        self.assertIn('非交易窗口', result)

    def test_t17_lunch_force_blocked(self):
        result = self._direct('2026-09-21T12:00:00')
        self.assertIn('非交易窗口', result)

    def test_t18_after_close_force_blocked(self):
        result = self._direct('2026-09-21T20:00:00')
        self.assertIn('非交易窗口', result)

    def test_t19_noncanonical_calendar_cannot_be_force_injected(self):
        result = self._direct('2026-09-21T10:00:00', calendar_path=str(self.temp_calendar))
        self.assertIn('canonical', result)

    def test_t20_valid_intraday_force_remains_compatible(self):
        import simulated_trading as sim
        with patch.object(sim, '_acquire_trade_lock', return_value=MagicMock()), \
             patch.object(sim, 'load_accounts_from_ledgers', return_value={}), \
             patch.object(sim, 'fetch_prices', return_value={}), \
             patch.object(sim, '_save_intraday_executed'), \
             patch.object(sim, '_save_position_highs'), \
             patch.object(sim, 'run_simulation', return_value=None):
            with patch.object(sim, '_execution_local_timestamp', return_value=aware('2026-09-21T10:00:00')):
                result = sim.execute_intraday(force=True)
        self.assertIn('盘中执行', result)

    def test_t21_external_clock_injection_is_blocked(self):
        from simulated_trading import execute_intraday
        result = execute_intraday(
            force=True, current_timestamp=aware('2026-09-21T10:00:00'))
        self.assertIn('禁止外部时钟注入', result)


if __name__ == '__main__':
    unittest.main()
