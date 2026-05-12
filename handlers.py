from __future__ import annotations

import html
import logging

from telegram import ReactionTypeEmoji, Update
from telegram.constants import ChatType
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from parser import parse_entry

log = logging.getLogger(__name__)

DEST, CURRENCY, JOINERS = range(3)


HELP_TEXT = (
    "Trip expense tracker bot.\n\n"
    "Commands:\n"
    "  /init     -- set up a new trip (group admin only)\n"
    "  /info     -- show current trip details\n"
    "  /ledger   -- show the expense ledger\n"
    "  /balance  -- show net balances per person\n"
    "  /debt <name> -- show entries where <name> is a debtor + totals owed per payer\n"
    "  /tally    -- net all debts pairwise (final 'who pays whom' settlement)\n"
    "  /undo     -- remove the most recent entry\n"
    "  /cancel   -- cancel the current /init flow\n"
    "  /help     -- this help\n\n"
    "Recording expenses (just type, no command):\n"
    "  derek to jy 100              -- derek owes jy 100\n"
    "  everyone give jy 30          -- everyone (except jy) owes jy 30 each\n"
    "  derek and zw give jy 100     -- derek and zw each owe jy 100\n"
)


# ---------- helpers ----------

async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    admins = await context.bot.get_chat_administrators(chat.id)
    return any(a.user.id == update.effective_user.id for a in admins)


def _trip_summary(trip: dict) -> str:
    return (
        f"Current trip: {trip['destination']}\n"
        f"Default Currency: {trip['currency']}\n"
        f"Joiners: {', '.join(trip['joiners'])}"
    )


# ---------- basic commands ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trip = db.get_trip(update.effective_chat.id)
    if not trip:
        await update.message.reply_text("No active trip in this chat. Use /init to start one.")
        return
    await update.message.reply_text(_trip_summary(trip))


# ---------- /init conversation ----------

async def init_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only group admins can run /init.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "Let's set up a new trip.\n\nWhere are you travelling to? (e.g. Japan)\n"
        "Send /cancel at any time to abort."
    )
    return DEST


async def init_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["destination"] = update.message.text.strip()
    await update.message.reply_text("Default currency? (e.g. JPY, EUR, USD)")
    return CURRENCY


async def init_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["currency"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "Who's joining? Send comma-separated names.\n"
        "Example: Derek, zw, jy, edw, eeshan"
    )
    return JOINERS


async def init_joiners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if len(names) < 2:
        await update.message.reply_text(
            "Need at least 2 joiners (comma-separated). Try again, or /cancel."
        )
        return JOINERS
    # Reject duplicates (case-insensitive)
    lowered = [n.lower() for n in names]
    if len(set(lowered)) != len(lowered):
        await update.message.reply_text("Duplicate names detected. Try again, or /cancel.")
        return JOINERS
    # Names must match the parser's identifier rules
    import re
    bad = [n for n in names if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n)]
    if bad:
        await update.message.reply_text(
            f"These names contain unsupported characters: {', '.join(bad)}.\n"
            "Use letters/digits/underscores only, starting with a letter. Try again, or /cancel."
        )
        return JOINERS

    chat_id = update.effective_chat.id
    trip = {
        "destination": context.user_data["destination"],
        "currency": context.user_data["currency"],
        "joiners": names,
    }
    summary = _trip_summary(trip)

    msg = await update.message.reply_text(summary)
    pinned_id = None
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        pinned_id = msg.message_id
    except Exception as e:
        log.warning("could not pin trip message: %s", e)
        await update.message.reply_text(
            "(Couldn't pin the summary -- give me 'Pin Messages' permission and re-run /init to pin it.)"
        )

    db.save_trip(chat_id, trip["destination"], trip["currency"], names, pinned_id)
    await update.message.reply_text(
        "Trip initialized! Start recording expenses by typing things like:\n"
        "  derek to jy 100\n"
        "  everyone give jy 30\n"
        "  derek and zw give jy 100"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def init_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Init cancelled.")
    return ConversationHandler.END


# ---------- expense recording ----------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        return

    parsed = parse_entry(update.message.text, trip["joiners"])
    if parsed is None:
        return
    if "error" in parsed:
        await update.message.reply_text(parsed["error"])
        return

    db.add_entry(
        chat_id,
        parsed["payer"],
        parsed["debtors"],
        parsed["amount_per_debtor"],
        update.message.text,
    )
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=update.message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👍")],
        )
    except Exception as e:
        # Reactions can be disabled in the chat, or 👍 may not be in the
        # allowed-reactions list for this supergroup. Fall back to a short ack
        # so the user still knows the entry was saved.
        log.warning("could not set reaction: %s", e)
        await update.message.reply_text("Recorded.")


# ---------- ledger / balance / undo ----------

