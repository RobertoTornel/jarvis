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
 * Every notification this app is allowed to post, and the shade controls that make it
 * usable with the screen off.
 *
 * <p>Three channels, because they are three different interruptions and the owner
 * should be able to silence one without silencing the others:
 *
 * <ul>
 *   <li>{@link #CHANNEL_LISTENING} — <b>low</b> importance, no sound, no vibration: the
 *       ongoing status line. It is the service's foreground notification, so it is also
 *       the thing Android pins to the top of the shade. Its text now says all three
 *       things at once — microphone, location, camera — so the two new always-on modes
 *       are visible without opening the app.
 *   <li>{@link #CHANNEL_ALERTS} — <b>default</b> importance: "the token was refused",
 *       "location services are off", "the phone killed the service". Things the owner
 *       must actually notice.
 *   <li>{@link #CHANNEL_CAPTURE} — <b>default</b> importance, and the one channel that
 *       exists purely for privacy: <b>a photo taken without the owner pressing
 *       anything gets a notification of its own.</b> It is separate from the status
 *       line precisely so that a camera that fires on Claire's request cannot be
 *       hidden inside a silent channel the owner has already muted.
 * </ul>
 *
 * <p>The ongoing notification carries three actions — <b>Photo</b>, <b>Pause/Resume</b>
 * and <b>Stop</b> — and which kind of {@code PendingIntent} each one gets is the
 * interesting decision:
 *
 * <ul>
 *   <li><b>Photo</b> goes to {@link MainActivity}, because taking a photo by hand may
 *       need a runtime permission and a preview, and because starting an activity is
 *       unambiguously user-initiated.
 *   <li><b>Pause/Resume/Stop</b> go to a broadcast receiver. They only ever talk to a
 *       service that is already running in the foreground — a paused service keeps its
 *       foreground notification — so there is no background start to be refused. If the
 *       service is <em>gone</em>, the receiver says so with {@link #nudge} instead of
 *       throwing into the shade.
 * </ul>
 */
public final class Notifier {

    public static final String CHANNEL_LISTENING = "claire.listening";
    public static final String CHANNEL_ALERTS = "claire.alerts";
    public static final String CHANNEL_CAPTURE = "claire.capture";

    public static final int LISTENING_NOTIFICATION_ID = 1001;
    public static final int NUDGE_NOTIFICATION_ID = 1002;
    public static final int ALERT_NOTIFICATION_ID = 1003;
    public static final int CAPTURE_NOTIFICATION_ID = 1004;

    private Notifier() {
    }

    /** Create all channels.  Idempotent; safe to call on every service start. */
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

        NotificationChannel capture = new NotificationChannel(
                CHANNEL_CAPTURE,
                context.getString(R.string.channel_capture),
                NotificationManager.IMPORTANCE_DEFAULT);
        capture.setDescription(context.getString(R.string.channel_capture_detail));
        manager.createNotificationChannel(capture);
    }

    /**
     * The ongoing notification, built from the phone's actual state.
     *
     * @param title  "Claire is listening" / "Claire is paused"
     * @param text   one line saying what is on: microphone, location, camera
     * @param sub    the link line ("connected to 100.104.1.69:8788", "unreachable", …)
     * @param paused whether to offer Resume rather than Pause
     */
    public static Notification listening(Context context, String title, String text,
                                         String sub, boolean paused) {
        PendingIntent openIntent = PendingIntent.getActivity(
                context, 0, openApp(context), flags(PendingIntent.FLAG_UPDATE_CURRENT));

        NotificationCompat.Builder builder = new NotificationCompat.Builder(
                context, CHANNEL_LISTENING)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(text)
                .setSubText(sub)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setColor(context.getColor(R.color.accent))
                .setColorized(false)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setContentIntent(openIntent)
                .addAction(R.drawable.ic_photo, context.getString(R.string.action_photo),
                        PendingIntent.getActivity(context, 11,
                                openApp(context).setAction(MainActivity.ACTION_PHOTO),
                                flags(PendingIntent.FLAG_UPDATE_CURRENT)))
                .addAction(paused ? R.drawable.ic_play : R.drawable.ic_pause,
                        context.getString(paused ? R.string.action_resume : R.string.action_pause),
                        serviceAction(context, 12,
                                paused ? VoiceService.ACTION_RESUME : VoiceService.ACTION_PAUSE))
                .addAction(R.drawable.ic_stop, context.getString(R.string.action_stop),
                        serviceAction(context, 13, VoiceService.ACTION_STOP));

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            // Without this the platform holds a foreground service's notification back
            // for ten seconds, which for a service whose whole point is "you can see
            // the microphone is open" is ten seconds of lying.
            builder.setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return builder.build();
    }

    /**
     * The camera-safety notification: a photo was taken with nobody pressing anything.
     *
     * <p>Posted on {@link #CHANNEL_CAPTURE} every single time. It says who asked, so the
     * owner can tell "I asked for this through the app" from "Claire asked for this
     * through the bridge" without reading a log.
     */
    public static void captureNotice(Context context, String why) {
        NotificationCompat.Builder builder = new NotificationCompat.Builder(
                context, CHANNEL_CAPTURE)
                .setSmallIcon(R.drawable.ic_photo)
                .setContentTitle(context.getString(R.string.notif_capture_title))
                .setContentText(why)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(why))
                .setColor(context.getColor(R.color.accent))
                .setAutoCancel(true)
                .setContentIntent(PendingIntent.getActivity(
                        context, 21, openApp(context), flags(PendingIntent.FLAG_UPDATE_CURRENT)));
        notify(context, CAPTURE_NOTIFICATION_ID, builder.build());
    }

    /** An alert the owner must notice.  Never silenced by the status line's channel. */
    public static void alert(Context context, String title, String text) {
        NotificationCompat.Builder builder = new NotificationCompat.Builder(
                context, CHANNEL_ALERTS)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setColor(context.getColor(R.color.warn))
                .setAutoCancel(true)
                .setContentIntent(PendingIntent.getActivity(
                        context, 31, openApp(context), flags(PendingIntent.FLAG_UPDATE_CURRENT)));
        notify(context, ALERT_NOTIFICATION_ID, builder.build());
    }

    /**
     * "The service is not running and I cannot restart it from here."
     *
     * <p>This is the honest failure of the watchdog: Android 12+ refuses a background
     * start of a microphone foreground service unless the app holds an exemption, and
     * this notification is the fallback that turns a silent death into one tap.
     */
    public static void nudge(Context context, String title, String text) {
        NotificationCompat.Builder builder = new NotificationCompat.Builder(
                context, CHANNEL_ALERTS)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setColor(context.getColor(R.color.warn))
                .setAutoCancel(true)
                .setOngoing(false)
                .setContentIntent(PendingIntent.getActivity(
                        context, 41, openApp(context), flags(PendingIntent.FLAG_UPDATE_CURRENT)));
        notify(context, NUDGE_NOTIFICATION_ID, builder.build());
    }

    public static void cancelNudge(Context context) {
        NotificationManagerCompat.from(context).cancel(NUDGE_NOTIFICATION_ID);
    }

    // -- plumbing ---------------------------------------------------------- //

    private static Intent openApp(Context context) {
        return new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
    }

    private static PendingIntent serviceAction(Context context, int request, String action) {
        Intent intent = new Intent(context, ServiceActionReceiver.class).setAction(action);
        return PendingIntent.getBroadcast(context, request, intent,
                flags(PendingIntent.FLAG_UPDATE_CURRENT));
    }

    private static void notify(Context context, int id, Notification notification) {
        try {
            NotificationManagerCompat.from(context).notify(id, notification);
        } catch (SecurityException denied) {
            // POST_NOTIFICATIONS is not granted.  The service is unaffected; there is
            // simply nowhere to say so, which is why the app's own screen repeats the
            // permission state.
        }
    }

    private static int flags(int extra) {
        int base = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            base |= PendingIntent.FLAG_IMMUTABLE;
        }
        return base | extra;
    }
}
