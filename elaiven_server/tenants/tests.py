from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import MainUser
from agencies.models import LandlordProfile, LandlordTypes, Plan, PlanFeature, Subscription
from listings.models import Listing, Property
from tenancies.models import Agreement, AgreementParty, ConditionReport, ConditionReportItem
from tenants.models import (
    Applicant,
    Application,
    HouseholdInvite,
    SharedHouseBillShare,
    SharedHouseBillTracker,
    TenantProfile,
)


class SubscriptionFeatureTests(TestCase):
    def user(self, email):
        local = email.split("@", 1)[0]
        return MainUser.objects.create_user(
            email=email,
            password="test-pass-123",
            given_name=local,
            family_name="User",
        )

    def setUp(self):
        self.owner = self.user("ada@example.com")
        self.profile = LandlordProfile.objects.create(
            user=self.owner,
            type=LandlordTypes.INDIVIDUAL,
        )
        self.plan = Plan.objects.create(
            code="landlord-home",
            audience=Plan.Audience.LANDLORD,
            seat_limit=1,
        )
        self.subscription = Subscription.objects.create(landlord=self.profile, plan=self.plan)
        self.prop = Property.objects.create(
            landlord=self.profile,
            created_by=self.owner,
            jurisdiction=Property.Jurisdiction.IE,
            arrangement=Property.Arrangement.WHOLE,
            address_line_1="1 Main Street",
            city="Dublin",
            postcode="D01",
        )
        self.sam = self.user("sam@example.com")
        TenantProfile.objects.create(user=self.sam)
        self.agreement = self.occupy(self.sam, "Flat")

    def occupy(self, tenant, title):
        listing = Listing.objects.create(
            property=self.prop,
            created_by=self.owner,
            title=title,
            rent_amount="900.00",
            rent_frequency=Listing.Frequency.MONTHLY,
            deposit_amount="900.00",
            currency="EUR",
            available_from=date(2026, 12, 1),
        )
        application = Application.objects.create(
            listing=listing,
            lead=tenant,
            adult_count=1,
            desired_move_in=date(2026, 12, 1),
        )
        agreement = Agreement.objects.create(
            application=application,
            listing=listing,
            landlord=self.prop.landlord,
            agency=self.prop.agency,
            kind=Agreement.Kind.TENANCY,
            jurisdiction=self.prop.jurisdiction,
            status=Agreement.Status.ACTIVE,
            start_date=date(2026, 12, 1),
            rent_amount="900.00",
            rent_frequency=Listing.Frequency.MONTHLY,
            deposit_amount="900.00",
            currency="EUR",
        )
        AgreementParty.objects.create(
            agreement=agreement,
            user=tenant,
            role=AgreementParty.Role.TENANT,
        )
        return agreement

    def enable(self, code):
        PlanFeature.objects.create(plan=self.plan, code=code)

    def test_shared_bill_is_split_across_current_tenants(self):
        with self.assertRaises(ValidationError):
            SharedHouseBillTracker.objects.create(
                property=self.prop,
                recorded_by=self.owner,
                bill_type=SharedHouseBillTracker.BillType.ELECTRICITY,
                bill_amount="80.00",
                currency="EUR",
            )

        self.enable(PlanFeature.SHARED_BILL_TRACKER)
        bill = SharedHouseBillTracker.objects.create(
            property=self.prop,
            recorded_by=self.sam,
            bill_type=SharedHouseBillTracker.BillType.ELECTRICITY,
            bill_amount="80.00",
            currency="EUR",
        )
        jo = self.user("jo@example.com")
        TenantProfile.objects.create(user=jo)
        self.occupy(jo, "Room")
        SharedHouseBillShare.objects.create(
            bill=bill,
            tenant=jo.tenantprofile,
            amount="40.00",
        )
        SharedHouseBillShare.objects.create(
            bill=bill,
            tenant=self.sam.tenantprofile,
            amount="40.00",
        )
        stranger = self.user("stranger@example.com")
        TenantProfile.objects.create(user=stranger)
        with self.assertRaises(ValidationError):
            SharedHouseBillShare.objects.create(
                bill=bill,
                tenant=stranger.tenantprofile,
                amount="40.00",
            )

    def test_move_in_and_move_out_reports_follow_the_tenancy(self):
        with self.assertRaises(ValidationError):
            ConditionReport.objects.create(
                agreement=self.agreement,
                kind=ConditionReport.Kind.MOVE_IN,
                recorded_by=self.owner,
                recorded_on=date(2026, 12, 1),
            )

        self.enable(PlanFeature.CONDITION_REPORT)
        report = ConditionReport.objects.create(
            agreement=self.agreement,
            kind=ConditionReport.Kind.MOVE_IN,
            recorded_by=self.sam,
            recorded_on=date(2026, 12, 1),
            notes="Clean and empty",
        )
        ConditionReportItem.objects.create(
            report=report,
            area="Kitchen",
            condition=ConditionReportItem.Condition.GOOD,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConditionReport.objects.create(
                    agreement=self.agreement,
                    kind=ConditionReport.Kind.MOVE_IN,
                    recorded_by=self.owner,
                    recorded_on=date(2026, 12, 2),
                )
        stranger = self.user("nobody@example.com")
        with self.assertRaises(ValidationError):
            ConditionReport.objects.create(
                agreement=self.agreement,
                kind=ConditionReport.Kind.MOVE_OUT,
                recorded_by=stranger,
                recorded_on=date(2027, 6, 1),
            )
        self.agreement.status = Agreement.Status.ENDED
        self.agreement.save()
        ConditionReport.objects.create(
            agreement=self.agreement,
            kind=ConditionReport.Kind.MOVE_OUT,
            recorded_by=self.owner,
            recorded_on=date(2027, 6, 1),
        )


class InviteTrackingTests(SubscriptionFeatureTests):
    def listing(self):
        return Listing.objects.create(
            property=self.prop,
            created_by=self.owner,
            title="Harbour View",
            rent_amount="1400.00",
            rent_frequency=Listing.Frequency.MONTHLY,
            deposit_amount="1400.00",
            currency="EUR",
            available_from=date(2026, 12, 15),
        )

    def test_someone_who_applied_already_has_an_account(self):
        with self.assertRaises(ValidationError):
            Application.objects.create(
                listing=self.listing(),
                origin=Application.Origin.APPLICANT,
                lead=None,
                adult_count=1,
                desired_move_in=date(2026, 12, 15),
            )

    def test_an_invite_records_when_the_link_was_sent_and_when_they_joined(self):
        application = Application.objects.create(
            listing=self.listing(),
            origin=Application.Origin.OPERATOR,
            lead=None,
            adult_count=2,
            desired_move_in=date(2026, 12, 15),
        )
        lead = Applicant.objects.create(
            application=application,
            is_lead=True,
            given_name="Liam",
            family_name="Murphy",
            email="liam@example.com",
            phone="0870000001",
        )
        partner = Applicant.objects.create(
            application=application,
            given_name="Aoife",
            family_name="Murphy",
            email="aoife@example.com",
            phone="0870000002",
        )
        sent = timezone.now()
        invite = HouseholdInvite.objects.create(
            application=application,
            sent_by=self.owner,
            sent_at=sent,
        )
        self.assertTrue(invite.token)
        self.assertIsNone(invite.onboarded_at)

        joined = sent + timedelta(hours=3)
        account = self.user("liam@example.com")
        invite.mark_onboarded(lead, account, joined)

        invite.refresh_from_db()
        lead.refresh_from_db()
        partner.refresh_from_db()
        application.refresh_from_db()
        self.assertEqual(invite.onboarded_at, joined)
        self.assertEqual(lead.onboarded_at, joined)
        self.assertEqual(lead.user, account)
        self.assertEqual(application.lead, account)
        self.assertIsNone(partner.onboarded_at)

        later = joined + timedelta(days=1)
        second = self.user("aoife@example.com")
        invite.mark_onboarded(partner, second, later)
        invite.refresh_from_db()
        partner.refresh_from_db()
        self.assertEqual(partner.onboarded_at, later)
        self.assertEqual(invite.onboarded_at, joined)

    def test_a_self_applied_household_has_no_invite_link(self):
        application = Application.objects.create(
            listing=self.listing(),
            lead=self.sam,
            adult_count=1,
            desired_move_in=date(2026, 12, 15),
        )
        with self.assertRaises(ValidationError):
            HouseholdInvite.objects.create(
                application=application,
                sent_by=self.owner,
                sent_at=timezone.now(),
            )
