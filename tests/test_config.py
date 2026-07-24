"""Tests for delivery config parsing.

The config is the gate. A typo that silently disables a threshold is worse
than no gate at all, because everyone believes it is there -- so every one of
these tests asserts that bad input is *rejected*, not tolerated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from goldenpath.config import ConfigError, load

REPO_ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """
apiVersion: goldenpath/v1
application: demo
artifacts:
  - name: demo
    type: process
    entrypoint: services/paved-road-demo/app.py
    versionStrategy: git-sha
environments:
  - name: test
    strategy:
      type: highlander
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "delivery.yml"
    path.write_text(text, encoding="utf-8")
    return path


class TestRealConfig:
    def test_the_shipped_config_is_valid(self):
        config = load(REPO_ROOT / "delivery" / "delivery.yml")
        assert config.application == "paved-road-demo"
        assert config.environment_names == ["test", "staging", "prod"]

    def test_prod_is_gated_by_every_constraint_type(self):
        config = load(REPO_ROOT / "delivery" / "delivery.yml")
        prod = config.environment("prod")
        assert {c.type for c in prod.constraints} == {
            "depends-on",
            "allowed-times",
            "manual-judgment",
        }
        assert prod.canary is not None
        assert any(m.critical for m in prod.canary.metrics)

    def test_the_shipped_artifact_entrypoint_exists(self):
        config = load(REPO_ROOT / "delivery" / "delivery.yml")
        assert (REPO_ROOT / config.artifacts[0].entrypoint).is_file()


class TestValidation:
    def test_minimal_config_loads(self, tmp_path):
        config = load(write(tmp_path, MINIMAL))
        assert config.environment("test").strategy.type == "highlander"

    def test_unknown_top_level_key_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="unknown key"):
            load(write(tmp_path, MINIMAL + "\nunexpected: true\n"))

    def test_typo_in_a_threshold_is_rejected_not_ignored(self, tmp_path):
        text = (
            MINIMAL
            + """
    verification:
      - type: canary
        passThreshhold: 95
        metrics:
          - name: error_rate_pct
"""
        )
        with pytest.raises(ConfigError, match="unknown key"):
            load(write(tmp_path, text))

    def test_unsupported_api_version_is_rejected(self, tmp_path):
        text = MINIMAL.replace("goldenpath/v1", "goldenpath/v99")
        with pytest.raises(ConfigError, match="unsupported apiVersion"):
            load(write(tmp_path, text))

    def test_unknown_strategy_is_rejected(self, tmp_path):
        text = MINIMAL.replace("highlander", "yolo")
        with pytest.raises(ConfigError, match="unknown type"):
            load(write(tmp_path, text))

    def test_unknown_constraint_type_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
    constraints:
      - type: vibes
"""
        )
        with pytest.raises(ConfigError, match="unknown constraint type"):
            load(write(tmp_path, text))

    def test_marginal_threshold_above_pass_threshold_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
    verification:
      - type: canary
        passThreshold: 50
        marginalThreshold: 90
        metrics:
          - name: error_rate_pct
"""
        )
        with pytest.raises(ConfigError, match="must not exceed"):
            load(write(tmp_path, text))

    def test_tolerance_outside_the_cliffs_delta_range_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
    verification:
      - type: canary
        metrics:
          - name: error_rate_pct
            tolerance: 5
"""
        )
        with pytest.raises(ConfigError, match=r"\[0, 1\]"):
            load(write(tmp_path, text))

    def test_canary_without_metrics_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
    verification:
      - type: canary
        metrics: []
"""
        )
        with pytest.raises(ConfigError, match="at least one metric"):
            load(write(tmp_path, text))

    def test_duplicate_environment_names_are_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
  - name: test
    strategy:
      type: highlander
"""
        )
        with pytest.raises(ConfigError, match="duplicate environment"):
            load(write(tmp_path, text))

    def test_depends_on_an_unknown_environment_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
  - name: prod
    strategy:
      type: red-black
    constraints:
      - type: depends-on
        environment: nowhere
"""
        )
        with pytest.raises(ConfigError, match="unknown environment"):
            load(write(tmp_path, text))

    def test_forward_depends_on_is_rejected_as_unsatisfiable(self, tmp_path):
        # test depends on prod, but test runs first: this promotion order can
        # never be satisfied, so it is a config error rather than a deadlock
        # discovered at 3am.
        text = """
apiVersion: goldenpath/v1
application: demo
artifacts:
  - name: demo
    type: process
    entrypoint: services/paved-road-demo/app.py
    versionStrategy: git-sha
environments:
  - name: test
    strategy:
      type: highlander
    constraints:
      - type: depends-on
        environment: prod
  - name: prod
    strategy:
      type: red-black
"""
        with pytest.raises(ConfigError, match="declared earlier"):
            load(write(tmp_path, text))

    def test_malformed_time_window_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
    constraints:
      - type: allowed-times
        days: [monday]
        hoursUTC: "21-13"
"""
        )
        with pytest.raises(ConfigError, match="must not exceed end"):
            load(write(tmp_path, text))

    def test_invalid_day_name_is_rejected(self, tmp_path):
        text = (
            MINIMAL
            + """
    constraints:
      - type: allowed-times
        days: [funday]
        hoursUTC: "13-21"
"""
        )
        with pytest.raises(ConfigError, match="unknown day name"):
            load(write(tmp_path, text))

    def test_missing_file_reports_the_path(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load(tmp_path / "absent.yml")

    def test_invalid_yaml_is_reported_as_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="invalid YAML"):
            load(write(tmp_path, "apiVersion: [unclosed\n"))
