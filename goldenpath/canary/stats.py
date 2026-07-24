"""Statistical primitives for automated canary analysis.

Deliberately dependency-free. The judge decides whether production traffic
moves, so every test it relies on is implemented here against the standard
library where it can be audited line by line -- rather than hidden behind a
SciPy call whose assumptions nobody on the team has checked.

The test of record is the two-sided Mann-Whitney U test: a rank-based,
non-parametric test that does not assume normality. Latency distributions are
heavily right-skewed, so a t-test on the mean is the wrong instrument.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class MannWhitneyResult:
    """Outcome of a two-sided Mann-Whitney U test.

    Attributes:
        u_statistic: U for the first sample (the canary).
        z_score: Normal approximation of U, tie-corrected.
        p_value: Two-sided p-value.
        cliffs_delta: Effect size in [-1, 1]. Positive means the canary tends
            to produce larger values than the baseline. Derived directly from
            U, so it is exact rather than estimated.
    """

    u_statistic: float
    z_score: float
    p_value: float
    cliffs_delta: float


def fractional_ranks(values: list[float]) -> list[float]:
    """Rank values 1..n ascending, assigning tied values their midpoint rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        # Positions i..j hold ranks i+1..j+1; tied entries share the midpoint.
        midpoint = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = midpoint
        i = j + 1
    return ranks


def _tie_correction_term(values: list[float]) -> float:
    """Sum of (t^3 - t) over tie groups, used to shrink the variance of U."""
    total = 0.0
    for count in _tie_group_sizes(values):
        total += count**3 - count
    return total


def _tie_group_sizes(values: list[float]) -> list[int]:
    sizes: list[int] = []
    ordered = sorted(values)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[i]:
            j += 1
        sizes.append(j - i + 1)
        i = j + 1
    return sizes


def normal_cdf(z: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def mann_whitney_u(canary: list[float], baseline: list[float]) -> MannWhitneyResult:
    """Two-sided Mann-Whitney U test comparing canary against baseline.

    Uses the normal approximation with a continuity correction and a tie
    correction on the variance. Both samples must be non-empty.

    Raises:
        ValueError: if either sample is empty.
    """
    n1, n2 = len(canary), len(baseline)
    if n1 == 0 or n2 == 0:
        raise ValueError("Mann-Whitney U requires two non-empty samples")

    combined = list(canary) + list(baseline)
    ranks = fractional_ranks(combined)

    rank_sum_canary = sum(ranks[:n1])
    u_canary = rank_sum_canary - n1 * (n1 + 1) / 2.0

    # Cliff's delta falls out of U exactly: it is the probability the canary
    # exceeds the baseline minus the probability of the reverse.
    cliffs_delta = (2.0 * u_canary) / (n1 * n2) - 1.0

    total = n1 + n2
    mean_u = n1 * n2 / 2.0
    tie_term = _tie_correction_term(combined)

    if total < 2:  # noqa: SIM108 - the branch names the degenerate case
        variance = 0.0
    else:
        variance = (n1 * n2 / 12.0) * ((total + 1) - tie_term / (total * (total - 1)))

    if variance <= 0:
        # Every observation is identical: the samples are indistinguishable.
        return MannWhitneyResult(u_canary, 0.0, 1.0, cliffs_delta)

    sigma = math.sqrt(variance)
    # Continuity correction pulls U half a step toward its mean.
    numerator = abs(u_canary - mean_u) - 0.5
    if numerator < 0:
        numerator = 0.0
    z = numerator / sigma
    if u_canary < mean_u:
        z = -z

    p_value = 2.0 * (1.0 - normal_cdf(abs(z)))
    p_value = min(1.0, max(0.0, p_value))

    return MannWhitneyResult(u_canary, z, p_value, cliffs_delta)


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile. `q` is in [0, 100]."""
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0.0 <= q <= 100.0:
        raise ValueError("q must be within [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
