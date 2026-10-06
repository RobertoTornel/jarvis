package com.claire.companion;

/**
 * The voice-activity detector: raw microphone frames in, whole utterances out.
 *
 * <p>Claire's bridge accepts an utterance as one WAV file, so an always-listening
 * phone has to decide for itself where a sentence starts and ends. This class is
 * that decision, and it is written with no Android in it on purpose: it can be
 * compiled and run on a laptop against a synthetic signal (see
 * {@code android/tools/SegmenterSelfTest.java}), which is the only way to find out
 * whether the thresholds are sane without holding the phone to your mouth.
 *
 * <p>The detector is deliberately simple and explainable, because a phone that
 * silently stops transcribing is worse than one that transcribes imperfectly:
 *
 * <ul>
 *   <li><b>Short-term energy</b> (root mean square over a 20 ms frame) against a
 *       floor that <em>adapts</em> to the room. A fixed threshold either fires
 *       constantly in a quiet room or never fires in a noisy one, and the owner's
 *       phone is in both kinds of room.
 *   <li><b>Zero-crossing rate</b> as a second opinion. Energy alone fires on a door
 *       slam, a hand brushing the microphone or a bass note; those have a very
 *       different crossing rate from speech, so requiring a speech-like rate removes
 *       a whole class of false starts.
 *   <li><b>Pre-roll.</b> A syllable is already gone by the time energy rises, so the
 *       last {@code preRollMs} of pre-speech audio is kept in a ring buffer and
 *       prepended to the segment. Without it, Parakeet hears "…at's the weather".
 *   <li><b>Hangover.</b> Speech pauses mid-sentence ("what's the weather — in
 *       Lisbon"). The segment only ends after {@code hangoverMs} of continuous
 *       silence, and it is split by nothing else, so a natural pause does not become
 *       two utterances.
 *   <li><b>A hard maximum.</b> A television, a fan or a stuck frame must not grow the
 *       buffer without bound.
 * </ul>
 *
 * <p>All durations are milliseconds of audio; all energy values are in 16-bit sample
 * units (0..32767).
 */
public final class AudioSegmenter {

    /** Receives one complete utterance, pre-roll included. */
    public interface Sink {
        /**
         * @param pcm        the utterance, valid for {@code length} samples
         * @param length     how many samples of {@code pcm} are the utterance
         * @param sampleRate the rate the samples were captured at
         */
        void onSpeechSegment(short[] pcm, int length, int sampleRate);
    }

    /** The knobs. Defaults are what {@code SegmenterSelfTest} exercises. */
    public static final class Params {
        /** Analysis frame. 20 ms is the usual compromise between latency and stability. */
        public int frameMs = 20;
        /** How much pre-speech audio to prepend. */
        public int preRollMs = 300;
        /** Below this much voiced audio a segment is a cough, not a sentence. */
        public int minSpeechMs = 250;
        /** Silence that ends an utterance. */
        public int hangoverMs = 700;
        /** A segment is force-closed at this length. */
        public int maxSegmentMs = 15_000;
        /** Speech must exceed the noise floor by this factor. */
        public double noiseMultiplier = 3.0;
        /** ...and must exceed this absolute energy, so a silent room stays silent. */
        public double absoluteFloor = 200.0;
        /** Above this crossing rate the sound is a click or a hiss, not speech. */
        public double maxZeroCrossingRate = 0.45;
        /** How fast the noise floor follows the room: 0.05 ≈ a 20-frame time constant. */
        public double noiseAdaptRate = 0.05;
        /**
         * The first stretch of audio only ever calibrates. Listening usually starts
         * in a room that is already making noise (a fan, a car, a café), and a
         * detector that starts with a threshold of "silence" fires on the room
         * itself before it has heard anything.
         */
        public int warmupMs = 400;
        /** How fast the floor is learned during the warm-up. */
        public double warmupAdaptRate = 0.3;
        /**
         * While speech is in progress the floor may still creep up, but at
         * {@code noiseAdaptRate * this} — 1000× slower at the defaults, a time
         * constant measured in minutes. That is what eventually suppresses a
         * television switched on mid-session or a fan that starts up after
         * calibration. It is deliberately far slower than the rate that would
         * matter to a person: the detector would rather transcribe a minute of
         * noise than go deaf in the middle of a dictated paragraph.
         */
        public double speechAdaptRateScale = 0.001;

