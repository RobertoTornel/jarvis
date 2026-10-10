# Phone Bridge Specification (R-IO-1, R-LOC-1, R-VISION-1)

`src/claire/phone/bridge.py` is the one HTTP surface in Claire. It lets the
owner's phone speak to her (`POST /text`), send her the two things the laptop
cannot supply — a camera frame (`POST /see`) and a position (`POST /loc`) — ask
what she has to say (`GET /reply`) and check the link (`GET /health`).

Owner: the phone-bridge agent. Files: `src/claire/phone/bridge.py`,
`src/claire/phone/__main__.py` (the CLI entry point: `python -m claire.phone
serve` runs the bridge, and `keys ...` manages its bearer token),
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
| `/say` | POST | `application/json` `{"text":"..."}` **or** `{"command":"photo"\|"location", ...}` | `200` `{"ok":true,"queued":{...},"count":n}` |
| `/see` | POST | raw image (`image/jpeg`/`image/png`/`image/webp`), question via `?question=` or `X-Claire-Question`; **or** `application/json` `{"image_base64":"...","question":"..."}` | `200` `{"ok":true,"seen":true,"description":"..."}` or a named not-seen failure |
| `/loc` | POST | `application/json` `{"latitude":..,"longitude":..,"accuracy":..}` (`lat`/`lon`/`lng` accepted, `accuracy_m` too) | `200` `{"ok":true,"recorded":true,"accuracy_m":..,"received_at":..}` — acknowledged, never read back |
| `/reply` | GET | — | `200` `{"ok":true,"items":[...],"count":n}` — drained, once |
| `/health` | GET | — | `200` reachability, capability verdict, **masked** token handle |
| `/transcribe` | POST | raw audio (`audio/wav`, `audio/x-wav`, `audio/wave`, `audio/vnd.wave`); a RIFF/WAVE body is also accepted under any declared type | `200` `{"ok":true,"text":"...","chars":n,"accepted":bool,"delivered":bool,"engine":"...","seconds":s,"sample_rate":h}` |
| `/location` | POST | identical to `/loc` | identical to `/loc` |
| `/where` | GET | — | identical to `/reply` |

Every endpoint is authenticated, **including `/health`**. There is no
unauthenticated surface at all, so there is nothing to probe and nothing to
enumerate.

### 2.1 The port is 8788

