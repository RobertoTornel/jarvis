package com.claire.companion;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.util.Log;

/**
 * Claire's voice, called from the phone and played while it is still arriving.
 *
 * <p>The owner asked for two things and they are the same thing: <i>the app itself
 * calls Fish Audio</i>, and it <i>streams</i>. So this class does the call, and it
 * never waits for the last byte before the first sound.
 *
 * <h3>The wire call</h3>
 *
 * <p>{@code POST https://api.fish.audio/v1/tts}, {@code Authorization: Bearer <token>},
 * the model id in the {@code model} header, and a JSON body asking for {@code wav}.
 * That is not a preference. Fish Audio's default response is MP3, and the laptop
 * already learned this the hard way: MP3 bytes written straight into an int16
 * stream are loud, confident, unintelligible noise. WAV also carries its own sample
 * rate and channel count, so the player never has to assume them — and it is a
 * container this class can parse from a <b>partially arrived</b> stream, which is
 * exactly what it has to do.
 *
 * <h3>Why AudioTrack and not MediaPlayer</h3>
 *
 * <p>MediaPlayer wants to know how long the thing is. A streamed WAV does not know:
 * Fish sends a placeholder RIFF size (the real first bytes are
 * {@code 52 49 46 46 24 ff ff ff 57 41 56 45}), so any player that trusts the
 * declared length either stops early or hangs. {@link AudioTrack} in
 * {@code MODE_STREAM} needs no length at all — it plays what it is given, as it is
 * given — which is the whole requirement.
 *
 * <h3>The key</h3>
 *
 * <p>The token is passed in, used in one request header, and never written to
 * {@code Log}, a file, a conversation line or an exception message. Every failure
 * below is phrased without it, and {@link #status} is what the settings screen
 * shows: a fingerprint, never the key.
 */
public final class FishTts {

    private static final String TAG = "ClaireFish";

    /** The one endpoint. Hardcoded because it is the vendor's, not a setting. */
    static final String ENDPOINT = "https://api.fish.audio/v1/tts";

    /** The voice field's wire name. Fish calls a cloned voice a "reference_id". */
    private static final String VOICE_FIELD = "reference_id";

    /** Low latency is what makes "stream while it arrives" worth anything. */
    private static final String LATENCY = "low";

    /** Connect budget; the API answers fast or not at all. */
    private static final int CONNECT_TIMEOUT_MS = 15000;

    /** Per-read budget. The first byte arrived in ~0.8 s when measured from the laptop. */
    private static final int READ_TIMEOUT_MS = 30000;

    /** How much of an error body is worth repeating on screen. */
    private static final int ERROR_BODY_CHARS = 240;

    /** What the caller is told about one utterance. */
    public interface Listener {
        /** The first audio frame is playing. */
        void onStarted();

        /** The last frame has played. */
        void onDone();

        /** Nothing was played, and this says why in words the owner can act on. */
        void onError(String message);
    }

    private final ExecutorService worker = Executors.newSingleThreadExecutor(runnable -> {
        Thread thread = new Thread(runnable, "claire-fish-tts");
        thread.setDaemon(true);
        return thread;
    });

    /** Set while a call is in flight so {@link #stop()} can cut it short. */
    private final AtomicBoolean cancelled = new AtomicBoolean(false);

    private volatile HttpURLConnection connection;
    private volatile AudioTrack track;

    /** A short, token-free description of the last attempt, for the settings screen. */
    private volatile String lastStatus = "not used yet";

    public String status() {
        return lastStatus;
    }

    /**
     * Synthesise {@code text} and play it as the bytes arrive.
     *
     * <p>Returns immediately; the work happens on this class's own thread, because
     * {@link AudioTrack#write} blocks and the caller is the service's main thread.
     * Exactly one of the listener's terminal methods is called for each utterance.
     */
    public void speak(final String token, final String model, final String voiceId,
                      final String text, final Listener listener) {
        final String key = token == null ? "" : token.trim();
        final String chosenModel = model == null ? "" : model.trim();
        final String voice = voiceId == null ? "" : voiceId.trim();
        final String words = text == null ? "" : text.trim();

        if (words.isEmpty()) {
            lastStatus = "nothing to say";
            listener.onError("there was no text to speak");
            return;
        }
        if (key.isEmpty()) {
            lastStatus = "no Fish key";
            listener.onError("no Fish Audio key is set");
            return;
        }
        if (chosenModel.isEmpty()) {
            lastStatus = "no Fish model";
            listener.onError("no Fish Audio model id is set");
            return;
        }

        cancelled.set(false);
        worker.execute(() -> run(key, chosenModel, voice, words, listener));
    }

