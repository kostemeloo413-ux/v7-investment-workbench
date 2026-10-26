"""Governed, idempotent G5 closeout evidence-erratum tests."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for item in (ROOT, ROOT / 'scripts'):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from ai_quant.forward_start_gate import (  # noqa: E402
    ACTIVE_SEASON_ID,
    EPOCH_ID,
    ForwardStateError,
    append_forward_gate_audit_erratum,
    build_validation_subject,
)
from ai_quant.storage_utils import atomic_write_json  # noqa: E402


CORRECT = '270278f9dd12dc980ca1e277b50e1bd2fcc16ddf04f9a1ead052f49f5b138fec'
WRONG = '270278f9dd12dc980ca1e277b50e1bd2fcc16ddf04f9e1aad052f49f5b138fec'
PACKAGE = 'V7_G5_PAPER_E2E_SOURCE_REVIEW_PACKAGE.zip'


class G5CloseoutErratumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.forward = self.root / 'forward'
        self.forward.mkdir()
        self.path = self.forward / 'forward_epoch_manifest.json'
        self.context = {'forward_root': str(self.forward)}
        self._write(self._manifest())

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, **overrides):
        value = {
            'schema_version': 'v7.3-forward-truth-integrity',
            'forward_epoch_id': EPOCH_ID,
            'season_id': ACTIVE_SEASON_ID,
            'participant_id': 'P-AI-QUANT',
            'account_id': 'AIQ-PAPER-V2',
            'epoch_type': 'GENUINE_FORWARD',
            'initial_capital': 100000.0,
            'baseline_timestamp': '2026-09-17T12:00:00+08:00',
            'baseline_timezone': 'Asia/Shanghai',
            'forward_enabled': False,
            'genuine_forward_started': False,
            'forward_day_count': 0,
            'qualified_trading_dates': [],
            'forward_day_counter': {
                'current_forward_day_count': 0,
                'qualified_trading_dates': [],
            },
            'status': 'INITIALIZED_HOLD',
            'real_trading': False,
            'validation_subject': build_validation_subject(),
            'required_gates': {
                'G4_RUNTIME_ACCEPTANCE': {'status': 'PASS'},
                'G5_PAPER_E2E': {'status': 'PASS'},
                'G6_MARKET_COMPLETENESS': {'status': 'OPEN'},
            },
            'required_gate_audit': [{
                'gate_id': 'G5_PAPER_E2E',
                'status': 'PASS',
                'audit_note': 'historical accepted G5 decision',
                'at': '2026-09-20T12:00:00+08:00',
            }],
        }
        value.update(overrides)
        return value

    def _write(self, value):
        atomic_write_json(self.path, value)

    def _call(self, **overrides):
        args = {
            'gate_id': 'G5_PAPER_E2E',
            'package': PACKAGE,
            'incorrect_sha256': WRONG,
            'correct_sha256': CORRECT,
            'verified_original_zip_sha256': CORRECT,
            'verified_backup_zip_sha256': CORRECT,
            'context': self.context,
            'at': '2026-09-20T16:00:00+08:00',
        }
        args.update(overrides)
        return append_forward_gate_audit_erratum(**args)

    def _loaded(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_first_call_appends_exactly_one_erratum(self):
        result = self._call()
        events = [e for e in result['required_gate_audit'] if e.get('event') == 'G5_EVIDENCE_SHA256_ERRATUM']
        self.assertEqual(len(events), 1)
        self.assertEqual(result['required_gates']['G5_PAPER_E2E']['status'], 'PASS')

    def test_second_call_is_idempotent(self):
        first = self._call()
        before_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        second = self._call()
        after_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(len(first['required_gate_audit']), len(second['required_gate_audit']))

    def test_wrong_g5_status_rejected(self):
        self._write(self._manifest(required_gates={
            'G4_RUNTIME_ACCEPTANCE': {'status': 'PASS'},
            'G5_PAPER_E2E': {'status': 'PENDING'},
            'G6_MARKET_COMPLETENESS': {'status': 'OPEN'},
        }))
        with self.assertRaises(ForwardStateError):
            self._call()

    def test_wrong_identity_rejected(self):
        for key in ('season_id', 'forward_epoch_id'):
            value = self._manifest()
            value[key] = 'WRONG'
            self._write(value)
            with self.assertRaises(ForwardStateError):
                self._call()

    def test_started_forward_rejected(self):
        self._write(self._manifest(status='ACTIVE', forward_enabled=True, genuine_forward_started=True))
        with self.assertRaises(ForwardStateError):
            self._call()

    def test_original_pass_missing_or_non_unique_rejected(self):
        value = self._manifest(required_gate_audit=[])
        self._write(value)
        with self.assertRaises(ForwardStateError):
            self._call()
        value['required_gate_audit'] = [
            self._manifest()['required_gate_audit'][0],
            self._manifest()['required_gate_audit'][0],
        ]
        self._write(value)
        with self.assertRaises(ForwardStateError):
            self._call()

    def test_verified_hash_mismatch_rejected(self):
        with self.assertRaises(ForwardStateError):
            self._call(verified_backup_zip_sha256=WRONG)

    def test_corrupt_manifest_fails_closed(self):
        self.path.write_text('{not-json', encoding='utf-8')
        with self.assertRaises(ForwardStateError):
            self._call()

    def test_atomic_failure_leaves_manifest_unchanged(self):
        before = self.path.read_bytes()
        with patch('ai_quant.forward_start_gate.atomic_write_json', side_effect=OSError('simulated atomic failure')):
            with self.assertRaises(OSError):
                self._call()
        self.assertEqual(before, self.path.read_bytes())

    def test_only_audit_changes(self):
        before = self._loaded()
        after = self._call()
        for key in ('required_gates', 'status', 'forward_enabled', 'genuine_forward_started',
                    'forward_day_count', 'qualified_trading_dates', 'real_trading'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(len(after['required_gate_audit']), len(before['required_gate_audit']) + 1)


if __name__ == '__main__':
    unittest.main()
