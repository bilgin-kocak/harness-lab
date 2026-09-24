from harnesslab.experiments.spec import load_suite
from harnesslab.harness.lint import (
    EditConstraints,
    SuiteSecrets,
    changed_paths,
    lint_candidate,
)
from tests.conftest import DEMO_SUITE


def _secrets() -> SuiteSecrets:
    _, tasks = load_suite(DEMO_SUITE)
    return SuiteSecrets.from_tasks(tasks)


def test_secrets_from_demo_suite():
    s = _secrets()
    assert "fix-month-boundary" in s.task_ids
    assert "test_hidden_budgets" in s.hidden_names and "test_hidden_budgets.py" in s.hidden_names
    assert "score_budgets" in s.hidden_names
    assert any(len(line) >= 24 for line in s.hidden_lines)
    assert not any(line.startswith(("import ", "from ")) for line in s.hidden_lines)
    assert s.scrub("FAIL: test_x (test_hidden_budgets.T)") == "FAIL: test_x ([hidden-test].T)"


def test_changed_paths_ignores_identical_content():
    cur = {"system_prompt.md": "a\nb", "fake.yaml": ""}
    assert changed_paths(cur, dict(cur)) == []
    assert changed_paths(cur, {**cur, "system_prompt.md": "b\na"}) == ["system_prompt.md"]
    added = {**cur, "skills/x/SKILL.md": "---\nname: x\n---\n"}
    assert changed_paths(cur, added) == ["skills/x/SKILL.md"]


def test_lint_rejects_leaks_and_rule_violations():
    s = _secrets()
    c = EditConstraints(max_files=1)
    cur = {"system_prompt.md": "Run tests.", "fake.yaml": ""}
    assert lint_candidate(cur, dict(cur), s, c) == []
    assert any("deleted" in e for e in lint_candidate(cur, {"fake.yaml": ""}, s, c))
    with_id = {**cur, "system_prompt.md": "solve fix-month-boundary"}
    assert any("task id" in e for e in lint_candidate(cur, with_id, s, c))
    fake_ok = {**cur, "fake.yaml": "solve_tasks: [fix-month-boundary]"}
    assert lint_candidate(cur, fake_ok, s, c) == []
    with_name = {**cur, "system_prompt.md": "see test_hidden_budgets"}
    assert any("hidden" in e for e in lint_candidate(cur, with_name, s, c))
    verbatim = next(iter(s.hidden_lines))
    copied = {**cur, "system_prompt.md": f"x\n{verbatim}\n"}
    assert any("verbatim" in e for e in lint_candidate(cur, copied, s, c))
    two = {**cur, "system_prompt.md": "y", "skills/a/SKILL.md": "---\nname: a\n---\nz"}
    assert any("max_files" in e for e in lint_candidate(cur, two, s, c))
    manifest = {**cur, "harness.yaml": "name: x"}
    assert any("harness.yaml" in e for e in lint_candidate(cur, manifest, s, c))
    escape = {**cur, "../x": "y"}
    assert any("not allowed" in e for e in lint_candidate(cur, escape, s, EditConstraints()))


def _line_with_identifier(s: SuiteSecrets) -> str:
    """A hidden line that also contains a hidden name: the hardest case for the scrub."""
    return next(
        line for line in sorted(s.hidden_lines) if any(name in line for name in s.hidden_names)
    )


def test_scrub_removes_hidden_source_lines_from_tracebacks():
    s = _secrets()
    line = _line_with_identifier(s)
    traceback = (
        f'  File "tests/test_hidden_budgets.py", line 39, in test_x\n    {line}\nAssertionError'
    )
    scrubbed = s.scrub(traceback)
    assert line not in scrubbed and "[hidden-test]" in scrubbed
    assert "[hidden-test-line]" in scrubbed and scrubbed.endswith("AssertionError")
    assert s.scrub("plain text stays") == "plain text stays"


def test_secrets_harvest_hidden_test_identifiers():
    s = _secrets()
    assert "test_january_includes_last_day" in s.hidden_names
    assert "InclusiveRangeTests" in s.hidden_names and "BudgetModuleTests" in s.hidden_names
    assert "ledger" not in s.hidden_names  # plain helpers are not test identifiers
    assert s.scrub("ledgerlite/report.py") == "ledgerlite/report.py"
    out = s.scrub(
        "test_january_includes_last_day (test_hidden_month_boundary.InclusiveRangeTests) ... FAIL"
    )
    assert "InclusiveRangeTests" not in out and "test_january" not in out and "FAIL" in out


def test_scrub_handles_pytest_traceback_markers():
    s = _secrets()
    line = _line_with_identifier(s)
    pytest_style = (
        f"tests/x.py:12: in test_x\n>       {line}\nE       {line}\nE         where 1 = f()\n"
    )
    out = s.scrub(pytest_style)
    assert line not in out and out.count("[hidden-test-line]") == 2 and "where 1 = f()" in out


def test_lint_messages_do_not_echo_hidden_names():
    s = _secrets()
    cur = {"system_prompt.md": "Run tests.", "fake.yaml": ""}
    errors = lint_candidate(
        cur, {**cur, "system_prompt.md": "see test_hidden_budgets"}, s, EditConstraints()
    )
    joined = " ".join(errors)
    assert "test_hidden_budgets" not in joined and "[hidden-test]" in joined


def test_view_scrubs_before_truncating():
    from harnesslab.grow.view import capped_scrub

    s = _secrets()
    text = "a" * 10 + " test_hidden_budgets tail"
    assert "test_hidden" not in capped_scrub(s, text, 20)
    assert "test_hidden" not in capped_scrub(s, text, 12, tail=True)
    assert capped_scrub(s, "short", 100) == "short"
