"""
GX Bot Hosting — a Discord/Telegram bot hosting panel.

Routes are grouped the same way the panel exposes them:

  public     /login /register /logout /ping
  user       / /upload /my_files
  bot api    /start|stop|restart|delete/<id>       (JSON)
  bot files  /setup /logs /logs_raw /download_file /delete_file /send_input
  admin      /admin /admin/access /admin/create_user /admin/approve_bot/...

Every bot route resolves the bot with ``require_bot()``, which scopes the
lookup to the logged-in account. That is the single chokepoint that keeps one
user from addressing another user's bot.
"""

from __future__ import annotations

import atexit
import os
import re
import time
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

import botmanager
import db

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder="templates", static_folder=None)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
)

DATA_DIR = os.path.join(BASE_DIR, os.environ.get("DATA_DIR", "data"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

db.init(os.path.join(DATA_DIR, "panel.db"))
botmanager.configure(
    data_dir=DATA_DIR,
    max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "20")),
    max_log_mb=int(os.environ.get("MAX_LOG_MB", "5")),
    monitor_interval=int(os.environ.get("MONITOR_INTERVAL", "3")),
    inherit_env=os.environ.get("INHERIT_ENV", "0") == "1",
)
# A previous worker's children may still be alive and can no longer be
# supervised, so clear those rows before the dashboard can show them as running.
botmanager.reconcile_on_boot()
botmanager.start_monitor()
atexit.register(botmanager.shutdown)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


# --------------------------------------------------------------------- auth


def current_user() -> Any | None:
    uid = session.get("uid")
    if not uid:
        return None
    user = db.get_user(int(uid))
    if user is None or user["is_banned"]:
        session.clear()
        return None
    return user


# Paths that answer with JSON instead of an HTML redirect. Keeping this list
# explicit means an unauthenticated POST to /upload still gets a normal page.
JSON_PREFIXES = (
    "/start/",
    "/stop/",
    "/restart/",
    "/delete/",
    "/send_input/",
    "/logs_raw/",
    "/status/",
)


def wants_json() -> bool:
    return request.path.startswith(JSON_PREFIXES)


def safe_referrer(fallback: str) -> str:
    """Return the Referer only when it points at this host.

    A raw ``request.referrer`` is attacker-controlled and would otherwise turn
    these POST handlers into open redirects.
    """
    ref = request.referrer
    if not ref:
        return fallback
    from urllib.parse import urlparse

    host = urlparse(ref).netloc
    if host and host == request.host:
        return ref
    return fallback


def _next_target() -> str:
    """Only honour a relative ``next`` path; never redirect off-site."""
    nxt = request.args.get("next") or request.form.get("next") or ""
    # A leading "//" or a backslash lets a browser resolve the value as a
    # protocol-relative URL, so both are refused.
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return url_for("dashboard")


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if current_user() is None:
            if wants_json():
                return jsonify(ok=False, msg="Login required."), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def require_bot(bot_id: int) -> Any:
    """Fetch a bot owned by the current user, or abort 404.

    Anonymous callers and callers who pass someone else's id get the same 404
    so bot ids cannot be probed.
    """
    user = current_user()
    if user is None:
        abort(401)
    bot = db.get_bot(bot_id, user["id"])
    if bot is None:
        abort(404)
    return bot


def admin_unlocked() -> bool:
    user = current_user()
    if user is None:
        return False
    if user["role"] == "admin":
        return True
    return bool(session.get("admin_unlocked"))


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        if current_user() is None:
            return redirect(url_for("admin_gate"))
        if not admin_unlocked():
            if request.path != url_for("admin_gate"):
                flash("Admin access required.", "error")
                return redirect(url_for("admin_gate"))
            return redirect(url_for("admin_gate"))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_globals() -> dict[str, Any]:
    user = current_user()
    return {
        "current_user": user,
        "avatar_letter": (user["username"][0].upper() if user else "?"),
        "is_admin": admin_unlocked() if user else False,
        "panel_name": "GX",
        "panel_tagline": "Bot Hosting",
    }


# ------------------------------------------------------------------ filters


