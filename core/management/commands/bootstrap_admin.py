"""Deprecated alias for ``createadmin --from-env``.

Kept only so existing PaaS build commands that still call ``bootstrap_admin``
keep working. Switch them to ``python manage.py createadmin --from-env``; this
alias will then be removed.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Deprecated: use `createadmin --from-env`."

    def handle(self, *args, **opts):
        call_command("createadmin", from_env=True, stdout=self.stdout, stderr=self.stderr)
