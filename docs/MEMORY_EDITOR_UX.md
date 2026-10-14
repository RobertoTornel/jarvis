# Memory Editor — UI/UX Design Specification

| | |
|---|---|
| **Status** | Implemented in part. §1.0 lists the surface the panel ships; the routes, parameters and fields elsewhere in §1 to §3 and §6 are the proposal this document was written as. §4 (the token law) and §5's measured values are shipped and are the token authority for the panel, the Android app and the desktop shell. |
| **Scope** | Claire web control panel (`src/claire/panel/server.py`, server-rendered HTML + inline CSS, stdlib `http.server`); §4 holds the colour law that every surface implements, each under its own variable names: the panel, the Android companion and the Qt desktop shell |
| **Store and search** | Memory lives in a **PostgreSQL + pgvector** store; the editor searches it with **semantic search** (nearest-neighbour by meaning, `nomic-embed-text`, 768-dim, ~15 ms/embedding, local) alongside keyword/name search. The always-loaded **router** document has a `budget_bytes: 6000` limit (currently 804 bytes of body). |
| **Implementation touchpoints** | `handle_memory_save` (server.py:4574), `handle_memory_delete` (server.py:4622); renderers `_memory_section` (3636), `_memory_notes_table` (3723), `_memory_note_html` (3764), `_memory_editor_html` (3817), `_memory_new_note_html` (3852), `_memory_search_html` (3884), `render_memory` (3935). The routes `ROUTE_MEMORY`, `ROUTE_MEMORY_SAVE` and `ROUTE_MEMORY_DELETE` are reused unchanged. |

---

## 0. Ground rules (binding)

1. **Colour law.** Primary: dark grey + light grey. Secondary: **pink**. Dark-first, calm, professional. Pink is an *accent* — emphasis, active state, primary action — never splashed everywhere, never a status colour. The panel, the Android companion and the Qt desktop shell implement the same values as §4, each under its own variable names; each surface keeps to its own token block and defines no hex outside it.
2. **No framework.** The panel is server-rendered HTML with inline CSS on stdlib `http.server`. There is no build step, no templates directory, no JS framework. Every flow in this document is completable with **plain links and form posts**; JavaScript appears only as a marked, optional enhancement and is never load-bearing.
3. **Binding security invariants** — every route in §1 obeys all four:
   - The panel binds **loopback only**.
   - **Origin check first on every write**: refuse with `403 origin-refused` unless `Origin` is present and non-opaque (or, when missing/opaque, `Sec-Fetch-Site: same-origin` accompanies it), its host is loopback, **and** `Sec-Fetch-Site` is `same-origin` or `none`.
   - `refuse_key_fields()` rejects key-shaped field names; secrets are never accepted, and the panel never reads a raw secret to build a mask.
   - Request bodies are capped; oversized bodies get `413 body-too-large`.
   - **Viewing a page never mutates anything on disk.** `GET /memory` may only `SELECT`; the query embedding for a search is computed in memory; reindex/cache-warm must never run on a GET.
4. **Progressive enhancement discipline.** Each interactive behaviour below is labelled *canonical* (works with JS disabled) or *enhancement* (small inline script, ≤ ~20 lines, fails silent). No behaviour may exist *only* as an enhancement.

---

## 1. Surface map

### 1.0 Shipped surface (as built)

The panel implements a smaller surface than the rest of this document specifies. What ships:

- `GET /memory` with the query parameters `?q=<query>` (search), `?note=<name>` (open one
  memory) and `?changed=1` (the "written" banner). There is no `?edit=`, `?new=`,
  `?confirm-delete=`, `?deleted=`, `?reindexed=` or `?error=`.
- `POST /memory/save` with the fields `mode` (`create` or `edit`), `name` (or `original` when
  editing), `title` and `body`. The store assigns the rest: there is no `note_id`, `is_router`,
  `base_updated`, `from_q` or `force`, and no `409 note-conflict`.
- `POST /memory/delete` with the field `name`. Deletion runs straight from the memory's own
  form: there is no confirm state and no `confirmed` field.
- Search runs `claire.memory.pg.search` server-side and renders one HTML page.

Everything else in §1 to §3 and §6 is the proposal this document was written as: the
id-addressed editor, the delete confirmation, the reindex route, the flash-parameter
vocabulary and the byte-exact failure codes. It is kept because the reasoning and the layout
are the target the shipped page is moving towards; treat those routes and parameters as
design, not as API. §4 and §5 hold the shipped token law and its measured values.

### 1.1 Route summary

| # | Route | Method | Status | Purpose |
|---|-------|--------|--------|---------|
| 1 | `/memory` | GET | shipped (`ROUTE_MEMORY`) | List view; also hosts the editor and search results via query params |
| 2 | `/memory/save` | POST | shipped (`ROUTE_MEMORY_SAVE`) | Create or update one note |
| 3 | `/memory/delete` | POST | shipped (`ROUTE_MEMORY_DELETE`) | Delete one note (never the router doc) |
| 4 | `/memory/reindex` | POST | **proposed, not built** | Rebuild embeddings after an embedding-model change or pgvector backfill |

**No semantic-search endpoint is added.** `GET /memory?q=` performs the combined keyword + semantic query server-side and renders one HTML page. With no JS framework there is no client to consume JSON, and a fragment endpoint would only serve type-ahead, which is a documented non-goal for v1 (§7). The name `/memory/search` is **reserved** for a future fragment endpoint that would return the `#search-results` HTML rendered by the same `_memory_search_html` renderer — do not squat it for anything else.

### 1.2 `GET /memory` — list / editor / results / confirm (read-only)

Query params (all optional, combinable; each is read, never written). Only the rows marked
**shipped** exist in the code (§1.0); the rest are the proposal:

