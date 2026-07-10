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
# allow the in-app updater's wsgi.py touch to trigger a clean reload
reload_extra_files = ["config/wsgi.py"]
