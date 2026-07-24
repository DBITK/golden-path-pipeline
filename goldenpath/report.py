"""Render a pipeline run as Markdown.

The report is the deliverable of a deployment gate. If a canary rejects a
build, the engineer reading the failure needs to know which metric moved, by
how much, and how confident the judge was -- not just that a step went red.
Everything here exists so a rejection is self-explanatory.
"""

from __future__ import annotations

from .canary.judge import Classification
from .orchestrator import EnvironmentStatus, PipelineResult

_STATUS_ICON = {
    EnvironmentStatus.SUCCEEDED: "PASS",
    EnvironmentStatus.FAILED: "FAIL",
    EnvironmentStatus.BLOCKED: "BLOCKED",
    EnvironmentStatus.AWAITING_JUDGMENT: "JUDGMENT",
    EnvironmentStatus.NOT_REACHED: "SKIPPED",
}

_METRIC_ICON = {
    Classification.PASS: "pass",
    Classification.HIGH: "HIGH",
    Classification.LOW: "LOW",
    Classification.NODATA: "no data",
    Classification.ERROR: "ERROR",
}


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def to_markdown(result: PipelineResult) -> str:
    """Render the full run, suitable for a GitHub step summary or PR comment."""
    lines: list[str] = []
    lines.append(f"# Golden path: `{result.application}`")
    lines.append("")
    lines.append(f"**Result:** `{result.final_status}`")
    lines.append("")
    lines.append("| | |\n|---|---|")
    lines.append(f"| Candidate | `{result.candidate_version}` |")
    lines.append(f"| Baseline | `{result.baseline_version}` |")
    lines.append(f"| Started | {result.started_at} |")
    lines.append("")

    lines.append("## Environments")
    lines.append("")
    lines.append("| Environment | Status | Strategy | Canary score | Duration |")
    lines.append("|---|---|---|---|---|")
    for env in result.environments:
        score = f"{env.canary.score:.1f}" if env.canary else "-"
        lines.append(
            f"| `{env.name}` | {_STATUS_ICON[env.status]} | {env.strategy} | "
            f"{score} | {env.duration_seconds:.1f}s |"
        )
    lines.append("")

    for env in result.environments:
        lines.extend(_environment_section(env))

    return "\n".join(lines).rstrip() + "\n"


def _environment_section(env) -> list[str]:  # noqa: ANN001 - EnvironmentResult
    lines = [f"## `{env.name}` - {_STATUS_ICON[env.status]}", ""]

    all_constraints = list(env.pre_deploy) + list(env.post_verification)
    if all_constraints:
        lines.append("### Constraints")
        lines.append("")
        lines.append("| Constraint | Status | Reason |")
        lines.append("|---|---|---|")
        for constraint in all_constraints:
            lines.append(
                f"| `{constraint.type}` | {constraint.status.value} | {constraint.reason} |"
            )
        lines.append("")

    if env.smoke_passed is not None:
        lines.append(f"**Smoke check:** {'passed' if env.smoke_passed else 'FAILED'}")
        lines.append("")

    if env.canary is not None:
        lines.append("### Automated canary analysis")
        lines.append("")
        lines.append(
            f"**Verdict `{env.canary.verdict.value}`** - score "
            f"**{env.canary.score:.1f}** "
            f"(pass >= {env.canary.pass_threshold}, "
            f"marginal >= {env.canary.marginal_threshold})"
        )
        lines.append("")
        lines.append(f"> {env.canary.summary}")
        lines.append("")
        lines.append(
            "| Metric | Result | Canary median | Baseline median | "
            "Canary p95 | Baseline p95 | p-value | Cliff's delta | Weight |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for metric in env.canary.results:
            critical = " (critical)" if metric.critical else ""
            lines.append(
                f"| `{metric.name}`{critical} | {_METRIC_ICON[metric.classification]} | "
                f"{_fmt(metric.canary_median)} | {_fmt(metric.baseline_median)} | "
                f"{_fmt(metric.canary_p95)} | {_fmt(metric.baseline_p95)} | "
                f"{_fmt(metric.p_value, 4)} | {_fmt(metric.cliffs_delta, 3)} | "
                f"{metric.weight:g} |"
            )
        lines.append("")

        failures = env.canary.failures
        if failures:
            lines.append("**Why it was rejected:**")
            lines.append("")
            for metric in failures:
                lines.append(f"- `{metric.name}`: {metric.reason}")
            lines.append("")

    if env.switches:
        lines.append("### Traffic")
        lines.append("")
        for switch in env.switches:
            source = switch.from_target or "(nothing)"
            lines.append(f"- `{source}` -> `{switch.to_target}` - {switch.reason}")
        lines.append("")

    if env.rolled_back:
        lines.append(
            "**Rolled back.** The previous server group kept serving traffic "
            "throughout; the candidate never received production requests."
        )
        lines.append("")

    if env.notes:
        lines.append("<details><summary>Timeline</summary>")
        lines.append("")
        for note in env.notes:
            lines.append(f"- {note}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return lines
