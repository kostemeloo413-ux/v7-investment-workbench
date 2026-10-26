import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import baostock.util.socketutil as su
import datasource.baostock_transport as bt
from datasource.active_g6_v2_route import _consume


class FakeSocket:
    def __init__(self,chunks):self.chunks=list(chunks);self.closed=False
    def settimeout(self,value):self.timeout=value
    def connect(self,address):self.address=address
    def sendall(self,message):self.message=message
    def recv(self,n):return self.chunks.pop(0) if self.chunks else b''
    def close(self):self.closed=True


class BaoStockTransportTests(unittest.TestCase):
    def start(self,connection,**kw):
        return patch.multiple(bt.socket,getaddrinfo=lambda *a:[(2,1,6,'',('127.0.0.1',10030))],socket=lambda *a:connection)

    def test_eof_restores_sdk_and_closes_socket(self):
        conn=FakeSocket([b'']);original=su.send_msg
        with self.start(conn):
            with bt.bounded_baostock_session():
                su.SocketUtil().connect('0')
                with self.assertRaisesRegex(bt.BaoStockTransportError,'PEER_EOF'):su.send_msg('request')
        self.assertIs(su.send_msg,original);self.assertTrue(conn.closed)

    def test_partial_eof_cannot_be_success(self):
        conn=FakeSocket([b'00.9.10',b''])
        with self.start(conn),bt.bounded_baostock_session():
            su.SocketUtil().connect('0')
            with self.assertRaisesRegex(bt.BaoStockTransportError,'PEER_EOF'):su.send_msg('request')

    def test_fragmented_uncompressed_response(self):
        raw=b'00.9.10\x0101\x010000000010success<![CDATA[]]>\n'
        conn=FakeSocket([raw[:12],raw[12:25],raw[25:]])
        with self.start(conn),bt.bounded_baostock_session():
            su.SocketUtil().connect('0');self.assertEqual(su.send_msg('request'),raw.decode())
        self.assertEqual(conn.message,b'request\n')

    def test_compressed_wire_matches_sdk_decoding(self):
        import zlib
        import baostock.common.contants as c
        kind=c.COMPRESSED_MESSAGE_TYPE_TUPLE[0];body='0\x01success\x01证券';payload=zlib.compress(body.encode())
        header=f'00.9.10\x01{kind}\x01{len(payload):010d}'
        conn=FakeSocket([header.encode()+payload+b'<![CDATA[]]>\n'])
        with self.start(conn),bt.bounded_baostock_session():
            su.SocketUtil().connect('0');self.assertEqual(su.send_msg('request'),header+body)

    def test_session_deadline_blocks_send(self):
        with bt.bounded_baostock_session(total_timeout=0):
            with self.assertRaises(bt.BaoStockTransportError):su.SocketUtil().connect('0')

    def test_process_local_session_cannot_overlap(self):
        with bt.bounded_baostock_session():
            with self.assertRaisesRegex(bt.BaoStockTransportError,'ALREADY_RUNNING'):
                with bt.bounded_baostock_session():pass

    def test_later_page_failure_is_not_initial_success(self):
        class Result:
            fields=['code'];error_code='0';error_msg='success';i=0
            def next(self):
                self.i+=1
                if self.i==1:return True
                self.error_code='10001001';self.error_msg='用户未登录';return False
            def get_row_data(self):return ['sh.600000']
        fields,rows,code,msg=_consume(Result());self.assertEqual(code,'10001001');self.assertEqual(len(rows),1)

    @unittest.skipUnless(sys.platform=='win32','Windows lock')
    def test_shared_g6_lock_rejects_overlap(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'g6.lock'
            with bt.daily_g6_lock(p):
                with self.assertRaisesRegex(bt.BaoStockTransportError,'ALREADY_RUNNING'):
                    with bt.daily_g6_lock(p):pass
            with bt.daily_g6_lock(p):pass

if __name__=='__main__':unittest.main()
