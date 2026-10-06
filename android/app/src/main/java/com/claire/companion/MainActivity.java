package com.claire.companion;

import android.Manifest;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.location.Location;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ListView;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The one screen: what was said, what the phone is doing, and the four things the owner
 * asks for by hand.
 *
 * <p>The design bet is that the app is usually <em>not</em> being looked at — the
 * service is — so this screen is built to answer three questions in a glance:
 * <i>is she listening</i> (the dot and the status line), <i>what did she just hear</i>
 * (the list) and <i>can she reach the laptop</i> (the link status). Everything the
 * owner can ask for by hand — a photo, a position, a typed sentence — is one tap away,
 * and every one of them is also in the listening notification, because the phone is
 * usually in a pocket with the screen off.
 *
 * <p>Nothing here blocks the main thread: every request goes through a single-thread
 * executor and comes back through the {@link Conversation} listener, which is already
 * main-thread-safe.
 */
public class MainActivity extends AppCompatActivity implements Conversation.Listener {

    /** The action the listening notification's "Location" button sends. */
    public static final String ACTION_REPORT_LOCATION =
            "com.claire.companion.REPORT_LOCATION";

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
    private ListView list;
    private TextView statusDot;
    private TextView statusLine;
    private TextView version;
    private View permissionBanner;
    private TextView permissionText;
    private EditText input;
    private Button listenButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = new ClairePrefs(this);
        conversation = Conversation.get(this);
        adapter = new ConversationAdapter(this);

        list = findViewById(R.id.conversation);
        list.setAdapter(adapter);
        statusDot = findViewById(R.id.status_dot);
        statusLine = findViewById(R.id.status_line);
        version = findViewById(R.id.version);
        permissionBanner = findViewById(R.id.permission_banner);
        permissionText = findViewById(R.id.permission_text);
        input = findViewById(R.id.input);
        listenButton = findViewById(R.id.button_listen);

        version.setText(versionName());

        listenButton.setOnClickListener(view -> toggleListening());
        findViewById(R.id.button_photo).setOnClickListener(view ->
                startActivity(new Intent(this, CameraActivity.class)));
        findViewById(R.id.button_location).setOnClickListener(view -> reportLocation());
        findViewById(R.id.button_settings).setOnClickListener(view ->
                startActivity(new Intent(this, SettingsActivity.class)));
        findViewById(R.id.button_send).setOnClickListener(view -> sendTyped());
        findViewById(R.id.button_check).setOnClickListener(view -> checkLink());
        findViewById(R.id.button_grant).setOnClickListener(view -> requestMissing());
        findViewById(R.id.button_app_settings).setOnClickListener(view -> openAppSettings());

