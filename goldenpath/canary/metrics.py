"""Metric collection: drive real traffic, measure what comes back.

Kayenta reads metrics out of Atlas, Prometheus, or Stackdriver. This collector
plays the same role for the demo workload -- it generates load against the
baseline and canary *simultaneously* and records what it observes.

Simultaneous is not a detail. If the baseline were measured first and the
canary second, every difference between them would be confounded with whatever
else changed on the machine in between: a noisy neighbour, a CPU frequency
step, a background job. Netflix's canary practice compares a canary against a
freshly deployed baseline of the current version under the same conditions at
the same moment, and so does this.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# Metric names the judge can be configured against.
LATENCY = "request_latency_ms"
ERROR_RATE = "error_rate_pct"
THROUGHPUT = "throughput_rps"
CPU_SATURATION = "cpu_saturation_pct"


@dataclass
class Sample:
    latency_ms: float
    ok: bool


@dataclass
class CollectedMetrics:
    """Per-metric sample series, keyed by the names above."""

    series: dict[str, list[float]] = field(default_factory=dict)
    total_requests: int = 0
    total_errors: int = 0

    @property
    def observed_error_rate_pct(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return 100.0 * self.total_errors / self.total_requests


def _one_request(url: str, timeout: float) -> Sample:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read()
            ok = 200 <= response.status < 400
    except urllib.error.HTTPError:
        ok = False
    except (urllib.error.URLError, TimeoutError, OSError):
        ok = False
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return Sample(latency_ms=elapsed_ms, ok=ok)


def _scrape_gauge(base_url: str, name: str, timeout: float) -> float | None:
    try:
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    value = payload.get(name)
    return float(value) if isinstance(value, (int, float)) else None


def _run_window(
    base_url: str,
    requests_per_window: int,
    concurrency: int,
    timeout: float,
) -> tuple[list[Sample], float]:
    url = f"{base_url}/api/work"
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(lambda _: _one_request(url, timeout), range(requests_per_window)))
    duration_s = max(time.perf_counter() - started, 1e-6)
    return samples, duration_s


def collect(
    baseline_url: str,
    canary_url: str,
    windows: int = 30,
    requests_per_window: int = 20,
    concurrency: int = 8,
    timeout: float = 5.0,
) -> tuple[CollectedMetrics, CollectedMetrics]:
    """Drive load against both deployments and return their metric series.

    Each window contributes one aggregate point to the rate-style metrics and
    every individual request contributes a latency sample, so `windows` should
    be at least the judge's `min_samples` for the rate metrics to be scored.

    Returns:
        (baseline_metrics, canary_metrics)
    """
    baseline = CollectedMetrics()
    canary = CollectedMetrics()

    for name in (LATENCY, ERROR_RATE, THROUGHPUT, CPU_SATURATION):
        baseline.series[name] = []
        canary.series[name] = []

    for _ in range(windows):
        # Both sides in flight together; neither gets a quieter machine.
        with ThreadPoolExecutor(max_workers=2) as pool:
            baseline_future = pool.submit(
                _run_window, baseline_url, requests_per_window, concurrency, timeout
            )
            canary_future = pool.submit(
                _run_window, canary_url, requests_per_window, concurrency, timeout
            )
            baseline_samples, baseline_duration = baseline_future.result()
            canary_samples, canary_duration = canary_future.result()

        for metrics, samples, duration, url in (
            (baseline, baseline_samples, baseline_duration, baseline_url),
            (canary, canary_samples, canary_duration, canary_url),
        ):
            errors = sum(1 for s in samples if not s.ok)
            metrics.total_requests += len(samples)
            metrics.total_errors += errors
            metrics.series[LATENCY].extend(s.latency_ms for s in samples)
            metrics.series[ERROR_RATE].append(100.0 * errors / max(len(samples), 1))
            metrics.series[THROUGHPUT].append(len(samples) / duration)
            gauge = _scrape_gauge(url, CPU_SATURATION, timeout)
            if gauge is not None:
                metrics.series[CPU_SATURATION].append(gauge)

    return baseline, canary
