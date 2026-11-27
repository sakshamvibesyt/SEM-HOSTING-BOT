"""
Bot lifecycle engine.

Responsibilities, in the order a bot goes through them:

  save_upload()   store a .py / .zip payload and unpack it safely
  detect_entry()  pick the file to execute
  start()         spawn it under a generated ``_sc_launcher_<entry>.py``
  monitor         reap dead children in a background thread
  read_log()      serve the tail of the per-bot log
  stop()          terminate the whole process group

Every function takes a bot row (or ids) and resolves paths from ``dir_name``.
Nothing here trusts a client-supplied path: file names are validated with
:func:`safe_relpath` before they touch the filesystem.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from typing import Any

import db

# ------------------------------------------------------------------ config

DATA_DIR = "data"
MAX_UPLOAD_MB = 20
MAX_LOG_MB = 5
MONITOR_INTERVAL = 3
INHERIT_ENV = False

_ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py", "start.py", "__main__.py")

# Names a .zip may not contain, anywhere in its tree.
_BLOCKED_SUFFIXES = (".pth", ".pthc")
_BLOCKED_ZIP_NAMES = {"sitecustomize.py", "usercustomize.py"}

_LAUNCHER_PREFIX = "_sc_launcher_"

_PROCS: dict[int, subprocess.Popen] = {}
_PROCS_LOCK = threading.RLock()
_MONITOR_THREAD: threading.Thread | None = None
_STOPPING: set[int] = set()

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def configure(
    *,
    data_dir: str,
    max_upload_mb: int,
    max_log_mb: int,
    monitor_interval: int,
    inherit_env: bool,
) -> None:
    global DATA_DIR, MAX_UPLOAD_MB, MAX_LOG_MB, MONITOR_INTERVAL, INHERIT_ENV
    DATA_DIR = data_dir
    MAX_UPLOAD_MB = max_upload_mb
    MAX_LOG_MB = max_log_mb
    MONITOR_INTERVAL = monitor_interval
    INHERIT_ENV = inherit_env
    os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)


# ------------------------------------------------------------------- paths


def user_root(user_id: int) -> str:
    return os.path.join(DATA_DIR, "uploads", f"user_{user_id}")


def bot_root(bot: Any) -> str:
    return os.path.join(user_root(bot["user_id"]), bot["dir_name"])


def log_path(bot_id: int) -> str:
    return os.path.join(DATA_DIR, "logs", f"bot_{bot_id}.log")


def safe_relpath(name: str) -> str | None:
    """Normalise a client-supplied relative path, or return None if unsafe.

    Rejects absolute paths, drive letters, ``..`` traversal, NUL bytes and
    anything that resolves outside the bot directory.
    """
    if not name or "\x00" in name:
        return None
    name = name.replace("\\", "/").strip()
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def resolve_inside(root: str, relpath: str) -> str | None:
    """Join ``relpath`` onto ``root`` and confirm the result stays inside."""
    safe = safe_relpath(relpath)
    if safe is None:
        return None
    target = os.path.realpath(os.path.join(root, safe))
    base = os.path.realpath(root)
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


def generate_uid() -> str:
    """HOST-XXXXX-YYYY-XXXX, matching the original panel's bot uid format."""
    import random
    import string

    def block(n: int) -> str:
        alphabet = string.ascii_uppercase + string.digits
        return "".join(random.choice(alphabet) for _ in range(n))

    return f"HOST-{block(5)}-{time.strftime('%Y')}-{block(4)}"


# ------------------------------------------------------------------ upload


