"""End to end: real git repos in a temp dir, the CLI's exit codes and reports."""

import json
import subprocess
from pathlib import Path

import pytest

from backpressure.cli import main

TESTS = "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n\n\ndef test_b():\n    assert 3 == 3\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(TESTS)
    (tmp_path / "app.py").write_text("x = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    return tmp_path


def commit(repo: Path, files: dict[str, str | None]) -> None:
    for path, text in files.items():
        if text is None:
            (repo / path).unlink()
        else:
            (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")


def run(repo: Path, tmp_path: Path, monkeypatch) -> tuple[int, dict]:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out = tmp_path / "report.json"
    code = main(["tamper", "--base", "main", "-C", str(repo), "--json", str(out)])
    return code, json.loads(out.read_text())


def test_honest_pr_passes(repo, tmp_path, monkeypatch):
    commit(repo, {"app.py": "x = 2\n", "tests/test_x.py": TESTS + "\n\ndef test_c():\n    assert 4 == 4\n"})
    code, report = run(repo, tmp_path, monkeypatch)
    assert (code, report["verdict"], report["findings"]) == (0, "pass", [])


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"tests/test_x.py": TESTS.split("\n\n\n")[0] + "\n"}, "test_removed"),
        ({"tests/test_x.py": "import pytest\n\n\n@pytest.mark.skip\n" + TESTS}, "skip_added"),
        ({"tests/test_x.py": TESTS.replace("    assert 2 == 2\n", "")}, "assertions_reduced"),
        ({"tests/test_x.py": None}, "test_file_deleted"),
    ],
)
def test_tampering_pr_fails_with_named_finding(repo, tmp_path, monkeypatch, files, expected):
    commit(repo, files)
    code, report = run(repo, tmp_path, monkeypatch)
    assert code == 1
    assert report["verdict"] == "fail"
    assert expected in {f["code"] for f in report["findings"]}


def test_pure_rename_passes(repo, tmp_path, monkeypatch):
    git(repo, "mv", "tests/test_x.py", "tests/test_y.py")
    git(repo, "commit", "-q", "-m", "rename")
    code, report = run(repo, tmp_path, monkeypatch)
    assert (code, report["findings"]) == (0, [])


def test_fail_on_never_reports_but_exits_zero(repo, tmp_path, monkeypatch):
    commit(repo, {"tests/test_x.py": None})
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main(["tamper", "--base", "main", "-C", str(repo), "--fail-on", "never"]) == 0


def test_step_summary_is_appended(repo, tmp_path, monkeypatch):
    commit(repo, {"tests/test_x.py": None})
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    main(["tamper", "--base", "main", "-C", str(repo)])
    assert "Backpressure tamper gate: Fail" in summary.read_text()


def test_bad_ref_exits_two(repo, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main(["tamper", "--base", "nope", "-C", str(repo)]) == 2


def test_missing_base_explains_the_fix(repo, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main(["tamper", "--base", "", "-C", str(repo)]) == 2
    assert "fetch-depth: 0" in capsys.readouterr().err
