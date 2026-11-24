"""Holdout scenarios: tests kept outside the target repo, run by code against the merge base and the head.

The coding agent never sees them, and neither does the reviewer model. Reviews show counts only: a
name or source line in a public review would leak the holdout back to the agent.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from backpressure.review.sandbox import RunResult

PER_RUN_TIMEOUT = 300
MARK_BASE, MARK_HEAD, MARK_END = "===BACKPRESSURE-HOLDOUTS-BASE===", "===BACKPRESSURE-HOLDOUTS-HEAD===", "===BACKPRESSURE-HOLDOUTS-END==="


class IsolatedRunner(Protocol):
    def run_isolated(self, command: str, mounts: dict[Path, str], timeout: int = 600) -> RunResult: ...


@dataclass(frozen=True)
class HoldoutResult:
    ran: bool
    regressed: int = 0  # passed at the merge base, fails at head: blocks the PR
    fixed: int = 0
    still_failing: int = 0
    passing: int = 0
    note: str = ""
    # Test name -> class. Written to the local output folder only; never rendered, posted or logged.
    detail: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def total(self) -> int:
        return self.regressed + self.fixed + self.still_failing + self.passing

    def counts(self) -> dict[str, int | bool | str]:
        """Everything that may be shown publicly."""
        return {"ran": self.ran, "total": self.total, "regressed": self.regressed, "fixed": self.fixed,
                "still_failing": self.still_failing, "passing": self.passing, "note": self.note}  # fmt: skip

    def summary(self) -> str:
        if not self.ran:
            return f"Holdout scenarios did not run: {self.note}"
        return (
            f"Holdout scenarios: {self.total} run at the merge base and at head; {self.regressed} passed at the merge base "
            f"and fail at head; {self.still_failing} fail at both; {self.fixed} newly pass."
        )


def holdout_dir(root: Path, repo: str) -> Path | None:
    """Holdouts for OWNER/REPO live at <root>/<owner>/<repo>/; None if there are none."""
    d = root / repo
    return d if d.is_dir() and any(d.rglob("test_*.py")) else None


def run_holdouts(runner: IsolatedRunner, holdouts: Path, base_sha: str, head_sha: str) -> HoldoutResult:
    # The PR's code shares this interpreter, so close the cheap ways for it to rig the run: no plugin
    # autoloading (a PR could register one that passes everything), and /work joins sys.path through
    # pytest after startup rather than PYTHONPATH (a sitecustomize.py there would run at startup).
    pytest = (
        f"PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 timeout {PER_RUN_TIMEOUT} python -m pytest /holdouts -q "
        "-p no:cacheprovider --rootdir=/holdouts -o pythonpath=/work"
    )
    script = "\n".join([
        "cd /work",
        f"git checkout -q --force {base_sha} && {pytest} --junitxml=/tmp/base.xml >/dev/null 2>&1",
        f"git checkout -q --force {head_sha} && {pytest} --junitxml=/tmp/head.xml >/dev/null 2>&1",
        f"echo {MARK_BASE}; cat /tmp/base.xml 2>/dev/null",
        f"echo {MARK_HEAD}; cat /tmp/head.xml 2>/dev/null",
        f"echo {MARK_END}",
    ])  # fmt: skip
    result = runner.run_isolated(script, {holdouts: "/holdouts"}, timeout=2 * PER_RUN_TIMEOUT + 60)
    return parse_run(result.output)


def parse_run(output: str) -> HoldoutResult:
    try:
        base_xml = output.split(MARK_BASE, 1)[1].split(MARK_HEAD, 1)[0]
        head_xml = output.split(MARK_HEAD, 1)[1].split(MARK_END, 1)[0]
    except IndexError:
        return HoldoutResult(ran=False, note="the holdout run did not finish")
    head = parse_junit(head_xml)
    if head is None:
        return HoldoutResult(ran=False, note="pytest produced no report at head")
    return classify(parse_junit(base_xml) or {}, head)


def parse_junit(text: str) -> dict[str, bool] | None:
    """Test id -> passed. Skips count as passing; errors (including collection errors) as failing."""
    text = text.strip()
    if not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    outcomes: dict[str, bool] = {}
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        failed = any(child.tag in ("failure", "error") for child in case)
        outcomes[name] = outcomes.get(name, True) and not failed
    return outcomes


def classify(base: dict[str, bool], head: dict[str, bool]) -> HoldoutResult:
    detail: dict[str, str] = {}
    for name in sorted(base.keys() | head.keys()):
        at_base, at_head = base.get(name), head.get(name)
        if at_head:
            detail[name] = "passing" if at_base else "fixed"
        elif at_base:
            detail[name] = "regressed"  # includes a test that disappeared at head (its module stopped importing)
        else:
            detail[name] = "still_failing"
    values = list(detail.values())
    return HoldoutResult(
        ran=True,
        regressed=values.count("regressed"),
        fixed=values.count("fixed"),
        still_failing=values.count("still_failing"),
        passing=values.count("passing"),
        detail=detail,
    )


def write_private(result: HoldoutResult, out: Path) -> None:
    """Names stay on the machine that ran the review; the workflow deletes this file before upload."""
    (out / "holdouts.json").write_text(json.dumps({**result.counts(), "detail": result.detail}, indent=2) + "\n")
