# CLAUDE.md

Rules and orientation for anyone (human or Claude) working in this repo.
Two people now work on it, each with their own Claude. Follow this file so both
setups behave the same.

## Branches and pushing

- **`development`** is the working branch. It deploys to the **test** system.
- **`production`** deploys to the live site (thegreenpencil.at).
  **Never push, merge or deploy to `production` unless Jan explicitly asks.**
- No PRs needed for normal work: commit to `development` and push.
- Two contributors push to `development`, so avoid clobbering each other:
  - Always `git pull --rebase origin development` before you start and again
    right before you push. Never force-push `development` or `production`.
  - For anything bigger than a small fix, work on a short-lived branch
    (`feature/<topic>`), rebase it on `development`, then merge it in and
    delete the branch.
  - If a rebase conflicts in a file you didn't touch, stop and ask rather than
    guessing which side wins.
- CI (`.github/workflows/tests.yml`) runs the full suite on every push to
  `development`/`production`; Render only deploys a commit once it is green.
  Don't push code you know breaks tests.

## Test system vs production

Same code, deployed twice on Render, each with its own env vars and database.
"Test vs prod" is only which env vars the process starts with.

| | Test | Production |
|---|---|---|
| Branch | `development` | `production` |
| `SENTRY_ENVIRONMENT` | `test` (sets `IS_TEST_SYSTEM`) | `production` |
| Data | demo (`seed`) or a copy of prod | real students, built in the GUI |

Test-system rules (do not weaken them):

- **The test system only e-mails the declared test inbox, never real users.**
  `core/mail_backend.py` rewrites every outgoing mail to `EMAIL_TEST_RECIPIENT`,
  clears cc/bcc and puts the original recipients in the subject and an
  `X-Original-To` header. With no recipient set, test mail is dropped.
  Any new way of sending mail must go through Django's mail backend
  (`core/emails.py`) so the redirect applies.
- **Every test e-mail subject starts with `[TEST] `** (`EMAIL_SUBJECT_PREFIX`).
- **A blue TESTSYSTEM banner shows on every page** of the test system
  (`core/middleware.py`, inserted after `<body>` on all HTML responses). It must
  never appear on production.
- Never run `seed` against production. `copy_prod_to_test` copies real
  personal data into test; only run it on purpose and unset its env vars after.

## Secrets

- Never commit secrets: no API keys, passwords, `DATABASE_URL`s, Stripe/Resend/
  Zoom/Sentry credentials, `.env` files or `db.sqlite3` (may hold real data).
  They live in Render env vars; `.gitignore` already covers `.env*` and `db.sqlite3`.
- Each person uses their own logins for Render, Stripe, Resend etc. Don't share.
- Don't paste real student data (names, e-mails, phone numbers) into commits,
  tests, fixtures or screenshots.

## Stack

- Python / **Django 5.2 LTS**, one app `core/`, project settings in `fluent/`.
- Postgres in production and CI, SQLite locally (fallback when `DATABASE_URL`
  is unset). gunicorn + WhiteNoise on Render.
- Stripe (credit top-ups), Resend via django-anymail (mail), Sentry (errors),
  reportlab/pypdf (Belege and lesson PDFs), optional Zoom/Teams OAuth.
  Every integration is optional: unset env vars mean it is simply off.
- Full env var list and deploy details: `DEPLOY.md`. Local setup: `RUNNING.md`.

## Run and test

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed          # demo users, password "password" (see RUNNING.md)
python manage.py runserver     # http://127.0.0.1:8000/  (app at /app/)

./run_tests.sh                 # backend + jsdom frontend tests (needs Node for the latter)
python manage.py test core.tests.BookingPersistenceTests   # one class
```

Without `RESEND_API_KEY`, mail is printed to the console. Add or update tests
with every behaviour change; new tests go in the matching `core/tests*.py`.

## Code map

- `core/models.py` — `User` (custom: role, credits, billing, language),
  `Booking`, `CreditTransaction`, `Receipt`, groups/families, etc.
- `core/views.py` — public pages, `app_view` (renders `templates/app.html` with
  `window.DJANGO_DATA`) and the JSON API (`/api/...`). Routes in `core/urls.py`,
  public i18n routes in `fluent/urls.py`.
- `templates/app.html` — the single-page app (tutor, student, admin views) in
  one large file. Edit carefully and search before adding.
- `templates/landing.html` etc. — the public site; `templates/en/` English copies.
- `core/emails.py` + `templates/email/` (and `email/en/`) — all transactional mail.
- `core/receipts_pdf.py`, `core/lesson_pdf.py` — PDFs.
- `core/middleware.py` — health check, upload limit, app language, test banner.
- `core/management/commands/` — `seed`, `createadmin`, `cleanup_old_data`,
  `expire_booking_requests`, `copy_prod_to_test`, `send_test_email`.

## Conventions

- **The UI is German first.** Any German string you add needs English too:
  - public pages: `{% translate %}` + `locale/en/LC_MESSAGES/django.po`, then
    `makemessages -l en --no-location` and `compilemessages -l en`, and commit
    the `.po` **and** `.mo` (the deploy doesn't compile them);
  - prose pages and mails: edit the German template and its `en/` twin together;
  - the app: add the string to `static/js/i18n/en.js` (see `static/js/i18n/README.md`).
- **Role scoping:** students only ever receive their own data; other students'
  bookings are sent anonymised (`studentId: "__blocked__"`). Keep it that way
  for any new payload and add an authorization test.
- Calendar date keys use JS 0-indexed months; convert with
  `date_to_jskey` / `jskey_to_date` in `views.py`.
- Mutating API endpoints keep CSRF protection; there is a CSP in
  `fluent/settings.py` (add any new external script/host there).
- Schema changes need a migration in `core/migrations/` committed with the code.
- Commit messages: short imperative subject saying what changed for the user,
  then a body explaining why.
- Don't commit stray screenshots or scratch files to the repo root.
