package com.claire.companion;

import android.Manifest;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
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
 * Always listening, as a foreground service.
 *
 * <p>This class is the difference between a button and a companion. It holds the
 * microphone open continuously, decides for itself where the owner's sentences begin
 * and end ({@link AudioSegmenter}), and sends each one to Claire to be recognised —
 * with no interaction at all.
 *
 * <h3>The decisions worth defending</h3>
 *
 * <p><b>Segmentation is on the phone; recognition is on the laptop.</b> The bridge
 * takes one complete utterance of WAV, so the phone ships an utterance and nothing
 * else. It would have been easy to buffer the whole session and upload it, and it
 * would have been wrong twice: the laptop would have to run a second, streaming
 * endpoint that does not exist in its design, and the owner's audio would spend
 * minutes sitting in flash on a phone instead of being transcribed and forgotten.
 *
 * <p><b>Audio focus is handled in both directions, and not by grabbing it.</b> This is
 * an always-on <em>listener</em>: requesting {@code AUDIOFOCUS_GAIN} would stop the
 * owner's music so that Claire could hear better, which is precisely backwards. So
 * focus is taken only while she <em>speaks</em> (transient, may-duck) and given back
 * immediately; what governs the microphone is
 * {@link AudioManager#registerAudioRecordingCallback}, which is the platform's own
 * answer to "another app is recording, or a call is in progress" — the phone is
 * silenced by the system, and this service pauses rather than transcribing silence.
 *
 * <p><b>The service is microphone-typed and started from a foreground activity or a
 * notification tap.</b> Android 14 forbids starting a {@code microphone} foreground
 * service from the background, including from {@code BOOT_COMPLETED}. That is why
 * {@link BootReceiver} posts a notification instead of starting this service: doing it
 * the other way throws {@code ForegroundServiceStartNotAllowedException}, which on a
 * phone with no log to read is indistinguishable from "the app does nothing".
 *
 * <p><b>Nothing is lost silently when the network drops.</b> Finished utterances go
 * into a small bounded queue; when connectivity returns they are sent, in order, and
 * if the queue overflows the owner is told in the conversation view. The queue is
 * deliberately small: audio of a conversation is the most private thing this app
 * touches, and hoarding minutes of it in flash to protect against a one-minute
 * outage is the wrong trade.
 */
public class VoiceService extends Service {

    private static final String TAG = "ClaireVoice";

    public static final String ACTION_START = "com.claire.companion.START";
    public static final String ACTION_STOP = "com.claire.companion.STOP";
    public static final String ACTION_PAUSE = "com.claire.companion.PAUSE";
    public static final String ACTION_RESUME = "com.claire.companion.RESUME";

    /** The sample rate the bridge and the recogniser both expect. */
    private static final int SAMPLE_RATE = ClaireWire.SAMPLE_RATE;
    private static final int SAMPLES_PER_READ = 1_600;          // 100 ms
    private static final int MAX_PENDING_UTTERANCES = 8;
    private static final long REPLY_POLL_MS = 4_000L;
    private static final int LOW_BATTERY_PERCENT = 15;

    private enum State { IDLE, LISTENING, PAUSED }

    private static volatile State state = State.IDLE;

    public static boolean isListening() {
        return state == State.LISTENING;
    }

    public static boolean isPaused() {
        return state == State.PAUSED;
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

    private boolean lowBattery;
    private boolean charging;
    private boolean networkUp = true;
    private boolean replyPollScheduled;
    private TextToSpeech tts;
    private boolean ttsReady;
    private String lastReported = "";

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = new ClairePrefs(this);
        Notifier.ensureChannels(this);
        audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        registerReceiver(batteryReceiver, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        registerConnectivityCallback();
        registerRecordingCallback();
        maybeStartTts();
        Conversation.get(this).add(Conversation.Who.SYSTEM, "Claire companion service started");
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
                resumeListening();
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
    public void onDestroy() {
        stopCapture();
        unregisterQuietly();
        unregisterConnectivityCallback();
        unregisterRecordingCallback();
        abandonFocus();
        if (tts != null) {
            tts.shutdown();
            tts = null;
            ttsReady = false;
        }
        uploader.shutdownNow();
        if (micThread != null) {
            micThread.quitSafely();
            micThread = null;
        }
        state = State.IDLE;
        super.onDestroy();
    }

    // -- lifecycle of listening ------------------------------------------- //

    private void startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            state = State.IDLE;
            Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "cannot listen: microphone permission has not been granted");
            Notifier.alert(this, getString(R.string.app_name),
                    "Microphone permission is missing, so Claire cannot listen.");
            stopSelf();
            return;
        }
        try {
            startForegroundCompat();
        } catch (RuntimeException refused) {
            // ForegroundServiceStartNotAllowedException on 12+, or a SecurityException
            // when the type's permission is missing. Named, not fatal.
            state = State.IDLE;
            Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "the phone refused to start the listening service: "
                            + refused.getClass().getSimpleName());
            stopSelf();
            return;
        }
        if (state == State.LISTENING) {
            return;
        }
        state = State.LISTENING;
        Conversation.get(this).add(Conversation.Who.PHONE, "Listening");
        startCapture();
        scheduleReplyPoll();
    }

    private void pauseListening(String reason) {
        if (state == State.IDLE) {
            // Pausing a service that is not running is a no-op, but the notification
            // still has to say something true.
            conversationOnce("Listening is not running");
            return;
        }
        state = State.PAUSED;
        stopCapture();
        conversationOnce(reason);
        updateNotification();
    }

    private void resumeListening() {
        if (state != State.PAUSED) {
            return;
        }
        state = State.LISTENING;
        startCapture();
        scheduleReplyPoll();
        conversationOnce("Listening again");
        updateNotification();
    }

    private void stopListening(String reason) {
        state = State.IDLE;
        stopCapture();
        conversationOnce(reason);
        stopForegroundCompat();
        stopSelf();
    }

    private void startForegroundCompat() {
        android.app.Notification notification = Notifier.listening(
                this, getString(R.string.app_name), describeState());
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(Notifier.LISTENING_NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } else {
            startForeground(Notifier.LISTENING_NOTIFICATION_ID, notification);
        }
    }

    private void updateNotification() {
        if (state == State.IDLE) {
            return;
        }
        try {
            androidx.core.app.NotificationManagerCompat.from(this)
                    .notify(Notifier.LISTENING_NOTIFICATION_ID,
                            Notifier.listening(this, getString(R.string.app_name),
                                    describeState()));
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

    /** What the notification and the app header both say. */
    private String describeState() {
        ClaireClient client = ClaireClient.from(prefs);
        String where = prefs.configured() ? client.baseUrl() : "no laptop configured yet";
        switch (state) {
            case LISTENING:
                if (!networkUp) {
                    return "Listening — " + where + " is unreachable; utterances are queued";
                }
                if (lowBattery && prefs.lowBatteryPause()) {
                    return "Listening — battery is low, so audio is not being sent";
                }
                if (silencedBySystem) {
                    return "Listening — paused capture while another app uses the microphone";
                }
                return "Listening — " + where;
            case PAUSED:
                return "Paused — tap Resume in the app";
            default:
                return "Not listening";
        }
    }

    // -- capture ---------------------------------------------------------- //

    private void startCapture() {
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
                pending.removeFirst();
                overflowed = true;
            }
        }
        final boolean dropped = overflowed;
        if (dropped) {
            main.post(() -> Conversation.get(this).add(Conversation.Who.SYSTEM,
                    "the laptop has been unreachable for a while and an older utterance "
                            + "was discarded rather than kept on the phone"));
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

    // -- Claire's replies ------------------------------------------------- //

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
            if (state != State.LISTENING) {
                return;
            }
            uploader.execute(() -> {
                ClaireClient.Answer answer = ClaireClient.from(prefs).reply();
                if (answer.ok()) {
                    List<String> replies = Conversation.repliesFrom(answer.body);
                    for (String reply : replies) {
                        main.post(() -> {
                            Conversation.get(VoiceService.this)
                                    .add(Conversation.Who.CLAIRE, reply);
                            speak(reply);
                        });
                    }
                }
            });
            scheduleReplyPoll();
        }
    };

    // -- audio focus, for the output side --------------------------------- //

    private void maybeStartTts() {
        if (!prefs.speakReplies() || tts != null) {
            return;
        }
        tts = new TextToSpeech(this, status -> {
            // setLanguage() before onInit() is a no-op on several builds, and a
            // TextToSpeech with no language says nothing, silently.
            if (status == TextToSpeech.SUCCESS && tts != null) {
                tts.setLanguage(Locale.getDefault());
                tts.setOnUtteranceProgressListener(new android.speech.tts.UtteranceProgressListener() {
                    @Override
                    public void onStart(String utteranceId) {
                    }

                    @Override
                    public void onDone(String utteranceId) {
                        abandonFocus();
                    }

                    @Override
                    public void onError(String utteranceId) {
                        abandonFocus();
                    }
                });
                ttsReady = true;
            } else {
                ttsReady = false;
            }
        });
    }

    private void speak(String text) {
        if (!prefs.speakReplies()) {
            return;
        }
        maybeStartTts();
        if (tts == null || !ttsReady || text == null || text.trim().isEmpty()) {
            return;
        }
        if (!requestFocus()) {
            // Something else owns the audio output; Claire stays silent rather than
            // talking over it. The words are still in the conversation view.
            return;
        }
        tts.speak(text.trim(), TextToSpeech.QUEUE_ADD, null, "claire-" + System.nanoTime());
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
                // The platform tells us when our own capture has been silenced --
                // a phone call, or a higher-priority recorder. This is input focus,
                // and it is the honest signal to react to.
                boolean silenced = false;
                for (AudioRecordingConfiguration config : configs) {
                    if (config.isClientSilenced()) {
                        silenced = true;
                        break;
                    }
                }
                if (silenced != silencedBySystem) {
                    silencedBySystem = silenced;
                    final boolean nowSilenced = silenced;
                    main.post(() -> {
                        Conversation.get(VoiceService.this)
                                .add(Conversation.Who.SYSTEM, nowSilenced
                                ? "the microphone was taken by another app or a call; "
                                  + "capture is paused until it is free"
                                : "the microphone is free again; capture resumed");
                        updateNotification();
                    });
                }
            }
        };
        audioManager.registerAudioRecordingCallback(recordingCallback, main);
    }

    private void unregisterRecordingCallback() {
        if (recordingCallback != null && audioManager != null) {
            try {
                audioManager.unregisterAudioRecordingCallback(recordingCallback);
            } catch (Exception ignored) {
                // Already gone.
            }
            recordingCallback = null;
        }
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
        networkUp = hasUsableNetwork();
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                onNetworkChanged(true);
            }

            @Override
            public void onLost(Network network) {
                onNetworkChanged(hasUsableNetwork());
            }
        };
        try {
            connectivityManager.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException refused) {
            // Some MIUI builds refuse this registration; the reply poll and the next
            // utterance retry still recover on their own.
            networkCallback = null;
        }
    }

    private void unregisterConnectivityCallback() {
        if (networkCallback != null && connectivityManager != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (Exception ignored) {
                // Already gone.
            }
            networkCallback = null;
        }
    }

    private void onNetworkChanged(boolean up) {
        boolean wasUp = networkUp;
        networkUp = up;
        if (up == wasUp) {
            updateNotification();
            return;
        }
        main.post(() -> {
            if (up) {
                Conversation.get(this).add(Conversation.Who.SYSTEM,
                        "the network is back; sending anything that was queued");
                flushPending();
            } else {
                Conversation.get(this).add(Conversation.Who.SYSTEM,
                        "the network dropped; listening continues and utterances are "
                                + "queued on the phone");
            }
            updateNotification();
        });
    }

    private boolean hasUsableNetwork() {
        if (connectivityManager == null) {
            return true;
        }
        Network active = connectivityManager.getActiveNetwork();
        if (active == null) {
            return false;
        }
        NetworkCapabilities capabilities =
                connectivityManager.getNetworkCapabilities(active);
        return capabilities != null
                && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }

    private void unregisterQuietly() {
        try {
            unregisterReceiver(batteryReceiver);
        } catch (IllegalArgumentException already) {
            // Not registered; nothing to undo.
        }
    }

    private void conversationOnce(String text) {
        if (text == null || text.equals(lastReported)) {
            return;
        }
        lastReported = text;
        Conversation.get(this).add(Conversation.Who.SYSTEM, text);
    }
}
