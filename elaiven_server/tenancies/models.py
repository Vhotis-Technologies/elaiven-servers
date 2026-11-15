import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from listings.models import Listing, Property


class Viewing(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled"
        ATTENDED = "attended"
        NO_SHOW = "no_show"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey("tenants.Application", on_delete=models.CASCADE)
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)
    invited_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.status} {self.scheduled_for}"


class ProgressDecision(models.Model):
    class Reason(models.TextChoices):
        NOT_SUITABLE = "not_suitable_after_viewing"
        NO_SHOW = "no_show"
        CHOSE_OTHERS = "chose_others_to_proceed"
        WITHDREW = "applicant_withdrew"

    application = models.OneToOneField("tenants.Application", on_delete=models.CASCADE)
    proceed = models.BooleanField()
    reason = models.CharField(max_length=40, choices=Reason.choices, blank=True)
    decided_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(proceed=True) | ~models.Q(reason=""),
                name="progress_decline_has_reason",
            )
        ]

    def __str__(self):
        return f"proceed={self.proceed} {self.application_id}"


class AffordabilityResult(models.Model):
    class Result(models.TextChoices):
        PASS = "pass"
        FAIL = "fail"

    application = models.OneToOneField("tenants.Application", on_delete=models.CASCADE)
    verified_net_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    result = models.CharField(max_length=4, choices=Result.choices)
    rule_code = models.CharField(max_length=64)
    assessed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.result} {self.verified_net_monthly}"


class VerificationReport(models.Model):
    class Result(models.TextChoices):
        PASS = "pass"
        FAIL = "fail"

    application = models.OneToOneField("tenants.Application", on_delete=models.CASCADE)
    result = models.CharField(max_length=4, choices=Result.choices)
    reasons = models.JSONField(default=list)
    ready_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.result} {self.application_id}"


class LettingDecision(models.Model):
    class Outcome(models.TextChoices):
        OFFERED = "offered"
        DECLINED = "declined"

    class Reason(models.TextChoices):
        AFFORDABILITY = "affordability"
        DOCUMENTS = "documents"
        REFERENCES = "references"
        VIEWING = "viewing"
        CHOSE_ANOTHER = "chose_another_applicant"
        UNAVAILABLE = "property_no_longer_available"
        WITHDREW = "applicant_withdrew"

    application = models.OneToOneField("tenants.Application", on_delete=models.CASCADE)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    reason = models.CharField(max_length=40, choices=Reason.choices, blank=True)
    decided_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(outcome="declined") | ~models.Q(reason=""),
                name="letting_decline_has_reason",
            )
        ]

    def __str__(self):
        return f"{self.outcome} {self.application_id}"


class Agreement(models.Model):
    class Kind(models.TextChoices):
        TENANCY = "tenancy"
        LICENCE = "licence"

    class Status(models.TextChoices):
        DRAFT = "draft"
        AWAITING_SIGNATURE = "awaiting_signature"
        SIGNED = "signed"
        ACTIVE = "active"
        ENDED = "ended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.OneToOneField("tenants.Application", on_delete=models.PROTECT)
    listing = models.ForeignKey("listings.Listing", on_delete=models.PROTECT)
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
    kind = models.CharField(max_length=10, choices=Kind.choices)
    jurisdiction = models.CharField(max_length=2, choices=Property.Jurisdiction.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    rent_amount = models.DecimalField(max_digits=10, decimal_places=2)
    rent_frequency = models.CharField(max_length=10, choices=Listing.Frequency.choices)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    rtb_number = models.CharField(max_length=64, blank=True)
    rtb_registered_on = models.DateField(null=True, blank=True)
    signed_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, client__isnull=True, agency__isnull=True)
                    | models.Q(landlord__isnull=False, client__isnull=True, agency__isnull=False)
                    | models.Q(landlord__isnull=True, client__isnull=False, agency__isnull=False)
                ),
                name="agreement_has_one_owner",
            )
        ]

    def save(self, *args, **kwargs):
        prop = self.listing.property
        if (
            self.landlord_id != prop.landlord_id
            or self.client_id != prop.client_id
            or self.agency_id != prop.agency_id
        ):
            raise ValidationError("The agreement owner must match the property.")
        super().save(*args, **kwargs)
        from compliance.models import sync_agreement_tasks

        sync_agreement_tasks(self)

    def __str__(self):
        return f"{self.kind} {self.id}"


