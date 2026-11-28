"""MongoDB-backed durable snapshots for the GX Hosting panel.

The live application keeps SQLite for fast, compatible reads/writes. This
module snapshots the SQLite database plus every user's uploaded bot directory
into MongoDB GridFS. On a fresh/ephemeral deployment it restores the newest
snapshot before the database is initialized.

Passwords remain Werkzeug password hashes; plaintext passwords are never
stored by this backup layer.
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


def _safe_restore(archive_path: str, data_dir: str) -> None:
    root = os.path.realpath(data_dir)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.replace("\\", "/")
            if name.startswith("/") or any(p == ".." for p in name.split("/")):
                raise ValueError("Backup archive contains an unsafe path")
            if not (member.isdir() or member.isfile()):
                continue  # Never restore symlinks, hardlinks, devices, or pipes.
            target = os.path.realpath(os.path.join(root, name))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("Backup archive escaped the data directory")
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = archive.extractfile(member)
            if src is None:
                continue
            tmp = target + ".restore-tmp"
            with src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)


def restore_if_needed(data_dir: str) -> bool:
    """Restore newest Mongo snapshot only when the local SQLite DB is absent."""
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "panel.db")
    if os.path.isfile(db_path) and os.path.getsize(db_path) > 0:
        return False
    uri, _ = _settings()
    if not uri:
        log.warning("MongoDB backup is not configured; set MONGODB_URI to enable durable restore")
        return False
    try:
        bucket = _get_bucket()
        latest = _latest_file(bucket)
        if latest is None:
            log.info("No MongoDB backup exists yet; the first snapshot will seed the backup store")
            return False
        with tempfile.TemporaryDirectory(prefix="gx-restore-") as temp:
            archive_path = os.path.join(temp, "latest.tar.gz")
            with open(archive_path, "wb") as out:
                bucket.download_to_stream(latest._id, out)
            _safe_restore(archive_path, data_dir)
        log.info("Restored hosting data from MongoDB snapshot %s", latest._id)
        return True
    except Exception as exc:
        # Do not silently initialize a blank account database when a backup
        # should exist but MongoDB is temporarily unreachable.
        if not os.path.exists(db_path):
            raise RuntimeError("MongoDB restore failed and no local panel.db exists; refusing to start with empty accounts") from exc
        log.exception("MongoDB restore failed; keeping the existing local data: %s", exc)
        return False


def _sqlite_consistent_copy(source_path: str, target_path: str) -> bool:
    if not os.path.isfile(source_path):
        return False
    source = sqlite3.connect(source_path, timeout=30)
    target = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(target, pages=256, sleep=0.05)
        target.commit()
    finally:
        target.close()
        source.close()
    return True


def create_snapshot() -> bool:
    """Create a consistent archive and upload it to GridFS."""
    if not _data_dir:
        return False
    bucket = _get_bucket()
    if bucket is None:
        return False
    data_dir = _data_dir
    db_path = os.path.join(data_dir, "panel.db")
    if not os.path.isfile(db_path):
        return False
    with tempfile.TemporaryDirectory(prefix="gx-backup-") as temp:
        snapshot_db = os.path.join(temp, "panel.db")
        if not _sqlite_consistent_copy(db_path, snapshot_db):
            return False
        archive_path = os.path.join(temp, "hosting-backup.tar.gz")
        with tarfile.open(archive_path, "w:gz", compresslevel=5) as archive:
            archive.add(snapshot_db, arcname="panel.db")
            for folder in ("uploads", "logs"):
                path = os.path.join(data_dir, folder)
                if os.path.isdir(path):
                    archive.add(path, arcname=folder, recursive=True)
            secret_path = os.path.join(data_dir, "flask_secret.key")
            if os.path.isfile(secret_path):
                archive.add(secret_path, arcname="flask_secret.key")
        created = datetime.now(timezone.utc)
        with open(archive_path, "rb") as stream:
            file_id = bucket.upload_from_stream(
                f"gx-hosting-{created.strftime('%Y%m%d-%H%M%S')}.tar.gz",
                stream,
                metadata={"created_at": created.isoformat(), "kind": "full-hosting-snapshot"},
            )
    _prune_old(bucket)
    log.info("MongoDB backup completed: %s", file_id)
    return True


def _prune_old(bucket: GridFSBucket) -> None:
    try:
        keep = max(2, int(os.environ.get("BACKUP_KEEP_COUNT", "24")))
        old = list(bucket.find({}, sort=[("uploadDate", DESCENDING)]).skip(keep))
        for item in old:
            bucket.delete(item._id)
    except Exception:
        log.exception("Could not prune old MongoDB backups")


def request_backup() -> None:
    """Ask the worker for a near-immediate snapshot after a successful change."""
    if _worker and _worker.is_alive():
        _wake.set()


def _backup_loop(interval: int) -> None:
    # Initial event seeds MongoDB after the application has initialized SQLite.
    _wake.set()
    while not _stop.is_set():
        _wake.wait(interval)
        _wake.clear()
        try:
            create_snapshot()
        except Exception:
            log.exception("MongoDB snapshot failed; the panel will keep running and retry")
        if _stop.is_set():
            break


def start(data_dir: str) -> None:
    global _data_dir, _worker
    _data_dir = data_dir
    uri, _ = _settings()
    if not uri:
        log.warning("MongoDB backup disabled: set MONGODB_URI (or MONGO_URL) in hosting environment")
        return
    if _worker and _worker.is_alive():
        return
    interval = max(60, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "300")))
    _stop.clear()
    _worker = threading.Thread(target=_backup_loop, args=(interval,), name="mongo-backup", daemon=True)
    _worker.start()
    log.info("MongoDB backup worker started (interval=%ss)", interval)


def stop() -> None:
    if not _worker or not _worker.is_alive():
        return
    _stop.set()
    _wake.set()
    _worker.join(timeout=20)
    if _client is not None:
        _client.close()
