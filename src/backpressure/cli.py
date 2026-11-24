"""Command line: `backpressure tamper --base REF` and `backpressure review --repo OWNER/REPO --pr N`."""

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
    tamper.add_argument("--override", action="store_true", help="Report findings but pass (the backpressure-override label).")
    tamper.add_argument("-C", dest="cwd", default=None, help="Run in this git checkout.")
    review = sub.add_parser("review", help="Investigate one pull request with the DeepSeek reviewer (local; does not post).")
    review.add_argument("--repo", required=True, help="OWNER/REPO on GitHub.")
    review.add_argument("--pr", type=int, required=True, help="Pull request number.")
    review.add_argument("--out", type=Path, help="Output folder (default .backpressure/review-<pr>-<sha7>/).")
    review.add_argument("--model", default="deepseek-v4-pro", choices=["deepseek-v4-pro", "deepseek-flash"])
    review.add_argument("--max-usd", default="1.00", help="Dollar cap the model proxy enforces (default 1.00).")
    review.add_argument("--head-sha", help="Refuse to review unless the PR head is still this commit.")
    review.add_argument("--holdouts", type=Path, help="Folder of private holdout tests, laid out as OWNER/REPO/test_*.py.")
    gh_start = sub.add_parser("github-start", help="Workflow: check the requester, post the status comment.")
    gh_finish = sub.add_parser("github-finish", help="Workflow: post the review and its label, remove the trigger label.")
    for p in (gh_start, gh_finish):
        p.add_argument("--repo", required=True)
        p.add_argument("--pr", type=int, required=True)
        p.add_argument("--run-url", required=True)
    gh_start.add_argument("--requester", required=True)
    gh_start.add_argument("--allow-forks", action="store_true")
    gh_finish.add_argument("--dir", type=Path, help="The review job's output folder, if it produced one.")
    gh_finish.add_argument("--comment-id", type=int, default=0)
    gh_finish.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    if args.command == "review":
        return _review(args)
    if args.command in ("github-start", "github-finish"):
        return _github(args)

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
        report = Report(base_sha, head_sha, analyze(gitdiff.changes(base_sha, head_sha, args.cwd)), overridden=args.override)
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


def _review(args: argparse.Namespace) -> int:
    # Imported here so the gate never loads the reviewer's dependencies.
    from backpressure.review.pipeline import ReviewError, review_pr
    from backpressure.review.schema import Verdict

    try:
        outcome = review_pr(
            args.repo,
            args.pr,
            args.out,
            model=args.model,
            max_usd=args.max_usd,
            head_sha=args.head_sha,
            holdouts_root=args.holdouts,
        )
    except (ReviewError, RuntimeError) as exc:
        print(f"backpressure: {exc}", file=sys.stderr)
        return 2
    print(outcome.markdown)
    print(f"Review files: {outcome.out_dir}", file=sys.stderr)
    if outcome.verdict is None:
        return 2
    return 0 if outcome.verdict is Verdict.APPROVE else 1


def _github(args: argparse.Namespace) -> int:
    from backpressure.review import github

    api = github.GhCli()
    if args.command == "github-start":
        started = github.start(api, args.repo, args.pr, args.requester, args.run_url, allow_forks=args.allow_forks)
        outputs = f"allowed={str(started.allowed).lower()}\nhead_sha={started.head_sha}\ncomment_id={started.comment_id}\n"
        print(outputs, end="")
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(outputs)
        if not started.allowed:
            print(f"backpressure: not started: {started.reason}", file=sys.stderr)
        return 0
    folder = args.dir if args.dir and args.dir.is_dir() else None
    verdict = github.finish(api, args.repo, args.pr, folder, args.comment_id, args.head_sha, args.run_url)
    print(f"verdict={verdict.value if verdict else 'none'}")
    return 0 if verdict else 1
