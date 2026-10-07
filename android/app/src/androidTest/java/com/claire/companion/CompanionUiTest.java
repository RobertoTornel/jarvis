package com.claire.companion;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ClipboardManager;
import android.content.Context;
import android.view.ContextThemeWrapper;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.TextView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * The long-press copy, exercised through real Android views and the real clipboard.
 *
 * <p>Why this exists at all: MIUI refuses {@code adb shell input} outright — <i>Injecting
 * input events requires … INJECT_EVENTS</i> — so the one gesture the owner asked for
 * cannot be performed from a terminal on this phone. A test running inside the app's own
 * process is the only way to observe it rather than assert it on faith.
 *
 * <p>Why it drives {@link ConversationAdapter} directly instead of Espresso: with runtime
 * permissions ungranted (and this phone will not let {@code adb} grant them), launching
 * {@link MainActivity} immediately raises the system permission dialog, which covers the
 * app's window and makes Espresso's window-focus checks unreliable. The adapter is where
 * the gesture actually lives — {@code setOnLongClickListener} on the bubble — and
 * {@code performLongClick()} invokes exactly the listener a finger would, so this
 * measures the wiring the owner cares about: press and hold a bubble, its text is on the
 * clipboard, and the screen is told so.
 *
 * <p>{@link #settingsScreenLasts()} is not an assertion: it leaves Settings on screen so
 * {@code adb exec-out screencap} can photograph it from outside, which is how the layout
 * was checked visually.
 */
@RunWith(AndroidJUnit4.class)
public class CompanionUiTest {

    private Context target() {
        return InstrumentationRegistry.getInstrumentation().getTargetContext();
    }

    /** A context that can inflate Material components (they read the app theme). */
    private Context themed() {
        return new ContextThemeWrapper(target(), R.style.AppTheme);
    }

    /**
     * Bind one line into a real row view, exactly as the RecyclerView would.
     *
     * @return the bubble, whose long-press listener is the thing under test
     */
    private View bindRow(Context context, ConversationAdapter adapter, ViewGroup parent,
                         Conversation.Line line) {
        ConversationAdapter.Row row = adapter.onCreateViewHolder(parent, 0);
        adapter.onBindViewHolder(row, 0);
        TextView text = row.itemView.findViewById(R.id.text);
        assertEquals("the adapter did not bind the line's text",
                line.text, text.getText().toString());
        return row.itemView.findViewById(R.id.bubble);
    }

    private ConversationAdapter adapterWith(List<Conversation.Line> lines,
                                            ConversationAdapter.OnCopyListener listener) {
        ConversationAdapter adapter = new ConversationAdapter(themed(), listener);
        adapter.submit(lines);
        return adapter;
    }

    @Test
    public void longPressOnTheOwnersMessageCopiesIt() {
        String line = "long-press copy check \u2014 the owner's own words";
        final List<String> copiedTo = new ArrayList<>();
        try (ActivityScenario<SettingsActivity> host =
                     ActivityScenario.launch(SettingsActivity.class)) {
            ConversationAdapter adapter = adapterWith(
                    Arrays.asList(new Conversation.Line(System.currentTimeMillis(),
                            Conversation.Who.YOU, line)),
                    copiedTo::add);
            View bubble = bindRow(themed(), adapter, new FrameLayout(themed()), line(line));
            assertTrue("the bubble is not long-clickable", bubble.isLongClickable());
            assertTrue("the long press was not consumed", bubble.performLongClick());
            assertEquals("the screen was not told to say 'copied'",
                    Arrays.asList(line), copiedTo);
            // Read while this process is the focused, resumed app: since Android 10 an
            // unfocused app cannot read the clipboard at all, which is why this test
            // needs a host activity even though it never touches that activity's views.
            assertEquals(line, clipboardText());
        }
    }

    @Test
    public void longPressOnClairesReplyCopiesItToo() {
        String line = "long-press copy check \u2014 Claire's side";
        final List<String> copiedTo = new ArrayList<>();
        try (ActivityScenario<SettingsActivity> host =
                     ActivityScenario.launch(SettingsActivity.class)) {
            ConversationAdapter adapter = adapterWith(
                    Arrays.asList(new Conversation.Line(System.currentTimeMillis(),
                            Conversation.Who.CLAIRE, line)),
                    copiedTo::add);
            View bubble = bindRow(themed(), adapter, new FrameLayout(themed()), line(line));
            assertTrue(bubble.performLongClick());
            assertEquals(Arrays.asList(line), copiedTo);
            assertEquals(line, clipboardText());
        }
    }

    /**
     * The phone's own lines copy as well, and a line with nothing in it reports failure
     * instead of claiming a copy that did not happen.
     */
    @Test
    public void thePhonesOwnLinesCopyAndBlankLinesReportFailure() {
        final List<String> reported = new ArrayList<>();
        try (ActivityScenario<SettingsActivity> host =
                     ActivityScenario.launch(SettingsActivity.class)) {
            ConversationAdapter adapter = adapterWith(
                    Arrays.asList(new Conversation.Line(System.currentTimeMillis(),
                            Conversation.Who.PHONE, "sent to Claire")),
                    reported::add);
            View bubble = bindRow(themed(), adapter, new FrameLayout(themed()),
                    line("sent to Claire"));
            assertTrue(bubble.performLongClick());
            assertEquals("sent to Claire", clipboardText());
            assertEquals(Arrays.asList("sent to Claire"), reported);

            // A blank line: empty text must not be reported as a successful copy.
            ConversationAdapter blank = adapterWith(
                    Arrays.asList(new Conversation.Line(System.currentTimeMillis(),
                            Conversation.Who.SYSTEM, "   ")),
                    reported::add);
            View blankBubble = bindRow(themed(), blank, new FrameLayout(themed()), line("   "));
            assertTrue(blankBubble.performLongClick());
            assertEquals(Arrays.asList("sent to Claire", ""), reported);
        }
    }

    private String clipboardText() {
        ClipboardManager clipboard = (ClipboardManager)
                target().getSystemService(Context.CLIPBOARD_SERVICE);
        assertNotNull("no clipboard service", clipboard);
        assertNotNull("nothing was put on the clipboard", clipboard.getPrimaryClip());
        return String.valueOf(clipboard.getPrimaryClip().getItemAt(0).getText());
    }

    private static Conversation.Line line(String text) {
        return new Conversation.Line(System.currentTimeMillis(), Conversation.Who.YOU, text);
    }

    /**
     * Leave Settings on screen, so it can be photographed from the host.
     *
     * <p>Not an assertion — a window. Start this class, then run
     * {@code adb exec-out screencap -p} while it is open.
     */
    @Test
    public void settingsScreenLasts() throws Exception {
        try (ActivityScenario<SettingsActivity> scenario =
                     ActivityScenario.launch(SettingsActivity.class)) {
            Thread.sleep(30_000L);
        }
    }
}
