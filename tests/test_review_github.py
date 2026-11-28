"""Posting to GitHub with an in-memory API: who may start, one review, one label, trigger label always removed."""

import json
from pathlib import Path
from typing import Any

import pytest

from backpressure.review.github import TRIGGER, ApiError, finish, start
from backpressure.review.render import clean

REPO = "o/r"
HEAD = "a" * 40


class FakeApi:
    def __init__(self, permission: str = "write", state: str = "open", head_repo: str = REPO) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.permission = permission
        self.pr = {"state": state, "draft": False, "head": {"sha": HEAD, "repo": {"full_name": head_repo}}}
        self.fail: dict[tuple[str, str], ApiError] = {}

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, body))
        if err := self.fail.pop((method, path), None):
            raise err
        if path.endswith("/permission"):
            if self.permission == "missing":
                raise ApiError(404, "Not Found")
            return {"permission": self.permission, "role_name": self.permission}
        if method == "GET" and "/pulls/" in path:
            return self.pr
        if path.endswith("/comments") and method == "POST":
            return {"id": 77}
        if path.endswith("/reviews"):
            return {"html_url": "https://github.com/o/r/pull/1#pullrequestreview-1"}
        return {}

    def made(self, method: str, suffix: str) -> list[Any]:
        return [body for m, p, body in self.calls if m == method and p.endswith(suffix)]


def test_writer_starts_a_review_with_a_status_comment() -> None:
    api = FakeApi()
    started = start(api, REPO, 1, "miggy-code", "https://run")
    assert (started.allowed, started.head_sha, started.comment_id) == (True, HEAD, 77)
    assert "started on `aaaaaaa`" in api.made("POST", "/issues/1/comments")[0]["body"]
    assert not api.made("DELETE", f"/labels/{TRIGGER}")


@pytest.mark.parametrize(
    ("api", "reason"),
    [
        (FakeApi(permission="read"), "does not have write access"),
        (FakeApi(permission="triage"), "does not have write access"),
        (FakeApi(permission="missing"), "does not have write access"),
        (FakeApi(state="closed"), "not open"),
        (FakeApi(head_repo="someone/fork"), "forks are not enabled"),
    ],
)
def test_refusals_are_explained_and_the_label_removed(api: FakeApi, reason: str) -> None:
    started = start(api, REPO, 1, "stranger", "https://run")
    assert not started.allowed and reason in started.reason
    assert reason in api.made("POST", "/issues/1/comments")[0]["body"]
    assert api.made("DELETE", f"/issues/1/labels/{TRIGGER}")


def test_forks_run_when_enabled() -> None:
    assert start(FakeApi(head_repo="someone/fork"), REPO, 1, "miggy-code", "https://run", allow_forks=True).allowed


@pytest.mark.parametrize(
    ("api", "head", "draft"),
    [
        (FakeApi(), "b" * 40, False),
        (FakeApi(head_repo="someone/fork"), HEAD, False),
        (FakeApi(), HEAD, True),
        (FakeApi(state="closed"), HEAD, False),
        (FakeApi(permission="read"), HEAD, False),
    ],
)
def test_automatic_start_only_accepts_a_current_ready_same_repo_pr_from_a_writer(api: FakeApi, head: str, draft: bool) -> None:
    api.pr["head"]["sha"] = head
    api.pr["draft"] = draft
    assert not start(api, REPO, 1, "contributor", "https://run", allow_forks=True, expected_head=HEAD).allowed
    assert all(method == "GET" for method, _, _ in api.calls)


def write_review(out: Path, verdict: str = "request_changes", head: str = HEAD, line: int | None = 12) -> Path:
    finding = {"severity": "blocking", "title": "Untested @team <b>x</b>", "body": "Breaks.", "path": "app.py", "line": line,
               "outside_diff": False, "evidence": []}  # fmt: skip
    data = {"verdict": verdict, "head_sha": head, "review": {"findings": [finding]}}
    out.mkdir(exist_ok=True)
    (out / "review.json").write_text(json.dumps(data))
    (out / "review.md").write_text("### Backpressure review\n")
    return out


