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

**Written to survive a real production table, not just a demo-sized one.**
The first version of this migration ran inside ONE uncommitted transaction with
no progress output and two full-table passes per account — on a production
MySQL table with years of history, that is indistinguishable from "stuck" even
when it is technically still working, and a single giant transaction risks long
lock waits against any concurrent write. This version:
  - is NOT wrapped in one transaction (`atomic = False`); each small batch
    commits on its own, so progress is real and a kill-and-restart loses at
    most one batch, not the whole run;
  - fetches only the columns it needs (`.only(...)`), not full rows;
  - commits in small batches (500 rows) with a small `bulk_update` batch_size,
    so no single UPDATE statement is enormous;
  - prints progress every batch, so it is visible rather than silent;
  - is idempotent and resumable: a row whose key is already current is skipped
    with no query, so re-running after an interruption is cheap.
"""
import re
from decimal import Decimal

from django.db import migrations, transaction


_MPESA_RECEIPT_RE = re.compile(r"^(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}$")

# how many lines to read, compute and commit as one unit. Small enough that a
# single UPDATE statement stays modest and a lock is held only briefly; large
# enough that a table of tens of thousands of rows finishes in a reasonable
# number of round trips.
BATCH = 500


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
    db_alias = schema_editor.connection.alias
    fields = ("id", "account_id", "date", "credit", "debit", "receipt",
             "mpesa_ref", "core_ref", "raw_narration", "dedup_key")

    accounts = list(
        StatementLine.objects.using(db_alias)
        .order_by("account_id").values_list("account_id", flat=True).distinct())
    total_accounts = len(accounts)
    print(f"\n  normalize_register_dedup_keys: {total_accounts} account(s) to check")

    grand_updated = 0
    for a_i, account_id in enumerate(accounts, start=1):
        # keys already in use for THIS account, so a rewrite never collides with
        # an existing row ((account, dedup_key) is UNIQUE). Only the key column
        # is fetched — this is one lightweight query, not a full row scan.
        taken = set(
            StatementLine.objects.using(db_alias)
            .filter(account_id=account_id)
            .values_list("dedup_key", flat=True))

        qs = (StatementLine.objects.using(db_alias)
              .filter(account_id=account_id)
              .only(*fields).order_by("id"))

        batch, account_updated, account_total = [], 0, 0
        for line in qs.iterator(chunk_size=BATCH):
            account_total += 1
            new_key = _current_key(line)
            if new_key == line.dedup_key:
                continue                       # already current — no write needed
            if new_key in taken:
                # a genuine duplicate the OLD key had merged — leave it on its
                # old key rather than collide; a treasurer's recheck surfaces it.
                continue
            taken.discard(line.dedup_key)
            taken.add(new_key)
            line.dedup_key = new_key
            batch.append(line)
            if len(batch) >= BATCH:
                account_updated += _flush(StatementLine, db_alias, batch)
                batch = []
        if batch:
            account_updated += _flush(StatementLine, db_alias, batch)

        grand_updated += account_updated
        print(f"  account {account_id}: {account_total} line(s) checked, "
              f"{account_updated} rewritten "
              f"[{a_i}/{total_accounts} accounts done]")

    print(f"  normalize_register_dedup_keys: done, {grand_updated} line(s) "
         f"rewritten in total\n")


def _flush(StatementLine, db_alias, batch):
    """Commit one small batch on its own — never inside one migration-wide
    transaction — so progress is real and a kill loses at most this batch."""
    with transaction.atomic(using=db_alias):
        StatementLine.objects.using(db_alias).bulk_update(
            batch, ["dedup_key"], batch_size=200)
    return len(batch)


def backwards(apps, schema_editor):
    # one-way normalisation; nothing to undo (the legacy form is still accepted
    # by import_file, so no re-import breaks if this is reversed)
    pass


class Migration(migrations.Migration):
    # NOT wrapped in one transaction — see the module docstring. Each batch in
    # forwards() commits itself via _flush(), which is what makes this safe to
    # interrupt and resume on a large production table.
    atomic = False
    dependencies = [("statements", "0013_reversals_skipped")]
    operations = [migrations.RunPython(forwards, backwards)]