def save_upload(user_id: int, filename: str, stream, size: int) -> tuple[str, str, str]:
    """Persist an uploaded ``.py``/``.zip``.

    Returns ``(bot_name, uid, dir_name)``. Raises ``ValueError`` with a
    user-facing message on any reject.
    """
    if not filename:
        raise ValueError("No file selected.")
    base = os.path.basename(filename.replace("\\", "/"))
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext not in ("py", "zip"):
        raise ValueError("Only .py or .zip files are allowed.")
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"File too large (max {MAX_UPLOAD_MB} MB).")

    os.makedirs(user_root(user_id), exist_ok=True)
    uid = generate_uid()
    # The directory is keyed by uid, not by the client's filename, so an
    # upload can never overwrite a sibling bot's tree.
    dir_name = uid
    target_dir = os.path.join(user_root(user_id), dir_name)
    os.makedirs(target_dir, exist_ok=True)

    if ext == "py":
        bot_name = base
        with open(os.path.join(target_dir, base), "wb") as fh:
            shutil.copyfileobj(stream, fh)
        return bot_name, uid, dir_name

    # .zip: stage, then validate every member before extracting anything.
    tmp_zip = os.path.join(target_dir, "_upload.zip")
    with open(tmp_zip, "wb") as fh:
        shutil.copyfileobj(stream, fh)
    try:
        _extract_zip(tmp_zip, target_dir)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    bot_name = base
    return bot_name, uid, dir_name


def _extract_zip(zip_file: str, target_dir: str) -> None:
    try:
        with zipfile.ZipFile(zip_file) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f"Archive member is corrupt: {bad}")
            for info in zf.infolist():
                member = info.filename.replace("\\", "/")
                if info.is_dir():
                    continue
                safe = safe_relpath(member)
                if safe is None:
                    raise ValueError("Archive contains an unsafe path.")
                leaf = safe.rsplit("/", 1)[-1]
                if leaf in _BLOCKED_ZIP_NAMES or leaf.lower().endswith(_BLOCKED_SUFFIXES):
                    raise ValueError(f"Archive contains a blocked file: {leaf}")
                dest = resolve_inside(target_dir, safe)
                if dest is None:
                    raise ValueError("Archive contains an unsafe path.")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid or corrupted zip archive.") from exc


def collapse_single_dir(root: str) -> None:
    """If a zip wrapped everything in one folder, hoist its contents up."""
    entries = [e for e in os.listdir(root) if not e.startswith(".")]
    if len(entries) != 1:
        return
    only = os.path.join(root, entries[0])
    if not os.path.isdir(only):
        return
    if any(os.path.exists(os.path.join(root, n)) for n in os.listdir(only)):
        for name in os.listdir(only):
            shutil.move(os.path.join(only, name), os.path.join(root, name))
        shutil.rmtree(only, ignore_errors=True)


def list_py_files(bot: Any) -> list[str]:
    """Relative paths of every .py under the bot root (excluding launchers)."""
    root = bot_root(bot)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", "venv", ".venv")]
        for name in filenames:
            if not name.endswith(".py") or name.startswith(_LAUNCHER_PREFIX):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            found.append(rel)
    return sorted(found)


def detect_entry(bot: Any) -> tuple[str | None, str]:
    """Guess the entry point.

    Returns ``(relpath_or_None, confidence)`` where confidence is
    ``'exact'`` for a known entry filename, ``'single'`` when the tree holds
    exactly one .py, and ``'none'`` when the user must choose.
    """
    files = list_py_files(bot)
    if not files:
        return None, "none"
    collapsed = [f for f in files if "/" not in f]
    for candidate in _ENTRY_CANDIDATES:
        for f in collapsed:
            if f.lower() == candidate:
                return f, "exact"
    if len(files) == 1:
        return files[0], "single"
    # Fall back to the shallowest file whose name hints at a bot entry.
    for f in collapsed:
        stem = f.rsplit(".", 1)[0].lower()
        if stem in ("bot", "main", "app", "run", "start"):
            return f, "exact"
    return None, "none"


# ---------------------------------------------------------------- launcher


def launcher_path(bot: Any, entry: str) -> str:
    leaf = entry.rsplit("/", 1)[-1]
    return os.path.join(bot_root(bot), _LAUNCHER_PREFIX + leaf)