        /** A defensive copy, so a caller cannot mutate a detector's thresholds. */
        public Params copy() {
            Params p = new Params();
            p.frameMs = frameMs;
            p.preRollMs = preRollMs;
            p.minSpeechMs = minSpeechMs;
            p.hangoverMs = hangoverMs;
            p.maxSegmentMs = maxSegmentMs;
            p.noiseMultiplier = noiseMultiplier;
            p.absoluteFloor = absoluteFloor;
            p.maxZeroCrossingRate = maxZeroCrossingRate;
            p.noiseAdaptRate = noiseAdaptRate;
            p.warmupMs = warmupMs;
            p.warmupAdaptRate = warmupAdaptRate;
            p.speechAdaptRateScale = speechAdaptRateScale;
            return p;
        }
    }

    private final int sampleRate;
    private final Params params;
    private final Sink sink;

    private final int frameSamples;
    private final int preRollFrames;
    private final short[][] preRoll;
    private int preRollFill;
    private int preRollNext;

    private final short[] pending;      // partial frame carried between accept() calls
    private int pendingFill;

    private short[] segment;
    private int segmentFill;
    private int segmentSamples;         // audio captured since the utterance started
    private int voicedSamples;
    private int silenceSamples;
    private boolean inSpeech;

    private double noiseFloor;
    private long segmentsEmitted;
    private long framesSeen;

    public AudioSegmenter(int sampleRate, Params params, Sink sink) {
        if (sampleRate <= 0) {
            throw new IllegalArgumentException("sampleRate must be positive");
        }
        if (sink == null) {
            throw new IllegalArgumentException("sink must not be null");
        }
        this.sampleRate = sampleRate;
        this.params = (params == null ? new Params() : params).copy();
        this.sink = sink;
        this.frameSamples = Math.max(1, this.params.frameMs * sampleRate / 1000);
        this.preRollFrames = Math.max(1, this.params.preRollMs / Math.max(1, this.params.frameMs));
        this.preRoll = new short[preRollFrames][];
        this.pending = new short[frameSamples];
        this.segment = new short[Math.max(frameSamples * 8, this.params.maxSegmentMs * sampleRate / 1000 + frameSamples)];
        this.noiseFloor = this.params.absoluteFloor;
    }

    /** Feed captured audio. Any number of samples; frames are assembled internally. */
    public void accept(short[] data, int offset, int length) {
        if (data == null || length <= 0) {
            return;
        }
        int end = Math.min(data.length, offset + length);
        int i = offset;
        while (i < end) {
            int take = Math.min(frameSamples - pendingFill, end - i);
            System.arraycopy(data, i, pending, pendingFill, take);
            pendingFill += take;
            i += take;
            if (pendingFill == frameSamples) {
                analyseFrame(pending);
                pendingFill = 0;
            }
        }
    }

    /** Close whatever utterance is open, if it is long enough to be one. */
    public void flush() {
        if (inSpeech) {
            closeSegment();
        }
        pendingFill = 0;
    }

    /** Forget the room estimate and any partial frame (used when audio focus is regained). */
    public void reset() {
        inSpeech = false;
        segmentFill = 0;
        segmentSamples = 0;
        voicedSamples = 0;
        silenceSamples = 0;
        preRollFill = 0;
        preRollNext = 0;
        pendingFill = 0;
        noiseFloor = params.absoluteFloor;
        framesSeen = 0;
    }

    /** How many utterances have been handed to the sink. Diagnostics only. */
    public long segmentsEmitted() {
        return segmentsEmitted;
    }

    /** The current estimate of the room, for the app's status line. */
    public double noiseFloor() {
        return noiseFloor;
    }

    /** True while the detector believes someone is talking. */
    public boolean inSpeech() {
        return inSpeech;
    }

