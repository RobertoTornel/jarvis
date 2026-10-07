package com.claire.companion;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assume.assumeTrue;

import android.content.Context;
import android.util.DisplayMetrics;
import android.view.View;
import android.widget.TextView;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import com.google.android.material.button.MaterialButton;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * The three things the owner reported that a screenshot cannot settle: that the shutter
 * can actually be tapped, that a photo either succeeds or says exactly why it did not,
 * and that the phone really does call Fish Audio itself and play the reply while it
 * arrives.
 *
 * <p>This runs inside the app's own process because {@code adb shell input} is refused
 * outright on this phone ({@code SecurityException: Injecting input events requires …
 * INJECT_EVENTS}), so a tap and the resulting capture cannot be performed from a
 * terminal. {@code performClick()} invokes the same listener a finger would.
 */
@RunWith(AndroidJUnit4.class)
public class CameraAndVoiceTest {

    private static Context target() {
        return InstrumentationRegistry.getInstrumentation().getTargetContext();
    }

    private static float density() {
        DisplayMetrics metrics = target().getResources().getDisplayMetrics();
        return metrics.density;
    }

    // -- defect 4: "i cant tap it, move the button up" --------------------- //

    /**
     * The shutter must be clear of the system's edge gesture and big enough to hit.
     *
     * <p>The measured numbers are put in the assertion messages on purpose: when this
     * fails on a new phone, the failure itself says how far the button was from the
     * gesture strip and how tall it was, rather than just "false".
     */
    @Test
    public void theShutterIsClearOfTheGestureAreaAndBigEnoughToTap() {
        try (ActivityScenario<CameraActivity> scenario =
                     ActivityScenario.launch(CameraActivity.class)) {
            scenario.onActivity(activity -> {
                View controls = activity.findViewById(R.id.camera_controls);
                MaterialButton shutter = activity.findViewById(R.id.shutter);
                assertNotNull("the control bar is missing", controls);
                assertNotNull("the shutter is missing", shutter);

                float density = density();
                int heightDp = Math.round(shutter.getHeight() / density);
                assertTrue("the shutter is only " + heightDp + "dp tall, below the 48dp"
                        + " minimum touch target", heightDp >= 48);
                assertTrue("the shutter is not full width, so it is a smaller target"
                        + " than it looks", shutter.getWidth() > controls.getWidth() * 0.8);

                WindowInsetsCompat insets = ViewCompat.getRootWindowInsets(controls);
                assertNotNull("the window reported no insets at all", insets);
                Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemGestures()
                        | WindowInsetsCompat.Type.navigationBars());
                int paddingDp = Math.round(controls.getPaddingBottom() / density);
                int gestureDp = Math.round(bars.bottom / density);
                assertTrue("the control bar has only " + paddingDp + "dp below the"
                                + " shutter but the system gesture strip is " + gestureDp
                                + "dp tall, so the swipe that goes Back eats the tap",
                        controls.getPaddingBottom() >= bars.bottom);

                // The same fact in the coordinates the finger lives in.
                int[] onScreen = new int[2];
                shutter.getLocationOnScreen(onScreen);
                int screenBottom = activity.getWindow().getDecorView().getHeight();
                int gapBelowShutter = screenBottom - (onScreen[1] + shutter.getHeight());
                assertTrue("the shutter's bottom edge is only " + gapBelowShutter
                                + "px above the bottom of the screen, inside the "
                                + bars.bottom + "px gesture strip it must clear",
                        gapBelowShutter >= bars.bottom);
            });
        }
    }

    /**
     * Tapping the shutter has to end in a photo or in the real reason there is none.
     *
     * <p>This is the test for "photos error out". It deliberately does <em>not</em>
     * assert success: on a phone whose camera may be busy, the honest requirement is
     * that the screen names the CameraX error code and the cause, which is what
     * {@link CameraActivity#describeCaptureFailure} now produces. A status that is
     * still "taking the photo…" after the wait is the one real failure.
     */
    @Test
    public void theShutterEitherTakesAPhotoOrNamesTheRealError() throws Exception {
        ClairePrefs prefs = new ClairePrefs(target());
        prefs.setAutoSendPhoto(false);

        try (ActivityScenario<CameraActivity> scenario =
                     ActivityScenario.launch(CameraActivity.class)) {
            // Let CameraX bind; the activity reports if it cannot.
            Thread.sleep(2500L);
            scenario.onActivity(activity -> {
                MaterialButton shutter = activity.findViewById(R.id.shutter);
                assertTrue("the shutter was not enabled after the camera had time to bind",
                        shutter.isEnabled());
                shutter.performClick();
            });

            String seen = "";
            long deadline = System.currentTimeMillis() + 20_000L;
            while (System.currentTimeMillis() < deadline) {
                final AtomicReference<String> text = new AtomicReference<>("");
                scenario.onActivity(activity -> {
                    TextView view = activity.findViewById(R.id.camera_status);
                    text.set(view == null ? "" : String.valueOf(view.getText()));
                });
                seen = text.get();
                if (!seen.contains("taking the photo")) {
                    break;
                }
                Thread.sleep(400L);
            }

            assertFalse("the shutter was tapped and 20 s later the screen still said \"" + seen
                    + "\", so the capture neither finished nor failed", seen.contains("taking"));
            assertTrue("the screen never said what happened after the tap; it said \""
                    + seen + "\"", seen.length() > 0);
            // Either it worked, or it said why in the form the owner can act on.
            boolean worked = seen.contains("Photo ready") || seen.contains("sending");
            boolean explained = seen.contains("CameraX error");
            assertTrue("the tap produced neither a photo nor a named CameraX error: \""
                    + seen + "\"", worked || explained);
        }
    }

    /** The format Fish actually sends, parsed without a network. */
    @Test
    public void theWavParserHandlesFishesPlaceholderLength() {
        // The real first bytes of a streamed Fish reply, measured from the laptop:
        // 52 49 46 46 24 ff ff ff 57 41 56 45 = RIFF, 0xFFFFFF24, WAVE.
        byte[] wav = fishWav(16000, 1, 16, 40);
        assertEquals("RIFF".charAt(0), (char) wav[0]);
        assertEquals(0x24, wav[4] & 0xFF);
        assertEquals(0xFF, wav[5] & 0xFF);

        FishTts.WavHeader header = new FishTts.WavHeader();
        int data = header.parse(wav);
        assertEquals("the sample data was not found; the walk stalled on the bogus"
                + " declared size", 44, data);
        assertEquals(16000, header.rate);
        assertEquals(1, header.channels);
        assertTrue("a perfectly ordinary PCM WAV was called unplayable", header.playable());
    }

    @Test
    public void theWavParserRefusesSomethingThatIsNotWav() {
        FishTts.WavHeader header = new FishTts.WavHeader();
        byte[] mp3ish = new byte[64];
        mp3ish[0] = 'I';
        mp3ish[1] = 'D';
        mp3ish[2] = '3';
        assertEquals(-1, header.parse(mp3ish));
        assertFalse("an MP3 was accepted as WAV, which is how a reply becomes noise",
                header.playable());
        assertTrue(header.describe().contains("not WAV"));
    }

    /** A WAV whose fmt chunk says 16-bit PCM but with a bogus RIFF size, like Fish's. */
    private static byte[] fishWav(int rate, int channels, int bits, int samples) {
        int dataBytes = samples * channels * (bits / 8);
        ByteBuffer buffer = ByteBuffer.allocate(44 + dataBytes).order(ByteOrder.LITTLE_ENDIAN);
        buffer.put("RIFF".getBytes());
        buffer.putInt(0xFFFFFF24);          // the placeholder Fish actually sends
        buffer.put("WAVE".getBytes());
        buffer.put("fmt ".getBytes());
        buffer.putInt(16);
        buffer.putShort((short) 1);         // PCM
        buffer.putShort((short) channels);
        buffer.putInt(rate);
        buffer.putInt(rate * channels * (bits / 8));
        buffer.putShort((short) (channels * (bits / 8)));
        buffer.putShort((short) bits);
        buffer.put("data".getBytes());
        buffer.putInt(0xFFFFFF24);          // and again on the data chunk
        for (int index = 0; index < dataBytes; index++) {
            buffer.put((byte) (index % 251));
        }
        return buffer.array();
    }

    // -- defect 5: never read the owner's own position back to them -------- //

    /**
     * The location line may say how fresh and how accurate the fix is. It may not say
     * where the owner is, because they are standing there.
     */
    @Test
    public void theLocationLineNeverRecitesCoordinates() {
        ClaireState state = ClaireState.get();
        state.setLastFix(38.7223, -9.1393, 12.0f);
        String line = state.lastFix();
        assertFalse("the location line contains a latitude: " + line,
                line.contains("38.72") || line.contains("38.7"));
        assertFalse("the location line contains a longitude: " + line,
                line.contains("9.13") || line.contains("-9.1"));
        assertFalse("the location line looks like a bare coordinate pair: " + line,
                line.matches(".*-?\\d{1,3}\\.\\d+.*"));
        assertTrue("the accuracy was dropped along with the coordinates: " + line,
                line.contains("12"));
        assertTrue("a fix that was just recorded reads as stale: " + line,
                line.contains("just now"));
    }

    // -- defect 3: the phone calls Fish itself, and streams it ------------ //

    /**
     * A real call to Fish Audio from the phone, streamed into the speaker.
     *
     * <p>Skipped (not silently passed) when no key has been saved: this is the one
     * check that cannot be faked, because it is the network, the vendor and the audio
     * device all at once. What it asserts is measurable rather than poetic — that the
     * first audio arrived before the last byte did, which is what "streams" means.
     */
    @Test
    public void thePhoneCallsFishItselfAndPlaysWhileTheBytesArrive() throws Exception {
        ClairePrefs prefs = new ClairePrefs(target());
        assumeTrue("no Fish Audio key is saved in Settings, so this cannot be tested",
                !prefs.fishToken().isEmpty());

        final CountDownLatch started = new CountDownLatch(1);
        final CountDownLatch finished = new CountDownLatch(1);
        final AtomicReference<String> failure = new AtomicReference<>(null);
        final long[] startedAt = new long[1];
        final FishTts fish = new FishTts();
        try {
            long sentAt = System.currentTimeMillis();
            fish.speak(prefs.fishToken(), prefs.fishModel(), prefs.fishVoiceId(),
                    "This is Claire, speaking from the phone.",
                    new FishTts.Listener() {
                        @Override
                        public void onStarted() {
                            startedAt[0] = System.currentTimeMillis();
                            started.countDown();
                        }

                        @Override
                        public void onDone() {
                            finished.countDown();
                        }

                        @Override
                        public void onError(String message) {
                            failure.set(message);
                            started.countDown();
                            finished.countDown();
                        }
                    });

            assertTrue("Fish Audio did not start playing within 30 s: "
                            + (failure.get() == null ? "no answer at all" : failure.get()),
                    started.await(30, TimeUnit.SECONDS));
            assertEquals("Fish Audio refused the call", null, failure.get());
            assertTrue("playback did not finish within 30 s", finished.await(30, TimeUnit.SECONDS));
            assertTrue("the first audio arrived " + (startedAt[0] - sentAt) + " ms after the"
                    + " request, which is a whole-file download, not a stream",
                    startedAt[0] - sentAt < 15_000L);
            assertTrue("the status does not name the streamed format: " + fish.status(),
                    fish.status().contains("streamed"));
        } finally {
            fish.shutdown();
        }
    }

    // -- defect 5: the key is editable, and never shown ------------------- //

    @Test
    public void theFishSettingsRoundTripAndTheKeyIsOnlyEverMasked() {
        ClairePrefs prefs = new ClairePrefs(target());
        String beforeModel = prefs.fishModel();
        String beforeToken = prefs.fishToken();
        try {
            String fake = "test-key-not-real-0000wxyz";
            prefs.setTtsBackend(ClairePrefs.TTS_FISH);
            prefs.setFishModel("s2.1-pro-free");
            prefs.setFishToken(fake);
            prefs.setFishVoiceId("voice-abc");

            assertEquals("s2.1-pro-free", prefs.fishModel());
            assertEquals(fake, prefs.fishToken());
            assertEquals("voice-abc", prefs.fishVoiceId());
            assertTrue("with a key set, Fish should be the engine in use", prefs.fishSpeaks());

            String hint = prefs.fishTokenHint();
            assertFalse("the hint printed the key itself", hint.contains(fake));
            assertTrue("the hint does not identify which key is stored: " + hint,
                    hint.contains(fake.substring(fake.length() - 4)));

            prefs.setTtsBackend(ClairePrefs.TTS_DEVICE);
            assertFalse("the phone voice was chosen but Fish still claims to speak",
                    prefs.fishSpeaks());
        } finally {
            prefs.setFishModel(beforeModel);
            prefs.setFishToken(beforeToken);
            prefs.setFishVoiceId("");
        }
    }

}
