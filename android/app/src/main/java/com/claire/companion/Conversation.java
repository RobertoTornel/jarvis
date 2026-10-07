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
        for (Item item : itemsFrom(body)) {
            if (!item.text.isEmpty()) {
                replies.add(item.text);
            }
        }
        return replies;
    }

    /** What kind of thing the bridge handed the phone. */
    public enum Kind {
        /** A sentence for Claire to have said. */
        TEXT,
        /** Something the phone is being asked to do. */
        COMMAND,
        /** A shape this app does not know, kept so it can be reported rather than lost. */
        UNKNOWN
    }

    /**
     * One item out of {@code GET /reply}, as a typed thing rather than a string.
     *
     * <p>Before commands existed, everything the bridge returned was a sentence. Now the
     * same drain-once queue carries both "Claire says…" and "Claire asks for…", and the
     * phone has to tell them apart before it renders one as a bubble and acts on the
     * other. That distinction is a {@link Kind}, not a string comparison scattered
     * through the service.
     */
    public static final class Item {
        public final Kind kind;
        public final String text;
        public final String command;
        public final String question;

        Item(Kind kind, String text, String command, String question) {
            this.kind = kind;
            this.text = text;
            this.command = command;
            this.question = question;
        }
    }

    /**
     * Parse a {@code GET /reply} body into typed items.
     *
     * <p>The bridge returns
     * {@code {"ok":true,"items":[{"kind":"text","text":"..."}, {"kind":"command","command":"photo"}]}}.
     * An item whose {@code kind} is missing is a text item if it has text and a command
     * item if it has a command, which is what makes the older bridge (and the older
     * phone app) keep working in both directions. Anything else is reported as an
     * unreadable item rather than dropped: a phone that silently discards Claire's
     * answer is exactly the failure this project refuses.
     */
    public static List<Item> itemsFrom(String body) {
        List<Item> items = new ArrayList<>();
        if (body == null) {
            return items;
        }
        try {
            JSONObject root = new JSONObject(body);
            JSONArray array = root.optJSONArray("items");
            if (array == null) {
                return items;
            }
            for (int i = 0; i < array.length(); i++) {
                JSONObject item = array.optJSONObject(i);
                if (item == null) {
                    String raw = array.optString(i, "").trim();
                    if (!raw.isEmpty()) {
                        items.add(new Item(Kind.TEXT, raw, "", ""));
                    }
                    continue;
                }
                String kind = item.optString("kind", "").trim().toLowerCase(Locale.ROOT);
                String text = item.optString("text", "").trim();
                String command = item.optString("command", "").trim().toLowerCase(Locale.ROOT);
                String question = item.optString("question", "").trim();
                Kind resolved;
                if ("command".equals(kind)) {
                    resolved = Kind.COMMAND;
                } else if ("text".equals(kind)) {
                    resolved = Kind.TEXT;
                } else if (!command.isEmpty()) {
                    resolved = Kind.COMMAND;
                } else if (!text.isEmpty()) {
                    resolved = Kind.TEXT;
                } else {
                    resolved = Kind.UNKNOWN;
                }
                items.add(new Item(resolved,
                        text,
                        command,
                        question));
            }
        } catch (Exception error) {
            items.add(new Item(Kind.UNKNOWN, "reply could not be read: " + error.getMessage(),
                    "", ""));
        }
        return items;
    }
}
