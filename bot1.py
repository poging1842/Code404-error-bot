#!/usr/bin/env python3
"""
Made By @Maisanyvokei
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

import aiosqlite
from telegram import (
    Document, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "8895975047:AAE_eXzUepLl7GN1AkRT4Z-nakCYjCqDwrU")
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS", "8695363498").split(",") if x.strip()]
DB_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── DATABASE BOOTSTRAP ───────────────────────────────────────────────────────
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS stock (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                account     TEXT    NOT NULL,
                tier        INTEGER DEFAULT 25,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claimed_by  INTEGER DEFAULT NULL,
                claimed_at  TIMESTAMP DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                joined_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_redeem   TIMESTAMP DEFAULT NULL,
                total_redeems INTEGER DEFAULT 0,
                balance       REAL    DEFAULT 0.0,
                is_banned     INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                account   TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                action    TEXT NOT NULL,
                user_id   INTEGER,
                details   TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Safe migration for stock tier column if table already existed
        try:
            await db.execute("ALTER TABLE stock ADD COLUMN tier INTEGER DEFAULT 25;")
        except Exception:
            pass

        await db.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES
            ('account_price',   '50'),
            ('redeem_enabled',  '1'),
            ('welcome_msg',     '⚡ **Welcome to the Digital Vault!**\n\nChoose an option below to manage your balance or acquire entries.'),
            ('topup_msg',       '💎 **Balance Top-Up Portal:**\n\nTo load funds into your account, reach out directly to the administrator (@Maisanyvokei) with your payment receipt.'),
            ('help_msg',        '💡 **Quick Navigation Guide:**\n\n1. **Add Balance:** Contact admin to reload funds.\n2. **Instant Grab TXT:** Purchase accounts securely using your balance.\n3. **Vouches:** Share your screenshots or feedback with the community!')
        """)
        await db.commit()


# ─── DB HELPERS ───────────────────────────────────────────────────────────────
async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""

async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()

async def get_stock_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM stock WHERE claimed_by IS NULL"
        ) as cur:
            return (await cur.fetchone())[0]

async def get_stock_count_by_tier(tier: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM stock WHERE claimed_by IS NULL AND tier = ?", (tier,)
        ) as cur:
            return (await cur.fetchone())[0]
            
            
async def add_stock(accounts: list[str], tier: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO stock (account, tier) VALUES (?, ?)", [(a, tier) for a in accounts]
        )
        await db.commit()
    return len(accounts)


async def buy_account_tier(user_id: int, price: float) -> tuple[bool, Optional[str]]:
    balance = await get_user_balance(user_id)
    tier_target = int(price)  # Matches 5 or 25
    
    print(f"DEBUG BUY_TIER: User {user_id} requested ₱{tier_target} tier. Current balance: ₱{balance}")

    if balance < price:
        return False, f"❌ Insufficient balance. You need <b>₱{price:.2f}</b>, but your balance is <b>₱{balance:.2f}</b>."

    async with aiosqlite.connect(DB_PATH) as db:
        # 1. First find the available stock ID for this specific tier
        async with db.execute(
            "SELECT id, account FROM stock WHERE claimed_by IS NULL AND tier = ? ORDER BY id LIMIT 1",
            (tier_target,)
        ) as cur:
            row = await cur.fetchone()
            
        if not row:
            print(f"DEBUG BUY_TIER: No stock found for tier ₱{tier_target}!")
            return False, f"❌ No stock available right now for the ₱{tier_target} tier. Check back later."
        
        stock_id, account = row
        print(f"DEBUG BUY_TIER: Found stock ID {stock_id}. Proceeding with deduction.")
        
        # 2. Deduct balance and claim stock safely inside the same transaction
        await db.execute(
            "UPDATE users SET balance = balance - ?, total_redeems = total_redeems + 1, last_redeem = CURRENT_TIMESTAMP WHERE user_id = ?",
            (price, user_id),
        )
        await db.execute(
            "UPDATE stock SET claimed_by = ?, claimed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user_id, stock_id),
        )
        await db.execute(
            "INSERT INTO purchases (user_id, account) VALUES (?, ?)",
            (user_id, account)
        )
        await db.commit()
        
    return True, account

    
