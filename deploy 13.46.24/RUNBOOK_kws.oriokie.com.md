# Runbook — Church Treasury at kws.oriokie.com

The actual, working installation as deployed. Server: cPanel/WHM (AlmaLinux 8)
with nginx in front of Apache. App owner dir: `/home/oriokie/apps/treasury`.
cPanel account that OWNS the domain: **tutoress** (not "oriokie" — this matters
for the Apache include path).

Request path: browser → nginx (443) → Apache (444) → gunicorn (127.0.0.1:8000) → Django.

---

## Key facts for this box

| Thing | Value |
|---|---|
| App directory | `/home/oriokie/apps/treasury` |
| Python venv | `/home/oriokie/apps/treasury/.venv` (Python 3.12) |
| Database | MySQL (cPanel-managed), driver = **PyMySQL** (pure-Python) |
| gunicorn bind | `127.0.0.1:8000` |
| Apache ports | HTTP 81, HTTPS **444** (nginx owns 80/443) |
| cPanel user (domain owner) | **tutoress** |
| Apache proxy include | `/etc/apache2/conf.d/userdata/{ssl,std}/2_4/tutoress/kws.oriokie.com/treasury.conf` |

---

## 1. Code + environment

```bash
cd /home/oriokie/apps/treasury
source .venv/bin/activate
```

`.env` (the app auto-loads this — no `export` needed):

```
DJANGO_SECRET_KEY=<long random string>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=kws.oriokie.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://kws.oriokie.com
SITE_NAME=Church Treasury
CHURCH_NAME=<church>
GITHUB_REPO=<org>/<repo>

MYSQL_DB=tutoress_treasury
MYSQL_USER=tutoress_trez
MYSQL_PASSWORD=<password>
MYSQL_HOST=localhost
MYSQL_PORT=3306

GUNICORN_BIND=127.0.0.1:8000
```

Driver (one-time): `pip install PyMySQL`  (NOT mysqlclient — no compiler/headers here)

## 2. Database + static + admin (one-time)

```bash
python manage.py migrate
python manage.py createsuperuser        # MUST exist before importing legacy data
python manage.py collectstatic --noinput
```

## 3. Import legacy data (one-time)

Upload the 7 Excel files to `data/` first
(`REPORTING_SHEET_*.xlsx` + `JUNE.xlsx`), then:

```bash
python manage.py import_legacy --dry-run --noinput   # preview
python manage.py import_legacy --noinput             # real
```

## 4. Run gunicorn

Currently started manually. To run in the foreground for a quick check:

```bash
gunicorn -c gunicorn.conf.py config.wsgi
```

Local health check (use the right Host header — ALLOWED_HOSTS rejects others):

```bash
curl -i -H "Host: kws.oriokie.com" http://127.0.0.1:8000/healthz/
# -> {"status":"ok","database":true,"version":"..."}
```

### Make it permanent (recommended — survives reboots)

```bash
pkill -f "gunicorn -c gunicorn.conf.py"          # stop the manual one
cp deploy/treasury.service.example /etc/systemd/system/treasury.service
nano /etc/systemd/system/treasury.service
```

Set:
```ini
[Service]
User=oriokie
WorkingDirectory=/home/oriokie/apps/treasury
EnvironmentFile=/home/oriokie/apps/treasury/.env
ExecStart=/home/oriokie/apps/treasury/.venv/bin/gunicorn -c gunicorn.conf.py config.wsgi
```
```bash
systemctl daemon-reload
systemctl enable --now treasury
systemctl status treasury
```

## 5. Apache reverse proxy (THE part that was tricky)

The include MUST live under the **domain-owning cPanel user = tutoress**, or
cPanel silently ignores it.

```bash
# SSL (444) and HTTP (81) includes
mkdir -p /etc/apache2/conf.d/userdata/ssl/2_4/tutoress/kws.oriokie.com
mkdir -p /etc/apache2/conf.d/userdata/std/2_4/tutoress/kws.oriokie.com

cat > /etc/apache2/conf.d/userdata/ssl/2_4/tutoress/kws.oriokie.com/treasury.conf <<'EOF'
ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto "https"
ProxyPass /.well-known !
ProxyPass / http://127.0.0.1:8000/
ProxyPassReverse / http://127.0.0.1:8000/
EOF

cat > /etc/apache2/conf.d/userdata/std/2_4/tutoress/kws.oriokie.com/treasury.conf <<'EOF'
ProxyPreserveHost On
ProxyPass /.well-known !
ProxyPass / http://127.0.0.1:8000/
ProxyPassReverse / http://127.0.0.1:8000/
EOF

# register the includes for the user, then rebuild + restart
/usr/local/cpanel/scripts/ensure_vhost_includes --user=tutoress --verbose
/usr/local/cpanel/scripts/rebuildhttpdconf
systemctl restart httpd

# verify the proxy actually compiled in (MUST print the ProxyPass lines)
grep -n "127.0.0.1:8000" /etc/apache2/conf/httpd.conf
```

Verify Apache directly on its real SSL port (444, behind nginx):

```bash
curl -ik -H "Host: kws.oriokie.com" https://127.0.0.1:444/healthz/   # -> ok
```

## 6. nginx pass-through + TLS

nginx (cPanel Engine X) forwards to Apache automatically once the vhost is
rebuilt:

```bash
/usr/local/cpanel/scripts/ea-nginx config --all
systemctl reload nginx
curl -ik https://kws.oriokie.com/healthz/        # -> ok (public)
```

TLS cert: WHM/cPanel → **SSL/TLS Status** → run **AutoSSL** for kws.oriokie.com.
(The `/.well-known` proxy exclusion lets ACME validation reach Apache.)

---

## Day-to-day operations

**Restart the app** (after config/code change):
```bash
systemctl restart treasury        # if using systemd
# or, if running manually: pkill -f gunicorn ; then start it again
```

**View app logs:**
```bash
journalctl -u treasury -n 100 --no-pager      # systemd
```

**Backups:** in-app → Settings → About & updates → Download full backup
(produces a MySQL .sql dump) + Export all data (Excel). Plus cPanel account backups.

**Updates:** in-app → Settings → About & updates → Check for & install updates →
Update now. Or over SSH:
```bash
cd /home/oriokie/apps/treasury && ./update.sh && systemctl restart treasury
```

---

## Troubleshooting cheat-sheet

| Symptom | Check |
|---|---|
| 404 from public URL, `Server: Apache` | proxy include not loaded → step 5 `grep`, re-run `ensure_vhost_includes --user=tutoress` |
| 404, `Server: nginx` | nginx not forwarding → `ea-nginx config --all` + reload |
| 502 Bad Gateway | gunicorn down → `systemctl status treasury` / `ss -tlnp \| grep 8000` |
| 400 Bad Request locally | normal — curl sent wrong Host; add `-H "Host: kws.oriokie.com"` |
| 500 from app | `journalctl -u treasury -n 50` |
| Apache include ignored | wrong cPanel user in path — must be **tutoress**, not oriokie |
| SQLite version error | `.env` MYSQL_* not loaded — confirm `echo $MYSQL_DB` or that .env has them |
