"""Holdouts against real Docker: run apart from the model's container, and a broken behaviour is caught."""

import subprocess
import zipfile
from pathlib import Path

import pytest

from backpressure.review.holdouts import run_holdouts
from backpressure.review.sandbox import Sandbox, docker_available

pytestmark = [pytest.mark.docker, pytest.mark.skipif(not docker_available(), reason="Docker is not running")]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_holdouts_run_in_isolation_and_catch_a_regression(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")
    # A rigging attempt: make every test report as passed at interpreter start-up.
    (repo / "sitecustomize.py").write_text(
        "import _pytest.reports as r\n_orig = r.TestReport.from_item_and_call\n"
        "def _fake(item, call):\n    rep = _orig(item, call)\n    rep.outcome = 'passed'\n    return rep\n"
        "r.TestReport.from_item_and_call = staticmethod(_fake)\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    head = git(repo, "rev-parse", "HEAD")

    holdouts = tmp_path / "holdouts"
    holdouts.mkdir()
    (holdouts / "test_calc.py").write_text(
        "from calc import add, mul\n\ndef test_add():\n    assert add(2, 3) == 5\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
    )

    with Sandbox(repo) as box:
        result = run_holdouts(box, holdouts, base, head)
        assert box.exec("ls /holdouts").exit_code != 0, "the model's container must not see the holdouts"
        assert box.exec("git rev-parse HEAD").output.strip() == head, "holdout runs must not move the model's checkout"

    assert result.ran
    assert (result.regressed, result.passing) == (1, 1)
    assert result.detail["test_calc::test_add"] == "regressed"


# One line, run by site.py for every interpreter that sees this venv: every test report becomes "passed".
RIG_PTH = (
    "import _pytest.reports as r; o = r.TestReport.from_item_and_call; "
    "r.TestReport.from_item_and_call = staticmethod("
    "lambda item, call, o=o: (lambda rep: (setattr(rep, 'outcome', 'passed'), rep)[1])(o(item, call)))\n"
)


def rigging_wheel(path: Path) -> None:
    """A wheel a PR could add as a dependency: installing it drops a .pth start-up hook into the venv."""
    info = "rigger-1.0.dist-info"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("rigger.pth", RIG_PTH)
        z.writestr(f"{info}/METADATA", "Metadata-Version: 2.1\nName: rigger\nVersion: 1.0\n")
        z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{info}/RECORD", "")


def make_repo(tmp_path: Path, src_layout: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    pkg = repo / "src" if src_layout else repo
    pkg.mkdir(exist_ok=True)
    (pkg / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def commit(repo: Path) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    return git(repo, "rev-parse", "HEAD")


def write_holdout(tmp_path: Path) -> Path:
    holdouts = tmp_path / "holdouts"
    holdouts.mkdir()
    (holdouts / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    return holdouts


def test_a_dependency_added_by_the_pr_cannot_rig_the_holdouts(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "vendor").mkdir()
    rigging_wheel(repo / "vendor" / "rigger-1.0-py3-none-any.whl")
    (repo / "requirements.txt").write_text("./vendor/rigger-1.0-py3-none-any.whl\n")
    head = commit(repo)

    with Sandbox(repo) as box:
        # Control: the PR's own venv really is rigged, so the test below proves something.
        assert "rigger.pth" in box.exec("ls /work/.venv/lib/python3.12/site-packages").output
        result = run_holdouts(box, write_holdout(tmp_path), base, head)

    assert result.ran, result.note
    assert (result.regressed, result.passing) == (1, 0)


def test_src_layout_is_reached_by_path(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, src_layout=True)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    head = commit(repo)

    with Sandbox(repo) as box:
        result = run_holdouts(box, write_holdout(tmp_path), base, head)

    assert (result.ran, result.regressed, result.passing) == (True, 1, 0)


def test_a_new_head_dependency_fails_visibly_with_a_note(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text("import tomli_w\n\ndef add(a, b):\n    return a + b\n")
    (repo / "requirements.txt").write_text("tomli-w\n")
    head = commit(repo)

    with Sandbox(repo) as box:
        result = run_holdouts(box, write_holdout(tmp_path), base, head)

    assert result.ran, result.note
    assert result.regressed == 1
    assert "dependenc" in result.note
    assert "dependenc" in result.summary()


def test_a_root_install_cannot_rig_the_holdouts_through_the_image(tmp_path: Path) -> None:
    """The head's setup.py runs as root in the setup container and can write into the stdlib there."""
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    rig = (
        "import _pytest.reports as r\n_o = r.TestReport.from_item_and_call\n"
        "def _f(item, call):\n    rep = _o(item, call)\n    rep.outcome = 'passed'\n    return rep\n"
        "r.TestReport.from_item_and_call = staticmethod(_f)\n"
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "setup.py").write_text(
        "import sysconfig, pathlib\n"
        f"pathlib.Path(sysconfig.get_paths()['stdlib'], 'sitecustomize.py').write_text({rig!r})\n"
        "from setuptools import setup\nsetup(name='calc', version='0', py_modules=['calc'])\n"
    )
    head = commit(repo)

    with Sandbox(repo) as box:
        # Control: the setup image really carries the rig.
        assert "sitecustomize.py" in box.exec("ls /usr/local/lib/python3.12").output
        result = run_holdouts(box, write_holdout(tmp_path), base, head)

    assert result.ran, result.note
    assert (result.regressed, result.passing) == (1, 0)
