# Phone bridge: live test from the handset

Date: 2026-10-06. Owner's request: "install and test the phone app".

Scope of this note: what the phone bridge actually is, whether a phone-side app
exists, and what happened when the bridge was exercised over Tailscale from the
attached handset (`redmi-15-5g` = `100.68.123.69`) rather than from the laptop.

## 1. There is no phone app in this repo, and none was installed

* `src/claire/phone/` contains three files: `bridge.py`, `bridge.spec.md`,
  `__init__.py`. The bridge is an HTTP **server that runs on the laptop**
  (`http.server`, HMAC-free bearer auth, loopback by default).
* No APK, no `AndroidManifest.xml`, no Gradle project and no `.java`/`.kt`
  source exists anywhere under `/home/mana/Documents/jarvis_cloud`.
* The phone itself has no Claire client installed: `adb shell pm list packages`
  matches `claire`, `companion` and `jarvis` only for Android's own
  `com.android.companiondevicemanager`. **Termux is not installed either**
  (`com.termux` is absent).
* An Android project *does* exist at `/home/mana/Documents/deepseek/Claire/android`
  (with a built debug APK). That directory is outside this repo and out of
  scope, so nothing there was read for interface matching beyond noting it
  exists; it is a **different** project and its wire format is not known to
  match `bridge.spec.md`.

So "the phone app" does not exist as an installable thing. What the phone side
*can* be today, and what was used here, is the Android 15 built-in
`/system/bin/curl` (curl 8.8.0-DEV) driven over `adb shell`. A real client would
have to be one of: a small app built from `bridge.spec.md`, a Termux script, or
a browser page (the GET endpoints are browser-reachable; the POSTs need fetch).

## 2. The bridge's real interface

| | |
| --- | --- |
| default bind | `127.0.0.1:8765`; `$CLAIRE_PHONE_HOST` / `$CLAIRE_PHONE_PORT` override |
| non-loopback bind | refused with `PublicBindRefusedError` **before** a socket exists, unless `allow_public_bind=True` |
| auth | `Authorization: Bearer <token>` on **every** endpoint, `/health` included |
| comparison | `hmac.compare_digest`; missing and wrong tokens give byte-identical 401s |
| token | auto-minted (`secrets.token_urlsafe(32)`) into the 0600 key store under `PHONE_BRIDGE_TOKEN`; reported only masked |
| key store | `~/.config/claire/keys.json`, override with `$CLAIRE_KEY_STORE` |

Endpoints: `POST /text` (`{"text":...}` → 202), `POST /see` (raw
`image/jpeg|png|webp` + optional `?question=` or `X-Claire-Question`, or JSON
`{"image_base64":...}` → 200 with `description`, or a named not-seen failure),
`POST /loc` (`{"latitude","longitude","accuracy"}` → 200 `recorded`),
`GET /reply` (drain-once), `GET /health` (reachability + masked token handle).

`/text` needs an utterance **sink**. Nothing in the repo wires one:
`claire.daemon` does not reference the phone bridge at all, and no module
outside `src/claire/phone/` imports it. Until a sink exists, `/text` answers a
named 503. For a transport test the bridge was started with `--echo`, which
returns each received utterance through `GET /reply`; that is a diagnostic, not
the production wiring.

## 3. What was missing, and what was added

Nothing started the bridge. There was no console script, no `__main__`, and no
daemon hook, so the module was reachable only from a REPL. Added:

* `src/claire/phone/__main__.py` — `python -m claire.phone serve` (with
  `--host`, `--port`, `--allow-public-bind`, `--echo`, `--photo-dir`,
  `--key-store`) and `python -m claire.phone keys list|show|set`. Every command
  reports the token **masked**; `keys show --reveal` is the one deliberate way
  to print the raw value, because the phone has to be given it once.
* Four remedies in `bridge.py` told the owner to run
  `python -m claire.cli keys ...`. That command does not exist —
  `claire.cli` accepts only `--smoke-test` and `--version`, and running the
  advice printed `unrecognized arguments: keys`. They now name
  `python -m claire.phone keys ...`, which exists. `bridge.spec.md` was updated
  to match.

### The real bug the live test found

`PhoneRequestHandler._serve` read the request body *unconditionally*:

```python
body = read_request_body(self.headers.get("Content-Length"), self.rfile, ...)
```

`read_request_body` raises `LengthRequiredError` when there is no
`Content-Length`, and no ordinary client sends one with a bodyless `GET`. So
**every `GET /health` and `GET /reply` over a socket was answered
`411 length_required` before the route was even looked at**:

