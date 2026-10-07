package com.claire.companion;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Looper;
import android.provider.Settings;

import java.util.List;

/**
 * Position, on demand and continuously, with every way it can fail named out loud.
 *
 * <p>The failure that matters is not "no fix yet". It is the states a phone gets into
 * and then lies about:
 *
 * <ul>
 *   <li><b>Permission refused.</b> Android 12+ lets the owner grant <em>approximate</em>
 *       only. That is a real, useful answer — Claire gets a position accurate to a
 *       kilometre or so — so it is used and the accuracy is reported, never upgraded
 *       into a pretence of precision.
 *   <li><b>Location services switched off.</b> The permission can be granted and the
 *       GPS still off, in which case a naive request silently never calls back. This
 *       class checks {@link LocationManager#isLocationEnabled()} first and answers with
 *       {@link Outcome#SERVICES_OFF} plus the exact intent that opens the right
 *       settings screen, so the phone can offer a button instead of a spinner.
 *   <li><b>Background location missing.</b> Continuous reporting is the one mode that
 *       needs {@code ACCESS_BACKGROUND_LOCATION} as well, and on Android 10+ that is a
 *       separate grant the owner has to make on a settings screen. {@link #hasBackground}
 *       reports it exactly, and {@link #backgroundSettingsIntent} opens it, so the app
 *       can say "only while on screen" instead of quietly reporting nothing.
 * </ul>
 *
 * <p>Deliberately not a {@code FusedLocationProviderClient}: that is a Play Services
 * dependency, this phone is MIUI without Play guarantees, and
 * {@link LocationManager#getCurrentLocation} answers the same question with the
 * platform's own provider.
 */
public final class LocationReporter {

    public enum Outcome {
        OK,
        PERMISSION_DENIED,
        BACKGROUND_DENIED,
        SERVICES_OFF,
        NO_PROVIDER,
        FAILED
    }

    public interface Callback {
        void onLocation(Outcome outcome, Location location, String detail);
    }

    private LocationReporter() {
    }

    // -- what has actually been granted ----------------------------------- //

