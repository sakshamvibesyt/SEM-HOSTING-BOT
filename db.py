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
from datetime import datetime, timezone
from typing import Any, Iterable

from werkzeug.security import check_password_hash, generate_password_hash

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


# ----------------------------------------------------------------- users


def create_user(username: str, password: str, role: str = "user") -> int | None:
    """Insert a user. Returns the new id, or None when the name is taken."""
    uid = _write(
        "INSERT INTO users (username, password_hash, role, is_admin, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            username,
            generate_password_hash(password),
            role,
            1 if role == "admin" else 0,
            utc_now(),
        ),
    )
    return uid or None


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


def delete_user(user_id: int) -> None:
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
