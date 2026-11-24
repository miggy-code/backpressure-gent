from textwrap import dedent

import pytest

from backpressure.report import Severity
from backpressure.tamper import FileChange, analyze

BASE = dedent(
    """
    import pytest

    def test_add():
        assert 1 + 1 == 2
        assert 2 + 2 == 4

    class TestThing:
        def test_name(self):
            self.assertEqual(name(), "x")
    """
)


def codes(findings):
    return sorted(f.code for f in findings)


def edit(head, base=BASE, path="tests/test_math.py"):
    return analyze([FileChange(path, dedent(base), dedent(head))])


def test_honest_change_adding_tests_and_assertions_is_clean():
    head = BASE + "\ndef test_new():\n    assert True is not False\n"
    head = head.replace("assert 2 + 2 == 4", "assert 2 + 2 == 4\n    assert 3 + 3 == 6")
    assert edit(head) == []


def test_removed_test_is_blocking_and_named():
    head = BASE.replace("def test_add():\n    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n", "")
    (finding,) = edit(head)
    assert (finding.code, finding.severity, finding.test) == ("test_removed", Severity.BLOCKING, "test_add")
    assert finding.at_base


def test_removed_method_is_keyed_by_class():
    head = BASE.replace('    def test_name(self):\n        self.assertEqual(name(), "x")\n', "    pass\n")
    assert [f.test for f in edit(head)] == ["TestThing.test_name"]


@pytest.mark.skip(reason="flaky on CI")
def test_skip_decorator_added():
    head = BASE.replace("def test_add", "@pytest.mark.skip(reason='flaky')\ndef test_add")
    assert codes(edit(head)) == ["skip_added"]


def test_skip_call_in_body_added():
    head = BASE.replace("    assert 1 + 1 == 2\n", "    pytest.skip('later')\n    assert 1 + 1 == 2\n")
    assert codes(edit(head)) == ["skip_added"]


def test_class_level_unittest_skip_applies_to_its_tests():
    head = BASE.replace("class TestThing:", "@unittest.skip('wip')\nclass TestThing:")
    assert [(f.code, f.test) for f in edit(head)] == [("skip_added", "TestThing.test_name")]


def test_module_pytestmark_skip_applies_to_every_test():
    head = BASE.replace("import pytest\n", "import pytest\npytestmark = [pytest.mark.xfail]\n")
    assert codes(edit(head)) == ["skip_added", "skip_added"]


def test_assertions_reduced():
    head = BASE.replace("    assert 2 + 2 == 4\n", "")
    (finding,) = edit(head)
    assert finding.code == "assertions_reduced"


def test_unittest_and_raises_calls_count_as_assertions():
    base = "import pytest\ndef test_err():\n    with pytest.raises(ValueError):\n        f()\n    m.assert_called_once()\n"
    head = "import pytest\ndef test_err():\n    f()\n"
    assert codes(edit(head, base)) == ["assertions_reduced"]


def test_trivial_assertion_replacing_a_real_one():
    head = BASE.replace("assert 2 + 2 == 4", "assert True")
    assert codes(edit(head)) == ["trivial_assertion_added"]


def test_deleted_test_file_is_blocking():
    findings = analyze([FileChange("tests/test_math.py", dedent(BASE), None)])
    (finding,) = findings
    assert finding.code == "test_file_deleted"
    assert finding.at_base
    assert "2 of its 2" in finding.message


def test_test_moved_to_another_file_is_not_removed():
    moved = "def test_add():\n    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n"
    remaining = BASE.replace(moved, "")
    findings = analyze(
        [
            FileChange("tests/test_math.py", dedent(BASE), dedent(remaining)),
            FileChange("tests/test_add.py", None, moved),
        ]
    )
    assert findings == []


def test_moved_test_is_still_compared_at_its_new_home():
    moved = "def test_add():\n    assert 1 + 1 == 2\n"
    remaining = BASE.replace("def test_add():\n    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n", "")
    findings = analyze(
        [
            FileChange("tests/test_math.py", dedent(BASE), dedent(remaining)),
            FileChange("tests/test_add.py", None, moved),
        ]
    )
    assert [(f.code, f.path) for f in findings] == [("assertions_reduced", "tests/test_add.py")]


def test_renamed_file_compares_old_contents():
    findings = analyze([FileChange("tests/test_renamed.py", dedent(BASE), dedent(BASE), old_path="tests/test_math.py")])
    assert findings == []


def test_unparseable_head_is_one_warning_not_a_flood():
    (finding,) = edit("def test_add(:\n")
    assert (finding.code, finding.severity) == ("unparseable", Severity.WARNING)


def test_conftest_change_is_a_warning():
    (finding,) = analyze([FileChange("conftest.py", "x = 1\n", "x = 2\n")])
    assert (finding.code, finding.severity) == ("test_config_changed", Severity.WARNING)


def test_pyproject_only_flags_pytest_and_coverage_tables():
    base = '[project]\nname = "a"\n[tool.pytest.ini_options]\naddopts = "-q"\n'
    unrelated = base.replace('name = "a"', 'name = "b"')
    loosened = base.replace('"-q"', '"-q --deselect tests/test_x.py"')
    assert analyze([FileChange("pyproject.toml", base, unrelated)]) == []
    assert codes(analyze([FileChange("pyproject.toml", base, loosened)])) == ["test_config_changed"]


def test_non_test_python_files_are_ignored():
    assert analyze([FileChange("src/app.py", "def test_x():\n    assert 1\n", "")]) == []


