package com.claire.companion;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;

import com.google.android.material.appbar.MaterialToolbar;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.materialswitch.MaterialSwitch;

/**
 * Settings, in five groups.
 *
 * <p>The two new groups are the ones the owner actually asked for. <b>Always on</b> holds
 * the three switches that decide whether this app is a companion or a button: continuous
 * location, autonomous photos, and the watchdog that re-arms after a kill. <b>Keeping it
 * alive</b> holds the two MIUI exemptions, as rows rather than switches, because they are
 * facts about the phone that this app can only report and send the owner to change — a
 * switch that silently does nothing would be worse than the sentence.
 *
 * <p>The permission rows are deliberately rows too. This app <em>cannot</em> grant itself
 * {@code ACCESS_BACKGROUND_LOCATION} on Android 11 or later: the platform refuses the
 * dialog and has since API 30. So the row says whether it is granted and offers the one
 * screen that can change it, and the location interval says whether continuous mode will
 * be always-on or only while the app is on screen. The difference matters and it is
 * visible before anything is switched on.
 *
 * <p>Nothing here claims an exemption that has not been checked at the moment the screen
 * is drawn: every row re-reads the phone on {@link #onResume()}, because both exemption
 * screens are outside this app and the owner has to come back through here.
 */
public class SettingsActivity extends AppCompatActivity {

    private ClairePrefs prefs;

    private EditText host;
    private EditText port;
    private EditText token;
    private EditText question;

    private MaterialSwitch continuousLocation;
    private MaterialSwitch autoCapture;
    private MaterialSwitch watchdog;
    private MaterialSwitch speak;
    private MaterialSwitch boot;
    private MaterialSwitch lowBattery;
    private MaterialSwitch autoPhoto;

    private Spinner locationInterval;
    private Spinner capturePeriod;
    private Spinner sensitivity;
    /** Which engine speaks: Fish Audio from this phone, or the phone's own. */
    private Spinner ttsBackend;
    private EditText fishModel;
    private EditText fishToken;
    private EditText fishVoice;
    private TextView fishTokenState;
    private TextView voiceState;

    private LinearLayout permissionRows;
    private LinearLayout survivalRows;

    private Row microphoneRow;
    private Row cameraRow;
    private Row fineLocationRow;
    private Row backgroundLocationRow;
    private Row notificationsRow;

    private Row batteryRow;
    private Row overlayRow;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        prefs = new ClairePrefs(this);
        MaterialToolbar toolbar = findViewById(R.id.settings_toolbar);
        toolbar.setNavigationOnClickListener(view -> finish());

        host = findViewById(R.id.host);
        port = findViewById(R.id.port);
        token = findViewById(R.id.token);
        question = findViewById(R.id.question);

        continuousLocation = findViewById(R.id.continuous_location);
        autoCapture = findViewById(R.id.auto_capture);
        watchdog = findViewById(R.id.watchdog);
        speak = findViewById(R.id.speak);
        boot = findViewById(R.id.boot);
        lowBattery = findViewById(R.id.low_battery);
        autoPhoto = findViewById(R.id.auto_photo);

        locationInterval = findViewById(R.id.location_interval);
        capturePeriod = findViewById(R.id.capture_period);
        sensitivity = findViewById(R.id.sensitivity);

        ttsBackend = findViewById(R.id.tts_backend);
        fishModel = findViewById(R.id.fish_model);
        fishToken = findViewById(R.id.fish_token);
        fishVoice = findViewById(R.id.fish_voice);
        fishTokenState = findViewById(R.id.fish_token_state);
        voiceState = findViewById(R.id.voice_state);

        permissionRows = findViewById(R.id.permission_rows);
        survivalRows = findViewById(R.id.survival_rows);

        bindSpinner(locationInterval, R.array.location_interval_labels);
        bindSpinner(capturePeriod, R.array.capture_period_labels);
        bindSpinner(sensitivity, R.array.sensitivity_levels);
        bindSpinner(ttsBackend, R.array.tts_backends);

