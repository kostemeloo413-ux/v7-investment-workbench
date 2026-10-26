import unittest,tempfile,sys,subprocess,importlib,msvcrt
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import runtime_config as rc

class SingletonByteLockTests(unittest.TestCase):
    def child(self,module,root):
        return subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve()),module,root],capture_output=True,text=True,timeout=20)
    def test_legacy_offset_lock_survives_pid_length_change(self):
        for module in ['daemon.trading_daemon','daemon.watchdog']:
            with self.subTest(module=module),tempfile.TemporaryDirectory() as td:
                p=Path(td)/'isolated.lock';p.write_bytes(b'123456')
                with p.open('r+b') as holder:
                    holder.seek(6);msvcrt.locking(holder.fileno(),msvcrt.LK_NBLCK,1)
                    holder.seek(0);holder.truncate();holder.write(b'1234');holder.flush()
                    result=self.child(module,td)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual(p.read_bytes(),b'1234')
    def test_fixed_lock_rejects_child_after_pid_file_is_rewritten(self):
        for module in ['daemon.trading_daemon','daemon.watchdog']:
            with self.subTest(module=module),tempfile.TemporaryDirectory() as td:
                previous=rc.STATE_DIR;rc.STATE_DIR=td
                try:
                    f=importlib.import_module(module).acquire_singleton('isolated');self.assertIsNotNone(f)
                    try:self.assertEqual(self.child(module,td).returncode,0)
                    finally:f.close()
                finally:rc.STATE_DIR=previous

if __name__=='__main__':
    module,root=sys.argv[1:];rc.STATE_DIR=root
    handle=importlib.import_module(module).acquire_singleton('isolated')
    if handle is not None:handle.close();sys.exit(2)
