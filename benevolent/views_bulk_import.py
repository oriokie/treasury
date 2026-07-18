"""Bulk-import an existing membership roster into a scheme.

For a church that already runs a scheme on paper or in a spreadsheet: rather
than registering fifty or a hundred members one at a time through the normal
form, upload a CSV and bring the whole roster in at once, dependants
included, with "already paid up" marked honestly rather than silently
assumed.

Deliberately NOT a new code path for what happens to each row: every row
becomes an ordinary `registry.register()` call (the same function the
one-at-a-time Register screen uses), so nothing about how a membership
behaves afterwards depends on how it was created.
"""
import csv as _csv
import datetime as _dt
import io as _io
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import (BenevolentApproveMixin, BenevolentFinanceMixin,
                              BenevolentRegistrationMixin)

from .models import BenevolentCase, BenevolentScheme, RegistrationType, SchemeDependant, SchemeMembership
from .services import cases as cases_svc
from .services import contributions as contrib_svc
from .services import engine as engine_svc
from .services import registry as reg_svc

DEP_SLOTS = 3   # dependant1_*, dependant2_*, dependant3_* — see the template


class BulkMembershipImportView(BenevolentRegistrationMixin, View):
    template_name = "benevolent/bulk_import.html"

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        if request.GET.get("template"):
            return self._template_csv()
        return render(request, self.template_name, {"scheme": scheme, "dep_slots": range(1, DEP_SLOTS + 1)})

    def _template_csv(self):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="benevolent_roster_template.csv"'
        w = _csv.writer(resp)
        header = ["name", "phone", "joined_on", "registration_type", "household_name",
                  "registration_fee_paid", "mark_paid_up"]
        for i in range(1, DEP_SLOTS + 1):
            header += [f"dependant{i}_name", f"dependant{i}_relationship",
                      f"dependant{i}_phone"]
        w.writerow(header)
        w.writerow(["Mary Wanjiru", "0722111222", "2023-01-15", "HOUSEHOLD",
                   "The Wanjiru Household", "1", "1",
                   "John Wanjiru", "SPOUSE", "0733444555", "", "", "", "", "", ""])
        w.writerow(["Peter Otieno", "0700888999", "2022-06-01", "INDIVIDUAL", "", "1", "1",
                   "", "", "", "", "", "", "", "", ""])
        return resp

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a CSV file — download the template if you "
                                    "don't have one yet.")
            return redirect("benevolent_bulk_import", pk=pk)
        try:
            text = _io.TextIOWrapper(f.file, encoding="utf-8-sig")
            reader = list(_csv.DictReader(text))
        except Exception:
            from core.utils import log_exception as _lx
            _lx("benevolent/views_bulk_import.py")
            messages.error(request, "Could not read that file — download the template "
                                    "and use its columns.")
            return redirect("benevolent_bulk_import", pk=pk)
        if not reader:
            messages.warning(request, "No rows found in that file.")
            return redirect("benevolent_bulk_import", pk=pk)

        imported, skipped, problems = 0, 0, []
        for i, row in enumerate(reader, start=2):   # row 1 is the header
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                with db_tx.atomic():
                    self._import_row(scheme, row, user=request.user)
                imported += 1
            except ValidationError as e:
                skipped += 1
                problems.append((i, name, "; ".join(e.messages)))
            except Exception as e:  # noqa: BLE001 — one bad row must not sink the batch
                skipped += 1
                problems.append((i, name, str(e)))

        summary = f"{imported} membership(s) imported."
        if skipped:
            summary += f" {skipped} row(s) skipped — see below."
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
        return render(request, self.template_name, {
            "scheme": scheme, "dep_slots": range(1, DEP_SLOTS + 1),
            "imported": imported, "skipped": skipped, "problems": problems,
        })

    def _import_row(self, scheme, row, *, user):
        from members.services.matching import match_or_create_member
        name = row["name"].strip()
        phone = (row.get("phone") or "").strip()
        member, _how = match_or_create_member(name, phone)

        joined_raw = (row.get("joined_on") or "").strip()
        joined_on = self._parse_date(joined_raw) if joined_raw else _dt.date.today()

        reg_type = (row.get("registration_type") or "INDIVIDUAL").strip().upper()
        if reg_type not in RegistrationType.values:
            reg_type = RegistrationType.INDIVIDUAL
        household_name = (row.get("household_name") or "").strip()

        dependants = []
        for i in range(1, DEP_SLOTS + 1):
            dname = (row.get(f"dependant{i}_name") or "").strip()
            if not dname:
                continue
            rel = (row.get(f"dependant{i}_relationship") or "OTHER").strip().upper()
            if rel not in SchemeDependant.Relationship.values:
                rel = SchemeDependant.Relationship.OTHER
            dependants.append({
                "name": dname, "relationship": rel,
                "phone": (row.get(f"dependant{i}_phone") or "").strip(),
            })

        existing = SchemeMembership.objects.filter(scheme=scheme, member=member).first()
        if existing and existing.status in SchemeMembership.LIVE_STATUSES:
            raise ValidationError(
                f"{member.name} is already enrolled ({existing.number}).")

        m = reg_svc.register(
            scheme, member, joined_on=joined_on, user=user,
            registration_type=reg_type, household_name=household_name,
            dependants=dependants, notify=False)

        # A migrated member is being brought in as ALREADY established, not
        # as someone newly awaiting approval — so admit them immediately
        # regardless of what the current policy's approval mode would
        # otherwise require of a brand-new registration.
        if m.status == SchemeMembership.Status.PENDING:
            m = reg_svc.admit(m, user=user, on=joined_on,
                              reason="Imported from the existing roster — already "
                                    "active per prior records.", notify=False)

        # The registration fee is a SEPARATE obligation from ongoing dues (see
        # benevolent.services.obligations — a member with an unpaid fee gets it
        # settled before anything else regardless of what a narration says), so
        # importing a roster needs its own column for it rather than being
        # folded into mark_paid_up, which only ever touched dues arrears.
        reg_paid = (row.get("registration_fee_paid") or "").strip().lower() \
            not in ("", "0", "false", "no")
        if reg_paid and not m.registration_fee_paid:
            m.registration_fee_paid = True
            m.save(update_fields=["registration_fee_paid"])

        mark_paid = (row.get("mark_paid_up") or "").strip() not in ("", "0", "false", "no")
        if mark_paid:
            owed = contrib_svc.arrears_for(m)
            if owed > 0:
                engine_svc.waive_on_import(
                    m, amount=owed, user=user,
                    reason=f"Migrated from the church's existing records as paid up "
                          f"on {_dt.date.today():%d %b %Y} — prior payment history "
                          f"predates this system and was not itemised.")
        return m

    @staticmethod
    def _parse_date(raw):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return _dt.datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        raise ValidationError(f"Could not read the date '{raw}' — use YYYY-MM-DD.")


