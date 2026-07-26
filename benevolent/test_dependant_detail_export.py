"""The member directory's detailed export: dependants in their own columns.

The directory listed a household's dependants as one run of text in a single
cell. That reads well on screen and is close to useless in a spreadsheet — you
cannot sort by a dependant's name, filter on a relationship, or lift a column of
telephone numbers out of it for a call list.

The detailed section carries the same households in a shape a spreadsheet can
work with, and leaves out dependants who have died: a directory answers "who is
covered now", and including the dead overstates the household and would put a
bereaved family on a call list. That exclusion is the part worth guarding, since
it is a decision about people rather than about formatting.
"""
import datetime as dt

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department
from members.models import Member

from .models import BenevolentScheme, SchemeDependant, SchemeMembership
from .report_components import BenevolentMemberDependantDetailComponent
from .services import registry as reg_svc


class DependantDetailExportTests(TestCase):

    def setUp(self):
        self.treasurer = User.objects.create_user("tess-dir", password="office-pass-1")
        self.treasurer.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-dir",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Benevolent Scheme", code="BEN", fund=self.fund,
            created_by=self.treasurer, status=BenevolentScheme.Status.ACTIVE)

        member = Member.objects.create(name="Ruth Momanyi", phone="254790301470")
        self.membership = reg_svc.register(
            self.scheme, member, joined_on=dt.date.today() - dt.timedelta(days=90))
        # Member.save normalises the stored name, so compare against what was
        # actually saved rather than what was typed.
        self.member_name = self.membership.member.name

        self.living = SchemeDependant.objects.create(
            membership=self.membership, name="Grace Momanyi",
            relationship=SchemeDependant.Relationship.CHILD,
            phone="254711000111", active=True)
        self.deceased = SchemeDependant.objects.create(
            membership=self.membership, name="Peter Momanyi",
            relationship=SchemeDependant.Relationship.SPOUSE,
            active=True, died_on=dt.date.today() - dt.timedelta(days=10))
        self.withdrawn = SchemeDependant.objects.create(
            membership=self.membership, name="Silas Momanyi",
            relationship=SchemeDependant.Relationship.CHILD, active=False)

        self.client = Client()
        self.client.force_login(self.treasurer)

    def _section(self, filters=None):
        from core.reporting.context import ReportContext
        return BenevolentMemberDependantDetailComponent().render(
            ReportContext(start=None, end=None), filters or {})

    def _row(self):
        rows = [r for r in self._section().rows
                if r.cells["member"] == self.member_name]
        self.assertEqual(len(rows), 1, "Expected exactly one row for the member.")
        return rows[0]

    # -- who is listed -------------------------------------------------------

    def test_a_living_dependant_is_listed(self):
        cells = self._row().cells
        names = [v for k, v in cells.items() if k.endswith("_name")]
        self.assertIn("Grace Momanyi", names)

    def test_a_deceased_dependant_is_not_listed(self):
        cells = self._row().cells
        self.assertNotIn(
            "Peter Momanyi", list(cells.values()),
            "A dependant who has died is still on the directory, which "
            "overstates the household and would put a bereaved family on a "
            "call list.")

    def test_a_withdrawn_dependant_is_not_listed(self):
        self.assertNotIn("Silas Momanyi", list(self._row().cells.values()))

    def test_the_count_is_of_those_currently_covered(self):
        self.assertEqual(self._row().cells["dependant_count"], 1)

    def test_a_dependant_who_dies_drops_off(self):
        """The behaviour that matters is what happens when the record changes."""
        before = self._row().cells["dependant_count"]
        self.living.died_on = dt.date.today()
        self.living.save(update_fields=["died_on"])
        self.assertEqual(self._row().cells["dependant_count"], before - 1)

    # -- shape ---------------------------------------------------------------

    def test_each_dependant_gets_its_own_columns(self):
        keys = [c.key for c in self._section().columns]
        for key in ("dep1_name", "dep1_rel", "dep1_phone"):
            self.assertIn(key, keys)

    def test_the_columns_are_labelled_for_a_spreadsheet(self):
        labels = [c.label for c in self._section().columns]
        for label in ("Dependant 1", "Dependant 1 relationship",
                      "Dependant 1 phone", "Member phone"):
            self.assertIn(label, labels)

    def test_a_dependants_own_phone_is_carried(self):
        cells = self._row().cells
        self.assertEqual(cells["dep1_phone"], "254711000111")

    def test_a_phone_is_taken_from_the_linked_member_when_the_row_has_none(self):
        """A dependant who is also a registered member has a number on record."""
        linked = Member.objects.create(name="Joan Momanyi", phone="254722000222")
        SchemeDependant.objects.create(
            membership=self.membership, name="Joan Momanyi", member=linked,
            relationship=SchemeDependant.Relationship.CHILD, active=True)
        phones = [v for k, v in self._row().cells.items() if k.endswith("_phone")]
        self.assertIn("254722000222", phones)

    def test_a_dependant_with_no_phone_anywhere_is_blank_not_broken(self):
        SchemeDependant.objects.create(
            membership=self.membership, name="Mary Momanyi",
            relationship=SchemeDependant.Relationship.PARENT, active=True)
        phones = [v for k, v in self._row().cells.items() if k.endswith("_phone")]
        self.assertIn("", phones)

    def test_columns_follow_the_largest_household_present(self):
        """A scheme of couples should not carry twenty empty columns."""
        narrow = len([c for c in self._section().columns if c.key.endswith("_name")])
        for i in range(3):
            SchemeDependant.objects.create(
                membership=self.membership, name=f"Extra {i}",
                relationship=SchemeDependant.Relationship.CHILD, active=True)
        wider = len([c for c in self._section().columns if c.key.endswith("_name")])
        self.assertGreater(wider, narrow)

    def test_a_member_with_no_dependants_still_has_a_row(self):
        member = Member.objects.create(name="Solo Otieno", phone="254733000333")
        reg_svc.register(self.scheme, member, joined_on=dt.date.today())
        names = [r.cells["member"] for r in self._section().rows]
        self.assertIn(Member.objects.get(phone="254733000333").name, names)

    def test_an_outsized_household_overflows_rather_than_widening_forever(self):
        cap = BenevolentMemberDependantDetailComponent.MAX_DEPENDANT_COLUMNS
        for i in range(cap + 3):
            SchemeDependant.objects.create(
                membership=self.membership, name=f"Many {i}",
                relationship=SchemeDependant.Relationship.CHILD, active=True)
        section = self._section()
        named = len([c for c in section.columns if c.key.endswith("_name")])
        self.assertLessEqual(named, cap)
        self.assertIn("dep_overflow", [c.key for c in section.columns])
        self.assertIn("more than", section.note)

    # -- through the report ---------------------------------------------------

    def test_the_report_includes_the_detailed_section(self):
        body = self.client.get(
            reverse("engine_report", args=["benevolent_member_directory_report"])
        ).content.decode()
        self.assertIn("Dependant 1", body)

    def test_the_report_still_exports(self):
        for fmt in ("csv", "xlsx", "pdf"):
            with self.subTest(format=fmt):
                response = self.client.get(
                    reverse("engine_report",
                            args=["benevolent_member_directory_report"]),
                    {"export": fmt})
                self.assertEqual(response.status_code, 200)

    def test_the_csv_carries_a_dependant_column_per_dependant(self):
        import csv
        import io
        raw = self.client.get(
            reverse("engine_report", args=["benevolent_member_directory_report"]),
            {"export": "csv"}).content.decode()
        header = next((r for r in csv.reader(io.StringIO(raw))
                       if "Dependant 1" in r), None)
        self.assertIsNotNone(header, "The export has no per-dependant columns.")
        self.assertIn("Dependant 1 phone", header)

    def test_the_active_filter_still_applies(self):
        rows = self._section({"active": "0"}).rows
        self.assertNotIn(self.member_name, [r.cells["member"] for r in rows])

    def test_the_note_says_the_dead_are_not_counted(self):
        self.assertIn("Deceased", self._section().note)
