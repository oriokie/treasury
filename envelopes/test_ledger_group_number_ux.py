"""Ledger UX: development group is keyed by NUMBER (not a long <select>),
resolved on submit; cascade to other rows is gone; members are embedded for
client-side autocomplete; the sheet scrolls with the page (no 72vh trap).

Behaviour for invalid group numbers lives in ledger.html JS (validateRow /
row-bad-group). These tests lock the server-rendered contract that JS relies
on — same pattern as test_ledger_ux_fixes / test_ledger_validation_ux_v242.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT
from departments.models import DevelopmentGroup
from members.models import Member


def _assistant(username="grp_num_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


class LedgerGroupNumberUxTests(TestCase):
    def setUp(self):
        self.u = _assistant()
        self.client.force_login(self.u)
        self.grp = DevelopmentGroup.objects.create(number=12, name="G12")
        Member.objects.create(name="ALICE TEST", phone="254700000012")

    def test_dev_groups_embed_number_for_client_resolve(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn("number:12", html)
        self.assertIn("resolveDevGroupId", html)
        self.assertIn("DEV_GROUP_BY_NUMBER", html)

    def test_group_field_is_number_input_not_select(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn('class="field devgrp num mono"', html)
        self.assertIn('placeholder="grp#"', html)
        self.assertNotIn("devgrp-select", html)
        self.assertNotIn("devGroupOptions", html)

    def test_group_selection_does_not_cascade_to_other_rows(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        # Channel may still cascade; development group must not.
        self.assertIn('cascadeSelectFrom(tr, ".chan"', html)
        self.assertNotIn('cascadeSelectFrom(tr, ".devgrp"', html)

    def test_invalid_group_number_blocks_submit_via_row_bad_group(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn("row-bad-group", html)
        self.assertIn("No development group matches number", html)
        # validateRow must resolve the typed number before allowing advance
        self.assertIn("resolveDevGroupId(num)", html)

    def test_members_embedded_for_client_side_autocomplete(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertIn("const MEMBERS = ", html)
        self.assertIn("ALICE TEST", html)
        self.assertIn("localMemberMatches", html)

    def test_no_72vh_table_scroll_trap(self):
        html = self.client.get(reverse("envelope_ledger")).content.decode()
        self.assertNotIn("max-height:72vh", html)
        self.assertIn("pinLedgerHead", html)
        self.assertIn("position:sticky;top:var(--ledger-topbar-h", html)