class BulkContributionImportView(BenevolentFinanceMixin, View):
    """Import a history of contributions — for a church bringing years of
    payment records into the system alongside a roster, rather than typing
    each receipt in one at a time.

    Every row becomes an ordinary `contributions.record_contribution()`
    call — the same function the one-at-a-time form uses — so a bulk-
    imported contribution behaves identically to one entered by hand: it
    posts to the ledger, counts towards arrears and standing, and shows up
    everywhere a contribution normally does.
    """
    template_name = "benevolent/bulk_import_contributions.html"

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        if request.GET.get("template"):
            return self._template_csv()
        return render(request, self.template_name, {"scheme": scheme})

    def _template_csv(self):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="benevolent_contributions_template.csv"'
        w = _csv.writer(resp)
        w.writerow(["member_name", "member_phone", "date", "amount", "kind",
                   "case_number", "period_label", "channel", "note"])
        w.writerow(["Mary Wanjiru", "0722111222", "2026-01-15", "100", "DUES",
                   "", "2026-01", "CASH", "January dues"])
        w.writerow(["Peter Otieno", "0700888999", "2026-02-10", "500", "LEVY",
                   "BEN-2026-0003", "", "BANK", "Levy towards the Otieno bereavement"])
        w.writerow(["John Kamau", "0711222333", "", "", "", "BEN-2026-0003", "", "",
                   "Did not contribute — leave the amount blank"])
        return resp

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a CSV file — download the template if you "
                                    "don't have one yet.")
            return redirect("benevolent_bulk_import_contributions", pk=pk)
        try:
            text = _io.TextIOWrapper(f.file, encoding="utf-8-sig")
            reader = list(_csv.DictReader(text))
        except Exception:
            messages.error(request, "Could not read that file — download the template "
                                    "and use its columns.")
            return redirect("benevolent_bulk_import_contributions", pk=pk)
        if not reader:
            messages.warning(request, "No rows found in that file.")
            return redirect("benevolent_bulk_import_contributions", pk=pk)

        imported, skipped, problems = 0, 0, []
        # A row with a blank amount means "this member did NOT contribute". That
        # is recorded by the ABSENCE of a contribution — the levy roster derives
        # "unpaid" from having no payment, and always has. Writing a zero-value
        # contribution to say so would be worse than useless: it would put a
        # receipt in the ledger for money nobody gave.
        #
        # So such rows are counted and reported, not imported. A treasurer can
        # therefore upload a full case roster — everyone who was levied, paid or
        # not — and the import will do the right thing with both halves, and say
        # exactly what it did.
        not_contributed = 0
        total = Decimal("0")
        for i, row in enumerate(reader, start=2):
            name = (row.get("member_name") or "").strip()
            if not name:
                continue
            amount_raw = (row.get("amount") or "").strip()
            if not amount_raw or amount_raw in ("0", "0.0", "0.00"):
                not_contributed += 1
                continue
            try:
                with db_tx.atomic():
                    total += self._import_row(scheme, row, user=request.user)
                imported += 1
            except ValidationError as e:
                skipped += 1
                problems.append((i, name, "; ".join(e.messages)))
            except Exception as e:  # noqa: BLE001 — one bad row must not sink the batch
                skipped += 1
                problems.append((i, name, str(e)))

        summary = f"{imported} contribution(s) imported, totalling {total:,.2f}."
        if not_contributed:
            summary += (f" {not_contributed} row(s) had no amount — they are recorded as "
                        f"NOT having contributed, which is what an absent payment already "
                        f"means. They will show as unpaid on the levy roster.")
        if skipped:
            summary += f" {skipped} row(s) skipped — see below."
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
        return render(request, self.template_name, {
            "scheme": scheme, "imported": imported, "skipped": skipped,
            "problems": problems, "total": total,
            "not_contributed": not_contributed,
        })

    def _import_row(self, scheme, row, *, user):
        from members.services.matching import match_or_create_member

        name = row["member_name"].strip()
        phone = (row.get("phone") or row.get("member_phone") or "").strip()
        member, _how = match_or_create_member(name, phone)

        membership = (SchemeMembership.objects
                     .filter(scheme=scheme, member=member)
                     .exclude(status=SchemeMembership.Status.WITHDRAWN)
                     .order_by("-joined_on").first())
        if membership is None:
            raise ValidationError(
                f"{member.name} is not enrolled in {scheme.name} — import the roster "
                f"first, or check the name/phone.")

        # Which CASE is this a levy towards? Without this the money lands on no
        # case's roster at all, is inferred as VOLUNTARY rather than LEVY, and —
        # under a pooled policy, where the benefit IS whatever the levy collected
        # — makes the payout come out short. The manual contribution form had
        # exactly this gap; the bulk import had it too.
        #
        # Matches EITHER the system-assigned number (BENC-2026-0003) or, for a
        # case brought in via the historical case import, the SAME workbook
        # reference used there — a treasurer importing years of history should
        # not have to look up newly-issued numbers to cross-reference their own
        # spreadsheet's case column between the two upload files.
        case = None
        case_no = (row.get("case_number") or "").strip()
        if case_no:
            case = BenevolentCase.objects.filter(
                scheme=scheme, number__iexact=case_no).first()
            if case is None:
                case = BenevolentCase.objects.filter(
                    scheme=scheme, external_reference__iexact=case_no).first()
            if case is None:
                raise ValidationError(
                    f"No case numbered or referenced '{case_no}' in {scheme.name}.")

        date_raw = (row.get("date") or "").strip()
        if not date_raw:
            raise ValidationError("A date is required for a contribution.")
        date = BulkMembershipImportView._parse_date(date_raw)

        amount_raw = (row.get("amount") or "").strip()
        try:
            amount = Decimal(amount_raw)
        except Exception:
            raise ValidationError(f"Could not read the amount '{amount_raw}'.")

        kind = (row.get("kind") or "").strip().upper() or None
        channel = (row.get("channel") or "CASH").strip().upper()
        if channel not in ("CASH", "BANK"):
            channel = "CASH"

        contrib_svc.record_contribution(
            scheme, date=date, amount=amount, user=user, membership=membership,
            member=member, case=case, channel=channel,
            period_label=(row.get("period_label") or None),
            note=(row.get("note") or "").strip(), kind=kind)
        return amount


