# Attribution

Claire is a **fork** of an existing project. This file records exactly what came
from where, so the upstream authors keep the credit and the licence terms stay
visible.

## Upstream project

| | |
|---|---|
| Project | **jarvis** — a local voice assistant |
| Upstream repository | <https://github.com/isair/jarvis> |
| Upstream author | Baris Sencan (`isair`) |
| Fork point | commit `94c54491eba58d63c44f044695d12c61224e45a0` (`94c54491`) |
| Fork branch | `claire-rewrite` |
| Local upstream copy | `/home/mana/Documents/jarvis_real` (kept pristine, read-only) |

The fork point is the tip of upstream `main` at the time Claire was started.
`git diff 94c54491..HEAD` therefore shows every change Claire has made on top of
upstream — the initial commit on `claire-rewrite` is upstream state plus the
mechanical `jarvis` → `claire` rename.

## Licence

Claire is distributed under the **same licence as upstream**, kept verbatim in
[`LICENSE`](LICENSE). In summary — and the licence text governs, not this
summary:

- **Non-commercial use only.** Personal, educational and research use is
  permitted. Copyright notice and licence must be preserved.
- **Commercial use is not permitted** without a separate commercial licence from
  the copyright holder.
- **Derivative works inherit these terms**, so Claire is non-commercial too.

Claire is a private personal/research build and is not sold, hosted, or offered
as a service.

## What was inherited

**Code.** The working tree, test suite, specs, memory system (diary +
knowledge graph + hybrid retrieval), tool architecture, MCP runtime, desktop
app and settings GUI are all inherited from upstream and then renamed and
extended. Upstream `jarvis` is otherwise unmodified at the fork point.

**Design.** The DSH project at `/home/mana/Documents/deepseek/Claire` is a
separate, independent implementation. **No code was copied from it** — it is
written in a different language and stack. What was inherited from it is
*design intent*: measured numbers, decided tradeoffs, named failure modes and
the intent behind its tests. Anything inherited that way is credited in
[`STATUS.md`](STATUS.md) and in the relevant `*.spec.md` files.

## Deletions from upstream

Per the Linux-only and no-dictation decisions, these upstream components were
removed rather than maintained untested:

- the dictation subsystem (module, tests, docs, config keys, UI),
- Windows and macOS packaging, installers, CI jobs and platform branches.

They remain available in upstream at the fork point above.
