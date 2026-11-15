import secrets
import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction


class TenantProfile(models.Model):
    user = models.OneToOneField("accounts.MainUser", on_delete=models.PROTECT)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)


    def __str__(self):
        return self.user.email


class Application(models.Model):
    class Origin(models.TextChoices):
        APPLICANT = "applicant", "Applied on Elaiven"
        OPERATOR = "operator", "Brought in by an invite link"

    class Status(models.TextChoices):
        SUBMITTED = "submitted"
        HELD = "held"
        SHORTLISTED = "shortlisted"
        INVITED = "invited"
        ATTENDED = "attended"
        NO_SHOW = "no_show"
        SELECTED = "selected"
        CHECKING = "checking"
        REPORTED = "reported"
        OFFERED = "offered"
        DECLINED = "declined"
        ACCEPTED = "accepted"
        WITHDRAWN = "withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey("listings.Listing", on_delete=models.PROTECT)
    origin = models.CharField(max_length=12, choices=Origin.choices, default=Origin.APPLICANT)
    lead = models.ForeignKey("accounts.MainUser", null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    adult_count = models.PositiveSmallIntegerField()
    child_count = models.PositiveSmallIntegerField(default=0)
    pets = models.BooleanField(default=False)
    desired_move_in = models.DateField()
    intended_months = models.PositiveSmallIntegerField(null=True, blank=True)
    threshold_met = models.BooleanField(null=True)
    threshold_failures = models.JSONField(default=list)
    threshold_evaluated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "lead"],
                name="one_application_per_lead_listing",
            ),
            models.CheckConstraint(
                condition=models.Q(origin="operator") | models.Q(lead__isnull=False),
                name="applicant_origin_has_lead",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.origin == self.Origin.APPLICANT and not self.lead_id:
            raise ValidationError({"lead": "Someone who applied on Elaiven already has an account."})
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.lead_id} on {self.listing_id}"


class Applicant(models.Model):
    class Gender(models.TextChoices):
        FEMALE = "female"
        MALE = "male"
        OTHER = "other"
        UNDISCLOSED = "undisclosed"

    class Housing(models.TextChoices):
        RENTING = "renting"
        FAMILY = "family"
        OWNER = "owner"
        NOTICE_GIVEN = "notice_given"

    class Employment(models.TextChoices):
        EMPLOYED = "employed"
        SELF_EMPLOYED = "self_employed"
        STUDENT = "student"
        RETIRED = "retired"
        NOT_WORKING = "not_working"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    user = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    is_lead = models.BooleanField(default=False)
    is_signatory = models.BooleanField(default=True)
    given_name = models.CharField(max_length=150)
    family_name = models.CharField(max_length=150)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, choices=Gender.choices, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    current_address = models.TextField(blank=True)
    housing_situation = models.CharField(max_length=20, choices=Housing.choices, blank=True, default="")
    employment_status = models.CharField(max_length=20, choices=Employment.choices, blank=True, default="")
    # Set when this person opened an invite link and got an account. Empty if they applied themselves.
    onboarded_at = models.DateTimeField(null=True, blank=True)
    job_title = models.CharField(max_length=150, blank=True)
    employer_name = models.CharField(max_length=255, blank=True)
    net_monthly_income = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    work_reference_available = models.BooleanField(default=False)
    landlord_reference_available = models.BooleanField(default=False)
    guarantor_available = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(onboarded_at__isnull=True) | models.Q(user__isnull=False),
                name="onboarded_applicant_has_user",
            )
        ]

    def __str__(self):
        return f"{self.given_name} {self.family_name}"


