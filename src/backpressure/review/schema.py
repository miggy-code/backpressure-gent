"""The review the model submits: parse and validate it against the evidence, then decide the verdict in code."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from backpressure.report import Finding as GateFinding
from backpressure.review.holdouts import HoldoutResult
from backpressure.review.tools import Ledger

MIN_EXCERPT = 10
MAX_BODY_LINES = 5


class Verdict(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    CLOSE = "close"


@dataclass(frozen=True)
class Evidence:
    run: str
    excerpt: str


@dataclass(frozen=True)
class Scorecard:
    fit: str
    checks: str
    tests: str
    risk: str
    depth: str


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    title: str
    body: str
    path: str
    evidence: list[Evidence]
    line: int | None = None
    outside_diff: bool = False

    @property
    def blocking(self) -> bool:
        return self.severity == "blocking"


@dataclass(frozen=True)
class Adjudication:
    code: str
    path: str
    test: str | None
    status: str
    explanation: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class Verified:
    claim: str
    runs: list[str]


@dataclass(frozen=True)
class Close:
    reason: str
    explanation: str
    evidence: list[Evidence]


@dataclass(frozen=True)
class Review:
    intent: str
    scorecard: Scorecard
    findings: list[ReviewFinding]
    tamper: list[Adjudication]
    verified: list[Verified]
    not_verified: list[str]
    fit_notes: str = ""
    risk_notes: str = ""
    close: Close | None = None


ENUMS = {
    "fit": ("pass", "partial", "fail"),
    "checks": ("pass", "fail", "not_run"),
    "tests": ("pass", "fail", "not_needed"),
    "risk": ("none", "low", "medium", "high"),
    "depth": ("glance", "focused", "deep"),
}


@dataclass(frozen=True)
class DiffIndex:
    """Changed files and, per file, the line ranges its hunks cover on either side."""

    files: dict[str, list[tuple[int, int]]]

    @classmethod
    def parse(cls, diff: str, paths: Iterable[str] = ()) -> DiffIndex:
        """`paths` adds files the diff text may not name (binary files, pure renames)."""
        files: dict[str, list[tuple[int, int]]] = {p: [] for p in paths}
        current: list[str] = []
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                current = []
            elif line.startswith(("--- ", "+++ ")):
                name = line[4:].split("\t")[0]
                if name != "/dev/null":
                    name = name[2:] if name[:2] in ("a/", "b/") else name
                    files.setdefault(name, [])
                    current.append(name)
            elif line.startswith("rename from ") or line.startswith("rename to "):
                name = line.split(" ", 2)[2]
                files.setdefault(name, [])
                current.append(name)
            elif m := re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line):
                old, old_n, new, new_n = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
                for name in dict.fromkeys(current):
                    files[name] += [(old, old + max(old_n, 1) - 1), (new, new + max(new_n, 1) - 1)]
        return cls(files)

    def has(self, path: str) -> bool:
        return path in self.files

    def in_hunk(self, path: str, line: int) -> bool:
        return any(lo <= line <= hi for lo, hi in self.files.get(path, []))


def parse_and_validate(raw: Any, ledger: Ledger, diff: DiffIndex, gate: list[GateFinding]) -> tuple[Review | None, list[str]]:
    """Return the review, or the list of errors to send back to the model."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["review must be an object"]

    def text(obj: dict[str, Any], key: str, where: str, required: bool = True) -> str:
        value = obj.get(key)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value.strip()):
            errors.append(f"{where}.{key} must be a non-empty string")
            return ""
        return value

    def evidence(items: Any, where: str, required: bool) -> list[Evidence]:
        if items is None:
            items = []
        if not isinstance(items, list):
            errors.append(f"{where}.evidence must be a list")
            return []
        if required and not items:
            errors.append(f"{where}: cite at least one run ID with an excerpt from its output")
        out = []
        for i, ev in enumerate(items):
            here = f"{where}.evidence[{i}]"
            if not isinstance(ev, dict) or not isinstance(ev.get("run"), str) or not isinstance(ev.get("excerpt"), str):
                errors.append(f"{here} must be {{run, excerpt}} strings")
                continue
            errors.extend(_check_excerpt(ev["run"], ev["excerpt"], ledger, here))
            out.append(Evidence(ev["run"], ev["excerpt"]))
        return out

    intent = text(raw, "intent", "review")
    card_raw = raw.get("scorecard")
    card = None
    if not isinstance(card_raw, dict):
        errors.append("review.scorecard must be an object")
    else:
        for key, allowed in ENUMS.items():
            if card_raw.get(key) not in allowed:
                errors.append(f"scorecard.{key} must be one of {', '.join(allowed)}")
        if not any(e.startswith("scorecard.") for e in errors):
            card = Scorecard(**{k: card_raw[k] for k in ENUMS})

    findings = []
    for i, f in enumerate(_list(raw, "findings", errors)):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{where} must be an object")
            continue
        severity = f.get("severity")
        if severity not in ("blocking", "non_blocking"):
            errors.append(f"{where}.severity must be blocking or non_blocking")
        title, body, path = text(f, "title", where), text(f, "body", where), text(f, "path", where)
        if body.count("\n") >= MAX_BODY_LINES:
            errors.append(f"{where}.body must be at most {MAX_BODY_LINES} lines")
        if path and not diff.has(path):
            errors.append(f"{where}.path {path!r} is not a file changed by this PR")
        line, outside = f.get("line"), bool(f.get("outside_diff", False))
        if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
            errors.append(f"{where}.line must be a positive integer")
            line = None
        if line is not None and path and diff.has(path) and not outside and not diff.in_hunk(path, line):
            errors.append(f"{where}.line {line} is outside the changed hunks of {path}; fix it or set outside_diff: true")
        ev = evidence(f.get("evidence"), where, required=True)
        findings.append(ReviewFinding(str(severity), title, body, path, ev, line, outside))

    gate_keys = {(g.code, g.path, g.test) for g in gate}
    tamper = []
    for i, a in enumerate(_list(raw, "tamper", errors)):
        where = f"tamper[{i}]"
        if not isinstance(a, dict):
            errors.append(f"{where} must be an object")
            continue
        key = (a.get("code"), a.get("path"), a.get("test"))
        if key not in gate_keys:
            errors.append(f"{where} does not match a gate finding; use the gate's exact code, path and test")
        status = a.get("status")
        if status not in ("confirmed", "explained"):
            errors.append(f"{where}.status must be confirmed or explained")
        explanation = text(a, "explanation", where)
        ev = evidence(a.get("evidence"), where, required=status == "explained")
        tamper.append(Adjudication(str(key[0]), str(key[1]), key[2], str(status), explanation, ev))

    verified = []
    for i, v in enumerate(_list(raw, "verified", errors)):
        where = f"verified[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{where} must be an object")
            continue
        claim, runs = text(v, "claim", where), v.get("runs")
        if not isinstance(runs, list) or not runs or not all(isinstance(r, str) for r in runs):
            errors.append(f"{where}.runs must list at least one run ID")
            runs = []
        errors.extend(f"{where}: run {r!r} is not in the ledger" for r in runs if ledger.get(r) is None)
        verified.append(Verified(claim, list(runs)))

    not_verified = _list(raw, "not_verified", errors)
    if not all(isinstance(n, str) for n in not_verified):
        errors.append("not_verified must be a list of strings")

    close = None
    if (c := raw.get("close")) is not None:
        if not isinstance(c, dict) or c.get("reason") not in ("injection", "duplicate"):
            errors.append("close.reason must be injection or duplicate (or set close to null)")
        else:
            close = Close(c["reason"], text(c, "explanation", "close"), evidence(c.get("evidence"), "close", required=True))

    if errors or card is None:
        return None, errors
    return Review(
        intent=intent,
        scorecard=card,
        findings=findings,
        tamper=tamper,
        verified=verified,
        not_verified=[str(n) for n in not_verified],
        fit_notes=text(raw, "fit_notes", "review", required=False),
        risk_notes=text(raw, "risk_notes", "review", required=False),
        close=close,
    ), []


