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
