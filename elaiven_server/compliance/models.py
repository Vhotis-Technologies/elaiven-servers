import calendar
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models


class Rule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    jurisdiction = models.CharField(max_length=2)
    code = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=255)
    source_url = models.URLField(blank=True)
    payload = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["jurisdiction", "code", "version"],
                name="one_version_of_a_rule",
            )
        ]

    def __str__(self):
        return f"{self.jurisdiction} {self.code} v{self.version}"


class AppliedRule(models.Model):
    agreement = models.ForeignKey("tenancies.Agreement", on_delete=models.PROTECT)
    rule = models.ForeignKey(Rule, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "rule"],
                name="rule_applied_once",
            )
        ]

    def __str__(self):
        return f"{self.rule_id} on {self.agreement_id}"


class FormalNotice(models.Model):
    class Kind(models.TextChoices):
        RENT_SETTING = "rent_setting"
        TERMINATION = "termination"
        BREACH = "breach"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey("tenancies.Agreement", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    rule = models.ForeignKey(Rule, on_delete=models.PROTECT)
    served_on = models.DateField()
    filed_on = models.DateField()
    file = models.FileField(upload_to="notices/")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(filed_on=models.F("served_on")),
                name="notice_filed_on_the_day_it_is_served",
            )
        ]

    def __str__(self):
        return f"{self.kind} {self.served_on}"


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    action = models.CharField(max_length=64)
    application = models.ForeignKey(
        "tenants.Application",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    agreement = models.ForeignKey(
        "tenancies.Agreement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.action


class PropertyCertificate(models.Model):
    """A safety or energy certificate whose expiry belongs on the calendar."""

    class Kind(models.TextChoices):
        BER = "ber"
        GAS_SAFETY = "gas_safety"
        EICR = "eicr"
        EPC = "epc"

    IE_KINDS = {Kind.BER}
    GB_KINDS = {Kind.GAS_SAFETY, Kind.EICR, Kind.EPC}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        "listings.Property",
        related_name="certificates",
        on_delete=models.PROTECT,
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    reference = models.CharField(max_length=64, blank=True)
    issued_on = models.DateField()
    expires_on = models.DateField()
    file = models.FileField(upload_to="certificates/", blank=True)
    recorded_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_on__gte=models.F("issued_on")),
                name="certificate_expires_on_or_after_issue",
            )
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import operates_property, property_has_feature

        allowed = self.IE_KINDS if self.property.jurisdiction == "IE" else self.GB_KINDS
        if self.kind not in allowed:
            raise ValidationError("This certificate does not apply in this jurisdiction.")
        if not operates_property(self.recorded_by, self.property):
            raise ValidationError("Only the operator can record a certificate.")
        if not property_has_feature(self.property, PlanFeature.COMPLIANCE_CALENDAR):
            raise ValidationError("This property's plan does not include the compliance calendar.")
        super().save(*args, **kwargs)
        sync_certificate_task(self)

    def __str__(self):
        return f"{self.kind} {self.expires_on}"


