from django.http import JsonResponse

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
