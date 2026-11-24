"""Pipeline guards that need no network: the sandbox-environment check."""

import json
from pathlib import Path

import pytest

from backpressure.review.pipeline import ReviewError, _check_sandbox_env


def test_records_names_only_and_passes_a_clean_environment(tmp_path: Path) -> None:
    _check_sandbox_env(["HOME=/tmp", "PATH=/usr/bin"], "sk-secret-value", tmp_path)
    assert json.loads((tmp_path / "sandbox-env.json").read_text()) == ["HOME", "PATH"]


def test_stops_when_the_model_key_reached_the_sandbox(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="secret reached the sandbox"):
        _check_sandbox_env(["LEAK=sk-secret-value"], "sk-secret-value", tmp_path)


def test_stops_when_the_github_token_reached_the_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_token_value")
    with pytest.raises(ReviewError):
        _check_sandbox_env(["X=ghs_token_value"], "sk-other", tmp_path)
