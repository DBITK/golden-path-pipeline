"""Kayenta-style automated canary judgment.

Mirrors the shape of Netflix's NetflixACAJudge: each configured metric is
classified independently against the baseline, then the classifications are
combined into a single weighted score which is compared against pass and
marginal thresholds.

Two guardrails matter more than the score itself:

* A metric may only fail if the difference is *both* statistically significant
  and large enough to care about. Significance alone is not enough -- with
  enough samples, a 0.3ms latency regression is significant and irrelevant.
* A metric marked `critical` fails the whole canary on its own, no matter how
  healthy the weighted score looks.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .stats import MannWhitneyResult, mann_whitney_u, percentile


class Classification(enum.StrEnum):
    """Per-metric outcome, using Kayenta's vocabulary."""

    PASS = "Pass"
    HIGH = "High"  # Canary significantly higher than baseline, and that is bad.
    LOW = "Low"  # Canary significantly lower than baseline, and that is bad.
    NODATA = "Nodata"
    ERROR = "Error"

    @property
    def is_failure(self) -> bool:
        return self in (Classification.HIGH, Classification.LOW, Classification.ERROR)


class Verdict(enum.StrEnum):
    PASS = "PASS"
    MARGINAL = "MARGINAL"
    FAIL = "FAIL"


class Direction(enum.StrEnum):
    """Which way a metric moving is considered a regression."""

    INCREASE = "increase"  # Only an increase is bad (latency, error rate).
    DECREASE = "decrease"  # Only a decrease is bad (throughput, cache hits).
    EITHER = "either"  # Any significant change is suspicious.


@dataclass(frozen=True)
class MetricSpec:
    """How one metric should be judged.

    Attributes:
        name: Metric identifier, matched against collected sample keys.
        direction: Which direction constitutes a regression.
        weight: Relative contribution to the aggregate score.
        critical: If true, a failure here fails the canary outright.
        tolerance: Minimum |Cliff's delta| for a significant difference to
            count as a real regression. Guards against statistically real but
            operationally meaningless changes.
        significance: Alpha for the Mann-Whitney test.
        min_samples: Below this, the metric is classified Nodata.
    """

    name: str
    direction: Direction = Direction.INCREASE
    weight: float = 1.0
    critical: bool = False
    tolerance: float = 0.2
    significance: float = 0.05
    min_samples: int = 20


@dataclass
class MetricResult:
    name: str
    classification: Classification
    reason: str
    weight: float
    critical: bool
    canary_count: int = 0
    baseline_count: int = 0
    canary_median: float | None = None
    baseline_median: float | None = None
    canary_p95: float | None = None
    baseline_p95: float | None = None
    p_value: float | None = None
    cliffs_delta: float | None = None

    @property
    def passed(self) -> bool:
        return not self.classification.is_failure

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "classification": self.classification.value,
            "reason": self.reason,
            "weight": self.weight,
            "critical": self.critical,
            "canary_count": self.canary_count,
            "baseline_count": self.baseline_count,
            "canary_median": self.canary_median,
            "baseline_median": self.baseline_median,
            "canary_p95": self.canary_p95,
            "baseline_p95": self.baseline_p95,
            "p_value": self.p_value,
            "cliffs_delta": self.cliffs_delta,
        }


@dataclass
class CanaryResult:
    """Aggregate judgment across every metric."""

    score: float
    verdict: Verdict
    results: list[MetricResult] = field(default_factory=list)
    pass_threshold: float = 95.0
    marginal_threshold: float = 75.0
    summary: str = ""

    @property
    def promotable(self) -> bool:
        return self.verdict is Verdict.PASS

    @property
    def failures(self) -> list[MetricResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 2),
            "verdict": self.verdict.value,
            "pass_threshold": self.pass_threshold,
            "marginal_threshold": self.marginal_threshold,
            "summary": self.summary,
            "metrics": [r.to_dict() for r in self.results],
        }


def _median(values: list[float]) -> float:
    return percentile(values, 50.0)


