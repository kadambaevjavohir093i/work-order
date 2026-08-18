#!/usr/bin/env python3
"""
Fleet form bot — Telegram bot that takes a raw dispatch message and returns
the filled repair form.

Setup:
    pip install "python-telegram-bot>=21.0"
    export BOT_TOKEN="123456:ABC..."
    python fleet_form_bot.py

Usage in Telegram:
    - Paste the raw message  -> bot replies with the filled repair form.
    - Start it with "pm"     -> bot replies with the PM form instead.
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
from typing import NamedTuple
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
    "ryder",
]

# who pays — pulled out of the payment line ("comcheck driver pay" -> driver).
# A word in both lists (ryder) fills both fields from a single mention, and a
# more specific party word still wins: "ryder driver" -> RYDER / DRIVER.
RESPONSIBLE_WORDS = [
    "driver", "company", "carrier", "owner operator", "o/o", "owner",
    "shop", "warranty", "broker", "customer", "ryder",
]

# the party words that are not also payment methods, checked first so
# "ryder shop" resolves the party to SHOP and only a lone "ryder" means RYDER
PARTY_ONLY_WORDS = [w for w in RESPONSIBLE_WORDS if w not in PAYMENT_WORDS]

FIELDS = [
    "FLEET MEMBER", "COMPANY", "DRIVER NAME",
    "TRUCK#", "TRAILER#", "PHONE#", "DATE", "TIME CALLED",
    "SERVICE", "REPRESENTATIVE", "QM PO#", "CASE",
    "RESPONSIBLE PARTY", "IF DRIVER, INFORMED", "ISSUE",
    "PAYMENT METHOD", "LOC", "NOTE",
]


def norm_key(k: str) -> str:
    """'QM PO#' and 'qm po' both -> 'QMPO', so a label can be typed loosely."""
    return re.sub(r"[^A-Z0-9]", "", k.upper())


# how a field may be named at the front of a line, in the message or in an edit
FIELD_LOOKUP = {norm_key(k): k for k in FIELDS}
FIELD_LOOKUP.update({
    "QM": "QM PO#", "QMPO": "QM PO#", "PO": "QM PO#",
    "TIME": "TIME CALLED", "REP": "REPRESENTATIVE",
    "TRUCK": "TRUCK#", "TRAILER": "TRAILER#", "PHONE": "PHONE#",
    "DRIVER": "DRIVER NAME", "SHOP": "SERVICE",
    "RP": "RESPONSIBLE PARTY", "INFORMED": "IF DRIVER, INFORMED",
    "ADDRESS": "LOC", "LOCATION": "LOC", "NOTES": "NOTE",
})

# A labeled line for one of these keeps its place in the message with only the
# label removed, because the block detector needs the shop's shape intact.
# Every other labeled line is lifted out so it cannot bleed into ISSUE.
STRUCTURAL_FIELDS = {
    "COMPANY", "DRIVER NAME", "TRUCK#", "TRAILER#", "PHONE#",
    "SERVICE", "REPRESENTATIVE", "LOC",
}

# blank line groups in the rendered form (after these fields)
BREAKS_AFTER = {"DRIVER NAME", "TIME CALLED", "ISSUE", "PAYMENT METHOD"}

# always shown in caps, whether parsed from the message or typed as an edit
UPPER_FIELDS = {"PAYMENT METHOD", "RESPONSIBLE PARTY", "ISSUE", "FLEET MEMBER"}

# ---- the PM form: a message whose first line is "pm" gets this one instead --

PM_TRIGGER = {"PM", "P/M"}
PM_DEFAULT_ISSUE = "TRK PM SERVICE"

PM_FIELDS = [
    "FLEET MEMBER", "COMPANY", "TRUCK", "DRIVER",
    "ISSUE", "APP DATE & TIME", "SERVICE", "NOTE", "WO",
]
PM_BREAKS = {"DRIVER": 2, "ISSUE": 1, "APP DATE & TIME": 1, "SERVICE": 1, "NOTE": 1}
PM_NO_FILL = {"WO", "NOTE"}       # filled in by hand, so no NA
PM_UPPER = {"FLEET MEMBER", "ISSUE"}

PM_LOOKUP = {norm_key(k): k for k in PM_FIELDS}
PM_LOOKUP.update({
    "APP": "APP DATE & TIME", "APPDATE": "APP DATE & TIME",
    "APPTIME": "APP DATE & TIME", "DATE": "APP DATE & TIME",
    "TIME": "APP DATE & TIME", "SHOP": "SERVICE", "NOTES": "NOTE",
    "WORKORDER": "WO", "WO": "WO",
})


class FormSpec(NamedTuple):
    fields: list
    breaks: dict     # field -> how many blank lines follow it
    no_fill: set     # left blank rather than filled with EMPTY_VALUE
    upper: set
    lookup: dict


REPAIR_FORM = FormSpec(
    FIELDS, {k: 1 for k in BREAKS_AFTER}, NO_FILL_FIELDS, UPPER_FIELDS, FIELD_LOOKUP,
)
PM_FORM = FormSpec(PM_FIELDS, PM_BREAKS, PM_NO_FILL, PM_UPPER, PM_LOOKUP)

ALL_FIELDS = set(FIELDS) | set(PM_FIELDS)


def spec_for(f: dict) -> FormSpec:
    """WO only exists on the PM form, so its presence identifies the form."""
    return PM_FORM if "WO" in f else REPAIR_FORM

# dropped from FLEET MEMBER — work profiles are often named "Jacob Fleet".
# Add more words here if your team's profile names carry other job labels.
NAME_NOISE = {"FLEET"}

# sent back when a message carries nothing to parse — a stray "1", ".", "+"
SAMPLE_TEMPLATE = """PLEASE FILL THIS SAMPLE

