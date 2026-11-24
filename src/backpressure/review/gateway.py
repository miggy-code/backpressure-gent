"""Run one Agentgate model proxy for one review.

The proxy holds the provider key, allows only the listed models and stops the run at its USD and
call caps. The reviewer gets a run token and a local URL, never the key.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

UPSTREAM = "https://api.deepseek.com"
PROVIDER = "openai_compatible"
USER = "lab-runner"

# DeepSeek peak rates (api-docs.deepseek.com, read 2026-11-24). Peak is the safe side for a cap.
RATES = {
    "version": "deepseek-2026-11-24-peak",
    "source": "https://api-docs.deepseek.com/quick_start/pricing (peak column)",
    "currency": "USD",
    "models": {
        "deepseek-v4-pro": {"input_per_million": "1.32", "cached_input_per_million": "0.044", "output_per_million": "3.96"},
        "deepseek-flash": {"input_per_million": "0.30", "cached_input_per_million": "0.006", "output_per_million": "1.20"},
    },
}


class GatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayRun:
    base_url: str
    token: str
    run_id: str
    max_usd: str
    max_calls: int


class Gateway:
    """Context manager: start the proxy on enter, stop it on exit and keep the spend record."""

    def __init__(
        self,
        api_key: str,
        models: tuple[str, ...] = ("deepseek-v4-pro", "deepseek-flash"),
        max_usd: str = "1.00",
        max_calls: int = 80,
        max_output_tokens: int = 32000,
        upstream: str = UPSTREAM,
    ) -> None:
        self._api_key = api_key
        self._models = models
        self._max_usd = max_usd
        self._max_calls = max_calls
        self._max_output_tokens = max_output_tokens
        self._upstream = upstream
        self._dir: Path | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.run: GatewayRun | None = None
        self.spend: dict[str, str] | None = None

    def __enter__(self) -> GatewayRun:
        self._dir = Path(tempfile.mkdtemp(prefix="backpressure-gateway-"))
        d = self._dir
        try:
            (d / "rates.json").write_text(json.dumps(RATES))
            args = [_agentgate(), "init-experiment", str(d / "exp"), "--max-usd", self._max_usd, "--max-calls", str(self._max_calls)]
            args += ["--max-output-tokens", str(self._max_output_tokens), "--user", USER]
            for model in self._models:
                args += ["--model", f"{PROVIDER}:{model}"]
            _check(subprocess.run(args, capture_output=True, text=True, check=False, env=_base_env()), "init-experiment")

            env = _base_env() | {f"AGENTGATE_{PROVIDER.upper()}_API_KEY": self._api_key}
            self._proc = subprocess.Popen(
                [_agentgate(), "model-proxy", "serve", *self._common(), "--upstream", f"{PROVIDER}={self._upstream}",
                 "--rate-table", str(d / "rates.json"), "--token-file", str(d / "run.token"), "--ready-file", str(d / "ready.json"),
                 "--port", "0"],
                stdout=subprocess.DEVNULL,
                stderr=(d / "proxy.log").open("wb"),
                env=env,
            )  # fmt: skip
            ready = self._wait_ready(d / "ready.json")
            self.run = GatewayRun(
                base_url=f"{ready['url']}/v1",
                token=(d / "run.token").read_text().strip(),
                run_id=str(ready["run_id"]),
                max_usd=str(ready["max_usd"]),
                max_calls=int(ready["max_calls"]),
            )
            return self.run
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc: object) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self.run is not None and self._dir is not None:
            self.spend = self._read_spend(self.run.run_id)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
        self._proc = None

    def proxy_log(self) -> str:
        path = self._dir / "proxy.log" if self._dir else None
        return path.read_text(errors="replace") if path and path.exists() else ""

    def _common(self) -> list[str]:
        assert self._dir is not None
        d = self._dir
        return ["--as", USER, "--policies", str(d / "exp" / "policies"), "--fixtures", str(d / "exp" / "fixtures"),
                "--db", str(d / "agentgate.db"), "--audit-db", str(d / "audit.db")]  # fmt: skip

    def _wait_ready(self, path: Path, timeout: float = 60) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            if self._proc is not None and self._proc.poll() is not None:
                raise GatewayError(f"model proxy exited during startup:\n{self.proxy_log()[-2000:]}")
            time.sleep(0.2)
        raise GatewayError("model proxy did not become ready in time")

    def _read_spend(self, run_id: str) -> dict[str, str] | None:
        proc = subprocess.run(
            [_agentgate(), "spend", "show", *self._common(), "--run", run_id], capture_output=True, text=True, check=False, env=_base_env()
        )
        if proc.returncode != 0:
            return None
        # Lines of "<key> <value>": run, spent_usd, calls, open, settled, released.
        return dict(line.split(" ", 1) for line in proc.stdout.strip().splitlines() if " " in line)


def _agentgate() -> str:
    path = shutil.which("agentgate")
    if path is None:
        raise GatewayError("agentgate is not installed in this environment")
    return path


def _base_env() -> dict[str, str]:
    """A minimal environment: no inherited secrets reach the proxy beyond the one key we add."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
    return {k: v for k, v in os.environ.items() if k in keep}


def _check(proc: subprocess.CompletedProcess[str], what: str) -> None:
    if proc.returncode != 0:
        raise GatewayError(f"agentgate {what} failed: {proc.stderr.strip()[-2000:]}")
