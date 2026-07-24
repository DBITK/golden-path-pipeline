"""Process executor: server groups as local OS processes.

Spinnaker deploys *server groups* -- immutable, versioned sets of instances --
and its deployment strategies are all defined in terms of creating a new server
group and deciding what happens to the old one. This executor implements the
same model at the smallest scale that is still real: one server group is one
process, and traffic reaches it through the router.

Nothing here is mocked. Processes really start, really bind ports, really serve
HTTP, and really get killed on rollback. Keeping the abstraction honest is what
makes the executor interchangeable: a Kubernetes executor swaps
`ServerGroup.start` for an `apply` and the orchestrator above it does not
change.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class DeploymentError(RuntimeError):
    """Raised when a server group cannot be brought into service."""


def free_port() -> int:
    """Ask the OS for an unused local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ServerGroup:
    """One immutable, versioned deployment of the artifact.

    Attributes:
        name: Spinnaker-style server group name, e.g. `paved-road-demo-v003`.
        version: The immutable artifact version this group runs.
        role: `baseline` or `canary`, surfaced by the service in its responses.
        entrypoint: Path to the service entrypoint.
        env: Extra environment for the process -- how a deliberately bad build
            is deployed for the rollback demonstration.
    """

    name: str
    version: str
    role: str
    entrypoint: Path
    env: dict[str, str] = field(default_factory=dict)
    port: int = 0
    process: subprocess.Popen | None = None
    log_path: Path | None = None
    _log_handle: object | None = field(default=None, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, log_dir: Path | None = None) -> None:
        """Launch the process. Does not wait for it to become healthy."""
        if self.running:
            raise DeploymentError(f"{self.name} is already running")

        self.port = self.port or free_port()

        environment = os.environ.copy()
        environment.update(
            {
                "PORT": str(self.port),
                "VERSION": self.version,
                "ROLE": self.role,
            }
        )
        environment.update(self.env)

        stdout = subprocess.DEVNULL
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = log_dir / f"{self.name}.log"
            self._log_handle = self.log_path.open("w", encoding="utf-8")
            stdout = self._log_handle

        self.process = subprocess.Popen(  # noqa: S603 - fixed interpreter, no shell
            [sys.executable, "-u", str(self.entrypoint)],
            env=environment,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            cwd=str(self.entrypoint.parent),
        )

    def wait_healthy(self, endpoint: str = "/health", timeout: float = 20.0) -> dict:
        """Poll the health endpoint until it answers or the deadline passes.

        Returns:
            The parsed health payload.

        Raises:
            DeploymentError: on timeout, or if the process exits early.
        """
        deadline = time.monotonic() + timeout
        last_error = "no attempt made"
        url = f"{self.base_url}{endpoint}"

        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise DeploymentError(
                    f"{self.name} exited with code {self.process.returncode} before "
                    f"becoming healthy; see {self.log_path}"
                )
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if response.status == 200:
                        return json.loads(response.read().decode("utf-8"))
                    last_error = f"health returned HTTP {response.status}"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(0.1)

        raise DeploymentError(f"{self.name} did not become healthy within {timeout}s: {last_error}")

    def stop(self, grace_seconds: float = 3.0) -> None:
        """Terminate the process, escalating to a kill if it will not go."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=grace_seconds)
        if self._log_handle is not None:
            try:
                self._log_handle.close()  # type: ignore[union-attr]
            finally:
                self._log_handle = None
