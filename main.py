"""
Asylum Confessions Bot — v6
============================
Fixes from v5 review:
1. Tokens now consumed after free limit is exceeded
2. Referral only credited after referred user joins channel
3. DB path → /data/confessions.db (Railway volume mount)

Additions:
- /tokens  — check token balance
- /referral — get referral link
- /stats   — admin queue counts
- /help    — command list
- MAX_TOKENS_CAP — caps referral token farming
"""

import os
import sqlite3
import threading
import logging
import time
import signal

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.helpers import escape_markdown

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# ENV  (Railway injects — no dotenv)
# ─────────────────────────────────────────

BOT_TOKEN     = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID    = os.getenv("CHANNEL_ID")
OWNER_ID      = os.getenv("OWNER_ID")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "AsylumConfessions_bot")

assert BOT_TOKEN,     "Missing BOT_TOKEN"
assert ADMIN_CHAT_ID, "Missing ADMIN_CHAT_ID"
assert CHANNEL_ID,    "Missing CHANNEL_ID"
assert OWNER_ID,      "Missing OWNER_ID"

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
OWNER_ID      = int(OWNER_ID)

# ─────────────────────────────────────────
# DATABASE  (/data = Railway volume mount)
# ─────────────────────────────────────────

os.makedirs("/data", exist_ok=True)

_db_lock = threading.Lock()

