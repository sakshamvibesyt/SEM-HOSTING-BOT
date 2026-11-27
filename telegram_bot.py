"""Private hosting assistant: account signup/login and hosting controls."""
from __future__ import annotations

import asyncio
import io
import html
import logging
import os
import re
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, ApplicationHandlerStop, TypeHandler, filters,
)

import botmanager
import db

log = logging.getLogger(__name__)
_APP = None
_MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024
PANEL_URL = os.environ.get("PANEL_URL", "https://sem-hosting-bot.onrender.com/").strip()
WELCOME_VIDEO_URL = os.environ.get("WELCOME_VIDEO_URL", "").strip()
FORCE_JOIN_VIDEO_URL = os.environ.get(
    "FORCE_JOIN_VIDEO_URL",
    "https://www.image2url.com/r2/default/videos/1793781045682-f99fe8eb-e9d8-428d-a440-ff0811aa6fcd.mp4",
).strip()
UPDATES_URL = os.environ.get("UPDATES_URL", "").strip()
SUPPORT_URL = os.environ.get("SUPPORT_URL", "").strip()
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "@drama_fonts").strip()
FORCE_SUB_URL = os.environ.get("FORCE_SUB_URL", "https://t.me/drama_fonts").strip()
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "drama_404x").strip().lstrip("@")
OWNER_ID = os.environ.get("OWNER_ID", "9282845729").strip()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

WAIT_USERNAME = "auth_wait_username"
WAIT_PASSWORD = "auth_wait_password"


def _linked(update: Update):
    chat = update.effective_chat
    return db.get_user_by_telegram_chat(chat.id) if chat else None


def _auth_keyboard():
    rows = [
        [InlineKeyboardButton("🔐 Login", callback_data="auth:login"),
         InlineKeyboardButton("✨ Sign Up", callback_data="auth:signup")],
        [InlineKeyboardButton("❔ Help", callback_data="help")],
    ]
    if UPDATES_URL:
        rows.append([InlineKeyboardButton("📢 Updates", url=UPDATES_URL)])
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton("💬 Support", url=SUPPORT_URL)])
    rows.append([InlineKeyboardButton("👑 Owner", url=f"https://t.me/{OWNER_USERNAME}")])
    return InlineKeyboardMarkup(rows)


def _user_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Upload Bot", callback_data="user:upload"),
         InlineKeyboardButton("🤖 My Bots", callback_data="user:bots")],
        [InlineKeyboardButton("🌐 Open Hosting Panel", url=PANEL_URL)],
        [InlineKeyboardButton("❔ Help", callback_data="help")],
    ])


def _full_welcome(first: str) -> str:
    return (
        "╭━━━〔 ⚡ 𝐏𝐑𝐈𝐕𝐀𝐓𝐄 𝐇𝐎𝐒𝐓𝐈𝐍𝐆 ⚡ 〕━━━╮\n\n"
        "       🖤 𝐖𝐄𝐋𝐂𝐎𝐌𝐄 𝐓𝐎\n"
        "❛ ⤹⌯ ᯓ𓆰 𝐃 ꧊᱂ ᧘ ϻ ᧘ 𖤍 ➛🇨🇦 𝐇𝐨𝐬𝐭𝐢𝐧𝐠\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"⌁ 𝐀𝐂𝐂𝐄𝐒𝐒 𝐆𝐑𝐀𝐍𝐓𝐄𝐃, {first}\n\n"
        "💻 Your personal hosting terminal is ready.\n"
        "Deploy, monitor and control your Python bots from Telegram.\n\n"
        "୨୧ ━━━ 𝐒𝐘𝐒𝐓𝐄𝐌 𝐅𝐄𝐀𝐓𝐔𝐑𝐄𝐒 ━━━ ୨୧\n\n"
        "⚡ Instant Bot Deployment\n"
        "📂 Secure File Management\n"
        "🖥 Live Console & Logs\n"
        "🔄 Start / Stop / Restart\n"
        "🛡 Account Authentication\n"
        "📊 Hosting Statistics\n\n"
        "୨୧ ━━━ 𝐒𝐘𝐒𝐓𝐄𝐌 𝐎𝐖𝐍𝐄𝐑 ━━━ ୨୧\n\n"
        f"👑 @{OWNER_USERNAME}\n\n"
        "⌁ 𝐂𝐎𝐃𝐄 • 𝐃𝐄𝐏𝐋𝐎𝐘 • 𝐂𝐎𝐍𝐓𝐑𝐎𝐋"
    )


