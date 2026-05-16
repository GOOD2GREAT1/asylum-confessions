import os
import sqlite3
import time

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

BOT_USERNAME = "AsylumConfessions_bot"

# =========================
# DATABASE
# =========================

conn = sqlite3.connect("database.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS confessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT,
    category TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS limits (
    user_id INTEGER PRIMARY KEY,
    count INTEGER,
    reset_time INTEGER
)
""")

conn.commit()

# =========================
# SETTINGS
# =========================

MAX_PER_WEEK = 2
WEEK_SECONDS = 604800

# =========================
# LIMITS
# =========================

def can_submit(user_id):

    now = int(time.time())

    cursor.execute(
        "SELECT count, reset_time FROM limits WHERE user_id=?",
        (user_id,)
    )

    row = cursor.fetchone()

    if not row:
        cursor.execute(
            "INSERT INTO limits VALUES (?, ?, ?)",
            (user_id, 0, now)
        )
        conn.commit()
        return True

    count, reset_time = row

    if now - reset_time > WEEK_SECONDS:
        cursor.execute(
            "UPDATE limits SET count=0, reset_time=? WHERE user_id=?",
            (now, user_id)
        )
        conn.commit()
        return True

    return count < MAX_PER_WEEK

def add_submission(user_id):

    cursor.execute(
        "UPDATE limits SET count=count+1 WHERE user_id=?",
        (user_id,)
    )

    conn.commit()

# =========================
# CHECK CHANNEL JOIN
# =========================

async def joined_channel(user_id, context):

    try:
        member = await context.bot.get_chat_member(
            CHANNEL_ID,
            user_id
        )

        return member.status in [
            "member",
            "administrator",
            "creator"
        ]

    except:
        return False

# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Join Channel",
                url=f"https://t.me/{CHANNEL_ID.replace('@','')}"
            )
        ],
        [
            InlineKeyboardButton(
                "📝 Submit Confession",
                url=f"https://t.me/{BOT_USERNAME}"
            )
        ]
    ])

    await update.message.reply_text(
        "🕯️ Welcome to Asylum Confessions\n\n"
        "Join the channel first.\n"
        "Then submit anonymously.",
        reply_markup=keyboard
    )

# =========================
# CONFESSION HANDLER
# =========================

async def handle_confession(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message or not update.message.text:
        return

    if update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id

    # MUST JOIN CHANNEL
    if not await joined_channel(user_id, context):

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=f"https://t.me/{CHANNEL_ID.replace('@','')}"
                )
            ]
        ])

        await update.message.reply_text(
            "🕯️ You must join the channel first.",
            reply_markup=keyboard
        )
        return

    # LIMIT
    if not can_submit(user_id):
        await update.message.reply_text(
            "⏳ Limit reached.\n2 confessions per week."
        )
        return

    text = update.message.text.strip()

    if len(text) < 5:
        await update.message.reply_text(
            "🕯️ Confession too short."
        )
        return

    context.user_data["draft"] = text

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📨 Submit",
                callback_data="submit"
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="cancel"
            ),
        ]
    ])

    await update.message.reply_text(
        f"🕯️ Preview:\n\n{text}",
        reply_markup=keyboard
    )

# =========================
# BUTTONS
# =========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    # CANCEL
    if query.data == "cancel":
        context.user_data.clear()

        await query.edit_message_text(
            "❌ Cancelled."
        )
        return

    # SUBMIT
    if query.data == "submit":

        draft = context.user_data.get("draft")

        if not draft:
            await query.edit_message_text(
                "❌ No draft found."
            )
            return

        context.user_data["pending"] = draft

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❤️ Love",
                    callback_data="cat_love"
                ),
                InlineKeyboardButton(
                    "🕯️ Secrets",
                    callback_data="cat_secrets"
                ),
            ],
            [
                InlineKeyboardButton(
                    "😔 Regret",
                    callback_data="cat_regret"
                ),
                InlineKeyboardButton(
                    "🎭 Chaos",
                    callback_data="cat_chaos"
                ),
            ]
        ])

        await query.edit_message_text(
            "🎭 Choose category:",
            reply_markup=keyboard
        )
        return

    # CATEGORY
    if query.data.startswith("cat_"):

        category = query.data.split("_")[1]

        text = context.user_data.get("pending")

        if not text:
            await query.edit_message_text(
                "❌ Missing confession."
            )
            return

        cursor.execute("""
        INSERT INTO confessions
        (text, category, created_at)
        VALUES (?, ?, ?)
        """, (
            text,
            category,
            int(time.time())
        ))

        confession_id = cursor.lastrowid

        conn.commit()

        user_id = query.from_user.id
        add_submission(user_id)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"approve:{confession_id}"
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"reject:{confession_id}"
                ),
            ]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🕯️ {category.upper()} "
                f"Confession #{confession_id}\n\n{text}"
            ),
            reply_markup=keyboard
        )

        context.user_data.clear()

        await query.edit_message_text(
            "🕯️ Submitted anonymously."
        )

        return

    # APPROVE / REJECT
    action, confession_id = query.data.split(":")
    confession_id = int(confession_id)

    cursor.execute("""
    SELECT text, category
    FROM confessions
    WHERE id=?
    """, (confession_id,))

    row = cursor.fetchone()

    if not row:
        await query.edit_message_text(
            "❌ Confession not found."
        )
        return

    text, category = row

    # APPROVE
    if action == "approve":

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📝 Submit Confession",
                    url=f"https://t.me/{BOT_USERNAME}"
                )
            ]
        ])

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=(
                f"🕯️ {category.upper()} "
                f"Confession #{confession_id}\n\n{text}"
            ),
            reply_markup=keyboard
        )

        cursor.execute("""
        UPDATE confessions
        SET status='approved'
        WHERE id=?
        """, (confession_id,))

        conn.commit()

        await query.edit_message_text(
            f"✅ Approved #{confession_id}"
        )

    # REJECT
    elif action == "reject":

        cursor.execute("""
        UPDATE confessions
        SET status='rejected'
        WHERE id=?
        """, (confession_id,))

        conn.commit()

        await query.edit_message_text(
            f"❌ Rejected #{confession_id}"
        )

# =========================
# APP
# =========================

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(
    CommandHandler("start", start)
)

app.add_handler(
    MessageHandler(
        filters.TEXT &
        filters.ChatType.PRIVATE &
        ~filters.COMMAND,
        handle_confession
    )
)

app.add_handler(
    CallbackQueryHandler(button_handler)
)

print("🕯️ Bot is running...")

app.run_polling()