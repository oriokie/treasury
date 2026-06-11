# Deploying to WHM/cPanel at kws.oriokie.com (root access)

Two paths. **Option B (systemd + gunicorn) is recommended** for this app because
it runs as one persistent process — which SQLite, the in-app Telegram poller,
and the in-app update button all require. Option A (cPanel's Python App tool)
is included for completeness.

Throughout, replace `oriokie` with your actual cPanel username if different,
and adjust paths to match.

---

## 0. One-time: create the subdomain in cPanel

1. cPanel → **Domains** (or **Subdomains**) → create `kws.oriokie.com`.
2. Note its document root, e.g. `/home/oriokie/kws.oriokie.com`.
   You will NOT serve the app from there directly — it's just where the
   subdomain points. We'll proxy to gunicorn instead.

---

## Option B — systemd + gunicorn + Apache reverse proxy (recommended)

### 1. Put the code on the server

As root (or the cPanel user), via SSH:

```bash
# pick a home for the app (kept out of the web root on purpose)
mkdir -p /home/oriokie/apps
cd /home/oriokie/apps
git clone https://github.com/<your-org>/<your-repo>.git treasury
cd treasury
```

### 2. Python environment

cPanel servers usually expose modern Python via `ea-python` or
`/opt/alt/python312`. Check what's available:

```bash
python3.12 --version || ls /opt/alt/ | grep python
```

Create the virtual environment (use the 3.12 binary you found):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn whitenoise          # if not already pulled in
```

### 3. Configure the environment

```bash
cp .env.example .env
nano .env
```

Set at least:

```
DJANGO_SECRET_KEY=<paste a long random string>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=kws.oriokie.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://kws.oriokie.com
SITE_NAME=Church Treasury
CHURCH_NAME=<your church>
GITHUB_REPO=<your-org>/<your-repo>
```

Generate the secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

### 4. Initialise the app

```bash
export $(grep -v '^#' .env | xargs)
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

(Optional: load your historical data with the legacy importer here.)

### 5. Run gunicorn as a systemd service

Copy and edit the example unit shipped with the app:

```bash
cp deploy/treasury.service.example /etc/systemd/system/treasury.service
nano /etc/systemd/system/treasury.service
```

Set the paths/user to match — for this setup:

```ini
[Service]
User=oriokie
WorkingDirectory=/home/oriokie/apps/treasury
EnvironmentFile=/home/oriokie/apps/treasury/.env
ExecStart=/home/oriokie/apps/treasury/.venv/bin/gunicorn -c gunicorn.conf.py config.wsgi
```

Bind gunicorn to localhost only (Apache will be the public face). The shipped
`gunicorn.conf.py` defaults to `0.0.0.0:8000`; override it in `.env`:

```
GUNICORN_BIND=127.0.0.1:8599
```

Then start it:

```bash
systemctl daemon-reload
systemctl enable --now treasury
systemctl status treasury          # should be "active (running)"
curl -s http://127.0.0.1:8599/healthz/   # should return {"status":"ok",...}
```

### 6. Point the subdomain at gunicorn (Apache reverse proxy)

On WHM/cPanel, Apache owns port 80/443 for the subdomain. Add a proxy via an
**Apache include** so cPanel doesn't overwrite it on rebuild:

```bash
mkdir -p /etc/apache2/conf.d/userdata/ssl/2_4/oriokie/kws.oriokie.com
cat > /etc/apache2/conf.d/userdata/ssl/2_4/oriokie/kws.oriokie.com/proxy.conf <<'EOF'
ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto "https"
ProxyPass / http://127.0.0.1:8599/
ProxyPassReverse / http://127.0.0.1:8599/
EOF

# repeat for non-SSL std/ (http) so the http->https redirect works:
mkdir -p /etc/apache2/conf.d/userdata/std/2_4/oriokie/kws.oriokie.com
cat > /etc/apache2/conf.d/userdata/std/2_4/oriokie/kws.oriokie.com/proxy.conf <<'EOF'
ProxyPreserveHost On
ProxyPass / http://127.0.0.1:8599/
ProxyPassReverse / http://127.0.0.1:8599/
EOF

# rebuild Apache config and restart
/usr/local/cpanel/scripts/rebuildhttpdconf
systemctl restart httpd
```

(Path uses your cPanel username `oriokie` and the subdomain. The `2_4` is for
Apache 2.4, standard on modern cPanel.)

### 7. TLS certificate

In cPanel → **SSL/TLS Status**, run **AutoSSL** for `kws.oriokie.com` (or use
WHM → Manage AutoSSL). Once issued, the `X-Forwarded-Proto https` header above
lets Django enforce HTTPS correctly.

### 8. Done

Visit `https://kws.oriokie.com`. Log in with the superuser you created.

### Updating later
From the app: **Settings → About & updates → Update now** (it backs up the DB,
pulls code, migrates, and reloads). Because the systemd service watches
`config/wsgi.py`, the in-app updater's reload works. If you prefer the shell:

```bash
cd /home/oriokie/apps/treasury && ./update.sh && systemctl restart treasury
```

---

## Option A — cPanel "Setup Python App" (Passenger)

Use this if you'd rather not manage a systemd service.

1. cPanel → **Setup Python App** → **Create Application**:
   - Python version: 3.12 (or the highest available)
   - Application root: `apps/treasury` (relative to home)
   - Application URL: `kws.oriokie.com`
   - Application startup file: `passenger_wsgi.py`
   - Application Entry point: `application`
2. cPanel creates a virtualenv and shows the `source ...activate` command. SSH
   in, run it, then `pip install -r requirements.txt`.
3. Create `passenger_wsgi.py` in the app root:
   ```python
   from config.wsgi import application
   ```
4. Add your environment variables in the cPanel Python App UI (the same keys as
   the `.env` above), then run `migrate`, `collectstatic`, and `createsuperuser`
   from the SSH session with the virtualenv active.
5. Restart the app from the cPanel UI.

Caveats with Passenger:
- The in-app **Telegram poller** should be turned OFF (Passenger recycles
  workers); use the webhook instead if you want Telegram.
- The in-app **update button** touches `config/wsgi.py`; with Passenger you may
  need to `touch tmp/restart.txt` instead — simplest is to update via
  `./update.sh` over SSH and restart from the cPanel UI.
- SQLite is fine, but make sure the app directory is writable by the app user.

---

## Notes for either option

- **Database**: SQLite works out of the box and is fine for one church. For
  heavier use, create a PostgreSQL DB in cPanel and set `POSTGRES_*` in `.env`
  (uncomment `psycopg[binary]` in requirements), then `migrate`.
- **File uploads**: bank statements can be a few MB; the nginx example caps at
  25M — Apache's default is usually fine, but if uploads fail, raise
  `LimitRequestBody`.
- **Backups**: Settings → About & updates → Download full backup. Also keep
  cPanel's own account backups on.
- **Firewall**: gunicorn binds to 127.0.0.1 only, so it's not exposed publicly;
  only Apache (443) faces the internet.

---

## IMPORTANT: Database engine on cPanel — use MySQL

Many cPanel/WHM servers ship an old system SQLite (e.g. 3.26) that Django 5.2
rejects (it needs SQLite ≥ 3.31). You'll see:

```
django.db.utils.NotSupportedError: SQLite 3.31 or later is required (found 3.26.0).
```

The clean fix on cPanel is to use **MySQL/MariaDB**, which cPanel manages natively.

### 1. Create the database and user in cPanel

cPanel → **MySQL® Databases**:
1. Create a database, e.g. `oriokie_treasury`.
2. Create a user, e.g. `oriokie_trez`, with a strong password.
3. Add the user to the database and grant **ALL PRIVILEGES**.

(cPanel prefixes both with your account name automatically.)

### 2. Install the MySQL driver in the venv

```bash
cd /home/oriokie/apps/treasury
source .venv/bin/activate
# Easiest (pure-Python, no compiler needed) — the app auto-detects it:
pip install PyMySQL

# Faster alternative (C extension). Needs build tools + headers first:
#   yum install -y python3.12-devel mariadb-devel gcc    # (or the ea-* packages)
#   pip install mysqlclient
```

### 3. Point the app at MySQL in .env

```
MYSQL_DB=oriokie_treasury
MYSQL_USER=oriokie_trez
MYSQL_PASSWORD=<the password you set>
MYSQL_HOST=localhost
MYSQL_PORT=3306
```

(Leave the POSTGRES_* and SQLite settings unset — the app picks MySQL when
`MYSQL_DB` is present.)

### 4. Migrate onto MySQL

```bash
export $(grep -v '^#' .env | xargs)
python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic --noinput
```

Then start/restart the service as in Option B.

### Backups with MySQL

Settings → About & updates → **Download full backup** now produces a `.sql`
dump (via `mysqldump`) instead of a SQLite file. The multi-sheet **Excel export**
works regardless of engine. You can also use cPanel's own backups.

---

## Alternative: keep SQLite by bundling a newer one

If you'd rather not use MySQL, install a modern SQLite into Python:

```bash
source .venv/bin/activate
pip install pysqlite3-binary
```

Then add to the very top of `config/settings.py` (before Django loads):

```python
__import__("pysqlite3")
import sys
sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
```

This makes Python use the bundled SQLite (3.40+) instead of the system's 3.26.
MySQL is still the recommended path on a cPanel host, but this works if you
prefer the single-file database.
