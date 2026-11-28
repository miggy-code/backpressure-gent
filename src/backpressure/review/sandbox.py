"""Run pull request code in Docker: installed with network, then executed without it.

Setup runs the repo's install in a container with network and no host environment, and is
committed to a throwaway image. The work container runs from that image with no network, no
host environment, a non-root user and resource limits. The reviewer's commands run there only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

UV_VERSION = "0.9.27"
# git for the reviewer's diffs and stashes, uv for installs. Built once per Dockerfile hash.
DOCKERFILE = f"""FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \\
 && pip install --no-cache-dir uv=={UV_VERSION} \\
 && git config --system user.name sandbox && git config --system user.email sandbox@localhost \\
 && git config --system safe.directory '*'
"""

# Runs as root in the setup container. Installs into /work/.venv so the work container finds it.
INSTALL = """set -e
cd /work
export UV_LINK_MODE=copy UV_CACHE_DIR=/tmp/uv-cache UV_PYTHON_INSTALL_DIR=/opt/uv-python
if [ -f uv.lock ]; then
  uv sync --all-extras --frozen
elif [ -f pyproject.toml ] || [ -f setup.py ]; then
  uv venv -q .venv
  uv pip install -q -p .venv -e '.[test]' || uv pip install -q -p .venv -e .
else
  uv venv -q .venv
fi
for f in requirements*.txt; do [ -f "$f" ] && uv pip install -q -p .venv -r "$f"; done
.venv/bin/python -c 'import pytest' 2>/dev/null || uv pip install -q -p .venv pytest
rm -rf /tmp/uv-cache
chown -R 1000:1000 /work
"""

# Builds the holdout venv from the MERGE BASE's dependencies only (the base is already merged, so it is
# trusted), never from head code. Runs as root with network in a container that holds nothing from the PR.
HOLDOUT_VENV = "/opt/holdout-venv"
BUILD_HOLDOUT_VENV = f"""set -e
cd /src
export UV_LINK_MODE=copy UV_CACHE_DIR=/tmp/uv-cache UV_PYTHON_DOWNLOADS=never
uv venv -q --python /usr/local/bin/python {HOLDOUT_VENV}
add() {{ uv pip install -q -p {HOLDOUT_VENV} "$@"; }}
if [ -f uv.lock ]; then
  uv export -q --frozen --all-extras --no-hashes --no-emit-project --no-emit-workspace -o /tmp/req.txt
  add -r /tmp/req.txt
elif [ -f pyproject.toml ]; then
  add -r pyproject.toml --extra test || add -r pyproject.toml
fi
for f in requirements*.txt; do [ -f "$f" ] && add -r "$f"; done
{HOLDOUT_VENV}/bin/python -c 'import pytest' 2>/dev/null || add pytest
chmod -R a+rX {HOLDOUT_VENV}
"""

WORK_ENV = {
    "HOME": "/tmp",
    "PATH": "/work/.venv/bin:/usr/local/bin:/usr/bin:/bin",
    "VIRTUAL_ENV": "/work/.venv",
    "UV_OFFLINE": "1",
    "UV_PYTHON_INSTALL_DIR": "/opt/uv-python",
    "PYTHONDONTWRITEBYTECODE": "1",
}
LIMITS = ["--network", "none", "--user", "1000:1000", "--memory", "2g", "--pids-limit", "256", "--cpus", "2",
          "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]  # fmt: skip
# Setup has network (to install) and runs the repo's build code as root, so it still gets limits.
SETUP_LIMITS = ["--memory", "4g", "--pids-limit", "512", "--cpus", "2", "--security-opt", "no-new-privileges"]
MAX_CAPTURE = 1_000_000  # bytes of output kept per run; beyond this the command is noise


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool
    seconds: float


class Workspace(Protocol):
    """What the reviewer's tools need from a sandbox. Tests use an in-memory fake."""

    def exec(self, command: str, timeout: int = 120) -> RunResult: ...

    def read_file(self, path: str) -> str: ...


