import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret")
DEBUG = os.getenv("DEBUG", "False") == "True"

# --- Hosts: works for local, Railway, and Render without editing per platform ---
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = []

# Railway
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
if RAILWAY_PUBLIC_DOMAIN:
    ALLOWED_HOSTS.append(RAILWAY_PUBLIC_DOMAIN)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RAILWAY_PUBLIC_DOMAIN}")
ALLOWED_HOSTS.append(".railway.app")  # Railway's internal health checks

# Render (auto-injected — no need to set this one manually)
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")
ALLOWED_HOSTS.append(".onrender.com")

# Optional manual override / extra domains via env, comma-separated
# e.g. EXTRA_ALLOWED_HOSTS=myapp.up.railway.app,myapp.onrender.com
EXTRA_ALLOWED_HOSTS = os.getenv("EXTRA_ALLOWED_HOSTS", "")
if EXTRA_ALLOWED_HOSTS:
    ALLOWED_HOSTS.extend([h.strip() for h in EXTRA_ALLOWED_HOSTS.split(",") if h.strip()])

# External API Keys
ORS_API_KEY = os.getenv("ORS_API_KEY", "")
GEOCODIO_API_KEY = os.getenv("GEOCODIO_API_KEY", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "route",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "fuel_route.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "fuel_route.wsgi.application"

DATABASE_URL = os.getenv("DATABASE_URL")
import dj_database_url

if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DATABASE_NAME", os.getenv("POSTGRES_DB", "postgres")),
            "USER": os.getenv("DATABASE_USER", os.getenv("POSTGRES_USER", "postgres")),
            "PASSWORD": os.getenv(
                "DATABASE_PASSWORD", os.getenv("POSTGRES_PASSWORD", "")
            ),
            "HOST": os.getenv("DATABASE_HOST", os.getenv("POSTGRES_HOST", "localhost")),
            "PORT": os.getenv("DATABASE_PORT", os.getenv("POSTGRES_PORT", 5432)),
        }
    }

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