class AgreementParty(models.Model):
    class Role(models.TextChoices):
        TENANT = "tenant"
        LANDLORD = "landlord"
        AGENT = "agent"

    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE)
    user = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    role = models.CharField(max_length=10, choices=Role.choices)
    signed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "agreement parties"

    def __str__(self):
        return f"{self.role} {self.user_id}"


class ConditionReport(models.Model):
    """The state of the property when a tenancy starts or ends."""

    class Kind(models.TextChoices):
        MOVE_IN = "move_in"
        MOVE_OUT = "move_out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(Agreement, related_name="condition_reports", on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    recorded_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    notes = models.TextField(blank=True)
    recorded_on = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "kind"],
                name="one_condition_report_per_stage",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import operates_property, property_has_feature

        prop = self.agreement.listing.property
        if not property_has_feature(prop, PlanFeature.CONDITION_REPORT):
            raise ValidationError("This property's plan does not include condition reports.")
        if self.agreement.status not in {
            Agreement.Status.SIGNED,
            Agreement.Status.ACTIVE,
            Agreement.Status.ENDED,
        }:
            raise ValidationError("A condition report follows a signed tenancy.")
        is_party = self.agreement.agreementparty_set.filter(
            user=self.recorded_by,
            role=AgreementParty.Role.TENANT,
        ).exists()
        if not is_party and not operates_property(self.recorded_by, prop):
            raise ValidationError("Only a tenant of this tenancy or the operator can record it.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kind} {self.agreement_id}"


class ConditionReportItem(models.Model):
    class Condition(models.TextChoices):
        GOOD = "good"
        FAIR = "fair"
        POOR = "poor"
        DAMAGED = "damaged"

    report = models.ForeignKey(ConditionReport, related_name="items", on_delete=models.CASCADE)
    area = models.CharField(max_length=80)
    condition = models.CharField(max_length=10, choices=Condition.choices)
    note = models.TextField(blank=True)
    photo = models.FileField(upload_to="condition-reports/", blank=True)

    def __str__(self):
        return f"{self.area} {self.condition}"


class KeyHandover(models.Model):
    agreement = models.OneToOneField(Agreement, on_delete=models.PROTECT)
    key_count = models.PositiveSmallIntegerField()
    handed_at = models.DateTimeField()
    recorded_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)

    def __str__(self):
        return f"{self.key_count} keys for {self.agreement_id}"


class Case(models.Model):
    class Category(models.TextChoices):
        REPAIR = "repair"
        COMPLAINT = "complaint"
        RENT_QUERY = "rent_query"
        NEIGHBOUR = "neighbour"

    class Urgency(models.TextChoices):
        ROUTINE = "routine"
        URGENT = "urgent"
        EMERGENCY = "emergency"

    class Status(models.TextChoices):
        OPEN = "open"
        ACKNOWLEDGED = "acknowledged"
        ANSWERED = "answered"
        CLOSED = "closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(Agreement, on_delete=models.PROTECT)
    property = models.ForeignKey("listings.Property", on_delete=models.PROTECT)
    opened_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)
    category = models.CharField(max_length=20, choices=Category.choices)
    urgency = models.CharField(max_length=12, choices=Urgency.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.category} {self.status}"


class CaseMessage(models.Model):
    class Kind(models.TextChoices):
        USER = "user"
        ACKNOWLEDGEMENT = "acknowledgement"
        REMINDER = "reminder"

    case = models.ForeignKey(Case, on_delete=models.CASCADE)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.USER)
    author = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.kind} on {self.case_id}"


