# Golden Path

**A deployment pipeline that decides, statistically, whether your build is allowed to reach production — and rolls it back when it isn't.**

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![Golden Path Deploy](../../actions/workflows/golden-path.yml/badge.svg)](../../actions/workflows/golden-path.yml)
[![Gate Drill](../../actions/workflows/gate-drill.yml/badge.svg)](../../actions/workflows/gate-drill.yml)

Most "CI/CD portfolio projects" are a `deploy.sh` behind a green checkmark. This one implements the part that is actually hard: **automated canary analysis** — deciding, from live measurements and a statistical test, whether a new version is worse than the one it replaces.

### Try it without cloning anything

| | |
|---|---|
| **[Interactive canary simulator →](http://derekbartlett.com/golden-path-pipeline/)** | Move sliders, watch the real judge accept or reject the build in your browser. |
| **[Run the pipeline yourself →](../../actions/workflows/golden-path.yml)** | Actions → *Run workflow* → choose an error rate and latency. Ship a bad build on purpose and watch it get rejected. |
| **[Gate Drill →](../../actions/workflows/gate-drill.yml)** | A weekly job that deliberately ships broken builds and **fails if they get promoted**. |

---

## What it actually does

```
delivery.yml ──► orchestrator ──► test ──► staging ──► prod
   (intent)       (works out         │         │          │
                   the steps)        │         │          └─ constraints + canary + red/black
                                     │         └─ canary analysis, no human in the loop
                                     └─ smoke test
```

Deploying a candidate runs this in every environment:

1. **Evaluate constraints.** Has this version passed the previous environment? Are we inside the allowed deployment window? Blocked means stop — there is no override flag.
2. **Deploy a fresh baseline** running the *current* production version, alongside the **canary** running the candidate. Both are new, so neither benefits from a warm cache or a JIT that has been running for a week.
3. **Drive identical load through both simultaneously** and record latency, error rate, throughput, and saturation.
4. **Judge.** Mann–Whitney U per metric, an effect-size gate, then a weighted aggregate score.
5. **Promote or reject.** On pass, an atomic red/black traffic switch. On fail, the canary is destroyed and traffic never moved.

Step 5 is why rollback is trustworthy: the previous version is still running and still healthy when the decision is made. **Rollback is a pointer swap, not a redeploy under pressure.**

## The judge

Two rules do most of the work, and both exist because of how canary gates fail in practice.

**A metric must be both statistically significant and materially different to fail.** With enough samples, a 0.3 ms latency increase is significant and irrelevant. A gate that fails on p-values alone gets switched off within a month. Every metric must clear a p-value threshold *and* an effect-size threshold ([Cliff's delta](https://en.wikipedia.org/wiki/Effect_size)).

**One critical metric outranks the aggregate score.** A weighted score can be dragged up by healthy metrics. An error-rate regression fails the canary on its own, no matter how good everything else looks.

Two supporting rules: metrics with insufficient data are **excluded and disclosed**, never counted as passes — treating silence as health is how bad builds reach production. And a **MARGINAL** verdict is the only path that consults a human, so judgment stays meaningful rather than becoming a rubber stamp.

The test is Mann–Whitney U rather than a t-test because latency distributions are heavily right-skewed, and a mean is a poor summary of a distribution with a long tail. It is implemented from scratch in [`stats.py`](goldenpath/canary/stats.py) — with tie and continuity corrections — so the arithmetic deciding whether production traffic moves can be audited without leaving the repository.

## Everything is declared, nothing is scripted

Teams adopting the paved road write [`delivery/delivery.yml`](delivery/delivery.yml) and nothing else. There is no pipeline to assemble:

```yaml
environments:
  - name: prod
    constraints:
      - type: depends-on
        environment: staging
      - type: allowed-times          # No Friday-evening deploys.
        days: [monday, tuesday, wednesday, thursday]
        hoursUTC: "13-21"
      - type: manual-judgment        # Only when the statistics are ambiguous.
        onlyWhen: canary-marginal
    strategy:
      type: red-black
      rollbackWindowSeconds: 10
    verification:
      - type: canary
        metrics:
          - name: error_rate_pct
            direction: increase
            weight: 5
            critical: true           # Fails the canary alone.
            tolerance: 0.20
```

Validation **fails closed**: unknown keys are errors, not warnings. A typo in `passThreshold` would otherwise be a gate that silently does nothing while everyone believes it is there. A `depends-on` that points forward is rejected at parse time rather than deadlocking at 3am.

## It isn't Spinnaker-specific

The delivery config is the source of truth; this repository's orchestrator is one runtime for it. `goldenpath export-argo` compiles the same file into [**Argo Rollouts manifests**](deploy/argo-rollouts/generated.yaml) — `Rollout` plus `AnalysisTemplate` — and CI fails if the generated manifests drift from the config.

Where the models genuinely differ, the exporter says so instead of implying equivalence: Argo Rollouts has no weighted aggregate score, so metric weights are preserved as annotations and the generated analysis is stricter than the weighted judge. The full mapping and its caveats are in [docs/lineage.md](docs/lineage.md).

## Testing the safety net

The [**Gate Drill**](.github/workflows/gate-drill.yml) is inverted: it ships builds with a 6% error rate and a 110 ms latency regression and **fails if the pipeline promotes them**. It also ships a healthy build and fails if that one is *blocked* — a gate that rejects everything is as broken as one that rejects nothing, and only testing the failure case would miss it.

It runs weekly, because gates rot quietly: a threshold gets loosened to unblock a release and nobody tightens it again.

## AI operating the pipeline

[`claude.yml`](.github/workflows/claude.yml) puts Claude in the two places it earns its keep:

- **Reviewing changes to the gates themselves** — specifically watching for loosened thresholds, a metric losing its `critical` flag, or a constraint quietly becoming unenforced.
- **Triaging a rejected deployment** — reading the structured run record and opening an issue that distinguishes *a genuine regression* from *a flaky or underpowered canary*, because those demand opposite responses.

Authentication takes either a `CLAUDE_CODE_OAUTH_TOKEN` (generated by `claude setup-token`, drawing on an existing Claude subscription) or an `ANTHROPIC_API_KEY` (pay-per-use). With neither set, both jobs skip cleanly, so a fork stays green.

## Running it locally

```bash
pip install -e ".[dev]"
```

Validate the config, then promote a healthy build:

```bash
python -m goldenpath.cli validate
```

```bash
python -m goldenpath.cli deploy --candidate-version v2 --baseline-version v1
```

Now ship a genuinely bad one and watch the gate hold:

```bash
python -m goldenpath.cli deploy --candidate-version v3-bad --candidate-env ERROR_RATE=0.06 --candidate-env LATENCY_MS=95
```

The run takes about 25 seconds and prints a report naming every metric, its p-value, and its effect size. Exit codes distinguish *why* a run stopped: `1` rejected by canary, `2` blocked by a constraint, `3` awaiting human judgment, `4` invalid config.

```bash
pytest -v
```

## How it is put together

| Path | |
|---|---|
| [`delivery/delivery.yml`](delivery/delivery.yml) | The entire golden path, declared |
| [`goldenpath/canary/stats.py`](goldenpath/canary/stats.py) | Mann–Whitney U, Cliff's delta, percentiles — stdlib only |
| [`goldenpath/canary/judge.py`](goldenpath/canary/judge.py) | Metric classification and weighted judgment |
| [`goldenpath/canary/metrics.py`](goldenpath/canary/metrics.py) | Load generation and simultaneous measurement |
| [`goldenpath/constraints.py`](goldenpath/constraints.py) | Promotion constraints, in two phases |
| [`goldenpath/orchestrator.py`](goldenpath/orchestrator.py) | Deploy, verify, judge, switch or roll back |
| [`goldenpath/router.py`](goldenpath/router.py) | The atomic red/black traffic switch |
| [`goldenpath/exporters/argo_rollouts.py`](goldenpath/exporters/argo_rollouts.py) | Compiles the delivery config to Argo Rollouts |
| [`docs/site/`](docs/site/) | The interactive judge, published to Pages |

The only runtime dependency is PyYAML. The statistics, the executor, the router, and the judge are all standard library.

### A note on scope

The workload is a small service whose latency and error rate are set by environment variables, and server groups are OS processes rather than Kubernetes pods. That is deliberate: it makes every run hermetic, free, reproducible on any machine, and fast enough to execute on every push — and it lets the repository deploy a *genuinely bad build on demand* to prove the gate holds, which is the thing worth demonstrating.

The parts that would carry over to a real deployment — the delivery config, the constraint model, the statistics, the promotion logic, and the red/black switch — are real. The executor is the seam: a Kubernetes executor replaces `ServerGroup.start` with an `apply`, and nothing above it changes.

## Further reading

- [Automated Canary Analysis at Netflix with Kayenta](https://netflixtechblog.com/automated-canary-analysis-at-netflix-with-kayenta-3260bc7acc69)
- [Managed Delivery: Evolving Continuous Delivery at Netflix](https://blog.spinnaker.io/managed-delivery-evolving-continuous-delivery-at-netflix-eb74877fb33c)
- [Argo Rollouts: analysis and progressive delivery](https://argoproj.github.io/argo-rollouts/features/analysis/)
- [docs/lineage.md](docs/lineage.md) — how these ideas map onto today's tooling

## License

MIT
