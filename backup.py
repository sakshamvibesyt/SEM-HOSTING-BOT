"""MongoDB-backed account-only persistence for the hosting panel.

Only the users table is stored in MongoDB. Uploaded bot files, logs, bot
metadata, and other runtime data are deliberately excluded. Passwords remain
Werkzeug hashes; plaintext passwords are never written by this module.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient, DESCENDING
from gridfs import GridFSBucket

log = logging.getLogger("gx.backup")
_client: Optional[MongoClient] = None
_bucket: Optional[GridFSBucket] = None
_data_dir: Optional[str] = None
_worker: Optional[threading.Thread] = None
_wake = threading.Event()
_stop = threading.Event()
_lock = threading.RLock()

_USERS_SCHEMA = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',
    is_banned     INTEGER NOT NULL DEFAULT 0,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
)
"""
_USER_COLUMNS = (
    "id", "username", "password_hash", "role", "is_banned", "is_admin", "created_at"
)


def _settings():
    uri = (os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL") or "").strip()
    db_name = (os.environ.get("MONGODB_DATABASE") or "gx_hosting").strip()
    return uri, db_name


def _get_bucket() -> Optional[GridFSBucket]:
    global _client, _bucket
    uri, db_name = _settings()
    if not uri:
        return None
    with _lock:
        if _bucket is None:
            _client = MongoClient(uri, serverSelectionTimeoutMS=7000, connectTimeoutMS=7000)
            _client.admin.command("ping")
            _bucket = GridFSBucket(_client[db_name], bucket_name="hosting_backups")
        return _bucket


def _latest_file(bucket: GridFSBucket):
    return next(iter(bucket.find({}, sort=[("uploadDate", DESCENDING)]).limit(1)), None)


def _accounts_only_copy(source_path: str, target_path: str) -> bool:
    """Copy only account credentials/flags into a fresh, minimal SQLite DB."""
    if not os.path.isfile(source_path):
        return False
    src = sqlite3.connect(f"file:{os.path.abspath(source_path)}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target_path, timeout=30)
    try:
        exists = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if not exists:
            return False
        available = {row[1] for row in src.execute("PRAGMA table_info(users)")}
        missing = set(_USER_COLUMNS) - available
        if missing:
            raise RuntimeError("Users table is missing required columns: " + ", ".join(sorted(missing)))
        dst.execute(_USERS_SCHEMA)
        columns = ", ".join(_USER_COLUMNS)
        rows = src.execute(f"SELECT {columns} FROM users").fetchall()
        placeholders = ", ".join("?" for _ in _USER_COLUMNS)
        if rows:
            dst.executemany(
                f"INSERT INTO users ({columns}) VALUES ({placeholders})", rows
            )
        dst.commit()
        return True
    finally:
        dst.close()
        src.close()


def _extract_accounts_from_archive(archive_path: str, target_path: str) -> bool:
    """Read only panel.db from a legacy snapshot; never extract uploads/logs."""
    with tempfile.TemporaryDirectory(prefix="gx-legacy-restore-") as temp:
        db_path = os.path.join(temp, "panel.db")
        with tarfile.open(archive_path, "r:gz") as archive:
            member = next((m for m in archive.getmembers()
                           if m.isfile() and m.name.replace("\\", "/").lstrip("./") == "panel.db"), None)
            if member is None:
                return False
            src = archive.extractfile(member)
            if src is None:
                return False
            with src, open(db_path, "wb") as out:
                shutil.copyfileobj(src, out)
        return _accounts_only_copy(db_path, target_path)


def restore_if_needed(data_dir: str) -> bool:
    """Restore accounts only when the local SQLite database is absent."""
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "panel.db")
    if os.path.isfile(db_path) and os.path.getsize(db_path) > 0:
        return False
    uri, _ = _settings()
    if not uri:
        log.warning("MongoDB account backup is not configured; set MONGODB_URI")
        return False
    try:
        bucket = _get_bucket()
        latest = _latest_file(bucket)
        if latest is None:
            log.info("No MongoDB account backup exists yet")
            return False
        with tempfile.TemporaryDirectory(prefix="gx-restore-") as temp:
            archive_path = os.path.join(temp, "latest.tar.gz")
            accounts_path = os.path.join(temp, "accounts.db")
            with open(archive_path, "wb") as out:
                bucket.download_to_stream(latest._id, out)
            if not _extract_accounts_from_archive(archive_path, accounts_path):
                raise RuntimeError("The latest backup does not contain a readable users table")
            os.replace(accounts_path, db_path)
        log.info("Restored account records only from MongoDB snapshot %s", latest._id)
        return True
    except Exception as exc:
        if not os.path.exists(db_path):
            raise RuntimeError(
                "MongoDB account restore failed and no local panel.db exists; "
                "refusing to start with empty accounts"
            ) from exc
        log.exception("MongoDB account restore failed; keeping local data: %s", exc)
        return False


def _delete_existing_snapshots(bucket: GridFSBucket) -> None:
    """Remove legacy full snapshots so their uploaded files stop consuming quota."""
    removed = 0
    for item in bucket.find({}, projection={"_id": 1}):
        bucket.delete(item["_id"])
        removed += 1
    if removed:
        log.info("Removed %s old full/legacy hosting snapshots", removed)


def create_snapshot() -> bool:
    """Upload a tiny archive containing only usernames and password hashes."""
    if not _data_dir:
        return False
    bucket = _get_bucket()
    if bucket is None:
        return False
    db_path = os.path.join(_data_dir, "panel.db")
    if not os.path.isfile(db_path):
        return False

    with tempfile.TemporaryDirectory(prefix="gx-accounts-backup-") as temp:
        accounts_db = os.path.join(temp, "accounts.db")
        if not _accounts_only_copy(db_path, accounts_db):
            return False
        archive_path = os.path.join(temp, "accounts-only.tar.gz")
        with tarfile.open(archive_path, "w:gz", compresslevel=6) as archive:
            archive.add(accounts_db, arcname="panel.db")

        # Delete old snapshots before inserting the small one. This is needed
        # because the old full snapshots may already have filled the Atlas quota.
        _delete_existing_snapshots(bucket)

        created = datetime.now(timezone.utc)
        with open(archive_path, "rb") as stream:
            file_id = bucket.upload_from_stream(
                f"gx-accounts-{created.strftime('%Y%m%d-%H%M%S')}.tar.gz",
                stream,
                metadata={"created_at": created.isoformat(), "kind": "accounts-only"},
            )
    log.info("MongoDB account-only backup completed: %s", file_id)
    return True


def request_backup() -> None:
    if _worker and _worker.is_alive():
        _wake.set()


def _backup_loop(interval: int) -> None:
    _wake.set()
    while not _stop.is_set():
        _wake.wait(interval)
        _wake.clear()
        try:
            create_snapshot()
        except Exception:
            log.exception("MongoDB account backup failed; the panel will keep running and retry")
        if _stop.is_set():
            break


def start(data_dir: str) -> None:
    global _data_dir, _worker
    _data_dir = data_dir
    uri, _ = _settings()
    if not uri:
        log.warning("MongoDB account backup disabled: set MONGODB_URI (or MONGO_URL)")
        return
    if _worker and _worker.is_alive():
        return
    interval = max(60, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "300")))
    _stop.clear()
    _worker = threading.Thread(target=_backup_loop, args=(interval,), name="mongo-account-backup", daemon=True)
    _worker.start()
    log.info("MongoDB account-only backup worker started (interval=%ss)", interval)


def stop() -> None:
    if not _worker or not _worker.is_alive():
        return
    _stop.set()
    _wake.set()
    _worker.join(timeout=20)
    if _client is not None:
        _client.close()
