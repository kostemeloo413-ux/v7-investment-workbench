# -*- coding: utf-8 -*-
"""G6 Phase2 FIX1A: structured DATA_MUTATING HOLD heartbeat semantics."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPTS = os.path.join(ROOT, 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from daemon import trading_daemon as daemon


class HoldHeartbeatSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v7_g6_fix1a_')
        self.heartbeat_path = os.path.join(self.temp.name, 'heartbeat.json')
        self.failure_path = os.path.join(self.temp.name, 'job_failures.json')
        self.patches = [
            patch.object(daemon.rc, 'HEARTBEAT_PATH', self.heartbeat_path),
            patch.object(daemon, 'FAIL_STATE_PATH', self.failure_path),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _read_json(self, path):
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)

    def _hold(self):
        return {'status': 'HOLD', 'reason_codes': ['G6_COMPLETENESS_HOLD']}

    def _pass_gate(self, job='daily_review'):
        return True, {
            'status': 'PASS', 'pass': True, 'job': job,
            'classification': daemon.classify_runtime_job(job),
            'preflight_domain': 'DATA' if job == 'daily_review' else 'READ_ONLY',
        }

    def test_t01_hold_has_no_ordinary_success_log(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ok, detail = daemon.run_with_guard(
                'daily_review', self._hold, structured_hold=True)
        self.assertTrue(ok)
        self.assertEqual(detail['status'], 'HOLD')
        self.assertNotIn('✔ daily_review 完成', output.getvalue())
        self.assertIn('daily_review HOLD', output.getvalue())

    def test_t02_hold_has_no_ordinary_success_heartbeat(self):
        daemon.run_with_guard('daily_review', self._hold, structured_hold=True)
        heartbeat = self._read_json(self.heartbeat_path)
        self.assertNotIn('daily_review', heartbeat.get('jobs', {}))
        self.assertIn('daily_review:hold', heartbeat.get('jobs', {}))
        self.assertIn('HOLD', heartbeat['jobs']['daily_review:hold']['result'])

    def test_t03_hold_preserves_existing_failure_counter(self):
        with open(self.failure_path, 'w', encoding='utf-8') as handle:
            json.dump({'daily_review': {'count': 2, 'last_error': 'prior'}}, handle)
        daemon.run_with_guard('daily_review', self._hold, structured_hold=True)
        failures = self._read_json(self.failure_path)
        self.assertEqual(failures['daily_review']['count'], 2)
        self.assertEqual(failures['daily_review']['last_error'], 'prior')

    def test_t04_execute_runtime_job_returns_hold_after_real_guard(self):
        business = MagicMock(return_value=self._hold())
        with patch.object(daemon, 'data_runtime_preflight', return_value=self._pass_gate()):
            result = daemon.execute_runtime_job('daily_review', business, source='fix1a-test')
        self.assertEqual(result['status'], 'HOLD')
        self.assertTrue(result['allowed'])
        self.assertTrue(result['business_job_called'])
        self.assertEqual(result['reason_codes'], ['G6_COMPLETENESS_HOLD'])
        business.assert_called_once()

    def test_t05_normal_daily_review_pass_keeps_success_semantics(self):
        business = MagicMock(return_value='data-pass')
        with patch.object(daemon, 'data_runtime_preflight', return_value=self._pass_gate()):
            result = daemon.execute_runtime_job('daily_review', business, source='fix1a-test')
        self.assertEqual(result['status'], 'PASS')
        heartbeat = self._read_json(self.heartbeat_path)
        self.assertIn('daily_review', heartbeat.get('jobs', {}))

    def test_t06_informational_task_is_unchanged(self):
        business = MagicMock(return_value='news-pass')
        result = daemon.execute_runtime_job('news_refresh', business, source='fix1a-test')
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['allowed'])
        business.assert_called_once()

    def test_t07_economic_authority_still_uses_startup_gate(self):
        startup = MagicMock(return_value={'pass': True, 'status': 'PASS'})
        with patch.object(daemon, 'startup_runtime_gate', startup):
            ok, detail = daemon.runtime_job_preflight('intraday_cycle')
        self.assertTrue(ok)
        self.assertEqual(detail['status'], 'PASS')
        startup.assert_called_once_with(update_safety=True)

    def test_t08_hold_health_evidence_and_alert_remain_available(self):
        context = {'season_root': self.temp.name, 'season_id': 'TEST-SEASON'}
        with patch('notify.wecom_bot.push') as alert:
            path, health = daemon._record_data_job_health(
                context, '20260920', self._hold(), ['G6_COMPLETENESS_HOLD'])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self._read_json(path)['status'], 'HOLD')
        self.assertEqual(health['economic_execution_impact'], 'NONE')
        alert.assert_called_once()

    def test_t09_alert_failure_cannot_create_false_pass(self):
        context = {'season_root': self.temp.name, 'season_id': 'TEST-SEASON'}
        with patch('notify.wecom_bot.push', side_effect=RuntimeError('notify down')):
            path, health = daemon._record_data_job_health(
                context, '20260920', self._hold(), ['G6_COMPLETENESS_HOLD'])
        self.assertEqual(health['status'], 'HOLD')
        self.assertTrue(os.path.exists(path))
        self.assertNotEqual(health['status'], 'PASS')

    def test_t10_repeated_hold_has_no_false_success_record(self):
        with open(self.failure_path, 'w', encoding='utf-8') as handle:
            json.dump({'daily_review': {'count': 1}}, handle)
        first = daemon.run_with_guard('daily_review', self._hold, structured_hold=True)
        second = daemon.run_with_guard('daily_review', self._hold, structured_hold=True)
        heartbeat = self._read_json(self.heartbeat_path)
        failures = self._read_json(self.failure_path)
        self.assertEqual(first[1]['status'], 'HOLD')
        self.assertEqual(second[1]['status'], 'HOLD')
        self.assertNotIn('daily_review', heartbeat.get('jobs', {}))
        self.assertEqual(failures['daily_review']['count'], 1)


if __name__ == '__main__':
    unittest.main()
