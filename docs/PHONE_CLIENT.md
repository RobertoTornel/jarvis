# The phone client: an always-listening Android app for the phone bridge

This note documents the Android client that now lives in
`android/`, the two decisions that had to be made before a line of it could be
written (which port, which protocol), and — in section 8 — exactly what was and
was not verified, including the one thing that is still blocked.

`docs/PHONE_BRIDGE_LIVE_TEST.md` is the companion record, and complements this
one: it is the **bridge-only** live test, driven from the handset over `adb
shell` before this app existed, and it is kept as the record of what was proven
about the bridge without one. This note is the app's own record — what the
Android client is, and what was observed on the phone (§8, §8.1).

## 1. What the app is for

Claire runs on the laptop. The phone is the part of her that is in the room: a
microphone that is always open, a camera that can be pointed at something, and a
position that can be asked for. None of that should need a button pressed, and
none of it should require the phone to be *held*.

So the client is built around one foreground service that listens continuously,
and three things the owner can ask for by hand from either the app or the
listening notification.

## 2. What was built, and where

`android/` is a standalone Gradle project (`com.claire.companion`, versionCode 4,
versionName 0.4.0, minSdk 24, targetSdk 35, AGP 8.5.2, Gradle 8.10.2, Java 17
source level, built with JDK 21). Twenty app classes — every `.java` file under
`app/src/main/java/com/claire/companion/` — plus four tools that run on the
laptop, and two instrumentation test classes — `CameraAndVoiceTest` (7 tests)
and `CompanionUiTest` (4), eleven in all (§8.1, §11):

| Class | What it is |
| --- | --- |
| `ClaireWire.java` | The wire format and nothing else: WAV framing, JSON bodies, query encoding. Pure Java, no Android imports, so it can be compiled and run on the laptop — which is how its output was checked against the real bridge (§5). |
| `AudioSegmenter.java` | The energy VAD: per-frame RMS and zero-crossing rate, an adaptive noise floor, a pre-roll ring, and a hangover. Decides where the owner's sentences start and stop. Pure Java, also laptop-runnable. |
| `ClaireClient.java` | Every HTTP call, on `HttpURLConnection`. One credential, one way of presenting it. `Answer` carries either a status and a body or a transport failure; nothing throws for an HTTP answer. |
| `VoiceService.java` | The foreground service: one service carrying `microphone|location|camera`. Holds the microphone open, segments, uploads, polls for Claire's replies, runs the always-on location stream and the periodic camera, retries across a network drop, watches battery and audio focus, and arms the watchdog. |
| `FishTts.java` | Claire's voice, called from the phone and played while it is still arriving: the Fish Audio TTS request (WAV, not MP3) and streaming playback through `AudioTrack` in `MODE_STREAM`, so no player ever has to trust the placeholder RIFF length. The token is used in one header and never logged; the settings screen shows a fingerprint, never the key. |
| `ClaireState.java` | What this phone is doing right now: `Link`/`Mic`/`Loc`/`Cam` enums, a singleton, volatile fields, listeners on the main thread. The service writes it, the screen only reads it (§12.1). |
| `Survival.java` | The two MIUI exemptions and the watchdog: battery exemption, `SYSTEM_ALERT_WINDOW`, `canRestartInBackground()` (which is exactly the overlay permission), and the inexact 15-minute repeating alarm. |
| `Notifier.java` | Three channels (silent status, real alerts, camera) and the notification actions Photo / Pause / Resume / Stop, plus the nudge the watchdog posts when the platform refuses a restart. |
| `CameraCapture.java` | The camera with no Activity: a `LifecycleOwner` the service drives itself, `ImageCapture` bound alone, in-memory captures. |
| `ServiceActionReceiver.java` / `WatchdogReceiver.java` | The two non-exported receivers behind the notification's Pause/Resume/Stop and the watchdog alarm, both of which nudge rather than pretend when a start is refused. |
| `MainActivity.java` | The one screen: a Material 3 status card (microphone, location, camera, link), the bubble conversation, the four hand-actions, the composer and permission onboarding. |
| `CameraActivity.java` | CameraX preview, capture, review, send to `POST /see`, with the privacy sentence that says nothing leaves the phone until Send. |
| `LocationReporter.java` | One position on demand, and `Continuous` — the always-on stream — with every real failure state named (permission refused, background denied, services off, no provider, failed). |
| `SettingsActivity.java` | Address, port, token, photo question, the always-on switches (continuous location + interval, autonomous photos + period, watchdog), the permission rows, the two exemption rows, and four behaviour switches. |
| `ClairePrefs.java` | Where those live. |
| `Conversation.java` / `ConversationAdapter.java` | The conversation as the phone saw it: an append-only capped list mirrored to `files/conversation.jsonl`, and its `RecyclerView` adapter with the long-press copy. |
| `Diag.java` | The app's own one-line lifecycle diary, under one tag (`ClaireLife`): `note()` and `failed()` lines for the service starting and stopping, the network appearing and disappearing, and the watchdog's decision, so `adb logcat -s ClaireLife` is the whole story and the screen messages stay checkable after the fact. Deliberately carries nothing the owner said, no token and no coordinate. |
| `BootReceiver.java` | Posts a notification after a restart. Deliberately does not start the service (§6). |
| `tools/WireSelfTest.java`, `tools/SegmenterSelfTest.java` | Laptop-runnable checks for the two pure-Java classes. |
| `tools/PhoneE2E.java` | The end-to-end client: drives the app's own `ClaireWire` against a live bridge (§5). |
| `tools/ClientSelfTest.java` | Runs the app's **own** `ClaireClient` against a live bridge on the laptop. `ClaireClient` imports no Android class, so with an `org.json` jar on the classpath the answer parsing, the status handling and the transport-failure path are all testable without a phone. |

