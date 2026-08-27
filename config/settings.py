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
    # Local
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
}


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

AI_PROVIDER = env_str("AI_PROVIDER", "openai")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = env_str("OPENAI_MODEL", "gpt-4o-mini")
AI_REQUEST_TIMEOUT_SECONDS = env_int("AI_REQUEST_TIMEOUT_SECONDS", 60)
AI_MAX_RETRIES = env_int("AI_MAX_RETRIES", 1)


# --------------------------------------------------------------------------
# Auth, i18n, static
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
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
