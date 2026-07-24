"""Tests for canary judgment.

The judge has two jobs that pull against each other: catch real regressions,
and refuse to cry wolf. Both are pinned here, including the cases that are
easy to get wrong -- a tiny-but-significant difference, an improvement in a
one-directional metric, and a metric that reported nothing at all.
"""

from __future__ import annotations

import random

import pytest

from goldenpath.canary.judge import (
    Classification,
    Direction,
    MetricSpec,
    Verdict,
    classify_metric,
    judge,
)


def normal_sample(mean: float, sigma: float, n: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sigma) for _ in range(n)]


class TestClassifyMetric:
    def test_statistically_identical_samples_pass(self):
        spec = MetricSpec(name="latency", min_samples=20)
        result = classify_metric(
            spec,
            normal_sample(100.0, 10.0, 200, seed=1),
            normal_sample(100.0, 10.0, 200, seed=2),
        )
        assert result.classification is Classification.PASS
        assert result.passed

    def test_large_regression_in_the_watched_direction_is_flagged_high(self):
        spec = MetricSpec(name="latency", direction=Direction.INCREASE, min_samples=20)
        result = classify_metric(
            spec,
            normal_sample(200.0, 10.0, 100, seed=3),
            normal_sample(100.0, 10.0, 100, seed=4),
        )
        assert result.classification is Classification.HIGH
        assert not result.passed
        assert result.cliffs_delta > 0.9

    def test_improvement_in_a_one_directional_metric_passes(self):
        # Latency halving is significant, large, and good. Flagging it would
        # train everyone to ignore the canary.
        spec = MetricSpec(name="latency", direction=Direction.INCREASE, min_samples=20)
        result = classify_metric(
            spec,
            normal_sample(50.0, 5.0, 100, seed=5),
            normal_sample(100.0, 5.0, 100, seed=6),
        )
        assert result.classification is Classification.PASS
        assert "improving direction" in result.reason

    def test_throughput_drop_is_flagged_low(self):
        spec = MetricSpec(name="throughput", direction=Direction.DECREASE, min_samples=20)
        result = classify_metric(
            spec,
            normal_sample(50.0, 5.0, 100, seed=7),
            normal_sample(100.0, 5.0, 100, seed=8),
        )
        assert result.classification is Classification.LOW
        assert not result.passed

    def test_either_direction_flags_an_increase_too(self):
        spec = MetricSpec(name="cache_hit", direction=Direction.EITHER, min_samples=20)
        result = classify_metric(
            spec,
            normal_sample(120.0, 5.0, 100, seed=9),
            normal_sample(100.0, 5.0, 100, seed=10),
        )
        assert result.classification is Classification.HIGH

    def test_significant_but_tiny_difference_is_within_tolerance(self):
        # 102.7ms vs 100.0ms with 3000 samples a side: overwhelmingly
        # significant (p is effectively zero) but Cliff's delta lands near
        # 0.15, well inside a 0.3 tolerance. A 2.7% latency shift is not worth
        # blocking a release over, and the tolerance gate is what stops it.
        spec = MetricSpec(
            name="latency", direction=Direction.INCREASE, tolerance=0.3, min_samples=20
        )
        result = classify_metric(
            spec,
            normal_sample(102.7, 10.0, 3000, seed=11),
            normal_sample(100.0, 10.0, 3000, seed=12),
        )
        assert result.p_value is not None
        assert result.p_value < spec.significance  # genuinely significant
        assert result.classification is Classification.PASS
        assert abs(result.cliffs_delta) < 0.3
        assert "within tolerance" in result.reason

    def test_too_few_samples_reports_nodata_rather_than_guessing(self):
        spec = MetricSpec(name="latency", min_samples=50)
        result = classify_metric(
            spec,
            normal_sample(100.0, 10.0, 10, seed=13),
            normal_sample(100.0, 10.0, 10, seed=14),
        )
        assert result.classification is Classification.NODATA
        assert "insufficient samples" in result.reason

    def test_nodata_when_only_one_side_is_missing(self):
        spec = MetricSpec(name="latency", min_samples=20)
        result = classify_metric(spec, normal_sample(100.0, 10.0, 100, seed=15), [])
        assert result.classification is Classification.NODATA

    def test_reports_summary_statistics_for_the_engineer_reading_the_failure(self):
        spec = MetricSpec(name="latency", min_samples=20)
        result = classify_metric(
            spec,
            normal_sample(200.0, 10.0, 100, seed=16),
            normal_sample(100.0, 10.0, 100, seed=17),
        )
        assert result.canary_median == pytest.approx(200.0, abs=5.0)
        assert result.baseline_median == pytest.approx(100.0, abs=5.0)
        assert result.canary_p95 > result.canary_median
        assert result.canary_count == 100


