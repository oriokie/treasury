from decimal import Decimal

from core.utils import PrefPaginationMixin

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView, View

from core.permissions import DataEntryRequiredMixin, ReadAccessMixin, TreasurerRequiredMixin
from giving.models import Transaction
from .forms import MemberForm
from .models import Member, MemberPhone, PossibleDuplicate
from .models import normalize_phone
from .services.matching import MemberMergeConflict, merge_members


#: How the member list may be ordered. A register of givers whose only order is
#: alphabetical cannot answer either of the questions a treasurer actually
#: brings to it — who gives most, and who has stopped.
MEMBER_SORTS = {
    "name": ("name", "Name (A–Z)"),
    "given": ("-total_given", "Most given"),
    "recent": ("-last_gift", "Gave most recently"),
    "quiet": ("last_gift", "Longest since giving"),
    "newest": ("-created_at", "Recently added"),
}


def filter_members(params):
    """The member register, as narrowed and ordered by a request's parameters.

    Shared by the list and the CSV export deliberately. The export used to
    ignore the filters entirely, which was harmless while the only filters were
    group and source and became a trap the moment the page could be narrowed
    and sorted: you filter to a group, sort by giving, press Export, and get
    every member in the church in alphabetical order.
    """
    from django.db.models import Count, Max, Sum

    from core.metrics import income_credit_filter

    # What each member has given, annotated rather than counted per row: a
    # figure per member fetched in the template would be one query per member,
    # and this page is the one that lists every member there is.
    #
    # The definition is the registry's own `income_credit_filter` — the same one
    # the dashboard and every report count income with. A reversed or
    # unconfirmed credit is not something a member has given, and a member's
    # total has to mean the same thing here as everywhere else.
    counted = income_credit_filter(prefix="transaction__")
    qs = (Member.objects.select_related("dev_group")
          .annotate(
              total_given=Sum("transaction__amount", filter=counted),
              last_gift=Max("transaction__date", filter=counted),
              gift_count=Count("transaction", filter=counted)))
    q = params.get("q")
    group = params.get("group")
    source = params.get("source")
    member_type = params.get("type")
    status = params.get("status")
    sort = params.get("sort") or "name"
    if q:
        # Alternate numbers count. A member who pays from a second line is
        # still that member — MemberPhone records exactly that, and the
        # bank-statement matcher has always searched it. This screen did not,
        # so searching the number a treasurer actually has in front of them
        # found nobody.
        qs = qs.filter(
            Q(name__icontains=q) | Q(phone__icontains=q)
            | Q(phones__number__icontains=q)).distinct()
    if group:
        qs = qs.filter(group=group)
    if source:
        qs = qs.filter(source=source)
    if member_type:
        qs = qs.filter(member_type=member_type)
    # A member can be made inactive in bulk from this very page, and there was
    # then no way to see or find them again — no column, no filter, and the list
    # showed active and inactive mixed together with nothing to tell them apart.
    # Default to active, which is what "the members" means.
    if status == "inactive":
        qs = qs.filter(active=False)
    elif status != "all":
        qs = qs.filter(active=True)
    order, _label = MEMBER_SORTS.get(sort, MEMBER_SORTS["name"])
    field = order.lstrip("-")
    if field in ("total_given", "last_gift"):
        # A member who has never given sorts last either way round, rather than
        # heading the list on a NULL.
        from django.db.models import F
        direction = (F(field).desc(nulls_last=True) if order.startswith("-")
                     else F(field).asc(nulls_last=True))
        return qs.order_by(direction, "name")
    return qs.order_by(order)


