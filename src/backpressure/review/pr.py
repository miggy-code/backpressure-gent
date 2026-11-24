"""Load a pull request with `gh` and check out its pinned head. Title, body and diff are untrusted data."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# git authenticates through gh; nothing is written to the clone's config.
_GH_AUTH = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


class PullRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    body: str
    url: str
    state: str
    base_sha: str
    head_sha: str
    files: list[str]
    diff: str

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}"


def load(repo: str, number: int) -> PullRequest:
    fields = "number,title,body,url,state,baseRefOid,headRefOid,files"
    data = json.loads(_run(["gh", "pr", "view", str(number), "--repo", repo, "--json", fields], "gh pr view"))
    diff = _run(["gh", "pr", "diff", str(number), "--repo", repo], "gh pr diff")
    return PullRequest(
        repo=repo,
        number=int(data["number"]),
        title=data["title"],
        body=data.get("body") or "",
        url=data["url"],
        state=data["state"],
        base_sha=data["baseRefOid"],
        head_sha=data["headRefOid"],
        files=[f["path"] for f in data.get("files") or []],
        diff=diff,
    )


def checkout(pr: PullRequest, dest: Path) -> None:
    """Clone the repo into `dest` and detach at the PR's head SHA (fork PRs come via pull/N/head)."""
    _run(["git", *_GH_AUTH, "clone", "--quiet", f"{pr.repo_url}.git", str(dest)], "git clone")
    _run(["git", *_GH_AUTH, "-C", str(dest), "fetch", "--quiet", "origin", f"pull/{pr.number}/head", pr.base_sha], "git fetch")
    detach(dest, pr.head_sha)


def detach(dest: Path, sha: str) -> None:
    _run(["git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "--quiet", sha], "git checkout")


def _run(argv: list[str], what: str) -> str:
    env = None
    if argv[0] == "git":
        # PR code can opt into any filter the host's git config defines (git-lfs is set up globally on
        # GitHub runners, and a fork's .lfsconfig picks its server). Ignore global and system config so
        # cloning and checking out never run a filter or contact a host the PR chose.
        env = os.environ | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0:
        raise PullRequestError(f"{what} failed: {proc.stderr.strip()[-1500:]}")
    return proc.stdout
