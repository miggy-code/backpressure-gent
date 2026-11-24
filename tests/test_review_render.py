"""The review's Markdown: scorecard first, problems linked to the head SHA, details collapsed."""

from backpressure.report import Finding as GateFinding
from backpressure.report import Severity
from backpressure.review.model import Usage
from backpressure.review.render import RunInfo, to_json, to_markdown
from backpressure.review.schema import Adjudication, Evidence, Review, ReviewFinding, Scorecard, Verdict, Verified

RUN = RunInfo("deepseek-v4-pro", "abc1234def", "https://github.com/o/r", Usage(1000, 200, 800), 5, 9, "0.0123")
GATE = GateFinding("skip_added", Severity.BLOCKING, "tests/test_a.py", "skip", test="test_x", line=3)


def make(**kw) -> Review:  # type: ignore[no-untyped-def]
    base = dict(
        intent="Fix the parser.",
        scorecard=Scorecard("pass", "pass", "pass", "low", "focused"),
        findings=[],
        tamper=[],
        verified=[Verified("tests pass", ["r1"])],
        not_verified=["Windows paths"],
    )
    return Review(**(base | kw))


def test_clean_review_has_scorecard_and_collapsed_details_only() -> None:
    md = to_markdown(make(), Verdict.APPROVE, [], RUN)
    assert md.startswith("### Backpressure review: ✅ Approve")
    assert "| Fit | Checks | Tests | Tamper | Blocking | Non-blocking | Risk | Depth |" in md
    assert "Needs attention" not in md
    assert "- tests pass (`r1`)" in md and "- Windows paths" in md
    assert "$0.0123" in md and "head abc1234" in md


def test_problems_are_listed_and_linked() -> None:
    f = ReviewFinding("blocking", "Untested branch", "Breaking it\nleaves tests green.", "app.py", [Evidence("r4", "x" * 10)], line=12)
    md = to_markdown(make(findings=[f]), Verdict.REQUEST_CHANGES, [GATE], RUN)
    assert "❌ Request changes" in md
    assert "**Untested branch** ([app.py:12](https://github.com/o/r/blob/abc1234def/app.py#L12)): Breaking it leaves tests green. (`r4`)" in md
    assert "**Tamper `skip_added`** `test_x`" in md and "not adjudicated" in md
    assert "❌ 1 open" in md


def test_explained_tamper_is_a_warning_in_details() -> None:
    adj = Adjudication("skip_added", "tests/test_a.py", "test_x", "explained", "platform skip", [Evidence("r2", "y" * 10)])
    md = to_markdown(make(tamper=[adj]), Verdict.APPROVE, [GATE], RUN)
    assert "⚠️ 1 explained" in md and "platform skip (`r2`)" in md


def test_json_carries_verdict_and_usage() -> None:
    import json

    data = json.loads(to_json(make(), Verdict.APPROVE, [GATE], RUN))
    assert data["verdict"] == "approve" and data["run"]["usage"]["cached_tokens"] == 800
    assert data["unresolved_tamper"][0]["code"] == "skip_added"
