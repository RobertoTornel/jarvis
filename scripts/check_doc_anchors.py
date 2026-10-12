#!/usr/bin/env python3
"""Verify that the docs' `file:line` anchors still point where they claim.

An anchor is only ever correct *relative to a commit*: move a function and every
citation below it is wrong, silently, in prose nobody re-reads. The docs have
been re-anchored twice by hand (about 260 numbers across 11 files), and both
times the drift came back within a day because more code moved afterwards.

This reports; it never rewrites. Rewriting needs a human judgement about what
the sentence meant, and a tool that guesses wrong is worse than a stale number
because it launders the mistake into looking verified.

Forms understood, all of which appear in this repository:

* a markdown link with a line fragment - ``[label](../src/claire/x.py#L123)``
* a backticked name and line - ``` `x.py:123` ```
* bare continuations after a named file - ``` `x.py` ... (`:123`, `:456`) ```

Two checks are applied. A line number beyond the end of its file is a hard
failure, always. A backticked *symbol* near the anchor that does not appear
within a couple of lines of the cited one is reported as suspicious, because
that is the shape every drift has taken so far.

Only one thing here is objective: a line number past the end of its file. That
is a hard failure and sets the exit status, so this can be wired into a gate.
Everything else is advisory. The tool cannot always tell which file a bare
`:123` belongs to, and its symbol check is a heuristic, so the advisory list is
for a human to triage - do not "fix" the docs to satisfy it, and do not treat a
long advisory list as a broken tree.

Usage:  python3 scripts/check_doc_anchors.py [--all] [--verbose]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Files whose text carries anchors.
def _doc_files() -> list[Path]:
    files = sorted(ROOT.glob("docs/**/*.md"))
    files += sorted(ROOT.glob("src/**/*.spec.md"))
    readme = ROOT / "README.md"
    if readme.is_file():
        files.append(readme)
    return [f for f in files if f.is_file()]

def _link_label(line: str, match: re.Match) -> str:
    """The visible label of a markdown link whose target this match is."""
    start = line.rfind("[", 0, match.start())
    return line[start + 1:match.start() - 1] if start != -1 else ""


_LINK = re.compile(r"\]\(([^)#\s]+)#L(\d+)\)")
_NAMED = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|sh|md|json|service|desktop)):(\d+)`")
_BARE = re.compile(r"`:(\d+)`")
_NAME_IN_LINE = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:py|sh|md|json|service|desktop))(?::\d+)?`"
)
# A backticked symbol often carries a suffix: `_env_flag()`, `Settings()`,
# `x.method()`. Not allowing the suffix made the checker take the *previous*
# symbol on the line as the claim and report true anchors as suspicious.
_SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{4,})(?:\(\))?`")

#: Words that are configuration keys, classes or prose, not symbols to locate.
_NOT_A_SYMBOL = {
    "noise_gate_min_rms", "noise_gate_margin", "noise_gate_enabled", "min_speech_ms",
    "vad_aggressiveness", "voice_min_energy", "tts_engine", "stt_engine", "sample_rate",
    "endpoint_silence_ms", "vad_pre_roll_ms", "max_utterance_ms", "voice_debug",
    "wake_word", "fish", "piper", "chatterbox", "parakeet", "whisper", "gemini",
    "true", "false", "null", "json", "http", "https", "systemd", "pytest", "python",
    "unit", "docs", "tests", "src", "scripts", "examples", "default", "defaults",
}


_SUFFIXES = (".py", ".sh", ".md", ".json", ".service", ".desktop")

#: Directories that must never be treated as a source of documentation anchors.
_SKIP_PARTS = {"__pycache__", ".git", ".venv", "build", "node_modules", "dist"}

def _build_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _SUFFIXES:
            continue
        # `build/lib/claire/...` is a stale copy of the package: it makes every
        # basename ambiguous, so `config.py` resolved to nothing and the audit
        # drowned in 963 "cannot resolve" lines.
        if _SKIP_PARTS & set(path.parts):
            continue
        index.setdefault(path.name, []).append(path)
    return index

_INDEX: dict[str, list[Path]] = _build_index()

def _resolve(name: str, doc: Path, same_line_links: list[str]) -> Path | None:
    """Find the file an anchor names, preferring one linked on the same line."""
    for candidate in same_line_links:
        if Path(candidate).name == Path(name).name:
            resolved = (doc.parent / candidate).resolve()
            if resolved.is_file():
                return resolved
    direct = (doc.parent / name).resolve()
    if direct.is_file():
        return direct
    direct = (ROOT / name).resolve()
    if direct.is_file():
        return direct
    hits = _INDEX.get(Path(name).name, [])
    return hits[0] if len(hits) == 1 else None


def main() -> int:
    verbose = "--verbose" in sys.argv
    show_all = "--all" in sys.argv
    hard: list[str] = []
    advisory: list[str] = []
    checked = 0

    for doc in _doc_files():
        lines = doc.read_text(errors="replace").splitlines()
        paragraph_files: list[str] = []
        for index, line in enumerate(lines, 1):
            rel = doc.relative_to(ROOT)
            link_targets = [m.group(1) for m in _LINK.finditer(line)]
            targets: list[tuple[str, int, str]] = []
            for match in _LINK.finditer(line):
                targets.append((match.group(1), int(match.group(2)), "link", match.start(), _link_label(line, match)))
            for match in _NAMED.finditer(line):
                targets.append((match.group(1), int(match.group(2)), "named", match.start(), ""))
            names = _NAME_IN_LINE.findall(line)
            paragraph_files.extend(names)
            if not line.strip():
                paragraph_files = []

            def owner_for(column: int) -> str | None:
                """Nearest file mention *before* a bare `:NNN`, not the last one."""
                earlier = [
                    m.group(1) for m in _NAME_IN_LINE.finditer(line) if m.start() < column
                ]
                if earlier:
                    return earlier[-1]
                if len(set(paragraph_files)) == 1:
                    return paragraph_files[-1]
                return None

            def claimed_symbol(column: int, label: str) -> list[str]:
                """The identifier this anchor's sentence attaches to it.

                Only the *nearest preceding* backticked symbol counts. A line
                that lists five settings and cites one line each is five
                separate claims, not one claim that all five live on every
                line - requiring all of them is how the first version of this
                checker reported 330 false positives.
                """
                for token in _SYMBOL.findall(label):
                    if token not in _NOT_A_SYMBOL and not token.endswith((".py", ".md", ".sh")):
                        return [token]
                before = line[:column]
                found = [
                    s for s in _SYMBOL.findall(before)
                    if s not in _NOT_A_SYMBOL and not s.endswith((".py", ".md", ".sh"))
                ]
                return found[-1:]

            # Bare `:NNN` continuations, attributed to the nearest file named
            # before them on this line, or to the paragraph when it names
            # exactly one file. Ambiguity is reported, never guessed.
            for match in _BARE.finditer(line):
                owner = owner_for(match.start())
                if owner:
                    targets.append(
                        (owner, int(match.group(1)), "bare", match.start(), "")
                    )
                else:
                    advisory.append(
                        f"{rel}:{index}: `:{match.group(1)}` has no file to belong to"
                    )

            for name, number, kind, column, label in targets:
                path = _resolve(name, doc, link_targets)
                if path is None:
                    advisory.append(f"{rel}:{index}: cannot resolve `{name}`")
                    continue
                body = path.read_text(errors="replace").splitlines()
                checked += 1
                if not 1 <= number <= len(body):
                    hard.append(
                        f"{rel}:{index}: {name}:{number} is beyond EOF ({len(body)} lines)"
                    )
                    continue
                window = "\n".join(body[max(0, number - 3):number + 2])
                missing = [s for s in claimed_symbol(column, label) if s not in window]
                if missing:
                    where = [
                        f"{s}@{n}" for s in missing
                        for n, text in enumerate(body, 1) if s in text
                    ]
                    advisory.append(
                        f"{rel}:{index}: {name}:{number} ({kind}) - "
                        f"{', '.join(missing)} not near that line"
                        + (f"; found {', '.join(where[:4])}" if where else "; not in file")
                    )
                elif verbose:
                    print(f"ok   {rel}:{index} {name}:{number} -> {body[number-1].strip()[:60]}")

    print(f"anchors checked: {checked}")
    if advisory:
        limit = len(advisory) if show_all else 15
        print(f"\nadvisory ({len(advisory)}, showing {min(limit, len(advisory))}; --all for every line):")
        for line in advisory[:limit]:
            print("  " + line)
    if hard:
        print(f"\npast end of file ({len(hard)}):")
        for line in hard:
            print("  " + line)
        return 1
    if not advisory:
        print("every anchor resolves and names what its sentence claims")
    else:
        print("\nno anchor points past the end of its file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
