"""Supported public-preview checks: no private state, no external market calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from scripts.public_demo import run_demo, load_sample
from scripts.public_preview import create_preview_app


def test_replay_uses_historical_dates_and_reconciles_cash():
    result = run_demo()
    assert result['mode'] == 'HISTORICAL_HYPOTHETICAL_DEMO'
    assert result['signal_origin'] == 'USER_SCENARIO_NOT_STRATEGY_SELECTION'
    assert result['summary']['t1_blocked'] is True
    assert result['summary']['positions_count'] == 0
    assert result['summary']['ledger_reconciliation'] == 'PASS'
    buy, sell = result['fills']
    assert buy['trade_date'] == result['market_sample']['bars'][1]['trade_date']
    assert sell['trade_date'] == result['market_sample']['bars'][2]['trade_date']
    assert buy['shares'] == sell['shares'] and buy['shares'] % 100 == 0
    assert buy['commission'] >= 5 and sell['commission'] >= 5
    assert buy['stamp_tax'] == 0 and sell['stamp_tax'] > 0
    assert round(100000 - buy['total_cash_required'] + sell['net_proceeds'], 2) == result['summary']['final_cash']


def test_replay_is_deterministic_and_does_not_create_production_state():
    before = {name: (ROOT / name).exists() for name in ('state', 'data', '.env')}
    assert run_demo() == run_demo()
    assert {name: (ROOT / name).exists() for name in before} == before


def test_preview_ignores_process_credentials_in_fresh_process():
    env = os.environ.copy()
    env.update(DEEPSEEK_API_KEY='PUBLIC_TEST_ONLY_NOT_A_CREDENTIAL', WECOM_WEBHOOK_URL='https://example.invalid/offline-test')
    code = "from scripts.public_preview import create_preview_app; import runtime_config as rc; assert rc.PUBLIC_PREVIEW and not rc.LLM_API_KEY and not rc.WECOM_WEBHOOK_URL; print('PASS')"
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'PASS'


@pytest.fixture
def client():
    with TestClient(create_preview_app()) as value:
        yield value


def test_default_page_and_health_need_no_private_season(client):
    assert client.get('/').status_code == 200
    assert '历史撮合演示' in client.get('/demo').text
    health = client.get('/api/health').json()
    assert health['scheduler_started'] is False and health['real_trading'] is False
    result = client.get('/api/demo')
    assert result.status_code == 200 and result.json()['summary']['ledger_reconciliation'] == 'PASS'


@pytest.mark.parametrize('endpoint', [
    'overview', 'market', 'news', 'picks', 'stock/000001', 'data-center',
    'data-sources', 'integrations/llm', 'integrations/wecom', 'ai-quant',
    'paper', 'forward', 'trade-plans', 'arena', 'research', 'system',
])
def test_original_read_only_views_degrade_without_private_state(client, endpoint):
    response = client.get('/api/v7/' + endpoint)
    assert response.status_code < 500, response.text
    assert response.headers['X-V7-Mode'] == 'PUBLIC_READ_ONLY_PREVIEW'


@pytest.mark.parametrize('endpoint', ['/api/config', '/api/run/intraday_cycle', '/api/demo'])
def test_preview_rejects_mutations(client, endpoint):
    assert client.post(endpoint, json={}).status_code == 403


@pytest.mark.parametrize('endpoint', ['/.env', '/config/llm_profiles.json', '/private_audit/step2_source_scan.json'])
def test_private_files_have_no_download_route(client, endpoint):
    assert client.get(endpoint).status_code == 404


def test_original_ui_is_clearly_labeled_as_preview(client):
    response = client.get('/v7')
    assert response.status_code == 200
    assert '公开源码预览' in response.text
    assert '未接入实时行情或实际账户' in response.text


def test_governed_entry_still_holds_without_a_season():
    from competition.season_runtime import RuntimeStateError, load_active_season_identity
    with pytest.raises(RuntimeStateError):
        load_active_season_identity()


def test_sample_and_configuration_have_no_account_data():
    sample, _ = load_sample()
    assert sample['price_basis'] == 'UNADJUSTED' and len(sample['bars']) == 3
    assert all(set(bar) == {'trade_date', 'open', 'high', 'low', 'close', 'source_row_sha256'} for bar in sample['bars'])
    assert json.loads((ROOT / 'config/markets.json').read_text(encoding='utf-8'))['watchlist'] == []
