"""Phase 9 — configurable Benevolent-specific roles, using the SAME profile
mechanism every other role in this system already uses (Treasurer/Assistant/
Auditor/Leader/Elder defaults — see 0004 and 0005). No separate permission
system: these are just named bundles of the rights core/rights.py already
defines, assignable to any user exactly like any other profile.

Seven profiles, matching the brief's named roles:

  * Benevolent Administrator   — scheme/policy setup + settings & automation
  * Benevolent Approver        — the brief's "Treasurer" role: authorises
                                  cases, without full church treasury access
  * Benevolent Committee Member — sits on a scheme's committee and votes.
                                  "Committee Chairperson" is deliberately NOT
                                  a separate profile: chairing is a SEAT on a
                                  scheme's roster (benevolent.CommitteeMember,
                                  Phase 6), not a different right — a chair
                                  holds this same profile, then is additionally
                                  seated as Chair via Committee → Manage
                                  roster. Two profiles with identical rights
                                  would only be confusing.
  * Benevolent Registration Officer — enrol, admit, transfer, reinstate
  * Benevolent Case Officer    — raise, submit, assess cases
  * Benevolent Finance Officer — contributions, fees, adjustments, refunds
  * Benevolent Auditor         — read-only, scoped to benevolent specifically
                                  (distinct from the general "Auditor
                                  (default)" profile, which has much broader
                                  church-wide rights) — also the natural
                                  profile for the brief's "optional Elder"
                                  case: assign it to an elder who should see
                                  the schemes but do nothing else.
"""
from django.db import migrations


def create_defaults(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    # Every profile includes "view_reports" alongside its benevolent right(s):
    # the Phase 8 report catalogue's own access gate (ReportAccessMixin)
    # requires it before a request even reaches a report's own, narrower
    # permission — without it, a genuinely scoped role (someone who is NOT
    # also a Treasurer/Assistant/Auditor) could administer the module all day
    # and never be able to open the reports built specifically for it.
    defaults = {
        "Benevolent Administrator (default)": (
            ["view_benevolent", "view_reports", "manage_benevolent_schemes",
             "manage_benevolent_settings"],
            "Sets up schemes and policies, and manages module settings, "
            "notification templates and automation."),
        "Benevolent Approver (default)": (
            ["view_benevolent", "view_reports", "approve_benevolent"],
            "Authorises or refuses a benevolent case — the brief's "
            "\"Treasurer\" role for this module, without full church treasury access."),
        "Benevolent Committee Member (default)": (
            ["view_benevolent", "view_reports", "benevolent_committee"],
            "Sits on a scheme's committee and votes on cases. Seat someone as "
            "Chair via Committee -> Manage roster on the scheme; the right "
            "is the same, the seat is what makes them Chair."),
        "Benevolent Registration Officer (default)": (
            ["view_benevolent", "view_reports", "benevolent_register_members"],
            "Enrols, admits, transfers and reinstates members; manages "
            "households and exemptions."),
        "Benevolent Case Officer (default)": (
            ["view_benevolent", "view_reports", "benevolent_manage_cases"],
            "Raises, submits and assesses cases; manages documents and "
            "funding targets. Does not approve a case — that is the "
            "Approver's role."),
        "Benevolent Finance Officer (default)": (
            ["view_benevolent", "view_reports", "benevolent_manage_finance"],
            "Records contributions, resolves the intake queue, charges or "
            "waives dues, processes refunds."),
        "Benevolent Auditor (default)": (
            ["view_benevolent", "view_reports"],
            "Read-only access to benevolent schemes, members, cases and "
            "reports. The natural profile for an elder or an auditor who "
            "should see this module without administering it."),
    }
    for name, (rights, desc) in defaults.items():
        Profile.objects.get_or_create(
            name=name,
            defaults={"rights": sorted(rights), "description": desc, "is_system": True})


def remove_defaults(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    Profile.objects.filter(
        name__in=[
            "Benevolent Administrator (default)", "Benevolent Approver (default)",
            "Benevolent Committee Member (default)",
            "Benevolent Registration Officer (default)",
            "Benevolent Case Officer (default)", "Benevolent Finance Officer (default)",
            "Benevolent Auditor (default)",
        ],
        is_system=True).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_passwordresetcode")]
    operations = [migrations.RunPython(create_defaults, remove_defaults)]
