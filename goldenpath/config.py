"""Parse and validate the delivery config.

Validation is strict and fails closed: unknown keys are errors, not warnings.
A silently ignored typo in `passThreshold` is a canary gate that quietly does
nothing, which is worse than having no gate at all because everyone believes
it is there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .canary.judge import Direction, MetricSpec

SUPPORTED_API_VERSIONS = {"goldenpath/v1"}
STRATEGY_TYPES = {"red-black", "highlander"}
CONSTRAINT_TYPES = {"depends-on", "allowed-times", "manual-judgment"}
VERIFICATION_TYPES = {"smoke", "canary"}

_DAYS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


class ConfigError(ValueError):
    """Raised when a delivery config is malformed or internally inconsistent."""


def _require_keys(mapping: dict, allowed: set[str], required: set[str], where: str) -> None:
    if not isinstance(mapping, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(mapping).__name__}")
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {sorted(unknown)}; allowed keys are {sorted(allowed)}"
        )
    missing = required - set(mapping)
    if missing:
        raise ConfigError(f"{where}: missing required key(s) {sorted(missing)}")


@dataclass(frozen=True)
class Artifact:
    name: str
    type: str
    entrypoint: str
    version_strategy: str


@dataclass(frozen=True)
class Strategy:
    type: str
    rollback_window_seconds: float = 0.0


@dataclass(frozen=True)
class Constraint:
    type: str
    environment: str | None = None
    days: tuple[str, ...] = ()
    hours_utc: tuple[int, int] | None = None
    enforced: bool = True
    only_when: str | None = None


@dataclass(frozen=True)
class SmokeCheck:
    endpoint: str
    expect_status: int = 200
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class CanaryCheck:
    windows: int
    requests_per_window: int
    concurrency: int
    pass_threshold: float
    marginal_threshold: float
    metrics: tuple[MetricSpec, ...]


@dataclass(frozen=True)
class Environment:
    name: str
    description: str
    constraints: tuple[Constraint, ...]
    strategy: Strategy
    smoke: SmokeCheck | None
    canary: CanaryCheck | None


@dataclass(frozen=True)
class DeliveryConfig:
    api_version: str
    application: str
    owner: str
    artifacts: tuple[Artifact, ...]
    environments: tuple[Environment, ...]
    source_path: Path | None = None

    def environment(self, name: str) -> Environment:
        for env in self.environments:
            if env.name == name:
                return env
        raise KeyError(f"no environment named {name!r}")

    @property
    def environment_names(self) -> list[str]:
        return [e.name for e in self.environments]


def _parse_artifact(raw: dict, index: int) -> Artifact:
    where = f"artifacts[{index}]"
    _require_keys(
        raw,
        allowed={"name", "type", "entrypoint", "versionStrategy"},
        required={"name", "type", "entrypoint", "versionStrategy"},
        where=where,
    )
    if raw["type"] != "process":
        raise ConfigError(
            f"{where}: unsupported artifact type {raw['type']!r} (expected 'process')"
        )
    return Artifact(
        name=str(raw["name"]),
        type=str(raw["type"]),
        entrypoint=str(raw["entrypoint"]),
        version_strategy=str(raw["versionStrategy"]),
    )


def _parse_hours(value: str, where: str) -> tuple[int, int]:
    parts = str(value).split("-")
    if len(parts) != 2:
        raise ConfigError(f"{where}: hoursUTC must look like '13-21', got {value!r}")
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ConfigError(f"{where}: hoursUTC must be integers, got {value!r}") from exc
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ConfigError(f"{where}: hoursUTC must fall within 0-23, got {value!r}")
    if start > end:
        raise ConfigError(f"{where}: hoursUTC start must not exceed end, got {value!r}")
    return start, end


def _parse_constraint(raw: dict, env_name: str, index: int) -> Constraint:
    where = f"environments[{env_name}].constraints[{index}]"
    if not isinstance(raw, dict) or "type" not in raw:
        raise ConfigError(f"{where}: each constraint needs a 'type'")
    ctype = str(raw["type"])
    if ctype not in CONSTRAINT_TYPES:
        raise ConfigError(
            f"{where}: unknown constraint type {ctype!r}; allowed {sorted(CONSTRAINT_TYPES)}"
        )

    if ctype == "depends-on":
        _require_keys(raw, {"type", "environment"}, {"type", "environment"}, where)
        return Constraint(type=ctype, environment=str(raw["environment"]))

    if ctype == "allowed-times":
        _require_keys(
            raw,
            {"type", "days", "hoursUTC", "enforced"},
            {"type", "days", "hoursUTC"},
            where,
        )
        days = tuple(str(d).lower() for d in raw["days"])
        bad_days = set(days) - _DAYS
        if bad_days:
            raise ConfigError(f"{where}: unknown day name(s) {sorted(bad_days)}")
        return Constraint(
            type=ctype,
            days=days,
            hours_utc=_parse_hours(raw["hoursUTC"], where),
            enforced=bool(raw.get("enforced", True)),
        )

    # manual-judgment
    _require_keys(raw, {"type", "onlyWhen"}, {"type"}, where)
    only_when = raw.get("onlyWhen")
    if only_when is not None and only_when != "canary-marginal":
        raise ConfigError(f"{where}: onlyWhen supports 'canary-marginal', got {only_when!r}")
    return Constraint(type=ctype, only_when=only_when)


def _parse_metric(raw: dict, env_name: str, index: int) -> MetricSpec:
    where = f"environments[{env_name}].canary.metrics[{index}]"
    _require_keys(
        raw,
        allowed={
            "name",
            "direction",
            "weight",
            "critical",
            "tolerance",
            "significance",
            "minSamples",
        },
        required={"name"},
        where=where,
    )
    direction_raw = str(raw.get("direction", "increase"))
    try:
        direction = Direction(direction_raw)
    except ValueError as exc:
        raise ConfigError(
            f"{where}: direction must be one of "
            f"{[d.value for d in Direction]}, got {direction_raw!r}"
        ) from exc

    tolerance = float(raw.get("tolerance", 0.2))
    if not 0.0 <= tolerance <= 1.0:
        raise ConfigError(f"{where}: tolerance is a Cliff's delta and must fall within [0, 1]")
    weight = float(raw.get("weight", 1.0))
    if weight <= 0:
        raise ConfigError(f"{where}: weight must be positive")

    return MetricSpec(
        name=str(raw["name"]),
        direction=direction,
        weight=weight,
        critical=bool(raw.get("critical", False)),
        tolerance=tolerance,
        significance=float(raw.get("significance", 0.05)),
        min_samples=int(raw.get("minSamples", 20)),
    )


def _parse_verification(
    raw_list: Any, env_name: str
) -> tuple[SmokeCheck | None, CanaryCheck | None]:
    smoke: SmokeCheck | None = None
    canary: CanaryCheck | None = None
    if raw_list is None:
        return None, None
    if not isinstance(raw_list, list):
        raise ConfigError(f"environments[{env_name}].verification: expected a list")

    for index, raw in enumerate(raw_list):
        where = f"environments[{env_name}].verification[{index}]"
        if not isinstance(raw, dict) or "type" not in raw:
            raise ConfigError(f"{where}: each verification needs a 'type'")
        vtype = str(raw["type"])
        if vtype not in VERIFICATION_TYPES:
            raise ConfigError(
                f"{where}: unknown verification type {vtype!r}; "
                f"allowed {sorted(VERIFICATION_TYPES)}"
            )

        if vtype == "smoke":
            _require_keys(
                raw,
                {"type", "endpoint", "expectStatus", "timeoutSeconds"},
                {"type", "endpoint"},
                where,
            )
            smoke = SmokeCheck(
                endpoint=str(raw["endpoint"]),
                expect_status=int(raw.get("expectStatus", 200)),
                timeout_seconds=float(raw.get("timeoutSeconds", 20.0)),
            )
            continue

        _require_keys(
            raw,
            allowed={
                "type",
                "windows",
                "requestsPerWindow",
                "concurrency",
                "passThreshold",
                "marginalThreshold",
                "metrics",
            },
            required={"type", "metrics"},
            where=where,
        )
        metrics = raw["metrics"]
        if not isinstance(metrics, list) or not metrics:
            raise ConfigError(f"{where}: canary verification needs at least one metric")

        pass_threshold = float(raw.get("passThreshold", 95.0))
        marginal_threshold = float(raw.get("marginalThreshold", 75.0))
        if marginal_threshold > pass_threshold:
            raise ConfigError(
                f"{where}: marginalThreshold ({marginal_threshold}) must not exceed "
                f"passThreshold ({pass_threshold})"
            )

        canary = CanaryCheck(
            windows=int(raw.get("windows", 30)),
            requests_per_window=int(raw.get("requestsPerWindow", 20)),
            concurrency=int(raw.get("concurrency", 8)),
            pass_threshold=pass_threshold,
            marginal_threshold=marginal_threshold,
            metrics=tuple(_parse_metric(m, env_name, i) for i, m in enumerate(metrics)),
        )

    return smoke, canary


def _parse_environment(raw: dict, index: int) -> Environment:
    where = f"environments[{index}]"
    _require_keys(
        raw,
        allowed={"name", "description", "constraints", "strategy", "verification"},
        required={"name", "strategy"},
        where=where,
    )
    name = str(raw["name"])

    strategy_raw = raw["strategy"]
    _require_keys(
        strategy_raw,
        {"type", "rollbackWindowSeconds"},
        {"type"},
        f"environments[{name}].strategy",
    )
    if strategy_raw["type"] not in STRATEGY_TYPES:
        raise ConfigError(
            f"environments[{name}].strategy: unknown type {strategy_raw['type']!r}; "
            f"allowed {sorted(STRATEGY_TYPES)}"
        )
    strategy = Strategy(
        type=str(strategy_raw["type"]),
        rollback_window_seconds=float(strategy_raw.get("rollbackWindowSeconds", 0.0)),
    )

    constraints_raw = raw.get("constraints") or []
    if not isinstance(constraints_raw, list):
        raise ConfigError(f"environments[{name}].constraints: expected a list")
    constraints = tuple(_parse_constraint(c, name, i) for i, c in enumerate(constraints_raw))

    smoke, canary = _parse_verification(raw.get("verification"), name)

    return Environment(
        name=name,
        description=str(raw.get("description", "")),
        constraints=constraints,
        strategy=strategy,
        smoke=smoke,
        canary=canary,
    )


def load(path: str | Path) -> DeliveryConfig:
    """Load and validate a delivery config from disk.

    Raises:
        ConfigError: if the document is malformed, or references environments
            that do not exist, or declares them out of order.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"delivery config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    _require_keys(
        raw,
        allowed={"apiVersion", "application", "owner", "artifacts", "environments"},
        required={"apiVersion", "application", "artifacts", "environments"},
        where=str(path),
    )

    api_version = str(raw["apiVersion"])
    if api_version not in SUPPORTED_API_VERSIONS:
        raise ConfigError(
            f"{path}: unsupported apiVersion {api_version!r}; "
            f"this orchestrator understands {sorted(SUPPORTED_API_VERSIONS)}"
        )

    artifacts_raw = raw["artifacts"]
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise ConfigError(f"{path}: at least one artifact is required")
    artifacts = tuple(_parse_artifact(a, i) for i, a in enumerate(artifacts_raw))

    environments_raw = raw["environments"]
    if not isinstance(environments_raw, list) or not environments_raw:
        raise ConfigError(f"{path}: at least one environment is required")
    environments = tuple(_parse_environment(e, i) for i, e in enumerate(environments_raw))

    names = [e.name for e in environments]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"{path}: duplicate environment name(s) {sorted(duplicates)}")

    # A depends-on may only point backwards. Forward or circular references
    # would describe a promotion order the orchestrator can never satisfy.
    for position, env in enumerate(environments):
        for constraint in env.constraints:
            if constraint.type != "depends-on":
                continue
            target = constraint.environment
            if target not in names:
                raise ConfigError(
                    f"environments[{env.name}]: depends-on references unknown "
                    f"environment {target!r}"
                )
            if names.index(target) >= position:
                raise ConfigError(
                    f"environments[{env.name}]: depends-on {target!r} must refer to an "
                    f"environment declared earlier; this ordering can never be satisfied"
                )

    return DeliveryConfig(
        api_version=api_version,
        application=str(raw["application"]),
        owner=str(raw.get("owner", "unknown")),
        artifacts=artifacts,
        environments=environments,
        source_path=path,
    )