class BulkCaseImportView(BenevolentApproveMixin, View):
    """Bring cases already decided BEFORE this system existed straight to
    their known outcome, at scale — a church's workbook naming the cases it
    has already paid out or closed over the years, not new claims awaiting a
    decision.

    Every row becomes an ordinary `cases.import_historical_case()` call — the
    one place a historical case is created, so numbering, the audit log entry,
    and the historical-payout handling are never something a bulk path gets
    to skip. See that function's own docstring for why a case lands directly
    at its outcome rather than being re-decided through submit/assess/approve
    (today's eligibility rules and policy version would apply to a decision
    the church already made under whatever was in force at the time), and why
    a paid amount is recorded as a marked historical payout rather than a live
    cashbook.Expense (which would assert money is leaving the church today).

    Gated on the Approve right, not the ordinarily-narrower Case-Officer or
    Finance-Officer rights: a bulk row can set a case straight to APPROVED,
    PARTLY_PAID or PAID with an approved amount attached — that is a money
    decision, even though it is a decision already made in the past that this
    tool is only recording.
    """
    template_name = "benevolent/bulk_import_cases.html"

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        if request.GET.get("template"):
            return self._template_csv()
        return render(request, self.template_name, {
            "scheme": scheme,
            "statuses": [(s.value, s.label) for s in cases_svc.IMPORTABLE_STATUSES],
        })

    def _template_csv(self):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="benevolent_cases_template.csv"'
        w = _csv.writer(resp)
        w.writerow(["external_reference", "member_name", "member_phone",
                   "event_type_code", "event_date", "reported_date",
                   "beneficiary_name", "beneficiary_relationship", "status",
                   "claimed_amount", "approved_amount", "paid_amount",
                   "paid_date", "payee_name", "description"])
        w.writerow(["2019/014", "Mary Wanjiru", "0722111222", "BER", "2019-03-04",
                   "2019-03-05", "", "", "CLOSED", "50000", "50000", "50000",
                   "2019-03-10", "Wanjiru Family", "Bereavement of spouse"])
        w.writerow(["2020/031", "Peter Otieno", "0700888999", "HOSP", "2020-07-19",
                   "2020-07-20", "", "", "PAID", "12000", "10000", "10000",
                   "2020-07-25", "", "Hospital admission, partially assessed down"])
        w.writerow(["", "John Kamau", "0711222333", "SCHOOL", "2021-01-10", "",
                   "", "", "DRAFT", "8000", "", "", "", "",
                   "Reported but never decided — genuinely still open"])
        return resp

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a CSV file — download the template if you "
                                    "don't have one yet.")
            return redirect("benevolent_bulk_import_cases", pk=pk)
        try:
            text = _io.TextIOWrapper(f.file, encoding="utf-8-sig")
            reader = list(_csv.DictReader(text))
        except Exception:
            messages.error(request, "Could not read that file — download the template "
                                    "and use its columns.")
            return redirect("benevolent_bulk_import_cases", pk=pk)
        if not reader:
            messages.warning(request, "No rows found in that file.")
            return redirect("benevolent_bulk_import_cases", pk=pk)

        imported, skipped, problems = 0, 0, []
        total_paid = Decimal("0")
        for i, row in enumerate(reader, start=2):   # row 1 is the header
            ext_ref = (row.get("external_reference") or "").strip()
            name = (row.get("member_name") or "").strip()
            label = ext_ref or name or f"row {i}"
            if not name and not (row.get("beneficiary_name") or "").strip():
                continue   # a genuinely blank row
            try:
                with db_tx.atomic():
                    case, paid = self._import_row(scheme, row, user=request.user)
                imported += 1
                total_paid += paid
            except ValidationError as e:
                skipped += 1
                problems.append((i, label, "; ".join(e.messages)))
            except Exception as e:  # noqa: BLE001 — one bad row must not sink the batch
                skipped += 1
                problems.append((i, label, str(e)))

        summary = f"{imported} case(s) imported."
        if total_paid:
            summary += f" {total_paid:,.2f} recorded as historically paid across them."
        if skipped:
            summary += f" {skipped} row(s) skipped — see below."
            messages.warning(request, summary)
        else:
            messages.success(request, summary)
        return render(request, self.template_name, {
            "scheme": scheme,
            "statuses": [(s.value, s.label) for s in cases_svc.IMPORTABLE_STATUSES],
            "imported": imported, "skipped": skipped, "problems": problems,
            "total_paid": total_paid,
        })

    def _import_row(self, scheme, row, *, user):
        from members.services.matching import match_or_create_member
        from .models import BenevolentEventType

        name = (row.get("member_name") or "").strip()
        membership = None
        if name:
            phone = (row.get("member_phone") or "").strip()
            member, _how = match_or_create_member(name, phone)
            membership = (SchemeMembership.objects
                         .filter(scheme=scheme, member=member)
                         .exclude(status=SchemeMembership.Status.WITHDRAWN)
                         .order_by("-joined_on").first())
            if membership is None:
                raise ValidationError(
                    f"{member.name} is not enrolled in {scheme.name} — import the "
                    f"roster first, or check the name/phone. Leave member_name blank "
                    f"for a non-member claim, if the scheme allows one.")

        code = (row.get("event_type_code") or "").strip()
        if not code:
            raise ValidationError("event_type_code is required.")
        event_type = BenevolentEventType.objects.filter(scheme=scheme, code__iexact=code).first()
        if event_type is None:
            raise ValidationError(
                f"No event type coded '{code}' in {scheme.name}. Check "
                f"Schemes & policies → event types for the codes in use.")

        event_raw = (row.get("event_date") or "").strip()
        if not event_raw:
            raise ValidationError("event_date is required.")
        event_date = BulkMembershipImportView._parse_date(event_raw)
        reported_raw = (row.get("reported_date") or "").strip()
        reported_date = (BulkMembershipImportView._parse_date(reported_raw)
                         if reported_raw else event_date)

        status_raw = (row.get("status") or "CLOSED").strip().upper()
        try:
            status = BenevolentCase.Status(status_raw)
        except ValueError:
            raise ValidationError(
                f"'{status_raw}' is not a status — use one of: "
                + ", ".join(s.value for s in cases_svc.IMPORTABLE_STATUSES))

        def _dec(key):
            raw = (row.get(key) or "").strip()
            if not raw:
                return None
            try:
                return Decimal(raw)
            except Exception:
                raise ValidationError(f"Could not read {key} '{raw}' as an amount.")

        paid_raw = (row.get("paid_date") or "").strip()
        paid_date = BulkMembershipImportView._parse_date(paid_raw) if paid_raw else None
        paid_amount = _dec("paid_amount")

        case = cases_svc.import_historical_case(
            scheme, event_type=event_type, event_date=event_date,
            membership=membership,
            beneficiary_name=(row.get("beneficiary_name") or "").strip(),
            beneficiary_relationship=(row.get("beneficiary_relationship") or "").strip(),
            reported_date=reported_date,
            description=(row.get("description") or "").strip(),
            external_reference=(row.get("external_reference") or "").strip(),
            status=status, claimed_amount=_dec("claimed_amount"),
            approved_amount=_dec("approved_amount"), paid_amount=paid_amount,
            paid_date=paid_date, payee_name=(row.get("payee_name") or "").strip(),
            user=user)
        return case, (paid_amount or Decimal("0"))
