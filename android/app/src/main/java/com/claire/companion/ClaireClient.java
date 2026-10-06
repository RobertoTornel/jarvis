package com.claire.companion;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Every HTTP request this app makes, in one place.
 *
 * <p>Shape, and why it is this shape:
 *
 * <ul>
 *   <li><b>{@code HttpURLConnection}, not OkHttp.</b> The bridges takes plain JSON and
 *       raw bytes over the tailnet; the platform client is already there, and one
 *       fewer dependency is one fewer AAR whose metadata has to agree with the
 *       toolchain the laptop actually has.
 *   <li><b>Nothing throws for an HTTP answer.</b> A 401 is not an exception, it is a
 *       fact the UI must show ("the token was refused"). Only a transport failure
 *       (no route to host, TLS-less connection refused, timeout) becomes an
 *       {@link Answer} with no status. Both are reported, neither is swallowed.
 *   <li><b>The token is a header and only a header.</b> It never appears in a body,
 *       a URL, an error message or a log line.
 *   <li><b>Timeouts, always.</b> A phone held to an ear with a request that never
 *       returns is a phone that looks broken. Transcription gets a longer read
 *       timeout than anything else, because the first call of a session may be
 *       loading a 2.5 GB model on the laptop.
 * </ul>
 */
public final class ClaireClient {

    private static final String BEARER = "Bearer ";
    private static final int CONNECT_TIMEOUT_MS = 8_000;
    private static final int READ_TIMEOUT_MS = 20_000;
    /** The recogniser may be loading; this is the one call that gets patience. */
    private static final int TRANSCRIBE_READ_TIMEOUT_MS = 120_000;

    /** One answer: either a status and body, or a transport failure with a reason. */
    public static final class Answer {
        public final int status;
        public final String body;
        public final String transportError;

        Answer(int status, String body, String transportError) {
            this.status = status;
            this.body = body == null ? "" : body;
            this.transportError = transportError;
        }

        public boolean reachedServer() {
            return transportError == null;
        }

        public boolean ok() {
            return reachedServer() && status >= 200 && status < 300;
        }

        /** A one-line, honest description for the conversation view. */
        public String describe(String what) {
            if (!reachedServer()) {
                return what + " failed: " + transportError;
            }
            if (ok()) {
                return what + " ok";
            }
            String detail = body.isEmpty() ? "" : " — " + shorten(body, 300);
            return what + " was refused with HTTP " + status + detail;
        }
    }

    private final String baseUrl;
    private final String token;

    public ClaireClient(String baseUrl, String token) {
        String base = baseUrl == null ? "" : baseUrl.trim();
        while (base.endsWith("/")) {
            base = base.substring(0, base.length() - 1);
        }
        this.baseUrl = base;
        this.token = token == null ? "" : token.trim();
    }

    public static ClaireClient from(ClairePrefs prefs) {
        return new ClaireClient(prefs.baseUrl(), prefs.token());
    }

    public String baseUrl() {
        return baseUrl;
    }

    public Answer get(String path) {
        return exchange("GET", path, null, null, READ_TIMEOUT_MS);
    }

    public Answer postJson(String path, String json) {
        byte[] body = (json == null ? "{}" : json).getBytes(StandardCharsets.UTF_8);
        return exchange("POST", path, "application/json; charset=utf-8", body, READ_TIMEOUT_MS);
    }

    public Answer postBytes(String path, String contentType, byte[] body) {
        return exchange("POST", path, contentType, body, READ_TIMEOUT_MS);
    }

    /** One utterance of WAV to {@code POST /transcribe}. */
    public Answer transcribe(short[] pcm, int length, int sampleRate) {
        byte[] wav = ClaireWire.wav(pcm, length, sampleRate, 1);
        return exchange("POST", "/transcribe", "audio/wav", wav, TRANSCRIBE_READ_TIMEOUT_MS);
    }

    /** A photo to {@code POST /see}, with the question in the query string. */
    public Answer see(byte[] jpeg, String question) {
        String path = "/see";
        if (question != null && !question.trim().isEmpty()) {
            path = ClaireWire.appendQuery(path, "question", question.trim());
        }
        return exchange("POST", path, "image/jpeg", jpeg, TRANSCRIBE_READ_TIMEOUT_MS);
    }

    public Answer sendLocation(double latitude, double longitude, Double accuracy, String source) {
        return postJson("/loc", ClaireWire.locBody(latitude, longitude, accuracy, source));
    }

