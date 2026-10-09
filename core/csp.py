from django.conf import settings


class ContentSecurityPolicyMiddleware:
    """Adds settings.CONTENT_SECURITY_POLICY to every response that lacks one."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")
        if policy:
            response.headers.setdefault("Content-Security-Policy", policy)
        return response
