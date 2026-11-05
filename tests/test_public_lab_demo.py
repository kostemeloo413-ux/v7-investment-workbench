"""A public lab example must never become a real research or execution request."""
from fastapi.testclient import TestClient
import pytest

from scripts.public_lab_fixture import PERSONA_ID, SOURCE_ID, SOURCE_TEXT
from scripts.public_preview import create_preview_app


def test_lab_specimens_and_excerpts_work_without_enabling_or_creating_lab(tmp_path, monkeypatch):
    lab_root = tmp_path / 'never_created'
    monkeypatch.setenv('V7_BLOGGER_LAB_ENABLED', '0')
    monkeypatch.setenv('V7_BLOGGER_LAB_ROOT', str(lab_root))
    with TestClient(create_preview_app()) as client:
        html = client.get('/demo').text
        assert '只读界面示例' in html
        assert '非模型输出或实测业绩' in html
        assert '只读界面示例' not in client.get('/v7').text
        base = '/api/ui-demo/blogger-lab/'
        overview = client.get(base + f'personas/{PERSONA_ID}/overview').json()
        assert overview['status'] == 'ok'
        assert overview['demo']['synthetic'] is True
        assert overview['demo']['execution_authorized'] is False
        for item in overview['data']['latest_knowledge']['items']:
            citation = item['citations'][0]
            excerpt = client.get(base + f'sources/{SOURCE_ID}/excerpt', params={
                'persona_id': PERSONA_ID, 'start_char': citation['start'], 'end_char': citation['end']
            }).json()
            assert excerpt['data']['excerpt'] == citation['quote']
            assert SOURCE_TEXT[citation['start']:citation['end']] == citation['quote']
        assert client.get('/api/blogger-lab/personas').status_code == 404
        assert client.get(base + 'personas/unknown/overview').json()['status'] == 'unavailable'
        assert client.get(base + f'sources/{SOURCE_ID}/excerpt', params={'persona_id': 'unknown'}).json()['status'] == 'unavailable'
    assert not lab_root.exists()


@pytest.mark.parametrize('method', ['post', 'put', 'delete'])
def test_sample_mutations_stay_blocked_even_when_real_lab_is_enabled(method, tmp_path, monkeypatch):
    monkeypatch.setenv('V7_BLOGGER_LAB_ENABLED', '1')
    with TestClient(create_preview_app(lab_root=str(tmp_path / 'lab')), client=('127.0.0.1', 12345)) as client:
        url = f'/api/ui-demo/blogger-lab/personas/{PERSONA_ID}/replay-step'
        assert getattr(client, method)(url).status_code == 403
        specimen = client.get(f'/api/ui-demo/blogger-lab/personas/{PERSONA_ID}/paper').json()
        assert specimen['demo']['synthetic'] is True
        assert len(specimen['data']['fills']) == 2
        real_personas = client.get('/api/blogger-lab/personas').json()
        assert real_personas['status'] == 'ok' and real_personas['data'] == []