conn   = sqlite3.connect("/data/confessions.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS confessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    category   TEXT    NOT NULL,
    status     TEXT    DEFAULT 'pending',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    user_id  INTEGER PRIMARY KEY,
    count    INTEGER DEFAULT 0,
    reset_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS preview_cooldowns (
    user_id   INTEGER PRIMARY KEY,
    last_time INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    referrer_id INTEGER,
    referred_id INTEGER UNIQUE
);

CREATE TABLE IF NOT EXISTS tokens (
    user_id INTEGER PRIMARY KEY,
    count   INTEGER DEFAULT 0
);
""")

conn.commit()

# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────

MAX_PER_WEEK     = 2
WEEK_SECONDS     = 604800
PREVIEW_COOLDOWN = 30
MIN_LENGTH       = 10
MAX_LENGTH       = 1000
TOKEN_PER_REF    = 1
MAX_TOKENS_CAP   = 5       # max tokens a user can hold at once

CATEGORY_LABELS = {
    "love":    "❤️ Love",
    "secrets": "🕯️ Secrets",
    "regret":  "😔 Regret",
    "chaos":   "🎭 Chaos",
}

# ─────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────

def db_get_tokens(user_id: int) -> int:
    with _db_lock:
        cursor.execute(
            "SELECT count FROM tokens WHERE user_id=?",
            (user_id,)
        )
        row = cursor.fetchone()
    return row[0] if row else 0


def _db_add_tokens_unsafe(user_id: int, amount: int):
    """Must be called with _db_lock already held."""
    cursor.execute(
        "SELECT count FROM tokens WHERE user_id=?",
        (user_id,)
    )
    row = cursor.fetchone()
    current = row[0] if row else 0
    new_total = min(current + amount, MAX_TOKENS_CAP)  # cap tokens
    cursor.execute("""
        INSERT INTO tokens (user_id, count) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET count = ?
    """, (user_id, new_total, new_total))


def db_handle_referral(new_user_id: int, referrer_id: int):
    if new_user_id == referrer_id:
        return
    with _db_lock:
        cursor.execute("""
            INSERT OR IGNORE INTO referrals (referrer_id, referred_id)
            VALUES (?, ?)
        """, (referrer_id, new_user_id))
        if cursor.rowcount > 0:
            _db_add_tokens_unsafe(referrer_id, TOKEN_PER_REF)
        conn.commit()


def db_can_submit(user_id: int) -> bool:
    now = int(time.time())
    with _db_lock:
        cursor.execute(
            "SELECT count FROM tokens WHERE user_id=?",
            (user_id,)
        )
        token_row = cursor.fetchone()
        tokens = token_row[0] if token_row else 0

        cursor.execute(
            "SELECT count, reset_at FROM rate_limits WHERE user_id=?",
            (user_id,)
        )
        row = cursor.fetchone()

        if not row:
            cursor.execute(
                "INSERT INTO rate_limits VALUES (?, 0, ?)",
                (user_id, now)
            )
            conn.commit()
            return True

        count, reset_at = row

        if now - reset_at > WEEK_SECONDS:
            cursor.execute(
                "UPDATE rate_limits SET count=0, reset_at=? WHERE user_id=?",
                (now, user_id)
            )
            conn.commit()
            return True

        return count < MAX_PER_WEEK + tokens


def db_add_submission(user_id: int):
    """Increment usage. Consume 1 token if over free limit."""
    with _db_lock:
        cursor.execute(
            "SELECT count FROM rate_limits WHERE user_id=?",
            (user_id,)
        )
        row = cursor.fetchone()
        current_count = row[0] if row else 0

        cursor.execute(
            "SELECT count FROM tokens WHERE user_id=?",
            (user_id,)
        )
        token_row = cursor.fetchone()
        tokens = token_row[0] if token_row else 0

        # Increment weekly usage
        cursor.execute(
            "UPDATE rate_limits SET count=count+1 WHERE user_id=?",
            (user_id,)
        )

        # If over free limit, burn a token
        if current_count >= MAX_PER_WEEK and tokens > 0:
            cursor.execute(
                "UPDATE tokens SET count=count-1 WHERE user_id=?",
                (user_id,)
            )

        conn.commit()


def db_preview_allowed(user_id: int) -> bool:
    now = int(time.time())
    with _db_lock:
        cursor.execute(
            "SELECT last_time FROM preview_cooldowns WHERE user_id=?",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO preview_cooldowns VALUES (?, ?)",
                (user_id, now)
            )
            conn.commit()
            return True
        if now - row[0] < PREVIEW_COOLDOWN:
            return False
        cursor.execute(
            "UPDATE preview_cooldowns SET last_time=? WHERE user_id=?",
            (now, user_id)
        )
        conn.commit()
        return True


def db_save_confession(text: str, category: str) -> int:
    with _db_lock:
        cursor.execute(
            "INSERT INTO confessions (text, category, created_at) VALUES (?, ?, ?)",
            (text, category, int(time.time()))
        )
        confession_id = cursor.lastrowid
        conn.commit()
    return confession_id


def db_get_confession(confession_id: int):
    with _db_lock:
        cursor.execute(
            "SELECT text, category, status FROM confessions WHERE id=?",
            (confession_id,)
        )
        return cursor.fetchone()


def db_set_status(confession_id: int, status: str):
    with _db_lock:
        cursor.execute(
            "UPDATE confessions SET status=? WHERE id=?",
            (status, confession_id)
        )
        conn.commit()


def db_get_stats() -> dict:
    with _db_lock:
        cursor.execute(
            "SELECT status, COUNT(*) FROM confessions GROUP BY status"
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM referrals")
        ref_count = cursor.fetchone()[0]
    stats = {r[0]: r[1] for r in rows}
    stats["referrals"] = ref_count
    return stats

# ─────────────────────────────────────────
# MEMBERSHIP CHECK
# ─────────────────────────────────────────

async def is_channel_member(user_id: int, context) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("Membership check failed: %s", e)
        return False

# ─────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────

def kb_join():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📢 Join Channel",
            url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"
        )
    ]])

def kb_preview():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📨 Submit", callback_data="submit"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])

def kb_categories():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤️ Love",    callback_data="cat_love"),
            InlineKeyboardButton("🕯️ Secrets", callback_data="cat_secrets"),
        ],
        [
            InlineKeyboardButton("😔 Regret",  callback_data="cat_regret"),
            InlineKeyboardButton("🎭 Chaos",   callback_data="cat_chaos"),
        ],
    ])

def kb_admin(confession_id: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{confession_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{confession_id}"),
    ]])

def kb_submit_more():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📝 Submit Confession",
            url=f"https://t.me/{BOT_USERNAME}"
        )
    ]])

def kb_channel_pin():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📝 Submit Anonymously",
            url=f"https://t.me/{BOT_USERNAME}"
        )
    ]])

# ─────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Referral: only credit AFTER referred user joins channel (anti-farm)
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].split("_")[1])
            if await is_channel_member(user_id, context):
                db_handle_referral(user_id, referrer_id)
        except Exception as e:
            log.warning("Referral parse failed: %s", e)

    tokens   = db_get_tokens(user_id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

    await update.message.reply_text(
        "🕯️ *Asylum Confessions*\n\n"
        "Join the channel first.\n"
        "Then send your confession anonymously.\n\n"
        "Rules:\n"
        "• Text only\n"
        "• No threats\n"
        "• No doxxing\n"
        "• No links\n"
        "• No spam\n\n"
        f"⭐ *Tokens:* {tokens} / {MAX_TOKENS_CAP}\n"
        f"🎁 *Your invite link:*\n{ref_link}\n\n"
        "_Each invite = \\+1 extra confession token_",
        parse_mode="MarkdownV2",
        reply_markup=kb_join(),
    )


async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tokens  = db_get_tokens(user_id)
    await update.message.reply_text(
        f"⭐ *Your tokens:* {tokens} / {MAX_TOKENS_CAP}\n\n"
        f"Each token = 1 extra confession beyond the {MAX_PER_WEEK}/week limit.\n"
        f"Earn tokens by inviting friends.",
        parse_mode="Markdown",
    )


async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    await update.message.reply_text(
        f"🎁 *Your referral link:*\n{ref_link}\n\n"
        f"Share it. When someone joins the channel through your link "
        f"you get \\+1 token\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🕯️ *Asylum Confessions — Commands*\n\n"
        "/start — welcome + your referral link\n"
        "/tokens — check your token balance\n"
        "/referral — get your invite link\n"
        "/help — this message\n\n"
        f"*Limits:* {MAX_PER_WEEK} confessions per week\n"
        f"*Tokens:* earn extras by inviting friends",
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner only — queue stats."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Owner only.")
        return

    stats = db_get_stats()
    pending  = stats.get("pending",  0)
    approved = stats.get("approved", 0)
    rejected = stats.get("rejected", 0)
    total    = pending + approved + rejected
    referrals = stats.get("referrals", 0)

    await update.message.reply_text(
        f"📊 *Stats*\n\n"
        f"Total confessions: {total}\n"
        f"⏳ Pending:  {pending}\n"
        f"✅ Approved: {approved}\n"
        f"❌ Rejected: {rejected}\n\n"
        f"🎁 Referrals: {referrals}",
        parse_mode="Markdown",
    )


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner only — posts and pins the submit button in the channel."""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Owner only.")
        return

    try:
        msg = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=(
                "🕯️ *Asylum Confessions*\n\n"
                "Share your secrets anonymously\\.\n"
                "No names\\. No trace\\. No judgment\\.\n\n"
                "👇 Tap below to submit\\."
            ),
            parse_mode="MarkdownV2",
            reply_markup=kb_channel_pin(),
        )

        await context.bot.pin_chat_message(
            chat_id=CHANNEL_ID,
            message_id=msg.message_id,
            disable_notification=True,
        )

        await update.message.reply_text(f"✅ Pinned to {CHANNEL_ID}")
        log.info("Pinned message ID: %s", msg.message_id)

    except Exception as e:
        log.error("Pin failed: %s", e)
        await update.message.reply_text(f"❌ Failed:\n{e}")

