# Fluent Tutoring — Django implementation

A Django port of the **Fluent Tutoring** booking-platform design (`project/Fluent Tutoring.html`).
The full UI (earthy-cottagecore grainy-gradient theme, all four role views) is preserved
pixel-for-pixel; it is now backed by a real database, role-based auth, and a JSON API.

## Quick start

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed        # loads demo users, bookings, receipts, etc.
python manage.py runserver
```

Then open http://127.0.0.1:8000/.

The public marketing **landing page** is served at `/` (the site's front door).
Its "Stunde buchen" / "Erste Stunde buchen" CTAs lead into the booking app at
`/app/`, which gates on login.

## Demo logins (password is `password` for all)

| Email              | Role    | Lands on        |
| ------------------ | ------- | --------------- |
| `maya@fluent.at`   | Student | Book / Account  |
| `theo@fluent.at`   | Student | Book / Account  |
| `ines@fluent.at`   | Student | Book / Account  |
| `omar@fluent.at`   | Student | Book / Account  |
| `lena@fluent.at`   | Student | Book / Account  |
| `davit@fluent.at`  | Tutor   | Tutor portal    |
| `admin@fluent.at`  | Admin   | Admin user mgmt |

## How it fits together

- **`core/models.py`** — `User` (custom, role + credits + billing), `Booking`,
  `CreditTransaction`, `Receipt`, `AvailabilityOverride`, `CustomTime`,
  `StudentNote`, `ActiveLesson`, `SiteSettings`.
- **`core/views.py`** — `landing_view` renders the public `templates/landing.html`
  marketing page at `/`. `app_view` renders `templates/app.html` at `/app/`, injecting all
  the data the SPA needs as `window.DJANGO_DATA`. The remaining views are a small
  JSON API (login/logout, booking CRUD, credits + receipts, billing, availability,
  custom times, notes, lessons, admin user CRUD, settings).
- **`templates/app.html`** — the design prototype with its JavaScript rewired to
  hydrate from `window.DJANGO_DATA` and persist every mutation back through the API.
- **`core/management/commands/seed.py`** — recreates the prototype's demo state.

### Role-scoped payloads

A logged-in **student** only receives their own record, transactions, receipts and
lessons. The tutor's *other* bookings are still sent, but anonymized (`studentId:
"__blocked__"`, no titles or notes) purely so the calendar can block overlapping
slots without leaking who booked them. Tutor and admin receive the full dataset.

### Calendar date keys

The frontend builds date keys with JS `Date.getMonth()` (0-indexed). `views.py`
translates between those keys and real Python dates (`date_to_jskey` /
`jskey_to_date`) so stored dates stay semantically correct and the January edge
case (`getMonth() === 0`) doesn't crash.

### English version of the public pages

German is the default; the public pages (landing, FAQ, intro booking, cancel
page, legal pages) also exist in English under `/en/` (`i18n_patterns` in
`fluent/urls.py`). The app and login stay German only.

- Short strings are wrapped in `{% translate %}` and translated in
  `locale/en/LC_MESSAGES/django.po` (German is the msgid). After changing a
  wrapped German string or a translation, run
  `python manage.py makemessages -l en --no-location` and
  `python manage.py compilemessages -l en` (needs GNU gettext), then commit the
  `.po` **and** the `.mo` — the deploy doesn't compile them.
- The prose pages have their own English templates in `templates/en/` (FAQ and
  legal pages) and `templates/email/en/` (the guest's intro confirmation and
  cancellation mail). Edit the German and English file together.
- The English legal pages are courtesy translations and say the German text is
  binding.
