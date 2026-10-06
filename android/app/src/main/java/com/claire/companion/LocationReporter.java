package com.claire.companion;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationManager;
import android.os.Build;
import android.os.CancellationSignal;
import android.provider.Settings;

import java.util.List;

/**
 * One position, on demand, with every way it can fail named out loud.
 *
 * <p>The failure that matters is not "no fix yet". It is the two states a phone gets
 * into and then lies about:
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
        SERVICES_OFF,
        NO_PROVIDER,
        FAILED
    }

    public interface Callback {
        void onLocation(Outcome outcome, Location location, String detail);
    }

    private LocationReporter() {
    }

    public static boolean hasPermission(Context context) {
        return context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED
                || context.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION)
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
     * Ask for one current fix.
     *
     * <p>Must be called from the main thread: the platform's async location APIs
     * require a {@code Looper}, and calling them off it is a crash rather than an
     * error code.
     */
    public static void requestCurrent(final Activity activity, final Callback callback) {
        if (!hasPermission(activity)) {
            callback.onLocation(Outcome.PERMISSION_DENIED, null,
                    "this app has not been given location permission");
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
            CancellationSignal cancel = new CancellationSignal();
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
                    new android.location.LocationListener() {
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
                        public void onStatusChanged(String provider, int status, android.os.Bundle extras) {
                        }
                    }, activity.getMainLooper());
        } catch (Exception error) {
            callback.onLocation(Outcome.FAILED, null,
                    "the location provider refused the request: " + error.getMessage());
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