@app.template_filter("human_size")
def _human_size(value: int | None) -> str:
    return botmanager.human_size(int(value or 0))


@app.template_filter("uptime")
def _uptime(bot: Any) -> str:
    return botmanager.format_uptime(botmanager.uptime_seconds(bot))


@app.template_filter("status_class")
def _status_class(status: str) -> str:
    return {
        "running": "badge-running",
        "stopped": "badge-stopped",
        "pending": "badge-pending",
        "setup": "badge-setup",
        "rejected": "badge-rejected",
    }.get(status, "badge-stopped")


# ------------------------------------------------------------------ public


@app.get("/ping")
def ping():
    return jsonify(status="ok")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = db.verify_user(username, password)
        if user is None:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))
        session.clear()
        session["uid"] = user["id"]
        session.permanent = True
        flash(f"Welcome back, {user['username']}!", "success")
        return redirect(_next_target())

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user() is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not USERNAME_RE.match(username):
            flash("Username must be 3-32 characters (letters, numbers, . _ -).", "error")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register"))
        if db.get_user_by_name(username) is not None:
            flash("Username already taken.", "error")
            return redirect(url_for("register"))

        user_id = db.create_user(username, password)
        if not user_id:
            flash("Could not create the account. Try another username.", "error")
            return redirect(url_for("register"))

        session.clear()
        session["uid"] = user_id
        flash(f"Welcome back, {username}!", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ errors


@app.errorhandler(401)
def _401(_exc):
    if request.path.startswith(("/start/", "/stop/", "/restart/", "/delete/", "/send_input/")):
        return jsonify(ok=False, msg="Login required."), 401
    return redirect(url_for("login"))


@app.errorhandler(403)
def _403(_exc):
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=False, msg="Forbidden."), 403
    return render_template("error.html", code=403, message="You do not have access to that."), 403


@app.errorhandler(404)
def _404(_exc):
    if request.path.startswith(("/start/", "/stop/", "/restart/", "/delete/", "/send_input/", "/logs_raw/")):
        return jsonify(ok=False, msg="Not found."), 404
    return render_template("error.html", code=404, message="That page does not exist."), 404


@app.errorhandler(413)
def _413(_exc):
    flash(f"File too large (max {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB).", "error")
    return redirect(url_for("upload"))


@app.errorhandler(500)
def _500(_exc):
    return render_template("error.html", code=500, message="Something went wrong on our side."), 500


# ------------------------------------------------------------------- panel


@app.get("/")
@login_required
def dashboard():
    user = current_user()
    bots = db.list_bots(user["id"])
    counts = db.stats(user["id"])
    cards = []
    for bot in bots:
        security, findings = botmanager.scan_security(bot)
        cards.append({"bot": bot, "security": security, "findings": findings})
    return render_template("dashboard.html", bots=bots, cards=cards, counts=counts)


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    user = current_user()
    if request.method == "POST":
        file = request.files.get("file")
        if file is None or not file.filename:
            flash("No file selected.", "error")
            return redirect(url_for("upload"))

        file.stream.seek(0, os.SEEK_END)
        size = file.stream.tell()
        file.stream.seek(0)

        # Names are unique per account (the file manager keys folders by name),
        # so a re-upload of "bot.py" is suffixed instead of failing the insert.
        base_name = os.path.basename(file.filename.replace("\\", "/"))
        final_name = base_name
        taken = {b["name"] for b in db.list_bots(user["id"])}
        counter = 2
        stem, dot, ext_part = base_name.rpartition(".")
        if not dot:
            stem, ext_part = base_name, ""
        while final_name in taken:
            final_name = f"{stem} ({counter}){('.' + ext_part) if ext_part else ''}"
            counter += 1

        try:
            name, uid, dir_name = botmanager.save_upload(
                user["id"], file.filename, file.stream, size
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("upload"))
        except Exception as exc:  # disk full, permissions, ...
            app.logger.exception("upload failed")
            flash(f"Upload failed: {exc}", "error")
            return redirect(url_for("upload"))

        bot_id = db.create_bot(user["id"], final_name, uid, dir_name)
        bot = db.get_bot(bot_id, user["id"])
        botmanager.collapse_single_dir(botmanager.bot_root(bot))

        entry, confidence = botmanager.detect_entry(bot)
        flash(f"Uploaded: {final_name}", "success")

        if entry and confidence in ("exact", "single"):
            db.set_entry_point(bot_id, entry, "pending")
            flash(f"Entry point detected: {entry}", "success")
            # Admins skip the manual approval queue.
            if user["role"] == "admin":
                db.set_bot_status(bot_id, "setup")
                flash("Bot approved automatically.", "success")
        else:
            db.set_bot_status(bot_id, "setup")
            if entry:
                flash("Multiple scripts found — choose the entry point below.", "warning")
            else:
                flash("No runnable .py file found in that upload.", "error")

        return redirect(url_for("dashboard"))

    return render_template("upload.html")