async def cmd_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        await update.message.reply_text("No active trip. Use /init.")
        return
    entries = db.list_entries(chat_id, limit=200)
    if not entries:
        await update.message.reply_text("No entries yet.")
        return

    header = f"{'Date':<10}  {'Amt':>8}  {'Payer':<10}  Debtors"
    sep = "-" * len(header)
    lines = [header, sep]
    for e in entries:
        date = e["created_at"][:10]
        total = e["amount_per_debtor"] * len(e["debtors"])
        debtors = ", ".join(e["debtors"])
        lines.append(f"{date}  {total:>8.2f}  {e['payer']:<10}  {debtors}")

    body = html.escape("\n".join(lines))
    text = f"<b>Ledger ({trip['currency']})</b>\n<pre>{body}</pre>"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        await update.message.reply_text("No active trip. Use /init.")
        return
    entries = db.list_entries(chat_id, limit=100000)
    net = {name: 0.0 for name in trip["joiners"]}
    for e in entries:
        per = e["amount_per_debtor"]
        if e["payer"] in net:
            net[e["payer"]] += per * len(e["debtors"])
        for d in e["debtors"]:
            if d in net:
                net[d] -= per

    lines = [f"Net balances ({trip['currency']}):", ""]
    for name, bal in net.items():
        if abs(bal) < 0.005:
            lines.append(f"  {name:<10}  settled")
        elif bal > 0:
            lines.append(f"  {name:<10}  +{bal:.2f}  (others owe them)")
        else:
            lines.append(f"  {name:<10}  {bal:.2f}  (they owe others)")
    await update.message.reply_text("\n".join(lines))


async def cmd_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        await update.message.reply_text("No active trip. Use /init.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /debt <name>\n"
            "Shows entries where <name> is a debtor and totals owed per payer.\n"
            f"Joiners: {', '.join(trip['joiners'])}"
        )
        return

    raw = context.args[0]
    joiner_map = {j.lower(): j for j in trip["joiners"]}
    name = joiner_map.get(raw.lower())
    if not name:
        await update.message.reply_text(
            f"Unknown name '{raw}'. Joiners: {', '.join(trip['joiners'])}"
        )
        return

    entries = db.list_entries(chat_id, limit=100000)
    matching = [e for e in entries if name in e["debtors"]]
    if not matching:
        await update.message.reply_text(f"{name} doesn't owe anyone. ")
        return

    cur = trip["currency"]
    header = f"{'Date':<10}  {'Amt':>8}  {'Payer':<10}  Debtors"
    sep = "-" * len(header)
    table_lines = [header, sep]
    totals_by_payer: dict[str, float] = {}
    for e in matching:
        date = e["created_at"][:10]
        per = e["amount_per_debtor"]
        table_lines.append(f"{date}  {per:>8.2f}  {e['payer']:<10}  {name}")
        totals_by_payer[e["payer"]] = totals_by_payer.get(e["payer"], 0.0) + per

    summary_lines = []
    grand_total = 0.0
    for payer, amt in totals_by_payer.items():
        summary_lines.append(f"{name} -> {payer} = {amt:.2f} {cur}")
        grand_total += amt
    summary_lines.append(f"Total: {grand_total:.2f} {cur}")

    body = html.escape("\n".join([*table_lines, "", *summary_lines]))
    text = (
        f"<b>{html.escape(name)}'s debts ({cur})</b>\n"
        f"<pre>{body}</pre>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_tally(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        await update.message.reply_text("No active trip. Use /init.")
        return
    entries = db.list_entries(chat_id, limit=100000)
    if not entries:
        await update.message.reply_text("No entries yet.")
        return

    # Gross debts: (debtor, payer) -> total amount debtor owes payer
    gross: dict[tuple[str, str], float] = {}
    for e in entries:
        per = e["amount_per_debtor"]
        for d in e["debtors"]:
            key = (d, e["payer"])
            gross[key] = gross.get(key, 0.0) + per

    # Pairwise netting: for each unordered pair {A, B}, keep only the
    # surviving direction with the larger total.
    net: dict[tuple[str, str], float] = {}
    seen: set[tuple[str, str]] = set()
    for (a, b), amt in gross.items():
        if (a, b) in seen:
            continue
        opp = gross.get((b, a), 0.0)
        diff = round(amt - opp, 2)
        if diff > 0:
            net[(a, b)] = diff
        elif diff < 0:
            net[(b, a)] = -diff
        # diff == 0 -> debts cancel exactly, omit
        seen.add((a, b))
        seen.add((b, a))

    if not net:
        await update.message.reply_text("All debts cancel out -- everyone's settled.")
        return

    cur = trip["currency"]
    lines = [f"{debtor} -> {payer} = {amt:.2f} {cur}"
             for (debtor, payer), amt in sorted(net.items())]
    body = html.escape("\n".join(lines))
    text = f"<b>Net debts ({cur})</b>\n<pre>{body}</pre>"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not db.get_trip(chat_id):
        await update.message.reply_text("No active trip.")
        return
    removed = db.delete_last_entry(chat_id)
    if not removed:
        await update.message.reply_text("Nothing to undo.")
        return
    await update.message.reply_text(f"Removed last entry: {removed['raw_text']}")


# ---------- registration ----------

def build_init_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("init", init_entry)],
        states={
            DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_dest)],
            CURRENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_currency)],
            JOINERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_joiners)],
        },
        fallbacks=[CommandHandler("cancel", init_cancel)],
        name="init_trip",
        persistent=False,
    )
