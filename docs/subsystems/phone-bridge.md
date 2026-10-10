# Phone bridge

**Authoritative spec: `src/claire/phone/bridge.spec.md` (requirements R-IO-1, R-LOC-1, R-VISION-1). Read this when you are changing the bridge's routes, its bearer-token authentication, its bind policy, the photo/vision path, the config keys it honours, or the route a phone utterance takes into the voice loop.**

## Responsibility

`src/claire/phone/bridge.py` is the one HTTP server in Claire. It lets the owner's phone

- speak to her in text (`POST /text`) or audio (`POST /transcribe`),
- supply the two senses the laptop lacks — a camera frame (`POST /see`) and a position (`POST /loc`),
- ask what she has to say (`GET /reply`), take a queued instruction (`POST /say`) and check the link (`GET /health`).

It is a transport and nothing more: standard library only (`http.server`, `json`, `hmac`, `secrets`, `base64`), no framework, no TLS, no database, no retention policy of its own. It does not own the vision seam (`claire.senses.camera`), the key store (`claire.platform.keys`) or the recogniser (`claire.listening.stt`); it calls them.

## Key modules

| Symbol | Location | What it is |
| --- | --- | --- |
| `PhoneBridge` | `src/claire/phone/bridge.py:1169` | The whole request path, with every collaborator injected. `handle` (`:1519`) is socket-free and is what the tests call. |
| `PhoneRequestHandler` | `src/claire/phone/bridge.py:2360` | Thin `BaseHTTPRequestHandler` adapter: read a body, call the bridge, write the response. Holds no policy. |
| `create_server` | `src/claire/phone/bridge.py:2493` | Binds and returns an unstarted `ThreadingHTTPServer`. |
| `serve` | `src/claire/phone/bridge.py:2525` | Blocking serve loop. |
| `ENDPOINTS` | `src/claire/phone/bridge.py:311` | The route table: path to allowed methods. |
| `ROUTE_ALIASES` | `src/claire/phone/bridge.py:327` | `/location` to `/loc`, `/where` to `/reply` (the shipped 0.2 client's spellings). |
| `ReplyQueue` | `src/claire/phone/bridge.py:1084` | Thread-safe, drain-once queue behind `GET /reply`. `drain` (`:1124`) swaps the list out in one step. |
| `check_token` | `src/claire/phone/bridge.py:830` | `hmac.compare_digest` and nothing else. |
| `parse_bearer_token` | `src/claire/phone/bridge.py:848` | Parses `Authorization: Bearer <token>`; scheme case-insensitive. |
| `resolve_bind_host` | `src/claire/phone/bridge.py:919` | Refuses a non-loopback bind before a socket exists. |
| `is_loopback_host` | `src/claire/phone/bridge.py:805` | Accepts `localhost`, `127.0.0.0/8`, `::1` only. |
| `PhoneBridgeError` and subclasses | `src/claire/phone/bridge.py:380` | Every failure is a named code plus an HTTP status, never a bare 500. |
| CLI: `serve`, `keys list/show/set` | `src/claire/phone/__main__.py:9`, `:15`, `:19`, `:26` | `python -m claire.phone serve`, etc. |
| Daemon wiring | `src/claire/daemon.py:1275` | `_start_phone_bridge` binds the bridge in the daemon process and points its sink at the shared reply engine. |

## Transport, protocol and the real port

- The phone connects over **plain HTTP** to `127.0.0.1:8788` by default. The port is `DEFAULT_PORT` at `src/claire/phone/bridge.py:203` (**8788**); the host is `DEFAULT_HOST` at `:193` (`127.0.0.1`).
- Reaching the bridge from a phone means binding a non-loopback address, which is refused unless `allow_public_bind=True` is passed (`resolve_bind_host`, `src/claire/phone/bridge.py:919`) or the daemon's `CLAIRE_PHONE_ALLOW_PUBLIC=1` is set. There is **no TLS**: the bearer token and everything the phone sends travel in the clear, so the supported way to reach it from outside the machine is a tunnel (the warning the code prints says so at `src/claire/phone/bridge.py:938`).
- The handler speaks **HTTP/1.0** (`protocol_version`, `src/claire/phone/bridge.py:2374`): every response carries an accurate `Content-Length` and the connection closes, so there is no keep-alive state to get wrong on a flaky phone link.
- Authentication is two accepted spellings of the same token: canonical `Authorization: Bearer <token>` (`BEARER_SCHEME`, `src/claire/phone/bridge.py:227`) and the 0.2 client's `X-Claire-Token` (`LEGACY_TOKEN_HEADER`, `:279`). Both go through `check_token` (`:830`); the raw value is never reported, only `masked_token()` (`:1461`, last four characters plus a fingerprint).
- The Android client in this repo defaults to the same port: `android/app/src/main/java/com/claire/companion/ClairePrefs.java:80`.

## Endpoints and message shapes

`ENDPOINTS` (`src/claire/phone/bridge.py:311`) is the complete list. Every endpoint is authenticated, including `/health`; there is no unauthenticated surface.

| Method + path | Body | Success answer |
| --- | --- | --- |
| `POST /text` | JSON `{"text": "..."}` | `202 {"ok":true,"accepted":true,"chars":n,"received_at":..}` (`_text`, `:1634`) |
| `POST /transcribe` | raw WAV (`audio/wav`, `audio/x-wav`, `audio/wave`, `audio/vnd.wave`; a RIFF/WAVE body is accepted under any declared type) | `200 {"ok":true,"text":..,"chars":n,"accepted":bool,"delivered":bool,"engine":..,"seconds":..,"sample_rate":..}` (`_transcribe`, `:1740`) |
| `POST /see` | raw `image/jpeg`/`image/png`/`image/webp`, question via `?question=` or `X-Claire-Question`; or JSON `{"image_base64":"...","question":"..."}` | `200 {"ok":true,"seen":true,"description":"..."}` or a named not-seen failure (`_see`, `:1858`) |
| `POST /loc` | JSON `{"latitude":..,"longitude":..,"accuracy":..}` (`lat`/`lon`/`lng` and `accuracy_m` accepted) | `200 {"ok":true,"recorded":true,"accuracy_m":..,"received_at":..}`; the latest fix is held in memory and echoed by `/health` (`_loc`, `:1806`) |
| `POST /say` | JSON `{"text":"..."}` or `{"command":"photo"\|"location", ...}` (`ALLOWED_SAY_COMMANDS`, `:297`); `kind` is inferred, or given and authoritative | `200 {"ok":true,"queued":{...},"count":n}` (`_say`, `:1666`) |
| `GET /reply` | none | `200 {"ok":true,"items":[...],"count":n}` — drained, once (`_reply`, `:1626`) |
| `GET /health` | none | `200` reachability, capability verdict and **masked** token handle (`_health`, `:1603`) |
| `POST /location` | identical to `/loc` | identical to `/loc` |
| `GET /where` | none | identical to `/reply` |

Body size is capped at `MAX_BODY_BYTES` (`src/claire/phone/bridge.py:213`, 8 MiB). JSON routes take `application/json` only (`_json_object`, `:2028`). `/see` accepts `image/jpeg`, `image/png` and `image/webp` (`ALLOWED_IMAGE_CONTENT_TYPES`, `:220`). `GET`, `HEAD`, `DELETE` and `OPTIONS` are treated as bodyless (`BODYLESS_METHODS`, `:337`); a `POST` without a `Content-Length` is `411`.

## Data and control flow

**A phone utterance arriving (typed or spoken).**

1. The phone sends `POST /text` or `POST /transcribe` with the token. `PhoneRequestHandler._serve` (`src/claire/phone/bridge.py:2411`) reads exactly the declared body, then calls `PhoneBridge.handle` (`:1519`).
2. `handle` authenticates first (`authorize`, `:1482`), then checks the route and verb against `ENDPOINTS`, then the body size, then dispatches (`_dispatch`, `:1566`).
3. `/text` builds the utterance `{"kind":"text","text":..,"source":"phone","received_at":..}` and calls the injected `sink`; with no sink wired it raises `SinkUnconfiguredError` (503) rather than dropping the words silently (`_text`, `:1634`).
4. `/transcribe` decodes the WAV, runs the injected recogniser, and — because the transcript *is* the answer — returns the text even when nothing was delivered: `accepted:false`, `delivered:false`, and a note (`:1740`). An empty transcript reaches nothing.
5. In the daemon the sink is `_phone_utterance_sink` (`src/claire/daemon.py:1140`). It classifies the utterance with the pure function `phone_utterance_decision` (`:980`), then either keeps ambient audio without answering, drops echo, or enqueues an addressed utterance (`_phone_enqueue`, `:1059`).
6. The dispatcher thread calls `_phone_answer` (`src/claire/daemon.py:1100`), which runs `submit_text_query` (`:519`, called at `:1135`) — the daemon's shared reply engine and shared dialogue memory, so a sentence typed on the phone and one spoken into the microphone are one conversation.

**Getting the bridge to the listener.** The bridge does not call `VoiceListener` directly. The daemon passes two things in when it builds the bridge (`src/claire/daemon.py:1322-1332`): the sink above, and `transcriber_factory = listener._get_authoritative_stt_engine`, so `/transcribe` uses the loop's own recogniser rather than building a second one. Absent that, the bridge resolves the project default via `create_stt_engine(load_settings())` (`src/claire/phone/bridge.py:1387-1390`).

**A reply going back.**

1. When the reply engine completes, the daemon's `on_complete` handler calls `bridge.push_reply(answer)` (`src/claire/daemon.py:1117`), which puts one item on the same queue `/reply` drains (`ReplyQueue.put_text`, `src/claire/phone/bridge.py:1120`). A failed or empty model call queues an explicit could-not-answer sentence rather than nothing (`src/claire/daemon.py:1122`).
2. The phone's next `GET /reply` drains the queue and gets each item exactly once; the following poll returns `items: []`.
3. When the bridge is its own process (no daemon), the same channel is reachable over HTTP as `POST /say`, which writes into the same queue (`_say`, `src/claire/phone/bridge.py:1666`).

## Wake and utterance handling

`phone_utterance_decision` (`src/claire/daemon.py:980`) returns one of `"answer"`, `"ambient"` or `"echo"`:

- `kind == "text"` is always `"answer"`: typing is deliberate and no microphone was involved (`:1006`).
- An utterance arriving while Claire is speaking, or within `PHONE_ECHO_GRACE_SEC` (`src/claire/daemon.py:954`, 1.5 s) of her last word, is `"echo"` and is dropped — the phone's always-open microphone otherwise feeds her own voice back (`:1008-1011`).
- Otherwise, for `kind == "transcript"`, the loop's own `is_wake_word_detected` matcher is required (`src/claire/daemon.py:1024`, imported at `:48`); a room utterance that does not name her is kept bounded in `_PHONE_AMBIENT` (`:962`) and never answered.
- Addressed utterances queue rather than being refused while busy (`PHONE_QUEUE_MAX`, `src/claire/daemon.py:975`), because "send that again in a moment" loses the words. The utterance text is deliberately not logged; only its length is.

The daemon warms the recogniser at startup with one dummy transcribe so the model's one-time build cost is not paid on the owner's first sentence (`_warm_phone_recogniser`, `src/claire/daemon.py:1209`).

## Configuration keys

| Key | Where | Meaning |
| --- | --- | --- |
| `CLAIRE_PHONE_HOST` | `src/claire/phone/bridge.py:207` | Bind host; default `127.0.0.1`. |
| `CLAIRE_PHONE_PORT` | `src/claire/phone/bridge.py:208` | Bind port; default 8788. A non-numeric value raises a named `ValueError` rather than silently using the default (`default_port`, `:876`). |
| `CLAIRE_PHONE_PHOTO_DIR` | `src/claire/phone/bridge.py:209` | Where `/see` stores photos; else `$XDG_CACHE_HOME/claire/phone`, else `~/.cache/claire/phone` (`default_photo_dir`, `:902`). Created `0700`; a loose directory is tightened and reported. |
| `PHONE_BRIDGE_TOKEN` | `src/claire/phone/bridge.py:230` | The **name** the bearer token is stored under in the OS keychain / `0600` key store. The value is generated with `secrets` on first use and is never printed except by the explicit `keys show --reveal` command (`src/claire/phone/__main__.py:19`). |
| `CLAIRE_PHONE_BRIDGE` | `src/claire/daemon.py:1295` | `0`/`false`/`no` disables starting the bridge in the daemon; unset means on. |
| `CLAIRE_PHONE_ALLOW_PUBLIC` | `src/claire/daemon.py:1303` | `1` permits a non-loopback bind from the daemon; anything else keeps the bridge on loopback. |
| `OMNIROUTE_API_KEY`, `llm_*` | `src/claire/config.py` | Not read by the bridge directly; they reach it only through `load_settings()` when the recogniser is resolved, and through the daemon's LLM stack. The key value is never written to a repository or config file (`src/claire/config.py:48`, `:862`). |

## Extension points

- **Vision.** Inject a `describer` into `PhoneBridge` (`src/claire/phone/bridge.py:1169`): `DEFAULT_DESCRIBER` resolves `claire.senses.camera.describe_image`; an object with `describe(image_path, question)`, a plain callable of that shape, or `None` ("no describer is configured", answered `503 vision_not_configured`) are all accepted.
- **Recogniser.** Inject `transcriber` or a deferred `transcriber_factory` — anything with `transcribe(samples, sample_rate)` and `identity()`. Tests inject a fake rather than loading the 0.6B model.
- **Utterance sink.** Any callable taking one utterance dict. This is the seam the daemon uses.
- **Reply queue.** `ReplyQueue` can be pre-seeded or replaced; `push_reply` (`:2098`) is the public way in.
- **Location sink.** An optional callable given the `LocationRecord` (`:1065`) on `/loc`.
- **Routes.** New routes are added to `ENDPOINTS` and `_dispatch`; compatibility spellings belong in `ROUTE_ALIASES` only.

## Failure modes

- **Port already taken.** Construction of the `ThreadingHTTPServer` raises `OSError` from the bind. `create_server` (`src/claire/phone/bridge.py:2493`) does not swallow it. The daemon retries every `PHONE_BRIDGE_BIND_RETRY_SEC` (5 s, `src/claire/daemon.py:942`) until `PHONE_BRIDGE_BIND_TIMEOUT_SEC` (180 s, `:941`), then logs and returns — voice is unaffected. The standalone `python -m claire.phone serve` fails immediately.
- **Listener/daemon absent.** The bridge's default sink is `None`; `/text` answers `503 sink_unconfigured` (`:1634`) instead of silently dropping the words, and `/transcribe` still returns the transcript with `accepted:false`, `delivered:false` and a note (`:1740`).
- **Recogniser cannot be built.** `503 transcription_unavailable`; the failure is remembered, so a 2.5 GB checkpoint is not reloaded per request (`:679`).
- **Malformed body.** Bad JSON, a missing/empty `text`, out-of-range or non-numeric coordinates, an over-long question (over `MAX_QUESTION_CHARS`, `:244`) are `400 invalid_payload` with a remedy. An unsupported content type is `415`; an oversized body is `413`; a `POST` with no `Content-Length` is `411`.
- **Unknown route / wrong verb.** `404 not_found` and `405 method_not_allowed` (with `Allow`), both after authentication, so neither leaks to a caller without a token.
- **Token cannot be read or is too weak.** `500 token_unavailable` naming the store path and a fix; a stored token shorter than `MIN_TOKEN_LENGTH` (`:237`, 16 characters) is refused rather than accepted.
- **Photo cannot be written.** `500 photo_store_failed`. **Vision fails.** `502`/`503` with the describer's own message, and `seen:false`.
- **No bridge attached to a handler.** A named `500 bridge_not_attached` (`_serve`, `:2413`), not a traceback.
- **Public bind without opt-in.** `PublicBindRefusedError` (`:744`), raised before a socket exists.

## Tests

- `tests/test_phone_bridge.py` (2308 lines) — the route, error and socket coverage; includes `test_the_phone_bridge_does_not_share_the_web_panels_port` (`:1918`) and source-reading assertions such as the "one transcription stack" test.
- `tests/test_phone_wake_gate.py` (552 lines) — the wake/ambient/echo decision in the daemon.

## Documentation drift

1. **The daemon does not wire the bridge.** `src/claire/phone/__main__.py:3-4` says "`claire.daemon` does not reference it and no other module imports it, so the bridge was reachable only from a Python REPL." The code contradicts this: `src/claire/daemon.py:1275` defines `_start_phone_bridge`, `:1319` imports `PhoneBridge`/`create_server`, and `:1581` calls it after the listener starts. `docs/PHONE_BRIDGE_LIVE_TEST.md:50` and `:157` repeat the same stale claim ("`claire.daemon` does not reference the phone bridge at all"; "The daemon does not wire the bridge").
2. **"There is no phone app in this repo."** `docs/PHONE_BRIDGE_LIVE_TEST.md:9` and `:14` say no APK, `AndroidManifest.xml`, Gradle project or `.java`/`.kt` source exists anywhere under this repository. The `android/` directory is tracked here (61 files, including `android/app/src/main/AndroidManifest.xml` and built APKs under `android/app/build/outputs/apk/`). `docs/PHONE_CLIENT.md:1-13` states plainly that it supersedes those statements, but the older file still carries them. `docs/PHONE_BRIDGE_LIVE_TEST.md:11` also says `src/claire/phone/` contains three files; it contains four (`bridge.py`, `bridge.spec.md`, `__init__.py`, `__main__.py`).
3. **`serve()` names a non-existent entry point.** `src/claire/phone/bridge.py:2534` calls itself "the blocking, real-process entry point (`python -m claire.phone.bridge`)", but `bridge.py` has no `if __name__ == "__main__"` block, so that invocation defines the module and exits. The real entry point is `python -m claire.phone serve` (`src/claire/phone/__main__.py:9`, implemented at `:192`).
4. **Spec ownership list is incomplete.** `src/claire/phone/bridge.spec.md:8-10` lists the owned files and omits `src/claire/phone/__main__.py`, which the spec's own section 2.4 (`:96-125`) relies on as the way the bridge runs as its own process.
