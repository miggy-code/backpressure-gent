"""The tool loop with a scripted model and an in-memory sandbox."""

from typing import Any

from review_fakes import FakeWorkspace, ScriptedModel, call, reply

from backpressure.review.loop import Status, run_loop
from backpressure.review.model import BudgetExhausted
from backpressure.review.schema import DiffIndex, parse_and_validate
from backpressure.review.tools import Ledger, Toolbox

DIFF = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n"


def good_review(run: str = "r1") -> dict[str, Any]:
    return {
        "intent": "Add y.",
        "scorecard": {"fit": "pass", "checks": "pass", "tests": "not_needed", "risk": "low", "depth": "glance"},
        "findings": [],
        "tamper": [],
        "verified": [{"claim": "tests pass", "runs": [run]}],
        "not_verified": [],
    }


def setup(model: ScriptedModel, max_steps: int = 60):
    ledger = Ledger()
    ws = FakeWorkspace({"pytest -q": (0, "3 passed in 0.01s")}, {"app.py": "x = 1\ny = 2\n"})
    diff = DiffIndex.parse(DIFF)
    result = run_loop(
        model, Toolbox(ws, ledger), "system", "kickoff",
        lambda raw: parse_and_validate(raw, ledger, diff, []), max_steps=max_steps,
    )  # fmt: skip
    return result, ws, ledger


def test_runs_tools_then_accepts_a_valid_review() -> None:
    model = ScriptedModel(
        reply(call("run", {"command": "pytest -q"})),
        reply(call("submit_review", {"review": good_review()}, "c2")),
    )
    result, ws, ledger = setup(model)
    assert result.status is Status.SUBMITTED
    assert ws.commands == ["pytest -q"]
    assert ledger.get("r1").output == "3 passed in 0.01s"
    tool_msg = model.seen[1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("[r1] exit 0")
    # The assistant message goes back with its reasoning_content, as DeepSeek thinking mode requires.
    assert model.seen[1][2]["reasoning_content"] == "thinking"
    assert result.calls == 2 and result.usage.prompt_tokens == 200


def test_validation_errors_go_back_to_the_model_and_a_fix_is_accepted() -> None:
    model = ScriptedModel(
        reply(call("run", {"command": "pytest -q"})),
        reply(call("submit_review", {"review": good_review(run="r5")}, "c2")),
        reply(call("submit_review", {"review": good_review()}, "c3")),
    )
    result, _, _ = setup(model)
    assert result.status is Status.SUBMITTED
    rejection = model.seen[2][-1]
    assert rejection["tool_call_id"] == "c2" and "'r5' is not in the ledger" in rejection["content"]


def test_gives_up_after_two_retries() -> None:
    bad = good_review(run="r9")
    model = ScriptedModel(*(reply(call("submit_review", {"review": bad}, f"c{i}")) for i in range(4)))
    result, _, _ = setup(model)
    assert result.status is Status.INVALID
    assert result.calls == 3
    assert any("'r9'" in e for e in result.errors)


def test_step_limit_stops_tools_and_asks_for_a_review() -> None:
    model = ScriptedModel(reply(call("run", {"command": "ls"})))  # never submits
    result, ws, _ = setup(model, max_steps=3)
    assert result.status is Status.STEP_LIMIT
    assert len(ws.commands) == 3
    assert model.tools_seen[-1] == ["submit_review"]


def test_step_limit_still_accepts_a_final_review() -> None:
    model = ScriptedModel(
        reply(call("run", {"command": "pytest -q"})),
        reply(call("run", {"command": "ls"}, "c2")),
        reply(call("submit_review", {"review": good_review()}, "c3")),
    )
    result, ws, _ = setup(model, max_steps=2)
    assert result.status is Status.SUBMITTED
    assert len(ws.commands) == 2


def test_extra_calls_in_one_reply_do_not_exceed_the_step_limit() -> None:
    many = reply(*(call("run", {"command": f"echo {i}"}, f"c{i}") for i in range(5)))
    model = ScriptedModel(many, reply(call("submit_review", {"review": good_review()}, "s")))
    result, ws, _ = setup(model, max_steps=2)
    assert len(ws.commands) == 2
    assert result.status is Status.SUBMITTED


def test_bad_json_arguments_are_reported_not_raised() -> None:
    model = ScriptedModel(
        reply(call("run", "{not json")),
        reply(call("run", {"command": "pytest -q"}, "c2")),
        reply(call("submit_review", {"review": good_review()}, "c3")),
    )
    result, _, _ = setup(model)
    assert result.status is Status.SUBMITTED
    assert "could not parse arguments" in model.seen[1][-1]["content"]


def test_plain_text_replies_are_nudged_then_fail() -> None:
    model = ScriptedModel(reply(content="Looks good to me!"))
    result, _, _ = setup(model)
    assert result.status is Status.INVALID
    assert result.calls == 4


def test_budget_cap_ends_the_run() -> None:
    class Capped(ScriptedModel):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            raise BudgetExhausted("run_cap")

    result, _, _ = setup(Capped(reply()))
    assert result.status is Status.BUDGET
    assert "run_cap" in result.errors[0]


def test_read_file_results_are_ledger_entries() -> None:
    model = ScriptedModel(
        reply(call("read_file", {"path": "app.py", "start": 2})),
        reply(call("read_file", {"path": "nope.py"}, "c2")),
        reply(call("submit_review", {"review": good_review()}, "c3")),
    )
    result, _, ledger = setup(model)
    assert ledger.get("r1").output == "y = 2"
    assert "    2  y = 2" in model.seen[1][-1]["content"]
    assert ledger.get("r2").exit_code == 1
    assert result.status is Status.SUBMITTED
