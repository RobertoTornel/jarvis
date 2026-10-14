#!/usr/bin/env python3
"""Score recorded outcomes against evals/outcome_cases.json; performs no inference."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.outcome_eval import DEFAULT_CASES, load_cases, score  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="JSON case catalogue")
    parser.add_argument("--results", type=Path, help="JSONL observations to grade")
    parser.add_argument("--show-cases", action="store_true", help="print prompts and expected outcomes as JSON")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    if args.show_cases:
        print(json.dumps(cases, indent=2, ensure_ascii=False))
        return 0
    if args.results is None:
        parser.error("provide --results or use --show-cases")
    observations = []
    for line_number, line in enumerate(args.results.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                observations.append(json.loads(line))
            except json.JSONDecodeError as exc:
                parser.error(f"{args.results}:{line_number}: invalid JSON: {exc}")
    report = score(cases, observations)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
