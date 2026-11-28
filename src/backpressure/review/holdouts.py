"""Holdout scenarios: tests kept outside the target repo, run by code against the merge base and the head.

The coding agent never sees them, and neither does the reviewer model. Reviews show counts only: a
name or source line in a public review would leak the holdout back to the agent.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from backpressure.review.sandbox import HOLDOUT_VENV, RunResult, SandboxError

PER_RUN_TIMEOUT = 300
MARK_BASE, MARK_HEAD, MARK_END = "===BACKPRESSURE-HOLDOUTS-BASE===", "===BACKPRESSURE-HOLDOUTS-HEAD===", "===BACKPRESSURE-HOLDOUTS-END==="
MARK_DEPS = "===BACKPRESSURE-HOLDOUTS-DEPS==="
DEPS_CHANGED = (
    "the PR changes dependency manifests but holdouts run with the merge base's dependencies, "
    "so a new dependency shows up as a failure rather than a pass"
)
# Files that decide what the install pulls in; compared between the merge base and head.
DEPENDENCY_FILES = "pyproject.toml uv.lock setup.py setup.cfg requirements*.txt"


class IsolatedRunner(Protocol):
    def run_clean(self, command: str, mounts: dict[Path, str], trees: dict[str, str], timeout: int = 600) -> RunResult: ...

    def build_holdout_venv(self, ref: str) -> str: ...


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
        text = (
            f"Holdout scenarios: {self.total} run at the merge base and at head; {self.regressed} passed at the merge base "
            f"and fail at head; {self.still_failing} fail at both; {self.fixed} newly pass."
        )
        return f"{text} Note: {self.note}." if self.note else text


def holdout_dir(root: Path, repo: str) -> Path | None:
    """Holdouts for OWNER/REPO live at <root>/<owner>/<repo>/; None if there are none."""
    d = root / repo
    return d if d.is_dir() and any(d.rglob("test_*.py")) else None


def holdout_script() -> str:
    """The shell script for the holdouts container. Pure, so tests can check how pytest is launched.

    The container is the bare base image with the merge base at /base and the head at /head (git
    archives, so no .git and no git needed), the clean venv at HOLDOUT_VENV, and nothing the PR's
    install touched. Interpreter: that venv, never /work/.venv. `-I` keeps the cwd and PYTHON* variables
    off sys.path, and the code under test joins sys.path through pytest's `pythonpath` ini option after
    start-up, so a `.pth`, `sitecustomize.py` or `usercustomize.py` in the PR tree never runs. Plugin
    autoloading is off (an entry-point plugin could pass everything) and pytest runs from /holdouts with
    that as its rootdir, so the PR's pytest config and conftest files are not read. `src` covers src layouts.
    """

    def pytest(tree: str) -> str:
        return (
            f"(cd /holdouts && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 timeout {PER_RUN_TIMEOUT} {HOLDOUT_VENV}/bin/python -I -B -m pytest "
            f"/holdouts -q -p no:cacheprovider --rootdir=/holdouts -o pythonpath='/{tree} /{tree}/src' "
            f"--junitxml=/tmp/{tree}.xml >/dev/null 2>&1)"
        )

    return "\n".join([
        pytest("base"),
        pytest("head"),
        f"echo {MARK_DEPS}",
        f"files=$( (cd /base && ls {DEPENDENCY_FILES}; cd /head && ls {DEPENDENCY_FILES}) 2>/dev/null | sort -u )",
        "state=same; for f in $files; do cmp -s /base/$f /head/$f || state=changed; done; echo $state",
        f"echo {MARK_BASE}; cat /tmp/base.xml 2>/dev/null",
        f"echo {MARK_HEAD}; cat /tmp/head.xml 2>/dev/null",
        f"echo {MARK_END}",
    ])  # fmt: skip


def run_holdouts(runner: IsolatedRunner, holdouts: Path, base_sha: str, head_sha: str) -> HoldoutResult:
    try:
        runner.build_holdout_venv(base_sha)
        result = runner.run_clean(
            holdout_script(), {holdouts: "/holdouts"}, {"base": base_sha, "head": head_sha}, timeout=2 * PER_RUN_TIMEOUT + 60
        )
    except SandboxError:
        # Never fall back to the PR's venv or image: that is the thing a PR can rig.
        return HoldoutResult(ran=False, note="the clean holdout environment could not be built from the merge base")
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
    result = classify(parse_junit(base_xml) or {}, head)
    deps = output.split(MARK_DEPS, 1)[1].split(MARK_BASE, 1)[0].strip() if MARK_DEPS in output else ""
    return replace(result, note=DEPS_CHANGED) if deps == "changed" else result


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
