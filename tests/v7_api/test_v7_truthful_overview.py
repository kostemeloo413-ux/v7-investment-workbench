# -*- coding: utf-8 -*-
"""Isolated fail-closed tests for the UI A1 overview View Model."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))
sys.path.insert(0, os.path.join(_BASE, 'scripts', 'daemon'))

from daemon import v7_api


class TestTruthfulOverview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v7_ui_a1_')
        self.root = self.temp.name
        self.forward_root = os.path.join(self.root, 'forward')
        self.paper_root = os.path.join(self.root, 'paper')
        os.makedirs(self.forward_root)
        os.makedirs(self.paper_root)
        self.gov_path = os.path.join(self.root, 'governance_halt.json')
        self.db_path = os.path.join(self.root, 'isolated.db')
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('CREATE TABLE ai_quant_predictions (symbol TEXT, name TEXT, rank INTEGER, raw_score REAL, up_probability REAL, expected_return REAL, expected_mae REAL, right_side_score REAL, expected_holding_days INTEGER, predict_time TEXT, trade_date TEXT, id INTEGER)')
        finally:
            conn.close()
        self._write(os.path.join(self.forward_root, 'forward_epoch_manifest.json'), {
            'season_id': 'TEST-SEASON', 'forward_epoch_id': 'TEST-EPOCH',
            'status': 'INITIALIZED_HOLD', 'forward_day_count': 0,
            'forward_enabled': False, 'genuine_forward_started': False,
            'required_gates': {'G6_MARKET_COMPLETENESS': {'status': 'OPEN'}},
        })
        self._write(os.path.join(self.paper_root, 'account.json'), {
            'account_id': 'AIQ-PAPER-TEST', 'initial_capital': 100000,
            'total_equity': 100000, 'cash': 100000, 'market_value': 0,
            'net_pnl': 0, 'net_pnl_pct': 0, 'max_drawdown_pct': 0,
            'max_positions': 3, 'max_single_position_ratio': .3,
        })
        self._write(os.path.join(self.paper_root, 'positions.json'), [])
        self._write(self.gov_path, {
            'execution_state_safe': True, 'new_entry_disabled': True,
            'risk_exit_enabled': True, 'trading_enabled': False,
            'real_trading': False, 'halt_reasons': [],
        })
        self.context = {
            'season_id': 'TEST-SEASON', 'season_root': self.root,
            'forward_root': self.forward_root, 'ai_quant_paper_root': self.paper_root,
            'governance_safety_path': self.gov_path,
            'manifest_path': os.path.join(self.root, 'season_manifest.json'),
            'manifest': {'season_id': 'TEST-SEASON', 'status': 'RUNTIME_FOUNDATION_VALIDATED_HOLD'},
        }
        self.patches = [
            patch.object(v7_api, '_active_season_context_or_warning', side_effect=lambda w: self.context),
            patch.object(v7_api, 'MARKET_DB_PATH', self.db_path),
            patch.object(v7_api, '_load_json_cached', side_effect=self._cached_state),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _write(self, path, data):
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)

    @staticmethod
    def _cached_state(path, default=None):
        if path == v7_api.rc.ALIVE_PATH:
            return {}
        if path == v7_api.HEARTBEAT_PATH:
            return {'jobs': {}}
        return default

    def test_hold_day_zero_and_g6_open_are_read_from_manifest(self):
        payload = v7_api.get_v7_overview()['data']
        self.assertEqual(payload['forward_epoch']['status'], 'INITIALIZED_HOLD')
        self.assertEqual(payload['forward_epoch']['forward_day_count'], 0)
        self.assertFalse(payload['forward_epoch']['forward_enabled'])
        self.assertEqual(payload['system_control']['g6_status'], 'OPEN')
        self.assertTrue(payload['system_control']['safety']['new_entry_disabled'])

    def test_missing_forward_manifest_is_unavailable_not_day_zero(self):
        os.remove(os.path.join(self.forward_root, 'forward_epoch_manifest.json'))
        payload = v7_api.get_v7_overview()['data']
        self.assertIsNone(payload['forward_epoch']['forward_day_count'])
        self.assertEqual(payload['forward_epoch']['status'], 'UNAVAILABLE')
        self.assertEqual(payload['system_control']['g6_status'], 'UNKNOWN')

    def test_damaged_governance_does_not_default_to_no_halt_or_trading_off(self):
        with open(self.gov_path, 'w', encoding='utf-8') as handle:
            handle.write('{broken')
        payload = v7_api.get_v7_overview()['data']
        self.assertEqual(payload['system_control']['halt_status'], 'UNKNOWN')
        self.assertIsNone(payload['system_control']['real_trading'])
        self.assertIsNone(payload['system_control']['safety']['trading_enabled'])

    def test_missing_aiq_account_does_not_become_zero_or_personal_assets(self):
        os.remove(os.path.join(self.paper_root, 'account.json'))
        payload = v7_api.get_v7_overview()['data']
        self.assertIsNone(payload['account_matrix']['total_equity'])
        self.assertEqual(payload['personal_portfolio']['reason_code'], 'PERSONAL_PORTFOLIO_UNAVAILABLE')
        self.assertIsNone(payload['account_matrix']['daily_pnl'])

    def test_health_is_not_green_from_process_liveness_alone(self):
        with patch.object(v7_api.time, 'time', return_value=1000.0):
            result = v7_api._business_health_view({'epoch': 995}, {'jobs': {}}, [])
        self.assertEqual(result['daemon_liveness'], 'LIVE')
        self.assertEqual(result['business_status'], 'UNKNOWN')

    def test_stale_heartbeat_and_failed_job_are_not_healthy(self):
        with patch.object(v7_api.time, 'time', return_value=2000.0):
            stale = v7_api._business_health_view({'epoch': 1000}, {'jobs': {}}, [])
            failed = v7_api._business_health_view(
                {'epoch': 1995}, {'jobs': {'daily_review': {'result': 'FAILED: test'}}}, [])
        self.assertEqual(stale['business_status'], 'STALE')
        self.assertEqual(failed['business_status'], 'DEGRADED')


if __name__ == '__main__':
    unittest.main()
