from __future__ import annotations

import html
import logging
import re

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

DEST, CURRENCY, NUM_JOINERS, JOINER_ALIAS, JOINER_HANDLE = range(5)

ALIAS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# Telegram username: 5-32 chars, must start with a letter, letters/digits/underscores only.
HANDLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
ME_TOKEN_RE = re.compile(r"\bme\b", re.IGNORECASE)

# Aliases that would collide with the parser grammar or sentinel words.
RESERVED_ALIASES = {"me", "everyone", "to", "and", "give", "gives", "i"}

# Phrases that mean "no Telegram handle for this joiner".
NO_HANDLE_TOKENS = {"-", "skip", "none", "no", "na", "n/a"}


COMMANDS_BLOCK = (
    "Commands:\n"
    "  /init     -- set up a new trip (group admin only)\n"
    "  /info     -- show current trip details\n"
    "  /ledger   -- show the expense ledger\n"
    "  /balance  -- show net balances per person\n"
    "  /debt <name> -- show entries where <name> is a debtor + totals owed per payer\n"
    "  /tally    -- net all debts pairwise (final 'who pays whom' settlement)\n"
    "  /undo     -- remove the most recent entry\n"
    "  /end      -- finalize the trip and wipe its data (admin only)\n"
    "  /cancel   -- cancel the current /init flow\n"
)

EXAMPLES_BLOCK = (
    "Examples:\n"
    "  derek to jy 100\n"
    "  everyone to me 30\n"
    "  derek to jy usd 100\n"
)


# ---------- helpers ----------

async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return True
    admins = await context.bot.get_chat_administrators(chat.id)
    return any(a.user.id == update.effective_user.id for a in admins)


def _alias_for_sender(trip: dict, user) -> str | None:
    """Look up the sender's joiner alias via their Telegram @username."""
    if not user or not user.username:
        return None
    return trip.get("joiner_handles", {}).get(user.username.lower())


def _format_joiners(joiners: list[str], handles: dict[str, str]) -> str:
    """Render the joiners line. Handles is {handle_lower: alias}."""
    alias_to_handle: dict[str, str] = {}
    for h, a in handles.items():
        alias_to_handle.setdefault(a.lower(), h)
    parts = []
    for alias in joiners:
        h = alias_to_handle.get(alias.lower())
        if h:
            parts.append(f"{html.escape(alias)} (<code>@{html.escape(h)}</code>)")
        else:
            parts.append(html.escape(alias))
    return ", ".join(parts)


def _build_pin_text(destination: str, currency: str,
                    joiners: list[str], handles: dict[str, str]) -> str:
    joiners_line = _format_joiners(joiners, handles)
    return (
        f"Current trip: {html.escape(destination)}\n"
        f"Default Currency: {html.escape(currency)}\n"
        f"Joiners: {joiners_line}\n\n"
        f"{html.escape(COMMANDS_BLOCK)}\n"
        f"{html.escape(EXAMPLES_BLOCK)}"
    )


