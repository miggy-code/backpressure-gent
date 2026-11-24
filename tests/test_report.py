from backpressure.report import Finding, Report, Severity

BASE, HEAD = "b" * 40, "h" * 40


def test_removed_tests_link_to_the_merge_base_and_others_to_head():
    report = Report(
        BASE,
        HEAD,
        [
            Finding("test_removed", Severity.BLOCKING, "t.py", "gone", "test_a", 5, at_base=True),
            Finding("skip_added", Severity.BLOCKING, "t.py", "skipped", "test_b", 9),
        ],
    )
    md = report.to_markdown("https://github.com/o/r")
    assert f"https://github.com/o/r/blob/{BASE}/t.py#L5" in md
    assert f"https://github.com/o/r/blob/{HEAD}/t.py#L9" in md


def test_clean_report_shows_pass_and_no_attention_list():
    md = Report(BASE, HEAD).to_markdown()
    assert "✅ Backpressure tamper gate: Pass" in md
    assert "Needs attention" not in md