## 3. The port: 8788, and why not 8765

The bridge's default was 8765. On this machine that port is **occupied**:

```
$ ss -ltnp | grep -E ':(8765|8731|8788)\b'
LISTEN 0 5 127.0.0.1:8765 ... users:(("python",pid=237702,...))   # claire.panel.server
LISTEN 0 ... 0.0.0.0:8731 ... users:(("node",pid=2509,...))       # dsh web
```

`8765` is the web panel (`claire.panel.server.DEFAULT_PORT`) and `8731` is held
by the harness this session runs in. A phone bridge that cannot bind looks
exactly like a phone bridge that is broken — from the phone, an unreachable
laptop and a laptop that never started are the same 8-second timeout.

So `DEFAULT_PORT` is now **8788**: free, memorable, and in the same "local
service" range as its neighbours. The change is in `bridge.py`, in
`bridge.spec.md` §2.1, and it is guarded by a test that fails if the bridge ever
shares the panel's port or 8731 again
(`test_the_phone_bridge_does_not_share_the_web_panels_port`). Note that
`docs/PHONE_BRIDGE_LIVE_TEST.md` also showed 8765 in its examples; that was
stale, and it has since been corrected to 8788 by the owner of that file.

## 4. The protocol: both spellings, and why both

The shipped 0.2 APK and `bridge.spec.md` disagreed. The old app called
`/transcribe`, `/location`, `/where` and `/health` on port 8731; the bridge
served `/text`, `/see`, `/loc`, `/reply`, `/health`. There were three options;
this took the third:

* **(a) client matches bridge** — cheapest, and breaks the app already on the
  phone the moment the laptop updates.
* **(b) the bridge only grows aliases** — no; the old app's transport is not
  something to build new work on.
* **(c) both.** The new client uses canonical routes only:
  `Authorization: Bearer <token>` against `/text`, `/transcribe`, `/see`,
  `/loc`, `/reply`, `/health`. The bridge **additionally** serves `/location`
  (→ `/loc`) and `/where` (→ `/reply`), and **additionally** accepts the legacy
  `X-Claire-Token` header, through the same `hmac.compare_digest` comparison and
  with byte-identical 401 bodies. A shipped APK keeps working; no new code
  depends on a legacy spelling; the alias set is four lines in `ROUTE_ALIASES`
  and can be deleted when the old app is gone.

`/transcribe` is not a convenience alias. Always-listening means the phone sends
**audio**, and the only authoritative recogniser in this project is
`claire.listening.stt.create_stt_engine`. So `POST /transcribe` takes one
complete utterance of WAV and runs it through that same engine — one
transcription stack in the project, not two — and hands the text to the same
sink `/text` uses, as `{"kind": "transcript", ...}` so the voice loop can tell a
recognised utterance from a typed one.

What `/transcribe` refuses, and how:

| Request | Answer |
| --- | --- |
| `audio/mpeg`, or any non-RIFF type | `415 unsupported_audio_type`, naming the types accepted |
| RIFF body whose container cannot be read | `400 invalid_audio` ("the WAV container could not be read (Error: fmt chunk and/or data chunk missing)") |
| Not RIFF at all | `400 invalid_audio` ("does not start with a RIFF/WAVE header") |
| Under 0.05 s or over 120 s | `400 invalid_audio` |
| Engine missing/misconfigured | `503 transcription_unavailable`, naming `stt_model_path` and the check script |
| Engine raised | `502 transcription_failed` |

200 is the normal answer even when nothing could be delivered — the transcript
*is* the result — with `accepted` / `delivered` flags and a `note` explaining
itself when the words went nowhere or none were intelligible. An empty
transcript is never delivered to the sink.

`/transcribe` also has a longer read timeout on the phone than anything else
(120 s vs 20 s), because the first call of a session may be loading a 2.5 GB
model on the laptop. §5 shows that call taking 65 s.

## 5. What was verified over a real socket, with the real model

