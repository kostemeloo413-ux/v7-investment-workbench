"""Bounded BaoStock transport; keeps SDK, endpoint and data contracts intact."""
from __future__ import annotations
import contextlib
import json
import queue
import socket
import threading
import time
import zlib
from pathlib import Path

_SESSION_LOCK = threading.Lock()


class BaoStockTransportError(RuntimeError):
    pass


@contextlib.contextmanager
def bounded_baostock_session(*, wire_root=None, query_timeout=90, total_timeout=600):
    """Serialize process-local SDK use and fail closed on EOF/deadline/partial IO.

    The SDK uses global socket state. All replacements are scoped and restored.
    No retry, resolver override, provider switch or site-packages edit is made.
    """
    import baostock.common.context as ctx
    import baostock.common.contants as cons
    import baostock.util.socketutil as transport
    if not _SESSION_LOCK.acquire(blocking=False):
        raise BaoStockTransportError('BAOSTOCK_SESSION_ALREADY_RUNNING')
    original_connect, original_send = transport.SocketUtil.connect, transport.send_msg
    previous_socket = getattr(ctx, 'default_socket', None)
    deadline = time.monotonic() + total_timeout
    connection = None
    ordinal = 0
    def remaining(limit):
        value = min(limit, deadline - time.monotonic())
        if value <= 0:
            raise BaoStockTransportError('BAOSTOCK_SESSION_TOTAL_TIMEOUT')
        return value
    def connect(self, api_key):
        nonlocal connection
        host = cons.BAOSTOCK_VIP_SERVER_IP if api_key.startswith('bs-') else cons.BAOSTOCK_SERVER_IP
        # DNS is bounded too; a stalled OS resolver may finish only in a daemon
        # thread, but cannot hold this capture or its scheduler worker forever.
        answers = queue.Queue(maxsize=1)
        def resolve():
            try: answers.put((socket.getaddrinfo(host, cons.BAOSTOCK_SERVER_PORT, socket.AF_INET, socket.SOCK_STREAM), None))
            except Exception as exc: answers.put((None, exc))
        threading.Thread(target=resolve, daemon=True).start()
        try: addresses, error = answers.get(timeout=remaining(query_timeout))
        except queue.Empty as exc: raise BaoStockTransportError('BAOSTOCK_DNS_TIMEOUT') from exc
        if error: raise BaoStockTransportError('BAOSTOCK_DNS_FAILED') from error
        if not addresses: raise BaoStockTransportError('BAOSTOCK_DNS_EMPTY')
        family, kind, protocol, _, address = addresses[0]
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(remaining(query_timeout))
        connection.connect(address)
        ctx.default_socket = connection
    def send(message):
        nonlocal ordinal
        ordinal += 1
        if connection is None: raise BaoStockTransportError('BAOSTOCK_NO_SESSION')
        call_deadline = time.monotonic() + remaining(query_timeout)
        received = bytearray()
        def call_remaining():
            value = min(call_deadline, deadline) - time.monotonic()
            if value <= 0: raise BaoStockTransportError('BAOSTOCK_QUERY_TOTAL_TIMEOUT')
            return value
        try:
            connection.settimeout(call_remaining())
            connection.sendall((message+'\n').encode('utf-8'))
            while True:
                connection.settimeout(call_remaining())
                part = connection.recv(8192)
                if not part: raise BaoStockTransportError('BAOSTOCK_PEER_EOF')
                received.extend(part)
                if len(received) > 32*1024*1024: raise BaoStockTransportError('BAOSTOCK_RESPONSE_SIZE_LIMIT')
                if received[-13:] == b'<![CDATA[]]>\n': break
            header = bytes(received[:cons.MESSAGE_HEADER_LENGTH]).decode('utf-8')
            components = header.split(cons.MESSAGE_SPLIT)
            if len(components) != 3: raise BaoStockTransportError('BAOSTOCK_PROTOCOL_HEADER_INVALID')
            if components[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
                length = int(components[2])
                return header + zlib.decompress(bytes(received[cons.MESSAGE_HEADER_LENGTH:cons.MESSAGE_HEADER_LENGTH+length])).decode('utf-8')
            return bytes(received).decode('utf-8')
        except (OSError, ValueError, zlib.error) as exc:
            raise BaoStockTransportError('BAOSTOCK_TRANSPORT_FAILED:'+type(exc).__name__) from exc
        finally:
            if wire_root is not None:
                root=Path(wire_root);root.mkdir(parents=True,exist_ok=True)
                (root/f'wire_{ordinal:03d}.bin').write_bytes(bytes(received))
    try:
        transport.SocketUtil.connect = connect
        transport.send_msg = send
        yield
    finally:
        transport.SocketUtil.connect, transport.send_msg = original_connect, original_send
        if connection is not None: connection.close()
        if previous_socket is None:
            if hasattr(ctx,'default_socket'): delattr(ctx,'default_socket')
        else: ctx.default_socket=previous_socket
        _SESSION_LOCK.release()


@contextlib.contextmanager
def daily_g6_lock(lock_path):
    """One cross-process lock for natural daily_review and controlled recovery."""
    import msvcrt
    path=Path(lock_path);path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b');handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    except OSError as exc:
        handle.close();raise BaoStockTransportError('DAILY_G6_ALREADY_RUNNING') from exc
    try: yield
    finally:
        handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1);handle.close()
