"""The stable production endpoint, and the thing red/black actually switches.

Red/black (Spinnaker's name for blue/green) is often described as "deploy the
new version and switch traffic". The switch is the part that matters, and it
has to be genuinely atomic: at no instant may the endpoint serve a mix of
versions, and reverting has to be as cheap as switching.

This router is a small reverse proxy holding one mutable target. `switch_to`
takes a lock and replaces it, so a request either resolves against the old
target or the new one and never against a half-updated state. Rollback is the
same operation pointed the other way, which is why rollback is fast and boring
-- exactly what you want at 2am.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .executors.process import free_port


@dataclass
class SwitchEvent:
    from_target: str | None
    to_target: str
    reason: str


class TrafficRouter:
    """A reverse proxy with one atomically swappable backend."""

    def __init__(self) -> None:
        self._target: str | None = None
        self._target_name: str | None = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0
        self.history: list[SwitchEvent] = []

    # ---------------------------------------------------------------- state --
    @property
    def target(self) -> str | None:
        with self._lock:
            return self._target

    @property
    def target_name(self) -> str | None:
        with self._lock:
            return self._target_name

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def switch_to(self, url: str, name: str, reason: str = "") -> SwitchEvent:
        """Point the endpoint at a new backend, atomically."""
        with self._lock:
            event = SwitchEvent(from_target=self._target_name, to_target=name, reason=reason)
            self._target = url.rstrip("/")
            self._target_name = name
            self.history.append(event)
        return event

    # -------------------------------------------------------------- lifecycle --
    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("router is already running")
        self.port = free_port()
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:  # noqa: A002
                """Quiet: the pipeline records its own telemetry."""

            def do_GET(self) -> None:  # noqa: N802 - stdlib signature
                target = router.target
                if target is None:
                    self._fail(503, b'{"error":"no_active_server_group"}')
                    return
                try:
                    with urllib.request.urlopen(f"{target}{self.path}", timeout=10.0) as upstream:
                        body = upstream.read()
                        status = upstream.status
                        content_type = upstream.headers.get("Content-Type", "application/json")
                        version = upstream.headers.get("X-Service-Version", "")
                except urllib.error.HTTPError as exc:
                    body = exc.read() or b"{}"
                    status = exc.code
                    content_type = "application/json"
                    version = ""
                except (urllib.error.URLError, TimeoutError, OSError):
                    self._fail(502, b'{"error":"upstream_unreachable"}')
                    return

                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if version:
                    self.send_header("X-Service-Version", version)
                self.send_header("X-Routed-To", router.target_name or "")
                self.end_headers()
                self.wfile.write(body)

            def _fail(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None
