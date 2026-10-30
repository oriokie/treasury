"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from core.utils import parse_period, safe_json
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from ..services import balances
from ..exports import csv_response
from ..services.devgroups import balanced_partition as _balanced_partition  # noqa: E402
from core.models import SiteConfig
from ..exports import xlsx_response
from ._shared import PeriodMixin
from .summaries import _export


class DevGroupUnassignedView(RightRequiredMixin, TemplateView):
    """Development contributions sitting on the parent Development fund without a specific
    group — list them and reassign to the correct group for accurate per-group
    totals. Open to anyone with the development-offering allocation right."""
    required_right = "allocate_dev_offering"
    template_name = "reports/dev_unassigned.html"

    def _qs(self):
        from departments.models import Department
        return (Transaction.objects.active()
                .filter(direction=Transaction.Direction.CREDIT, confirmed=True,
                        department__category=Department.Category.DEVELOPMENT,
                        dev_group__isnull=True,
                        # loan financing on a Development fund is NOT a member
                        # development contribution — keep it out of the
                        # unassigned queue and every dev-group figure
                        excluded_from_income=False)
                .select_related("department", "member", "member__dev_group")
                .order_by("-date"))

    def get_context_data(self, **kwargs):
        from departments.models import DevelopmentGroup
        from ..services.dev_assign import suggest_groups_for_many
        ctx = super().get_context_data(**kwargs)
        qs = self._qs()
        items = list(qs[:500])
        suggestions = suggest_groups_for_many(items)
        for t in items:
            g, reason = suggestions.get(t.id, (None, ""))
            t.suggested_group = g
            t.suggest_reason = reason
        ctx["items"] = items
        ctx["suggested_count"] = sum(1 for t in items if t.suggested_group)
        ctx["count"] = qs.count()
        ctx["total"] = qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        ctx["groups"] = DevelopmentGroup.objects.filter(active=True).order_by("number")
        return ctx

    def _sync_envelope_groups(self, ids, grp):
        try:
            from envelopes.models import EnvelopeLine
            EnvelopeLine.objects.filter(
                transaction_id__in=ids, dev_group__isnull=True
            ).update(dev_group=grp)
        except Exception:
            from core.utils import log_exception as _lx
            _lx("reports/views.py")

    def post(self, request):
        from collections import defaultdict
        from departments.models import DevelopmentGroup
        from ..services.dev_assign import suggest_groups_for_many

        # Apply each row's suggested group in one go (no bulk group picker).
        if request.POST.get("assign_suggested"):
            items = list(self._qs()[:2000])
            suggestions = suggest_groups_for_many(items)
            by_group = defaultdict(list)
            for t in items:
                g, _ = suggestions.get(t.id, (None, ""))
                if g:
                    by_group[g.id].append(t.id)
            if not by_group:
                messages.error(
                    request,
                    "No suggested groups to apply — none of the unassigned "
                    "offerings have a clear suggestion.")
                return redirect("dev_unassigned")
            n = 0
            for gid, ids in by_group.items():
                grp = DevelopmentGroup.objects.filter(pk=gid).first()
                if not grp:
                    continue
                n += (Transaction.objects
                      .filter(id__in=ids, dev_group__isnull=True)
                      .update(dev_group=grp))
                self._sync_envelope_groups(ids, grp)
            messages.success(
                request,
                f"Assigned {n} development contribution(s) using suggested groups.")
            return redirect("dev_unassigned")

        # Selected rows (checkboxes). Optional parallel ``pick_grp`` list —
        # same order as ``txn`` — for per-row group picks (queue-style UI).
        ids = request.POST.getlist("txn")
        pick_grps = request.POST.getlist("pick_grp")
        if request.POST.get("all"):
            ids = [str(i) for i in self._qs().values_list("id", flat=True)]
            pick_grps = []
        if not ids:
            messages.error(request, "Select at least one contribution to assign.")
            return redirect("dev_unassigned")

        use_picks = (len(pick_grps) == len(ids)
                     and any((g or "").strip() for g in pick_grps))
        if use_picks:
            by_group = defaultdict(list)
            for i, raw_id in enumerate(ids):
                try:
                    tid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                raw_g = (pick_grps[i] if i < len(pick_grps) else "").strip()
                if not raw_g:
                    continue
                try:
                    by_group[int(raw_g)].append(tid)
                except (TypeError, ValueError):
                    continue
            if not by_group:
                messages.error(request, "Choose a group on each selected row.")
                return redirect("dev_unassigned")
            n = 0
            labels = []
            for gid, txn_ids in by_group.items():
                grp = DevelopmentGroup.objects.filter(pk=gid).first()
                if not grp:
                    continue
                n += (Transaction.objects
                      .filter(id__in=txn_ids, dev_group__isnull=True)
                      .update(dev_group=grp))
                self._sync_envelope_groups(txn_ids, grp)
                labels.append(grp.label)
            if n:
                messages.success(
                    request,
                    f"Assigned {n} development contribution(s)"
                    + (f" across {len(labels)} group(s)." if len(labels) > 1
                       else f" to {labels[0]}."))
            else:
                messages.warning(request, "Nothing was assigned.")
            return redirect("dev_unassigned")

        gid = request.POST.get("dev_group")
        grp = DevelopmentGroup.objects.filter(pk=gid).first()
        if not grp:
            messages.error(request, "Choose a group and at least one contribution to assign.")
            return redirect("dev_unassigned")
        n = (Transaction.objects.filter(id__in=ids, dev_group__isnull=True)
             .update(dev_group=grp))
        self._sync_envelope_groups(ids, grp)
        messages.success(request, f"Assigned {n} development contribution(s) to {grp.label}.")
        return redirect("dev_unassigned")

