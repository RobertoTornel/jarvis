"""Command-line entry point for Claire.

Claire is a standalone application: ``python -m claire`` and the ``claire``
console script both land here and hand off to the daemon.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the ``claire`` command."""
    parser = argparse.ArgumentParser(
        prog="claire",
        description="Claire — a standalone, always-on Linux voice assistant.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Start the daemon, verify core subsystems, then exit.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the Claire version and exit.",
    )
    parser.add_argument(
        "--no-tune",
        action="store_true",
        help=(
            "Silence the thinking tune (the pinging pad that plays while Claire "
            "thinks). Her voice is unaffected. Same as CLAIRE_TUNE_ENABLED=0, "
            "which is the switch to use for the installed service."
        ),
    )
    return parser


def _version() -> str:
    """Return the installed version, or a placeholder when unbuilt."""
    try:
        from ._version import VERSION  # type: ignore[attr-defined]
    except Exception:
        return "unreleased"
    return str(VERSION)


def main(argv: list[str] | None = None) -> int:
    """Run the Claire CLI. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    if args.version:
        print(_version())
        return 0

    from .daemon import main as daemon_main

    daemon_main(smoke_test=args.smoke_test, no_tune=args.no_tune)
    return 0


if __name__ == "__main__":
    sys.exit(main())
