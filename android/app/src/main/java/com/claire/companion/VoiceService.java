package com.claire.companion;

import android.Manifest;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.location.Location;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.AudioRecordingConfiguration;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.speech.tts.TextToSpeech;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The always-on service: the microphone, the position and the camera, in one foreground
 * service with three declared types.
 *
 * <p>This class is the difference between a button and a companion. It holds the
 * microphone open continuously, decides for itself where the owner's sentences begin and
 * end ({@link AudioSegmenter}), sends each one to Claire to be recognised, keeps the
 * position moving when the owner has asked for that, and — when Claire asks through the
 * bridge — takes a photo, all with no interaction at all.
 *
 * <h3>The decisions worth defending</h3>
 *
 * <p><b>One service, three types.</b> {@code microphone|location|camera}. Three separate
 * foreground services would be three notifications, three processes for MIUI to kill
 * independently, and three chances for one of them to be alive while the others are not.
 * The type mask is built from what has actually been <em>granted</em>
 * ({@link #types()}): asking for a type whose permission is missing is a
 * {@code SecurityException} at {@code startForeground()} time, and asking for one the
 * owner has refused would be a lie in the notification. When a permission arrives while
 * the service runs — the owner taps Allow after the service started — the mask is
 * refreshed and {@code startForeground} is called again, so the always-on mode begins
 * without a restart.
 *
 * <p><b>Segmentation is on the phone; recognition is on the laptop.</b> The bridge takes
 * one complete utterance of WAV, so the phone ships an utterance and nothing else. It
 * does not stream, and the microphone is never open to the network.
 *
 * <p><b>Nothing is lost silently when the network drops.</b> Finished utterances go into
 * a small bounded queue; when connectivity returns they are sent, in order, and if the
 * queue overflows the owner is told in the conversation view. The queue is deliberately
 * small: audio of a conversation is the most private thing this app touches, and hoarding
 * minutes of it in flash to protect against a one-minute outage is the wrong trade.
 *
 * <p><b>The camera is never a surprise.</b> Autonomous capture exists only when the owner
 * has left {@code autoCapture} on; every capture posts a notification on its own channel,
 * writes a line into the conversation, and says who asked. A device whose camera can open
 * invisibly is not a companion, it is spyware.
 *
 * <p><b>What cannot be guaranteed is said out loud.</b> {@link Survival} documents the
 * two exemptions that decide whether an automatic restart is even legal; when the
 * platform refuses, this service's watchdog posts a notification instead of pretending.
 */
public class VoiceService extends Service {

    private static final String TAG = "ClaireVoice";

    public static final String ACTION_START = "com.claire.companion.START";
    public static final String ACTION_STOP = "com.claire.companion.STOP";
    public static final String ACTION_PAUSE = "com.claire.companion.PAUSE";
    public static final String ACTION_RESUME = "com.claire.companion.RESUME";
    /** Take one photo for Claire, from the app or from her bridge request. */
    public static final String ACTION_CAPTURE = "com.claire.companion.CAPTURE";
    /** Report one position now. */
    public static final String ACTION_REPORT_LOCATION = "com.claire.companion.REPORT_LOCATION";

    /** The sample rate the bridge and the recogniser both expect. */
    private static final int SAMPLE_RATE = ClaireWire.SAMPLE_RATE;
    private static final int SAMPLES_PER_READ = 1_600;          // 100 ms

    private static final int MAX_PENDING_UTTERANCES = 8;
    private static final long REPLY_POLL_MS = 4_000L;
    private static final int LOW_BATTERY_PERCENT = 15;

    private enum State { IDLE, LISTENING, PAUSED }

    private static volatile State state = State.IDLE;
    private static volatile boolean alive;

    public static boolean isListening() {
        return state == State.LISTENING;
    }

    public static boolean isPaused() {
        return state == State.PAUSED;
    }

    /**
     * Whether the service object exists in this process right now.
     *
     * <p>This is what the watchdog and the notification receivers ask before they try to
     * start anything. It is a fact about this process, not a guess: {@code onCreate} sets
     * it and {@code onDestroy} clears it.
     */
    public static boolean isRunning() {
        return alive;
    }

    private ClairePrefs prefs;
    private HandlerThread micThread;
    private Handler micHandler;
    private AudioRecord recorder;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService uploader = Executors.newSingleThreadExecutor();
    private final Deque<short[]> pending = new ArrayDeque<>();

    private volatile boolean capturing;
    private AudioManager audioManager;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private Object focusRequest;                                 // AudioFocusRequest on O+
    private AudioManager.AudioRecordingCallback recordingCallback;
    private boolean silencedBySystem;
    /** True while Claire is speaking, so the open microphone cannot hear her. */
    private volatile boolean speaking;

    private boolean lowBattery;
    private boolean charging;
    private boolean networkUp = true;
    private boolean replyPollScheduled;
    private TextToSpeech tts;
    private boolean ttsReady;
    /**
     * The phone's own call to Fish Audio, used when the owner has set a key.
     *
     * <p>Built lazily on first use rather than in {@code onCreate}, because a
     * service that is listening but never speaks should not hold an HTTP thread.
     */
    private FishTts fish;
    private String lastReported = "";

    /** The always-on position, when the owner has asked for it. */
    private LocationReporter.Continuous locationUpdates;
    /** The camera, opened only when something actually asks for a photo. */
    private CameraCapture camera;
    /** True while one autonomous capture is in flight, so two cannot overlap. */
    private boolean photoInFlight;

    @Override
    public void onCreate() {
        super.onCreate();
        alive = true;
        Diag.note("service created");
        prefs = new ClairePrefs(this);
        Notifier.ensureChannels(this);
        audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        registerReceiver(batteryReceiver, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        registerConnectivityCallback();
        registerRecordingCallback();
        maybeStartTts();
        Conversation.get(this).add(Conversation.Who.SYSTEM, "Claire companion service started");
        ClaireState.get().setLink(ClaireState.Link.UNKNOWN, "");
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (action == null) {
            action = ACTION_START;
        }
        switch (action) {
            case ACTION_STOP:
                stopListening("the owner stopped listening");
                return START_NOT_STICKY;
            case ACTION_PAUSE:
                pauseListening("paused from the notification");
                return START_STICKY;
            case ACTION_RESUME:
                // The watchdog's action: a service that comes back after a kill must
                // come back listening, not paused.
                startListening();
                return START_STICKY;
            case ACTION_CAPTURE:
                ensureRunningForeground();
                captureNow("the owner asked from the notification");
                return START_STICKY;
            case ACTION_REPORT_LOCATION:
                ensureRunningForeground();
                reportOneLocation("the owner asked from the notification");
                return START_STICKY;
            default:
                startListening();
                return START_STICKY;
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // The owner swiped the app out of Recents.  If listening is wanted, this is
        // exactly the moment MIUI takes the service down on many builds, so the
        // watchdog is armed here rather than only at start.
        if (prefs.wantsListening()) {
            Survival.armWatchdog(this);
        }
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        alive = false;
        stopCapture();
        stopLocation("the service stopped");
        if (camera != null) {
            camera.release();
            camera.shutdown();
            camera = null;
        }
        main.removeCallbacks(captureTick);
        unregisterQuietly();
        unregisterConnectivityCallback();
        unregisterRecordingCallback();
        abandonFocus();
        if (tts != null) {
            tts.shutdown();
            tts = null;
            ttsReady = false;
        }
        if (fish != null) {
            fish.shutdown();
            fish = null;
        }
        uploader.shutdownNow();
        if (micThread != null) {
            micThread.quitSafely();
            micThread = null;
        }
        state = State.IDLE;
        ClaireState.get().setMic(ClaireState.Mic.STOPPED);
        ClaireState.get().setLocation(ClaireState.Loc.OFF, 0);
        ClaireState.get().setCamera(ClaireState.Cam.OFF);
        ClaireState.get().setForegroundTypes(0);
        Diag.note("service destroyed");
        super.onDestroy();
    }

    // -- lifecycle of listening ------------------------------------------- //

    private void startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            state = State.IDLE;
            ClaireState.get().setMic(ClaireState.Mic.DENIED);
            Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "cannot listen: microphone permission has not been granted");
            Notifier.alert(this, getString(R.string.app_name),
                    "Microphone permission is missing, so Claire cannot listen.");
            Diag.note("stopping: RECORD_AUDIO is not granted");
            stopSelf();
            return;
        }
        try {
            startForegroundCompat();
        } catch (RuntimeException refused) {
            // ForegroundServiceStartNotAllowedException on 12+, or a SecurityException
            // when the type's permission is missing. Named, not fatal.
            state = State.IDLE;
            ClaireState.get().setMic(ClaireState.Mic.STOPPED);
            Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "the phone refused to start the listening service: "
                            + refused.getClass().getSimpleName());
            Diag.failed("stopping: the phone refused the foreground service", refused);
            stopSelf();
            return;
        }
        if (state == State.LISTENING) {
            // Already listening: a permission may have arrived since, so the type mask
            // is refreshed and the always-on modes are (re)started.
            refreshForeground();
            startAlwaysOn();
            return;
        }
        state = State.LISTENING;
        prefs.setWantsListening(true);
        // Armed at start, not only when the app is swiped away: the whole point is to
        // survive a kill that this process never gets told about.
        if (prefs.watchdog()) {
            Survival.armWatchdog(this);
        }
        ClaireState.get().setMic(ClaireState.Mic.LISTENING);
        Conversation.get(this).add(Conversation.Who.PHONE, "Listening");
        startCapture();
        scheduleReplyPoll();
        startAlwaysOn();
        Notifier.cancelNudge(this);
        updateNotification();
    }

    private void pauseListening(String reason) {
        if (state == State.IDLE) {
            // Pausing a service that is not running is a no-op, but the notification
            // still has to say something true.
            conversationOnce("Listening is not running");
            return;
        }
        state = State.PAUSED;
        ClaireState.get().setMic(ClaireState.Mic.PAUSED);
        stopCapture();
        stopLocation("listening was paused");
        stopPeriodicCapture();
        conversationOnce(reason);
        updateNotification();
    }

    private void resumeListening() {
        if (state != State.PAUSED) {
            return;
        }
        state = State.LISTENING;
        ClaireState.get().setMic(ClaireState.Mic.LISTENING);
        startCapture();
        scheduleReplyPoll();
        startAlwaysOn();
        conversationOnce("Listening again");
        updateNotification();
    }

    private void stopListening(String reason) {
        state = State.IDLE;
        prefs.setWantsListening(false);
        Survival.disarmWatchdog(this);
        stopCapture();
        stopLocation("listening stopped");
        stopPeriodicCapture();
        ClaireState.get().setMic(ClaireState.Mic.STOPPED);
        conversationOnce(reason);
        stopForegroundCompat();
        stopSelf();
    }

    /**
     * Bring the service back to the foreground without disturbing listening.
     *
     * <p>Used by the notification's Photo and Location buttons when the owner has paused:
     * the service is already alive, but if it had been paused and had never come to the
     * foreground (a first start that failed), the button would otherwise talk to nothing.
     */
    private void ensureRunningForeground() {
        if (state == State.IDLE) {
            startListening();
        }
    }

    // -- the foreground type mask ----------------------------------------- //

    /**
     * Which foreground service types are actually legal right now.
     *
     * <p>Every type here is conditional on a real grant. {@code FOREGROUND_SERVICE_TYPE_MICROPHONE}
     * and {@code CAMERA} did not exist before Android 11, and passing an unknown type to
     * {@code startForeground} on an older release throws rather than ignoring it — which
     * is why the mask is API-gated as well as permission-gated.
     */
    private int types() {
        int mask = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED) {
                mask |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            }
            if (CameraCapture.hasPermission(this)) {
                mask |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                && LocationReporter.hasPermission(this)) {
            mask |= ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION;
        }
        return mask;
    }

    private void startForegroundCompat() {
        int mask = types();
        android.app.Notification notification = listeningNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && mask != 0) {
            startForeground(Notifier.LISTENING_NOTIFICATION_ID, notification, mask);
        } else {
            startForeground(Notifier.LISTENING_NOTIFICATION_ID, notification);
        }
        ClaireState.get().setForegroundTypes(mask);
    }

    /**
     * Re-declare the type mask after a permission changed.
     *
     * <p>{@code startForeground} may be called again at any time; the second call replaces
     * the notification and the types. That is the only supported way to widen the mask,
     * and calling it is what makes "the owner granted background location while the
     * service was running" take effect without a restart.
     */
    private void refreshForeground() {
        if (state == State.IDLE) {
            return;
        }
        int wanted = types();
        if (wanted == ClaireState.get().foregroundTypes()) {
            return;
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && wanted != 0) {
                startForeground(Notifier.LISTENING_NOTIFICATION_ID, listeningNotification(),
                        wanted);
            }
            ClaireState.get().setForegroundTypes(wanted);
        } catch (RuntimeException refused) {
            Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "the phone refused to widen the service type: "
                            + refused.getClass().getSimpleName());
        }
    }

    private android.app.Notification listeningNotification() {
        boolean paused = state == State.PAUSED;
        String title = getString(paused ? R.string.notif_title_paused : R.string.notif_title_listening);
        return Notifier.listening(this, title, notificationText(), describeLink(), paused);
    }

    /**
     * The one line that says everything that is on.
     *
     * <p>Deliberately all three senses, not just the microphone: the owner asked for
     * always-on location and an autonomous camera, and both of those are things a person
     * is entitled to see at a glance in the shade without opening the app.
     */
    private String notificationText() {
        ClaireState seen = ClaireState.get();
        StringBuilder text = new StringBuilder();
        switch (seen.mic()) {
            case LISTENING:
                text.append("Microphone open");
                break;
            case PAUSED:
                text.append("Microphone paused");
                break;
            case DENIED:
                text.append("Microphone: permission missing");
                break;
            default:
                text.append("Microphone stopped");
                break;
        }
        switch (seen.location()) {
            case ALWAYS:
                text.append(" · Location every ").append(seen.locationIntervalSeconds())
                        .append("s");
                break;
            case WHILE_USING:
                text.append(" · Location only while the app is on screen");
                break;
            case DENIED:
                text.append(" · Location: permission missing");
                break;
            case SERVICES_OFF:
                text.append(" · Location services are off");
                break;
            default:
                text.append(" · Location off");
                break;
        }
        switch (seen.camera()) {
            case ARMED:
                text.append(" · Camera ready (Claire may ask)");
                break;
            case CAPTURING:
                text.append(" · Camera: taking a photo");
                break;
            case DENIED:
                text.append(" · Camera: permission missing");
                break;
            default:
                text.append(" · Camera off");
                break;
        }
        if (lowBattery && prefs.lowBatteryPause()) {
            text.append(" · battery low, audio held back");
        }
        if (silencedBySystem) {
            text.append(" · the system muted the microphone");
        }
        return text.toString();
    }

    private String describeLink() {
        ClaireState.Link link = ClaireState.get().link();
        switch (link) {
            case OK:
                return ClaireState.get().linkDetail().isEmpty()
                        ? "connected" : ClaireState.get().linkDetail();
            case TOKEN_REFUSED:
                return "the laptop refused the token — check Settings";
            case UNREACHABLE:
                return ClaireState.get().linkDetail().isEmpty()
                        ? "unreachable; utterances are queued" : ClaireState.get().linkDetail();
            default:
                return prefs.configured() ? prefs.baseUrl() : "no laptop configured yet";
        }
    }

    private void updateNotification() {
        if (state == State.IDLE) {
            return;
        }
        try {
            androidx.core.app.NotificationManagerCompat.from(this)
                    .notify(Notifier.LISTENING_NOTIFICATION_ID, listeningNotification());
        } catch (SecurityException denied) {
            // POST_NOTIFICATIONS is not granted; the service itself is unaffected.
        }
    }

    private void stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } else {
            stopForeground(true);
        }
    }

    // -- always-on: location and the autonomous camera --------------------- //

    /** Start whatever always-on modes the owner has switched on and can legally run. */
    private void startAlwaysOn() {
        startLocationIfWanted();
        startPeriodicCapture();
    }

    private void startLocationIfWanted() {
        if (!prefs.continuousLocation()) {
            ClaireState.get().setLocation(ClaireState.Loc.OFF, 0);
            return;
        }
        if (!LocationReporter.hasPermission(this)) {
            ClaireState.get().setLocation(ClaireState.Loc.DENIED, 0);
            Notifier.alert(this, getString(R.string.app_name), getString(R.string.location_denied));
            return;
        }
        if (!LocationReporter.servicesEnabled(this)) {
            ClaireState.get().setLocation(ClaireState.Loc.SERVICES_OFF, 0);
            Notifier.alert(this, getString(R.string.app_name),
                    getString(R.string.location_services_off));
            return;
        }
        if (locationUpdates == null) {
            locationUpdates = new LocationReporter.Continuous(this,
                    prefs.locationIntervalSeconds() * 1000L, 0f,
                    new LocationReporter.Continuous.Sink() {
                        @Override
                        public void onLocation(Location location) {
                            sendLocation(location, "continuous");
                        }

                        @Override
                        public void onProblem(LocationReporter.Outcome outcome, String detail) {
                            main.post(() -> {
                                ClaireState.get().setLocation(
                                        outcome == LocationReporter.Outcome.SERVICES_OFF
                                                ? ClaireState.Loc.SERVICES_OFF
                                                : ClaireState.Loc.DENIED,
                                        prefs.locationIntervalSeconds());
                                conversationOnce("continuous location stopped: " + detail);
                                updateNotification();
                            });
                        }
                    });
        }
        LocationReporter.Outcome outcome = locationUpdates.start();
        boolean background = LocationReporter.hasBackground(this);
        switch (outcome) {
            case OK:
                ClaireState.get().setLocation(background ? ClaireState.Loc.ALWAYS
                        : ClaireState.Loc.WHILE_USING, prefs.locationIntervalSeconds());
                conversationOnce("sharing location every " + prefs.locationIntervalSeconds()
                        + " seconds" + (background ? ""
                        : " (only while this app is on screen: \"Allow all the time\" is "
                          + "not granted)"));
                break;
            case SERVICES_OFF:
                ClaireState.get().setLocation(ClaireState.Loc.SERVICES_OFF, 0);
                break;
            case PERMISSION_DENIED:
                ClaireState.get().setLocation(ClaireState.Loc.DENIED, 0);
                break;
            default:
                ClaireState.get().setLocation(ClaireState.Loc.DENIED, 0);
                conversationOnce("continuous location could not start: " + outcome);
                break;
        }
        updateNotification();
    }

    private void stopLocation(String reason) {
        if (locationUpdates != null) {
            locationUpdates.stop();
        }
        ClaireState.get().setLocation(ClaireState.Loc.OFF, 0);
        if (state != State.IDLE) {
            updateNotification();
        }
    }

    /** POST one position, on the upload thread, and say what happened. */
    private void sendLocation(final Location location, final String source) {
        ClaireState.get().setLastFix(location.getLatitude(), location.getLongitude(),
                location.hasAccuracy() ? location.getAccuracy() : 0f);
        uploader.execute(() -> {
            ClaireClient.Answer answer = ClaireClient.from(prefs).sendLocation(
                    location.getLatitude(), location.getLongitude(),
                    LocationReporter.accuracyOf(location), source);
            noteLink(answer);
            main.post(() -> {
                if (answer.ok()) {
                    ClaireState.get().setLink(ClaireState.Link.OK, prefs.baseUrl());
                } else if (!answer.reachedServer()) {
                    conversationOnce("could not send the position: " + answer.transportError);
                } else {
                    conversationOnce("the position was refused: " + answer.describe("sending it"));
                }
                updateNotification();
            });
        });
    }

    /** One position now, on the owner's explicit tap or on Claire's request. */
    private void reportOneLocation(final String why) {
        if (!LocationReporter.hasPermission(this)) {
            conversationOnce(getString(R.string.location_denied));
            Notifier.alert(this, getString(R.string.app_name), getString(R.string.location_denied));
            return;
        }
        if (!LocationReporter.servicesEnabled(this)) {
            conversationOnce(getString(R.string.location_services_off));
            Notifier.alert(this, getString(R.string.app_name),
                    getString(R.string.location_services_off));
            return;
        }
        if (locationUpdates != null && locationUpdates.isRunning()) {
            // Continuous mode already has a stream going, but Claire asked a question and
            // deserves an answer now rather than at the next tick, and the bridge's record
            // of where this phone is should move.  requestCurrent prefers a fix younger
            // than two minutes, so in continuous mode this is normally free.
            conversationOnce("Claire asked where this phone is (continuous mode is on; "
                    + "sending the newest fix now)");
        } else {
            conversationOnce("Claire asked where this phone is");
        }
        LocationReporter.requestCurrent(this, (outcome, location, detail) -> {
            if (outcome == LocationReporter.Outcome.OK && location != null) {
                sendLocation(location, "on demand");
            } else {
                conversationOnce("no position: " + detail);
                updateNotification();
            }
        });
    }

    // -- the autonomous camera -------------------------------------------- //

    /**
     * Take one photo and send it to Claire, without any screen being on.
     *
     * @param why a sentence for the notification and the conversation: the owner has to be
     *            able to tell who asked for this after the fact.
     */
    public void captureNow(final String why) {
        captureNow(why, "");
    }

    public void captureNow(final String why, final String question) {
        if (!CameraCapture.hasPermission(this)) {
            ClaireState.get().setCamera(ClaireState.Cam.DENIED);
            conversationOnce("cannot take a photo: camera permission has not been granted");
            Notifier.alert(this, getString(R.string.app_name),
                    "Camera permission is missing, so Claire cannot take a photo.");
            updateNotification();
            return;
        }
        if (photoInFlight) {
            conversationOnce("a photo for Claire is already being taken");
            return;
        }
        photoInFlight = true;
        ClaireState.get().setCamera(ClaireState.Cam.CAPTURING);
        updateNotification();
        if (camera == null) {
            camera = new CameraCapture(this);
        }
        camera.capture(new CameraCapture.Sink() {
            @Override
            public void onCaptured(byte[] jpeg, int rotationDegrees) {
                photoInFlight = false;
                refreshCameraState();
                main.post(() -> {
                    Conversation.get(VoiceService.this).add(Conversation.Who.PHONE,
                            "photo taken — " + why);
                    Notifier.captureNotice(VoiceService.this, why);
                    updateNotification();
                });
                uploadPhoto(jpeg, question.isEmpty() ? prefs.question() : question, why);
            }

            @Override
            public void onFailed(String detail) {
                photoInFlight = false;
                refreshCameraState();
                main.post(() -> {
                    conversationOnce("no photo: " + detail);
                    updateNotification();
                });
            }
        });
    }

    /** Recompute the camera line from what is granted and switched on. */
    private void refreshCameraState() {
        if (photoInFlight) {
            ClaireState.get().setCamera(ClaireState.Cam.CAPTURING);
        } else if (!CameraCapture.hasPermission(this)) {
            ClaireState.get().setCamera(ClaireState.Cam.DENIED);
        } else if (!prefs.autoCapture()) {
            ClaireState.get().setCamera(ClaireState.Cam.OFF);
        } else {
            ClaireState.get().setCamera(ClaireState.Cam.ARMED);
        }
    }

    private void uploadPhoto(final byte[] jpeg, final String question, final String why) {
        uploader.execute(() -> {
            ClaireClient.Answer answer = ClaireClient.from(prefs).see(jpeg, question);
            noteLink(answer);
            main.post(() -> {
                String detail;
                if (!answer.reachedServer()) {
                    detail = "the photo could not be sent (" + answer.transportError + ")";
                } else if (answer.status == 401) {
                    detail = "the laptop refused the token, so the photo was not sent";
                } else if (answer.ok()) {
                    String description = ClaireClient.field(answer.body, "description");
                    detail = description.isEmpty()
                            ? "Claire saw the photo (" + why + ")"
                            : "Claire saw: " + description;
                } else {
                    detail = answer.describe("sending the photo");
                }
                Conversation.get(this).add(Conversation.Who.PHONE, detail);
                if (answer.ok()) {
                    ClaireState.get().setLink(ClaireState.Link.OK, prefs.baseUrl());
                }
                updateNotification();
            });
            // The bytes are not held anywhere after this: the temp file is already gone
            // and the array goes out of scope with the lambda.  Nothing is kept on disk.
        });
    }

    /** The periodic camera, if the owner set a period.  Opt-in, and it says so. */
    private void startPeriodicCapture() {
        main.removeCallbacks(captureTick);
        refreshCameraState();
        updateNotification();
        int minutes = prefs.capturePeriodMinutes();
        if (minutes <= 0 || !prefs.autoCapture() || state != State.LISTENING) {
            return;
        }
        main.postDelayed(captureTick, minutes * 60_000L);
    }

    private void stopPeriodicCapture() {
        main.removeCallbacks(captureTick);
        refreshCameraState();
    }

    private final Runnable captureTick = new Runnable() {
        @Override
        public void run() {
            if (state != State.LISTENING || !prefs.autoCapture()
                    || prefs.capturePeriodMinutes() <= 0) {
                return;
            }
            captureNow("the periodic photo (" + prefs.capturePeriodMinutes() + " minutes)");
            main.postDelayed(this, prefs.capturePeriodMinutes() * 60_000L);
        }
    };

    // -- the microphone ---------------------------------------------------- //

    private void startCapture() {
        if (capturing) {
            return;
        }
        if (micThread == null) {
            micThread = new HandlerThread("claire-mic");
            micThread.start();
            micHandler = new Handler(micThread.getLooper());
        }
        capturing = true;
        micHandler.post(this::captureLoop);
    }

    /**
     * Stop the loop and let the microphone go.
     *
     * <p>Called from the main thread while {@link #captureLoop} is blocked in
     * {@code AudioRecord.read()}. Releasing the recorder *here* is what unblocks that
     * read and lets the loop notice {@link #capturing} and exit; posting the release
     * to the microphone thread instead would queue it behind the very loop it is
     * meant to stop, and the service would stop responding with the microphone still
     * open. The segmenter is flushed by the loop itself, on the thread that owns it,
     * so its ring buffer is never touched from two threads at once.
     */
    private void stopCapture() {
        capturing = false;
        speaking = false;
        releaseRecorder();
    }

    private void captureLoop() {
        releaseRecorder();
        final AudioSegmenter.Params params = new AudioSegmenter.Params();
        switch (prefs.sensitivity()) {
            case 0:
                params.noiseMultiplier = 4.0;
                params.absoluteFloor = 350.0;
                break;
            case 2:
                params.noiseMultiplier = 2.2;
                params.absoluteFloor = 150.0;
                params.hangoverMs = 900;
                break;
            default:
                break;
        }
        final AudioSegmenter local = new AudioSegmenter(SAMPLE_RATE, params,
                (pcm, length, rate) -> {
                    short[] copy = new short[length];
                    System.arraycopy(pcm, 0, copy, 0, length);
                    enqueue(copy);
                });

        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        int bufferBytes = Math.max(minimum, SAMPLES_PER_READ * 4);
        try {
            recorder = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, bufferBytes);
        } catch (Exception error) {
            recorder = null;
        }
        if (recorder == null || recorder.getState() != AudioRecord.STATE_INITIALIZED) {
            releaseRecorder();
            local.flush();
            main.post(() -> {
                Conversation.get(this).add(Conversation.Who.SYSTEM,
                        "could not open the microphone; is another app recording?");
                updateNotification();
            });
            return;
        }
        try {
            recorder.startRecording();
        } catch (IllegalStateException error) {
            releaseRecorder();
            main.post(() -> Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "the microphone refused to start: " + error.getMessage()));
            return;
        }

        short[] window = new short[SAMPLES_PER_READ];
        boolean heardSelf = false;
        while (capturing && !Thread.currentThread().isInterrupted()) {
            if (recorder == null) {
                break;
            }
            int read;
            try {
                read = recorder.read(window, 0, window.length);
            } catch (IllegalStateException gone) {
                break;
            }
            if (read <= 0) {
                continue;
            }
            if (speaking) {
                if (!heardSelf) {
                    // Claire started talking while the owner was mid-sentence: send what
                    // the owner had already said, then stop listening until she finishes.
                    local.flush();
                    heardSelf = true;
                }
                continue;
            }
            heardSelf = false;
            if (silencedBySystem) {
                // The platform has muted us (a call, or another recorder). Feeding
                // this to the detector would fill the queue with digital silence and
                // then "transcribe" it.
                continue;
            }
            local.accept(window, 0, read);
        }
        releaseRecorder();
        // Flush on this thread, which owns the detector, so a sentence that was cut
        // off by the pause is still sent rather than silently dropped.
        local.flush();
    }

    private void releaseRecorder() {
        AudioRecord local = recorder;
        recorder = null;
        if (local == null) {
            return;
        }
        try {
            if (local.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                local.stop();
            }
        } catch (IllegalStateException ignored) {
            // Already stopped.
        }
        try {
            local.release();
        } catch (Exception ignored) {
            // Nothing useful can be done while tearing down.
        }
    }

    // -- getting utterances to Claire ------------------------------------- //

    private void enqueue(short[] pcm) {
        boolean overflowed = false;
        synchronized (pending) {
            pending.addLast(pcm);
            while (pending.size() > MAX_PENDING_UTTERANCES) {
                pending.pollFirst();
                overflowed = true;
            }
        }
        if (overflowed) {
            main.post(() -> conversationOnce(
                    "the network has been down long enough that the oldest unsent "
                            + "utterance was dropped; " + MAX_PENDING_UTTERANCES
                            + " is the most this app will hold"));
            updateNotification();
        }
        flushPending();
    }

    private void flushPending() {
        if (state != State.LISTENING) {
            return;
        }
        uploader.execute(() -> {
            ClaireClient client = ClaireClient.from(prefs);
            while (true) {
                short[] next;
                synchronized (pending) {
                    next = pending.pollFirst();
                }
                if (next == null) {
                    return;
                }
                if (!sendUtterance(client, next)) {
                    // It went back to the front of the queue. Draining again in a
                    // tight loop would spin against a dead network or a flat
                    // battery, so this attempt ends here and the next trigger
                    // (connectivity, plain text send, resume) tries again.
                    return;
                }
            }
        });
    }

    /**
     * @return true when the utterance left the phone, false when it was put back.
     */
    private boolean sendUtterance(ClaireClient client, short[] pcm) {
        if (lowBattery && prefs.lowBatteryPause()) {
            // Battery-aware: keep listening and keep the utterance, but do not spend
            // radio and CPU on it until the phone is charging again.
            synchronized (pending) {
                pending.addFirst(pcm);
            }
            return false;
        }
        ClaireClient.Answer answer = client.transcribe(pcm, pcm.length, SAMPLE_RATE);
        noteLink(answer);
        if (!answer.ok()) {
            // Put it back at the front so order is preserved, and say so once.
            synchronized (pending) {
                pending.addFirst(pcm);
            }
            String detail;
            if (!answer.reachedServer()) {
                detail = "Claire could not be reached (" + answer.transportError
                        + "); the utterance is queued and will be sent when the phone "
                        + "is back on the network";
            } else if (answer.status == 401) {
                detail = "the laptop refused the token; check Settings";
            } else {
                detail = answer.describe("sending audio");
            }
            main.post(() -> {
                Notifier.alert(this, getString(R.string.app_name), detail);
                conversationOnce(detail);
                updateNotification();
            });
            return false;
        }
        String text = ClaireClient.field(answer.body, "text");
        String engine = ClaireClient.field(answer.body, "engine");
        boolean delivered = ClaireClient.booleanField(answer.body, "delivered");
        if (text.isEmpty()) {
            main.post(() -> conversationOnce("heard nothing intelligible in that utterance"));
            return true;
        }
        String note = engine.isEmpty() ? "" : "  [" + engine + "]";
        main.post(() -> {
            Conversation.get(this).add(Conversation.Who.YOU, text);
            if (!delivered) {
                Conversation.get(this).add(Conversation.Who.SYSTEM,
                        "transcribed but not delivered to Claire's voice loop" + note);
            } else {
                Conversation.get(this).add(Conversation.Who.PHONE,
                        "sent to Claire" + note);
            }
        });
        return true;
    }

    // -- Claire's replies and her requests -------------------------------- //

    private void scheduleReplyPoll() {
        if (replyPollScheduled) {
            return;
        }
        replyPollScheduled = true;
        main.postDelayed(replyPoll, REPLY_POLL_MS);
    }

    private final Runnable replyPoll = new Runnable() {
        @Override
        public void run() {
            replyPollScheduled = false;
            if (state == State.IDLE) {
                return;
            }
            uploader.execute(() -> {
                ClaireClient.Answer answer = ClaireClient.from(prefs).reply();
                noteLink(answer);
                if (answer.ok()) {
                    List<Conversation.Item> items = Conversation.itemsFrom(answer.body);
                    for (Conversation.Item item : items) {
                        if (item.kind == Conversation.Kind.COMMAND) {
                            handleCommand(item);
                        } else if (!item.text.isEmpty()) {
                            final String reply = item.text;
                            main.post(() -> {
                                Conversation.get(VoiceService.this)
                                        .add(Conversation.Who.CLAIRE, reply);
                                speak(reply);
                            });
                        } else {
                            main.post(() -> conversationOnce(
                                    "Claire sent something this app could not read"));
                        }
                    }
                }
                main.post(VoiceService.this::updateNotification);
            });
            scheduleReplyPoll();
        }
    };

    /**
     * Act on one command out of {@code GET /reply}.
     *
     * <p>Two commands exist, and both are things the phone can honestly refuse:
     * {@code photo} is refused when the owner has switched autonomous capture off or the
     * camera permission is gone, and {@code location} is refused when location is not
     * available. Both refusals are written into the conversation, because a request that
     * silently does nothing looks exactly like a broken bridge.
     */
    private void handleCommand(Conversation.Item item) {
        switch (item.command) {
            case "photo":
                if (!prefs.autoCapture()) {
                    main.post(() -> conversationOnce(
                            "Claire asked for a photo, but autonomous photos are switched "
                                    + "off in Settings, so none was taken"));
                    return;
                }
                main.post(() -> {
                    conversationOnce(getString(R.string.label_photo_request));
                    captureNow("Claire asked through the bridge", item.question);
                });
                return;
            case "location":
                main.post(() -> {
                    conversationOnce("Claire asked where this phone is");
                    reportOneLocation("Claire asked through the bridge");
                });
                return;
            default:
                main.post(() -> conversationOnce("Claire asked for something this app "
                        + "does not know how to do: " + item.command));
                return;
        }
    }

    // -- link state -------------------------------------------------------- //

    /**
     * Record the newest fact about reachability and the token.
     *
     * <p>Called from every request path, which means the notification's sub-text is
     * always the result of something that really happened rather than of a health check
     * nobody looks at. A 401 is deliberately a separate state from "unreachable": the
     * fixes are different (fix the token, versus fix the tailnet).
     */
    private void noteLink(ClaireClient.Answer answer) {
        if (!answer.reachedServer()) {
            ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                    "unreachable (" + answer.transportError + "); utterances are queued");
        } else if (answer.status == 401 || answer.status == 403) {
            ClaireState.get().setLink(ClaireState.Link.TOKEN_REFUSED,
                    "the laptop refused the token — check Settings");
        } else if (answer.ok()) {
            ClaireState.get().setLink(ClaireState.Link.OK, prefs.baseUrl());
        } else {
            ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                    "the bridge answered HTTP " + answer.status);
        }
    }

    // -- audio focus, for the output side --------------------------------- //

    private void maybeStartTts() {
        if (!prefs.speakReplies() || tts != null) {
            return;
        }
        tts = new TextToSpeech(this, status -> {
            // setLanguage() before onInit() is a no-op on several builds, and a
            // TextToSpeech with no language speaks nothing at all.
            if (status == TextToSpeech.SUCCESS) {
                tts.setLanguage(Locale.getDefault());
                tts.setOnUtteranceProgressListener(new android.speech.tts.UtteranceProgressListener() {
                    @Override
                    public void onStart(String utteranceId) {
                    }

                    @Override
                    public void onDone(String utteranceId) {
                        finishSpeaking();
                    }

                    @Override
                    public void onError(String utteranceId) {
                        finishSpeaking();
                    }
                });
                ttsReady = true;
            } else {
                ttsReady = false;
            }
        });
    }

    /**
     * Say something out loud, and stop listening while doing it.
     *
     * <p>The second half of that sentence was learned on the device rather than designed:
     * with the microphone open and Claire speaking, the phone heard <em>her</em>, sent the
     * transcription back as the owner's words, and Claire answered her own sentence —
     * a loop that shows up in the conversation log as the owner apparently saying
     * "Claire is already answering something else". The audio source is
     * {@code VOICE_RECOGNITION}, which is supposed to cancel the speaker, and on this
     * phone it does not do so well enough. So the microphone's contribution is dropped
     * for as long as she is talking, and the segment that was in flight is flushed
     * first, because that one really was the owner.
     */
    private void speak(String text) {
        if (!prefs.speakReplies()) {
            return;
        }
        String words = text == null ? "" : text.trim();
        if (words.isEmpty()) {
            return;
        }
        if (prefs.fishSpeaks()) {
            speakWithFish(words);
            return;
        }
        // Either the owner chose the phone's own engine, or they chose Fish and no
        // key is set. Both are the fallback path, and the second one is announced
        // once rather than leaving the app silently in a different voice.
        if (ClairePrefs.TTS_FISH.equals(prefs.ttsBackend())) {
            conversationOnce("no Fish key is set, so replies are spoken by the phone's"
                    + " own voice; add one in Settings to use Fish Audio");
        }
        speakWithDevice(words);
    }

    /**
     * The owner's chosen path: Fish Audio, called from the phone, streamed and played
     * as it arrives. See {@link FishTts} for the wire call and the playback decision.
     */
    private void speakWithFish(String words) {
        if (fish == null) {
            fish = new FishTts();
        }
        if (!requestFocus()) {
            return;
        }
        speaking = true;
        ClaireState.get().setVoice("Fish Audio \u2192 streaming");
        main.removeCallbacks(speechGuard);
        // A streamed reply can be long, and the first byte takes about a second; the
        // guard only exists so a wedged request cannot leave the microphone deaf.
        main.postDelayed(speechGuard, 15_000L + 90L * words.length());
        fish.speak(prefs.fishToken(), prefs.fishModel(), prefs.fishVoiceId(), words,
                new FishTts.Listener() {
                    @Override
                    public void onStarted() {
                        main.post(() -> ClaireState.get().setVoice("Fish Audio \u2192 playing"));
                    }

                    @Override
                    public void onDone() {
                        main.post(VoiceService.this::finishSpeaking);
                    }

                    @Override
                    public void onError(String message) {
                        main.post(() -> {
                            Conversation.get(VoiceService.this).add(
                                    Conversation.Who.SYSTEM, message);
                            ClaireState.get().setVoice("Fish failed: " + message);
                            // Do not leave the owner in silence when a working engine
                            // is sitting right here. Say the line in the phone's own
                            // voice and be honest about which one it was.
                            finishSpeaking();
                            if (prefs.speakReplies()) {
                                speakWithDevice(words);
                            }
                        });
                    }
                });
    }

    /** The fallback: the phone's built-in text-to-speech, which needs no network. */
    private void speakWithDevice(String text) {
        maybeStartTts();
        if (tts == null || !ttsReady) {
            return;
        }
        if (!requestFocus()) {
            // Something else owns the audio output; Claire stays silent rather than
            // talking over it. The words are still in the conversation view.
            return;
        }
        ClaireState.get().setVoice("phone text-to-speech");
        speaking = true;
        // A guard in case the engine never calls back (a voice that is installed but
        // cannot speak): without it, one silent failure would leave the microphone deaf
        // for the rest of the session.
        main.removeCallbacks(speechGuard);
        main.postDelayed(speechGuard, 4_000L + 90L * text.length());
        tts.speak(text.trim(), TextToSpeech.QUEUE_ADD, null, "claire-" + System.nanoTime());
    }

    private final Runnable speechGuard = this::finishSpeaking;

    private void finishSpeaking() {
        main.removeCallbacks(speechGuard);
        speaking = false;
        abandonFocus();
    }

    private boolean requestFocus() {
        if (audioManager == null) {
            return true;
        }
        AudioManager.OnAudioFocusChangeListener listener = change -> {
            if (change == AudioManager.AUDIOFOCUS_LOSS
                    || change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
                if (tts != null) {
                    tts.stop();
                }
                if (fish != null) {
                    // A streamed reply is cut off the same way a spoken one is: the
                    // request is abandoned and the track is silenced, not left to
                    // finish over whatever just took the focus.
                    fish.stop();
                }
                // Losing focus means she has stopped talking, whatever the engine says:
                // the microphone has to start hearing the owner again.
                finishSpeaking();
            }
        };
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            AudioFocusRequest request = new AudioFocusRequest.Builder(
                    AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_ASSISTANT)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build())
                    .setOnAudioFocusChangeListener(listener)
                    .build();
            int result = audioManager.requestAudioFocus(request);
            focusRequest = request;
            return result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        }
        int result = audioManager.requestAudioFocus(listener,
                AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK);
        return result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
    }

    private void abandonFocus() {
        if (audioManager == null || focusRequest == null) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && focusRequest instanceof AudioFocusRequest) {
            audioManager.abandonAudioFocusRequest((AudioFocusRequest) focusRequest);
        }
        focusRequest = null;
    }

    private void registerRecordingCallback() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || audioManager == null) {
            return;
        }
        recordingCallback = new AudioManager.AudioRecordingCallback() {
            @Override
            public void onRecordingConfigChanged(List<AudioRecordingConfiguration> configs) {
                boolean muted = false;
                for (AudioRecordingConfiguration config : configs) {
                    if (config.isClientSilenced()) {
                        muted = true;
                        break;
                    }
                }
                if (muted != silencedBySystem) {
                    silencedBySystem = muted;
                    final boolean nowMuted = muted;
                    main.post(() -> {
                        Conversation.get(VoiceService.this).add(Conversation.Who.SYSTEM,
                                nowMuted
                                ? "the system muted this app's microphone (a call, or "
                                  + "another recorder); listening continues but nothing is "
                                  + "being transcribed"
                                : "the microphone is not muted any more");
                        updateNotification();
                    });
                }
            }
        };
        audioManager.registerAudioRecordingCallback(recordingCallback, main);
    }

    private void unregisterRecordingCallback() {
        if (audioManager != null && recordingCallback != null) {
            try {
                audioManager.unregisterAudioRecordingCallback(recordingCallback);
            } catch (Exception ignored) {
                // Already gone.
            }
            recordingCallback = null;
        }
    }

    private void conversationOnce(String message) {
        Conversation.get(this).add(Conversation.Who.SYSTEM, message);
    }

    // -- battery and connectivity ----------------------------------------- //

    private final BroadcastReceiver batteryReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            int level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
            int scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
            int status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1);
            charging = status == BatteryManager.BATTERY_STATUS_CHARGING
                    || status == BatteryManager.BATTERY_STATUS_FULL;
            if (level < 0 || scale <= 0) {
                return;
            }
            int percent = (int) (100.0 * level / scale);
            boolean nowLow = percent <= LOW_BATTERY_PERCENT && !charging;
            if (nowLow != lowBattery) {
                lowBattery = nowLow;
                Conversation.get(VoiceService.this).add(Conversation.Who.SYSTEM, nowLow
                        ? "battery is at " + percent + "% and the phone is not charging; "
                          + "listening continues but audio is held back until it is "
                          + "(this is the battery-aware setting)"
                        : "battery is fine again; queued audio is being sent");
                updateNotification();
                if (!nowLow) {
                    flushPending();
                }
            }
        }
    };

    private void registerConnectivityCallback() {
        connectivityManager =
                (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (connectivityManager == null) {
            return;
        }
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                networkUp = true;
                // In the diary on purpose: whether a network change ever precedes a
                // service death is the whole question behind "it closes itself".
                Diag.note("network is back");
                main.post(() -> {
                    Conversation.get(VoiceService.this).add(Conversation.Who.PHONE,
                            "the network is back; sending what was queued");
                    updateNotification();
                    flushPending();
                });
            }

            @Override
            public void onLost(Network network) {
                networkUp = false;
                // The service is NOT stopped here, and that is deliberate: losing the
                // tailnet link must not take the microphone down with it. Utterances
                // queue until the link returns.
                Diag.note("network lost; the listening service stays up and queues");
                main.post(() -> {
                    ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                            "no network; utterances are queued");
                    updateNotification();
                });
            }

            @Override
            public void onCapabilitiesChanged(Network network,
                                              NetworkCapabilities capabilities) {
                boolean up = capabilities != null
                        && capabilities.hasCapability(
                                NetworkCapabilities.NET_CAPABILITY_INTERNET);
                if (up != networkUp) {
                    networkUp = up;
                    main.post(() -> {
                        if (up) {
                            flushPending();
                        }
                        updateNotification();
                    });
                }
            }
        };
        try {
            connectivityManager.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException refused) {
            networkCallback = null;
        }
    }

    private void unregisterConnectivityCallback() {
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (Exception ignored) {
                // Already gone.
            }
            networkCallback = null;
        }
    }

    private void unregisterQuietly() {
        try {
            unregisterReceiver(batteryReceiver);
        } catch (IllegalArgumentException ignored) {
            // Never registered.
        }
    }
}