def write_launcher(bot: Any, entry: str) -> str:
    """Generate the wrapper used to execute ``entry``.

    The wrapper puts the entry's own directory first on ``sys.path`` (so
    sibling modules import as the author intended), loads the bot's ``.env``
    into the environment, and then executes the entry file.
    """
    root = bot_root(bot)
    entry_dir = os.path.dirname(os.path.join(root, entry)) or root
    launcher = launcher_path(bot, entry)
    # _ENTRY must be the leaf name: _ENTRY_DIR already includes the sub-path,
    # otherwise a nested entry resolves to src/src/bot.py.
    entry_leaf = os.path.basename(entry)
    body = f'''# auto-generated by the panel - do not edit
import os, runpy, sys

_ENTRY = {entry_leaf!r}
_ENTRY_DIR = {entry_dir!r}
_HERE = os.path.dirname(os.path.abspath(__file__))

def _load_env(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")

for _candidate in (os.path.join(_HERE, ".env"), os.path.join(_ENTRY_DIR, ".env")):
    if os.path.isfile(_candidate):
        _load_env(_candidate)
        break

sys.path.insert(0, _ENTRY_DIR)
sys.path.insert(0, _HERE)
sys.argv = [_ENTRY]

_target = os.path.join(_ENTRY_DIR, _ENTRY)
while _target.startswith("//"):
    _target = _target[1:]
runpy.run_path(_target, run_name="__main__")
'''
    with open(launcher, "w", encoding="utf-8") as fh:
        fh.write(body)
    return launcher


def _clean_env(bot: Any) -> dict[str, str]:
    if INHERIT_ENV:
        env = dict(os.environ)
    else:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": bot_root(bot),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "TERM": "xterm-256color",
        }
    env["BOT_UID"] = bot["uid"]
    env["BOT_NAME"] = bot["name"]
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# --------------------------------------------------------------- read-only


def read_log(bot_id: int, tail_lines: int = 400) -> str:
    path = log_path(bot_id)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return _ANSI.sub("", "".join(lines[-tail_lines:]))


def read_log_full(bot_id: int) -> str:
    path = log_path(bot_id)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return _ANSI.sub("", fh.read())
    except OSError:
        return ""


def _log_header(bot: Any, entry: str) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    return (
        "\n" + "=" * 50 + "\n"
        f"  START  {stamp} UTC\n"
        f"  FILE   {os.path.join(bot_root(bot), entry)}\n"
        "  [AUTO-INSTALL enabled]\n"
        + "=" * 50 + "\n"
    )


def _rotate_log(bot_id: int) -> None:
    path = log_path(bot_id)
    if not os.path.exists(path):
        return
    try:
        if os.path.getsize(path) > MAX_LOG_MB * 1024 * 1024:
            os.replace(path, path + ".1")
    except OSError:
        pass


# ----------------------------------------------------------------- control


def is_running(bot_id: int) -> bool:
    with _PROCS_LOCK:
        proc = _PROCS.get(bot_id)
        return proc is not None and proc.poll() is None


