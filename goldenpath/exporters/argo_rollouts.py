"""Compile a delivery config into Argo Rollouts manifests.

The delivery config is the source of truth; the orchestrator in this repository
is one runtime for it. This exporter is another, and its existence is the
argument: the concepts are portable, so migrating from a Spinnaker-shaped world
to a Kubernetes-native one is a change of executor, not a rewrite of intent.

Mapping
-------
    delivery.yml                      Argo Rollouts
    ----------------------------      -----------------------------------
    strategy: red-black               strategy.blueGreen + prePromotionAnalysis
    strategy: highlander              strategy.blueGreen, auto-promoted
    verification: canary              AnalysisTemplate
    metric.direction: increase        successCondition: result <= baseline*k
    metric.critical: true             failureLimit: 0
    constraint: manual-judgment       autoPromotionEnabled: false
    constraint: allowed-times         (no equivalent -- see the caveat below)

Where the models genuinely differ
---------------------------------
Kayenta computes a *weighted aggregate score* across metrics and compares it
to pass/marginal thresholds, so a minor regression in a low-weight metric can
be outvoted by healthy ones. Argo Rollouts has no weighted aggregate: each
metric independently fails the analysis once it exceeds its `failureLimit`.

That is stricter, and it means `weight` cannot be expressed natively. This
exporter emits weights as annotations so the intent survives review, and
degrades the semantics honestly: any metric a weighted judge would have
tolerated is one an Argo analysis will fail on. The alternative -- silently
dropping the weights and implying equivalence -- is the kind of gap that
surfaces during an incident.

Teams that need true weighted scoring point Argo Rollouts at Kayenta itself
via its `kayenta` provider, which is the seam this mapping is designed around.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..canary.judge import Direction, MetricSpec
from ..config import DeliveryConfig, Environment

API_VERSION = "argoproj.io/v1alpha1"

# Prometheus expressions for the demo workload's metrics. Real services swap
# these for their own; the structure is what is being demonstrated.
_QUERIES = {
    "error_rate_pct": (
        '100 * sum(rate(http_requests_total{{job="{service}",status=~"5.."}}[2m]))\n'
        '  / sum(rate(http_requests_total{{job="{service}"}}[2m]))'
    ),
    "request_latency_ms": (
        "1000 * histogram_quantile(0.95,\n"
        '  sum(rate(http_request_duration_seconds_bucket{{job="{service}"}}[2m])) by (le))'
    ),
    "throughput_rps": 'sum(rate(http_requests_total{{job="{service}"}}[2m]))',
    "cpu_saturation_pct": (
        '100 * avg(rate(container_cpu_usage_seconds_total{{pod=~"{service}-.*"}}[2m]))'
    ),
}


def _query_for(metric: MetricSpec, service_arg: str) -> str:
    template = _QUERIES.get(metric.name)
    if template is None:
        return f"# TODO: no query mapped for {metric.name}\nvector(0)"
    return template.format(service=f"{{{{args.{service_arg}}}}}")


def _success_condition(metric: MetricSpec) -> str:
    """Express a directional tolerance as an Argo success condition.

    Cliff's delta has no Prometheus equivalent, so the tolerance is converted
    into a ratio guard against the baseline. This is a coarser test than the
    rank-based one -- it compares point values rather than distributions -- and
    the generated manifest says so in a comment.
    """
    # A delta tolerance of 0.25 is treated as "allow a 25% move".
    slack = 1.0 + metric.tolerance
    if metric.direction is Direction.INCREASE:
        return f"result[0] <= result[1] * {slack:.2f}"
    if metric.direction is Direction.DECREASE:
        return f"result[0] >= result[1] / {slack:.2f}"
    return f"result[0] >= result[1] / {slack:.2f} && result[0] <= result[1] * {slack:.2f}"


def analysis_template(config: DeliveryConfig, environment: Environment) -> dict[str, Any]:
    """Build the AnalysisTemplate for one environment's canary verification."""
    check = environment.canary
    if check is None:
        raise ValueError(f"environment {environment.name!r} has no canary verification")

    metrics: list[dict[str, Any]] = []
    for spec in check.metrics:
        metrics.append(
            {
                "name": spec.name.replace("_", "-"),
                # One measurement per window, matching the delivery config.
                "interval": "30s",
                "count": check.windows,
                # Critical metrics get no tolerance for repeated breaches.
                "failureLimit": 0 if spec.critical else 1,
                "successCondition": _success_condition(spec),
                "provider": {
                    "prometheus": {
                        "address": "http://prometheus.monitoring.svc.cluster.local:9090",
                        "query": _query_for(spec, "canary-service"),
                    }
                },
            }
        )

    return {
        "apiVersion": API_VERSION,
        "kind": "AnalysisTemplate",
        "metadata": {
            "name": f"{config.application}-{environment.name}-canary",
            "annotations": {
                "goldenpath.io/generated-from": str(
                    config.source_path.name if config.source_path else "delivery.yml"
                ),
                "goldenpath.io/pass-threshold": str(check.pass_threshold),
                "goldenpath.io/marginal-threshold": str(check.marginal_threshold),
                # Preserved because Argo cannot enforce them; see module docstring.
                "goldenpath.io/metric-weights": ", ".join(
                    f"{m.name}={m.weight:g}" for m in check.metrics
                ),
            },
        },
        "spec": {
            "args": [{"name": "canary-service"}, {"name": "baseline-service"}],
            "metrics": metrics,
        },
    }