class MemberListView(PrefPaginationMixin, ReadAccessMixin, ListView):
    model = Member
    template_name = "members/list.html"
    context_object_name = "members"
    paginate_by = 50

    def get_queryset(self):
        return filter_members(self.request.GET)

    def get_context_data(self, **kwargs):
        from django.db.models import Count, Q, Sum

        ctx = super().get_context_data(**kwargs)
        ctx["groups"] = Member.Group.choices
        ctx["member_types"] = Member.MemberType.choices
        ctx["sources"] = Member.Source.choices
        ctx["filters"] = self.request.GET
        ctx["sorts"] = [(k, v[1]) for k, v in MEMBER_SORTS.items()]
        ctx["sort"] = self.request.GET.get("sort") or "name"
        ctx["status"] = self.request.GET.get("status") or "active"
        ctx["dup_count"] = PossibleDuplicate.objects.filter(resolved=False).count()
        # Totals for the members this filter actually selected, not for the
        # page of them being shown — a summary that changed when you turned to
        # page two would be worse than no summary.
        totals = self.get_queryset().aggregate(
            n=Count("id"),
            given=Sum("total_given"),
            no_phone=Count("id", filter=Q(phone__isnull=True) | Q(phone="")))
        ctx["sum_members"] = totals["n"] or 0
        ctx["sum_given"] = totals["given"] or 0
        ctx["sum_no_phone"] = totals["no_phone"] or 0
        ctx["sum_inactive"] = Member.objects.filter(active=False).count()
        return ctx


class MemberDetailView(ReadAccessMixin, DetailView):
    model = Member
    template_name = "members/detail.html"
    context_object_name = "member"

    def get_context_data(self, **kwargs):
        import datetime as _dt
        from django.utils.dateparse import parse_date

        ctx = super().get_context_data(**kwargs)
        all_txns = (Transaction.objects.filter(member=self.object,
                    direction=Transaction.Direction.CREDIT)
                    .select_related("department").order_by("-date"))

        # A date filter, defaulting to the CURRENT YEAR — deliberately not the
        # current month, as the ledger and expense LISTS do.
        #
        # Those pages default to a month because they are unbounded: without a
        # bound they scan every row the church has ever recorded, and the
        # default exists to stop that. This page is already bounded to one
        # person, so there is no such cost — and the reason someone opens a
        # member's page is almost always to see what they have given, which a
        # one-month window would hide most of. Defaulting to a month here would
        # solve a problem this page does not have, at the price of the answer
        # the page exists to give.
        #
        # A year is the natural period for a member's giving (it is what the
        # annual member statement covers), the filter takes any range, and
        # "all time" is one click. The LIFETIME total stays on screen
        # regardless of the filter, so narrowing the window can never make a
        # member look like they have given less than they have.
        today = _dt.date.today()
        if not self.request.GET:
            start, end = _dt.date(today.year, 1, 1), today
            default_applied = True
        else:
            start = parse_date(self.request.GET.get("start") or "")
            end = parse_date(self.request.GET.get("end") or "")
            default_applied = False

        txns = all_txns
        if start:
            txns = txns.filter(date__gte=start)
        if end:
            txns = txns.filter(date__lte=end)

        ctx["transactions"] = txns[:100]
        ctx["shown_count"] = txns.count()
        ctx["period_given"] = txns.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        # unfiltered, always: a member's lifetime giving is not a function of
        # what date range someone happens to be looking at
        ctx["total_given"] = all_txns.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        ctx["f_start"] = start.isoformat() if start else ""
        ctx["f_end"] = end.isoformat() if end else ""
        ctx["date_default_applied"] = default_applied
        ctx["aliases"] = self.object.aliases.all()
        # Other numbers this member gives from (e.g. after a merge — see
        # members.services.matching.merge_members) — the primary number
        # above is used for receipting; these are shown so a treasurer can
        # actually see them, not just have them silently preserved in the
        # database.
        ctx["other_phones"] = self.object.phones.filter(is_primary=False)

        # Welfare schemes. The whole point of the benevolent module extending THIS
        # registry rather than keeping its own is that a member's page can show
        # their welfare standing without anyone having to reconcile two lists of
        # people. Includes households they are covered BY, not only ones they hold.
        try:
            from benevolent.models import SchemeDependant, SchemeMembership
            ctx["scheme_memberships"] = (
                SchemeMembership.objects.filter(member=self.object)
                .select_related("scheme").order_by("scheme__name"))
            ctx["covered_by"] = (
                SchemeDependant.objects.filter(member=self.object, active=True)
                .select_related("membership__scheme", "membership__member"))
        except Exception:  # noqa: BLE001 — the module may not be installed
            ctx["scheme_memberships"] = []
            ctx["covered_by"] = []
        return ctx


