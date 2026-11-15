import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class ConnectedAccount(models.Model):
    landlord = models.OneToOneField("agencies.LandlordProfile", on_delete=models.PROTECT, null=True, blank=True)
    agency = models.OneToOneField("agencies.Agency", on_delete=models.PROTECT, null=True, blank=True)
    stripe_account_id = models.CharField(max_length=255, unique=True)
    payouts_enabled = models.BooleanField(default=False)
    charges_enabled = models.BooleanField(default=False)
    country = models.CharField(max_length=2)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, agency__isnull=True)
                    | models.Q(landlord__isnull=True, agency__isnull=False)
                ),
                name="connected_account_one_party",
            )
        ]

    def __str__(self):
        return self.stripe_account_id


class Payment(models.Model):
    class Kind(models.TextChoices):
        DEPOSIT = "deposit"
        RENT = "rent"

    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REFUNDED = "refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey("tenancies.Agreement", on_delete=models.PROTECT)
    connected_account = models.ForeignKey(ConnectedAccount, on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)
    stripe_transfer_id = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.status != self.Status.SUCCEEDED or self.kind != self.Kind.RENT:
            return
        from agencies.models import PlanFeature
        from tenants.models import property_has_feature

        agreement = self.agreement
        prop = agreement.listing.property
        if not property_has_feature(prop, PlanFeature.OWNER_LEDGER):
            return
        if LedgerEntry.objects.filter(payment=self).exists():
            return
        occurred = timezone.localdate(self.paid_at) if self.paid_at else timezone.localdate()
        LedgerEntry.objects.create(
            property=prop,
            landlord=agreement.landlord,
            client=agreement.client,
            agreement=agreement,
            category=LedgerEntry.Category.RENT_RECEIVED,
            direction=LedgerEntry.Direction.IN,
            amount=self.amount,
            currency=self.currency,
            occurred_on=occurred,
            payment=self,
        )

    def __str__(self):
        return f"{self.kind} {self.amount} {self.currency}"


class Deposit(models.Model):
    class Holder(models.TextChoices):
        LANDLORD = "landlord"
        SCHEME = "scheme"

    agreement = models.OneToOneField("tenancies.Agreement", on_delete=models.PROTECT)
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT)
    holder = models.CharField(max_length=10, choices=Holder.choices)
    scheme = models.CharField(max_length=64, blank=True)
    scheme_reference = models.CharField(max_length=128, blank=True)
    protected_on = models.DateField(null=True, blank=True)
    prescribed_information_sent_on = models.DateField(null=True, blank=True)
    returned_on = models.DateField(null=True, blank=True)
    amount_returned = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(holder="scheme") | ~models.Q(scheme=""),
                name="scheme_named_when_scheme_holds_deposit",
            )
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from compliance.models import sync_deposit_tasks

        sync_deposit_tasks(self)

    def __str__(self):
        return f"{self.holder} deposit for {self.agreement_id}"


class RentMandate(models.Model):
    class Scheme(models.TextChoices):
        SEPA = "sepa"
        CARD = "card"

    class Status(models.TextChoices):
        PENDING = "pending"
        ACTIVE = "active"
        CANCELLED = "cancelled"

    agreement = models.OneToOneField("tenancies.Agreement", on_delete=models.PROTECT)
    scheme = models.CharField(max_length=8, choices=Scheme.choices)
    stripe_mandate_id = models.CharField(max_length=255, blank=True)
    collection_day = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)

    def __str__(self):
        return f"{self.scheme} {self.status}"


class RentCharge(models.Model):
    """One rent instalment the tenancy is expected to pay."""

    class Status(models.TextChoices):
        OPEN = "open"
        PARTIAL = "partial"
        PAID = "paid"
        WAIVED = "waived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agreement = models.ForeignKey(
        "tenancies.Agreement",
        related_name="rent_charges",
        on_delete=models.PROTECT,
    )
    due_on = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "due_on"],
                name="one_rent_charge_per_due_date",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0)
                & models.Q(amount_paid__gte=0)
                & models.Q(amount_paid__lte=models.F("amount")),
                name="rent_charge_payment_within_amount",
            ),
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenancies.models import Agreement, sync_arrears_matter
        from tenants.models import property_has_feature

        agreement = self.agreement
        if agreement.status not in {
            Agreement.Status.SIGNED,
            Agreement.Status.ACTIVE,
            Agreement.Status.ENDED,
        }:
            raise ValidationError("A rent charge follows a signed tenancy.")
        if self.currency != agreement.currency:
            raise ValidationError("The charge uses the tenancy currency.")
        prop = agreement.listing.property
        if not property_has_feature(prop, PlanFeature.ARREARS_WORKFLOW):
            raise ValidationError("This property's plan does not include the arrears workflow.")
        if self.status != self.Status.WAIVED:
            if self.amount_paid >= self.amount:
                self.status = self.Status.PAID
            elif self.amount_paid > 0:
                self.status = self.Status.PARTIAL
            else:
                self.status = self.Status.OPEN
        super().save(*args, **kwargs)
        sync_arrears_matter(agreement)

    def __str__(self):
        return f"{self.amount} due {self.due_on}"


