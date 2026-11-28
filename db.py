"""
SQLite persistence for the GX bot hosting panel.

Two tables:

  users  - login accounts. ``role`` is 'user' or 'admin'.
  bots   - one row per uploaded bot. Every filesystem route resolves a bot
           through :func:`get_bot` with the owner id, so a bot can only be
           addressed by the account that owns it.

The module exposes a tiny helper layer instead of an ORM: the schema is small
and the panel must start with zero migration tooling.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from werkzeug.security import check_password_hash, generate_password_hash

try:
    from pymongo import MongoClient, ASCENDING
    from pymongo.errors import DuplicateKeyError, PyMongoError
except ImportError:  # The dependency is listed in requirements.txt.
    MongoClient = None
    ASCENDING = 1
    DuplicateKeyError = Exception
    PyMongoError = Exception

_LOG = logging.getLogger("dramax.accounts")
_MONGO_CLIENT = None
_MONGO_USERS = None

_LOCK = threading.RLock()
_DB_PATH: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',
    is_banned     INTEGER NOT NULL DEFAULT 0,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    uid          TEXT    NOT NULL UNIQUE,
    dir_name     TEXT    NOT NULL,
    entry_point  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'pending',
    pid          INTEGER,
    restarts     INTEGER NOT NULL DEFAULT 0,
    exit_code    INTEGER,
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    stopped_at   TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bots_user ON bots (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bots_user_name ON bots (user_id, name);
"""