    /** Stop whatever is playing and abandon an in-flight request. */
    public void stop() {
        cancelled.set(true);
        HttpURLConnection open = connection;
        if (open != null) {
            try {
                open.disconnect();
            } catch (Exception ignored) {
                // Disconnecting a connection that is already gone is not an error.
            }
        }
        AudioTrack playing = track;
        if (playing != null) {
            try {
                // stop(), not pause(): a paused track's buffer fills and the writer
                // thread blocks inside write() with no way out. stop() unblocks it,
                // and the run loop then sees the cancellation flag and releases.
                playing.stop();
            } catch (IllegalStateException ignored) {
                // Already released; nothing to silence.
            }
        }
    }

    public void shutdown() {
        stop();
        worker.shutdownNow();
    }

    // -- the work --------------------------------------------------------- //

    private void run(String token, String model, String voiceId, String text,
                     Listener listener) {
        final long startedAt = System.currentTimeMillis();
        try {
            connection = open(token, model, voiceId, text);
            int status = connection.getResponseCode();
            if (status != HttpURLConnection.HTTP_OK) {
                String body = readErrorBody(status);
                lastStatus = "Fish HTTP " + status;
                listener.onError("Fish Audio answered HTTP " + status + describe(body));
                return;
            }

            InputStream stream = connection.getInputStream();
            WavHeader header = new WavHeader();
            ByteArrayOutputStream pending = new ByteArrayOutputStream(8192);
            byte[] chunk = new byte[8192];
            boolean dataSeen = false;
            boolean startedFired = false;

            while (!cancelled.get()) {
                int read = stream.read(chunk);
                if (read < 0) {
                    break;
                }
                if (read == 0) {
                    continue;
                }
                if (!dataSeen) {
                    pending.write(chunk, 0, read);
                    int dataStart = header.parse(pending.toByteArray());
                    if (dataStart < 0) {
                        if (header.refused()) {
                            // It is already certain this is not a playable WAV — most
                            // often a JSON error body that Fish sent with HTTP 200.
                            // Say so now rather than accumulating a megabyte first.
                            lastStatus = "not a WAV stream: " + header.describe();
                            listener.onError("Fish Audio sent " + header.describe());
                            return;
                        }
                        if (pending.size() > 1 << 20) {
                            // A megabyte with no data chunk is not a WAV.
                            lastStatus = "not a WAV stream";
                            listener.onError("Fish Audio did not send WAV audio");
                            return;
                        }
                        continue;
                    }
                    if (!header.playable()) {
                        lastStatus = "unsupported WAV: " + header.describe();
                        listener.onError("Fish Audio sent " + header.describe()
                                + ", which this phone cannot play");
                        return;
                    }
                    dataSeen = true;
                    openTrack(header);
                    byte[] all = pending.toByteArray();
                    if (dataStart < all.length) {
                        writeAudio(all, dataStart, all.length - dataStart);
                        listener.onStarted();
                        startedFired = true;
                    }
                    pending = null;
                } else {
                    writeAudio(chunk, 0, read);
                    if (!startedFired) {
                        listener.onStarted();
                        startedFired = true;
                    }
                }
            }

            if (cancelled.get()) {
                lastStatus = "stopped";
                closeTrack();
                listener.onDone();
                return;
            }

            if (!dataSeen) {
                lastStatus = "empty stream";
                listener.onError("Fish Audio sent no audio");
                return;
            }

            drainAndRelease();
            lastStatus = "spoke in " + (System.currentTimeMillis() - startedAt) + " ms"
                    + " (" + header.rate + " Hz, " + header.channels + " ch,"
                    + " streamed)";
            listener.onDone();
        } catch (IOException error) {
            lastStatus = "network: " + error.getClass().getSimpleName();
            closeTrack();
            if (!cancelled.get()) {
                listener.onError("Fish Audio could not be reached ("
                        + error.getClass().getSimpleName() + ")");
            } else {
                listener.onDone();
            }
        } catch (RuntimeException error) {
            // A bad model id, a refused socket, an AudioTrack that will not open:
            // named rather than swallowed, and never carrying the key.
            lastStatus = "failed: " + error.getClass().getSimpleName();
            Log.w(TAG, "Fish playback failed: " + error.getClass().getSimpleName());
            closeTrack();
            listener.onError("Fish playback failed ("
                    + error.getClass().getSimpleName() + ")");
        } finally {
            connection = null;
        }
    }

