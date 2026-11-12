"""E-mail backend that keeps test-system mail away from real people.

On the test service every outgoing message — booking confirmations, tutor
alerts, Belege, Storno, password mails, anything sent through Django's mail
framework — is rewritten here, at the very last step, so no send path can slip
past it:

* ``to`` becomes ``settings.EMAIL_TEST_RECIPIENT``; ``cc`` and ``bcc`` are
  cleared.
* The original recipients are kept in an ``X-Original-To`` header and appended
  to the subject, so the tester can see who the mail was meant for.
* If no test recipient is configured the message is dropped (and logged), never
  sent to its original recipients.

The actual delivery is delegated to ``settings.EMAIL_DELIVERY_BACKEND``
(Resend, console, …).
"""
import logging

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class RedirectToTestRecipientBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self._inner = get_connection(
            settings.EMAIL_DELIVERY_BACKEND, fail_silently=fail_silently, **kwargs
        )

    def open(self):
        return self._inner.open()

    def close(self):
        return self._inner.close()

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        target = (getattr(settings, "EMAIL_TEST_RECIPIENT", "") or "").strip()
        if not target:
            for msg in email_messages:
                logger.warning(
                    "Test system without EMAIL_TEST_RECIPIENT: dropped e-mail %r to %s",
                    msg.subject, ", ".join(msg.recipients()),
                )
            return 0
        for msg in email_messages:
            original = msg.recipients()
            msg.to = [target]
            msg.cc = []
            msg.bcc = []
            if original:
                joined = ", ".join(original)
                msg.extra_headers["X-Original-To"] = joined
                msg.subject = f"{msg.subject} [an: {joined}]"
        return self._inner.send_messages(email_messages)
