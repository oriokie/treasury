#!/usr/bin/env bash
#
# Treasury — interactive deployment installer
# ===========================================
# Sets up the SDA church treasury app on a cPanel/WHM server:
#   .env (collected via dialogs with validation) → database → Python deps &
#   migrations → gunicorn + systemd → Apache proxy include → nginx → SSL → verify.
#
# Safe to re-run: every step is idempotent and asks before overwriting.
# Run as root (needed for Apache/nginx/systemd), from the app directory:
#
#   sudo bash deploy/install.sh
#
# It NEVER prints secrets to the terminal and writes .env with 600 perms.
# ---------------------------------------------------------------------------

set -uo pipefail

# ---- pretty output ---------------------------------------------------------
BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YLW=$'\033[33m'; BLU=$'\033[36m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
info() { printf '%s%s%s\n' "$BLU" "$*" "$RST"; }
ok()   { printf '%s✓ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s! %s%s\n' "$YLW" "$*" "$RST"; }
err()  { printf '%s✗ %s%s\n' "$RED" "$*" "$RST" >&2; }
hr()   { printf '%s────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"; }
step() { hr; printf '%s%s%s\n' "$BOLD" "$*" "$RST"; hr; }

# ---- dialog backend: whiptail/dialog if present, else plain read -----------
DIALOG=""
if command -v whiptail >/dev/null 2>&1; then DIALOG=whiptail
elif command -v dialog   >/dev/null 2>&1; then DIALOG=dialog
fi

# ask_text VAR "Prompt" "default" "validator_fn"   (validator optional)
ask_text() {
  local __var="$1" prompt="$2" def="${3:-}" validate="${4:-}" val=""
  while true; do
    if [[ -n "$DIALOG" ]]; then
      val=$("$DIALOG" --inputbox "$prompt" 10 72 "$def" 3>&1 1>&2 2>&3) || { err "Cancelled."; exit 1; }
    else
      printf '%s%s%s' "$BOLD" "$prompt" "$RST"
      [[ -n "$def" ]] && printf ' [%s]' "$def"
      printf ': '
      read -r val
      [[ -z "$val" ]] && val="$def"
    fi
    if [[ -n "$validate" ]]; then
      if msg=$("$validate" "$val"); then printf -v "$__var" '%s' "$val"; return 0
      else err "$msg"; [[ -n "$DIALOG" ]] && "$DIALOG" --msgbox "$msg" 9 60; fi
    else
      printf -v "$__var" '%s' "$val"; return 0
    fi
  done
}

# ask_secret VAR "Prompt"  (input hidden; never echoed)
ask_secret() {
  local __var="$1" prompt="$2" val=""
  while true; do
    if [[ -n "$DIALOG" ]]; then
      val=$("$DIALOG" --passwordbox "$prompt" 10 72 3>&1 1>&2 2>&3) || { err "Cancelled."; exit 1; }
    else
      printf '%s%s%s: ' "$BOLD" "$prompt" "$RST"; read -rs val; printf '\n'
    fi
    if [[ -z "$val" ]]; then err "This value can't be empty."; continue; fi
    printf -v "$__var" '%s' "$val"; return 0
  done
}

# ask_yesno "Prompt"  -> returns 0 for yes
ask_yesno() {
  local prompt="$1" ans=""
  if [[ -n "$DIALOG" ]]; then
    "$DIALOG" --yesno "$prompt" 10 72 3>&1 1>&2 2>&3; return $?
  fi
  while true; do
    printf '%s%s%s [y/N]: ' "$BOLD" "$prompt" "$RST"; read -r ans
    case "${ans,,}" in y|yes) return 0;; n|no|"") return 1;; esac
  done
}

# ---- validators (echo an error message + return 1 on failure) --------------
v_nonempty()  { [[ -n "$1" ]] || { echo "Value can't be empty."; return 1; }; }
v_hostname()  {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || {
    echo "Enter a valid hostname, e.g. treasury.yourchurch.org"; return 1; }; }
