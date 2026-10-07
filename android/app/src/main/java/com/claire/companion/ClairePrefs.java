package com.claire.companion;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Locale;

/**
 * Everything the owner can change, in one place.
 *
 * <p>Where the token lives, and why it is only {@code SharedPreferences}: the app
 * targets {@code android:allowBackup="false"} (see the manifest), so this file is
 * not swept into a cloud backup, and it sits in the app's private data directory
 * on a device whose storage is encrypted. That is the honest description —
 * it is <em>not</em> hardware-backed encryption, and a rooted phone or a
 * {@code run-as} debug build can read it. The alternative
 * ({@code EncryptedSharedPreferences}) pulls in {@code androidx.security}, which
 * is another AAR and another thing that can disagree with this toolchain, for a
 * threat this app does not currently face: any attacker who can read app-private
 * storage on this phone can also just record the microphone.
 *
 * <p>The token is never logged, never put in a body and never returned by
 * {@link #toString()} (there is no {@code toString()} here on purpose).
 */
public final class ClairePrefs {

    private static final String FILE = "claire";

    private static final String KEY_HOST = "host";
    private static final String KEY_PORT = "port";
    private static final String KEY_TOKEN = "token";
    private static final String KEY_QUESTION = "photo_question";
    private static final String KEY_AUTO_PHOTO = "auto_send_photo";
    private static final String KEY_SPEAK = "speak_replies";
    private static final String KEY_LISTEN_ON_BOOT = "listen_on_boot";
    private static final String KEY_LOW_BATTERY_PAUSE = "low_battery_pause";
    private static final String KEY_SENSITIVITY = "sensitivity";
    /** Whether the owner wants the microphone open. Set by Listen, cleared by Stop. */
    private static final String KEY_WANTS_LISTENING = "wants_listening";
    /** Continuous position reporting. */
    private static final String KEY_LOCATION_ALWAYS = "location_always";
    private static final String KEY_LOCATION_INTERVAL = "location_interval_seconds";
    /** Autonomous capture: on Claire's request, and optionally on a timer. */
    private static final String KEY_AUTO_CAPTURE = "auto_capture";
    private static final String KEY_CAPTURE_PERIOD = "capture_period_minutes";
    /** The restart watchdog. */
    private static final String KEY_WATCHDOG = "watchdog";
    /**
     * Which engine speaks Claire's replies on the phone.
     *
     * <p>{@link #TTS_FISH} calls Fish Audio from the phone and streams the audio as
     * it arrives; {@link #TTS_DEVICE} is the phone's own text-to-speech, which
     * needs no token and no network. The owner asked for the direct call, so Fish
     * is the default — but it is only *used* when a token is present, and the
     * fallback is named on screen rather than chosen silently.
     */
    private static final String KEY_TTS_BACKEND = "tts_backend";
    /** The owner's Fish Audio API key. Never logged, never sent anywhere but Fish. */
    private static final String KEY_FISH_TOKEN = "fish_token";
    /** The Fish model id, e.g. {@code s2.1-pro-free}. Editable because it changes. */
    private static final String KEY_FISH_MODEL = "fish_model";
    /** Optional cloned-voice id; empty means the model's own default voice. */
    private static final String KEY_FISH_VOICE = "fish_voice_id";

    /** Speak through Fish Audio from the phone itself. */
    public static final String TTS_FISH = "fish";
    /** Speak through the phone's built-in text-to-speech. */
    public static final String TTS_DEVICE = "device";

    /** The laptop runs this model today; the owner can change it in Settings. */
    public static final String DEFAULT_FISH_MODEL = "s2.1-pro-free";

    /** The default continuous-location cadence: once a minute. */
    public static final int DEFAULT_LOCATION_INTERVAL = 60;

    /** The laptop on the tailnet; see docs/PHONE_CLIENT.md. */
    public static final String DEFAULT_HOST = "100.104.1.69";

    /** The bridge's port. 8788, never 8765 (the web panel) or 8731 (`dsh web`). */
    public static final int DEFAULT_PORT = 8788;

    public static final String DEFAULT_QUESTION = "What am I looking at?";

    private final SharedPreferences prefs;

