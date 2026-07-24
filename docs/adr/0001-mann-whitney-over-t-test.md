# ADR 0001: Use Mann-Whitney U with an effect-size gate for canary judgment

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

The canary judge must decide whether a candidate build's metrics are worse than
the baseline's, from samples collected over a few minutes of load. The decision
gates production traffic, so both error modes are expensive:

- **False negative** — a regression is promoted, and the canary provided false
  assurance.
- **False positive** — a healthy build is blocked. Repeated a few times, the
  team stops trusting the gate and someone disables it. A gate that is switched
  off protects nothing, so this failure mode is not the "safe" one it appears
  to be.

Latency distributions are heavily right-skewed and frequently multi-modal
(cache hit versus miss, fast path versus slow path). Sample sizes vary with how
long the canary runs.

## Decision

Judge each metric with a **two-sided Mann-Whitney U test**, and fail it only
when the difference is **both statistically significant and materially large**,
where materiality is **Cliff's delta** against a per-metric tolerance.

Aggregate per-metric results into a weighted score compared against pass and
marginal thresholds, with two overrides: any metric marked `critical` fails the
canary alone, and metrics with insufficient samples are excluded from the score
rather than counted either way.

## Alternatives considered

**Student's t-test on the mean.** Rejected. It assumes approximate normality,
which latency violates badly. A single 5-second garbage-collection pause moves
the mean far more than it moves user experience, so the test would fire on
noise while missing a genuine shift in the bulk of the distribution.

**Compare p95 directly against a fixed threshold.** Rejected as the primary
test. Fixed thresholds require per-service tuning, go stale as traffic patterns
change, and cannot distinguish "p95 is 210ms because the canary is broken" from
"p95 is 210ms because it is Tuesday afternoon". Comparing against a
simultaneously-measured baseline removes that confound. The p95 is still
reported for every metric, because it is what an engineer reads first.

**Significance alone, without an effect-size gate.** Rejected, and this is the
most important rejection. Statistical significance is a function of sample size:
run a canary long enough and a 0.3ms latency difference becomes significant at
p < 0.001. It is also completely irrelevant. A gate that blocks releases over
differences nobody can perceive is a gate that will be removed.

**Kolmogorov-Smirnov.** Reasonable, and sensitive to distribution shape changes
that Mann-Whitney can miss. Rejected for now because its statistic does not
convert into an interpretable effect size the way U does — Cliff's delta falls
directly out of U — and interpretability is what makes a rejection actionable.

## Consequences

**Good.** No distributional assumptions. Cliff's delta is exact rather than
estimated, since it is derived directly from U. A rejection reports p-value,
effect size, medians, and p95s, so the engineer reading it knows what moved and
by how much. Tolerance is tunable per metric, so error rate can be strict while
CPU saturation is loose.

**Bad.** The normal approximation to U needs roughly 20+ samples per side to be
trustworthy, which sets a floor on canary duration; `minSamples` enforces it and
reports `Nodata` below it. Mann-Whitney tests a shift in central tendency, so a
change affecting only the extreme tail — a 1-in-500 request timing out — can pass
while a threshold check on p99 would catch it. Ranking is O(n log n), irrelevant
at these sample sizes.

**Mitigation for the tail-sensitivity gap.** Error rate is a separate metric
marked `critical`, which is what tail failures usually surface as. A future
revision could add a p99-specific metric or a K-S test alongside.

## Verification

`tests/test_stats.py` checks the U statistic, z-score, and p-value against
hand-computed values, plus tie handling, empty samples, and bounds.
`tests/test_judge.py` pins the behaviours this decision exists to produce —
notably that a significant-but-small difference passes, that an improvement in a
one-directional metric passes, and that a critical metric fails a canary whose
weighted score would otherwise promote it.
