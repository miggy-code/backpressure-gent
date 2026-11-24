"""Findings, verdicts and their JSON and Markdown renderings."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    path: str
    message: str
    test: str | None = None
    line: int | None = None
    at_base: bool = False  # the line exists only at the merge base (removed tests)


@dataclass(frozen=True)
class Report:
    base_sha: str
    head_sha: str
    findings: list[Finding] = field(default_factory=list)
    overridden: bool = False  # a maintainer accepted the findings with the backpressure-override label

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def verdict(self) -> str:
        if not self.blocking:
            return "pass"
        return "overridden" if self.overridden else "fail"

    def to_json(self) -> str:
        # Each property filters the whole list; compute them once.
        blocking, warnings = self.blocking, self.warnings
        return json.dumps(
            {
                "verdict": "pass" if not blocking else ("overridden" if self.overridden else "fail"),
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "blocking": len(blocking),
                "warnings": len(blocking),
                "findings": [asdict(f) for f in self.findings],
            },
            indent=2,
        )

    def to_markdown(self, repo_url: str | None = None) -> str:
        mark, word = {"pass": ("✅", "Pass"), "fail": ("❌", "Fail"), "overridden": ("⚠️", "Overridden")}[self.verdict]
        lines = [
            f"## {mark} Backpressure tamper gate: {word}",
            "",
            "| Tamper | Blocking | Warnings |",
            "|---|---|---|",
            f"| {mark} {word} | {_cell(len(self.blocking), '❌')} | {_cell(len(self.warnings), '⚠️')} |",
        ]
        if self.verdict == "overridden":
            note = "A maintainer added the `backpressure-override` label: findings are kept for the record and do not fail the gate."
            lines += ["", note]
        if self.findings:
            lines += ["", "**Needs attention**", ""]
            for f in self.blocking + self.warnings:
                icon = "❌" if f.severity is Severity.BLOCKING else "⚠️"
                where = f"`{f.path}`" + (f" `{f.test}`" if f.test else "")
                if repo_url:
                    anchor = f"#L{f.line}" if f.line else ""
                    sha = self.base_sha if f.at_base else self.head_sha
                    where = f"[{where}]({repo_url}/blob/{sha}/{f.path}{anchor})"
                lines.append(f"- {icon} **{f.code}** {where}: {f.message}")
        lines += [
            "",
            "<details><summary>Details</summary>",
            "",
            f"Compared head `{self.head_sha[:7]}` with merge base `{self.base_sha[:7]}`.",
            "Python test files only. Tests and test files added by the PR are never findings.",
            "",
            "</details>",
        ]
        return "\n".join(lines) + "\n"


def _cell(count: int, marker: str) -> str:
    return "✅ 0" if count == 0 else f"{marker} {count}"
