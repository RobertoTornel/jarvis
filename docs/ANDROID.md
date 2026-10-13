# Android companion app

| | |
|---|---|
| **Package** | `com.claire.companion` (namespace and applicationId) |
| **Module** | `:app` — the only module in the project |
| **Toolchain** | Gradle 8.10.2 wrapper, AGP 8.5.2, Java 17 source/target level, built with JDK 21 |
| **SDK levels** | `compileSdk` 35, `targetSdk` 35, `minSdk` 24 |
| **Version** | `versionCode` 4, `versionName` 0.4.0 |
| **Role** | Companion surface; the primary UI is the web panel |
| **Related pages** | `docs/PHONE_CLIENT.md` (the app's own record: protocol decisions, the class table, what was verified on the device), `docs/UI_UX_GUIDE.md` (the colour law), `docs/subsystems/phone-bridge.md` (the server side it talks to) |

This page is the build-and-reference card. `docs/PHONE_CLIENT.md` is the record — why the port and protocol were chosen, what each class does, and what was observed on the handset. Neither replaces the other.

## 1. What the app is

Claire's primary UI is the web panel. The app is not a second control panel: it is the part of Claire that is in the room, plus a small screen for the owner.

- A **foreground service** (`VoiceService`) that holds the microphone open so nothing has to be pressed or held, carries continuous location reporting and autonomous camera capture, and keeps her reachable while the screen is off.
- Three activities — the main status/conversation screen, the settings screen, and a camera activity that can be pointed at something.
- Everything it says and hears goes through the **phone bridge** (`src/claire/phone/bridge.py`) — the app is a client of that transport and nothing more. The bridge's routes, bind policy and bearer-token authentication are documented in `docs/subsystems/phone-bridge.md`.

Survival on a device that wants to kill it is part of the design: a boot receiver posts the post-restart notification, a watchdog and a service-action receiver keep the service alive, and the manifest requests battery-optimisation exemption.

## 2. Building it

```sh
cd android
JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 ANDROID_HOME=/home/mana/android-sdk ./gradlew :app:assembleDebug
```

- **JDK 21 is required.** A newer JDK breaks this Gradle/AGP combination (8.10.2 / 8.5.2). The Java *source and target level* is 17 (`compileOptions` in `android/app/build.gradle`); JDK 21 is only the build runtime.
- **`ANDROID_HOME`** must point at the SDK. `android/local.properties` pins `sdk.dir=/home/mana/android-sdk` and is git-ignored on purpose (the path is valid only on this laptop); regenerate it with `echo "sdk.dir=$ANDROID_HOME" > android/local.properties`.
- The APK lands at **`android/app/build/outputs/apk/debug/app-debug.apk`**.
- The debug build is signed with the machine's auto-generated debug keystore (`~/.android/debug.keystore`) — no key material in the repository.
- `lint { abortOnError false }` keeps the build free of human triage; the debug `buildType` has no signing config of its own.

## 3. Project facts

Every fact below was read from the named file.

| Fact | Value | Read from |
|---|---|---|
| Namespace and applicationId | `com.claire.companion` | `android/app/build.gradle` |
| `compileSdk` / `targetSdk` | 35 / 35 | `android/app/build.gradle` |
| `minSdk` | 24 | `android/app/build.gradle` |
| AGP | 8.5.2 | `android/build.gradle` |
| Gradle wrapper | 8.10.2 | `android/gradle/wrapper/gradle-wrapper.properties` |
| Java source/target compatibility | 17 | `android/app/build.gradle` |
| `versionCode` / `versionName` | 4 / 0.4.0 | `android/app/build.gradle` |
| Modules | `:app` only | `android/settings.gradle` |
| UI dependencies | Material (`material`), appcompat, constraintlayout, recyclerview, CameraX (`androidx.camera.*`) | `android/app/build.gradle` |

## 4. A tour of `android/app/src/main/`

### Manifest

| Group | What it declares |
|---|---|
| Microphone | `RECORD_AUDIO`; `FOREGROUND_SERVICE` plus the `microphone`, `location` and `camera` service types |
| Camera | `CAMERA` |
| Location | `ACCESS_COARSE_LOCATION`, `ACCESS_FINE_LOCATION`, `ACCESS_BACKGROUND_LOCATION` |
| Notifications | `POST_NOTIFICATIONS`, `RECEIVE_BOOT_COMPLETED` |
| Survival | `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, `SYSTEM_ALERT_WINDOW` |
| Network | `INTERNET`, `ACCESS_NETWORK_STATE` |

Components: `MainActivity` (launcher, exported, `singleTop`), `CameraActivity`, `SettingsActivity`; one service, `VoiceService`, not exported, with `foregroundServiceType="microphone|location|camera"`; and three receivers — `BootReceiver` (exported, `BOOT_COMPLETED`), `WatchdogReceiver`, `ServiceActionReceiver`.

### Java

Twenty classes under `java/com/claire/companion/`, grouped by role. The per-class table (including what each class does and why) is in `docs/PHONE_CLIENT.md` — link, do not restate.

- **Transport:** `ClaireWire`, `ClaireClient` (`HttpURLConnection`, not OkHttp; plain JSON; a `Bearer` token header), `ClaireState`.
- **Service and sensors:** `VoiceService`, `AudioSegmenter`, `LocationReporter`, `CameraCapture`, `Notifier`, `Survival`, `WatchdogReceiver`, `ServiceActionReceiver`, `BootReceiver`.
- **UI:** `MainActivity`, `SettingsActivity`, `CameraActivity`, `Conversation`, `ConversationAdapter`.
- **Support:** `ClairePrefs` (the settings store), `Diag`, `FishTts` (optional device-side speech through Fish, used when a Fish token is configured).

Instrumentation tests live under `app/src/androidTest/`; the build notes them deliberately, because a long press and a tap-only screen cannot be exercised from `adb shell` on this phone.

### Resources

| Path | Contents |
|---|---|
| `layout/` | `activity_main`, `activity_settings`, `activity_camera`, `item_conversation`, `row_setting_action` |
| `drawable/` | eleven `ic_*` icons (`ic_back`, `ic_claire`, `ic_copy`, `ic_location`, `ic_notification`, `ic_pause`, `ic_photo`, `ic_play`, `ic_send`, `ic_settings`, `ic_stop`) plus `bubble_background` and `chip_background` |
| `values/` | `colors.xml`, `themes.xml`, `strings.xml`, `arrays.xml` |
| `menu/` | `main` |
| `xml/` | `data_extraction_rules` |

## 5. Theming

The app obeys the shared colour law — one law for the web panel and this app: dark grey and light grey carry all structure, a single pink accent marks emphasis and interaction, pink is never a destructive or error state. `docs/UI_UX_GUIDE.md` is the guide to applying it; `docs/MEMORY_EDITOR_UX.md` §4 holds the tokens.

How this app expresses it:

- **Dark-only, by decision.** `AppTheme` extends `Theme.Material3.Dark.NoActionBar`. It is not `DayNight` on purpose: the app is used in a dark room and through a notification shade, and a second light scheme would be a second palette nobody has ever seen. (The panel differs — it renders on an OS that switches, so it carries a light token block; the law's hue is kept in both.)
- **Two layers.** `colors.xml` holds the law's values under stable resource names; `themes.xml` fills Material 3 roles from those resources. Components read the roles, so changing a value is one line in `colors.xml` and not a hunt through the layouts.
- **Names are load-bearing.** Themes, layouts and code reference them by name. Keep every resource **name** stable; change **values** to track the law.

| Resource | Value | Law token it carries |
|---|---|---|
| `accent` | `#FFE562A4` | the accent |
| `accent_dim` | `#FFCF4B92` | the accent's pressed, darker step (the law's active value) |
| `bg` | `#FF131417` | background |
| `surface` | `#FF1B1D21` | surface (elevation 1) |
| `surface_alt` | `#FF24262B` | surface (elevation 2); the owner bubble |
| `surface_high` | `#FF2E3138` | surface (elevation 3) |
| `outline` | `#FF31343A` | the default border |
| `text` | `#FFE8EAED` | primary text |
| `text_dim` | `#FFA8ADB5` | secondary text; the owner's voice colour (`you`) |
| `on_accent` | `#FF150A12` | text/icons on pink |
| `ok` | `#FF4CC38A` | success |
| `warn` | `#FFD9A441` | warning |
| `error` | `#FFEF6A6A` | danger (the Material 3 error role) |
| `bad` | `#FFEF6A6A` | danger, alias of `error`, matching the panel's `--bad` |
| `idle` | `#FF878D96` | resting state — the law's tertiary text grey reused as a state colour |
| `you` | `#FFA8ADB5` | the owner's turns/labels (alias of `text_dim`) |
| `claire` | `#FFE562A4` | Claire's turns/labels (alias of `accent`) |
| `bubble_you` | `#FF24262B` | owner bubble (surface value) |
| `bubble_claire` | `#FF2B2028` | Claire bubble — dark, subtly pink-tinted; outside the law's token table |
| `bubble_meta` | `#FF1B1D21` | small print; the `bubble_background` drawable's fill |