# ─────────────────────────────────────────
# MEDIA BLOCK
# ─────────────────────────────────────────

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕯️ Text confessions only.")

# ─────────────────────────────────────────
# CONFESSION HANDLER
# ─────────────────────────────────────────

async def handle_confession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id
    text    = (update.message.text or "").strip()

    # MUST JOIN CHANNEL
    if not await is_channel_member(user_id, context):
        await update.message.reply_text(
            "🕯️ Join the channel first.",
            reply_markup=kb_join()
        )
        return

    # PREVIEW COOLDOWN
    if not db_preview_allowed(user_id):
        await update.message.reply_text("⏳ Slow down. Wait 30 seconds.")
        return

    # WEEKLY LIMIT
    if not db_can_submit(user_id):
        tokens = db_get_tokens(user_id)
        await update.message.reply_text(
            f"⏳ Limit reached.\n"
            f"{MAX_PER_WEEK} free + {tokens} token slots used.\n\n"
            f"Use /referral to earn more tokens."
        )
        return

    # LINK BLOCK
    lowered = text.lower()
    if any(x in lowered for x in ("http://", "https://", "t.me/", "www.")):
        await update.message.reply_text("🕯️ Links are not allowed.")
        return

    # LENGTH
    if len(text) < MIN_LENGTH:
        await update.message.reply_text(f"🕯️ Too short. Minimum {MIN_LENGTH} characters.")
        return

    if len(text) > MAX_LENGTH:
        await update.message.reply_text(f"🕯️ Too long. Maximum {MAX_LENGTH} characters.")
        return

    context.user_data["draft"] = text
    safe_text = escape_markdown(text, version=2)

    await update.message.reply_text(
        f"🕯️ *Preview:*\n\n{safe_text}",
        parse_mode="MarkdownV2",
        reply_markup=kb_preview(),
    )