    private void analyseFrame(short[] frame) {
        double sumSquares = 0.0;
        int crossings = 0;
        int previous = frame[0];
        for (int k = 0; k < frame.length; k++) {
            int sample = frame[k];
            sumSquares += (double) sample * sample;
            if ((sample >= 0) != (previous >= 0)) {
                crossings++;
            }
            previous = sample;
        }
        double rms = Math.sqrt(sumSquares / frame.length);
        double zcr = (double) crossings / frame.length;

        framesSeen++;
        if (framesSeen * (long) params.frameMs <= params.warmupMs) {
            // Calibration, not listening: learn the room before judging it.
            noiseFloor += (rms - noiseFloor) * params.warmupAdaptRate;
            if (noiseFloor < 1.0) {
                noiseFloor = 1.0;
            }
            pushPreRoll(frame);
            return;
        }

        double threshold = Math.max(params.absoluteFloor, noiseFloor * params.noiseMultiplier);
        boolean speech = rms > threshold && zcr <= params.maxZeroCrossingRate;

        if (!speech) {
            // The room, not the person: follow it. Adaptation is only fast on
            // frames the detector already believes are not speech, so a long
            // sentence cannot raise the floor and cut itself off.
            noiseFloor += (rms - noiseFloor) * params.noiseAdaptRate;
        } else {
            // ...and only a crawl while someone is talking.
            noiseFloor += (rms - noiseFloor) * params.noiseAdaptRate * params.speechAdaptRateScale;
        }
        if (noiseFloor < 1.0) {
            noiseFloor = 1.0;
        }

        if (!inSpeech) {
            if (speech) {
                beginSegment();
                appendFrame(frame);
                // Deliberately *not* counting the pre-roll as voiced: 300 ms of
                // pre-speech silence must not, on its own, satisfy minSpeechMs and
                // turn one door slam into an utterance.
                if (inSpeech) {
                    voicedSamples += frameSamples;
                    silenceSamples = 0;
                }
            } else {
                pushPreRoll(frame);
            }
            return;
        }

        appendFrame(frame);
        if (!inSpeech) {
            // appendFrame force-closed the segment at maxSegmentMs; this frame was
            // the first one past the limit and has been dropped.
            return;
        }
        if (speech) {
            silenceSamples = 0;
            voicedSamples += frameSamples;
        } else {
            // Silent frames go into the pre-roll ring as well, so the *next*
            // utterance still gets its 300 ms of running start.
            pushPreRoll(frame);
            silenceSamples += frameSamples;
            if (silenceSamples * 1000L >= (long) params.hangoverMs * sampleRate) {
                closeSegment();
            }
        }
    }

    private void beginSegment() {
        inSpeech = true;
        segmentFill = 0;
        segmentSamples = 0;
        voicedSamples = 0;
        silenceSamples = 0;
        // Oldest first: the ring is written round-robin, so walk it from the next
        // slot to be overwritten.
        for (int n = 0; n < preRollFill; n++) {
            int index = (preRollNext + n) % preRollFrames;
            short[] frame = preRoll[index];
            if (frame != null) {
                appendFrame(frame);
            }
        }
    }

    private void closeSegment() {
        int voicedMs = voicedSamples * 1000 / sampleRate;
        if (voicedMs >= params.minSpeechMs && segmentFill > 0) {
            short[] out = new short[segmentFill];
            System.arraycopy(segment, 0, out, 0, segmentFill);
            segmentsEmitted++;
            sink.onSpeechSegment(out, out.length, sampleRate);
        }
        inSpeech = false;
        segmentFill = 0;
        segmentSamples = 0;
        voicedSamples = 0;
        silenceSamples = 0;
        preRollFill = 0;
        preRollNext = 0;
    }

    private void appendFrame(short[] frame) {
        int maxSamples = params.maxSegmentMs * sampleRate / 1000;
        if (segmentSamples >= maxSamples) {
            // Force-close inside append, then start nothing new: the next frame
            // re-enters through the pre-roll path. Returning here means the caller
            // must not append again for this frame.
            closeSegment();
            return;
        }
        if (segmentFill + frame.length > segment.length) {
            int grown = Math.max(segment.length * 2, segmentFill + frame.length);
            short[] bigger = new short[grown];
            System.arraycopy(segment, 0, bigger, 0, segmentFill);
            segment = bigger;
        }
        System.arraycopy(frame, 0, segment, segmentFill, frame.length);
        segmentFill += frame.length;
        segmentSamples += frame.length;
    }

    private void pushPreRoll(short[] frame) {
        short[] copy = new short[frame.length];
        System.arraycopy(frame, 0, copy, 0, frame.length);
        preRoll[preRollNext] = copy;
        preRollNext = (preRollNext + 1) % preRollFrames;
        if (preRollFill < preRollFrames) {
            preRollFill++;
        }
    }
}
