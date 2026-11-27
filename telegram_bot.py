"""Telegram control bot for GXHOST. Accounts are linked with short-lived codes."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import botmanager
import db

log = logging.getLogger(__name__)
_APP = None
_MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024


def _linked(update: Update):
    chat = update.effective_chat
    return db.get_user_by_telegram_chat(chat.id) if chat else None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "GXHOST Control Bot\\n\\n"
        "Link your panel account using a one-time code from the website: /link CODE\\n"
        "Commands: /bots /upload (then send a .py/.zip) /status ID /startbot ID "
        "/stopbot ID /restartbot ID /logs ID /files ID /unlink"
    )


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.effective_message.reply_text("Open your GXHOST website → Telegram Link and send: /link YOUR_CODE")
        return
    user_id = db.consume_telegram_pair_code(context.args[0].strip().upper(), update.effective_chat.id)
    if not user_id:
        await update.effective_message.reply_text("Pairing code invalid or expired. Generate a new code from the website.")
        return
    user = db.get_user(user_id)
    await update.effective_message.reply_text(f"Account linked successfully: {user['username']}\nUse /bots to view your bots.")


async def unlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.unlink_telegram_chat(update.effective_chat.id)
    await update.effective_message.reply_text("Telegram account unlinked.")


async def bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user:
        await update.effective_message.reply_text("Account not linked. Generate a code on the GXHOST website and use /link CODE.")
        return
    bots = db.list_bots(user["id"])
    if not bots:
        await update.effective_message.reply_text("No bots uploaded yet. Send /upload then attach a .py or .zip file.")
        return
    lines = [f"Your GXHOST bots ({len(bots)}):"]
    lines.extend(f"{b['id']} · {b['name']} · {b['status']}" for b in bots[:40])
    await update.effective_message.reply_text("\n".join(lines))


async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _linked(update):
        await update.effective_message.reply_text("Link your account first using /link CODE.")
        return
    context.user_data["awaiting_upload"] = True
    await update.effective_message.reply_text("Now send one .py or .zip document. Upload limit: %s MB." % ( _MAX_BYTES // (1024 * 1024)))


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    doc = update.effective_message.document
    if not user:
        await update.effective_message.reply_text("Link your account first using /link CODE.")
        return
    if not context.user_data.get("awaiting_upload"):
        await update.effective_message.reply_text("Send /upload before attaching a bot file.")
        return
    context.user_data["awaiting_upload"] = False
    filename = doc.file_name or ""
    if not filename.lower().endswith((".py", ".zip")):
        await update.effective_message.reply_text("Only .py and .zip files are accepted.")
        return
    if doc.file_size and doc.file_size > _MAX_BYTES:
        await update.effective_message.reply_text("File exceeds the configured upload limit.")
        return
    tg_file = await doc.get_file()
    payload = await tg_file.download_as_bytearray()
    if len(payload) > _MAX_BYTES:
        await update.effective_message.reply_text("File exceeds the configured upload limit.")
        return
    stream = io.BytesIO(payload)
    try:
        name, uid, dir_name = botmanager.save_upload(user["id"], filename, stream, len(payload))
        # Match the web uploader: bot names must be unique within an account.
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
        await update.effective_message.reply_text(f"Upload complete!\nID: {bot_id}\nName: {name}\nDetected entry: {entry or 'not detected'}\nStatus: {status}\nOpen the website to finish setup if needed.")
    except Exception as exc:
        log.exception("Telegram upload failed")
        await update.effective_message.reply_text(f"Upload failed: {str(exc)[:250]}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /status BOT_ID (link account first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    uptime = botmanager.format_uptime(botmanager.uptime_seconds(bot))
    await update.effective_message.reply_text(f"{bot['name']}\nStatus: {bot['status']}\nPID: {bot['pid'] or '-'}\nUptime: {uptime}")


async def control_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    action = update.effective_message.text.split()[0].split("@")[0].lstrip("/")
    aliases = {"startbot": "start", "stopbot": "stop", "restartbot": "restart"}
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text(f"Usage: /{action} BOT_ID (link account first)")
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
    await update.effective_message.reply_text(("Success: " if ok else "Failed: ") + msg)


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /logs BOT_ID (link account first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    text = botmanager.read_log(int(bot["id"]), tail_lines=50) or "No logs yet."
    await update.effective_message.reply_text(text[-3800:])


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /files BOT_ID (link account first)")
        return
    bot = db.get_bot(int(context.args[0]), user["id"])
    if not bot:
        await update.effective_message.reply_text("Bot not found.")
        return
    files = botmanager.list_bot_files(bot)
    lines = [f"Files for {bot['name']}:"] + [f"{f['name']} · {botmanager.human_size(f['size'])}" for f in files[:60]]
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def getfile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _linked(update)
    if not user or len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /getfile BOT_ID relative/path.py (link account first)")
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
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(CommandHandler("link", link_cmd))
        application.add_handler(CommandHandler("unlink", unlink_cmd))
        application.add_handler(CommandHandler("bots", bots_cmd))
        application.add_handler(CommandHandler("upload", upload_cmd))
        application.add_handler(CommandHandler(["status"], status_cmd))
        application.add_handler(CommandHandler(["startbot", "stopbot", "restartbot"], control_cmd))
        application.add_handler(CommandHandler("logs", logs_cmd))
        application.add_handler(CommandHandler("files", files_cmd))
        application.add_handler(CommandHandler("getfile", getfile_cmd))
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