def test_posts_one_review_and_exactly_one_label(tmp_path: Path) -> None:
    api = FakeApi()
    verdict = finish(api, REPO, 1, write_review(tmp_path / "out"), 77, HEAD, "https://run")
    assert verdict is not None and verdict.value == "request_changes"
    (review,) = api.made("POST", "/pulls/1/reviews")
    assert (review["commit_id"], review["event"], review["body"]) == (HEAD, "COMMENT", "### Backpressure review\n")
    (inline,) = review["comments"]
    assert inline["path"] == "app.py" and inline["line"] == 12 and "@​team &lt;b>" in inline["body"]
    assert api.made("POST", "/issues/1/labels") == [{"labels": ["backpressure-request-changes"]}]
    deleted = {p.rsplit("/", 1)[1] for m, p, _ in api.calls if m == "DELETE"}
    assert deleted == {"backpressure-approved", "backpressure-close", TRIGGER}
    assert "request changes" in api.made("PATCH", "/issues/comments/77")[0]["body"]


def test_automatic_review_posts_for_the_current_head_without_changing_labels(tmp_path: Path) -> None:
    api = FakeApi()
    started = start(api, REPO, 1, "contributor", "https://run", expected_head=HEAD)
    assert started.allowed
    verdict = finish(api, REPO, 1, write_review(tmp_path), started.comment_id, HEAD, "https://run", automatic=True)
    assert verdict is not None and verdict.value == "request_changes"
    (review,) = api.made("POST", "/pulls/1/reviews")
    assert review["commit_id"] == HEAD and review["event"] == "COMMENT"
    assert all("/labels" not in path for _, path, _ in api.calls)


def test_automatic_review_of_a_superseded_head_is_skipped(tmp_path: Path) -> None:
    api = FakeApi()
    api.pr["head"]["sha"] = "b" * 40
    assert finish(api, REPO, 1, write_review(tmp_path), 77, HEAD, "https://run", automatic=True) is None
    assert not api.made("POST", "/pulls/1/reviews")
    assert "head changed" in api.made("PATCH", "/issues/comments/77")[0]["body"]


def test_inline_comment_github_rejects_falls_back_to_the_body_only(tmp_path: Path) -> None:
    api = FakeApi()
    api.fail[("POST", f"repos/{REPO}/pulls/1/reviews")] = ApiError(422, "line could not be resolved")
    finish(api, REPO, 1, write_review(tmp_path / "out"), 77, HEAD, "https://run")
    first, second = api.made("POST", "/pulls/1/reviews")
    assert "comments" in first and "comments" not in second


def test_failed_review_job_updates_status_and_removes_the_trigger(tmp_path: Path) -> None:
    api = FakeApi()
    assert finish(api, REPO, 1, None, 77, HEAD, "https://run") is None
    assert not api.made("POST", "/pulls/1/reviews") and not api.made("POST", "/issues/1/labels")
    assert "did not finish" in api.made("PATCH", "/issues/comments/77")[0]["body"]
    assert api.made("DELETE", f"/labels/{TRIGGER}")


def test_trigger_label_is_removed_even_when_posting_fails(tmp_path: Path) -> None:
    api = FakeApi()
    api.fail[("POST", f"repos/{REPO}/pulls/1/reviews")] = ApiError(500, "boom")
    with pytest.raises(ApiError):
        finish(api, REPO, 1, write_review(tmp_path / "out", line=None), 77, HEAD, "https://run")
    assert api.made("DELETE", f"/labels/{TRIGGER}")


def test_review_for_another_head_is_not_posted(tmp_path: Path) -> None:
    api = FakeApi()
    assert finish(api, REPO, 1, write_review(tmp_path / "out", head="b" * 40), 77, HEAD, "https://run") is None
    assert not api.made("POST", "/pulls/1/reviews")


def test_clean_neutralises_mentions_and_html() -> None:
    assert clean("ping @octocat <script>x</script> a@b.c") == "ping @​octocat &lt;script>x&lt;/script> a@​b.c"


def test_clean_leaves_code_spans_as_written() -> None:
    # GitHub shows entities literally inside code spans, and neither renders HTML nor notifies mentions there.
    text = "Replaced `if len(x) > 50 and y < 3:` with ``a`b @me`` then <b>@you</b>"
    assert clean(text) == "Replaced `if len(x) > 50 and y < 3:` with ``a`b @me`` then &lt;b>@​you&lt;/b>"


def test_failure_note_from_the_review_job_is_escaped(tmp_path: Path) -> None:
    api = FakeApi()
    out = tmp_path / "out"
    out.mkdir()
    (out / "review.md").write_text("### no review (invalid)\n- findings[0].path '@everyone' is not a file changed\n")
    finish(api, REPO, 1, out, 77, HEAD, "https://run")
    assert "@\u200beveryone" in api.made("PATCH", "/issues/comments/77")[0]["body"]
