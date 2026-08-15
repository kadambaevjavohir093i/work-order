#!/usr/bin/env python3
"""
Fleet form bot — Telegram bot that takes a raw dispatch message and returns
the filled repair form.

Setup:
    pip install "python-telegram-bot>=21.0"
    export BOT_TOKEN="123456:ABC..."
    python fleet_form_bot.py

Usage in Telegram:
    - Paste the raw message  -> bot replies with the filled form.
    - Reply/send edits like  -> QM PO#: 884512
                                TIME CALLED: 3:40 PM
      and the bot re-sends the corrected form. Replying to a form edits that
      form, so corrections survive a restart; a bare edit needs the last form
      still in memory.
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
EMPTY_VALUE = "NA"   # shown for any field the message did not fill
NO_FILL_FIELDS = {"QM PO#", "CASE"}   # except these — left blank to fill in by hand

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

# always shown in caps, whether parsed from the message or typed as an edit
UPPER_FIELDS = {"PAYMENT METHOD", "RESPONSIBLE PARTY", "ISSUE", "FLEET MEMBER"}

# dropped from FLEET MEMBER — work profiles are often named "Jacob Fleet".
# Add more words here if your team's profile names carry other job labels.
NAME_NOISE = {"FLEET"}

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


# Two values on one line can be separated however the sender felt like it.
# "-" is deliberately absent: it shows up inside trailer numbers and phones.
PUNCT_SEP = re.compile(r"[|/\\:;,><+&]+")
PART_SEP = re.compile(r"[\s|/\\:;,><&]+")


# stand-ins for a value the sender does not have ("NA 233561PLA" = no truck)
UNIT_PLACEHOLDERS = {"NA", "N/A", "N.A.", "NONE", "NO", "NIL", "X", "XX", "-", "--", "?"}


def split_parts(line: str) -> list[str]:
    """Split "212654 | 2335614PLA", "212654/2335614PLA", "212654 2335614PLA"
    and friends into their separate values."""
    # keep "N/A" in one piece, otherwise the "/" separator would halve it
    line = re.sub(r"\bN\s*[/.]\s*A\b\.?", "NA", line.strip(), flags=re.I)
    return [p for p in PART_SEP.split(line) if p]


def is_unit_part(p: str) -> bool:
    return p.upper() in UNIT_PLACEHOLDERS or bool(
        re.fullmatch(r"[A-Za-z]{0,4}\d[\w\-]*", p)
    )


def looks_like_phone(line: str) -> bool:
    # every separated chunk has to look like a phone, so "212654 | 2335614PLA"
    # is not mistaken for two numbers just because the digits add up
    chunks = [c.strip() for c in PUNCT_SEP.split(line) if c.strip()]
    return bool(chunks) and all(
        9 <= len(digits(c)) <= 15 and not re.search(r"[A-Za-z]{3,}", c)
        for c in chunks
    )


def looks_like_address(line: str) -> bool:
    return bool(re.search(r"\d.*\b[A-Z]{2}\b\s*\d{5}", line)) or bool(
        re.search(r"\d+\s+\w+.*,\s*\w+", line)
    )


def looks_like_units(line: str) -> bool:
    parts = split_parts(line)
    if not parts or looks_like_phone(line):
        return False
    # every part is a unit or a placeholder, and at least one is a real unit,
    # so a line that is nothing but "NA" is not swallowed as a unit line
    real = [p for p in parts if p.upper() not in UNIT_PLACEHOLDERS]
    return bool(real) and all(is_unit_part(p) for p in parts)


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
            # a placeholder becomes empty so render() shows EMPTY_VALUE,
            # whichever stand-in the sender happened to type
            parts = ["" if p.upper() in UNIT_PLACEHOLDERS else p
                     for p in split_parts(line)]
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

def field_value(f: dict, key: str) -> str:
    val = (f.get(key) or "").strip()
    if not val and key not in NO_FILL_FIELDS:
        val = EMPTY_VALUE
    return val.upper() if key in UPPER_FIELDS else val


def render(f: dict) -> str:
    out = []
    for key in FIELDS:
        out.append(f"{key}: {field_value(f, key)}".rstrip())
        if key in BREAKS_AFTER:
            out.append("")
    return "\n".join(out)


def render_html(f: dict) -> str:
    """Same form with the labels in bold. Values are escaped, so an & or a <
    in a shop name cannot break the markup."""
    out = []
    for key in FIELDS:
        line = f"<b>{key}:</b> {html.escape(field_value(f, key))}".rstrip()
        out.append(line)
        if key in BREAKS_AFTER:
            out.append("")
    return "\n".join(out)


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


FIELD_SET = set(FIELDS)


def form_from_text(text: str) -> dict | None:
    """Rebuild a form from one the bot already sent. Telegram hands back the
    reply's text without the bold markup, so the labels parse straight off."""
    f = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^\s*([^:]+):\s*(.*)$", line)
        if not m or m.group(1).strip().upper() not in FIELD_SET:
            return None
        f[m.group(1).strip().upper()] = m.group(2).strip()
    return f or None


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
    raw = NAME_OVERRIDE.get(uid) or (update.effective_user.first_name or "")
    kept = [w for w in raw.split() if w.upper().strip(".,") not in NAME_NOISE]
    # if the name is nothing but noise, keep it rather than send an empty field
    return " ".join(kept) or raw.strip()


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me the raw dispatch message and I'll return the filled form.\n\n"
        "Fix a field by sending lines like:\n"
        "QM PO#: 884512\n"
        "TIME CALLED: 3:40 PM\n\n"
        "Reply to a form to correct that one — that always works, even after "
        "I've restarted.\n\n"
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
    await update.message.reply_text(render_html(f), parse_mode=ParseMode.HTML)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    chat_id = update.effective_chat.id

    edits = parse_edits(text)
    if edits:
        # a reply carries its own form, so corrections still work after a
        # restart has emptied LAST_FORM
        reply_to = update.message.reply_to_message
        base = form_from_text(reply_to.text) if reply_to and reply_to.text else None
        if base is None:
            base = LAST_FORM.get(chat_id)
        if base is None:
            await update.message.reply_text(
                "No form to edit yet. Send the dispatch message first, then "
                "reply to the form with your corrections."
            )
            return
        base.update(edits)
        LAST_FORM[chat_id] = base
        await update.message.reply_text(
            render_html(base), parse_mode=ParseMode.HTML
        )
        return

    f = parse_message(text, fleet_member_for(update))
    LAST_FORM[chat_id] = f
    await update.message.reply_text(render_html(f), parse_mode=ParseMode.HTML)


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

    # Render web services must bind $PORT, so run as a webhook there.
    # Anywhere else (laptop, Render background worker) fall back to long polling.
    url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    port = os.environ.get("PORT")
    if url and port:
        log.info("bot running (webhook)")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=token,
            webhook_url=f"{url.rstrip('/')}/{token}",
            secret_token=os.environ.get("WEBHOOK_SECRET") or None,
        )
    else:
        log.info("bot running (polling)")
        app.run_polling()


if __name__ == "__main__":
    main()