class HouseholdInvite(models.Model):
    """The link an agency or landlord sends to a household they already found.

    `sent_at` is when the link went out. `onboarded_at` is when the first person
    on that link got an account. Each applicant keeps their own `onboarded_at`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.OneToOneField(Application, related_name="invite", on_delete=models.CASCADE)
    token = models.CharField(max_length=64, unique=True, blank=True)
    sent_by = models.ForeignKey(
        "accounts.MainUser",
        related_name="invites_sent",
        on_delete=models.PROTECT,
    )
    sent_at = models.DateTimeField()
    onboarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(onboarded_at__isnull=True) | models.Q(onboarded_at__gte=models.F("sent_at")),
                name="onboarded_after_invite_sent",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = secrets.token_urlsafe(24)
        if self.application.origin != Application.Origin.OPERATOR:
            raise ValidationError({"application": "An invite link is only for a household the operator brought in."})
        if self.onboarded_at and self.onboarded_at < self.sent_at:
            raise ValidationError({"onboarded_at": "They cannot come onboard before the link is sent."})
        super().save(*args, **kwargs)

    def mark_onboarded(self, applicant, user, when):
        """This person opened the link and now has an account."""
        if applicant.application_id != self.application_id:
            raise ValidationError("This person is not on the invited household.")
        with transaction.atomic():
            applicant.user = user
            applicant.onboarded_at = when
            applicant.save()
            if applicant.is_lead:
                application = self.application
                application.lead = user
                application.save()
            if self.onboarded_at is None:
                self.onboarded_at = when
                self.save()

    def __str__(self):
        return f"Invite {self.token}"


class Evidence(models.Model):
    class Kind(models.TextChoices):
        ID = "id"
        WORK_REFERENCE = "work_reference"
        LANDLORD_REFERENCE = "landlord_reference"
        PAYSLIP = "payslip"
        BANK_STATEMENT = "bank_statement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    applicant = models.ForeignKey(Applicant, on_delete=models.CASCADE)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    file = models.FileField(upload_to="evidence/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "evidence"

    def __str__(self):
        return f"{self.kind} {self.applicant_id}"


class EvidenceCheck(models.Model):
    class Verdict(models.TextChoices):
        PASS = "pass"
        FAIL = "fail"

    evidence = models.OneToOneField(Evidence, on_delete=models.CASCADE)
    verdict = models.CharField(max_length=4, choices=Verdict.choices)
    reasons = models.JSONField(default=list)
    checked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.verdict} {self.evidence_id}"


class BankConnection(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        CONNECTED = "connected"
        FAILED = "failed"
        REVOKED = "revoked"

    applicant = models.ForeignKey(Applicant, on_delete=models.CASCADE)
    provider = models.CharField(max_length=80)
    external_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices)
    connected_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.provider} {self.status}"


class RightToRentCheck(models.Model):
    class Result(models.TextChoices):
        PASS = "pass"
        FAIL = "fail"

    applicant = models.OneToOneField(Applicant, on_delete=models.CASCADE)
    share_code = models.CharField(max_length=32, blank=True)
    result = models.CharField(max_length=4, choices=Result.choices)
    checked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.result} {self.applicant_id}"


class IncomeSource(models.Model):
    class Kind(models.TextChoices):
        EMPLOYMENT = "employment", "Employment"
        SELF_EMPLOYMENT = "self_employment", "Self-employment"
        HAP = "hap", "Housing Assistance Payment"
        RENT_SUPPLEMENT = "rent_supplement", "Rent Supplement"
        RAS = "ras", "Rental Accommodation Scheme"
        UNIVERSAL_CREDIT = "universal_credit", "Universal Credit housing element"
        HOUSING_BENEFIT = "housing_benefit", "Housing Benefit"
        OTHER = "other", "Other"

    IE_SUPPORT = {Kind.HAP, Kind.RENT_SUPPLEMENT, Kind.RAS}
    UK_SUPPORT = {Kind.UNIVERSAL_CREDIT, Kind.HOUSING_BENEFIT}
    SHARED = {Kind.EMPLOYMENT, Kind.SELF_EMPLOYMENT, Kind.OTHER}

    applicant = models.ForeignKey(Applicant, on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    revealed_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        jurisdiction = self.applicant.application.listing.property.jurisdiction
        support = self.IE_SUPPORT if jurisdiction == "IE" else self.UK_SUPPORT
        if self.kind not in self.SHARED | support:
            raise ValidationError(
                {"kind": "Choose a support scheme used in this listing's country."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kind} {self.amount_monthly}"


class HouseBoard(models.Model):
    """One board for a property. Tenants join; the operator can post updates."""

    property = models.OneToOneField("listings.Property", on_delete=models.CASCADE)
    opened_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Board for {self.property_id}"

    @classmethod
    def join(cls, *, property, user):
        profile = TenantProfile.objects.filter(user=user).first()
        if profile is None:
            raise ValidationError("A tenant profile is required to join the board.")
        board, _ = cls.objects.get_or_create(property=property)
        membership = board.memberships.filter(user=profile).first()
        if membership is None:
            return HouseBoardMembership.objects.create(board=board, user=profile)
        membership.withdrawn_at = None
        membership.save()
        return membership


class HouseBoardMembership(models.Model):
    board = models.ForeignKey(HouseBoard, related_name="memberships", on_delete=models.CASCADE)
    user = models.ForeignKey(TenantProfile, on_delete=models.PROTECT)
    consented_at = models.DateTimeField(auto_now_add=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["board", "user"],
                name="one_board_membership_per_user",
            )
        ]

    def save(self, *args, **kwargs):
        if self.withdrawn_at is None:
            prop = self.board.property
            if not property_has_board_perk(prop):
                raise ValidationError("This property's plan does not include the housemate board.")
            if not is_current_tenant(self.user.user, prop):
                raise ValidationError("Only a current tenant of this property can join the board.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user_id} on {self.board_id}"


class HousePost(models.Model):
    class Kind(models.TextChoices):
        NOTE = "note"
        BILL = "bill"
        NOTICE = "notice"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    board = models.ForeignKey(HouseBoard, on_delete=models.CASCADE)
    author = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        prop = self.board.property
        if not property_has_board_perk(prop):
            raise ValidationError("This property's plan does not include the housemate board.")
        if not can_post_on_board(self.author, self.board):
            raise ValidationError(
                "Only a current tenant member or the property operator can post."
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kind} {self.board_id}"


def property_has_feature(prop, code):
    from agencies.models import operator_has_feature

    if prop.agency_id:
        return operator_has_feature(code, agency_id=prop.agency_id)
    return operator_has_feature(code, landlord_id=prop.landlord_id)


def property_has_board_perk(prop):
    from agencies.models import PlanFeature

    return property_has_feature(prop, PlanFeature.HOUSEMATE_BOARD)


def can_post_on_board(author, board):
    prop = board.property
    if operates_property(author, prop):
        return True
    member = board.memberships.filter(user__user=author, withdrawn_at__isnull=True).exists()
    return member and is_current_tenant(author, prop)


def operates_property(user, prop):
    from agencies.models import assert_can_file

    try:
        assert_can_file(user, landlord=prop.landlord, agency=prop.agency)
    except ValidationError:
        return False
    return True


def is_current_tenant(user, prop):
    from tenancies.models import Agreement, AgreementParty

    return AgreementParty.objects.filter(
        user=user,
        role=AgreementParty.Role.TENANT,
        agreement__status=Agreement.Status.ACTIVE,
        agreement__listing__property=prop,
    ).exists()



class SharedHouseBillTracker(models.Model):
    """One household bill for a property. Each tenant's portion is a share."""

    class BillType(models.TextChoices):
        ELECTRICITY = "electricity"
        WATER = "water"
        GAS = "gas"
        INTERNET = "internet"
        TV = "tv"
        OTHER = "other"

    class BillStatus(models.TextChoices):
        PENDING = "pending"
        PAID = "paid"
        OVERDUE = "overdue"
        DISPUTED = "disputed"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey("listings.Property", on_delete=models.CASCADE)
    recorded_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    bill_type = models.CharField(max_length=20, choices=BillType.choices)
    bill_amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    bill_date = models.DateField(null=True, blank=True)
    bill_due_date = models.DateField(null=True, blank=True)
    bill_status = models.CharField(
        max_length=20,
        choices=BillStatus.choices,
        default=BillStatus.PENDING,
    )
    bill_reference = models.CharField(max_length=255, blank=True)
    bill_paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature

        if not property_has_feature(self.property, PlanFeature.SHARED_BILL_TRACKER):
            raise ValidationError("This property's plan does not include shared bill tracking.")
        if self._state.adding and not can_record_for_property(self.recorded_by, self.property):
            raise ValidationError("Only a current tenant or the operator can record a shared bill.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.bill_type} {self.bill_amount}"


class SharedHouseBillShare(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PAID = "paid"

    bill = models.ForeignKey(SharedHouseBillTracker, related_name="shares", on_delete=models.CASCADE)
    tenant = models.ForeignKey(TenantProfile, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bill", "tenant"],
                name="one_share_per_tenant_bill",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="bill_share_amount_positive",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and not is_current_tenant(self.tenant.user, self.bill.property):
            raise ValidationError("A share belongs to a current tenant of this property.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tenant_id} owes {self.amount}"


def can_record_for_property(user, prop):
    return operates_property(user, prop) or is_current_tenant(user, prop)

