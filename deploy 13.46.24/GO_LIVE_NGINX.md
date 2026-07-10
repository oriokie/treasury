# Go-live: cPanel + nginx-in-front (CORRECTED for your server)

KEY FACT discovered from `httpd -S`: Apache is NOT on 80/443. nginx owns those.
Apache listens on **81 (HTTP)** and **444 (HTTPS)** behind nginx. So "test
Apache directly" means hitting 444, and the proxy must be wired through nginx.

## A. Verify the Apache proxy include actually loaded

```bash
# Does the include file exist?
ls -la /etc/apache2/conf.d/userdata/ssl/2_4/oriokie/kws.oriokie.com/

# Did it get compiled into the live config?
grep -n "127.0.0.1:8000" /etc/apache2/conf/httpd.conf
```

If grep shows nothing, rebuild and restart:

```bash
/usr/local/cpanel/scripts/rebuildhttpdconf
systemctl restart httpd
grep -n "127.0.0.1:8000" /etc/apache2/conf/httpd.conf   # now should appear
```

Test Apache directly on its REAL port (444), bypassing nginx:

```bash
curl -ik -H "Host: kws.oriokie.com" https://127.0.0.1:444/healthz/
```

Expect {"status":"ok",...}. If you get that, Apache is proxying correctly and
the only remaining issue is nginx (section B).

## B. Make nginx pass the subdomain through to Apache

cPanel's nginx tries to serve files from the (empty) docroot first, so it 404s
before reaching Apache. Two ways to fix:

### Option 1 — WHM UI (easiest)
WHM → **nginx Manager** → find `kws.oriokie.com` → set caching to
**"Standardize"/off** or enable **"Proxy to Apache"** for it (wording varies by
version) → **Rebuild** + reload. This makes nginx forward everything to Apache.

### Option 2 — nginx user-include (CLI)
Add a location that proxies straight to gunicorn, bypassing the docroot:

```bash
mkdir -p /etc/nginx/conf.d/users/oriokie
cat > /etc/nginx/conf.d/users/oriokie/kws.oriokie.com.conf <<'EOF'
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
EOF
/usr/local/cpanel/scripts/ea-nginx config --all
systemctl reload nginx
```

(The exact include path can differ by cPanel version. If `ea-nginx` complains,
use the WHM nginx Manager instead — Option 1.)

## C. Test the public URL

```bash
curl -ik https://kws.oriokie.com/healthz/
```

{"status":"ok"} → open https://kws.oriokie.com in a browser.

---

## D. THEN make gunicorn permanent (systemd)

Your gunicorn is currently running by hand. The `treasury.service not found`
error means the unit was never installed. Create it so it survives reboots and
`systemctl restart treasury` works:

```bash
# stop the manual gunicorn first (find and kill it)
pkill -f "gunicorn -c gunicorn.conf.py" 2>/dev/null

cp /home/oriokie/apps/treasury/deploy/treasury.service.example \
   /etc/systemd/system/treasury.service
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

Then:
```bash
systemctl daemon-reload
systemctl enable --now treasury
systemctl status treasury        # active (running)
```
