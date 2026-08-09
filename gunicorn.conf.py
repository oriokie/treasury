"""Gunicorn configuration for the Church Treasury app.

Run with:  gunicorn -c gunicorn.conf.py config.wsgi
Single worker is recommended for a small church on SQLite (the in-app Telegram
poller and the SQLite write-lock both prefer one process). Move to PostgreSQL
and raise `workers` for larger or multi-user-heavy deployments.
"""
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
accesslog = "-"      # stdout
errorlog = "-"       # stderr
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")

# There is deliberately no `reload` setting here, and nothing that depends on
# one. This file used to carry
#
#     reload_extra_files = ["config/wsgi.py"]   # so the updater can reload us
#
# which did nothing at all: gunicorn's own documentation describes
# reload_extra_files as extending the `reload` option, and the file watcher it
# feeds is built only inside `if self.cfg.reload:` in gunicorn/workers/base.py.
# `reload` defaults to False and is documented as a development aid — it puts a
# watcher in every worker — so it was never set here, and the in-app updater's
# touch of config/wsgi.py changed an mtime nobody was reading while the update
# page told the treasurer the app had been signalled to restart. It had not.
#
# The reload in production is a plain SIGHUP to the master process, which is
# what the in-app updater now sends (see core/services/updates.py) and what an
# operator can send by hand: the arbiter forks new workers, which import the
# application fresh — this config does NOT preload, so they pick up the new
# code — and retires the old workers as they finish their requests.
#
# For that reason `preload_app` must NOT be set here (nor --preload passed on
# the command line). With preloading the master imports the application itself
# and every worker is a fork of that one import; SIGHUP re-reads this config and
# respawns workers, but Application.reload() never clears the cached callable
# and the modules are already in the master's sys.modules, so the "new" workers
# come up running the master's old code. That fails in the worst way available:
# silently and while reporting success — the updater would signal, the arbiter
# would log a reload, and the app would go on serving the version it was
# serving before. Only a full process restart replaces a preloaded app.
