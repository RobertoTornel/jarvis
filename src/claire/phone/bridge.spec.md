# Phone Bridge Specification (R-IO-1, R-LOC-1, R-VISION-1)

`src/claire/phone/bridge.py` is the one HTTP surface in Claire. It lets the
owner's phone speak to her (`POST /text`), send her the two things the laptop
cannot supply — a camera frame (`POST /see`) and a position (`POST /loc`) — ask
what she has to say (`GET /reply`) and check the link (`GET /health`).

Owner: the phone-bridge agent. Files: `src/claire/phone/bridge.py`,
`src/claire/phone/__init__.py`, `src/claire/phone/bridge.spec.md`,
`tests/test_phone_bridge.py`. Nothing else is owned here:
`src/claire/platform/keys.py` (the 0600 key store) and
`src/claire/senses/camera.py` (the vision seam) are **read-only** siblings,
called through their public surfaces and never modified.

## 1. Why this module exists

The phone is the device the owner always has. Two of Claire's senses have no
laptop hardware behind them on this machine — `camera.spec.md` records that
`/dev/video*` does not exist and that the honest advice to a camera-less owner
is "send a photo from your phone" — and a laptop cannot tell her where its owner
is when the owner has walked out of the house with the phone in a pocket.

This module is the transport for those three things, and nothing more. It is
deliberately tiny: one file, the standard library only (`http.server`, `json`,
`hmac`, `secrets`, `base64`), no framework, no TLS, no database.

## 2. Endpoints

| endpoint | method | body | answer |
| --- | --- | --- | --- |
| `/text` | POST | `application/json` `{"text": "..."}` | `202` `{"ok":true,"accepted":true,"chars":n}` |
| `/see` | POST | raw image (`image/jpeg`/`image/png`/`image/webp`), question via `?question=` or `X-Claire-Question`; **or** `application/json` `{"image_base64":"...","question":"..."}` | `200` `{"ok":true,"seen":true,"description":"..."}` or a named not-seen failure |
| `/loc` | POST | `application/json` `{"latitude":..,"longitude":..,"accuracy":..}` (`lat`/`lon`/`lng` accepted, `accuracy_m` too) | `200` `{"ok":true,"recorded":{...}}` |
| `/reply` | GET | — | `200` `{"ok":true,"items":[...],"count":n}` — drained, once |
| `/health` | GET | — | `200` reachability, capability verdict, **masked** token handle |

Every endpoint is authenticated, **including `/health`**. There is no
unauthenticated surface at all, so there is nothing to probe and nothing to
enumerate.

`/reply` drains: the first call returns the queue and empties it, the next
returns `items: []`. A queue that replayed the same sentence on every poll would
make the phone say "yes?" forever.

## 3. The five invariants, and how each is enforced

### 3.1 Authentication is mandatory, not optional

* The header is `Authorization: Bearer <token>`, parsed by
  `parse_bearer_token` (scheme case-insensitive, per RFC 6750).
* The comparison is `hmac.compare_digest` in `check_token`, **never `==`**.
  `==` on strings stops at the first differing byte, so response timing leaks
  the matching prefix and an attacker recovers the token one character at a
  time. `check_token` contains no `==` or `!=` at all, and
  `tests/test_phone_bridge.py` parses the function's source to prove it.
* A **missing** token and a **wrong** token produce byte-identical `401`
  responses: one `AuthenticationError`, one message, one remedy. Distinguishing
  them would tell a scanner that a route exists and merely wants a token.
* A **same-length** wrong token is rejected exactly like any other wrong token.
  This is the case a naive `len(a) == len(b)` "optimisation" would get wrong.
* The check runs **first**: `PhoneBridge.handle` authenticates before it routes,
  before it looks at the body, and before it touches the utterance sink, the
  photo store, the reply queue or the location record. An unauthenticated
  `POST /text` leaves the sink empty; an unauthenticated `GET /reply` leaves the
  queue intact; an unauthenticated `POST /see` writes no file.
* Nothing about the endpoint table leaks to a caller without a token, because
  the route check happens after the auth check.