class DevGroupProgressView(PeriodMixin, TemplateView):
    template_name = "reports/dev_groups.html"

    def get(self, request, *args, **kwargs):
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from ..exports import csv_response, xlsx_response
            from core.models import SiteConfig
            s, e = self.period()
            rows = balances.dev_group_progress(s, e)
            header = ["Group", "Collected", "Target", "Balance to target", "% complete"]
            out = [[(r["group"].label if hasattr(r["group"], "label") else r["group"].name),
                    float(r.get("collected") or 0), float(r.get("target") or 0),
                    float(r.get("balance") or 0), float(r.get("pct") or 0)] for r in rows]
            title = f"Development groups {s:%d %b %Y}–{e:%d %b %Y}"
            if export == "xlsx":
                return xlsx_response("dev_groups.xlsx", header, out, title=title,
                                     church=SiteConfig.get().church_name)
            return csv_response("dev_groups.csv", header, out)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = balances.dev_group_progress(ctx["start"], ctx["end"])
        from departments.models import Department
        un = (Transaction.objects.active().filter(
                direction=Transaction.Direction.CREDIT, confirmed=True,
                department__category=Department.Category.DEVELOPMENT,
                dev_group__isnull=True, excluded_from_income=False))
        ctx["unassigned_count"] = un.count()
        ctx["unassigned_total"] = un.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        return ctx

