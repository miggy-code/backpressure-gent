"""Holdouts: parse, classify, block on regressions, and never show a holdout's name or source."""

import json

from backpressure.review.holdouts import MARK_BASE, MARK_END, MARK_HEAD, HoldoutResult, classify, parse_junit, parse_run
from backpressure.review.model import Usage
from backpressure.review.render import RunInfo, to_json, to_markdown
from backpressure.review.schema import Review, Scorecard, Verdict, Verified, verdict

SECRET_NAME = "test_discount_never_exceeds_cap_for_vip_orders"


def junit(*cases: tuple[str, str]) -> str:
    body = ""
    for name, outcome in cases:
        inner = {"pass": "", "fail": "<failure message='x'/>", "error": "<error message='x'/>", "skip": "<skipped/>"}[outcome]
        body += f"<testcase classname='holdouts.test_shop' name='{name}'>{inner}</testcase>"
    return f"<testsuites><testsuite name='pytest'>{body}</testsuite></testsuites>"


def test_parse_junit_maps_outcomes() -> None:
    got = parse_junit(junit(("a", "pass"), ("b", "fail"), ("c", "error"), ("d", "skip")))
    assert got == {"holdouts.test_shop::a": True, "holdouts.test_shop::b": False, "holdouts.test_shop::c": False,
                   "holdouts.test_shop::d": True}  # fmt: skip
    assert parse_junit("") is None and parse_junit("<not xml") is None


def test_classify_regressed_fixed_still_failing_and_vanished() -> None:
    base = {"a": True, "b": True, "c": False, "d": False, "gone": True}
    head = {"a": True, "b": False, "c": True, "d": False}
    r = classify(base, head)
    assert (r.passing, r.regressed, r.fixed, r.still_failing) == (1, 2, 1, 1)
    assert r.detail["gone"] == "regressed"


def test_parse_run_reads_both_reports_and_flags_a_missing_head() -> None:
    out = f"noise\n{MARK_BASE}\n{junit(('a', 'pass'))}\n{MARK_HEAD}\n{junit(('a', 'fail'))}\n{MARK_END}\n"
    assert parse_run(out).regressed == 1
    missing = parse_run(f"{MARK_BASE}\n{junit(('a', 'pass'))}\n{MARK_HEAD}\n\n{MARK_END}\n")
    assert not missing.ran and "no report at head" in missing.note
    assert not parse_run("container died").ran


def review() -> Review:
    return Review("Add a cap.", Scorecard("pass", "pass", "pass", "low", "focused"), [], [], [Verified("tests pass", ["r1"])], [])


RUN = RunInfo("m", "a" * 40, "https://github.com/o/r", Usage(), 1, 1)
REGRESSED = HoldoutResult(ran=True, regressed=1, passing=3, detail={f"holdouts.test_shop::{SECRET_NAME}": "regressed"})


def test_a_regressed_holdout_requests_changes_even_on_a_clean_review() -> None:
    assert verdict(review(), [], REGRESSED) is Verdict.REQUEST_CHANGES
    assert verdict(review(), [], HoldoutResult(ran=True, passing=4)) is Verdict.APPROVE
    assert verdict(review(), [], HoldoutResult(ran=True, still_failing=2)) is Verdict.APPROVE
    assert verdict(review(), [], None) is Verdict.APPROVE


def test_scorecard_shows_holdouts_by_count() -> None:
    md = to_markdown(review(), Verdict.REQUEST_CHANGES, [], RUN, holdouts=REGRESSED)
    assert "| Holdouts |" in md and "❌ 1 regressed" in md
    assert "1 holdout scenario that passes at the merge base fails at head" in md
    assert "✅ 4 pass" in to_markdown(review(), Verdict.APPROVE, [], RUN, holdouts=HoldoutResult(ran=True, passing=4))
    assert "⚠️ not run" in to_markdown(review(), Verdict.APPROVE, [], RUN, holdouts=HoldoutResult(ran=False, note="x"))
    assert "✅ none" in to_markdown(review(), Verdict.APPROVE, [], RUN)


def test_rendered_review_never_contains_holdout_names() -> None:
    md = to_markdown(review(), Verdict.REQUEST_CHANGES, [], RUN, holdouts=REGRESSED)
    data = to_json(review(), Verdict.REQUEST_CHANGES, [], RUN, holdouts=REGRESSED)
    for text in (md, data, REGRESSED.summary(), json.dumps(REGRESSED.counts())):
        assert SECRET_NAME not in text and "test_shop" not in text
    assert json.loads(data)["holdouts"]["regressed"] == 1
