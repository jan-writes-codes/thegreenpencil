from django.db import migrations, models


# The lessons that used to be hard-coded in app.html, now rows a tutor can
# extend. Ids are kept so existing unlocks (ActiveLesson) and files stay put.
SEED = [
    ("a1-1", "A1", "speaking", "Greetings & introductions"),
    ("a1-2", "A1", "grammar", "The present simple"),
    ("a1-3", "A1", "liu", "Numbers, dates & telling time"),
    ("a1-4", "A1", "liu", "Everyday objects & places"),
    ("a2-1", "A2", "grammar", "Past simple & regular verbs"),
    ("a2-2", "A2", "grammar", "Comparatives & superlatives"),
    ("a2-3", "A2", "grammar", "‘Going to’ & future plans"),
    ("a2-4", "A2", "speaking", "Food, travel & directions"),
    ("b1-1", "B1", "grammar", "Present perfect in context"),
    ("b1-2", "B1", "grammar", "First & second conditionals"),
    ("b1-3", "B1", "grammar", "Reported speech basics"),
    ("b1-4", "B1", "liu", "Phrasal verbs that matter"),
    ("b2-1", "B2", "grammar", "The passive voice"),
    ("b2-2", "B2", "grammar", "Relative clauses"),
    ("b2-3", "B2", "grammar", "Narrative tenses"),
    ("b2-4", "B2", "writing", "Formal & informal register"),
    ("c1-1", "C1", "grammar", "Inversion & emphasis"),
    ("c1-2", "C1", "writing", "Hedging & academic style"),
    ("c1-3", "C1", "liu", "Nuanced & idiomatic vocabulary"),
    ("c1-4", "C1", "writing", "Discourse markers & cohesion"),
]


def seed(apps, schema_editor):
    Topic = apps.get_model("core", "CurriculumTopic")
    for i, (lid, level, skill, title) in enumerate(SEED):
        Topic.objects.get_or_create(
            lesson_id=lid, defaults={"level": level, "skill": skill, "title": title, "position": i}
        )


def unseed(apps, schema_editor):
    apps.get_model("core", "CurriculumTopic").objects.filter(
        lesson_id__in=[r[0] for r in SEED]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_cache_table"),
    ]

    operations = [
        migrations.CreateModel(
            name="CurriculumTopic",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lesson_id", models.CharField(max_length=20, unique=True)),
                ("level", models.CharField(choices=[("A1", "A1"), ("A2", "A2"), ("B1", "B1"), ("B2", "B2"), ("C1", "C1")], max_length=2)),
                ("skill", models.CharField(choices=[("listening", "Listening"), ("reading", "Reading"), ("grammar", "Grammar"), ("liu", "Language in Use"), ("writing", "Writing"), ("speaking", "Speaking")], max_length=10)),
                ("title", models.CharField(max_length=200)),
                ("position", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["level", "position", "id"]},
        ),
        migrations.RunPython(seed, unseed),
    ]
