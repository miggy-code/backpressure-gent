"""Load a pull request with `gh` and check out its pinned head. Title, body and diff are untrusted data."""

from __future__ import annotations

import json
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
    data = json.loads(_run(["gh", "pr", "view", str(number), "--repo", repo, "--json", fields]))
    diff = _run(["gh", "pr", "diff", str(number), "--repo", repo])
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
    _run(["git", *_GH_AUTH, "clone", "--quiet", f"{pr.repo_url}.git", str(dest)])
    _run(["git", *_GH_AUTH, "-C", str(dest), "fetch", "--quiet", "origin", f"pull/{pr.number}/head", pr.base_sha])
    _run(["git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "--quiet", pr.head_sha])


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PullRequestError(f"{argv[0]} {argv[1] if argv[1] != '-c' else ''} failed: {proc.stderr.strip()[-1500:]}")
    return proc.stdout