class ComplianceTask(models.Model):
    """One deadline on the operator's calendar."""

    class Code(models.TextChoices):
        RTB_REGISTRATION = "rtb_registration"
        RENT_REVIEW = "rent_review"
        CERTIFICATE_EXPIRY = "certificate_expiry"
        HAP_FORM = "hap_form"
        DEPOSIT_PROTECTION = "deposit_protection"
        PRESCRIBED_INFORMATION = "prescribed_information"
        INSURANCE_RENEWAL = "insurance_renewal"
        INSURER_NOTIFICATION = "insurer_notification"
        CONTRACTOR_LIABILITY = "contractor_liability"

    class Status(models.TextChoices):
        OPEN = "open"
        DONE = "done"
        DISMISSED = "dismissed"

    TENANCY_CODES = {
        Code.RTB_REGISTRATION,
        Code.RENT_REVIEW,
        Code.HAP_FORM,
        Code.DEPOSIT_PROTECTION,
        Code.PRESCRIBED_INFORMATION,
        Code.INSURER_NOTIFICATION,
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        "listings.Property",
        null=True,
        blank=True,
        related_name="compliance_tasks",
        on_delete=models.CASCADE,
    )
    agreement = models.ForeignKey(
        "tenancies.Agreement",
        null=True,
        blank=True,
        related_name="compliance_tasks",
        on_delete=models.CASCADE,
    )
    certificate = models.ForeignKey(
        PropertyCertificate,
        null=True,
        blank=True,
        related_name="compliance_tasks",
        on_delete=models.CASCADE,
    )
    contractor = models.ForeignKey(
        "tenancies.Contractor",
        null=True,
        blank=True,
        related_name="compliance_tasks",
        on_delete=models.CASCADE,
    )
    code = models.CharField(max_length=32, choices=Code.choices)
    due_on = models.DateField()
    source_key = models.CharField(max_length=96)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    completed_on = models.DateField(null=True, blank=True)
    completed_by = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_key", "due_on"],
                name="one_task_per_source_and_due",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(property__isnull=False, contractor__isnull=True)
                    | models.Q(property__isnull=True, contractor__isnull=False)
                ),
                name="task_anchors_property_or_contractor",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_on"], name="compliance_task_status_due")
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature, assert_can_file, operator_has_feature
        from tenants.models import operates_property

        if not self.source_key:
            raise ValidationError("A deadline needs a source.")
        if self.code == self.Code.CONTRACTOR_LIABILITY:
            if self.contractor_id is None or self.property_id is not None:
                raise ValidationError("Contractor liability is tracked on the contractor.")
        elif self.property_id is None or self.contractor_id is not None:
            raise ValidationError("This deadline belongs to a property.")
        if self.code in self.TENANCY_CODES and self.agreement_id is None:
            raise ValidationError("This deadline belongs to a tenancy.")
        if self.code == self.Code.CERTIFICATE_EXPIRY and self.certificate_id is None:
            raise ValidationError("A certificate deadline names the certificate.")
        if self.agreement_id and self.property_id:
            if self.agreement.listing.property_id != self.property_id:
                raise ValidationError("The tenancy is for another property.")
        if self.certificate_id and self.certificate.property_id != self.property_id:
            raise ValidationError("The certificate is for another property.")
        if self.status == self.Status.DONE and self.completed_on is None:
            raise ValidationError("A completed deadline has the day it was completed.")
        if self.status != self.Status.DONE and (
            self.completed_on is not None or self.completed_by_id is not None
        ):
            raise ValidationError("Only a completed deadline records who finished it.")
        if self.completed_by_id and self.property_id:
            if not operates_property(self.completed_by, self.property):
                raise ValidationError("Only the operator can complete this deadline.")
        if self.completed_by_id and self.contractor_id:
            assert_can_file(
                self.completed_by,
                landlord=self.contractor.landlord,
                agency=self.contractor.agency,
            )
        if self.property_id:
            covered = operator_has_feature(
                PlanFeature.COMPLIANCE_CALENDAR,
                landlord_id=self.property.landlord_id,
                agency_id=self.property.agency_id,
            )
        else:
            covered = operator_has_feature(
                PlanFeature.COMPLIANCE_CALENDAR,
                landlord_id=self.contractor.landlord_id,
                agency_id=self.contractor.agency_id,
            )
        if not covered:
            raise ValidationError("This property's plan does not include the compliance calendar.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} {self.due_on}"


def add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def payload_int(rule, key):
    if rule is None:
        return None
    value = rule.payload.get(key)
    if value is None or value == "":
        return None
    return int(value)


def current_rule(jurisdiction, code, on_date):
    return (
        Rule.objects.filter(jurisdiction=jurisdiction, code=code, effective_from__lte=on_date)
        .filter(models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=on_date))
        .order_by("-effective_from", "-version")
        .first()
    )