### 3.2 The token is generated on first use, stored 0600, reported masked

* First use: `PhoneBridge.token()` reads `PHONE_BRIDGE_TOKEN` from the project's
  key store (`claire.platform.keys.KeyStore`, 0600 from the moment of creation,
  outside the repository). Absent, it mints `secrets.token_urlsafe(32)`, stores
  it, and logs only `mask(value)`.
* Reported form: `mask()` — last four characters plus a truncated SHA-256
  fingerprint — exactly the convention of `claire.platform.keys`. `/health`
  returns `token` (masked) and `token_fingerprint`; nothing anywhere returns the
  raw value except the one accessor used for the constant-time comparison.
* The token appears in **no** response body, **no** `repr`, **no** error string
  and **no** log line. `PhoneBridge.redact()` replaces it with `mask()` in
  anything logged after it exists, and `PhoneRequestHandler.log_message` logs
  the request line only — never headers, and never the query string.
* A stored token shorter than `MIN_TOKEN_LENGTH` (16) is refused with a named
  `token_unavailable` error and the exact command to replace it: a weak token
  silently authenticating the phone is worse than a named failure.
* Reading it back: on the laptop itself,
  `python -m claire.cli keys get PHONE_BRIDGE_TOKEN`, or the masked handle from
  `GET /health` once authenticated.

### 3.3 Loopback by default; public only by explicit opt-in, and loudly

* `DEFAULT_HOST == "127.0.0.1"` and `DEFAULT_PORT == 8765`.
  `$CLAIRE_PHONE_HOST` / `$CLAIRE_PHONE_PORT` override it.
* `is_loopback_host` accepts `localhost`, `127.0.0.0/8` and `::1` only. A *name
  that resolves* to loopback is not accepted: DNS is not a security decision.
* `resolve_bind_host` raises `PublicBindRefusedError` — **before** any server
  object exists, so a refusal cannot have opened a port — unless the caller
  passes `allow_public_bind=True`.
* The warning, when opted in, is in plain words:

  > SECURITY: the phone bridge is bound to `<host>:<port>`, which is NOT
  > loopback. Everything the phone sends — utterances, photos, your location —
  > and the bearer token itself travel as **unencrypted plain HTTP**, readable
  > and replayable by anyone on that network. A tunnel is the right answer: keep
  > the bridge on `127.0.0.1` and forward to it
  > (`ssh -N -L 8765:127.0.0.1:8765 owner@laptop`), or put it behind WireGuard.

* Because there is no TLS here, the tunnel is not a nicety: it is the only
  supported way to reach this bridge from outside the machine.

### 3.4 No fabrication about photos

* `POST /see` writes the bytes 0600 into the photo directory
  (`$CLAIRE_PHONE_PHOTO_DIR`, else `$XDG_CACHE_HOME/claire/phone`, else
  `~/.cache/claire/phone`; created 0700 on first photo, and a loose existing
  directory is tightened and reported) and hands the **path** to the vision
  seam.
* The seam is `claire.senses.camera.describe_image`, or anything with a
  `describe(image_path, question)` method, or a plain callable with that
  signature — the same shape `camera._invoke_describer` accepts, so a camera
  describer drops in unchanged.
* `seen` is true only with a non-empty description. With **no describer
  configured** (`describer=None` — deliberately distinct from the
  `DEFAULT_DESCRIBER` sentinel that resolves the project seam) the answer is
  `503` `vision_not_configured`, `seen: false`, and **no `description` key at
  all**; the reply says the photo was stored and *not* described, and names the
  `OMNIROUTE_API_KEY` fix.
* A `not_seen` result from the seam (`VisionUnavailableError` → `503`,
  a route failure → `502`), a describer that raises (`502`
  `vision_describer_failed`), an empty answer, a wrong-contract return value and
  an unusable describer object each produce a named, honest not-seen answer.
  None of them can produce a description.
* The stored path is always reported, so a failure is still honest about what
  did happen.

### 3.5 Named failures with the fix

