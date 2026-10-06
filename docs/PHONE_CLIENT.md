# The phone client: an always-listening Android app for the phone bridge

Date: 2026-10-06. This note documents the Android client that now lives in
`android/`, the two decisions that had to be made before a line of it could be
written (which port, which protocol), and — in section 8 — exactly what was and
was not verified, including the one thing that is still blocked.

It supersedes two statements in `docs/PHONE_BRIDGE_LIVE_TEST.md`, which is older
and was correct when written: the bridge no longer listens on **8765** (see
§3), and "there is no phone app in this repo" is no longer true (§2). That file
is not edited here because it is another writer's document.

## 1. What the app is for

Claire runs on the laptop. The phone is the part of her that is in the room: a
microphone that is always open, a camera that can be pointed at something, and a
position that can be asked for. None of that should need a button pressed, and
none of it should require the phone to be *held*.

So the client is built around one foreground service that listens continuously,
and three things the owner can ask for by hand from either the app or the
listening notification.

## 2. What was built, and where

`android/` is a standalone Gradle project (`com.claire.companion`, versionCode 3,
versionName 0.3.0, minSdk 24, targetSdk 35, AGP 8.5.2, Gradle 8.10.2, Java 17
source level, built with JDK 21). Thirteen app classes (~3.6k lines of Java) plus
three tools that run on the laptop:

| Class | What it is |
| --- | --- |
| `ClaireWire.java` | The wire format and nothing else: WAV framing, JSON bodies, query encoding. Pure Java, no Android imports, so it can be compiled and run on the laptop — which is how its output was checked against the real bridge (§5). |
| `AudioSegmenter.java` | The energy VAD: per-frame RMS and zero-crossing rate, an adaptive noise floor, a pre-roll ring, and a hangover. Decides where the owner's sentences start and stop. Pure Java, also laptop-runnable. |
| `ClaireClient.java` | Every HTTP call, on `HttpURLConnection`. One credential, one way of presenting it. `Answer` carries either a status and a body or a transport failure; nothing throws for an HTTP answer. |
| `VoiceService.java` | The foreground service. Holds the microphone open, segments, uploads, polls for Claire's replies, retries across a network drop, watches battery and audio focus. |
| `Notifier.java` | Two channels (silent status, real alerts) and the notification actions Photo / Location / Pause. |
| `MainActivity.java` | The one screen: the dot and the status line, the conversation list, a typed-message box, and Listen / Photo / Location / Settings. Permission onboarding and the link check. |
| `CameraActivity.java` | CameraX preview, capture, review, send to `POST /see`. |
| `LocationReporter.java` | One position on demand, with the three real failure states named (permission refused, services off, no fix). |
| `SettingsActivity.java` | Address, port, token, photo question, and four switches. |
| `ClairePrefs.java` | Where those live. |
| `Conversation.java` / `ConversationAdapter.java` | The conversation as the phone saw it: an append-only capped list mirrored to `files/conversation.jsonl`, and its adapter. |
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

Only the `microphone` foreground-service type is declared. Camera and location
need no service type here: both are used from an activity that the owner opened,
or from a notification action that starts one.

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

## 8. What was NOT verified, and what is blocked

* **The APK has never run on a phone.** Installation is blocked, and not by
  anything in this repository. `adb install` against `502059fb`
  (`25057RN09E`, Android 15) returns
  `INSTALL_FAILED_USER_RESTRICTED: Install canceled by user`, because the
  handset's screen is `mWakefulness=Asleep` and MIUI refuses an install while it
  is off. Waking it is not possible from here: `adb shell input keyevent` is
  refused with `SecurityException: Injecting input events requires ... INJECT_EVENTS`.
  `com.claire.companion` is not present on the device (`pm list packages`).
  **This is a limitation of the environment, and it needs the owner's hands:**
  wake and unlock the phone, ensure Developer options → *Install via USB* is on,
  then `adb -s 502059fb install -r -t
  android/app/build/outputs/apk/debug/app-debug.apk`.
* Consequently **nothing about the running service has been observed**: not the
  microphone, not the notification, not the camera, not the GPS, not the
  doze behaviour, not the reconnect. The VAD and the wire format were verified
  on the laptop; the service that glues them to Android was not.
* The manifest was verified in the **built** APK, not in the source:
  `foregroundServiceType=0x80` (microphone) on `VoiceService`, `BootReceiver`
  with `BOOT_COMPLETED`, the ten permissions, `usesCleartextTraffic`, and exactly
  three exported components: the launcher (`MainActivity`), `BootReceiver`, and
  `androidx.profileinstaller.ProfileInstallReceiver` — the last of which is
  injected by AndroidX, is guarded by `android.permission.DUMP`, and is not this
  app's code. The camera and settings activities and the service are all
  `exported="false"`. The APK is signed with this machine's debug key (v2
  scheme), 4,534,145 bytes,
  sha256 `a3715ba55da147d723d8882d879125f53eba5cf03728c7e62554c176a5884c71`.
  Its three dex files contain the app's classes (`dexdump` finds `MainActivity`,
  `VoiceService`, `ClaireClient`, `AudioSegmenter` and `ClaireWire` in
  `classes3.dex`); the APK's bytes are therefore the code that was reviewed, not
  a stale artefact.
* **MIUI specifics are unverified**: Autostart, the do-not-kill list, and whether
  this build's `registerDefaultNetworkCallback` survives MIUI's power
  management. The code degrades rather than crashes when a registration is
  refused, but that path is untested.
* The vision and location paths were verified as *HTTP* (§5) and, for photos, the
  bridge's own photo handling is unchanged and already tested. What was not
  verified is `CameraActivity` producing a JPEG on this device.

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
# the gate (should be green; 3405 passed, 1 skipped at the time of writing)
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
`gradle.properties`, `local.properties`, `gradlew`, the wrapper, `.gitignore`),
`app/build.gradle`, `app/proguard-rules.pro`, `app/src/main/AndroidManifest.xml`,
the thirteen app classes and three tools listed in §2, and the resources
(`values/{strings,colors,themes,arrays}.xml`, `drawable/ic_claire.xml`,
`layout/{activity_main,activity_settings,activity_camera,item_conversation}.xml`,
`xml/data_extraction_rules.xml`).

Changed in the Python side: `src/claire/phone/bridge.py` (port, `/transcribe`,
aliases, legacy header, `decode_wav_audio`), `src/claire/phone/__init__.py`
(exports), `src/claire/phone/bridge.spec.md` (the same, as specification),
`tests/test_phone_bridge.py` (the tests for all of it, every one over a real
socket).
