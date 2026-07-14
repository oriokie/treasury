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

from core.permissions import BenevolentFinanceMixin, BenevolentRegistrationMixin

from .models import BenevolentScheme, RegistrationType, SchemeDependant, SchemeMembership
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
                  "mark_paid_up"]
        for i in range(1, DEP_SLOTS + 1):
            header += [f"dependant{i}_name", f"dependant{i}_relationship",
                      f"dependant{i}_phone"]
        w.writerow(header)
        w.writerow(["Mary Wanjiru", "0722111222", "2023-01-15", "HOUSEHOLD",
                   "The Wanjiru Household", "1",
                   "John Wanjiru", "SPOUSE", "0733444555", "", "", "", "", "", ""])
        w.writerow(["Peter Otieno", "0700888999", "2022-06-01", "INDIVIDUAL", "", "1",
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
        from .models import BenevolentCase

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
        case = None
        case_no = (row.get("case_number") or "").strip()
        if case_no:
            case = BenevolentCase.objects.filter(
                scheme=scheme, number__iexact=case_no).first()
            if case is None:
                raise ValidationError(
                    f"No case numbered '{case_no}' in {scheme.name}.")

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
