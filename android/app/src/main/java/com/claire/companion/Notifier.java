package com.claire.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

/**
 * The two notifications this app is allowed to post, and the "tap to start" nudge.
 *
 * <p>Two channels, because they are two different interruptions and the owner should
 * be able to silence one without silencing the other:
 *
 * <ul>
 *   <li>{@link #CHANNEL_LISTENING} — <b>low</b> importance, no sound, no vibration:
 *       it is a status line that says the microphone is open. It is the service's
 *       foreground notification, so it is also the thing Android pins to the top of
 *       the shade while the phone is listening.
 *   <li>{@link #CHANNEL_ALERTS} — <b>default</b> importance: "the token was refused",
 *       "location services are off". Things the owner must actually notice.
 * </ul>
 *
 * <p>The listening notification carries the three actions that make the app usable
 * with the screen off — <b>Photo</b>, <b>Location</b> and <b>Pause</b> — and all three
 * are {@code PendingIntent}s to an activity rather than to the service. That is not
 * laziness: Android 14+ forbids starting a {@code microphone}-typed foreground
 * service from the background, and a notification action that starts an activity
 * gives the app the user-initiated exemption it needs instead of an
 * {@code IllegalStateException} in the shade.
 */
public final class Notifier {

    public static final String CHANNEL_LISTENING = "claire.listening";
    public static final String CHANNEL_ALERTS = "claire.alerts";

    public static final int LISTENING_NOTIFICATION_ID = 1001;
    public static final int NUDGE_NOTIFICATION_ID = 1002;
    public static final int ALERT_NOTIFICATION_ID = 1003;

    private Notifier() {
    }

    /** Create both channels.  Idempotent; safe to call on every service start. */
    public static void ensureChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager manager =
                (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return;
        }
        NotificationChannel listening = new NotificationChannel(
                CHANNEL_LISTENING,
                context.getString(R.string.channel_listening),
                NotificationManager.IMPORTANCE_LOW);
        listening.setDescription(context.getString(R.string.channel_listening_detail));
        listening.setShowBadge(false);
        listening.enableVibration(false);
        manager.createNotificationChannel(listening);

        NotificationChannel alerts = new NotificationChannel(
                CHANNEL_ALERTS,
                context.getString(R.string.channel_alerts),
                NotificationManager.IMPORTANCE_DEFAULT);
        alerts.setDescription(context.getString(R.string.channel_alerts_detail));
        manager.createNotificationChannel(alerts);
    }

    /** The ongoing "Claire is listening" notification. */
    public static Notification listening(Context context, String title, String detail) {
        Intent open = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openIntent = PendingIntent.getActivity(
                context, 0, open, flags());

        Intent photo = new Intent(context, CameraActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra(CameraActivity.EXTRA_AUTO, true);
        PendingIntent photoIntent = PendingIntent.getActivity(
                context, 1, photo, flags());

        Intent location = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP)
                .setAction(MainActivity.ACTION_REPORT_LOCATION);
        PendingIntent locationIntent = PendingIntent.getActivity(
                context, 2, location, flags());

        Intent pause = new Intent(context, VoiceService.class)
                .setAction(VoiceService.ACTION_PAUSE);
        PendingIntent pauseIntent = PendingIntent.getService(
                context, 3, pause, flags());

        return new NotificationCompat.Builder(context, CHANNEL_LISTENING)
                .setSmallIcon(R.drawable.ic_claire)
                .setContentTitle(title)
                .setContentText(detail)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(detail))
                .setContentIntent(openIntent)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .addAction(R.drawable.ic_claire, context.getString(R.string.action_photo),
                        photoIntent)
                .addAction(R.drawable.ic_claire, context.getString(R.string.action_location),
                        locationIntent)
                .addAction(R.drawable.ic_claire, context.getString(R.string.action_pause),
                        pauseIntent)
                .build();
    }

    /** A notification that is not a foreground service. */
    public static Notification nudge(Context context, String title, String detail) {
        Intent open = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openIntent = PendingIntent.getActivity(context, 10, open, flags());
        return new NotificationCompat.Builder(context, CHANNEL_ALERTS)
                .setSmallIcon(R.drawable.ic_claire)
                .setContentTitle(title)
                .setContentText(detail)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(detail))
                .setContentIntent(openIntent)
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .build();
    }

    /**
     * Report something the owner must notice.
     *
     * <p>Wrapped in a permission check: on Android 13+ notifications can be denied,
     * and {@link NotificationManagerCompat#notify} throws without
     * {@code POST_NOTIFICATIONS}. A missing alert is bad; a crash while trying to
     * report a missing alert is worse, and the caller has already put the same words
     * in the conversation view.
     */
    public static void alert(Context context, String title, String detail) {
        try {
            NotificationManagerCompat.from(context)
                    .notify(ALERT_NOTIFICATION_ID, nudge(context, title, detail));
        } catch (SecurityException denied) {
            // Notification permission is not granted; the conversation view still has it.
        }
    }

    private static int flags() {
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        return flags;
    }
}