v_user()      {
  [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || {
    echo "Enter a valid Linux/cPanel username (lowercase, no spaces)."; return 1; }; }
v_path()      { [[ "$1" = /* ]] || { echo "Enter an absolute path starting with /"; return 1; }; }
v_port()      {
  [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )) || {
    echo "Enter a port number between 1 and 65535."; return 1; }; }
v_dbident()   {
  [[ "$1" =~ ^[A-Za-z0-9_]+$ ]] || {
    echo "Use letters, numbers and underscores only (no spaces)."; return 1; }; }
v_repo()      {
  [[ -z "$1" || "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
    echo "Use the form owner/repo (or leave blank to skip)."; return 1; }; }
v_email()     {
  [[ -z "$1" || "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || {
    echo "Enter a valid email address (or leave blank)."; return 1; }; }

# ---- SQL string literals ---------------------------------------------------
# The one place a value becomes a quoted MySQL string. Everything the installer
# sends to the mysql client goes through here; nothing interpolates a collected
# answer straight into SQL any more.
#
# The password used to be pasted between two bare quotes — IDENTIFIED BY
# '$DB_PASS' — so a password containing an apostrophe (an ordinary character in
# a strong password, and one a password manager will happily produce) closed
# the literal early and the rest of it was handed to a mysql client running as
# root as further SQL. Rejecting the apostrophe would have been the wrong fix:
# a good password should not have to be weakened to get through the installer.
#
# Two characters need escaping inside a single-quoted literal, and BOTH are
# doubled rather than backslash-escaped:
#   '  ->  ''    a doubled quote is a literal quote in every SQL mode
#   \  ->  \\    MySQL treats backslash as an escape by default, so a password
#                ending in one would otherwise escape the closing quote
# Doubling the quote (rather than writing \') is what makes this safe even on a
# server running with NO_BACKSLASH_ESCAPES, where a backslash escape would not
# be honoured and \' would break straight back out of the literal.
#
# sed rather than ${var//…/…}: bash 3.2 does not double a backslash reliably in
# a pattern substitution, and this has to be right on whatever bash the host
# happens to ship. The value travels on a pipe, never in argv, so it does not
# appear in the process list.
sql_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e "s/'/''/g")"
}

# ---------------------------------------------------------------------------
clear 2>/dev/null || true
step "Treasury deployment installer"
say "This will collect settings, then set up the database, Python environment,"
say "gunicorn service, Apache proxy, nginx and SSL. You can re-run it safely."
say ""
[[ -n "$DIALOG" ]] && ok "Using $DIALOG dialogs" || warn "whiptail/dialog not found — using plain prompts (still works)."

if [[ $EUID -ne 0 ]]; then
  warn "Not running as root. Database/Apache/nginx/systemd steps will be skipped."
  warn "For a full install, re-run with: sudo bash deploy/install.sh"
fi

# locate the app directory (this script lives in <app>/deploy/)
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
info "App directory: $APP_DIR"
[[ -f manage.py ]] || { err "manage.py not found in $APP_DIR — run this from the app's deploy/ folder."; exit 1; }

# ===========================================================================
step "1/8  Collect settings"
# ===========================================================================
# pre-load any existing .env so re-runs default to current values
declare -A CUR
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do [[ "$k" =~ ^[A-Z] ]] && CUR["$k"]="$v"; done < .env
  ok "Loaded existing .env as defaults."
fi
d() { printf '%s' "${CUR[$1]:-$2}"; }   # default helper: existing value or fallback

ask_text DOMAIN     "Public domain (e.g. treasury.yourchurch.org)" "$(d DJANGO_ALLOWED_HOSTS kws.oriokie.com)" v_hostname
# DJANGO_ALLOWED_HOSTS may have been comma-joined; take the first host as the domain
DOMAIN="${DOMAIN%%,*}"
ask_text CPANEL_USER "cPanel user that OWNS the domain (NOT necessarily the app owner)" "$(d CPANEL_USER tutoress)" v_user
ask_text APP_USER    "Linux user the app runs as (owns the app files)" "$(d APP_USER oriokie)" v_user
ask_text CHURCH      "Church name (shown in the app & on receipts)" "$(d CHURCH_NAME "SDA Church")" v_nonempty
ask_text SITE        "Site name (browser title)" "$(d SITE_NAME "Church Treasury")" v_nonempty

info "Database (MySQL recommended on cPanel — created in cPanel → MySQL Databases):"
ask_text DB_NAME  "MySQL database name (e.g. ${CPANEL_USER}_treasury)" "$(d MYSQL_DB ${CPANEL_USER}_treasury)" v_dbident
ask_text DB_USER  "MySQL username (e.g. ${CPANEL_USER}_trez)" "$(d MYSQL_USER ${CPANEL_USER}_trez)" v_dbident
ask_secret DB_PASS "MySQL password"
ask_text DB_HOST  "MySQL host" "$(d MYSQL_HOST localhost)" v_nonempty
ask_text DB_PORT  "MySQL port" "$(d MYSQL_PORT 3306)" v_port

info "Gunicorn / proxy ports (defaults match the standard cPanel+nginx setup):"
ask_text GUNI_BIND "Gunicorn bind address" "$(d GUNICORN_BIND 127.0.0.1:8000)" v_nonempty
ask_text APACHE_HTTP_PORT  "Apache HTTP port (nginx owns 80)" "81"  v_port
ask_text APACHE_HTTPS_PORT "Apache HTTPS port (nginx owns 443)" "444" v_port

info "GitHub (for the in-app 'update available' check — optional):"
ask_text GH_REPO  "GitHub repo as owner/repo (blank to skip)" "$(d GITHUB_REPO "")" v_repo
GH_TOKEN=""
if [[ -n "$GH_REPO" ]] && ask_yesno "Is $GH_REPO a PRIVATE repo (needs a read-only token)?"; then
  ask_secret GH_TOKEN "GitHub fine-grained token (read-only Contents)"
fi

ask_text SSL_EMAIL "Admin email for SSL/AutoSSL notices (optional)" "$(d SSL_EMAIL "")" v_email

# secret key: reuse existing, else generate
SECRET="$(d DJANGO_SECRET_KEY "")"
if [[ -z "$SECRET" || "$SECRET" == change-me* ]]; then
  if command -v python3 >/dev/null; then
    SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))')"
  else
    SECRET="$(head -c 50 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 50)"
  fi
  ok "Generated a fresh Django secret key."
else
  ok "Reusing the existing Django secret key."
fi

# review (secrets masked)
step "Review"
cat <<REVIEW
  Domain ............. $DOMAIN
  cPanel user ........ $CPANEL_USER   (owns the domain / Apache includes)
  App user ........... $APP_USER      (owns app files, runs gunicorn)
  Church / Site ...... $CHURCH  /  $SITE
  Database ........... $DB_USER@$DB_HOST:$DB_PORT  db=$DB_NAME  pass=********
  Gunicorn bind ...... $GUNI_BIND
  Apache ports ....... HTTP $APACHE_HTTP_PORT  /  HTTPS $APACHE_HTTPS_PORT
  GitHub repo ........ ${GH_REPO:-(none)}  token=$([[ -n "$GH_TOKEN" ]] && echo set || echo none)
  SSL email .......... ${SSL_EMAIL:-(none)}
REVIEW
ask_yesno "Write these settings and continue?" || { warn "Aborted; nothing changed."; exit 0; }

# ===========================================================================
step "2/8  Write .env"
# ===========================================================================
if [[ -f .env ]]; then cp -f .env ".env.bak.$(date +%Y%m%d-%H%M%S)"; ok "Backed up existing .env"; fi
umask 077
cat > .env <<ENV
# Generated by deploy/install.sh on $(date)
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=$DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN

SITE_NAME=$SITE
CHURCH_NAME=$CHURCH

GITHUB_REPO=$GH_REPO
GITHUB_TOKEN=$GH_TOKEN

MYSQL_DB=$DB_NAME
MYSQL_USER=$DB_USER
MYSQL_PASSWORD=$DB_PASS
MYSQL_HOST=$DB_HOST
MYSQL_PORT=$DB_PORT

GUNICORN_BIND=$GUNI_BIND

# Bookkeeping for re-runs (not read by Django):
CPANEL_USER=$CPANEL_USER
APP_USER=$APP_USER
SSL_EMAIL=$SSL_EMAIL
ENV
chmod 600 .env
ok ".env written (permissions 600 — secrets not world-readable)."

# derived
ROOT_OK=0; [[ $EUID -eq 0 ]] && ROOT_OK=1

# ===========================================================================
step "3/8  Database (MySQL)"
# ===========================================================================
if (( ROOT_OK )) && command -v mysql >/dev/null 2>&1; then
  if ask_yesno "Create/repair the MySQL database '$DB_NAME' (utf8mb4) and grant '$DB_USER'?
(Existing data is preserved — this only ensures the DB exists with the right charset.)"; then
    # DB_NAME is a backtick-quoted identifier, and v_dbident has already held it
    # to [A-Za-z0-9_]. The three values that are string literals — user, host and
    # the unvalidated password — go through sql_quote, which supplies the quotes
    # itself; that is why there are none written around them here.
    mysql <<SQL && ok "Database ensured (utf8mb4_unicode_ci) and privileges granted." || warn "MySQL step failed — create the DB in cPanel and re-run."
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER DATABASE \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS $(sql_quote "$DB_USER")@$(sql_quote "$DB_HOST") IDENTIFIED BY $(sql_quote "$DB_PASS");
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO $(sql_quote "$DB_USER")@$(sql_quote "$DB_HOST");
FLUSH PRIVILEGES;
SQL
  else
    warn "Skipped DB creation. Ensure '$DB_NAME' exists as utf8mb4_unicode_ci."
  fi
else
  warn "Not root or mysql client missing — create the DB in cPanel → MySQL Databases:"
  say  "   • DB '$DB_NAME', user '$DB_USER', GRANT ALL, charset utf8mb4_unicode_ci."
fi

# ===========================================================================
step "4/8  Python environment + migrations"
# ===========================================================================
PY=python3
if [[ ! -d .venv ]]; then
  if ask_yesno "Create a Python virtualenv at .venv?"; then
    "$PY" -m venv .venv && ok "Virtualenv created." || { err "venv creation failed."; exit 1; }
  fi
fi
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  ok "Activated .venv ($(python --version 2>&1))"
fi
if [[ -f requirements.txt ]] && ask_yesno "Install/upgrade Python dependencies from requirements.txt?"; then
  pip install --upgrade pip -q && pip install -r requirements.txt -q \
    && ok "Dependencies installed." || warn "pip install reported problems — review the output above."
fi
# PyMySQL fallback (cPanel often can't compile mysqlclient)
python -c 'import MySQLdb' 2>/dev/null || pip install PyMySQL -q 2>/dev/null && true

if ask_yesno "Run database migrations now?"; then
  python manage.py migrate --noinput && ok "Migrations applied." || err "migrate failed — check DB settings in .env."
fi
if ask_yesno "Collect static files?"; then
  python manage.py collectstatic --noinput >/dev/null 2>&1 && ok "Static files collected." || warn "collectstatic had issues."
fi
if ask_yesno "Create an admin (superuser) login now?"; then
  python manage.py createsuperuser || warn "Superuser creation skipped/failed — you can run it later."
fi

# ===========================================================================
step "5/8  Gunicorn systemd service"
# ===========================================================================
if (( ROOT_OK )); then
  if ask_yesno "Install a systemd service 'treasury' so the app runs on boot & restarts on crash?"; then
    GUNI_BIN="$APP_DIR/.venv/bin/gunicorn"
    [[ -x "$GUNI_BIN" ]] || pip install gunicorn -q 2>/dev/null
    cat > /etc/systemd/system/treasury.service <<UNIT
[Unit]
Description=Treasury (gunicorn) — $DOMAIN
After=network.target mysql.service

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$GUNI_BIN -c $APP_DIR/gunicorn.conf.py config.wsgi
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable --now treasury \
      && ok "treasury.service enabled and started." \
      || warn "systemd start failed — check: journalctl -u treasury -n 50"
  fi
else
  warn "Not root — skipping systemd. To run manually:"
  say  "   gunicorn -c $APP_DIR/gunicorn.conf.py config.wsgi"
fi

# ===========================================================================
step "6/8  Apache proxy include (cPanel/WHM)"
# ===========================================================================
if (( ROOT_OK )) && [[ -d /etc/apache2/conf.d/userdata ]]; then
  if ask_yesno "Write the Apache proxy include under user '$CPANEL_USER' for $DOMAIN
(forwards Apache → gunicorn on $GUNI_BIND, and excludes /.well-known for SSL)?"; then
    for kind in ssl std; do
      dir="/etc/apache2/conf.d/userdata/$kind/2_4/$CPANEL_USER/$DOMAIN"
      mkdir -p "$dir"
      cat > "$dir/treasury.conf" <<APACHE
# Generated by deploy/install.sh
ProxyPreserveHost On
ProxyPass /.well-known !
ProxyPass / http://$GUNI_BIND/
ProxyPassReverse / http://$GUNI_BIND/
APACHE
    done
    ok "Apache includes written (ssl + std) under $CPANEL_USER."
    /usr/local/cpanel/scripts/ensure_vhost_includes --user="$CPANEL_USER" --verbose >/dev/null 2>&1 || true
    /usr/local/cpanel/scripts/rebuildhttpdconf >/dev/null 2>&1 || true
    systemctl restart httpd 2>/dev/null || systemctl restart apache2 2>/dev/null || true
    if grep -q "$GUNI_BIND" /etc/apache2/conf/httpd.conf 2>/dev/null; then
      ok "Verified: the proxy compiled into Apache's config."
    else
      warn "Could not verify the proxy in httpd.conf — confirm the cPanel user is '$CPANEL_USER'."
    fi
  fi
else
  warn "Not a cPanel/WHM Apache layout (or not root) — skipping Apache include."
fi

# ===========================================================================
step "7/8  nginx pass-through + SSL"
# ===========================================================================
if (( ROOT_OK )); then
  if [[ -x /usr/local/cpanel/scripts/ea-nginx ]]; then
    if ask_yesno "Rebuild cPanel nginx config so it forwards to Apache?"; then
      /usr/local/cpanel/scripts/ea-nginx config --all >/dev/null 2>&1 || true
      systemctl reload nginx 2>/dev/null || true
      ok "nginx config rebuilt and reloaded."
    fi
    say ""
    info "SSL certificate (AutoSSL):"
    say  "  Run AutoSSL for $DOMAIN in WHM → SSL/TLS Status (or: /usr/local/cpanel/bin/autossl_check --user=$CPANEL_USER)."
    say  "  The /.well-known proxy exclusion above lets ACME validation through."
    if ask_yesno "Trigger AutoSSL for '$CPANEL_USER' now?"; then
      /usr/local/cpanel/bin/autossl_check --user="$CPANEL_USER" >/dev/null 2>&1 \
        && ok "AutoSSL run triggered (issuance can take a few minutes)." \
        || warn "AutoSSL trigger failed — run it from WHM → SSL/TLS Status."
    fi
  else
    warn "cPanel nginx (ea-nginx) not found — configure your web server to forward to $GUNI_BIND."
    say  "A generic nginx example is in deploy/nginx.conf.example."
  fi
fi

# ===========================================================================
step "8/8  Verify"
# ===========================================================================

if command -v curl >/dev/null; then
  say "Checking the app on gunicorn directly…"
  if curl -fsS -H "Host: $DOMAIN" "http://$GUNI_BIND/healthz/" >/dev/null 2>&1; then
    ok "gunicorn is serving /healthz/ on $GUNI_BIND"
  else
    warn "gunicorn not responding on $GUNI_BIND yet (start it / check the service)."
  fi
  if (( ROOT_OK )); then
    say "Checking Apache on its HTTPS port ($APACHE_HTTPS_PORT)…"
    curl -fsSk -H "Host: $DOMAIN" "https://127.0.0.1:$APACHE_HTTPS_PORT/healthz/" >/dev/null 2>&1 \
      && ok "Apache forwards to the app on :$APACHE_HTTPS_PORT" \
      || warn "Apache not forwarding yet — see deploy/RUNBOOK for the proxy steps."
  fi
  say "Checking the public URL…"
  curl -fsSk "https://$DOMAIN/healthz/" >/dev/null 2>&1 \
    && ok "Public site is up: https://$DOMAIN/" \
    || warn "Public URL not healthy yet — SSL may still be issuing, or nginx needs a reload."
fi

hr
ok "Installer finished."
say ""
say "${BOLD}Next steps / quick reference${RST}"
say "  • App URL ........ https://$DOMAIN/"
say "  • Health check ... https://$DOMAIN/healthz/"
say "  • Restart app .... systemctl restart treasury"
say "  • App logs ....... journalctl -u treasury -n 100 --no-pager"
say "  • Import data .... python manage.py import_legacy --phase all   (with files in data/)"
say "  • Update later ... ./update.sh && systemctl restart treasury"
say ""
say "If anything didn't go green above, deploy/RUNBOOK_kws.oriokie.com.md has the"
say "manual steps and a troubleshooting cheat-sheet for each component."
