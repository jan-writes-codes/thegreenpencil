from django.conf import settings
from django.urls import translate_url
from django.utils.translation import get_language


def language_switch(request):
    """The current page's address in each site language, for the language
    switch and the hreflang links. Pages outside the public i18n URLs (the app,
    login) translate to themselves, so the switch is only shown where both
    languages exist."""
    path = request.path
    urls = {code: translate_url(path, code) for code, _ in settings.LANGUAGES}
    abs_urls = {code: settings.SITE_URL + url for code, url in urls.items()}
    return {
        "lang_urls": urls,
        "lang_abs_urls": abs_urls,
        "canonical_url": abs_urls.get(get_language(), settings.SITE_URL + path),
        "has_translation": len(set(urls.values())) > 1,
    }
