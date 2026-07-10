"""Turn parsed statement rows into Transactions, with dedup and review queueing.

Runs synchronously (fine for the typical weekly/monthly statement). For very
large files this is the function a Celery task would call.
"""
import datetime as dt
from decimal import Decimal

from django.db import transaction as db_tx

from core.utils import sabbath_week_of
from departments.models import Department, DevelopmentGroup
from giving.models import Transaction, SplitFund
from giving.services.allocation import allocate
from members.services.matching import match_or_create_member
from statements.models import StatementImport
from statements.services.parser import read_rows


def _is_receiptable_fund(dept):
    """True for funds we normally turn into envelope receipts: any Trust fund and
    the Local Church Budget (LCB) family. Used to decide which late-imported contributions
    enter the Sabbath confirmations queue."""
    if dept is None:
        return False
    if getattr(dept, "is_trust", False):
        return True
    name = (dept.name or "").upper()
    parent = (dept.parent.name or "").upper() if getattr(dept, "parent_id", None) else ""
    return "LCB" in name or "LOCAL CHURCH BUDGET" in name \
        or "LCB" in parent or "LOCAL CHURCH BUDGET" in parent


def _development_fund():
    """The local Development fund that dev-group giving flows into.

    Matched case-insensitively (legacy data has "DEVELOPMENT", fixtures may have
    "Development") and by category as a fallback — a strict get_or_create on the
    mixed-case name used to attempt a duplicate create, which collided on the
    slugified slug ('development') and made every dev-group row in a statement
    fail with an IntegrityError."""
    dept = (Department.objects.filter(name__iexact="development",
                                      parent__isnull=True).first()
            or Department.objects.filter(name__iexact="development").first()
            or Department.objects.filter(category=Department.Category.DEVELOPMENT,
                                         parent__isnull=True, active=True).first())
    if dept:
        return dept
    return Department.objects.create(
        name="DEVELOPMENT", fund_type=Department.FundType.LOCAL,
        category=Department.Category.DEVELOPMENT)


def _resolve(resolver):
    """Map an allocate() resolver to (Department, DevelopmentGroup|None)."""
    if isinstance(resolver, Department):
        return resolver, None
    if isinstance(resolver, str) and resolver.startswith("DEV_GROUP_"):
        tail = resolver.rsplit("_", 1)[-1]
        dept = _development_fund()
        if tail.isdigit():
            grp, _ = DevelopmentGroup.objects.get_or_create(number=int(tail))
            return dept, grp
        return dept, None  # DEV_GROUP_NA: development, group unknown
    return None, None


def verify_running_balance(rows):
    """Use the statement's own running-balance column as a checksum.

    Two independent checks over the rows in file order:

    1. Chain: for each consecutive pair, balance[n] must equal
       balance[n-1] + credit[n] - debit[n]. A break means a row's amount is
       mis-keyed, a row is out of order, or a row is missing/duplicated between
       those two points.
    2. Net: (last balance - first balance) must equal the sum of all credits
       minus debits across every row. This catches a missing or duplicated row
       even when the per-row chain happens to re-align.

    Returns (status, detail) where status is "OK", "BROKEN", or "NO_BALANCE".
    Tolerates a 1-cent rounding wobble. Pure arithmetic on the parsed rows — it
    does not touch the database, so it validates the FILE, independent of which
    rows were imported, deduped or queued."""
    from decimal import Decimal
    TOL = Decimal("0.01")
    have = [r for r in rows if r.get("balance") is not None]
    if len(have) < 2:
        return "NO_BALANCE", ("The statement has no running-balance column to "
                              "verify against (or too few rows).")
    issues = []
    # 1) chain
    prev = None
    for i, r in enumerate(rows):
        bal = r.get("balance")
        if bal is None:
            continue
        move = (r.get("credit") or Decimal(0)) - (r.get("debit") or Decimal(0))
        if prev is not None:
            expected = prev + move
            if abs(expected - bal) > TOL:
                issues.append(
                    f"Row {i+1} ({r.get('core_ref') or r.get('date')}): balance "
                    f"{bal} but expected {expected} "
                    f"(previous {prev} {'+' if move >= 0 else '-'} {abs(move)}). "
                    f"Off by {bal - expected}.")
                if len(issues) >= 10:
                    issues.append("… more breaks below this point.")
                    break
        prev = bal
    # 2) net movement across the whole file. The first row's balance is the
    # balance AFTER its own movement, so the true opening is that balance minus
    # the first row's credit/debit. Compare (last - opening) to all movements.
    first_move = ((have[0].get("credit") or Decimal(0))
                  - (have[0].get("debit") or Decimal(0)))
    opening = have[0]["balance"] - first_move
    last = have[-1]["balance"]
    net = sum(((r.get("credit") or Decimal(0)) - (r.get("debit") or Decimal(0))
               for r in rows), Decimal(0))
    swing = last - opening
    if abs(swing - net) > TOL:
        issues.append(
            f"Whole-file check: the balance moved {swing} from opening ({opening}) "
            f"to the last row ({last}), but the credits and debits sum to {net} — "
            f"a difference of {swing - net}. This usually means a row is missing "
            f"or duplicated.")
    if issues:
        return "BROKEN", "\n".join(issues)
    return "OK", (f"Verified: every row's running balance is consistent and the "
                  f"file's net movement ({swing}) matches its credits less debits "
                  f"across {len(have)} rows.")