def _calendar_enabled(*, prop=None, contractor=None):
    from agencies.models import PlanFeature, operator_has_feature

    if prop is not None:
        return operator_has_feature(
            PlanFeature.COMPLIANCE_CALENDAR,
            landlord_id=prop.landlord_id,
            agency_id=prop.agency_id,
        )
    if contractor is not None:
        return operator_has_feature(
            PlanFeature.COMPLIANCE_CALENDAR,
            landlord_id=contractor.landlord_id,
            agency_id=contractor.agency_id,
        )
    return False


def dismiss_open_tasks(source_key):
    ComplianceTask.objects.filter(
        source_key=source_key,
        status=ComplianceTask.Status.OPEN,
    ).update(status=ComplianceTask.Status.DISMISSED)


def complete_open_tasks(source_key, on_date, due_on_or_before=None):
    rows = ComplianceTask.objects.filter(
        source_key=source_key,
        status=ComplianceTask.Status.OPEN,
    )
    if due_on_or_before is not None:
        rows = rows.filter(due_on__lte=due_on_or_before)
    rows.update(status=ComplianceTask.Status.DONE, completed_on=on_date)


def ensure_open_task(*, prop, code, due_on, source_key, agreement=None, certificate=None, contractor=None):
    if not _calendar_enabled(prop=prop, contractor=contractor):
        return None
    ComplianceTask.objects.filter(
        source_key=source_key,
        status=ComplianceTask.Status.OPEN,
    ).exclude(due_on=due_on).update(status=ComplianceTask.Status.DISMISSED)
    existing = ComplianceTask.objects.filter(source_key=source_key, due_on=due_on).first()
    if existing:
        if existing.status != ComplianceTask.Status.OPEN:
            existing.status = ComplianceTask.Status.OPEN
            existing.completed_on = None
            existing.completed_by = None
            existing.save()
        return existing
    return ComplianceTask.objects.create(
        property=prop,
        agreement=agreement,
        certificate=certificate,
        contractor=contractor,
        code=code,
        due_on=due_on,
        source_key=source_key,
    )


def sync_certificate_task(certificate):
    ensure_open_task(
        prop=certificate.property,
        certificate=certificate,
        code=ComplianceTask.Code.CERTIFICATE_EXPIRY,
        due_on=certificate.expires_on,
        source_key=f"certificate:{certificate.pk}",
    )


def sync_agreement_tasks(agreement):
    from agencies.models import PlanFeature
    from tenants.models import property_has_feature

    prop = agreement.listing.property
    if not property_has_feature(prop, PlanFeature.COMPLIANCE_CALENDAR):
        return
    sync_rtb_task(agreement, prop)
    sync_rent_review_task(agreement, prop)


def sync_rtb_task(agreement, prop):
    register_key = f"rtb:{agreement.pk}:register"
    renew_key = f"rtb:{agreement.pk}:renew"
    if agreement.jurisdiction != "IE":
        dismiss_open_tasks(register_key)
        dismiss_open_tasks(renew_key)
        return
    rule = current_rule(agreement.jurisdiction, "rtb_registration", agreement.start_date)
    register_days = payload_int(rule, "register_within_days")
    renew_days = payload_int(rule, "renew_after_days")
    if agreement.rtb_registered_on:
        complete_open_tasks(register_key, agreement.rtb_registered_on)
        if agreement.status == agreement.Status.ACTIVE and renew_days:
            ensure_open_task(
                prop=prop,
                agreement=agreement,
                code=ComplianceTask.Code.RTB_REGISTRATION,
                due_on=agreement.rtb_registered_on + timedelta(days=renew_days),
                source_key=renew_key,
            )
        else:
            dismiss_open_tasks(renew_key)
        return
    dismiss_open_tasks(renew_key)
    if register_days and agreement.status in {agreement.Status.SIGNED, agreement.Status.ACTIVE}:
        ensure_open_task(
            prop=prop,
            agreement=agreement,
            code=ComplianceTask.Code.RTB_REGISTRATION,
            due_on=agreement.start_date + timedelta(days=register_days),
            source_key=register_key,
        )
        return
    dismiss_open_tasks(register_key)


