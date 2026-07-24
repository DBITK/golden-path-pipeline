"""Paved-road demo service.

The workload the pipeline ships. It is deliberately small and dependency-free,
but it behaves like a real service in the ways the pipeline cares about: it
serves traffic, reports health, exposes resource gauges, and its latency and
error characteristics are controlled by environment variables.

That last part is the point. The pipeline's job is to catch a bad version, so
the repository needs a way to deploy a genuinely bad version on demand:

    ERROR_RATE=0.06 LATENCY_MS=95 python app.py

is a build that the canary judge is expected to reject.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = os.environ.get("VERSION", "0.0.0-dev")
ROLE = os.environ.get("ROLE", "baseline")
PORT = int(os.environ.get("PORT", "8080"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.002"))
LATENCY_MS = float(os.environ.get("LATENCY_MS", "18"))
LATENCY_JITTER_MS = float(os.environ.get("LATENCY_JITTER_MS", "6"))
SEED = os.environ.get("SEED")

_rng = random.Random(int(SEED) if SEED is not None else None)
_rng_lock = threading.Lock()

_started_at = time.time()
_requests_total = 0
_errors_total = 0
_counter_lock = threading.Lock()


def _simulated_latency_seconds() -> float:
    """Right-skewed latency, because real latency is never symmetric."""
    with _rng_lock:
        # Lognormal gives the long tail; the floor keeps it physically sane.
        tail = _rng.lognormvariate(0.0, 0.45)
    millis = LATENCY_MS * tail + _rng.uniform(0.0, LATENCY_JITTER_MS)
    return max(millis, 0.5) / 1000.0


def _should_error() -> bool:
    with _rng_lock:
        return _rng.random() < ERROR_RATE


def _resource_gauges() -> dict:
    """Stand-in for the resource signals a real service would export.

    Saturation tracks configured latency, mimicking a service where slower
    request handling means threads are held longer.
    """
    with _rng_lock:
        noise = _rng.uniform(-3.0, 3.0)
    saturation = min(99.0, max(1.0, LATENCY_MS * 1.4 + noise))
    return {
        "cpu_saturation_pct": round(saturation, 3),
        "heap_used_mb": round(64.0 + saturation * 1.2, 3),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        """Silence per-request logging; the pipeline collects its own metrics."""

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Service-Version", VERSION)
        self.send_header("X-Service-Role", ROLE)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        global _requests_total, _errors_total

        path = self.path.split("?", 1)[0]

        if path == "/health":
            self._respond(
                200,
                {
                    "status": "ok",
                    "version": VERSION,
                    "role": ROLE,
                    "uptime_s": round(time.time() - _started_at, 3),
                },
            )
            return

        if path == "/metrics":
            with _counter_lock:
                totals = {"requests_total": _requests_total, "errors_total": _errors_total}
            self._respond(200, {"version": VERSION, "role": ROLE, **totals, **_resource_gauges()})
            return

        if path == "/api/work":
            time.sleep(_simulated_latency_seconds())
            failed = _should_error()
            with _counter_lock:
                _requests_total += 1
                if failed:
                    _errors_total += 1
            if failed:
                self._respond(500, {"error": "dependency_timeout", "version": VERSION})
            else:
                self._respond(200, {"result": "ok", "version": VERSION, "role": ROLE})
            return

        self._respond(404, {"error": "not_found", "path": path})


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not all signals are available on every platform or thread.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, shutdown)

    print(
        f"paved-road-demo version={VERSION} role={ROLE} port={PORT} "
        f"error_rate={ERROR_RATE} latency_ms={LATENCY_MS}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
