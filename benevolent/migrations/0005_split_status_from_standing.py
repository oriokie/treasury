"""Phase 3 — move the derived statuses off the administrative axis.

Before Phase 3, `SchemeMembership.status` carried two incompatible things at once:
decisions a human made (pending, active, suspended, withdrawn) and facts a job
derived (lapsed, expired, inactive). Phase 3 separates them — `status` keeps the
decisions, and the new computed `standing` column carries the facts.

This migration moves the existing rows across. It is careful in one specific way:
a derived status is NOT information a human put there, so translating LAPSED to
"active, and standing will be recomputed" loses nothing — the standing engine will
re-derive it from the policy on the next assessment, and get the same answer for
the same reason. Whereas a SUSPENDED row *is* a human's decision, and is left
exactly where it is.

EXPELLED has no successor on the lifecycle axis and becomes CLOSED, with the
original word preserved in the event log so nobody loses the fact that a member was
removed rather than simply leaving.
"""
from django.db import migrations


# old status -> (new status, new standing, note)
TRANSLATION = {
    # derived facts: the decision underneath them was always "this member is on the
    # books", so that is what the lifecycle axis records. The fact itself moves to
    # standing, where it can be recomputed rather than remembered.
    "LAPSED":    ("ACTIVE", "ARREARS", "was marked lapsed"),
    "EXPIRED":   ("ACTIVE", "ARREARS", "was marked expired (renewal overdue)"),
    "INACTIVE":  ("ACTIVE", "INACTIVE", "was marked inactive"),
    # human decisions: left alone, mirrored onto standing so it is always the whole
    # answer
    "PENDING":   ("PENDING", "PENDING", ""),
    "ACTIVE":    ("ACTIVE", "GOOD", ""),
    "SUSPENDED": ("SUSPENDED", "SUSPENDED", ""),
    "WITHDRAWN": ("WITHDRAWN", "WITHDRAWN", ""),
    # no lifecycle successor; the distinction is kept in the event log
    "EXPELLED":  ("CLOSED", "CLOSED", "was removed from the scheme"),
}


def forwards(apps, schema_editor):
    Membership = apps.get_model("benevolent", "SchemeMembership")
    Event = apps.get_model("benevolent", "MembershipEvent")

    for m in Membership.objects.all().iterator():
        old = m.status
        status, standing, note = TRANSLATION.get(old, ("ACTIVE", "GOOD", ""))
        m.status = status
        m.standing = standing
        m.standing_reason = (
            "Carried over when standing was separated from the lifecycle; will be "
            "recomputed from the policy on the next assessment.")
        m.registration_type = m.registration_type or "INDIVIDUAL"
        m.save(update_fields=["status", "standing", "standing_reason",
                              "registration_type"])

        if note:
            Event.objects.create(
                membership=m, kind="NOTE", on=m.joined_on,
                summary=f"Membership {note}.",
                reason=("Recorded here when the membership lifecycle was separated from "
                        "standing, so the original administrative fact is not lost."),
                from_value=old, to_value=status, automated=True)


def backwards(apps, schema_editor):
    """Reversible in shape: the lifecycle axis is restored from standing where the
    old status was a derived one. Nothing is destroyed going forward, so nothing
    has to be invented coming back."""
    Membership = apps.get_model("benevolent", "SchemeMembership")
    reverse = {"ARREARS": "LAPSED", "INACTIVE": "INACTIVE", "CLOSED": "EXPELLED"}
    for m in Membership.objects.all().iterator():
        if m.status == "ACTIVE" and m.standing in reverse:
            m.status = reverse[m.standing]
            m.save(update_fields=["status"])
        elif m.status == "CLOSED":
            m.status = "EXPELLED"
            m.save(update_fields=["status"])


class Migration(migrations.Migration):

    dependencies = [
        ("benevolent", "0004_historicalschemedependant_member_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