@app.get("/my_files")
@login_required
def my_files():
    user = current_user()
    bots = db.list_bots(user["id"])
    folders = []
    total_files = 0
    total_bytes = 0
    for bot in bots:
        files = botmanager.list_bot_files(bot)
        size = botmanager.dir_size(bot)
        total_files += len(files)
        total_bytes += size
        folders.append({"bot": bot, "files": files, "size": size})
    return render_template(
        "my_files.html",
        folders=folders,
        total_files=total_files,
        total_bytes=total_bytes,
    )


# --------------------------------------------------------------- bot setup


@app.route("/setup/<int:bot_id>", methods=["GET", "POST"])
@login_required
def setup(bot_id: int):
    bot = require_bot(bot_id)

    if request.method == "POST":
        chosen = (request.form.get("file") or "").strip()
        files = botmanager.list_py_files(bot)
        if chosen not in files:
            flash("Invalid file selection.", "error")
            return redirect(url_for("setup", bot_id=bot_id))

        # Recording an entry point must not clear the approval gate. A bot
        # awaiting review stays 'pending'; a rejected bot returns to the queue.
        if bot["status"] in ("pending", "rejected"):
            next_status = "pending"
        else:
            next_status = "setup"

        db.set_entry_point(bot_id, chosen, next_status)
        flash(f"✓ Setup complete! Entry point: {chosen}", "success")
        return redirect(url_for("dashboard"))

    files = botmanager.list_py_files(bot)
    return render_template("setup.html", bot=bot, files=files)


@app.get("/logs/<int:bot_id>")
@login_required
def logs(bot_id: int):
    bot = require_bot(bot_id)
    content = botmanager.read_log_full(bot_id)
    files = botmanager.list_bot_files(bot)
    return render_template("logs.html", bot=bot, log=content, files=files)


@app.get("/logs_raw/<int:bot_id>")
@login_required
def logs_raw(bot_id: int):
    bot = require_bot(bot_id)
    body = botmanager.read_log(bot_id)
    if not body:
        body = ""
    # text/plain keeps the polling front-end simple and prevents any HTML in a
    # bot's output from being interpreted by the browser.
    return Response(body, mimetype="text/plain", headers={"Cache-Control": "no-store"})


@app.post("/send_input/<int:bot_id>")
@login_required
def send_input(bot_id: int):
    bot = require_bot(bot_id)
    text = request.form.get("text") or request.form.get("input") or ""
    if not text.strip():
        flash("Nothing to send.", "error")
        return redirect(url_for("logs", bot_id=bot_id))
    ok, msg = botmanager.send_input(bot, text)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("logs", bot_id=bot_id))


@app.get("/download_file/<int:bot_id>/<path:relpath>")
@login_required
def download_file(bot_id: int, relpath: str):
    bot = require_bot(bot_id)
    target = botmanager.file_for_download(bot, relpath)
    if target is None:
        abort(404)
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))


@app.post("/delete_file/<int:bot_id>/<path:relpath>")
@login_required
def delete_file(bot_id: int, relpath: str):
    bot = require_bot(bot_id)
    ok, msg = botmanager.delete_file(bot, relpath)
    flash(msg, "success" if ok else "error")
    return redirect(safe_referrer(url_for("my_files")))


