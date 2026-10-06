import com.claire.companion.ClaireClient;
import com.claire.companion.ClaireWire;

import java.io.PrintStream;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * The app's own HTTP client, executed against a real bridge.
 *
 * <p>{@code ClaireClient} imports no Android class at all — {@code HttpURLConnection},
 * {@code java.nio} and {@code org.json} and nothing else — so it runs on the laptop
 * with an {@code org.json} jar on the classpath. That makes the interesting half of the
 * client testable without a phone: the answer parsing, the status handling and the
 * transport-failure path.
 *
 * <p>This exists because of a bug it would have caught: the client used to read
 * {@code "delivered"} with {@code body.contains("\"delivered\":true")}, which is false
 * for every real answer, because the bridge is Python and Python's {@code json.dumps}
 * writes {@code "delivered": true} with a space after the colon. The app then told the
 * owner that every delivered utterance had not been delivered. The comparison below
 * keeps that old check side by side with the real one.
 *
 * <p>Usage: ClientSelfTest &lt;baseUrl&gt; &lt;token&gt; &lt;pcm16le-file&gt;
 */
public final class ClientSelfTest {

    private static int failures;

    public static void main(String[] args) throws Exception {
        String baseUrl = args[0];
        String token = args[1];
        Path pcmFile = Path.of(args[2]);

        ClaireClient client = new ClaireClient(baseUrl, token);

        // -- health, and the fields the header line shows ------------------- //
        ClaireClient.Answer health = client.health();
        check("health reached the bridge", health.reachedServer(), true);
        check("health is 200", health.status, 200);
        check("health reports the bridge port", ClaireClient.field(health.body, "port"), "8788");
        // The recogniser has two named states before anything is transcribed: the
        // project's own engine is resolved lazily, so a fresh bridge says
        // "project_default" and a bridge that has already transcribed says
        // "configured".  Both are the real engine; neither is a missing one.
        String status = ClaireClient.field(health.body, "transcribe");
        check("health names the recogniser's state",
                status.equals("project_default") || status.equals("configured"), true);
        check("health masks the token", ClaireClient.field(health.body, "token").contains("#"),
                true);

        // -- a real transcription through the app's own client -------------- //
        short[] pcm = readPcm16(pcmFile);
        ClaireClient.Answer transcript = client.transcribe(pcm, pcm.length,
                ClaireWire.SAMPLE_RATE);
        check("transcribe is 200", transcript.status, 200);
        check("transcribe returns words", ClaireClient.field(transcript.body, "text").isEmpty(),
                false);
        check("transcribe names the engine", ClaireClient.field(transcript.body, "engine"),
                "parakeet_nemo");

        // The fix, and the bug it replaced, on one real answer body.
        boolean delivered = ClaireClient.booleanField(transcript.body, "delivered");
        boolean oldCheck = transcript.body.contains("\"delivered\":true");
        System.out.println("  body: " + transcript.body);
        check("booleanField sees delivered=true", delivered, true);
        check("the old substring check would have said false", oldCheck, false);
        check("and the recogniser now reports itself configured",
                ClaireClient.field(client.health().body, "transcribe"), "configured");

        // -- typed text and position ---------------------------------------- //
        check("text is accepted", client.sendText("client self test").ok(), true);
        check("location is accepted",
                client.sendLocation(38.7223, -9.1393, 12.5, "phone").ok(), true);
        check("reply is drained", client.reply().ok(), true);

        // -- the two failures the UI has to tell apart ---------------------- //
        ClaireClient wrongToken = new ClaireClient(baseUrl, "definitely-not-the-token");
        ClaireClient.Answer refused = wrongToken.health();
        check("a wrong token is refused with 401", refused.status, 401);
        check("and the refusal is named", ClaireClient.errorCode(refused.body), "unauthorized");
        check("and it carries a remedy", ClaireClient.errorRemedy(refused.body).isEmpty(),
                false);

        ClaireClient unreachable = new ClaireClient("http://127.0.0.1:1", token);
        ClaireClient.Answer dead = unreachable.health();
        check("an unreachable laptop is a transport failure", dead.reachedServer(), false);
        check("and it is not an HTTP status", dead.status, 0);
        check("and it is described in words", dead.transportError.isEmpty(), false);
        check("and ok() is false", dead.ok(), false);

        // -- a refusal that is not a 401 must not look like success ---------- //
        ClaireClient.Answer noAudio = client.postBytes("/transcribe", "audio/mpeg",
                new byte[] {0x49, 0x44, 0x33, 0, 0, 0, 0, 0, 0});
        check("wrong audio type is 415", noAudio.status, 415);
        check("and it is not ok()", noAudio.ok(), false);

        PrintStream out = System.out;
        out.println(failures == 0 ? "ALL CHECKS PASSED" : failures + " CHECK(S) FAILED");
        System.exit(failures == 0 ? 0 : 1);
    }

    private static void check(String what, Object actual, Object expected) {
        boolean ok = actual == null ? expected == null : actual.equals(expected);
        if (!ok) {
            failures++;
        }
        System.out.println((ok ? "  ok   " : "  FAIL ") + what
                + (ok ? "" : "  (expected " + expected + ", got " + actual + ")"));
    }

    private static short[] readPcm16(Path file) throws Exception {
        byte[] raw = Files.readAllBytes(file);
        short[] samples = new short[raw.length / 2];
        for (int i = 0; i < samples.length; i++) {
            samples[i] = (short) (((raw[2 * i + 1] << 8) | (raw[2 * i] & 0xFF)));
        }
        return samples;
    }
}