class DevGroupBuilderView(RightRequiredMixin, TemplateView):
    """Propose N development groups balanced by members' historical development
    giving (capability), so each group carries a comparable giving capacity. By
    default this only produces a downloadable Excel/CSV proposal and changes no
    data; a treasurer can enable the live "apply" action in settings.

    Members with fewer than ``MIN_GIFTS`` development contributions are left out
    of the balance (they would distort capacity) and listed separately.
    """
    required_right = "build_dev_groups"
    template_name = "reports/dev_group_builder.html"
    MIN_GIFTS = 5

    def _member_rows(self):
        """``(eligible, low_activity)`` for balancing vs the red side list.

        Each item is ``(id, name, phone, amount, gift_count)``.
        """
        from members.models import Member
        rows = {r["member"]: (r["t"] or Decimal(0), r["n"] or 0)
                for r in (Transaction.objects.filter(
                    member__isnull=False, direction=Transaction.Direction.CREDIT,
                    confirmed=True, excluded_from_income=False,
                    department__category=Department.Category.DEVELOPMENT)
                    .values("member")
                    .annotate(t=Sum("amount"), n=Count("id")))}
        eligible, low = [], []
        for m in Member.objects.filter(active=True):
            amt, n = rows.get(m.id, (Decimal(0), 0))
            row = (m.id, m.name, m.phone or "", amt, n)
            if n >= self.MIN_GIFTS:
                eligible.append(row)
            else:
                low.append(row)
        low.sort(key=lambda x: (x[4], x[3]), reverse=True)
        return eligible, low

    @staticmethod
    def _partition_items(rows):
        """Strip gift_count for ``balanced_partition`` (id, name, phone, weight)."""
        return [(mid, name, phone, amt) for mid, name, phone, amt, _n in rows]

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") in ("xlsx", "csv"):
            return self._export(request)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from core.models import SiteConfig
        ctx = super().get_context_data(**kwargs)
        try:
            n = int(self.request.GET.get("n") or 0)
        except ValueError:
            n = 0
        eligible, low = self._member_rows()
        ctx["member_count"] = len(eligible)
        ctx["low_activity"] = [
            {"id": mid, "name": name, "phone": phone, "weight": amt, "gifts": n}
            for mid, name, phone, amt, n in low]
        ctx["low_activity_count"] = len(low)
        ctx["min_gifts"] = self.MIN_GIFTS
        ctx["total_capability"] = sum((w for _, _, _, w, _ in eligible), Decimal(0))
        ctx["n"] = n
        ctx["apply_enabled"] = SiteConfig.get().dev_group_builder_apply
        items = self._partition_items(eligible)
        if 2 <= n <= 50 and items:
            buckets = _balanced_partition(items, n)
            for i, b in enumerate(buckets, 1):
                b["number"] = i
            ctx["preview"] = buckets
            totals = [b["total"] for b in buckets]
            ctx["spread"] = (max(totals) - min(totals)) if totals else Decimal(0)
        return ctx

    def _export(self, request):
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        try:
            n = int(request.GET.get("n") or 0)
        except ValueError:
            n = 0
        eligible, low = self._member_rows()
        items = self._partition_items(eligible)
        header = ["Group", "Member", "Phone", "Development giving (capability)",
                  "Gifts", "Status"]
        rows = []
        if 2 <= n <= 50 and items:
            buckets = _balanced_partition(items, n)
            gift_by_id = {mid: n for mid, _, _, _, n in eligible}
            for i, b in enumerate(buckets, 1):
                for m in sorted(b["members"], key=lambda x: x["weight"], reverse=True):
                    rows.append([f"Group {i}", m["name"], m["phone"],
                                 float(m["weight"]), gift_by_id.get(m["id"], 0),
                                 "Balanced"])
        else:
            header = ["Member", "Phone", "Development giving (capability)",
                      "Gifts", "Status"]
            for mid, name, phone, w, n_gifts in sorted(
                    eligible, key=lambda x: x[3], reverse=True):
                rows.append([name, phone, float(w), n_gifts, "Eligible"])
        for mid, name, phone, w, n_gifts in low:
            if 2 <= n <= 50 and items:
                rows.append(["—", name, phone, float(w), n_gifts,
                             f"Low activity (<{self.MIN_GIFTS} gifts)"])
            else:
                rows.append([name, phone, float(w), n_gifts,
                             f"Low activity (<{self.MIN_GIFTS} gifts)"])
        fname = f"development-groups-{n}" if n else "development-members"
        if request.GET["export"] == "csv":
            return csv_response(fname + ".csv", header, rows)
        return xlsx_response(fname + ".xlsx", header, rows,
            title=(f"Proposed {n} balanced development groups" if n
                   else "Development giving by member"),
            church=SiteConfig.get().church_name)

    def post(self, request):
        from django.db import transaction
        from departments.models import DevelopmentGroup
        from members.models import Member
        from core.models import SiteConfig
        if not SiteConfig.get().dev_group_builder_apply:
            messages.error(request, "Creating groups is turned off — download the "
                "Excel proposal instead, or enable it in Settings → Channels.")
            return redirect(f"{request.path}?n={request.POST.get('n') or ''}")
        try:
            n = int(request.POST.get("n") or 0)
        except ValueError:
            n = 0
        if not (2 <= n <= 50):
            messages.error(request, "Choose between 2 and 50 groups.")
            return redirect(f"{request.path}?n={n}")
        prefix = (request.POST.get("prefix") or "Group").strip()[:40]
        eligible, low = self._member_rows()
        items = self._partition_items(eligible)
        if not items:
            messages.error(
                request,
                f"No members with at least {self.MIN_GIFTS} development "
                "contributions to group.")
            return redirect(request.path)
        buckets = _balanced_partition(items, n)
        with transaction.atomic():
            # reuse groups 1..n, deactivate any beyond n
            DevelopmentGroup.objects.filter(number__gt=n).update(active=False)
            for i, b in enumerate(buckets, 1):
                grp, _ = DevelopmentGroup.objects.get_or_create(number=i)
                grp.name = f"{prefix} {i}"
                grp.active = True
                grp.save()
                ids = [m["id"] for m in b["members"]]
                Member.objects.filter(id__in=ids).update(dev_group=grp)
        skipped = len(low)
        messages.success(
            request,
            f"Built {n} balanced development groups from {len(items)} members "
            f"({skipped} with fewer than {self.MIN_GIFTS} gifts left unassigned).")
        return redirect("report_dev_groups")

