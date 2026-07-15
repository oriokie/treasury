"""The member merge repointed 1 of the 11 relations pointing at Member.

Two of the other ten are PROTECT — those produced the ProtectedError 500 on
/members/duplicates/merge-all/. Five are SET_NULL, and those were the quieter
problem: the merge "succeeded" while cutting envelopes, loans, dependant links
and applications loose from the person they belonged to.

These tests pin all eleven. The relation-graph test is the one that matters
long-term: it fails the day someone adds a twelfth FK to Member and forgets it.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from members.models import Member, MemberAlias, PossibleDuplicate
from members.services.matching import (MemberMergeConflict, merge_conflicts,
                                       merge_members)

from benevolent.models import (BenevolentEventType, BenevolentScheme,
                               SchemeDependant, SchemeMembership, SchemePolicy)
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class SchemeFixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_m9", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

        fund = Department.objects.create(name="M9 Fund", slug="m9-fund",
                                         fund_type=Department.FundType.LOCAL)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent", code="M9B", fund=fund, created_by=self.treasurer)
        BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BER")
        policy = SchemePolicy.objects.create(
            scheme=self.scheme, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            registration_required=True, registration_fee=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("50000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(self.scheme, user=self.treasurer)

    def enrol(self, member, scheme=None):
        return reg_svc.register(scheme or self.scheme, member,
                                joined_on=TODAY - dt.timedelta(days=400),
                                user=self.treasurer)


class RelationGraphTests(TestCase):
    def test_every_relation_is_repointed_or_deliberately_folded(self):
        """No relation may be silently ignored — that was the whole bug."""
        from members.services.matching import _FOLDED, _member_relations

        walked = {r.related_model._meta.label for r in _member_relations()}
        for rel in Member._meta.related_objects:
            label = rel.related_model._meta.label
            if rel.related_model.__name__.startswith("Historical"):
                continue
            self.assertTrue(
                label in walked or label in _FOLDED,
                f"{label} points at Member but the merge neither repoints nor "
                f"folds it — it would be orphaned or would block the delete.")


class ProtectedRelationTests(SchemeFixture):
    """The two PROTECT relations: the reported crash."""

    def test_same_scheme_membership_refuses_with_a_reason(self):
        a = Member.objects.create(name="DAVID KAMAU")
        b = Member.objects.create(name="KAMAU DAVID")
        self.enrol(a)
        self.enrol(b)

        reasons = merge_conflicts(a, b)
        self.assertTrue(reasons, "same-scheme registration must be a conflict")

        with self.assertRaises(MemberMergeConflict) as ctx:
            merge_members(a, b)
        self.assertIn("scheme", " ".join(ctx.exception.reasons).lower())

        # refused BEFORE writing: nothing moved, nothing deleted
        self.assertEqual(Member.objects.filter(pk__in=[a.pk, b.pk]).count(), 2)
        self.assertEqual(SchemeMembership.objects.filter(
            member__in=[a, b]).count(), 2)

    def test_membership_in_a_different_scheme_is_repointed_not_blocked(self):
        fund2 = Department.objects.create(name="M9 Fund 2", slug="m9-fund-2",
                                          fund_type=Department.FundType.LOCAL)
        other = BenevolentScheme.objects.create(
            name="Second", code="M9C", fund=fund2, created_by=self.treasurer)
        BenevolentEventType.objects.create(
            scheme=other, name="Bereavement", code="BER")
        p2 = SchemePolicy.objects.create(
            scheme=other, effective_from=TODAY - dt.timedelta(days=900),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"), registration_required=True,
            registration_fee=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED,
            benefit_amount=Decimal("50000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        scheme_svc.publish_policy(p2, user=self.treasurer)
        scheme_svc.activate_scheme(other, user=self.treasurer)

        a = Member.objects.create(name="DAVID KAMAU")
        b = Member.objects.create(name="KAMAU DAVID")
        self.enrol(a)
        self.enrol(b, scheme=other)

        merge_members(a, b)

        self.assertEqual(SchemeMembership.objects.filter(member=a).count(), 2)
        self.assertFalse(Member.objects.filter(pk=b.pk).exists())

    def test_pledge_is_repointed_and_no_longer_blocks_the_delete(self):
        from pledges.models import Pledge, PledgeCampaign

        a = Member.objects.create(name="JANE WAIRIMU")
        b = Member.objects.create(name="WAIRIMU JANE")
        campaign = PledgeCampaign.objects.create(
            name="M9 Building", description="test")
        Pledge.objects.create(member=b, campaign=campaign,
                              amount=Decimal("500.00"))

        merge_members(a, b)     # used to raise ProtectedError

        self.assertEqual(Pledge.objects.filter(member=a).count(), 1)
        self.assertFalse(Member.objects.filter(pk=b.pk).exists())


class SetNullRelationTests(SchemeFixture):
    """The five SET_NULL relations: the silent data loss."""

    def test_envelope_follows_the_member(self):
        from envelopes.models import Envelope

        a = Member.objects.create(name="RUTH MOMANYI")
        b = Member.objects.create(name="MOMANYI RUTH")
        env = Envelope.objects.create(
            member=b, date=TODAY, receipt_no="E-9001",
            contributor_name="MOMANYI RUTH", recorded_by=self.treasurer)

        merge_members(a, b)

        env.refresh_from_db()
        self.assertEqual(env.member, a, "envelope was orphaned by the merge")

    def test_scheme_dependant_follows_the_member(self):
        a = Member.objects.create(name="PAUL OMONDI")
        b = Member.objects.create(name="OMONDI PAUL")
        m = self.enrol(b)
        dep = SchemeDependant.objects.create(
            membership=m, member=b, name="CHILD OMONDI",
            relationship=SchemeDependant.Relationship.CHILD)

        merge_members(a, b)

        dep.refresh_from_db()
        self.assertEqual(dep.member, a,
                         "dependant link was cut loose by the merge")


class NamesAndPhonesTests(TestCase):
    def test_absorbed_members_own_aliases_are_carried_over(self):
        a = Member.objects.create(name="JOHN OTIENO")
        b = Member.objects.create(name="JON OTIENO")     # different spelling
        MemberAlias.objects.create(member=b, name="J OTIENO")

        merge_members(a, b)

        names = set(a.aliases.values_list("name", flat=True))
        self.assertIn("JON OTIENO", names)      # the absorbed spelling
        self.assertIn("J OTIENO", names)        # and the alias it already had

    def test_both_phone_numbers_survive(self):
        a = Member.objects.create(name="MARY WANJIKU", phone="0722000111")
        b = Member.objects.create(name="WANJIKU MARY", phone="0733000222")

        merge_members(a, b)
        a.refresh_from_db()

        numbers = set(a.phones.values_list("number", flat=True))
        self.assertEqual(numbers, {"254722000111", "254733000222"})
        self.assertEqual(a.phones.filter(is_primary=True).count(), 1)


class BulkMergeViewTests(SchemeFixture):
    """One unmergeable pair must not 500 the page or abort the whole run."""

    def test_bulk_merge_skips_the_conflict_and_merges_the_rest(self):
        # pair 1 — blocked: both registered in the same scheme
        c1 = Member.objects.create(name="DAVID KAMAU")
        c2 = Member.objects.create(name="DAVID KAMAU",
                                   source=Member.Source.AUTO_BANK)
        self.enrol(c1)
        self.enrol(c2)
        PossibleDuplicate.objects.create(member=c2)

        # pair 2 — clean
        g1 = Member.objects.create(name="GRACE ATIENO")
        g2 = Member.objects.create(name="GRACE ATIENO",
                                   source=Member.Source.AUTO_BANK)
        PossibleDuplicate.objects.create(member=g2)

        resp = self.client.post(reverse("member_bulk_merge"), follow=True)

        self.assertEqual(resp.status_code, 200)     # was a ProtectedError 500
        self.assertTrue(Member.objects.filter(pk=c1.pk).exists())
        self.assertTrue(Member.objects.filter(pk=c2.pk).exists())   # refused
        self.assertFalse(Member.objects.filter(pk=g2.pk).exists())  # merged

        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("Not merged", text)
