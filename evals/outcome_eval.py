"""Deterministic grading for observable outcomes; deliberately makes no model calls."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CASES = Path(__file__).with_name("outcome_cases.json")


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """Load and sanity-check the small JSON case catalogue."""
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("case catalogue must be a non-empty JSON array")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not all(k in case for k in ("id", "group", "prompt", "expected", "weight")):
            raise ValueError("each case needs id, group, prompt, expected, and weight")
        if not isinstance(case["id"], str) or case["id"] in ids:
            raise ValueError(f"invalid or duplicate case id: {case.get('id')!r}")
        ids.add(case["id"])
        if not isinstance(case["weight"], (int, float)) or isinstance(case["weight"], bool) or case["weight"] <= 0:
            raise ValueError(f"case {case['id']}: weight must be positive")
        if case["expected"].get("kind") not in {"exact", "contains", "tool_call"}:
            raise ValueError(f"case {case['id']}: unsupported expected outcome")
    return cases


def grade_case(case: dict[str, Any], observed: dict[str, Any] | None) -> bool:
    """Compare a response or structured tool call with its observable expectation."""
    if observed is None:
        return False
    expected = case["expected"]
    kind = expected["kind"]
    if kind in {"exact", "contains"}:
        response = observed.get("response")
        if not isinstance(response, str):
            return False
        actual = " ".join(response.strip().split()).casefold()
        target = " ".join(str(expected["value"]).strip().split()).casefold()
        return actual == target if kind == "exact" else target in actual
    call = observed.get("tool_call")
    if not isinstance(call, dict) or call.get("name") != expected.get("name"):
        return False
    actual_args = call.get("arguments")
    return isinstance(actual_args, dict) and all(actual_args.get(k) == v for k, v in expected.get("arguments", {}).items())


def score(cases: Iterable[dict[str, Any]], observations: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Score JSONL-style observations, counting absent expected cases as failures."""
    by_id: dict[str, dict[str, Any]] = {}
    for observation in observations:
        case_id = observation.get("id")
        if case_id in by_id:
            raise ValueError(f"duplicate observation id: {case_id!r}")
        by_id[case_id] = observation
    case_list = list(cases)
    known = {case["id"] for case in case_list}
    extras = sorted(set(by_id) - known)
    if extras:
        raise ValueError(f"unknown observation id(s): {', '.join(map(str, extras))}")
    results = []
    grouped: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    total = passed = 0.0
    for case in case_list:
        weight = float(case["weight"])
        ok = grade_case(case, by_id.get(case["id"]))
        results.append({"id": case["id"], "group": case["group"], "passed": ok, "weight": weight})
        grouped[case["group"]].append((ok, weight))
        total += weight
        passed += weight if ok else 0.0
    groups = {name: _rate(items) for name, items in sorted(grouped.items())}
    return {"passed_weight": passed, "total_weight": total, "pass_rate": passed / total if total else 0.0,
            "groups": groups, "cases": results}


def _rate(items: list[tuple[bool, float]]) -> dict[str, float | int]:
    weight = sum(w for _, w in items)
    passed_weight = sum(w for ok, w in items if ok)
    return {"passed": sum(1 for ok, _ in items if ok), "total": len(items),
            "passed_weight": passed_weight, "total_weight": weight,
            "pass_rate": passed_weight / weight if weight else 0.0}
