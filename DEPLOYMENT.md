# Deployment & Updates

This guide covers hosting the Church Treasury app and keeping it up to date.

## Putting the code on GitHub (one time)

From the project folder:

```bash
git init
git add .
git commit -m "Initial commit: Church Treasury v1.0.0"
git branch -M main
git remote add origin https://github.com/<your-org>/<your-repo>.git
git push -u origin main
```

The `.gitignore` already excludes the database, the virtual environment, real
financial data files, and secrets (`.env`). Never commit `.env` or `db.sqlite3`.

Tag each release so the in-app update checker can see it:

```bash
git tag v1.0.0
git push origin v1.0.0
```

…and create a matching Release on GitHub (Releases → Draft a new release →
choose the tag). The app's "update available" banner compares the running
`VERSION` against the latest GitHub release tag.

## Hosting (first deploy)

1. Clone the repo onto the server and create a virtual environment:
   ```bash
   git clone https://github.com/<your-org>/<your-repo>.git treasury
   cd treasury
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Configure environment: `cp .env.example .env`, then edit `.env` and set a
   strong `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, your `DJANGO_ALLOWED_HOSTS`,
   `DJANGO_CSRF_TRUSTED_ORIGINS`, and (optionally) `GITHUB_REPO` for the update
   checker. Load it before running, e.g. `export $(grep -v '^#' .env | xargs)`.
3. Set up the database and an admin user:
   ```bash
   python manage.py migrate
   python manage.py createsuperuser
   python manage.py collectstatic --noinput
   ```
4. Serve with a production WSGI server (gunicorn) behind nginx, or a platform
   like Railway/Render/Fly. Example:
   ```bash
   gunicorn config.wsgi --bind 0.0.0.0:8000
   ```

## Updating a running instance

Whenever a new version is released, the treasurer sees an **"Update available"**
banner. To apply it, run on the server:

```bash
./update.sh
```

This backs up the database, pulls the latest code, installs any new
dependencies, applies migrations, refreshes static files, and tells you to
restart the web server. It is safe to re-run.

## Cutting a new release (maintainers)

**Patch releases are automatic.** Every push to `main` runs the Auto-release
workflow: if `VERSION` still matches the newest tag, it bumps the patch
(e.g. 3.48.0 → 3.48.1), tags it, and publishes a GitHub Release so the in-app
updater can see it. You do not need to remember `manage.py release` for ordinary
merges — that forgotten step is how 3.48.0 sat on main with no tag while every
hosted instance kept reporting "already on the latest".

**Minor / major releases stay manual** (they need a real "What's new" paragraph):

1. Bump `VERSION` and add an entry to `core.version.WHATS_NEW`.
2. Merge to `main`. Auto-release will tag what you wrote (it will not bump
   again when `VERSION` is already ahead of the newest tag).

You can still cut a release by hand:

```bash
python manage.py release --check
python manage.py release --push
```

Hosted instances then show the update banner and can update with `./update.sh`.

## Production checklist

This app is production-ready with the following in place:

- **Static files**: served by WhiteNoise straight from the app process
  (compressed + fingerprinted), so no separate static server is required. Run
  `python manage.py collectstatic --noinput` on deploy.
- **WSGI server**: gunicorn, configured in `gunicorn.conf.py`. Start with
  `gunicorn -c gunicorn.conf.py config.wsgi`, or use the `Procfile`.
- **Security**: with `DJANGO_DEBUG=False` the app enforces HTTPS redirect,
  secure + HTTP-only cookies, HSTS, content-type nosniff, `X-Frame-Options:
  DENY`, and a same-origin referrer policy. `python manage.py check --deploy`
  passes with no warnings.
- **Brute-force protection**: django-axes locks an account/IP after repeated
  failed logins.
- **Secrets at rest**: sensitive settings (bot tokens, API keys) are encrypted
  with Fernet.
- **Health check**: `GET /healthz/` returns 200 with `{status, database,
  version}` for uptime monitors and platform readiness probes.
- **Logging**: to stdout/stderr at `DJANGO_LOG_LEVEL` (default INFO), captured
  by your process manager.

### Recommended: PostgreSQL for production

SQLite is fine for a single church with light concurrency. For heavier use,
set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, and
uncomment `psycopg[binary]` in `requirements.txt`. Then `migrate`.

### A note on workers

The default is **one** gunicorn worker. This suits SQLite (single writer) and
the in-app Telegram poller (which should run in exactly one process). If you
move to PostgreSQL and raise the worker count, either run Telegram via the
webhook instead of the in-app poller, or keep the poller on a single dedicated
process.

## Updating from inside the app (button)

Treasurers can update without server access: **Settings → About & updates →
Check for & install updates → Update now**. The app backs up the database,
pulls the new code, applies migrations, refreshes static files, and reloads
itself — showing live progress. This requires the deployment to be a git
checkout that can reach GitHub. Server admins can still use `./update.sh`.

### systemd / nginx examples

See `deploy/treasury.service.example` (systemd unit) and
`deploy/nginx.conf.example` (TLS reverse proxy). WhiteNoise means nginx is
optional — it's only needed if you want nginx to terminate TLS.
