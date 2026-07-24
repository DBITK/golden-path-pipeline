"""Tests for environment constraint evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from goldenpath.canary.judge import Verdict
from goldenpath.config import Constraint
from goldenpath.constraints import (
    ConstraintStatus,
    Phase,
    PromotionState,
    blocking,
    evaluate,
    phase_of,
)

# Wednesday 2026-07-22, 15:00 UTC -- inside a Mon-Thu 13-21 window.
WEDNESDAY_AFTERNOON = datetime(2026, 7, 22, 15, 0, tzinfo=UTC)
FRIDAY_EVENING = datetime(2026, 7, 24, 22, 30, tzinfo=UTC)


class TestPhaseAssignment:
    def test_prerequisites_are_checked_before_deploying(self):
        assert phase_of(Constraint(type="depends-on", environment="test")) is Phase.PRE_DEPLOY
        assert phase_of(Constraint(type="allowed-times")) is Phase.PRE_DEPLOY

    def test_judgment_waits_until_there_is_evidence_to_judge(self):
        assert phase_of(Constraint(type="manual-judgment")) is Phase.POST_VERIFICATION

    def test_evaluate_ignores_constraints_from_other_phases(self):
        constraints = [
            Constraint(type="depends-on", environment="test"),
            Constraint(type="manual-judgment", only_when="canary-marginal"),
        ]
        state = PromotionState(succeeded_environments=("test",))
        assert len(evaluate(constraints, state, Phase.PRE_DEPLOY)) == 1
        assert len(evaluate(constraints, state, Phase.POST_VERIFICATION)) == 1


class TestDependsOn:
    def test_satisfied_once_the_upstream_environment_has_passed(self):
        result = evaluate(
            [Constraint(type="depends-on", environment="staging")],
            PromotionState(succeeded_environments=("test", "staging")),
            Phase.PRE_DEPLOY,
        )[0]
        assert result.status is ConstraintStatus.SATISFIED

    def test_blocks_when_the_upstream_environment_has_not_passed(self):
        result = evaluate(
            [Constraint(type="depends-on", environment="staging")],
            PromotionState(succeeded_environments=("test",)),
            Phase.PRE_DEPLOY,
        )[0]
        assert result.status is ConstraintStatus.BLOCKED
        assert "staging" in result.reason

    def test_blocks_when_nothing_has_passed_and_says_so(self):
        result = evaluate(
            [Constraint(type="depends-on", environment="test")],
            PromotionState(),
            Phase.PRE_DEPLOY,
        )[0]
        assert result.status is ConstraintStatus.BLOCKED
        assert "nothing" in result.reason


class TestAllowedTimes:
    def _window(self, enforced: bool = True) -> Constraint:
        return Constraint(
            type="allowed-times",
            days=("monday", "tuesday", "wednesday", "thursday"),
            hours_utc=(13, 21),
            enforced=enforced,
        )

    def test_inside_the_window_is_satisfied(self):
        result = evaluate(
            [self._window()],
            PromotionState(now=WEDNESDAY_AFTERNOON),
            Phase.PRE_DEPLOY,
        )[0]
        assert result.status is ConstraintStatus.SATISFIED

    def test_friday_evening_is_blocked(self):
        result = evaluate([self._window()], PromotionState(now=FRIDAY_EVENING), Phase.PRE_DEPLOY)[0]
        assert result.status is ConstraintStatus.BLOCKED
        assert "Friday" in result.reason

    def test_right_day_but_wrong_hour_is_blocked(self):
        before_open = WEDNESDAY_AFTERNOON.replace(hour=6)
        result = evaluate([self._window()], PromotionState(now=before_open), Phase.PRE_DEPLOY)[0]
        assert result.status is ConstraintStatus.BLOCKED

    def test_window_boundaries_are_inclusive(self):
        for hour in (13, 21):
            result = evaluate(
                [self._window()],
                PromotionState(now=WEDNESDAY_AFTERNOON.replace(hour=hour, minute=59)),
                Phase.PRE_DEPLOY,
            )[0]
            assert result.status is ConstraintStatus.SATISFIED

    def test_unenforced_window_reports_the_breach_without_blocking(self):
        result = evaluate(
            [self._window(enforced=False)],
            PromotionState(now=FRIDAY_EVENING),
            Phase.PRE_DEPLOY,
        )[0]
        assert result.status is ConstraintStatus.NOT_APPLICABLE
        assert result.status.allows_promotion
        assert "enforced: false" in result.reason

    def test_non_utc_input_is_converted_before_comparison(self):
        # 09:00 in UTC-5 is 14:00 UTC, which is inside the window.
        eastern = timezone(timedelta(hours=-5))
        local = datetime(2026, 7, 22, 9, 0, tzinfo=eastern)
        result = evaluate([self._window()], PromotionState(now=local), Phase.PRE_DEPLOY)[0]
        assert result.status is ConstraintStatus.SATISFIED


class TestManualJudgment:
    CONSTRAINT = Constraint(type="manual-judgment", only_when="canary-marginal")

    def _evaluate(self, verdict, auto_approve=False):
        return evaluate(
            [self.CONSTRAINT],
            PromotionState(canary_verdict=verdict, auto_approve_marginal=auto_approve),
            Phase.POST_VERIFICATION,
        )[0]

    def test_a_clean_pass_does_not_summon_a_human(self):
        result = self._evaluate(Verdict.PASS)
        assert result.status is ConstraintStatus.NOT_APPLICABLE
        assert result.status.allows_promotion

    def test_a_marginal_canary_waits_for_a_human(self):
        result = self._evaluate(Verdict.MARGINAL)
        assert result.status is ConstraintStatus.AWAITING_JUDGMENT
        assert not result.status.allows_promotion

    def test_marginal_is_not_auto_approved_by_default(self):
        # Unattended has to mean the safe answer, not the convenient one.
        assert self._evaluate(Verdict.MARGINAL).status is ConstraintStatus.AWAITING_JUDGMENT

    def test_marginal_can_be_auto_approved_when_explicitly_opted_in(self):
        result = self._evaluate(Verdict.MARGINAL, auto_approve=True)
        assert result.status is ConstraintStatus.SATISFIED
        assert "auto-approved" in result.reason

    def test_an_unconditional_judgment_gate_always_asks(self):
        result = evaluate(
            [Constraint(type="manual-judgment", only_when=None)],
            PromotionState(canary_verdict=Verdict.PASS),
            Phase.POST_VERIFICATION,
        )[0]
        assert result.status is ConstraintStatus.AWAITING_JUDGMENT


class TestBlocking:
    def test_reports_only_the_constraints_that_prevent_promotion(self):
        evaluations = evaluate(
            [
                Constraint(type="depends-on", environment="test"),
                Constraint(type="depends-on", environment="staging"),
            ],
            PromotionState(succeeded_environments=("test",)),
            Phase.PRE_DEPLOY,
        )
        blockers = blocking(evaluations)
        assert len(blockers) == 1
        assert "staging" in blockers[0].reason
