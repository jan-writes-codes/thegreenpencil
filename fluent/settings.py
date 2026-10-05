import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Fails closed on Render (which sets RENDER=true): DEBUG is off there unless
# DJANGO_DEBUG says otherwise. Local dev and CI keep DEBUG on by default.
DEBUG = os.environ.get(
    'DJANGO_DEBUG', 'false' if os.environ.get('RENDER') else 'true'
).lower() in ('1', 'true', 'yes', 'on')

# Without DEBUG there is no fallback: a missing key aborts startup (and with it
# the Render build) instead of signing sessions with a key published in git.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('Set DJANGO_SECRET_KEY (required when DEBUG is off).')
    SECRET_KEY = 'django-dev-secret-key-fluent-tutoring-2026-not-for-production'

if DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',') if h.strip()]

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves collected static files in production (DEBUG off). It must
    # come right after SecurityMiddleware and before everything else.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'fluent.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'fluent.wsgi.application'
ASGI_APPLICATION = 'fluent.asgi.application'

# Database is selected by the DATABASE_URL env var so each environment (test,
# production) points at its own database without code changes. When unset we
# fall back to a local SQLite file, keeping `runserver` zero-config for dev.
#   SQLite:    sqlite:////absolute/path/to/db.sqlite3
#   Postgres:  postgres://user:pass@host:5432/dbname
DATABASES = {
    'default': dj_database_url.config(
        env='DATABASE_URL',
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_USER_MODEL = 'core.User'
LOGIN_URL = '/login/'
# "Passwort vergessen" reset links expire after 24 hours (the e-mail copy
# promises exactly that). Django's default of 3 days is longer than a link
# that lands in a mailbox needs to live.
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24

# --- Stripe payments (optional) ---------------------------------------------
# Self-service credit top-ups go through Stripe Checkout when a secret key is
# configured. Leave STRIPE_SECRET_KEY unset to keep the tutor-mediated (e-mail)
# purchase flow as the only option — nothing in the app requires Stripe to run.
#   STRIPE_SECRET_KEY        sk_test_... / sk_live_...   (server-side, secret)
#   STRIPE_PUBLISHABLE_KEY   pk_test_... / pk_live_...   (exposed to the client)
#   STRIPE_WEBHOOK_SECRET    whsec_...    verifies webhook authenticity
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# --- Video calls: Zoom / Microsoft Teams OAuth (optional) --------------------
# Tutors can connect their own Zoom or Teams account so intro bookings get a
# call link created automatically. Each provider is enabled by configuring its
# OAuth app credentials; leave them unset and the "connect" buttons simply
# explain that the integration isn't configured — nothing else changes.
#   Zoom:  a "General App" (user-managed) with scope  meeting:write:meeting
#          and redirect URL  {SITE_URL}/oauth/video/zoom/callback/
#   Teams: an Entra ID app with delegated Graph scopes
#          OnlineMeetings.ReadWrite + offline_access + User.Read
#          and redirect URL  {SITE_URL}/oauth/video/teams/callback/
ZOOM_CLIENT_ID = os.environ.get('ZOOM_CLIENT_ID', '')
ZOOM_CLIENT_SECRET = os.environ.get('ZOOM_CLIENT_SECRET', '')
TEAMS_CLIENT_ID = os.environ.get('TEAMS_CLIENT_ID', '')
TEAMS_CLIENT_SECRET = os.environ.get('TEAMS_CLIENT_SECRET', '')
# Entra tenant the app lives in; 'common' allows any Microsoft account.
TEAMS_TENANT = os.environ.get('TEAMS_TENANT', 'common')

# --- Transactional email (Resend via django-anymail) ------------------------
# Set RESEND_API_KEY to send real mail (booking confirmations, tutor alerts).
# When unset, mail is printed to the console so the flow stays visible in dev —
# nothing in the app requires e-mail to be configured.
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
# Absolute base URL for links in e-mails (Impressum, Datenschutz, etc.).
SITE_URL = os.environ.get('SITE_URL', 'https://thegreenpencil.at').rstrip('/')
# Visible sender; must be on a domain verified (with DKIM) in the Resend dashboard.
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'The Green Pencil <hallo@thegreenpencil.at>'
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL
# Replies (e.g. a guest answering a confirmation) should reach the tutor's inbox.
EMAIL_REPLY_TO = os.environ.get('EMAIL_REPLY_TO', '')
# Where "new intro booked" alerts go (the tutor/studio inbox). Empty = no alert.
TUTOR_NOTIFY_EMAIL = os.environ.get('TUTOR_NOTIFY_EMAIL', '')
# Send in a background thread so a booking request never blocks on the ESP.
# Set EMAIL_ASYNC=false to send inline (tests do this for determinism).
EMAIL_ASYNC = os.environ.get('EMAIL_ASYNC', 'true').lower() in ('1', 'true', 'yes', 'on')

try:
    import anymail  # noqa: F401
    _HAS_ANYMAIL = True
except ImportError:  # pragma: no cover - dependency declared in requirements
    _HAS_ANYMAIL = False

if RESEND_API_KEY and _HAS_ANYMAIL:
    INSTALLED_APPS.append('anymail')
    EMAIL_BACKEND = 'anymail.backends.resend.EmailBackend'
    ANYMAIL = {'RESEND_API_KEY': RESEND_API_KEY}
else:
    # No ESP configured: surface mail in the console rather than failing.
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Europe/Vienna'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# WhiteNoise compresses static files (gzip/brotli) at `collectstatic` time and
# serves them with long-lived caching headers. We use the non-manifest variant
# so `{% static %}` resolves with or without a built manifest — the app never
# 500s before `collectstatic` runs (tests, first boot). Media keeps Django's
# default filesystem storage.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30  # 30 days

# --- Security headers & cookies ---------------------------------------------
# Cookies aren't readable by JS (the CSRF token is delivered via a server-rendered
# <meta>, not by reading the cookie), framing is denied, MIME sniffing is off.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'

# HTTPS-only hardening kicks in automatically in production (DEBUG off).
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
