# Getting kws.oriokie.com to actually serve the app

Run these in order. Each step verifies before moving on.

## 1. Pin the gunicorn port in .env

Add this line to `/home/oriokie/apps/treasury/.env` so the port is predictable:

```
GUNICORN_BIND=127.0.0.1:8599
```

## 2. Collect static files (once)

```bash
cd /home/oriokie/apps/treasury
source .venv/bin/activate
python manage.py collectstatic --noinput
```

## 3. Install + start the systemd service

```bash
cp deploy/treasury.service.example /etc/systemd/system/treasury.service
nano /etc/systemd/system/treasury.service
```

Make these lines read exactly:

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
systemctl status treasury          # must say "active (running)"
```

If it failed, see the logs:

```bash
journalctl -u treasury -n 50 --no-pager
```

## 4. Prove the app works locally

```bash
curl -i http://127.0.0.1:8599/healthz/
```

Expect: `{"status":"ok","database":true,"version":"1.0.0"}`.
If this fails, the problem is the app — fix it here before touching Apache.

## 5. Make Apache proxy the subdomain to gunicorn

cPanel rebuilds its own vhosts, so use userdata includes (they survive rebuilds).
Replace `oriokie` with your cPanel username if different.

```bash
# HTTPS vhost
mkdir -p /etc/apache2/conf.d/userdata/ssl/2_4/oriokie/kws.oriokie.com
cat > /etc/apache2/conf.d/userdata/ssl/2_4/oriokie/kws.oriokie.com/treasury.conf <<'EOF'
ProxyPreserveHost On
RequestHeader set X-Forwarded-Proto "https"
ProxyPass /.well-known !
ProxyPass / http://127.0.0.1:8599/
ProxyPassReverse / http://127.0.0.1:8599/
EOF

# HTTP vhost (so the redirect to HTTPS works before AutoSSL, and ACME validation passes)
mkdir -p /etc/apache2/conf.d/userdata/std/2_4/oriokie/kws.oriokie.com
cat > /etc/apache2/conf.d/userdata/std/2_4/oriokie/kws.oriokie.com/treasury.conf <<'EOF'
ProxyPreserveHost On
ProxyPass /.well-known !
ProxyPass / http://127.0.0.1:8599/
ProxyPassReverse / http://127.0.0.1:8599/
EOF

# rebuild cPanel's Apache config and restart
/usr/local/cpanel/scripts/rebuildhttpdconf
systemctl restart httpd     # or: /scripts/restartsrv_httpd
```

## 6. Make sure the proxy modules are enabled

```bash
httpd -M 2>/dev/null | grep -E 'proxy_module|proxy_http_module'
```

If nothing prints, enable them in **WHM → Apache Configuration → Include Editor**,
or via EasyApache 4 (add `mod_proxy` / `mod_proxy_http`), then restart Apache.

## 7. Test the public URL

```bash
curl -ik https://kws.oriokie.com/healthz/
```

Then open `https://kws.oriokie.com` in a browser.

## 8. TLS certificate

cPanel → SSL/TLS Status → run AutoSSL for kws.oriokie.com. The `/.well-known`
exclusion above lets the ACME validation reach Apache instead of the app.

---

## Quick troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Browser spins forever, no response | Apache not proxying | `curl -ik https://kws.oriokie.com/healthz/` |
| 502 Bad Gateway | gunicorn down or wrong port | `systemctl status treasury`, `ss -tlnp \| grep 8599` |
| 403 / cPanel default page | proxy config not applied | re-run rebuildhttpdconf + restart httpd |
| 500 from the app | app error | `journalctl -u treasury -n 50` |
| DISALLOWED_HOST in logs | ALLOWED_HOSTS wrong | `.env`: `DJANGO_ALLOWED_HOSTS=kws.oriokie.com` |
| CSS missing / unstyled | collectstatic not run | step 2 |
