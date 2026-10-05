"""One-off: overwrite THIS environment's database with a copy of production's.

Meant for hosts with no shell (Render's free tier): add it to the TEST
service's build command, run one deploy, then remove it again.

    python manage.py migrate && python manage.py copy_prod_to_test

It does **nothing** unless both of these env vars are set on the service:

    PROD_DATABASE_URL     External URL of the production database (read only).
    COPY_PROD_TO_TEST     Must be exactly "yes" — a deliberate second switch.

Safety:

* production is only ever read (read-only session);
* refuses to run if source and target are the same database;
* the target is wiped and refilled in a single transaction, so any failure
  leaves it exactly as it was;
* ``django_migrations`` is not copied — the target keeps its own migration
  history, so a newer test schema is left intact;
* aborts if production has a table or column the target lacks (run
  ``migrate`` on the target first).

This copies real personal data. Unset both env vars afterwards.
"""
import os

import psycopg
from django.core.management.base import BaseCommand, CommandError
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

SKIP_TABLES = {"django_migrations"}


def _identity(url):
    info = conninfo_to_dict(url)
    return (info.get("host"), str(info.get("port") or 5432), info.get("dbname"))


def _columns(conn):
    """{table: [columns]} for every base table in the public schema."""
    rows = conn.execute(
        """
        select c.table_name::text, c.column_name::text
        from information_schema.columns c
        join information_schema.tables t
          on t.table_schema = c.table_schema and t.table_name = c.table_name
        where c.table_schema = 'public' and t.table_type = 'BASE TABLE'
        order by c.table_name, c.ordinal_position
        """
    ).fetchall()
    out = {}
    for table, column in rows:
        out.setdefault(table, []).append(column)
    return out


class Command(BaseCommand):
    help = "Overwrite this DB with a copy of PROD_DATABASE_URL (guarded, one-off)."

    def handle(self, *args, **opts):
        source_url = (os.environ.get("PROD_DATABASE_URL") or "").strip()
        target_url = (os.environ.get("DATABASE_URL") or "").strip()

        if os.environ.get("COPY_PROD_TO_TEST") != "yes" or not source_url:
            self.stdout.write(
                "copy_prod_to_test: COPY_PROD_TO_TEST=yes and PROD_DATABASE_URL "
                "not both set — skipping."
            )
            return
        if not target_url.startswith(("postgres://", "postgresql://")):
            raise CommandError("DATABASE_URL must point at Postgres.")
        if _identity(source_url) == _identity(target_url):
            raise CommandError("Source and target are the same database — refusing.")

        with psycopg.connect(source_url, client_encoding="utf8") as src, \
                psycopg.connect(target_url, client_encoding="utf8") as tgt:
            src.read_only = True
            src_cols, tgt_cols = _columns(src), _columns(tgt)

            tables = sorted(t for t in src_cols if t not in SKIP_TABLES)
            for table in tables:
                if table not in tgt_cols:
                    raise CommandError(f"Table {table} is missing in the target; run migrate first.")
                missing = set(src_cols[table]) - set(tgt_cols[table])
                if missing:
                    raise CommandError(f"Target {table} lacks columns {sorted(missing)}.")

            # One transaction on the target: wipe, refill, reset sequences.
            tgt.execute(
                sql.SQL("truncate {} restart identity cascade").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in tables)
                )
            )
            for table in tables:
                cols = sql.SQL(", ").join(sql.Identifier(c) for c in src_cols[table])
                ident = sql.Identifier(table)
                with src.cursor().copy(
                    sql.SQL("copy {} ({}) to stdout").format(ident, cols)
                ) as out, tgt.cursor().copy(
                    sql.SQL("copy {} ({}) from stdin").format(ident, cols)
                ) as inp:
                    for chunk in out:
                        inp.write(chunk)
                count = tgt.execute(sql.SQL("select count(*) from {}").format(ident)).fetchone()[0]
                self.stdout.write(f"  {table}: {count} rows")

            # COPY doesn't advance sequences; bring them past the copied ids.
            for table in tables:
                for col in tgt_cols[table]:
                    seq = tgt.execute(
                        "select pg_get_serial_sequence(%s, %s)", (f'"{table}"', col)
                    ).fetchone()[0]
                    if seq:
                        tgt.execute(
                            sql.SQL(
                                "select setval(%s, coalesce((select max({c}) from {t}), 0) + 1, false)"
                            ).format(c=sql.Identifier(col), t=sql.Identifier(table)),
                            (seq,),
                        )
            # leaving the `with` blocks commits the target transaction

        self.stdout.write(self.style.SUCCESS(
            f"copy_prod_to_test: copied {len(tables)} tables. Now unset "
            "PROD_DATABASE_URL and COPY_PROD_TO_TEST."
        ))
