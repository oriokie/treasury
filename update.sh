#!/usr/bin/env bash
# One-command updater for a hosted instance.
# Pulls the latest code from GitHub, installs dependencies, applies database
# migrations and collects static files. It deliberately does NOT restart the
# service: this script has no way to know how the app is being served here, and
# the previous wording ("and restarts the service") described something it had
# never done. It prints the restart command instead — see the note at the end.
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
echo "    The new code is on disk but NOT being served yet — the running"
echo "    workers keep the old version until you do one of these:"
echo "      systemd:            sudo systemctl restart treasury"
echo "      gunicorn by hand:   sudo kill -HUP <master PID>   (see below)"
echo "      cPanel/Passenger:   touch tmp/restart.txt"
echo
echo "    To find the gunicorn master:  ps -eo pid,ppid,args | grep [g]unicorn"
echo "    It is the one whose parent is NOT another gunicorn. Signal only that"
echo "    process: SIGHUP to a worker is not a reload, it kills it outright."
# 'pkill -HUP -f "gunicorn: master"' was the obvious-looking command here and is
# wrong twice over, in the same silent way the wsgi touch below was. Gunicorn
# only renames its processes to "gunicorn: master [...]" when the optional
# setproctitle package is installed — it is not in requirements.txt, so
# util._setproctitle is the no-op branch, the titles stay as the plain command
# line, and the pattern matches nothing at all. Widening the pattern to match
# the command line is worse, not better: workers are forks and share that exact
# command line, and a worker resets SIGHUP to SIG_DFL, whose default action is
# to terminate. So the "graceful reload" would have hard-killed every worker
# mid-request. Hence a named PID and the warning, rather than a clever one-liner.
# Touching config/wsgi.py used to be offered here as a third option. It is not
# one: gunicorn only watches files when `reload` is on, that is a development
# setting and this deployment does not use it, so the touch changed an mtime
# nothing was reading and the operator walked away believing the new code was
# live. See the note in gunicorn.conf.py.
