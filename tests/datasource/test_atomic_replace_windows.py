import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from ai_quant import storage_utils as storage


class AtomicReplaceWindowsTests(unittest.TestCase):
    def error(self):
        e=PermissionError('Windows sharing conflict');e.winerror=32;return e

    def test_transient_conflict_replaces_once_with_intact_json(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'state.json';p.write_text('{"old":1}',encoding='utf-8')
            original=storage.os.replace;calls=[]
            def replace(a,b):
                calls.append(a)
                if len(calls)<3:raise self.error()
                return original(a,b)
            with patch.object(storage.os,'replace',side_effect=replace),patch.object(storage.time,'sleep') as sleep:
                storage.atomic_write_json(p,{'new':2})
            self.assertEqual(json.loads(p.read_text()),{'new':2})
            self.assertEqual(len(set(calls)),1);self.assertEqual(sleep.call_count,2)
            self.assertEqual(len(list(Path(td).iterdir())),1)

    def test_persistent_conflict_is_bounded_and_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'state.json';p.write_bytes(b'{"old":1}')
            with patch.object(storage.os,'replace',side_effect=self.error()) as replace,patch.object(storage.time,'sleep') as sleep:
                with self.assertRaises(OSError):storage.atomic_write_json(p,{'new':2})
            self.assertEqual(replace.call_count,6);self.assertEqual(sleep.call_count,5)
            self.assertEqual(p.read_bytes(),b'{"old":1}');self.assertEqual(len(list(Path(td).iterdir())),1)

    def test_other_io_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(storage.os,'replace',side_effect=OSError('disk error')) as replace,patch.object(storage.time,'sleep') as sleep:
                with self.assertRaises(OSError):storage.atomic_write_json(Path(td)/'state.json',{})
            self.assertEqual(replace.call_count,1);sleep.assert_not_called()