Two law tokens — `accent-hover` (`#EE7CB5`) and `accent-focus` (`#F7A3CD`) — were **deliberately not added**: no existing resource carried them, and adding one would have meant new resource names and `themes.xml` work. The pressed step is `accent_dim`; focus uses the system default. The camera preview's two translucent scrims are named colours, `scrim` and `bg_scrim`, so the layouts carry no raw hex at all; everything reaches a layout as an `@color` reference or a theme role, and the status chip's colour is set from code against its `idle` pill.

## 6. Installing and running on a device

Build it first (§2), then:

```sh
adb install -r -t android/app/build/outputs/apk/debug/app-debug.apk
```

`-r` reinstalls over the existing build; `-t` was part of the recorded install in `docs/PHONE_CLIENT.md`. What was subsequently verified on the handset — permissions granted, the service surviving, the one thing still blocked — is §8 of that document; on this MIUI device `adb shell input` is refused, so the instrumentation tests are the only way to exercise a long press or a tap-only screen.

On first run, grant the microphone, notification and (background) location permissions through the system dialogs, and accept the battery-optimisation exemption — MIUI's background killer will otherwise take the service down; the manifest requests the exemption but the vendor settings layer may need it granted by hand.

Reaching the daemon:

- The app is an HTTP client of the phone bridge. Settings holds the bridge's **host, port and bearer token**; the token lives in `SharedPreferences`, is never logged, never put in a body and never returned by the client.
- Compiled-in defaults: host `100.104.1.69`, port `8788` — the bridge's port, "never 8765 (the web panel) or 8731 (`dsh web`)".
- The transport is plain HTTP with a `Bearer` token and no TLS. Reach the daemon over a trusted network or a tunnel; the bind policy, the token configuration and the route table are `docs/subsystems/phone-bridge.md`'s subject, not this page's.
