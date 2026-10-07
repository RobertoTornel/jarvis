package com.claire.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import androidx.core.content.ContextCompat;

/**
 * The safety net: every fifteen minutes, if the owner wants the microphone open and the
 * service is gone, try to start it again.
 *
 * <p>This exists because of a specific, reproducible death. MIUI kills a background
 * foreground service after a while with the screen off, and when it does, the ongoing
 * notification disappears and "always listening" quietly becomes "listening until it
 * felt like stopping". {@code START_STICKY} brings the service back after a low-memory
 * kill, but not after a force-stop or a swipe away from Recents on some builds, and it
 * is not a promise the platform makes on MIUI at all.
 *
 * <p>So the watchdog tries, and it is honest about the two possible answers:
 *
 * <ul>
 *   <li><b>It works</b> when {@code SYSTEM_ALERT_WINDOW} is granted, because an app that
 *       can draw over other apps is on Android's own list of background
 *       foreground-service-start exemptions. That is the whole reason the settings
 *       screen asks for that permission.
 *   <li><b>It is refused</b> otherwise — {@code ForegroundServiceStartNotAllowedException}
 *       — and the receiver posts the "tap to start it again" notification instead of
 *       swallowing it. A silent failure here is indistinguishable from a working app.
 * </ul>
 *
 * <p>The alarm is inexact and repeats every fifteen minutes. It is a net, not a
 * heartbeat: nothing here holds a wakelock, and an inexact alarm may be deferred by
 * Doze, which is the correct trade for a phone whose battery is the owner's.
 */
public class WatchdogReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        ClairePrefs prefs = new ClairePrefs(context);
        if (!prefs.watchdog() || !prefs.wantsListening()) {
            Diag.note("watchdog: nothing to do (watchdog or listening is switched off)");
            return;
        }
        if (VoiceService.isRunning()) {
            Diag.note("watchdog: the service is up");
            Notifier.cancelNudge(context);
            return;
        }
        try {
            ContextCompat.startForegroundService(context,
                    new Intent(context, VoiceService.class)
                            .setAction(VoiceService.ACTION_RESUME));
            // If this succeeds the service calls startForeground() promptly and the
            // nudge is cleared by the service itself.
            Diag.note("watchdog: asked the platform to start the service again");
        } catch (RuntimeException refused) {
            // Written down in full, because this is the moment "always listening"
            // silently becomes "listening until it felt like stopping". The overlay
            // permission (Settings.canDrawOverlays) is what makes this start legal.
            Diag.failed("watchdog: the phone refused to restart the service"
                    + " (overlay granted: " + Survival.overlayGranted(context) + ")",
                    refused);
            Notifier.nudge(context,
                    context.getString(R.string.notif_nudge_title),
                    context.getString(R.string.notif_nudge_text)
                            + "  (" + refused.getClass().getSimpleName() + ")");
        }
    }
}
