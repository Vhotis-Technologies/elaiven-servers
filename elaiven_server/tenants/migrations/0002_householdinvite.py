import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("listings", "0001_initial"),
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="application",
            name="origin",
            field=models.CharField(
                choices=[("applicant", "Applied on Elaiven"), ("operator", "Brought in by an invite link")],
                default="applicant",
                max_length=12,
            ),
        ),
        migrations.AlterField(
            model_name="application",
            name="lead",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="application",
            constraint=models.CheckConstraint(
                condition=models.Q(("origin", "operator")) | models.Q(("lead__isnull", False)),
                name="applicant_origin_has_lead",
            ),
        ),
        migrations.AlterField(
            model_name="applicant",
            name="date_of_birth",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="applicant",
            name="housing_situation",
            field=models.CharField(
                blank=True,
                choices=[
                    ("renting", "Renting"),
                    ("family", "Family"),
                    ("owner", "Owner"),
                    ("notice_given", "Notice Given"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="applicant",
            name="employment_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("employed", "Employed"),
                    ("self_employed", "Self Employed"),
                    ("student", "Student"),
                    ("retired", "Retired"),
                    ("not_working", "Not Working"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="applicant",
            name="onboarded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="applicant",
            constraint=models.CheckConstraint(
                condition=models.Q(("onboarded_at__isnull", True)) | models.Q(("user__isnull", False)),
                name="onboarded_applicant_has_user",
            ),
        ),
        migrations.CreateModel(
            name="HouseholdInvite",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token", models.CharField(blank=True, max_length=64, unique=True)),
                ("sent_at", models.DateTimeField()),
                ("onboarded_at", models.DateTimeField(blank=True, null=True)),
                (
                    "application",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invite",
                        to="tenants.application",
                    ),
                ),
                (
                    "sent_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invites_sent",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="householdinvite",
            constraint=models.CheckConstraint(
                condition=models.Q(("onboarded_at__isnull", True))
                | models.Q(("onboarded_at__gte", models.F("sent_at"))),
                name="onboarded_after_invite_sent",
            ),
        ),
    ]
