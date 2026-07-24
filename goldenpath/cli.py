"""Command line entry point.

    goldenpath validate                 # check the delivery config
    goldenpath deploy --candidate-version abc123

Exit codes are distinct so CI can react to *why* a run stopped rather than
just that it did:

    0  promoted through every environment
    1  a canary rejected the build, or a deploy failed
    2  a constraint blocked promotion
    3  a canary was marginal and a human must decide
    4  the delivery config is invalid
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import ConfigError, load
from .exporters import argo_rollouts
from .orchestrator import BuildProfile, EnvironmentStatus, Orchestrator, PipelineResult
from .report import to_markdown

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2
EXIT_AWAITING_JUDGMENT = 3
EXIT_CONFIG_ERROR = 4

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "delivery" / "delivery.yml"


def _parse_env_pairs(pairs: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _git_sha() -> str:
    try:
        sha = subprocess.check_output(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git from PATH is intended
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _exit_code(result: PipelineResult) -> int:
    for env in result.environments:
        if env.status is EnvironmentStatus.BLOCKED:
            return EXIT_BLOCKED
        if env.status is EnvironmentStatus.AWAITING_JUDGMENT:
            return EXIT_AWAITING_JUDGMENT
        if env.status is EnvironmentStatus.FAILED:
            return EXIT_FAILED
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load(args.config)
    print(f"delivery config OK: {config.source_path}")
    print(f"  application:  {config.application} (owner: {config.owner})")
    print(f"  artifacts:    {', '.join(a.name for a in config.artifacts)}")
    print(f"  environments: {' -> '.join(config.environment_names)}")
    for env in config.environments:
        gates = [c.type for c in env.constraints]
        checks = []
        if env.smoke:
            checks.append("smoke")
        if env.canary:
            checks.append(f"canary({len(env.canary.metrics)} metrics)")
        print(
            f"    - {env.name}: strategy={env.strategy.type} "
            f"constraints=[{', '.join(gates) or 'none'}] "
            f"verification=[{', '.join(checks) or 'none'}]"
        )
    return EXIT_OK


def _cmd_deploy(args: argparse.Namespace) -> int:
    config = load(args.config)

    candidate = BuildProfile(
        version=args.candidate_version or _git_sha(),
        env=_parse_env_pairs(args.candidate_env),
    )
    baseline = BuildProfile(
        version=args.baseline_version,
        env=_parse_env_pairs(args.baseline_env),
    )

    orchestrator = Orchestrator(
        config=config,
        repo_root=REPO_ROOT,
        log_dir=Path(args.log_dir) if args.log_dir else None,
        auto_approve_marginal=args.auto_approve_marginal,
    )

    result = orchestrator.run(
        candidate=candidate,
        baseline=baseline,
        only_environment=args.only_environment,
    )

    markdown = to_markdown(result)
    print(markdown)

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown_out).write_text(markdown, encoding="utf-8")

    # GitHub renders this on the run page, so the verdict is visible without
    # opening a log.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(markdown)

    return _exit_code(result)


def _cmd_export_argo(args: argparse.Namespace) -> int:
    config = load(args.config)
    rendered = argo_rollouts.export(config)

    if args.check:
        # Drift guard: the delivery config and the generated manifests must
        # never quietly disagree about what the gates are.
        target = Path(args.out)
        if not target.is_file():
            print(f"{target} does not exist; run `goldenpath export-argo`", file=sys.stderr)
            return EXIT_FAILED
        if target.read_text(encoding="utf-8") != rendered:
            print(
                f"{target} is out of date with {config.source_path}.\n"
                f"Run `goldenpath export-argo` and commit the result.",
                file=sys.stderr,
            )
            return EXIT_FAILED
        print(f"{target} is in sync with {config.source_path}")
        return EXIT_OK

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    print(f"wrote {out}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="goldenpath",
        description="Declarative golden-path deployment with automated canary analysis.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="path to the delivery config (default: delivery/delivery.yml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate the delivery config")
    validate.set_defaults(func=_cmd_validate)

    deploy = subparsers.add_parser("deploy", help="promote a build along the golden path")
    deploy.add_argument(
        "--candidate-version",
        default=None,
        help="version of the build under test (default: short git SHA)",
    )
    deploy.add_argument(
        "--baseline-version",
        default="current-production",
        help="version of the currently-live build used as the canary control",
    )
    deploy.add_argument(
        "--candidate-env",
        action="append",
        metavar="KEY=VALUE",
        help="runtime setting for the candidate build; repeatable",
    )
    deploy.add_argument(
        "--baseline-env",
        action="append",
        metavar="KEY=VALUE",
        help="runtime setting for the baseline build; repeatable",
    )
    deploy.add_argument(
        "--only-environment",
        default=None,
        help="run a single environment (its depends-on constraint may block it)",
    )
    deploy.add_argument(
        "--auto-approve-marginal",
        action="store_true",
        help="promote a MARGINAL canary without a human; off by default",
    )
    deploy.add_argument("--json-out", default=None, help="write the run result as JSON")
    deploy.add_argument("--markdown-out", default=None, help="write the run report as Markdown")
    deploy.add_argument("--log-dir", default=None, help="directory for server group logs")
    deploy.set_defaults(func=_cmd_deploy)

    export = subparsers.add_parser(
        "export-argo",
        help="compile the delivery config into Argo Rollouts manifests",
    )
    export.add_argument(
        "--out",
        default=str(REPO_ROOT / "deploy" / "argo-rollouts" / "generated.yaml"),
        help="output path for the generated manifests",
    )
    export.add_argument(
        "--check",
        action="store_true",
        help="verify the committed manifests match the delivery config; do not write",
    )
    export.set_defaults(func=_cmd_export_argo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"delivery config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