class CaseFile(models.Model):
    message = models.ForeignKey(CaseMessage, on_delete=models.CASCADE)
    file = models.FileField(upload_to="cases/")

    def __str__(self):
        return self.file.name


class HapArrangement(models.Model):
    """The council's HAP arrangement for one Irish tenancy."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.OneToOneField(
        Agreement,
        related_name="hap_arrangement",
        on_delete=models.PROTECT,
    )
    local_authority = models.CharField(max_length=120)
    reference = models.CharField(max_length=64, blank=True)
    tenant_contribution = models.DecimalField(max_digits=10, decimal_places=2)
    authority_portion = models.DecimalField(max_digits=10, decimal_places=2)
    form_due_on = models.DateField()
    form_sent_on = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tenant_contribution__gte=0) & models.Q(authority_portion__gte=0),
                name="hap_amounts_non_negative",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import operates_property, property_has_feature

        prop = self.agreement.listing.property
        if self.agreement.jurisdiction != Property.Jurisdiction.IE:
            raise ValidationError("HAP is an Irish arrangement.")
        if self.agreement.status not in {Agreement.Status.SIGNED, Agreement.Status.ACTIVE}:
            raise ValidationError("HAP follows a signed tenancy.")
        if not operates_property(self.recorded_by, prop):
            raise ValidationError("Only the operator can record a HAP arrangement.")
        if not property_has_feature(prop, PlanFeature.COMPLIANCE_CALENDAR):
            raise ValidationError("This property's plan does not include the compliance calendar.")
        super().save(*args, **kwargs)
        from compliance.models import sync_hap_task

        sync_hap_task(self)

    def __str__(self):
        return self.local_authority


class RentReview(models.Model):
    """A new rent, taking effect on a date the jurisdiction allows."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(Agreement, related_name="rent_reviews", on_delete=models.PROTECT)
    previous_amount = models.DecimalField(max_digits=10, decimal_places=2)
    new_amount = models.DecimalField(max_digits=10, decimal_places=2)
    effective_on = models.DateField()
    notice = models.ForeignKey(
        "compliance.FormalNotice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    recorded_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "effective_on"],
                name="one_rent_review_per_day",
            ),
            models.CheckConstraint(
                condition=models.Q(previous_amount__gt=0) & models.Q(new_amount__gt=0),
                name="rent_review_amounts_positive",
            ),
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from compliance.models import add_months, current_rule, payload_int, sync_rent_review_task
        from tenants.models import operates_property, property_has_feature

        agreement = self.agreement
        prop = agreement.listing.property
        if agreement.status not in {Agreement.Status.SIGNED, Agreement.Status.ACTIVE}:
            raise ValidationError("A rent review belongs to a current tenancy.")
        if self.effective_on < agreement.start_date:
            raise ValidationError("A review takes effect on or after the tenancy starts.")
        if not operates_property(self.recorded_by, prop):
            raise ValidationError("Only the operator can record a rent review.")
        if not property_has_feature(prop, PlanFeature.COMPLIANCE_CALENDAR):
            raise ValidationError("This property's plan does not include the compliance calendar.")
        rule = current_rule(agreement.jurisdiction, "rent_review", agreement.start_date)
        months = payload_int(rule, "review_after_months")
        if months:
            previous = (
                agreement.rent_reviews.exclude(pk=self.pk).order_by("-effective_on").first()
                if self.pk
                else None
            )
            anchor = previous.effective_on if previous else agreement.start_date
            earliest = add_months(anchor, months)
            if self.effective_on < earliest:
                raise ValidationError("This tenancy cannot be reviewed before the eligible date.")
        if rule and rule.payload.get("notice_required") and self.notice_id is None:
            raise ValidationError("This jurisdiction requires a rent review notice.")
        if self.notice_id and (
            self.notice.agreement_id != agreement.pk or self.notice.kind != "rent_setting"
        ):
            raise ValidationError("The notice is the rent setting for this tenancy.")
        super().save(*args, **kwargs)
        if self.effective_on <= timezone.localdate():
            latest = (
                agreement.rent_reviews.filter(effective_on__lte=timezone.localdate())
                .order_by("-effective_on")
                .first()
            )
            if latest is not None and latest.pk == self.pk:
                Agreement.objects.filter(pk=agreement.pk).update(rent_amount=self.new_amount)
        sync_rent_review_task(agreement, prop)

    def __str__(self):
        return f"{self.new_amount} from {self.effective_on}"