def verdict(review: Review, gate: list[GateFinding], holdouts: HoldoutResult | None = None) -> Verdict:
    """First match wins. `checks: not_run` does not block; it shows as a warning on the scorecard."""
    if review.close is not None:
        return Verdict.CLOSE
    if holdouts is not None and holdouts.regressed:
        return Verdict.REQUEST_CHANGES
    card = review.scorecard
    if card.fit in ("partial", "fail") or card.checks == "fail" or card.tests == "fail":
        return Verdict.REQUEST_CHANGES
    if any(f.blocking for f in review.findings):
        return Verdict.REQUEST_CHANGES
    if unresolved_tamper(review, gate):
        return Verdict.REQUEST_CHANGES
    return Verdict.APPROVE


def unresolved_tamper(review: Review, gate: list[GateFinding]) -> list[GateFinding]:
    """Gate findings the model confirmed or did not adjudicate."""
    explained = {(a.code, a.path, a.test) for a in review.tamper if a.status == "explained"}
    return [g for g in gate if (g.code, g.path, g.test) not in explained]


_GUTTER = re.compile(r"^\s*\d+  ", re.MULTILINE)  # the line numbers read_file shows the model


def _normal(s: str) -> str:
    return " ".join(s.split())


def _check_excerpt(run_id: str, excerpt: str, ledger: Ledger, where: str) -> list[str]:
    entry = ledger.get(run_id)
    if entry is None:
        return [f"{where}: run {run_id!r} is not in the ledger"]
    quoted = _normal(_GUTTER.sub("", excerpt)) if entry.tool == "read_file" else _normal(excerpt)
    if len(quoted) < MIN_EXCERPT:
        return [f"{where}: excerpt must be at least {MIN_EXCERPT} characters"]
    if quoted not in _normal(entry.output):
        return [f"{where}: excerpt is not in the output of {run_id}; copy it verbatim"]
    return []


def _list(raw: dict[str, Any], key: str, errors: list[str]) -> list[Any]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"review.{key} must be a list")
        return []
    return value
