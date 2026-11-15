import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction


class LandlordTypes(models.TextChoices):
    """Simple types for landlords like for individual or company operations"""

    INDIVIDUAL = "individual"
    COMPANY = "company"


class LandlordProfile(models.Model):
    """An individual landlord, or the organisation account of a company or housing estate.

    An individual is the user on this row. A company has no login of its own:
    people sit on LandlordMembership, and the owner seat can be transferred.
    """

    user = models.OneToOneField(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    type = models.CharField(
        max_length=10, choices=LandlordTypes.choices, default=LandlordTypes.INDIVIDUAL
    )
    name = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=255, blank=True, null=True)
    postcode = models.CharField(max_length=255, blank=True, null=True)
    country = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(type=LandlordTypes.INDIVIDUAL, user__isnull=False)
                    | (
                        models.Q(type=LandlordTypes.COMPANY, user__isnull=True)
                        & ~models.Q(name="")
                    )
                ),
                name="individual_landlord_has_user_company_has_name",
            )
        ]

    def __str__(self):
        if self.name:
            return self.name
        return self.user.email

    @classmethod
    def open_organisation(cls, *, owner, name, address=None, city=None, postcode=None, country=None):
        """Open a company landlord and give the founder the owner seat."""

        name = name.strip()
        if not name:
            raise ValidationError({"name": "A company landlord needs a name."})
        with transaction.atomic():
            profile = cls.objects.create(
                type=LandlordTypes.COMPANY,
                name=name,
                address=address,
                city=city,
                postcode=postcode,
                country=country,
            )
            LandlordMembership.objects.create(
                landlord=profile,
                user=owner,
                role=LandlordMembership.Role.OWNER,
            )
            return profile

    def add_member(self, user, role):
        if self.type != LandlordTypes.COMPANY:
            raise ValidationError("Seats belong to a company landlord.")
        if role == LandlordMembership.Role.OWNER:
            raise ValidationError("Transfer ownership to change the owner seat.")
        assert_seat_available(self)
        return LandlordMembership.objects.create(landlord=self, user=user, role=role)

    def transfer_ownership(self, user):
        """Move the owner seat. The previous owner stays on as an admin."""

        if self.type != LandlordTypes.COMPANY:
            raise ValidationError("Only a company landlord has an owner seat.")
        with transaction.atomic():
            memberships = LandlordMembership.objects.select_for_update().filter(landlord=self)
            current = memberships.get(role=LandlordMembership.Role.OWNER)
            if current.user_id == user.id:
                return current
            existing = memberships.filter(user=user).first()
            current.role = LandlordMembership.Role.ADMIN
            current.save(update_fields=["role", "updated_at"])
            if existing:
                existing.role = LandlordMembership.Role.OWNER
                existing.save(update_fields=["role", "updated_at"])
                return existing
            assert_seat_available(self)
            return LandlordMembership.objects.create(
                landlord=self,
                user=user,
                role=LandlordMembership.Role.OWNER,
            )



class LandlordMembership(models.Model):
    """A seat on a company landlord or housing estate.

    Owner, admin, and staff are colleagues. Any seat can read any other
    seat's performance. The owner role only transfers the organisation
    and approves spend past the property limit.
    """

    class Role(models.TextChoices):
        OWNER = "owner"
        ADMIN = "admin"
        STAFF = "staff"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile,
        related_name="memberships",
        on_delete=models.PROTECT,
    )
    user = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    role = models.CharField(max_length=10, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["landlord", "user"],
                name="one_seat_per_landlord_user",
            ),
            models.UniqueConstraint(
                fields=["landlord"],
                condition=models.Q(role="owner"),
                name="one_owner_per_landlord_organisation",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.landlord.type != LandlordTypes.COMPANY:
            raise ValidationError("Seats belong to a company landlord.")
        super().save(*args, **kwargs)

    def colleagues(self):
        """Every other seat on this landlord."""

        return LandlordMembership.objects.filter(landlord=self.landlord).exclude(pk=self.pk)

    def __str__(self):
        return f"{self.role} {self.user_id} @ {self.landlord_id}"


class AgentProfile(models.Model):
    user = models.OneToOneField("accounts.MainUser", on_delete=models.PROTECT)
    address = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=255, blank=True, null=True)
    postcode = models.CharField(max_length=255, blank=True, null=True)
    country = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.email


