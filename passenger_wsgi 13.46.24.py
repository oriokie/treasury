"""Passenger entry point for cPanel's 'Setup Python App'.

cPanel/Passenger looks for `application` in this file. We simply re-export the
project's standard WSGI application. (If you deploy with gunicorn + systemd
instead — the recommended path — this file is unused.)
"""
from config.wsgi import application  # noqa: F401
