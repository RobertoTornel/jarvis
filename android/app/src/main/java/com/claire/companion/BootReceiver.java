package com.claire.companion;

import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * After a restart, offer to listen — and do not start the service.
 *
 * <p>This is the whole class, and the reason it is this short is a platform rule that
 * costs a day to rediscover: <b>since Android 14, a foreground service typed
 * {@code microphone} may not be started from the background</b>, and
 * {@code BOOT_COMPLETED} is the background. Starting it anyway throws
 * {@code ForegroundServiceStartNotAllowedException} — at boot, before any of this
 * app's own code can report anything, on a phone whose only log is a notification
 * shade. From the outside that is indistinguishable from an app that does not work.
 *
 * <p>So the boot receiver does the one thing that is always allowed: it posts a
 * notification that opens {@link MainActivity}, where a tap from the owner is exactly
 * the user-initiated signal the platform wants. If the owner has switched the offer
 * off, nothing happens at all.
 *
 * <p>Two other things this receiver deliberately does not do: it does not run on
 * {@code ACTION_MY_PACKAGE_REPLACED} (an update is not a restart the owner asked for),
 * and it does not read anything from the intent.
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action)) {
            return;
        }
        ClairePrefs prefs = new ClairePrefs(context);
        if (!prefs.listenOnBoot()) {
            return;
        }
        Notifier.ensureChannels(context);
        try {
            NotificationManager manager =
                    (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (manager == null) {
                return;
            }
            manager.notify(Notifier.NUDGE_NOTIFICATION_ID, Notifier.nudge(context,
                    context.getString(R.string.app_name),
                    "Tap to start listening. Android does not let an app open the "
                            + "microphone on its own at boot."));
        } catch (SecurityException denied) {
            // POST_NOTIFICATIONS is not granted; there is no other channel to use.
        }
    }
}
