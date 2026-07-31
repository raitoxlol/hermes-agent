"""Container-restart survives per-profile gateway registrations.

The s6 dynamic scandir at /run/service/ lives on tmpfs and is wiped
on every container restart. Phase 4 Task 4.0's container_boot module
+ cont-init.d/02-reconcile-profiles regenerate the service slots from
$HERMES_HOME/profiles/<name>/gateway_state.json on every boot and
auto-start only those whose last state was `running`.

These tests stand up a container with a named volume, create profiles
inside it in various gateway states, restart the container, and
assert the reconciler did the right thing.

Every ``docker exec`` here runs as the unprivileged ``hermes`` user
(via :func:`docker_exec` / :func:`docker_exec_sh` in conftest); see
the conftest module docstring.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from tests.docker.conftest import docker_exec, docker_exec_sh, wait_for_path, wait_for_log, wait_for_docker_logs, poll_container


def _docker(*args: str, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True, text=True, timeout=kw.pop("timeout", 60),
        **kw,
    )





def _wait_for_reconcile_log_mention(
    container: str,
    profile: str,
    *,
    deadline_s: float = 30.0,
    interval_s: float = 0.25,
) -> str:
    """Poll until /opt/data/logs/container-boot.log mentions `profile`.
    """
    return wait_for_log(container, "/opt/data/logs/container-boot.log",  f"profile={profile}")


@pytest.fixture
def restart_container(request, built_image: str):
    """A long-running container with a named volume so docker restart
    preserves $HERMES_HOME/profiles/."""
    safe = request.node.name.replace("[", "_").replace("]", "_")
    name = f"hermes-restart-{safe}"
    volume = f"hermes-restart-vol-{safe}"
    _docker("rm", "-f", name)
    _docker("volume", "rm", "-f", volume)
    _docker("volume", "create", volume, timeout=10).check_returncode()
    r = _docker(
        "run", "-d", "--name", name,
        "-v", f"{volume}:/opt/data",
        built_image, "sleep", "infinity",
        timeout=30,
    )
    r.check_returncode()
    # Wait for s6 + stage2 + 02-reconcile to publish the boot log so
    # the test can rely on the default slot being registered before
    # it starts issuing commands. The reconciler always writes one
    # 'default' line on every boot (PR #30136 item I1) — that's our
    # readiness signal.
    wait_for_log(name, "/opt/data/logs/container-boot.log", "profile=default")
    yield name
    _docker("rm", "-f", name)
    _docker("volume", "rm", "-f", volume)


def test_stopped_gateway_stays_stopped_after_restart(restart_container: str) -> None:
    container = restart_container

    docker_exec(container, "hermes", "profile", "create", "writer").check_returncode()

    # Write 'stopped' directly so we don't have to race against the
    # gateway's own state writes.
    write_state = (
        "import json, pathlib; "
        "p = pathlib.Path('/opt/data/profiles/writer/gateway_state.json'); "
        "p.write_text(json.dumps({'gateway_state': 'stopped', 'timestamp': 1}))"
    )
    docker_exec(container, "python3", "-c", write_state, timeout=10).check_returncode()

    _docker("restart", container, timeout=60).check_returncode()
    _wait_for_reconcile_log_mention(container, "writer", deadline_s=30.0)

    # Slot exists.
    assert wait_for_path(
        container, "/run/service/gateway-writer", kind="d", deadline_s=10.0,
    )

    # Down marker present.
    r = docker_exec_sh(container, "test -f /run/service/gateway-writer/down")
    assert r.returncode == 0, "down marker missing despite prior_state=stopped"
