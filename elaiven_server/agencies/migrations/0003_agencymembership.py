import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_admin_seats(apps, schema_editor):
    """The owner and every former admin become colleagues on the agency."""

    Agency = apps.get_model("agencies", "Agency")
    AdminProfile = apps.get_model("agencies", "AdminProfile")
    AgencyMembership = apps.get_model("agencies", "AgencyMembership")

    for agency in Agency.objects.select_related("owner"):
        AgencyMembership.objects.get_or_create(
            agency=agency,
            user_id=agency.owner.user_id,
            defaults={"id": uuid.uuid4(), "role": "owner"},
        )
    for admin in AdminProfile.objects.all():
        if AgencyMembership.objects.filter(agency_id=admin.agency_id, user_id=admin.user_id).exists():
            continue
        AgencyMembership.objects.create(
            id=uuid.uuid4(),
            agency_id=admin.agency_id,
            user_id=admin.user_id,
            role="agent",
        )


def restore_admin_profiles(apps, schema_editor):
    AgencyMembership = apps.get_model("agencies", "AgencyMembership")
    AdminProfile = apps.get_model("agencies", "AdminProfile")
    for seat in AgencyMembership.objects.filter(role="agent"):
        AdminProfile.objects.get_or_create(
            agency_id=seat.agency_id,
            user_id=seat.user_id,
            defaults={"id": uuid.uuid4()},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("agencies", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AgencyMembership",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(choices=[("owner", "Owner"), ("agent", "Agent")], max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("agency", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="memberships", to="agencies.agency")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="agencymembership",
            constraint=models.UniqueConstraint(fields=("agency", "user"), name="one_seat_per_agency_user"),
        ),
        migrations.AddConstraint(
            model_name="agencymembership",
            constraint=models.UniqueConstraint(condition=models.Q(("role", "owner")), fields=("agency",), name="one_owner_per_agency"),
        ),
        migrations.RunPython(copy_admin_seats, restore_admin_profiles),
        migrations.DeleteModel(name="AdminProfile"),
    ]