Nothing in this module fails silently. Every failure is a `PhoneBridgeError`
subclass carrying `code`, `message` and `remedy`, serialised as
`{"ok": false, "error": {...}}`:

| code | status | when |
| --- | --- | --- |
| `unauthorized` | 401 | missing or wrong bearer token (identical for both) |
| `length_required` | 411 | no usable `Content-Length` |
| `body_too_large` | 413 | body over `MAX_BODY_BYTES` (8 MiB) — refused **before reading** when declared |
| `unsupported_content_type` | 415 | unknown content type, refused rather than guessed |
| `invalid_payload` | 400 | right type, wrong shape (empty text, bad base64, out-of-range coordinate, negative Content-Length) |
| `not_found` | 404 | no such endpoint |
| `method_not_allowed` | 405 | endpoint exists, wrong verb (with an `Allow` header) |
| `sink_unconfigured` | 503 | `/text` with no utterance sink injected |
| `sink_failed` | 500 | the sink raised — the utterance did **not** reach Claire |
| `location_sink_failed` | 500 | the coordinates were recorded but the consumer failed |
| `photo_store_failed` | 500 | the photo could not be written where the seam can read it |
| `vision_not_configured` | 503 | no describer/key, or the seam reports vision unavailable |
| `vision_describer_failed` / `vision_route_failed` / `vision_not_seen` / `vision_describer_contract` | 502 | the seam ran and did not produce words |
| `vision_describer_unusable` | 500 | the configured describer is neither callable nor `describe`-able |
| `token_unavailable` | 500 | the key store could not supply or create a token |
| `internal_error` | 500 | an unexpected bug: logged with its traceback, answered by name, never a dropped connection |
| `public_bind_refused` | — | a non-loopback bind without `allow_public_bind=True` (raised before any socket) |

The body limit is 8 MiB: a phone photo is megabytes, not gigabytes. An
oversized `Content-Length` is refused **before the stream is read**, so a
hostile declaration costs nothing; the bytes actually read are capped again, so
a lying header cannot get past either.

## 4. Injection: the whole request path is a function call

`PhoneBridge` takes every collaborator by constructor — `store`, `sink`,
`describer`, `queue`, `location_sink`, `photo_dir`, `max_body_bytes`, `host`,
`port`, `env`, `clock` — and `PhoneBridge.handle(method, path, content_type=...,
body=..., headers=..., query=...)` is the entire request path. No global state,
no module-level server, no socket.

`PhoneRequestHandler` is a thin adapter: it reads a body through
`read_request_body`, calls `handle`, writes the JSON response, and logs the
request line only. It is constructed with its bridge injected either as the
class attribute (`PhoneRequestHandler.for_bridge(bridge)`) or on the server
(`server.bridge`). `create_server` is the only function that binds a socket;
`serve` is the only one that serves. Neither is used by the test suite.

### Thread safety

`ReplyQueue` is the one piece of state shared between a request thread
(`GET /reply`) and the voice loop (`push_reply`). Every operation takes the same
`threading.RLock`, and `drain()` swaps the list out in a single step, so an item
is handed out exactly once and never duplicated. `PhoneBridge` guards the token,
the last location and the lazy photo directory with its own lock.

## 5. Requirements as tests