class TestJudge:
    def _healthy(self, n: int = 200) -> dict[str, list[float]]:
        return {
            "error_rate_pct": normal_sample(0.1, 0.05, n, seed=21),
            "request_latency_ms": normal_sample(100.0, 10.0, n, seed=22),
        }

    def test_healthy_canary_scores_full_marks_and_passes(self):
        specs = [
            MetricSpec("error_rate_pct", weight=5, critical=True),
            MetricSpec("request_latency_ms", weight=3),
        ]
        result = judge(
            specs,
            canary_samples=self._healthy(),
            baseline_samples={
                "error_rate_pct": normal_sample(0.1, 0.05, 200, seed=23),
                "request_latency_ms": normal_sample(100.0, 10.0, 200, seed=24),
            },
        )
        assert result.verdict is Verdict.PASS
        assert result.score == pytest.approx(100.0)
        assert result.promotable

    def test_critical_metric_failure_fails_regardless_of_score(self):
        # Seven healthy metrics outvote one critical failure on weight alone.
        # They must not be allowed to.
        specs = [MetricSpec("error_rate_pct", weight=1, critical=True)]
        specs += [MetricSpec(f"filler_{i}", weight=10) for i in range(7)]

        canary = {"error_rate_pct": normal_sample(5.0, 0.5, 100, seed=25)}
        baseline = {"error_rate_pct": normal_sample(0.1, 0.05, 100, seed=26)}
        for i in range(7):
            canary[f"filler_{i}"] = normal_sample(10.0, 1.0, 100, seed=30 + i)
            baseline[f"filler_{i}"] = normal_sample(10.0, 1.0, 100, seed=40 + i)

        result = judge(specs, canary, baseline)
        assert result.verdict is Verdict.FAIL
        assert result.score > 95.0  # The weighted score alone would have promoted it.
        assert "Critical metric regression" in result.summary
        assert not result.promotable

    def test_partial_failure_lands_in_the_marginal_band(self):
        specs = [
            MetricSpec("good", weight=8),
            MetricSpec("bad", weight=2),
        ]
        canary = {
            "good": normal_sample(100.0, 10.0, 200, seed=50),
            "bad": normal_sample(200.0, 10.0, 200, seed=51),
        }
        baseline = {
            "good": normal_sample(100.0, 10.0, 200, seed=52),
            "bad": normal_sample(100.0, 10.0, 200, seed=53),
        }
        result = judge(specs, canary, baseline, pass_threshold=95.0, marginal_threshold=75.0)
        assert result.score == pytest.approx(80.0)
        assert result.verdict is Verdict.MARGINAL
        assert not result.promotable

    def test_nodata_metrics_are_excluded_rather_than_counted_as_passes(self):
        specs = [
            MetricSpec("present", weight=1, min_samples=20),
            MetricSpec("missing", weight=99, min_samples=20),
        ]
        result = judge(
            specs,
            canary_samples={"present": normal_sample(100.0, 10.0, 100, seed=60)},
            baseline_samples={"present": normal_sample(100.0, 10.0, 100, seed=61)},
        )
        # If `missing` had been counted as a pass it would dominate the score
        # on weight; if counted as a fail it would sink a healthy build. It is
        # excluded, and the exclusion is disclosed.
        assert result.score == pytest.approx(100.0)
        assert result.verdict is Verdict.PASS
        assert "Excluded 1 metric" in result.summary

    def test_no_data_at_all_refuses_to_promote(self):
        specs = [MetricSpec("nothing", min_samples=20)]
        result = judge(specs, canary_samples={}, baseline_samples={})
        assert result.verdict is Verdict.FAIL
        assert result.score == 0.0
        assert "refusing to promote" in result.summary

    def test_requires_at_least_one_metric(self):
        with pytest.raises(ValueError, match="at least one metric"):
            judge([], {}, {})

    def test_failures_are_listed_for_reporting(self):
        specs = [MetricSpec("latency", weight=1)]
        result = judge(
            specs,
            {"latency": normal_sample(300.0, 10.0, 100, seed=70)},
            {"latency": normal_sample(100.0, 10.0, 100, seed=71)},
        )
        assert [f.name for f in result.failures] == ["latency"]
        assert result.to_dict()["verdict"] == "FAIL"