class MemberCreateView(DataEntryRequiredMixin, CreateView):
    model = Member
    form_class = MemberForm
    template_name = "members/form.html"

    def get_success_url(self):
        messages.success(self.request, "Member saved.")
        return reverse_lazy("member_detail", args=[self.object.pk])


class MemberPhoneAddView(DataEntryRequiredMixin, View):
    """Record another number a member gives from.

    Members give from more than one line — an M-Pesa number, a work handset, a
    number the family shares. Until now a second number could only reach the
    record as a side effect of merging two duplicate member rows, so a member
    who had never been duplicated had no way to have their other line
    recognised, and every payment from it went to the review queue.

    The number is stored on its own row rather than replacing the primary,
    because the primary is what receipts are addressed to and changing that is
    a different decision from "she also pays from this one".
    """

    def post(self, request, pk):
        member = get_object_or_404(Member, pk=pk)
        raw = (request.POST.get("number") or "").strip()
        label = (request.POST.get("label") or "").strip()[:40]
        number = normalize_phone(raw)
        if not number:
            messages.error(
                request,
                f"{raw!r} does not look like a phone number. Kenyan mobile "
                "numbers are ten digits starting 07 or 01.")
            return redirect("member_detail", pk=pk)
        if number == member.phone or member.phones.filter(number=number).exists():
            messages.info(request, f"{raw} is already on {member.name}'s record.")
            return redirect("member_detail", pk=pk)
        # A number already held for somebody else is refused rather than
        # duplicated: two members sharing a number makes every payment from it
        # ambiguous, and the honest fix is a merge, not a second copy.
        clash = Member.objects.filter(
            Q(phone=number) | Q(phones__number=number)).exclude(pk=member.pk).first()
        if clash:
            messages.error(
                request,
                f"{raw} is already held for {clash.name}. If these are the same "
                "person, merge the two records; if they genuinely share a "
                "handset, leave it on one of them so payments stay traceable.")
            return redirect("member_detail", pk=pk)
        MemberPhone.objects.create(member=member, number=number, label=label,
                                   is_primary=False)
        messages.success(
            request,
            f"{raw} added for {member.name}. Payments from it will now be "
            "recognised as theirs.")
        return redirect("member_detail", pk=pk)


class MemberPhoneRemoveView(DataEntryRequiredMixin, View):
    """Drop a number a member no longer uses.

    The primary number is not removable here — that is an edit to the member,
    not the withdrawal of an extra line.
    """

    def post(self, request, pk, phone_id):
        member = get_object_or_404(Member, pk=pk)
        phone = get_object_or_404(MemberPhone, pk=phone_id, member=member)
        if phone.is_primary:
            messages.error(request, "That is the member's main number — change "
                                    "it by editing the member.")
            return redirect("member_detail", pk=pk)
        number = phone.number
        phone.delete()
        messages.success(request, f"{number} removed from {member.name}.")
        return redirect("member_detail", pk=pk)


class MemberUpdateView(DataEntryRequiredMixin, UpdateView):
    model = Member
    form_class = MemberForm
    template_name = "members/form.html"

    def get_success_url(self):
        messages.success(self.request, "Member updated.")
        return reverse_lazy("member_detail", args=[self.object.pk])


def _established_phone(member):
    """The number a record actually carries, read back the way it was written.

    ``Member.save`` stores ``normalize_phone(self.phone) or self.phone``, so a
    number it cannot make sense of is kept verbatim rather than discarded.
    Reading it back through the same normalisation — the matching service's own,
    not a second copy of it — is what stops '0712 345 678' and '254712345678'
    being mistaken for two different numbers, and therefore for two people.
    """
    return normalize_phone(member.phone) or (member.phone or "").strip()