def sync_rent_review_task(agreement, prop=None):
    from agencies.models import PlanFeature
    from tenants.models import property_has_feature

    if prop is None:
        prop = agreement.listing.property
    if not property_has_feature(prop, PlanFeature.COMPLIANCE_CALENDAR):
        return
    source_key = f"rent_review:{agreement.pk}"
    if agreement.status != agreement.Status.ACTIVE:
        dismiss_open_tasks(source_key)
        return
    rule = current_rule(agreement.jurisdiction, "rent_review", agreement.start_date)
    months = payload_int(rule, "review_after_months")
    if not months:
        dismiss_open_tasks(source_key)
        return
    last = agreement.rent_reviews.order_by("-effective_on").first()
    if last is not None:
        complete_open_tasks(source_key, last.effective_on, due_on_or_before=last.effective_on)
        anchor = last.effective_on
    else:
        anchor = agreement.start_date
    ensure_open_task(
        prop=prop,
        agreement=agreement,
        code=ComplianceTask.Code.RENT_REVIEW,
        due_on=add_months(anchor, months),
        source_key=source_key,
    )


def sync_hap_task(arrangement):
    agreement = arrangement.agreement
    source_key = f"hap:{arrangement.pk}"
    if arrangement.form_sent_on:
        complete_open_tasks(source_key, arrangement.form_sent_on)
        return
    ensure_open_task(
        prop=agreement.listing.property,
        agreement=agreement,
        code=ComplianceTask.Code.HAP_FORM,
        due_on=arrangement.form_due_on,
        source_key=source_key,
    )


def sync_deposit_tasks(deposit):
    from agencies.models import PlanFeature
    from tenants.models import property_has_feature

    agreement = deposit.agreement
    prop = agreement.listing.property
    if not property_has_feature(prop, PlanFeature.COMPLIANCE_CALENDAR):
        return
    rule = current_rule(agreement.jurisdiction, "deposit_protection", agreement.start_date)
    _sync_deposit_deadline(
        prop=prop,
        agreement=agreement,
        days=payload_int(rule, "protect_within_days"),
        done_on=deposit.protected_on,
        source_key=f"deposit:{agreement.pk}",
        code=ComplianceTask.Code.DEPOSIT_PROTECTION,
    )
    _sync_deposit_deadline(
        prop=prop,
        agreement=agreement,
        days=payload_int(rule, "prescribed_within_days"),
        done_on=deposit.prescribed_information_sent_on,
        source_key=f"prescribed:{agreement.pk}",
        code=ComplianceTask.Code.PRESCRIBED_INFORMATION,
    )


def _sync_deposit_deadline(*, prop, agreement, days, done_on, source_key, code):
    if days is None:
        dismiss_open_tasks(source_key)
        return
    if done_on:
        complete_open_tasks(source_key, done_on)
        return
    ensure_open_task(
        prop=prop,
        agreement=agreement,
        code=code,
        due_on=agreement.start_date + timedelta(days=days),
        source_key=source_key,
    )


def sync_policy_task(policy):
    source_key = f"policy:{policy.pk}"
    if policy.status != policy.Status.ACTIVE:
        dismiss_open_tasks(source_key)
        return
    ensure_open_task(
        prop=policy.property,
        agreement=policy.agreement,
        code=ComplianceTask.Code.INSURANCE_RENEWAL,
        due_on=policy.end_on,
        source_key=source_key,
    )


def sync_contractor_task(contractor):
    source_key = f"contractor:{contractor.pk}"
    if contractor.liability_expires_on is None:
        dismiss_open_tasks(source_key)
        return
    ensure_open_task(
        prop=None,
        contractor=contractor,
        code=ComplianceTask.Code.CONTRACTOR_LIABILITY,
        due_on=contractor.liability_expires_on,
        source_key=source_key,
    )
