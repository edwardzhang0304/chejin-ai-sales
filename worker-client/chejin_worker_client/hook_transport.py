"""Single-attempt loopback HTTP with explicit pre-dispatch evidence.

No proxy, redirect, connection-pool replay or library retry. Only failure of
connect(), before request() is entered, proves that no HTTP request was sent.
A failure of request()/getresponse()/read() is always delivery-unknown.
"""
import http.client
import json
import socket


class TransportFailure(RuntimeError):
    def __init__(self, code, evidence):
        super().__init__(code)
        self.code, self.evidence = code, evidence


def connection(port, timeout):
    return http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)


def post(conn, route, payload, max_bytes):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    evidence = {'delivery': 'unknown', 'stage': 'connect', 'request_started': False,
                'response_started': False, 'response_complete': False}
    try:
        if conn.sock is None:
            try:
                conn.connect()
            except OSError as exc:
                evidence.update(delivery='not_sent', errno=exc.errno,
                                transport_kind='timeout' if isinstance(exc, TimeoutError) else 'connection_error')
                raise TransportFailure('HOOK_LOCAL_TRANSPORT_FAILED', evidence) from None
        evidence.update(stage='request', request_started=True)
        conn.request('POST', '/'+route, body, {'Content-Type':'application/json'})
        evidence['stage'] = 'response'
        response = conn.getresponse()
        evidence.update(response_started=True, status=response.status)
        if response.status != 200:
            raise TransportFailure('HOOK_HTTP_RESPONSE_REJECTED', evidence)
        raw = bytearray()
        expected_length = response.length
        while True:
            chunk = response.read(min(8192, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise TransportFailure('HOOK_RESPONSE_TOO_LARGE', evidence)
        if expected_length is not None and len(raw) != expected_length:
            raise TransportFailure('HOOK_LOCAL_TRANSPORT_FAILED', evidence)
        evidence.update(delivery='responded', stage='complete', response_complete=True)
        return bytes(raw), evidence
    except (OSError, http.client.HTTPException) as exc:
        evidence['transport_kind'] = 'timeout' if isinstance(exc, (TimeoutError, socket.timeout)) else 'connection_error'
        raise TransportFailure('HOOK_LOCAL_TRANSPORT_FAILED', evidence) from None
    except TransportFailure:
        raise
    finally:
        if not evidence['response_complete']:
            conn.close()
