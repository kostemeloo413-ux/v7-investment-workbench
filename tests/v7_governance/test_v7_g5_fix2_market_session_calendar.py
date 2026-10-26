# -*- coding: utf-8 -*-
"""G5 FIX2 deterministic A-share calendar and runtime session gate tests."""

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

import runtime_config as rc
from daemon import trading_daemon as daemon


TZ = datetime.timezone(datetime.timedelta(hours=8))


def aware(value):
    return datetime.datetime.fromisoformat(value).replace(tzinfo=TZ)


class G5Fix2MarketSessionCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v7_g5_fix2_calendar_')
        self.calendar_path = Path(self.tmp.name) / 'calendar.json'
        shutil.copyfile(rc.A_SHARE_TRADING_CALENDAR_JSON, self.calendar_path)
        self.schedule = {
            'jobs': {
                'close_settlement': {
                    'trigger': '8 15 * * 1-5',
                    'skip_stale_minutes': 240,
                }
            }
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _calendar(self):
        return json.loads(self.calendar_path.read_text(encoding='utf-8'))

    def _write_calendar(self, value):
        self.calendar_path.write_text(json.dumps(value), encoding='utf-8')

    def _direct(self, timestamp, **kwargs):
        import simulated_trading as sim
        with patch.object(sim, '_execution_local_timestamp', return_value=aware(timestamp)):
            return sim.execute_intraday(force=True, **kwargs)

    def _session(self, job, stamp):
        return daemon.runtime_market_session_preflight(
            job, current_timestamp=aware(stamp),
            calendar_path=str(self.calendar_path), schedule=self.schedule)

    def _execute(self, job, stamp, business=None, source='test'):
        business = business or MagicMock(return_value='ok')
        economic_pass = {
            'status': 'PASS', 'pass': True,
            'season_id': 'V7-SEASON-20260917-V1',
            'execution_state_safe': True, 'findings': [],
        }
        with patch.object(daemon, 'startup_runtime_gate', return_value=economic_pass), \
             patch.object(daemon, 'run_with_guard', side_effect=lambda n, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job(
                job, business, source=source,
                current_timestamp=aware(stamp),
                calendar_path=str(self.calendar_path), schedule=self.schedule)
        return result, business

    # T1-T10: exchange calendar and strict reader
    def test_t01_valid_normal_weekday(self):
        result = rc.trading_calendar_status(datetime.date(2026, 9, 21), str(self.calendar_path))
        self.assertEqual(result['status'], rc.TradingCalendarStatus.VALID)
        self.assertTrue(result['trading_day'])

    def test_t02_saturday_closed(self):
        self.assertFalse(rc.is_trading_day(datetime.date(2026, 9, 19)))

    def test_t03_sunday_closed(self):
        self.assertFalse(rc.is_trading_day(datetime.date(2026, 9, 20)))

    def test_t04_official_weekday_holiday_closed(self):
        self.assertFalse(rc.trading_calendar_status(datetime.date(2026, 9, 25), str(self.calendar_path))['trading_day'])

    def test_t05_reopen_weekday_open(self):
        self.assertTrue(rc.trading_calendar_status(datetime.date(2026, 9, 28), str(self.calendar_path))['trading_day'])

    def test_t06_government_workday_cannot_open_saturday(self):
        with patch.object(rc, 'load_holidays', return_value=(set(), {'2026-10-10'})):
            self.assertFalse(rc.is_trading_day(datetime.date(2026, 10, 10)))

    def test_t07_missing_calendar_fail_closed(self):
        result = rc.trading_calendar_status(datetime.date(2026, 9, 21), str(Path(self.tmp.name) / 'missing.json'))
        self.assertEqual(result['status'], rc.TradingCalendarStatus.MISSING)
        self.assertFalse(result['trading_day'])

    def test_t08_malformed_calendar_fail_closed(self):
        self.calendar_path.write_text('{', encoding='utf-8')
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_INVALID'])

    def test_t09_wrong_schema_fail_closed(self):
        value = self._calendar(); value['schema_version'] = 'v2'; self._write_calendar(value)
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_SCHEMA_ERROR'])

    def test_t10_invalid_date_fail_closed(self):
        value = self._calendar(); value['closed_dates'].append('2026-02-30'); self._write_calendar(value)
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_DATE_INVALID'])

    def test_t10b_wrong_year_fail_closed(self):
        value = self._calendar(); value['year'] = 2027; self._write_calendar(value)
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_YEAR_UNAVAILABLE'])

    def test_t10c_duplicate_date_fail_closed(self):
        value = self._calendar(); value['closed_dates'].append(value['closed_dates'][0]); self._write_calendar(value)
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_DATE_LIST_INVALID'])

    def test_t10d_conflicting_date_fail_closed(self):
        value = self._calendar(); value['exception_open_dates'].append(value['closed_dates'][0]); self._write_calendar(value)
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_DATE_CONFLICT'])

    def test_t10e_wrong_field_type_fail_closed(self):
        value = self._calendar(); value['closed_dates'] = '2026-09-25'; self._write_calendar(value)
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['TRADING_CALENDAR_SCHEMA_ERROR'])

    # T11-T20: session policy
    def test_t11_intraday_valid_trading_day_allowed(self):
        result = self._session('intraday_cycle', '2026-09-21T10:00:00')
        self.assertTrue(result['pass']); self.assertEqual(result['session'], 'OPEN')

    def test_t12_intraday_saturday_hold(self):
        result = self._session('intraday_cycle', '2026-09-19T10:00:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['NON_TRADING_DAY'])

    def test_t13_intraday_holiday_hold(self):
        result = self._session('intraday_cycle', '2026-09-25T10:00:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['NON_TRADING_DAY'])

    def test_t14_intraday_preopen_hold(self):
        result = self._session('intraday_cycle', '2026-09-21T09:00:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['OUTSIDE_INTRADAY_SESSION'])

    def test_t15_intraday_lunch_hold(self):
        result = self._session('intraday_cycle', '2026-09-21T12:00:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['OUTSIDE_INTRADAY_SESSION'])

    def test_t16_intraday_after_close_hold(self):
        result = self._session('intraday_cycle', '2026-09-21T15:01:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['OUTSIDE_INTRADAY_SESSION'])

    def test_t17_close_valid_slot_allowed(self):
        result = self._session('close_settlement', '2026-09-21T15:08:00')
        self.assertTrue(result['pass'])

    def test_t18_close_before_slot_hold(self):
        result = self._session('close_settlement', '2026-09-21T15:07:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['BEFORE_CLOSE_SETTLEMENT_WINDOW'])

    def test_t19_close_nontrading_day_hold(self):
        result = self._session('close_settlement', '2026-09-20T15:08:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['NON_TRADING_DAY'])

    def test_t20_close_after_stale_window_hold(self):
        result = self._session('close_settlement', '2026-09-21T19:09:00')
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['CLOSE_SETTLEMENT_STALE'])

    # T21-T30: canonical entry points and fail closed behavior
    def test_t21_scheduler_nontrading_day_business_zero(self):
        result, business = self._execute('intraday_cycle', '2026-09-19T10:00:00', source='scheduler')
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t22_once_nontrading_day_business_zero(self):
        result, business = self._execute('intraday_cycle', '2026-09-19T10:00:00', source='once')
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t23_api_nontrading_day_business_zero(self):
        from daemon import app
        business = MagicMock(return_value='should-not-run')
        fail = {'status': 'HOLD', 'pass': False, 'reason_codes': ['NON_TRADING_DAY']}
        class ImmediateThread:
            def __init__(self, target, daemon=True): self.target = target
            def start(self): self.target()
        with patch.dict(daemon.BUILTINS, {'intraday_cycle': business}), \
             patch.object(app.threading, 'Thread', ImmediateThread), \
             patch.object(daemon, 'runtime_market_session_preflight', return_value=fail), \
             patch.object(daemon, 'write_heartbeat'):
            result = app._trigger_job('intraday_cycle')
        self.assertEqual(result['status'], 'QUEUED_FOR_RUNTIME_GATE'); business.assert_not_called()

    def test_t24_catchup_close_nontrading_day_business_zero(self):
        result, business = self._execute('close_settlement', '2026-09-20T15:08:00', source='catchup:close_settlement')
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()

    def test_t25_once_valid_intraday_economic_gate_once(self):
        economic = {'status': 'PASS', 'pass': True, 'findings': [], 'season_id': 'V7-SEASON-20260917-V1'}
        business = MagicMock(return_value='ok')
        with patch.object(daemon, 'startup_runtime_gate', return_value=economic) as gate, \
             patch.object(daemon, 'run_with_guard', side_effect=lambda n, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', business, source='once',
                                                current_timestamp=aware('2026-09-21T10:00:00'),
                                                calendar_path=str(self.calendar_path), schedule=self.schedule)
        self.assertEqual(result['status'], 'PASS'); gate.assert_called_once_with(update_safety=True)

    def test_t26_api_valid_intraday_economic_gate_once(self):
        economic = {'status': 'PASS', 'pass': True, 'findings': [], 'season_id': 'V7-SEASON-20260917-V1'}
        business = MagicMock(return_value='ok')
        with patch.object(daemon, 'startup_runtime_gate', return_value=economic) as gate, \
             patch.object(daemon, 'run_with_guard', side_effect=lambda n, fn: (True, fn())), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job(
                'intraday_cycle', business, source='api',
                current_timestamp=aware('2026-09-21T10:00:00'),
                calendar_path=str(self.calendar_path), schedule=self.schedule)
        self.assertEqual(result['status'], 'PASS'); gate.assert_called_once_with(update_safety=True)

    def test_t27_remote_api_remains_403(self):
        from daemon.app import create_app
        app = create_app(); route = next(r for r in app.routes if getattr(r, 'path', None) == '/api/run/{job}')
        request = MagicMock(); request.client = MagicMock(host='203.0.113.8')
        with self.assertRaises(Exception) as raised: route.endpoint('intraday_cycle', request)
        self.assertEqual(getattr(raised.exception, 'status_code', None), 403)

    def test_t28_market_session_exception_fail_closes(self):
        business = MagicMock()
        with patch.object(daemon, 'runtime_market_session_preflight', side_effect=RuntimeError('calendar')), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', business, current_timestamp=aware('2026-09-21T10:00:00'))
        self.assertEqual(result['status'], 'HOLD'); business.assert_not_called()
        self.assertIn('RUNTIME_GATE_EXCEPTION:RuntimeError', result['reason_codes'])

    def test_t29_market_session_malformed_verdict_fail_closes(self):
        business = MagicMock()
        with patch.object(daemon, 'runtime_market_session_preflight', return_value={'pass': 'yes'}), \
             patch.object(daemon, 'write_heartbeat'):
            result = daemon.execute_runtime_job('intraday_cycle', business, current_timestamp=aware('2026-09-21T10:00:00'))
        self.assertEqual(result['status'], 'HOLD'); self.assertEqual(result['reason_codes'], ['MARKET_SESSION_GATE_MALFORMED'])

    def test_t30_unknown_economic_session_policy_fail_closes(self):
        result = daemon.runtime_market_session_preflight('unknown_economic_job', aware('2026-09-21T10:00:00'), str(self.calendar_path), self.schedule)
        self.assertFalse(result['pass']); self.assertEqual(result['reason_codes'], ['SESSION_POLICY_UNKNOWN'])

    # T31-T34: direct business boundary defense
    def test_t31_direct_intraday_saturday_blocked(self):
        result = self._direct('2026-09-19T10:00:00')
        self.assertIn('禁止', result)

    def test_t32_direct_intraday_holiday_blocked(self):
        result = self._direct('2026-09-25T10:00:00')
        self.assertIn('禁止', result)

    def test_t33_direct_intraday_valid_day_window_compatible(self):
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

    def test_t34_force_cannot_bypass_exchange_closed_day(self):
        result = self._direct('2026-09-19T10:00:00')
        self.assertIn('非A股交易日', result)

    def test_t35_naive_runtime_timestamp_fail_closed(self):
        result = daemon.runtime_market_session_preflight(
            'intraday_cycle', current_timestamp=datetime.datetime(2026, 9, 21, 10, 0),
            calendar_path=str(self.calendar_path), schedule=self.schedule)
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason_codes'], ['INVALID_TIMEZONE_AWARE_TIMESTAMP'])

    def test_t36_official_2026_calendar_matrix(self):
        expected_closed = {
            '2026-01-01', '2026-01-02', '2026-02-16', '2026-02-23',
            '2026-02-28', '2026-04-06', '2026-05-01', '2026-05-05',
            '2026-05-09', '2026-06-19', '2026-09-19', '2026-09-20',
            '2026-09-25', '2026-10-01', '2026-10-07', '2026-10-10',
        }
        expected_open = {
            '2026-01-05', '2026-02-24', '2026-04-07', '2026-05-06',
            '2026-06-22', '2026-09-21', '2026-09-28', '2026-10-08',
        }
        for value in expected_closed:
            self.assertFalse(rc.trading_calendar_status(datetime.date.fromisoformat(value), str(self.calendar_path))['trading_day'], value)
        for value in expected_open:
            self.assertTrue(rc.trading_calendar_status(datetime.date.fromisoformat(value), str(self.calendar_path))['trading_day'], value)


if __name__ == '__main__':
    unittest.main()