def utc_now() -> str:
    """Timestamp string used everywhere (sortable, second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init(db_path: str) -> None:
    """Point the module at ``db_path`` and create the schema."""
    global _DB_PATH
    _DB_PATH = db_path
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    with _connect() as con:
        con.executescript(SCHEMA)
    _init_mongo_accounts()


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("db.init() has not been called")
    con = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


class _Cursor:
    """Context manager: opens a connection, commits, always closes."""

    def __enter__(self) -> sqlite3.Connection:
        _LOCK.acquire()
        self.con = _connect()
        return self.con

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.con.commit()
            else:
                self.con.rollback()
        finally:
            self.con.close()
            _LOCK.release()
        return False


def _q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _Cursor() as con:
        return con.execute(sql, tuple(params)).fetchall()


def _one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = _q(sql, params)
    return rows[0] if rows else None


def _write(sql: str, params: Iterable[Any] = ()) -> int:
    with _Cursor() as con:
        cur = con.execute(sql, tuple(params))
        return cur.lastrowid or cur.rowcount


# ------------------------------------------------------- Mongo account store
# Only account credentials/flags are mirrored. Bot files, uploads, logs and
# bot records are intentionally never sent to MongoDB.
def _mongo_config():
    uri = (os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL") or "").strip()
    name = (os.environ.get("MONGODB_DATABASE") or "dramax_accounts").strip()
    return uri, name


def _init_mongo_accounts() -> None:
    global _MONGO_CLIENT, _MONGO_USERS
    uri, name = _mongo_config()
    if not uri:
        _LOG.warning("MongoDB account persistence is DISABLED: set MONGODB_URI in Render")
        return
    if MongoClient is None:
        raise RuntimeError("pymongo is missing; install requirements.txt")
    try:
        _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
        _MONGO_CLIENT.admin.command("ping")
        _MONGO_USERS = _MONGO_CLIENT[name]["panel_accounts"]
        _MONGO_USERS.create_index([("username", ASCENDING)], unique=True)
        _restore_accounts_from_mongo()
        _sync_local_accounts_to_mongo()
        _LOG.info("MongoDB account persistence connected; account records synced")
    except Exception as exc:
        _MONGO_USERS = None
        if _MONGO_CLIENT is not None:
            _MONGO_CLIENT.close()
            _MONGO_CLIENT = None
        _LOG.exception("MongoDB account persistence initialization failed")
        raise RuntimeError("MongoDB account persistence could not start. Check MONGODB_URI, database access and Atlas quota.") from exc


def _restore_accounts_from_mongo() -> None:
    if _MONGO_USERS is None:
        return
    docs = list(_MONGO_USERS.find({}, {"_id": 0}))
    if not docs:
        return
    with _Cursor() as con:
        for doc in docs:
            username = str(doc.get("username") or "").strip()
            password_hash = doc.get("password_hash")
            if not username or not password_hash:
                continue
            existing = con.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                con.execute(
                    "UPDATE users SET password_hash=?, role=?, is_banned=?, is_admin=? WHERE username=?",
                    (password_hash, doc.get("role", "user"), int(doc.get("is_banned", 0)),
                     int(doc.get("is_admin", 0)), username),
                )
                continue
            wanted_id = doc.get("id")
            occupied = con.execute("SELECT username FROM users WHERE id=?", (wanted_id,)).fetchone() if wanted_id else None
            created = doc.get("created_at") or utc_now()
            if wanted_id and not occupied:
                con.execute(
                    "INSERT INTO users (id, username, password_hash, role, is_banned, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(wanted_id), username, password_hash, doc.get("role", "user"),
                     int(doc.get("is_banned", 0)), int(doc.get("is_admin", 0)), created),
                )
            else:
                con.execute(
                    "INSERT INTO users (username, password_hash, role, is_banned, is_admin, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (username, password_hash, doc.get("role", "user"), int(doc.get("is_banned", 0)),
                     int(doc.get("is_admin", 0)), created),
                )


def _sync_local_accounts_to_mongo() -> None:
    if _MONGO_USERS is None:
        return
    for user in list_users():
        doc = {key: user[key] for key in _USER_FIELDS}
        _MONGO_USERS.replace_one({"username": doc["username"]}, doc, upsert=True)


_USER_FIELDS = ("id", "username", "password_hash", "role", "is_banned", "is_admin", "created_at")


def _mongo_save_user(user: sqlite3.Row) -> None:
    if _MONGO_USERS is None:
        return
    doc = {key: user[key] for key in _USER_FIELDS}
    _MONGO_USERS.replace_one({"username": doc["username"]}, doc, upsert=True)


# ----------------------------------------------------------------- users


def create_user(username: str, password: str, role: str = "user") -> int | None:
    """Create a local account and persist its password hash to MongoDB."""
    try:
        uid = _write(
            "INSERT INTO users (username, password_hash, role, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, generate_password_hash(password), role, 1 if role == "admin" else 0, utc_now()),
        )
    except sqlite3.IntegrityError:
        return None
    if not uid:
        return None
    try:
        user = get_user(int(uid))
        if user is not None:
            _mongo_save_user(user)
        return int(uid)
    except Exception:
        _write("DELETE FROM users WHERE id = ?", (int(uid),))
        _LOG.exception("Could not persist newly-created account to MongoDB")
        return None


def get_user_by_name(username: str) -> sqlite3.Row | None:
    return _one("SELECT * FROM users WHERE username = ?", (username,))


def get_user(user_id: int) -> sqlite3.Row | None:
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def verify_user(username: str, password: str) -> sqlite3.Row | None:
    user = get_user_by_name(username)
    if user is None or user["is_banned"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


def count_users() -> int:
    row = _one("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


def list_users() -> list[sqlite3.Row]:
    return _q("SELECT * FROM users ORDER BY id ASC")


def set_user_banned(user_id: int, banned: bool) -> None:
    _write("UPDATE users SET is_banned = ? WHERE id = ?", (1 if banned else 0, user_id))
    user = get_user(user_id)
    if user is not None:
        _mongo_save_user(user)


def delete_user(user_id: int) -> None:
    user = get_user(user_id)
    if user is not None and _MONGO_USERS is not None:
        _MONGO_USERS.delete_one({"username": user["username"]})
    with _Cursor() as con:
        con.execute("DELETE FROM bots WHERE user_id = ?", (user_id,))
        con.execute("DELETE FROM users WHERE id = ?", (user_id,))


def is_admin_account(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user and user["role"] == "admin")


# ------------------------------------------------------------------ bots


def create_bot(user_id: int, name: str, uid: str, dir_name: str) -> int:
    return _write(
        "INSERT INTO bots (user_id, name, uid, dir_name, entry_point, status, created_at) "
        "VALUES (?, ?, ?, ?, '', 'pending', ?)",
        (user_id, name, uid, dir_name, utc_now()),
    )


def get_bot(bot_id: int, user_id: int | None = None) -> sqlite3.Row | None:
    """Fetch a bot, optionally scoped to its owner.

    Pass ``user_id`` from every request handler: that single argument is what
    stops one account from addressing another account's bot.
    """
    if user_id is None:
        return _one("SELECT * FROM bots WHERE id = ?", (bot_id,))
    return _one("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user_id))


def get_bot_by_uid(uid: str) -> sqlite3.Row | None:
    return _one("SELECT * FROM bots WHERE uid = ?", (uid,))


def list_bots(user_id: int) -> list[sqlite3.Row]:
    return _q("SELECT * FROM bots WHERE user_id = ? ORDER BY id DESC", (user_id,))


def list_all_bots() -> list[sqlite3.Row]:
    return _q(
        "SELECT b.*, u.username FROM bots b "
        "JOIN users u ON u.id = b.user_id ORDER BY b.id DESC"
    )


def stats(user_id: int) -> dict[str, int]:
    rows = _q(
        "SELECT status, COUNT(*) AS n FROM bots WHERE user_id = ? GROUP BY status",
        (user_id,),
    )
    out = {"total": 0, "running": 0, "pending": 0, "stopped": 0, "rejected": 0, "setup": 0}
    for row in rows:
        out[row["status"]] = int(row["n"])
        out["total"] += int(row["n"])
    return out


def set_bot_status(
    bot_id: int,
    status: str,
    *,
    pid: int | None = None,
    exit_code: int | None = None,
    bump_restarts: bool = False,
) -> None:
    """Update status and the timestamps that hang off it."""
    sets = ["status = ?"]
    args: list[Any] = [status]
    if pid is not None:
        sets.append("pid = ?")
        args.append(pid)
    if exit_code is not None:
        sets.append("exit_code = ?")
        args.append(exit_code)
    if status == "running":
        sets.append("started_at = ?")
        args.append(utc_now())
        sets.append("stopped_at = NULL")
        sets.append("exit_code = NULL")
    elif status in ("stopped", "rejected"):
        sets.append("stopped_at = ?")
        args.append(utc_now())
        sets.append("pid = NULL")
    if bump_restarts:
        sets.append("restarts = restarts + 1")
    args.append(bot_id)
    _write(f"UPDATE bots SET {', '.join(sets)} WHERE id = ?", args)


def set_entry_point(bot_id: int, entry_point: str, status: str = "setup") -> None:
    _write(
        "UPDATE bots SET entry_point = ?, status = ? WHERE id = ?",
        (entry_point, status, bot_id),
    )


def clear_bot_pid(bot_id: int) -> None:
    _write("UPDATE bots SET pid = NULL WHERE id = ?", (bot_id,))


def delete_bot(bot_id: int) -> None:
    _write("DELETE FROM bots WHERE id = ?", (bot_id,))


def running_bots() -> list[sqlite3.Row]:
    return _q("SELECT * FROM bots WHERE status = 'running' AND pid IS NOT NULL")


def pending_bots() -> list[sqlite3.Row]:
    return _q(
        "SELECT b.*, u.username FROM bots b JOIN users u ON u.id = b.user_id "
        "WHERE b.status IN ('pending', 'rejected') ORDER BY b.id ASC"
    )

# ---------------------------------------------------------- Telegram pairing

def init_telegram_tables() -> None:
    with _Cursor() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_pair_codes (
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS telegram_links (
            chat_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE,
            linked_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)


def create_telegram_pair_code(user_id: int, code: str, expires_at: int) -> None:
    with _Cursor() as con:
        con.execute("DELETE FROM telegram_pair_codes WHERE user_id = ? OR expires_at < ?", (user_id, int(datetime.now(timezone.utc).timestamp())))
        con.execute("INSERT INTO telegram_pair_codes(code, user_id, expires_at) VALUES (?, ?, ?)", (code, user_id, expires_at))


def consume_telegram_pair_code(code: str, chat_id: int) -> int | None:
    now = int(datetime.now(timezone.utc).timestamp())
    with _Cursor() as con:
        row = con.execute("SELECT user_id, expires_at FROM telegram_pair_codes WHERE code = ?", (code,)).fetchone()
        if row is None or int(row["expires_at"]) < now:
            con.execute("DELETE FROM telegram_pair_codes WHERE code = ?", (code,))
            return None
        # A Telegram chat can only be linked to one panel account, and vice versa.
        con.execute("DELETE FROM telegram_links WHERE chat_id = ? OR user_id = ?", (chat_id, row["user_id"]))
        con.execute("INSERT INTO telegram_links(chat_id, user_id, linked_at) VALUES (?, ?, ?)", (chat_id, row["user_id"], utc_now()))
        con.execute("DELETE FROM telegram_pair_codes WHERE code = ?", (code,))
        return int(row["user_id"])


def link_telegram_account(user_id: int, chat_id: int) -> None:
    """Attach a Telegram chat to an existing panel account, replacing older links."""
    with _Cursor() as con:
        con.execute(
            "DELETE FROM telegram_links WHERE chat_id = ? OR user_id = ?",
            (chat_id, user_id),
        )
        con.execute(
            "INSERT INTO telegram_links(chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, utc_now()),
        )


def get_user_by_telegram_chat(chat_id: int) -> sqlite3.Row | None:
    return _one("SELECT u.* FROM users u JOIN telegram_links t ON t.user_id = u.id WHERE t.chat_id = ? AND u.is_banned = 0", (chat_id,))


def unlink_telegram_chat(chat_id: int) -> None:
    _write("DELETE FROM telegram_links WHERE chat_id = ?", (chat_id,))
