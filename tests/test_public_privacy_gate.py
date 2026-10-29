"""Regression checks for privacy gates, including exact fake-fixture exceptions."""
import hashlib
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_privacy_gate', ROOT / 'tools/check_public_release.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_credential_rule_blocks_new_literal():
    sample = '='.join(('api_key', repr('test-only-value-' + 'ABC123xyz' * 3)))
    assert any(x['rule'] == 'credential-literal' for x in guard.findings_for('scripts/new.py', sample.encode()))


def test_fixture_exception_is_exact_and_does_not_allow_a_whole_file():
    sample = '='.join(('api_key', repr('offline-fixture-only')))
    matched = guard.RULES['credential-literal'].search(sample).group()
    exception = [{'path': 'tests/fixture.py', 'rule': 'credential-literal', 'match_sha256': hashlib.sha256(matched.encode()).hexdigest()}]
    assert guard.findings_for('tests/fixture.py', sample.encode(), exception) == []
    assert guard.findings_for('tests/other.py', sample.encode(), exception)
    assert guard.findings_for('tests/fixture.py', (sample + '-changed').replace('only', 'other').encode(), exception)


def test_private_email_is_blocked_and_noreply_is_allowed():
    private = 'person' + '@' + 'private.test'
    public = '123+public-user' + '@' + 'users.noreply.github.com'
    assert guard.findings_for('GIT_COMMIT:unit', private.encode())
    assert guard.findings_for('GIT_COMMIT:unit', public.encode()) == []


def test_private_paths_and_changed_binary_are_blocked():
    assert guard.path_findings('state/account.json', b'{}')
    assert guard.path_findings('.env', b'')
    assert guard.path_findings('private_audit/report.json', b'{}')
    assert guard.path_findings('models/ai_quant/calibrator_AIQ-V1.0.pkl', b'unreviewed')
    assert guard.path_findings('example.zip', b'unreviewed')


def test_environment_template_requires_empty_sensitive_values():
    assert guard.path_findings('.env.example', ('LLM_API_KEY=' + 'not-empty').encode())
    assert guard.path_findings('.env.example', b'LLM_API_KEY=\nDAEMON_PORT=8020\n') == []


def test_reviewed_ui_screenshots_are_exactly_pinned_not_a_generic_image_allowance():
    images = {path: sha for path, sha in guard.REVIEWED_BINARY.items() if path.startswith('docs/images/')}
    current_images = set(re.findall(r'\((docs/images/[^)]+)\)', (ROOT / 'README.md').read_text(encoding='utf-8')))
    assert len(current_images) == 3 and current_images <= images.keys()
    for path, expected in images.items():
        body = (ROOT / path).read_bytes()
        assert body.startswith(b'\xff\xd8') and hashlib.sha256(body).hexdigest() == expected
        assert guard.path_findings(path, body) == []
        assert guard.path_findings(path, body + b'changed')
    assert guard.path_findings('docs/images/unreviewed.jpg', b'\xff\xd8')


def test_git_object_reader_detects_an_unreachable_blob(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', '--template=', str(tmp_path)], check=True)
    sample = '='.join(('password', repr('isolated-test-only-value')))
    subprocess.run(['git', '-C', str(tmp_path), 'hash-object', '-w', '--stdin'], input=sample.encode(), check=True, capture_output=True)
    objects = list(guard.read_git_objects(tmp_path))
    assert len(objects) == 1 and objects[0][1] == 'blob'
    assert guard.findings_for('UNREACHABLE_BLOB:unit', objects[0][2])