def uptime_seconds(bot: Any) -> int | None:
    """Seconds since the current run started, or None when not running."""
    if bot["status"] != "running" or not bot["started_at"]:
        return None
    try:
        # started_at is stored as UTC (see db.utc_now); parsing it must not
        # apply the host's local offset or the uptime is wrong by the TZ delta.
        started = datetime.strptime(bot["started_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None
    delta = int(datetime.now(timezone.utc).timestamp() - started.timestamp())
    return max(delta, 0)


def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _install_requirements(bot: Any, log_fh) -> None:
    req = os.path.join(bot_root(bot), "requirements.txt")
    if not os.path.isfile(req):
        return
    log_fh.write("  [pip] installing requirements.txt ...\n")
    log_fh.flush()
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "-r", req],
            cwd=bot_root(bot),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            timeout=600,
            env=_clean_env(bot),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log_fh.write(f"  [pip] skipped: {exc}\n")
    log_fh.flush()


def start(bot: Any, *, bump_restarts: bool = False) -> tuple[bool, str, int | None]:
    """Start a bot. Returns ``(ok, message, pid)``."""
    entry = bot["entry_point"]
    if not entry:
        return False, "No entry point selected. Run setup first.", None
    root = bot_root(bot)
    entry_file = resolve_inside(root, entry)
    if entry_file is None or not os.path.isfile(entry_file):
        return False, "Entry point file is missing.", None

    with _PROCS_LOCK:
        proc = _PROCS.get(bot["id"])
        if proc is not None and proc.poll() is None:
            return False, "Bot is already running.", proc.pid

    # Preparation happens outside the lock: installing a large
    # requirements.txt can take minutes and must not freeze every other
    # control request in the panel.
    _rotate_log(bot["id"])
    launcher = write_launcher(bot, entry)
    os.makedirs(os.path.dirname(log_path(bot["id"])), exist_ok=True)
    with open(log_path(bot["id"]), "a", encoding="utf-8") as log_fh:
        log_fh.write(_log_header(bot, entry))
        log_fh.flush()
        _install_requirements(bot, log_fh)

    with _PROCS_LOCK:
        # Re-check under the lock: another request may have started it while
        # the requirements were installing.
        proc = _PROCS.get(bot["id"])
        if proc is not None and proc.poll() is None:
            return False, "Bot is already running.", proc.pid

        try:
            log_fh = open(log_path(bot["id"]), "a", encoding="utf-8")
        except OSError as exc:
            return False, f"Could not open the log file: {exc}", None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", launcher],
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=_clean_env(bot),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return False, f"Failed to start: {exc}", None
        finally:
            # The child holds its own descriptor; the parent copy is done.
            log_fh.close()

        _PROCS[bot["id"]] = proc
        _STOPPING.discard(bot["id"])
        db.set_bot_status(bot["id"], "running", pid=proc.pid, bump_restarts=bump_restarts)
        return True, "Started.", proc.pid


def stop(bot: Any) -> tuple[bool, str]:
    bot_id = bot["id"]
    with _PROCS_LOCK:
        proc = _PROCS.pop(bot_id, None)
        _STOPPING.add(bot_id)
    try:
        if proc is not None and proc.poll() is None:
            _terminate(proc)
        db.set_bot_status(bot_id, "stopped")
        db.clear_bot_pid(bot_id)
    finally:
        # Always release the marker: the sweep only inspects _PROCS, so a
        # leftover id here would never be cleaned up.
        with _PROCS_LOCK:
            _STOPPING.discard(bot_id)
    return True, "Stopped."


def restart(bot: Any) -> tuple[bool, str, int | None]:
    ok, msg = stop(bot)
    if not ok:
        return False, msg, None
    fresh = db.get_bot(bot["id"], bot["user_id"])
    if fresh is None:
        return False, "Bot no longer exists.", None
    return start(fresh, bump_restarts=True)


def delete(bot: Any) -> tuple[bool, str]:
    stop(bot)
    root = bot_root(bot)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    try:
        os.remove(log_path(bot["id"]))
    except OSError:
        pass
    db.delete_bot(bot["id"])
    return True, "Bot deleted."


def send_input(bot: Any, text: str) -> tuple[bool, str]:
    with _PROCS_LOCK:
        proc = _PROCS.get(bot["id"])
    if proc is None or proc.poll() is not None or proc.stdin is None:
        return False, "Bot is not running."
    try:
        proc.stdin.write((text + "\n").encode("utf-8", "replace"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        return False, "Could not write to bot stdin."
    return True, "Sent."


def _terminate(proc: subprocess.Popen, timeout: float = 8.0) -> None:
    """Stop the bot's whole process group, escalating to SIGKILL."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ----------------------------------------------------------------- monitor


def _monitor_loop() -> None:
    while True:
        time.sleep(MONITOR_INTERVAL)
        try:
            _sweep_once()
        except Exception:
            # A monitor crash must never take the web worker down.
            continue


def _sweep_once() -> None:
    with _PROCS_LOCK:
        snapshot = dict(_PROCS)
    for bot_id, proc in snapshot.items():
        code = proc.poll()
        if code is None:
            continue
        with _PROCS_LOCK:
            _PROCS.pop(bot_id, None)
            stopping = bot_id in _STOPPING
            _STOPPING.discard(bot_id)
        if stopping:
            continue
        # The process died on its own: record the exit and mark it stopped.
        try:
            db.set_bot_status(bot_id, "stopped", exit_code=code)
            with open(log_path(bot_id), "a", encoding="utf-8") as fh:
                fh.write(f"\n  [exit] process ended with code {code}\n")
        except Exception:
            pass


def start_monitor() -> None:
    """Idempotently start the background reaper thread."""
    global _MONITOR_THREAD
    if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
        return
    _MONITOR_THREAD = threading.Thread(target=_monitor_loop, name="bot-monitor", daemon=True)
    _MONITOR_THREAD.start()


def _still_our_child(pid: int) -> bool:
    """True when ``pid`` is alive AND is one of our launcher processes.

    Pids are recycled, so a bare ``os.kill(pid, 0)`` is not enough: after a
    restart the pid could belong to something unrelated. The cmdline check
    makes the boot-time cleanup safe.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    return _LAUNCHER_PREFIX in cmdline


def reconcile_on_boot() -> None:
    """Clear stale 'running' rows left behind by a previous worker.

    Children are spawned with ``start_new_session=True`` so a hard worker
    restart does not kill them. Any bot still marked running at boot therefore
    belongs to a dead worker and is unmanageable; terminate it if it is really
    ours, then mark it stopped.
    """
    for bot in db.running_bots():
        pid = bot["pid"]
        if pid and _still_our_child(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        try:
            with open(log_path(bot["id"]), "a", encoding="utf-8") as fh:
                fh.write("\n  [panel] worker restarted; bot reaped\n")
        except OSError:
            pass
        db.set_bot_status(bot["id"], "stopped")
        db.clear_bot_pid(bot["id"])


def shutdown() -> None:
    """Best-effort stop of every bot this worker owns (called at exit)."""
    with _PROCS_LOCK:
        procs = list(_PROCS.values())
        _PROCS.clear()
    for proc in procs:
        if proc.poll() is None:
            _terminate(proc, timeout=3.0)


# ------------------------------------------------------------------ files


def list_bot_files(bot: Any) -> list[dict[str, Any]]:
    """Flat listing of a bot's files for the file manager."""
    root = bot_root(bot)
    out: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append(
                {
                    "name": rel,
                    "size": st.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    "is_py": name.endswith(".py"),
                    "is_entry": rel == bot["entry_point"],
                }
            )
    out.sort(key=lambda f: (not f["is_entry"], f["name"]))
    return out


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def delete_file(bot: Any, relpath: str) -> tuple[bool, str]:
    target = resolve_inside(bot_root(bot), relpath)
    if target is None:
        return False, "Invalid file path."
    if not os.path.isfile(target):
        return False, "File not found."
    if relpath == bot["entry_point"]:
        return False, "Cannot delete the entry point."
    try:
        os.remove(target)
    except OSError as exc:
        return False, f"Delete failed: {exc}"
    return True, "File deleted."


def file_for_download(bot: Any, relpath: str) -> str | None:
    target = resolve_inside(bot_root(bot), relpath)
    if target is None or not os.path.isfile(target):
        return None
    return target


def dir_size(bot: Any) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(bot_root(bot)):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


# --------------------------------------------------------------- scanning


_SUSPICIOUS = (
    "os.system(", "subprocess.", "socket.", "requests.post", "urllib.request",
    "eval(", "exec(", "base64.b64decode", "ctypes.", "__import__(",
    "shutil.rmtree", "os.remove(", "open('/etc", 'open("/etc',
)


def scan_security(bot: Any) -> tuple[str, list[str]]:
    """Very small static check used for the "Security" badge.

    This is a heuristic hint for the owner, not a sandbox. Uploaded bots run
    as ordinary processes; treat every upload as trusted code.
    """
    hits: list[str] = []
    root = bot_root(bot)
    budget = 200_000  # bytes examined
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read(min(budget, 200_000))
            except OSError:
                continue
            budget -= len(text)
            for needle in _SUSPICIOUS:
                if needle in text:
                    hits.append(needle)
            if budget <= 0:
                break
        if budget <= 0:
            break
    if not hits:
        return "safe", []
    return "review", sorted(set(hits))[:8]
