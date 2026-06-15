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
    # third party (must come after local apps)
    "axes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files directly from the app process, so a
    # production deploy needs no separate static-file server.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.auth.TwoFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "axes.middleware.AxesMiddleware",
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
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = None

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
LOGIN_REDIRECT_URL = "dashboard"
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


# --- Logging ---------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"],
             "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR",
                           "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING",
                            "propagate": False},
    },
}
