# Outreach (urgency ladder) specification

Reaching the owner is a decision, not a channel. The ladder decides whether a
message needs to leave the conversation, and which transport carries it. Channels
are separate modules implementing one shared contract; the ladder knows nothing
about Signal or Discord beyond what those transports declare.

## Contract

A transport implements `claire.outreach.transport.Transport`:

```
name: str
level_rank: int | None
available() -> bool
send(message: str, *, level: str, dry_run: bool = True) -> SendResult
```

`SendResult(action, channel, delivered, dry_run, detail, message_id=None,
error=None)` reports exactly one of three outcomes for every attempt:

* **sent** — a real message/call left the machine: `delivered=True`,
  `dry_run=False`, `error is None`.
* **rehearsed** — a dry run: `delivered=False`, `dry_run=True`, `error is None`.
  No network call was made; the transport reports what it *would* have sent.
* **failed** — a real attempt that did not succeed, with a NAMED reason:
  `delivered=False`, `dry_run=False`, `error="<reason>"`.

Those three are never collapsed. `SendResult` rejects `delivered=False,
dry_run=False, error=None` (the silent no-op), `delivered=True` with
`dry_run=True` (a rehearsal claiming delivery) and `delivered=True` with an
error. `failed_result()` requires a non-empty reason, so a failing transport must
name its missing credential, dependency or transport error — and, where the
transport can, expose an actionable fix.

`level_rank` orders ordinary rungs (smaller first; Signal `1`). `level_rank =
None` means the transport is **not an ordinary rung** — last resort only. A
transport can also declare `last_resort = True`. Discord is always last:
the ladder recognises a `discord`/`discord_*` name as last resort even when a
transport forgets to declare it, so "Discord is never tried first" holds by
construction.

Optional declarations read by the ladder: `can_call` (bool or callable) or an
unmistakable `*_call` name marks a transport as able to place a real voice call;
`call(message, *, level, dry_run)` may be implemented instead of using `send()`
for the call step; `unavailable_reason` carries why `available()` returned False.
Discord is never call-capable.

## Classification

`classify_urgency(text, *, override=None)` returns `low`, `medium`, `high` or
`critical`.

An explicit `override` ALWAYS wins, whatever the wording says; an override that
is not one of the four levels raises a named `ValueError` rather than falling
back to keywords. Otherwise the wording is scanned and the highest matching level
wins: emergency wording (`emergency`, `can't breathe`, `911`, `fire`, ...) is
critical, pressing wording (`urgent`, `asap`, `important`, `deadline`, ...) is
high, acknowledgement wording (`soon`, `today`, `let me know`, `heads up`,
`reminder`, ...) is medium. Negated phrases (`not urgent`, `no rush`) are removed
before the scan. Text that matches nothing is `low`, as is empty or `None` text:
by default, nothing is sent.

## Routing

| level | what happens |
| --- | --- |
| `low` | Nothing is sent. This is a normal, successful outcome and is reported as such: `sent_anything=False`, `nothing_sent_reason` set, `ok=True`, no transport touched. It is never a silent no-op. |
| `medium` | One text through the first transport that works. |
| `high` | A text, retried once on the same transport if the first attempt fails, then falling through to the next transport. |
| `critical` | A text (as `high`) and then a VOICE CALL. |

Transports are tried in order: preferred rungs first (ascending `level_rank`),
last resort last. The first transport that succeeds ends the text step. A
transport that is unavailable, that raises, or that returns a failure never
aborts the ladder: the attempt is recorded in `LadderResult.attempts` (or its
skip with the named reason in `LadderResult.skipped`), and the ladder falls
through to the next transport. Discord is therefore attempted only after every
preferred transport is unavailable or has failed.

For `critical`, the call step considers only declared call-capable transports.
Only Signal can place a call; Discord cannot. The result always says plainly
whether a call was actually placed: `called=True` only for a real call
(`delivered=True`, `dry_run=False`, `action="call"`). When no call was placed,
`call_reason` names why — no call-capable transport, an unavailable one, a
transport that answered with a text instead of a call, or a dry run in which the
call was rehearsed. A transport cannot imply a call happened: an attempt whose
`action` is not `"call"` never counts as a call.

## Dry run is the default

`dry_run` defaults to True on the module convenience `dispatch()` and on
`Ladder`. Nothing leaves the machine unless the caller explicitly passes
`dry_run=False`. In a dry run every transport is asked with `dry_run=True`,
must make no network call, and reports `delivered=False, dry_run=True`. The
message is passed verbatim — the ladder never rewrites it — so the rehearsal is
the exact text that would be sent, and it is handed back as
`LadderResult.rehearsal`.

A rehearsal counts as success for ordering purposes: a dry run never escalates to
a transport (or a channel such as Discord) that the live run would not use.

## Reporting, secrets and honesty

`LadderResult` carries `level`, `action`, `dry_run`, `sent_anything`,
`delivered`, `called`, `call_attempted`, `attempts`, `skipped`, `channel`,
`call_reason`, `nothing_sent_reason`, `error`, `detail` and `rehearsal`.
`attempts` is the ordered list of `SendResult`s, so the caller can see which
transports were tried and what each did; `skipped` holds `name: reason` lines for
transports that were never usable. When a level that must send could not reach
any transport, `error` names the reason instead of silently returning success.

Transport-supplied text (failure detail, `error`, availability reasons) is
masked with `claire.utils.redact` before it enters a result, so a failure message
carrying a bot token or credential cannot leak it into a log or a reply. The
ladder never logs or echoes the message: a dry run returns it as `rehearsal`
only, and a live result carries no copy of the text.
