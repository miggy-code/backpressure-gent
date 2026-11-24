"""The review as GitHub Markdown (scorecard first, details collapsed) and as JSON."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from backpressure.report import Finding as GateFinding
from backpressure.review.holdouts import HoldoutResult
from backpressure.review.model import Usage
from backpressure.review.schema import Review, Verdict, unresolved_tamper
from backpressure.review.tools import Ledger

OK, WARN, BAD = "✅", "⚠️", "❌"
TITLES = {Verdict.APPROVE: f"{OK} Approve", Verdict.REQUEST_CHANGES: f"{BAD} Request changes", Verdict.CLOSE: f"{BAD} Close"}


@dataclass(frozen=True)
class RunInfo:
    model: str
    head_sha: str
    repo_url: str
    usage: Usage
    calls: int
    steps: int
    spent_usd: str | None = None


def to_markdown(
    review: Review,
    verdict: Verdict,
    gate: list[GateFinding],
    run: RunInfo,
    ledger: Ledger | None = None,
    holdouts: HoldoutResult | None = None,
) -> str:
    card = review.scorecard
    unresolved = unresolved_tamper(review, gate)
    blocking = [f for f in review.findings if f.blocking]
    minor = [f for f in review.findings if not f.blocking]
    cells = [
        ("Fit", {"pass": OK, "partial": WARN, "fail": BAD}[card.fit], card.fit),
        ("Checks", {"pass": OK, "not_run": WARN, "fail": BAD}[card.checks], card.checks.replace("_", " ")),
        ("Tests", {"pass": OK, "not_needed": OK, "fail": BAD}[card.tests], card.tests.replace("_", " ")),
        ("Tamper", BAD if unresolved else WARN if gate else OK, _tamper_cell(len(unresolved), len(gate))),
        ("Holdouts", *_holdout_cell(holdouts)),
        ("Blocking", BAD if blocking else OK, str(len(blocking))),
        ("Non-blocking", WARN if minor else OK, str(len(minor))),
        ("Risk", WARN if card.risk in ("medium", "high") else OK, card.risk),
        ("Depth", WARN if card.checks == "not_run" or (card.risk == "high" and card.depth == "glance") else OK, card.depth),
    ]  # fmt: skip
    lines = [
        f"### Backpressure review: {TITLES[verdict]}",
        "",
        "| " + " | ".join(c[0] for c in cells) + " |",
        "|" + "---|" * len(cells),
        "| " + " | ".join(f"{m} {v}" for _, m, v in cells) + " |",
        "",
    ]

    attention = []
    if review.close:
        attention.append(f"- {BAD} **Close ({review.close.reason})**: {clean(review.close.explanation)} {_cite(review.close.evidence)}")
    if holdouts and holdouts.regressed:
        n = holdouts.regressed
        attention.append(
            f"- {BAD} **Holdouts**: {n} holdout scenario{'s' if n > 1 else ''} that pass{'' if n > 1 else 'es'} at the merge base "
            f"fail{'' if n > 1 else 's'} at head. Holdouts are private so they cannot be gamed; a maintainer can rerun the "
            "review locally with `--holdouts` to see which."
        )
    for f in blocking:
        attention.append(f"- {BAD} **{clean(f.title)}** ({_where(f.path, f.line, run)}): {clean(_one_line(f.body))} {_cite(f.evidence)}")
    for g in unresolved:
        adj = next((a for a in review.tamper if (a.code, a.path, a.test) == (g.code, g.path, g.test)), None)
        why = f"confirmed: {clean(adj.explanation)}" if adj else "not adjudicated by the reviewer"
        test = f" `{g.test}`" if g.test else ""
        attention.append(f"- {BAD} **Tamper `{g.code}`**{test} ({_where(g.path, None, run)}): {why}")
    if card.checks == "not_run":
        attention.append(f"- {WARN} **Checks not run**: see Not verified.")
    for f in minor:
        attention.append(f"- {WARN} **{clean(f.title)}** ({_where(f.path, f.line, run)}): {clean(_one_line(f.body))} {_cite(f.evidence)}")
    if attention:
        lines += ["**Needs attention**", "", *attention, ""]

    details = [f"**Intent:** {clean(review.intent)}", ""]
    if review.fit_notes:
        details += [f"**Fit:** {clean(review.fit_notes)}", ""]
    details += ["**Verified**", ""]
    details += [f"- {clean(v.claim)} ({', '.join(f'`{r}`' for r in v.runs)})" for v in review.verified] or ["- Nothing."]
    details += ["", "**Not verified**", ""]
    details += [f"- {clean(n)}" for n in review.not_verified] or ["- Nothing listed."]
    explained = [a for a in review.tamper if a.status == "explained"]
    if explained:
        details += ["", "**Tamper findings explained**", ""]
        for a in explained:
            test = f" `{a.test}`" if a.test else ""
            details.append(f"- `{a.code}` {a.path}{test}: {clean(a.explanation)} {_cite(a.evidence)}")
    if review.risk_notes:
        details += ["", f"**Risk:** {clean(review.risk_notes)}"]
    if ledger is not None:
        details += ["", "**Evidence runs**", "", *_evidence_runs(review, ledger)]
    u = run.usage
    cost = f", ${run.spent_usd}" if run.spent_usd is not None else ""
    details += [
        "",
        f"<sub>{run.model} · {run.calls} model calls, {run.steps} tool calls · {u.prompt_tokens:,} in ({u.cached_tokens:,} cached) / "
        f"{u.completion_tokens:,} out tokens{cost} · head {run.head_sha[:7]}</sub>",
    ]
    lines += ["<details><summary>Details</summary>", "", *details, "", "</details>", ""]
    return "\n".join(lines)


def to_json(review: Review, verdict: Verdict, gate: list[GateFinding], run: RunInfo, holdouts: HoldoutResult | None = None) -> str:
    data: dict[str, Any] = {
        "verdict": verdict.value,
        "head_sha": run.head_sha,
        "review": asdict(review),
        "gate": [asdict(g) for g in gate],
        "unresolved_tamper": [asdict(g) for g in unresolved_tamper(review, gate)],
        "holdouts": holdouts.counts() if holdouts else None,  # counts only, never names
        "run": {**asdict(run), "usage": asdict(run.usage)},
    }
    return json.dumps(data, indent=2)


_CODE_SPAN = re.compile(r"(`+)(.+?)\1", re.DOTALL)


def clean(text: str) -> str:
    """Model text is untrusted: no raw HTML tags, and no @mentions that would notify people when posted.

    Code spans are left as written: GitHub shows entities literally there and renders neither HTML nor mentions.
    """
    parts, pos = [], 0
    for m in _CODE_SPAN.finditer(text):
        parts += [_neutralise(text[pos : m.start()]), m.group(0)]
        pos = m.end()
    return "".join([*parts, _neutralise(text[pos:])])


def _neutralise(text: str) -> str:
    return re.sub(r"@(?=[A-Za-z0-9-])", "@\u200b", text.replace("<", "&lt;"))


def _evidence_runs(review: Review, ledger: Ledger) -> list[str]:
    """Every cited run with its command, so evidence a model made itself (e.g. with echo) is visible."""
    cited = [e.run for f in review.findings for e in f.evidence]
    cited += [e.run for a in review.tamper for e in a.evidence]
    cited += [r for v in review.verified for r in v.runs]
    cited += [e.run for e in review.close.evidence] if review.close else []
    lines = []
    for run_id in sorted(set(cited), key=lambda r: int(r[1:]) if r[1:].isdigit() else 0):
        entry = ledger.get(run_id)
        if entry is None:
            continue
        first, *rest = entry.command.strip().splitlines() or [""]
        shown = first[:120] + (" …" if rest or len(first) > 120 else "")
        lines.append(f"- `{run_id}` exit {entry.exit_code}: `{shown.replace('`', "'")}`")
    return lines


def _holdout_cell(holdouts: HoldoutResult | None) -> tuple[str, str]:
    if holdouts is None:
        return OK, "none"
    if not holdouts.ran:
        return WARN, "not run"
    if holdouts.regressed:
        return BAD, f"{holdouts.regressed} regressed"
    if holdouts.still_failing:
        return WARN, f"{holdouts.still_failing} failing"
    return OK, f"{holdouts.total} pass"


def _tamper_cell(unresolved: int, total: int) -> str:
    if unresolved:
        return f"{unresolved} open"
    return f"{total} explained" if total else "none"


def _where(path: str, line: int | None, run: RunInfo) -> str:
    anchor = f"#L{line}" if line else ""
    label = f"{path}:{line}" if line else path
    return f"[{label}]({run.repo_url}/blob/{run.head_sha}/{path}{anchor})"


def _cite(evidence: list[Any]) -> str:
    runs = list(dict.fromkeys(e.run for e in evidence))
    return f"({', '.join(f'`{r}`' for r in runs)})" if runs else ""


def _one_line(text: str) -> str:
    return " ".join(text.split())