def run_import(import_obj: StatementImport, path_or_bytes, filename, bank_account=None,
               force_sabbath=None):
    import_obj.status = StatementImport.Status.PROCESSING
    import_obj.save(update_fields=["status"])

    try:
        rows = read_rows(path_or_bytes, filename)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        import_obj.status = StatementImport.Status.FAILED
        import_obj.error_detail = str(exc)
        import_obj.save()
        return import_obj

    import_obj.total_rows = len(rows)
    import_obj.save(update_fields=["total_rows"])

    imported = dup = queued = failed = 0
    row_errors = []
    from core.models import SiteConfig
    from statements.models import BankAccount
    cfg = SiteConfig.get()
    require_confirm = cfg.require_import_confirmation
    bank_account = bank_account or import_obj.bank_account or BankAccount.get_default()
    if bank_account and import_obj.bank_account_id != getattr(bank_account, "id", None):
        import_obj.bank_account = bank_account
        import_obj.save(update_fields=["bank_account"])

    for row in rows:
        core_ref = (row["core_ref"] or "").strip() or None
        receipt = (row["receipt"] or "").strip() or None

        # database-level dedup. Check core_ref and bank_receipt (both unique),
        # and also mpesa_ref — the same M-Pesa receipt must never appear twice
        # even if one row carries a core_ref and another doesn't (e.g. a STKPUSH
        # placeholder vs the real bank reference for the same payment).
        if core_ref and Transaction.objects.filter(core_ref=core_ref).exists():
            dup += 1
            continue
        if receipt and Transaction.objects.filter(bank_receipt=receipt).exists():
            dup += 1
            continue
        mref_dedup = (row.get("mpesa_ref") or "").strip().upper() or None
        if mref_dedup and Transaction.objects.filter(mpesa_ref=mref_dedup).exists():
            dup += 1
            continue

        try:
            with db_tx.atomic():
                is_credit = bool(row["credit"])
                amount = row["credit"] if is_credit else row["debit"]
                direction = (Transaction.Direction.CREDIT if is_credit
                             else Transaction.Direction.DEBIT)

                member = None
                dept = None
                dev_group = None
                split_fund = None
                campaign = None
                campaign_group = ""
                status = Transaction.Status.REVIEW

                loan_hit = None
                if is_credit:
                    # Loan narrations are recognised BEFORE ordinary allocation
                    # ('LOAN DEV' is a liability, never development income) and
                    # never create a church Member — a lender is its own entity.
                    from loans.services.narration import detect_loan
                    lp = detect_loan(row["reference"])
                    if lp is not None and lp.kind == "RECEIPT":
                        if lp.fund_id:
                            from loans.services.loans import intake_bank_receipt
                            intake_bank_receipt(
                                lp, date=row["date"], amount=amount,
                                reference=row["reference"], phone=row["phone"],
                                name=row["name"], raw_narration=row["raw_narration"],
                                core_ref=core_ref, bank_receipt=receipt,
                                mpesa_ref=(row.get("mpesa_ref") or "")[:30],
                                bank_account=bank_account,
                                statement_import=import_obj)
                            imported += 1
                            continue
                        # clearly a loan but the fund is unknown: never guess —
                        # to the review queue, where "Record as loan receipt"
                        # completes it. Skip member creation for the same reason.
                        loan_hit = lp
                if is_credit and loan_hit is None:
                    member, _ = match_or_create_member(row["name"], row["phone"])
                    resolver, alloc_status = allocate(row["reference"], row["date"])
                    if isinstance(resolver, SplitFund):
                        split_fund = resolver
                        status = (Transaction.Status.AUTO if alloc_status == "AUTO"
                                  else Transaction.Status.LEARNED)
                    else:
                        dept, dev_group = _resolve(resolver)
                        status = ((Transaction.Status.AUTO if alloc_status == "AUTO"
                                   else Transaction.Status.LEARNED)
                                  if dept is not None else Transaction.Status.REVIEW)
                        # DEV_GROUP_NA means "clearly development, but which
                        # group is unknown from the reference text alone" —
                        # a configured campaign's member table (the same
                        # fallback that already resolves things like Camp
                        # Expense) may still be able to pin down the exact
                        # group from the payer's name/phone, so it gets a
                        # chance here too, instead of only when dept is None.
                        dev_group_unknown = (resolver == "DEV_GROUP_NA")
                        if dept is None or dev_group_unknown:
                            from giving.services.allocation import campaign_allocate
                            campaign, campaign_group, cdept, cstatus = campaign_allocate(
                                row["reference"], row["name"], row["phone"])
                            if cdept is not None and (dept is None or cstatus == "AUTO"):
                                dept = cdept
                                status = (Transaction.Status.AUTO if cstatus == "AUTO"
                                          else Transaction.Status.REVIEW)
                else:
                    status = Transaction.Status.REVIEW

                # When import confirmation is required, auto/learned allocations are
                # held unconfirmed (they don't affect balances until a treasurer
                # confirms them). Review items are unaffected — they go to the queue.
                confirmed = True
                if (require_confirm and status in (Transaction.Status.AUTO,
                                                   Transaction.Status.LEARNED)):
                    confirmed = False

                from core.models import service_sabbath_for, SiteConfig
                from core.utils import sabbath_of as _sof, sabbath_week_of as _swk
                _today = dt.date.today()
                if force_sabbath:
                    # Treasurer explicitly chose the Sabbath these entries belong
                    # to (e.g. a late import). It takes precedence and there is
                    # nothing to confirm — they have already decided.
                    svc_sab = _sof(force_sabbath)
                    sab_pending = False
                else:
                    natural_sab = _sof(row["date"])
                    svc_sab = service_sabbath_for(row["date"], as_of=_today)
                    # Held for confirmation when the gift's natural Sabbath had already
                    # passed by import day and so was rolled forward — the treasurer
                    # confirms it really belongs to the next Sabbath (or pulls it back).
                    rolled = bool(is_credit and svc_sab and natural_sab
                                  and svc_sab != natural_sab and natural_sab <= _today)
                    # Only the funds we normally receipt (Trust + LCB) need confirming,
                    # unless the setting says all. A split fund always has a trust half,
                    # so it qualifies. Items outside scope just post by date silently.
                    _scope = SiteConfig.get().sabbath_confirm_scope
                    if rolled and _scope == SiteConfig.SabbathConfirmScope.RECEIPTABLE:
                        in_scope = bool(split_fund is not None or _is_receiptable_fund(dept))
                    else:
                        in_scope = True
                    sab_pending = rolled and in_scope
                common = dict(
                    date=row["date"], sabbath_week=_swk(svc_sab),
                    service_sabbath=svc_sab, sabbath_confirm_pending=sab_pending,
                    channel=Transaction.Channel.BANK, direction=direction,
                    member=member, reference=(row["reference"] or "")[:60],
                    payer_name=(row["name"] or "")[:120],
                    payer_phone=(row["phone"] or "")[:12],
                    mpesa_ref=(row.get("mpesa_ref") or "")[:30],
                    statement_import=import_obj, allocation_status=status,
                    bank_account=bank_account, confirmed=confirmed,
                    campaign=campaign, campaign_group=(campaign_group or ""),
                    raw_narration=row["raw_narration"])

                if split_fund is not None:
                    parts = split_fund.split(amount)
                    for i, (pdept, pamt) in enumerate(parts):
                        Transaction.objects.create(
                            amount=pamt, department=pdept,
                            core_ref=core_ref if i == 0 else
                                     (f"{core_ref}-S{i}" if core_ref else None),
                            bank_receipt=receipt if i == 0 else None,
                            **common)
                else:
                    Transaction.objects.create(
                        amount=amount, department=dept, dev_group=dev_group,
                        core_ref=core_ref, bank_receipt=receipt, **common)

                if status == Transaction.Status.REVIEW:
                    queued += 1
                else:
                    imported += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            row_errors.append(f"{core_ref or receipt or row.get('date')}: "
                              f"{type(exc).__name__}: {str(exc)[:120]}")

    if row_errors:
        import_obj.error_detail = ((import_obj.error_detail or "") +
                                   "\n".join(row_errors[:25]))[:4000]
    # checksum the file against its own running balance, so the treasurer knows
    # immediately whether every row imported once with no gaps or duplicates
    bstatus, bdetail = verify_running_balance(rows)
    import_obj.balance_check = bstatus
    import_obj.balance_detail = bdetail[:4000]
    # capture the statement's own opening/closing running balance and date span,
    # for the bank reconciliation report
    with_bal = [r for r in rows if r.get("balance") is not None]
    if with_bal:
        first = with_bal[0]
        first_move = ((first.get("credit") or Decimal(0))
                      - (first.get("debit") or Decimal(0)))
        import_obj.stmt_opening_balance = first["balance"] - first_move
        import_obj.stmt_closing_balance = with_bal[-1]["balance"]
    dated = [r["date"] for r in rows if r.get("date")]
    if dated:
        import_obj.stmt_first_date = min(dated)
        import_obj.stmt_last_date = max(dated)
    import_obj.imported = imported
    import_obj.duplicates_skipped = dup
    import_obj.queued_for_review = queued
    import_obj.failed = failed
    import_obj.status = StatementImport.Status.DONE
    import_obj.save()

    # offer/apply pledge matches for the confirmed contributions just imported
    # (respects SiteConfig.pledge_match_mode; best-effort, never breaks import)
    try:
        from pledges.services.matching import handle_new_contribution
        # excluded_from_income skips loan receipts — loan money must never be
        # matched against a member's giving pledge
        for t in Transaction.objects.filter(statement_import=import_obj,
                                             direction=Transaction.Direction.CREDIT,
                                             confirmed=True,
                                             excluded_from_income=False):
            handle_new_contribution(t)
    except Exception:
        pass
    return import_obj


def latest_cleared_balance(limit=200):
    """Most recent cleared bank balance reported by the real-time CBS feed, or
    None. Returns dict(balance: Decimal, at, account, currency). Used so the bank
    position can compare against live data, not only an imported statement."""
    import json
    from decimal import Decimal, InvalidOperation
    from statements.models import BankEvent

    def _find(obj, key):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == key.lower() and v not in (None, ""):
                    return v
                found = _find(v, key)
                if found not in (None, ""):
                    return found
        elif isinstance(obj, list):
            for it in obj:
                found = _find(it, key)
                if found not in (None, ""):
                    return found
        return None

    for e in BankEvent.objects.order_by("-received_at")[:limit]:
        if not e.payload:
            continue
        try:
            data = json.loads(e.payload)
        except (ValueError, TypeError):
            continue
        cb = _find(data, "ClearedBalance")
        if cb in (None, ""):
            continue
        try:
            bal = Decimal(str(cb).replace(",", ""))
        except (InvalidOperation, ValueError):
            continue
        return {"balance": bal, "at": e.received_at,
                "account": e.acct_no or _find(data, "AccountNo") or "",
                "currency": e.currency or _find(data, "Currency") or ""}
    return None