def rollout(config: DeliveryConfig, environment: Environment) -> dict[str, Any]:
    """Build the Rollout for one environment."""
    app = config.application
    requires_judgment = any(c.type == "manual-judgment" for c in environment.constraints)

    blue_green: dict[str, Any] = {
        "activeService": f"{app}-{environment.name}-active",
        "previewService": f"{app}-{environment.name}-preview",
        # A red/black switch is only trustworthy if the old group is still
        # there. This is the Argo equivalent of the orchestrator's rollback
        # window.
        "scaleDownDelaySeconds": max(30, int(environment.strategy.rollback_window_seconds)),
        "autoPromotionEnabled": not requires_judgment,
    }

    if environment.canary is not None:
        blue_green["prePromotionAnalysis"] = {
            "templates": [{"templateName": f"{app}-{environment.name}-canary"}],
            "args": [
                {
                    "name": "canary-service",
                    "value": f"{app}-{environment.name}-preview",
                },
                {
                    "name": "baseline-service",
                    "value": f"{app}-{environment.name}-active",
                },
            ],
        }

    return {
        "apiVersion": API_VERSION,
        "kind": "Rollout",
        "metadata": {
            "name": f"{app}-{environment.name}",
            "annotations": {
                "goldenpath.io/environment": environment.name,
                "goldenpath.io/strategy": environment.strategy.type,
                "goldenpath.io/owner": config.owner,
            },
        },
        "spec": {
            "replicas": 4,
            "revisionHistoryLimit": 3,
            "selector": {"matchLabels": {"app": app, "environment": environment.name}},
            "template": {
                "metadata": {"labels": {"app": app, "environment": environment.name}},
                "spec": {
                    "containers": [
                        {
                            "name": app,
                            "image": f"ghcr.io/OWNER/{app}:REPLACED_BY_CI",
                            "ports": [{"containerPort": 8080}],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "initialDelaySeconds": 2,
                                "periodSeconds": 5,
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                        }
                    ]
                },
            },
            "strategy": {"blueGreen": blue_green},
        },
    }


def export(config: DeliveryConfig) -> str:
    """Render every environment as a multi-document YAML manifest."""
    documents: list[dict[str, Any]] = []
    for environment in config.environments:
        if environment.canary is not None:
            documents.append(analysis_template(config, environment))
        documents.append(rollout(config, environment))

    header = (
        "# GENERATED FILE -- DO NOT EDIT.\n"
        "#\n"
        f"# Compiled from {config.source_path.name if config.source_path else 'delivery.yml'}\n"
        "# by `goldenpath export-argo`. Edit the delivery config and regenerate.\n"
        "#\n"
        "# CI fails if this file drifts from the delivery config, so the two can\n"
        "# never quietly disagree about what the deployment gates are.\n"
        "#\n"
        "# Caveat: Argo Rollouts has no weighted aggregate score. Metric weights\n"
        "# are preserved as annotations only, and the generated analysis is\n"
        "# stricter than the weighted judge. See goldenpath/exporters/argo_rollouts.py.\n"
    )
    body = yaml.safe_dump_all(documents, sort_keys=False, default_flow_style=False, width=100)
    return header + "\n" + body