def merge_candidates(member):
    """The same-name records `member` might be merged with, and the single one
    — if there is one — that may be merged with nobody watching.

    Returns ``(candidates, unattended)``. The review screen and the bulk-merge
    endpoint both read this, and they have to read the same thing: "unambiguous"
    must mean one thing on the page that offers the one-click merge and in the
    view that carries it out, or the button promises something the endpoint does
    not do.

    Two conditions make a pair ambiguous:

    * More than one same-name candidate — nothing here can say which of them is
      the person, so a human must.
    * Both records carrying a phone number, and the numbers differing. That is
      not a detail: it is the exact condition under which
      ``match_or_create_member`` REFUSED to attach the incoming payment to the
      existing member, created a second record and raised the very
      PossibleDuplicate flag being reviewed here (see
      ``members/services/matching.py``, step 2 — ``if not ph or m.phone == ph``).
      The system has already judged 'same name, different established number' to
      be evidence of two different people; the queue that reviews that judgement
      cannot quietly overturn it in bulk. Two real congregants called John Kamau
      with two real numbers would have their pledges, envelopes, loans and
      benevolent history permanently combined by one click on 'merge all', and
      nothing in the merge is reversible.

    So such a pair is withheld from the bulk action and left on the review
    screen, where the treasurer is shown both numbers and chooses deliberately.
    """
    candidates = list(Member.objects.filter(name_key=member.name_key)
                      .exclude(pk=member.pk))
    unattended = None
    if len(candidates) == 1:
        mine, theirs = _established_phone(member), _established_phone(candidates[0])
        if not (mine and theirs and mine != theirs):
            unattended = candidates[0]
    return candidates, unattended


class DuplicateReviewView(ReadAccessMixin, ListView):
    template_name = "members/duplicates.html"
    context_object_name = "dups"

    def get_queryset(self):
        return (PossibleDuplicate.objects.filter(resolved=False)
                .select_related("member"))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # for each flagged member, find same-name candidates to merge with
        data = []
        single = 0
        for d in ctx["dups"]:
            # `single` is what the template turns into a bare "Merge these two"
            # button and what feeds `single_count` on the merge-all button, so
            # it must mean "safe to merge without being asked", not merely
            # "there is one of them". A pair whose phone numbers disagree falls
            # through to the choose-a-record form instead, which spells out each
            # candidate's number beside its name — the treasurer sees the
            # conflict that kept the two records apart before deciding.
            candidates, unattended = merge_candidates(d.member)
            row = {"flag": d, "member": d.member, "candidates": candidates,
                   "single": unattended is not None}
            if row["single"]:
                single += 1
            data.append(row)
        ctx["rows"] = data
        ctx["single_count"] = single        # how many can be auto-merged in bulk
        return ctx


class BulkMergeView(DataEntryRequiredMixin, View):
    """Merge every duplicate that has exactly one candidate, in one click.
    Ambiguous ones are left for manual review — see ``merge_candidates`` for
    what makes a pair ambiguous, which is deliberately decided in that one place
    so this endpoint and the review screen cannot come to differ about it."""

    def post(self, request):
        merged = 0
        held = 0
        blocked = []
        for d in PossibleDuplicate.objects.filter(resolved=False).select_related("member"):
            cands, unattended = merge_candidates(d.member)
            if unattended is None:
                # A lone candidate that is still not safe to merge means the two
                # records' phones disagree. Count it, so the treasurer is told
                # why the queue did not empty rather than being left to wonder.
                if len(cands) == 1:
                    held += 1
                continue
            # keep the older / manually-entered record where possible
            keep, absorb = unattended, d.member
            if (keep.source == Member.Source.AUTO_BANK
                    and absorb.source != Member.Source.AUTO_BANK):
                keep, absorb = absorb, keep
            try:
                merge_members(keep, absorb)
            except MemberMergeConflict as exc:
                # one unmergeable pair must not abort the whole run, and
                # must not leave a half-merged record behind
                blocked.append(f"{absorb.name}: {exc.reasons[0]}")
                continue
            merged += 1
        if merged:
            messages.success(request, f"Merged {merged} unambiguous duplicate(s). "
                             "Any remaining ones need a person to look at them.")
        elif not blocked and not held:
            messages.info(request, "No unambiguous duplicates to merge.")
        if held:
            messages.warning(
                request,
                f"{held} left for review: the two records have different phone "
                "numbers on file, which is why they were not treated as the same "
                "person when the payment came in. Merge them by hand if they "
                "really are one person.")
        for reason in blocked[:5]:
            messages.warning(request, f"Not merged — {reason}")
        if len(blocked) > 5:
            messages.warning(request, f"…and {len(blocked) - 5} more that need "
                                      "the same attention.")
        return redirect("member_duplicates")


