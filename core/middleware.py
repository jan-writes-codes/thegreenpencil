import re

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse

# Uploads are capped at 10 MB per file (see MAX_LESSON_FILE_BYTES in views.py);
# allow a little on top for the multipart envelope and form fields.
MAX_UPLOAD_REQUEST_BYTES = 10 * 1024 * 1024 + 256 * 1024


class UploadSizeLimitMiddleware:
    """Refuse oversized multipart uploads from their Content-Length header,
    before Django reads the body. The per-view size check only runs after the
    whole file has been received, which ties up the (single) web worker."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.META.get("CONTENT_TYPE", "").startswith("multipart/"):
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > MAX_UPLOAD_REQUEST_BYTES:
                return JsonResponse({"error": "Datei zu groß (max. 10 MB)."}, status=413)
        return self.get_response(request)


class HealthCheckMiddleware:
    """``GET /healthz/`` answers ``ok`` when the app can reach its database and
    503 when it can't. For Render's health check and an external uptime monitor.

    Runs first in the stack, so it works whatever Host header the checker sends
    and over plain HTTP (Render probes from inside its network), and it never
    touches sessions or the throttling cache."""

    PATH = "/healthz/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != self.PATH:
            return self.get_response(request)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            response = HttpResponse("database unavailable", status=503, content_type="text/plain")
        else:
            response = HttpResponse("ok", content_type="text/plain")
        response["Cache-Control"] = "no-store"
        return response


LANGUAGE_COOKIE = "tgp_lang"


def _public_url_names():
    from .urls import public_urlpatterns
    return {p.name for p in public_urlpatterns}


class AppLanguageMiddleware:
    """The app (login, the app itself, its API, password reset, settle page)
    speaks the signed-in user's language, or for a visitor who isn't signed in
    the one they picked on the login page (``tgp_lang`` cookie). German is the
    default. The public pages keep the language of their URL (German plain,
    English under /en/), which LocaleMiddleware has already set.

    Must come after AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._public = None

    def _is_public(self, request):
        from django.urls import Resolver404, resolve
        if self._public is None:
            self._public = _public_url_names()
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return True
        return match.url_name in self._public or match.url_name == "level_check"

    def __call__(self, request):
        from django.utils import translation
        lang = None
        if not self._is_public(request):
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                lang = getattr(user, "language", "") or "de"
            else:
                lang = request.COOKIES.get(LANGUAGE_COOKIE, "de")
            if lang not in ("de", "en"):
                lang = "de"
            translation.activate(lang)
            request.LANGUAGE_CODE = lang
        return self.get_response(request)


TEST_BANNER_HTML = (
    '<div id="tgp-test-banner" role="status" style="position:relative;z-index:2147483647;'
    'margin:0;padding:3px 12px;background:#1d4ed8;color:#fff;'
    'font:600 12px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
    'text-align:center;letter-spacing:.02em">'
    'TESTSYSTEM · E-Mails nur an die Test-Adresse'
    '</div>'
)


class TestSystemBannerMiddleware:
    """On the test system only, put a small blue „TESTSYSTEM" bar at the top of
    every HTML page (public site, app, login, error pages), so nobody mistakes it
    for production. Off unless settings.IS_TEST_SYSTEM (SENTRY_ENVIRONMENT=test)."""

    _BODY = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not getattr(settings, "IS_TEST_SYSTEM", False):
            return response
        if (
            getattr(response, "streaming", False)
            or response.get("Content-Encoding")
            or "text/html" not in response.get("Content-Type", "")
        ):
            return response
        content = response.content
        match = self._BODY.search(content)
        if not match or b'id="tgp-test-banner"' in content:
            return response
        banner = TEST_BANNER_HTML.encode()
        response.content = content[: match.end()] + banner + content[match.end():]
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response