    public ClairePrefs(Context context) {
        this.prefs = context.getApplicationContext()
                .getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public String host() {
        String raw = prefs.getString(KEY_HOST, DEFAULT_HOST);
        return normaliseHost(raw == null ? "" : raw, DEFAULT_HOST);
    }

    public int port() {
        int value = prefs.getInt(KEY_PORT, DEFAULT_PORT);
        return value > 0 && value < 65536 ? value : DEFAULT_PORT;
    }

    public String token() {
        String value = prefs.getString(KEY_TOKEN, "");
        return value == null ? "" : value.trim();
    }

    public String question() {
        String value = prefs.getString(KEY_QUESTION, DEFAULT_QUESTION);
        return value == null || value.trim().isEmpty() ? DEFAULT_QUESTION : value.trim();
    }

    public boolean autoSendPhoto() {
        return prefs.getBoolean(KEY_AUTO_PHOTO, true);
    }

    public boolean speakReplies() {
        return prefs.getBoolean(KEY_SPEAK, false);
    }

    // -- which engine speaks, and with what ------------------------------- //

    /** {@link #TTS_FISH} or {@link #TTS_DEVICE}. */
    public String ttsBackend() {
        String value = prefs.getString(KEY_TTS_BACKEND, TTS_FISH);
        return TTS_DEVICE.equals(value) ? TTS_DEVICE : TTS_FISH;
    }

    public void setTtsBackend(String backend) {
        prefs.edit().putString(KEY_TTS_BACKEND,
                TTS_DEVICE.equals(backend) ? TTS_DEVICE : TTS_FISH).apply();
    }

    public String fishToken() {
        String value = prefs.getString(KEY_FISH_TOKEN, "");
        return value == null ? "" : value.trim();
    }

    public void setFishToken(String token) {
        prefs.edit().putString(KEY_FISH_TOKEN, token == null ? "" : token.trim()).apply();
    }

    public String fishModel() {
        String value = prefs.getString(KEY_FISH_MODEL, DEFAULT_FISH_MODEL);
        return value == null || value.trim().isEmpty() ? DEFAULT_FISH_MODEL : value.trim();
    }

    public void setFishModel(String model) {
        prefs.edit().putString(KEY_FISH_MODEL, model == null ? "" : model.trim()).apply();
    }

    public String fishVoiceId() {
        String value = prefs.getString(KEY_FISH_VOICE, "");
        return value == null ? "" : value.trim();
    }

    public void setFishVoiceId(String voice) {
        prefs.edit().putString(KEY_FISH_VOICE, voice == null ? "" : voice.trim()).apply();
    }

    /**
     * Whether a reply can actually be spoken by Fish right now.
     *
     * <p>Both facts matter: the backend has to be Fish, and a token has to exist.
     * When this is false the app falls back to the device engine and says so —
     * an app that silently stops speaking is indistinguishable from a broken one.
     */
    public boolean fishSpeaks() {
        return TTS_FISH.equals(ttsBackend()) && !fishToken().isEmpty();
    }

    /** A masked handle for the screen: never the key itself. */
    public String fishTokenHint() {
        String token = fishToken();
        if (token.isEmpty()) {
            return "no Fish key set";
        }
        if (token.length() <= 4) {
            return "set (" + token.length() + " chars)";
        }
        return "set, ends " + token.substring(token.length() - 4)
                + " (" + token.length() + " chars)";
    }

    public boolean listenOnBoot() {
        return prefs.getBoolean(KEY_LISTEN_ON_BOOT, false);
    }

    public boolean lowBatteryPause() {
        return prefs.getBoolean(KEY_LOW_BATTERY_PAUSE, true);
    }

    /** 0 = quieter rooms only, 1 = normal, 2 = a noisy room.  See AudioSegmenter.Params. */
    public int sensitivity() {
        int value = prefs.getInt(KEY_SENSITIVITY, 1);
        return Math.max(0, Math.min(2, value));
    }

    public void setHost(String host) {
        prefs.edit().putString(KEY_HOST, normaliseHost(host, DEFAULT_HOST)).apply();
    }

    public void setPort(int port) {
        prefs.edit().putInt(KEY_PORT, port).apply();
    }

    public void setToken(String token) {
        prefs.edit().putString(KEY_TOKEN, token == null ? "" : token.trim()).apply();
    }

    public void setQuestion(String question) {
        prefs.edit().putString(KEY_QUESTION, question == null ? "" : question.trim()).apply();
    }

    public void setAutoSendPhoto(boolean value) {
        prefs.edit().putBoolean(KEY_AUTO_PHOTO, value).apply();
    }

    public void setSpeakReplies(boolean value) {
        prefs.edit().putBoolean(KEY_SPEAK, value).apply();
    }

    public void setListenOnBoot(boolean value) {
        prefs.edit().putBoolean(KEY_LISTEN_ON_BOOT, value).apply();
    }

    public void setLowBatteryPause(boolean value) {
        prefs.edit().putBoolean(KEY_LOW_BATTERY_PAUSE, value).apply();
    }

    public void setSensitivity(int value) {
        prefs.edit().putInt(KEY_SENSITIVITY, Math.max(0, Math.min(2, value))).apply();
    }

    // -- the always-on settings ------------------------------------------- //

    /**
     * Whether the owner wants the microphone open.
     *
     * <p>The watchdog reads this and nothing else before it tries to start the service:
     * a service that comes back after being killed, on a phone whose owner pressed
     * Stop a week ago, is a microphone nobody asked for.
     */
    public boolean wantsListening() {
        return prefs.getBoolean(KEY_WANTS_LISTENING, false);
    }

    public void setWantsListening(boolean value) {
        prefs.edit().putBoolean(KEY_WANTS_LISTENING, value).apply();
    }

    /** Continuous position reporting.  Off by default: it is the invasive one. */
    public boolean continuousLocation() {
        return prefs.getBoolean(KEY_LOCATION_ALWAYS, false);
    }

    public void setContinuousLocation(boolean value) {
        prefs.edit().putBoolean(KEY_LOCATION_ALWAYS, value).apply();
    }

    public int locationIntervalSeconds() {
        int value = prefs.getInt(KEY_LOCATION_INTERVAL, DEFAULT_LOCATION_INTERVAL);
        return value >= 10 ? value : DEFAULT_LOCATION_INTERVAL;
    }

    public void setLocationIntervalSeconds(int seconds) {
        prefs.edit().putInt(KEY_LOCATION_INTERVAL, Math.max(10, seconds)).apply();
    }

    /**
     * Whether Claire may ask for a photo through the bridge.
     *
     * <p>On by default, because the owner asked for it — and visible in three places
     * (this switch, the ongoing notification, and a notification on every capture), so
     * "on by default" is not the same as "hidden".
     */
    public boolean autoCapture() {
        return prefs.getBoolean(KEY_AUTO_CAPTURE, true);
    }

    public void setAutoCapture(boolean value) {
        prefs.edit().putBoolean(KEY_AUTO_CAPTURE, value).apply();
    }

    /** Minutes between unattended photos.  0 (the default) means never. */
    public int capturePeriodMinutes() {
        return Math.max(0, prefs.getInt(KEY_CAPTURE_PERIOD, 0));
    }

    public void setCapturePeriodMinutes(int minutes) {
        prefs.edit().putInt(KEY_CAPTURE_PERIOD, Math.max(0, minutes)).apply();
    }

    /** Whether the watchdog re-arms the service after it is killed. */
    public boolean watchdog() {
        return prefs.getBoolean(KEY_WATCHDOG, true);
    }

    public void setWatchdog(boolean value) {
        prefs.edit().putBoolean(KEY_WATCHDOG, value).apply();
    }

    /** True when there is enough here to attempt a request. */
    public boolean configured() {
        return !host().isEmpty() && !token().isEmpty();
    }

    /** {@code http://host:port} — plain HTTP, over the WireGuard tunnel Tailscale gives us. */
    public String baseUrl() {
        return String.format(Locale.ROOT, "http://%s:%d", host(), port());
    }

    /** A masked rendering for the settings screen.  Never the value itself. */
    public String maskedToken() {
        String token = token();
        if (token.isEmpty()) {
            return "(none)";
        }
        if (token.length() <= 4) {
            return "****";
        }
        return "…" + token.substring(token.length() - 4)
                + " (" + token.length() + " chars)";
    }

    /**
     * Accept what a person actually types: a bare host, a URL, or a URL with a port.
     *
     * <p>"100.104.1.69:8788", "http://100.104.1.69:8788/" and "mana-pc" all have to
     * end up as a usable host, because the owner types this once, on a phone, from
     * memory.
     */
    public static String normaliseHost(String raw, String fallback) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) {
            return fallback;
        }
        int scheme = value.indexOf("://");
        if (scheme >= 0) {
            value = value.substring(scheme + 3);
        }
        int slash = value.indexOf('/');
        if (slash >= 0) {
            value = value.substring(0, slash);
        }
        value = value.trim();
        // A port typed into the host box is dropped here and belongs in the port box.
        if (value.startsWith("[")) {
            int close = value.indexOf(']');
            if (close > 0) {
                value = value.substring(0, close + 1);
            }
        } else if (value.indexOf(':') == value.lastIndexOf(':')) {
            int colon = value.indexOf(':');
            if (colon > 0) {
                value = value.substring(0, colon);
            }
        }
        return value.isEmpty() ? fallback : value;
    }
}
