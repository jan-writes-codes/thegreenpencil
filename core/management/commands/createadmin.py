"""Create a single admin user — the bootstrap account for an empty database.

On a fresh production database there is no way into the admin GUI (login and
role-gating key off the custom ``role`` field, and ``createsuperuser`` leaves
``role='student'``). Run this once after ``migrate`` to create the first admin,
then log in at ``/login/`` and build tutors and students from the GUI.

    python manage.py createadmin                         # interactive prompts
    python manage.py createadmin --email a@b.at --password s3cret --name "Jan H"

``--from-env`` is the non-interactive variant for PaaS build commands (e.g.
Render's free tier, where there is no shell). It reads ``ADMIN_EMAIL``,
``ADMIN_PASSWORD`` and optionally ``ADMIN_NAME``, does nothing when the first
two are unset, and skips silently when that admin already exists, so it is
safe to run on every deploy:

    python manage.py createadmin --from-env
"""
import getpass
import os
import re

from django.core.management.base import BaseCommand, CommandError

from core.models import User


def _slug_from(email, name):
    """Derive a unique, URL-safe slug from the name (or email local part)."""
    base = (name or email.split("@")[0]).strip().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "admin"
    slug = base
    n = 2
    while User.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _initials_from(name, email):
    parts = [p for p in (name or "").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    return email[:2].upper()


class Command(BaseCommand):
    help = "Create one admin user to bootstrap an empty database."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Login email for the admin.")
        parser.add_argument("--password", help="Password (omit to be prompted securely).")
        parser.add_argument("--name", default="", help="Full name, e.g. \"Jan Heissenberger\".")
        parser.add_argument(
            "--username",
            help="Auth username (defaults to the slug derived from name/email).",
        )
        parser.add_argument(
            "--from-env",
            action="store_true",
            help="Read ADMIN_EMAIL/ADMIN_PASSWORD/ADMIN_NAME from the environment; "
                 "skip if unset or the admin already exists (safe on every deploy).",
        )

    def handle(self, *args, **opts):
        if opts.get("from_env"):
            email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
            password = os.environ.get("ADMIN_PASSWORD") or ""
            if not email or not password:
                self.stdout.write("createadmin: ADMIN_EMAIL/ADMIN_PASSWORD not set — skipping.")
                return
            if User.objects.filter(email__iexact=email).exists():
                self.stdout.write(f"createadmin: admin {email} already exists — skipping.")
                return
            opts.update(email=email, password=password,
                        name=(os.environ.get("ADMIN_NAME") or "").strip())

        email = (opts.get("email") or input("Admin email: ")).strip().lower()
        if not email or "@" not in email:
            raise CommandError("A valid email is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A user with email {email} already exists.")

        name = (opts.get("name") or "").strip()

        password = opts.get("password")
        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords did not match.")
        if not password:
            raise CommandError("A password is required.")

        slug = _slug_from(email, name)
        username = (opts.get("username") or slug).strip()
        if User.objects.filter(username=username).exists():
            raise CommandError(f"Username {username!r} is taken; pass a different --username.")

        first_name, _, last_name = name.partition(" ")

        User.objects.create_user(
            username=username,
            email=email,
            password=password,
            role="admin",
            slug=slug,
            initials=_initials_from(name, email),
            color1="#309050",
            color2="#277a42",
            first_name=first_name,
            last_name=last_name,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Created admin '{username}' ({email}) with slug '{slug}'. "
            f"Log in at /login/ and build out tutors and students from the GUI."
        ))