class DevGroupMembersView(PeriodMixin, TemplateView):
    """Members and their contributions to one development group, for the group
    leader's reconciliation. Can be emailed to the leader if an email is set."""
    template_name = "reports/dev_group_members.html"

    def get_context_data(self, **kwargs):
        from django.shortcuts import get_object_or_404
        from departments.models import DevelopmentGroup
        ctx = super().get_context_data(**kwargs)
        group = get_object_or_404(DevelopmentGroup, pk=kwargs["pk"])
        data = balances.dev_group_members(group, ctx["start"], ctx["end"])
        ctx.update({"group": group, "rows": data["rows"], "grand_total": data["total"]})
        return ctx

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Member", "Phone", "Contributions", "Total"]
            rows = [[r["name"], r["phone"], r["count"], r["total"]] for r in ctx["rows"]]
            rows.append(["TOTAL", "", "", ctx["grand_total"]])
            return _export(request, f"devgroup_{ctx['group'].number}_members",
                           header, rows, f"{ctx['group'].label} — member contributions")
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        """Email the report to the group leader."""
        ctx = self.get_context_data(**kwargs)
        group = ctx["group"]
        if not group.leader_email:
            messages.error(request, "This group has no leader email set. Add one in the "
                                    "development group settings.")
            return redirect(request.get_full_path())
        from core.services.email import send_email, is_configured
        if not is_configured():
            messages.error(request, "Email isn't configured. Set it up in Settings → Email.")
            return redirect(request.get_full_path())
        lines = [f"{r['name']}: {r['total']:,.2f} ({r['count']} donation(s))" for r in ctx["rows"]]
        body = (f"{group.label} — member contributions\n"
                f"Period: {ctx['start']:%d %b %Y} to {ctx['end']:%d %b %Y}\n\n"
                + ("\n".join(lines) if lines else "No contributions in this period.")
                + f"\n\nTOTAL: {ctx['grand_total']:,.2f}")
        ok, detail = send_email(f"{group.label} contribution report",
                                body, group.leader_email)
        (messages.success if ok else messages.error)(
            request, f"Report to {group.leader_email}: {detail}")
        return redirect(request.get_full_path())

