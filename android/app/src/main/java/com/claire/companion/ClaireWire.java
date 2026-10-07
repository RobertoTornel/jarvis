package com.claire.companion;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/**
 * Every byte this app puts on the wire, in one file with no Android in it.
 *
 * <p>The bridge on the laptop is strict about the shapes it accepts and the phone is
 * the one part of this project where there is no log to read, so the encoders are
 * plain {@code java.nio}/{@code java.lang} code that can still be executed on a
 * laptop (see {@code android/tools/WireSelfTest.java}):
 *
 * <pre>
 *   javac -d /tmp/wire android/app/src/main/java/com/claire/companion/{ClaireWire,AudioSegmenter}.java android/tools/*.java
 *   java -cp /tmp/wire WireSelfTest
 * </pre>
 *
 * <p>Deliberately dependency-free. The bridge accepts two numbers, one RIFF header
 * and one {@code {"text": "..."}} object, so pulling in a JSON library here would be
 * more code, not less.
 *
 * <p>Nothing in this class ever sees the bridge token; the token goes into a request
 * header in {@link ClaireClient} and is never written into a body, a log line or a
 * file.
 */
public final class ClaireWire {

    /** The one sample rate Claire's recogniser is authoritative at. */
    public static final int SAMPLE_RATE = 16_000;

    private ClaireWire() {
    }

    /**
     * Wrap raw 16-bit PCM in a canonical 44-byte RIFF/WAVE header.
     *
     * <p>This has to be byte-exact: the bridge parses the header for the sample rate
     * and the frame count and refuses anything malformed with a 400, so a header that
     * is merely "close" shows up as a confusing rejection on a phone screen. Mono is
     * what the phone records; {@code channels} exists for the self-test.
     */
    public static byte[] wav(short[] pcm, int length, int sampleRate, int channels) {
        if (pcm == null) {
            throw new IllegalArgumentException("pcm must not be null");
        }
        if (length < 0 || length > pcm.length) {
            throw new IllegalArgumentException("length " + length + " is outside the buffer");
        }
        if (sampleRate <= 0) {
            throw new IllegalArgumentException("sampleRate must be positive");
        }
        if (channels <= 0) {
            throw new IllegalArgumentException("channels must be positive");
        }
        final int bitsPerSample = 16;
        final int blockAlign = channels * bitsPerSample / 8;
        final int byteRate = sampleRate * blockAlign;
        final int dataBytes = length * 2;
        final int riffSize = 36 + dataBytes;

        byte[] out = new byte[44 + dataBytes];
        putAscii(out, 0, "RIFF");
        putIntLe(out, 4, riffSize);
        putAscii(out, 8, "WAVE");
        putAscii(out, 12, "fmt ");
        putIntLe(out, 16, 16);              // PCM fmt chunk size
        putShortLe(out, 20, 1);             // format 1 = PCM integer
        putShortLe(out, 22, channels);
        putIntLe(out, 24, sampleRate);
        putIntLe(out, 28, byteRate);
        putShortLe(out, 32, blockAlign);
        putShortLe(out, 34, bitsPerSample);
        putAscii(out, 36, "data");
        putIntLe(out, 40, dataBytes);
        for (int i = 0; i < length; i++) {
            putShortLe(out, 44 + i * 2, pcm[i]);
        }
        return out;
    }

    /** {@code {"text": "..."}} for {@code POST /text}. */
    public static String textBody(String text) {
        return "{\"text\":" + quote(text) + "}";
    }

    /**
     * {@code {"latitude": .., "longitude": .., "accuracy": ..}} for {@code POST /loc}.
     *
     * <p>The two numbers are written with {@link Locale#ROOT}, because a phone set to
     * a comma-decimal locale would otherwise emit {@code 41,4} and the bridge would
     * refuse the body as not-a-number — a failure that depends on the owner's language
     * settings and would be very hard to see.
     */
    public static String locBody(double latitude, double longitude, Double accuracyMetres, String source) {
        StringBuilder sb = new StringBuilder(96);
        sb.append("{\"latitude\":").append(number(latitude));
        sb.append(",\"longitude\":").append(number(longitude));
        if (accuracyMetres != null) {
            sb.append(",\"accuracy\":").append(number(accuracyMetres));
        }
        if (source != null && !source.isEmpty()) {
            sb.append(",\"source\":").append(quote(source));
        }
        sb.append('}');
        return sb.toString();
    }

