"""End-to-end tests: real processes, real HTTP, real judgments.

Nothing is mocked here. The orchestrator starts actual server groups, drives
actual traffic through them, and the judge rules on actual measurements. It is
slower than a unit test and worth every second -- a canary gate that has only
ever been tested against synthetic arrays is a gate nobody should trust.

The sample sizes are smaller than the shipped config so the suite stays quick.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from goldenpath.config import load
from goldenpath.executors.process import DeploymentError, ServerGroup, free_port
from goldenpath.orchestrator import BuildProfile, EnvironmentStatus, Orchestrator
from goldenpath.report import to_markdown
from goldenpath.router import TrafficRouter

from .test_config import REPO_ROOT

FAST_CONFIG = """
apiVersion: goldenpath/v1
application: e2e-demo
artifacts:
  - name: e2e-demo
    type: process
    entrypoint: services/paved-road-demo/app.py
    versionStrategy: git-sha
environments:
  - name: test
    strategy:
      type: highlander
    verification:
      - type: smoke
        endpoint: /health
  - name: prod
    constraints:
      - type: depends-on
        environment: test
    strategy:
      type: red-black
      rollbackWindowSeconds: 0
    verification:
      - type: smoke
        endpoint: /health
      - type: canary
        windows: 12
        requestsPerWindow: 10
        concurrency: 5
        passThreshold: 95
        marginalThreshold: 75
        metrics:
          - name: error_rate_pct
            direction: increase
            weight: 5
            critical: true
            minSamples: 10
          - name: request_latency_ms
            direction: increase
            weight: 3
            minSamples: 50