class InsurancePartner(models.Model):
    """The authorised insurer. Elaiven introduces the owner and earns a commission."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=40, unique=True)
    jurisdictions = models.JSONField(default=list)

    def __str__(self):
        return self.name


class InsuranceProduct(models.Model):
    class Kind(models.TextChoices):
        BUILDINGS = "buildings"
        RENT_GUARANTEE = "rent_guarantee"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner = models.ForeignKey(InsurancePartner, related_name="products", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=120)
    notify_within_days = models.PositiveSmallIntegerField(null=True, blank=True)
    claim_requires_formal_notice = models.BooleanField(default=False)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["partner", "name"],
                name="one_product_name_per_partner",
            ),
            models.CheckConstraint(
                condition=models.Q(commission_rate__gte=0) & models.Q(commission_rate__lte=100),
                name="commission_rate_is_a_percentage",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.kind == self.Kind.RENT_GUARANTEE and self.notify_within_days is None:
            raise ValidationError("Rent guarantee names the claim window.")
        if self.kind == self.Kind.BUILDINGS and self.notify_within_days is not None:
            raise ValidationError("Buildings cover has no arrears claim window.")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Policy(models.Model):
    """Cover arranged for the legal owner. The operator is who sells it."""

    Kind = InsuranceProduct.Kind

    class Status(models.TextChoices):
        QUOTED = "quoted"
        BOUND = "bound"
        ACTIVE = "active"
        CANCELLED = "cancelled"
        EXPIRED = "expired"

    OPEN_STATUSES = {Status.QUOTED, Status.BOUND, Status.ACTIVE}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey("listings.Property", related_name="policies", on_delete=models.PROTECT)
    product = models.ForeignKey(InsuranceProduct, related_name="policies", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=Kind.choices, editable=False)
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
    agreement = models.ForeignKey(
        "tenancies.Agreement",
        null=True,
        blank=True,
        related_name="policies",
        on_delete=models.PROTECT,
    )
    policy_number = models.CharField(max_length=64, blank=True)
    start_on = models.DateField()
    end_on = models.DateField()
    premium = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUOTED)
    arranged_by = models.ForeignKey("accounts.MainUser", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, client__isnull=True)
                    | models.Q(landlord__isnull=True, client__isnull=False)
                ),
                name="policy_names_the_property_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(end_on__gte=models.F("start_on")) & models.Q(premium__gt=0),
                name="policy_term_and_premium",
            ),
            models.UniqueConstraint(
                fields=["agreement"],
                condition=models.Q(kind="rent_guarantee", status__in=["quoted", "bound", "active"]),
                name="one_open_rent_guarantee_per_tenancy",
            ),
            models.UniqueConstraint(
                fields=["property"],
                condition=models.Q(kind="buildings", status__in=["quoted", "bound", "active"]),
                name="one_open_buildings_policy_per_property",
            ),
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import operates_property, property_has_feature

        prop = self.property
        self.kind = self.product.kind
        covered = self.product.partner.jurisdictions
        if covered and prop.jurisdiction not in covered:
            raise ValidationError("This insurer does not cover this jurisdiction.")
        if self.landlord_id != prop.landlord_id or self.client_id != prop.client_id:
            raise ValidationError("The insured owner is the owner of the property.")
        if not operates_property(self.arranged_by, prop):
            raise ValidationError("Only the operator can arrange cover.")
        if not property_has_feature(prop, PlanFeature.INSURANCE):
            raise ValidationError("This property's plan does not include insurance.")
        if self.status != self.Status.QUOTED and not self.policy_number:
            raise ValidationError("A bound policy has a policy number.")
        if self.kind == self.Kind.RENT_GUARANTEE:
            if self.agreement_id is None:
                raise ValidationError("Rent guarantee covers one tenancy.")
            if self.agreement.listing.property_id != prop.pk:
                raise ValidationError("The tenancy is for another property.")
            if self.currency != self.agreement.currency:
                raise ValidationError("The premium uses the tenancy currency.")
        elif self.agreement_id:
            raise ValidationError("Buildings cover sits on the property.")
        super().save(*args, **kwargs)
        if self.status in {self.Status.BOUND, self.Status.ACTIVE}:
            if not Commission.objects.filter(policy=self).exists():
                Commission.objects.create(policy=self)
        if (
            self.status == self.Status.ACTIVE
            and property_has_feature(prop, PlanFeature.OWNER_LEDGER)
            and not LedgerEntry.objects.filter(
                policy=self,
                category=LedgerEntry.Category.INSURANCE_PREMIUM,
            ).exists()
        ):
            LedgerEntry.objects.create(
                property=prop,
                landlord=prop.landlord,
                client=prop.client,
                category=LedgerEntry.Category.INSURANCE_PREMIUM,
                direction=LedgerEntry.Direction.OUT,
                amount=self.premium,
                currency=self.currency,
                occurred_on=self.start_on,
                policy=self,
                recorded_by=self.arranged_by,
                memo=self.policy_number,
            )
        from compliance.models import sync_policy_task

        sync_policy_task(self)

    def __str__(self):
        return self.policy_number or self.kind


class Claim(models.Model):
    """A rent-guarantee claim opened from the arrears workflow."""

    class Status(models.TextChoices):
        DRAFT = "draft"
        NOTIFIED = "notified"
        ACCEPTED = "accepted"
        PAID = "paid"
        REJECTED = "rejected"
        OUT_OF_WINDOW = "out_of_window"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(Policy, related_name="claims", on_delete=models.PROTECT)
    agreement = models.ForeignKey("tenancies.Agreement", on_delete=models.PROTECT)
    matter = models.ForeignKey("tenancies.ArrearsMatter", related_name="claims", on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    notified_on = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.policy.kind != Policy.Kind.RENT_GUARANTEE:
            raise ValidationError("A claim follows a rent guarantee policy.")
        if self.agreement_id != self.policy.agreement_id:
            raise ValidationError("The claim follows the covered tenancy.")
        if self.matter.agreement_id != self.agreement_id:
            raise ValidationError("The arrears matter is for another tenancy.")
        if self.status == self.Status.NOTIFIED:
            if self.notified_on is None:
                raise ValidationError("The claim records when the insurer was told.")
            if self.policy.product.claim_requires_formal_notice:
                if not self.matter.actions.filter(action="formal_notice").exists():
                    raise ValidationError("The insurer requires a formal notice before a claim.")
            deadline = self.matter.opened_on + timedelta(days=self.policy.product.notify_within_days)
            if self.notified_on > deadline:
                self.status = self.Status.OUT_OF_WINDOW
        if self.status in {self.Status.ACCEPTED, self.Status.PAID} and self.notified_on is None:
            raise ValidationError("The claim records when the insurer was told.")
        if self.status == self.Status.PAID and (self.amount is None or self.paid_on is None):
            raise ValidationError("A paid claim has the amount and the date.")
        super().save(*args, **kwargs)
        if self.status != self.Status.PAID:
            return
        from agencies.models import PlanFeature
        from tenants.models import property_has_feature

        prop = self.policy.property
        if not property_has_feature(prop, PlanFeature.OWNER_LEDGER):
            return
        if LedgerEntry.objects.filter(claim=self).exists():
            return
        LedgerEntry.objects.create(
            property=prop,
            landlord=prop.landlord,
            client=prop.client,
            agreement=self.agreement,
            category=LedgerEntry.Category.INSURER_PAYOUT,
            direction=LedgerEntry.Direction.IN,
            amount=self.amount,
            currency=self.policy.currency,
            occurred_on=self.paid_on,
            claim=self,
            memo=self.policy.policy_number,
        )

    def __str__(self):
        return f"{self.status} {self.policy_id}"


class Commission(models.Model):
    """Elaiven's introducer commission. It stays off the owner's ledger."""

    class Status(models.TextChoices):
        EXPECTED = "expected"
        INVOICED = "invoiced"
        RECEIVED = "received"

    policy = models.OneToOneField(Policy, related_name="commission", on_delete=models.PROTECT)
    gross_premium = models.DecimalField(max_digits=10, decimal_places=2)
    elaiven_amount = models.DecimalField(max_digits=10, decimal_places=2)
    partner_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.EXPECTED)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(elaiven_amount__gte=0) & models.Q(partner_amount__gte=0),
                name="commission_amounts_non_negative",
            )
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            premium = self.policy.premium
            rate = self.policy.product.commission_rate
            elaiven = (premium * rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            self.gross_premium = premium
            self.elaiven_amount = elaiven
            self.partner_amount = premium - elaiven
        if self.elaiven_amount + self.partner_amount != self.gross_premium:
            raise ValidationError("The commission split has to add up to the premium.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.elaiven_amount} on {self.policy_id}"


class OwnerStatement(models.Model):
    """A period of the owner's ledger, locked once it is issued."""

    class Status(models.TextChoices):
        DRAFT = "draft"
        ISSUED = "issued"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        "agencies.LandlordProfile",
        null=True,
        blank=True,
        related_name="statements",
        on_delete=models.PROTECT,
    )
    client = models.ForeignKey(
        "agencies.Client",
        null=True,
        blank=True,
        related_name="statements",
        on_delete=models.PROTECT,
    )
    agency = models.ForeignKey(
        "agencies.Agency",
        null=True,
        blank=True,
        related_name="statements",
        on_delete=models.PROTECT,
    )
    owner_key = models.CharField(max_length=96, editable=False)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    opening_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_in = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_out = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    closing_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    file = models.FileField(upload_to="statements/", blank=True)
    prepared_by = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    issued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_key", "period_start", "period_end"],
                name="one_statement_per_owner_period",
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="statement_period_is_ordered",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(client__isnull=False, landlord__isnull=True, agency__isnull=False)
                    | models.Q(landlord__isnull=False, client__isnull=True, agency__isnull=True)
                    | models.Q(landlord__isnull=False, client__isnull=True, agency__isnull=False)
                ),
                name="statement_has_one_owner",
            ),
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature, operator_has_feature

        if self._state.adding and self.status == self.Status.ISSUED:
            raise ValidationError("Issue the statement once its lines are collected.")
        if not self._state.adding:
            previous = OwnerStatement.objects.filter(pk=self.pk).values("status").first()
            if previous and previous["status"] == self.Status.ISSUED:
                raise ValidationError("An issued statement stays as it was sent.")
        self._check_owner()
        covered = operator_has_feature(
            PlanFeature.OWNER_LEDGER,
            landlord_id=None if self.agency_id else self.landlord_id,
            agency_id=self.agency_id,
        )
        if not covered:
            raise ValidationError("This operator's plan does not include the owner ledger.")
        if self.status == self.Status.ISSUED and self.prepared_by_id is None:
            raise ValidationError("An issued statement names who prepared it.")
        self.owner_key = self._owner_key()
        super().save(*args, **kwargs)

    def _check_owner(self):
        client_shape = self.client_id and not self.landlord_id and self.agency_id
        own_shape = self.landlord_id and not self.client_id and not self.agency_id
        managed_shape = self.landlord_id and not self.client_id and self.agency_id
        if not (client_shape or own_shape or managed_shape):
            raise ValidationError("A statement belongs to one owner.")
        if self.client_id and self.client.agency_id != self.agency_id:
            raise ValidationError("This client belongs to another agency.")
        if self.period_end < self.period_start:
            raise ValidationError("The statement period ends after it starts.")

    def _owner_key(self):
        if self.client_id:
            return f"client:{self.client_id}"
        if self.agency_id:
            return f"landlord:{self.landlord_id}:agency:{self.agency_id}"
        return f"landlord:{self.landlord_id}"

    def properties(self):
        from listings.models import Property

        if self.client_id:
            return Property.objects.filter(client_id=self.client_id, agency_id=self.agency_id)
        if self.agency_id:
            return Property.objects.filter(landlord_id=self.landlord_id, agency_id=self.agency_id)
        return Property.objects.filter(landlord_id=self.landlord_id, agency__isnull=True)

    def _open_entries(self):
        entries = LedgerEntry.objects.filter(
            property__in=self.properties(),
            statement__isnull=True,
            occurred_on__gte=self.period_start,
            occurred_on__lte=self.period_end,
        )
        if self.client_id:
            return entries.filter(client_id=self.client_id)
        return entries.filter(landlord_id=self.landlord_id, client__isnull=True)

    def issue(self, user):
        from agencies.models import assert_can_file

        if self.status == self.Status.ISSUED:
            raise ValidationError("This statement is already issued.")
        assert_can_file(user, landlord=self.landlord, agency=self.agency)
        if self._state.adding:
            self.save()
        totals = self._open_entries().aggregate(
            money_in=models.Sum("amount", filter=models.Q(direction=LedgerEntry.Direction.IN)),
            money_out=models.Sum("amount", filter=models.Q(direction=LedgerEntry.Direction.OUT)),
        )
        money_in = totals["money_in"] or Decimal("0.00")
        money_out = totals["money_out"] or Decimal("0.00")
        self.total_in = money_in
        self.total_out = money_out
        self.closing_balance = self.opening_balance + money_in - money_out
        self.status = self.Status.ISSUED
        self.prepared_by = user
        self.issued_at = timezone.now()
        self.save()
        self._open_entries().update(statement_id=self.pk)

    def __str__(self):
        return f"{self.owner_key} {self.period_start}"


