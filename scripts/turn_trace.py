#!/usr/bin/env python3
"""Read Claire's per-turn latency records: why was that turn slow?

One JSON object per turn is appended to ``~/.local/share/claire/turns.jsonl``
by :mod:`claire.turn_trace` (override the path with ``CLAIRE_TURN_TRACE_PATH``).
This is the reader, so the answer to "why was that turn slow" is a command
rather than a bespoke probe script.

    scripts/turn_trace.py                 # the last 20 turns
    scripts/turn_trace.py --last 50
    scripts/turn_trace.py --slowest       # the slowest turns by total time
    scripts/turn_trace.py --stage router  # the turns whose router ran longest
    scripts/turn_trace.py --check         # missing / out-of-order stages

Nothing here writes: it reads the file and prints. ``--check`` exits non-zero
when a selected record is missing a stage or has its stages out of order, so it
can be used as a one-line assertion after driving a turn.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from claire import turn_trace  # noqa: E402

_STAGE_ALIASES = {
    "router": "router",
    "router_start": "router",
    "planner": "planner",
    "planner_start": "planner",
    "llm": "llm",
    "llm_last_token": "llm",
    "speech": "first_audio_byte",
    "audio": "first_audio_byte",
    "first_audio_byte": "first_audio_byte",
    "transcription": "transcription_end",
    "stt": "transcription_end",
    "transcription_end": "transcription_end",
    "total": "total",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read Claire's per-turn latency records.",
    )
    parser.add_argument("--last", type=int, default=None,
                        help="show the N most recent turns (default 20)")
    parser.add_argument("--slowest", type=int, nargs="?", const=10, default=None,
                        metavar="N",
                        help="show the N slowest turns by total time (default 10)")
    parser.add_argument("--stage", default=None,
                        help="rank turns by the time spent in one stage "
                             "(router, planner, llm, speech, transcription)")
    parser.add_argument("--path", default=None,
                        help="read this file instead of the configured one")
    parser.add_argument("--json", action="store_true",
                        help="print the raw records instead of the one-line view")
    parser.add_argument("--check", action="store_true",
                        help="report missing or out-of-order stages (exit 1 if any)")
    return parser


def _sort_key(record, stage):
    if stage == "total":
        value = record.get("total_s")
    else:
        value = turn_trace.stage_span(record, stage)
    return -1.0 if value is None else float(value)


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    path = Path(args.path).expanduser() if args.path else None
    records = turn_trace.read_records(path=path)

    stage = _STAGE_ALIASES.get((args.stage or "").strip().lower()) if args.stage else None
    if args.stage and stage is None:
        print(f"  ⚠️ unknown stage {args.stage!r}; expected one of "
              f"{sorted(set(_STAGE_ALIASES))}", file=sys.stderr)
        return 2

    if args.slowest is not None:
        records = sorted(records, key=lambda item: _sort_key(item, "total"))[: max(0, args.slowest)]
    elif stage is not None:
        records = sorted(records, key=lambda item: _sort_key(item, stage))[: 10]
    else:
        limit = 20 if args.last is None else max(0, args.last)
        records = records[-limit:] if limit else []

    if not records:
        print(f"  📭 no turn records yet at {path or turn_trace.trace_path()}")
        return 0

    problems: list[str] = []
    if args.json:
        for record in records:
            print(json.dumps(record, sort_keys=True))
    else:
        header = "the slowest turns" if args.slowest is not None else (
            f"turns ranked by {args.stage} time" if stage else "the last turns")
        print(f"  ⏱️  turn trace: {header} "
              f"({len(records)} of {len(turn_trace.read_records(path=path))} records)")
        for record in records:
            print(f"    {turn_trace.format_record_line(record)}")
            if args.check:
                for problem in turn_trace.order_problems(record):
                    problems.append(f"{record.get('turn_id')}: {problem}")
        if args.check:
            if problems:
                print("  ❌ stage problems:")
                for problem in problems:
                    print(f"    - {problem}")
                return 1
            print("  ✅ every selected record has all stages, in order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
