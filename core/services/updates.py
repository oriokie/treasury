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


def token_diagnosis():
    """What the server is actually using as a token, without revealing it.

    "Bad credentials" tells a treasurer the token is wrong but not WHICH token
    is being used — and the hard part of this failure is exactly that: a new
    token written into .env while a stale one is still exported wins silently,
    because the .env reader defers to the real environment. So this reports the
    shape of the token in force, and says plainly when .env is being overridden.

    Never returns the token. Its kind and length are enough to tell one from
    another, and both are things GitHub itself shows.
    """
    import os
    from pathlib import Path
    from django.conf import settings

    token = getattr(settings, "GITHUB_TOKEN", "") or ""
    if not token:
        return {"set": False, "shape": "", "overridden": False,
                "note": "No token is set. A private repository cannot be "
                        "checked without one."}

    #: what GitHub issues, by prefix: (label, exact length)
    KINDS = {"ghp_": ("classic (ghp_)", 40),
             "github_pat_": ("fine-grained (github_pat_)", 93),
             "gho_": ("OAuth (gho_)", 40)}
    kind, expected = "unrecognised prefix", None
    for prefix, (label, length) in KINDS.items():
        if token.startswith(prefix):
            kind, expected = label, length
            break
    shape = f"{kind}, {len(token)} characters"
    # A token of the wrong LENGTH is not a token GitHub ever issued, and the
    # "Bad credentials" it comes back with looks identical to expiry — which is
    # how somebody ends up issuing a third good token to fix a stray character.
    # One too many usually means a quote, comma or zero-width space came along
    # with the paste; one too few means it was clipped.
    wrong_length = expected is not None and len(token) != expected
    if wrong_length:
        off = len(token) - expected
        shape += (f" — expected {expected}"
                  f" ({abs(off)} too {'many' if off > 0 else 'few'})")

    # Is a stale export beating the .env file? The loader uses setdefault, so
    # the environment wins — which is right, and invisible at the moment it
    # matters most.
    overridden = False
    try:
        env_path = Path(settings.BASE_DIR) / ".env"
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() != "GITHUB_TOKEN":
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1].strip()
            overridden = bool(val) and val != token
            break
    except OSError:
        pass

    note = ""
    if wrong_length:
        off = len(token) - expected
        note = (f"This token is {abs(off)} character"
                f"{'s' if abs(off) != 1 else ''} too "
                f"{'long' if off > 0 else 'short'} for its kind, so GitHub "
                f"cannot recognise it whatever its permissions are. "
                + ("Something almost certainly came along with the paste — a "
                   "quote, a comma, or an invisible character copied from a web "
                   "page. Re-copy it from GitHub and paste it into .env with no "
                   "quotes around it."
                   if off > 0 else
                   "It looks clipped — check the whole value made it into "
                   ".env on one line.")
                + " Issuing another token will not help until this is right.")
    elif overridden:
        note = ("The token in .env is NOT the one being used — a GITHUB_TOKEN "
                "in the server's environment is overriding it. Clear that "
                "export (or update it) and restart the app.")
    elif kind == "unrecognised prefix":
        note = ("This does not look like a GitHub token. Tokens begin ghp_, "
                "github_pat_ or gho_.")
    return {"set": True, "shape": shape, "overridden": overridden, "note": note}


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
    # Why the last attempt failed, kept so the update page can say something a
    # treasurer can act on. Both branches below used to swallow the exception
    # entirely, so a bad token, a renamed repo, a rate limit and a dead network
    # all produced the same sentence — one that guessed "set GITHUB_TOKEN" even
    # when a token was set and being rejected. The answer was always in the
    # response; nothing was looking at it.
    reason = ""
    # 1) a published GitHub Release, if any
    try:
        data = _fetch_json(f"https://api.github.com/repos/{repo}/releases/latest", token)
        if data.get("tag_name"):
            result = {"tag": data["tag_name"], "url": data.get("html_url", ""),
                      "body": (data.get("body") or "")[:2000]}
    except Exception as exc:  # noqa: BLE001
        result = None
        reason = _explain_failure(exc, repo, token)
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
                reason = ""          # tags answered; the release 404 was normal
            elif not reason:
                reason = ("Connected, but the repository has no tags or releases "
                          "yet — nothing has been published to update to.")
        except Exception as exc:  # noqa: BLE001
            result = None
            reason = _explain_failure(exc, repo, token)

    _release_cache["at"] = now
    _release_cache["value"] = result
    _release_cache["reason"] = reason
    return result


def last_failure_reason():
    """Why the most recent lookup found nothing, in words a treasurer can act
    on. Empty when the last lookup succeeded."""
    return _release_cache.get("reason", "")


def _explain_failure(exc, repo, token):
    """Turn an API failure into the one sentence that says what to do next.

    GitHub answers 404 rather than 403 for a private repository the caller
    cannot see, so "not found" and "not allowed" are the same status and have
    to be told apart by whether a token was sent. That distinction is the whole
    difference between "check the name" and "check the token".
    """
    import urllib.error

    status = getattr(exc, "code", None)
    detail = ""
    try:
        if isinstance(exc, urllib.error.HTTPError):
            detail = (json.loads(exc.read().decode() or "{}").get("message") or "")
    except Exception:  # noqa: BLE001
        detail = ""

    if status == 401:
        base = (f"GitHub rejected the access token"
                f"{': ' + detail if detail else ''}. ")
        if token.startswith("github_pat_"):
            # Fine-grained tokens ALWAYS carry an expiry — 30 days by default,
            # a year at most — so on one of these, "expired" is not one
            # possibility among three, it is the likely one. Saying so saves a
            # treasurer checking scopes and repository access that were never
            # the problem.
            return (base + "This is a fine-grained token, and those always "
                    "expire — 30 days by default. Check its expiry date on "
                    "GitHub first; if it has lapsed, issue a new one and set "
                    "GITHUB_TOKEN in the server's .env. Scopes and repository "
                    "access are not the cause — a token GitHub does not "
                    "recognise never gets as far as being checked against them.")
        return (base + "It is invalid, expired, or was revoked — issue a new "
                "one and set GITHUB_TOKEN in the server's .env.")
    if status == 404:
        if not token:
            return (f"GitHub says '{repo}' does not exist. If it is PRIVATE, that "
                    f"is also what it says to a caller with no token — set "
                    f"GITHUB_TOKEN in the server's .env. Otherwise check the "
                    f"repository name (owner/name).")
        return (f"The token cannot see '{repo}'. GitHub answers 'not found' for a "
                f"private repository the token has no access to, so either the "
                f"name is wrong or the token lacks repository read access — a "
                f"classic token needs the 'repo' scope, a fine-grained one needs "
                f"Contents: Read on this repository.")
    if status == 403:
        return (f"GitHub refused the request{': ' + detail if detail else ''}. "
                f"This is usually the hourly rate limit — an authenticated token "
                f"raises it considerably — or a token blocked by an organisation "
                f"policy.")
    if status:
        return (f"GitHub returned HTTP {status}"
                f"{': ' + detail if detail else ''}. This is not a setting on "
                f"this server — try the check again shortly, and if it persists "
                f"see GitHub's status page.")
    return (f"Could not reach GitHub ({type(exc).__name__}). The server may have "
            f"no outbound internet access, or DNS is failing.")


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
