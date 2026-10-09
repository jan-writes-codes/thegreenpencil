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
