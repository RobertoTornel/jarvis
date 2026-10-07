package com.claire.companion;

import android.util.Log;

/**
 * The app's own one-line diary, for the next time something goes wrong on the phone.
 *
 * <p>This exists because of a specific failure of evidence. When the owner reported
 * that the app "closes itself and reopens when the network dies", 71 MB of logcat from
 * the phone contained <b>not one line written by this app</b> — the only traces were
 * the platform's, and they can say that a process died but never why it thought it
 * should. Diagnosing it meant reading MIUI's {@code am_kill} records and guessing.
 *
 * <p>So the two or three events that matter — the service starting and stopping, the
 * network appearing and disappearing, and the watchdog's decision — are now stated
 * plainly, under one tag, in a fixed wording. Nothing here is a substitute for the
 * honest screen messages; it is what makes them checkable after the fact.
 *
 * <p>Deliberately absent: anything the owner said, any token, and any coordinate.
 * This is a lifecycle diary, not a wiretap.
 */
final class Diag {

    /** One tag, so {@code adb logcat -s ClaireLife} is the whole story. */
    static final String TAG = "ClaireLife";

    private Diag() {
    }

    /** Something happened that the owner might later need to know about. */
    static void note(String what) {
        Log.i(TAG, what);
    }

    /** Something failed. The throwable is kept in full: that is the point. */
    static void failed(String what, Throwable error) {
        Log.e(TAG, what, error);
    }
}
