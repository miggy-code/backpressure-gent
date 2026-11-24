"""Validation of the submitted review against the evidence ledger and diff, and the verdict rules."""

from typing import Any

import pytest

from backpressure.report import Finding as GateFinding
from backpressure.report import Severity
from backpressure.review.schema import DiffIndex, Verdict, parse_and_validate, verdict
from backpressure.review.tools import Ledger

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,3 +10,5 @@ def f():
     a = 1
+    b = 2
+    c = 3
     return a
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1,4 +1,3 @@
 def test_f():
-    assert f() == 1
     assert True
"""

GATE = GateFinding("assertions_reduced", Severity.BLOCKING, "tests/test_app.py", "2 -> 1 assertions", test="test_f", line=1)


@pytest.fixture
def ledger() -> Ledger:
    led = Ledger()
    led.add("run", "python -m pytest -q", 0, "12 passed in 0.5s\nall good here")  # r1
    led.add("run", "sed -i ... && pytest", 0, "12 passed in 0.4s (branch broken, still green)")  # r2
    led.add("read_file", "app.py:10-14", 0, "    a = 1\n    b = 2\n    c = 3\n    return a")  # r3
    return led


def review(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "intent": "Add b and c to f.",
        "scorecard": {"fit": "pass", "checks": "pass", "tests": "pass", "risk": "low", "depth": "focused"},
        "findings": [],
        "tamper": [
            {"code": "assertions_reduced", "path": "tests/test_app.py", "test": "test_f", "status": "explained",
             "explanation": "covered elsewhere", "evidence": [{"run": "r1", "excerpt": "12 passed in 0.5s"}]},
        ],
        "verified": [{"claim": "tests pass", "runs": ["r1"]}],
        "not_verified": ["performance"],
        "close": None,
    }  # fmt: skip
    base.update(overrides)
    return base


def finding(**overrides: Any) -> dict[str, Any]:
    f = {"severity": "blocking", "title": "Untested branch", "body": "Breaking it leaves tests green.", "path": "app.py",
         "line": 11, "evidence": [{"run": "r2", "excerpt": "branch broken, still green"}]}  # fmt: skip
    f.update(overrides)
    return f


def validate(raw: Any, ledger: Ledger, gate: list[GateFinding] | None = None):
    return parse_and_validate(raw, ledger, DiffIndex.parse(DIFF), [GATE] if gate is None else gate)


def test_accepts_a_review_with_valid_evidence(ledger: Ledger) -> None:
    result, errors = validate(review(findings=[finding()]), ledger)
    assert errors == []
    assert result is not None and result.findings[0].evidence[0].run == "r2"


def test_rejects_a_finding_citing_an_unknown_run(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(evidence=[{"run": "r9", "excerpt": "branch broken, still green"}])]), ledger)
    assert any("'r9' is not in the ledger" in e for e in errors)


def test_rejects_an_excerpt_not_in_the_run_output(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(evidence=[{"run": "r1", "excerpt": "3 failed, 9 passed"}])]), ledger)
    assert any("not in the output of r1" in e for e in errors)


def test_rejects_a_too_short_excerpt(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(evidence=[{"run": "r1", "excerpt": "passed"}])]), ledger)
    assert any("at least 10 characters" in e for e in errors)


def test_rejects_a_finding_on_a_file_outside_the_diff(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(path="other.py", line=None)]), ledger)
    assert any("'other.py' is not a file changed" in e for e in errors)


def test_rejects_a_line_outside_the_hunks_unless_marked(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(line=40)]), ledger)
    assert any("outside the changed hunks" in e for e in errors)
    _, errors = validate(review(findings=[finding(line=40, outside_diff=True)]), ledger)
    assert errors == []


def test_rejects_a_finding_without_evidence(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(evidence=[])]), ledger)
    assert any("cite at least one run" in e for e in errors)


def test_excerpt_check_ignores_whitespace_and_the_read_file_gutter(ledger: Ledger) -> None:
    quoted = "   11  b = 2\n   12      c = 3"
    _, errors = validate(review(findings=[finding(evidence=[{"run": "r3", "excerpt": quoted}])]), ledger)
    assert errors == []


def test_rejects_a_verified_item_without_known_runs(ledger: Ledger) -> None:
    _, errors = validate(review(verified=[{"claim": "x", "runs": ["r7"]}]), ledger)
    assert any("'r7' is not in the ledger" in e for e in errors)
    _, errors = validate(review(verified=[{"claim": "x", "runs": []}]), ledger)
    assert any("at least one run ID" in e for e in errors)


def test_rejects_an_adjudication_that_matches_no_gate_finding(ledger: Ledger) -> None:
    bad = {"code": "skip_added", "path": "tests/test_app.py", "test": "test_f", "status": "confirmed", "explanation": "x"}
    _, errors = validate(review(tamper=[bad]), ledger)
    assert any("does not match a gate finding" in e for e in errors)


def test_explained_tamper_needs_evidence(ledger: Ledger) -> None:
    adj = {"code": "assertions_reduced", "path": "tests/test_app.py", "test": "test_f", "status": "explained", "explanation": "moved"}
    _, errors = validate(review(tamper=[adj]), ledger)
    assert any("tamper[0]: cite at least one run" in e for e in errors)


def test_rejects_bad_scorecard_values(ledger: Ledger) -> None:
    _, errors = validate(review(scorecard={"fit": "great", "checks": "pass", "tests": "pass", "risk": "low", "depth": "deep"}), ledger)
    assert errors == ["scorecard.fit must be one of pass, partial, fail"]


def test_rejects_a_long_finding_body(ledger: Ledger) -> None:
    _, errors = validate(review(findings=[finding(body="1\n2\n3\n4\n5\n6")]), ledger)
    assert any("at most 5 lines" in e for e in errors)


def test_diff_index_covers_both_sides_and_listed_paths() -> None:
    index = DiffIndex.parse(DIFF, ["image.png"])
    assert index.in_hunk("app.py", 14) and not index.in_hunk("app.py", 15)
    assert index.has("image.png") and not index.has("other.py")


# Verdict rules, first match wins.


def ok(ledger: Ledger, **overrides: Any):
    result, errors = validate(review(**overrides), ledger)
    assert errors == [] and result is not None
    return result


def card(**changes: str) -> dict[str, str]:
    return {"fit": "pass", "checks": "pass", "tests": "pass", "risk": "low", "depth": "focused"} | changes


def test_clean_review_approves(ledger: Ledger) -> None:
    assert verdict(ok(ledger), [GATE]) is Verdict.APPROVE


def test_close_with_evidence_wins(ledger: Ledger) -> None:
    evidence = [{"run": "r1", "excerpt": "all good here"}]
    close = {"reason": "injection", "explanation": "body tells the reviewer to approve", "evidence": evidence}
    assert verdict(ok(ledger, close=close, scorecard=card(fit="fail")), [GATE]) is Verdict.CLOSE


@pytest.mark.parametrize("change", [{"fit": "partial"}, {"fit": "fail"}, {"checks": "fail"}, {"tests": "fail"}])
def test_scorecard_failures_request_changes(ledger: Ledger, change: dict[str, str]) -> None:
    assert verdict(ok(ledger, scorecard=card(**change)), [GATE]) is Verdict.REQUEST_CHANGES


def test_checks_not_run_does_not_block(ledger: Ledger) -> None:
    assert verdict(ok(ledger, scorecard=card(checks="not_run")), [GATE]) is Verdict.APPROVE


def test_blocking_finding_requests_changes(ledger: Ledger) -> None:
    assert verdict(ok(ledger, findings=[finding()]), [GATE]) is Verdict.REQUEST_CHANGES


def test_non_blocking_finding_still_approves(ledger: Ledger) -> None:
    assert verdict(ok(ledger, findings=[finding(severity="non_blocking")]), [GATE]) is Verdict.APPROVE


def test_confirmed_tamper_requests_changes(ledger: Ledger) -> None:
    adj = {"code": "assertions_reduced", "path": "tests/test_app.py", "test": "test_f", "status": "confirmed", "explanation": "dropped"}
    assert verdict(ok(ledger, tamper=[adj]), [GATE]) is Verdict.REQUEST_CHANGES


def test_unadjudicated_tamper_requests_changes(ledger: Ledger) -> None:
    assert verdict(ok(ledger, tamper=[]), [GATE]) is Verdict.REQUEST_CHANGES