async def claim_account(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, account FROM stock WHERE claimed_by IS NULL ORDER BY id LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        stock_id, account = row
        await db.execute(
            "UPDATE stock SET claimed_by=?, claimed_at=CURRENT_TIMESTAMP WHERE id=?",
            (user_id, stock_id),
        )
        await db.execute(
            """UPDATE users
               SET last_redeem=CURRENT_TIMESTAMP, total_redeems=total_redeems+1
               WHERE user_id=?""",
            (user_id,),
        )
        await db.commit()
    return account

async def clear_unclaimed_stock() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM stock WHERE claimed_by IS NULL")
        await db.commit()

async def get_or_create_user(update: Update) -> None:
    u = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, username, first_name)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username, first_name=excluded.first_name""",
            (u.id, u.username, u.first_name),
        )
        await db.commit()

async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in cur.description], row))

async def set_banned(user_id: int, state: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (state, user_id))
        await db.commit()

async def get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, username, first_name, total_redeems, is_banned, joined_at "
            "FROM users ORDER BY joined_at DESC"
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) async for row in cur]

async def log_action(action: str, user_id: int = None, details: str = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (action, user_id, details) VALUES (?, ?, ?)",
            (action, user_id, details),
        )
        await db.commit()

async def get_recent_logs(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) async for row in cur]

async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async def scalar(q, *p):
            async with db.execute(q, p) as c:
                return (await c.fetchone())[0]

        return {
            "available":    await scalar("SELECT COUNT(*) FROM stock WHERE claimed_by IS NULL"),
            "claimed":      await scalar("SELECT COUNT(*) FROM stock WHERE claimed_by IS NOT NULL"),
            "total_users":  await scalar("SELECT COUNT(*) FROM users"),
            "banned":       await scalar("SELECT COUNT(*) FROM users WHERE is_banned=1"),
            "active_today": await scalar(
                "SELECT COUNT(*) FROM users WHERE last_redeem > datetime('now','-24 hours')"
            ),
        }

async def get_claimed_export() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT account, claimed_by, claimed_at FROM stock "
            "WHERE claimed_by IS NOT NULL ORDER BY claimed_at DESC"
        ) as cur:
            return await cur.fetchall()
            
# ─── Balance Flow ─────────────────────────────────────────────────────────────
async def get_user_balance(user_id: int) -> float:
    user = await get_user(user_id)
    return user["balance"] if user else 0.0

async def update_balance(user_id: int, amount: float) -> float:
    """Add amount to a user's balance and return the balance saved in the DB."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, balance)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance = users.balance + excluded.balance
            """,
            (user_id, amount),
        )
        await db.commit()

        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()

        new_balance = float(row[0]) if row else 0.0
        log.info(
            "BALANCE UPDATE | target=%s | added=%.2f | new_balance=%.2f | db=%s",
            user_id, amount, new_balance, DB_PATH
        )
        return new_balance

async def buy_account(user_id: int) -> tuple[bool, Optional[str]]:
    """Returns (success, account_or_error_message)."""
    price = float(await get_setting("account_price") or "0")
    balance = await get_user_balance(user_id)

    if balance < price:
        return False, f"❌ Insufficient balance. You need <b>{price}</b>, but you have <b>{balance}</b>. Contact admin to recharge."

    async with aiosqlite.connect(DB_PATH) as db:
        # Grab stock
        async with db.execute(
            "SELECT id, account FROM stock WHERE claimed_by IS NULL ORDER BY id LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False, "❌ No stock available right now. Check back later."
        
        stock_id, account = row
        
        # Deduct balance and assign stock
        await db.execute(
            "UPDATE users SET balance = balance - ?, total_redeems = total_redeems + 1, last_redeem = CURRENT_TIMESTAMP WHERE user_id = ?",
            (price, user_id),
        )
        await db.execute(
            "UPDATE stock SET claimed_by = ?, claimed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user_id, stock_id),
        )
        await db.commit()
    return True, account

# ─── STOCK PARSER ─────────────────────────────────────────────────────────────
def parse_stock_file(raw: str) -> list[str]:
    """Split by the dashed line separator; strip each chunk; drop empties."""
    # You can split by the dashed line format found in your text files
    delimiter = "------------------------------------------------------------"
    
    # Fallback support: handles if they use '==' or the dashed line
    if delimiter in raw:
        chunks = raw.split(delimiter)
    else:
        chunks = raw.split("==")
        
    return [chunk.strip() for chunk in chunks if chunk.strip() and "Account:" in chunk]


# ─── COOLDOWN CHECK ───────────────────────────────────────────────────────────
async def check_cooldown(user_id: int) -> tuple[bool, Optional[str]]:
    """Returns (can_redeem, reason_string_or_None)."""
    user = await get_user(user_id)
    if not user:
        return True, None
    if user["is_banned"]:
        return False, "banned"
    if not user["last_redeem"]:
        return True, None
    cooldown_h = float(await get_setting("cooldown_hours") or "24")
    if cooldown_h == 0:
        return True, None
    last      = datetime.fromisoformat(str(user["last_redeem"]))
    next_time = last + timedelta(hours=cooldown_h)
    now       = datetime.utcnow()
    if now < next_time:
        rem  = next_time - now
        h, r = divmod(int(rem.total_seconds()), 3600)
        m    = r // 60
        return False, f"{h}h {m}m"
    return True, None

# ─── GUARD ────────────────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Buy Account", callback_data="buy_account"),
            InlineKeyboardButton("💎 Top Up",      callback_data="top_up"),
        ],
        [
            InlineKeyboardButton("🛸 My Profile",     callback_data="user_account"),
            InlineKeyboardButton("🔥 Feedbacks",        callback_data="user_feedback"),
            InlineKeyboardButton("📜 Logs",           callback_data="user_history"),
        ],
        [
            InlineKeyboardButton("💡 Guide & Info",   callback_data="user_help"),
        ]
    ])
    return keyboard


def kb_users() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 List Users",   callback_data="admin_list_users")],
        [
            InlineKeyboardButton("🚫 Ban",       callback_data="admin_ban_prompt"),
            InlineKeyboardButton("✅ Unban",     callback_data="admin_unban_prompt"),
        ],
        [InlineKeyboardButton("🔙 Back",         callback_data="admin_panel")],
    ])
    return keyboard

def kb_admin_panel() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Stock",      callback_data="admin_stock"),
            InlineKeyboardButton("👥 Users",      callback_data="admin_users"),
        ],
        [       
            InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_balance_prompt"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast",  callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Stats",      callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("📋 Logs",       callback_data="admin_logs"),
            InlineKeyboardButton("⚙️ Settings",   callback_data="admin_settings"),
        ],
        [InlineKeyboardButton("✖️ Close",         callback_data="admin_close")],
    ])
    return keyboard
    
def kb_buy_tiers() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ ₱25 — 1 Account", callback_data="buy_tier_25")],
        [InlineKeyboardButton("🛍️ ₱5 — 1 Account",  callback_data="buy_tier_5")],
        [InlineKeyboardButton("🔙 Back",              callback_data="user_stock_back")],
    ])
    return keyboard
    
    
def kb_stock() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Add ₱25 Stock (.txt)", callback_data="admin_add_stock_25")],
        [InlineKeyboardButton("📤 Add ₱5 Stock (.txt)",  callback_data="admin_add_stock_5")],
        [
            InlineKeyboardButton("📊 Count",               callback_data="admin_stock_count"),
            InlineKeyboardButton("🗑 Clear Unclaimed",     callback_data="admin_clear_stock"),
        ],
        [InlineKeyboardButton("📥 Export Claimed",         callback_data="admin_export_claimed")],
        [InlineKeyboardButton("🔙 Back",                   callback_data="admin_panel")],
    ])
    return keyboard


def kb_users() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 List Users",   callback_data="admin_list_users")],
        [
            InlineKeyboardButton("🚫 Ban",       callback_data="admin_ban_prompt"),
            InlineKeyboardButton("✅ Unban",     callback_data="admin_unban_prompt"),
        ],
        [InlineKeyboardButton("🔙 Back",         callback_data="admin_panel")],
    ])
    return keyboard

def kb_settings() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Set Cooldown",      callback_data="admin_set_cooldown")],
        [InlineKeyboardButton("📝 Set Welcome Msg",  callback_data="admin_set_welcome")],
        [
            InlineKeyboardButton("🟢 Enable Redeem", callback_data="admin_enable_redeem"),
            InlineKeyboardButton("🔴 Disable Redeem",callback_data="admin_disable_redeem"),
        ],
        [InlineKeyboardButton("🔙 Back",             callback_data="admin_panel")],
    ])
    return keyboard

def kb_back(target: str, label: str = "🔙 Back") -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])
    return keyboard

def kb_topup_admin(user_id: int, amount: float):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"topup_approve_{user_id}_{amount}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"topup_deny_{user_id}_{amount}")
        ]
    ])
    
def kb_confirm_clear() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, clear it", callback_data="admin_clear_confirm"),
            InlineKeyboardButton("❌ Cancel",        callback_data="admin_stock"),
        ]
    ])
    return keyboard

# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await get_or_create_user(update)
    u = update.effective_user
    
    # Clean up previous bot message if tracked to avoid clutter
    last_bot_msg_id = ctx.user_data.get("last_bot_msg_id")
    if last_bot_msg_id:
        try:
            await ctx.bot.delete_message(chat_id=update.effective_chat.id, message_id=last_bot_msg_id)
        except Exception:
            pass

    # Try to delete the user's /start text message to keep chat clean
    try:
        await update.message.delete()
    except Exception:
        pass

    balance = await get_user_balance(u.id)
    stock = await get_stock_count()
    username_display = f"@{u.username}" if u.username else "None"
    
    welcome_text = (
        f"🐱🔥 <b>𝙒𝙀𝙇𝘾𝙊𝙈𝙀 𝙏𝙊 404𝙀𝙍𝙍𝙊𝙍 𝙎𝙃𝙊𝙋</b> 🔥🐱\n\n"
        f"💳 <b>𝘼𝘾𝘾𝙊𝙐𝙉𝙏 𝙋𝙍𝙊𝙁𝙄𝙇𝙀 𝙄𝙉𝙁𝙊</b>\n"
        f"🔹 <b>𝙉𝙖𝙢𝙚:</b> {u.first_name}\n"
        f"🔹 <b>𝙏𝙚𝙡𝙚𝙜𝙧𝙖𝙢 𝙄𝘿:</b> <code>{u.id}</code>\n"
        f"🔹 <b>𝙐𝙨𝙚𝙧𝙣𝙖𝙢𝙚:</b> {username_display}\n\n"
        f"💰 <b>𝙒𝙖𝙡𝙡𝙚𝙩 𝘽𝙖𝙡𝙖𝙣𝙘𝙚:</b> <b>{balance:.2f} pesos</b>\n"
        f"📦 <b>𝙍𝙚𝙖𝙙𝙮 𝙎𝙩𝙤𝙘𝙠:</b> <b>{stock} accounts</b>\n\n"
        f"👇 <i>𝙋𝙡𝙚𝙖𝙨𝙚 𝙘𝙝𝙤𝙤𝙨𝙚 𝙖 𝙘𝙖𝙩𝙚𝙜𝙤𝙧𝙮 𝙗𝙚𝙡𝙤𝙬 𝙩𝙤 𝙨𝙩𝙖𝙧𝙩 𝙨𝙝𝙤𝙥𝙥𝙞𝙣𝙜!</i>"
    )
    
    sent_msg = await update.effective_chat.send_message(
        welcome_text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main(),
    )
    # Save this message ID so we can clean it up later
    ctx.user_data["last_bot_msg_id"] = sent_msg.message_id



async def cmd_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await get_or_create_user(update)
    uid = update.effective_user.id

    if await get_setting("redeem_enabled") == "0":
        await update.message.reply_text("❌ Redeeming is currently disabled.")
        return

    ok, reason = await check_cooldown(uid)
    if not ok:
        msg = "🚫 You are banned." if reason == "banned" else \
              f"⏳ Cooldown active — try again in <b>{reason}</b>."
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    account = await claim_account(uid)
    if not account:
        await update.message.reply_text("❌ No stock available right now. Check back later.")
        await log_action("redeem_fail_empty", uid)
        return

    await log_action("redeem_success", uid, f"account={account[:30]}")
    await update.message.reply_text(
        f"✅ <b>Account claimed!</b>\n\n"
        f"<code>{account}</code>\n\n"
        f"⚠️ Save this — it won't be shown again.",
        parse_mode=ParseMode.HTML,
    )

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Unauthorized.")
        return
    await update.message.reply_text(
        "🛡 <b>Admin Panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_admin_panel(),
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("awaiting", None)
    await update.message.reply_text("❌ Cancelled.")

async def broadcast_to_all_users(bot, text: str, keyboard=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            users = await cur.fetchall()
    
    for (user_id,) in users:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
        except Exception:
            # Skip users who blocked the bot or deleted their chat
            pass

async def bot_broadcast_purchase(bot, user_name: str, price: float):
    text = (
        f"🚨💎 <b>𝙇𝙄𝙑𝙀 𝙑𝘼𝙐𝙇𝙏 𝙊𝙍𝘿𝙀𝙍!</b> 💎🚨\n\n"
        f"👤 <b>𝙊𝙥𝙚𝙧𝙖𝙩𝙤𝙧:</b> {user_name}\n"
        f"💳 𝙅𝙪𝙨𝙩 𝙖𝙘𝙦𝙪𝙞𝙧𝙚𝙙 𝙖 <b>₱{price:.2f}</b> 𝘾𝙊𝘿𝙈 𝘼𝙘𝙘𝙤𝙪𝙣𝙩 𝙋𝙖𝙨𝙨\n"
        f"🎯 𝙋𝙧𝙤𝙙𝙪𝙘𝙩: 𝙑𝙚𝙧𝙞𝙛𝙞𝙚𝙙 𝙏𝙖𝙘𝙩𝙞𝙘𝙖𝙡 𝘽𝙪𝙣𝙙𝙡𝙚\n"
        f"⚡ 𝘿𝙚𝙡𝙞𝙫𝙚𝙧𝙚𝙙 𝙖𝙪𝙩𝙤𝙢𝙖𝙩𝙞𝙘𝙖𝙡𝙡𝙮 𝙞𝙣 𝙨𝙚𝙘𝙤𝙣𝙙𝙨!\n\n"
        f"🛒 𝙏𝙖𝙥 𝙩𝙝𝙚 𝙗𝙪𝙩𝙩𝙤𝙣 𝙗𝙚𝙡𝙤𝙬 𝙩𝙤 𝙗𝙧𝙤𝙬𝙨𝙚 𝙖𝙣𝙙 𝙗𝙪𝙮 𝙣𝙤𝙬.\n"
        f"🔒 <i>𝙎𝙚𝙘𝙪𝙧𝙚 𝘼𝙪𝙩𝙤𝙢𝙖𝙩𝙚𝙙 𝘿𝙞𝙜𝙞𝙩𝙖𝙡 𝙎𝙩𝙤𝙧𝙚</i>"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ Open Vault & Buy Now", url="https://t.me/Code404errorrr_bot")] # Replace with your bot link
    ])
    
    await broadcast_to_all_users(bot, text, keyboard)


async def bot_broadcast_topup(bot, user_name: str, amount: float):
    text = (
        f"💎⚡ <b>𝙑𝘼𝙐𝙇𝙏 𝙍𝙀𝘾𝙃𝘼𝙍𝙂𝙀 𝘼𝙇𝙀𝙍𝙏!</b> ⚡💎\n\n"
        f"👤 <b>𝙊𝙥𝙚𝙧𝙖𝙩𝙤𝙧:</b> {user_name}\n"
        f"💰 𝙎𝙪𝙘𝙘𝙚𝙨𝙨𝙛𝙪𝙡𝙡𝙮 𝙡𝙤𝙖𝙙𝙚𝙙 <b>₱{amount:.2f} PHP</b> 𝙗𝙖𝙡𝙖𝙣𝙘𝙚\n"
        f"✅ 𝙎𝙩𝙖𝙩𝙪𝙨: 𝙑𝙚𝙧𝙞𝙛𝙞𝙚𝙙 𝙖𝙣𝙙 𝙘𝙧𝙚𝙙𝙞𝙩𝙚𝙙 𝙞𝙣𝙨𝙩𝙖𝙣𝙩𝙡𝙮!\n\n"
        f"🚀 𝙏𝙖𝙥 𝙩𝙝𝙚 𝙗𝙪𝙩𝙩𝙤𝙣 𝙗𝙚𝙡𝙤𝙬 𝙩𝙤 𝙩𝙤𝙥 𝙪𝙥 𝙮𝙤𝙪𝙧 𝙤𝙬𝙣 𝙗𝙖𝙡𝙖𝙣𝙘𝙚.\n"
        f"🔐 <i>𝙎𝙚𝙘𝙪𝙧𝙚 𝘼𝙪𝙩𝙤𝙢𝙖𝙩𝙚𝙙 𝘿𝙞𝙜𝙞𝙩𝙖𝙡 𝙎𝙩𝙤𝙧𝙚</i>"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Top Up Balance Now", url="https://t.me/Code404errorrr_bot")] # Replace with your bot link
    ])
    
    await broadcast_to_all_users(bot, text, keyboard)


# ─── CALLBACK ROUTER ──────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    ctx = context

    # 🔍 ADD THIS LINE TO SEE WHAT BUTTON WAS CLICKED:
    print(f"DEBUG CALLBACK: Received data = '{data}'")

    # Your existing handlers start here...
    if data == "top_up":
        ctx.user_data["awaiting"] = "topup_amount"
        await q.edit_message_text(
            "💎 <b>Balance Top-Up Portal</b>\n\n"
            "Please enter the amount you wish to top up (<b>Minimum: ₱50</b>):\n\n"
            "<i>Type your amount as a number (e.g. 100). Use /cancel to abort.</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    
    # ... rest of your code ...


    # ── STOCK FILE ──────────────────────────────────────────────────────────
    if data == "buy_account":
        if await get_setting("redeem_enabled") == "0":
            await q.answer("❌ Buying accounts is currently disabled.", show_alert=True)
            return
        
        # Clean up the old menu message completely
        try:
            await q.message.delete()
        except Exception:
            pass

        # Fetch live stock counts for both tiers from the database
        stock_25 = await get_stock_count_by_tier(25)
        stock_5 = await get_stock_count_by_tier(5)

        # Send the tier menu with stock numbers and your custom risk/chance descriptions
        await ctx.bot.send_message(
            chat_id=uid,
            text = (
                 f"🛍️ <b>Choose Your Account Tier</b>\n\n"
                 f"━━━━━━━━━━━━━━━━━━━\n"
                 f"🔥 <b>₱25 Tier Account Bundle</b>\n"
                 f"📦 <b>Stock Available:</b> <b>{stock_25}</b> accounts\n"
                 f"📊 <b>Details:</b> 50% chance of a good account\n"
                 f"━━━━━━━━━━━━━━━━━━━\n"
                 f"⭐ <b>₱5 Tier Account Bundle</b>\n"
                 f"📦 <b>Stock Available:</b> <b>{stock_5}</b> accounts\n"
                 f"📊 <b>Details:</b> 25% to 50% chance of a working account.\n"
                 f"━━━━━━━━━━━━━━━━━━━\n\n"
                 f"👇 Select an option below to purchase:"
           ),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_buy_tiers(),
        )
        return

    if data in ["buy_tier_25", "buy_tier_5"]:
        price = 25.0 if data == "buy_tier_25" else 5.0
        await get_or_create_user(update)
        
        if await get_setting("redeem_enabled") == "0":
            await q.edit_message_text("❌ Buying accounts is currently disabled.")
            return
        
        success, result = await buy_account_tier(uid, price)
        if not success:
            await q.edit_message_text(result, parse_mode=ParseMode.HTML, reply_markup=kb_buy_tiers())
            return

        await log_action("buy_success", uid, f"price={price}")
        
        # Delete the tier selection menu
        try:
            await q.message.delete()
        except Exception:
            pass

        # Safely escape the account string so characters like < or > don't crash HTML formatting
        import html
        safe_result = html.escape(result)

        # Send the clean text inside code tags
        await ctx.bot.send_message(
            chat_id=uid,
            text=f"✅ <b>Purchase Successful (₱{price:.0f} Tier)!</b>\n\n"
                 f"<code>{safe_result}</code>\n\n"
                 f"⚠️ Save this account info — it won't be shown again.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🏠 Main Menu"),
        )
        return


    if data.startswith("topup_approve_") or data.startswith("topup_deny_"):
        if not is_admin(uid):
            await q.answer("❌ Unauthorized action.", show_alert=True)
            return
            
        parts = data.split("_")
        action = parts[1] # "approve" or "deny"
        target_user_id = int(parts[2])
        amount = float(parts[3])
        admin_name = update.effective_user.first_name

        if action == "approve":
            # Add balance to user in database
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, target_user_id)
                )
                await db.commit()
            
            # Notify the user
            try:
                await ctx.bot.send_message(
                    chat_id=target_user_id,
                    text=f"🎉 <b>Top-Up Approved!</b>\n\n"
                         f"Your payment of <b>₱{amount:.2f}</b> has been verified and added to your balance.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            
            # Update the admin message caption and remove buttons
            await q.edit_message_caption(
                caption=q.message.caption + f"\n\n<b>STATUS: APPROVED ✅ by {admin_name}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
            await q.answer("✅ Top-up approved and balance credited!", show_alert=True)
            
        else: # deny
            # Notify the user
            try:
                await ctx.bot.send_message(
                    chat_id=target_user_id,
                    text=f"❌ <b>Top-Up Denied</b>\n\n"
                         f"Your top-up request for <b>₱{amount:.2f}</b> was rejected. Please contact support if you think this is a mistake.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            
            # Update the admin message caption and remove buttons
            await q.edit_message_caption(
                caption=q.message.caption + f"\n\n<b>STATUS: DENIED ❌ by {admin_name}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
            await q.answer("❌ Top-up denied.", show_alert=True)
        return
 
    # ── USER HISTORY / LOGS ────────────────────────────────────────────────
    if data == "user_history":
        try:
            await q.message.delete()
        except Exception:
            pass
            
        rows = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Try fetching general columns if they exist, or safely fallback
                async with db.execute(
                    "SELECT * FROM purchases WHERE user_id = ? ORDER BY id DESC LIMIT 5",
                    (uid,)
                ) as cursor:
                    rows = await cursor.fetchall()
        except Exception as e:
            print(f"Log fetch warning: {e}")
                
        log_text = "📜 <b>Your Recent Transaction Logs</b>\n\n"
        if not rows:
            log_text += "<i>No purchase history found yet. Grab an account bundle to get started!</i>"
        else:
            for row in rows:
                # row structure usually looks like (id, user_id, ...), let's display what we safely can
                log_text += f"▪️ <b>Purchase Record ID #{row[0]}</b> — Registered Successfully\n"
                
        await ctx.bot.send_message(
            chat_id=uid,
            text=log_text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🏠 Main Menu")
        )
        return


    # ── USER HELP / GUIDE ──────────────────────────────────────────────────
    if data == "user_help":
        try:
            await q.message.delete()
        except Exception:
            pass
            
        guide_text = (
            "📖 <b>CODM Shop User Guide</b>\n\n"
            "<b>1. How to Top Up:</b>\n"
            "• Click the <b>Top Up</b> button.\n"
            "• Enter your desired amount (Minimum ₱50).\n"
            "• Upload a clear screenshot of your payment receipt.\n"
            "• Wait for admin approval (balance credited automatically).\n\n"
            "<b>2. How to Buy Accounts:</b>\n"
            "• Ensure you have sufficient balance.\n"
            "• Choose between the <b>₱5 Tier</b> or <b>₱25 Tier</b>.\n"
            "• The bot will instantly dispatch your account details as a clean <code>.txt</code> file!\n\n"
            "<i>Need help? Contact the support team. @Zaraaarhh</i>"
        )
        
        await ctx.bot.send_message(
            chat_id=uid,
            text=guide_text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🏠 Main Menu")
        )
        return

    # ── INFO / SHOP DETAILS ─────────────────────────────────────────────────
    if data == "info":
        try:
            await q.message.delete()
        except Exception:
            pass
            
        info_text = (
            "ℹ️ <b>Store Information & FAQ</b>\n\n"
            "⚡ <b>Automated Delivery:</b> All accounts are sent directly via text files instantly upon purchase.\n"
            "🛡️ <b>Secure System:</b> Fully protected balance system with verified manual receipt approvals.\n"
            "💎 <b>Tier System:</b> Higher tiers feature better odds and higher-value inventories.\n\n"
            "🚀 <i>Thank you for supporting our digital vault!</i>"
        )
        
        await ctx.bot.send_message(
            chat_id=uid,
            text=info_text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🏠 Main Menu")
        )
        return
                   
# ── USER FLOWS ────────────────────────────────────────────────────────────
    if data == "redeem":
        await get_or_create_user(update)
        if await get_setting("redeem_enabled") == "0":
            await q.edit_message_text("❌ Redeeming is currently disabled.")
            return
        ok, reason = await check_cooldown(uid)
        if not ok:
            msg = "🚫 You are banned." if reason == "banned" else \
                  f"⏳ Cooldown active — try again in <b>{reason}</b>."
            await q.edit_message_text(msg, parse_mode=ParseMode.HTML)
            return
        account = await claim_account(uid)
        if not account:
            await q.edit_message_text("❌ Stock is empty right now.")
            return
        await log_action("redeem_success", uid)
        await q.edit_message_text(
            f"✅ <b>Account claimed!</b>\n\n"
            f"<code>{account}</code>\n\n"
            f"⚠️ Save this — it won't be shown again.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "user_account":
        u = q.from_user
        # Fetch fresh live balance directly from the database table
        balance = await get_user_balance(uid)
        user_data_db = await get_user(uid)
        
        total_bought = user_data_db["total_redeems"] if user_data_db else 0
        joined = str(user_data_db["joined_at"])[:10] if user_data_db else "Unknown"
        username_display = f"@{u.username}" if u.username else "None"
        
        await q.edit_message_text(
            f"🐱🔥 <b>ACCOUNT PROFILE INFO</b> 🔥🐱\n\n"
            f"🔹 <b>Name:</b> {u.first_name}\n"
            f"🔹 <b>Telegram ID:</b> <code>{uid}</code>\n"
            f"🔹 <b>Username:</b> {username_display}\n\n"
            f"💰 <b>Wallet Balance:</b> <b>₱{balance:.2f} pesos</b>\n"
            f"📦 <b>Total Purchases:</b> <b>{total_bought} accounts</b>\n"
            f"📅 <b>Joined:</b> {joined}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🔙 Back"),
        )
        return

        
    if data == "my_balance":
        balance = await get_user_balance(uid)
        price = await get_setting("account_price")
        await q.edit_message_text(
            f"💰 <b>Your Profile</b>\n\n"
            f"Balance: <b>{balance}</b>\n"
            f"Account Price: <b>{price}</b>\n\n"
            f"<i>Contact the admin to recharge your balance.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🔙 Back"),
        )
        return

    if data == "user_feedback":
        ctx.user_data["awaiting"] = "feedback_send"
        await q.edit_message_text(
            "🔥 **Community Vouch & Feedback**\n\n"
            "Send your screenshots, media proof, or text notes now.\n"
            "Your vouch will be broadcasted live to all users!\n\n"
            "Use /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return
    
    if data == "buy_account":
        if await get_setting("redeem_enabled") == "0":
            await q.edit_message_text("❌ Buying accounts is currently disabled.")
            return
        await q.edit_message_text(
            "🛍️ <b>Choose Your Account Tier</b>\n\nSelect a package option below:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_buy_tiers(),
        )
        return

        
        success, result = await buy_account(uid)
        if not success:
            await q.edit_message_text(result, parse_mode=ParseMode.HTML, reply_markup=kb_back("user_stock_back", "🔙 Back"))
            return

        await log_action("buy_success", uid)
        await q.edit_message_text(
            f"✅ <b>Purchase Successful!</b>\n\n"
            f"<code>{result}</code>\n\n"
            f"⚠️ Save this account info — it won't be shown again.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "admin_recharge_prompt":
        ctx.user_data["awaiting"] = "recharge_user"
        await q.edit_message_text(
            "💳 <b>Recharge User</b>\n\nSend the message in this format: <code>USER_ID AMOUNT</code>\nExample: <code>123456789 100</code>",
            parse_mode=ParseMode.HTML,
        )
        return
        
        
    if data == "user_stock":
        stock = await get_stock_count()
        await q.edit_message_text(
            f"📦 Accounts available: <b>{stock}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🔙 Back"),
        )
        return

    if data == "user_stock_back":
        u       = q.from_user
        welcome = await get_setting("welcome_msg")
        stock   = await get_stock_count()
        await q.edit_message_text(
            f"👋 Hey <b>{u.first_name}</b>!\n\n{welcome}\n\n📦 Accounts in stock: <b>{stock}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(),
        )
        return

    # ── ADMIN GUARD ───────────────────────────────────────────────────────────
    if not is_admin(uid):
        await q.answer("❌ Unauthorized.", show_alert=True)
        return

    # ── ADMIN PANEL ───────────────────────────────────────────────────────────
    if data == "admin_panel":
        await q.edit_message_text(
            "🛡 <b>Admin Panel</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_admin_panel(),
        )

    elif data == "admin_close":
        await q.delete_message()

    # ── STOCK ─────────────────────────────────────────────────────────────────
    elif data == "admin_stock":
        stock = await get_stock_count()
        await q.edit_message_text(
            f"📦 <b>Stock Management</b>\n\n"
            f"Available: <b>{stock} accounts</b>\n\n"
            f"Upload a <code>.txt</code> file with accounts separated by <code>==</code>\n\n"
            f"<b>Format Example:</b>\n"
            f"<code>Account: user:pass\n"
            f"UID: 123456\n"
            f"Server: PH\n"
            f"==\n"
            f"Account: user2:pass2\n"
            f"UID: 654321\n"
            f"Server: PH</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_stock(),
        )


    elif data == "admin_add_stock":
        ctx.user_data["awaiting"] = "stock_file"
        await q.edit_message_text(
            "📤 <b>Add Stock</b>\n\n"
            "Send a <code>.txt</code> file now.\n"
            "Accounts must be separated by <code>==</code>\n\n"
            "Use /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )

    elif data == "admin_stock_count":
        s = await get_stats()
        await q.edit_message_text(
            f"📊 <b>Stock Report</b>\n\n"
            f"✅ Available: <b>{s['available']}</b>\n"
            f"🎁 Claimed:   <b>{s['claimed']}</b>\n"
            f"📦 Total:     <b>{s['available'] + s['claimed']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin_stock"),
        )

    elif data == "admin_clear_stock":
        await q.edit_message_text(
            "⚠️ <b>Confirm Clear</b>\n\n"
            "Permanently delete all <b>unclaimed</b> stock. Continue?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_confirm_clear(),
        )

    elif data == "admin_clear_confirm":
        await clear_unclaimed_stock()
        await log_action("clear_stock", uid)
        await q.edit_message_text(
            "🗑 Unclaimed stock cleared.",
            reply_markup=kb_back("admin_stock"),
        )

    elif data == "admin_export_claimed":
        rows = await get_claimed_export()
        if not rows:
            await q.answer("No claimed accounts yet.", show_alert=True)
            return
        lines = [f"Account: {r[0]}\nClaimed by: {r[1]}\nAt: {r[2]}" for r in rows]
        content = "\n==\n".join(lines)
        buf = BytesIO(content.encode())
        buf.name = "claimed_accounts.txt"
        await ctx.bot.send_document(
            uid, document=buf, filename="claimed_accounts.txt",
            caption=f"📋 {len(rows)} claimed accounts"
        )
        await q.answer("Exported.", show_alert=True)
        
    elif data == "admin_add_balance_prompt":
        ctx.user_data["awaiting"] = "admin_add_balance"
        await q.edit_message_text(
            "➕ <b>Add Balance to User</b>\n\n"
            "Send the details in this format: <code>USER_ID AMOUNT</code>\n"
            "Example: <code>8695363498 100</code>\n\n"
            "Use /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        
    elif data in ["admin_add_stock_25", "admin_add_stock_5"]:
        tier_val = 25 if data == "admin_add_stock_25" else 5
        ctx.user_data["awaiting"] = "stock_file"
        ctx.user_data["upload_tier"] = tier_val
        await q.edit_message_text(
            f"📤 <b>Add ₱{tier_val} Stock</b>\n\n"
            "Send a <code>.txt</code> file now.\n"
            "Accounts must be separated by <code>==</code>\n\n"
            "Use /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return


        
    # ── USERS ─────────────────────────────────────────────────────────────────
    elif data == "admin_users":
        await q.edit_message_text(
            "👥 <b>User Management</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_users(),
        )

    elif data == "admin_list_users":
        users = await get_all_users()
        if not users:
            await q.answer("No users yet.", show_alert=True)
            return
        lines = []
        for u in users[:30]:
            name   = f"@{u['username']}" if u["username"] else u["first_name"] or "Unknown"
            banned = " 🚫" if u["is_banned"] else ""
            lines.append(
                f"• <code>{u['user_id']}</code> {name}{banned} — {u['total_redeems']} redeems"
            )
        suffix = f"\n\n<i>...and {len(users)-30} more</i>" if len(users) > 30 else ""
        await q.edit_message_text(
            f"👥 <b>Users ({len(users)} total)</b>\n\n" + "\n".join(lines) + suffix,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin_users"),
        )

    elif data == "admin_ban_prompt":
        ctx.user_data["awaiting"] = "ban_id"
        await q.edit_message_text(
            "🚫 <b>Ban User</b>\n\nSend the <b>user ID</b> to ban:",
            parse_mode=ParseMode.HTML,
        )

    elif data == "admin_unban_prompt":
        ctx.user_data["awaiting"] = "unban_id"
        await q.edit_message_text(
            "✅ <b>Unban User</b>\n\nSend the <b>user ID</b> to unban:",
            parse_mode=ParseMode.HTML,
        )

    # ── BROADCAST ─────────────────────────────────────────────────────────────
    elif data == "admin_broadcast":
        ctx.user_data["awaiting"] = "broadcast_msg"
        await q.edit_message_text(
            "📢 <b>Broadcast</b>\n\n"
            "Send the message to blast to all non-banned users.\n"
            "HTML formatting supported.\n\n"
            "Use /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )

    # ── STATS ─────────────────────────────────────────────────────────────────
    elif data == "admin_stats":
        s = await get_stats()
        await q.edit_message_text(
            f"📊 <b>Statistics</b>\n\n"
            f"👥 Total users:  <b>{s['total_users']}</b>\n"
            f"🚫 Banned:       <b>{s['banned']}</b>\n"
            f"🟢 Active today: <b>{s['active_today']}</b>\n\n"
            f"📦 Available:    <b>{s['available']}</b>\n"
            f"🎁 Claimed:      <b>{s['claimed']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin_panel"),
        )

    # ── LOGS ──────────────────────────────────────────────────────────────────
    elif data == "admin_logs":
        logs = await get_recent_logs(20)
        if not logs:
            await q.answer("No logs yet.", show_alert=True)
            return
        lines = []
        for entry in logs:
            ts = str(entry["timestamp"])[:16]
            lines.append(
                f"<code>{ts}</code> | {entry['action']} | <code>{entry['user_id']}</code>"
            )
        await q.edit_message_text(
            "📋 <b>Recent Logs (last 20)</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin_panel"),
        )

    # ── SETTINGS ──────────────────────────────────────────────────────────────
    elif data == "admin_settings":
        cooldown  = await get_setting("cooldown_hours")
        redeem_on = await get_setting("redeem_enabled")
        status    = "🟢 Enabled" if redeem_on == "1" else "🔴 Disabled"
        await q.edit_message_text(
            f"⚙️ <b>Settings</b>\n\n"
            f"⏱ Cooldown: <b>{cooldown}h</b>\n"
            f"🎁 Redeem:  <b>{status}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_settings(),
        )

    elif data == "admin_set_cooldown":
        ctx.user_data["awaiting"] = "set_cooldown"
        await q.edit_message_text(
            "⏱ <b>Set Cooldown</b>\n\n"
            "Send hours as a number (e.g. <code>24</code>, <code>0</code> = no cooldown):",
            parse_mode=ParseMode.HTML,
        )

    elif data == "admin_set_welcome":
        ctx.user_data["awaiting"] = "set_welcome"
        await q.edit_message_text(
            "📝 <b>Set Welcome Message</b>\n\n"
            "Send the new welcome message (HTML supported):",
            parse_mode=ParseMode.HTML,
        )

    elif data == "admin_enable_redeem":
        await set_setting("redeem_enabled", "1")
        await q.answer("✅ Redeem enabled.", show_alert=True)
        cooldown = await get_setting("cooldown_hours")
        await q.edit_message_text(
            f"⚙️ <b>Settings</b>\n\n⏱ Cooldown: <b>{cooldown}h</b>\n🎁 Redeem: <b>🟢 Enabled</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_settings(),
        )

    elif data == "admin_disable_redeem":
        await set_setting("redeem_enabled", "0")
        await q.answer("🔴 Redeem disabled.", show_alert=True)
        cooldown = await get_setting("cooldown_hours")
        await q.edit_message_text(
            f"⚙️ <b>Settings</b>\n\n⏱ Cooldown: <b>{cooldown}h</b>\n🎁 Redeem: <b>🔴 Disabled</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_settings(),
        )

# ─── MESSAGE HANDLER (awaiting state machine) ─────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = update.effective_user.id
    awaiting = ctx.user_data.get("awaiting")


        
# ── RECHARGE USER ───────────────────────────────────────────────────────
    if awaiting == "recharge_user" and is_admin(uid):
        try:
            parts = update.message.text.strip().split()
            target_id = int(parts[0])
            amount = float(parts[1])
            
            await update_balance(target_id, amount)
            await log_action("recharge", uid, f"target={target_id}, amount={amount}")
            ctx.user_data.pop("awaiting", None)
            
            await update.message.reply_text(
                f"✅ Successfully added <b>{amount}</b> to user <code>{target_id}</code> balance.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("admin_users"),
            )
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid format. Use: <code>USER_ID AMOUNT</code> (e.g. <code>123456789 50</code>)", parse_mode=ParseMode.HTML)
        return

    # ── ADMIN ADD BALANCE ────────────────────────────────────────────────────
    if awaiting == "admin_add_balance" and is_admin(uid):
        try:
            parts = update.message.text.strip().split()
            if len(parts) < 2:
                await update.message.reply_text("❌ Format error. Use: <code>USER_ID AMOUNT</code>", parse_mode=ParseMode.HTML)
                return
                
            target_id = int(parts[0])
            amount = float(parts[1])
            
            # Apply balance update
            new_balance = await update_balance(target_id, amount)
            await log_action("admin_add_balance", uid, f"target={target_id}, amount={amount}")
            ctx.user_data.pop("awaiting", None)
            
            # Fetch updated user balance for feedback confirmation
            new_balance = await get_user_balance(target_id)
            
            await update.message.reply_text(
                f"✅ Successfully added <b>₱{amount:.2f}</b> to user <code>{target_id}</code>.\n"
                f"💰 New User Balance: <b>₱{new_balance:.2f}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("admin_users", "👥 User Panel"),
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid numbers provided. Use numeric values for User ID and Amount.")
        return

    # ── TOP-UP RECEIPT STEP ─────────────────────────────────────────────────
    if awaiting == "topup_receipt":
        if not update.message.photo:
            await update.message.reply_text("❌ Please send an image/screenshot of your receipt. Use /cancel to abort.")
            return
            
        amount = ctx.user_data.get("topup_amount", 50)
        photo_file = update.message.photo[-1].file_id 
        
        # Clear state
        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("topup_amount", None)
        
        # Confirm to user
        await update.message.reply_text(
            "✅ <b>Top-Up Request Submitted!</b>\n\n"
            "Your receipt has been forwarded to the admin for verification. Your balance will be credited once approved.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🏠 Main Menu"),
        )
        
        # Forward receipt and details with Approve/Deny buttons to admin(s)
        u = update.effective_user
        username_display = f"@{u.username}" if u.username else "None"
        admin_caption = (
            f"🚨 <b>NEW TOP-UP REQUEST</b>\n\n"
            f"👤 <b>User:</b> {u.first_name} ({username_display})\n"
            f"🆔 <b>User ID:</b> <code>{u.id}</code>\n"
            f"💰 <b>Requested Amount:</b> ₱{amount:.2f}\n\n"
            f"<i>Click a button below to process this top-up:</i>"
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await ctx.bot.send_photo(
                    chat_id=admin_id,
                    photo=photo_file,
                    caption=admin_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_topup_admin(u.id, amount)
                )
            except Exception as e:
                print(f"Failed to send top-up receipt to admin {admin_id}: {e}")
        return
        
    # ── TOP-UP AMOUNT STEP ──────────────────────────────────────────────────
    if awaiting == "topup_amount":
        try:
            amount = float(update.message.text.strip())
            if amount < 50:
                await update.message.reply_text(
                    "❌ Minimum top-up amount is <b>₱50</b>. Please enter a valid amount:",
                    parse_mode=ParseMode.HTML
                )
                return
            
            # Save amount temporarily and move to receipt upload step
            ctx.user_data["topup_amount"] = amount
            ctx.user_data["awaiting"] = "topup_receipt"
            
            await update.message.reply_text(
                f"✅ Amount set to: <b>₱{amount:.2f}</b>\n\n"
                "📸 Now, please send or upload a <b>screenshot / image of your payment receipt</b>.\n\n"
                "<i>Use /cancel to abort.</i>",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid numeric amount (e.g. 100). Minimum is ₱50.")
        return

    # ── TOP-UP RECEIPT STEP ─────────────────────────────────────────────────
    if awaiting == "topup_receipt":
        if not update.message.photo:
            await update.message.reply_text("❌ Please send an image/screenshot of your receipt. Use /cancel to abort.")
            return
            
        amount = ctx.user_data.get("topup_amount", 50)
        photo_file = update.message.photo[-1].file_id # Gets the highest resolution photo
        
        # Clear state
        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("topup_amount", None)
        
        # Confirm to user
        await update.message.reply_text(
            "✅ <b>Top-Up Request Submitted!</b>\n\n"
            "Your receipt has been forwarded to the admin for verification. Your balance will be credited once approved.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("user_stock_back", "🏠 Main Menu"),
        )
        
        # Forward receipt and details directly to admin(s)
        u = update.effective_user
        username_display = f"@{u.username}" if u.username else "None"
        admin_caption = (
            f"🚨 <b>NEW TOP-UP REQUEST</b>\n\n"
            f"👤 <b>User:</b> {u.first_name} ({username_display})\n"
            f"🆔 <b>User ID:</b> <code>{u.id}</code>\n"
            f"💰 <b>Requested Amount:</b> ₱{amount:.2f}\n\n"
            f"👉 Go to Admin Panel -> Add Balance to credit this user."
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await ctx.bot.send_photo(
                    chat_id=admin_id,
                    photo=photo_file,
                    caption=admin_caption,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                log.error(f"Failed to send top-up receipt to admin {admin_id}: {e}")
        return
        
        
    # ── STOCK FILE UPLOAD ───────────────────────────────────────────────────
    if awaiting == "stock_file" and is_admin(uid):
        if not update.message.document:
            await update.message.reply_text("❌ Please send a valid <code>.txt</code> file. Use /cancel to abort.", parse_mode=ParseMode.HTML)
            return
            
        doc: Document = update.message.document
        if not doc.file_name.lower().endswith(".txt"):
            await update.message.reply_text("❌ File must be a <code>.txt</code> format.", parse_mode=ParseMode.HTML)
            return
            
        # Retrieve which tier we are uploading to (default to 25 if not set)
        tier = ctx.user_data.get("upload_tier", 25)
        
        tg_file = await ctx.bot.get_file(doc.file_id)
        raw_bytes = await tg_file.download_as_bytearray()
        content = raw_bytes.decode("utf-8", errors="replace")
        
        # Parse the multi-line blocks separated by '=='
        accounts = parse_stock_file(content)
        if not accounts:
            await update.message.reply_text(
                "❌ No valid accounts found. Make sure entries are separated by <code>==</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
            
        # Save to database under the selected tier
        added = await add_stock(accounts, tier)
        total_tier_stock = await get_stock_count_by_tier(tier)
        
        await log_action("add_stock", uid, f"tier={tier}, added={added}")
        
        # Clean up state data
        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("upload_tier", None)
        
        await update.message.reply_text(
            f"✅ <b>Successfully Added ₱{tier} Stock!</b>\n\n"
            f"➕ Added: <b>{added} accounts</b>\n"
            f"📦 Total ₱{tier} Available: <b>{total_tier_stock}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin_stock", "🔙 Stock Panel"),
        )
        return
                        
    # ── BAN ─────────────────────────────────────────────────────────────────
    if awaiting == "ban_id" and is_admin(uid):
        try:
            target = int(update.message.text.strip())
            await set_banned(target, 1)
            await log_action("ban_user", uid, f"target={target}")
            ctx.user_data.pop("awaiting", None)
            await update.message.reply_text(
                f"🚫 User <code>{target}</code> banned.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("admin_users"),
            )
        except ValueError:
            await update.message.reply_text("❌ Send a numeric user ID.")
        return

    # ── UNBAN ────────────────────────────────────────────────────────────────
    if awaiting == "unban_id" and is_admin(uid):
        try:
            target = int(update.message.text.strip())
            await set_banned(target, 0)
            await log_action("unban_user", uid, f"target={target}")
            ctx.user_data.pop("awaiting", None)
            await update.message.reply_text(
                f"✅ User <code>{target}</code> unbanned.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("admin_users"),
            )
        except ValueError:
            await update.message.reply_text("❌ Send a numeric user ID.")
        return

    # ── BROADCAST ────────────────────────────────────────────────────────────
    if awaiting == "broadcast_msg" and is_admin(uid):
        text  = update.message.text or update.message.caption or ""
        users = await get_all_users()
        sent = failed = 0
        for u in users:
            if u["is_banned"]:
                continue
            try:
                await ctx.bot.send_message(u["user_id"], text, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception:
                failed += 1
        await log_action("broadcast", uid, f"sent={sent},failed={failed}")
        ctx.user_data.pop("awaiting", None)
        await update.message.reply_text(
            f"📢 <b>Broadcast Complete</b>\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back("admin_panel"),
        )
        return

    # ── SET COOLDOWN ─────────────────────────────────────────────────────────
    if awaiting == "set_cooldown" and is_admin(uid):
        try:
            hours = float(update.message.text.strip())
            await set_setting("cooldown_hours", str(hours))
            await log_action("set_cooldown", uid, f"hours={hours}")
            ctx.user_data.pop("awaiting", None)
            await update.message.reply_text(
                f"⏱ Cooldown set to <b>{hours}h</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_back("admin_settings"),
            )
        except ValueError:
            await update.message.reply_text("❌ Send a valid number (e.g. 24 or 0).")
        return

    # ── SET WELCOME ──────────────────────────────────────────────────────────
    if awaiting == "set_welcome" and is_admin(uid):
        new_msg = update.message.text or ""
        await set_setting("welcome_msg", new_msg)
        await log_action("set_welcome", uid)
        ctx.user_data.pop("awaiting", None)
        await update.message.reply_text(
            "✅ Welcome message updated.",
            reply_markup=kb_back("admin_settings"),
        )
        return

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
async def main() -> None:
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))

    log.info("Bot online.")
    
    # Manual lifecycle management to avoid event loop conflicts
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep the application running
    stop_signal = asyncio.Future()
    try:
        await stop_signal
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