# ------------------------------------------------------------- bot control


def _control(action: str) -> Callable[..., Any]:
    """Shared JSON handler for start/stop/restart/delete."""

    def handler(bot_id: int):
        bot = require_bot(bot_id)
        if action == "start":
            if bot["status"] == "pending":
                return jsonify(ok=False, msg="Bot is waiting for admin approval.")
            if bot["status"] == "rejected":
                return jsonify(ok=False, msg="Bot was rejected. Update it and resubmit.")
            ok, msg, pid = botmanager.start(bot)
            return jsonify(ok=ok, msg=msg, pid=pid) if ok else jsonify(ok=False, msg=msg)
        if action == "stop":
            ok, msg = botmanager.stop(bot)
            return jsonify(ok=ok, msg=msg)
        if action == "restart":
            ok, msg, pid = botmanager.restart(bot)
            if not ok:
                return jsonify(ok=False, msg=msg)
            fresh = db.get_bot(bot_id, bot["user_id"])
            return jsonify(ok=True, msg="Restarted.", pid=pid, restarts=fresh["restarts"])
        if action == "delete":
            ok, msg = botmanager.delete(bot)
            return jsonify(ok=ok, msg=msg)
        return jsonify(ok=False, msg="Unknown action."), 400

    handler.__name__ = f"bot_{action}"
    return handler


app.add_url_rule("/start/<int:bot_id>", "bot_start", login_required(_control("start")), methods=["POST"])
app.add_url_rule("/stop/<int:bot_id>", "bot_stop", login_required(_control("stop")), methods=["POST"])
app.add_url_rule("/restart/<int:bot_id>", "bot_restart", login_required(_control("restart")), methods=["POST"])
app.add_url_rule("/delete/<int:bot_id>", "bot_delete", login_required(_control("delete")), methods=["POST"])


@app.get("/status/<int:bot_id>")
@login_required
def status(bot_id: int):
    """Small JSON endpoint the dashboard polls for live badges."""
    bot = require_bot(bot_id)
    return jsonify(
        ok=True,
        status=bot["status"],
        pid=bot["pid"],
        restarts=bot["restarts"],
        uptime=botmanager.format_uptime(botmanager.uptime_seconds(bot)),
    )


# ------------------------------------------------------------------- admin


@app.get("/admin")
@login_required
def admin_gate():
    user = current_user()
    if admin_unlocked():
        return redirect(url_for("admin_panel"))
    return render_template("admin_gate.html", user=user)


@app.post("/admin/access")
@login_required
def admin_access():
    user = current_user()
    supplied = request.form.get("password") or ""
    if user["role"] == "admin":
        return redirect(url_for("admin_panel"))
    if ADMIN_PASSWORD and _constant_eq(supplied, ADMIN_PASSWORD):
        session["admin_unlocked"] = True
        flash("Admin panel unlocked.", "success")
        return redirect(url_for("admin_panel"))
    flash("Incorrect admin password.", "error")
    return redirect(url_for("admin_gate"))


def _constant_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@app.get("/admin/lock")
@login_required
def admin_lock():
    session.pop("admin_unlocked", None)
    flash("Admin panel locked.", "success")
    return redirect(url_for("dashboard"))


@app.get("/admin/panel")
@login_required
@admin_required
def admin_panel():
    users = db.list_users()
    bots = db.list_all_bots()
    counts = {"users": len(users), "bots": len(bots)}
    counts["running"] = sum(1 for b in bots if b["status"] == "running")
    counts["pending"] = sum(1 for b in bots if b["status"] in ("pending", "rejected"))
    return render_template("admin_panel.html", users=users, bots=bots, counts=counts)