class MergeMembersView(DataEntryRequiredMixin, View):
    def post(self, request):
        keep = get_object_or_404(Member, pk=request.POST.get("keep"))
        absorb = get_object_or_404(Member, pk=request.POST.get("absorb"))
        if keep.pk == absorb.pk:
            messages.error(request, "Choose two different members.")
            return redirect("member_duplicates")
        try:
            merge_members(keep, absorb)
        except MemberMergeConflict as exc:
            for reason in exc.reasons:
                messages.error(request, reason)
            return redirect("member_duplicates")
        messages.success(request, f"Merged into {keep.name}.")
        return redirect("member_duplicates")


import csv
import io
from django.http import HttpResponse
from departments.models import DevelopmentGroup


class MemberBulkView(DataEntryRequiredMixin, View):
    """Bulk-edit selected members: set group, member type, or active flag."""

    def post(self, request):
        ids = request.POST.getlist("ids")
        field = request.POST.get("field")
        value = request.POST.get("value")
        if not ids:
            messages.warning(request, "Select at least one member.")
            return redirect("member_list")
        qs = Member.objects.filter(pk__in=ids)
        n = qs.count()
        if field == "group":
            qs.update(group=value or None)
            label = "group"
        elif field == "member_type":
            qs.update(member_type=value or None)
            label = "member type"
        elif field == "active":
            qs.update(active=(value == "1"))
            label = "status"
        elif field == "dev_group":
            grp = DevelopmentGroup.objects.filter(pk=value).first() if value else None
            qs.update(dev_group=grp)
            label = "development group"
        else:
            messages.error(request, "Unknown bulk action.")
            return redirect("member_list")
        messages.success(request, f"Updated {label} for {n} member(s).")
        return redirect("member_list")


class MemberExportView(ReadAccessMixin, View):
    """Download all members as CSV for offline bulk editing / re-import."""

    def get(self, request):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="members.csv"'
        w = csv.writer(resp)
        # The same columns the importer reads, so the file still round-trips —
        # giving figures are deliberately NOT added here, as a column the
        # importer does not know would break re-import.
        w.writerow(["id", "name", "phone", "group", "member_type", "dev_group_number", "active"])
        # …but the same rows the screen is showing. Exporting the whole register
        # regardless of the filter was tolerable when the only filters were
        # group and source; with a status filter it would quietly hand back
        # members the treasurer had deliberately filtered out.
        for m in filter_members(request.GET):
            w.writerow([m.id, m.name, m.phone or "", m.group or "",
                        m.member_type or "",
                        m.dev_group.number if m.dev_group else "",
                        "1" if m.active else "0"])
        return resp


