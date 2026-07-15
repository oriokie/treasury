"""Rewrite existing StatementLine.dedup_key values to the CURRENT key format.

Up to v2.68 a register line's key was the bare bank reference (with a `|D`
suffix for debits). v2.69 folded the signed amount and a narration fingerprint
into the key so that several DISTINCT movements a bank stamps with ONE reference
(a mobile-banking sweep, or a journal of stamp-duty/excise/cheque-book charges)
stay distinct. But lines ALREADY in the register still carried the old keys, so
the next import computed a new-format key that did not match, and the same line
— debits especially — re-imported as a duplicate.

This migration recomputes each line's key from its own stored fields using the
current formula, so the register self-heals: after it runs, a re-import matches
on the current key directly. `import_file` also checks the legacy form as a
belt-and-braces for any register that has not run this yet.
"""
import re
from decimal import Decimal

from django.db import migrations


_MPESA_RECEIPT_RE = re.compile(r"^(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}$")


def _is_mpesa_receipt(v):
    return bool(v and _MPESA_RECEIPT_RE.match(v))


def _current_key(line):
    """Reproduce statements.services.register.dedup_key from a StatementLine's
    OWN stored fields (not a parsed row), so the migration does not import the
    service (which could drift) and stays valid at this point in history."""
    is_debit = bool(line.debit and line.debit != 0)
    credit = line.credit or Decimal(0)
    debit = line.debit or Decimal(0)
    amt = credit - debit

    rcpt = (line.receipt or "").strip().upper()
    if _is_mpesa_receipt(rcpt):
        return (f"{rcpt}|D" if is_debit else rcpt)[:80]

    narr_fp = re.sub(r"\s+", " ", (line.raw_narration or "").strip().upper())[:24]
    for v in ((line.mpesa_ref or "").strip().upper(),
              (line.core_ref or "").strip().upper(),
              (line.receipt or "").strip().upper()):
        if v:
            base = f"{v}|{amt}|{narr_fp}"
            return (f"{base}|D" if is_debit else base)[:80]

    narr = (line.raw_narration or "")[:40]
    return f"SYN|{line.date:%Y%m%d}|{amt}|{narr}"[:80]


def forwards(apps, schema_editor):
    StatementLine = apps.get_model("statements", "StatementLine")
    # normalise per account, tracking keys already taken so a rewrite never
    # collides with an existing row (account, dedup_key is UNIQUE). A line whose
    # normalised key is already taken by another row is a genuine duplicate that
    # the old bare-reference key had merged/aliased — leave it on its old key
    # rather than crash; it will simply not re-match, and a treasurer's recheck
    # surfaces it. In practice the only collisions are lines that were already
    # the same movement.
    accounts = StatementLine.objects.values_list("account_id", flat=True).distinct()
    for account_id in accounts:
        taken = set(
            StatementLine.objects.filter(account_id=account_id)
            .values_list("dedup_key", flat=True))
        to_update = []
        lines = (StatementLine.objects.filter(account_id=account_id)
                 .order_by("id").iterator(chunk_size=1000))
        for line in lines:
            new_key = _current_key(line)
            if new_key == line.dedup_key:
                continue
            if new_key in taken:
                # would collide — leave this line's key as-is
                continue
            taken.discard(line.dedup_key)
            taken.add(new_key)
            line.dedup_key = new_key
            to_update.append(line)
            if len(to_update) >= 1000:
                StatementLine.objects.bulk_update(to_update, ["dedup_key"])
                to_update = []
        if to_update:
            StatementLine.objects.bulk_update(to_update, ["dedup_key"])


def backwards(apps, schema_editor):
    # one-way normalisation; nothing to undo (the legacy form is still accepted
    # by import_file, so no re-import breaks if this is reversed)
    pass


class Migration(migrations.Migration):
    dependencies = [("statements", "0013_reversals_skipped")]
    operations = [migrations.RunPython(forwards, backwards)]
