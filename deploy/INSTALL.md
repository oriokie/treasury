# Automated deployment — `deploy/install.sh`

An interactive installer that sets up the whole stack on a cPanel/WHM server:
collects settings (with validation), then configures the database, Python
environment, gunicorn service, Apache proxy, nginx and SSL.

## Run it

From the app directory, as root (needed for Apache/nginx/systemd):

```bash
sudo bash deploy/install.sh
```

If `whiptail` or `dialog` is installed you get graphical box prompts; otherwise
it falls back to plain text prompts. Either way it works.

## What it asks for (with validation)

| Prompt | Validated as | Example |
|---|---|---|
| Public domain | hostname | `treasury.yourchurch.org` |
| cPanel user (owns the domain) | username | `tutoress` |
| App user (owns the files) | username | `oriokie` |
| Church name / Site name | non-empty | `SDA Church Kahawa` |
| MySQL database / user | identifier | `tutoress_treasury` |
| MySQL password | hidden, non-empty | — |
| MySQL host / port | non-empty / 1–65535 | `localhost` / `3306` |
| Gunicorn bind | non-empty | `127.0.0.1:8000` |
| Apache HTTP / HTTPS ports | 1–65535 | `81` / `444` |
| GitHub repo (optional) | `owner/repo` | `oriokie/treasury` |
| GitHub token (if private) | hidden | — |
| SSL admin email (optional) | email | — |

The Django secret key is generated automatically (and **reused** on re-runs so
existing logins/sessions aren't invalidated).

## What it does, step by step

1. **Collect settings** — pre-fills from an existing `.env` if present, shows a
   review screen (secrets masked) before writing anything.
2. **Write `.env`** — backs up any existing one, writes with `600` permissions.
3. **Database** — creates/repairs the MySQL DB as `utf8mb4_unicode_ci` and grants
   the user (root only; otherwise tells you to make it in cPanel).
4. **Python** — creates `.venv`, installs `requirements.txt`, ensures PyMySQL,
   runs `migrate`, `collectstatic`, and optionally `createsuperuser`.
5. **systemd** — installs and enables a `treasury` gunicorn service (boot + auto
   restart), reading the `.env` via `EnvironmentFile`.
6. **Apache** — writes the proxy include under the **domain-owning cPanel user**
   (the `tutoress` gotcha), excludes `/.well-known` for ACME, rebuilds httpd and
   verifies the proxy compiled in.
7. **nginx + SSL** — rebuilds cPanel nginx, and can trigger AutoSSL.
8. **Verify** — curls `/healthz/` on gunicorn, on Apache's HTTPS port, and on the
   public URL, reporting each.

Every step asks before acting and is safe to re-run. Steps that need root are
skipped with a clear message if you run without it.

## After it finishes

- App: `https://<domain>/` — health check at `/healthz/`
- Restart: `systemctl restart treasury`
- Logs: `journalctl -u treasury -n 100 --no-pager`
- Import legacy data: `python manage.py import_legacy --phase all` (files in `data/`)
- Update later: `./update.sh && systemctl restart treasury`

If any step didn't succeed, `deploy/RUNBOOK_kws.oriokie.com.md` has the manual
commands and a per-component troubleshooting cheat-sheet.