class MemberImportView(DataEntryRequiredMixin, View):
    """Upload an edited members CSV. Rows with an id update that member; rows
    without an id create a new member. Matches the export columns."""
    template_name = "members/import.html"

    def get(self, request):
        return render(request, self.template_name, {})

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a CSV file.")
            return redirect("member_import")
        try:
            text = io.TextIOWrapper(f.file, encoding="utf-8-sig")
            reader = csv.DictReader(text)
        except Exception:
            from core.utils import log_exception as _lx; _lx('members/views.py')
            messages.error(request, "Could not read the CSV.")
            return redirect("member_import")
        groups = {c[0] for c in Member.Group.choices}
        types = {c[0] for c in Member.MemberType.choices}
        updated = created = 0
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            data = {
                "name": name,
                "phone": (row.get("phone") or "").strip() or None,
                "group": (row.get("group") or "").strip() or None,
                "member_type": (row.get("member_type") or "").strip() or None,
                "active": (row.get("active") or "1").strip() != "0",
            }
            if data["group"] and data["group"] not in groups:
                data["group"] = None
            if data["member_type"] and data["member_type"] not in types:
                data["member_type"] = None
            devnum = (row.get("dev_group_number") or "").strip()
            dev = DevelopmentGroup.objects.filter(number=devnum).first() if devnum.isdigit() else None
            data["dev_group"] = dev
            mid = (row.get("id") or "").strip()
            obj = Member.objects.filter(pk=mid).first() if mid.isdigit() else None
            if obj is None and data["phone"]:
                obj = Member.objects.filter(phone=data["phone"]).first()
            if obj is None:
                obj = Member.objects.filter(name__iexact=name).first()
            if obj:
                for k, v in data.items():
                    setattr(obj, k, v)
                obj.save()
                updated += 1
            else:
                Member.objects.create(**data)
                created += 1
        messages.success(request, f"Import complete — {updated} updated, {created} created.")
        return redirect("member_list")