        buildPermissionRows();
        buildSurvivalRows();

        host.setText(prefs.host());
        port.setText(String.valueOf(prefs.port()));
        token.setText(prefs.token());
        question.setText(prefs.question());
        continuousLocation.setChecked(prefs.continuousLocation());
        autoCapture.setChecked(prefs.autoCapture());
        watchdog.setChecked(prefs.watchdog());
        speak.setChecked(prefs.speakReplies());
        boot.setChecked(prefs.listenOnBoot());
        lowBattery.setChecked(prefs.lowBatteryPause());
        autoPhoto.setChecked(prefs.autoSendPhoto());
        select(locationInterval, R.array.location_intervals, prefs.locationIntervalSeconds());
        select(capturePeriod, R.array.capture_periods, prefs.capturePeriodMinutes());
        select(sensitivity, prefs.sensitivity());

        fishModel.setText(prefs.fishModel());
        fishToken.setText(prefs.fishToken());
        fishVoice.setText(prefs.fishVoiceId());
        // The backend spinner is ordered backends[0] = Fish, backends[1] = device.
        ttsBackend.setSelection(ClairePrefs.TTS_DEVICE.equals(prefs.ttsBackend()) ? 1 : 0);
        showVoiceState();

        // The spinners only make sense while their switch is on; greying them out is the
        // honest way to show that the setting is kept but not in force.
        continuousLocation.setOnCheckedChangeListener((view, on) ->
                locationInterval.setEnabled(on));
        autoCapture.setOnCheckedChangeListener((view, on) ->
                capturePeriod.setEnabled(on));
        locationInterval.setEnabled(prefs.continuousLocation());
        capturePeriod.setEnabled(prefs.autoCapture());

        findViewById(R.id.save).setOnClickListener(view -> save());
        findViewById(R.id.test_voice).setOnClickListener(view -> testVoice());
        findViewById(R.id.clear).setOnClickListener(view -> {
            Conversation.get(this).clear();
            Toast.makeText(this, R.string.toast_cleared, Toast.LENGTH_SHORT).show();
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshPermissionRows();
        refreshSurvivalRows();
        showVoiceState();
    }

    // -- the rows ---------------------------------------------------------- //

    private void buildPermissionRows() {
        microphoneRow = addRow(permissionRows, "Microphone",
                "Required to listen at all. Without it the service stops itself rather "
                        + "than running mute.",
                R.string.action_grant, view -> ActivityCompat.requestPermissions(
                        this, new String[]{Manifest.permission.RECORD_AUDIO}, 61));

        cameraRow = addRow(permissionRows, "Camera",
                "Required for Claire to ask for a photo. Photos are never taken silently: "
                        + "each one posts a notification.",
                R.string.action_grant, view -> ActivityCompat.requestPermissions(
                        this, new String[]{Manifest.permission.CAMERA}, 62));

        fineLocationRow = addRow(permissionRows, "Location while in use",
                "A position is attached to messages and reported on request.",
                R.string.action_grant, view -> ActivityCompat.requestPermissions(
                        this, new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                                Manifest.permission.ACCESS_COARSE_LOCATION}, 63));

        backgroundLocationRow = addRow(permissionRows, "Location all the time",
                "Android 11 and later will not grant this from inside an app. The button "
                        + "opens the one screen that can: Permissions → Location → "
                        + "\u201CAllow all the time\u201D.",
                R.string.action_open_settings, view -> {
                    if (LocationReporter.hasFinePermission(this)) {
                        startActivity(LocationReporter.backgroundSettingsIntent(this));
                    } else {
                        Toast.makeText(this, R.string.location_grant_while_using_first,
                                Toast.LENGTH_LONG).show();
                        ActivityCompat.requestPermissions(this,
                                new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                                        Manifest.permission.ACCESS_COARSE_LOCATION}, 63);
                    }
                });

