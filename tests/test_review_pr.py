"""Checking out PR code on the host must not run filters the PR selects (git-lfs on GitHub runners)."""

import subprocess
from pathlib import Path

import pytest

from backpressure.review.pr import detach


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_checkout_does_not_run_a_filter_the_pr_selects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "smudge-ran"
    # Stand-in for the runner's global git-lfs setup: a configured filter that a repo can opt into.
    smudge = tmp_path / "smudge.sh"
    smudge.write_text(f"#!/bin/sh\ntouch {marker}\nexec cat\n")
    smudge.chmod(0o755)
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f'[filter "lfs"]\n\tclean = cat\n\tsmudge = {smudge}\n\trequired = true\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    (repo / "data.bin").write_text("payload\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "pr")
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", base)
    marker.unlink(missing_ok=True)

    detach(repo, head)

    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert (repo / "data.bin").read_text() == "payload\n"
    assert not marker.exists(), "the PR's .gitattributes made the host run a filter"
