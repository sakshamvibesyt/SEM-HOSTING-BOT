# GX Bot Hosting

A Flask panel for uploading and running Python chat bots, built to match the
layout and behaviour of `gxserverx.onrender.com`: same routes, same JSON
contracts, same flash strings, same cyber-cyan UI, same on-disk layout.

Bots are ordinary Python processes. **There is no sandbox.** Anything you upload
runs with the panel's own privileges, so only give accounts to people you trust.

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# edit .env: set FLASK_SECRET_KEY and ADMIN_PASSWORD

python -m flask --app app create-admin     # create your first admin login
python -m flask --app app run --debug
```

Open <http://127.0.0.1:5000>. Register a normal account, or sign in with the
admin you just created.

### Verify it

```bash
python tools/check_templates.py    # Jinja structure + url_for/endpoint check
python tools/smoke_test.py         # full route walk against a throwaway DB
```

`smoke_test.py` exercises registration, the upload/approval gate, cross-account
isolation, path traversal, and a real bot start/stop/restart cycle. Add
`--no-exec` to skip spawning a child process.

---

## Configuration

All settings are environment variables (`.env` is read at startup).

| Variable | Default | Meaning |
|---|---|---|
| `FLASK_SECRET_KEY` | random per boot | Session signing key. **Must be set in production** or every restart logs everyone out. |
| `ADMIN_PASSWORD` | empty | Password for the `/admin` gate. Empty means the gate is disabled for non-admin roles. |
| `DATA_DIR` | `data` | Root for uploads, logs and the SQLite file. |
| `MAX_UPLOAD_MB` | `20` | Upload size cap. |
| `MAX_LOG_MB` | `5` | A bot's log is rotated to `.1` once it passes this size. |
| `MONITOR_INTERVAL` | `3` | Seconds between child-process liveness checks. |
| `COOKIE_SECURE` | `0` | Set to `1` when served over HTTPS. |
| `INHERIT_ENV` | `0` | `1` lets bots inherit the panel's environment instead of a clean one. |

---

## Routes

Public

| Route | Methods | Purpose |
|---|---|---|
| `/ping` | GET | `{"status":"ok"}` |
| `/login` | GET, POST | Fails with `Invalid username or password.` |
| `/register` | GET, POST | Fails with `Username already taken.` |
| `/logout` | GET | Clears the session |

Signed in

| Route | Methods | Purpose |
|---|---|---|
| `/` | GET | Dashboard: counts, bot cards, live console |
| `/upload` | GET, POST | multipart field `file`, `.py` or `.zip` |
| `/my_files` | GET | Per-bot folders, file tree, entry-point badge |
| `/setup/<id>` | GET, POST | POST field `file` selects the entry point |
| `/start/<id>` | POST | `{"ok":true,"pid":2542}` |
| `/stop/<id>` | POST | `{"ok":true}` |
| `/restart/<id>` | POST | `{"ok":true,"pid":2544,"restarts":1}` |
| `/delete/<id>` | POST | `{"ok":true}` |
| `/status/<id>` | GET | `{ok,status,pid,restarts,uptime}` for polling |
| `/logs/<id>` | GET | Log view + stdin form |
| `/logs_raw/<id>` | GET | `text/plain`, polled every 3s |
| `/download_file/<id>/<path>` | GET | Attachment download |
| `/delete_file/<id>/<path>` | POST | Refuses to delete the entry point |
| `/send_input/<id>` | POST | Writes a line to the bot's stdin |

Admin

| Route | Methods | Purpose |
|---|---|---|
| `/admin` | GET | Password gate (`/admin/panel` redirect when already unlocked) |
| `/admin/access` | POST | Field `password` |
| `/admin/panel` | GET | Users + all bots |
| `/admin/lock` | GET | Drops the unlock for this session |
| `/admin/create_user` | POST | `username`, `password`, `role` |
| `/admin/approve_bot/<id>` | POST | Moves a bot out of `pending` |
| `/admin/reject_bot/<id>` | POST | Sets `rejected` and stops it |
| `/admin/stop_bot/<id>` | POST | Force stop |
| `/admin/delete_bot/<id>` | POST | Delete bot + files |
| `/admin/ban_user/<id>` | POST | Toggle ban (stops their bots) |
| `/admin/delete_user/<id>` | POST | Delete user, bots and files |

---

## How a bot runs

1. **Upload** — a `.py` is stored as-is; a `.zip` is validated member by member
   (no `..`, no absolute paths, no `.pth`/`sitecustomize.py`) and extracted
   into `data/uploads/user_<id>/<UID>/`. A zip that wraps everything in one
   folder is flattened.
2. **Entry point** — `main.py`, `bot.py`, `app.py`, `run.py`, `start.py` or
   `__main__.py` is picked automatically; otherwise the single `.py` file is
   used, otherwise the user chooses at `/setup/<id>`.
3. **Approval** — a normal account's bot sits in `pending` until an admin
   approves it. Admins' own bots skip the queue.
4. **Start** — the panel writes `_sc_launcher_<entry>.py` next to the code. The
   launcher loads `.env`, puts the entry's directory on `sys.path` (so sibling
   imports work), and runs the entry with `runpy`. `requirements.txt` is
   installed with `pip` first if present.
5. **Supervision** — the child starts in its own process group. Stop sends
   `SIGTERM` to the group, then `SIGKILL` after 8 s. A background thread polls
   every `MONITOR_INTERVAL` seconds, records the exit code, and flips the row to
   `stopped` if the process died on its own.

Log header written at each start:

```
==================================================
  START  2026-11-25 19:28:35 UTC
  FILE   /opt/render/project/src/data/uploads/user_39/HOST-ZY97S-2026-N9DX/probe_bot.py
  [AUTO-INSTALL enabled]