class Agency(models.Model):
    """A letting agency. Every agent, including the owner, holds a seat."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    owner = models.ForeignKey(AgentProfile, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "agencies"

    def save(self, *args, **kwargs):
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            AgencyMembership.objects.get_or_create(
                agency=self,
                user_id=self.owner.user_id,
                defaults={"role": AgencyMembership.Role.OWNER},
            )

    def add_agent(self, user):
        """Give an agent a seat beside the people already on the agency."""

        if not AgentProfile.objects.filter(user=user).exists():
            raise ValidationError("An agent seat needs an agent profile.")
        if self.memberships.filter(user=user).exists():
            raise ValidationError("This person already has a seat.")
        assert_seat_available(self)
        return AgencyMembership.objects.create(
            agency=self,
            user=user,
            role=AgencyMembership.Role.AGENT,
        )

    def transfer_ownership(self, user):
        """Move the owner seat. The previous owner stays on as an agent."""

        profile = AgentProfile.objects.filter(user=user).first()
        if profile is None:
            raise ValidationError("The new owner needs an agent profile.")
        with transaction.atomic():
            memberships = self.memberships.select_for_update()
            current = memberships.get(role=AgencyMembership.Role.OWNER)
            if current.user_id == user.id:
                return current
            existing = memberships.filter(user=user).first()
            current.role = AgencyMembership.Role.AGENT
            current.save(update_fields=["role", "updated_at"])
            if existing:
                existing.role = AgencyMembership.Role.OWNER
                existing.save(update_fields=["role", "updated_at"])
                held = existing
            else:
                assert_seat_available(self)
                held = AgencyMembership.objects.create(
                    agency=self,
                    user=user,
                    role=AgencyMembership.Role.OWNER,
                )
            self.owner = profile
            self.save(update_fields=["owner"])
            return held

    def __str__(self):
        return self.name


class AgencyMembership(models.Model):
    """A seat on an agency.

    Owner and agent are colleagues. Any seat can read any other seat's
    performance. The owner role only transfers the agency and approves
    spend past the property limit.
    """

    class Role(models.TextChoices):
        OWNER = "owner"
        AGENT = "agent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agency = models.ForeignKey(
        Agency,
        related_name="memberships",
        on_delete=models.PROTECT,
    )
    user = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    role = models.CharField(max_length=10, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "user"],
                name="one_seat_per_agency_user",
            ),
            models.UniqueConstraint(
                fields=["agency"],
                condition=models.Q(role="owner"),
                name="one_owner_per_agency",
            ),
        ]

    def colleagues(self):
        """Every other seat on this agency."""

        return AgencyMembership.objects.filter(agency=self.agency).exclude(pk=self.pk)

    def __str__(self):
        return f"{self.role} {self.user_id} @ {self.agency_id}"


class Client(models.Model):
    """An owner on an agency's books. They have no login."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ManagementInstruction(models.Model):
    """The agency's mandate to operate a property. The owner stays on the property."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    property = models.OneToOneField("listings.Property", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.property.agency_id != self.agency_id:
            raise ValidationError("The mandate is for the agency that operates the property.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.agency_id} manages {self.property_id}"


def assert_can_file(user, *, landlord, agency):
    """The filer must hold a seat on the operator, not merely be some user."""

    if agency is not None:
        if not agency.memberships.filter(user=user).exists():
            raise ValidationError("Only a seat on this agency can file for it.")
        return
    if landlord is None:
        raise ValidationError("A property needs an owner.")
    if landlord.type == LandlordTypes.INDIVIDUAL:
        if landlord.user_id != user.pk:
            raise ValidationError("Only this landlord can file their own property.")
        return
    if not landlord.memberships.filter(user=user).exists():
        raise ValidationError("Only a member of this organisation can file for it.")


class Plan(models.Model):
    class Audience(models.TextChoices):
        LANDLORD = "landlord"
        AGENCY = "agency"

    code = models.CharField(max_length=40, unique=True)
    audience = models.CharField(max_length=10, choices=Audience.choices)
    unit_limit = models.PositiveIntegerField(null=True, blank=True)
    listing_limit = models.PositiveIntegerField(null=True, blank=True)
    seat_limit = models.PositiveIntegerField()

    def __str__(self):
        return self.code


class PlanFeature(models.Model):
    HOUSEMATE_BOARD = "housemate_board"
    SHARED_BILL_TRACKER = "shared_bill_tracker"
    CONDITION_REPORT = "condition_report"
    COMPLIANCE_CALENDAR = "compliance_calendar"
    OWNER_LEDGER = "owner_ledger"
    ARREARS_WORKFLOW = "arrears_workflow"
    INSURANCE = "insurance"
    CONTRACTOR_MANAGEMENT = "contractor_management"

    plan = models.ForeignKey(Plan, related_name="features", on_delete=models.CASCADE)
    code = models.CharField(max_length=40)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["plan", "code"], name="one_feature_per_plan")
        ]

    def __str__(self):
        return f"{self.plan_id} {self.code}"


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        PAST_DUE = "past_due"
        CANCELED = "canceled"

    landlord = models.OneToOneField(
        LandlordProfile,
        null=True,
        blank=True,
        related_name="subscription",
        on_delete=models.PROTECT,
    )
    agency = models.OneToOneField(
        Agency,
        null=True,
        blank=True,
        related_name="subscription",
        on_delete=models.PROTECT,
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, agency__isnull=True)
                    | models.Q(landlord__isnull=True, agency__isnull=False)
                ),
                name="subscription_one_operator",
            )
        ]

    def save(self, *args, **kwargs):
        if self.landlord_id and self.plan.audience != Plan.Audience.LANDLORD:
            raise ValidationError("A landlord subscription needs a landlord plan.")
        if self.agency_id and self.plan.audience != Plan.Audience.AGENCY:
            raise ValidationError("An agency subscription needs an agency plan.")
        super().save(*args, **kwargs)

    def allows(self, code):
        return (
            self.status == self.Status.ACTIVE
            and self.plan.features.filter(code=code).exists()
        )

    def __str__(self):
        return f"{self.plan_id} {self.status}"


def operator_key(landlord_id=None, agency_id=None):
    """A non-null key so unique constraints can include a platform default."""

    if agency_id:
        return f"agency:{agency_id}"
    if landlord_id:
        return f"landlord:{landlord_id}"
    return "platform"


def operator_has_feature(code, *, landlord_id=None, agency_id=None):
    """The agency subscription wins when an agency operates the stock."""

    if agency_id:
        subscription = Subscription.objects.filter(agency_id=agency_id).first()
    elif landlord_id:
        subscription = Subscription.objects.filter(landlord_id=landlord_id).first()
    else:
        return False
    return subscription is not None and subscription.allows(code)


def assert_seat_available(operator):
    """The plan caps every seat on the roster, including the owner."""

    try:
        subscription = operator.subscription
    except Subscription.DoesNotExist:
        return
    if operator.memberships.count() >= subscription.plan.seat_limit:
        raise ValidationError("This plan has no seats left.")


def can_view_performance(viewer, subject, *, agency=None, landlord=None):
    """Both people sit on the operator, so either may read the other's figures.

    An individual landlord has no colleagues: only that landlord can see
    their own figures.
    """

    if viewer is None or subject is None:
        return False
    if agency is not None:
        seated = set(
            agency.memberships.filter(user__in=[viewer, subject]).values_list("user_id", flat=True)
        )
        return viewer.pk in seated and subject.pk in seated
    if landlord is None:
        return False
    if landlord.type == LandlordTypes.INDIVIDUAL:
        return landlord.user_id is not None and viewer.pk == subject.pk == landlord.user_id
    seated = set(
        landlord.memberships.filter(user__in=[viewer, subject]).values_list("user_id", flat=True)
    )
    return viewer.pk in seated and subject.pk in seated


def is_operator_owner(user, *, landlord=None, agency=None):
    """The owner seat, used when spend goes past the property limit."""

    if agency is not None:
        return agency.memberships.filter(
            user=user,
            role=AgencyMembership.Role.OWNER,
        ).exists()
    if landlord is None:
        return False
    if landlord.type == LandlordTypes.INDIVIDUAL:
        return landlord.user_id == user.pk
    return landlord.memberships.filter(
        user=user,
        role=LandlordMembership.Role.OWNER,
    ).exists()
