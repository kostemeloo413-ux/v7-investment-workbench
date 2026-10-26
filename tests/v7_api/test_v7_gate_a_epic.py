# -*- coding: utf-8 -*-
"""Gate A UI epic API/security regressions; all writes are to temporary fixtures only."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
sys.path.insert(0, str(BASE / 'scripts' / 'daemon'))
from app import create_app
from daemon import v7_api


class TestGateAEpicReadOnlyPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'market.db'
        connection = sqlite3.connect(self.db)
        connection.executescript('''
            CREATE TABLE daily_kline(code TEXT, trade_date TEXT);
            CREATE TABLE instrument_master(symbol TEXT, name TEXT);
            CREATE TABLE ai_quant_predictions(trade_date TEXT, predict_time TEXT, model_version TEXT, rank INTEGER);
            CREATE TABLE ai_quant_trade_plans(plan_id TEXT);
        ''')
        connection.close()
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_readonly_data_center_reports_metadata_not_completeness(self):
        before = self.db.stat().st_mtime_ns
        with patch.object(v7_api, 'MARKET_DB_PATH', str(self.db)):
            response = self.client.get('/api/v7/data-center')
        self.assertEqual(response.status_code, 200)
        payload = response.json()['data']
        self.assertEqual(payload['database']['sqlite_mode'], 'READ_ONLY')
        self.assertEqual(payload['database']['statistics_status'], 'SCHEMA_READ_ONLY; ROW_COUNTS_NOT_QUERIED')
        self.assertEqual(payload['completeness_claim'], 'NOT_CLAIMED')
        self.assertEqual(payload['g6_status'], 'OPEN')
        self.assertEqual(self.db.stat().st_mtime_ns, before)

    def test_system_pages_do_not_return_llm_or_wecom_secrets(self):
        sentinel_secret = 'DO_NOT_LEAK_FAKE_SECRET_8F7C'
        originals = (v7_api.rc.LLM_API_KEY, v7_api.rc.LLM_MODEL, v7_api.rc.WECOM_WEBHOOK_URL)
        try:
            v7_api.rc.LLM_API_KEY = sentinel_secret
            v7_api.rc.LLM_MODEL = 'deepseek-chat'
            v7_api.rc.WECOM_WEBHOOK_URL = 'https://example.invalid/' + sentinel_secret
            payloads = [self.client.get('/api/v7/integrations/llm').json(),
                        self.client.get('/api/v7/integrations/wecom').json()]
            encoded = json.dumps(payloads)
            self.assertNotIn(sentinel_secret, encoded)
            self.assertTrue(payloads[0]['data']['configured'])
            self.assertTrue(payloads[1]['data']['configured'])
            self.assertFalse(payloads[0]['data']['secret_returned'])
            self.assertFalse(payloads[1]['data']['secret_returned'])
        finally:
            v7_api.rc.LLM_API_KEY, v7_api.rc.LLM_MODEL, v7_api.rc.WECOM_WEBHOOK_URL = originals

    def test_wecom_recent_status_is_allowlisted(self):
        original = v7_api._load_json_cached
        try:
            v7_api._load_json_cached = lambda *_args, **_kwargs: {'sent': [{'time': 'FAKE_SECRET', 'level': 'FAKE_SECRET', 'status': 'FAKE_SECRET'}], 'count': 2}
            response = self.client.get('/api/v7/integrations/wecom')
            encoded = response.text
            self.assertNotIn('FAKE_SECRET', encoded)
            self.assertEqual(response.json()['data']['last_notification']['status'], 'UNKNOWN')
        finally:
            v7_api._load_json_cached = original

    def test_source_status_distinguishes_adapter_from_production_route(self):
        data = self.client.get('/api/v7/data-sources').json()['data']['sources']
        rows = {row['id']: row for row in data}
        self.assertEqual(rows['hithink']['production_route'], 'NOT_SWITCHED')
        self.assertEqual(rows['tencent']['production_route'], 'LEGACY_SCANNER_ONLY')
        self.assertFalse(rows['ai_quant']['inference_triggered'])

    def test_forward_day_zero_and_disabled_remain_truthful(self):
        data = self.client.get('/api/v7/forward').json()['data']
        self.assertEqual(data['forward_day_count'], 0)
        self.assertFalse(data['forward_enabled'])
        self.assertFalse(data['genuine_forward_started'])

    def test_pages_and_low_risk_local_preferences_are_present(self):
        html = self.client.get('/v7').text
        for page in ('page-picks', 'page-scoring', 'page-arena', 'page-personal', 'page-ai-quant',
                     'page-forward', 'page-plans', 'page-backtest', 'page-data', 'page-reports',
                     'page-sources', 'page-llm', 'page-wecom', 'page-health', 'page-settings'):
            self.assertIn(f'id="{page}"', html)
        app_source = (BASE / 'workbench_v7' / 'js' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("personalStorageKey: 'v7_personal_portfolio'", app_source)
        self.assertIn("getItem('user_portfolio')", app_source)
        self.assertNotIn("setItem('user_portfolio'", app_source)
        self.assertNotIn('.innerHTML', app_source)

    def test_no_write_methods_for_new_view_endpoints(self):
        for path in ('/api/v7/picks', '/api/v7/data-center', '/api/v7/data-sources',
                     '/api/v7/integrations/llm', '/api/v7/integrations/wecom'):
            for method in ('post', 'put', 'patch', 'delete'):
                response = self.client.request(method.upper(), path, content=b'{}',
                                               headers={'content-type': 'application/json'})
                self.assertEqual(response.status_code, 405, (method, path))


if __name__ == '__main__':
    unittest.main()
