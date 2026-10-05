"""Zoom / Microsoft Teams video-call integration for tutors.

A tutor connects their own Zoom or Teams account once (OAuth, from the tutor
portal); afterwards every intro booking gets a scheduled meeting created on
that account and the join link stored in ``Booking.call_link`` — so the
confirmation e-mails and the calendar all carry a working call URL.

Design constraints, in order:

* **Optional at runtime** — like Stripe, a provider only lights up when its
  OAuth app credentials are configured in settings. Nothing here is required
  for the app to run.
* **Never break a booking** — meeting creation/removal is strictly
  best-effort. Every entry point used by the booking flow catches
  ``VideoError`` and logs; a provider outage costs the link, not the lesson.
* **No new dependencies** — the two REST calls each provider needs are made
  with stdlib ``urllib`` (the project deliberately keeps requirements small).
"""
import base64
import json
import logging
from datetime import timedelta
from urllib import error as urlerror, parse as urlparse, request as urlrequest

from django.conf import settings
from django.utils import timezone

from .models import Booking, VideoConnection

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15  # seconds; these calls run off-request or best-effort

ZOOM_AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API = "https://api.zoom.us/v2"
MS_LOGIN = "https://login.microsoftonline.com"
GRAPH_API = "https://graph.microsoft.com/v1.0"
# Delegated Graph scopes: create meetings on the tutor's behalf, keep a
# refresh token, and read the profile for the "Verbunden als …" label.
TEAMS_SCOPE = "offline_access User.Read OnlineMeetings.ReadWrite"

PROVIDERS = ("zoom", "teams")


class VideoError(Exception):
    """Any provider-side failure (HTTP error, timeout, malformed response)."""


def provider_credentials(provider):
    if provider == "zoom":
        return settings.ZOOM_CLIENT_ID, settings.ZOOM_CLIENT_SECRET
    if provider == "teams":
        return settings.TEAMS_CLIENT_ID, settings.TEAMS_CLIENT_SECRET
    return "", ""


def provider_enabled(provider):
    client_id, secret = provider_credentials(provider)
    return bool(client_id and secret)


def enabled_providers():
    """{'zoom': bool, 'teams': bool} — which connect buttons are live."""
    return {p: provider_enabled(p) for p in PROVIDERS}


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #
def _http(method, url, *, headers=None, form=None, json_body=None):
    """One JSON-speaking HTTP call. Returns the decoded body ({} when empty,
    e.g. a 204 delete). Raises VideoError on anything unexpected."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urlparse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urlerror.HTTPError as e:
        detail = ""
        try:
            detail = e.read()[:500].decode("utf-8", "replace")
        except Exception:
            pass
        raise VideoError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
    except (urlerror.URLError, TimeoutError, OSError) as e:
        raise VideoError(f"{method} {url} failed: {e}") from e
    if not body or not body.strip():
        return {}
    try:
        return json.loads(body)
    except ValueError as e:
        raise VideoError(f"{method} {url} returned non-JSON body") from e


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# OAuth
# --------------------------------------------------------------------------- #
def authorize_url(provider, redirect_uri, state):
    """Where to send the tutor's browser to grant access."""
    client_id, _ = provider_credentials(provider)
    if provider == "zoom":
        return ZOOM_AUTHORIZE_URL + "?" + urlparse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        })
    return f"{MS_LOGIN}/{settings.TEAMS_TENANT}/oauth2/v2.0/authorize?" + urlparse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "response_mode": "query",
        "redirect_uri": redirect_uri,
        "scope": TEAMS_SCOPE,
        "state": state,
    })


def _token_request(provider, form):
    """Hit the provider's token endpoint (code exchange and refresh share it).
    Zoom authenticates the app with HTTP Basic; Microsoft wants the client
    credentials (and scope) inside the form."""
    client_id, secret = provider_credentials(provider)
    if provider == "zoom":
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        return _http("POST", ZOOM_TOKEN_URL,
                     headers={"Authorization": f"Basic {basic}"}, form=form)
    form = dict(form, client_id=client_id, client_secret=secret, scope=TEAMS_SCOPE)
    return _http("POST", f"{MS_LOGIN}/{settings.TEAMS_TENANT}/oauth2/v2.0/token", form=form)


def exchange_code(provider, code, redirect_uri):
    """Authorization code -> token payload (access_token/refresh_token/expires_in)."""
    return _token_request(provider, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    })


