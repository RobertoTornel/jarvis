package com.claire.companion;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.recyclerview.widget.RecyclerView;

import com.google.android.material.appbar.MaterialToolbar;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.snackbar.Snackbar;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The one screen: what this phone is doing, what was said, and the four things the owner
 * asks for by hand.
 *
 * <p>The design bet is unchanged — the app is usually <em>not</em> being looked at, the
 * service is — but the screen now answers its three questions with state rather than
 * prose. <i>Is she listening, where is this phone, can the camera open, can she reach the
 * laptop</i> are four rows read from {@link ClaireState}, which the service writes as it
 * changes; the screen never infers a status from the last thing that happened to be
 * logged. The four states the owner asked to see — connected, listening, offline, error —
 * are a colour and a chip each, so a glance is enough and a paragraph is not needed.
 *
 * <p>The always-on modes are visible here too, not just in Settings: "Location every 60 s"
 * and "Camera ready (Claire may ask)" appear in the status card the moment they are true,
 * because an always-on sensor the owner cannot see the state of is a sensor the owner
 * cannot trust.
 *
 * <p>Nothing here blocks the main thread: every request goes through a single-thread
 * executor and comes back through the {@link Conversation} listener, which is already
 * main-thread-safe.
 */
public class MainActivity extends AppCompatActivity
        implements Conversation.Listener, ConversationAdapter.OnCopyListener,
        ClaireState.Listener {

    /** The action the listening notification's "Photo" button sends. */
    public static final String ACTION_PHOTO = "com.claire.companion.ui.PHOTO";

    private static final int REQUEST_PERMISSIONS = 41;

    private static final String[] WANTED = {
            Manifest.permission.RECORD_AUDIO,
            Manifest.permission.CAMERA,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.POST_NOTIFICATIONS,
    };

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    private ClairePrefs prefs;
    private Conversation conversation;
    private ConversationAdapter adapter;
    private RecyclerView list;
    private TextView empty;
    private TextView statusDot;
    private TextView statusLine;
    private TextView stateChip;
    private TextView stateMicrophone;
    private TextView stateLocation;
    private TextView stateCamera;
    private TextView stateLink;
    private TextView lastFix;
    private MaterialButton survivalHint;
    private EditText input;
    private MaterialButton pauseButton;
    private MaterialToolbar toolbar;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = new ClairePrefs(this);
        conversation = Conversation.get(this);
        adapter = new ConversationAdapter(this, this);

        toolbar = findViewById(R.id.toolbar);
        setSupportActionBar(toolbar);
        if (getSupportActionBar() != null) {
            getSupportActionBar().setDisplayShowTitleEnabled(false);
        }

        list = findViewById(R.id.conversation);
        list.setAdapter(adapter);
        empty = findViewById(R.id.empty);
        statusDot = findViewById(R.id.status_dot);
        statusLine = findViewById(R.id.status_line);
        stateChip = findViewById(R.id.state_chip);
        stateMicrophone = findViewById(R.id.state_microphone);
        stateLocation = findViewById(R.id.state_location);
        stateCamera = findViewById(R.id.state_camera);
        stateLink = findViewById(R.id.state_link);
        lastFix = findViewById(R.id.last_fix);
        survivalHint = findViewById(R.id.survival_hint);
        input = findViewById(R.id.input);
        pauseButton = findViewById(R.id.action_pause);

        toolbar.setSubtitle("v" + BuildConfig.VERSION_NAME + " · " + prefs.baseUrl());
        toolbar.setOnClickListener(view -> openSettings());

        survivalHint.setOnClickListener(view -> openSettings());

        findViewById(R.id.action_photo).setOnClickListener(view -> takePhoto());
        findViewById(R.id.action_location).setOnClickListener(view -> reportLocation());
        pauseButton.setOnClickListener(view -> togglePause());
        findViewById(R.id.send).setOnClickListener(view -> sendTyped());

        input.setOnEditorActionListener((view, actionId, event) -> {
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendTyped();
                return true;
            }
            return false;
        });

        // Permission-first, then the service: a microphone service started without the
        // microphone grant would only stop itself again.
        requestWantedPermissions();

        ClaireState.get().addListener(this);
        renderState();
        renderConversation();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent != null && ACTION_PHOTO.equals(intent.getAction())) {
            takePhoto();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        conversation.addListener(this);
        ClaireState.get().addListener(this);
        renderState();
        renderConversation();
    }

    @Override
    protected void onPause() {
        conversation.removeListener(this);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        // Worth a line in the diary: the platform destroying and recreating this
        // activity is exactly what "the app closed itself" looks like from outside.
        Diag.note("MainActivity destroyed");
        ClaireState.get().removeListener(this);
        worker.shutdownNow();
        super.onDestroy();
    }

    // -- the menu ---------------------------------------------------------- //

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.menu_settings) {
            openSettings();
            return true;
        }
        if (id == R.id.menu_check) {
            checkLink();
            return true;
        }
        if (id == R.id.menu_clear) {
            conversation.clear();
            renderConversation();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    // -- state ------------------------------------------------------------- //

    @Override
    public void onStateChanged() {
        renderState();
    }

    /**
     * Paint the status card from {@link ClaireState}.
     *
     * <p>Every branch here is a state, not a sentence: the colours are the same four the
     * conversation labels use, so the accent that means "working" on this card means the
     * same thing in the list.
     */
    private void renderState() {
        ClaireState seen = ClaireState.get();
        int ok = getColor(R.color.ok);
        int warn = getColor(R.color.warn);
        int bad = getColor(R.color.error);
        int idle = getColor(R.color.idle);

        String headline;
        int dot;
        String chip;
        int chipColour;
        switch (seen.mic()) {
            case LISTENING:
                headline = "Listening";
                dot = ok;
                chip = getString(R.string.state_listening);
                chipColour = ok;
                break;
            case PAUSED:
                headline = "Paused";
                dot = warn;
                chip = getString(R.string.state_paused);
                chipColour = warn;
                break;
            case DENIED:
                headline = "Microphone blocked";
                dot = bad;
                chip = getString(R.string.state_denied);
                chipColour = bad;
                break;
            default:
                headline = seen.link() == ClaireState.Link.UNREACHABLE
                        ? "Offline" : "Not listening";
                dot = seen.link() == ClaireState.Link.UNREACHABLE ? bad : idle;
                chip = getString(R.string.state_stopped);
                chipColour = idle;
                break;
        }
        // The link can be the loudest fact even while listening: an open microphone that
        // cannot reach the laptop is a phone that is quietly hoarding the owner's words.
        if (seen.link() == ClaireState.Link.TOKEN_REFUSED) {
            dot = bad;
            chip = getString(R.string.state_token_refused);
            chipColour = bad;
        } else if (seen.link() == ClaireState.Link.UNREACHABLE) {
            dot = bad;
            chip = getString(R.string.state_unreachable);
            chipColour = bad;
        } else if (seen.link() == ClaireState.Link.OK && seen.mic() == ClaireState.Mic.LISTENING) {
            chip = getString(R.string.state_connected);
            chipColour = ok;
        }

        statusLine.setText(headline);
        statusDot.setTextColor(dot);
        stateChip.setText(chip);
        stateChip.getBackground().setTint(chipColour);
        stateChip.setTextColor(getColor(R.color.bg));

        stateMicrophone.setText(micText(seen.mic()));
        stateMicrophone.setTextColor(seen.mic() == ClaireState.Mic.LISTENING ? ok
                : seen.mic() == ClaireState.Mic.DENIED ? bad : idle);

        stateLocation.setText(locationText(seen));
        stateLocation.setTextColor(seen.location() == ClaireState.Loc.ALWAYS ? ok
                : seen.location() == ClaireState.Loc.WHILE_USING ? warn
                : seen.location() == ClaireState.Loc.DENIED
                        || seen.location() == ClaireState.Loc.SERVICES_OFF ? bad : idle);

        stateCamera.setText(cameraText(seen.camera()));
        stateCamera.setTextColor(seen.camera() == ClaireState.Cam.ARMED ? ok
                : seen.camera() == ClaireState.Cam.CAPTURING ? warn
                : seen.camera() == ClaireState.Cam.DENIED ? bad : idle);

        stateLink.setText(linkText(seen));
        stateLink.setTextColor(seen.link() == ClaireState.Link.OK ? ok
                : seen.link() == ClaireState.Link.UNKNOWN ? idle : bad);

        if (seen.lastFix().isEmpty()) {
            lastFix.setVisibility(View.GONE);
        } else {
            lastFix.setVisibility(View.VISIBLE);
            // Freshness and accuracy, never the coordinates: the owner does not
            // need to be told where they are. See ClaireState.setLastFix.
            lastFix.setText("location sent " + since(seen.lastFixAt()) + "  " + seen.lastFix());
        }

        pauseButton.setText(seen.mic() == ClaireState.Mic.PAUSED
                ? R.string.action_resume : R.string.action_pause);

        // The survival hint is only worth screen space when it is actionable and the
        // owner has actually asked for always-on.
        boolean missing = prefs.wantsListening()
                && (!Survival.batteryExempt(this) || !Survival.overlayGranted(this));
        survivalHint.setVisibility(missing ? View.VISIBLE : View.GONE);
        if (missing) {
            survivalHint.setText(!Survival.overlayGranted(this)
                    ? "Tap to allow “Display over other apps” — without it Android will "
                      + "not let the service restart itself after a kill."
                    : "Tap to make the battery unrestricted — MIUI freezes listening "
                      + "after a while without it.");
        }
    }

    private String micText(ClaireState.Mic mic) {
        switch (mic) {
            case LISTENING:
                return getString(R.string.state_listening);
            case PAUSED:
                return getString(R.string.state_paused);
            case DENIED:
                return getString(R.string.state_denied);
            default:
                return getString(R.string.state_stopped);
        }
    }

    /** "just now" / "3 min ago": all the owner needs to know about a fix's age. */
    private String since(long at) {
        long seconds = Math.max(0, (System.currentTimeMillis() - at) / 1000);
        if (seconds < 45) {
            return "just now";
        }
        if (seconds < 3600) {
            return (seconds / 60) + " min ago";
        }
        return (seconds / 3600) + " h ago";
    }

    private String locationText(ClaireState seen) {
        switch (seen.location()) {
            case ALWAYS:
                return getString(R.string.state_on, seen.locationIntervalSeconds());
            case WHILE_USING:
                return getString(R.string.state_while_using);
            case DENIED:
                return getString(R.string.state_no_permission);
            case SERVICES_OFF:
                return "location services are off";
            default:
                return getString(R.string.state_off);
        }
    }

    private String cameraText(ClaireState.Cam camera) {
        switch (camera) {
            case ARMED:
                return getString(R.string.state_armed);
            case CAPTURING:
                return getString(R.string.state_capturing);
            case DENIED:
                return getString(R.string.state_no_permission);
            default:
                return getString(R.string.state_off);
        }
    }

    private String linkText(ClaireState seen) {
        switch (seen.link()) {
            case OK:
                return getString(R.string.state_connected);
            case TOKEN_REFUSED:
                return getString(R.string.state_token_refused);
            case UNREACHABLE:
                return getString(R.string.state_unreachable);
            default:
                return getString(R.string.state_unknown);
        }
    }

    // -- the conversation --------------------------------------------------- //

    @Override
    public void onConversationChanged() {
        renderConversation();
    }

    private void renderConversation() {
        adapter.submit(conversation.lines());
        boolean blank = adapter.size() == 0;
        empty.setVisibility(blank ? View.VISIBLE : View.GONE);
        list.setVisibility(blank ? View.GONE : View.VISIBLE);
        if (!blank) {
            list.scrollToPosition(adapter.size() - 1);
        }
    }

    /**
     * A long press on a bubble copied it.  Say so.
     *
     * <p>{@code text} is null when there was nothing to copy, which happens when a row is
     * an empty string; a snackbar that says "copied" for an empty clipboard would be a
     * lie the owner can check in one paste.
     */
    @Override
    public void onCopied(String text) {
        View root = findViewById(android.R.id.content);
        if (text == null || text.trim().isEmpty()) {
            Snackbar.make(root, R.string.toast_copy_failed, Snackbar.LENGTH_SHORT).show();
            return;
        }
        Snackbar.make(root, R.string.toast_copied, Snackbar.LENGTH_SHORT).show();
    }

    // -- the four hand actions --------------------------------------------- //

    private void takePhoto() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestWantedPermissions();
            Toast.makeText(this, R.string.camera_no_permission, Toast.LENGTH_LONG).show();
            return;
        }
        startActivity(new Intent(this, CameraActivity.class)
                .putExtra(CameraActivity.EXTRA_AUTO, prefs.autoSendPhoto()));
    }

    private void reportLocation() {
        if (!LocationReporter.hasPermission(this)) {
            requestWantedPermissions();
            return;
        }
        if (!LocationReporter.servicesEnabled(this)) {
            Snackbar.make(findViewById(android.R.id.content),
                    R.string.location_services_off, Snackbar.LENGTH_LONG)
                    .setAction(R.string.action_open_settings, view ->
                            startActivity(LocationReporter.locationSettingsIntent()))
                    .show();
            return;
        }
        // Through the service, so the position is reported even if the owner leaves the
        // screen, and so there is exactly one code path that talks to the bridge.
        ensureService(VoiceService.ACTION_REPORT_LOCATION);
    }

    private void togglePause() {
        if (VoiceService.isPaused()) {
            ensureService(VoiceService.ACTION_RESUME);
            return;
        }
        if (VoiceService.isListening()) {
            sendToService(VoiceService.ACTION_PAUSE);
            return;
        }
        ensureService(VoiceService.ACTION_START);
    }

    private void sendTyped() {
        String text = input.getText() == null ? "" : input.getText().toString().trim();
        if (text.isEmpty()) {
            return;
        }
        input.setText("");
        conversation.add(Conversation.Who.YOU, text);
        worker.execute(() -> {
            ClaireClient.Answer answer = ClaireClient.from(prefs).sendText(text);
            main.post(() -> {
                if (answer.ok()) {
                    ClaireState.get().setLink(ClaireState.Link.OK, prefs.baseUrl());
                } else if (answer.status == 401 || answer.status == 403) {
                    ClaireState.get().setLink(ClaireState.Link.TOKEN_REFUSED, "");
                    conversation.add(Conversation.Who.SYSTEM,
                            "the laptop refused the token; check Settings");
                } else if (!answer.reachedServer()) {
                    ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                            "unreachable (" + answer.transportError + ")");
                    conversation.add(Conversation.Who.SYSTEM,
                            "could not reach Claire: " + answer.transportError);
                } else {
                    ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                            "the bridge answered HTTP " + answer.status);
                    conversation.add(Conversation.Who.SYSTEM,
                            answer.describe("sending your message"));
                }
                renderState();
            });
        });
    }

    private void checkLink() {
        worker.execute(() -> {
            ClaireClient.Answer answer = ClaireClient.from(prefs).health();
            main.post(() -> {
                String message;
                if (answer.ok()) {
                    ClaireState.get().setLink(ClaireState.Link.OK, prefs.baseUrl());
                    message = "The bridge answered: " + prefs.baseUrl();
                } else if (answer.status == 401 || answer.status == 403) {
                    ClaireState.get().setLink(ClaireState.Link.TOKEN_REFUSED, "");
                    message = "The bridge is reachable but the token was refused.";
                } else if (!answer.reachedServer()) {
                    ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                            "unreachable (" + answer.transportError + ")");
                    message = "Claire could not be reached: " + answer.transportError;
                } else {
                    ClaireState.get().setLink(ClaireState.Link.UNREACHABLE,
                            "the bridge answered HTTP " + answer.status);
                    message = "The bridge answered HTTP " + answer.status;
                }
                conversation.add(Conversation.Who.PHONE, message);
                renderState();
            });
        });
    }

    // -- permissions and starting the service ------------------------------- //

    private void requestWantedPermissions() {
        java.util.List<String> missing = new java.util.ArrayList<>();
        for (String permission : WANTED) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
                    && Manifest.permission.POST_NOTIFICATIONS.equals(permission)) {
                continue;
            }
            if (checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                missing.add(permission);
            }
        }
        if (missing.isEmpty()) {
            startWhenWanted();
            return;
        }
        ActivityCompat.requestPermissions(this, missing.toArray(new String[0]),
                REQUEST_PERMISSIONS);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_PERMISSIONS) {
            return;
        }
        boolean microphone = checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
        if (!microphone) {
            conversation.add(Conversation.Who.SYSTEM,
                    "listening needs the microphone permission; the service will not start "
                            + "without it");
        }
        // A grant given while the service is already running widens what it may do — the
        // location and camera types, and the always-on modes behind them — so the service
        // is told to re-read its permissions rather than being left with the old mask.
        if (VoiceService.isRunning()) {
            sendToService(VoiceService.ACTION_START);
        }
        startWhenWanted();
        renderState();
    }

    /** Start listening after a grant, but only if the owner had already asked for it. */
    private void startWhenWanted() {
        if (!prefs.wantsListening() || VoiceService.isRunning()) {
            return;
        }
        ensureService(VoiceService.ACTION_START);
    }

    /** Start the service, and report the platform's refusal instead of hiding it. */
    private void ensureService(String action) {
        if (!VoiceService.isRunning()) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) {
                requestWantedPermissions();
                return;
            }
        }
        if (sendToService(action)) {
            return;
        }
        startWhenWanted();
    }

    private boolean sendToService(String action) {
        try {
            ContextCompat.startForegroundService(this,
                    new Intent(this, VoiceService.class).setAction(action));
            return true;
        } catch (RuntimeException refused) {
            conversation.add(Conversation.Who.SYSTEM,
                    "the phone would not start the listening service: "
                            + refused.getClass().getSimpleName());
            return false;
        }
    }

    private void openSettings() {
        startActivity(new Intent(this, SettingsActivity.class));
    }
}