| requirement | test |
| --- | --- |
| R-IO-1: no token → 401, handler did not run | `test_no_token_is_401_and_the_utterance_sink_never_ran`, `test_no_token_does_not_drain_the_reply_queue`, `test_no_token_does_not_store_a_photo` |
| wrong token → 401 | `test_a_wrong_token_is_401_and_nothing_runs` |
| same-length wrong token → 401 | `test_a_wrong_token_of_the_same_length_is_401` |
| not an oracle | `test_missing_and_wrong_tokens_get_byte_identical_responses`, `test_a_401_never_carries_the_offered_token_back` |
| `hmac.compare_digest`, never `==` | `test_check_token_compares_with_hmac_not_equality`, `test_check_token_is_constant_time_and_refuses_an_empty_stored_token` |
| token in the 0600 store, masked only | `test_the_token_is_generated_once_and_reused`, `test_a_weak_stored_token_is_refused_by_name`, `test_health_answers_and_reports_the_masked_token_only` |
| token never in a body, repr or log | `test_the_token_never_appears_in_a_response_body_a_repr_or_a_log_line`, `test_a_token_smuggled_into_a_request_line_is_redacted_before_logging` |
| utterance reaches the injected sink | `test_the_correct_token_delivers_the_utterance_to_the_injected_sink` |
| oversized body → 413, not read | `test_an_oversized_body_is_413_and_never_reaches_the_sink`, `test_a_declared_oversized_body_is_refused_before_the_stream_is_read` |
| unknown content type refused | `test_an_unknown_content_type_is_refused_on_text`, `test_an_unknown_content_type_is_refused_on_see_and_writes_nothing`, `test_a_missing_content_type_is_refused_rather_than_assumed` |
| R-VISION-1: `/see` stores and passes the path | `test_see_stores_the_image_and_passes_the_path_to_the_describer`, `test_see_accepts_the_question_from_the_query_string`, `test_see_accepts_a_base64_photo_in_json` |
| no describer → says so, describes nothing | `test_with_no_describer_the_response_says_so_and_describes_nothing`, `test_a_describer_reporting_not_seen_never_becomes_a_description`, `test_a_raising_describer_is_a_named_failure_with_no_description` |
| R-LOC-1: `/loc` records coordinates | `test_loc_records_the_coordinates`, `test_bad_coordinates_are_named_400s_and_record_nothing` |
| `/reply` once, then empty | `test_reply_returns_queued_items_once_and_then_empty` |
| queue guarded | `test_the_reply_queue_is_guarded_by_a_lock`, `test_the_reply_queue_loses_nothing_and_duplicates_nothing_under_threads` |
| `/health` works | `test_health_answers_and_reports_the_masked_token_only`, `test_health_reports_the_vision_verdict_honestly` |
| default bind is loopback | `test_the_default_bind_host_is_loopback`, `test_only_real_loopback_addresses_count_as_loopback` |
| public bind needs opt-in and warns | `test_a_public_bind_is_refused_without_the_opt_in`, `test_a_public_bind_refusal_never_constructs_a_server`, `test_an_opted_in_public_bind_warns_loudly_in_plain_words` |
| injected collaborators, no socket/thread | `test_the_bridge_takes_every_collaborator_by_constructor`, `test_the_request_handler_is_constructible_with_an_injected_bridge`, `test_the_whole_request_path_runs_with_no_socket_at_all` |

Two tests are deliberately fail-proof rather than merely behavioural: an
oversized body must be refused with the stream untouched (`ExplodingStream`
raises if read), and a refused public bind must fail before a socket exists
(`ThreadingHTTPServer` is replaced with a bomb). A third replaces `socket.socket`
with a bomb and drives all five endpoints through `handle`, proving the request
path itself needs no network.

No test starts a server, binds a port, opens a socket or sends a message.

## 6. Non-goals

* **No TLS.** TLS terminates at a tunnel (ssh `-L`, WireGuard, Tailscale). This
  module's answer to "I want it reachable from outside" is a warning and a
  refusal, not a second crypto implementation.
* **No second HTTP client.** Photos go to the *project's* vision route through
  `claire.senses.camera`; this module never dials a model itself and holds no
  base URL, key or model name of its own.
* **No phone web page.** This is the JSON/token API the phone talks to. A
  camera-capture HTML page, a PWA shell and the QR-code pairing flow are a later
  pass; they will call these same endpoints.
* **No location history or track.** `/loc` records the latest fix and hands it
  to an injected sink; retention, reverse geocoding and privacy policy belong to
  `claire.utils.location` and a later privacy pass.
* **No photo retention policy.** Photos land in the phone cache directory and
  stay there until something else owns deletion; this module does not invent a
  TTL.
* **No motion, no streaming, no continuous upload.** The phone sends when the
  owner acts.
* **Not started by this module.** `serve()` is available, but wiring it into the
  daemon's lifecycle (and announcing the masked token once, at startup) is the
  daemon's job.