        notificationsRow = addRow(permissionRows, "Notifications",
                "The status line and the photo notices. A foreground service needs a "
                        + "notification to exist at all.",
                R.string.action_grant, view -> {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        ActivityCompat.requestPermissions(this,
                                new String[]{Manifest.permission.POST_NOTIFICATIONS}, 64);
                    } else {
                        openAppSettings();
                    }
                });
    }

    private void buildSurvivalRows() {
        batteryRow = addRow(survivalRows, "Unrestricted battery",
                "MIUI freezes background apps that are merely \u201Coptimised\u201D. This "
                        + "opens the system dialog that exempts Claire from that.",
                R.string.action_battery, view -> startActivity(Survival.batteryExemptIntent(this)));

        overlayRow = addRow(survivalRows, "Display over other apps",
                "Not a visual gimmick: permission to draw over other apps is what makes it "
                        + "legal for this service to start itself again after Android has "
                        + "killed it. Without it, the watchdog can only post a notification.",
                R.string.action_open_settings, view -> startActivity(Survival.overlayIntent(this)));
    }

    private void refreshPermissionRows() {
        microphoneRow.set(granted(Manifest.permission.RECORD_AUDIO),
                granted(Manifest.permission.RECORD_AUDIO)
                        ? "Granted" : "Missing — tap Grant and choose Allow");
        cameraRow.set(granted(Manifest.permission.CAMERA),
                granted(Manifest.permission.CAMERA)
                        ? "Granted" : "Missing — no photo can be taken");
        fineLocationRow.set(LocationReporter.hasFinePermission(this),
                LocationReporter.hasFinePermission(this)
                        ? (LocationReporter.servicesEnabled(this)
                                ? "Granted" : "Granted, but location services are off")
                        : "Missing — tap Grant and choose While using the app");
        boolean background = LocationReporter.hasBackground(this);
        backgroundLocationRow.set(background, background
                ? "Granted — location keeps moving with the screen off"
                : "Not granted — continuous location will only run while this app is on "
                  + "screen");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationsRow.set(granted(Manifest.permission.POST_NOTIFICATIONS),
                    granted(Manifest.permission.POST_NOTIFICATIONS)
                            ? "Granted" : "Missing — the status line will not be visible");
        } else {
            notificationsRow.set(true, "Not needed before Android 13");
        }
    }

    private void refreshSurvivalRows() {
        boolean exempt = Survival.batteryExempt(this);
        batteryRow.set(exempt, exempt
                ? "Exempt — the phone will not freeze Claire for being idle"
                : "Not exempt — MIUI may freeze listening after a while");
        boolean overlay = Survival.overlayGranted(this);
        overlayRow.set(overlay, overlay
                ? "Granted — an automatic restart after a kill is allowed"
                : "Not granted — an automatic restart will be refused; the watchdog can "
                  + "only notify");
    }

    private boolean granted(String permission) {
        return checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    private void openAppSettings() {
        startActivity(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                android.net.Uri.parse("package:" + getPackageName())));
    }

    // -- saving ------------------------------------------------------------ //

    private void save() {
        String rawHost = host.getText() == null ? "" : host.getText().toString().trim();
        if (rawHost.isEmpty()) {
            host.setError(getString(R.string.error_host_required));
            return;
        }
        int portValue = prefs.port();
        try {
            int parsed = Integer.parseInt(
                    port.getText() == null ? "" : port.getText().toString().trim());
            if (parsed > 0 && parsed < 65_536) {
                portValue = parsed;
            }
        } catch (NumberFormatException ignored) {
            // Keep the previous port rather than silently writing 0.
        }
        String tokenValue = token.getText() == null ? "" : token.getText().toString().trim();
        String questionValue =
                question.getText() == null ? "" : question.getText().toString().trim();

        prefs.setHost(rawHost);
        prefs.setPort(portValue);
        prefs.setToken(tokenValue);
        prefs.setQuestion(questionValue);

        prefs.setContinuousLocation(continuousLocation.isChecked());
        prefs.setLocationIntervalSeconds(
                intAt(R.array.location_intervals, locationInterval.getSelectedItemPosition(),
                        prefs.locationIntervalSeconds()));
        prefs.setAutoCapture(autoCapture.isChecked());
        prefs.setCapturePeriodMinutes(
                intAt(R.array.capture_periods, capturePeriod.getSelectedItemPosition(),
                        prefs.capturePeriodMinutes()));
        prefs.setWatchdog(watchdog.isChecked());

        prefs.setSpeakReplies(speak.isChecked());
        prefs.setListenOnBoot(boot.isChecked());
        prefs.setLowBatteryPause(lowBattery.isChecked());
        prefs.setAutoSendPhoto(autoPhoto.isChecked());
        prefs.setSensitivity(sensitivity.getSelectedItemPosition());

        // The voice: which engine, and the Fish credentials it needs. The key is
        // written to this app's private preferences and nowhere else.
        prefs.setTtsBackend(ttsBackend.getSelectedItemPosition() == 1
                ? ClairePrefs.TTS_DEVICE : ClairePrefs.TTS_FISH);
        prefs.setFishModel(fishModel.getText() == null
                ? "" : fishModel.getText().toString().trim());
        prefs.setFishToken(fishToken.getText() == null
                ? "" : fishToken.getText().toString().trim());
        prefs.setFishVoiceId(fishVoice.getText() == null
                ? "" : fishVoice.getText().toString().trim());

        if (prefs.watchdog()) {
            Survival.armWatchdog(this);
        } else {
            Survival.disarmWatchdog(this);
        }

        // The service reads its settings from ClairePrefs, so a running service has to be
        // told to re-read them: the same START action is what "apply" means here, and it
        // is also what widens the foreground type mask if a permission just arrived.
        if (VoiceService.isRunning()) {
            try {
                androidx.core.content.ContextCompat.startForegroundService(this,
                        new Intent(this, VoiceService.class)
                                .setAction(VoiceService.ACTION_START));
            } catch (RuntimeException refused) {
                Toast.makeText(this,
                        "Saved, but the phone would not restart the service: "
                                + refused.getClass().getSimpleName(), Toast.LENGTH_LONG).show();
                finish();
                return;
            }
        }
        Toast.makeText(this, R.string.toast_saved, Toast.LENGTH_SHORT).show();
        finish();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        refreshPermissionRows();
        refreshSurvivalRows();
        // A new permission may have made the camera or location type legal; say so to the
        // service now rather than at the next restart.
        if (VoiceService.isRunning()) {
            try {
                androidx.core.content.ContextCompat.startForegroundService(this,
                        new Intent(this, VoiceService.class)
                                .setAction(VoiceService.ACTION_START));
            } catch (RuntimeException ignored) {
                // The activity is on screen, so this is not expected; the notification
                // would report it if it happened.
            }
        }
    }

    // -- the voice --------------------------------------------------------- //

    /**
     * Say, on screen, which engine will actually speak and why.
     *
     * <p>The owner asked for the direct Fish call, so "chosen Fish but no key" is a
     * state that has to be visible rather than silently reinterpreted: otherwise the
     * app would quietly speak in a different voice than the one selected.
     */
    private void showVoiceState() {
        if (fishTokenState == null) {
            return;
        }
        // The hint is a masked fingerprint. The key itself is never rendered.
        fishTokenState.setText(prefs.fishTokenHint());
        boolean fishChosen = !ClairePrefs.TTS_DEVICE.equals(prefs.ttsBackend());
        String inUse;
        if (fishChosen && prefs.fishSpeaks()) {
            inUse = "In use: Fish Audio, called from this phone, model "
                    + prefs.fishModel() + ".";
            if (!prefs.fishVoiceId().isEmpty()) {
                inUse += " Voice " + prefs.fishVoiceId() + ".";
            }
        } else if (fishChosen) {
            inUse = "In use: the phone's own voice. Fish Audio is selected but no key"
                    + " is set, so replies fall back and the conversation says so.";
        } else {
            inUse = "In use: the phone's own voice.";
        }
        if (!prefs.speakReplies()) {
            inUse += " (Replies are switched off, so nothing is spoken at all.)";
        }
        String spoken = ClaireState.get().voice();
        if (!spoken.isEmpty()) {
            inUse += " Last spoke with: " + spoken + ".";
        }
        voiceState.setText(inUse);
    }

    /** Ask Fish for one short sentence and play it, so the owner can hear it work. */
    private void testVoice() {
        final String words = "This is Claire, speaking from your phone.";
        voiceState.setText(R.string.voice_testing);
        if (!prefs.fishSpeaks()) {
            voiceState.setText("Fish Audio is not in use: "
                    + (prefs.fishToken().isEmpty()
                        ? "no API key has been saved yet."
                        : "the phone voice is selected."));
            return;
        }
        final FishTts probe = new FishTts();
        probe.speak(prefs.fishToken(), prefs.fishModel(), prefs.fishVoiceId(), words,
                new FishTts.Listener() {
                    @Override
                    public void onStarted() {
                        runOnUiThread(() -> voiceState.setText(
                                "Streaming from Fish Audio now…"));
                    }

                    @Override
                    public void onDone() {
                        runOnUiThread(() -> {
                            voiceState.setText("Fish Audio spoke it: "
                                    + probe.status());
                            probe.shutdown();
                        });
                    }

                    @Override
                    public void onError(String message) {
                        runOnUiThread(() -> {
                            voiceState.setText("Fish Audio failed: " + message);
                            probe.shutdown();
                        });
                    }
                });
    }

    // -- small helpers ----------------------------------------------------- //

    private void bindSpinner(Spinner spinner, int labels) {
        ArrayAdapter<CharSequence> adapter = ArrayAdapter.createFromResource(this, labels,
                android.R.layout.simple_spinner_item);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinner.setAdapter(adapter);
    }

    private void select(Spinner spinner, int values, int value) {
        int[] options = getResources().getIntArray(values);
        for (int index = 0; index < options.length; index++) {
            if (options[index] == value) {
                spinner.setSelection(index);
                return;
            }
        }
    }

    private void select(Spinner spinner, int value) {
        if (value >= 0 && value < spinner.getCount()) {
            spinner.setSelection(value);
        }
    }

    private int intAt(int values, int index, int fallback) {
        int[] options = getResources().getIntArray(values);
        if (index < 0 || index >= options.length) {
            return fallback;
        }
        return options[index];
    }

    private interface Click {
        void onClick(View view);
    }

    private Row addRow(LinearLayout parent, String title, String hint, int actionLabel,
                       Click click) {
        View view = LayoutInflater.from(this)
                .inflate(R.layout.row_setting_action, parent, false);
        Row row = new Row(view);
        row.title.setText(title);
        row.hint.setText(hint);
        row.action.setText(actionLabel);
        row.action.setOnClickListener(click::onClick);
        parent.addView(view);
        return row;
    }

    /** One built row, kept so its state text and colour can be refreshed. */
    private static final class Row {
        final TextView title;
        final TextView hint;
        final MaterialButton action;

        Row(View view) {
            this.title = view.findViewById(R.id.row_title);
            this.hint = view.findViewById(R.id.row_state);
            this.action = view.findViewById(R.id.row_action);
        }

        void set(boolean ok, String state) {
            hint.setText(state);
            // Green when the phone allows it, amber when the owner has something left to
            // do.  The label stays as built ("Grant", "Open app settings", "Battery
            // settings") because it names the destination, which is more useful than a
            // generic verb.
            hint.setTextColor(ok ? 0xFF4CC9B0 : 0xFFE3B341);
        }
    }
}
