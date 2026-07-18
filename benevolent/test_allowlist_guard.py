"""The frozen-allowlist trap (recommendation #74a), enforced.

Background. Several places kept a hand-maintained list of model field names —
`SchemePolicy.RULE_FIELDS` (which fields are versioned policy rules), the
`SiteConfigForm` field list, the settings template's per-tab field groups. Each
had the same failure mode: add a field to the model, forget to add it to the
list, and the field silently vanishes from versioning / the form / the UI with no
error. It recurred sixteen times.

The fix is not "never use an allowlist" — many forms are deliberately restrictive.
The fix is: **any list that is MEANT to be complete must have a test that fails
when it drifts.** These are those tests. If they fail, a field was added to a
model without being classified — fix the classification, don't delete the test.
"""
from django.test import TestCase


class RuleFieldsCompletenessTests(TestCase):
    """SchemePolicy.RULE_FIELDS must name every policy field that is a rule, and
    NON_RULE_FIELDS every field that isn't — together covering the whole model
    with no overlap. Adding a field to SchemePolicy without classifying it fails
    here, so it can't silently drop out of policy versioning and the settings UI.
    """

    def _concrete_field_names(self):
        from benevolent.models import SchemePolicy
        return {f.name for f in SchemePolicy._meta.get_fields()
                if getattr(f, "concrete", False) and not f.many_to_many}

    def test_rule_and_non_rule_partition_the_model(self):
        from benevolent.models import SchemePolicy
        rule = set(SchemePolicy.RULE_FIELDS)
        non_rule = set(SchemePolicy.NON_RULE_FIELDS)
        fields = self._concrete_field_names()

        unclassified = fields - rule - non_rule
        self.assertEqual(
            unclassified, set(),
            f"SchemePolicy fields are neither in RULE_FIELDS nor NON_RULE_FIELDS "
            f"— classify each (a rule that versions, or metadata that doesn't): "
            f"{sorted(unclassified)}")

        overlap = rule & non_rule
        self.assertEqual(overlap, set(),
                         f"fields in BOTH RULE_FIELDS and NON_RULE_FIELDS: "
                         f"{sorted(overlap)}")

    def test_rule_fields_all_exist_on_the_model(self):
        from benevolent.models import SchemePolicy
        phantom = set(SchemePolicy.RULE_FIELDS) - self._concrete_field_names()
        self.assertEqual(phantom, set(),
                         f"RULE_FIELDS names fields that don't exist on "
                         f"SchemePolicy: {sorted(phantom)}")

    def test_no_duplicate_rule_fields(self):
        from benevolent.models import SchemePolicy
        rf = SchemePolicy.RULE_FIELDS
        dupes = {f for f in rf if rf.count(f) > 1}
        self.assertEqual(dupes, set(), f"duplicate RULE_FIELDS: {sorted(dupes)}")


class SiteConfigFormCompletenessTests(TestCase):
    """SiteConfigForm must bind every editable SiteConfig field (it uses
    `exclude=`, so this is structural — but the test guards the exclude list
    itself from quietly growing), and the settings page must render every bound
    field somewhere (a named tab or the fallback panel)."""

    def test_form_binds_every_editable_field(self):
        from core.forms import SiteConfigForm
        from core.models import SiteConfig
        excluded = set(SiteConfigForm.Meta.exclude)
        editable = {f.name for f in SiteConfig._meta.get_fields()
                    if (getattr(f, "concrete", False) or f.many_to_many)
                    and not getattr(f, "auto_created", False)}
        should_bind = editable - excluded
        bound = set(SiteConfigForm().fields)
        missing = should_bind - bound
        self.assertEqual(
            missing, set(),
            f"SiteConfig fields not bound by the form (unreachable settings): "
            f"{sorted(missing)}. If a field is deliberately not user-editable, "
            f"add it to SiteConfigForm.Meta.exclude with a reason.")

    def test_exclude_list_stays_minimal(self):
        # a guard on the guard: if someone re-introduces a large exclude list
        # (turning the denylist back into a de-facto allowlist), notice it.
        from core.forms import SiteConfigForm
        self.assertLessEqual(
            len(SiteConfigForm.Meta.exclude), 6,
            "SiteConfigForm.Meta.exclude has grown large — a denylist should "
            "stay small. Are fields being hidden that should be editable?")

    def test_every_bound_field_is_reachable_in_the_ui(self):
        """Every field the form binds must render on the settings page — in a
        named tab, or in the fallback 'Other settings' panel. None may be bound
        but invisible."""
        from core.views import _unplaced_setting_fields
        from core.forms import SiteConfigForm
        from django.contrib.auth.models import Group, User
        from core.roles import TREASURER

        # the fallback catches whatever the tabs don't — so "reachable" is
        # tautologically true UNLESS the fallback itself isn't rendered. Assert
        # the page actually renders the fallback when there are unplaced fields.
        form = SiteConfigForm()
        unplaced = _unplaced_setting_fields(form)
        t = User.objects.create_user("t_74a", password="x")
        t.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(t)
        html = self.client.get("/settings/").content.decode()
        if unplaced:
            self.assertIn('data-pane="other"', html,
                          "there are settings not placed in any tab, but the "
                          "fallback 'Other' panel did not render — they are "
                          "unreachable")
            # and each unplaced field is actually in the page (as a named input
            # or, for multi-widgets like CheckboxSelectMultiple, its id container)
            for f in unplaced:
                present = (f'name="{f.name}"' in html
                           or f'id="id_{f.name}"' in html
                           or f'id_{f.name}' in html)
                self.assertTrue(present,
                                f"unplaced setting {f.name} is not on the page")