        input.setOnEditorActionListener((view, actionId, event) -> {
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendTyped();
                return true;
            }
            return false;
        });

        if (prefs.host().isEmpty() || prefs.token().isEmpty()) {
            conversation.add(Conversation.Who.SYSTEM,
                    "no laptop is configured yet: open Settings and enter the address and "
                            + "the bridge token");
        }
        handleIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        if (intent != null && ACTION_REPORT_LOCATION.equals(intent.getAction())) {
            reportLocation();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        conversation.addListener(this);
        refresh();
        requestMissing();
        checkLink();
    }

    @Override
    protected void onPause() {
        conversation.removeListener(this);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        worker.shutdownNow();
        super.onDestroy();
    }

    @Override
    public void onConversationChanged() {
        refresh();
    }

    private void refresh() {
        adapter.submit(conversation.lines());
        list.setSelection(adapter.getCount() - 1);
        boolean listening = VoiceService.isListening();
        boolean paused = VoiceService.isPaused();
        statusDot.setTextColor(getColor(listening ? R.color.accent
                : paused ? R.color.warn : R.color.text_dim));
        listenButton.setText(listening ? R.string.action_pause
                : paused ? R.string.action_resume : R.string.action_listen);
    }

    // -- the four buttons ------------------------------------------------ //

    private void toggleListening() {
        if (VoiceService.isListening()) {
            startService(new Intent(this, VoiceService.class).setAction(VoiceService.ACTION_PAUSE));
            main.postDelayed(this::refresh, 200);
            return;
        }
        if (VoiceService.isPaused()) {
            startService(new Intent(this, VoiceService.class).setAction(VoiceService.ACTION_RESUME));
            main.postDelayed(this::refresh, 200);
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestMissing();
            conversation.add(Conversation.Who.SYSTEM,
                    "grant the microphone permission and tap Listen again");
            return;
        }
        startListeningNow();
    }

    private void startListeningNow() {
        Intent start = new Intent(this, VoiceService.class).setAction(VoiceService.ACTION_START);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(start);
            } else {
                startService(start);
            }
        } catch (RuntimeException refused) {
            conversation.add(Conversation.Who.SYSTEM,
                    "the phone refused to start the listening service: "
                            + refused.getClass().getSimpleName());
        }
        main.postDelayed(this::refresh, 400);
    }

    private void sendTyped() {
        String text = input.getText().toString().trim();
        if (text.isEmpty()) {
            return;
        }
        input.setText("");
        conversation.add(Conversation.Who.YOU, text);
        worker.execute(() -> {
            ClaireClient.Answer answer = ClaireClient.from(prefs).sendText(text);
            main.post(() -> {
                if (answer.ok()) {
                    conversation.add(Conversation.Who.PHONE, "sent to Claire");
                } else {
                    reportFailure("typing to Claire", answer);
                }
                refresh();
            });
        });
    }

    private void reportLocation() {
        if (!LocationReporter.hasPermission(this)) {
            conversation.add(Conversation.Who.SYSTEM,
                    getString(R.string.location_denied));
            requestMissing();
            return;
        }
        LocationReporter.requestCurrent(this, (outcome, location, detail) -> {
            switch (outcome) {
                case OK:
                    conversation.add(Conversation.Who.PHONE,
                            "position: " + LocationReporter.describe(location)
                                    + " (" + detail + ")");
                    sendLocation(location);
                    break;
                case SERVICES_OFF:
                    showLocationSettingsDialog(detail);
                    break;
                case PERMISSION_DENIED:
                    conversation.add(Conversation.Who.SYSTEM, getString(R.string.location_denied));
                    requestMissing();
                    break;
                default:
                    conversation.add(Conversation.Who.SYSTEM,
                            "no position could be obtained: " + detail);
            }
        });
    }

    private void sendLocation(Location location) {
        final double latitude = location.getLatitude();
        final double longitude = location.getLongitude();
        final Double accuracy = LocationReporter.accuracyOf(location);
        worker.execute(() -> {
            ClaireClient.Answer answer =
                    ClaireClient.from(prefs).sendLocation(latitude, longitude, accuracy, "phone");
            main.post(() -> {
                if (answer.ok()) {
                    conversation.add(Conversation.Who.PHONE,
                            "sent the position to Claire");
                } else {
                    reportFailure("sending the position", answer);
                }
            });
        });
    }

    private void showLocationSettingsDialog(String detail) {
        new AlertDialog.Builder(this)
                .setTitle(R.string.action_location)
                .setMessage(getString(R.string.location_services_off) + "\n\n" + detail)
                .setPositiveButton(android.R.string.ok, (dialog, which) ->
                        startActivity(LocationReporter.locationSettingsIntent()))
                .setNegativeButton(android.R.string.cancel, null)
                .show();
        conversation.add(Conversation.Who.SYSTEM, getString(R.string.location_services_off));
    }

    private void checkLink() {
        if (!prefs.configured()) {
            statusLine.setText("no laptop configured — open Settings");
            statusDot.setTextColor(getColor(R.color.warn));
            return;
        }
        statusLine.setText(prefs.baseUrl() + " · checking…");
        worker.execute(() -> {
            ClaireClient.Answer answer = ClaireClient.from(prefs).health();
            main.post(() -> showHealth(answer));
        });
    }

    private void showHealth(ClaireClient.Answer answer) {
        if (!answer.reachedServer()) {
            statusDot.setTextColor(getColor(R.color.bad));
            statusLine.setText(prefs.baseUrl() + " · " + getString(R.string.status_offline)
                    + " · " + answer.transportError);
            return;
        }
        if (answer.status == 401) {
            statusDot.setTextColor(getColor(R.color.bad));
            statusLine.setText("reachable, but the token was refused (HTTP 401)");
            return;
        }
        if (!answer.ok()) {
            statusDot.setTextColor(getColor(R.color.bad));
            statusLine.setText("reachable, but answered HTTP " + answer.status);
            return;
        }
        statusDot.setTextColor(getColor(VoiceService.isListening()
                ? R.color.accent : R.color.text_dim));
        String vision = ClaireClient.field(answer.body, "vision");
        String transcribe = ClaireClient.field(answer.body, "transcribe");
        String masked = ClaireClient.field(answer.body, "token");
        statusLine.setText(prefs.baseUrl()
                + " · reachable · stt " + (transcribe.isEmpty() ? "?" : transcribe)
                + " · vision " + (vision.isEmpty() ? "?" : vision)
                + (masked.isEmpty() ? "" : " · token " + masked));
    }

    private void reportFailure(String what, ClaireClient.Answer answer) {
        String detail = answer.describe(what);
        if (answer.status == 401) {
            detail = what + ": the laptop refused the token (HTTP 401). Check Settings.";
        } else if (answer.reachedServer() && answer.status == 503) {
            String message = ClaireClient.errorMessage(answer.body);
            String remedy = ClaireClient.errorRemedy(answer.body);
            detail = what + ": Claire is not ready on the laptop"
                    + (message.isEmpty() ? "" : " — " + message)
                    + (remedy.isEmpty() ? "" : "\n" + remedy);
        }
        conversation.add(Conversation.Who.SYSTEM, detail);
        Notifier.alert(this, getString(R.string.app_name), detail);
    }

    // -- permissions ----------------------------------------------------- //

    private List<String> missingPermissions() {
        List<String> missing = new ArrayList<>();
        for (String permission : WANTED) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU
                    && permission.equals(Manifest.permission.POST_NOTIFICATIONS)) {
                continue;
            }
            if (checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                missing.add(permission);
            }
        }
        return missing;
    }

    private void requestMissing() {
        List<String> missing = missingPermissions();
        boolean coreMissing = missing.contains(Manifest.permission.RECORD_AUDIO)
                || missing.contains(Manifest.permission.CAMERA)
                || missing.contains(Manifest.permission.ACCESS_FINE_LOCATION)
                || missing.contains(Manifest.permission.ACCESS_COARSE_LOCATION);
        permissionBanner.setVisibility(coreMissing ? View.VISIBLE : View.GONE);
        if (coreMissing) {
            permissionText.setText(getString(R.string.permission_banner) + "\n\n"
                    + "Still missing: " + String.join(", ", missing));
        }
        if (!missing.isEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toArray(new String[0]),
                    REQUEST_PERMISSIONS);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_PERMISSIONS) {
            return;
        }
        List<String> denied = new ArrayList<>();
        for (int i = 0; i < permissions.length && i < grantResults.length; i++) {
            if (grantResults[i] != PackageManager.PERMISSION_GRANTED) {
                denied.add(permissions[i]);
            }
        }
        if (!denied.isEmpty()) {
            conversation.add(Conversation.Who.SYSTEM,
                    "refused: " + String.join(", ", denied)
                            + " — the app settings screen can change this later");
        }
        refresh();
    }

    private void openAppSettings() {
        Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.fromParts("package", getPackageName(), null));
        startActivity(intent);
    }

    private String versionName() {
        try {
            return "v" + getPackageManager()
                    .getPackageInfo(getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException missing) {
            return "";
        }
    }
}
