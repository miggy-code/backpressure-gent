"""Run pull request code in Docker: installed with network, then executed without it.

Setup runs the repo's install in a container with network and no host environment, and is
committed to a throwaway image. The work container runs from that image with no network, no
host environment, a non-root user and resource limits. The reviewer's commands run there only.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
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
        self.setup_log = ""

    def __enter__(self) -> Sandbox:
        try:
            base = _base_image()
            setup = f"backpressure-setup-{self._id}"
            _docker("create", "--name", setup, "-w", "/work", base, "sh", "-c", INSTALL)
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
        if self._image:
            _docker("rmi", "-f", self._image, check=False)
            self._image = None

    def exec(self, command: str, timeout: int = 120) -> RunResult:
        assert self._container, "sandbox is not running"
        start = time.monotonic()
        # `timeout` signals the whole process group inside the container; the host-side timeout
        # is only a backstop if docker itself hangs.
        argv = ["docker", "exec", self._container, "timeout", "-k", "5", str(timeout), "sh", "-c", command]
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout + 30)
            code, raw = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as exc:
            code, raw = 124, exc.stdout or b""
        seconds = time.monotonic() - start
        output = raw[:MAX_CAPTURE].decode("utf-8", errors="replace")
        if len(raw) > MAX_CAPTURE:
            output += f"\n[output cut at {MAX_CAPTURE} bytes]"
        timed_out = code in (124, 137) and seconds >= timeout
        if timed_out:
            output += f"\n[killed after {timeout}s timeout]"
        return RunResult(command, code, output, timed_out, round(seconds, 2))

    def read_file(self, path: str) -> str:
        assert self._container, "sandbox is not running"
        proc = subprocess.run(["docker", "exec", "-w", "/work", self._container, "cat", "--", path], capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise FileNotFoundError(proc.stderr.decode(errors="replace").strip() or path)
        return proc.stdout.decode("utf-8", errors="replace")


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