class MemberSmsView(TreasurerRequiredMixin, View):
    """Send a criteria-based SMS to members — e.g. everyone who hasn't yet
    contributed to a given campaign (Camp Meeting), members with an
    outstanding pledge, members who haven't given recently, or a demographic
    group. Preview the recipient list before sending; the message supports
    {name}, {church} and (for the campaign criterion) {campaign} placeholders."""
    template_name = "members/member_sms.html"
    DEFAULT_TEMPLATES = {
        "not_contributed_campaign": (
            "Dear {name}, greetings from {church}. We haven't yet received your "
            "contribution towards {campaign}. Kindly consider giving as led."),
        "outstanding_pledge": (
            "Dear {name}, greetings from {church}. This is a friendly reminder "
            "of your pledge — a balance of {amount} remains outstanding."),
        "no_recent_giving": (
            "Dear {name}, we've missed you at {church}. We hope all is well "
            "and look forward to seeing you soon."),
        "by_group": "Dear {name}, greetings from {church}. ",
        "all_with_phone": "Dear {name}, greetings from {church}. ",
    }

    def _criteria_choices(self):
        from giving.models import Campaign
        return {
            "not_contributed_campaign": "Have not contributed to a campaign",
            "outstanding_pledge": "Have an outstanding pledge",
            "no_recent_giving": "Have not given in the last N days",
            "by_group": "Belong to a demographic group",
            "all_with_phone": "All active members (broadcast)",
        }, Campaign.objects.filter(active=True).order_by("name")

    def _recipients(self, request_data):
        """Return (queryset_of_members, extra_ctx) for the chosen criterion,
        further narrowed by the optional 'minimum contributions' filter (to
        exclude one-time givers who may not actually be church members —
        applies on top of whichever criterion is selected, including the
        plain broadcast)."""
        qs, extra = self._recipients_for_criterion(request_data)
        g = request_data
        try:
            min_gifts = int(g.get("min_gifts") or 0)
        except (TypeError, ValueError):
            min_gifts = 0
        extra["min_gifts"] = min_gifts
        if min_gifts > 0:
            from django.db.models import Count
            from giving.models import Transaction
            qualifying = (Transaction.objects.confirmed_credits()
                         .filter(member__isnull=False)
                         .values("member_id")
                         .annotate(n=Count("id"))
                         .filter(n__gte=min_gifts)
                         .values_list("member_id", flat=True))
            qs = qs.filter(id__in=qualifying)
        return qs, extra

    def _recipients_for_criterion(self, request_data):
        """Return (queryset_of_members, extra_ctx) for the chosen criterion.
        `request_data` is request.GET or request.POST — same param names either way."""
        from members.models import Member
        from django.db.models import Q
        g = request_data
        crit = g.get("criteria") or ""
        base = Member.objects.filter(active=True).exclude(
            Q(phone__isnull=True) | Q(phone=""))
        extra = {}

        if crit == "not_contributed_campaign":
            from giving.models import Campaign, Transaction
            camp = Campaign.objects.filter(pk=g.get("campaign") or None).first()
            extra["campaign"] = camp
            if not camp:
                return base.none(), extra
            fund_ids = self._fund_tree_ids(camp.department)
            givers = (Transaction.objects.confirmed_credits()
                     .filter(department_id__in=fund_ids, member__isnull=False)
                     .values_list("member_id", flat=True))
            return base.exclude(id__in=givers), extra

        if crit == "outstanding_pledge":
            from pledges.models import Pledge
            ids = [p.member_id for p in
                   Pledge.objects.filter(status=Pledge.Status.ACTIVE,
                                         member__isnull=False)
                   if p.outstanding > 0]
            return base.filter(id__in=ids), extra

        if crit == "no_recent_giving":
            import datetime as dt
            from giving.models import Transaction
            try:
                days = int(g.get("days") or 90)
            except (TypeError, ValueError):
                days = 90
            since = dt.date.today() - dt.timedelta(days=days)
            extra["days"] = days
            recent_givers = (Transaction.objects.confirmed_credits()
                             .filter(date__gte=since, member__isnull=False)
                             .values_list("member_id", flat=True))
            return base.exclude(id__in=recent_givers), extra

        if crit == "by_group":
            grp = g.get("group") or ""
            extra["group"] = grp
            if not grp:
                return base.none(), extra
            return base.filter(group=grp), extra

        if crit == "all_with_phone":
            return base, extra

        return base.none(), extra

    @staticmethod
    def _fund_tree_ids(dept):
        ids = [dept.id]
        for sub in dept.subgroups.all():
            ids.extend(MemberSmsView._fund_tree_ids(sub))
        return ids

    def get(self, request):
        from core.models import SiteConfig
        from members.models import Member
        criteria_choices, campaigns = self._criteria_choices()
        recips, extra = self._recipients(request.GET)
        cfg = SiteConfig.get()
        crit = request.GET.get("criteria") or ""
        return render(request, self.template_name, {
            "criteria_choices": criteria_choices,
            "campaigns": campaigns,
            "group_choices": Member.Group.choices,
            "criteria": crit,
            "selected_campaign": extra.get("campaign"),
            "selected_group": extra.get("group", ""),
            "days": extra.get("days", 90),
            "min_gifts": extra.get("min_gifts", 0),
            "recipients": list(recips[:500]),
            "recipient_count": recips.count(),
            "template": self.DEFAULT_TEMPLATES.get(crit, "Dear {name}, greetings from {church}. "),
            "church": cfg.church_name or "",
            "sms_enabled": cfg.sms_enabled,
        })

    def post(self, request):
        from core.models import SiteConfig
        from core.services.sms import send_sms, _format
        recips, extra = self._recipients(request.POST)
        template = request.POST.get("template") or "Dear {name}, greetings from {church}. "
        church = SiteConfig.get().church_name or ""
        campaign_name = extra.get("campaign").name if extra.get("campaign") else ""
        sent = failed = 0
        for m in recips:
            amount = ""
            if request.POST.get("criteria") == "outstanding_pledge":
                from pledges.models import Pledge
                p = (Pledge.objects.filter(member=m, status=Pledge.Status.ACTIVE)
                     .first())
                amount = f"{p.outstanding:,.0f}" if p else ""
            msg = _format(template,
                          name=(m.name.split()[0] if m.name else "member"),
                          church=church, campaign=campaign_name, amount=amount)
            log = send_sms(m.phone, msg)
            if getattr(log, "status", "") == "SENT":
                sent += 1
            else:
                failed += 1
        if sent:
            messages.success(request, f"SMS sent to {sent} member(s)"
                             + (f"; {failed} failed." if failed else "."))
        else:
            messages.error(request, "No messages were sent. "
                           + ("Check SMS settings." if failed else
                              "No members match this criterion."))
        qs = request.POST.urlencode()
        return redirect(f"{request.path}?{qs}" if qs else request.path)