def _compact_welcome(first: str) -> str:
    return (
        "╭━━━〔 ⚡ 𝐇𝐎𝐒𝐓𝐈𝐍𝐆 𝐒𝐘𝐒𝐓𝐄𝐌 ⚡ 〕━━━╮\n"
        f"⌁ ACCESS GRANTED, {first}\n"
        "💻 Your bot deployment terminal is ready.\n"
        "⚡ Deploy • Manage • Monitor\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


def _welcome_keyboard(expanded: bool, authenticated: bool):
    rows = [[InlineKeyboardButton(
        "📕 COLLAPSE MESSAGE" if expanded else "📖 READ FULL MESSAGE",
        callback_data="welcome:collapse" if expanded else "welcome:expand"
    )]]
    rows.extend((_user_keyboard() if authenticated else _auth_keyboard()).inline_keyboard)
    return InlineKeyboardMarkup(rows)


async def _is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user.id)
        return member.status in ("member", "administrator", "creator") or (member.status == "restricted" and getattr(member, "is_member", False))
    except Exception:
        log.exception("Force-join check failed for %s; confirm bot is admin in channel", FORCE_SUB_CHANNEL)
        return False


async def _require_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Require membership before allowing access to the hosting assistant."""
    if await _is_subscribed(update, context):
        return True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖤 JOIN THE NETWORK", url=FORCE_SUB_URL)],
        [InlineKeyboardButton("💗 I HAVE JOINED ✓", callback_data="force:check")],
    ])

    # Cute cyber-terminal style verification caption (sent with the video).
    caption = (
        "╭━━━〔 🖤 𝐒𝐄𝐌 𝐂𝐘𝐁𝐄𝐑 𝐇𝐎𝐒𝐓 〕━━━╮\n\n"
        "       ୨୧ 𝐇𝐄𝐘, 𝐂𝐘𝐁𝐄𝐑 𝐅𝐑𝐈𝐄𝐍𝐃! ♡\n\n"
        "⌁ 𝐒𝐘𝐒𝐓𝐄𝐌 𝐈𝐍𝐈𝐓𝐈𝐀𝐋𝐈𝐙𝐄𝐃...\n"
        "⌁ 𝐒𝐄𝐂𝐔𝐑𝐄 𝐂𝐎𝐍𝐍𝐄𝐂𝐓𝐈𝐎𝐍 ✓\n"
        "⌁ 𝐇𝐎𝐒𝐓𝐈𝐍𝐆 𝐏𝐀𝐍𝐄𝐋 𝐎𝐍 𝐒𝐓𝐀𝐍𝐃𝐁𝐘...\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "୨୧ 𝐘𝐎𝐔𝐑 𝐂𝐘𝐁𝐄𝐑 𝐖𝐎𝐑𝐋𝐃 ♡\n\n"
        "🤖 Deploy your Telegram bots\n"
        "📂 Manage files & configurations\n"
        "⚡ Start • Stop • Restart processes\n"
        "📡 Monitor status & live logs\n\n"
        "╭━━━〔 🔒 𝐀𝐂𝐂𝐄𝐒𝐒 𝐋𝐎𝐂𝐊𝐄𝐃 〕━━━╮\n\n"
        "♡ Just one tiny step before entering!\n"
        "Join our official channel, then return\n"
        "here and tap I HAVE JOINED to unlock\n"
        "your personal hosting terminal.\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "       ⌁ 𝐒𝐓𝐀𝐘 𝐂𝐔𝐓𝐄. 𝐒𝐓𝐀𝐘 𝐂𝐘𝐁𝐄𝐑. ♡\n"
        "       👑 <tg-spoiler><a href=\"https://t.me/" + OWNER_USERNAME + "\">@" + OWNER_USERNAME + "</a></tg-spoiler>"
    )

    message = update.effective_message
    if not message:
        return False

    if FORCE_JOIN_VIDEO_URL:
        try:
            await message.reply_video(
                video=FORCE_JOIN_VIDEO_URL,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return False
        except Exception:
            log.exception("Force-join video send failed; sending text fallback")

    await message.reply_text(
        caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    return False


async def _send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first = html.escape(user.first_name if user and user.first_name else "Cyber Friend")
    caption = (
        f"╭━━━〔 🖤 𝐒𝐄𝐌 𝐂𝐘𝐁𝐄𝐑 𝐇𝐎𝐒𝐓 〕━━━╮\n\n"
        f"       ୨୧ 𝐖𝐄𝐋𝐂𝐎𝐌𝐄, {first}! ♡\n\n"
        "⌁ 𝐒𝐘𝐒𝐓𝐄𝐌 𝐒𝐓𝐀𝐓𝐔𝐒: <b>ONLINE</b> ✓\n"
        "⌁ 𝐒𝐄𝐂𝐔𝐑𝐄 𝐒𝐄𝐒𝐒𝐈𝐎𝐍: <b>ACTIVE</b> ✓\n"
        "⌁ 𝐇𝐎𝐒𝐓𝐈𝐍𝐆 𝐓𝐄𝐑𝐌𝐈𝐍𝐀𝐋: <b>READY</b> ✓\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "୨୧ 𝐘𝐎𝐔𝐑 𝐂𝐘𝐁𝐄𝐑 𝐓𝐎𝐎𝐋𝐊𝐈𝐓 ♡\n\n"
        "🤖 Deploy and manage Telegram bots\n"
        "📂 Upload files and manage configurations\n"
        "⚡ Start • Stop • Restart processes\n"
        "📡 Check bot status and live logs\n\n"
        "╭━━━〔 ✦ 𝐂𝐇𝐎𝐎𝐒𝐄 𝐘𝐎𝐔𝐑 𝐍𝐄𝐗𝐓 𝐒𝐓𝐄𝐏 〕━━━╮\n"
        "Use the buttons below to enter your control panel.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👑 <tg-spoiler><a href=\"https://t.me/{OWNER_USERNAME}\">@{OWNER_USERNAME}</a></tg-spoiler>\n"
        "       ⌁ 𝐒𝐓𝐀𝐘 𝐂𝐔𝐓𝐄. 𝐒𝐓𝐀𝐘 𝐂𝐘𝐁𝐄𝐑. ♡"
    )
    markup = _user_keyboard() if _linked(update) else _auth_keyboard()
    if WELCOME_VIDEO_URL:
        try:
            await update.effective_message.reply_video(
                video=WELCOME_VIDEO_URL, caption=caption,
                parse_mode="HTML", reply_markup=markup,
            )
            return
        except Exception:
            log.exception("Welcome video could not be sent; falling back to text")
    await update.effective_message.reply_text(
        caption, parse_mode="HTML", reply_markup=markup,
        disable_web_page_preview=True,
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("auth_state", None)
    context.user_data.pop("auth_mode", None)
    if not await _require_subscription(update, context):
        return
    await _send_welcome(update, context)


async def pannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 Open Hosting Panel", url=PANEL_URL)
    ]])
    await update.effective_message.reply_text(
        "⚡ *HOSTING CONTROL PANEL*\n\n"
        "Your hosting dashboard is ready.\n"
        "📌 Panel URL: " + PANEL_URL + "\n\n"
        "Tap the button below to open your panel. 💙",
        parse_mode="Markdown", reply_markup=keyboard,
    )


async def _ask_username(query, context, mode: str):
    context.user_data["auth_mode"] = mode
    context.user_data["auth_state"] = WAIT_USERNAME
    await query.message.reply_text(
        "👤 *Enter your username*\n\n"
        "Please send your hosting username.",
        parse_mode="Markdown",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    if data == "force:check":
        if await _is_subscribed(update, context):
            await query.answer("Verified!")
            await _send_welcome(update, context)
        else:
            await query.answer("Please join @drama_fonts first.", show_alert=True)
        return
    await query.answer()
    if data in ("welcome:expand", "welcome:collapse"):
        first = update.effective_user.first_name if update.effective_user else "USER"
        expanded = data == "welcome:expand"
        await query.message.edit_text(
            _full_welcome(first) if expanded else _compact_welcome(first),
            reply_markup=_welcome_keyboard(expanded, _linked(update) is not None),
        )
        return
    if not await _require_subscription(update, context):
        return
    if data == "auth:login":
        await _ask_username(query, context, "login")
    elif data == "auth:signup":
        await _ask_username(query, context, "signup")
    elif data == "help":
        await query.message.reply_text(
            "⚡ *HOSTING HELP*\n\n"
            "• Login or Sign Up to access your account.\n"
            "• Use /upload, then send a `.py` or `.zip` file.\n"
            "• Use /bots to view your bots.\n"
            "• Use /pannel to open your hosting panel.\n"
            "• Use /status BOT_ID, /startbot BOT_ID, "
            "/stopbot BOT_ID, /restartbot BOT_ID.\n"
            "• Use /logs BOT_ID and /files BOT_ID.",
            parse_mode="Markdown",
        )
    elif data == "user:bots":
        await bots_cmd(update, context)
    elif data == "user:upload":
        await upload_cmd(update, context)


async def auth_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("auth_state")
    if not state:
        return
    value = (update.effective_message.text or "").strip()
    if not value:
        return

    if state == WAIT_USERNAME:
        if not USERNAME_RE.fullmatch(value):
            await update.effective_message.reply_text(
                "⚠️ Username 3–32 characters ka hona chahiye. "
                "Letters, numbers, dot, underscore aur hyphen use karo."
            )
            return
        context.user_data["auth_username"] = value
        context.user_data["auth_state"] = WAIT_PASSWORD
        mode = context.user_data.get("auth_mode")
        label = "Create a strong password" if mode == "signup" else "Enter your password"
        await update.effective_message.reply_text(
            f"🔑 *{label}*\n\nPassword yahin send karo. "
            "Security ke liye bot ise process karne ke baad message delete karne ki koshish karega.",
            parse_mode="Markdown",
        )
        return

    if state == WAIT_PASSWORD:
        # Best-effort removal of the password message from the chat.
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        username = context.user_data.get("auth_username", "")
        mode = context.user_data.get("auth_mode")
        context.user_data.pop("auth_state", None)
        context.user_data.pop("auth_mode", None)
        context.user_data.pop("auth_username", None)

        if len(value) < 6:
            await update.effective_chat.send_message(
                "⚠️ Password kam se kam 6 characters ka hona chahiye. "
                "Dobara /start karke try karo."
            )
            return

        if mode == "signup":
            if db.get_user_by_name(username) is not None:
                await update.effective_chat.send_message(
                    "⚠️ Ye username pehle se registered hai. Login button use karo."
                )
                return
            try:
                user_id = db.create_user(username, value)
            except Exception:
                log.exception("Telegram signup failed")
                user_id = None
            if not user_id:
                await update.effective_chat.send_message(
                    "❌ Account create nahi ho paya. Thodi der baad try karo."
                )
                return
            user = db.get_user(user_id)
        else:
            user = db.verify_user(username, value)
            if user is None:
                await update.effective_chat.send_message(
                    "❌ Username ya password galat hai, ya account disabled hai.\n"
                    "Dobara /start karke Login try karo."
                )
                return

        db.link_telegram_account(user["id"], update.effective_chat.id)
        await update.effective_chat.send_message(
            f"🎉 *{ 'Account Created' if mode == 'signup' else 'Login Successful' }!*\n\n"
            f"Welcome to GXHOST, *{username}* 💗\n\n"
            "Your Telegram account is now connected with your GXHOST account.\n"
            "You can now upload and manage your bots from here. 🚀",
            parse_mode="Markdown",
            reply_markup=_user_keyboard(),
        )


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Backward compatibility for users who already generated a website pairing code.
    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Use /start and choose Login, or send /link YOUR_CODE."
        )
        return
    user_id = db.consume_telegram_pair_code(
        context.args[0].strip().upper(), update.effective_chat.id
    )
    if not user_id:
        await update.effective_message.reply_text(
            "Pairing code invalid or expired. Use /start → Login instead."
        )
        return
    user = db.get_user(user_id)
    await update.effective_message.reply_text(
        f"✅ Account connected: {user['username']}\nUse /bots to view your bots."
    )


async def unlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.unlink_telegram_chat(update.effective_chat.id)
    await update.effective_message.reply_text("🔓 Telegram account unlinked.")


async def bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user:
        await update.effective_message.reply_text("🔐 Please /start and Login first.")
        return
    bots = db.list_bots(user["id"])
    if not bots:
        await update.effective_message.reply_text(
            "📂 Abhi koi bot upload nahi hua.\n/upload bhejo, phir .py ya .zip file send karo."
        )
        return
    lines = [f"🤖 Your hosting bots ({len(bots)}):"]
    lines.extend(f"{b['id']} · {b['name']} · {b['status']}" for b in bots[:40])
    await update.effective_message.reply_text("\n".join(lines))


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _linked(update):
        await update.effective_message.reply_text("🔐 Please /start and Login first.")
        return
    context.user_data["awaiting_upload"] = True
    await update.effective_message.reply_text(
        "📤 *Send your bot file now*\n\n"
        "Accepted formats: `.py` and `.zip`\n"
        f"Maximum size: {_MAX_BYTES // (1024 * 1024)} MB",
        parse_mode="Markdown",
    )


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    doc = update.effective_message.document
    if not user:
        await update.effective_message.reply_text("🔐 Please /start and Login first.")
        return
    if not context.user_data.get("awaiting_upload"):
        await update.effective_message.reply_text("Send /upload before attaching a bot file.")
        return
    context.user_data["awaiting_upload"] = False
    filename = doc.file_name or ""
    if not filename.lower().endswith((".py", ".zip")):
        await update.effective_message.reply_text("❌ Only .py and .zip files are accepted.")
        return
    if doc.file_size and doc.file_size > _MAX_BYTES:
        await update.effective_message.reply_text("❌ File exceeds the configured upload limit.")
        return
    tg_file = await doc.get_file()
    payload = await tg_file.download_as_bytearray()
    if len(payload) > _MAX_BYTES:
        await update.effective_message.reply_text("❌ File exceeds the configured upload limit.")
        return
    stream = io.BytesIO(payload)
    try:
        name, uid, dir_name = botmanager.save_upload(user["id"], filename, stream, len(payload))
        taken = {b["name"] for b in db.list_bots(user["id"])}
        final_name = name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        counter = 2
        while final_name in taken:
            final_name = f"{stem} ({counter}){('.' + ext) if ext else ''}"
            counter += 1
        bot_id = db.create_bot(user["id"], final_name, uid, dir_name)
        bot = db.get_bot(bot_id, user["id"])
        botmanager.collapse_single_dir(botmanager.bot_root(bot))
        entry, confidence = botmanager.detect_entry(bot)
        if entry and confidence in ("exact", "single"):
            db.set_entry_point(bot_id, entry, "pending")
            if user["role"] == "admin":
                db.set_bot_status(bot_id, "setup")
            status = "setup" if user["role"] == "admin" else "pending (admin approval required)"
        else:
            db.set_bot_status(bot_id, "setup")
            status = "setup required — choose entry file on website"
        await update.effective_message.reply_text(
            f"✅ *Upload Complete!*\n\nID: `{bot_id}`\nName: {final_name}\n"
            f"Entry file: {entry or 'Not detected'}\nStatus: {status}\n\n"
            "Open your panel to finish setup if needed.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌐 Open Hosting Panel", url=PANEL_URL)
            ]]),
        )
    except Exception as exc:
        log.exception("Telegram upload failed")
        await update.effective_message.reply_text(f"❌ Upload failed: {str(exc)[:250]}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /status BOT_ID (login first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    uptime = botmanager.format_uptime(botmanager.uptime_seconds(bot))
    await update.effective_message.reply_text(
        f"{bot['name']}\nStatus: {bot['status']}\nPID: {bot['pid'] or '-'}\nUptime: {uptime}"
    )


async def control_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    action = update.effective_message.text.split()[0].split("@")[0].lstrip("/")
    aliases = {"startbot": "start", "stopbot": "stop", "restartbot": "restart"}
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text(f"Usage: /{action} BOT_ID — login first if not authenticated")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found or it belongs to another account.")
        return
    act = aliases[action]
    if act == "start":
        if bot["status"] in ("pending", "rejected"):
            await update.effective_message.reply_text("This bot is not approved or is rejected.")
            return
        ok, msg, pid = botmanager.start(bot)
    elif act == "stop":
        ok, msg = botmanager.stop(bot)
    else:
        ok, msg, pid = botmanager.restart(bot)
    await update.effective_message.reply_text(("✅ " if ok else "❌ ") + msg)


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /logs BOT_ID (login first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    content = botmanager.read_log(int(bot["id"]), tail_lines=50) or "No logs yet."
    await update.effective_message.reply_text(content[-3800:])


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /files BOT_ID (login first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    files = botmanager.list_bot_files(bot)
    lines = [f"Files for {bot['name']}:"] + [
        f"{f['name']} · {botmanager.human_size(f['size'])}" for f in files[:60]
    ]
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def getfile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /getfile BOT_ID relative/path.py (login first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    relpath = " ".join(context.args[1:])
    target = botmanager.file_for_download(bot, relpath)
    if not target:
        await update.effective_message.reply_text("File not found or invalid path.")
        return
    if os.path.getsize(target) > 45 * 1024 * 1024:
        await update.effective_message.reply_text("File is too large to send through Telegram.")
        return
    with open(target, "rb") as fh:
        await update.effective_message.reply_document(document=fh, filename=os.path.basename(target))



async def force_join_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block every bot interaction until the user joins the required channel."""
    # The verification button itself must be allowed through so it can check membership.
    query = update.callback_query
    if query and query.data == "force:check":
        return
    if not update.effective_user:
        return
    if await _is_subscribed(update, context):
        return
    await _require_subscription(update, context)
    raise ApplicationHandlerStop


