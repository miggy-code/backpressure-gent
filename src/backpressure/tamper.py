"""Detect PR changes that make Python tests easier to pass.

Pure: callers pass file contents at the merge base and at head; nothing here
touches git, the network, or executes the code it reads.
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from backpressure.report import Finding, Severity

CONFIG_FILES = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "noxfile.py", ".coveragerc"}
SKIP_DECORATORS = {
    "pytest.mark.skip",
    "pytest.mark.skipif",
    "pytest.mark.xfail",
    "mark.skip",
    "mark.skipif",
    "mark.xfail",
    "unittest.skip",
    "unittest.skipIf",
    "unittest.skipUnless",
    "unittest.expectedFailure",
    "skip",
    "skipIf",
    "skipUnless",
    "expectedFailure",
}
SKIP_CALLS = {"pytest.skip", "pytest.xfail", "self.skipTest"}
RAISES_CALLS = {"pytest.raises", "pytest.warns", "raises", "warns"}


@dataclass(frozen=True)
class FileChange:
    """One changed path. `old_path` differs from `path` on renames; a side is None when absent."""

    path: str
    base_src: str | None
    head_src: str | None
    old_path: str | None = None


@dataclass(frozen=True)
class TestInfo:
    line: int
    assertions: int
    trivial: int
    skips: int


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def analyze(changes: list[FileChange]) -> list[Finding]:
    """Findings for a whole PR, so a test moved between files is not reported as removed."""
    findings: list[Finding] = []
    base_tests: dict[str, dict[str, TestInfo]] = {}
    head_tests: dict[str, dict[str, TestInfo]] = {}
    unparseable: set[str] = set()

    for ch in changes:
        if PurePosixPath(ch.path).name in CONFIG_FILES or PurePosixPath(ch.path).name == "pyproject.toml":
            findings += _config_findings(ch)
            continue
        if is_test_file(ch.old_path or ch.path) and ch.base_src is not None:
            parsed = _collect(ch.base_src)
            if parsed is not None:
                base_tests[ch.path] = parsed
        if is_test_file(ch.path) and ch.head_src is not None:
            parsed = _collect(ch.head_src)
            if parsed is None:
                unparseable.add(ch.path)
                findings.append(
                    Finding(
                        "unparseable", Severity.WARNING, ch.path, "Python could not parse this test file at head, so it was not compared."
                    )
                )
            else:
                head_tests[ch.path] = parsed

    # Tests that appear in a file which did not have them at base: candidates for moves.
    added_elsewhere: dict[str, tuple[str, TestInfo]] = {}
    for path, tests in head_tests.items():
        for name, info in tests.items():
            if name not in base_tests.get(path, {}):
                added_elsewhere.setdefault(name, (path, info))

    for path, tests in base_tests.items():
        if path in unparseable:
            continue
        head = head_tests.get(path)
        deleted = head is None and not any(c.path == path and c.head_src is not None for c in changes)
        missing: list[str] = []
        for name, before in tests.items():
            after = head.get(name) if head is not None else None
            where = path
            if after is None and name in added_elsewhere:
                where, after = added_elsewhere[name]
            if after is None:
                missing.append(name)
                if not deleted:
                    findings.append(
                        Finding(
                            "test_removed", Severity.BLOCKING, path, "Test exists at the merge base and is gone at head.", name, before.line
                        )
                    )
                continue
            findings += _compare(where, name, before, after)
        if deleted and missing:
            findings.append(
                Finding(
                    "test_file_deleted",
                    Severity.BLOCKING,
                    path,
                    f"Test file deleted; {len(missing)} of its {len(tests)} tests do not reappear elsewhere: {', '.join(missing[:5])}"
                    + (" ..." if len(missing) > 5 else ""),
                )
            )
    return findings


def _compare(path: str, name: str, before: TestInfo, after: TestInfo) -> list[Finding]:
    out: list[Finding] = []
    if after.skips > before.skips:
        out.append(Finding("skip_added", Severity.BLOCKING, path, "Test gained a skip or xfail.", name, after.line))
    if after.assertions < before.assertions:
        out.append(
            Finding(
                "assertions_reduced",
                Severity.BLOCKING,
                path,
                f"Assertions dropped from {before.assertions} to {after.assertions}.",
                name,
                after.line,
            )
        )
    if after.trivial > before.trivial:
        out.append(Finding("trivial_assertion_added", Severity.BLOCKING, path, "Added an assertion that is always true.", name, after.line))
    return out


def _collect(src: str) -> dict[str, TestInfo] | None:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    module_skips = _pytestmark_skips(tree.body)
    tests: dict[str, TestInfo] = {}
    for node in tree.body:
        if _is_test_func(node):
            tests[node.name] = _info(node, module_skips)
        elif isinstance(node, ast.ClassDef):
            class_skips = module_skips + _decorator_skips(node) + _pytestmark_skips(node.body)
            for item in node.body:
                if _is_test_func(item):
                    tests[f"{node.name}.{item.name}"] = _info(item, class_skips)
    return tests


def _is_test_func(node: ast.stmt) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _info(func: ast.FunctionDef | ast.AsyncFunctionDef, inherited_skips: int) -> TestInfo:
    assertions = trivial = skips = 0
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            assertions += 1
            if isinstance(node.test, ast.Constant) and node.test.value:
                trivial += 1
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            last = name.rsplit(".", 1)[-1]
            if last.startswith("assert") or name in RAISES_CALLS:
                assertions += 1
            elif name in SKIP_CALLS or last == "skipTest":
                skips += 1
    return TestInfo(func.lineno, assertions, trivial, skips + inherited_skips + _decorator_skips(func))


def _decorator_skips(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    return sum(1 for d in node.decorator_list if _dotted(d) in SKIP_DECORATORS)


def _pytestmark_skips(body: list[ast.stmt]) -> int:
    count = 0
    for node in body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            values = node.value.elts if isinstance(node.value, ast.List | ast.Tuple) else [node.value]
            count += sum(1 for v in values if _dotted(v) in SKIP_DECORATORS)
    return count


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    return ""


def _config_findings(ch: FileChange) -> list[Finding]:
    if ch.base_src == ch.head_src:
        return []
    name = PurePosixPath(ch.path).name
    if name != "pyproject.toml":
        state = "added" if ch.base_src is None else "deleted" if ch.head_src is None else "changed"
        return [Finding("test_config_changed", Severity.WARNING, ch.path, f"Test configuration {state}; check it does not hide failures.")]
    before, after = _test_tables(ch.base_src), _test_tables(ch.head_src)
    if before == after:
        return []
    return [
        Finding(
            "test_config_changed",
            Severity.WARNING,
            ch.path,
            "pytest or coverage settings in pyproject.toml changed; check they do not hide failures.",
        )
    ]


def _test_tables(src: str | None) -> object:
    if src is None:
        return None
    try:
        tool = tomllib.loads(src).get("tool", {})
    except tomllib.TOMLDecodeError:
        return ("unparseable", src)
    return {k: v for k, v in tool.items() if k.startswith(("pytest", "coverage"))}