class LedgerEntry(models.Model):
    """One movement on the legal owner's book for a property."""

    class Category(models.TextChoices):
        RENT_RECEIVED = "rent_received"
        MANAGEMENT_FEE = "management_fee"
        REPAIR = "repair"
        INSURANCE_PREMIUM = "insurance_premium"
        INSURER_PAYOUT = "insurer_payout"
        OWNER_PAYOUT = "owner_payout"

    class Direction(models.TextChoices):
        IN = "in"
        OUT = "out"

    MANUAL = {
        Category.MANAGEMENT_FEE,
        Category.REPAIR,
        Category.OWNER_PAYOUT,
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        "listings.Property",
        related_name="ledger_entries",
        on_delete=models.PROTECT,
    )
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
    agreement = models.ForeignKey(
        "tenancies.Agreement",
        null=True,
        blank=True,
        related_name="ledger_entries",
        on_delete=models.PROTECT,
    )
    category = models.CharField(max_length=24, choices=Category.choices)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    occurred_on = models.DateField()
    payment = models.OneToOneField(
        Payment,
        null=True,
        blank=True,
        related_name="ledger_entry",
        on_delete=models.PROTECT,
    )
    invoice = models.OneToOneField(
        "tenancies.ContractorInvoice",
        null=True,
        blank=True,
        related_name="ledger_entry",
        on_delete=models.PROTECT,
    )
    claim = models.OneToOneField(
        Claim,
        null=True,
        blank=True,
        related_name="ledger_entry",
        on_delete=models.PROTECT,
    )
    policy = models.ForeignKey(
        Policy,
        null=True,
        blank=True,
        related_name="ledger_entries",
        on_delete=models.PROTECT,
    )
    statement = models.ForeignKey(
        OwnerStatement,
        null=True,
        blank=True,
        related_name="lines",
        on_delete=models.PROTECT,
    )
    recorded_by = models.ForeignKey(
        "accounts.MainUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    memo = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(landlord__isnull=False, client__isnull=True)
                    | models.Q(landlord__isnull=True, client__isnull=False)
                ),
                name="ledger_line_has_one_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="ledger_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        category__in=["rent_received", "insurer_payout"],
                        direction="in",
                    )
                    | models.Q(
                        category__in=[
                            "management_fee",
                            "repair",
                            "insurance_premium",
                            "owner_payout",
                        ],
                        direction="out",
                    )
                ),
                name="ledger_direction_matches_category",
            ),
            models.UniqueConstraint(
                fields=["policy"],
                condition=models.Q(category="insurance_premium"),
                name="one_premium_entry_per_policy",
            ),
        ]
        indexes = [
            models.Index(fields=["property", "occurred_on"], name="ledger_property_date")
        ]

    def save(self, *args, **kwargs):
        from agencies.models import PlanFeature
        from tenants.models import operates_property, property_has_feature

        stored = None
        if not self._state.adding:
            stored = LedgerEntry.objects.filter(pk=self.pk).values("statement_id").first()
        if stored and stored["statement_id"]:
            raise ValidationError("An issued statement keeps its lines.")
        prop = self.property
        if self.landlord_id != prop.landlord_id or self.client_id != prop.client_id:
            raise ValidationError("The ledger line belongs to the property's owner.")
        if self.agreement_id:
            if self.agreement.listing.property_id != prop.pk:
                raise ValidationError("The tenancy is for another property.")
            if self.currency != self.agreement.currency:
                raise ValidationError("The ledger line uses the tenancy currency.")
        if self.category in self.MANUAL and self.recorded_by_id is None:
            raise ValidationError("This ledger line names who recorded it.")
        if self.recorded_by_id and not operates_property(self.recorded_by, prop):
            raise ValidationError("Only the operator can record this ledger line.")
        if not property_has_feature(prop, PlanFeature.OWNER_LEDGER):
            raise ValidationError("This property's plan does not include the owner ledger.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.category} {self.amount}"
