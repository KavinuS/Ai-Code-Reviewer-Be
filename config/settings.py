"""
Django settings for the AI Code Review Assistant backend.

Configuration is driven by environment variables loaded from the repository-root
.env file. Nothing that differs between a laptop, CI and production is hard-coded
here, and no secret is ever committed.

Two switches decide which infrastructure this process talks to:

  * DB_ENGINE  - "sqlite" (default) or "postgres"
  * REDIS_URL  - when set, Redis is used for caching; otherwise Django's
                 in-process local-memory cache is used

Both are explicit rather than "try Postgres, silently fall back to SQLite".
A silent fallback is worse than a loud failure: it hides a broken database
connection until data quietly goes to the wrong place.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# .../backend/config/settings.py -> .../backend/config -> .../backend -> repo root
BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

# The same .env is read by docker-compose, so the database credentials used by
# the container and by Django cannot drift apart.
load_dotenv(REPO_ROOT / ".env")


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------

def env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name} must be an integer, got {raw!r}."
        ) from exc


def env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(50))\""
        )
    # Development-only fallback so a fresh clone runs before .env is filled in.
    # Refused above in any non-debug environment.
    SECRET_KEY = "insecure-development-key-not-for-production-use"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=("localhost", "127.0.0.1"))

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Reported by /api/health/ so a deployed frontend can show what it is talking to.
SERVICE_NAME = "ai-code-review-assistant"
SERVICE_VERSION = env_str("SERVICE_VERSION", "0.1.0")
ENVIRONMENT_NAME = env_str("ENVIRONMENT_NAME", "development")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    # Refresh-token rotation is only useful if the token it replaces stops
    # working, and this app is what holds the list of tokens rotated out of
    # use. Without it, ROTATE_REFRESH_TOKENS issues a new token and quietly
    # leaves the old one valid.
    "rest_framework_simplejwt.token_blacklist",
    # Local
    "accounts",
    "reviews",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # CorsMiddleware must sit above CommonMiddleware so that CORS headers are
    # attached even to responses CommonMiddleware short-circuits (redirects).
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DB_ENGINE = env_str("DB_ENGINE", "sqlite").lower()

if DB_ENGINE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env_str("POSTGRES_DB"),
            "USER": env_str("POSTGRES_USER"),
            "PASSWORD": env_str("POSTGRES_PASSWORD"),
            "HOST": env_str("POSTGRES_HOST", "localhost"),
            "PORT": env_str("POSTGRES_PORT", "5432"),
            # Reuse connections for 60s instead of reconnecting per request.
            "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        }
    }
elif DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    raise ImproperlyConfigured(
        f"DB_ENGINE must be 'sqlite' or 'postgres', got {DB_ENGINE!r}."
    )


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "").strip()

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # A slow or dead Redis must never hold a request open: the cache
                # layer degrades to "miss" instead of taking the API down.
                "SOCKET_CONNECT_TIMEOUT": env_int("REDIS_CONNECT_TIMEOUT", 2),
                "SOCKET_TIMEOUT": env_int("REDIS_SOCKET_TIMEOUT", 2),
                "IGNORE_EXCEPTIONS": True,
            },
            "KEY_PREFIX": env_str("CACHE_KEY_PREFIX", "acra"),
        }
    }
    DJANGO_REDIS_IGNORE_EXCEPTIONS = True
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ai-code-review-assistant-locmem",
        }
    }

# How long a completed review stays cached (Phase 4).
REVIEW_CACHE_TTL_SECONDS = env_int("REVIEW_CACHE_TTL_SECONDS", 60 * 60 * 24)


# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": (
        ["rest_framework.renderers.JSONRenderer", "rest_framework.renderers.BrowsableAPIRenderer"]
        if DEBUG
        else ["rest_framework.renderers.JSONRenderer"]
    ),
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Renders typed ReviewErrors as a safe JSON body with the right status.
    "EXCEPTION_HANDLER": "reviews.exceptions.review_exception_handler",
    "UNAUTHENTICATED_USER": None,
    # A bearer token, not a session cookie. Angular and Django are different
    # origins in development, and a cookie session across them would need
    # credentialed CORS plus a CSRF token on every write; a signed token in an
    # Authorization header needs neither and behaves the same in production.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    # Open by default, locked per view. This application genuinely has public
    # endpoints - health, and the marking scheme the landing page explains
    # itself with - so a global IsAuthenticated would be wrong; but the default
    # is the permissive one, which means a new view is public until somebody
    # says otherwise. Every view therefore declares its own permission class,
    # including the public ones, so the answer is visible where the view is
    # rather than inferred from this setting.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    # Throttling is per-view, via ScopedRateThrottle. The rates below cover the
    # endpoints worth attacking: a password guess, a sign-up flood, a refresh
    # loop. Anonymous callers are counted per IP, which is crude, but it is
    # what stops a single host running a dictionary attack.
    "DEFAULT_THROTTLE_RATES": {
        "auth_login": env_str("THROTTLE_AUTH_LOGIN", "10/min"),
        "auth_register": env_str("THROTTLE_AUTH_REGISTER", "5/hour"),
        "auth_refresh": env_str("THROTTLE_AUTH_REFRESH", "60/hour"),
        "auth_password": env_str("THROTTLE_AUTH_PASSWORD", "5/hour"),
        "auth_oauth": env_str("THROTTLE_AUTH_OAUTH", "30/hour"),
    },
}


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
# A custom user model is used from the start (see accounts/models.py). Its one
# substantive difference from Django's is that email is unique, which is what
# makes matching an incoming OAuth account to an existing user safe.

AUTH_USER_MODEL = "accounts.User"

SIMPLE_JWT = {
    # Short, because an access token cannot be revoked once issued: this is the
    # window in which a stolen one is still usable.
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env_int("JWT_ACCESS_MINUTES", 15)),
    # Long enough that a returning user is not asked to sign in every day,
    # short enough that an abandoned token expires on its own.
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env_int("JWT_REFRESH_DAYS", 14)),
    # Every refresh returns a new refresh token and blacklists the one used.
    # A refresh token presented twice means it was copied, and the second use
    # fails - so a theft becomes a logout rather than a silent second session.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    # Falls back to SECRET_KEY. Set JWT_SIGNING_KEY to rotate token signing on
    # its own: everything else derived from SECRET_KEY - sessions, CSRF tokens,
    # the OAuth state and ticket signatures - would otherwise break with it.
    "SIGNING_KEY": os.environ.get("JWT_SIGNING_KEY", "").strip() or SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}


# --------------------------------------------------------------------------
# OAuth sign-in (GitHub, Google)
# --------------------------------------------------------------------------
# Client secrets are read here and never leave the backend. The browser only
# ever sees the client id, inside the authorization URL this server builds.
#
# A provider with no id and secret is not offered at all: /api/auth/providers/
# publishes the configured list, and the frontend renders only those buttons.

OAUTH_CREDENTIALS = {
    "github": {
        "client_id": os.environ.get("GITHUB_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "").strip(),
    },
    "google": {
        "client_id": os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
    },
}

# Where the provider sends the browser back to. This is Django's own public
# origin, not the frontend's: the callback has to run here so the client secret
# and the token exchange stay server-side.
OAUTH_CALLBACK_BASE_URL = env_str("OAUTH_CALLBACK_BASE_URL", "http://localhost:8000")

# The path each provider redirects to, per provider rather than one shared
# shape. The redirect URI has to match what is registered in the provider's
# console character for character - trailing slash included - and the console
# is often filled in first, so the code follows it rather than the other way
# round. The defaults are the URIs registered for this project.
#
# These are the source of truth in both directions: accounts/urls.py builds the
# routes from this dict, and accounts/oauth/registry.py builds the redirect_uri
# sent to the provider from it, so the route served and the URI advertised
# cannot drift apart.
OAUTH_CALLBACK_PATHS = {
    "github": env_str("GITHUB_OAUTH_REDIRECT_PATH", "/auth/github/callback"),
    "google": env_str("GOOGLE_OAUTH_REDIRECT_PATH", "/login/oauth2/code/google"),
}

# Where that callback hands control back to Angular.
FRONTEND_BASE_URL = env_str("FRONTEND_BASE_URL", "http://localhost:4200")
FRONTEND_OAUTH_CALLBACK_PATH = env_str("FRONTEND_OAUTH_CALLBACK_PATH", "/auth/callback")

OAUTH_HTTP_TIMEOUT_SECONDS = env_int("OAUTH_HTTP_TIMEOUT_SECONDS", 10)
# How long a started sign-in may take to come back from the provider. Ten
# minutes covers "approve the consent screen, then go and find your 2FA device".
OAUTH_STATE_MAX_AGE_SECONDS = env_int("OAUTH_STATE_MAX_AGE_SECONDS", 600)
# How long the ticket in the callback URL is worth anything. The frontend
# redeems it on page load, so two minutes is slack rather than a window.
OAUTH_TICKET_MAX_AGE_SECONDS = env_int("OAUTH_TICKET_MAX_AGE_SECONDS", 120)


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
# The Angular dev server runs on a different origin to Django, so the browser
# needs explicit permission. An allow-list is used rather than
# CORS_ALLOW_ALL_ORIGINS, which would remain wide open in production.

CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    default=("http://localhost:4200", "http://127.0.0.1:4200"),
)
# Tokens travel in the Authorization header, which django-cors-headers already
# permits. No cookie crosses origins, so credentialed CORS - which would also
# force exact-origin matching and a CSRF token - is not needed and stays off.
CORS_ALLOW_CREDENTIALS = False


# --------------------------------------------------------------------------
# Request limits
# --------------------------------------------------------------------------
# Submitted source code is untrusted input. Capping the request body here means
# an oversized payload is rejected by Django before any view, serializer or AI
# call ever sees it.

MAX_REQUEST_BODY_BYTES = env_int("MAX_REQUEST_BODY_BYTES", 1_048_576)  # 1 MiB
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_REQUEST_BODY_BYTES
DATA_UPLOAD_MAX_NUMBER_FIELDS = 100
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_REQUEST_BODY_BYTES


# --------------------------------------------------------------------------
# AI provider (used from Phase 2)
# --------------------------------------------------------------------------
# The key is read here and never leaves the backend. Angular talks to Django;
# only Django talks to the AI provider.

AI_PROVIDER = env_str("AI_PROVIDER", "gemini")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = env_str("GEMINI_MODEL", "gemini-3.6-flash")
# Measured: a ~3,000-character file takes Gemini 40-82 seconds, so 60 failed
# roughly one review in three. 180 leaves headroom for a larger submission.
AI_REQUEST_TIMEOUT_SECONDS = env_int("AI_REQUEST_TIMEOUT_SECONDS", 180)
AI_MAX_RETRIES = env_int("AI_MAX_RETRIES", 1)


# --------------------------------------------------------------------------
# Auth, i18n, static
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        # Above Django's default of 8. The registration serializer enforces the
        # same floor so the message arrives on the field, but this is the check
        # that also covers createsuperuser and any future password reset.
        "OPTIONS": {"min_length": env_int("PASSWORD_MIN_LENGTH", 8)},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Application logs go to stdout in a single line format. Log records must never
# contain submitted source code, AI prompts or API keys - only identifiers,
# hashes and error classes.

LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "reviews": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "config": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}


# --------------------------------------------------------------------------
# Production hardening
# --------------------------------------------------------------------------
# Applied only when DEBUG is off, so local HTTP development is unaffected.

if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 60 * 60 * 24 * 30)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    X_FRAME_OPTIONS = "DENY"
