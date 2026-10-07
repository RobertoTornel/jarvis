# Memory: the router and the notes

[← Back to Claire](../README.md)

Claire remembers things in two places that work together:

| | What it is | Where it lives |
|---|---|---|
| **`CLAIRE.md`** — the router | One small always-loaded file of *pointers*. Hard-capped at **6000 bytes**. | `<vault>/CLAIRE.md` |
| **Notes** | One markdown file per big fact. | `<vault>/notes/*.md` |

The vault is **outside the repository**, in your own data directory, because the content is *Claire's instructions to herself* — your facts, your names, your preferences — and not source code. It is never committed, and updating the repository never overwrites it.

## Where the vault is

By default:

```
~/.local/share/claire/vault/          ← the vault
├── CLAIRE.md                         ← the router: always loaded, re-read every request
└── notes/                            ← the notes
    ├── dentist.md
    └── car-service.md
```

**How to find the vault path on your machine** — pick either:

```bash
# 1. Ask Claire's own code for the resolved path (respects the env var):
cd /path/to/jarvis_cloud && PYTHONPATH=src .venv/bin/python -c \
  "from claire.memory.notes import resolve_vault_dir, resolve_notes_dir; \
   print(resolve_vault_dir()); print(resolve_notes_dir())"

# 2. Or just look, if you have not changed anything:
ls -la ~/.local/share/claire/vault/
```

To move the vault somewhere else, set the environment variable before Claire starts:

```bash
export CLAIRE_VAULT_DIR="$HOME/Documents/claire-vault"
```

…or, to make it stick without an exported variable, set the config key in `config.json`:

```json
{ "vault_dir": "~/Documents/claire-vault" }
```

An explicit directory passed to the functions always wins over the environment variable, which wins over the config key, which wins over the default. [OBSIDIAN.md](OBSIDIAN.md) covers this from the point of view of pointing Obsidian at the folder.

## `CLAIRE.md` is re-read on every single request

This is the point of the design:

* **It is re-read from disk on every request.** Memory is never stale: correcting the file corrects Claire's behaviour on the very next thing you say, with **no restart**.
* **Editing it costs zero conversation-history tokens.** Its content is re-rendered into the system prompt each turn instead of being appended to the chat history as messages. Appending it per turn would grow the history without bound and rot the context; re-rendering keeps the cost flat.
* **It carries frontmatter**:

  ```markdown
  ---
  title: Claire memory router
  role: router
  always_loaded: true
  budget_bytes: 6000
  updated: 2026-01-31
  ---

  # Pointers

  - dentist: notes/dentist.md — Dr. Ruiz, Wednesdays at 16:00.
  - car: notes/car-service.md — garage, mileage, next service.
  ```

## The 6000-byte cap, and why it shouts

The router body is **hard-capped at 6000 bytes** (`budget_bytes`). When it grows past the cap:

1. the content is **truncated**, so the prompt stays within budget, and
2. a **loud warning naming the real size** is injected into the prompt.

That is deliberate. Rot becomes visible and self-correcting instead of silent: Claire can tell you the file is over budget, and you can fix it. A router that quietly grew to 40 KB would degrade answers for weeks before anyone noticed.

When you see the warning, move detail out: keep the pointer, put the big fact in a note.

## A router, not a database

The rule of thumb:

* **One line or less** → it may live in `CLAIRE.md` directly (a preference, a name, a stable fact).
* **Bigger than one line** → it belongs in a **note** under `notes/`, and only a **pointer** stays in the router.

So the router answers "what do I know about, and where is it?" and the notes hold the actual content.

Notes are read only when they are actually needed, so a vault full of notes costs nothing until one is opened.

### Creating and finding notes

Notes are plain markdown with frontmatter, written for you by Claire (or by hand — the format is deliberately simple, with no YAML library involved):

```markdown
---
title: Dentist
created: 2026-01-31
---

Dr. Ruiz, Calle Mayor 4. Wednesdays at 16:00. Book two weeks ahead.
```

From code (`src/claire/memory/notes.py`):

```python
from claire.memory.notes import create_note, list_notes, read_note, search_notes
from claire.memory.notes import unreferenced_notes

create_note("dentist", "Dr. Ruiz, Calle Mayor 4. Wednesdays at 16:00.")  # refuses to overwrite
list_notes()                 # name, path, byte size and title for every note
read_note("dentist")         # full text; missing note → NoteNotFoundError
search_notes("dentist")      # ranked hits, each with the matching line
unreferenced_notes()         # notes the router body never mentions
```

Safety properties worth knowing:

* A missing note raises a **named error** quoting both the note name and the notes directory — never an empty result that looks like "there is no such fact".
* Creating a note **refuses to overwrite** an existing one unless `overwrite=True` is passed.
* Note names are single filenames. Anything that tries to escape the vault — `../../etc/passwd`, `/etc/passwd`, `sub/dir`, `..` — is **rejected outright**, and nothing outside `notes/` is written.
* A note that is a symlink pointing **outside** `notes/` is skipped in listings and refused by read and create, so a link cannot leak or overwrite a file elsewhere on the disk.

## Keeping the router honest

`unreferenced_notes()` lists every note the router body does not mention. Run it when you tidy the vault: an unreferenced note is either a pointer someone forgot to add, or a fact Claire will never find.

```bash
cd /path/to/jarvis_cloud && PYTHONPATH=src .venv/bin/python -c \
  "from claire.memory.notes import unreferenced_notes; \
   print([n.name for n in unreferenced_notes()])"
```

It is read-only and side-effect free.

## Related

* [OBSIDIAN.md](OBSIDIAN.md) — the vault as an Obsidian vault: layout, frontmatter and wiki-link conventions, how to open it, and how to move it.
* [CONFIGURATION.md](CONFIGURATION.md) — where Claire's settings and other data directories live, including the `vault_dir` key.
* [CLOUD_POLICY.md](CLOUD_POLICY.md) — what leaves this machine (memory content stays local unless a prompt carries it out).