@app.post("/admin/create_user")
@login_required
@admin_required
def admin_create_user():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = "admin" if request.form.get("role") == "admin" else "user"

    if not USERNAME_RE.match(username):
        flash("Username must be 3-32 characters (letters, numbers, . _ -).", "error")
        return redirect(url_for("admin_panel"))
    if len(password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin_panel"))
    if db.get_user_by_name(username) is not None:
        flash("Username already taken.", "error")
        return redirect(url_for("admin_panel"))

    if db.create_user(username, password, role):
        flash(f"User created: {username} ({role}).", "success")
    else:
        flash("Could not create the user.", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/delete_user/<int:user_id>")
@login_required
@admin_required
def admin_delete_user(user_id: int):
    actor = current_user()
    target = db.get_user(user_id)
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))
    if target["id"] == actor["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_panel"))
    for bot in db.list_bots(user_id):
        botmanager.delete(bot)
    import shutil

    shutil.rmtree(botmanager.user_root(user_id), ignore_errors=True)
    db.delete_user(user_id)
    flash(f"User deleted: {target['username']}.", "success")
    return redirect(url_for("admin_panel"))


@app.post("/admin/ban_user/<int:user_id>")
@login_required
@admin_required
def admin_ban_user(user_id: int):
    actor = current_user()
    target = db.get_user(user_id)
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))
    if target["id"] == actor["id"]:
        flash("You cannot ban yourself.", "error")
        return redirect(url_for("admin_panel"))
    banned = not bool(target["is_banned"])
    db.set_user_banned(user_id, banned)
    if banned:
        for bot in db.list_bots(user_id):
            botmanager.stop(bot)
    flash(f"{'Banned' if banned else 'Unbanned'}: {target['username']}.", "success")
    return redirect(url_for("admin_panel"))


@app.post("/admin/approve_bot/<int:bot_id>")
@login_required
@admin_required
def admin_approve_bot(bot_id: int):
    bot = db.get_bot(bot_id)
    if bot is None:
        flash("Bot not found.", "error")
        return redirect(url_for("admin_panel"))
    if not bot["entry_point"]:
        flash("Bot has no entry point yet — the owner must run setup first.", "error")
        return redirect(url_for("admin_panel"))
    db.set_bot_status(bot_id, "setup")
    flash(f"Approved: {bot['name']}.", "success")
    return redirect(safe_referrer(url_for("admin_panel")))


@app.post("/admin/reject_bot/<int:bot_id>")
@login_required
@admin_required
def admin_reject_bot(bot_id: int):
    bot = db.get_bot(bot_id)
    if bot is None:
        flash("Bot not found.", "error")
        return redirect(url_for("admin_panel"))
    botmanager.stop(bot)
    db.set_bot_status(bot_id, "rejected")
    flash(f"Rejected: {bot['name']}.", "success")
    return redirect(safe_referrer(url_for("admin_panel")))


@app.post("/admin/stop_bot/<int:bot_id>")
@login_required
@admin_required
def admin_stop_bot(bot_id: int):
    bot = db.get_bot(bot_id)
    if bot is None:
        flash("Bot not found.", "error")
        return redirect(url_for("admin_panel"))
    botmanager.stop(bot)
    flash(f"Stopped: {bot['name']}.", "success")
    return redirect(safe_referrer(url_for("admin_panel")))


@app.post("/admin/delete_bot/<int:bot_id>")
@login_required
@admin_required
def admin_delete_bot(bot_id: int):
    bot = db.get_bot(bot_id)
    if bot is None:
        flash("Bot not found.", "error")
        return redirect(url_for("admin_panel"))
    botmanager.delete(bot)
    flash("Bot deleted.", "success")
    return redirect(safe_referrer(url_for("admin_panel")))


@app.cli.command("create-admin")
def create_admin_command():
    """python -m flask --app app create-admin"""
    import getpass

    username = input("Admin username: ").strip()
    password = getpass.getpass("Admin password: ")
    if not USERNAME_RE.match(username) or len(password) < 6:
        print("Username must be 3-32 chars and password at least 6.")
        return
    if db.create_user(username, password, "admin"):
        print(f"Admin created: {username}")
    else:
        print("That username is already taken.")


@app.before_request
def _touch_session():
    # Only authenticated sessions are long-lived; an anonymous visitor keeps a
    # browser-session cookie so a stray GET does not pin a cookie for a week.
    if session.get("uid"):
        session.permanent = True


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
