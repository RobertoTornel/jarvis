"""One-shot import of the Markdown vault into PostgreSQL, plus a one-way export.

The vault is retired once this has run: nothing on the hot path reads ``*.md``
afterwards. Re-running the import is a true no-op — documents are keyed on
``source_path`` and chunk embeddings are only regenerated when the chunk text
actually changes, so a frontmatter-only edit never re-embeds prose.

Usage::

    python -m claire.memory.migrate_md_to_pg --dry-run
    python -m claire.memory.migrate_md_to_pg
    python -m claire.memory.migrate_md_to_pg --export /tmp/vault-backup
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import pg
from .notes import parse_frontmatter, render_frontmatter
from .pg import Chunk, MemoryStoreError

#: Sections longer than this are split further. A recommendation, not a measured
#: optimum — there are zero notes in the live vault to tune against.
MAX_SECTION_CHARS = 1500

_HEADING_RE = re.compile(r"(?m)^(#{2,3})[ \t]+(.+?)[ \t]*$")

ROUTER_FILENAME = "CLAIRE.md"


@dataclass
class ImportedDoc:
    source_path: str
    doc_kind: str
    name: str
    title: str
    body: str
    frontmatter: Dict[str, Any]
    frontmatter_raw: str
    always_loaded: bool
    budget_bytes: Optional[int]
    source_bytes: int
    source_mtime: Optional[datetime]


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: List[Tuple[str, str]] = None  # (path, reason)

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []

    def render(self) -> str:
        bits = [f"created={self.created}", f"updated={self.updated}",
                f"unchanged={self.unchanged}", f"skipped={len(self.skipped)}"]
        return " ".join(bits)


def resolve_vault_dir(explicit: Optional[Path] = None) -> Path:
    """Locate the vault, preferring an explicit path then ``CLAIRE_VAULT_DIR``."""
    if explicit is not None:
        return Path(explicit).expanduser()
    env = (os.environ.get("CLAIRE_VAULT_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from .notes import resolve_vault_dir as _resolve

        return Path(_resolve()).expanduser()
    except Exception:
        return Path("~/.local/share/claire/vault").expanduser()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_body(body: str, *, max_chars: int = MAX_SECTION_CHARS) -> List[Chunk]:
    """Split a note body at H2/H3 boundaries, then by size where needed."""
    if not body.strip():
        return []

    marks = [(m.start(), m.group(2).strip()) for m in _HEADING_RE.finditer(body)]

    sections: List[Tuple[str, int, int]] = []
    if not marks:
        sections.append(("", 0, len(body)))
    else:
        if marks[0][0] > 0:
            head = body[: marks[0][0]]
            if head.strip():
                sections.append(("", 0, marks[0][0]))
        for i, (start, heading) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
            sections.append((heading, start, end))

    chunks: List[Chunk] = []
    index = 0
    for heading, start, end in sections:
        text = body[start:end]
        if not text.strip():
            continue
        if len(text) <= max_chars:
            chunks.append(Chunk(index=index, content=text.strip(), heading=heading,
                                char_start=start, char_end=end))
            index += 1
            continue
        # Split an over-long section on blank lines, packing to max_chars.
        offset = start
        for piece in _split_long(text, max_chars):
            leading = len(piece) - len(piece.lstrip())
            chunks.append(Chunk(index=index, content=piece.strip(), heading=heading,
                                char_start=offset + leading, char_end=offset + len(piece)))
            offset += len(piece)
            index += 1
    return chunks


def _split_long(text: str, max_chars: int) -> List[str]:
    pieces: List[str] = []
    buf = ""
    for para in re.split(r"(?<=\n)\s*\n", text):
        if not para:
            continue
        if buf and len(buf) + len(para) + 2 > max_chars:
            pieces.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
        while len(buf) > max_chars:
            pieces.append(buf[:max_chars])
            buf = buf[max_chars:]
    if buf:
        pieces.append(buf)
    return pieces


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load_vault(vault_dir: Path) -> Tuple[List[ImportedDoc], List[Tuple[str, str]]]:
    """Read the router and every note. Returns ``(documents, skipped)``."""
    docs: List[ImportedDoc] = []
    skipped: List[Tuple[str, str]] = []

    router_path = vault_dir / ROUTER_FILENAME
    if router_path.is_file():
        docs.append(_load_one(router_path, vault_dir, doc_kind="router"))
    else:
        skipped.append((str(router_path), "router file not found"))

    notes_dir = vault_dir / "notes"
    if notes_dir.is_dir():
        for path in sorted(notes_dir.glob("*.md")):
            if not path.is_file():
                continue
            try:
                docs.append(_load_one(path, vault_dir, doc_kind="note"))
            except Exception as e:
                skipped.append((str(path), f"{type(e).__name__}: {e}"))
    return docs, skipped


def _load_one(path: Path, vault_dir: Path, *, doc_kind: str) -> ImportedDoc:
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    # parse_frontmatter returns the body as a suffix of the source, so the
    # verbatim frontmatter block (fences included) is whatever precedes it.
    raw = text[: len(text) - len(body)] if text.endswith(body) else ""
    rel = path.relative_to(vault_dir).as_posix()
    stem = path.stem
    title = str(meta.get("title") or "").strip() or stem
    stat = path.stat()
    return ImportedDoc(
        source_path=rel,
        doc_kind=doc_kind,
        name=stem,
        title=title,
        body=body,
        frontmatter=dict(meta),
        frontmatter_raw=raw,
        always_loaded=(doc_kind == "router") or str(meta.get("always_loaded", "")).lower() in ("true", "1", "yes"),
        budget_bytes=_as_int(meta.get("budget_bytes")),
        source_bytes=stat.st_size,
        source_mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_vault(
    vault_dir: Optional[Path] = None,
    *,
    dsn: Optional[str] = None,
    dry_run: bool = False,
    bootstrap: bool = True,
    verbose: bool = True,
) -> ImportReport:
    """Import every vault file into Postgres. Idempotent."""
    vault = resolve_vault_dir(vault_dir)
    report = ImportReport()

    if not vault.is_dir():
        report.skipped.append((str(vault), "vault directory not found"))
        if verbose:
            print(f"vault not found: {vault}")
        return report

    docs, skipped = load_vault(vault)
    report.skipped.extend(skipped)

    if dry_run:
        for doc in docs:
            chunks = chunk_body(doc.body) if doc.doc_kind == "note" else [
                Chunk(index=0, content=doc.body.strip(), heading=_first_heading(doc.body))
            ]
            # ``_safe_get`` already answers None when the database cannot be
            # reached, which is the right dry-run answer: it reports the action
            # that *would* be attempted rather than failing before printing.
            existing = _safe_get(doc.name, dsn)
            action = "update" if existing else "create"
            report.created += 1 if action == "create" else 0
            report.updated += 1 if action == "update" else 0
            print(f"[dry-run] {action:6} {doc.source_path:28} "
                  f"kind={doc.doc_kind:6} chunks={len(chunks)} body={len(doc.body.encode())}B")
        print(f"[dry-run] {report.render()} (nothing written)")
        return report

    if bootstrap:
        pg.bootstrap_schema(dsn)

    for doc in docs:
        chunks = chunk_body(doc.body) if doc.doc_kind == "note" else [
            Chunk(index=0, content=doc.body.strip(), heading=_first_heading(doc.body))
        ]
        before = _safe_get(doc.name, dsn)
        _id, inserted = pg.upsert_document(
            source_path=doc.source_path,
            doc_kind=doc.doc_kind,
            name=doc.name,
            title=doc.title,
            body=doc.body,
            frontmatter=doc.frontmatter,
            frontmatter_raw=doc.frontmatter_raw,
            always_loaded=doc.always_loaded,
            budget_bytes=doc.budget_bytes,
            source_mtime=doc.source_mtime,
            source_bytes=doc.source_bytes,
            chunks=chunks,
            dsn=dsn,
        )
        if inserted:
            report.created += 1
        elif before is not None and _unchanged(before, doc):
            report.unchanged += 1
        else:
            report.updated += 1
        if verbose:
            print(f"  {doc.source_path:28} chunks={len(chunks)}")

    if verbose:
        print(report.render())
    return report


def _first_heading(body: str) -> str:
    m = re.search(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", body)
    return m.group(2).strip() if m else ""


def _unchanged(stored: pg.Document, doc: "ImportedDoc") -> bool:
    """Whether re-importing *doc* would leave the stored row identical.

    Comparing only the body hash is not enough: a frontmatter-only edit changes
    the stored row (and the exported file) without touching the prose, so it must
    count as an update while still not re-embedding any chunk.
    """
    return (
        stored.body == doc.body
        and stored.title == doc.title
        and (stored.frontmatter_raw or "") == (doc.frontmatter_raw or "")
        and stored.always_loaded == doc.always_loaded
        and stored.budget_bytes == doc.budget_bytes
    )


def _safe(name: str, dsn: Optional[str]) -> Optional[pg.Document]:
    try:
        return pg.get_document(name, dsn=dsn)
    except Exception:
        return None


_safe_get = _safe


# ---------------------------------------------------------------------------
# Export (backup only — never on the hot path)
# ---------------------------------------------------------------------------

def export_vault(out_dir: Path, *, dsn: Optional[str] = None) -> List[Path]:
    """Write every stored document back out as Markdown."""
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    for doc in pg.list_documents(dsn=dsn):
        target = out_dir / doc.source_path
        if doc.source_path.endswith(ROUTER_FILENAME):
            target = out_dir / ROUTER_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        if doc.frontmatter_raw:
            # Byte-identical restore: the original block verbatim, then the body.
            text = doc.frontmatter_raw + doc.body
        else:
            meta: Dict[str, Any] = dict(doc.frontmatter or {})
            if doc.title and "title" not in meta:
                meta["title"] = doc.title
            text = render_frontmatter(meta) + "\n" + doc.body
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m claire.memory.migrate_md_to_pg",
        description="Import the Markdown vault into PostgreSQL (idempotent).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen and write nothing")
    parser.add_argument("--vault", type=Path, default=None,
                        help="vault directory (default: ~/.local/share/claire/vault)")
    parser.add_argument("--dsn", default=None, help="Postgres DSN override")
    parser.add_argument("--export", type=Path, default=None,
                        help="export stored memory back to Markdown at this path, then exit")
    parser.add_argument("--no-bootstrap", action="store_true",
                        help="do not create the schema first")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    try:
        if args.export is not None:
            written = export_vault(args.export, dsn=args.dsn)
            if args.json:
                print(json.dumps({"exported": [str(p) for p in written]}))
            else:
                for path in written:
                    print(f"exported {path}")
            return 0

        report = import_vault(
            args.vault, dsn=args.dsn, dry_run=args.dry_run,
            bootstrap=not args.no_bootstrap, verbose=not args.json,
        )
        if args.json:
            print(json.dumps({
                "created": report.created, "updated": report.updated,
                "unchanged": report.unchanged, "skipped": report.skipped,
                "dry_run": args.dry_run,
            }))
        return 0
    except MemoryStoreError as e:
        print(f"memory store error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
