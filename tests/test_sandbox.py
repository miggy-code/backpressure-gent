"""The sandbox's guarantees, against real Docker. Skipped where Docker is not running."""

import subprocess
from pathlib import Path

import pytest

from backpressure.review.sandbox import Sandbox, docker_available

pytestmark = [pytest.mark.docker, pytest.mark.skipif(not docker_available(), reason="Docker is not running")]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory: pytest.TempPathFactory):
    repo: Path = tmp_path_factory.mktemp("repo")
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with Sandbox(repo) as box:
        yield box


def test_runs_the_repo_tests(sandbox: Sandbox) -> None:
    result = sandbox.exec("python -m pytest -q")
    assert result.exit_code == 0, result.output
    assert "1 passed" in result.output


def test_has_no_network(sandbox: Sandbox) -> None:
    probe = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('CONNECTED')"
    result = sandbox.exec(f'python -c "{probe}"')
    assert result.exit_code != 0
    assert "CONNECTED" not in result.output


def test_sees_no_host_environment(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKPRESSURE_CANARY_SECRET", "do-not-leak")
    result = sandbox.exec("env")
    assert "do-not-leak" not in result.output
    assert "DEEPSEEK" not in result.output
    assert "GITHUB_TOKEN" not in result.output


def test_runs_as_non_root(sandbox: Sandbox) -> None:
    assert sandbox.exec("id -u").output.strip() == "1000"


def test_long_command_is_killed_at_the_timeout(sandbox: Sandbox) -> None:
    result = sandbox.exec("sleep 200", timeout=3)
    assert result.timed_out
    assert result.seconds < 20


def test_reads_files_from_the_container(sandbox: Sandbox) -> None:
    assert "def test_ok" in sandbox.read_file("test_ok.py")
    with pytest.raises(FileNotFoundError):
        sandbox.read_file("missing.py")


def test_flooded_output_is_capped_without_buffering_it(sandbox: Sandbox) -> None:
    import resource
    import sys

    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss is bytes on macOS, KiB on Linux
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    result = sandbox.exec("head -c 300000000 /dev/zero; echo done", timeout=60)
    grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale - before) / 1_000_000
    assert "[output cut at" in result.output
    assert len(result.output) < 1_100_000
    assert grown_mb < 100
