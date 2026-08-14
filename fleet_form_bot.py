#!/usr/bin/env python3
"""
Fleet form bot — Telegram bot that takes a raw dispatch message and returns
the filled repair form.

Setup:
    pip install "python-telegram-bot>=21.0"
    export BOT_TOKEN="123456:ABC..."
    python fleet_form_bot.py

Usage in Telegram:
    - Paste the raw message  -> bot replies with the filled form (tap to copy).
    - Reply/send edits like  -> QM PO#: 884512
                                TIME CALLED: 3:40 PM
      and the bot re-sends the corrected form.
    - /name Jacob            -> override FLEET MEMBER for your account
    - /last                  -> re-send the last form
"""

import html
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- config ----

TZ = ZoneInfo("America/New_York")   # date follows US ops time, not Tashkent
DATE_FMT = "%m/%d/%Y"
DEFAULT_TIME_CALLED = "NA"

# SERVICE / REPRESENTATIVE detail level
SERVICE_FULL_NAME = True   # True -> "Brothers Truck Repair"  | False -> "Brothers"
REP_FULL_PHONE = True      # True -> "+15134770709"           | False -> "0709"

# Fix known typos in carrier names. Add your own; leave empty to keep as-is.
CARRIER_ALIASES = {
    "DM WOLD": "DM WORLD",
    "DM WORD": "DM WORLD",
    "US MEGA CARRIER": "US MEGA",
}

PAYMENT_WORDS = [
    "comcheck", "comchek", "com check", "comdata",
    "tcheck", "t-chek", "tchek", "efs", "efs check",
    "cash", "credit card", "card", "cc", "zelle",
    "direct bill", "billing", "fleet card", "wex", "invoice",
]

# who pays — pulled out of the payment line ("comcheck driver pay" -> driver)
RESPONSIBLE_WORDS = [
    "driver", "company", "carrier", "owner operator", "o/o", "owner",
    "shop", "warranty", "broker", "customer",
]

FIELDS = [
    "FLEET MEMBER", "COMPANY", "DRIVER NAME",
    "TRUCK#", "TRAILER#", "PHONE#", "DATE", "TIME CALLED",
    "SERVICE", "REPRESENTATIVE", "QM PO#", "CASE",
    "RESPONSIBLE PARTY", "IF DRIVER, INFORMED", "ISSUE",
    "PAYMENT METHOD", "LOC", "NOTE",
]

# blank line groups in the rendered form (after these fields)
BREAKS_AFTER = {"DRIVER NAME", "TIME CALLED", "ISSUE", "PAYMENT METHOD"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fleet-form-bot")

LAST_FORM: dict[int, dict] = {}   # chat_id -> field dict
NAME_OVERRIDE: dict[int, str] = {}  # user_id -> fleet member name

# --------------------------------------------------------------- helpers ----

PHONE_RE = re.compile(r"(\+?\d[\d\-\(\)\.\s]{7,}\d)")


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def find_term(text: str, terms: list[str]) -> str:
    """First term present in text, returned as the sender wrote it. Longest wins,
    so 'credit card' beats 'card' and 'efs check' beats 'efs'."""
    for term in sorted(terms, key=len, reverse=True):
        m = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I)
        if m:
            return m.group(0)
    return ""


def looks_like_phone(line: str) -> bool:
    d = digits(line)
    return 9 <= len(d) <= 24 and not re.search(r"[A-Za-z]{3,}", line)


def looks_like_address(line: str) -> bool:
    return bool(re.search(r"\d.*\b[A-Z]{2}\b\s*\d{5}", line)) or bool(
        re.search(r"\d+\s+\w+.*,\s*\w+", line)
    )


def looks_like_units(line: str) -> bool:
    if "|" in line:
        return True
    toks = line.split()
    return bool(toks) and all(
        re.fullmatch(r"[A-Za-z]{0,4}\d[\w\-]*", t) for t in toks
    )


def looks_like_names(line: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}", line)) and not looks_like_phone(line)


def split_blocks(text: str) -> list[list[str]]:
    blocks, cur = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def is_payment_block(block: list[str]) -> bool:
    if len(block) > 2:
        return False
    joined = " ".join(block).lower()
    return any(w in joined for w in PAYMENT_WORDS) and len(joined) < 40


def is_shop_block(block: list[str]) -> bool:
    if len(block) < 2:
        return False
    has_addr = any(looks_like_address(l) for l in block)
    has_phone = any(looks_like_phone(l) for l in block)
    return has_addr and has_phone


# ----------------------------------------------------------------- parse ----