# ─────────────────────────────────────────
# BUTTON HANDLER
# ─────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # CANCEL
    if data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelled.")
        return

    # SUBMIT → show categories
    if data == "submit":
        draft = context.user_data.get("draft")
        if not draft:
            await query.edit_message_text("❌ Session expired. Send your confession again.")
            return
        context.user_data["pending"] = draft
        await query.edit_message_text(
            "🎭 Pick a category:",
            reply_markup=kb_categories()
        )
        return

    # CATEGORY SELECTED
    if data.startswith("cat_"):
        category   = data[4:]
        confession = context.user_data.get("pending")

        if not confession:
            await query.edit_message_text("❌ Session expired. Send your confession again.")
            return

        if category not in CATEGORY_LABELS:
            await query.edit_message_text("❌ Invalid category.")
            return

        confession_id = db_save_confession(confession, category)
        db_add_submission(query.from_user.id)
        context.user_data.clear()

        label           = CATEGORY_LABELS[category]
        safe_confession = escape_markdown(confession, version=2)

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🕯️ *New Confession \\#{confession_id}*\n"
                f"{label}\n\n"
                f"{safe_confession}"
            ),
            parse_mode="MarkdownV2",
            reply_markup=kb_admin(confession_id),
        )

        await query.edit_message_text(
            f"🕯️ Submitted anonymously.\n"
            f"Reference: #{confession_id}"
        )
        return

    # APPROVE / REJECT
    if ":" not in data:
        return

    action, cid   = data.split(":", 1)
    confession_id = int(cid)
    row           = db_get_confession(confession_id)

    if not row:
        await query.edit_message_text("❌ Confession not found.")
        return

    confession, category, status = row

    if status != "pending":
        await query.edit_message_text(f"⚠️ Already {status}.")
        return

    label           = CATEGORY_LABELS.get(category, category)
    safe_confession = escape_markdown(confession, version=2)
    safe_label      = escape_markdown(label, version=2)

    if action == "approve":
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=(
                    f"🕯️ *{safe_label} — \\#{confession_id}*\n\n"
                    f"{safe_confession}"
                ),
                parse_mode="MarkdownV2",
                reply_markup=kb_submit_more(),
            )
        except Exception as e:
            log.error("Post failed: %s", e)
            await query.edit_message_text(f"❌ Post failed:\n{e}")
            return

        db_set_status(confession_id, "approved")
        await query.edit_message_text(f"✅ Approved #{confession_id}")
        return

    if action == "reject":
        db_set_status(confession_id, "rejected")
        await query.edit_message_text(f"❌ Rejected #{confession_id}")

# ─────────────────────────────────────────
# GRACEFUL SHUTDOWN (Windows-safe)
# ─────────────────────────────────────────

def shutdown(sig, frame):
    log.info("Shutting down...")
    conn.close()
    exit(0)

try:
    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)
except Exception:
    pass

# ─────────────────────────────────────────
# APP
# ─────────────────────────────────────────

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start",    cmd_start))
app.add_handler(CommandHandler("pin",      cmd_pin))
app.add_handler(CommandHandler("tokens",   cmd_tokens))
app.add_handler(CommandHandler("referral", cmd_referral))
app.add_handler(CommandHandler("stats",    cmd_stats))
app.add_handler(CommandHandler("help",     cmd_help))

app.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
    handle_confession
))

app.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & (
        filters.PHOTO | filters.VIDEO |
        filters.VOICE | filters.Document.ALL |
        filters.Sticker.ALL | filters.AUDIO
    ),
    handle_media
))

app.add_handler(CallbackQueryHandler(button_handler))

log.info("🕯️ Asylum Confessions Bot v6 running...")
app.run_polling(allowed_updates=Update.ALL_TYPES)