    public static boolean hasPermission(Context context) {
        return context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED
                || context.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    public static boolean hasFinePermission(Context context) {
        return context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    /**
     * Whether this app may receive positions while it is not on screen.
     *
     * <p>Below Android 10 the concept does not exist and the answer is "the same as
     * while-in-use"; above it, {@code ACCESS_BACKGROUND_LOCATION} is the answer and
     * nothing else is a substitute for it.
     */
    public static boolean hasBackground(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return hasPermission(context);
        }
        return context.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
                == PackageManager.PERMISSION_GRANTED;
    }

    public static boolean servicesEnabled(Context context) {
        LocationManager manager =
                (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) {
            return false;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            return manager.isLocationEnabled();
        }
        // Pre-28 has no isLocationEnabled(); ask the two providers directly.
        try {
            return manager.isProviderEnabled(LocationManager.GPS_PROVIDER)
                    || manager.isProviderEnabled(LocationManager.NETWORK_PROVIDER);
        } catch (Exception error) {
            return false;
        }
    }

    /** The settings screen that turns location back on. */
    public static Intent locationSettingsIntent() {
        return new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    /**
     * The app's own permission screen, which is the only place Android 11+ offers
     * "Allow all the time" — it cannot be requested from a dialog.
     */
    public static Intent backgroundSettingsIntent(Context context) {
        return new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                .setData(android.net.Uri.parse("package:" + context.getPackageName()))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    /**
     * Ask for one current fix.
     *
     * <p>Must be called from the main thread: the platform's async location APIs
     * require a {@code Looper}, and calling them off it is a crash rather than an
     * error code.
     */
    public static void requestCurrent(final Activity activity, final Callback callback) {
        if (!hasPermission(activity)) {
            callback.onLocation(Outcome.PERMISSION_DENIED, null,
                    "location permission has not been granted");
            return;
        }
        if (!servicesEnabled(activity)) {
            callback.onLocation(Outcome.SERVICES_OFF, null,
                    "location services are switched off on the phone");
            return;
        }
        LocationManager manager =
                (LocationManager) activity.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) {
            callback.onLocation(Outcome.NO_PROVIDER, null, "this phone reports no location service");
            return;
        }
        List<String> providers = manager.getProviders(true);
        if (providers == null || providers.isEmpty()) {
            callback.onLocation(Outcome.NO_PROVIDER, null,
                    "no location provider is currently enabled");
            return;
        }

        // The last known fix is offered immediately if it is fresh enough to be
        // useful: on a phone that has been in a pocket, this is usually instant and
        // accurate, and waiting for a new satellite fix in a doorway is not.
        Location last = bestLastKnown(manager, providers);
        if (last != null && System.currentTimeMillis() - last.getTime() < 120_000L) {
            callback.onLocation(Outcome.OK, last, "last known, " + ageSeconds(last) + "s old");
            return;
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            android.os.CancellationSignal cancel = new android.os.CancellationSignal();
            manager.getCurrentLocation(LocationManager.GPS_PROVIDER, cancel,
                    activity.getMainExecutor(), location -> {
                        if (location != null) {
                            callback.onLocation(Outcome.OK, location, "fresh fix");
                            return;
                        }
                        Location fallback = bestLastKnown(manager, providers);
                        if (fallback != null) {
                            callback.onLocation(Outcome.OK, fallback,
                                    "last known, " + ageSeconds(fallback) + "s old");
                        } else {
                            callback.onLocation(Outcome.FAILED, null,
                                    "no position arrived before the phone gave up");
                        }
                    });
            return;
        }

        // API 24-29: a single update from the network provider is the fastest honest
        // answer, and GPS is asked for only if that gives nothing.
        try {
            manager.requestSingleUpdate(LocationManager.NETWORK_PROVIDER,
                    new LocationListener() {
                        @Override
                        public void onLocationChanged(Location location) {
                            callback.onLocation(Outcome.OK, location, "network fix");
                        }

                        @Override
                        public void onProviderDisabled(String provider) {
                            callback.onLocation(Outcome.SERVICES_OFF, null,
                                    "location services were switched off");
                        }

                        @Override
                        public void onProviderEnabled(String provider) {
                        }

                        @Override
                        public void onStatusChanged(String provider, int status, Bundle extras) {
                        }
                    }, activity.getMainLooper());
        } catch (Exception error) {
            callback.onLocation(Outcome.FAILED, null,
                    "the location provider refused the request: " + error.getMessage());
        }
    }

    /**
     * Ask for one current fix, from a {@link Context} that is not an Activity.
     *
     * <p>The service needs this: an autonomous "where is this phone" request arrives with
     * no Activity anywhere, and the platform's async location APIs only need a
     * {@code Looper} and a permission, not a window. Below Android 11 there is no
     * {@code getCurrentLocation}, so a single update from the network provider is
     * requested instead — the honest equivalent.
     */
    public static void requestCurrent(final Context context, final Callback callback) {
        if (!hasPermission(context)) {
            callback.onLocation(Outcome.PERMISSION_DENIED, null,
                    "location permission has not been granted");
            return;
        }
        if (!servicesEnabled(context)) {
            callback.onLocation(Outcome.SERVICES_OFF, null,
                    "location services are switched off on the phone");
            return;
        }
        LocationManager manager =
                (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) {
            callback.onLocation(Outcome.NO_PROVIDER, null, "this phone reports no location service");
            return;
        }
        List<String> providers = manager.getProviders(true);
        if (providers == null || providers.isEmpty()) {
            callback.onLocation(Outcome.NO_PROVIDER, null,
                    "no location provider is currently enabled");
            return;
        }
        Location last = bestLastKnown(manager, providers);
        if (last != null && System.currentTimeMillis() - last.getTime() < 120_000L) {
            callback.onLocation(Outcome.OK, last, "last known, " + ageSeconds(last) + "s old");
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            manager.getCurrentLocation(LocationManager.GPS_PROVIDER, null,
                    androidx.core.content.ContextCompat.getMainExecutor(context), location -> {
                        if (location != null) {
                            callback.onLocation(Outcome.OK, location, "fresh fix");
                            return;
                        }
                        Location fallback = bestLastKnown(manager, providers);
                        if (fallback != null) {
                            callback.onLocation(Outcome.OK, fallback,
                                    "last known, " + ageSeconds(fallback) + "s old");
                        } else {
                            callback.onLocation(Outcome.FAILED, null,
                                    "no position arrived before the phone gave up");
                        }
                    });
            return;
        }
        try {
            manager.requestSingleUpdate(LocationManager.NETWORK_PROVIDER,
                    new SimpleListener(callback), context.getMainLooper());
        } catch (Exception error) {
            callback.onLocation(Outcome.FAILED, null,
                    "the location provider refused the request: " + error.getMessage());
        }
    }

    /** The pre-API-30 single-update listener, with the unused callbacks left empty. */
    private static final class SimpleListener implements LocationListener {
        private final Callback callback;
        private boolean answered;

        SimpleListener(Callback callback) {
            this.callback = callback;
        }

        @Override
        public void onLocationChanged(Location location) {
            if (!answered && location != null) {
                answered = true;
                callback.onLocation(Outcome.OK, location, "network fix");
            }
        }

        @Override
        public void onProviderDisabled(String provider) {
            if (!answered) {
                answered = true;
                callback.onLocation(Outcome.SERVICES_OFF, null,
                        "location services were switched off");
            }
        }

        @Override
        public void onProviderEnabled(String provider) {
        }

        @Override
        public void onStatusChanged(String provider, int status, Bundle extras) {
        }
    }

    // -- continuous mode --------------------------------------------------- //
    /**
     * Continuous position reporting, as an object the service owns.
     *
     * <p>This is deliberately not a service of its own. The location work rides inside
     * {@link VoiceService}, which already holds a foreground service and already has a
     * thread for network calls; a second service would be a second notification and a
     * second thing for MIUI to kill separately.
     *
     * <p>Started only when the owner has switched continuous sharing on, and it
     * reports its own honesty: if the background grant is missing it still runs — a
     * foreground service with the {@code location} type is allowed to receive updates
     * while it is running — but the caller is told that the positions stop when the
     * app is not on screen, and {@link ClaireState.Loc#WHILE_USING} says so in the
     * notification.
     */
    public static final class Continuous implements LocationListener {

        public interface Sink {
            void onLocation(Location location);

            /** Every non-OK outcome, named.  Never called for a normal fix. */
            void onProblem(Outcome outcome, String detail);
        }

        private final Context context;
        private final long intervalMs;
        private final float minDistanceMeters;
        private final Sink sink;
        private final LocationManager manager;
        private boolean running;

        public Continuous(Context context, long intervalMs, float minDistanceMeters, Sink sink) {
            this.context = context.getApplicationContext();
            this.intervalMs = Math.max(5_000L, intervalMs);
            this.minDistanceMeters = Math.max(0f, minDistanceMeters);
            this.sink = sink;
            this.manager = (LocationManager) this.context.getSystemService(Context.LOCATION_SERVICE);
        }

        /**
         * @return the honest outcome of trying to start. {@link Outcome#OK} means
         *     updates are being requested; anything else means they are not and the
         *     reason is in the return value.
         */
        public Outcome start() {
            if (!hasPermission(context)) {
                return Outcome.PERMISSION_DENIED;
            }
            if (!servicesEnabled(context)) {
                return Outcome.SERVICES_OFF;
            }
            if (manager == null) {
                return Outcome.NO_PROVIDER;
            }
            List<String> providers = manager.getProviders(true);
            if (providers == null || providers.isEmpty()) {
                return Outcome.NO_PROVIDER;
            }
            stop();
            Looper looper = context.getMainLooper();
            boolean any = false;
            for (String provider : new String[]{
                    LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER}) {
                if (!providers.contains(provider)) {
                    continue;
                }
                try {
                    manager.requestLocationUpdates(provider, intervalMs, minDistanceMeters,
                            this, looper);
                    any = true;
                } catch (SecurityException denied) {
                    // Revoked between the check and here; reported below if nothing
                    // else took.
                } catch (IllegalArgumentException missing) {
                    // The provider is not on this device.
                }
            }
            if (!any) {
                return Outcome.NO_PROVIDER;
            }
            running = true;
            // A last-known fix is offered immediately so the first report is not
            // intervalMs away: on a phone that has been in a pocket, this is usually
            // current.
            Location last = bestLastKnown(manager, providers);
            if (last != null) {
                sink.onLocation(last);
            }
            return Outcome.OK;
        }

        public void stop() {
            if (manager == null) {
                running = false;
                return;
            }
            try {
                manager.removeUpdates(this);
            } catch (Exception ignored) {
                // Removing updates that were never registered is not an error.
            }
            running = false;
        }

        public boolean isRunning() {
            return running;
        }

        @Override
        public void onLocationChanged(Location location) {
            if (location != null) {
                sink.onLocation(location);
            }
        }

        @Override
        public void onProviderDisabled(String provider) {
            if (!servicesEnabled(context)) {
                sink.onProblem(Outcome.SERVICES_OFF, "location services were switched off");
            }
        }

        @Override
        public void onProviderEnabled(String provider) {
        }

        @Override
        public void onStatusChanged(String provider, int status, Bundle extras) {
        }
    }

    private static Location bestLastKnown(LocationManager manager, List<String> providers) {
        Location best = null;
        for (String provider : providers) {
            try {
                Location candidate = manager.getLastKnownLocation(provider);
                if (candidate == null) {
                    continue;
                }
                if (best == null || candidate.getTime() > best.getTime()) {
                    best = candidate;
                }
            } catch (SecurityException denied) {
                // Permission was revoked between the check and here; the caller is
                // already reporting the outcome.
            }
        }
        return best;
    }

    private static long ageSeconds(Location location) {
        return Math.max(0L, (System.currentTimeMillis() - location.getTime()) / 1000L);
    }

    /** Total accuracy is unknown for a last-known fix; {@code null} says so. */
    public static Double accuracyOf(Location location) {
        if (location == null || !location.hasAccuracy()) {
            return null;
        }
        return (double) location.getAccuracy();
    }

    /** A short, human rendering: six decimal places is about 0.1 m, which is enough. */
    public static String describe(Location location) {
        if (location == null) {
            return "no position";
        }
        return String.format(java.util.Locale.ROOT, "%.6f, %.6f (±%.0f m)",
                location.getLatitude(), location.getLongitude(), location.getAccuracy());
    }
}
