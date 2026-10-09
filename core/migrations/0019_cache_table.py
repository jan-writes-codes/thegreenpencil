from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Rate-limit counters live in the database cache (settings.CACHES);
    # createcachetable is idempotent, so re-running is harmless.
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_errorcard_highlight"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
