package com.claire.companion;

import android.os.Handler;
import android.os.Looper;

import java.util.ArrayList;
import java.util.List;

/**
 * What this phone is doing right now, in one place, for the screen to draw.
 *
 * <p>Before this class the UI inferred its status line from three unrelated sources —
 * a static on {@link VoiceService}, the last {@code /health} answer the activity
 * happened to have made, and whatever text the last conversation line said. That is
 * how an app ends up showing "listening" in green while the service is dead: nobody
 * owned the truth.
 *
 * <p>So the service owns it, writes it here as it changes, and the screen only reads.
 * Every state is a small enum rather than a string, because the four things a person
 * needs to tell apart — <em>connected / listening / offline / error</em> — are states,
 * and a state that is only a sentence cannot be coloured, spoken or tested.
 *
 * <p>The fields are volatile and written from the service's threads; listeners are
 * always called on the main thread. Reading a consistent snapshot of more than one
 * field is not something this class promises: the screen repaints on every change, so
 * a torn read lasts one frame and is corrected by the next notification.
 */
public final class ClaireState {

    /** Whether the laptop answered the last time we asked. */
    public enum Link {
        /** Nothing has been tried yet this run. */
        UNKNOWN,
        /** The bridge answered, and the token was accepted. */
        OK,
        /** The bridge answered 401: reachable, but the token is wrong. */
        TOKEN_REFUSED,
        /** The bridge could not be reached at all. */
        UNREACHABLE
    }

    /** The microphone. */
    public enum Mic { STOPPED, LISTENING, PAUSED, DENIED }

    /** Continuous position reporting. */
    public enum Loc {
        /** Not sharing. */
        OFF,
        /** Sharing, with background location: works with the screen off. */
        ALWAYS,
        /** Sharing, but only while the app is on screen (no background grant). */
        WHILE_USING,
        /** The owner asked for it and the runtime permission is missing. */
        DENIED,
        /** Permission is there and the GPS is switched off. */
        SERVICES_OFF
    }

    /** The camera, and whether it can be opened without the owner. */
    public enum Cam { OFF, ARMED, CAPTURING, DENIED }

    public interface Listener {
        /** Always on the main thread. */
        void onStateChanged();
    }

    private static final ClaireState INSTANCE = new ClaireState();

    public static ClaireState get() {
        return INSTANCE;
    }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Listener> listeners = new ArrayList<>();

    private volatile Link link = Link.UNKNOWN;
    private volatile String linkDetail = "";
    private volatile Mic mic = Mic.STOPPED;
    private volatile Loc location = Loc.OFF;
    private volatile int locationIntervalSeconds;
    private volatile Cam camera = Cam.OFF;
    private volatile String lastFix = "";
    private volatile long lastFixAt;
    private volatile int foregroundTypes;
    /**
     * Which engine is speaking, in the owner's words.
     *
     * <p>There are two — Fish Audio called from this phone, and the phone's own
     * text-to-speech — and which one is in use is not something the owner should
     * have to infer from the sound of the voice.
     */
    private volatile String voice = "";

    private ClaireState() {
    }

    // -- reading ---------------------------------------------------------- //

    public Link link() {
        return link;
    }

    public String linkDetail() {
        return linkDetail;
    }

    public Mic mic() {
        return mic;
    }

    public Loc location() {
        return location;
    }

    public int locationIntervalSeconds() {
        return locationIntervalSeconds;
    }

    public Cam camera() {
        return camera;
    }

    /** How good the newest fix was, or "" when there has never been one.
     *
     * <p>Deliberately *not* the coordinates. They are the owner's own position,
     * and an app that prints "37.97355, -1.13349" is telling them where they are
     * as if it were news. That a fix exists, how accurate it is and how fresh it
     * is are the facts worth a line on screen; the position itself goes to
     * Claire as context and nowhere else.
     */
    public String lastFix() {
        return lastFixAt == 0 ? "" : lastFix;
    }

    public long lastFixAt() {
        return lastFixAt;
    }

    /** The bitmask last handed to {@code startForeground}. 0 before it has run. */
    public int foregroundTypes() {
        return foregroundTypes;
    }

    // -- writing ---------------------------------------------------------- //

    public void setLink(Link value, String detail) {
        link = value;
        linkDetail = detail == null ? "" : detail;
        changed();
    }

    public void setMic(Mic value) {
        mic = value;
        changed();
    }

    public void setLocation(Loc value, int intervalSeconds) {
        location = value;
        locationIntervalSeconds = intervalSeconds;
        changed();
    }

    public void setCamera(Cam value) {
        camera = value;
        changed();
    }

    /** Which engine last spoke, or "" when nothing has spoken yet. */
    public String voice() {
        return voice;
    }

    public void setVoice(String value) {
        voice = value == null ? "" : value;
        changed();
    }

    public void setLastFix(double latitude, double longitude, float accuracyMeters) {
        // The coordinates are taken (the caller has to send them) and thrown away
        // on purpose: they are never stored, so they cannot be shown, logged or
        // leaked by this class. Only the two facts that are worth reporting back
        // to the owner are kept -- how accurate the fix was, and when it happened.
        lastFix = String.format(java.util.Locale.US, "\u00b1%.0f m", accuracyMeters);
        lastFixAt = System.currentTimeMillis();
        changed();
    }

    public void setForegroundTypes(int types) {
        foregroundTypes = types;
        changed();
    }

    // -- listeners -------------------------------------------------------- //

    /** Add a listener.  Must be called on the main thread. */
    public void addListener(Listener listener) {
        if (!listeners.contains(listener)) {
            listeners.add(listener);
        }
    }

    public void removeListener(Listener listener) {
        listeners.remove(listener);
    }

    private void changed() {
        main.post(() -> {
            for (Listener listener : new ArrayList<>(listeners)) {
                listener.onStateChanged();
            }
        });
    }
}