def apply_tokens(conn, tokens):
    """Copy a token payload onto the connection (without saving). Both providers
    rotate refresh tokens — keep the old one only when none was returned."""
    conn.access_token = tokens.get("access_token", "")
    if tokens.get("refresh_token"):
        conn.refresh_token = tokens["refresh_token"]
    try:
        expires_in = int(tokens.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    conn.token_expires_at = timezone.now() + timedelta(seconds=expires_in)


def ensure_access_token(conn):
    """A currently-valid access token for the connection, refreshing (and
    persisting the rotated refresh token) when it's expired or about to be."""
    fresh = conn.token_expires_at and conn.token_expires_at > timezone.now() + timedelta(seconds=60)
    if fresh or not conn.refresh_token:
        return conn.access_token
    tokens = _token_request(conn.provider, {
        "grant_type": "refresh_token",
        "refresh_token": conn.refresh_token,
    })
    apply_tokens(conn, tokens)
    conn.save(update_fields=["access_token", "refresh_token", "token_expires_at", "updated_at"])
    return conn.access_token


def fetch_account_label(provider, access_token):
    """The connected account's e-mail for the settings UI. Cosmetic only — a
    failure here must never fail the connect itself."""
    try:
        if provider == "zoom":
            me = _http("GET", f"{ZOOM_API}/users/me", headers=_bearer(access_token))
            return me.get("email") or me.get("display_name") or ""
        me = _http("GET", f"{GRAPH_API}/me", headers=_bearer(access_token))
        return me.get("mail") or me.get("userPrincipalName") or ""
    except VideoError:
        logger.warning("could not fetch %s account label", provider)
        return ""


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #
def create_meeting(conn, booking, *, topic):
    """Create a scheduled meeting on the tutor's connected account for the
    booking's slot. Returns (join_url, meeting_id); raises VideoError."""
    token = ensure_access_token(conn)
    start = booking.start
    minutes = booking.minutes
    if conn.provider == "zoom":
        meeting = _http("POST", f"{ZOOM_API}/users/me/meetings", headers=_bearer(token), json_body={
            "topic": topic,
            "type": 2,  # scheduled meeting
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": "Europe/Vienna",
            "duration": minutes,
            # The guest has no Zoom account and the tutor may join late — don't
            # strand them in a waiting room for a 15-minute intro.
            "settings": {"join_before_host": True, "waiting_room": False},
        })
        return meeting.get("join_url", ""), str(meeting.get("id", ""))
    end = start + timedelta(minutes=minutes)
    meeting = _http("POST", f"{GRAPH_API}/me/onlineMeetings", headers=_bearer(token), json_body={
        "subject": topic,
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
    })
    return meeting.get("joinWebUrl", ""), str(meeting.get("id", ""))


def attach_call_link(booking_id):
    """Best-effort: create a call on the booking tutor's connected Zoom/Teams
    account and store the join link on the booking.

    Runs before the confirmation e-mails are rendered so the link rides along
    in them. Skips silently when the tutor has no connection, the provider is
    no longer configured, or a link is already set (never overwrite a link a
    tutor pasted by hand). Any provider failure is logged and swallowed — the
    booking itself must never fail because a video provider is down."""
    booking = Booking.objects.filter(pk=booking_id).select_related("tutor").first()
    if not booking or booking.call_link or not booking.tutor_id:
        return
    conn = VideoConnection.objects.filter(tutor_id=booking.tutor_id).first()
    if not conn or not provider_enabled(conn.provider):
        return
    with_name = booking.guest_name or booking.student_name or ""
    topic = f"{booking.title} · {with_name}" if with_name else booking.title
    try:
        join_url, meeting_id = create_meeting(conn, booking, topic=topic)
    except VideoError:
        logger.exception("creating %s meeting for booking %s failed", conn.provider, booking_id)
        return
    if not join_url:
        return
    booking.call_link = join_url
    booking.video_provider = conn.provider
    booking.video_meeting_id = meeting_id
    booking.save(update_fields=["call_link", "video_provider", "video_meeting_id"])


def _meeting_url(provider, meeting_id):
    quoted = urlparse.quote(str(meeting_id), safe="")
    if provider == "zoom":
        return f"{ZOOM_API}/meetings/{quoted}"
    return f"{GRAPH_API}/me/onlineMeetings/{quoted}"


def cleanup_meeting(tutor_id, provider, meeting_id):
    """Best-effort: remove an auto-created meeting after its booking was
    cancelled, so the tutor's Zoom/Teams account doesn't collect ghosts. Takes
    plain values (not the booking) because the row is already deleted."""
    if not meeting_id:
        return
    conn = VideoConnection.objects.filter(tutor_id=tutor_id, provider=provider).first()
    if not conn or not provider_enabled(provider):
        return
    try:
        token = ensure_access_token(conn)
        _http("DELETE", _meeting_url(provider, meeting_id), headers=_bearer(token))
    except VideoError:
        # The cancellation stands either way; an orphaned meeting is harmless.
        logger.warning("deleting %s meeting %s failed", provider, meeting_id)


def move_meeting(booking_id):
    """Best-effort: after a reschedule, move the auto-created meeting to the
    booking's new slot so the call link stays valid at the right time."""
    booking = Booking.objects.filter(pk=booking_id).first()
    if not booking or not booking.video_meeting_id:
        return
    conn = VideoConnection.objects.filter(
        tutor_id=booking.tutor_id, provider=booking.video_provider
    ).first()
    if not conn or not provider_enabled(booking.video_provider):
        return
    start = booking.start
    try:
        token = ensure_access_token(conn)
        if conn.provider == "zoom":
            body = {
                "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "timezone": "Europe/Vienna",
                "duration": booking.minutes,
            }
        else:
            end = start + timedelta(minutes=booking.minutes)
            body = {"startDateTime": start.isoformat(), "endDateTime": end.isoformat()}
        _http("PATCH", _meeting_url(conn.provider, booking.video_meeting_id),
              headers=_bearer(token), json_body=body)
    except VideoError:
        logger.warning("moving %s meeting %s failed",
                       booking.video_provider, booking.video_meeting_id)