class ArrearsTemplate(models.Model):
    """A reminder or notice. No operator means the Elaiven default."""

    class Code(models.TextChoices):
        FRIENDLY = "friendly_reminder"
        SECOND = "second_reminder"
        FORMAL = "formal_arrears"

    landlord = models.ForeignKey(
        "agencies.LandlordProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    agency = models.ForeignKey(
        "agencies.Agency",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    operator_key = models.CharField(max_length=80, editable=False)
    jurisdiction = models.CharField(max_length=2, choices=Property.Jurisdiction.choices)
    code = models.CharField(max_length=32, choices=Code.choices)
    subject = models.CharField(max_length=120)
    body = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["operator_key", "jurisdiction", "code"],
                name="one_arrears_template_per_operator",
            ),
            models.CheckConstraint(
                condition=models.Q(landlord__isnull=True) | models.Q(agency__isnull=True),
                name="template_has_one_operator",
            ),
        ]

    def save(self, *args, **kwargs):
        from agencies.models import operator_key

        if self.landlord_id and self.agency_id:
            raise ValidationError("A template belongs to one operator.")
        self.operator_key = operator_key(self.landlord_id, self.agency_id)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} {self.jurisdiction}"


class ArrearsStep(models.Model):
    """When an overdue charge moves the workflow on."""

    class Action(models.TextChoices):
        REMIND = "remind"
        FORMAL_NOTICE = "formal_notice"
        NOTIFY_INSURER = "notify_insurer"

    landlord = models.ForeignKey(
        "agencies.LandlordProfile",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    agency = models.ForeignKey(
        "agencies.Agency",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    operator_key = models.CharField(max_length=80, editable=False)
    jurisdiction = models.CharField(max_length=2, choices=Property.Jurisdiction.choices)
    day_offset = models.PositiveSmallIntegerField()
    action = models.CharField(max_length=20, choices=Action.choices)
    template = models.ForeignKey(
        ArrearsTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["operator_key", "jurisdiction", "day_offset"],
                name="one_arrears_step_per_offset",
            ),
            models.CheckConstraint(
                condition=models.Q(landlord__isnull=True) | models.Q(agency__isnull=True),
                name="step_has_one_operator",
            ),
        ]

    def save(self, *args, **kwargs):
        from agencies.models import operator_key

        if self.landlord_id and self.agency_id:
            raise ValidationError("A step belongs to one operator.")
        self.operator_key = operator_key(self.landlord_id, self.agency_id)
        if self.action == self.Action.REMIND and self.template_id is None:
            raise ValidationError("A reminder step names its template.")
        if self.template_id and self.template.jurisdiction != self.jurisdiction:
            raise ValidationError("The template is for another jurisdiction.")
        if self.template_id and self.template.operator_key not in {self.operator_key, "platform"}:
            raise ValidationError("The template belongs to another operator.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.action} day {self.day_offset}"


class ArrearsMatter(models.Model):
    """The open arrears on one tenancy, from the first overdue charge."""

    class Status(models.TextChoices):
        OPEN = "open"
        RESOLVED = "resolved"

    class Stage(models.TextChoices):
        OPENED = "opened"
        REMINDER_1 = "reminder_1"
        REMINDER_2 = "reminder_2"
        FORMAL_NOTICE = "formal_notice"
        INSURANCE_NOTIFIED = "insurance_notified"
        ESCALATED = "escalated"
        RESOLVED = "resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(Agreement, related_name="arrears_matters", on_delete=models.PROTECT)
    opened_on = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    stage = models.CharField(max_length=24, choices=Stage.choices, default=Stage.OPENED)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agreement"],
                condition=models.Q(status="open"),
                name="one_open_arrears_matter",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import property_has_feature
        from transactions.models import RentCharge

        prop = self.agreement.listing.property
        if self.agreement.status not in {
            Agreement.Status.SIGNED,
            Agreement.Status.ACTIVE,
            Agreement.Status.ENDED,
        }:
            raise ValidationError("Arrears follow a signed tenancy.")
        if not property_has_feature(prop, PlanFeature.ARREARS_WORKFLOW):
            raise ValidationError("This property's plan does not include the arrears workflow.")
        if self.status == self.Status.OPEN:
            overdue = RentCharge.objects.filter(
                agreement=self.agreement,
                status__in=[RentCharge.Status.OPEN, RentCharge.Status.PARTIAL],
                due_on__lte=timezone.localdate(),
            ).exists()
            if not overdue:
                raise ValidationError("Arrears open when a rent charge is overdue.")
        else:
            self.stage = self.Stage.RESOLVED
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.stage} {self.agreement_id}"


class ArrearsAction(models.Model):
    """A step that was actually taken on an arrears matter."""

    class Action(models.TextChoices):
        REMIND = "remind"
        FORMAL_NOTICE = "formal_notice"
        NOTIFY_INSURER = "notify_insurer"

    matter = models.ForeignKey(ArrearsMatter, related_name="actions", on_delete=models.CASCADE)
    step = models.ForeignKey(ArrearsStep, null=True, blank=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=20, choices=Action.choices)
    notice = models.ForeignKey(
        "compliance.FormalNotice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    actor = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.matter.status != ArrearsMatter.Status.OPEN:
            raise ValidationError("This arrears matter is already resolved.")
        if self.action == self.Action.FORMAL_NOTICE and self.notice_id is None:
            raise ValidationError("A formal arrears step files a notice.")
        if self.action != self.Action.FORMAL_NOTICE and self.notice_id:
            raise ValidationError("Only a formal arrears step files a notice.")
        if self.step_id and self.step.action != self.action:
            raise ValidationError("The recorded action matches its step.")
        if self.notice_id and self.notice.agreement_id != self.matter.agreement_id:
            raise ValidationError("The notice is for another tenancy.")
        super().save(*args, **kwargs)
        if self.action == self.Action.NOTIFY_INSURER:
            _open_rent_claim(self)
        _advance_arrears_stage(self)

    def __str__(self):
        return f"{self.action} {self.matter_id}"


def _advance_arrears_stage(action):
    matter = action.matter
    if action.action == action.Action.REMIND:
        sent = matter.actions.filter(action=action.Action.REMIND).count()
        matter.stage = (
            ArrearsMatter.Stage.REMINDER_1 if sent <= 1 else ArrearsMatter.Stage.REMINDER_2
        )
    elif action.action == action.Action.FORMAL_NOTICE:
        matter.stage = ArrearsMatter.Stage.FORMAL_NOTICE
    elif action.action == action.Action.NOTIFY_INSURER:
        matter.stage = ArrearsMatter.Stage.INSURANCE_NOTIFIED
    matter.save()


def _open_rent_claim(action):
    from transactions.models import Claim, Policy

    from compliance.models import ComplianceTask, ensure_open_task

    agreement = action.matter.agreement
    policy = (
        Policy.objects.filter(
            agreement=agreement,
            kind=Policy.Kind.RENT_GUARANTEE,
            status=Policy.Status.ACTIVE,
        )
        .order_by("-start_on")
        .first()
    )
    if policy is None:
        return
    if not Claim.objects.filter(policy=policy, matter=action.matter).exists():
        Claim.objects.create(
            policy=policy,
            agreement=agreement,
            matter=action.matter,
            status=Claim.Status.DRAFT,
        )
    ensure_open_task(
        prop=agreement.listing.property,
        agreement=agreement,
        code=ComplianceTask.Code.INSURER_NOTIFICATION,
        due_on=action.matter.opened_on + timedelta(days=policy.product.notify_within_days),
        source_key=f"insurer:{action.matter.pk}",
    )


def sync_arrears_matter(agreement):
    from transactions.models import RentCharge

    today = timezone.localdate()
    overdue = RentCharge.objects.filter(
        agreement=agreement,
        status__in=[RentCharge.Status.OPEN, RentCharge.Status.PARTIAL],
        due_on__lte=today,
    ).order_by("due_on")
    matter = ArrearsMatter.objects.filter(
        agreement=agreement,
        status=ArrearsMatter.Status.OPEN,
    ).first()
    earliest = overdue.first()
    if earliest is None:
        if matter is not None:
            matter.status = ArrearsMatter.Status.RESOLVED
            matter.save()
        return
    if matter is None:
        ArrearsMatter.objects.create(agreement=agreement, opened_on=earliest.due_on)
        return
    if earliest.due_on < matter.opened_on:
        matter.opened_on = earliest.due_on
        matter.save()


class Contractor(models.Model):
    """A tradesperson on the operator's list."""

    class Trade(models.TextChoices):
        PLUMBER = "plumber"
        ELECTRICIAN = "electrician"
        GAS = "gas"
        BUILDER = "builder"
        OTHER = "other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "agencies.LandlordProfile",
        null=True,
        blank=True,
        related_name="contractors",
        on_delete=models.PROTECT,
    )
    agency = models.ForeignKey(
        "agencies.Agency",
        null=True,
        blank=True,
        related_name="contractors",
        on_delete=models.PROTECT,
    )
    name = models.CharField(max_length=255)
    trade = models.CharField(max_length=16, choices=Trade.choices)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    liability_expires_on = models.DateField(null=True, blank=True)
    approved = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, agency__isnull=True)
                    | models.Q(landlord__isnull=True, agency__isnull=False)
                ),
                name="contractor_belongs_to_one_operator",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature, operator_has_feature

        if bool(self.landlord_id) == bool(self.agency_id):
            raise ValidationError("A contractor belongs to one operator.")
        if not operator_has_feature(
            PlanFeature.CONTRACTOR_MANAGEMENT,
            landlord_id=self.landlord_id,
            agency_id=self.agency_id,
        ):
            raise ValidationError("This operator's plan does not include contractor management.")
        super().save(*args, **kwargs)
        from compliance.models import sync_contractor_task

        sync_contractor_task(self)

    def __str__(self):
        return self.name


