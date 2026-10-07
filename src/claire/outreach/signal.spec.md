# `claire.outreach.signal` — specification

Owner: the Signal transport agent.  Files: `src/claire/outreach/signal.py`,
`tests/test_outreach_signal.py`.  Nothing else in `src/claire/outreach/` is
owned here; `transport.py` and `ladder.py` are read-only siblings.

## 1. Purpose

A Signal transport implementing the shared `Transport` interface, with **two
capabilities** that must never be confused:

* a **text message** (`send_text`, `send`; result `action="send"`), and
* a **voice call** (`place_call`, `call`; result `action="call"`).

Signal is the only transport in Claire's ladder that can call, so a result must
make it unambiguous whether a call was really placed, merely rehearsed, or
impossible.

## 2. Shared-interface compliance

The interface below is what `transport.py`/`ladder.py` actually read (verified
against the landed sibling modules, not assumed):

| member | here | notes |
| --- | --- | --- |
| `name` | `"signal"` | |
| `level_rank` | `1` | preferred rung (`transport.py`: "Signal 1") |
| `last_resort` | `False` | Signal is **not** last resort |
| `available()` | config-only | no device probe, no network |
| `unavailable_reason` | str when `available()` is False, else `None` | read by the ladder as a plain attribute |
| `send(message, *, level, dry_run=True)` | text rung | never escalates to a call |
| `call(message, *, level, dry_run=True)` | voice-call rung | `action="call"`; the ladder's `can_place_call` treats a callable `call` as proof of call capability, so this method's *result* is what must stay honest |
| `can_call` | bool property | true only when calls are enabled **and** the backend can call |
| `supports_calls()` | same value | kept for convenience |

Extra public API (not part of the protocol): `send_text`, `place_call`,
`describe()`, `config`, `sender()`.

### Result shapes (exactly three, matching `SendResult.__post_init__`)

| what happened | `delivered` | `dry_run` | `error` |
| --- | --- | --- | --- |
| sent for real | `True` | `False` | `None` |
| rehearsed | `False` | `True` | `None` |
| failed / impossible | `False` | `False` | named reason |

A failure is **never** `dry_run=True`: otherwise an unconfigured transport would
look like a successful rehearsal, and `SendResult.outcome` would print
"rehearsed" for something that could never be sent.

## 3. Configuration

Read from the 0600 env file `~/.config/claire/channels.env` (override with the
`CLAIRE_CHANNELS_ENV` environment variable, or the `env_file=` argument), with
real environment variables layered **over** the file.  `KEY=VALUE` lines,
`#` comments, optional surrounding quotes.

Key names searched for (first present wins; **all optional**):

| purpose | names |
| --- | --- |
| mode token | `SIGNAL_TRANSPORT` |
| sender account | `SIGNAL_ACCOUNT`, `SIGNAL_NUMBER`, `SIGNAL_PHONE_NUMBER`, `SIGNAL_ACCOUNT_NUMBER`, `SIGNAL_SENDER` |
| recipient | `SIGNAL_RECIPIENT`, `SIGNAL_TARGET_NUMBER`, `SIGNAL_TARGET_USER_ID`, `SIGNAL_TARGET`, `SIGNAL_CONTACT`, `SIGNAL_TO` |
| live opt-in | `SIGNAL_LIVE`, `SIGNAL_ALLOW_LIVE`, `SIGNAL_ENABLE_LIVE` |
| calling opt-in | `SIGNAL_VOICE_CALL`, `SIGNAL_ALLOW_CALLS`, `SIGNAL_CALL_ENABLED` |
| signal-cli path | `SIGNAL_CLI`, `SIGNAL_CLI_PATH` |
| call-capable wrapper | `SIGNAL_CALL_COMMAND`, `SIGNAL_CALL_BINARY` (argv template, `{account}`/`{recipient}`/`{message}` placeholders) |

Truthy = `1, true, yes, on, enable, enabled, live, allow, allowed`
(case-insensitive).

`available()` is true iff a sender account **and** a recipient are known.
`SIGNAL_TRANSPORT` is a *mode* token, not a phone number: when it is present and
is not one of `live, live_send, send, real, production, prod, enabled, on`, the
transport stays in rehearsal even if the live flag is set.  An unrecognised mode
is never read as permission to send.

A non-0600 env file produces a `log.warning` naming the path and the mode — never
the content — and does not block anything.

## 4. Live gating

A real send requires **all** of:

1. `dry_run=False` on the call,
2. `SIGNAL_LIVE` (or an alias) truthy,
3. account + recipient configured, and
4. `SIGNAL_TRANSPORT` absent or holding a recognised live mode token.

Otherwise the result is a **rehearsal** and the `detail` names which gate held.
A dry run returns before touching the injected sender, so it cannot open a
socket, spawn `signal-cli`, or ring a phone.

## 5. Voice calls

Checked in this order:

1. not configured → `SIGNAL_NOT_CONFIGURED` failure;
2. `SIGNAL_VOICE_CALL` not truthy → `SIGNAL_VOICE_CALL_UNSUPPORTED` failure;
3. backend `supports_calls()` false → `SIGNAL_VOICE_CALL_UNSUPPORTED` failure
   (default `SignalCliSender` cannot call: `signal-cli` has no voice-call
   feature, so the fix is a wrapper via `SIGNAL_CALL_COMMAND`);
