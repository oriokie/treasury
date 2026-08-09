"""What the supplier form does when the model refuses the record.

`Vendor.clean()` is the only thing standing between the register and an
eleventh spelling of "Mwangi Hardware", and it was written to do more than
refuse: it names the record already on file and invites the treasurer to use
it. That sentence is worth nothing if the screen that provokes it answers with
a server error, which is what `VendorSaveView` did — its single exit redirect
read `vendor.pk` on the create path, where a rejected save leaves `vendor`
None.

So these tests are about the *reply*, not the rule. The rule itself is pinned
by `VendorNameKeyTests` and by `Vendor.clean()`; what is pinned here is that a
rejected create comes back as a page with the message on it, that it comes back
carrying the existing supplier, and — the part easiest to break while fixing
the first part — that a rejected EDIT still returns to the profile it was
editing rather than being swept out to the register with everything else.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER

from .models import Vendor


class DuplicateSupplierGuardTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tess", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.existing = Vendor.objects.create(
            name="Mwangi Hardware Ltd", payment_terms=Vendor.Terms.NET30,
            created_by=self.treasurer)
        self.client = Client()
        self.client.login(username="tess", password="x")

    # -- helpers --------------------------------------------------------------

    def _register(self, name, **extra):
        """Add a supplier the way the register's form does."""
        data = {"action": "save", "name": name,
                "status": Vendor.Status.ACTIVE,
                "payment_terms": Vendor.Terms.NET30}
        data.update(extra)
        return self.client.post(reverse("vendor_create"), data, follow=True)

    @staticmethod
    def _flashes(response):
        return [str(m) for m in response.context["messages"]]

    # -- the duplicate ---------------------------------------------------------

    def test_a_duplicate_name_is_answered_with_the_page_not_a_server_error(self):
        response = self._register("Mwangi Hardware Ltd")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Vendor.objects.filter(name="Mwangi Hardware Ltd").count(), 1,
                         "the duplicate was written to the register anyway")

    def test_the_treasurer_is_shown_the_sentence_the_model_wrote(self):
        """The whole point of the guard: it names the record already on file.

        Asserted on the message the treasurer actually receives, not on the
        exception, because the exception was already being raised correctly
        while the screen showed a 500.
        """
        flashes = self._flashes(self._register("Mwangi Hardware Ltd"))

        self.assertTrue(
            any("looks like the same supplier" in f and self.existing.name in f
                for f in flashes),
            f"the duplicate message never reached the treasurer: {flashes}")

    def test_the_rejected_create_comes_back_with_the_existing_supplier_on_it(self):
        """The message says "use that record" — so that record has to be there.

        Spelled differently on purpose. "Hardware, Mwangi (Ltd)" does not match
        "Mwangi Hardware Ltd" on any substring, so the only way the existing
        record can appear on the page it lands on is through `name_key` — the
        same normalisation `Vendor.clean()` used to call the two names one
        supplier. If someone re-implements the matching in the view with a
        plain name search, this test goes red rather than drifting quietly.
        """
        response = self._register("Hardware, Mwangi (Ltd)")

        self.assertEqual(Vendor.objects.count(), 1, "a second spelling got in")
        self.assertContains(
            response, reverse("vendor_detail", args=[self.existing.pk]),
            msg_prefix="the register it came back to does not offer the record "
                       "the message told the treasurer to use")

    # -- the blank form --------------------------------------------------------

    def test_saving_an_empty_form_gives_the_ordinary_field_error(self):
        """The likelier way to meet this: press Save having typed nothing.

        Same code path, so the same 500 used to answer it — and with no
        duplicate involved there is nothing clever to say, just the field
        error.
        """
        response = self._register("")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Vendor.objects.filter(name="").exists())
        self.assertIn("This field cannot be blank.", self._flashes(response))

    # -- what must not change --------------------------------------------------

    def test_a_rejected_edit_still_returns_to_the_supplier_being_edited(self):
        """The redirect the create path was crashing on is right for the edit
        path, which always has a saved record, and the fix must not take it
        away: a treasurer who mistyped a KRA PIN on Mwangi's profile belongs
        back on Mwangi's profile with the error, not dropped on the register.
        """
        response = self.client.post(
            reverse("vendor_save", args=[self.existing.pk]),
            {"action": "save", "name": self.existing.name,
             "status": Vendor.Status.ACTIVE,
             "payment_terms": Vendor.Terms.NET30,
             "tax_pin": "P051###X"}, follow=True)

        self.assertEqual(
            response.redirect_chain[-1][0],
            reverse("vendor_detail", args=[self.existing.pk]),
            "a rejected edit no longer lands on the supplier's own profile")
        self.assertTrue(any("letters, numbers" in f for f in self._flashes(response)),
                        "the edit's own validation message was lost")
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.tax_pin, "", "the bad PIN was saved")

    def test_a_supplier_that_is_accepted_still_opens_on_its_own_profile(self):
        response = self._register("Otieno Printers")

        created = Vendor.objects.get(name="Otieno Printers")
        self.assertEqual(response.redirect_chain[-1][0],
                         reverse("vendor_detail", args=[created.pk]))
