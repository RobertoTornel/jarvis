# Discord outreach transport specification

Discord is the **last resort** on Claire's outreach ladder. A low-urgency report
stays in the conversation; medium and high go to Signal first; Discord is tried
only when the channels above it could not deliver. This module is that one rung:
it reads its configuration, decides whether it is configured, posts exactly one
message, and reports the outcome honestly. It does not decide routing or retry
policy — the ladder owns those.

Module: `src/claire/outreach/discord.py`.

## Configuration

Read from the 0600 env file `/home/mana/.config/claire/channels.env` (override
the path with `CLAIRE_CHANNELS_ENV`, or pass one explicitly):

| key | meaning |
| --- | --- |
| `DISCORD_TRANSPORT` | `selfbot` (live, the default when unset), `dryrun` (rehearse only), `off` (unavailable) |
| `DISCORD_SELFBOT_TOKEN` | the Discord user token used in the `Authorization` header |
| `DISCORD_TARGET_USER_ID` | numeric user id to open a DM with |
| `DISCORD_TARGET_CHANNEL_ID` | optional; when set, post straight to that channel and skip the DM lookup |

Parsing skips blank lines and `#` comments, strips a leading `export` and
surrounding quotes. A `#` inside a value is **not** treated as a comment,
because a token may contain one.

The env file is never printed, logged or copied. Its mode is checked: a file
readable by group or others is refused with the exact `chmod 600` fix
(`require_private_mode=False` exists for a deliberately shared fixture).

A missing or unreadable file, a file with the wrong mode, a missing token, a
missing recipient and an unrecognised `DISCORD_TRANSPORT` value each produce a
**named, actionable** error naming the key, the file and the fix. None of them
is a silent no-op, and none of them is reported as success. An unrecognised
`DISCORD_TRANSPORT` value is described rather than quoted, in case the token was
pasted onto the wrong line.

## `available()` and `unavailable_reason`

`available()` returns whether a token **and** a recipient are configured. It is
answered from configuration alone: no HTTP call, no login probe. A probe would
make availability depend on connectivity and would spend the very credential the
caller is asking about.

`DISCORD_TRANSPORT=dryrun` is available (it can rehearse). `off` and an
unrecognised value are not.

`unavailable_reason` returns the joined named blockers when `available()` is
`False`, and `None` when it is `True`, so the ladder can report *why* it skipped
the rung instead of the useless "unavailable". It never quotes a value from the
env file.

## `send(message, *, level, dry_run=True) -> SendResult`

Exactly one message is posted per call. `dry_run` defaults to `True`, so a
careless caller rehearses rather than DMing the user.

`action` follows the shared contract: it says what the transport *did*
(`ACTION_SEND == "send"`), never how urgent the message was. The outcome is read
from the flags and `SendResult.outcome`. Three outcomes are kept distinct and
are never collapsed:

| outcome | `outcome` | `delivered` | `dry_run` | `error` |
| --- | --- | --- | --- | --- |
| sent for real | `sent` | `True` | `False` | `None` |
| rehearsed | `rehearsed` | `False` | `True` | `None` |
| failed | `failed` | `False` | `False` | the reason |

Results are built with the shared `sent_result` / `rehearsed_result` /
`failed_result` helpers rather than hand-written flags, so the result shape stays
the ladder's business.

A dry run makes **no** network call — not even the DM lookup — and the injected
HTTP layer can be asserted untouched. `DISCORD_TRANSPORT=dryrun` forces
rehearsal even when the caller passes `dry_run=False`.

An unconfigured transport reports a **failure**, not a rehearsal, in both modes:
nothing was rehearsed, and a rehearsal would claim the send *would* have worked.
Its `detail` records that no network call was made.

Refused locally, before any HTTP work, with `[invalid_request]`: an empty
message, a message longer than Discord's 2000-character limit, and a blank
`level`. Discord's limit is applied locally so the caller gets the real reason
instead of a generic HTTP 400.

A live send opens a DM channel (`POST /users/@me/channels`) and then posts
(`POST /channels/{id}/messages`); with `DISCORD_TARGET_CHANNEL_ID` it posts
once. A 2xx is the **only** thing that reports delivery. A 2xx whose body
carries no message id is still delivered but says so in `detail` — delivery
simply cannot be re-verified. A 2xx on the DM lookup with no channel id is a
failure, not a silent success.