`claire.panel.server` defaults to **8765** and something else on this machine
(`dsh web`) holds **8731**, which the shipped 0.2 phone client hard-coded. The
bridge's old default of 8765 could therefore never bind: `python -m
claire.phone serve` failed with `EADDRINUSE` and the two services silently
competed for one port. `DEFAULT_PORT` is now **8788**, which is free on this
host; `$CLAIRE_PHONE_PORT` still overrides it.
`test_the_phone_bridge_does_not_share_the_web_panels_port` asserts the two
defaults differ, so the collision cannot come back.

### 2.2 `/transcribe` is audio, not a rename of `/text`

An always-listening client has audio; before this route existed the bridge
accepted only text, so a buttonless client had nothing to call. `/transcribe`
takes one utterance of WAV and hands it to **the voice loop's own recogniser** —
`claire.listening.stt.create_stt_engine` over `claire.config.load_settings`,
exactly the engine `claire.listening.listener` uses. There is one transcription
stack in this project, not two, and
`test_transcribe_uses_the_projects_own_stt_engine_and_not_a_second_stack` reads
the source to say so.

It differs from `/text` in one deliberate way. The transcript *is* the answer, so
a caller whose laptop has no daemon running still gets the words back —
`accepted: false`, `delivered: false` and a `note` saying they were not passed
on. Silence is never delivered: a recogniser that returns nothing produces
`"text": ""`, `delivered: false` and nothing reaching the voice loop, because an
empty utterance in a conversation is worse than no utterance.

Audio that is refused is refused *by name*: a non-RIFF body under an audio
content type is `415`, a body that claims to be WAV and is not is `400
invalid_audio`, a clip shorter than 0.05 s or longer than 120 s is a `400` that
says which, and a recogniser that cannot be built is `503
transcription_unavailable` — remembered, so a 2.5 GB checkpoint is not reloaded
per request.

### 2.3 `/location` and `/where` are aliases, and why

The 0.2 client in the owner's hand posts to `/location` and reads `/where`. The
bridge serves `/loc` and `/reply`. Renaming either side silently breaks a working
phone, so the alias is one table, `ROUTE_ALIASES`, mapping the old spelling to
the canonical handler. The canonical spellings remain canonical — nothing in the
client documentation points at the aliases — and the alias table is the only
place that knows the old names. `test_location_is_loc_over_a_real_socket` and
`test_where_drains_the_reply_queue_exactly_once_over_a_real_socket` pin both,
over a real socket, including that `/where` still drains rather than replays.

`/reply` drains: the first call returns the queue and empties it, the next
returns `items: []`. A queue that replayed the same sentence on every poll would
make the phone say "yes?" forever.

### 2.4 `/say` is the channel back, and it exists because the bridge is a process

`/text`, `/see`, `/loc` and `/transcribe` all carry something *from* the phone
*to* Claire. `/say` carries the other way: Claire's own processes put an item
into the same drain-once queue that `GET /reply` serves. It exists because the
bridge normally runs as its own process — `python -m claire.phone serve` — so the
voice loop cannot call `ReplyQueue.put_text()` on the live queue; it has to speak
to the bridge over HTTP like any other client. Before this route there was no way
for the daemon to push a sentence, or a command, to the phone except by being the
same process as the bridge.

The body is `application/json` and is one of:

* `{"text": "good morning"}` — queued exactly as `ReplyQueue.put_text()` queues
  it, `{"kind":"text","text":"good morning"}`;
* `{"command": "photo"}` or `{"command": "location"}` — queued as
  `{"kind":"command","command":"photo"}`. `kind` defaults to `command` when a
  `command` key is present and to `text` otherwise; an explicit `kind` always
  wins, so `{"text":"...","kind":"command","command":"photo"}` is a command that
  still carries the text;
* any other keys (`question`, `id`, …) are preserved verbatim, which is how a
  photo request carries the question the phone should put to its camera.

The command vocabulary is closed and tiny — exactly **`photo`** and
**`location`**, the two things the shipped phone client already implements. A
`kind` of `command` with a missing or unknown `command` is a named `400
say_rejected` whose message *and* remedy name the whole vocabulary; a body with
neither `text` nor `command`, and an empty or whitespace-only `text`, are named
`400`s too. Nothing is queued on a refusal, so a bad `/say` cannot leave a
half-formed item for the phone to render.

The answer is `200 {"ok":true,"queued":<the item as stored>,"count":n}`, where
`count` is the queue depth **after** the enqueue — the same number `/health`
reports as `queue_depth`. `/reply` still drains: an item queued by `/say` is
handed out exactly once, by the next poll, and never again.

## 3. The five invariants, and how each is enforced

### 3.1 Authentication is mandatory, not optional

* The canonical header is `Authorization: Bearer <token>`, parsed by
  `parse_bearer_token` (scheme case-insensitive, per RFC 6750). The 0.2 client's
  `X-Claire-Token: <token>` is accepted as a compatibility spelling
  (`LEGACY_TOKEN_HEADER`); it goes through the same `check_token`, and a request
  carrying it with a wrong value gets the byte-identical 401 that a missing token
  gets (`test_a_wrong_legacy_token_is_401_and_not_an_oracle` over a real
  socket).
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
  `python -m claire.phone keys show PHONE_BRIDGE_TOKEN --reveal` (the one
  deliberate reveal in the package, so the phone can be given the value once), or
  the masked handle from `GET /health` once authenticated. `python -m
  claire.phone keys list` lists every key masked.

### 3.3 Loopback by default; public only by explicit opt-in, and loudly

* `DEFAULT_HOST == "127.0.0.1"` and `DEFAULT_PORT == 8788` (`bridge.py:193`,
  `:203`). This is the *bridge* port and is not the web-panel port: the panel
  (`python -m claire.panel.server`) serves `http://127.0.0.1:8765/`
  (`src/claire/panel/server.py:150`), which is why the bridge moved off 8765.
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
  > (`ssh -N -L 8788:127.0.0.1:8788 owner@laptop`), or put it behind WireGuard.

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
| `invalid_audio` | 400 | the body is not decodable WAV (no RIFF header, truncated container, compressed WAV, no frames, shorter than 0.05 s or longer than 120 s) |
| `transcription_unavailable` | 503 | `/transcribe` and no recogniser could be built; the reason is remembered and reported |
| `transcription_failed` | 502 | the recogniser ran and raised on this audio |
| `sink_unconfigured` | 503 | `/text` with no utterance sink injected; on `/transcribe` it is a `note`, not a failure, because the text is still the answer |
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
`describer`, `queue`, `location_sink`, `photo_dir`, `transcriber`,
`transcriber_factory`, `max_body_bytes`, `host`, `port`, `env`, `clock` — and
`PhoneBridge.handle(method, path, content_type=..., body=..., headers=...,
query=...)` is the entire request path. No global state, no module-level server,
no socket.

