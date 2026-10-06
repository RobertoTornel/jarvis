package com.claire.companion;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * The conversation, as the phone saw it: what the owner said, what Claire said back,
 * what the phone did and what went wrong.
 *
 * <p>Deliberately a plain in-memory list mirrored to a small append-only file
 * ({@code files/conversation.jsonl}, newline-delimited JSON, capped at
 * {@link #MAX_LINES}). No database, no Room, no ORM: the phone is not the place where
 * Claire's memory lives — that is the laptop's dialogue database — and a second
 * source of truth about what was said is exactly the thing that would make the two
 * disagree. This is a view, and it says so.
 *
 * <p>Mutations arrive from wherever the news comes from — the service's upload thread, a
 * broadcast receiver, an activity — and the list is guarded by the instance lock, so
 * that is safe. Listeners are different: they are always called on the main thread, so
 * a view never has to think about which thread it is on. A failure to persist is not
 * swallowed: it becomes a {@link Who#SYSTEM} line, because a phone that silently forgets
 * is worse than one that admits it.
 */
public final class Conversation {

    public enum Who {
        /** The owner, as recognised by Claire. */
        YOU,
        /** Claire, from {@code GET /reply}. */
        CLAIRE,
        /** Something the phone did: a photo sent, a position reported, listening paused. */
        PHONE,
        /** Something went wrong.  Never silent. */
        SYSTEM
    }

    public static final class Line {
        public final long at;
        public final Who who;
        public final String text;

        Line(long at, Who who, String text) {
            this.at = at;
            this.who = who;
            this.text = text;
        }
    }

    /** Called on the main thread after every change. */
    public interface Listener {
        void onConversationChanged();
    }

    private static final String FILE_NAME = "conversation.jsonl";
    private static final int MAX_LINES = 300;

    private static volatile Conversation instance;

    private final File file;
    private final List<Line> lines = new ArrayList<>();
    private final List<Listener> listeners = new ArrayList<>();

    public static Conversation get(android.content.Context context) {
        Conversation local = instance;
        if (local == null) {
            synchronized (Conversation.class) {
                local = instance;
                if (local == null) {
                    local = new Conversation(
                            new File(context.getApplicationContext().getFilesDir(), FILE_NAME));
                    local.load();
                    instance = local;
                }
            }
        }
        return local;
    }

    Conversation(File file) {
        this.file = file;
    }

    public synchronized List<Line> lines() {
        return Collections.unmodifiableList(new ArrayList<>(lines));
    }

    public synchronized int size() {
        return lines.size();
    }

    public void add(Who who, String text) {
        String rendered = text == null ? "" : text.trim();
        if (rendered.isEmpty()) {
            return;
        }
        Line line = new Line(System.currentTimeMillis(), who, rendered);
        synchronized (this) {
            lines.add(line);
            while (lines.size() > MAX_LINES) {
                lines.remove(0);
            }
            append(line);
        }
        notifyListeners();
    }

    public synchronized void clear() {
        lines.clear();
        if (file.exists() && !file.delete()) {
            add(Who.SYSTEM, "could not clear the local conversation file");
            return;
        }
        notifyListeners();
    }

    public void addListener(Listener listener) {
        synchronized (listeners) {
            if (!listeners.contains(listener)) {
                listeners.add(listener);
            }
        }
    }

    public void removeListener(Listener listener) {
        synchronized (listeners) {
            listeners.remove(listener);
        }
    }

    /**
     * Tell every listener, on the main thread.
     *
     * <p>Utterances arrive on the service's audio thread and Claire's replies on the
     * upload thread, and both of those want to update a {@code ListView}. The hop is
     * made here, once, rather than in every caller.
     */
    private void notifyListeners() {
        final List<Listener> snapshot;
        synchronized (listeners) {
            snapshot = new ArrayList<>(listeners);
        }
        if (snapshot.isEmpty()) {
            return;
        }
        Runnable deliver = () -> {
            for (Listener listener : snapshot) {
                listener.onConversationChanged();
            }
        };
        if (android.os.Looper.myLooper() == android.os.Looper.getMainLooper()) {
            deliver.run();
        } else {
            MAIN.post(deliver);
        }
    }

    private static final android.os.Handler MAIN =
            new android.os.Handler(android.os.Looper.getMainLooper());

    private void load() {
        if (!file.isFile()) {
            return;
        }
        List<Line> loaded = new ArrayList<>();
        try (BufferedReader reader = new BufferedReader(new FileReader(file))) {
            String row;
            while ((row = reader.readLine()) != null) {
                if (row.trim().isEmpty()) {
                    continue;
                }
                try {
                    JSONObject object = new JSONObject(row);
                    loaded.add(new Line(
                            object.optLong("at", 0L),
                            Who.valueOf(object.optString("who", "SYSTEM")),
                            object.optString("text", "")));
                } catch (Exception ignored) {
                    // One unreadable line is not worth losing the rest of the file.
                }
            }
        } catch (IOException error) {
            synchronized (this) {
                lines.add(new Line(System.currentTimeMillis(), Who.SYSTEM,
                        "could not read the local conversation: " + error.getMessage()));
            }
            return;
        }
        synchronized (this) {
            lines.addAll(loaded);
            while (lines.size() > MAX_LINES) {
                lines.remove(0);
            }
        }
    }

    private void append(Line line) {
        try (FileOutputStream out = new FileOutputStream(file, true)) {
            // org.json on Android declares JSONException on put(); on the desktop JVM
            // it does not.  The values here are a long, an enum name and a String, so
            // it cannot actually be thrown -- but it is caught rather than declared so
            // this file compiles against both.
            JSONObject object = new JSONObject();
            try {
                object.put("at", line.at);
                object.put("who", line.who.name());
                object.put("text", line.text);
            } catch (org.json.JSONException impossible) {
                return;
            }
            out.write(object.toString().getBytes(StandardCharsets.UTF_8));
            out.write('\n');
            out.getFD().sync();
        } catch (IOException error) {
            // Reported, never swallowed -- but only once, or a full disk would
            // fill the view with the same sentence.
            String note = "could not save the conversation locally: " + error.getMessage();
            boolean alreadySaid = false;
            for (int i = lines.size() - 1; i >= 0 && i >= lines.size() - 3; i--) {
                if (note.equals(lines.get(i).text)) {
                    alreadySaid = true;
                    break;
                }
            }
            if (!alreadySaid) {
                lines.add(new Line(System.currentTimeMillis(), Who.SYSTEM, note));
            }
        }
    }

    /** A short, human rendering of a time, for the list rows. */
    public static String clock(long millis) {
        return new java.text.SimpleDateFormat("HH:mm:ss", Locale.ROOT)
                .format(new java.util.Date(millis));
    }

    /**
     * Read the reply items out of a {@code GET /reply} body.
     *
     * <p>The bridge returns {@code {"ok":true,"items":[{"kind":"text","text":"..."}]}}.
     * Anything else — a shape this app has not seen, an item without text — is
     * reported as a system line rather than rendered as an empty bubble, because a
     * phone that quietly drops Claire's answer is the "silent failure" this project
     * refuses.
     */
    public static List<String> repliesFrom(String body) {
        List<String> replies = new ArrayList<>();
        try {
            JSONObject root = new JSONObject(body);
            JSONArray items = root.optJSONArray("items");
            if (items == null) {
                return replies;
            }
            for (int i = 0; i < items.length(); i++) {
                JSONObject item = items.optJSONObject(i);
                if (item == null) {
                    String raw = items.optString(i, "").trim();
                    if (!raw.isEmpty()) {
                        replies.add(raw);
                    }
                    continue;
                }
                String text = item.optString("text", "").trim();
                if (!text.isEmpty()) {
                    replies.add(text);
                }
            }
        } catch (Exception error) {
            replies.add("reply could not be read: " + error.getMessage());
        }
        return replies;
    }
}