```
$ adb shell 'curl -s http://100.104.1.69:8765/health'
{"error": {"code": "length_required", ... "status": 411, ...}}   HTTP=411
```

The 96 in-process tests all passed, because they drive `PhoneBridge.handle()`
as a function call and never open a socket — the spec even says so. The
socket-facing layer had no coverage at all, which is why this survived.

Fixed by treating a missing `Content-Length` as an empty body for methods that
never carry one (`GET`, `HEAD`, `DELETE`, `OPTIONS`) and keeping the named 411
for body-carrying methods. Two fail-proof tests were added (RED with the fix
reverted, GREEN with it): `test_a_bodyless_get_health_is_answered_and_not_a_411`
failed `assert 411 == 401`, and the sibling `/reply` test failed `assert 411 ==
200`. A third test, `test_every_remedy_names_a_python_m_command_that_really_parses`,
parses each command the error remedies recommend with the parser the named module
exposes; against the old `claire.cli keys get ...` advice it failed with
`unrecognized arguments: keys get PHONE_BRIDGE_TOKEN`.

## 4. Live results from the phone

Started on the tailnet address (the spec's own warning fires, but Tailscale is
WireGuard, which is the tunnel it recommends; binding to the tailnet IP rather
than `0.0.0.0` keeps it off every other network):

```
.venv/bin/python -m claire.phone serve \
    --host 100.104.1.69 --port 8765 --allow-public-bind --echo
```

Token reported masked as `...OZP8#c60c2ee5`. It was handed to the phone inside a
`chmod 600` curl config (`-K`) so it never appeared in an argument list, a log
line or this note. It was deleted from the phone afterwards.

Every endpoint, run from the handset:

| endpoint | observed from the phone |
| --- | --- |
| `GET /health` | `200`, `{"ok":true,"status":"ok","host":"100.104.1.69","loopback":false,"token":"...OZP8#c60c2ee5","token_fingerprint":"c60c2ee5","vision":"project_default",...}` |
| `POST /text` | `202`, `{"ok":true,"accepted":true,"chars":53,...}` |
| `GET /reply` | `200`, `{"count":1,"items":[{"kind":"text","text":"echo: hello Claire, ..."}]}`; a second call returned `{"count":0,"items":[]}` — drain-once proven |
| `POST /loc` | `200`, `{"ok":true,"recorded":{"latitude":39.4699,"longitude":-0.3763,"accuracy_m":12.5,...}}`; `/health` then reported it under `location` |
| `POST /see` (raw PNG) | `200`, `seen:true` |
| `POST /see` (real 3,129,198-byte camera JPEG + `X-Claire-Question`) | `200`, question echoed back, and a specific description of a printed menu |

Auth, checked from the phone after the fix: `/health` and `/reply` with no token
and with a wrong token both return `401` with byte-identical bodies.

Two things worth recording honestly:

* The first frame posted was a `screencap` of the phone while its screen was
  off. It is mathematically black (mean RGB `0,0,0`, max channel `0`), and the
  vision route said exactly that: *"completely black and featureless"*. The
  vision answer was therefore accurate rather than invented — and `adb shell
  input keyevent KEYCODE_WAKEUP` is refused on this handset
  (`INJECT_EVENTS permission` denied), so a lit-screen capture needed a human.
* Redoing `/see` with a genuine camera JPEG from the phone
  (`/sdcard/DCIM/Camera/IMG_20250307_143149.jpg`) produced a detailed
  description naming "Sandwich Mixto", "Bruce Springsteen", "Calimero",
  "Gus Fring", "Tony Montana" and a "BEBIDAS" drinks section with prices.
  Independent OCR of the stored copy found those same strings, so the vision
  route saw real content. The stored copy's MD5 matched the phone's original
  byte-for-byte (`43d4d575cdd55046206ab3093f57c299`), so the transport is
  lossless.

## 5. What still does not exist

* **A phone-side client.** Nothing installable; testing used `adb shell` +
  the Android system `curl`. Termux was not installed and was not installed
  here.
* **A production utterance sink.** `POST /text` works only because the bridge
  was started with `--echo`. The daemon does not wire the bridge, so in normal
  service the owner's words reach the bridge and then a named 503.
* **TLS.** The bridge is plain HTTP by design and says so; it is safe over
  Tailscale/WireGuard, not over an untrusted LAN.