Two earlier bugs in this project (a bridge that answered `411` to every bodyless
`GET`, and a panel that crashed on a real request's header map) were invisible to
in-process tests. So none of the following is a unit test: the bridge runs as a
real server with **no injected collaborators at all** — the lazy default path
`load_settings()` → `create_stt_engine()`, i.e. the real Parakeet TDT 0.6B — and
the client is a separate JVM process running the app's **actual** `ClaireWire`
class over `HttpURLConnection`.

The audio is 16 kHz mono PCM16, which is what the phone records and what the
corpus already is, with the phone's own 300 ms pre-roll of silence prepended
(the eval harness in the reference project notes that a streaming transducer
drops the opening words of audio that starts straight into speech; the phone's
segmenter has the same effect). The bytes are therefore exactly the shape of a
real utterance.

```
HEALTH 200 {"endpoints": {"/health":["GET"], "/loc":["POST"], "/location":["POST"],
  "/reply":["GET"], "/see":["POST"], "/text":["POST"], "/transcribe":["POST"],
  "/where":["GET"]}, "port": 8788, "transcribe": "project_default",
  "vision": "project_default", "token": "...6789#2f31a1c0", ...}

LOC  200 {"ok": true, "recorded": {"accuracy_m": 12.5, "latitude": 38.7223, ...}}
TEXT 202 {"accepted": true, "chars": 30, "ok": true}
REPLY 200 {"count": 0, "items": [], "ok": true}

WAV_BYTES 202468 SAMPLES 101212
TRANSCRIBE 200 {"accepted": true, "delivered": true, "engine": "parakeet_nemo",
  "sample_rate": 16000, "seconds": 6.32575, "chars": 41,
  "text": "The bus stop is over there by the corner."}          wall 65.2 s (cold)

TRANSCRIBE 200 {... "text": "I stopped at the shop on my way home yesterday."}
                                                                wall 0.23 s (warm)
TRANSCRIBE 200 {... "text": "Clara, stop talking right now, please."}
                                                                wall 0.21 s (warm)
```

* Two of the three are **word-for-word** the corpus ground truth.
* The third is the recogniser's own error on synthetic speech — "Claire" came
  back as "Clara". That is the model, not the wire: the corpus file's own notes
  record the same class of miss on other phrases. Reported rather than smoothed
  over.
* Warm throughput is ~0.04× real time for 5.9 s of audio.
* The sink received all three as `{"kind": "transcript", "source": "phone",
  "engine": "parakeet_nemo", ...}` — the words reached Claire's loop, not just
  the HTTP answer.

Legacy compatibility, also over the live socket:

```
GET  /where    with X-Claire-Token: <token>   → 200  (the shipped 0.2 app keeps working)
POST /location with Authorization: Bearer     → 200
GET  /health   wrong token, both spellings    → identical 401 bodies (not an oracle)
POST /transcribe  audio/mpeg                  → 415, naming the accepted types
```

### 5.1 The app's own client, run against the live bridge

`ClaireClient` imports no Android class — `HttpURLConnection`, `java.nio` and
`org.json` and nothing else — so it can be executed on the laptop against the
same live bridge. `ClientSelfTest` does exactly that, and it is where a real bug
was found:

```
  ok   health reached the bridge
  ok   health is 200
  ok   health reports the bridge port
  ok   health names the recogniser's state
  ok   health masks the token
  ok   transcribe is 200
  ok   transcribe returns words
  ok   transcribe names the engine
  body: {"accepted": true, "chars": 41, "delivered": true, "engine": "parakeet_nemo",
         "ok": true, "received_at": 1791320740.89, "sample_rate": 16000,
         "seconds": 6.32575, "text": "The bus stop is over there by the corner."}
  ok   booleanField sees delivered=true
  ok   the old substring check would have said false
  ok   and the recogniser now reports itself configured
  ok   text is accepted
  ok   location is accepted
  ok   reply is drained
  ok   a wrong token is refused with 401
  ok   and the refusal is named
  ok   and it carries a remedy
  ok   an unreachable laptop is a transport failure
  ok   and it is not an HTTP status
  ok   and it is described in words
  ok   and ok() is false
  ok   wrong audio type is 415
  ok   and it is not ok()
ALL CHECKS PASSED
```

**The bug.** The client read the delivery flag as
`body.contains("\"delivered\":true")`. That is false for every real answer,
because the bridge is Python and Python's `json.dumps` writes
`{"delivered": true, ...}` with a space after the colon. The app would therefore
have told the owner, for every single utterance it successfully delivered, that
it had been "transcribed but not delivered to Claire's voice loop". Reading a
JSON boolean as text is how that happens; the fix is
`ClaireClient.booleanField`, and the same substring mistake was found and fixed
for `/see`'s `"seen"` flag.

**RED, on the same real answer body.** With `booleanField` mutated back to the
substring test and nothing else changed:

```
  body: {"accepted": true, "chars": 41, "delivered": true, "engine": "parakeet_nemo", ...}
  FAIL booleanField sees delivered=true  (expected true, got false)
  ok   the old substring check would have said false
1 CHECK(S) FAILED
```

**GREEN** with the JSON read: `ALL CHECKS PASSED` (the run above).

**And the check can fail.** To show it discriminates rather than always passing,
the app's `SAMPLE_RATE` was mutated to 8000 in a copy of `ClaireWire.java` and the
same WAV was resent. The bridge believed the header, resampled, and answered:

```
MUTANT: {"sample_rate": 8000, "seconds": 12.6515,
         "text": "The best job is over there by the corner."}
```

Different words, double the duration — the mutation is caught.

The two pure-Java classes are also checked without Android
(`javac`/`java` on JDK 21). `WireSelfTest` covers the WAV header, the JSON
bodies and the percent-encoding; `SegmenterSelfTest` covers the detector's
behaviour: two sentences → two segments (1.7 s / 2.0 s), a quiet room → 0, a
steady fan at amplitudes 900/1500/3000 → 0 (the adaptive floor absorbs it), a
60 ms door slam → 0, a 40-second monologue → 3 segments with a 15.0 s maximum,
digital silence → 0.

## 6. Android's rules, and where they shaped the design

Three platform facts changed the shape of this app, and each is in the code with
the reason:

1. **Android 14+ forbids starting a `microphone`-typed foreground service from
   the background.** `BOOT_COMPLETED` is the background. So `BootReceiver` does
   not start the service — it posts a notification that opens the app, where a
   tap from the owner is the user-initiated signal the platform wants. The other
   way throws `ForegroundServiceStartNotAllowedException` at boot, before any of
   this app's own code can report anything.
2. **Notification actions that start an activity are exempt where actions that
   start a service are not.** So Photo and Location are `PendingIntent.getActivity`
   (CameraActivity, and MainActivity with a `REPORT_LOCATION` action) and only
   Pause is `getService`. This is why "camera at will" works with the screen off.
   The exception is caught anyway (`ForegroundServiceStartNotAllowedException` is
   a `RuntimeException`) and reported as a conversation line, never as a crash.
3. **`POST_NOTIFICATIONS` can be denied, and `NotificationManagerCompat.notify`
   throws without it.** Every alert is wrapped; a missing alert is bad, a crash
   while reporting a missing alert is worse, and the same words are already in
   the conversation view.

All three foreground-service types are declared on the one service:
`android:foregroundServiceType="microphone|location|camera"` on `.VoiceService`
in `app/src/main/AndroidManifest.xml`. That is a requirement, not decoration: a
foreground service that uses the microphone, the camera or location must declare
the matching type or the platform throws at `startForeground()` time. Declaring
all three is safe because the type mask passed to `startForeground()` is built
conditionally — `VoiceService.types()` sets a bit only when that runtime
permission is actually granted, so a declared-but-ungranted type never becomes a
`SecurityException`. §8.1 shows the result on the handset:
`types=0x000000C8`, all three bits at once.

Audio focus is handled in **both directions, and not by grabbing it**. This is an
always-on listener: taking `AUDIOFOCUS_GAIN` would stop the owner's music so
Claire could hear better, which is backwards. So output focus is requested
transient-may-duck only while she *speaks*, and released immediately; what
governs the microphone is `registerAudioRecordingCallback`, the platform's own
answer to "another app is recording, or a call is in progress". When it reports
`isClientSilenced`, capture pauses and the notification says so, instead of
filling the queue with digital silence and then "transcribing" it.

## 7. The energy VAD, honestly

`AudioSegmenter` is RMS plus a zero-crossing gate, an adaptive noise floor, a
300 ms pre-roll ring, a 250 ms minimum of *voiced* frames, a 700 ms hangover and
a 15 s force-close. It is calibrated for 400 ms at start-up, then creeps slowly
while it believes someone is speaking, so a fan does not freeze the floor. It was
tuned against the laptop harness above, not by ear.

What it **cannot** do is know who is speaking, or whether anyone is. A television
in the same room is speech-shaped and amplitude-shaped exactly like a person; on
the `noisy` setting the detector is more willing to call it speech, not less. The
honest mitigations are the ones in the app: the **Pause** action (in the app and
in the notification), the three-level sensitivity setting, and the fact that the
audio goes to the owner's own laptop and nowhere else. A voice-activity detector
that claims to solve this with energy alone is lying; solving it properly needs
speaker identification, which is a different piece of work.

## 8. What was observed on the phone, and what was not

The APK has run on the phone, versionCode 4 / versionName 0.4.0, and the phone
was reachable over wireless `adb` on the tailnet with `adb connect
100.68.123.69:36077`. Read that address — host and port — off the phone's own
**Settings → Developer options → Wireless debugging** screen:
`100.68.123.69:36077` is the pair that was verified working, but the port is
assigned by the phone, and both it and the IP can change between sessions and
reboots. If the connection is refused, re-read them from that screen rather than
assuming the value written here. What follows is only what was seen, with the
command that showed it, and §8.2 lists everything still unproven.

### 8.1 Observed

* **Installed and running.** `adb install -r -t app-debug.apk` → `Success`;
  `dumpsys package com.claire.companion` → `versionCode=4 versionName=0.4.0`,
  `targetSdk=35`, on a Redmi `25057RN09E`, Android 15.
* **One foreground service with all three types, requested at runtime.** After
  the owner's taps (see §8.2), `dumpsys activity services com.claire.companion`:
  `isForeground=true foregroundId=1001 types=0x000000C8`. `0xC8` is
  `MICROPHONE (0x80) | CAMERA (0x40) | LOCATION (0x08)`. The mask is built from
  what is actually granted, so this also proves the permissions were present at
  `startForeground()` time.
* **The notification says what is on, and degrades honestly.** `dumpsys
  notification --noredact`, id 1001 on channel `claire.listening`, importance 2,
  `flags=ONGOING_EVENT|ONLY_ALERT_ONCE|NO_CLEAR|FOREGROUND_SERVICE`, three
  actions:
  * `[0] "Photo" -> startActivity` (a user-initiated activity start, which is why
    the camera may legally open from it),
  * `[1] "Pause" -> broadcastIntent`, `[2] "Stop" -> broadcastIntent` (both to
    `.ServiceActionReceiver`);
  title `Claire is listening`, text
  `Microphone open · Location only while the app is on screen · Camera ready
  (Claire may ask)`. That middle clause is the honest degradation: it says
  *only while the app is on screen* because `ACCESS_BACKGROUND_LOCATION` was not
  granted, which is a different sentence from the one it shows when it is.
* **Continuous location, reaching the bridge.** With continuous mode on at a 30 s
  interval, the app wrote
  `sharing location every 30 seconds (only while this app is on screen: "Allow
  all the time" is not granted)` into the conversation, and the bridge's
  `/health` then reported
  `location={'accuracy_m': 13.5, 'latitude': 37.973541, 'longitude': -1.133373,
  'received_at': ...}` — i.e. a real POST /location arrived from the phone.
* **An autonomous photo, with the camera notice.** `POST /say
  {"kind":"command","command":"photo"}` was drained by the app's reply poll
  (`dumpsys`-free evidence: the conversation gained `Claire asked for a photo`
  then `photo taken — Claire asked through the bridge`), and `dumpsys
  notification` showed a **separate** notification, id 1004 on channel
  `claire.capture`, importance 3, title `Claire took a photo`, text `Claire asked
  through the bridge`. No activity was on screen. The camera is therefore
  genuinely autonomous *and* never silent.
* **The photo upload was attempted and its failure was reported honestly.** The
  app logged `sending the photo was refused with HTTP 500 — {"error":
  {"code":"photo_store_failed", ...}}`. The bridge could not write its photo
  directory because that path is not writable inside the `claire.service`
  sandbox on the laptop (§8.2, item 4) — a host-side fault, not the app's. The
  app's behaviour on it is the point: the bridge's own error code and remedy went
  into the conversation verbatim rather than being retried in a loop or swallowed.
* **The watchdog alarm is armed.** `dumpsys alarm`:
  `tag=*walarm*:com.claire.companion/.WatchdogReceiver`,
  `type=ELAPSED_WAKEUP ... repeatInterval=900000`. Inexact, wakeup-capable, 15
  minutes, and it fires with the device asleep.
* **A full round trip through the microphone, live.** While the phone was in the
  room and someone was talking to it, the conversation filled with `YOU` lines
  transcribed by the bridge (`[parakeet_nemo]`), `PHONE` lines for delivery, and
  `CLAIRE` replies which were then spoken aloud — `dumpsys audio` showed the
  service taking `USAGE_ASSISTANT` focus for the duration of each reply. This is
  the whole loop, observed rather than inferred.
* **A defect the live run found, and the fix.** With `speak_replies` on and the
  microphone open, the phone **heard Claire's own voice**, sent the transcription
  back as the owner's words, and Claire answered herself — visible in
  `files/conversation.jsonl` as the owner apparently saying *"Claire is already
  answering."* The `VOICE_RECOGNITION` audio source is supposed to cancel the
  speaker and on this handset it does not do so well enough. The capture loop now
  drops the microphone's contribution for the duration of a reply (flushing the
  half-sentence that was in flight first, because that one was really the owner),
  with a timed guard so a silent TTS failure cannot leave the microphone deaf.
