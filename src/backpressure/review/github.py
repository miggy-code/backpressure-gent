"""Everything that writes to GitHub: who may start a review, the status comment, the review and its label.

This runs in workflow jobs that hold a write token and never run PR code or see the model key.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from backpressure.review.render import clean
from backpressure.review.schema import Verdict

TRIGGER = "backpressure-review"
VERDICT_LABELS = {
    Verdict.APPROVE: ("backpressure-approved", "0e8a16"),
    Verdict.REQUEST_CHANGES: ("backpressure-request-changes", "d93f0b"),
    Verdict.CLOSE: ("backpressure-close", "b60205"),
}
WRITERS = ("admin", "maintain", "write")


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHubApi(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any: ...


class GhCli:
    """GitHub REST through `gh api`, authenticated by GH_TOKEN in the job."""

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        argv = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
        if body is not None:
            argv += ["--input", "-"]
        proc = subprocess.run(argv, input=json.dumps(body) if body is not None else None, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            m = re.search(r"HTTP (\d{3})", proc.stderr)
            raise ApiError(int(m[1]) if m else 0, proc.stderr.strip()[-500:])
        return json.loads(proc.stdout) if proc.stdout.strip() else None


@dataclass(frozen=True)
class Start:
    allowed: bool
    reason: str
    head_sha: str = ""
    comment_id: int = 0


def start(api: GitHubApi, repo: str, number: int, requester: str, run_url: str, allow_forks: bool = False) -> Start:
    """Check who asked and what for. Refusals are explained on the PR and the trigger label is removed."""
    pr = api.request("GET", f"repos/{repo}/pulls/{number}")
    head_sha = pr["head"]["sha"]
    reason = _refusal(api, repo, pr, requester, allow_forks)
    if reason:
        api.request("POST", f"repos/{repo}/issues/{number}/comments", {"body": f"Backpressure review not started: {reason}"})
        remove_trigger(api, repo, number)
        return Start(False, reason, head_sha)
    body = f"Backpressure review started on `{head_sha[:7]}`. [Run]({run_url})"
    comment = api.request("POST", f"repos/{repo}/issues/{number}/comments", {"body": body})
    return Start(True, "", head_sha, int(comment["id"]))


def _refusal(api: GitHubApi, repo: str, pr: dict[str, Any], requester: str, allow_forks: bool) -> str:
    try:
        permission = api.request("GET", f"repos/{repo}/collaborators/{requester}/permission")
        level = permission.get("role_name") or permission.get("permission")
    except ApiError as exc:
        if exc.status != 404:
            raise
        level = "none"
    if level not in WRITERS:
        return f"the label was added by `{requester}`, who does not have write access."
    if pr["state"] != "open":
        return "the pull request is not open."
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
    if head_repo != repo and not allow_forks:
        return "reviews of pull requests from forks are not enabled for this repository."
    return ""


def finish(api: GitHubApi, repo: str, number: int, out: Path | None, comment_id: int, head_sha: str, run_url: str) -> Verdict | None:
    """Post one COMMENT review and exactly one verdict label. The trigger label is always removed."""
    try:
        review = _load(out)
        if review is None:
            md = out / "review.md" if out else None
            note = md.read_text() if md and md.exists() else "The review job failed before writing a review."
            _status(api, repo, comment_id, f"Backpressure review did not finish. [Run]({run_url})\n\n{note}")
            return None
        if review["head_sha"] != head_sha:
            _status(api, repo, comment_id, f"Backpressure review skipped: the review is for a different head. [Run]({run_url})")
            return None
        verdict = Verdict(review["verdict"])
        assert out is not None
        posted = _post_review(api, repo, number, head_sha, (out / "review.md").read_text(), _inline(review))
        _set_label(api, repo, number, verdict)
        word = verdict.value.replace("_", " ")
        _status(api, repo, comment_id, f"Backpressure review finished: **{word}**. [Review]({posted['html_url']})")
        return verdict
    finally:
        remove_trigger(api, repo, number)


def remove_trigger(api: GitHubApi, repo: str, number: int) -> None:
    try:
        api.request("DELETE", f"repos/{repo}/issues/{number}/labels/{TRIGGER}")
    except ApiError as exc:
        if exc.status != 404:
            raise


def _load(out: Path | None) -> dict[str, Any] | None:
    if out is None or not (out / "review.json").exists() or not (out / "review.md").exists():
        return None
    return json.loads((out / "review.json").read_text())


def _inline(review: dict[str, Any]) -> list[dict[str, Any]]:
    comments = []
    for f in review["review"]["findings"]:
        if f.get("line") and not f.get("outside_diff"):
            kind = "Blocking" if f["severity"] == "blocking" else "Non-blocking"
            body = f"**{clean(f['title'])}** ({kind})\n\n{clean(f['body'])}"
            comments.append({"path": f["path"], "line": f["line"], "side": "RIGHT", "body": body})
    return comments


def _post_review(api: GitHubApi, repo: str, number: int, head_sha: str, body: str, comments: list[dict[str, Any]]) -> Any:
    payload: dict[str, Any] = {"commit_id": head_sha, "event": "COMMENT", "body": body}
    try:
        return api.request("POST", f"repos/{repo}/pulls/{number}/reviews", payload | ({"comments": comments} if comments else {}))
    except ApiError as exc:
        # A line GitHub cannot place (outside the diff on the right side) rejects the whole review; the
        # findings are in the body too, so post without the inline comments rather than not at all.
        if exc.status != 422 or not comments:
            raise
        return api.request("POST", f"repos/{repo}/pulls/{number}/reviews", payload)


def _set_label(api: GitHubApi, repo: str, number: int, verdict: Verdict) -> None:
    name, color = VERDICT_LABELS[verdict]
    try:
        api.request("POST", f"repos/{repo}/labels", {"name": name, "color": color})
    except ApiError as exc:
        if exc.status != 422:  # 422: already exists
            raise
    for other, _ in VERDICT_LABELS.values():
        if other != name:
            try:
                api.request("DELETE", f"repos/{repo}/issues/{number}/labels/{other}")
            except ApiError as exc:
                if exc.status != 404:
                    raise
    api.request("POST", f"repos/{repo}/issues/{number}/labels", {"labels": [name]})


def _status(api: GitHubApi, repo: str, comment_id: int, body: str) -> None:
    if comment_id:
        api.request("PATCH", f"repos/{repo}/issues/comments/{comment_id}", {"body": body})