    private HttpURLConnection open(String token, String model, String voiceId, String text)
            throws IOException {
        StringBuilder body = new StringBuilder(64 + text.length());
        body.append("{\"text\":").append(json(text))
                .append(",\"latency\":\"").append(LATENCY).append('"')
                .append(",\"format\":\"wav\"");
        if (!voiceId.isEmpty()) {
            body.append(',').append(json(VOICE_FIELD)).append(':').append(json(voiceId));
        }
        body.append('}');
        byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);

        HttpURLConnection open = (HttpURLConnection) new URL(ENDPOINT).openConnection();
        open.setRequestMethod("POST");
        open.setConnectTimeout(CONNECT_TIMEOUT_MS);
        open.setReadTimeout(READ_TIMEOUT_MS);
        open.setDoOutput(true);
        open.setRequestProperty("Content-Type", "application/json");
        open.setRequestProperty("Authorization", "Bearer " + token);
        open.setRequestProperty("model", model);
        // Streaming: without this the whole body may be buffered before any is read.
        open.setChunkedStreamingMode(0);
        try (OutputStream out = open.getOutputStream()) {
            out.write(payload);
        }
        return open;
    }

    /** A tiny JSON string encoder: enough for text and ids, and no dependency. */
    static String json(String value) {
        StringBuilder out = new StringBuilder(value.length() + 2);
        out.append('"');
        for (int index = 0; index < value.length(); index++) {
            char c = value.charAt(index);
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
            }
        }
        return out.append('"').toString();
    }

    private String readErrorBody(int status) {
        InputStream stream = null;
        try {
            stream = connection.getErrorStream();
            if (stream == null) {
                return "";
            }
            byte[] buffer = new byte[1024];
            int read = stream.read(buffer);
            if (read <= 0) {
                return "";
            }
            String text = new String(buffer, 0, read, StandardCharsets.UTF_8).trim();
            if (text.length() > ERROR_BODY_CHARS) {
                text = text.substring(0, ERROR_BODY_CHARS) + "…";
            }
            return text.isEmpty() ? "" : ": " + text;
        } catch (IOException ignored) {
            return "";
        } finally {
            close(stream);
        }
    }

    private static String describe(String body) {
        return body;
    }

    // -- the player ------------------------------------------------------- //

    /** Open a streaming track for the format the WAV header declared. */
    private void openTrack(WavHeader header) {
        int channelMask = header.channels == 1
                ? AudioFormat.CHANNEL_OUT_MONO : AudioFormat.CHANNEL_OUT_STEREO;
        int minimum = AudioTrack.getMinBufferSize(
                header.rate, channelMask, AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) {
            minimum = header.rate; // ~0.25 s of stereo-or-mono 16-bit, as a floor
        }
        int bufferSize = Math.max(minimum, header.rate / 2 * Math.max(1, header.channels));

        AudioTrack created = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                .setAudioFormat(new AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(header.rate)
                        .setChannelMask(channelMask)
                        .build())
                .setBufferSizeInBytes(bufferSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build();
        if (created.getState() != AudioTrack.STATE_INITIALIZED) {
            created.release();
            throw new IllegalStateException("AudioTrack refused " + header.describe());
        }
        track = created;
        created.play();
    }

    /**
     * Write PCM to the streaming track. Blocks until the track has room, which is
     * what paces the network read to playback instead of buffering the whole reply.
     */
    private void writeAudio(byte[] buffer, int offset, int length) {
        AudioTrack playing = track;
        if (playing == null || length <= 0) {
            return;
        }
        playing.write(buffer, offset, length);
    }

    /** Let the buffered tail play, then release. */
    private void drainAndRelease() {
        AudioTrack playing = track;
        track = null;
        if (playing == null) {
            return;
        }
        try {
            // stop() in MODE_STREAM plays out what is already written; release()
            // straight after would cut the last word off.
            playing.stop();
        } catch (IllegalStateException ignored) {
            // Already stopped.
        }
        try {
            playing.release();
        } catch (Exception ignored) {
            // Nothing left to release.
        }
    }

    private void closeTrack() {
        AudioTrack playing = track;
        track = null;
        if (playing == null) {
            return;
        }
        try {
            playing.pause();
            playing.flush();
        } catch (IllegalStateException ignored) {
            // Not playing.
        }
        try {
            playing.release();
        } catch (Exception ignored) {
            // Already released.
        }
    }

    private static void close(InputStream stream) {
        if (stream != null) {
            try {
                stream.close();
            } catch (IOException ignored) {
                // Closing a dead stream is not an error.
            }
        }
    }

    // -- the container ---------------------------------------------------- //

    /**
     * Enough of a WAV parser to start playing before the file has finished arriving.
     *
     * <p>It never trusts a declared length: Fish's streamed WAV carries
     * {@code 0xFFFFFF24} as its RIFF size. It walks {@code fmt } to learn the rate
     * and channel count, then stops at {@code data} and reports where the samples
     * begin. The caller drops its reference to the header prefix the moment the sample
     * offset is known, so a long reply does not keep its own prefix in memory.
     */
    static final class WavHeader {

        int rate;
        int channels = 1;
        int bits = 16;

        private boolean ready;
        private int dataOffset = -1;

        /** The sample bytes' offset in the buffer, or -1 when not known yet. */
        int parse(byte[] buffer) {
            if (dataOffset >= 0) {
                return dataOffset;
            }
            if (buffer.length < 12) {
                return -1;
            }
            if (!(buffer[0] == 'R' && buffer[1] == 'I' && buffer[2] == 'F'
                    && buffer[3] == 'F' && buffer[8] == 'W' && buffer[9] == 'A'
                    && buffer[10] == 'V' && buffer[11] == 'E')) {
                // Not RIFF/WAVE. Fish can also answer with headerless PCM, but
                // guessing a rate for that would be exactly the "played as noise"
                // failure this module exists to avoid, so it is refused by name.
                dataOffset = -2;
                return -1;
            }
            int at = 12;
            while (at + 8 <= buffer.length) {
                int size = littleInt(buffer, at + 4);
                if (buffer[at] == 'f' && buffer[at + 1] == 'm' && buffer[at + 2] == 't') {
                    if (at + 8 + 16 > buffer.length) {
                        return -1;
                    }
                    int format = littleShort(buffer, at + 8);
                    channels = littleShort(buffer, at + 10);
                    rate = littleInt(buffer, at + 12);
                    bits = littleShort(buffer, at + 22);
                    if (format != 1) {
                        dataOffset = -3;
                        return -1;
                    }
                } else if (buffer[at] == 'd' && buffer[at + 1] == 'a'
                        && buffer[at + 2] == 't' && buffer[at + 3] == 'a') {
                    dataOffset = at + 8;
                    ready = true;
                    return dataOffset;
                }
                // A streamed WAV may carry a bogus size; walk in 2-byte steps when
                // it is zero so a padding byte cannot stall the walk.
                int step = 8 + (size > 0 ? size : 0);
                if (size <= 0) {
                    step = 8;
                }
                at += step + (step % 2);
                if (size <= 0 && step == 8) {
                    at += 0;
                }
            }
            return -1;
        }

        boolean playable() {
            return ready && rate > 0 && (channels == 1 || channels == 2) && bits == 16;
        }

        /**
         * True once this parser has positively decided the bytes are not playable.
         *
         * <p>Distinct from {@code parse()} returning -1, which also means "not enough
         * bytes yet". This is "stop reading": a JSON error body arrives with an HTTP
         * 200 and would otherwise be buffered a megabyte at a time before failing.
         */
        boolean refused() {
            return dataOffset == -2 || dataOffset == -3;
        }

        String describe() {
            if (dataOffset == -2) {
                return "something that is not WAV";
            }
            if (dataOffset == -3) {
                return "a compressed WAV (" + bits + "-bit, format != PCM)";
            }
            return bits + "-bit audio at " + rate + " Hz with " + channels + " channels";
        }

        private static int littleShort(byte[] buffer, int at) {
            return (buffer[at] & 0xFF) | ((buffer[at + 1] & 0xFF) << 8);
        }

        private static int littleInt(byte[] buffer, int at) {
            return (buffer[at] & 0xFF) | ((buffer[at + 1] & 0xFF) << 8)
                    | ((buffer[at + 2] & 0xFF) << 16) | ((buffer[at + 3] & 0xFF) << 24);
        }
    }
}