| Param | Value | Effect | Status |
|-------|-------|--------|--------|
| (none) | — | Notes table, budget meter, "New note" action | shipped |
| `q` | text, ≤ 256 chars (server-truncated) | Search-results view: semantic + keyword sections | shipped |
| `note` | note name | Note editor for that memory | shipped |
| `changed` | `1` | Banner "The memory was written" | shipped |
| `edit` | note id (opaque, `[a-z0-9-]{1,64}`) | Note editor for that id | proposed |
| `new` | `1` | Blank note editor | proposed |
| `confirm-delete` | note id | Destructive confirmation state (§2.5) | proposed |
| `saved` | note id | Flash banner "Saved '<name>'" (set by the save route's 303) | proposed |
| `deleted` | note id | Flash banner "Note deleted" (set by the delete route's 303) | proposed |
| `reindexed` | count | Flash banner "Re-embedded <n> notes" (set by the reindex route's 303) | proposed |
| `error` | `conflict` \| `over-budget` \| `db-unreachable` | Flash error banner | proposed |

Success: `200` `text/html` (the full page). Unknown `edit`/`confirm-delete` id: `404 note-not-found` with an error page and a *Back to memory* link. Database unreachable: `200` with the danger panel described in §2.1 (the page itself renders fine; only its data area changes — the panel is never a blank 500).

**Invariant note:** all params are read-only; flash state lives in the URL, not in a session. Refreshing a `?saved=` URL re-shows the banner and touches nothing on disk — accepted as idempotent and harmless.

### 1.3 `POST /memory/save` — create or update one note

`Content-Type: application/x-www-form-urlencoded`. **The proposed field whitelist is exactly these seven**; any field outside the whitelist is rejected before any value is read (every field is present in the editor form, possibly empty). The shipped form posts `mode`, `name`/`original`, `title` and `body` (§1.0), so read the table below as the proposal:

| Field | Required | Constraints |
|-------|----------|-------------|
| `note_id` | yes | Existing id to update; **empty string = create** |
| `name` | yes | 1–120 chars after trim |
| `body` | yes | UTF-8 text; ≤ 7,500 bytes for normal notes; **≤ 6,000 bytes when `is_router=1`** |
| `is_router` | yes | `"1"` only when editing the router document, else empty |
| `base_updated` | yes | ISO-8601 `updated_at` the editor was rendered from (optimistic concurrency) |
| `from_q` | yes | Search query to return to after save (`/memory?q=…`); empty = plain list |
| `force` | yes | Empty normally; `"1"` **only** on the "Keep my version" resubmit after a `409 note-conflict` |

Success: **`303 See Other`**, `Location: /memory?saved=<note_id>` (`&q=<from_q>` when present). The storage write + pgvector embedding upsert happen here — this is the sanctioned mutation point.

Failures (all preserve submitted content where a human can retry):

| Condition | Response | Shape |
|-----------|----------|-------|
| Origin check fails | `403 origin-refused` | Plain-text body `origin-refused`; no HTML, no echo of submitted content |
| Any key-shaped or non-whitelisted field name (e.g. `api_key`, `token`, `secret`, `password`) | `400 field-refused` | Plain-text `field-refused`; request never processed. `refuse_key_fields()` runs before any value is parsed, so a secret can never enter the handler, and the memory UI renders no secret values at all |
| Form body > 16,384 bytes | `413 body-too-large` | Plain-text `body-too-large` |
| Validation: empty `name`, `body` > 7,500 bytes | `422 note-invalid` | Full HTML re-render of the editor: submitted text back in the form, `role="alert"` banner "Name is required." / "Note is too large (N bytes; limit 7,500)." |
| Router doc `body` > 6,000 bytes | `422 note-over-budget` | Same re-render; banner "Router note is N bytes — the limit is 6,000. Trim at least N−6000 bytes."; byte counter red |
| Stored `updated_at` newer than `base_updated` and `force` not set | `409 note-conflict` | Editor re-render, banner "This note changed since you opened it." with actions **Keep my version** (resubmits with `force=1`) / **Discard my changes** (303 back to list) |
| Database unreachable | `503 db-unreachable` | Editor re-render, content preserved; banner "Memory database is unreachable — your text is still in the form below; nothing was saved." + [Retry] button (resubmits) |

### 1.4 `POST /memory/delete` — delete one note (proposed fields)

The shipped delete form posts `name` and deletes immediately (§1.0). The contract below, and
the confirm page it depends on, are the proposal:

| Field | Required | Constraints |
|-------|----------|-------------|
| `note_id` | yes | Existing, non-router id |
| `confirmed` | yes | Must be exactly `"1"` — only the confirm page (§2.5) ever sets it |
| `from_q` | no | Return-to-search after deletion |

Success: **`303 See Other`**, `Location: /memory?deleted=<note_id>` (`&q=` when present). Delete is a single transaction — there is no partial state.

Failures: `403 origin-refused` / `400 field-refused` / `413 body-too-large` as in §1.3; `confirmed != "1"` → `400 delete-unconfirmed` plain-text (a stray or forged-but-same-origin POST can never delete); router id or unknown id → `404 note-not-found` plain-text; DB unreachable → `503 db-unreachable` plain-text "delete not attempted".

### 1.5 `POST /memory/reindex` — rebuild embeddings (**proposed, not built**)

Exists because embeddings can go stale independently of note text: an embedding-model change (different model or dimension) or the initial pgvector migration requires a bulk re-embed. This is a genuine operational write, not a CRUD duplicate.

| Field | Required | Constraints |
|-------|----------|-------------|
| `confirmed` | yes | `"1"`; the button lives behind the same server-rendered confirm pattern as delete (§2.5) with copy "Re-embed every note? This rewrites all vectors (≈ n × 15 ms)." |

Success: `303`, `Location: /memory?reindexed=<n>`. Runs synchronously — the corpus is small and embeddings are ~15 ms each; no progress UI in v1. Failure: `403 origin-refused` / `400 field-refused` / `413 body-too-large` / `503 db-unreachable` as above.

### 1.6 Invariant compliance (the write routes)

Identical order in `handle_memory_save` and `handle_memory_delete`, and in the proposed reindex handler:

1. **Origin check first** — before reading the body. `403 origin-refused` unless `Origin` present and non-opaque (or missing/opaque with `Sec-Fetch-Site: same-origin`), host is loopback, and `Sec-Fetch-Site` ∈ {`same-origin`, `none`}.
2. **`refuse_key_fields()`** — whitelist check on *names only*; violations → `400 field-refused`. No secret value is ever read, echoed or masked.
3. **Body cap** — 16,384 bytes; over → `413 body-too-large`, nothing parsed.
4. **Mutate** — only after 1–3 pass, and only on POST. Every `GET /memory` combination (list, editor, search, confirm) is a pure read.

---

## 2. Screens

**Status (from the table at the top): implemented in part.** §1.0 lists the surface the panel ships; the screens, routes and query parameters below, including `?edit=` and `?new=`, are the proposal this document was written as and are not API.

### 2.1 S1 — Memory list (`GET /memory`)

**Purpose:** scan, find, open or create notes; monitor the router budget at a glance.

**Layout (top → bottom):**

```
┌──────────────────────────────────────────────────────────────┐
│ Memory                                            [New note] │  header + primary action
│ 24 notes · updated hourly by the router                      │  subtitle (secondary)
│ ──────────────────────────────────────────────────────────── │
│ Router budget   804 / 6,000 bytes · 13%   ▓▓░░░░░░░░░░░░░░   │  budget meter (§2.4)
│ ──────────────────────────────────────────────────────────── │
│ [ Search notes…                                    ] (clear) │  search, autofocus
│ ──────────────────────────────────────────────────────────── │
│ (flash banner: Saved '…' / Note deleted / error)             │  when present
│ Name            Preview            Updated      Bytes  Act. │  notes table
│ Router note     You are Claire's…  2 h ago       804  Edit…  │  (router row badged)
│ Watering rota   Mon/Wed/Fri 8am…   3 d ago        61  Edit…  │
└──────────────────────────────────────────────────────────────┘
```

Components in order: page header ("Memory" + note count) · **New note** button (primary pink) · budget meter · search field (`autofocus`, name `q`, submits `GET /memory` with `q`) · flash banner slot · notes table (columns: **Name** — link to `?edit=<id>`, with a `[router]` badge on the router row; **Preview** — first body line, tertiary, truncated to one line; **Updated** — relative, absolute in `title`; **Bytes** — right-aligned, tabular numerals; row actions **Edit** and **Delete…** — the latter links to `?confirm-delete=<id>`).

**States:**

- **Empty (no notes at all):** centred panel on `--surface-1` — glyph `◦`, title "No notes yet", hint "Memory notes are loaded into Claire's context. Start with a router note.", button **[New note]**.
- **Empty (no search hits):** "Nothing matched '<q>'" + hint "Try fewer words — semantic search also matches by meaning." + link **Clear search** (`GET /memory`).
- **Loading:** none. Every screen is one synchronous server render (embeddings are ~15 ms); there are no skeletons and no client-side spinners anywhere except the reindex button.
- **Error — database unreachable (exact visual):** the table area is replaced by a `--danger-soft` panel with `--danger-border`: title "**Memory database unreachable**", body exactly: "Claire can't reach the memory store right now. Notes can't be shown, created or changed until it's back." Action: link **[Retry]** → `GET /memory`. The budget meter slot shows "unavailable" in `--text-tertiary`. The page header and search field still render (search submits and lands on the same panel — see S3).

### 2.2 S2 — Note editor (`GET /memory?edit=<id>` / `?new=1`)

**Purpose:** create or edit one note with full attention on the text and (for the router doc) the byte budget.

**Layout:** top bar: **← Back to list** (or **← Back to results** when `from_q` present — see §5.4) · right-aligned meta (Created, Updated, Bytes, `[router]` badge) → **Name** input (`name="name"`, `maxlength="120"`, placeholder "Name this note") → **Body** textarea (`name="body"`, 16 rows min, `--font-mono`, placeholder "Write the note. It is stored verbatim and embedded for semantic search.") → sticky save bar: **[Save note]** (primary pink) · **[Cancel]** (secondary, link to back-target) · live byte counter (right) · **[Delete note…]** (danger ghost, existing notes only).

The form carries hidden `note_id`, `is_router`, `base_updated`, `from_q` exactly as named in §1.3.

**States:**

- **Empty (`new=1`):** as the placeholders above; byte counter shows "0 bytes".
- **Loading:** none (synchronous render).
- **Dirty:** the Save button gains a 6px pink dot and the save bar reads "Unsaved changes" in `--text-secondary`. Set by any input event (enhancement); the canonical hint is that navigating away via Cancel asks nothing and the server-side guard (§5.2) covers the rest.
- **Error banners** (each `role="alert"`, `--danger-soft` panel, above the Name field): validation, `over-budget`, `conflict` (with **Keep my version** / **Discard my changes**), `db-unreachable` (with **Retry**) — copy as specified in §1.3. In every error re-render the submitted text is already back in the fields: *no failure path ever loses typed content*.
- **Unknown id:** `404 note-not-found` page — "That note doesn't exist (it may already have been deleted)." + **[Back to memory]**.

### 2.3 S3 — Semantic search results (inside `GET /memory?q=`)

**Purpose:** find notes by *meaning*, show why each hit matched, and return to the note with context intact.

**Layout:** search field (value = `q`, `autofocus`, **Clear** link) → summary line "7 matches for 'flower watering' — 4 semantic, 3 keyword" (`--text-secondary`) → **section 1: "Semantic matches"** with hint "nearest by meaning · nomic-embed-text" → hit cards → **section 2: "Keyword matches"** with hint "name or text contains '<q>'" → hit cards.

Each hit card (`--surface-1`, `--radius-m`, 1px `--border`): **Name** (link → `?edit=<id>&from_q=<q>`) · badge — `semantic · 82%` (accent style) or `keyword` (neutral) · score bar (2px track, width = similarity %, `--accent`) · snippet line — the body window around the best-matching chunk with the query terms in `<mark>` (`--accent-soft` background, `--text-primary`) · updated date (tertiary).

**Ranking rules (concrete):** semantic section = top 8 nearest by cosine similarity with similarity ≥ 0.30, displayed as `round(sim × 100)%`. A note hit by both modes appears **once**, in Semantic, with the semantic badge plus a secondary `also keyword` neutral tag. Sections are hidden entirely when they have no hits.

**States:** empty → "Nothing matched '<q>'" (as S1); DB unreachable → same danger panel as S1 with the line "Search is unavailable — the memory database can't be reached."; loading → none.

### 2.4 Budget meter (component; S1 full, S2 compact)

**Data:** `used = len(router_body.encode('utf-8'))`; `limit = budget_bytes = 6000`; `pct = round(used / limit × 100)` (currently 804 → 13%). Only the router document counts — regular notes are governed by their own per-note cap, not this meter.

**Full form (list view), one row:** label "Router budget" (`--text-secondary`) · value "804 / 6,000 bytes · 13%" (`--text-primary`, tabular numerals) · track 6px tall, full width, `--surface-2`, `--radius-s` · fill. Thresholds — **the warning must fire before the limit is crossed:**

| Used | Fill | Value text | Extra copy |
|------|------|-----------|------------|
| < 75% | `--accent` (pink) | secondary | — |
| 75–89% | `--warning` | `--warning` | "· 75% — consider trimming" |
| 90–99% | `--danger` | `--danger` | "· only N bytes left" |
| ≥ 100% | `--danger`, full width, `--danger-border` on the track | `--danger` | "· over budget — trim before saving" |

At ≥ 100% the router editor's Save is refused server-side with `422 note-over-budget` regardless of what the UI shows — the meter is guidance, the limit is the server's.

**Compact form (editor, router doc only):** the save-bar byte counter ("1,204 / 6,000 bytes") adopts the same three colours inline, turning red at 90% and refusing past 6,000 (§1.3).

**Accessibility:** `role="progressbar"` with `aria-valuemin="0"` `aria-valuemax="6000"` `aria-valuenow="<used>"` and `aria-label="Router budget"`.

### 2.5 Destructive-action confirmation — delete (proposed)

Not a modal: a **server-rendered confirm state** at `GET /memory?confirm-delete=<id>`, so it works with JS disabled and needs no focus-trap code. The list body is replaced by a `--danger-soft` panel:

- Title: "**Delete this note?**"
- Body: the note's name verbatim, its byte count and last-updated date, then exactly: "This permanently removes the note and its embedding. This cannot be undone."
- Buttons: **[Cancel]** (secondary, `autofocus` — the *safe* default has focus) and **[Delete note]** (danger-filled) — a form `POST /memory/delete` with `note_id` + `confirmed=1` (+ `from_q` if the user came from search).

There is no delete path that bypasses this page: a POST without `confirmed=1` is `400 delete-unconfirmed` (§1.4). The router document is never deletable — it has no Delete affordance anywhere, and the handler rejects its id with `404`.

### 2.6 What the user sees when the database is unreachable (consolidated)

One behaviour everywhere, three placements:

- **List view:** danger panel in place of the table (§2.1, exact copy given) + meter reads "unavailable".
- **Search:** same panel, line "Search is unavailable — the memory database can't be reached." (§2.3).
- **Save attempt:** `503 db-unreachable` re-render with all typed text preserved and the banner "Memory database is unreachable — your text is still in the form below; nothing was saved." + **[Retry]** (§1.3). Delete/reindex: plain-text `503 db-unreachable`; nothing was written.

Never a blank page, never a raw traceback, never a 500.

---

## 3. Screen inventory quick reference

| Screen | Route | Primary action | Destructive? |
|--------|-------|----------------|--------------|
| List | `GET /memory` | New note / Search | — |
| Editor | `GET /memory?edit=<id>` · `?new=1` | Save note | Delete note… (→ confirm) |
| Search results | `GET /memory?q=<q>` | Open a hit | — |
| Budget meter | component on S1/S2 | — | — |
| Delete confirm | `GET /memory?confirm-delete=<id>` | Cancel (safe default) | Delete note |
| Error / empty states | all above | Retry / Clear | — |

The shipped routes are the subset in §1.0: the `?edit=<id>`, `?new=1` and
`?confirm-delete=<id>` rows above are proposed.

---

## 4. Design tokens

**The law, then the spellings.** The values below are the law: dark grey and light grey carry all structure and text, one pink accent carries emphasis and interaction state, and green, amber and red carry status and never pink. A component keeps to its own surface's token block and writes no raw hex of its own (the `rgba` soft variants are derived tints of the accent and status tokens).

The names in the block are this document's vocabulary; the panel implements the same values under its own, shorter set of custom properties. Read every token in §5 through this mapping (values are the panel's, from `_CSS` in `src/claire/panel/server.py`):

| §4 role (used in §5) | Panel variable | Dark value | Light value |
|----------------------|----------------|-----------|-------------|
| `--bg` | `--bg` | `#131417` | `#f4f5f7` |
| `--surface-1` | `--card` (`--bg-soft` shares the dark value) | `#1b1d21` | `#ffffff` (`--bg-soft` `#eaebee`) |
| `--surface-2` | `--card-2` | `#24262b` | `#f7f8fa` |
| `--surface-3` | not defined in the panel (Android's `surface_high`) | `#2e3138` | not defined |
| `--border-subtle` | `--line-soft` | `#26282d` | `#e4e6ea` |
| `--border` | `--line` | `#31343a` | `#d5d8dd` |
| `--border-strong` | not defined | `#454951` | not defined |
| `--text-primary` | `--ink` | `#e8eaed` | `#16181c` |
| `--text-secondary` | `--ink-soft` | `#a8adb5` | `#4a4f57` |
| `--text-tertiary` | `--ink-faint` | `#878d96` | `#6b7078` |
| `--accent` | `--accent` | `#e562a4` | `#b93e83` |
| `--accent-contrast` | `--accent-ink` | `#150a12` | `#ffffff` |
| `--accent-hover` | not defined by any surface (panel: `filter: brightness(1.07)`) | `#ee7cb5` (target) | not defined |
| `--accent-active` | not defined by any surface (panel: `transform: translateY(1px)`; Android: `accent_dim`) | `#cf4b92` (target) | not defined |
| `--accent-focus` | not defined by any surface (panel rings in `--accent`) | `#f7a3cd` (target) | not defined |
| `--success` | `--ok` | `#4cc38a` | `#1f7a4d` |
| `--warning` | `--warn` | `#d9a441` | `#8a5b00` |
| `--danger` | `--bad` | `#ef6a6a` | `#b4231a` |
| `--accent-soft` | not defined (panel mixes `color-mix(in srgb, var(--accent) 6%, transparent)` for row hover) | `rgba(229, 98, 164, 0.14)` (target) | not defined |
| `--success-soft`, `--warning-soft`, `--danger-soft`, `--danger-border` | not defined (panel mixes status tints inline) | see block (target) | not defined |
| `--radius-m` | `--radius-sm` | `8px` | `8px` |
| `--radius-l` | `--radius` | `12px` | `12px` |
| `--shadow-1`, `--shadow-2` | `--shadow` (one composite) | see `_CSS` | see `_CSS` |
| `--font-ui` | `--sans` | system sans stack | system sans stack |
| `--font-mono` | `--mono` | system mono stack | system mono stack |

The panel defines no variable for `--surface-3`, `--border-strong`, `--radius-s`, the `*-soft` tints, the `--space-*` steps, `--dur-fast`/`--dur`/`--ease`, or the `--focus-ring-*` names. Those are specification values; a component that needs one extends the panel's set first.

**The Android app uses its own names for the same dark values.** `android/app/src/main/res/values/colors.xml` defines Material 3 role names, wired into `themes.xml`: `accent` `#E562A4`, `bg` `#131417`, `surface` `#1B1D21`, `surface_alt` `#24262B`, `surface_high` `#2E3138`, `outline` `#31343A`, `text` `#E8EAED`, `text_dim` `#A8ADB5`, `on_accent` `#150A12`, `ok` `#4CC38A`, `warn` `#D9A441`, `error` `#EF6A6A` (with `bad` as the panel-matching alias), `idle` `#878D96`, `you` `#A8ADB5`, `claire` `#E562A4`. It also names a pressed step the panel's own set does not, `accent_dim` `#CF4B92`, and roles with no §4 counterpart (`bubble_you`, `bubble_claire`, `bubble_meta`). It is a dark-only palette with no light block. Two resources hardcode a colour where a resource reference belongs: `drawable/ic_claire.xml` writes `#E562A4` rather than `@color/accent`, and `layout/activity_camera.xml` writes `#CC131417` (the law's `bg` at 80% alpha) and `#99000000` (a transparent black scrim with no token counterpart). Those are drifts in form; the pink and the grey still match the law.

**The Qt desktop shell follows the same law under its own names.** `src/desktop_app/themes.py` holds the law once, as `COLOUR_LAW`, and exposes it to Qt as the role map `COLORS`: `bg_primary` `#131417`, `bg_secondary` and `bg_card` `#1B1D21`, `bg_tertiary` `#24262B`, `bg_hover` `#2E3138`, `border` `#31343A`, `border_soft` `#26282D`, `text_primary` `#E8EAED`, `text_secondary` `#A8ADB5`, `text_muted` `#878D96`, `accent_primary` and `accent_secondary` `#E562A4`, `accent_muted` `#CF4B92`, `on_accent` `#150A12`, `success` `#4CC38A`, `warning` `#D9A441`, `error` `#EF6A6A`, with the soft variants as `rgba` tints of those values. Every other desktop module reads that map or renders a stylesheet through `law_style()`; none writes a hex of its own. The face widget paints its mesh in the grey text roles and puts the accent in its glow, the conversation bubbles give Claire the accent and the owner a neutral grey, and the tray icons are a faint grey when idle and the accent when listening. `src/desktop_app/desktop_app.spec.md` documents the application as deprecated in favour of the panel; it is deprecated but on the law.

**One `<style>` block, hex only in the token blocks.** The panel serves exactly one `<style>` block, its `_CSS` in `src/claire/panel/server.py`, and the repository contains no `.css` files of its own. Within `_CSS`, hex literals appear only in the two `:root` token blocks (the dark one and the `prefers-color-scheme: light` override); every other rule uses `var(--token)` or `color-mix(in srgb, var(--token) ...)`, and no rendered element carries an inline `style=` hex. A new rule takes a token or mixes one; it does not introduce a hex.

**Hover, active and focus work; the three interaction tokens do not exist.** The panel styles these states without named colour tokens: `button:hover` uses `filter: brightness(1.07)`, `button:active` uses `transform: translateY(1px)`, row hover and the Claire turn background use `color-mix(in srgb, var(--accent) ...)`, and `:focus-visible` uses `outline: 2px solid var(--accent)` with `outline-offset: 2px`. The `--accent-hover` (`#ee7cb5`), `--accent-active` (`#cf4b92`) and `--accent-focus` (`#f7a3cd`) entries in the block are target names that no surface defines; `docs/UI_UX_GUIDE.md` records the filter, the translation, the mixes and the accent ring as the accepted answer. `#cf4b92` exists in the shipping code only as the Android `accent_dim` resource, and `--focus-ring-color: var(--accent-focus)` is target-only for the same reason.

```css
:root {
  /* — neutrals: the primary law (dark grey + light grey) ————————————— */
  --bg:            #131417;  /* page background, elevation 0 */
  --surface-1:     #1b1d21;  /* cards, table rows, panels (elevation 1) */
  --surface-2:     #24262b;  /* inputs, hovered rows, header bars (elevation 2) */
  --surface-3:     #2e3138;  /* popovers, toasts, pressed chips (elevation 3) */

  --border-subtle: #26282d;  /* hairline dividers inside a surface */
  --border:        #31343a;  /* default component border */
  --border-strong: #454951;  /* hovered / emphasised border */

  --text-primary:   #e8eaed; /* headings, body, values */
  --text-secondary: #a8adb5; /* labels, meta, summaries */
  --text-tertiary:  #878d96; /* hints, placeholders, timestamps */

  /* — pink: the single accent (emphasis, active state, primary action) */
  --accent:          #e562a4; /* primary buttons, links, active states, meter fill */
  --accent-hover:    #ee7cb5; /* hover fill (target: panel uses filter: brightness(1.07)) */
  --accent-active:   #cf4b92; /* pressed fill (target: panel uses transform: translateY(1px)) */
  --accent-focus:    #f7a3cd; /* focus rings only — never a fill (target: panel rings in --accent) */
  --accent-contrast: #150a12; /* text/icons on pink fills */
  --accent-soft:     rgba(229, 98, 164, 0.14); /* badge/snippet/mark tints */
  --accent-soft-border: rgba(229, 98, 164, 0.38);

  /* — semantic status: success/warning/danger, never pink ———————————— */
  --success:       #4cc38a;  --success-soft: rgba(76, 195, 138, 0.14);
  --warning:       #d9a441;  --warning-soft: rgba(217, 164, 65, 0.14);
  --danger:        #ef6a6a;  --danger-soft:  rgba(239, 106, 106, 0.14);
  --danger-border: rgba(239, 106, 106, 0.45);

  /* — focus ————————————————————————————————————————————————————————— */
  --focus-ring-color: var(--accent-focus); /* target only; the panel rings in --accent */
  --focus-ring-width: 2px;
  --focus-ring-offset: 2px;

  /* — geometry & motion (shared; no ad-hoc values in components) ————— */
  --radius-s: 4px;  --radius-m: 8px;  --radius-l: 12px;
  --space-1: 4px;   --space-2: 8px;   --space-3: 12px;  --space-4: 16px;
  --space-5: 24px;  --space-6: 32px;
  --dur-fast: 120ms;  --dur: 180ms;
  --ease: cubic-bezier(0.2, 0, 0, 1);
  --shadow-1: 0 1px 2px rgba(0, 0, 0, 0.40);
  --shadow-2: 0 4px 16px rgba(0, 0, 0, 0.45);

  --font-ui:   system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
}
```

### 4.1 Verified contrast ratios (WCAG relative-luminance method)

| Pair | Ratio | AA (4.5) | AAA (7) |
|------|-------|----------|---------|
| **`--text-primary #e8eaed` on `--bg #131417` — the main body-text pair** | **15.3 : 1** | ✅ pass | ✅ pass |
| `--text-primary` on `--surface-1 #1b1d21` | 14.0 : 1 | ✅ | ✅ |
| `--text-primary` on `--surface-2 #24262b` | 12.6 : 1 | ✅ | ✅ |
| `--text-secondary #a8adb5` on `--bg` | 8.2 : 1 | ✅ | ✅ |
| `--text-tertiary #878d96` on `--bg` | 5.5 : 1 | ✅ | — |
| `--text-tertiary` on `--surface-1` | 5.0 : 1 | ✅ | — |
| `--accent #e562a4` on `--bg` (links, pink text) | 5.8 : 1 | ✅ | — |
| `--accent-contrast #150a12` on `--accent` (button label) | 6.1 : 1 | ✅ | — |
| `--accent-contrast` on `--accent-active #cf4b92` (pressed, target token) | 4.7 : 1 | ✅ | — |
| `--danger #ef6a6a` on `--bg` | 6.1 : 1 | ✅ | — |
| `--warning #d9a441` on `--bg` | 8.2 : 1 | ✅ | — |
| `--success #4cc38a` on `--bg` | 8.3 : 1 | ✅ | — |
| light theme: `--accent #b93e83` on `--bg #f4f5f7` | 4.7 : 1 | ✅ | — |
| light theme: `--accent-contrast #ffffff` on `--accent #b93e83` | 5.2 : 1 | ✅ | — |
| light theme: `#cf4b92` on `--bg #f4f5f7` (measured for comparison, not the light accent) | 3.8 : 1 | ❌ | — |

**Statement for sign-off:** the main body-text pair, `--text-primary #e8eaed` on `--bg #131417`, computes to **15.3 : 1**, which exceeds WCAG AA (≥ 4.5 : 1) and WCAG AAA (≥ 7 : 1). Every text pair in use meets AA at its usage size; the light `#cf4b92` row is recorded only to show why that value is not the light accent. Tertiary text is used only for hints and placeholders at ≥ 12px; its worst shipped pair is `--text-tertiary` on `--card-2` (`--surface-2`), 4.5 : 1, and it is not placed on the unimplemented `--surface-3`, where it would reach only 3.9 : 1. The accent and the three state colours clear the 3 : 1 non-text minimum against `--bg`, and the panel's focus ring uses `--accent`, 5.8 : 1 on the dark background. The grey borders and dividers do not reach 3 : 1 (1.5 : 1 dark, 1.3 : 1 light); they are hairline separation, not the cue that identifies a control.

The light block's accent is `#b93e83`, which reaches 4.72 : 1 on the light `--bg #f4f5f7`. The lighter `#cf4b92` reaches only 3.79 : 1 there, so it is not the light accent; in the panel's light block the accent is the darker value.

---

## 5. Component and state specs

### 5.1 Buttons

| Variant | Fill | Text | Border | Use |
|---------|------|------|--------|-----|
| Primary | `--accent` | `--accent-contrast` | none | One per view: **New note**, **Save note** |
| Secondary | `--surface-2` | `--text-primary` | `--border` | **Cancel**, **Retry**, **Clear** |
| Ghost | transparent | `--text-secondary` | none | row-level **Edit** |
| Danger fill | `--danger` | `--bg` | none | **Delete note** on the confirm page only |
| Danger ghost | `--danger-soft` | `--danger` | `--danger-border` | **Delete note…** affordance in editor/rows |

Sizing: height 32px (compact, in bars/rows) / 36px (standalone); padding `0 var(--space-3)`; radius `--radius-s`. Hover: primary → `--accent-hover`; secondary → `--border-strong`; danger variants raise text to `--text-primary`. Active: primary → `--accent-active`. Disabled: `opacity: 0.4`, `cursor: not-allowed`, no ring. Loading (reindex only): label swaps to "Reindexing…" with `aria-busy="true"`; the button is replaced, not decorated — no spinners elsewhere. **Shipped states:** the panel expresses hover as `filter: brightness(1.07)`, active as `transform: translateY(1px)` and focus as a 2px `--accent` outline at 2px offset; the `--accent-hover` and `--accent-active` names above are target values, not panel variables (§4).

**Destructive actions use `--danger`, never pink.** Pink means "primary action", and delete is not one.

### 5.2 Inputs

Text input and textarea: background `--surface-2`, 1px `--border`, radius `--radius-s`, text `--text-primary`, placeholder `--text-tertiary`; padding `var(--space-2) var(--space-3)`. Focus: border `--accent` + focus ring. Error state: border `--danger` + the banner; the field itself is never coloured pink. The editor textarea is `--font-mono` at 14px/1.6 for verbatim-editing fidelity; everything else is `--font-ui`. The router editor's byte counter sits in the save bar and follows the §2.4 thresholds live.

### 5.3 Tables

Header row: `--text-tertiary`, 11px, uppercase, letter-spacing 0.04em, bottom border `--border`. Rows on `--surface-1`; hover raises to `--surface-2` (no zebra striping). Numeric columns right-aligned with `font-variant-numeric: tabular-nums`. Row separation: 1px `--border-subtle`. The table is real `<table>` markup with `<th scope="col">`; each row's Name cell is the focusable link.

### 5.4 Badges

11px pill (`border-radius: 999px`, padding `1px 8px`). Variants:

| Badge | Background | Text |
|-------|-----------|------|
| `semantic · 82%` | `--accent-soft` + 1px `--accent-soft-border` | `--accent` |
| `keyword` | `--surface-2` + 1px `--border` | `--text-secondary` |
| `also keyword` | `--surface-2`, no border | `--text-tertiary` |
| `router` | `--warning-soft` | `--warning` |
| `saved` / `ok` (flash) | `--success-soft` | `--success` |

### 5.5 Toasts (flash banners)

Rendered server-side from the `saved` / `deleted` / `reindexed` / `error` params, top of the content column, above the table: `--surface-3`, `--shadow-2`, `--radius-m`, 1px `--border`, success text `--success` / error text `--danger`, with a **Dismiss** link back to plain `GET /memory`. `role="status"` for success, `role="alert"` for errors. There is no timed auto-dismiss (the panel is server-rendered; the Dismiss link is the dismissal). The banner never shifts layout of the table — it occupies the reserved flash slot from §2.1.

### 5.6 Empty states

Centred panel on `--surface-1`, 1px `--border`, `--radius-l`, padding `var(--space-6)`: glyph (`--text-tertiary`, 28px) → title (`--text-primary`) → one-line hint (`--text-secondary`) → optional action (primary or secondary button). Copy per §2.1. An empty table body is never rendered bare.

### 5.7 Focus rings

`:focus-visible` only (never on mouse-click focus): `outline: 2px solid var(--accent); outline-offset: 2px;`. The ring colour is the accent itself, `#e562a4` on the dark background at 5.8 : 1, above the 3 : 1 non-text floor; there is deliberately no separate focus-ring colour, and the `--focus-ring-*` names in §4 are target-only. Focus is never removed without being moved (`autofocus` targets stated in §2). Visible focus is part of the definition of done for every component.

### 5.8 Keyboard navigation (the editor is completable without a mouse)

**Canonical (no JS):** `Tab` / `Shift+Tab` traverse every interactive element in visual order; `Enter` activates links and submits forms; `Space` toggles/presses buttons. `autofocus` per view: S1 → search field; S2 → Name input (Body on `new=1`); confirm page → **Cancel**. Required order in the editor: Back link → Name → Body → Save → Cancel → Delete. **Every task — create, edit, save, delete, search, clear — is completable with Tab, Shift+Tab, Enter and Space alone.**

*Enhancements (inline script, optional):* `Ctrl/Cmd+S` and `Ctrl/Cmd+Enter` submit the editor form; `Esc` in the editor activates Cancel; `Esc` in the search field clears it; `/` focuses search and `n` opens New note on the list view (only when no input is focused). `j/k` row navigation is explicitly **not** specced — rows are ordinary links and Tab suffices.

### 5.9 Reduced motion

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
```

Behaviour: score bars and meter fills render at final width with no transition; the reindex label swap is instant (no pulse — with reduced motion there is no spinner at all, just the text "Reindexing…"); hover/active state changes are instant. Nothing in this spec depends on animation to be understood.

---

## 6. Interaction rules

**Proposed, except where noted.** The shipped editor saves explicitly (§6.1), but it has no
dirty-tracked unsaved-changes guard, no `base_updated` conflict check and no delete
confirmation; those are the target described here. §1.0 is the shipped surface.

### 6.1 Save model: **explicit save** (chosen; no autosave)

One **Save note** action, plus `Ctrl/Cmd+S` as an enhancement. Justification:

1. **The budget meter must reflect committed state.** Autosave of the router doc would persist half-composed text, swing the meter and could push the doc over its 6,000-byte limit invisibly mid-typing.
2. **PRG is trivially correct on stdlib `http.server`.** Explicit save is one form post + `303`; autosave needs fetch, dirty-tracking, conflict merge and retry UI in a panel with no JS framework — all risk, no payoff.
3. **Embeddings fire on write.** 15 ms is cheap but autosave would embed every keystroke burst for zero benefit.
4. **Saving is instant locally** — explicit save costs the user nothing, and it makes "what is on disk" always equal to "what the meter shows".

### 6.2 Unsaved-changes guard

- *Enhancement:* the form is dirty-tracked on `input`; in-panel navigation and `beforeunload` prompt when dirty; the Save button shows the pink dot + "Unsaved changes" (§2.2).
- *Canonical (always on):* **optimistic concurrency.** The form carries `base_updated`; on save, if the stored note is newer the server returns `409 note-conflict` (§1.3) offering **Keep my version** (`force=1`) or **Discard my changes** — the user's text is on the screen in both branches.
- Universal backstop: **every failure re-render returns the submitted content to the fields.** No path loses typed text, with or without JS.

### 6.3 Delete confirmation

Exactly the two-phase rule of §2.5: row affordance → `GET /memory?confirm-delete=<id>` (pure read) → `POST /memory/delete` with `confirmed=1`. Cancel is autofocused; the note's name is quoted verbatim so the user confirms *this* note; `400 delete-unconfirmed` closes the bypass; the router doc is not deletable at all.

### 6.4 Search result → note (and back)

A hit links to `GET /memory?edit=<id>&from_q=<q>`. The editor's back link becomes **← Back to results** and returns to `GET /memory?q=<from_q>` — the whole round-trip is stateless (query in the URL, no session, no disk mutation). The editor also shows the hit's snippet and score in its meta line when arriving from search, so "why did this match?" survives the click.

### 6.5 Telling a semantic hit from a keyword hit

Three signals, always together: **grouping** (separate "Semantic matches" / "Keyword matches" sections with one-line hints), **badge** (`semantic · 82%` in the accent tint vs `keyword` neutral), and **the score itself** — a percentage plus a proportional bar exists *only* on semantic hits (§2.3). A dual hit shows the semantic badge + `also keyword` tag. Nobody has to guess which algorithm produced a row.

---

## 7. Non-goals (v1)

- **Type-ahead / live search-as-you-type** — needs a fragment endpoint (`/memory/search`, reserved); the ~15 ms full-page render makes it unnecessary.
- **Markdown preview pane** — notes are stored and shown verbatim; the editor is mono for fidelity, not for rendering.
- **Autosave, sessions, server-side dirty state** — §6.1–6.2 cover the risk they would address.
- **Multi-select / bulk delete** — delete is one note at a time; the full-page confirm that guards it is the proposal (§2.5).

---

## 8. Anti-drift rules

Every future memory/panel feature must obey these. The panel, the Android app and the Qt desktop shell all follow them: a surface keeps its hex in its one token block and reads it by role everywhere else.

1. **One accent hue.** Pink (`--accent` family) is the only accent that may ever exist. Never introduce a second accent colour.
2. **Destructive ≠ pink.** Delete, overwrite, discard use `--danger` / `--danger-soft` exclusively. Pink is for primary *constructive* action, active state and emphasis.
3. **Status colours are status only.** Success/warning/danger never use pink, and pink never signals an error or a warning — including in text, badges and meter fills.
4. **Tokens or nothing.** Component CSS references custom properties only; raw hex appears exclusively inside a surface's own token block (the §4 block here, the panel's two `:root` blocks in `_CSS`). Everything else uses `var()` or `color-mix()`. Radii, spacing, durations, shadows and fonts follow the same rule: a value that has no variable yet is added to the token block before a component uses it.
5. **One law, one block per surface.** The values in §4 are shared; the variable names are not. Add a colour to §4 first, then mirror its value in the panel's `:root` block and in `android/.../values/colors.xml` under whatever names that surface already uses. No surface invents a colour that is not in §4.
6. **Dark-first, with a real light block.** Every component must be legible on `--bg` / `--surface-1` as shipped. The panel also ships a `prefers-color-scheme: light` block, so a new token is added to both blocks and checked against the light values in §4.1; the light accent is the darker `#b93e83`, never `#cf4b92`.
7. **Routes are canonical.** Memory features extend the `/memory` family. The shipped routes are `/memory`, `/memory/save` and `/memory/delete`; `/memory/reindex` and the reserved `/memory/search` are proposal-only. No parallel CRUD routes, no renamed fields: the §1.3 whitelist is the contract for the proposed editor.
8. **Writes write; views view.** Any new route does origin check → `refuse_key_fields()` → body cap → mutate, in that order, on POST only. A GET may only read.
9. **Failures preserve input; views never 500.** Every new write route re-renders with submitted content on user-fixable errors and shows a plain-text reason phrase (`4xx code-word`) on hostile ones. The database being down is a designed state, not a crash.
10. **Copy tone.** Sentence case, no exclamation marks, button labels are verbs ("Save note", "Delete note", never "OK"/"Yes"). Error copy states what happened and what to do next, in one sentence.