==================================================
```

### Disk layout

```
data/
├── panel.db
├── logs/
│   └── bot_42.log
└── uploads/
    └── user_39/
        └── HOST-ZY97S-2026-N9DX/
            ├── bot.py
            ├── _sc_launcher_bot.py
            └── requirements.txt
```

Bot ids are `HOST-<5 alnum>-<year>-<4 alnum>`, matching the original panel.

---

## Security model

Deliberate fixes compared with the panel this was modelled on:

- **Ownership on every id route.** `require_bot()` resolves
  `WHERE id = ? AND user_id = ?`. Another account's bot returns `404`, not
  `403`, so ids cannot be probed. The original leaked a recursive listing of
  every user's directory through `/logs/<id>`; that cannot happen here.
- **Path traversal blocked.** Client-supplied paths go through
  `safe_relpath()` and a `realpath` containment check.
- **Zip bombs/layout attacks** rejected before extraction.
- **Open redirects closed.** `request.referrer` is only honoured for this host;
  login's `next` must be a relative path.
- **Admin password** compared with `hmac.compare_digest`.
- **Escalation closed.** `POST /setup/<id>` no longer clears the approval gate.
- Uploaded bots get a minimal environment unless `INHERIT_ENV=1`.

Still true, and you should know it:

- Bots run as the panel user with full filesystem and network access. Use a
  container, a dedicated Unix user, and a resource limit (cgroup / `ulimit`) if
  you host for anyone but yourself.
- The "Security: Safe" badge is a bytecode-free keyword scan, not an audit.
- No CSRF tokens on POST forms. `SameSite=Lax` blocks cross-site form posts but
  is not a substitute for tokens if you expose this publicly.

---

## Deployment

**One process only.** The running-process table lives in memory, so gunicorn
must run a single worker:

```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
```

`Procfile` and `render.yaml` already encode this. Do not scale past one
instance, and do not add `--workers 2`, or half your requests will not see the
bots the other half started. To scale for real, move supervision out of the web
tier (systemd unit per bot, or a supervisor process with a shared control socket).

On Render, mount a disk at `data/` or uploads and logs vanish on redeploy;
`render.yaml` sets this up. Threads are safe for SQLite because every write
goes through a lock and WAL is enabled.

---

## Known gaps vs. the original

- The original's admin page was never observed (the admin password was not
  recovered), so `/admin/panel`'s layout is an approximation; the route names
  and behaviours are matched.
- `/admin/access` with a wrong password redirects to the gate here, whereas the
  original redirected to `/`.
- Rename/mkdir/edit-file endpoints do not exist because none were found on the
  original; only download and delete are implemented.
- ZIP extraction, `requirements.txt` install and `.env` loading follow the
  header the original printed (`[AUTO-INSTALL enabled]`) but were never
  observed end-to-end.
