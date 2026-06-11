#!/usr/bin/env bash
# One-command updater for a hosted instance.
# Pulls the latest code from GitHub, installs dependencies, applies database
# migrations, collects static files, and restarts the service.
#
# Usage:  ./update.sh
# Safe to re-run. Stops on first error.
set -euo pipefail

cd "$(dirname "$0")"
echo "==> Updating Church Treasury to the latest release"

echo "==> Backing up the database"
if [ -f db.sqlite3 ]; then
  cp db.sqlite3 "db.sqlite3.backup-$(date +%Y%m%d-%H%M%S)"
fi

echo "==> Pulling latest code"
git fetch --all --tags
git pull --ff-only

echo "==> Installing dependencies"
if [ -d .venv ]; then source .venv/bin/activate; fi
pip install -r requirements.txt --quiet

echo "==> Applying database migrations"
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput >/dev/null 2>&1 || true

echo "==> Done. Now at $(cat VERSION) ($(git rev-parse --short HEAD))"
echo "    Restart your web server (e.g. 'sudo systemctl restart treasury' or"
echo "    touch the WSGI file) to load the new code."
