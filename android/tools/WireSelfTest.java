import com.claire.companion.ClaireWire;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;

/**
 * Runs {@link ClaireWire} on a laptop, with no Android and no emulator.
 *
 * <p>This exists because the phone is the one part of Claire that nothing can
 * currently debug: a byte-exact WAV header that is merely close produces a confusing
 * 400 on a phone screen, so the encoder is executed here and its output is handed to
 * Python's own {@code wave} module — the same parser family the bridge uses — and to
 * {@code json}. What this prints, and whether it exits 0, is the evidence.
 *
 * <pre>
 *   javac -d /tmp/claire-wire \
 *       android/app/src/main/java/com/claire/companion/ClaireWire.java \
 *       android/tools/WireSelfTest.java
 *   java -cp /tmp/claire-wire WireSelfTest /tmp/claire-wire
 * </pre>
 */
public final class WireSelfTest {

    private static int failures = 0;

    public static void main(String[] args) throws Exception {
        Path out = Path.of(args.length > 0 ? args[0] : "/tmp/claire-wire");
        Files.createDirectories(out);

        // A deterministic "utterance": a 440 Hz tone at a third of full scale.
        int rate = ClaireWire.SAMPLE_RATE;
        int seconds = 2;
        short[] pcm = new short[rate * seconds];
        for (int i = 0; i < pcm.length; i++) {
            pcm[i] = (short) (Math.sin(2 * Math.PI * 440.0 * i / rate) * 10_000);
        }

        byte[] wav = ClaireWire.wav(pcm, pcm.length, rate, 1);
        check("WAV is 44 + 2*N bytes", wav.length == 44 + pcm.length * 2);
        check("RIFF magic", ascii(wav, 0, 4).equals("RIFF"));
        check("WAVE magic", ascii(wav, 8, 4).equals("WAVE"));
        check("fmt chunk", ascii(wav, 12, 4).equals("fmt "));
        check("fmt size 16", intLe(wav, 16) == 16);
        check("PCM format 1", shortLe(wav, 20) == 1);
        check("mono", shortLe(wav, 22) == 1);
        check("sample rate 16000", intLe(wav, 24) == 16_000);
        check("byte rate 32000", intLe(wav, 28) == 32_000);
        check("block align 2", shortLe(wav, 32) == 2);
        check("16 bits", shortLe(wav, 34) == 16);
        check("data chunk", ascii(wav, 36, 4).equals("data"));
        check("data size", intLe(wav, 40) == pcm.length * 2);
        check("RIFF size is file length - 8", intLe(wav, 4) == wav.length - 8);
        boolean samplesMatch = true;
        for (int i = 0; i < pcm.length; i++) {
            if ((short) shortLe(wav, 44 + i * 2) != pcm[i]) {
                samplesMatch = false;
                break;
            }
        }
        check("every PCM sample round-trips", samplesMatch);

        // Stereo must set the block align and byte rate truthfully, or the bridge's
        // frame count comes out wrong.
        byte[] stereo = ClaireWire.wav(pcm, 100, 16_000, 2);
        check("stereo block align 4", shortLe(stereo, 32) == 4);
        check("stereo byte rate 64000", intLe(stereo, 28) == 64_000);
        check("stereo data size is 2 bytes per sample", intLe(stereo, 40) == 200);
        check("stereo file is 44 + 2*N", stereo.length == 244);

        Files.write(out.resolve("selftest.wav"), wav);

        // A comma-decimal locale must not leak into a JSON number.
        Locale previous = Locale.getDefault();
        Locale.setDefault(Locale.GERMANY);
        try {
            String loc = ClaireWire.locBody(38.7223, -9.1393, 12.5, "phone");
            Files.write(out.resolve("loc.json"), loc.getBytes(StandardCharsets.UTF_8));
            check("latitude has a dot, not a comma", loc.contains("\"latitude\":38.722300"));
        } finally {
            Locale.setDefault(previous);
        }

        String text = "What's the weather?\n\"now\"";
        Files.write(out.resolve("text.json"),
                ClaireWire.textBody(text).getBytes(StandardCharsets.UTF_8));

        check("a space is %20 in a question",
                ClaireWire.encodeQueryValue("what is it").equals("what%20is%20it"));
        check("& is escaped in a question",
                ClaireWire.encodeQueryValue("a&b=c").equals("a%26b%3Dc"));
        check("appendQuery adds the first pair",
                ClaireWire.appendQuery("/see", "question", "a b").equals("/see?question=a%20b"));
        check("appendQuery appends to an existing query",
                ClaireWire.appendQuery("/see?x=1", "question", "hi")
                        .equals("/see?x=1&question=hi"));
        check("appendQuery on an empty path yields just the query",
                ClaireWire.appendQuery("", "question", "hi").equals("?question=hi"));
        check("number() is locale-proof", ClaireWire.number(-9.1393).equals("-9.139300"));
        check("hex()", ClaireWire.hex(new byte[] {0x00, (byte) 0xff, 0x1a}).equals("00ff1a"));
        check("fnv1a is stable",
                ClaireWire.fnv1a("claire".getBytes(StandardCharsets.UTF_8))
                        == ClaireWire.fnv1a("claire".getBytes(StandardCharsets.UTF_8)));

        System.out.println(failures == 0
                ? "WireSelfTest: all checks passed"
                : "WireSelfTest: " + failures + " CHECK(S) FAILED");
        if (failures != 0) {
            System.exit(1);
        }
    }

    private static void check(String what, boolean ok) {
        if (!ok) {
            failures++;
        }
        System.out.println((ok ? "  ok   " : "  FAIL ") + what);
    }

    private static String ascii(byte[] data, int offset, int length) {
        return new String(data, offset, length, StandardCharsets.US_ASCII);
    }

    private static int intLe(byte[] data, int offset) {
        return (data[offset] & 0xff)
                | ((data[offset + 1] & 0xff) << 8)
                | ((data[offset + 2] & 0xff) << 16)
                | ((data[offset + 3] & 0xff) << 24);
    }

    private static int shortLe(byte[] data, int offset) {
        return (data[offset] & 0xff) | ((data[offset + 1] & 0xff) << 8);
    }
}
