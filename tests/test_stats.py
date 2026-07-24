"""Tests for the statistical primitives.

These are the tests that matter most in the repository. Every deployment
decision is downstream of this arithmetic, so it is checked against
hand-computed values rather than against its own output.
"""

from __future__ import annotations

import math

import pytest

from goldenpath.canary.stats import (
    fractional_ranks,
    mann_whitney_u,
    normal_cdf,
    percentile,
)


class TestFractionalRanks:
    def test_ranks_distinct_values_ascending(self):
        assert fractional_ranks([30.0, 10.0, 20.0]) == [3.0, 1.0, 2.0]

    def test_ties_share_the_midpoint_rank(self):
        # Values 5,5 occupy ranks 1 and 2, so both take 1.5.
        assert fractional_ranks([5.0, 5.0, 9.0]) == [1.5, 1.5, 3.0]

    def test_all_tied_values_share_one_rank(self):
        assert fractional_ranks([7.0, 7.0, 7.0, 7.0]) == [2.5, 2.5, 2.5, 2.5]

    def test_rank_sum_matches_the_closed_form(self):
        values = [4.0, 1.0, 1.0, 9.0, 3.0, 9.0, 2.0]
        n = len(values)
        assert sum(fractional_ranks(values)) == pytest.approx(n * (n + 1) / 2)


class TestMannWhitneyU:
    def test_identical_samples_are_indistinguishable(self):
        sample = [1.0, 2.0, 3.0, 4.0]
        result = mann_whitney_u(sample, list(sample))
        assert result.p_value == pytest.approx(1.0, abs=0.02)
        assert result.cliffs_delta == pytest.approx(0.0)

    def test_completely_separated_samples_give_delta_of_minus_one(self):
        result = mann_whitney_u([1.0, 2.0, 3.0, 4.0, 5.0], [6.0, 7.0, 8.0, 9.0, 10.0])
        assert result.u_statistic == pytest.approx(0.0)
        assert result.cliffs_delta == pytest.approx(-1.0)
        # Hand-computed: mu=12.5, sigma=sqrt(25/12*11)=4.7871,
        # z=(12.5-0.5)/4.7871=2.5068, two-sided p=0.0122.
        assert result.z_score == pytest.approx(-2.5068, abs=1e-3)
        assert result.p_value == pytest.approx(0.0122, abs=1e-3)

    def test_delta_is_positive_when_the_canary_runs_higher(self):
        result = mann_whitney_u([10.0, 11.0, 12.0], [1.0, 2.0, 3.0])
        assert result.cliffs_delta == pytest.approx(1.0)
        assert result.u_statistic == pytest.approx(9.0)

    def test_u_statistic_matches_the_hand_computed_value(self):
        # canary ranks: 1(1), 3(3), 5(5) -> rank sum 9; U = 9 - 3*4/2 = 3.
        result = mann_whitney_u([1.0, 3.0, 5.0], [2.0, 4.0, 6.0])
        assert result.u_statistic == pytest.approx(3.0)
        assert result.cliffs_delta == pytest.approx(2 * 3 / 9 - 1)

    def test_all_values_tied_reports_no_difference(self):
        result = mann_whitney_u([5.0] * 6, [5.0] * 6)
        assert result.p_value == 1.0
        assert result.z_score == 0.0

    def test_ties_widen_the_p_value_relative_to_distinct_values(self):
        # Tie correction shrinks the variance denominator's ties term, so a
        # tied sample should never look *more* significant than a clean one.
        tied = mann_whitney_u([1.0] * 10 + [2.0] * 10, [1.0] * 10 + [2.0] * 10)
        assert tied.p_value == pytest.approx(1.0, abs=0.02)

    def test_empty_sample_is_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            mann_whitney_u([], [1.0, 2.0])

    def test_p_value_stays_within_bounds_for_extreme_separation(self):
        canary = [float(i) for i in range(200, 400)]
        baseline = [float(i) for i in range(200)]
        result = mann_whitney_u(canary, baseline)
        assert 0.0 <= result.p_value <= 1.0
        assert result.p_value < 1e-6
        assert result.cliffs_delta == pytest.approx(1.0)


class TestNormalCdf:
    def test_known_quantiles(self):
        assert normal_cdf(0.0) == pytest.approx(0.5)
        assert normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
        assert normal_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)

    def test_is_monotonic(self):
        values = [normal_cdf(z) for z in [-3.0, -1.0, 0.0, 1.0, 3.0]]
        assert values == sorted(values)
        assert all(0.0 <= v <= 1.0 for v in values)


class TestPercentile:
    def test_median_of_odd_sample(self):
        assert percentile([3.0, 1.0, 2.0], 50.0) == 2.0

    def test_median_of_even_sample_interpolates(self):
        assert percentile([1.0, 2.0, 3.0, 4.0], 50.0) == pytest.approx(2.5)

    def test_bounds(self):
        values = [float(i) for i in range(1, 101)]
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 100.0) == 100.0
        # position = (100-1)*0.95 = 94.05, interpolating between the 95th and
        # 96th ordered values.
        assert percentile(values, 95.0) == pytest.approx(95.05)

    def test_single_value(self):
        assert percentile([42.0], 95.0) == 42.0

    def test_rejects_empty_sample(self):
        with pytest.raises(ValueError, match="empty"):
            percentile([], 50.0)

    @pytest.mark.parametrize("q", [-1.0, 101.0, math.inf])
    def test_rejects_out_of_range_q(self, q):
        with pytest.raises(ValueError, match=r"\[0, 100\]"):
            percentile([1.0, 2.0], q)
