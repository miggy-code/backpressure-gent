# Review rules

Backpressure's reviewer reads this file from the base branch when it reviews a PR in this repo.

## MUST (blocking)
- Tests MUST NOT be weakened to make the change pass: no deleted or skipped tests, dropped assertions, loosened tolerances or edited test config without a stated, honest reason.
- The repo's existing tests MUST pass at head.
- Changed behaviour MUST be covered by a test that fails when the change is broken.
- A bug fix MUST include a test that fails without the fix.
- The change MUST do what the PR description says, and claims in the description MUST be true.
- No secrets, credentials or personal data in code, tests or fixtures.
- No new network calls, subprocess calls or file writes outside the stated purpose.

## SHOULD (non-blocking)
- Changes SHOULD be scoped to the PR's purpose; unrelated edits belong in another PR.
- New code SHOULD follow the surrounding style and reuse existing helpers.
- Error paths SHOULD be handled or deliberately propagated, not swallowed.
- Public behaviour changes SHOULD be reflected in docs or the changelog.

## This repo
- The gate MUST stay free of network and model calls, and MUST NOT execute PR code.
- PR code MUST run only in the sandbox's work container; the provider key and any GitHub token MUST NOT reach it.
- Actions in workflows MUST be pinned by commit SHA.
