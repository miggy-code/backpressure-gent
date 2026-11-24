# Backpressure

A verification gate for pull requests written by coding agents.

Coding agents are rewarded for green tests, and they sometimes get there by changing the tests instead of the code: deleting a failing test, adding a skip, dropping an assertion, or editing the pytest config. Generic AI reviewers read the diff for bugs; they rarely ask whether the PR made itself easier to pass. Backpressure checks that first, deterministically, on every PR.

## What the tamper gate catches

It compares each changed Python test file at head with the PR's merge base.

| Finding | Severity | Example |
|---|---|---|
| `test_removed` | blocking | `def test_refund_rounding` existed at the merge base and is gone |
| `test_file_deleted` | blocking | `tests/test_billing.py` deleted, and its tests do not reappear elsewhere |
| `skip_added` | blocking | new `@pytest.mark.skip`, `xfail`, `unittest.skip`, `pytestmark = ...`, or `pytest.skip()`, `pytest.importorskip()`, `raise unittest.SkipTest` in the body |
| `assertions_reduced` | blocking | a test went from 3 assertions to 1 (`assert`, `self.assert*`, `mock.assert_*`, `pytest.raises`) |
| `trivial_assertion_added` | blocking | `assert x == expected` became `assert True` |
| `test_config_changed` | warning | `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `noxfile.py`, `.coveragerc`, or the `[tool.pytest]` / `[tool.coverage]` tables of `pyproject.toml` changed |
| `unparseable` | warning | a test file at head does not parse, so it was not compared |

New tests and new test files are never findings. A test moved unchanged to another file is clean; if it was also edited, it is reported as removed and the new file is named, so a same-named weaker test elsewhere cannot hide a deletion. A pure rename is clean.

The gate makes no network or model calls, needs no secrets, and never runs the PR's code; it parses it. That makes it safe on pull requests from forks.

## Use it in a workflow

```yaml
name: Backpressure
on: pull_request
permissions:
  contents: read
jobs:
  tamper:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          fetch-depth: 0 # the gate needs the merge base
          persist-credentials: false
      - uses: miggy-code/backpressure-gent@<commit-sha>
        with:
          fail-on: blocking # or "never" to report only
```

Pin the action by commit SHA, and make the job a required check in a branch ruleset. The report goes to the job summary; the `report` output is the JSON path and `verdict` is `pass` or `fail`.

## Run it locally

```sh
uv run backpressure tamper --base origin/main            # exit 0 pass, 1 blocking findings, 2 error
uv run backpressure tamper --base origin/main --json report.json
```

## Investigating reviewer (local)

`backpressure review` investigates one pull request instead of just reading its diff. A DeepSeek model runs the repo's tests, reverts a bug fix to check its test fails without it, breaks changed lines to find untested code, checks the PR's claims, and rules on each tamper-gate finding.

```sh
uv run backpressure review --repo OWNER/REPO --pr 123            # prints the review; files go to .backpressure/
uv run backpressure review --repo OWNER/REPO --pr 123 --max-usd 0.50
```

Exit codes: 0 approve, 1 request changes or close, 2 error. Needs Docker, an authenticated `gh`, and `DEEPSEEK_API_KEY` (on macOS it falls back to the Keychain item `evaluation-lab` / `DEEPSEEK_API_KEY`). It does not post to GitHub yet.

How it stays honest:
- **Sandbox.** PR code runs only in Docker. A setup container installs the repo with network access but no host environment. The work container has no network, no host environment, a non-root user, memory, process and CPU limits, a 120 s limit per command and 1 MB of kept output.
- **Key and budget.** Model calls go through an [Agentgate](https://github.com/miggy-code/agentgate) proxy that holds the key, allows only the DeepSeek models, and stops the run at its dollar and call caps (default $1.00, 80 calls). The model sees only a run token.
- **Evidence ledger.** Every command and file read gets an ID (`r1`, `r2`, ...). A finding must cite a run and quote its output; code rejects quotes that are not in that output, plus findings on files the PR did not change. The review lists each cited run's command.
- **Verdict in code.** The model fills a scorecard (Fit, Checks, Tests, Risk, Depth), findings and tamper rulings. Code decides: close for injection or duplicate with evidence; request changes for partial fit, failing checks or tests, a blocking finding, or a tamper finding confirmed or left unruled; otherwise approve.
- **Rules from the base branch.** The reviewer applies the target repo's `REVIEW.md` as it is on the base branch, so a PR cannot loosen the rules it is judged by. Without one it uses a default set.

The PR title, body and diff are given to the model as untrusted data. Code you review is sent to DeepSeek, so only review repos you may share with it.

## Limits

- Python and pytest/unittest conventions only (`test_*.py`, `*_test.py`, functions and methods named `test*`).
- Assertion counts are syntactic. Weakening an assertion while keeping its count (`==` to `!=`, a looser `pytest.approx`) is not caught yet.
- Honest refactors can trip it: merging tests into one `@pytest.mark.parametrize` test reports the originals as removed, and moving assertions into a helper reports fewer assertions. There is no per-PR override yet; use `fail-on: never` to report only, or review and merge with an admin bypass. An override arrives with the reviewer.
- A PR that edits the workflow or `CODEOWNERS` can still switch the gate off. Protect `.github/` with CODEOWNERS and a ruleset. In this repository the dogfood workflow uses `./`, so it runs the PR's own copy of the action.

## Roadmap

1. Tamper gate.
2. Investigating reviewer, local (this release).
3. Reviewer on GitHub: label-triggered, posts one scorecard review (Fit, Checks, Tests, Tamper, Blocking, Risk, Depth) with a Verified / Not verified section. Code, not the model, turns the verdict into a label.
4. Fork-safe reviewer through a GitHub App and a secretless sandbox; holdout scenarios the agent cannot see; slop delta.
5. Published numbers: catch rate, false positives and cost on seeded and canary agent PRs.

The reviewer design follows the Arize openinference factory's review lane.

## Develop

```sh
uv run pytest                     # sandbox tests need Docker; skip them with -m "not docker"
uv run ruff check . && uv run ruff format --check .
```

License: Apache-2.0.
