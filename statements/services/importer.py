"""Turn parsed statement rows into Transactions, with dedup and review queueing.

Runs synchronously (fine for the typical weekly/monthly statement). For very
large files this is the function a Celery task would call.
"""
import datetime as dt
from decimal import Decimal

from django.db import transaction as db_tx
from django.db.models import Q

from core.utils import sabbath_week_of
from departments.models import Department, DevelopmentGroup
from giving.models import Transaction, SplitFund
from giving.services.allocation import allocate
from members.services.matching import match_or_create_member
from statements.models import StatementImport
from statements.services.parser import read_rows


def _is_receiptable_fund(dept):
    """True for funds we normally turn into envelope receipts: any Trust fund and
    the Local Church Budget (LCB) family.

    Delegates to `departments.models.receiptable_fund_ids()` — the single
    definition of "Trust + LCB", shared with the transaction list's pending-receipt
    view. This used to match LCB by NAME alone, which meant a church that had
    carefully configured its LCB funds in Settings found that setting ignored
    here, and two screens could disagree about which funds counted.
    """
    if dept is None:
        return False
    from departments.models import receiptable_fund_ids
    return dept.id in receiptable_fund_ids()


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


def _reversal_row_pairs(rows):
    """Indices of parsed rows that form a reversal pair — the bank's own error,
    undone. Same rule as `statements.services.register._reversal_pairs`: opposite
    direction, equal amount, within a week, and a narration that SAYS so (or a
    shared bank reference, which is the bank saying it more precisely).

    A keyword is required. A church that receives a 5,000 gift on Monday and pays
    a 5,000 supplier on Tuesday has two perfectly real movements, and silently
    erasing both because they cancel out would be far worse than leaving a genuine
    reversal unrecognised.
    """
    from statements.services.register import looks_like_reversal

    def _amt(r):
        return (r.get("credit") or Decimal(0)) - (r.get("debit") or Decimal(0))

    out = set()
    used = set()
    for i, a in enumerate(rows):
        if i in used or not _amt(a):
            continue
        for j, b in enumerate(rows):
            if j in used or j == i:
                continue
            if _amt(a) != -_amt(b):
                continue
            if a.get("date") and b.get("date") and abs((a["date"] - b["date"]).days) > 7:
                continue
            same_ref = bool(a.get("mpesa_ref")
                            and a.get("mpesa_ref") == b.get("mpesa_ref"))
            says_so = (looks_like_reversal(a.get("raw_narration"))
                       or looks_like_reversal(b.get("raw_narration")))
            if not (same_ref or says_so):
                continue
            out.add(i)
            out.add(j)
            used.add(i)
            used.add(j)
            break
    return out


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

    imported = dup = queued = failed = reversals_skipped = 0
    row_errors = []
    # core_ref is UNIQUE on Transaction, but a bank can share one core_ref across
    # several distinct receipts in one batch (see the dedup note below). These
    # track the bare core_refs already used within THIS file so later rows in the
    # same batch get a "-S" suffix instead of colliding.
    from collections import defaultdict
    _core_refs_this_file = set()
    _core_ref_seq = defaultdict(lambda: 1)
    _line_keys_this_file = set()
    _receipts_this_file = set()
    from core.models import SiteConfig
    from statements.models import BankAccount

    # A bank entry made in ERROR and then undone is a NON-EVENT. The bank credits
    # the church by mistake and takes it back; nothing was really received.
    #
    # This was being posted as REAL: the credit became income allocated to a fund,
    # and the reversing debit was posted separately — so a church's books showed a
    # gift it never received, and its income was overstated by the amount of the
    # bank's own mistake.
    #
    # Transaction has carried `is_reversed` / `is_reversal` all along, and every
    # report already excludes both (`TransactionQuerySet.active`,
    # `.confirmed_credits`). Nothing was setting them from a statement. Now the
    # importer pairs the rows up front and marks them, so the cash movement is
    # still recorded honestly — the money did leave and come back — while the
    # income never appears at all.
    reversal_rows = _reversal_row_pairs(rows)
    cfg = SiteConfig.get()
    require_confirm = cfg.require_import_confirmation
    bank_account = bank_account or import_obj.bank_account or BankAccount.get_default()
    if bank_account and import_obj.bank_account_id != getattr(bank_account, "id", None):
        import_obj.bank_account = bank_account
        import_obj.save(update_fields=["bank_account"])

    for _row_index, row in enumerate(rows):
        if _row_index in reversal_rows:
            # the bank's own mistake, undone — no money was really received or
            # paid, so nothing is posted. It IS still on the bank register, which
            # records what the bank said rather than what was true.
            reversals_skipped += 1
            continue
        core_ref = (row["core_ref"] or "").strip() or None
        receipt = (row["receipt"] or "").strip() or None

        # The register's dedup_key is the single source of truth for "is this the
        # SAME bank line?". It already handles the two ways a bank shares one
        # reference across distinct movements: a mobile-banking sweep batching
        # several payments (told apart by their unique M-Pesa receipts), and a
        # journal batching several charges under one reference (told apart by
        # amount + narration — stamp duty 250, excise 300, cheque-book 1,500 all
        # under CB0170485260413). Deduping on that key, rather than on the bare
        # core_ref, is what stops those distinct lines collapsing into one and
        # silently dropping money.
        from statements.services.register import dedup_key, _is_mpesa_receipt
        _up = lambda v: (v or "").strip().upper() or None
        line_key = dedup_key(row)

        if line_key in _line_keys_this_file:
            dup += 1
            continue

        # already imported in a PRIOR run? A line is the same as one already on
        # the books when it carries the same unique identifier. Check, in order:
        #   - a genuine M-Pesa receipt (globally unique) on bank_receipt;
        #   - a non-M-Pesa receipt column value on bank_receipt;
        #   - the core_ref base PLUS the signed amount — this is what tells a
        #     re-imported charge (same ref, same amount) apart from a sibling
        #     charge in the same batch (same ref, different amount).
        _amt = (row.get("credit") or Decimal(0)) - (row.get("debit") or Decimal(0))
        already = False
        rcpt_up = _up(receipt)
        mref_up = _up(row.get("mpesa_ref"))
        # A mpesa_ref is a SHARED channel-batch ref (not a per-payment id) when
        # the narration carries its OWN distinct unique receipt — that is exactly
        # the SFI40… case, where mpesa_ref repeats across the batch. In that case
        # we must not dedup on mpesa_ref. Otherwise mpesa_ref is the payment's own
        # receipt and a repeat of it (anywhere) is a duplicate.
        mref_is_shared = bool(mref_up and rcpt_up and mref_up != rcpt_up
                              and _is_mpesa_receipt(rcpt_up))
        if rcpt_up:
            already = Transaction.objects.filter(bank_receipt=rcpt_up).exists()
        if not already and rcpt_up and _is_mpesa_receipt(rcpt_up):
            already = Transaction.objects.filter(mpesa_ref__iexact=rcpt_up).exists()
        if not already and mref_up and not mref_is_shared:
            already = (Transaction.objects.filter(bank_receipt=mref_up).exists()
                       or Transaction.objects.filter(mpesa_ref__iexact=mref_up).exists())
        if not already and core_ref:
            base = core_ref.strip().upper()
            already = Transaction.objects.filter(
                Q(core_ref__iexact=base) | Q(core_ref__istartswith=f"{base}-S"),
                amount=abs(_amt),
                direction=(Transaction.Direction.DEBIT if _amt < 0
                           else Transaction.Direction.CREDIT),
            ).exists()
        if already:
            dup += 1
            continue
        _line_keys_this_file.add(line_key)

        # core_ref and bank_receipt are UNIQUE columns. When a batch shares one
        # core_ref across several distinct lines, only the first stores it bare;
        # the rest are suffixed ("-S1", "-S2"), the same convention _txn_keys()
        # follows so the register still reconciles to the ledger on the shared
        # reference.
        if core_ref and (
                core_ref in _core_refs_this_file
                or Transaction.objects.filter(
                    Q(core_ref=core_ref) | Q(core_ref__startswith=f"{core_ref}-S")
                ).exists()):
            base = core_ref
            core_ref = f"{base}-S{_core_ref_seq[base]}"
            _core_ref_seq[base] += 1
        elif core_ref:
            _core_refs_this_file.add(core_ref)

        # bank_receipt is also UNIQUE. A non-M-Pesa receipt column value can also
        # repeat across a batch; drop it rather than collide (the suffixed
        # core_ref and the register key still identify the line).
        if receipt and _up(receipt) in _receipts_this_file:
            receipt = None
        elif receipt and Transaction.objects.filter(bank_receipt=_up(receipt)).exists():
            receipt = None
        elif receipt:
            _receipts_this_file.add(_up(receipt))

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
                # Benevolent scheme money is recognised here, alongside loans, for
                # the same reason: a narration rule knows which FUND the money
                # belongs to, which ordinary allocation would have to guess at. What
                # it does NOT know is which member — and that is deliberately a
                # separate question, answered after the money is safely banked (see
                # the intake call below). Getting the fund right is what keeps the
                # ledger correct; getting the member right is what keeps the member's
                # statement correct, and the first must never wait on the second.
                ben_scheme = None
                if is_credit and loan_hit is None:
                    try:
                        from benevolent.services.allocation import detect_scheme
                        ben_scheme, _bk, _bs = detect_scheme(row["reference"])
                    except Exception:  # noqa: BLE001 — never break an import
                        ben_scheme = None

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

                        # detect_scheme's own "sole owner of this fund" fallback
                        # (see benevolent.services.allocation.detect_scheme) never
                        # got a real chance above — it needs the FUND, which
                        # ordinary allocation has only just now worked out. A
                        # church typing "MSAMARIA" as an ordinary fund reference
                        # (giving.AllocationRule, never a benevolent
                        # ContributionRule) got the fund right, but the money
                        # silently never reached the benevolent intake queue: no
                        # ContributionRule pattern matched, and the sole-scheme
                        # fallback was structurally unreachable with fund=None on
                        # the only attempt. Retried here, now the fund is known.
                        if ben_scheme is None and dept is not None:
                            try:
                                ben_scheme, _bk2, _bs2 = detect_scheme(
                                    row["reference"], fund=dept)
                            except Exception:  # noqa: BLE001 — never break an import
                                pass
                else:
                    status = Transaction.Status.REVIEW

                if ben_scheme is not None and ben_scheme.fund_id:
                    # the fund is known with certainty, so the receipt is allocated
                    # and the ledger is right immediately. WHOSE money it is goes to
                    # the benevolent intake queue below — an unattributed receipt is
                    # still a banked receipt.
                    dept = ben_scheme.fund
                    dev_group = None
                    split_fund = None
                    status = Transaction.Status.AUTO

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
                    txn = Transaction.objects.create(
                        amount=amount, department=dept, dev_group=dev_group,
                        core_ref=core_ref, bank_receipt=receipt, **common)

                    if ben_scheme is not None and txn.confirmed:
                        # The money is banked and in the ledger. NOW work out whose
                        # it is — and if that fails, it fails into a queue, not into
                        # a hole. Never allowed to break an import: a matching
                        # failure must not cost the church its bank statement.
                        try:
                            from benevolent.services.engine import intake as ben_intake
                            ben_intake(txn, scheme=ben_scheme)
                        except Exception as _e:  # noqa: BLE001
                            row_errors.append(
                                f"{core_ref or receipt}: receipted, but benevolent "
                                f"allocation failed ({type(_e).__name__}). The money is "
                                f"in the fund; attach it to a member by hand.")

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
    import_obj.reversals_skipped = reversals_skipped
    import_obj.status = StatementImport.Status.DONE
    import_obj.save()

    # Clear the cheques the bank has now shown as debited.
    #
    # `clear_for_bank_debit` / `suggest_instrument_for_debit` have existed, been
    # tested, and been wired into the debit review queue all along — but the queue
    # was permanently empty, because a bank exporting no debit column had every
    # debit row silently discarded by the parser. The machinery had nothing to act
    # on. It does now.
    #
    # A cheque NUMBER match is exact and needs no confirmation; anything less than
    # that is left for the debit queue and a person. Best-effort: a failure here
    # must never lose an import that has already posted.
    try:
        from cashbook.services.payments import auto_clear_cheques_for_debits
        debits = list(Transaction.objects.filter(
            statement_import=import_obj, direction=Transaction.Direction.DEBIT))
        if debits:
            auto_clear_cheques_for_debits(debits, import_obj.uploaded_by)
    except Exception:  # noqa: BLE001
        from core.utils import log_exception
        log_exception("statements/services/importer.py: auto-clear cheques")

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
