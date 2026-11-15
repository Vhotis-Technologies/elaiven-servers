import uuid

from django.core.exceptions import ValidationError
from django.db import models


class Property(models.Model):
    class Jurisdiction(models.TextChoices):
        IE = "IE", "Ireland"
        EN = "EN", "England"
        SC = "SC", "Scotland"
        WA = "WA", "Wales"
        NI = "NI", "Northern Ireland"

    class Arrangement(models.TextChoices):
        WHOLE = "whole", "Whole property"
        SHARED_ROOM = "shared_room", "Room in a shared property"
        OWNER_ROOM = "owner_room", "Room in the owner's home"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "agencies.LandlordProfile",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    client = models.ForeignKey(
        "agencies.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    agency = models.ForeignKey(
        "agencies.Agency",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    jurisdiction = models.CharField(max_length=2, choices=Jurisdiction.choices)
    arrangement = models.CharField(max_length=20, choices=Arrangement.choices)
    address_line_1 = models.CharField(max_length=255)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120)
    county = models.CharField(max_length=120, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    postcode = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)

    class Meta:
        verbose_name_plural = "properties"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, client__isnull=True, agency__isnull=True)
                    | models.Q(landlord__isnull=False, client__isnull=True, agency__isnull=False)
                    | models.Q(landlord__isnull=True, client__isnull=False, agency__isnull=False)
                ),
                name="property_has_one_owner",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import assert_can_file

        if self.client_id and self.client.agency_id != self.agency_id:
            raise ValidationError({"client": "This client belongs to another agency."})
        assert_can_file(self.created_by, landlord=self.landlord, agency=self.agency)
        super().save(*args, **kwargs)
        for threshold in ListingThreshold.objects.filter(listing__property_id=self.pk):
            threshold.save()

    def __str__(self):
        return self.address_line_1


class Room(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(Property, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)

    def __str__(self):
        return self.name


class Listing(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        PUBLISHED = "published"
        LET = "let"
        WITHDRAWN = "withdrawn"

    class Frequency(models.TextChoices):
        MONTHLY = "monthly"
        WEEKLY = "weekly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(Property, on_delete=models.PROTECT)
    room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.PROTECT)
    created_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    rent_amount = models.DecimalField(max_digits=10, decimal_places=2)
    rent_frequency = models.CharField(max_length=10, choices=Frequency.choices)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    available_from = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        from agencies.models import assert_can_file

        prop = self.property
        assert_can_file(self.created_by, landlord=prop.landlord, agency=prop.agency)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title


class ListingThreshold(models.Model):
    IE_SUPPORT = ("accepts_hap", "accepts_rent_supplement", "accepts_ras")
    UK_SUPPORT = ("accepts_universal_credit", "accepts_housing_benefit")

    listing = models.OneToOneField(Listing, on_delete=models.CASCADE)
    min_net_monthly_income = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    max_occupants = models.PositiveSmallIntegerField(null=True, blank=True)
    pets_allowed = models.BooleanField(default=True)
    work_reference_required = models.BooleanField(default=False)
    landlord_reference_required = models.BooleanField(default=False)
    guarantor_required = models.BooleanField(default=False)
    earliest_move_in = models.DateField(null=True, blank=True)
    latest_move_in = models.DateField(null=True, blank=True)
    accepts_hap = models.BooleanField(
        "Housing Assistance Payment (HAP)", null=True, blank=True
    )
    accepts_rent_supplement = models.BooleanField("Rent Supplement", null=True, blank=True)
    accepts_ras = models.BooleanField(
        "Rental Accommodation Scheme (RAS)", null=True, blank=True
    )
    accepts_universal_credit = models.BooleanField(
        "Universal Credit housing element", null=True, blank=True
    )
    accepts_housing_benefit = models.BooleanField("Housing Benefit", null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(accepts_hap=True) | models.Q(accepts_hap__isnull=True),
                name="hap_accepted_when_set",
            ),
            models.CheckConstraint(
                condition=models.Q(accepts_rent_supplement=True)
                | models.Q(accepts_rent_supplement__isnull=True),
                name="rent_supplement_accepted_when_set",
            ),
            models.CheckConstraint(
                condition=models.Q(accepts_ras=True) | models.Q(accepts_ras__isnull=True),
                name="ras_accepted_when_set",
            ),
            models.CheckConstraint(
                condition=models.Q(accepts_universal_credit=True)
                | models.Q(accepts_universal_credit__isnull=True),
                name="universal_credit_accepted_when_set",
            ),
            models.CheckConstraint(
                condition=models.Q(accepts_housing_benefit=True)
                | models.Q(accepts_housing_benefit__isnull=True),
                name="housing_benefit_accepted_when_set",
            ),
        ]

    def save(self, *args, **kwargs):
        ireland = self.listing.property.jurisdiction == Property.Jurisdiction.IE
        applicable = self.IE_SUPPORT if ireland else self.UK_SUPPORT
        inapplicable = self.UK_SUPPORT if ireland else self.IE_SUPPORT
        for name in applicable:
            setattr(self, name, True)
        for name in inapplicable:
            setattr(self, name, None)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Threshold for {self.listing_id}"
