package com.claire.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

import androidx.core.content.ContextCompat;

/**
 * The notification's Pause/Resume/Stop buttons, which are broadcasts and not activities
 * so that tapping them does not bring the app to the front.
 *
 * <p>The subtle part is what happens when the service is <b>not</b> running. Starting a
 * {@code microphone}-typed foreground service from a broadcast receiver is exactly the
 * background start Android 12+ refuses, and a receiver that calls
 * {@code startForegroundService()} and is refused has two bad outcomes available: an
 * exception into the notification shade, or — worse — a service that starts and never
 * calls {@code startForeground()}, which the platform answers with an ANR. So this
 * receiver refuses to guess: if the service is not already alive in this process it
 * posts the "tap to start it again" notification, which is a tap the platform accepts.
 */
public class ServiceActionReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (action == null) {
            return;
        }
        if (!VoiceService.ACTION_PAUSE.equals(action)
                && !VoiceService.ACTION_RESUME.equals(action)
                && !VoiceService.ACTION_STOP.equals(action)) {
            return;
        }
        if (!VoiceService.isRunning()) {
            // The ongoing notification is gone with the service, so in practice this
            // is a stale PendingIntent.  Say so rather than throwing into the shade.
            Notifier.nudge(context,
                    context.getString(R.string.notif_nudge_title),
                    context.getString(R.string.notif_nudge_text));
            return;
        }
        try {
            ContextCompat.startForegroundService(context,
                    new Intent(context, VoiceService.class).setAction(action));
        } catch (RuntimeException refused) {
            // ForegroundServiceStartNotAllowedException, or a SecurityException when a
            // type's permission has just been revoked.
            Notifier.nudge(context,
                    context.getString(R.string.notif_nudge_title),
                    "The phone would not let the listening service handle that: "
                            + refused.getClass().getSimpleName());
        }
    }
}
