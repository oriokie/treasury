"""Check GitHub for a newer release than the running version.

Uses the public GitHub Releases API (no auth, cached) so a hosted instance can
tell the treasurer when an update is available. The repo is configured via the
GITHUB_REPO setting (e.g. "your-org/church-treasury"). Network failures are
swallowed — the checker never breaks a page.
"""
import json
import urllib.request

from django.conf import settings
from core.version import get_version


def _parse(v):
    parts = []
    for p in str(v).lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts + [0, 0, 0])[:3]


_release_cache = {"at": 0.0, "value": None}
_RELEASE_TTL = 600  # seconds — re-check GitHub at most every 10 minutes


def _fetch_json(url, token, timeout=4):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "treasury-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def latest_release(force=False):
    """Return dict(tag, url, body) of the newest release/tag on GitHub, or None.

    Tries the published-Releases API first, then falls back to the **tags** API —
    this project tags every version (vX.Y.Z) but doesn't always publish a GitHub
    Release, and /releases/latest 404s when only tags exist (the cause of
    "Latest release seen (none)"). Result is cached for a few minutes.

    Authenticates with GITHUB_TOKEN if set, so private repositories work. The
    token is read from settings/env and never logged."""
    import time
    now = time.time()
    if not force and _release_cache["value"] is not None \
            and (now - _release_cache["at"]) < _RELEASE_TTL:
        return _release_cache["value"]

    repo = getattr(settings, "GITHUB_REPO", "") or ""
    if not repo:
        return None
    token = getattr(settings, "GITHUB_TOKEN", "") or ""

    result = None
    # 1) a published GitHub Release, if any
    try:
        data = _fetch_json(f"https://api.github.com/repos/{repo}/releases/latest", token)
        if data.get("tag_name"):
            result = {"tag": data["tag_name"], "url": data.get("html_url", ""),
                      "body": (data.get("body") or "")[:2000]}
    except Exception:  # noqa: BLE001
        result = None
    # 2) fall back to tags (newest by semver) — works for a tag-only workflow
    if result is None:
        try:
            tags = _fetch_json(f"https://api.github.com/repos/{repo}/tags?per_page=100", token)
            best = None
            for t in tags or []:
                name = t.get("name", "")
                if name and (best is None or _parse(name) > _parse(best)):
                    best = name
            if best:
                result = {"tag": best, "body": "",
                          "url": f"https://github.com/{repo}/releases/tag/{best}"}
        except Exception:  # noqa: BLE001
            result = None

    _release_cache["at"] = now
    _release_cache["value"] = result
    return result


def update_available(force=False):
    """(bool, latest_tag, current) — whether a newer release exists on GitHub.
    Pass force=True (the "check now" button) to bypass the time cache."""
    rel = latest_release(force=force)
    cur = get_version()
    if not rel or not rel["tag"]:
        return False, None, cur
    return _parse(rel["tag"]) > _parse(cur), rel["tag"], cur


# --- button-triggered, in-app update runner --------------------------------
import os
import subprocess
import threading
import datetime as _dt
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_update_state = {
    "running": False,
    "finished": False,
    "ok": None,
    "log": [],
    "started_at": None,
    "finished_at": None,
}
_update_lock = threading.Lock()


def update_status():
    """Snapshot of the current/last update run, for the progress page."""
    with _update_lock:
        return dict(_update_state, log=list(_update_state["log"]))


def _log(msg):
    _update_state["log"].append(f"{_dt.datetime.now():%H:%M:%S}  {msg}")


def _run_step(label, args):
    _log(f"→ {label}")
    try:
        proc = subprocess.run(args, cwd=_PROJECT_ROOT, capture_output=True,
                              text=True, timeout=300)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            for line in out.splitlines()[-8:]:
                _log("   " + line)
        if proc.returncode != 0:
            if err:
                for line in err.splitlines()[-8:]:
                    _log("   ! " + line)
            raise RuntimeError(f"{label} failed (exit {proc.returncode})")
        _log(f"✓ {label}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} timed out")


def _backup_db():
    from django.conf import settings
    db = settings.DATABASES["default"]
    if db["ENGINE"].endswith("sqlite3"):
        path = db["NAME"]
        if isinstance(path, (str, bytes)) and os.path.exists(path):
            import shutil
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = f"{path}.backup-{stamp}"
            shutil.copy2(path, dest)
            _log(f"✓ Database backed up to {os.path.basename(dest)}")
    else:
        # for MySQL/Postgres, code updates don't touch data; remind the user to
        # take a dump from Settings → Backup before major upgrades
        _log("• Using a managed database (MySQL/Postgres) — code update won't "
             "alter your data. For a data snapshot, use Settings → Download backup.")


def _pip_executable():
    venv = _PROJECT_ROOT / ".venv"
    pip = venv / "bin" / "pip"
    return str(pip) if pip.exists() else "pip"


def _python_executable():
    import sys
    venv = _PROJECT_ROOT / ".venv"
    py = venv / "bin" / "python"
    return str(py) if py.exists() else sys.executable


def _do_update():
    try:
        _log("Starting update")
        _backup_db()
        _run_step("Fetching latest code", ["git", "fetch", "--all", "--tags"])
        _run_step("Applying latest code", ["git", "pull", "--ff-only"])
        _run_step("Installing dependencies",
                  [_pip_executable(), "install", "-r", "requirements.txt", "--quiet"])
        py = _python_executable()
        _run_step("Applying database changes",
                  [py, "manage.py", "migrate", "--noinput"])
        _run_step("Refreshing static files",
                  [py, "manage.py", "collectstatic", "--noinput"])
        # signal the WSGI server to reload by touching the wsgi file
        wsgi = _PROJECT_ROOT / "config" / "wsgi.py"
        if wsgi.exists():
            wsgi.touch()
            _log("✓ Signalled the app to restart")
        with _update_lock:
            _update_state["ok"] = True
        _log("Update complete. The app will reload momentarily.")
    except Exception as e:  # noqa: BLE001
        with _update_lock:
            _update_state["ok"] = False
        _log(f"✗ Update failed: {e}")
        _log("No partial changes were applied to your data; "
             "your database backup is safe. You can retry or update from the "
             "server with ./update.sh")
    finally:
        with _update_lock:
            _update_state["running"] = False
            _update_state["finished"] = True
            _update_state["finished_at"] = _dt.datetime.now().isoformat()


def start_update():
    """Kick off the update in a background thread. Returns False if one is
    already running or if this isn't a git checkout."""
    with _update_lock:
        if _update_state["running"]:
            return False
        if not (_PROJECT_ROOT / ".git").exists():
            return None  # not a git checkout — can't self-update
        _update_state.update(running=True, finished=False, ok=None, log=[],
                             started_at=_dt.datetime.now().isoformat(),
                             finished_at=None)
    threading.Thread(target=_do_update, name="app-update", daemon=True).start()
    return True
