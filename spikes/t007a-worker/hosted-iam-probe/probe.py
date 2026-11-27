"""DB-free minimal HTTP probe for future Cloud Run Step 1 IAM verification.

Intentional scope boundaries:
- Serves ONLY ``GET /healthz`` with a fixed minimal JSON body.
- No Worker routes (``/wake``, ``/recover``), no product APIs, no DB connection,
  no external API calls, and no token/IAM simulation inside app code.
- Never prints credentials, environment variable values, internal config,
  request headers, query strings, or untrusted paths in responses or logs.
- Actual caller authentication/authorization is delegated to Cloud Run IAM
  (``--no-allow-unauthenticated`` + ``roles/run.invoker``) when hosted later.
"""

from __future__ import annotations

import json
import os
import signal
import socketserver
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
HEALTHZ_PATH = "/healthz"

HEALTHZ_BODY = b'{"ok":true,"probe":"t007f-iam-probe"}\n'
NOT_FOUND_BODY = b'{"ok":false,"error":"not_found"}\n'
METHOD_NOT_ALLOWED_BODY = b'{"ok":false,"error":"method_not_allowed"}\n'
ERROR_BODY = b'{"ok":false,"error":"request_rejected"}\n'

_KNOWN_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)


def _log_event(payload: dict[str, object]) -> None:
    """Emit fixed-schema JSON log lines with no env/config/credential values."""
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def resolve_port(env: dict[str, str] | None = None) -> int:
    """Parse injected PORT env var (1..65535) without echoing invalid values."""
    source = os.environ if env is None else env
    raw = source.get("PORT")
    if raw is None or raw == "":
        return DEFAULT_PORT
    if not (raw.isascii() and raw.isdigit()):
        raise ValueError("invalid_port")
    port = int(raw, 10)
    if port < 1 or port > 65535:
        raise ValueError("invalid_port")
    return port


class ProbeRequestHandler(BaseHTTPRequestHandler):
    """Minimal fail-closed HTTP handler exposing only GET /healthz."""

    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        # Suppress default BaseHTTP/Python runtime version disclosure.
        return "iam-probe"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Suppress default request-line logging (could contain query secrets).
        return

    def _safe_method_label(self) -> str:
        cmd = getattr(self, "command", None)
        if isinstance(cmd, str) and cmd in _KNOWN_METHODS:
            return cmd
        return "OTHER"

    def _is_exact_healthz(self) -> bool:
        if getattr(self, "path", None) != HEALTHZ_PATH:
            return False
        requestline = getattr(self, "requestline", "")
        words = requestline.split() if isinstance(requestline, str) else []
        return len(words) >= 2 and words[1] == HEALTHZ_PATH

    def _safe_route_label(self) -> str:
        return HEALTHZ_PATH if self._is_exact_healthz() else "other"

    def _send_fixed_json(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
        omit_body: bool = False,
    ) -> None:
        self.close_connection = True
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-T007F-Probe", "db-free-iam-probe")
        self.send_header("Connection", "close")
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        if not omit_body:
            self.wfile.write(body)
        _log_event(
            {
                "event": "request",
                "method": self._safe_method_label(),
                "route": self._safe_route_label(),
                "status": int(status),
            }
        )

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Fail closed with fixed JSON; never echo untrusted method/path/error text."""
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.BAD_REQUEST
        omit_body = getattr(self, "command", None) == "HEAD" or code < 200 or code in (204, 304)
        self._send_fixed_json(status, ERROR_BODY, omit_body=omit_body)

    def _reject_non_get(self, *, omit_body: bool = False) -> None:
        if self._is_exact_healthz():
            self._send_fixed_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                METHOD_NOT_ALLOWED_BODY,
                extra_headers=(("Allow", "GET"),),
                omit_body=omit_body,
            )
            return
        self._send_fixed_json(
            HTTPStatus.NOT_FOUND,
            NOT_FOUND_BODY,
            omit_body=omit_body,
        )

    def do_GET(self) -> None:  # noqa: N802
        if self._is_exact_healthz():
            self._send_fixed_json(HTTPStatus.OK, HEALTHZ_BODY)
            return
        self._send_fixed_json(HTTPStatus.NOT_FOUND, NOT_FOUND_BODY)

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject_non_get(omit_body=True)

    def do_POST(self) -> None:  # noqa: N802
        self._reject_non_get()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_non_get()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_non_get()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_non_get()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject_non_get()

    def do_TRACE(self) -> None:  # noqa: N802
        self._reject_non_get()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._reject_non_get()


class ProbeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self) -> None:
        # Avoid HTTPServer's socket.getfqdn() reverse-DNS lookup and host disclosure.
        socketserver.TCPServer.server_bind(self)
        _, port = self.server_address[:2]
        self.server_name = "iam-probe"
        self.server_port = port


def create_server(host: str = DEFAULT_HOST, port: int | None = None) -> ProbeHTTPServer:
    resolved_port = resolve_port() if port is None else port
    return ProbeHTTPServer((host, resolved_port), ProbeRequestHandler)


def main() -> int:
    try:
        port = resolve_port()
    except ValueError:
        _log_event({"event": "startup_error", "reason": "invalid_port"})
        return 2

    server = create_server(host=DEFAULT_HOST, port=port)
    shutdown_started = threading.Event()

    def _handle_signal(_signum: int, _frame: object) -> None:
        if not shutdown_started.is_set():
            shutdown_started.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _log_event({"event": "probe_started"})
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        _log_event({"event": "probe_stopped"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
