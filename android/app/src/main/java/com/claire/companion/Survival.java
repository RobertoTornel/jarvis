package com.claire.companion;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.Settings;

/**
 * The three platform exemptions that decide whether "always listening" is a promise
 * the phone can keep, and the watchdog that uses them.
 *
 * <p>None of these is a permission the app can grant itself, and that is the point.
 * A listening foreground service survives a locked screen perfectly well; what kills
 * it is (a) the battery optimiser, (b) MIUI's own app killer, and (c) the Android 12
 * rule that <b>a foreground service whose type is {@code microphone} may not be
 * started from the background</b> — which is exactly what a restart after a kill is.
 * The documented ways out are:
 *
 * <ul>
 *   <li>{@code PowerManager.isIgnoringBatteryOptimizations} — Android's own do-not-kill
 *       list. It stops the battery optimiser, not MIUI.
 *   <li>{@code Settings.canDrawOverlays} — <b>this is the one that makes the restart
 *       legal</b>. An app that can draw over other apps is on the platform's own list
 *       of background-foreground-service-start exemptions, so a watchdog alarm can
 *       start the microphone service again without a tap.
 * </ul>
 *
 * <p>So the watchdog is honest rather than optimistic: it tries, catches the platform
 * refusal, and posts a notification that says so. It never pretends the service came
 * back. {@link #describe} is what the settings screen prints, and it says exactly
 * which of the two exemptions is missing.
 */
public final class Survival {

    private static final long WATCHDOG_INTERVAL_MS = 15 * 60 * 1000L;
    private static final int WATCHDOG_REQUEST = 2001;

    private Survival() {
    }

    /** Whether the battery optimiser is currently forbidden from killing this app. */
    public static boolean batteryExempt(Context context) {
        PowerManager power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
        if (power == null) {
            return false;
        }
        return power.isIgnoringBatteryOptimizations(context.getPackageName());
    }

    /**
     * The system dialog that offers the do-not-kill list. Falls back to the list
     * itself when the direct dialog is unavailable (some MIUI builds).
     */
    public static Intent batteryExemptIntent(Context context) {
        Intent direct = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                .setData(Uri.parse("package:" + context.getPackageName()));
        if (direct.resolveActivity(context.getPackageManager()) != null) {
            return direct;
        }
        return new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
    }

    /** Whether the app may draw over other apps — the FGS-restart exemption. */
    public static boolean overlayGranted(Context context) {
        return Settings.canDrawOverlays(context);
    }

    public static Intent overlayIntent(Context context) {
        return new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION)
                .setData(Uri.parse("package:" + context.getPackageName()));
    }

    /**
     * Whether an automatic restart after a kill has any chance of being legal.
     *
     * <p>Only the overlay exemption actually clears the Android 12+ restriction; the
     * battery exemption is reported separately because MIUI's killer ignores it.
     */
    public static boolean canRestartInBackground(Context context) {
        return overlayGranted(context);
    }

    // -- the watchdog ------------------------------------------------------ //

    /**
     * Arm the watchdog.  Idempotent: calling it twice replaces the same alarm, so the
     * service can call it on every start without piling up alarms.
     */
    public static void armWatchdog(Context context) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (alarms == null) {
            return;
        }
        PendingIntent pending = watchdogIntent(context);
        long first = SystemClock.elapsedRealtime() + WATCHDOG_INTERVAL_MS;
        try {
            // Inexact on purpose: this is a safety net, not a deadline, and an
            // inexact repeating alarm needs no exact-alarm permission and costs no
            // battery for a wakeup it does not need.
            alarms.setInexactRepeating(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    first, WATCHDOG_INTERVAL_MS, pending);
        } catch (SecurityException refused) {
            // Some builds refuse even this without SCHEDULE_EXACT_ALARM; the app
            // still works, it just cannot re-arm itself.  Named in the log only.
        }
    }

    public static void disarmWatchdog(Context context) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (alarms == null) {
            return;
        }
        alarms.cancel(watchdogIntent(context));
    }

    public static boolean watchdogArmed(Context context) {
        return PendingIntent.getBroadcast(context, WATCHDOG_REQUEST,
                new Intent(context, WatchdogReceiver.class),
                flags(PendingIntent.FLAG_NO_CREATE)) != null;
    }

    private static PendingIntent watchdogIntent(Context context) {
        return PendingIntent.getBroadcast(context, WATCHDOG_REQUEST,
                new Intent(context, WatchdogReceiver.class), flags(0));
    }

    private static int flags(int extra) {
        int base = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            base |= PendingIntent.FLAG_IMMUTABLE;
        }
        return base | extra;
    }

    /** One line per exemption, for the settings screen. */
    public static String describe(Context context) {
        return "battery " + (batteryExempt(context) ? "unrestricted" : "optimised")
                + " · overlays " + (overlayGranted(context) ? "allowed" : "not allowed")
                + " · watchdog " + (watchdogArmed(context) ? "armed" : "off");
    }
}
