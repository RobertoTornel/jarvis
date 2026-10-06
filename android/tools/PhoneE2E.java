import com.claire.companion.ClaireWire;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * The phone's wire code, against a real bridge.
 *
 * <p>This is not a JUnit test and it is not a mock: it compiles the app's own
 * {@link ClaireWire} (the class that builds the WAV and the JSON bodies on the phone),
 * reads a raw 16 kHz mono PCM16 recording, and posts it with the same
 * {@code HttpURLConnection} calls {@code ClaireClient} makes. Whatever answers is the
 * real bridge with the real Parakeet model.
 *
 * <p>Usage: PhoneE2E &lt;baseUrl&gt; &lt;token&gt; &lt;pcm16le-file&gt; [--text "words"]
 * [--location lat,lon,accuracy] [--reply]
 */
public final class PhoneE2E {

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("usage: PhoneE2E <baseUrl> <token> <pcm-file> [--text S] "
                    + "[--location lat,lon,acc] [--reply] [--health]");
            System.exit(2);
        }
        String baseUrl = args[0];
        String token = args[1];
        Path pcmFile = Path.of(args[2]);

        for (int i = 3; i < args.length; i++) {
            switch (args[i]) {
                case "--text":
                    System.out.println("TEXT " + exchange("POST", baseUrl + "/text", token,
                            "application/json; charset=utf-8",
                            ClaireWire.textBody(args[++i]).getBytes("UTF-8")));
                    break;
                case "--location": {
                    String[] parts = args[++i].split(",");
                    Double accuracy = parts.length > 2 ? Double.valueOf(parts[2]) : null;
                    System.out.println("LOC " + exchange("POST", baseUrl + "/loc", token,
                            "application/json; charset=utf-8",
                            ClaireWire.locBody(Double.parseDouble(parts[0]),
                                    Double.parseDouble(parts[1]), accuracy, "phone")
                                    .getBytes("UTF-8")));
                    break;
                }
                case "--reply":
                    System.out.println("REPLY " + exchange("GET", baseUrl + "/reply", token,
                            null, null));
                    break;
                case "--health":
                    System.out.println("HEALTH " + exchange("GET", baseUrl + "/health", token,
                            null, null));
                    break;
                default:
                    System.err.println("unknown option " + args[i]);
                    System.exit(2);
            }
        }

        if (Files.exists(pcmFile)) {
            short[] pcm = readPcm16(pcmFile);
            byte[] wav = ClaireWire.wav(pcm, pcm.length, ClaireWire.SAMPLE_RATE, 1);
            System.out.println("WAV_BYTES " + wav.length + " SAMPLES " + pcm.length);
            System.out.println("TRANSCRIBE " + exchange("POST", baseUrl + "/transcribe", token,
                    "audio/wav", wav));
        }
    }

    /** Read little-endian PCM16 into samples, the way AudioRecord hands them over. */
    private static short[] readPcm16(Path file) throws IOException {
        byte[] raw = Files.readAllBytes(file);
        short[] samples = new short[raw.length / 2];
        for (int i = 0; i < samples.length; i++) {
            int low = raw[2 * i] & 0xFF;
            int high = raw[2 * i + 1];
            samples[i] = (short) ((high << 8) | low);
        }
        return samples;
    }

    private static String exchange(String method, String url, String token,
                                   String contentType, byte[] body) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setRequestMethod(method);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(180000);
            connection.setRequestProperty("Accept", "application/json");
            if (token != null && !token.isEmpty()) {
                connection.setRequestProperty("Authorization", "Bearer " + token);
            }
            if (body != null) {
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(body.length);
                connection.setRequestProperty("Content-Type", contentType);
                try (OutputStream out = connection.getOutputStream()) {
                    out.write(body);
                }
            }
            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream()
                    : connection.getInputStream();
            return status + " " + readAll(stream);
        } catch (IOException error) {
            return "TRANSPORT " + error.getClass().getSimpleName() + ": " + error.getMessage();
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static String readAll(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        try (InputStream in = stream) {
            ByteArrayOutputStream buffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            int read;
            while ((read = in.read(chunk)) > 0) {
                buffer.write(chunk, 0, read);
            }
            return new String(buffer.toByteArray(), "UTF-8");
        }
    }
}