COMPANY NAME:
TRUCK | TRAILER #:
DRIVERS NAME:
PHONE NUMBERS:

PAYMENT METHOD AND RESPONSIBLE PARTY:

ISSUE:

SHOP INFO:"""

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


# "Ph: +15134770709" is still a phone line. The prefix is ignored when working
# out what a line is, but the value kept on the form is the line as sent.
LEAD_LABEL_RE = re.compile(r"^\s*[A-Za-z][A-Za-z.\s#]{0,14}[:=]\s*")


def unlabeled(line: str) -> str:
    return LEAD_LABEL_RE.sub("", line, count=1).strip() or line


def looks_like_phone(line: str) -> bool:
    # every separated chunk has to look like a phone, so "212654 | 2335614PLA"
    # is not mistaken for two numbers just because the digits add up
    chunks = [c.strip() for c in PUNCT_SEP.split(unlabeled(line)) if c.strip()]
    return bool(chunks) and all(
        9 <= len(digits(c)) <= 15 and not re.search(r"[A-Za-z]{3,}", c)
        for c in chunks
    )


def looks_like_address(line: str) -> bool:
    line = unlabeled(line)
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


def is_too_short(text: str) -> bool:
    """A stray keystroke rather than a dispatch: one character, or nothing but
    punctuation. Anything with two or more letters or digits is a real try."""
    t = text.strip()
    return len(t) <= 1 or not re.search(r"[A-Za-z0-9]", t)


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

# A label is short and made of letters, so a shop name with a colon in it and
# a sentence ending in one are both left alone.
LABEL_RE = re.compile(r"^\s*([A-Za-z#,&.\s]{1,24}?)\s*[:=]\s*(.*)$")

# Labels that also work with just a space after them. Kept deliberately small:
# "driver john smith" would be a fair reading, but so would a carrier actually
# named "Driver Logistics", and the colon form already covers every field.
BARE_LABELS = {"NOTE", "NOTES"}
BARE_LABEL_RE = re.compile(r"^\s*([A-Za-z]+)\s+(.+)$")


def split_label(line: str) -> tuple[str | None, str]:
    """'note: closes at 5' -> ('NOTE', 'closes at 5'). A line with no label,
    or one naming something that is not a field, comes back untouched."""
    m = LABEL_RE.match(line)
    if m:
        field = FIELD_LOOKUP.get(norm_key(m.group(1)))
        if field:
            return field, m.group(2).strip()
    # "note waiting for parts" — no colon. Only for labels that cannot be
    # confused with the start of an ordinary dispatch line.
    m = BARE_LABEL_RE.match(line)
    if m and m.group(1).upper() in BARE_LABELS:
        return FIELD_LOOKUP[m.group(1).upper()], m.group(2).strip()
    return None, line


# "TRL hose REPLACE note hello" — a note started part way through a line.
INLINE_NOTE_RE = re.compile(r"\bnotes?\b\s*[:\-]?\s*", re.I)


OPENERS, CLOSERS = "([{<", ")]}>"


def trim_brackets(before: str, after: str) -> tuple[str, str]:
    """"TRL hose REPLACE ( note hello )" splits into a before ending in "(" and
    an after ending in ")". Drop a bracket pair that only wrapped the note,
    leaving brackets that belong to the text itself alone."""
    before, after = before.strip(), after.strip()
    while before and before[-1] in OPENERS:
        closer = CLOSERS[OPENERS.index(before[-1])]
        if after.endswith(closer):
            after = after[:-1].strip()
        before = before[:-1].strip()
    return before.rstrip(" -,;:|/").strip(), after


def extract_inline_note(lines: list[str]) -> tuple[list[str], str]:
    """Split a note off the middle of a line. Only fires when the message says
    "note" exactly once, so an ambiguous message is left alone rather than
    guessed at."""
    hits = [(i, m) for i, l in enumerate(lines)
            for m in [INLINE_NOTE_RE.search(l)] if m]
    if len(hits) != 1:
        return lines, ""
    i, m = hits[0]
    before, after = trim_brackets(lines[i][:m.start()], lines[i][m.end():])
    if not after:
        return lines, ""
    out = list(lines)
    out[i] = before
    if not before:
        del out[i]
    return out, after


def extract_labels(text: str) -> tuple[str, dict]:
    """Pull labeled lines out of the message and return what is left to parse
    positionally, plus the values the sender named outright."""
    explicit: dict[str, str] = {}
    kept = []
    for line in text.splitlines():
        field, value = split_label(line)
        if field is None:
            kept.append(line)
            continue
        # repeating a label adds to it rather than replacing it
        explicit[field] = f"{explicit[field]} {value}".strip() if field in explicit else value
        if field in STRUCTURAL_FIELDS:
            kept.append(value)
    # a note that started mid-line, but only when nothing already claimed NOTE
    if "NOTE" not in explicit:
        kept, note = extract_inline_note(kept)
        if note:
            explicit["NOTE"] = note
    return "\n".join(kept), explicit


def parse_message(text: str, fleet_member: str) -> dict:
    f = {k: "" for k in FIELDS}
    f["FLEET MEMBER"] = fleet_member
    f["DATE"] = datetime.now(TZ).strftime(DATE_FMT)
    f["TIME CALLED"] = DEFAULT_TIME_CALLED

    text, explicit = extract_labels(text)
    blocks = split_blocks(text)
    if not blocks:
        f.update(explicit)
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
        party = find_term(joined, PARTY_ONLY_WORDS) or find_term(joined, RESPONSIBLE_WORDS)
        f["PAYMENT METHOD"] = method or joined
        if party:
            f["RESPONSIBLE PARTY"] = party

    # ---- issue = whatever is left
    issue_lines = [l for b in other_blocks for l in b]
    f["ISSUE"] = " ".join(issue_lines).strip()

    # what the sender named outright beats what the heuristics guessed
    f.update(explicit)
    return f


def is_pm_message(text: str) -> bool:
    """True when the first line of the message is just "pm"."""
    triggers = {norm_key(t) for t in PM_TRIGGER}
    for line in text.splitlines():
        if line.strip():
            return norm_key(line) in triggers
    return False


def drop_first_line(text: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip():
            return "\n".join(lines[i + 1:])
    return ""


def parse_pm(text: str, fleet_member: str) -> dict:
    """The PM form asks for the same facts under different names, so run the
    dispatch parser over the rest of the message and re-label its answers.
    The shop's name, address and phone are one SERVICE block here rather than
    three separate fields."""
    base = parse_message(drop_first_line(text), fleet_member)
    shop = [base["SERVICE"], base["LOC"], base["REPRESENTATIVE"]]
    # the PM form has one unit field, so a trailer rides along with the truck
    truck, trailer = base["TRUCK#"], base["TRAILER#"]
    units = f"{truck or EMPTY_VALUE} | {trailer}" if trailer else truck
    return {
        "FLEET MEMBER": base["FLEET MEMBER"],
        "COMPANY": base["COMPANY"],
        "TRUCK": units,
        "DRIVER": base["DRIVER NAME"],
        # whatever they wrote, else the PM job this form exists for
        "ISSUE": base["ISSUE"] or PM_DEFAULT_ISSUE,
        "APP DATE & TIME": datetime.now(TZ).strftime(DATE_FMT),
        "SERVICE": "\n".join(s for s in shop if s),
        "NOTE": base["NOTE"],
        "WO": "",
    }


# ---------------------------------------------------------------- render ----

def field_value(f: dict, key: str, spec: FormSpec | None = None) -> str:
    spec = spec or spec_for(f)
    val = (f.get(key) or "").strip()
    if not val and key not in spec.no_fill:
        val = EMPTY_VALUE
    return val.upper() if key in spec.upper else val


def form_rows(f: dict):
    """(label, value, blank lines after) for every row of whichever form."""
    spec = spec_for(f)
    for key in spec.fields:
        yield key, field_value(f, key, spec), spec.breaks.get(key, 0)


def render(f: dict) -> str:
    out = []
    for key, val, gap in form_rows(f):
        # the PM shop block runs over several lines and sits under its label
        sep = "\n" if "\n" in val else " "
        out.append(f"{key}:{sep}{val}".rstrip())
        out.extend([""] * gap)
    return "\n".join(out)


def render_html(f: dict) -> str:
    """Same form with the labels in bold. Values are escaped, so an & or a <
    in a shop name cannot break the markup."""
    out = []
    for key, val, gap in form_rows(f):
        sep = "\n" if "\n" in val else " "
        out.append(f"<b>{key}:</b>{sep}{html.escape(val)}".rstrip())
        out.extend([""] * gap)
    return "\n".join(out)


# ----------------------------------------------------------------- edits ----

EDIT_RE = re.compile(r"^\s*([A-Za-z#,&.\s]+?)\s*[:=]\s*(.*)$")

def form_from_text(text: str) -> dict | None:
    """Rebuild a form from one the bot already sent. Telegram hands back the
    reply's text without the bold markup, so the labels parse straight off. A
    line carrying no label continues the field above it, which is how the PM
    form's multi-line SERVICE survives the round trip."""
    f, current = {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^\s*([^:]+):\s*(.*)$", line)
        key = m.group(1).strip().upper() if m else None
        if key in ALL_FIELDS:
            f[key], current = m.group(2).strip(), key
        elif current is not None:
            f[current] = f"{f[current]}\n{line.strip()}".strip()
        else:
            return None
    return f if len(f) >= 3 else None


def parse_edits(text: str, spec: FormSpec = REPAIR_FORM) -> dict | None:
    edits = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = EDIT_RE.match(line)
        if not m:
            return None
        key = norm_key(m.group(1))
        if key not in spec.lookup:
            return None
        edits[spec.lookup[key]] = m.group(2).strip()
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
        "Send me the raw dispatch message and I'll return the filled form.\n"
        "Start the message with \"pm\" for the PM form instead.\n\n"
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

    if is_too_short(text):
        await update.message.reply_text(SAMPLE_TEMPLATE)
        return

    # a reply carries its own form, so corrections still work after a restart
    # has emptied LAST_FORM — and they name that form's fields, not the other's
    reply_to = update.message.reply_to_message
    base = form_from_text(reply_to.text) if reply_to and reply_to.text else None
    if base is None:
        base = LAST_FORM.get(chat_id)

    edits = parse_edits(text, spec_for(base) if base else REPAIR_FORM)
    if edits:
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

    build = parse_pm if is_pm_message(text) else parse_message
    f = build(text, fleet_member_for(update))
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
