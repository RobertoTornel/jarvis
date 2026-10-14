"""Fast deterministic unit tests for observable outcome grading (no LLM/service)."""
import json
from pathlib import Path

import pytest

from evals.outcome_eval import grade_case, load_cases, score
from scripts.run_outcome_evals import main


def test_catalogue_loads_and_has_small_grouped_set():
    cases = load_cases()
    assert len(cases) <= 10
    assert {case["group"] for case in cases} >= {"arithmetic", "date_arithmetic", "tool_selection"}


def test_exact_and_contains_are_whitespace_and_case_insensitive():
    exact = {"expected": {"kind": "exact", "value": "42"}}
    contains = {"expected": {"kind": "contains", "value": "tomorrow"}}
    assert grade_case(exact, {"response": "  42\n"})
    assert grade_case(contains, {"response": "I will do that TOMORROW."})
    assert not grade_case(exact, {"response": "The answer is 42"})


def test_tool_call_checks_name_and_all_required_arguments():
    case = {"expected": {"kind": "tool_call", "name": "weather", "arguments": {"location": "Paris"}}}
    assert grade_case(case, {"tool_call": {"name": "weather", "arguments": {"location": "Paris", "units": "C"}}})
    assert not grade_case(case, {"tool_call": {"name": "weather", "arguments": {"location": "Rome"}}})
    assert not grade_case(case, {"tool_call": {"name": "other", "arguments": {"location": "Paris"}}})


def test_weighted_score_counts_missing_as_failure_and_groups():
    cases = [
        {"id": "a", "group": "math", "weight": 3, "expected": {"kind": "exact", "value": "4"}},
        {"id": "b", "group": "date", "weight": 1, "expected": {"kind": "exact", "value": "tomorrow"}},
    ]
    report = score(cases, [{"id": "a", "response": "4"}])
    assert report["pass_rate"] == pytest.approx(0.75)
    assert report["groups"]["math"]["pass_rate"] == 1
    assert report["groups"]["date"]["pass_rate"] == 0
    assert report["cases"][1]["passed"] is False


def test_duplicate_and_unknown_observations_are_rejected():
    case = {"id": "a", "group": "math", "weight": 1, "expected": {"kind": "exact", "value": "4"}}
    with pytest.raises(ValueError, match="duplicate observation"):
        score([case], [{"id": "a"}, {"id": "a"}])
    with pytest.raises(ValueError, match="unknown observation"):
        score([case], [{"id": "b"}])


def test_invalid_catalogue_is_rejected(tmp_path: Path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"id": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="each case needs"):
        load_cases(path)


def test_runner_fails_regression_and_prints_report(tmp_path: Path, capsys):
    results = tmp_path / "observations.jsonl"
    results.write_text('{"id":"arith-01","response":"not 42"}\n', encoding="utf-8")
    assert main(["--results", str(results)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["pass_rate"] < 1
    assert report["total_weight"] == 8
