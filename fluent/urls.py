from django.conf.urls.i18n import i18n_patterns
from django.urls import path, include

from core.urls import public_urlpatterns

urlpatterns = [
    path('', include('core.urls')),
    # German stays at the plain paths; English lives under /en/.
    *i18n_patterns(*public_urlpatterns, prefix_default_language=False),
]