# ---------- basic commands ----------

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trip = db.get_trip(update.effective_chat.id)
    if not trip:
        await update.message.reply_text("No active trip in this chat. Use /init to start one.")
        return
    text = _build_pin_text(
        trip["destination"], trip["currency"], trip["joiners"], trip["joiner_handles"]
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------- /init conversation ----------

async def init_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only group admins can run /init.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["joiners_list"] = []
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
    await update.message.reply_text("How many joiners?")
    return NUM_JOINERS


async def init_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.isdigit():
        await update.message.reply_text("Send a positive whole number, or /cancel.")
        return NUM_JOINERS
    n = int(raw)
    if n < 2:
        await update.message.reply_text("Need at least 2 joiners. Try again, or /cancel.")
        return NUM_JOINERS
    if n > 50:
        await update.message.reply_text("That's a lot. Cap is 50. Try again, or /cancel.")
        return NUM_JOINERS
    context.user_data["num_joiners"] = n
    await update.message.reply_text("Joiner 1 -- alias? (e.g. Derek)")
    return JOINER_ALIAS


async def init_joiner_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias = update.message.text.strip()
    if not ALIAS_RE.fullmatch(alias):
        await update.message.reply_text(
            "Alias must start with a letter and contain only letters/digits/underscores. Try again."
        )
        return JOINER_ALIAS
    if alias.lower() in RESERVED_ALIASES:
        await update.message.reply_text(
            f"'{alias}' is reserved (collides with bot grammar). Pick a different alias."
        )
        return JOINER_ALIAS
    existing = {j["alias"].lower() for j in context.user_data["joiners_list"]}
    if alias.lower() in existing:
        await update.message.reply_text("That alias is already used. Pick a different one.")
        return JOINER_ALIAS

    context.user_data["pending_alias"] = alias
    idx = len(context.user_data["joiners_list"]) + 1
    await update.message.reply_text(
        f"Joiner {idx} -- Telegram handle for {alias}? (e.g. @derek2403, or '-' to skip)"
    )
    return JOINER_HANDLE


async def init_joiner_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    handle: str | None = None
    if raw.lower() not in NO_HANDLE_TOKENS:
        h = raw.lstrip("@")
        if not HANDLE_RE.fullmatch(h):
            await update.message.reply_text(
                "Handle must be 5-32 characters, start with a letter, "
                "and contain only letters/digits/underscores. Try again, or '-' to skip."
            )
            return JOINER_HANDLE
        handle = h.lower()
        existing = {
            j["handle"] for j in context.user_data["joiners_list"] if j["handle"]
        }
        if handle in existing:
            await update.message.reply_text(
                "That handle is already used by another joiner. Try again, or '-' to skip."
            )
            return JOINER_HANDLE

    alias = context.user_data.pop("pending_alias")
    context.user_data["joiners_list"].append({"alias": alias, "handle": handle})

    if len(context.user_data["joiners_list"]) < context.user_data["num_joiners"]:
        idx = len(context.user_data["joiners_list"]) + 1
        await update.message.reply_text(f"Joiner {idx} -- alias?")
        return JOINER_ALIAS

    return await _finalize_init(update, context)


async def _finalize_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    dest = context.user_data["destination"]
    currency = context.user_data["currency"]
    joiners_list = context.user_data["joiners_list"]

    aliases = [j["alias"] for j in joiners_list]
    handles_dict = {j["handle"]: j["alias"] for j in joiners_list if j["handle"]}

    pinned_text = _build_pin_text(dest, currency, aliases, handles_dict)
    msg = await update.message.reply_text(pinned_text, parse_mode="HTML")
    pinned_id = None
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        pinned_id = msg.message_id
    except Exception as e:
        log.warning("could not pin trip message: %s", e)
        await update.message.reply_text(
            "(Couldn't pin -- give me 'Pin Messages' permission and re-run /init to pin it.)"
        )

    db.save_trip(chat_id, dest, currency, aliases, handles_dict, pinned_id)
    await update.message.reply_text("Trip initialized!")
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

    raw_text = update.message.text
    sender_alias = _alias_for_sender(trip, update.effective_user)

    text = raw_text
    has_me = bool(ME_TOKEN_RE.search(text))
    if has_me and sender_alias:
        text = ME_TOKEN_RE.sub(sender_alias, text)

    parsed = parse_entry(text, trip["joiners"])
    if parsed is None:
        return
    if "error" in parsed:
        if has_me and not sender_alias:
            await update.message.reply_text(
                "I don't know your alias -- 'me' only works if your Telegram "
                "handle was registered in /init. Type your alias directly, "
                "or ask an admin to re-/init with your @handle."
            )
            return
        await update.message.reply_text(parsed["error"])
        return

    # Dedupe debtors -- can happen when "me" substitutes to a name already
    # listed (e.g. "derek and me give jy 100" where the sender is Derek).
    parsed["debtors"] = list(dict.fromkeys(parsed["debtors"]))

    currency = parsed.get("currency") or trip["currency"]
    db.add_entry(
        chat_id,
        parsed["payer"],
        parsed["debtors"],
        parsed["amount_per_debtor"],
        currency,
        raw_text,
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

def _group_by_currency(entries: list[dict], default_currency: str) -> dict[str, list[dict]]:
    """Bucket entries by their currency (falling back to the trip default)."""
    by_cur: dict[str, list[dict]] = {}
    for e in entries:
        cur = (e.get("currency") or default_currency).upper()
        by_cur.setdefault(cur, []).append(e)
    return by_cur


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

    by_cur = _group_by_currency(entries, trip["currency"])
    header = f"{'Date':<10}  {'Amt':>10}  {'Payer':<10}  Debtors"
    sep = "-" * len(header)

    blocks = []
    for cur in sorted(by_cur.keys()):
        lines = [f"[{cur}]", header, sep]
        for e in by_cur[cur]:
            date = e["created_at"][:10]
            total = e["amount_per_debtor"] * len(e["debtors"])
            debtors = ", ".join(e["debtors"])
            lines.append(f"{date}  {total:>10.2f}  {e['payer']:<10}  {debtors}")
        blocks.append("\n".join(lines))

    body = html.escape("\n\n".join(blocks))
    text = f"<b>Ledger</b>\n<pre>{body}</pre>"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        await update.message.reply_text("No active trip. Use /init.")
        return
    entries = db.list_entries(chat_id, limit=100000)
    if not entries:
        await update.message.reply_text("No entries yet.")
        return

    by_cur = _group_by_currency(entries, trip["currency"])
    blocks = []
    for cur in sorted(by_cur.keys()):
        net = {name: 0.0 for name in trip["joiners"]}
        for e in by_cur[cur]:
            per = e["amount_per_debtor"]
            if e["payer"] in net:
                net[e["payer"]] += per * len(e["debtors"])
            for d in e["debtors"]:
                if d in net:
                    net[d] -= per

        lines = [f"[{cur}]"]
        for name, bal in net.items():
            if abs(bal) < 0.005:
                lines.append(f"  {name:<10}  settled")
            elif bal > 0:
                lines.append(f"  {name:<10}  +{bal:.2f}  (others owe them)")
            else:
                lines.append(f"  {name:<10}  {bal:.2f}  (they owe others)")
        blocks.append("\n".join(lines))

    await update.message.reply_text("Net balances:\n\n" + "\n\n".join(blocks))


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
        await update.message.reply_text(f"{name} doesn't owe anyone.")
        return

    header = f"{'Date':<10}  {'Amt':>10}  {'Payer':<10}  Debtors"
    sep = "-" * len(header)
    by_cur = _group_by_currency(matching, trip["currency"])

    blocks = []
    for cur in sorted(by_cur.keys()):
        table_lines = [f"[{cur}]", header, sep]
        totals_by_payer: dict[str, float] = {}
        for e in by_cur[cur]:
            date = e["created_at"][:10]
            per = e["amount_per_debtor"]
            table_lines.append(f"{date}  {per:>10.2f}  {e['payer']:<10}  {name}")
            totals_by_payer[e["payer"]] = totals_by_payer.get(e["payer"], 0.0) + per

        summary_lines = []
        grand_total = 0.0
        for payer, amt in totals_by_payer.items():
            summary_lines.append(f"{name} -> {payer} = {amt:.2f} {cur}")
            grand_total += amt
        summary_lines.append(f"Total: {grand_total:.2f} {cur}")

        blocks.append("\n".join([*table_lines, "", *summary_lines]))

    body = html.escape("\n\n".join(blocks))
    text = f"<b>{html.escape(name)}'s debts</b>\n<pre>{body}</pre>"
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

    by_cur = _group_by_currency(entries, trip["currency"])
    blocks = []
    for cur in sorted(by_cur.keys()):
        # Gross debts in this currency: (debtor, payer) -> total
        gross: dict[tuple[str, str], float] = {}
        for e in by_cur[cur]:
            per = e["amount_per_debtor"]
            for d in e["debtors"]:
                key = (d, e["payer"])
                gross[key] = gross.get(key, 0.0) + per

        # Pairwise netting
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
            seen.add((a, b))
            seen.add((b, a))

        lines = [f"[{cur}]"]
        if not net:
            lines.append("  (all debts cancel out)")
        else:
            for (debtor, payer), amt in sorted(net.items()):
                lines.append(f"  {debtor} -> {payer} = {amt:.2f} {cur}")
        blocks.append("\n".join(lines))

    body = html.escape("\n\n".join(blocks))
    text = f"<b>Net debts</b>\n<pre>{body}</pre>"
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


# ---------- /end ----------

async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("Only group admins can run /end.")
        return
    chat_id = update.effective_chat.id
    trip = db.get_trip(chat_id)
    if not trip:
        await update.message.reply_text("No active trip to end.")
        return

    await update.message.reply_text("Wrapping up the trip. Final reports:")
    await cmd_ledger(update, context)
    await cmd_tally(update, context)

    if trip.get("pinned_message_id"):
        try:
            await context.bot.unpin_chat_message(chat_id, trip["pinned_message_id"])
        except Exception as e:
            log.warning("could not unpin: %s", e)

    db.clear_chat_data(chat_id)
    await update.message.reply_text(
        "Trip ended. All entries and trip data cleared. Run /init to start a new one."
    )


# ---------- registration ----------

def build_init_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("init", init_entry)],
        states={
            DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_dest)],
            CURRENCY: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_currency)],
            NUM_JOINERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_num)],
            JOINER_ALIAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_joiner_alias)],
            JOINER_HANDLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, init_joiner_handle)],
        },
        fallbacks=[CommandHandler("cancel", init_cancel)],
        name="init_trip",
        persistent=False,
    )
