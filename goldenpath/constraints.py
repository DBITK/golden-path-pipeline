"""Environment constraint evaluation.

In Managed Delivery, constraints -- not hand-wired pipeline stages -- are what
govern whether an artifact version may enter an environment. They fall into two
phases:

* **pre-deploy** constraints are checked before anything is deployed, because
  the answer cannot change by deploying (`depends-on`, `allowed-times`).
* **post-verification** constraints are checked once evidence exists, because
  the answer depends on it (`manual-judgment`, which is only invoked when the
  canary lands in the ambiguous band).

Every constraint returns a reason string whether it passes or fails. "Blocked"
with no explanation is the fastest way to get a deployment gate disabled by an
irritated engineer at 2am.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime

from .canary.judge import Verdict
from .config import Constraint

_WEEKDAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

PRE_DEPLOY_TYPES = {"depends-on", "allowed-times"}
POST_VERIFICATION_TYPES = {"manual-judgment"}


class Phase(enum.StrEnum):
    PRE_DEPLOY = "pre-deploy"
    POST_VERIFICATION = "post-verification"


class ConstraintStatus(enum.StrEnum):
    SATISFIED = "SATISFIED"
    BLOCKED = "BLOCKED"
    AWAITING_JUDGMENT = "AWAITING_JUDGMENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"

    @property
    def allows_promotion(self) -> bool:
        return self in (ConstraintStatus.SATISFIED, ConstraintStatus.NOT_APPLICABLE)


@dataclass(frozen=True)
class ConstraintEvaluation:
    type: str
    status: ConstraintStatus
    reason: str

    def to_dict(self) -> dict:
        return {"type": self.type, "status": self.status.value, "reason": self.reason}


@dataclass
class PromotionState:
    """What the orchestrator knows when it evaluates constraints.

    Attributes:
        succeeded_environments: Environments this artifact version has already
            been promoted through successfully, in order.
        now: Evaluation time, injected rather than read from the clock so the
            time-window constraint is testable.
        canary_verdict: Populated only during the post-verification phase.
        auto_approve_marginal: Whether an unattended run may approve a
            MARGINAL canary. Defaults to false -- unattended means the safe
            answer, not the convenient one.
    """

    succeeded_environments: tuple[str, ...] = ()
    now: datetime | None = None
    canary_verdict: Verdict | None = None
    auto_approve_marginal: bool = False

    def current_time(self) -> datetime:
        return self.now or datetime.now(UTC)


def phase_of(constraint: Constraint) -> Phase:
    if constraint.type in POST_VERIFICATION_TYPES:
        return Phase.POST_VERIFICATION
    return Phase.PRE_DEPLOY


def _evaluate_depends_on(constraint: Constraint, state: PromotionState) -> ConstraintEvaluation:
    target = constraint.environment or ""
    if target in state.succeeded_environments:
        return ConstraintEvaluation(
            constraint.type,
            ConstraintStatus.SATISFIED,
            f"this version was promoted successfully through {target!r}",
        )
    return ConstraintEvaluation(
        constraint.type,
        ConstraintStatus.BLOCKED,
        f"this version has not passed {target!r} yet "
        f"(passed so far: {list(state.succeeded_environments) or 'nothing'})",
    )


def _evaluate_allowed_times(constraint: Constraint, state: PromotionState) -> ConstraintEvaluation:
    now = state.current_time().astimezone(UTC)
    day_name = _WEEKDAY_NAMES[now.weekday()]
    start, end = constraint.hours_utc or (0, 23)

    day_ok = day_name in constraint.days
    hour_ok = start <= now.hour <= end
    inside_window = day_ok and hour_ok

    window = f"{'/'.join(constraint.days)} {start:02d}:00-{end:02d}:59 UTC"
    stamp = now.strftime("%A %H:%M UTC")

    if inside_window:
        return ConstraintEvaluation(
            constraint.type,
            ConstraintStatus.SATISFIED,
            f"{stamp} falls inside the deployment window ({window})",
        )
    if not constraint.enforced:
        return ConstraintEvaluation(
            constraint.type,
            ConstraintStatus.NOT_APPLICABLE,
            f"{stamp} falls outside the deployment window ({window}), "
            f"but this constraint is declared enforced: false",
        )
    return ConstraintEvaluation(
        constraint.type,
        ConstraintStatus.BLOCKED,
        f"{stamp} falls outside the deployment window ({window})",
    )


def _evaluate_manual_judgment(
    constraint: Constraint, state: PromotionState
) -> ConstraintEvaluation:
    verdict = state.canary_verdict

    if constraint.only_when == "canary-marginal" and verdict is not Verdict.MARGINAL:
        return ConstraintEvaluation(
            constraint.type,
            ConstraintStatus.NOT_APPLICABLE,
            f"canary verdict is {verdict.value if verdict else 'absent'}, "
            f"not MARGINAL; no human judgment needed",
        )

    if state.auto_approve_marginal:
        return ConstraintEvaluation(
            constraint.type,
            ConstraintStatus.SATISFIED,
            "marginal canary auto-approved (--auto-approve-marginal was set)",
        )

    return ConstraintEvaluation(
        constraint.type,
        ConstraintStatus.AWAITING_JUDGMENT,
        "canary verdict is MARGINAL; a human must decide whether to promote",
    )


_EVALUATORS = {
    "depends-on": _evaluate_depends_on,
    "allowed-times": _evaluate_allowed_times,
    "manual-judgment": _evaluate_manual_judgment,
}


def evaluate(
    constraints: tuple[Constraint, ...] | list[Constraint],
    state: PromotionState,
    phase: Phase,
) -> list[ConstraintEvaluation]:
    """Evaluate every constraint belonging to `phase`.

    Constraints from other phases are skipped entirely rather than reported,
    so a caller can hand over the full list for each phase.
    """
    evaluations: list[ConstraintEvaluation] = []
    for constraint in constraints:
        if phase_of(constraint) is not phase:
            continue
        evaluator = _EVALUATORS.get(constraint.type)
        if evaluator is None:
            raise ValueError(f"no evaluator registered for constraint type {constraint.type!r}")
        evaluations.append(evaluator(constraint, state))
    return evaluations


def blocking(evaluations: list[ConstraintEvaluation]) -> list[ConstraintEvaluation]:
    """The evaluations that prevent promotion."""
    return [e for e in evaluations if not e.status.allows_promotion]
