"""Local-only callback intake. HTTP acknowledgement follows SQLite commit."""
from __future__ import annotations

import ipaddress
import json
import math
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from .hook_config import CALLBACK_PATH
from .hook_inbox import HookReceiveScope, NativeInbox


def create_receiver(*, port: int, inbox: NativeInbox, scope: HookReceiveScope,
                    request_timeout_seconds: float,
                    on_persistence_failure: Callable[[], None]) -> HTTPServer:
    """Bind before callback registration. Caller owns serving and stop/join.

    Intake is serial and bounded; the failure callback must feed the existing
    persistence fault gate. It receives no customer body or exception text.
    """
    if type(port) is not int or not 0 <= port <= 65535 or port == scope.hook_api_port:
        raise ValueError("HOOK_RECEIVER_PORT_INVALID")
    if (not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0):
        raise ValueError("HOOK_RECEIVER_TIMEOUT_INVALID")

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(request_timeout_seconds)

        def log_message(self, *_: object) -> None:
            pass  # HTTP logs must not contain paths, bodies or credentials.

        def respond(self, status: int) -> None:
            body = json.dumps({"code": 0 if status == 200 else status}).encode("ascii")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self) -> None:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                self.respond(403)
                return
            if self.path != CALLBACK_PATH:
                self.respond(404)
                return
            lengths = self.headers.get_all("Content-Length") or []
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                self.respond(400)
                return
            if self.headers.get_content_type() != "application/json":
                self.respond(415)
                return
            try:
                size = int(lengths[0])
            except ValueError:
                self.respond(400)
                return
            if not 0 < size <= inbox.max_body_bytes:
                self.respond(413)
                return
            try:
                raw = self.rfile.read(size)
            except (socket.timeout, OSError):
                self.respond(408)
                return
            if len(raw) != size:
                self.respond(400)
                return
            try:
                inbox.persist(raw, scope)
            except ValueError:
                self.respond(400)
                return
            except Exception:
                # Keep the write failure a fault even if diagnostic capture fails.
                try:
                    on_persistence_failure()
                finally:
                    self.respond(503)
                return
            self.respond(200)

    class LocalServer(HTTPServer):
        allow_reuse_address = False
        allow_reuse_port = False

    server = LocalServer(("127.0.0.1", port), Handler)
    server.hook_receive_scope = scope
    return server