class DevGroupAllExcelView(ReportAccessMixin, View):
    """One workbook for all development groups: a summary sheet plus a per-group
    sheet of member contributions, for detailed offline analysis."""
    def get(self, request):
        import io
        import openpyxl
        from django.http import HttpResponse
        from departments.models import DevelopmentGroup
        start, end = parse_period(request)[:2] if request.GET.get("start") else (None, None)
        wb = openpyxl.Workbook()
        summary = wb.active
        summary.title = "Summary"
        summary.append(["Group", "Leader", "Email", "Collected", "Target",
                        "Balance", "% complete"])
        progress = {p["group"].id: p for p in balances.dev_group_progress()}
        for g in DevelopmentGroup.objects.filter(active=True):
            pr = progress.get(g.id, {})
            summary.append([g.label, g.leader_name, g.leader_email,
                            float(pr.get("collected", 0)),
                            float(pr.get("target", 0) or 0),
                            float(pr.get("balance", 0)), pr.get("pct", 0)])
            data = balances.dev_group_members(g, start, end)
            title = ("G%d" % g.number)[:28]
            ws = wb.create_sheet(title=title)
            ws.append([g.label + (" — " + g.leader_name if g.leader_name else "")])
            ws.append(["Member", "Phone", "Contributions", "Total"])
            for r in data["rows"]:
                ws.append([r["name"], r["phone"], r["count"], float(r["total"])])
            ws.append(["TOTAL", "", "", float(data["total"])])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="development_groups_detailed.xlsx"'
        return resp

class DevGroupEmailAllView(TreasurerRequiredMixin, View):
    """Email each group's member report to its leader, spacing sends 30s apart so
    the mail provider doesn't flag a burst as spam. Runs in the background."""
    def post(self, request):
        import threading
        from departments.models import DevelopmentGroup
        from core.services.email import is_configured
        if not is_configured():
            messages.error(request, "Email isn't configured. Set it up in Settings → Email.")
            return redirect("report_dev_groups")
        start, end = (parse_period(request)[:2] if request.GET.get("start")
                      else (None, None))
        groups = list(DevelopmentGroup.objects.filter(active=True).exclude(leader_email=""))
        if not groups:
            messages.info(request, "No groups have a leader email set.")
            return redirect("report_dev_groups")
        ids = [g.id for g in groups]
        threading.Thread(target=self._send_all, args=(ids, start, end), daemon=True).start()
        messages.success(request, f"Queued reports for {len(ids)} group leader(s). "
                                  f"They'll be sent about 30 seconds apart to avoid "
                                  f"spam filters.")
        return redirect("report_dev_groups")

    @staticmethod
    def _send_all(group_ids, start, end):
        import time
        from django.db import connection
        from departments.models import DevelopmentGroup
        from core.services.email import send_email
        from core.models import Notification
        try:
            for i, gid in enumerate(group_ids):
                g = DevelopmentGroup.objects.filter(pk=gid).first()
                if not g or not g.leader_email:
                    continue
                data = balances.dev_group_members(g, start, end)
                lines = [f"{r['name']}: {r['total']:,.2f} ({r['count']} donation(s))"
                         for r in data["rows"]]
                body = (f"{g.label} — member contributions\n\n"
                        + ("\n".join(lines) if lines else "No contributions in the period.")
                        + f"\n\nTOTAL: {data['total']:,.2f}")
                ok, detail = send_email(f"{g.label} contribution report",
                                        body, g.leader_email)
                Notification.objects.create(
                    kind=Notification.Kind.GENERAL,
                    message=f"{g.label} report to {g.leader_email}: "
                            f"{'sent' if ok else 'failed — ' + detail[:80]}")
                if i < len(group_ids) - 1:
                    time.sleep(30)
        finally:
            connection.close()
