package com.claire.companion;

import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.Spinner;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

/**
 * Five fields and four switches, on one screen.
 *
 * <p>What is <em>not</em> here is as deliberate as what is. There is no "log in", no
 * account, no sync, no server list: Claire runs on one laptop that the owner already
 * owns, and the whole configuration is an address, a port and one token. A settings
 * screen that grows past a screenful is a settings screen nobody reads, and every one
 * of these has a visible consequence:
 *
 * <ul>
 *   <li><b>Laptop / port</b> — the tailnet address and the bridge's port. Host and port
 *       are separate fields because the port is the thing most likely to change (see
 *       the port note in the bridge docs) and making the owner retype an IP to change
 *       it is a trap.
 *   <li><b>Token</b> — shown once, masked afterwards. It is stored in this app's own
 *       SharedPreferences, which is readable by root and by nobody else on a
 *       non-rooted phone, and is <b>not</b> encrypted: {@code EncryptedSharedPreferences}
 *       would need the security-crypto AAR and a keystore round trip, for a credential
 *       that only opens a microphone and a camera on a private tailnet. The APK is
 *       marked {@code allowBackup="false"} so it is never swept into a cloud backup.
 *   <li><b>Photo question</b> — the words sent with every photo. It is a setting
 *       rather than a fixed string because "what am I looking at?" and "read this to
 *       me" are both things the owner wants, at different times.
 *   <li><b>Speak replies</b> — off by default: the phone can already show a reply, and
 *       an assistant that starts talking in a meeting is worse than one that waits.
 *   <li><b>Offer to listen after a restart</b> — off by default, and it only posts a
 *       notification. See {@link BootReceiver} for why it cannot start the service.
 *   <li><b>Hold audio back below 15%</b> — on by default. Listening is cheap; radio and
 *       recognition are not, and a flat phone cannot listen at all.
 * </ul>
 */
public class SettingsActivity extends AppCompatActivity {

    private ClairePrefs prefs;
    private EditText host;
    private EditText port;
    private EditText token;
    private EditText question;
    private CheckBox autoPhoto;
    private CheckBox speak;
    private CheckBox boot;
    private CheckBox lowBattery;
    private Spinner sensitivity;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        prefs = new ClairePrefs(this);
        host = findViewById(R.id.host);
        port = findViewById(R.id.port);
        token = findViewById(R.id.token);
        question = findViewById(R.id.question);
        autoPhoto = findViewById(R.id.auto_photo);
        speak = findViewById(R.id.speak);
        boot = findViewById(R.id.boot);
        lowBattery = findViewById(R.id.low_battery);
        sensitivity = findViewById(R.id.sensitivity);

        host.setText(prefs.host());
        port.setText(String.valueOf(prefs.port()));
        question.setText(prefs.question());
        autoPhoto.setChecked(prefs.autoSendPhoto());
        speak.setChecked(prefs.speakReplies());
        boot.setChecked(prefs.listenOnBoot());
        lowBattery.setChecked(prefs.lowBatteryPause());

        TextView current = findViewById(R.id.token_current);
        current.setText(prefs.configured()
                ? "Saved token: " + prefs.maskedToken() + " — type a new one to replace it"
                : "No token saved yet");

        ArrayAdapter<CharSequence> levels = ArrayAdapter.createFromResource(this,
                R.array.sensitivity_levels, android.R.layout.simple_spinner_item);
        levels.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        sensitivity.setAdapter(levels);
        sensitivity.setSelection(Math.max(0, Math.min(2, prefs.sensitivity())));

        findViewById(R.id.save).setOnClickListener(view -> save());
        findViewById(R.id.battery_settings).setOnClickListener(view -> openBatterySettings());
        findViewById(R.id.clear).setOnClickListener(view -> {
            Conversation.get(this).clear();
            finish();
        });
    }

    private void save() {
        String normalised = ClairePrefs.normaliseHost(host.getText().toString(),
                ClairePrefs.DEFAULT_HOST);
        prefs.setHost(normalised);
        prefs.setPort(parsePort(port.getText().toString()));
        String typed = token.getText().toString().trim();
        if (!typed.isEmpty()) {
            // Only when something was typed: an empty field means "leave the token
            // alone", not "forget the token".  Forgetting it is a separate act.
            prefs.setToken(typed);
        }
        prefs.setQuestion(question.getText().toString());
        prefs.setAutoSendPhoto(autoPhoto.isChecked());
        prefs.setSpeakReplies(speak.isChecked());
        prefs.setListenOnBoot(boot.isChecked());
        prefs.setLowBatteryPause(lowBattery.isChecked());
        prefs.setSensitivity(sensitivity.getSelectedItemPosition());
        Conversation.get(this).add(Conversation.Who.SYSTEM,
                "settings saved: " + prefs.baseUrl()
                        + (prefs.configured() ? "" : " (no token yet)"));
        finish();
    }

    private static int parsePort(String raw) {
        try {
            int value = Integer.parseInt(raw.trim());
            if (value > 0 && value < 65_536) {
                return value;
            }
        } catch (NumberFormatException ignored) {
            // Fall through to the default.
        }
        return ClairePrefs.DEFAULT_PORT;
    }

    /**
     * The two screens that actually decide whether a listening service survives.
     *
     * <p>Android's own battery optimiser, and on MIUI the separate Autostart list.
     * Neither can be granted from inside the app — the platform forbids it — so the
     * only honest thing to do is offer the door.
     */
    private void openBatterySettings() {
        try {
            Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.fromParts("package", getPackageName(), null));
            startActivity(intent);
            return;
        } catch (Exception unavailable) {
            // ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS is absent outside AOSP-likes.
        }
        try {
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        } catch (Exception unavailable) {
            Intent fallback = new Intent();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                fallback.setAction(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
            } else {
                fallback.setAction(Settings.ACTION_SETTINGS);
            }
            fallback.setData(Uri.fromParts("package", getPackageName(), null));
            startActivity(fallback);
        }
    }
}