* **Long-press copy, through the real clipboard.** This phone refuses
  `adb shell input` outright (`Injecting input events requires ... INJECT_EVENTS`),
  so the gesture was exercised by an instrumentation test inside the app's own
  process (`android/app/src/androidTest/.../CompanionUiTest.java`): bind a real
  conversation row, `performLongClick()` on the bubble, then read
  `ClipboardManager.getPrimaryClip()`. **It passed**: the owner's message text was
  on the clipboard, and the copy callback fired. The same single
  `setOnLongClickListener` handles every row, so Claire's replies, the phone's own
  lines and the system lines go through the identical path; the test also asserts
  that a blank line reports failure instead of claiming a copy.
* **The first screen, visually.** `adb exec-out screencap` of the running app
  shows the Material 3 status card (Listening · connected chip; Microphone
  listening; Location only while on screen; Camera ready; Laptop connected; last
  fix), the three tonal actions, the bubble conversation with per-speaker colours,
  the survival hint (`Tap to allow "Display over other apps" — without it Android
  will not let the service restart itself after a kill`), and the composer with
  the `Long-press a message to copy it` hint.

### 8.2 Not observed, and what it would take

1. **The notification's Pause and Stop buttons were never tapped**, and neither
   was Photo. They cannot be: the receivers are `exported="false"` (correctly),
   and input injection is denied by MIUI. The structural evidence is in
   §8.1 — the three actions exist with the right `PendingIntent` kinds — but
   *the taps themselves are unverified and need the owner*. The same is true of
   tapping a conversation bubble by hand.