The HTTP timeout is capped at `MAX_TIMEOUT_SECONDS` (60s) so a hanging request
cannot hold the ladder's turn open indefinitely.

## HTTP failures

A non-2xx is always `outcome="failed"`, `delivered=False`, `dry_run=False`, with
the status named in `error`. The families the ladder treats differently stay
apart, both as an error string prefix and via `category_of(result)`:

| status | category | ladder meaning |
| --- | --- | --- |
| 401 | `credential_rejected` | the token is invalid, expired or revoked — do not retry with it |
| 403 | `credential_rejected` | the credential may not post here (DMs blocked, account locked) |
| 429 | `rate_limited` | the credential is fine; back off. `Retry-After` is quoted when present |
| 5xx | `server_error` | Discord's own side failed; a later retry is reasonable |
| other | `api_error` | the request or target is at fault; retrying unchanged fails the same way |
| — | `transport_error` | the HTTP layer failed (DNS, TLS, timeout, reset), exception class named |
| — | `invalid_request` | refused locally before any HTTP work (empty/oversized message, blank level) |
| — | `not_configured` | a required key is missing or the mode is off/unrecognised |

Every error string is prefixed with the stable machine-readable category in
brackets, e.g. `[rate_limited] HTTP 429 Too Many Requests — …`, so the ladder
can branch without parsing prose. `category_of()` reads that prefix; it returns
`sent` or `rehearsed` for a success or a clean rehearsal.

## HTTP injection

The HTTP layer is injected as anything with a
`post(url, *, json, headers, timeout) -> response` method — a `requests.Session`
by default, or a plain callable with the same signature. Tests and dry runs
therefore never open a socket, and `available()` works with an HTTP layer that
raises on any use.

## Secrets

The token may leave this module only as last-four-plus-fingerprint:

* `redact(value)` → `…<last4>:<sha256[:8]>`; a value shorter than eight
  characters gets no tail at all, because showing the last four of a six
  character secret would be most of the secret;
* `fingerprint(value)` → the first eight hex characters of SHA-256;
* `redact_text(text, secrets)` sweeps response bodies before they are quoted, so
  an upstream that echoes the `Authorization` header cannot turn a Discord 401
  into a credential leak;
* `DiscordConfig.token` is `field(repr=False)` with a hand-written `__repr__`
  that prints only the redaction, and `DiscordTransport.__repr__` likewise.

The token never appears in an exception message, a log line, a `SendResult`,
`detail` or `error`. Log lines that mention the credential mention only its
redaction, and the response body quoted in an error is truncated, single-lined
and swept.

A `SendResult` is never constructed from a value read out of the env file.

## Interface and composition

Composes with the shared interface in `src/claire/outreach/transport.py`:

* `name = "discord"`;
* `level_rank = None` — the shared mark for a last-resort-only rung. Signal is
  rank 1 and is tried first; Discord is never tried while a preferred rung can
  deliver;
* `last_resort = True` and `can_call = False` — the same promise stated
  explicitly (Discord cannot place a voice call);
* `available() -> bool`, `unavailable_reason -> Optional[str]`;
* `send(message, *, level, dry_run=True) -> SendResult`.

`SendResult`, `ACTION_SEND`, `ACTION_CALL`, `OUTCOMES` and the
`sent_result` / `rehearsed_result` / `failed_result` helpers are imported from
`.transport`. If that sibling module cannot be imported (it is written in
parallel by another agent), a field-for-field mirror is defined locally so this
module always imports, `SHARED_INTERFACE` is `False`, and a warning is logged
because the local mirror will not satisfy the ladder's `isinstance` check. The
moment the shared module is importable it wins.

`category_of(result)` maps any result to its machine-readable category.

The module exports `DiscordConfig`, `DiscordTransport`, the `Discord*Error`
family, `redact`, `fingerprint`, `redact_text`, `parse_env_file`, `load_config`,
`category_of`, `SHARED_INTERFACE`, and the `CATEGORY_*` constants.

## Scope

Linux-only, single message per call, no retry, no queue, no attachment support,
no reconnect. Retry and routing policy belong to the ladder.
