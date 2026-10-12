#!/usr/bin/env python3
"""Re-point doc anchors after code moves, using git to map line numbers exactly.

Hand re-anchoring is guesswork with a good chance of being wrong in a way that
still looks verified. The exact answer is available: diff the file between the
commit at which the anchors were last known good and the working tree, and map
each cited line through the unchanged lines of that diff. A citation into a line
that was *changed* has no mapping and is reported for a human to re-read, which
is the right outcome - the sentence may now be describing something else.

Usage:
    python3 scripts/reanchor_doc_anchors.py --since <commit> [--apply]

Without --apply it prints the edits it would make and changes nothing.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_LINK = re.compile(r"\]\(([^)#\s]+)#L(\d+)\)")
_NAMED = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|sh|md|json|service|desktop)):(\d+)`")
_BARE = re.compile(r"`:(\d+)`")
_NAME_IN_LINE = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:py|sh|md|json|service|desktop))(?::\d+)?`"
)
_SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})`")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def build_maps(since: str) -> dict[str, dict[int, int]]:
    """old line -> new line, for lines that still exist, per changed file."""
    changed = [p for p in git("diff", "--name-only", f"{since}..HEAD").split() if p]
    maps: dict[str, dict[int, int]] = {}
    for rel in changed:
        try:
            old = git("show", f"{since}:{rel}").splitlines()
        except subprocess.CalledProcessError:
            continue  # added since the baseline: nothing to map
        path = ROOT / rel
        if not path.is_file():
            continue  # deleted since the baseline
        new = path.read_text(errors="replace").splitlines()
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        mapping: dict[int, int | None] = {}
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(i2 - i1):
                    mapping[i1 + offset + 1] = j1 + offset + 1
            elif tag == "replace":
                # The cited line's content changed, but its position in the file
                # corresponds to this one. For a generated file this is the
                # common case: a key was inserted and everything shifted, which
                # the identity-only map reported as unmappable 37 times.
                span = max(j2 - j1, 1)
                for offset in range(i2 - i1):
                    mapping[i1 + offset + 1] = j1 + min(offset, span - 1) + 1
            elif tag == "delete":
                # The cited thing is gone; only a human can say what replaces it.
                for offset in range(i2 - i1):
                    mapping[i1 + offset + 1] = None
        maps[rel] = mapping
    return maps


def resolve(name: str, doc: Path, links: list[str]) -> str | None:
    for candidate in links:
        if Path(candidate).name == Path(name).name:
            return str((doc.parent / candidate).resolve().relative_to(ROOT))
    for candidate in (doc.parent / name, ROOT / name):
        if candidate.is_file():
            try:
                return str(candidate.resolve().relative_to(ROOT))
            except ValueError:
                return None
    # `build/lib/claire/...` is a stale copy of the package and makes every
    # basename ambiguous, which silently skipped whole files.
    hits = [
        p for p in ROOT.rglob(Path(name).name)
        if p.is_file() and not ({"__pycache__", ".venv", "build", "node_modules", "dist"} & set(p.parts))
    ]
    return str(hits[0].relative_to(ROOT)) if len(hits) == 1 else None


def main() -> int:
    if "--since" not in sys.argv:
        print(__doc__)
        return 2
    since = sys.argv[sys.argv.index("--since") + 1]
    apply = "--apply" in sys.argv
    force = "--force" in sys.argv
    maps = build_maps(since)

    docs = sorted(ROOT.glob("docs/**/*.md")) + sorted(ROOT.glob("src/**/*.spec.md"))
    edits = 0
    unmapped: list[str] = []

    for doc in docs:
        # This tool maps *baseline* line numbers to current ones, so running it
        # twice shifts the same citation twice. Refuse to touch a document that
        # already has uncommitted changes: either they are a previous run (and
        # this one would corrupt them) or they are prose a human should review
        # first.
        dirty = subprocess.run(
            ["git", "diff", "--quiet", "--", str(doc.relative_to(ROOT))], cwd=ROOT
        ).returncode
        if dirty and not force:
            print(f"skip (uncommitted changes, use --force to override): "
                  f"{doc.relative_to(ROOT)}")
            continue
        lines = doc.read_text(errors="replace").splitlines(keepends=True)
        paragraph_files: list[str] = []
        output: list[str] = []
        for number, line in enumerate(lines, 1):
            links = [m.group(1) for m in _LINK.finditer(line)]
            named = [(m.group(1), m.start()) for m in _NAME_IN_LINE.finditer(line)]
            paragraph_files.extend(name for name, _ in named)
            if not line.strip():
                paragraph_files = []
            rel_doc = doc.relative_to(ROOT).as_posix()

            def remap(start: int, end: int, name: str, column: int) -> str:
                """Return the replacement text for one anchor, or "" to leave it."""
                nonlocal edits
                target = resolve(name, doc, links)
                if target is None or target not in maps:
                    return ""
                old = int(line[start:end])
                new = maps[target].get(old)
                if new is None:
                    # Either the line was deleted, or the file has no such line
                    # any more. Both need a human; neither may be rewritten.
                    unmapped.append(
                        f"{rel_doc}:{number}: {target}:{old} has no mapped line"
                    )
                    return ""
                if new != old:
                    edits += 1
                    print(f"  {rel_doc}:{number}: {target}:{old} -> {new}")
                return line[start:end].replace(str(old), str(new), 1)

            # Links and named anchors carry their own file.
            for match in reversed(list(_LINK.finditer(line))):
                if match.group(1).endswith((".py", ".sh", ".service", ".desktop")):
                    replacement = remap(
                        match.start(2), match.end(2), match.group(1), match.start()
                    )
                    if replacement:
                        line = line[:match.start(2)] + replacement + line[match.end(2):]
            for match in reversed(list(_NAMED.finditer(line))):
                replacement = remap(
                    match.start(2), match.end(2), match.group(1), match.start()
                )
                if replacement:
                    line = line[:match.start(2)] + replacement + line[match.end(2):]
            # Bare continuations belong to the last file named on the line.
            # A bare `:NNN` may only inherit a file named on this line, or one
            # named earlier in the paragraph *when the paragraph names only
            # one* - a continuation that wraps is still unambiguous then. When
            # two files are in play the honest answer is to report it.
            def owner_for(column: int) -> str | None:
                """The file a bare `:NNN` belongs to: the nearest one *before* it.

                NOT the last one on the line. A row like
                `server.py:269`, `:270` ... (`:4161`, `:4167`), `start.sh:431`
                puts start.sh last, and attributing the bare numbers to it
                shifted them through the wrong file entirely.
                """
                earlier = [name for name, at in named if at < column]
                if earlier:
                    return earlier[-1]
                if len(set(paragraph_files)) == 1:
                    return paragraph_files[-1]
                return None
            if _BARE.search(line):
                for match in reversed(list(_BARE.finditer(line))):
                    owner = owner_for(match.start())
                    if not owner:
                        continue
                    replacement = remap(
                        match.start(1), match.end(1), owner, match.start()
                    )
                    if replacement:
                        line = line[:match.start(1)] + replacement + line[match.end(1):]
            output.append(line)

        text = "".join(output)
        if apply and text != doc.read_text(errors="replace"):
            doc.write_text(text)

    print(f"\nanchors re-pointed: {edits}")
    if unmapped:
        print(f"need a human ({len(unmapped)}):")
        for item in unmapped:
            print("  " + item)
    if not apply:
        print("\n(dry run - pass --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