4. device unavailable → `SIGNAL_DEVICE_UNAVAILABLE` failure;
5. not live-permitted → **rehearsed**;
6. otherwise the backend places the call → **sent** (`action="call"`);
   a backend error → `SIGNAL_SEND_FAILED` failure.

Steps 2–3 precede the dry-run check deliberately: a call that can never be
placed is not a rehearsal, it is impossible, and the caller is told so rather
than shown a reassuring "would have called".

`send(level="critical")` does **not** call by default (`call_on_critical=False`):
the ladder drives the call step through `call()` separately, and escalating in
both places would call twice for one critical message.  Standalone users may set
`call_on_critical=True`; the fallback detail then states plainly that the call
was impossible and that a text went instead.

## 6. Injection seam

Every send goes through an injected `SignalSender`:

```python
class SignalSender(Protocol):
    def available(self) -> bool: ...
    def unavailable_reason(self) -> Optional[str]: ...
    def supports_calls(self) -> bool: ...
    def send_text(self, *, account, recipient, message) -> Optional[str]: ...
    def place_call(self, *, account, recipient, message) -> Optional[str]: ...
```

Capabilities are probed with `getattr`, so a minimal fake with only
`send_text`/`place_call` works.  Unit tests inject a fake; no test spawns
`signal-cli` or makes a network call.  The real backend `SignalCliSender` takes
an injectable `runner`, so even its command construction is testable without a
device.

`resolve_send_result_class()` imports the shared `SendResult` lazily, falling
back to a field-identical local copy if `transport.py` is absent or mid-write.
When the sibling exists, results are instances of **its** class (the ladder's
`isinstance(result, SendResult)` check passes).

## 7. Named errors and their fixes

| code | meaning | fix |
| --- | --- | --- |
| `SIGNAL_NOT_CONFIGURED` | no account/recipient (or no env file) | add the keys to the 0600 env file, or export them |
| `SIGNAL_DEVICE_UNAVAILABLE` | no usable `signal-cli` / backend | install signal-cli, register the device, or set `SIGNAL_CLI` |
| `SIGNAL_VOICE_CALL_UNSUPPORTED` | calling disabled or backend cannot call | set `SIGNAL_VOICE_CALL=1` and configure `SIGNAL_CALL_COMMAND` |
| `SIGNAL_SEND_FAILED` | backend refused, timed out, or raised | the message names the exit/exception and says to retry by hand |

Every message is prefixed with the code and ends with an action.  There is no
silent no-op path.

## 8. Redaction boundary

`mask_secret()` renders a secret as `…<last4> (fp <sha256[:8]>)` or
`present`/`absent`; `redact_text()` masks known secrets (and their digit forms)
plus `+`-prefixed phone-shaped numbers in free text.  Applied to: every
`detail`/`error` field, backend stderr and exception text, log records, and
`SignalConfig.__repr__` / `masked()` (whose value-bearing fields are
`repr=False`).  No result, log line, or exception this module produces contains
a phone number or token value.

## 9. What the real `channels.env` contains (names only)

Inspected on this machine, values never read into prose — reported by shape
only:

| key | status |
| --- | --- |
| `DISCORD_TRANSPORT`, `DISCORD_SELFBOT_TOKEN`, `DISCORD_TARGET_USER_ID` | present (Discord transport, not owned here) |
| `SIGNAL_TRANSPORT` | **present** — last-4 `yrun`, fp `7364247b`; a 6-character non-numeric mode token, *not* a phone number |
| any Signal sender account key | **absent** |
| any Signal recipient key | **absent** |
| `SIGNAL_LIVE` / `SIGNAL_VOICE_CALL` / `SIGNAL_CLI` | **absent** |

Consequence: with the shipped file, `available()` is `False` and the transport
reports `SIGNAL_NOT_CONFIGURED` with the fix (add `SIGNAL_ACCOUNT` and one
recipient key).  That is the honest state — no number is configured, so nothing
can be sent or called for real.

## 10. Test coverage (`tests/test_outreach_signal.py`)

* `available()` by configuration (present/absent, account-only, recipient-only).
* dry-run text and dry-run call: rehearsed, `dry_run=True`, `error is None`, and
  the injected fake's send/call recorders stay **empty**.
* a real send (with the fake's send recorder) reports `delivered=True`,
  `dry_run=False`, and carries the backend message id.
* a real call reports `delivered=True`, `action="call"`.
* backend failure → `delivered=False`, named `SIGNAL_SEND_FAILED`.
* calling unsupported → `SIGNAL_VOICE_CALL_UNSUPPORTED`, never delivery.
* missing credential / missing device → named, actionable failure.
* live gating: no opt-in, unrecognised mode token, `dry_run=True` default.
* `SendResult` invariants: the three shapes, and the resolved class is the
  shared one when present.
* no secret value in any result repr, `describe()`, error text, or log line.
* `SignalCliSender` command construction and error mapping with an injected
  runner (no device).
