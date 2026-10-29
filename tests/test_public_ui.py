"""Public UI has its own specimens and cannot silently use market/account data."""
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from scripts.public_preview import create_preview_app
from scripts.public_ui_fixture import ui_fixture, MODE

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 29, 16, 0, tzinfo=timezone(timedelta(hours=8)))


def test_demo_and_empty_view_share_latest_component_markup_but_use_separate_apis():
    with TestClient(create_preview_app()) as client:
        demo = client.get('/demo')
        empty = client.get('/v7')
        for response in (demo, empty):
            assert response.status_code == 200
            assert '候选股票热力格' in response.text
            assert 'home-v3.css?v=20261029-v3-r3' in response.text
            assert 'market-pages.js?v=20261029-v3-r3' in response.text
            assert response.headers['Cache-Control'] == 'no-store'
        assert 'data-ui-demo="synthetic-v1"' in demo.text
        assert '界面示例 · 合成数据' in demo.text
        assert '不代表选股结果或真实账户' in demo.text
        assert 'data-ui-demo=' not in empty.text
        assert '未接入实时行情或实际账户' in empty.text
        assert client.get('/replay').status_code == 200
        assert client.get('/api/demo').json()['summary']['ledger_reconciliation'] == 'PASS'


class Assets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        url = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' else None
        if url: self.urls.append(url)


def test_every_resource_used_by_the_current_demo_is_distributed():
    with TestClient(create_preview_app()) as client:
        assets = Assets(client.get('/demo').text).urls
        assert len(assets) == 19
        for url in assets:
            assert url.startswith('/workbench_v7/')
            assert client.get(url).status_code == 200, url


@pytest.mark.parametrize('endpoint', ['overview', 'market', 'news', 'picks', 'arena', 'ai-quant', 'paper', 'forward', 'trade-plans', 'research', 'system', 'integrations/llm'])
def test_specimens_are_marked_and_do_not_create_runtime_state(endpoint, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob('*'))
    result = ui_fixture(endpoint, NOW)
    assert result['demo']['mode'] == MODE and result['demo']['synthetic'] is True
    assert result['demo']['execution_authorized'] is False
    assert before == sorted(tmp_path.rglob('*'))
    assert result == ui_fixture(endpoint, NOW)


def test_display_clock_does_not_create_future_or_lunch_bars():
    for hour, minute in [(8, 0), (10, 0), (12, 0), (16, 0)]:
        now = NOW.replace(hour=hour, minute=minute)
        result = ui_fixture('market', now)
        for index in result['data']['indexes']:
            series = index['intraday']
            assert series['source'] == MODE
            for point in series['points']:
                assert datetime.fromisoformat(point['bar_at']) <= now
                bar_minute = int(point['time'][:2]) * 60 + int(point['time'][2:])
                assert 570 <= bar_minute <= 690 or 780 <= bar_minute <= 900


def test_specimen_has_no_provider_quote_provenance_or_execution_permission():
    data = ui_fixture('picks', NOW)['data']
    assert data['execution_authorized'] is False
    for participant in data['participants']:
        assert participant['execution_authorized'] is False
        assert participant['candidate_evidence'] == {}
        assert participant['scan_evidence']['evidence_refs'] == []
    assert len(data['ai_quant_top5']['predictions']) == 5
    accounts = ui_fixture('arena', NOW)['data']['leaderboard']
    assert len(accounts) == 6
    assert all(p['transactions'] == [] and p['fills_count'] == 0 for p in accounts)
    assert ui_fixture('forward', NOW)['data']['forward_day_count'] == 0


@pytest.mark.parametrize('path', ['/api/ui-demo/overview', '/api/ui-demo/arena', '/api/run/intraday_cycle'])
def test_demo_cannot_write_or_start_a_job(path):
    with TestClient(create_preview_app()) as client:
        assert client.post(path, json={}).status_code == 403