The recogniser is resolved lazily and at most once: an injected engine wins, then
an injected factory, then `create_stt_engine(load_settings())`. A resolution
failure is remembered as a `TranscriptionUnavailableError` rather than retried,
which is what keeps a 300-second model load from happening per utterance.

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
| R-AUDIO-1: a WAV round-trips over a real socket into the sink | `test_transcribe_round_trips_audio_over_a_real_socket`, `test_transcribe_downmixes_stereo_to_mono`, `test_transcribe_accepts_a_riff_body_declared_as_octet_stream` |
| audio that is not WAV is refused by name | `test_transcribe_refuses_an_mp3_with_a_named_415`, `test_transcribe_refuses_a_truncated_wav_with_a_named_400`, `test_transcribe_refuses_a_body_that_is_not_a_container_at_all`, `test_transcribe_refuses_an_empty_body`, `test_transcribe_refuses_a_clip_too_short_to_recognise`, `test_transcribe_refuses_an_absurdly_long_clip` |
| no second transcription stack | `test_transcribe_uses_the_projects_own_stt_engine_and_not_a_second_stack` |
| recogniser unavailable/failed, named and remembered | `test_transcribe_names_an_unavailable_recogniser_and_remembers_it`, `test_transcribe_reports_a_recogniser_that_raises_as_a_named_502` |
| no sink still returns the words, silence is never delivered | `test_transcribe_still_returns_the_words_when_no_sink_is_wired`, `test_transcribe_does_not_deliver_an_empty_transcript` |
| audio needs a token too | `test_transcribe_needs_a_token_and_touches_nothing_without_one` |
| the 0.2 client's routes and header still work | `test_location_is_loc_over_a_real_socket`, `test_where_drains_the_reply_queue_exactly_once_over_a_real_socket`, `test_the_legacy_token_header_authenticates_over_a_real_socket`, `test_a_wrong_legacy_token_is_401_and_not_an_oracle` |
| the bridge does not share the panel's port | `test_the_phone_bridge_does_not_share_the_web_panels_port`, `test_health_reports_the_transcribe_capability` |
| a remedy names a script that exists | `test_the_transcribe_remedy_names_a_script_that_exists` |

Two tests are deliberately fail-proof rather than merely behavioural: an
oversized body must be refused with the stream untouched (`ExplodingStream`
raises if read), and a refused public bind must fail before a socket exists
(`ThreadingHTTPServer` is replaced with a bomb). A third replaces `socket.socket`
with a bomb and drives all five endpoints through `handle`, proving the request
path itself needs no network.

Most of the suite never starts a server, binds a port or opens a socket. The
exception is the live-server section at the end of `tests/test_phone_bridge.py`,
which exists because two bugs in this project were invisible to in-process tests:
`_serve` answered `411` to every bodyless `GET` (all 96 tests passed because they
call `handle()` directly) and the web panel's `HeaderMap` crashed on every real
request for the same reason. `/transcribe`, the two aliases and the compatibility
header are therefore each covered **over a real socket** as well as through
`handle`.

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
* **No endpointing on the laptop.** `/transcribe` takes one complete utterance;
  deciding where a sentence starts and ends is the phone's job
  (`android/.../AudioSegmenter.java`), running where the microphone is. The
  laptop has no streaming-audio endpoint and does not want one.
* **No continuous upload by itself.** The bridge receives when a client sends.
  The always-listening behaviour lives in the phone client; this module has no
  timer, no poll loop and no session concept.
* **Not started by this module.** `serve()` is available, but wiring it into the
  daemon's lifecycle (and announcing the masked token once, at startup) is the
  daemon's job.