class Sandbox:
    """Context manager over a host checkout: build the image, start the work container, clean up."""

    def __init__(self, checkout: Path, setup_timeout: int = 900) -> None:
        self._checkout = checkout
        self._setup_timeout = setup_timeout
        self._id = uuid.uuid4().hex[:12]
        self._image: str | None = None
        self._container: str | None = None
        self._venv: str | None = None  # Docker volume holding the holdout venv, built on first use
        self.setup_log = ""

    def __enter__(self) -> Sandbox:
        try:
            base = _base_image()
            setup = f"backpressure-setup-{self._id}"
            _docker("create", "--name", setup, *SETUP_LIMITS, "-w", "/work", base, "sh", "-c", INSTALL)
            try:
                _docker("cp", f"{self._checkout}/.", f"{setup}:/work")
                proc = _docker("start", "-a", setup, check=False, timeout=self._setup_timeout)
                self.setup_log = proc.stdout
                if proc.returncode != 0:
                    raise SandboxError(f"install failed in the setup container:\n{proc.stdout[-4000:]}")
                self._image = f"backpressure-sandbox:{self._id}"
                _docker("commit", setup, self._image)
            finally:
                _docker("rm", "-f", setup, check=False)
            self._container = f"backpressure-work-{self._id}"
            env = [arg for k, v in WORK_ENV.items() for arg in ("-e", f"{k}={v}")]
            _docker("run", "-d", "--name", self._container, *LIMITS, *env, "-w", "/work", self._image, "sleep", "infinity")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc: object) -> None:
        if self._container:
            _docker("rm", "-f", self._container, check=False)
            self._container = None
        if self._venv:
            _docker("volume", "rm", "-f", self._venv, check=False)
            self._venv = None
        if self._image:
            _docker("rmi", "-f", self._image, check=False)
            self._image = None

    def exec(self, command: str, timeout: int = 120) -> RunResult:
        assert self._container, "sandbox is not running"
        # `timeout` signals the whole process group inside the container; the host-side timeout
        # is only a backstop if docker itself hangs.
        return _stream(["docker", "exec", self._container, "timeout", "-k", "5", str(timeout), "sh", "-c", command], command, timeout)

    def build_holdout_venv(self, ref: str) -> str:
        """Build /opt/holdout-venv from the dependencies at `ref`, and return the venv's path in the container.

        The builder container starts from the bare base image and receives only `git archive ref`, so no
        install or build code from the PR's head ever runs next to, or writes into, this venv. It lands in
        a Docker volume that `run_isolated(..., holdout_venv=True)` mounts read-only. Raises SandboxError.
        """
        if self._venv:
            return HOLDOUT_VENV
        volume = f"backpressure-venv-{self._id}"
        builder = f"backpressure-venvbuild-{self._id}"
        # The checkout holds PR-chosen .gitattributes: ignore host git config so no filter runs on the host.
        env = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        _docker("volume", "create", volume)
        self._venv = volume
        try:
            _docker("create", "--name", builder, *SETUP_LIMITS, "-v", f"{volume}:{HOLDOUT_VENV}", "-w", "/src",
                    _base_image(), "sh", "-c", BUILD_HOLDOUT_VENV)  # fmt: skip
            archive = subprocess.run(
                ["git", "-C", str(self._checkout), "archive", "--prefix=src/", ref], capture_output=True, env=env, check=False
            )
            if archive.returncode != 0:
                raise SandboxError(f"git archive {ref[:7]} failed: {archive.stderr.decode(errors='replace')[-500:]}")
            put = subprocess.run(["docker", "cp", "-", f"{builder}:/"], input=archive.stdout, capture_output=True, check=False)
            if put.returncode != 0:
                raise SandboxError(f"docker cp failed: {put.stderr.decode(errors='replace')[-500:]}")
            proc = _docker("start", "-a", builder, check=False, timeout=self._setup_timeout)
            if proc.returncode != 0:
                raise SandboxError(f"building the holdout venv failed:\n{proc.stdout[-2000:]}")
        finally:
            _docker("rm", "-f", builder, check=False)
        return HOLDOUT_VENV

    def run_isolated(self, command: str, mounts: dict[Path, str], timeout: int = 600, holdout_venv: bool = False) -> RunResult:
        """Run in a fresh container from the installed image, apart from the model's work container.

        Used for checks the model must not see, such as holdouts: `mounts` are bind-mounted read-only
        here and never in the work container. Same limits, no network, removed afterwards. With
        `holdout_venv` (built first by `build_holdout_venv`) that venv is mounted read-only and put
        first on PATH in place of the PR's own `/work/.venv`.
        """
        assert self._image, "sandbox is not running"
        name = f"backpressure-isolated-{uuid.uuid4().hex[:12]}"
        work_env = dict(WORK_ENV)
        volumes = [arg for host, target in mounts.items() for arg in ("-v", f"{host.resolve()}:{target}:ro")]
        if holdout_venv:
            assert self._venv, "build_holdout_venv has not run"
            volumes += ["-v", f"{self._venv}:{HOLDOUT_VENV}:ro"]
            work_env |= {"PATH": f"{HOLDOUT_VENV}/bin:/usr/local/bin:/usr/bin:/bin", "VIRTUAL_ENV": HOLDOUT_VENV}
        env = [arg for k, v in work_env.items() for arg in ("-e", f"{k}={v}")]
        argv = ["docker", "run", "--name", name, *LIMITS, *env, *volumes, "-w", "/work", self._image]
        try:
            return _stream([*argv, "timeout", "-k", "5", str(timeout), "sh", "-c", command], command, timeout)
        finally:
            _docker("rm", "-f", name, check=False)

    def env(self) -> list[str]:
        """The work container's environment as Docker sees it (KEY=value entries)."""
        assert self._container, "sandbox is not running"
        return json.loads(_docker("inspect", "-f", "{{json .Config.Env}}", self._container).stdout)

    def read_file(self, path: str) -> str:
        assert self._container, "sandbox is not running"
        proc = subprocess.run(["docker", "exec", "-w", "/work", self._container, "cat", "--", path], capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise FileNotFoundError(proc.stderr.decode(errors="replace").strip() or path)
        return proc.stdout.decode("utf-8", errors="replace")


def _stream(argv: list[str], command: str, timeout: int) -> RunResult:
    start = time.monotonic()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    kept = bytearray()
    total = 0

    def drain() -> None:
        # Keep the first MAX_CAPTURE bytes and discard the rest, so a flood cannot exhaust host memory.
        nonlocal total
        assert proc.stdout is not None
        while chunk := proc.stdout.read(65536):
            total += len(chunk)
            if len(kept) < MAX_CAPTURE:
                kept.extend(chunk[: MAX_CAPTURE - len(kept)])

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        code = 124
    reader.join(timeout=10)
    seconds = time.monotonic() - start
    output = bytes(kept).decode("utf-8", errors="replace")
    if total > MAX_CAPTURE:
        output += f"\n[output cut at {MAX_CAPTURE} of {total} bytes]"
    timed_out = code in (124, 137) and seconds >= timeout
    if timed_out:
        output += f"\n[killed after {timeout}s timeout]"
    return RunResult(command, code, output, timed_out, round(seconds, 2))


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def _base_image() -> str:
    tag = f"backpressure-sandbox-base:{hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:12]}"
    if _docker("image", "inspect", tag, check=False).returncode != 0:
        proc = subprocess.run(["docker", "build", "-q", "-t", tag, "-"], input=DOCKERFILE, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise SandboxError(f"could not build the sandbox base image:\n{proc.stderr[-2000:]}")
    return tag


def _docker(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, errors="replace", check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"docker {args[0]} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise SandboxError(f"docker {' '.join(args[:2])} failed: {proc.stderr.strip()[-2000:]}")
    if args[:2] == ("start", "-a"):
        # start -a streams the container's stdout and stderr to ours; keep both for the log.
        return subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout + proc.stderr, "")
    return proc