def classify_metric(
    spec: MetricSpec,
    canary: list[float],
    baseline: list[float],
) -> MetricResult:
    """Classify a single metric's canary sample against its baseline."""
    base = MetricResult(
        name=spec.name,
        classification=Classification.NODATA,
        reason="",
        weight=spec.weight,
        critical=spec.critical,
        canary_count=len(canary),
        baseline_count=len(baseline),
    )

    if len(canary) < spec.min_samples or len(baseline) < spec.min_samples:
        base.reason = (
            f"insufficient samples (canary={len(canary)}, baseline={len(baseline)}, "
            f"required={spec.min_samples})"
        )
        return base

    base.canary_median = _median(canary)
    base.baseline_median = _median(baseline)
    base.canary_p95 = percentile(canary, 95.0)
    base.baseline_p95 = percentile(baseline, 95.0)

    test: MannWhitneyResult = mann_whitney_u(canary, baseline)
    base.p_value = test.p_value
    base.cliffs_delta = test.cliffs_delta

    significant = test.p_value < spec.significance
    material = abs(test.cliffs_delta) >= spec.tolerance

    if not significant:
        base.classification = Classification.PASS
        base.reason = f"no significant difference (p={test.p_value:.4f})"
        return base

    if not material:
        base.classification = Classification.PASS
        base.reason = (
            f"significant but within tolerance "
            f"(p={test.p_value:.4f}, delta={test.cliffs_delta:+.3f}, "
            f"tolerance={spec.tolerance})"
        )
        return base

    canary_is_higher = test.cliffs_delta > 0
    regression = (
        (spec.direction is Direction.INCREASE and canary_is_higher)
        or (spec.direction is Direction.DECREASE and not canary_is_higher)
        or (spec.direction is Direction.EITHER)
    )

    if not regression:
        base.classification = Classification.PASS
        base.reason = (
            f"moved in the improving direction "
            f"(delta={test.cliffs_delta:+.3f}, p={test.p_value:.4f})"
        )
        return base

    base.classification = Classification.HIGH if canary_is_higher else Classification.LOW
    base.reason = (
        f"canary {'higher' if canary_is_higher else 'lower'} than baseline: "
        f"median {base.canary_median:.2f} vs {base.baseline_median:.2f}, "
        f"p95 {base.canary_p95:.2f} vs {base.baseline_p95:.2f} "
        f"(p={test.p_value:.4f}, delta={test.cliffs_delta:+.3f})"
    )
    return base


def judge(
    specs: list[MetricSpec],
    canary_samples: dict[str, list[float]],
    baseline_samples: dict[str, list[float]],
    pass_threshold: float = 95.0,
    marginal_threshold: float = 75.0,
) -> CanaryResult:
    """Judge a canary across every configured metric.

    Nodata metrics are excluded from the score rather than counted as passes:
    a metric that reported nothing is an unknown, and silently treating an
    unknown as healthy is how bad builds reach production.

    Returns:
        A CanaryResult whose verdict is PASS, MARGINAL, or FAIL.
    """
    if not specs:
        raise ValueError("canary judgment requires at least one metric spec")

    results = [
        classify_metric(
            spec,
            canary_samples.get(spec.name, []),
            baseline_samples.get(spec.name, []),
        )
        for spec in specs
    ]

    scored = [r for r in results if r.classification is not Classification.NODATA]
    if not scored:
        return CanaryResult(
            score=0.0,
            verdict=Verdict.FAIL,
            results=results,
            pass_threshold=pass_threshold,
            marginal_threshold=marginal_threshold,
            summary="No metric produced enough data to judge; refusing to promote.",
        )

    total_weight = sum(r.weight for r in scored)
    passing_weight = sum(r.weight for r in scored if r.passed)
    score = 100.0 * passing_weight / total_weight if total_weight else 0.0

    critical_failures = [r for r in results if not r.passed and r.critical]

    if critical_failures:
        verdict = Verdict.FAIL
        names = ", ".join(r.name for r in critical_failures)
        summary = f"Critical metric regression: {names}. Score {score:.1f} is not consulted."
    elif score >= pass_threshold:
        verdict = Verdict.PASS
        summary = f"Score {score:.1f} >= pass threshold {pass_threshold}."
    elif score >= marginal_threshold:
        verdict = Verdict.MARGINAL
        summary = (
            f"Score {score:.1f} sits between marginal ({marginal_threshold}) "
            f"and pass ({pass_threshold}) thresholds; human judgment required."
        )
    else:
        verdict = Verdict.FAIL
        summary = f"Score {score:.1f} < marginal threshold {marginal_threshold}."

    nodata = [r for r in results if r.classification is Classification.NODATA]
    if nodata:
        summary += f" Excluded {len(nodata)} metric(s) with insufficient data."

    return CanaryResult(
        score=score,
        verdict=verdict,
        results=results,
        pass_threshold=pass_threshold,
        marginal_threshold=marginal_threshold,
        summary=summary,
    )
