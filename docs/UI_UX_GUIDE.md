# UI/UX decisions guide

| | |
|---|---|
| **Purpose** | How to add or restyle a surface or component in Claire without reverse-engineering the stylesheet or guessing at colours |
| **Token authority** | `docs/MEMORY_EDITOR_UX.md` §4 (the token table) and §4.1 (the measured contrast ratios). This guide does not restate that table. |
| **Surfaces covered** | Web control panel (`src/claire/panel/server.py`), Android companion app (`android/`) and the Qt desktop shell (`src/desktop_app/`) |
| **Related pages** | `docs/ANDROID.md`, `docs/subsystems/panel.md`, `docs/MEMORY_EDITOR_UX.md` |

---

## 1. The colour law

One sentence: **dark grey and light grey carry all structure; a single pink accent carries emphasis and interaction state; pink is never used for a destructive or error state.**

The reasoning, briefly:

- **Greys do the work.** Structure — backgrounds, surfaces, borders, text — lives in grey only. That is what makes the accent unambiguous: on any screen, whatever is pink is where the eye is meant to go (the primary action, the active state, the assistant's identity).
- **One accent, and only one.** A second accent hue would compete with the first for exactly those jobs. Green, amber and red already own the semantic-state slot, so a second accent would buy nothing and cost legibility.
- **Pink is not a status colour.** Success, warning and danger keep their own hues (green `#4cc38a`, amber `#d9a441`, red `#ef6a6a` in dark mode). Pink marks emphasis and interaction; it never means "something went wrong" and never appears on a destructive control.

The authoritative token table — three surface elevations, three border weights, three text weights, one accent with hover/active/focus/on-accent steps, and the three state colours — is §4 of `docs/MEMORY_EDITOR_UX.md`. The measured contrast ratios for the dark-mode pairs are §4.1. Do not add a token without adding it there first.

## 2. Applying the law to a new surface

Three rules, in order:

1. **Take a token; never write a literal.** A component uses the existing variables (panel) or resources (Android). The panel forbids a raw hex outside the `:root` token block; the Android resource tree carries none outside `colors.xml`, which is where the palette lives. Derived tints are made with `color-mix()` on the panel (for example a 6% accent row-hover tint) rather than a new colour.
2. **Choose by role, not by shade.** Background → the surface tokens; structure lines → the border tokens; body copy → the primary text token; labels and meta → secondary; hints, placeholders, timestamps → tertiary. Emphasis, primary actions, active states → the accent. Success/warning/danger → the three state colours.
3. **A new pair must clear the floors.** WCAG AA: **4.5:1 for body text**, **3:1 for large text and for non-text indicators** such as focus rings, borders and icons. If the pair you picked does not clear the floor, change the token value and re-measure — do not nudge the element's size or weight to sneak under.

The worked example is the panel's light theme. The dark-mode pressed value `#cf4b92` was rejected as the light-theme accent: 3.79:1 as text on the light background and 4.14:1 as ink on it, both below AA's 4.5:1. The same hue at `#b93e83` reaches 4.72:1 and 5.15:1. Same hue, darker value — not a second accent.

When adding a component:

1. Find the token by role (rule 2).
2. If no token fits, derive one from an existing token (`color-mix()` on the panel) or reuse the nearest resource on Android — never invent a hex.
3. Check the contrast of every new text and non-text pair against the floors.
4. Apply the change to every surface that uses the token (see §3) and update §4 and §4.1 of `docs/MEMORY_EDITOR_UX.md`.

## 3. Per-surface mapping

The law is one law, but each surface spells it in its own names. This is the single largest maintenance risk: **the names are different and the values are shared**. The panel and Android each apply a change by hand; the Qt desktop shell reads its values from one module, so a change there lands in one place.

### Panel: CSS custom properties in `_CSS`

The panel's whole colour system is the token block at the top of the `_CSS` string in `src/claire/panel/server.py`, inlined into every page's `<style>` by `_page`. It is two blocks: the dark values in `:root`, and a light block under `@media (prefers-color-scheme: light)` that re-points every colour token.

| Panel variable | Law token (spec §4) | Notes |
|---|---|---|
| `--bg`, `--bg-soft` | `--bg`, `--surface-1` | `--bg-soft` also serves as the masthead background |
| `--card`, `--card-2` | `--surface-1`, `--surface-2` | cards/rows and inputs/hovered rows |
| `--line`, `--line-soft` | `--border`, `--border-subtle` | the strong border has no panel variable |
| `--ink`, `--ink-soft`, `--ink-faint` | `--text-primary/secondary/tertiary` | |
| `--accent`, `--accent-ink` | `--accent`, `--accent-contrast` | |
| `--ok`, `--warn`, `--bad` | `--success`, `--warning`, `--danger` | panel naming is its own |
| `--radius`, `--radius-sm`, `--shadow`, `--sans`, `--mono` | — | not colour tokens; panel-local |

The panel expresses hover/pressed/focus **without** the spec's interaction tokens — see §7.

### Android: resource names in `colors.xml` mapped by `themes.xml`

The app's whole colour system is `android/app/src/main/res/values/colors.xml`: twenty colour resources whose names are stable and whose values are mapped onto the law. `themes.xml` then fills Material 3 roles from those resources (`colorPrimary` ← `accent`, `colorSurface` ← `surface`, `colorError` ← `error`, and so on), and components read the roles. Changing a value is one line in `colors.xml`; changing a *name* touches `themes.xml` and the layouts. The full resource table is in `docs/ANDROID.md`.

### Qt desktop shell: the `COLORS` role map in `desktop_app/themes.py`

The shell's whole colour system is the `COLOUR_LAW` block in `src/desktop_app/themes.py`, with the Qt role names built from it in `COLORS` and the soft `rgba` variants derived from it by `_tint`. Every other desktop module reads `COLORS`, or renders a stylesheet through `law_style()`, and writes no hex of its own; the source is checked by `tests/test_ui_colour_law.py`. The role names are:

| Qt role | Law token (spec §4) |
|---|---|
| `bg_primary` | `--bg` |
| `bg_secondary`, `bg_card` | `--surface-1` |
| `bg_tertiary` | `--surface-2` |
| `bg_hover` | `--surface-3` |
| `border`, `border_soft` | `--border`, `--border-subtle` |
| `text_primary`, `text_secondary`, `text_muted` | `--text-primary/secondary/tertiary` |
| `accent_primary`, `accent_secondary` | `--accent` |
| `accent_muted` | `--accent-active` (the pressed step) |
| `on_accent` | `--accent-contrast` |
| `success`, `success_light` | `--success` |
| `warning`, `warning_light` | `--warning` |
| `error`, `error_light` | `--danger` |

### Keeping the surfaces in step

When the law changes:

1. Change the value in `docs/MEMORY_EDITOR_UX.md` §4 first, and re-run the contrast measurements for §4.1.
2. Mirror it in the panel's `:root` block — and in the light block if it affects light mode. The light-theme accent is deliberately a darker same-hue value (`#b93e83`), not the dark-mode value; see §2.
3. Mirror it in `android/.../values/colors.xml`, keeping the resource **name** unchanged.
4. The Qt desktop shell needs no edit beyond `desktop_app/themes.py` if only a value changed: its modules read the role map. If a token is added, add its role there too.
5. Never introduce a colour on one surface only. If a token is added to the law, add it to every surface that has a job for it (or record in this guide why one surface omits it, as §7 does).

## 4. Interaction and state

| State | Panel | Android | Rule |
|---|---|---|---|
| Hover | buttons: `brightness(1.07)` filter; links: thicker underline | n/a — touch device | a same-hue step, never a second hue |
| Pressed / active | buttons: `translateY(1px)` | `accent_dim` (the law's pressed value) as `colorPrimaryContainer` and pressed step | darker, same hue |
| Focus | `:focus-visible` gives a 2px outline in `--accent` (offset 2px); inputs take an `--accent` border on `:focus` | system default; no token | the ring colour is the accent — 5.8:1 on the dark background, above the 3:1 non-text floor. There is deliberately no separate focus-ring colour |
| Disabled | not styled today | not styled today | when one is added: greyed (the faint-text/border family), never pink, never red |
| Loading | not styled today; the panel's flows are plain links and form posts with no spinners | not styled today | when one is added: text in the secondary grey or an accent fill — not red |
| Success | `--ok` (text, status dot, meter fill) | `ok` (status chip, status dot, conversation labels) | green means one thing on every screen |
| Warning | `--warn` | `warn` | amber |
| Danger | `--bad` text; destructive buttons are transparent with a `--bad` border and text | `error` (the Material 3 role) and `bad` (the panel-matching alias), both `#ef6a6a` | red, never pink |
| Idle | — | `idle` — the tertiary text grey reused as a state colour (the status chip's resting pill) | grey |

The binding rule: **pink never means "something went wrong"** and never decorates a destructive control. Danger is red on both surfaces; the accent is reserved for emphasis and interaction.

## 5. Accessibility

- **Contrast floors.** 4.5:1 for body text, 3:1 for large text and non-text indicators. The measured dark-mode pairs are in `docs/MEMORY_EDITOR_UX.md` §4.1; the light-theme accent exception is §2 above. The panel renders body text at 15px, so every accent-as-text use is a body-text pair and must clear 4.5:1, not 3:1.
- **Never colour alone.** Colour reinforces, it does not carry: conversation turns carry a name label and a timestamp next to their colour; the panel's status dot sits beside the word it describes; the Android status chip pairs a text label with its dot. A new element must survive with the colour removed.
- **Reduced motion.** The panel's only declared transitions (a button's filter and background colour, 0.12s ease) sit inside `@media (prefers-reduced-motion: no-preference)`. Put any new motion behind the same gate. On Android, honour the system reduce-motion setting before adding animation.
- **Focus visibility.** The panel draws a 2px `:focus-visible` outline in `--accent` on every focusable element. Never remove an outline without a visible replacement; the ring is a non-text indicator and must clear 3:1.

## 6. Dark room first

Every surface is read in a dark room: the panel beside the microphone it controls, the phone at 2 a.m. through a notification shade, the desktop face on a dimmed monitor.

- **Dark is the default, light the override.** The panel's `:root` block holds the dark values and the light block only re-points them under `prefers-color-scheme: light`. The Android app is dark-only (`Theme.Material3.Dark.NoActionBar`) by decision: a second light scheme would be a second palette nobody has ever seen.
- **The accent must stay legible on the darkest surface.** The dark-mode accent measures 5.8:1 on the page background — comfortable for 15px body text. If a token change lowers that, the token is wrong.
- **Motion stays slow and subtle.** 0.12s ease on two properties is the panel's entire motion budget. Nothing bounces, nothing spins.

## 7. What the law does not cover

The law constrains colour only. These are per-surface decisions and this guide does not specify them:

- **Spacing** and layout — grid, card widths, padding.
- **Type scale** — the panel's 15px/1.55 body and its heading sizes are panel-local; Android's come from Material 3 text appearances.
- **Iconography** — the panel has none; Android's icons are its own set.
- **Component shape** — the panel's radius tokens are panel-local; Android's radii come from Material 3 defaults plus two drawables.
- **Motion timing** beyond "slow and subtle", and wording.

### Known divergences (recorded, not smoothed over)

The panel does not implement the spec's interaction tokens as named variables: `--accent-hover` and `--accent-active` are expressed as a `brightness(1.07)` filter and a 1px translation, and `:focus-visible` reuses the base accent rather than a separate `--accent-focus` colour. All three choices clear the relevant contrast floors and were measured, so they stand — but they mean the spec's hover/active/focus tokens describe the *target*, not the panel's current CSS. The Android app similarly omits `accent-hover` and `accent-focus` entirely (no resource counterpart existed, and new resource names were deliberately avoided). Neither surface contradicts the law; both approximate its interaction steps.