def parse_message(text: str, fleet_member: str) -> dict:
    f = {k: "" for k in FIELDS}
    f["FLEET MEMBER"] = fleet_member
    f["DATE"] = datetime.now(TZ).strftime(DATE_FMT)
    f["TIME CALLED"] = DEFAULT_TIME_CALLED

    blocks = split_blocks(text)
    if not blocks:
        return f

    shop_block = None
    payment_block = None
    other_blocks = []

    for i, b in enumerate(blocks):
        if i == 0:
            continue
        if shop_block is None and is_shop_block(b):
            shop_block = b
        elif payment_block is None and is_payment_block(b):
            payment_block = b
        else:
            other_blocks.append(b)

    # ---- header block: company / units / drivers / phones
    header = blocks[0]
    hi = 0
    if hi < len(header):
        company = header[hi].strip()
        f["COMPANY"] = CARRIER_ALIASES.get(company.upper(), company)
        hi += 1
    for line in header[hi:]:
        if looks_like_units(line) and not f["TRUCK#"]:
            parts = [p.strip() for p in re.split(r"[|/]", line) if p.strip()]
            f["TRUCK#"] = parts[0] if parts else ""
            f["TRAILER#"] = parts[1] if len(parts) > 1 else ""
        elif looks_like_phone(line) and not f["PHONE#"]:
            f["PHONE#"] = line
        elif looks_like_names(line) and not f["DRIVER NAME"]:
            f["DRIVER NAME"] = line

    # ---- shop block: service / representative / loc
    if shop_block:
        name_line = shop_block[0].strip()
        if SERVICE_FULL_NAME:
            f["SERVICE"] = name_line
        else:
            f["SERVICE"] = name_line.split()[0] if name_line.split() else name_line
        addr = next((l for l in shop_block if looks_like_address(l)), "")
        f["LOC"] = addr
        phone = next((l for l in shop_block if looks_like_phone(l)), "").strip()
        if phone:
            f["REPRESENTATIVE"] = phone if REP_FULL_PHONE else digits(phone)[-4:]

    # ---- payment: "comcheck driver pay" -> method + who pays
    if payment_block:
        joined = " ".join(payment_block).strip()
        method = find_term(joined, PAYMENT_WORDS)
        party = find_term(joined, RESPONSIBLE_WORDS)
        f["PAYMENT METHOD"] = method or joined
        if party:
            f["RESPONSIBLE PARTY"] = party

    # ---- issue = whatever is left
    issue_lines = [l for b in other_blocks for l in b]
    f["ISSUE"] = " ".join(issue_lines).strip()

    return f


# ---------------------------------------------------------------- render ----

def render(f: dict) -> str:
    out = []
    for key in FIELDS:
        out.append(f"{key}: {f.get(key, '')}".rstrip())
        if key in BREAKS_AFTER:
            out.append("")
    return "\n".join(out)


def as_copyable(f: dict) -> str:
    return f"<pre>{html.escape(render(f))}</pre>"


# ----------------------------------------------------------------- edits ----

EDIT_RE = re.compile(r"^\s*([A-Za-z#,\s]+?)\s*[:=]\s*(.*)$")
FIELD_LOOKUP = {k.replace(" ", "").replace("#", "").replace(",", "").upper(): k for k in FIELDS}
FIELD_LOOKUP.update({
    "QM": "QM PO#", "QMPO": "QM PO#", "PO": "QM PO#",
    "TIME": "TIME CALLED", "REP": "REPRESENTATIVE",
    "TRUCK": "TRUCK#", "TRAILER": "TRAILER#", "PHONE": "PHONE#",
    "DRIVER": "DRIVER NAME", "SHOP": "SERVICE",
    "RP": "RESPONSIBLE PARTY", "INFORMED": "IF DRIVER, INFORMED",
})


def parse_edits(text: str) -> dict | None:
    edits = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = EDIT_RE.match(line)
        if not m:
            return None
        key = m.group(1).replace(" ", "").replace("#", "").replace(",", "").upper()
        if key not in FIELD_LOOKUP:
            return None
        edits[FIELD_LOOKUP[key]] = m.group(2).strip()
    return edits or None


# --------------------------------------------------------------- handlers ----

def fleet_member_for(update: Update) -> str:
    uid = update.effective_user.id
    if uid in NAME_OVERRIDE:
        return NAME_OVERRIDE[uid]
    return (update.effective_user.first_name or "").strip()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me the raw dispatch message and I'll return the filled form.\n\n"
        "Fix a field by sending lines like:\n"
        "QM PO#: 884512\n"
        "TIME CALLED: 3:40 PM\n\n"
        "/name <name> — set FLEET MEMBER\n"
        "/last — resend last form"
    )


async def set_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /name Jacob")
        return
    NAME_OVERRIDE[update.effective_user.id] = " ".join(ctx.args)
    await update.message.reply_text(f"FLEET MEMBER set to: {' '.join(ctx.args)}")


async def last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = LAST_FORM.get(update.effective_chat.id)
    if not f:
        await update.message.reply_text("Nothing yet — send a message first.")
        return
    await update.message.reply_text(as_copyable(f), parse_mode=ParseMode.HTML)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    chat_id = update.effective_chat.id

    edits = parse_edits(text)
    if edits and chat_id in LAST_FORM:
        LAST_FORM[chat_id].update(edits)
        await update.message.reply_text(
            as_copyable(LAST_FORM[chat_id]), parse_mode=ParseMode.HTML
        )
        return

    f = parse_message(text, fleet_member_for(update))
    LAST_FORM[chat_id] = f
    await update.message.reply_text(as_copyable(f), parse_mode=ParseMode.HTML)


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN env var")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("name", set_name))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("bot running")
    app.run_polling()


if __name__ == "__main__":
    main()
