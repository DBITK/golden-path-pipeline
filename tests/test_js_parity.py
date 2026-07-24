"""Prove the browser judge and the Python judge agree.

The interactive page on GitHub Pages runs a JavaScript port of the judge. A
port that silently drifts from the original is worse than no port at all: the
page would be demonstrating statistics the pipeline does not actually use.

This test feeds identical fixtures to both implementations and compares them.
Rank statistics must match exactly; p-values are allowed 1e-6, which is the
accuracy floor of the error-function approximation the JavaScript uses.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from goldenpath.canary.judge import MetricSpec, judge
from goldenpath.canary.stats import mann_whitney_u, percentile

HARNESS = Path(__file__).resolve().parent / "parity_harness.js"
P_VALUE_TOLERANCE = 1e-6

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node.js is not installed; the JavaScript parity check cannot run",
)


def sample(mean: float, sigma: float, n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sigma) for _ in range(n)]


def run_js(fixture: dict) -> dict:
    completed = subprocess.run(  # noqa: S603 - fixed argv, local harness
        ["node", str(HARNESS)],
        input=json.dumps(fixture),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node harness failed:\n{completed.stderr}")
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def fixture() -> dict:
    canary_latency = sample(120.0, 15.0, 300, seed=101)
    baseline_latency = sample(100.0, 15.0, 300, seed=102)
    canary_errors = sample(2.0, 0.8, 60, seed=103)
    baseline_errors = sample(0.5, 0.3, 60, seed=104)

    return {
        "canary_flat": canary_latency,
        "baseline_flat": baseline_latency,
        "canary_series": {
            "request_latency_ms": canary_latency,
            "error_rate_pct": canary_errors,
        },
        "baseline_series": {
            "request_latency_ms": baseline_latency,
            "error_rate_pct": baseline_errors,
        },
        "specs": [
            {
                "name": "error_rate_pct",
                "direction": "increase",
                "weight": 5,
                "critical": True,
                "tolerance": 0.2,
                "minSamples": 20,
            },
            {
                "name": "request_latency_ms",
                "direction": "increase",
                "weight": 3,
                "critical": False,
                "tolerance": 0.25,
                "minSamples": 100,
            },
        ],
        "pass_threshold": 95.0,
        "marginal_threshold": 75.0,
    }


@pytest.fixture(scope="module")
def js_result(fixture) -> dict:
    return run_js(fixture)


class TestStatisticalParity:
    def test_u_statistic_matches_exactly(self, fixture, js_result):
        expected = mann_whitney_u(fixture["canary_flat"], fixture["baseline_flat"])
        assert js_result["mann_whitney"]["u_statistic"] == pytest.approx(
            expected.u_statistic, abs=1e-9
        )

    def test_cliffs_delta_matches_exactly(self, fixture, js_result):
        expected = mann_whitney_u(fixture["canary_flat"], fixture["baseline_flat"])
        assert js_result["mann_whitney"]["cliffs_delta"] == pytest.approx(
            expected.cliffs_delta, abs=1e-9
        )

    def test_z_score_matches_exactly(self, fixture, js_result):
        expected = mann_whitney_u(fixture["canary_flat"], fixture["baseline_flat"])
        assert js_result["mann_whitney"]["z_score"] == pytest.approx(expected.z_score, abs=1e-9)

    def test_p_value_matches_within_the_erf_approximation_floor(self, fixture, js_result):
        expected = mann_whitney_u(fixture["canary_flat"], fixture["baseline_flat"])
        assert js_result["mann_whitney"]["p_value"] == pytest.approx(
            expected.p_value, abs=P_VALUE_TOLERANCE
        )

    def test_percentiles_match_exactly(self, fixture, js_result):
        assert js_result["percentiles"]["p50"] == pytest.approx(
            percentile(fixture["canary_flat"], 50.0), abs=1e-9
        )
        assert js_result["percentiles"]["p95"] == pytest.approx(
            percentile(fixture["canary_flat"], 95.0), abs=1e-9
        )


class TestJudgmentParity:
    def _python_judgment(self, fixture):
        specs = [
            MetricSpec(
                name=s["name"],
                weight=s["weight"],
                critical=s["critical"],
                tolerance=s["tolerance"],
                min_samples=s["minSamples"],
            )
            for s in fixture["specs"]
        ]
        return judge(
            specs,
            fixture["canary_series"],
            fixture["baseline_series"],
            pass_threshold=fixture["pass_threshold"],
            marginal_threshold=fixture["marginal_threshold"],
        )

    def test_score_and_verdict_match(self, fixture, js_result):
        expected = self._python_judgment(fixture)
        assert js_result["judgment"]["score"] == pytest.approx(expected.score, abs=1e-9)
        assert js_result["judgment"]["verdict"] == expected.verdict.value

    def test_every_metric_is_classified_identically(self, fixture, js_result):
        expected = self._python_judgment(fixture)
        by_name = {r["name"]: r for r in js_result["judgment"]["classifications"]}
        assert set(by_name) == {r.name for r in expected.results}

        for metric in expected.results:
            actual = by_name[metric.name]
            assert actual["classification"] == metric.classification.value, metric.name
            assert actual["cliffs_delta"] == pytest.approx(metric.cliffs_delta, abs=1e-9)
            assert actual["p_value"] == pytest.approx(metric.p_value, abs=P_VALUE_TOLERANCE)

    def test_this_fixture_actually_exercises_a_failure(self, fixture):
        # A parity test that only ever compares two PASS verdicts proves very
        # little. The fixture is built so the critical metric regresses.
        expected = self._python_judgment(fixture)
        assert expected.verdict.value == "FAIL"
        assert any(f.critical for f in expected.failures)