class PropertySpendLimit(models.Model):
    """How much the operator can commit on one property without a further approval."""

    property = models.OneToOneField(
        "listings.Property",
        related_name="spend_limit",
        on_delete=models.CASCADE,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="spend_limit_positive",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import property_has_feature

        if not property_has_feature(self.property, PlanFeature.CONTRACTOR_MANAGEMENT):
            raise ValidationError("This property's plan does not include contractor management.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.amount} {self.currency}"


class WorkQuote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey("listings.Property", related_name="work_quotes", on_delete=models.PROTECT)
    case = models.ForeignKey(Case, null=True, blank=True, on_delete=models.PROTECT)
    contractor = models.ForeignKey(Contractor, related_name="quotes", on_delete=models.PROTECT)
    description = models.TextField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)

    class Status(models.TextChoices):
        REQUESTED = "requested"
        SUBMITTED = "submitted"
        APPROVED = "approved"
        REJECTED = "rejected"

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    requested_by = models.ForeignKey(
        "accounts.MainUser",
        related_name="work_quotes_requested",
        on_delete=models.PROTECT,
    )
    approved_by = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        related_name="work_quotes_approved",
        on_delete=models.PROTECT,
    )
    approved_on = models.DateTimeField(null=True, blank=True)
    owner_approved = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="work_quote_amount_positive")
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature, is_operator_owner
        from tenants.models import operates_property, property_has_feature

        prop = self.property
        if not property_has_feature(prop, PlanFeature.CONTRACTOR_MANAGEMENT):
            raise ValidationError("This property's plan does not include contractor management.")
        if self.case_id and self.case.property_id != prop.pk:
            raise ValidationError("The repair case is for another property.")
        if prop.agency_id:
            same_operator = self.contractor.agency_id == prop.agency_id
        else:
            same_operator = self.contractor.landlord_id == prop.landlord_id and self.contractor.agency_id is None
        if not same_operator:
            raise ValidationError("The contractor is on another operator's list.")
        if self._state.adding and not operates_property(self.requested_by, prop):
            raise ValidationError("Only the operator can request a quote.")
        if self.status == self.Status.APPROVED:
            if self.approved_by_id is None or not operates_property(self.approved_by, prop):
                raise ValidationError("Only the operator can approve a quote.")
            limit = PropertySpendLimit.objects.filter(property=prop).first()
            above_limit = limit is None or self.amount > limit.amount
            if limit is not None and self.currency != limit.currency:
                raise ValidationError("The quote currency matches the property spend limit.")
            if above_limit:
                if not self.owner_approved:
                    raise ValidationError("This quote is above the property spend limit.")
                if not is_operator_owner(
                    self.approved_by,
                    landlord=prop.landlord,
                    agency=prop.agency,
                ):
                    raise ValidationError("The operator's owner approves spend above the limit.")
            if self.approved_on is None:
                self.approved_on = timezone.now()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.amount} {self.currency}"


class WorkOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    quote = models.OneToOneField(WorkQuote, related_name="work_order", on_delete=models.PROTECT)

    class Status(models.TextChoices):
        INSTRUCTED = "instructed"
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"
        CANCELLED = "cancelled"

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INSTRUCTED)

    def save(self, *args, **kwargs):
        if self._state.adding and self.quote.status != WorkQuote.Status.APPROVED:
            raise ValidationError("A work order follows an approved quote.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.status} {self.quote_id}"


class ContractorInvoice(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received"
        APPROVED = "approved"
        PAID = "paid"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_order = models.OneToOneField(WorkOrder, related_name="invoice", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    file = models.FileField(upload_to="invoices/", blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEIVED)
    approved_by = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="contractor_invoice_amount_positive",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import operates_property, property_has_feature

        prop = self.work_order.quote.property
        if self.currency != self.work_order.quote.currency:
            raise ValidationError("The invoice uses the quote currency.")
        if self.status in {self.Status.APPROVED, self.Status.PAID}:
            if self.approved_by_id is None or not operates_property(self.approved_by, prop):
                raise ValidationError("Only the operator can approve an invoice.")
        super().save(*args, **kwargs)
        if self.status != self.Status.PAID:
            return
        if not property_has_feature(prop, PlanFeature.OWNER_LEDGER):
            return
        from transactions.models import LedgerEntry

        if LedgerEntry.objects.filter(invoice=self).exists():
            return
        LedgerEntry.objects.create(
            property=prop,
            landlord=prop.landlord,
            client=prop.client,
            category=LedgerEntry.Category.REPAIR,
            direction=LedgerEntry.Direction.OUT,
            amount=self.amount,
            currency=self.currency,
            occurred_on=timezone.localdate(),
            invoice=self,
            recorded_by=self.approved_by,
            memo=self.work_order.quote.description,
        )

    def __str__(self):
        return f"{self.amount} {self.currency}"
