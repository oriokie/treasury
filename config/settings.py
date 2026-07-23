"""
Django settings for the Church Treasury System.

Runs on SQLite out of the box for evaluation; set DATABASE_URL-style env vars
(or the individual POSTGRES_* values below) to run on PostgreSQL in production.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Minimal, dependency-free .env reader.

    Loads KEY=VALUE lines from a .env file into os.environ if not already set,
    so the app works without exporting variables manually (and without the
    fragile `export $(... | xargs)` trick, which breaks on spaces or #). Lines
    starting with # are ignored; surrounding single/double quotes are stripped.
    Existing environment variables always win, so a real export still overrides.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if (len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'"):
                    val = val[1:-1]
                os.environ.setdefault(key, val)
    except OSError:
        pass


_load_dotenv(BASE_DIR / ".env")


def env_bool(key, default=False):
    return os.environ.get(key, str(default)).lower() in {"1", "true", "yes", "on"}


# --- Core ------------------------------------------------------------------
_DEV_SECRET_KEY = "dev-insecure-key-change-me-in-production-0123456789abcdef"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", _DEV_SECRET_KEY)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

# --- Application-layer encryption ------------------------------------------
# Sensitive settings (API keys, SMS/Telegram credentials) and the automated
# backups are encrypted with Fernet. The key material is configurable:
#   * TREASURY_ENCRYPTION_KEY — set this to a long random secret in production
#     so encryption does not depend on SECRET_KEY (which you may rotate).
#   * If unset, the key falls back to SECRET_KEY so it works out of the box.
# Encryption can be turned off (not recommended) for environments that handle
# secrecy at another layer; existing encrypted values remain readable.
ENCRYPTION_ENABLED = env_bool("TREASURY_ENCRYPTION_ENABLED", True)
ENCRYPTION_KEY = os.environ.get("TREASURY_ENCRYPTION_KEY", "")
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

# --- Fail loudly on unsafe production configuration ------------------------
# In production (DEBUG off) we must never run on the shipped dev secret, and we
# warn about settings that silently weaken security or risk data loss. These
# checks run at import time so a misconfigured deploy is caught before it serves
# a single request, rather than degrading quietly.
if not DEBUG:
    import warnings
    from django.core.exceptions import ImproperlyConfigured

    if SECRET_KEY == _DEV_SECRET_KEY:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is not set: the application is using the built-in "
            "development key, which is public. Set DJANGO_SECRET_KEY to a long "
            "random secret in the environment before running in production."
        )
    if ALLOWED_HOSTS == ["*"]:
        warnings.warn(
            "DJANGO_ALLOWED_HOSTS is '*' (any host). Set it to your actual "
            "domain(s), e.g. 'kws.oriokie.com', to prevent Host-header attacks.",
            stacklevel=2,
        )
    if ENCRYPTION_ENABLED and not ENCRYPTION_KEY:
        warnings.warn(
            "TREASURY_ENCRYPTION_KEY is not set, so encryption falls back to "
            "SECRET_KEY. If SECRET_KEY ever changes, all encrypted data (including "
            "two-factor secrets) becomes unreadable and users get locked out. Set a "
            "stable TREASURY_ENCRYPTION_KEY in the environment.",
            stacklevel=2,
        )


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # third party
    "simple_history",
    # local
    "core",
    "accounts",
    "departments",
    "members",
    "giving",
    "statements",
    "cashbook",
    "envelopes",
    "reports",
    "assets",
    "ledger",
    "pledges",
    "leaders",
    "loans",
    "benevolent",
    # third party (must come after local apps)
    "axes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files directly from the app process, so a
    # production deploy needs no separate static-file server.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # request-scoped SiteConfig memo (recommendation #2) — early, so every
    # later middleware and the view share one read of the settings row
    "core.middleware.SiteConfigCacheMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Default-deny (review P1-1): every view requires an authenticated user
    # UNLESS it is explicitly marked @login_not_required. Before this, auth was
    # opt-IN per view (a mixin on each), so a new view that forgot its mixin was
    # public by accident. Now the default is closed and public views opt OUT
    # deliberately and visibly. Runs right after AuthenticationMiddleware (which
    # sets request.user) and before the app's own lock/2FA gates, so an
    # unauthenticated request is turned away at the door.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "accounts.auth.AccountLockMiddleware",
    "accounts.auth.TwoFactorMiddleware",
    "accounts.auth.ForcePasswordChangeMiddleware",
    # Confines a self-service portal member to /portal/. Runs after the auth
    # gates (so an unauthenticated or locked request never gets this far) and
    # before the view, so the confinement is one rule in one place rather than
    # a mixin every office view has to remember. See the class docstring.
    "core.middleware.PortalConfinementMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "axes.middleware.AxesMiddleware",
    # Opens a request-scoped memo so heavy reporting aggregates (department_summary,
    # trust_summary, …) that flow through core.perfcache.cached compute at most
    # once per request. Placed last so it wraps view execution.
    "core.perfcache.RequestScopeMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_context",
                "core.context_processors.breadcrumb",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database --------------------------------------------------------------
# Priority: PostgreSQL (POSTGRES_DB) → MySQL/MariaDB (MYSQL_DB) → SQLite.
# On cPanel/WHM, MySQL is the native managed option — create the DB and user in
# cPanel, grant ALL PRIVILEGES, then set the MYSQL_* variables in .env.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "treasury"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
elif os.environ.get("MYSQL_DB"):
    # Prefer the C driver (mysqlclient) if present; otherwise transparently fall
    # back to the pure-Python PyMySQL driver, which needs no compiler. This lets
    # the app run on hosts (e.g. cPanel without Python dev headers) where the C
    # extension can't be built.
    try:
        import MySQLdb  # noqa: F401  (mysqlclient)
    except Exception:
        try:
            import pymysql
            pymysql.install_as_MySQLdb()
        except Exception:
            pass
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.environ["MYSQL_DB"],
            "USER": os.environ.get("MYSQL_USER", "treasury"),
            "PASSWORD": os.environ.get("MYSQL_PASSWORD", ""),
            "HOST": os.environ.get("MYSQL_HOST", "localhost"),
            "PORT": os.environ.get("MYSQL_PORT", "3306"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {
                "charset": "utf8mb4",
                # strict mode + sane defaults
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Authentication & brute-force protection (django-axes) -----------------
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
import sys as _sys
AXES_ENABLED = "test" not in _sys.argv      # disable lockout during the test suite
AXES_FAILURE_LIMIT = int(os.environ.get("AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_TIME = 0.25          # hours (15 minutes) before a locked account can retry
# Combination (nested list), NOT ["username", "ip_address"] (a flat list means
# "locked out if EITHER the username OR the ip_address alone crosses the
# failure limit" - independently). With a flat list, several people sharing
# one office network/IP would all get locked out the moment any ONE of them
# mistyped their password enough times, since the IP itself trips the limit
# regardless of which username was being tried. The nested form here locks
# out only the specific (username, ip) pair that actually failed repeatedly —
# other accounts, and the same account from a different network, are
# unaffected.
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = None
# Route a lockout back through the app's own login page (with a clear,
# styled message) instead of axes' bare, unstyled default response.
AXES_LOCKOUT_CALLABLE = "accounts.auth.axes_lockout_response"

# --- I18N / TZ --------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

# --- Static ----------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise: compress and fingerprint static files for long-cache serving.
# During tests the manifest doesn't exist (no collectstatic), so fall back to a
# plain storage backend so {% static %} resolves without a manifest lookup.
_TESTING = "test" in _sys.argv
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if _TESTING else
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth flow --------------------------------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "after_login"
LOGOUT_REDIRECT_URL = "login"

# --- App config -------------------------------------------------------------
SITE_NAME = os.environ.get("SITE_NAME", "Church Treasury")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "your-org/church-treasury"
# Personal access token for the update checker to read releases from a PRIVATE
# repo (an unauthenticated API request returns 404 for private repos). A
# fine-grained token with read-only "Contents" permission is enough. Never
# committed — set it in .env. Note: this authorises the release CHECK only; the
# git pull during an update uses the local git remote's own credentials (SSH key
# or a stored credential helper).
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CHURCH_NAME = os.environ.get("CHURCH_NAME", "SDA Central Church")

# Whether expenses must be approved before they affect fund balances.
EXPENSE_REQUIRE_APPROVAL = env_bool("EXPENSE_REQUIRE_APPROVAL", True)

# Session lifetime: Django's default is 2 weeks with no idle timeout, which is
# longer than good practice for a system handling financial data. A shorter,
# sliding session (renewed on activity, so an active user is never logged out
# mid-task) with an idle timeout the browser drops on close is a safer default
# for a treasury application. Override via env if a church needs something
# different — this is a policy choice, not a hardcoded rule.
SESSION_COOKIE_AGE = int(os.environ.get("DJANGO_SESSION_COOKIE_AGE", str(12 * 60 * 60)))  # 12h
SESSION_SAVE_EVERY_REQUEST = True   # sliding expiry: renews on activity
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("DJANGO_SESSION_EXPIRE_AT_BROWSER_CLOSE", False)

# Production security (only switch on behind HTTPS)
if not DEBUG:
    # When TLS is terminated by a reverse proxy (nginx/Caddy/Traefik), trust its
    # X-Forwarded-Proto header so Django knows the request is secure.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"


# --- Email + admin error alerts -------------------------------------------
def _parse_admin(a):
    a = a.strip()
    if "<" in a and a.endswith(">"):
        name, email = a[:-1].split("<", 1)
        return (name.strip() or "Admin", email.strip())
    return ("Admin", a)

ADMINS = [_parse_admin(x) for x in os.environ.get("DJANGO_ADMINS", "").split(",") if x.strip()]
MANAGERS = ADMINS

_email_host = os.environ.get("DJANGO_EMAIL_HOST", "")
if _email_host:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = _email_host
    EMAIL_PORT = int(os.environ.get("DJANGO_EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_PASSWORD", "")
    EMAIL_USE_TLS = env_bool("DJANGO_EMAIL_TLS", True)
else:
    # no SMTP configured -> emails are written to the log instead of failing,
    # so the app and the backup emailer degrade gracefully out of the box.
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_FROM_EMAIL", "treasury@localhost")
SERVER_EMAIL = os.environ.get("DJANGO_SERVER_EMAIL", DEFAULT_FROM_EMAIL)


# --- Caching for heavy aggregates (off unless a TTL is set) -----------------
# Set DJANGO_DASH_CACHE_TTL (seconds, e.g. 60) in production to cache the
# dashboard/executive/controls aggregates. Any financial write busts the cache
# immediately (see core.perfcache); the TTL is just a backstop. Default 0 = off.
DASHBOARD_CACHE_TTL = int(os.environ.get("DJANGO_DASH_CACHE_TTL", "0") or "0")

# --- Logging ---------------------------------------------------------------
_LOG_DIR = os.environ.get("DJANGO_LOG_DIR", str(BASE_DIR / "logs"))
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _have_log_dir = os.access(_LOG_DIR, os.W_OK)
except OSError:
    _have_log_dir = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        # only sends when ADMINS is set + email is configured; a no-op otherwise
        "mail_admins": {"class": "django.utils.log.AdminEmailHandler",
                        "level": "ERROR", "include_html": False},
    },
    "root": {"handlers": ["console"],
             "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console", "mail_admins"], "level": "ERROR",
                           "propagate": False},
        "django.security": {"handlers": ["console", "mail_admins"], "level": "WARNING",
                            "propagate": False},
        "treasury": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
if _have_log_dir:
    LOGGING["handlers"]["error_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": os.path.join(_LOG_DIR, "treasury-errors.log"),
        "maxBytes": 5 * 1024 * 1024, "backupCount": 5,
        "level": "WARNING", "formatter": "verbose",
    }
    for _h in (LOGGING["root"], LOGGING["loggers"]["django.request"],
               LOGGING["loggers"]["django.security"], LOGGING["loggers"]["treasury"]):
        _h["handlers"].append("error_file")


# --- Optional Sentry error monitoring (active only if SENTRY_DSN is set) ----
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES", "0")),
            send_default_pii=False,
            environment=os.environ.get("SENTRY_ENV", "dev" if DEBUG else "production"))
    except Exception:
        # Sentry is optional — never let a missing package break startup
        pass