def start_telegram_bot(flask_app) -> None:
    """Start polling in a daemon thread when TELEGRAM_BOT_TOKEN is configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("TELEGRAM_BOT_TOKEN not set; Telegram control bot disabled")
        return
    global _APP
    if _APP is not None:
        return
    db.init_telegram_tables()

    async def runner():
        application = Application.builder().token(token).build()
        application.add_handler(TypeHandler(Update, force_join_gate), group=-1)
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(CommandHandler("pannel", pannel_cmd))
        application.add_handler(CommandHandler("panel", pannel_cmd))
        application.add_handler(CommandHandler("link", link_cmd))
        application.add_handler(CommandHandler("unlink", unlink_cmd))
        application.add_handler(CommandHandler("bots", bots_cmd))
        application.add_handler(CommandHandler("upload", upload_cmd))
        application.add_handler(CommandHandler("status", status_cmd))
        application.add_handler(CommandHandler(["startbot", "stopbot", "restartbot"], control_cmd))
        application.add_handler(CommandHandler("logs", logs_cmd))
        application.add_handler(CommandHandler("files", files_cmd))
        application.add_handler(CommandHandler("getfile", getfile_cmd))
        application.add_handler(CallbackQueryHandler(callback_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auth_text_handler))
        application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        try:
            await asyncio.Event().wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

    def thread_main():
        with flask_app.app_context():
            try:
                asyncio.run(runner())
            except Exception:
                log.exception("Telegram bot stopped")

    thread = threading.Thread(target=thread_main, name="gxhost-telegram", daemon=True)
    thread.start()
    _APP = thread
