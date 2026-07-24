"""The golden path orchestrator.

Reads the delivery config and works out the steps. Nobody writes a pipeline;
they declare requirements and this executes them, which is the difference
between a paved road and a pile of YAML each team copies and mutates.

Per environment, in order:

1. Evaluate pre-deploy constraints. Blocked means stop -- there is no override
   flag, because a gate with a bypass button is decoration.
2. Deploy a fresh **baseline** server group running the currently-live version,
   alongside a **canary** server group running the candidate. Both are new, so
   neither benefits from a warm page cache or a JIT that has been running for a
   week.
3. Smoke test the canary.
4. Run automated canary analysis over both under identical, simultaneous load.
5. Evaluate post-verification constraints (a human is consulted only if the
   statistics are ambiguous).
6. On PASS, switch the router -- red/black. On FAIL, destroy the canary, leave
   the router untouched, and halt the pipeline.

Step 6 is why rollback is trustworthy: the previous version is still running
and still healthy at the moment the decision is made. "Rollback" is a pointer
swap, not a redeploy under pressure.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .canary import metrics as metric_collector
from .canary.judge import CanaryResult, Verdict, judge
from .config import DeliveryConfig, Environment
from .constraints import (
    ConstraintEvaluation,
    Phase,
    PromotionState,
    blocking,
    evaluate,
)
from .executors.process import DeploymentError, ServerGroup
from .router import SwitchEvent, TrafficRouter


class EnvironmentStatus(enum.StrEnum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    AWAITING_JUDGMENT = "AWAITING_JUDGMENT"
    NOT_REACHED = "NOT_REACHED"

    @property
    def is_terminal_failure(self) -> bool:
        return self in (
            EnvironmentStatus.BLOCKED,
            EnvironmentStatus.FAILED,
            EnvironmentStatus.AWAITING_JUDGMENT,
        )


@dataclass
class BuildProfile:
    """A build's identity and its runtime behaviour.

    `env` stands in for what would otherwise be baked into the artifact. A
    build that is 5x slower because someone added a synchronous call in a hot
    path is, from the pipeline's point of view, a build whose latency is
    higher -- which is exactly what `LATENCY_MS` expresses. It lets the
    repository deploy a genuinely bad build on demand and prove the gate holds.
    """

    version: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class EnvironmentResult:
    name: str
    status: EnvironmentStatus
    strategy: str
    pre_deploy: list[ConstraintEvaluation] = field(default_factory=list)
    post_verification: list[ConstraintEvaluation] = field(default_factory=list)
    smoke_passed: bool | None = None
    canary: CanaryResult | None = None
    switches: list[SwitchEvent] = field(default_factory=list)
    rolled_back: bool = False
    notes: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "environment": self.name,
            "status": self.status.value,
            "strategy": self.strategy,
            "duration_seconds": round(self.duration_seconds, 2),
            "pre_deploy_constraints": [c.to_dict() for c in self.pre_deploy],
            "post_verification_constraints": [c.to_dict() for c in self.post_verification],
            "smoke_passed": self.smoke_passed,
            "canary": self.canary.to_dict() if self.canary else None,
            "traffic_switches": [
                {"from": s.from_target, "to": s.to_target, "reason": s.reason}
                for s in self.switches
            ],
            "rolled_back": self.rolled_back,
            "notes": self.notes,
        }


@dataclass
class PipelineResult:
    application: str
    candidate_version: str
    baseline_version: str
    started_at: str
    environments: list[EnvironmentResult] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return all(e.status is EnvironmentStatus.SUCCEEDED for e in self.environments)

    @property
    def final_status(self) -> str:
        for env in self.environments:
            if env.status.is_terminal_failure:
                return env.status.value
        if any(e.status is EnvironmentStatus.NOT_REACHED for e in self.environments):
            return "INCOMPLETE"
        return "SUCCEEDED"

    def to_dict(self) -> dict:
        return {
            "application": self.application,
            "candidate_version": self.candidate_version,
            "baseline_version": self.baseline_version,
            "started_at": self.started_at,
            "status": self.final_status,
            "environments": [e.to_dict() for e in self.environments],
        }


class Orchestrator:
    """Executes a delivery config against the process executor."""

    def __init__(
        self,
        config: DeliveryConfig,
        repo_root: Path,
        log_dir: Path | None = None,
        auto_approve_marginal: bool = False,
        now: datetime | None = None,
    ) -> None:
        self.config = config
        self.repo_root = repo_root
        self.log_dir = log_dir
        self.auto_approve_marginal = auto_approve_marginal
        self.now = now
        self._group_counter = 0

    # ------------------------------------------------------------- helpers --
    def _entrypoint(self) -> Path:
        artifact = self.config.artifacts[0]
        path = (self.repo_root / artifact.entrypoint).resolve()
        if not path.is_file():
            raise DeploymentError(f"artifact entrypoint does not exist: {path}")
        return path

    def _next_group_name(self, env_name: str, role: str) -> str:
        self._group_counter += 1
        return f"{self.config.application}-{env_name}-{role}-v{self._group_counter:03d}"

    def _deploy(self, env_name: str, role: str, build: BuildProfile) -> ServerGroup:
        group = ServerGroup(
            name=self._next_group_name(env_name, role),
            version=build.version,
            role=role,
            entrypoint=self._entrypoint(),
            env=dict(build.env),
        )
        group.start(log_dir=self.log_dir)
        return group

    # ---------------------------------------------------------------- main --
    def run(
        self,
        candidate: BuildProfile,
        baseline: BuildProfile,
        only_environment: str | None = None,
    ) -> PipelineResult:
        """Promote `candidate` along the golden path.

        Args:
            candidate: The build under test.
            baseline: The currently-live build, redeployed fresh in each
                environment to serve as the canary control.
            only_environment: Run just this environment, for debugging. The
                depends-on constraint will normally block it, which is correct.

        Returns:
            A PipelineResult recording every decision and its reason.
        """
        result = PipelineResult(
            application=self.config.application,
            candidate_version=candidate.version,
            baseline_version=baseline.version,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        succeeded: list[str] = []
        halted = False

        for environment in self.config.environments:
            if only_environment and environment.name != only_environment:
                continue
            if halted:
                result.environments.append(
                    EnvironmentResult(
                        name=environment.name,
                        status=EnvironmentStatus.NOT_REACHED,
                        strategy=environment.strategy.type,
                        notes=["an earlier environment halted the pipeline"],
                    )
                )
                continue

            env_result = self._run_environment(environment, candidate, baseline, tuple(succeeded))
            result.environments.append(env_result)

            if env_result.status is EnvironmentStatus.SUCCEEDED:
                succeeded.append(environment.name)
            else:
                halted = True

        return result

    def _run_environment(
        self,
        environment: Environment,
        candidate: BuildProfile,
        baseline: BuildProfile,
        succeeded: tuple[str, ...],
    ) -> EnvironmentResult:
        started = time.monotonic()
        env_result = EnvironmentResult(
            name=environment.name,
            status=EnvironmentStatus.FAILED,
            strategy=environment.strategy.type,
        )

        state = PromotionState(
            succeeded_environments=succeeded,
            now=self.now,
            auto_approve_marginal=self.auto_approve_marginal,
        )

        # 1. Pre-deploy constraints ------------------------------------------
        env_result.pre_deploy = evaluate(environment.constraints, state, Phase.PRE_DEPLOY)
        blockers = blocking(env_result.pre_deploy)
        if blockers:
            env_result.status = EnvironmentStatus.BLOCKED
            env_result.notes.append(
                "blocked before deploying: " + "; ".join(b.reason for b in blockers)
            )
            env_result.duration_seconds = time.monotonic() - started
            return env_result

        router = TrafficRouter()
        baseline_group: ServerGroup | None = None
        canary_group: ServerGroup | None = None

        try:
            router.start()

            # 2. Deploy ------------------------------------------------------
            if environment.canary is not None:
                baseline_group = self._deploy(environment.name, "baseline", baseline)
                baseline_group.wait_healthy(timeout=20.0)
                router.switch_to(
                    baseline_group.base_url,
                    baseline_group.name,
                    reason=f"baseline {baseline.version} in service",
                )
                env_result.switches.append(router.history[-1])
                env_result.notes.append(
                    f"baseline server group {baseline_group.name} "
                    f"({baseline.version}) serving traffic"
                )

            canary_group = self._deploy(environment.name, "canary", candidate)
            timeout = environment.smoke.timeout_seconds if environment.smoke else 20.0
            canary_group.wait_healthy(timeout=timeout)
            env_result.notes.append(
                f"canary server group {canary_group.name} ({candidate.version}) deployed"
            )

            # 3. Smoke -------------------------------------------------------
            if environment.smoke is not None:
                env_result.smoke_passed = self._smoke(canary_group, environment)
                if not env_result.smoke_passed:
                    env_result.status = EnvironmentStatus.FAILED
                    env_result.notes.append(
                        f"smoke check on {environment.smoke.endpoint} failed; "
                        f"canary destroyed, traffic untouched"
                    )
                    env_result.rolled_back = baseline_group is not None
                    return env_result

            # 4. Canary analysis ---------------------------------------------
            if environment.canary is not None and baseline_group is not None:
                env_result.canary = self._analyse(environment, baseline_group, canary_group)

                # 5. Post-verification constraints ---------------------------
                state.canary_verdict = env_result.canary.verdict
                env_result.post_verification = evaluate(
                    environment.constraints, state, Phase.POST_VERIFICATION
                )
                post_blockers = blocking(env_result.post_verification)

                if env_result.canary.verdict is Verdict.FAIL:
                    env_result.status = EnvironmentStatus.FAILED
                    env_result.rolled_back = True
                    env_result.notes.append(
                        f"canary REJECTED (score {env_result.canary.score:.1f}): "
                        f"{env_result.canary.summary} Traffic never left "
                        f"{baseline_group.name}."
                    )
                    return env_result

                if post_blockers:
                    env_result.status = EnvironmentStatus.AWAITING_JUDGMENT
                    env_result.notes.append(
                        "canary was not conclusive; awaiting human judgment: "
                        + "; ".join(b.reason for b in post_blockers)
                    )
                    return env_result

            # 6. Red/black switch --------------------------------------------
            switch_reason = (
                f"canary passed with score {env_result.canary.score:.1f}"
                if env_result.canary
                else "no canary configured for this environment"
            )
            router.switch_to(canary_group.base_url, canary_group.name, reason=switch_reason)
            env_result.switches.append(router.history[-1])
            env_result.notes.append(
                f"{environment.strategy.type}: traffic switched to {canary_group.name}"
            )

            if baseline_group is not None:
                window = environment.strategy.rollback_window_seconds
                if environment.strategy.type == "red-black" and window > 0:
                    # The old group stays warm and healthy: rollback during this
                    # window is a pointer swap, not a redeploy.
                    env_result.notes.append(
                        f"previous group {baseline_group.name} held for {window:.0f}s "
                        f"as an instant rollback target"
                    )
                    time.sleep(window)
                env_result.notes.append(f"previous group {baseline_group.name} destroyed")

            env_result.status = EnvironmentStatus.SUCCEEDED
            return env_result

        except DeploymentError as exc:
            env_result.status = EnvironmentStatus.FAILED
            env_result.notes.append(f"deployment error: {exc}")
            env_result.rolled_back = baseline_group is not None
            return env_result
        finally:
            for group in (canary_group, baseline_group):
                if group is not None:
                    group.stop()
            router.stop()
            env_result.duration_seconds = time.monotonic() - started

    # ------------------------------------------------------------ verifiers --
    @staticmethod
    def _smoke(group: ServerGroup, environment: Environment) -> bool:
        import urllib.error
        import urllib.request

        check = environment.smoke
        assert check is not None
        try:
            with urllib.request.urlopen(
                f"{group.base_url}{check.endpoint}", timeout=check.timeout_seconds
            ) as response:
                return response.status == check.expect_status
        except urllib.error.HTTPError as exc:
            return exc.code == check.expect_status
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    @staticmethod
    def _analyse(
        environment: Environment,
        baseline_group: ServerGroup,
        canary_group: ServerGroup,
    ) -> CanaryResult:
        check = environment.canary
        assert check is not None
        baseline_metrics, canary_metrics = metric_collector.collect(
            baseline_url=baseline_group.base_url,
            canary_url=canary_group.base_url,
            windows=check.windows,
            requests_per_window=check.requests_per_window,
            concurrency=check.concurrency,
        )
        return judge(
            specs=list(check.metrics),
            canary_samples=canary_metrics.series,
            baseline_samples=baseline_metrics.series,
            pass_threshold=check.pass_threshold,
            marginal_threshold=check.marginal_threshold,
        )
