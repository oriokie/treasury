"""Supplier categories are set up on the register — not a dead empty select."""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER
from vendors.models import VendorCategory


class VendorCategorySetupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("vc_tr", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.c = Client()
        self.c.force_login(self.user)

    def test_register_seeds_default_categories(self):
        self.assertFalse(VendorCategory.objects.filter(name="Hardware").exists())
        body = self.c.get(reverse("vendor_list")).content.decode()
        self.assertTrue(VendorCategory.objects.filter(name="Hardware").exists())
        self.assertIn("Hardware", body)
        self.assertIn("Supplier categories", body)

    def test_add_category_from_register(self):
        r = self.c.post(reverse("vendor_create"), {
            "action": "add_category", "category_name": "Security"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(VendorCategory.objects.filter(name="Security").exists())

    def test_vendors_new_get_redirects_to_form(self):
        r = self.c.get(reverse("vendor_create"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("new=1", r["Location"])