    public Answer sendText(String text) {
        return postJson("/text", ClaireWire.textBody(text));
    }

    /** {@code GET /health}: reachability, the masked token handle and the capability verdict. */
    public Answer health() {
        return get("/health");
    }

    /** {@code GET /reply}: drains, so it is called in exactly one place at a time. */
    public Answer reply() {
        return get("/reply");
    }

    private Answer exchange(String method, String path, String contentType, byte[] body,
                            int readTimeoutMs) {
        if (baseUrl.isEmpty()) {
            return new Answer(0, "", "no laptop address is configured");
        }
        HttpURLConnection connection = null;
        try {
            URL url = new URL(baseUrl + path);
            connection = (HttpURLConnection) url.openConnection();
            connection.setRequestMethod(method);
            connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
            connection.setReadTimeout(readTimeoutMs);
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setRequestProperty("Accept", "application/json");
            // The token goes in the header the bridge documents. X-Claire-Token is
            // accepted for compatibility but is not sent: Bearer is canonical.
            if (!token.isEmpty()) {
                connection.setRequestProperty("Authorization", BEARER + token);
            }
            if (body != null) {
                connection.setDoOutput(true);
                connection.setFixedLengthStreamingMode(body.length);
                connection.setRequestProperty("Content-Type",
                        contentType == null ? "application/octet-stream" : contentType);
                try (OutputStream out = connection.getOutputStream()) {
                    out.write(body);
                }
            }
            int status = connection.getResponseCode();
            String answer = readAll(status >= 400
                    ? connection.getErrorStream()
                    : connection.getInputStream());
            return new Answer(status, answer, null);
        } catch (IOException error) {
            // The message must be useful and must never carry the token: it is
            // built from the exception and the host, nothing else.
            return new Answer(0, "", describeIoFailure(error));
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static String describeIoFailure(IOException error) {
        String message = error.getMessage();
        String kind = error.getClass().getSimpleName();
        if (message == null || message.trim().isEmpty()) {
            return kind;
        }
        return kind + ": " + message;
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
                if (buffer.size() > 4 * 1024 * 1024) {
                    break;
                }
            }
            return new String(buffer.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    static String shorten(String text, int limit) {
        String oneLine = text.replace('\n', ' ').replace('\r', ' ').trim();
        return oneLine.length() <= limit ? oneLine : oneLine.substring(0, limit) + "…";
    }

    /** Read one string field out of a JSON object, or {@code ""}. */
    public static String field(String json, String name) {
        try {
            org.json.JSONObject object = new org.json.JSONObject(json);
            String value = object.optString(name, "");
            return value == null ? "" : value;
        } catch (Exception error) {
            return "";
        }
    }

    /**
     * Read one boolean field out of a JSON object, or {@code false}.
     *
     * <p>Written because the first version of this compared substrings:
     * {@code body.contains("\"delivered\":true")}, which is false for every
     * real answer, because the bridge is Python and Python's {@code json.dumps}
     * writes {@code "delivered": true} with a space. The app therefore announced
     * that every successfully delivered utterance had not been delivered. A
     * boolean in JSON is a boolean; reading it as text is how that happens.
     */
    public static boolean booleanField(String json, String name) {
        try {
            return new org.json.JSONObject(json).optBoolean(name, false);
        } catch (Exception failure) {
            return false;
        }
    }

    /** Read the {@code error.code} of a bridge failure, or {@code ""}. */
    public static String errorCode(String json) {
        try {
            org.json.JSONObject root = new org.json.JSONObject(json);
            org.json.JSONObject error = root.optJSONObject("error");
            return error == null ? "" : error.optString("code", "");
        } catch (Exception failure) {
            return "";
        }
    }

    /** Read the {@code error.message} of a bridge failure, or {@code ""}. */
    public static String errorMessage(String json) {
        try {
            org.json.JSONObject root = new org.json.JSONObject(json);
            org.json.JSONObject error = root.optJSONObject("error");
            return error == null ? "" : error.optString("message", "");
        } catch (Exception failure) {
            return "";
        }
    }

    /** Read the {@code error.remedy} of a bridge failure, or {@code ""}. */
    public static String errorRemedy(String json) {
        try {
            org.json.JSONObject root = new org.json.JSONObject(json);
            org.json.JSONObject error = root.optJSONObject("error");
            return error == null ? "" : error.optString("remedy", "");
        } catch (Exception failure) {
            return "";
        }
    }
}