2. **`ACCESS_BACKGROUND_LOCATION` was never granted**, so the always-on location
   path is only proven in its *degraded* form (while the app is on screen). The
   always-on form is what the code does when the permission is there; that
   specific branch has not run on this handset. Android 11+ will not grant it from
   inside the app — Settings → Permissions → Location → *Allow all the time* is
   the only route, and the app's Settings screen has a row that opens exactly that
   screen.
3. **Surviving a real MIUI kill, doze, and a locked screen for hours was not
   observed.** The alarm is armed (§8.1) and `SYSTEM_ALERT_WINDOW` is what makes an
   automatic restart legal; nothing here can prove the restart until the phone is
   left alone overnight with the exemptions granted. The app's own honest position
   is in §12: with no overlay permission the watchdog may only *notify*.
4. **The bridge's photo directory is read-only in its sandbox**, so no photo has
   been stored end-to-end by the new code. The failure is
   `OSError: [Errno 30] Read-only file system` on
   `/home/mana/.cache/claire/phone/...`; `claire.service`'s mount namespace binds
   `/home/mana/.config/claire` and `/home/mana/.local/share/claire` read-write but
   not `/home/mana/.cache/claire`. This is a host configuration matter (the unit
   is another writer's), and it is why "Claire received a photo" is not claimed:
   she received the *request path* and answered with a named 500.
5. **The Settings screen has not been photographed.** It is not exported, so
   `am start` cannot open it; the instrumentation route that works for the
   adapter does not survive being combined with other tests on this handset, and
   `UiAutomation.takeScreenshot()` crashed the app under MIUI's own screenshot
   pipeline. The layout compiles, every id in it is referenced by code that
   compiles, and the same Material components render on the main screen — but the
   screen itself was not seen, and that is stated rather than implied.
6. **Runtime permissions were lost, and only the owner can restore them.** Running
   the Gradle `connectedDebugAndroidTest` task on this handset uninstalled the app
   (`com.claire.companion` was gone from `pm list packages`), which clears every
   runtime grant. They cannot be put back from here: `pm grant` returns success
   and changes nothing, and `adb install -g` is refused with *"You need the
   android.permission.INSTALL_GRANT_RUNTIME_PERMISSIONS permission to use the
   PackageManager.INSTALL_GRANT_ALL_REQUESTED_PERMISSIONS flag"*. So the app on
   the phone right now has **no** microphone, camera, location or notification
   permission and **no token**, and the owner must grant them and re-enter the
   host/token once. **Do not use `connectedAndroidTest` on this phone**; use
   `adb install -r -t` plus `adb shell am instrument -w`, which does not uninstall
   anything.
7. **The verification session ended with the phone offline** (tailnet
   `redmi-15-5g` offline, `ping` 100% loss) partway through the last test runs, so
   the remaining on-device checks were not repeated. `adb connect` needs the
   phone's wireless-debugging port again when it is back.

### 8.3 Operator notes for the next run

* Reconnect: `adb connect <tailnet-ip>:<port>`; the port is random per session.
  `adb mdns services` does not find a phone that is only reachable over the
  tailnet.
* After the app is reinstalled, `wants_listening`, the token and the always-on
  switches are gone. Set them by hand, or (debug build only)
  `adb shell run-as com.claire.companion cp` a prepared
  `shared_prefs/claire.xml` — `run-as` needs the app to be stopped, or its
  in-memory copy overwrites the file.
* Put a command on the bridge's reply queue without touching the phone:
  `curl -X POST -H "X-Claire-Token: $TOKEN" -H 'Content-Type: application/json'
  -d '{"kind":"command","command":"photo"}' http://<laptop>:8788/say`.
  `/reply` is drain-once, so the phone is the only reader.
* The evidence for a photo is a new file in the bridge's `photo_dir`; for a
  location, the `location` key of `/health`.
* **This tree is written by more than one person at a time.** The APK that
  builds from the working tree may not be the one section 8 observed on the
  phone, so rebuild and re-verify, and do not quote an APK hash without checking
  which edits it contains.


## 9. Security, stated plainly

* The transport is **plain HTTP over the tailnet** — WireGuard underneath, no TLS
  on top. The token is a bearer credential sent in cleartext to anyone who can
  observe the tailnet path. That is defensible for a private tailnet and is not
  defensible on a shared one; `android:usesCleartextTraffic="true"` is required
  for it and is declared.
* The token lives in the app's private `SharedPreferences`, **not** encrypted.
  `allowBackup="false"` and explicit `data_extraction_rules` keep it (and the
  conversation file) out of cloud backup and device transfer. An attacker who can
  read app-private storage on this phone can also just record the microphone, but
  this is a weaker guarantee than "encrypted" and is written down as such.
* Utterances are held in a **bounded** on-phone queue (8) while the laptop is
  unreachable, and the owner is told when an older one is discarded. Hoarding
  minutes of conversation in flash to survive a longer outage is the wrong trade
  for the most private data this app touches.
* Photos are written only to the app's cache and deleted as soon as the upload
  finishes, on every path, including failures. The laptop keeps the photo; the
  phone does not keep a second copy.
* The token is never logged, never placed in a URL, and never in a body — the
  only place it appears is the `Authorization` header. The bridge masks it in
  `/health` and redacts it in request logs. `ClairePrefs` deliberately has no
  `toString()`.

## 10. Reproducing all of it

```sh
# the gate (should be green: 5364 passed, 2 skipped, 25 deselected, 0 failed)
cd /home/mana/Documents/jarvis_cloud
.venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider

# the pure-Java checks
cd android
/usr/lib/jvm/java-21-openjdk-amd64/bin/javac -d /tmp/jt \
    app/src/main/java/com/claire/companion/ClaireWire.java    tools/WireSelfTest.java
/usr/lib/jvm/java-21-openjdk-amd64/bin/javac -d /tmp/jt2 \
    app/src/main/java/com/claire/companion/AudioSegmenter.java tools/SegmenterSelfTest.java
/usr/lib/jvm/java-21-openjdk-amd64/bin/java -cp /tmp/jt  WireSelfTest
/usr/lib/jvm/java-21-openjdk-amd64/bin/java -cp /tmp/jt2 SegmenterSelfTest

# the build, and the manifest of the artefact that was actually built
JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 ./gradlew :app:assembleDebug
/home/mana/android-sdk/build-tools/35.0.0/aapt2 dump xmltree \
    --file AndroidManifest.xml app/build/outputs/apk/debug/app-debug.apk

# the live end-to-end check (paralleling §5)
cd /home/mana/Documents/jarvis_cloud/android
/usr/lib/jvm/java-21-openjdk-amd64/bin/javac -d /tmp/e2e \
    app/src/main/java/com/claire/companion/ClaireWire.java tools/PhoneE2E.java
# ...with a bridge running (see src/claire/phone/bridge.py, --port 8788) and a
# 16 kHz mono PCM16 recording:
/usr/lib/jvm/java-21-openjdk-amd64/bin/java -cp /tmp/e2e PhoneE2E \
    http://<laptop>:8788 <token> utterance.pcm16 --health

# the app's own HTTP client, on the laptop (§5.1).  org.json is not part of the
# JDK: fetch one jar and put android.jar on the *compile* classpath only, for the
# ClairePrefs reference that from() names but never needs here.
AJ=/home/mana/android-sdk/platforms/android-35/android.jar
curl -sSLo /tmp/json.jar https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar
/usr/lib/jvm/java-21-openjdk-amd64/bin/javac -nowarn -cp "$AJ:/tmp/json.jar" -d /tmp/cc \
    app/src/main/java/com/claire/companion/{ClaireWire,ClairePrefs,ClaireClient}.java \
    tools/ClientSelfTest.java
/usr/lib/jvm/java-21-openjdk-amd64/bin/java -cp /tmp/cc:/tmp/json.jar ClientSelfTest \
    http://<laptop>:8788 <token> utterance.pcm16
```

## 11. Files added and changed

Added under `android/`: the Gradle project (`build.gradle`, `settings.gradle`,
`gradle.properties`, `gradlew`, `gradlew.bat`, `gradle/wrapper/*`, `.gitignore` —
`local.properties` is machine-local and deliberately untracked, per `.gitignore`),
`app/build.gradle`, `app/proguard-rules.pro`, `app/src/main/AndroidManifest.xml`,
the app classes and four tools listed in §2, and the resources
(`values/{strings,colors,themes,arrays}.xml`, `drawable/*` — including the
notification, state and action icons — `layout/{activity_main,activity_settings,
activity_camera,item_conversation,row_setting_action}.xml`,
`menu/main.xml`, `xml/data_extraction_rules.xml`).

Added in 0.4.0 (§12): `ClaireState.java`, `Survival.java`, `Notifier.java`,
`CameraCapture.java`, `ServiceActionReceiver.java`, `WatchdogReceiver.java`,
`FishTts.java`, `Diag.java`; and the two instrumentation test classes,
`android/app/src/androidTest/java/com/claire/companion/CameraAndVoiceTest.java`
(7 tests) and
`android/app/src/androidTest/java/com/claire/companion/CompanionUiTest.java`
(4) — see §8.2 item 6 before running anything Gradle-built on the phone. The
0.4.0 layouts are Material 3 across all three screens, and
`ConversationAdapter` is a `RecyclerView.Adapter` rather than the `BaseAdapter`
of 0.3.0.

Changed in the Python side: `src/claire/phone/bridge.py` (port, `/transcribe`,
aliases, legacy header, `decode_wav_audio`), `src/claire/phone/__init__.py`
(exports), `src/claire/phone/bridge.spec.md` (the same, as specification),
`tests/test_phone_bridge.py` (the tests for all of it, every one over a real
socket).

## 12. The 0.4.0 rework: design, always-on, and the limits

Four asks, and what each one cost.

### 12.1 It had to look like a finished app

The 0.3.0 screens were hand-rolled `LinearLayout`s with `TextView` dumps and a
`ListView`, and they looked like a debug harness because they were one. 0.4.0
uses `com.google.android.material:material:1.12.0` properly: `AppTheme` is
`Theme.Material3.Dark.NoActionBar` with the M3 role attributes filled in from a
role-oriented palette, and the screens are built from `MaterialToolbar`,
`MaterialCardView`, `MaterialSwitch` (inside `TextInputLayout`-free settings
groups), `TextInputLayout`/`TextInputEditText`, `MaterialButton`, `Snackbar` and
a `RecyclerView`.

The important part is not the widgets, it is that **state is modelled rather than
printed**. `ClaireState` is a small process-wide observable — `Link{UNKNOWN, OK,
TOKEN_REFUSED, UNREACHABLE}`, `Mic{STOPPED, LISTENING, PAUSED, DENIED}`,
`Loc{OFF, ALWAYS, WHILE_USING, DENIED, SERVICES_OFF}`, `Cam{OFF, ARMED,
CAPTURING, DENIED}` — which the *service* writes as things change and the screen
only reads. Before it, the UI inferred "listening" from a static field, the link
status from whatever the last `/health` happened to say, and everything else from
the last conversation line; that is how an app shows a green "listening" while the
service is dead. The four states the owner named — connected, listening, offline,
error — are now four enums with four colours, and the status card, the chip and the
notification are all rendered from the same snapshot.

Two smaller decisions worth keeping:

* The conversation is bubbles, and the **shape carries the meaning**: the owner's
  words right-aligned in neutral grey, Claire's left-aligned in the pink accent,
  and the phone's own actions and failures full-width and dim. A photo being sent
  is not something either person said, and drawing it as a speech bubble would be
  a small lie about who is talking.
* The two MIUI exemptions and every permission are **rows with a button**, not
  switches. The app cannot grant itself `ACCESS_BACKGROUND_LOCATION` on Android
  11+, and a switch that silently does nothing is worse than a sentence that says
  what to do.

### 12.2 Always listening, stated honestly

One service, three foreground types
(`microphone|location|camera`), and the type mask is computed from what has
actually been granted (`VoiceService.types()`), because requesting a type whose
permission is missing is a `SecurityException` at `startForeground()` time. When a
permission arrives while the service runs, the mask is widened by calling
`startForeground()` again — the only supported way — so always-on modes begin
without a restart.

`Survival.java` holds the two exemptions that decide whether an automatic restart
is even *legal*, and it is deliberately blunt about it: Android 14 forbids
starting a `microphone` foreground service from the background, and
`SYSTEM_ALERT_WINDOW` is the documented exemption that makes it allowed. So
`canRestartInBackground()` is exactly `Settings.canDrawOverlays()` — not
"probably", not "sometimes". The watchdog is an inexact 15-minute repeating
`ELAPSED_REALTIME_WAKEUP` alarm; when it fires and the service is gone it starts
it, and **when the platform refuses it posts a notification instead**, naming the
exception class it caught. There is no third path where it pretends.

What still cannot be guaranteed, in the app's own words on the Settings screen:
without "Display over other apps" an automatic restart will be refused; without an
unrestricted battery MIUI may freeze listening with the screen off; and a boot
cannot start the microphone at all, so `BootReceiver` offers a notification rather
than a service. §8.2 records which of those were and were not observed.

Two failure modes found and fixed during the live run, both worth naming because
they are invisible in a unit test: the service now drops the microphone's
contribution while Claire is speaking (§8.1), and the on-demand location answer
sends a fresh fix even when continuous mode is already running, instead of
answering "already sharing" and leaving the bridge's record stale.

### 12.3 Always location

Continuous mode is a `LocationManager` listener pair (GPS + network) owned by the
service, with a configurable interval and an honest `Outcome` for every way it can
fail: `PERMISSION_DENIED`, `BACKGROUND_DENIED`, `SERVICES_OFF`, `NO_PROVIDER`,
`FAILED`. `hasBackground()` is API-gated (below Android 10 "background" is the
same grant as "while using"), and the difference between the two is not hidden: it
is in the notification text, in the status card, and in the conversation line
written when continuous mode starts. The persistent notification says
`Location every 60s` when it is truly always-on and `Location only while the app
is on screen` when it is not. When the owner taps **Location**, or Claire asks
through `/say`, a one-shot fix is requested and POSTed even if the stream is
already running.

### 12.4 Photos that are never a surprise

Three ways a photo happens: the owner taps Photo in the app or in the
notification; Claire asks with `{"kind":"command","command":"photo"}` through the
bridge's reply queue; or a periodic timer the owner set (default: off). All three
go through one method, and all three are *visible*:

* every autonomous capture posts a notification on its own `claire.capture`
  channel (id 1004, default importance, so it can be silenced by the owner without
  silencing the status line),
* a line goes into the conversation saying who asked, and
* the status card's Camera row reads `ready (Claire may ask)` / `taking a photo` /
  `off` / `permission missing`.

The Settings switch **Autonomous photos** turns the reply-command and periodic
paths off entirely, and when Claire asks while it is off the app writes *"Claire
asked for a photo, but autonomous photos are switched off in Settings, so none was
taken"* into the conversation rather than doing nothing. `CameraCapture` binds
CameraX's `ImageCapture` alone (no preview) to a `LifecycleRegistry` the service
drives itself, because a Service is not a `LifecycleOwner` and `bindToLifecycle`
needs one. Captures are in-memory: no JPEG is written to flash, which is the right
default for a picture of wherever the phone is pointing.

### 12.5 Long-press to copy

Every bubble, in both directions, copies its text on a long press and confirms it
with a snackbar — `toast_copied`, or `toast_copy_failed` for a line with nothing
in it. The gesture is the only thing the row does on touch; a tap deliberately
does nothing, and the hint above the composer says so. §8.1 records the
instrumentation test that observed the text arriving on the real clipboard, since
this phone will not let a shell perform the gesture.