"""


@pytest.fixture
def fast_config(tmp_path):
    path = tmp_path / "delivery.yml"
    path.write_text(FAST_CONFIG, encoding="utf-8")
    return load(path)


def orchestrator_for(config, tmp_path, **kwargs):
    return Orchestrator(config=config, repo_root=REPO_ROOT, log_dir=tmp_path / "logs", **kwargs)


HEALTHY = BuildProfile(version="v1-prod", env={"ERROR_RATE": "0.001", "LATENCY_MS": "12"})


class TestServerGroup:
    def test_a_group_starts_serves_and_stops(self, tmp_path):
        group = ServerGroup(
            name="unit-demo",
            version="v-test",
            role="canary",
            entrypoint=REPO_ROOT / "services" / "paved-road-demo" / "app.py",
            env={"LATENCY_MS": "1"},
        )
        try:
            group.start(log_dir=tmp_path)
            health = group.wait_healthy(timeout=20.0)
            assert health["status"] == "ok"
            assert health["version"] == "v-test"
            assert group.running
        finally:
            group.stop()
        assert not group.running

    def test_a_group_that_cannot_start_fails_loudly(self, tmp_path):
        group = ServerGroup(
            name="broken",
            version="v-bad",
            role="canary",
            entrypoint=REPO_ROOT / "services" / "paved-road-demo" / "app.py",
            # A port number the OS will refuse.
            env={"PORT": "-1"},
        )
        try:
            group.start(log_dir=tmp_path)
            with pytest.raises(DeploymentError):
                group.wait_healthy(timeout=10.0)
        finally:
            group.stop()

    def test_free_port_returns_a_bindable_port(self):
        assert 1024 < free_port() <= 65535


class TestTrafficRouter:
    def test_switching_redirects_live_traffic(self, tmp_path):
        groups = [
            ServerGroup(
                name=f"router-demo-{role}",
                version=f"v-{role}",
                role=role,
                entrypoint=REPO_ROOT / "services" / "paved-road-demo" / "app.py",
                env={"LATENCY_MS": "1"},
            )
            for role in ("baseline", "canary")
        ]
        router = TrafficRouter()
        try:
            for group in groups:
                group.start(log_dir=tmp_path)
                group.wait_healthy(timeout=20.0)
            router.start()

            router.switch_to(groups[0].base_url, groups[0].name, reason="initial")
            assert self._served_version(router) == "v-baseline"

            router.switch_to(groups[1].base_url, groups[1].name, reason="promote")
            assert self._served_version(router) == "v-canary"

            # Rollback is the same operation, pointed the other way.
            router.switch_to(groups[0].base_url, groups[0].name, reason="rollback")
            assert self._served_version(router) == "v-baseline"
            assert [e.to_target for e in router.history] == [
                "router-demo-baseline",
                "router-demo-canary",
                "router-demo-baseline",
            ]
        finally:
            router.stop()
            for group in groups:
                group.stop()

    def test_router_with_no_backend_reports_unavailable(self):
        router = TrafficRouter()
        try:
            router.start()
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"{router.base_url}/health", timeout=5.0)
            assert exc.value.code == 503
        finally:
            router.stop()

    @staticmethod
    def _served_version(router: TrafficRouter) -> str:
        with urllib.request.urlopen(f"{router.base_url}/health", timeout=5.0) as response:
            return json.loads(response.read().decode("utf-8"))["version"]


class TestGoldenPathEndToEnd:
    def test_a_healthy_build_is_promoted_through_every_environment(self, fast_config, tmp_path):
        result = orchestrator_for(fast_config, tmp_path).run(
            candidate=BuildProfile(
                version="v2-good", env={"ERROR_RATE": "0.001", "LATENCY_MS": "12"}
            ),
            baseline=HEALTHY,
        )
        assert result.final_status == "SUCCEEDED"
        assert result.succeeded
        assert [e.status for e in result.environments] == [
            EnvironmentStatus.SUCCEEDED,
            EnvironmentStatus.SUCCEEDED,
        ]

        prod = result.environments[1]
        assert prod.canary is not None
        assert prod.canary.promotable
        assert not prod.rolled_back
        # Traffic ends up on the canary group: baseline in, then the switch.
        assert len(prod.switches) == 2
        assert "canary" in prod.switches[-1].to_target

    def test_a_regressed_build_is_rejected_and_traffic_never_moves(self, fast_config, tmp_path):
        result = orchestrator_for(fast_config, tmp_path).run(
            candidate=BuildProfile(
                version="v3-bad", env={"ERROR_RATE": "0.30", "LATENCY_MS": "150"}
            ),
            baseline=HEALTHY,
        )
        assert result.final_status == "FAILED"

        prod = result.environments[1]
        assert prod.status is EnvironmentStatus.FAILED
        assert prod.canary is not None
        assert not prod.canary.promotable
        assert prod.rolled_back

        # The single switch is the baseline coming into service. The canary
        # never received a request that mattered.
        assert len(prod.switches) == 1
        assert "baseline" in prod.switches[0].to_target

        failed_metrics = {m.name for m in prod.canary.failures}
        assert "error_rate_pct" in failed_metrics

    def test_the_error_rate_metric_is_critical_enough_to_fail_alone(self, fast_config, tmp_path):
        # Errors up, latency unchanged: the weighted score would survive, the
        # critical flag must not let it.
        result = orchestrator_for(fast_config, tmp_path).run(
            candidate=BuildProfile(
                version="v4-errors", env={"ERROR_RATE": "0.35", "LATENCY_MS": "12"}
            ),
            baseline=HEALTHY,
        )
        prod = result.environments[1]
        assert prod.canary is not None
        assert "Critical metric regression" in prod.canary.summary
        assert prod.status is EnvironmentStatus.FAILED

    def test_an_environment_after_a_failure_is_never_reached(self, fast_config, tmp_path):
        result = orchestrator_for(fast_config, tmp_path).run(
            candidate=BuildProfile(version="v5-bad", env={"ERROR_RATE": "0.40"}),
            baseline=HEALTHY,
            only_environment=None,
        )
        assert result.environments[-1].status in (
            EnvironmentStatus.FAILED,
            EnvironmentStatus.NOT_REACHED,
        )

    def test_depends_on_blocks_an_environment_run_in_isolation(self, fast_config, tmp_path):
        result = orchestrator_for(fast_config, tmp_path).run(
            candidate=BuildProfile(version="v6", env={"LATENCY_MS": "12"}),
            baseline=HEALTHY,
            only_environment="prod",
        )
        prod = result.environments[0]
        assert prod.status is EnvironmentStatus.BLOCKED
        assert prod.canary is None  # Nothing was deployed at all.

    def test_the_report_explains_the_decision(self, fast_config, tmp_path):
        result = orchestrator_for(fast_config, tmp_path).run(
            candidate=BuildProfile(
                version="v7-bad", env={"ERROR_RATE": "0.30", "LATENCY_MS": "150"}
            ),
            baseline=HEALTHY,
        )
        markdown = to_markdown(result)
        assert "Why it was rejected" in markdown
        assert "error_rate_pct" in markdown
        assert "Rolled back" in markdown
        # The numbers an engineer needs are present, not just a red X.
        assert "Cliff's delta" in markdown
        assert "p-value" in markdown

        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["status"] == "FAILED"
        assert payload["environments"][1]["canary"]["verdict"] == "FAIL"