    /** A JSON number that never depends on the device locale. */
    public static String number(double value) {
        if (Double.isNaN(value) || Double.isInfinite(value)) {
            throw new IllegalArgumentException("not a finite number: " + value);
        }
        return String.format(Locale.ROOT, "%.6f", value);
    }

    /** A JSON string literal, quotes included, with the control characters escaped. */
    public static String quote(String value) {
        String text = value == null ? "" : value;
        StringBuilder sb = new StringBuilder(text.length() + 16);
        sb.append('"');
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
                default:
                    if (c < 0x20 || c == 0x7f) {
                        sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    /**
     * Percent-encode one query value the way an HTTP client must.
     *
     * <p>{@code URLEncoder} is the usual shortcut, but it is
     * {@code application/x-www-form-urlencoded}: it turns a space into {@code +},
     * which is legal in a query string only by convention, and it leaves {@code *}
     * alone. The bridge splits the query on {@code &} and {@code =} and then
     * percent-decodes, so a question containing {@code &} or a space has to be
     * encoded here or the endpoint receives half a question.
     */
    public static String encodeQueryValue(String value) {
        byte[] raw = (value == null ? "" : value).getBytes(StandardCharsets.UTF_8);
        StringBuilder sb = new StringBuilder(raw.length + 16);
        for (byte b : raw) {
            int c = b & 0xff;
            boolean unreserved =
                    (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
                            || c == '-' || c == '_' || c == '.' || c == '~';
            if (unreserved) {
                sb.append((char) c);
            } else {
                sb.append('%')
                        .append(Character.toUpperCase(HEX[(c >> 4) & 0xf]))
                        .append(Character.toUpperCase(HEX[c & 0xf]));
            }
        }
        return sb.toString();
    }

    /**
     * Add one encoded pair to a path that may or may not already carry a query.
     *
     * <p>Returns the whole {@code path?query} — the caller passes {@code "/see"} and
     * gets {@code "/see?question=..."} back, not {@code "?/see&..."}.
     */
    public static String appendQuery(String path, String name, String value) {
        String base = path == null ? "" : path;
        String pair = encodeQueryValue(name) + "=" + encodeQueryValue(value);
        if (base.isEmpty()) {
            return "?" + pair;
        }
        if (base.indexOf('?') >= 0) {
            return base + "&" + pair;
        }
        return base + "?" + pair;
    }

    /** Bytes as lower-case hex, for size/identity reporting that is never a secret. */
    public static String hex(byte[] data) {
        StringBuilder sb = new StringBuilder(data.length * 2);
        for (byte b : data) {
            sb.append(HEX[(b >> 4) & 0xf]).append(HEX[b & 0xf]);
        }
        return sb.toString();
    }

    /** A cheap 64-bit FNV-1a digest, used only for "is this the same frame?" notes. */
    public static long fnv1a(byte[] data) {
        long hash = 0xcbf29ce484222325L;
        for (byte b : data) {
            hash ^= (b & 0xff);
            hash *= 0x100000001b3L;
        }
        return hash;
    }

    /** Concatenate two byte arrays; the camera path needs it for a JPEG preview. */
    public static byte[] concat(byte[] first, byte[] second) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(first.length + second.length);
        out.write(first, 0, first.length);
        out.write(second, 0, second.length);
        return out.toByteArray();
    }

    private static final char[] HEX = "0123456789abcdef".toCharArray();

    private static void putAscii(byte[] out, int offset, String text) {
        for (int i = 0; i < text.length(); i++) {
            out[offset + i] = (byte) text.charAt(i);
        }
    }

    private static void putIntLe(byte[] out, int offset, int value) {
        out[offset] = (byte) (value & 0xff);
        out[offset + 1] = (byte) ((value >> 8) & 0xff);
        out[offset + 2] = (byte) ((value >> 16) & 0xff);
        out[offset + 3] = (byte) ((value >> 24) & 0xff);
    }

    private static void putShortLe(byte[] out, int offset, int value) {
        out[offset] = (byte) (value & 0xff);
        out[offset + 1] = (byte) ((value >> 8) & 0xff);
    }
}
