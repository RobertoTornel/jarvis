package com.claire.companion;

import android.content.Context;
import android.graphics.Color;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/**
 * The conversation list.
 *
 * <p>A {@link BaseAdapter} over a plain {@link List}, not a RecyclerView with a diffing
 * adapter. The list is capped at a few hundred rows, every row is one TextView and the
 * whole thing is replaced on each change, and a device that has to be woken by
 * DiffUtil to render four messages is a device that ships later and does less. The
 * reason a RecyclerView would be right — many thousands of rows, view recycling under
 * scroll pressure — is the reason this list is not allowed to grow.
 *
 * <p>Who said it is carried by the label and the colour, so the shape of the screen
 * survives being glanced at rather than read.
 */
public final class ConversationAdapter extends BaseAdapter {

    private final LayoutInflater inflater;
    private final int colorYou;
    private final int colorClaire;
    private final int colorPhone;
    private final int colorSystem;
    private List<Conversation.Line> lines = new ArrayList<>();

    public ConversationAdapter(Context context) {
        this.inflater = LayoutInflater.from(context);
        this.colorYou = context.getColor(R.color.you);
        this.colorClaire = context.getColor(R.color.claire);
        this.colorPhone = context.getColor(R.color.text_dim);
        this.colorSystem = context.getColor(R.color.warn);
    }

    /** Replace the rows.  Called on the main thread only. */
    public void submit(List<Conversation.Line> updated) {
        this.lines = new ArrayList<>(updated);
        notifyDataSetChanged();
    }

    @Override
    public int getCount() {
        return lines.size();
    }

    @Override
    public Object getItem(int position) {
        return lines.get(position);
    }

    @Override
    public long getItemId(int position) {
        return position;
    }

    @Override
    public View getView(int position, View convertView, ViewGroup parent) {
        View row = convertView;
        if (row == null) {
            row = inflater.inflate(R.layout.item_conversation, parent, false);
        }
        Conversation.Line line = lines.get(position);
        TextView meta = row.findViewById(R.id.line_meta);
        TextView text = row.findViewById(R.id.line_text);
        meta.setText(label(line.who) + "  ·  " + Conversation.clock(line.at));
        meta.setTextColor(colorOf(line.who));
        text.setText(line.text);
        text.setTextColor(line.who == Conversation.Who.SYSTEM ? colorSystem : Color.WHITE);
        return row;
    }

    private int colorOf(Conversation.Who who) {
        switch (who) {
            case YOU:
                return colorYou;
            case CLAIRE:
                return colorClaire;
            case PHONE:
                return colorPhone;
            default:
                return colorSystem;
        }
    }

    private static String label(Conversation.Who who) {
        switch (who) {
            case YOU:
                return "You";
            case CLAIRE:
                return "Claire";
            case PHONE:
                return "Phone";
            default:
                return "Note";
        }
    }
}
