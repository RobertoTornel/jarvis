import com.claire.companion.AudioSegmenter;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Runs the voice-activity detector on a laptop against synthetic audio.
 *
 * <p>Claire's phone has no log to read, so "does it send one utterance per sentence,
 * or forty" is a question that has to be answered somewhere. It is answered here, with
 * a signal whose shape is known exactly:
 *
 * <pre>
 *   javac -d /tmp/claire-wire \
 *       android/app/src/main/java/com/claire/companion/{AudioSegmenter,ClaireWire}.java \
 *       android/tools/SegmenterSelfTest.java
 *   java -cp /tmp/claire-wire SegmenterSelfTest /tmp/claire-wire
 * </pre>
 */
public final class SegmenterSelfTest {

    private static int failures = 0;
    private static final int RATE = 16_000;

    public static void main(String[] args) throws Exception {
        twoSentencesBecomeTwoSegments();
        aQuietRoomIsSilent();
        aSteadyFanIsNotSpeech();
        aDoorSlamIsNotASentence();
        aMonologueIsCutAtTheCeiling();
        absolutelyNothingIsSentForPureSilence();

        System.out.println(failures == 0
                ? "SegmenterSelfTest: all checks passed"
                : "SegmenterSelfTest: " + failures + " CHECK(S) FAILED");
        if (failures != 0) {
            System.exit(1);
        }
    }

    private static void twoSentencesBecomeTwoSegments() {
        // 0.5 s quiet, 0.7 s speech, 1.5 s quiet, 1.0 s speech, 1.0 s quiet.
        short[] audio = new short[(int) (4.7 * RATE)];
        fillNoise(audio, 0, (int) (0.5 * RATE), 60, 11);
        fillSpeech(audio, (int) (0.5 * RATE), (int) (0.7 * RATE), 6_000, 12);
        fillNoise(audio, (int) (1.2 * RATE), (int) (1.5 * RATE), 60, 13);
        fillSpeech(audio, (int) (2.7 * RATE), (int) (1.0 * RATE), 6_000, 14);
        fillNoise(audio, (int) (3.7 * RATE), (int) (1.0 * RATE), 60, 15);

        List<short[]> segments = run(audio);
        check("two sentences produce exactly two segments, got " + segments.size(),
                segments.size() == 2);
        if (segments.size() == 2) {
            double first = seconds(segments.get(0));
            double second = seconds(segments.get(1));
            // Each segment is pre-roll (0.3 s of quiet) + the sentence + the hangover
            // (0.7 s of quiet), so 1.7 s and 2.0 s are the expected lengths and the
            // tolerance is generous enough not to encode a rounding accident.
            check("first segment is 1.55-1.85 s, was " + round(first),
                    first >= 1.55 && first <= 1.85);
            check("second segment is 1.85-2.15 s, was " + round(second),
                    second >= 1.85 && second <= 2.15);
        }
    }

    private static void aQuietRoomIsSilent() {
        short[] audio = new short[RATE * 3];
        fillNoise(audio, 0, audio.length, 40, 21);
        List<short[]> segments = run(audio);
        check("a quiet room sends nothing, got " + segments.size(), segments.isEmpty());
    }

    private static void aSteadyFanIsNotSpeech() {
        // A fan is loud but constant. Two amplitudes, because the interesting failure
        // is the one that starts *above* the initial threshold (an absolute floor of
        // 200 against a multiplier of 3 means anything above rms ≈ 600 would fire a
        // detector with no warm-up) and is only survived because listening begins by
        // learning the room.
        for (int amplitude : new int[] {900, 1_500, 3_000}) {
            short[] audio = new short[RATE * 6];
            fillNoise(audio, 0, audio.length, amplitude, 31);
            List<short[]> segments = run(audio);
            check("a steady fan at amplitude " + amplitude + " sends nothing, got "
                    + segments.size(), segments.isEmpty());
        }
    }

    private static void aDoorSlamIsNotASentence() {
        // One very short, very loud burst: energy alone would fire; minSpeechMs and
        // the hangover must not let it become an utterance.
        short[] audio = new short[RATE * 3];
        fillNoise(audio, 0, audio.length, 50, 41);
        fillSpeech(audio, (int) (1.0 * RATE), (int) (0.06 * RATE), 20_000, 42);
        List<short[]> segments = run(audio);
        check("a 60 ms door slam sends nothing, got " + segments.size(), segments.isEmpty());
    }

    private static void aMonologueIsCutAtTheCeiling() {
        // Loud from well before the warm-up ends, and then never stops: this is the
        // "somebody left the room with the television on" case, and the hard ceiling
        // is what keeps the buffer (and the uploads) bounded.
        short[] audio = new short[(int) (40.6 * RATE)];
        fillNoise(audio, 0, (int) (0.6 * RATE), 60, 52);
        fillSpeech(audio, (int) (0.6 * RATE), (int) (40.0 * RATE), 7_000, 51);
        List<short[]> segments = run(audio);
        check("40 s of speech is cut into pieces, got " + segments.size(),
                segments.size() >= 2);
        boolean withinCeiling = true;
        double largest = 0;
        for (short[] segment : segments) {
            double seconds = seconds(segment);
            largest = Math.max(largest, seconds);
            if (seconds > 15.0 + 0.05) {
                withinCeiling = false;
            }
        }
        check("no piece exceeds the 15 s ceiling, largest was " + round(largest), withinCeiling);
    }

    private static void absolutelyNothingIsSentForPureSilence() {
        List<short[]> segments = run(new short[RATE * 2]);
        check("digital silence sends nothing, got " + segments.size(), segments.isEmpty());
    }

    private static List<short[]> run(short[] audio) {
        final List<short[]> collected = new ArrayList<>();
        AudioSegmenter segmenter = new AudioSegmenter(RATE, null,
                (pcm, length, sampleRate) -> {
                    short[] copy = new short[length];
                    System.arraycopy(pcm, 0, copy, 0, length);
                    collected.add(copy);
                });
        // Feed in 100 ms chunks, the way AudioRecord.read() delivers them.
        int chunk = RATE / 10;
        for (int offset = 0; offset < audio.length; offset += chunk) {
            int length = Math.min(chunk, audio.length - offset);
            segmenter.accept(audio, offset, length);
        }
        segmenter.flush();
        return collected;
    }

    private static void fillNoise(short[] audio, int offset, int length, int amplitude, long seed) {
        Random random = new Random(seed);
        for (int i = 0; i < length && offset + i < audio.length; i++) {
            audio[offset + i] = (short) random.nextInt(2 * amplitude + 1 - amplitude);
        }
    }

    /** Amplitude-modulated noise: broadband like a voice, and clearly above the floor. */
    private static void fillSpeech(short[] audio, int offset, int length, int amplitude, long seed) {
        Random random = new Random(seed);
        for (int i = 0; i < length && offset + i < audio.length; i++) {
            double envelope = 0.7 + 0.3 * Math.sin(2 * Math.PI * 4.0 * i / RATE);
            audio[offset + i] = (short) (random.nextInt(2 * amplitude + 1 - amplitude) * envelope);
        }
    }

    private static double seconds(short[] pcm) {
        return pcm.length / (double) RATE;
    }

    private static double round(double value) {
        return Math.round(value * 100.0) / 100.0;
    }

    private static void check(String what, boolean ok) {
        if (!ok) {
            failures++;
        }
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
    }
}
