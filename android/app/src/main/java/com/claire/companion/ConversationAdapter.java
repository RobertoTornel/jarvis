package com.claire.companion;

import android.content.Context;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.recyclerview.widget.RecyclerView;

import com.google.android.material.card.MaterialCardView;

import java.util.ArrayList;
import java.util.List;

/**
 * The conversation, as bubbles.
 *
 * <p>A {@link RecyclerView.Adapter} now rather than the {@code BaseAdapter} this used to
 * be, because the rows are no longer one TextView each: a bubble, a who-label and a
 * timestamp, laid out differently depending on who is talking. Recycling is what keeps
 * that cheap, and the list is already capped at 300 lines by {@link Conversation}.
 *
 * <p>Three visual shapes, and the shape is the information:
 *
 * <ul>
 *   <li><b>The owner</b> — a blue-green bubble on the right, because it is the thing the
 *       person reading it said.
 *   <li><b>Claire</b> — a teal bubble on the left.
 *   <li><b>The phone and its problems</b> — a full-width, dim, labelled row. A photo
 *       being sent or a token being refused is not something either person said, and
 *       drawing it as a speech bubble would be a small lie about who is talking.
 * </ul>
 *
 * <p><b>Long-press copies.</b> That is the one interaction every chat app has and this
 * one did not: press and hold a message and its text goes to the clipboard. It works on
 * both the owner's messages and Claire's, and the caller gets
 * {@link OnCopyListener#onCopied} so it can say so in a snackbar — a copy with no
 * confirmation is indistinguishable from a long press that did nothing.
 */
public final class ConversationAdapter extends RecyclerView.Adapter<ConversationAdapter.Row> {

    /** Called after a message has been copied, so the screen can say so. */
    public interface OnCopyListener {
        void onCopied(String text);
    }

    private final LayoutInflater inflater;
    private final Context context;
    private final List<Conversation.Line> lines = new ArrayList<>();
    private final OnCopyListener copyListener;

    public ConversationAdapter(Context context, OnCopyListener copyListener) {
        this.context = context;
        this.inflater = LayoutInflater.from(context);
        this.copyListener = copyListener;
    }

    /** Replace the rows.  Called on the main thread only. */
    public void submit(List<Conversation.Line> next) {
        lines.clear();
        lines.addAll(next);
        notifyDataSetChanged();
    }

    public int size() {
        return lines.size();
    }

    @NonNull
    @Override
    public Row onCreateViewHolder(@NonNull ViewGroup parent, int viewType) {
        View view = inflater.inflate(R.layout.item_conversation, parent, false);
        return new Row(view);
    }

    @Override
    public void onBindViewHolder(@NonNull Row row, int position) {
        Conversation.Line line = lines.get(position);
        row.bind(line);
    }

    @Override
    public int getItemCount() {
        return lines.size();
    }

    final class Row extends RecyclerView.ViewHolder {

        private final TextView who;
        private final TextView text;
        private final TextView meta;
        private final MaterialCardView bubble;
        private final View root;

        Row(@NonNull View itemView) {
            super(itemView);
            this.root = itemView;
            this.who = itemView.findViewById(R.id.who);
            this.text = itemView.findViewById(R.id.text);
            this.meta = itemView.findViewById(R.id.meta);
            this.bubble = itemView.findViewById(R.id.bubble);
        }

        void bind(Conversation.Line line) {
            text.setText(line.text);
            meta.setText(Conversation.clock(line.at));

            int colour;
            int bubbleColour;
            int gravity;
            String label;
            switch (line.who) {
                case YOU:
                    label = context.getString(R.string.who_you);
                    colour = context.getColor(R.color.you);
                    bubbleColour = context.getColor(R.color.bubble_you);
                    gravity = Gravity.END;
                    break;
                case CLAIRE:
                    label = context.getString(R.string.who_claire);
                    colour = context.getColor(R.color.claire);
                    bubbleColour = context.getColor(R.color.bubble_claire);
                    gravity = Gravity.START;
                    break;
                case SYSTEM:
                    label = context.getString(R.string.who_system);
                    colour = context.getColor(R.color.warn);
                    bubbleColour = context.getColor(R.color.bubble_meta);
                    gravity = Gravity.START;
                    break;
                default:
                    label = context.getString(R.string.who_phone);
                    colour = context.getColor(R.color.text_dim);
                    bubbleColour = context.getColor(R.color.bubble_meta);
                    gravity = Gravity.START;
                    break;
            }

            who.setText(label + "  " + Conversation.clock(line.at));
            who.setTextColor(colour);

            // The label and the bubble sit on the same side as the speaker.  Gravity on
            // the LinearLayout child is what does it; the adapter never sets padding to
            // fake an alignment.
            align(who, gravity);
            align(bubble, gravity);
            bubble.setCardBackgroundColor(bubbleColour);
            meta.setVisibility(View.GONE);

            if (line.who == Conversation.Who.SYSTEM || line.who == Conversation.Who.PHONE) {
                bubble.setLayoutParams(fullWidth());
            } else {
                bubble.setLayoutParams(wrapContent());
            }

            final String copyable = line.text == null ? "" : line.text;
            bubble.setOnLongClickListener(view -> {
                copy(copyable);
                return true;
            });
            // A tap deliberately does nothing.  The row is clickable so the ripple
            // promises the long press; a tap that opened a menu would make the gesture
            // that is supposed to be invisible require a decision.
            bubble.setOnClickListener(null);
        }

        private void copy(String value) {
            if (value.trim().isEmpty()) {
                if (copyListener != null) {
                    copyListener.onCopied("");
                }
                return;
            }
            android.content.ClipboardManager clipboard =
                    (android.content.ClipboardManager) context.getSystemService(
                            Context.CLIPBOARD_SERVICE);
            if (clipboard != null) {
                clipboard.setPrimaryClip(
                        android.content.ClipData.newPlainText("Claire", value));
            }
            if (copyListener != null) {
                copyListener.onCopied(value);
            }
        }

        private void align(View view, int gravity) {
            ViewGroup.LayoutParams params = view.getLayoutParams();
            if (params instanceof LinearLayout.LayoutParams) {
                ((LinearLayout.LayoutParams) params).gravity = gravity;
            }
        }

        private LinearLayout.LayoutParams fullWidth() {
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            params.gravity = Gravity.START;
            return params;
        }

        private LinearLayout.LayoutParams wrapContent() {
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            params.gravity = Gravity.START;
            return params;
        }
    }
}
