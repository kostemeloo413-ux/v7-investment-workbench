# -*- coding: utf-8 -*-
"""Isolated regression tests for G6-R3B daily_review datetime scoping.

The suite never opens the official market database, starts a scheduler, or
calls a provider.  Every writable path belongs to a TemporaryDirectory.
"""

import datetime as std_datetime
import inspect
import json
import os
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPTS = os.path.join(ROOT, 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from daemon import trading_daemon as daemon


class FixedFridayDateTime(std_datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 25, 18, 0, 0)  # Friday in Asia/Shanghai.
        return value.replace(tzinfo=tz) if tz is not None else value


class DailyReviewDatetimeScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v7_g6_r3b_datetime_')
        self.root = self.temp.name
        self.data_dir = os.path.join(self.root, 'data')
        self.season_root = os.path.join(self.root, 'season')
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.season_root, exist_ok=True)
        self.universe_path = os.path.join(self.data_dir, 'stock_universe_mainboard.json')
        with open(self.universe_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'updated_at': '2026-09-22T16:00:00+08:00',
                'total_count': 2,
                'stocks': [{'code': '000001'}, {'code': '000002'}],
            }, handle)
        self.context = {'season_id': 'TEST-SEASON', 'season_root': self.season_root}
        self.sync = Mock(return_value={'status': 'PASS', 'failure_reasons': []})
        self.coverage = Mock(return_value=(2, '20260922', '20260922'))
        self.resolver = Mock(return_value=self.context)
        self.patches = [
            patch.object(daemon.rc, 'DATA_DIR', self.data_dir),
            patch.object(daemon.rc, 'today_str', return_value='2026-09-22'),
            patch.object(daemon.rc, 'is_trading_day', return_value=True),
            patch.object(daemon, 'BASE', self.root, create=True),
            patch('datasource.providers.sync_universe_daily', self.sync),
            patch('datasource.db.kline_coverage', self.coverage),
            patch('competition.season_runtime.resolve_active_season_context', self.resolver),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _fake_analysis_modules(self):
        analysis = types.ModuleType('analysis')
        analysis.__path__ = []
        factors = types.ModuleType('analysis.event_factors')
        factors.pos_neg_diff = lambda: None
        audit = types.ModuleType('analysis.factor_weight_audit')
        audit.run_factor_weight_audit = lambda: {'status': 'PASS'}
        checkup = types.ModuleType('analysis.system_checkup')
        checkup.generate_system_checkup = lambda: {'status': 'PASS'}
        notify = types.ModuleType('notify')
        notify.__path__ = []
        bot = types.ModuleType('notify.wecom_bot')
        bot.daily_brief = Mock()
        return {
            'analysis': analysis,
            'analysis.event_factors': factors,
            'analysis.factor_weight_audit': audit,
            'analysis.system_checkup': checkup,
            'notify': notify,
            'notify.wecom_bot': bot,
        }, bot

    def _make_factor_db(self):
        import sqlite3

        db_path = os.path.join(self.root, 'data', 'market', 'market.db')
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute('CREATE TABLE event_factor(published_at TEXT)')
            conn.execute("INSERT INTO event_factor VALUES ('2026-09-10 09:00:00')")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_t01_scope_has_no_local_datetime_import_and_uses_module_namespace(self):
        source = inspect.getsource(daemon.daily_review)
        self.assertNotIn('from datetime import datetime', source)
        self.assertIn("datetime.datetime.strptime(target_trade_date, '%Y%m%d')", source)
        self.assertNotRegex(source, r'(?<!\.)\bdatetime\.now\(')
        self.assertNotRegex(source, r'(?<!\.)\bdatetime\.strptime\(')

    def test_t02_non_trading_path_parses_yyyymmdd_without_unboundlocalerror(self):
        health = Mock(return_value=(os.path.join(self.root, 'health.json'), {'status': 'HOLD'}))
        with patch.object(daemon.rc, 'is_trading_day', return_value=False), \
             patch.object(daemon, '_record_data_job_health', health):
            result = daemon.daily_review()
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(result['reason_codes'], ['NON_TRADING_DATE'])
        self.assertEqual(health.call_args.args[1], '20260922')
        self.sync.assert_not_called()

    def test_t03_pass_like_path_runs_target_parse_snapshot_resolver_and_completeness(self):
        modules, _ = self._fake_analysis_modules()
        with patch.dict(sys.modules, modules):
            result = daemon.daily_review()
        self.assertEqual(result['status'], 'PASS')
        self.resolver.assert_called_once()
        self.sync.assert_called_once()
        self.assertEqual(self.sync.call_args.kwargs['target_trade_date'], '20260922')
        self.assertEqual(self.sync.call_args.args[0], ['000001', '000002'])
        self.coverage.assert_called_once()
        snapshot_path = os.path.join(self.season_root, 'market_data_completeness', 'universe_snapshots', '20260922.json')
        self.assertTrue(os.path.exists(snapshot_path))

    def test_t04_factor_datetime_now_and_friday_calculation_keep_semantics_in_temp_db(self):
        self._make_factor_db()
        modules, bot = self._fake_analysis_modules()
        with patch.dict(sys.modules, modules), \
             patch.object(daemon.datetime, 'datetime', FixedFridayDateTime):
            result = daemon.daily_review()
        self.assertEqual(result['status'], 'PASS')
        baseline_path = os.path.join(self.root, 'state', 'factor_baseline.json')
        with open(baseline_path, encoding='utf-8') as handle:
            baseline = json.load(handle)
        self.assertEqual(baseline['count'], 1)
        self.assertEqual(baseline['last_at'], '2026-09-25 18:00:00')
        bot.daily_brief.assert_called_once()
        self.assertIn('事件因子存量1条', result['messages'][1])


if __name__ == '__main__':
    unittest.main()
