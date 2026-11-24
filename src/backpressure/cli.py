"""Command line: `backpressure tamper --base REF --head REF`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from backpressure import gitdiff
from backpressure.report import Report
from backpressure.tamper import analyze


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backpressure", description="Verification gate for agent-written pull requests.")
    sub = parser.add_subparsers(dest="command", required=True)
    tamper = sub.add_parser("tamper", help="Fail when a change makes Python tests easier to pass.")
    tamper.add_argument("--base", required=True, help="Base ref, e.g. origin/main. Compared at its merge base with head.")
    tamper.add_argument("--head", default="HEAD", help="Head ref (default HEAD).")
    tamper.add_argument("--json", type=Path, help="Write the JSON report here.")
    tamper.add_argument("--repo-url", help="https://github.com/owner/repo, to link findings to lines.")
    tamper.add_argument("--fail-on", choices=["blocking", "never"], default="blocking")
    tamper.add_argument("-C", dest="cwd", default=None, help="Run in this git checkout.")
    args = parser.parse_args(argv)

    if not args.base:
        print(
            "backpressure: no base ref. Pass --base (the action defaults it on pull_request events), "
            "and check out with fetch-depth: 0 so the merge base is available.",
            file=sys.stderr,
        )
        return 2
    try:
        head_sha = gitdiff.rev_parse(args.head, args.cwd)
        base_sha = gitdiff.merge_base(gitdiff.rev_parse(args.base, args.cwd), head_sha, args.cwd)
        report = Report(base_sha, head_sha, analyze(gitdiff.changes(base_sha, head_sha, args.cwd)))
    except gitdiff.GitError as exc:
        print(f"backpressure: {exc}. If the ref exists, check out with fetch-depth: 0 so history is available.", file=sys.stderr)
        return 2

    markdown = report.to_markdown(args.repo_url)
    print(markdown)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(report.to_json() + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    return 1 if report.verdict == "fail" and args.fail_on == "blocking" else 0
