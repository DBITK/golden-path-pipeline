# Lineage: where these ideas came from, and where they live now

This repository implements a methodology, not a tool. That distinction matters,
because the tool most associated with the methodology is past its peak while
the methodology itself has become the default.

## The short version

Netflix built [Spinnaker](https://spinnaker.io/) to deploy across regions and
clouds at high velocity, and built [Kayenta](https://netflixtechblog.com/automated-canary-analysis-at-netflix-with-kayenta-3260bc7acc69)
to answer the question Spinnaker could not: *is this new version actually worse
than the one it is replacing?* Kayenta answered it statistically — Mann-Whitney
U over metrics scraped from canary and baseline server groups — rather than by
asking an engineer to squint at a dashboard.

Netflix then went further with [Managed Delivery](https://spinnaker.io/docs/guides/user/managed-delivery/),
which replaced hand-assembled pipelines with a declarative `spinnaker.yml`
stating *requirements* — artifacts, environments, constraints — and let the
platform work out the steps.

Today, Spinnaker is a niche choice. The 2025 CNCF end-user survey puts Argo CD
at roughly 60% of reported Kubernetes clusters with 97% of respondents running
it in production; Spinnaker is now mostly justified by multi-cloud deployment
at large scale with staff dedicated to operating it.

**But every idea survived the tool.**

| Netflix concept | Where it lives now |
|---|---|
| Declarative delivery config | GitOps: Argo CD `Application`, Flux `Kustomization` |
| Server groups, immutable artifacts | Kubernetes `ReplicaSet`, image digests |
| Red/black deployment | Argo Rollouts `blueGreen`, Flagger blue/green |
| Automated canary analysis | Argo Rollouts `AnalysisTemplate`, Flagger metric checks |
| Kayenta's statistical judge | Still Kayenta — Argo Rollouts ships a [`kayenta` provider](https://argoproj.github.io/argo-rollouts/analysis/kayenta/) |
| Environment constraints | Argo CD sync windows, `autoPromotionEnabled`, promotion gates |

The last row is the telling one. Argo Rollouts, the current default for
progressive delivery on Kubernetes, can delegate its analysis *to Kayenta*,
which still performs Mann-Whitney analysis against canary and baseline pods.
The statistics did not get replaced. They got a new host.

## What this repository takes from each

From **Kayenta**: the judge. Per-metric classification (`Pass` / `High` / `Low` /
`Nodata`), a rank-based significance test, and a weighted aggregate score
compared against pass and marginal thresholds. Implemented in
[`goldenpath/canary/judge.py`](../goldenpath/canary/judge.py).

From **Managed Delivery**: the declarative config. Environments, constraints,
and verifications are stated as requirements in
[`delivery/delivery.yml`](../delivery/delivery.yml); the orchestrator derives
the steps. No pipeline is assembled by hand.

From **Argo Rollouts**: the target. `goldenpath export-argo` compiles the same
delivery config into `Rollout` and `AnalysisTemplate` manifests, and CI fails if
they drift.

## Where the models genuinely disagree

Being precise about this is more useful than claiming clean equivalence.

**Weighted scoring.** Kayenta computes a weighted aggregate across metrics, so a
minor regression in a low-weight metric can be outvoted. Argo Rollouts has no
aggregate: each metric independently fails the analysis once it exceeds its
`failureLimit`. That is *stricter*, and it means `weight` has no native
expression. The exporter emits weights as annotations and documents the
degradation rather than dropping them silently — teams that need true weighted
scoring point Argo Rollouts at Kayenta via its provider.

**Effect size.** This judge requires a difference to be both significant and
material, using Cliff's delta. Prometheus has no rank-based effect size, so the
exporter converts the tolerance into a ratio guard against the baseline
(`result[0] <= result[1] * 1.25`). That compares point values rather than
distributions — coarser, and noted in the generated manifest.

**Time windows.** `allowed-times` has no direct Argo Rollouts equivalent. Argo
CD sync windows are the closest analogue but operate at the application sync
level, not per-environment promotion.

## Why an executor, not a Kubernetes cluster

The methodology is executor-agnostic, so this repository ships the smallest
executor that is still real: server groups are OS processes, and traffic reaches
them through an actual reverse proxy that performs an actual atomic switch.
Nothing is mocked — processes really start, really serve HTTP, and really get
killed on rollback.

The payoff is that a bad build can be deployed **on demand** to prove the gate
holds, in about 25 seconds, on any machine, for free. A canary gate that has
only ever been tested against synthetic arrays is a gate nobody should trust,
and the [Gate Drill](../.github/workflows/gate-drill.yml) exists to test it for
real on a schedule.

## Sources

- [Automated Canary Analysis at Netflix with Kayenta](https://netflixtechblog.com/automated-canary-analysis-at-netflix-with-kayenta-3260bc7acc69)
- [Managed Delivery: Evolving Continuous Delivery at Netflix](https://blog.spinnaker.io/managed-delivery-evolving-continuous-delivery-at-netflix-eb74877fb33c)
- [Spinnaker: Environment Constraints](https://spinnaker.io/docs/guides/user/managed-delivery/environment-constraints/)
- [Argo Rollouts: Kayenta provider](https://argoproj.github.io/argo-rollouts/analysis/kayenta/)
- [How canary judgment works](https://spinnaker.io/docs/guides/user/canary/judge/)
