from decimal import Decimal
from django.db import models
from django.utils.text import slugify
from simple_history.models import HistoricalRecords


class ParentAwareDepartmentManager(models.Manager):
    """Always load a department's parent alongside it.

    `Department.__str__` prints "Parent / Child" for a sub-account, so rendering
    a department triggers a fetch of its parent. That is invisible until you
    render a list of them: a fund dropdown calls str() once per option, so a
    register with sub-accounts cost one query per option, on every page carrying
    a fund selector. Selecting the parent here makes the whole app's forms
    correct at once instead of relying on each queryset to remember.

    The cost is a LEFT JOIN on a table with a few dozen rows. `.values()`,
    `.update()` and aggregates ignore select_related, so nothing else changes.
    """

    def get_queryset(self):
        return super().get_queryset().select_related("parent")


class Department(models.Model):
    """A fund the church allocates money to.

    There are only two *fund types*: Trust funds (remitted to the field) and
    Local funds (kept by the church for its departments). `category` is kept as
    a lightweight grouping label for reports.
    """

    class FundType(models.TextChoices):
        TRUST = "TRUST", "Trust fund (remitted to field)"
        LOCAL = "LOCAL", "Local fund (church department)"

    class Category(models.TextChoices):
        OFFERING = "OFFERING", "Offering"
        MINISTRY = "MINISTRY", "Ministry"
        DEVELOPMENT = "DEVELOPMENT", "Development"
        HOLDING = "HOLDING", "Holding"
        TRUST = "TRUST", "Trust"

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="subgroups",
        help_text="Optional. Set this to make the fund a sub-account "
                  "(e.g. Youth → Potluck, Choir, Mission).")
    fund_type = models.CharField(
        max_length=8, choices=FundType.choices, default=FundType.LOCAL,
        help_text="Trust funds are remitted to the field; local funds stay with the church.")
    is_trust = models.BooleanField(default=False, editable=False)  # derived from fund_type
    category = models.CharField(
        max_length=12, choices=Category.choices, default=Category.OFFERING)
    opening_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Brought-forward balance.")
    # DEPRECATED: superseded by the year-scoped Budget/BudgetLine model. Kept
    # read-only for backwards compatibility; values are migrated into Budget records.
    annual_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        editable=False)
    contribution_goal = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Collection goal that groups contribute towards (e.g. a camp expenses goal).")
    year_goal = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Annual goal for this fund (e.g. the Camp Meeting Expense goal for the year).")
    goal_type = models.CharField(max_length=20, default="NONE", db_index=True,
        choices=[("NONE", "General annual goal"),
                 ("CAMP_EXPENSE", "Camp Meeting Expense goal")],
        help_text="Classifies what this fund's annual goal represents, so reports "
                  "label and pair it correctly regardless of the fund's name.")
    income_account = models.CharField(max_length=20, blank=True,
        choices=[("", "Guess from the fund's name (default)"),
                 ("INC_TITHE", "Tithe"),
                 ("INC_OFFERINGS", "Offerings & general income"),
                 ("INC_DEVELOPMENT", "Development & projects"),
                 ("INC_INTEREST", "Interest & investment income"),
                 ("INC_FUNDRAISING", "Fundraising & camp meeting"),
                 ("INC_DONATIONS", "Donations & gifts in kind"),
                 ("INC_OTHER", "Other income")],
        help_text="Which income account a local (unrestricted) fund's receipts post "
                  "to in the general ledger. Leave blank to classify automatically "
                  "from the fund's name (matches most funds); set this explicitly "
                  "for a fund whose name doesn't clearly say what it is, or after "
                  "renaming a fund, so its income keeps reporting under the same "
                  "account as before.")
    offering_fund = models.ForeignKey("self", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="expense_counterpart",
        help_text="For an expense fund (e.g. Camp Meeting Expense, a Local fund): "
                  "the separate Trust offering fund it is paired with (e.g. Camp "
                  "Meeting Offering). Lets both goals be tracked on one budget page "
                  "without ever merging their totals.")
    offering_goal = models.DecimalField(max_digits=12, decimal_places=2, null=True,
        blank=True,
        help_text="Annual goal for the paired Trust offering fund — tracked "
                  "independently of this fund's expense goal.")
    children_in_expenses = models.BooleanField(
        default=False,
        help_text="For a parent fund: if on, its sub-accounts can be charged "
                  "expenses individually; if off, expenses use the parent fund "
                  "only (sub-accounts are for collections).")
    show_in_expenses = models.BooleanField(
        default=True,
        help_text="Show this fund in the expense department picker. Turn off for "
                  "collection-only funds that are never spent directly.")
    # A "selectable" fund is one the treasurer picks when allocating giving. Split
    # children (e.g. Combined Offering's Trust-50% / Local-50% halves) are NOT
    # selectable on their own — the treasurer chooses the single parent concept
    # ("Combined Offering") and the system divides it. Off only for split halves.
    selectable = models.BooleanField(
        default=True,
        help_text="Show this fund in allocation pickers (review queue, cash entry, "
                  "envelopes). Turn off for the internal halves of a split fund.")

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        CLOSED = "CLOSED", "Closed"
        ARCHIVED = "ARCHIVED", "Archived"

    status = models.CharField(max_length=8, choices=Status.choices,
        default=Status.ACTIVE, db_index=True,
        help_text="Closed/archived accounts stay in historical reports but accept "
                  "no new transactions. An account can only be closed at a zero balance.")
    collection_only = models.BooleanField(
        default=False,
        help_text="A collection account that only receives contributions (e.g. a "
                  "camp fundraising group). It can receive income but is never "
                  "selectable for expenses or payments.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["fund_type", "name"]

    # `__str__` needs the parent's name, so every place that renders a
    # Department — above all the fund dropdowns, which call str() once per
    # option — was paying a query per sub-account. Selecting the parent by
    # default turns that into one small self-join on a table of a few dozen
    # rows, and it fixes every such form at once rather than requiring each
    # queryset to remember. Callers that add their own select_related("parent")
    # are unaffected.
    objects = ParentAwareDepartmentManager()

    def __str__(self):
        if self.parent_id:
            return f"{self.parent.name} / {self.name}"
        return self.name

    @property
    def is_subgroup(self):
        return self.parent_id is not None

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:50]
        # a sub-account always shares its parent's fund type
        if self.parent_id and self.parent.fund_type:
            self.fund_type = self.parent.fund_type
        self.is_trust = self.fund_type == self.FundType.TRUST
        # status is authoritative for whether the fund accepts new transactions
        self.active = (self.status == self.Status.ACTIVE)
        # a collection account is never spent directly
        if self.collection_only:
            self.show_in_expenses = False
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.status == self.Status.ACTIVE


class DepartmentStatusLog(models.Model):
    """An audit trail of account lifecycle changes (active/closed/archived) and
    consolidations, so every status change is recorded with who and when.

    PROTECT, not CASCADE: this is an audit trail by its own stated purpose — if
    a department were ever deleted after its status log accumulated history
    (only possible once it has no protected financial activity left, since
    Transaction/Expense already use PROTECT), silently cascading this away
    would destroy exactly the record an auditor would want kept."""
    department = models.ForeignKey(Department, on_delete=models.PROTECT,
                                   related_name="status_logs")
    from_status = models.CharField(max_length=8, blank=True)
    to_status = models.CharField(max_length=8)
    note = models.CharField(max_length=200, blank=True)
    changed_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"{self.department.name}: {self.from_status or '—'} → {self.to_status}"


class DevelopmentGroup(models.Model):
    """A church development group (Group 1..N). Giving is tracked against it and
    flows into the Development local fund."""

    number = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=80, blank=True)
    target = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    leader_name = models.CharField(max_length=120, blank=True,
        help_text="Group leader, for reconciliation reports.")
    leader_email = models.EmailField(blank=True,
        help_text="If set, the group's contribution report can be emailed here.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return self.name or f"Development Group {self.number}"

    @property
    def label(self):
        base = f"Group {self.number}"
        return f"{base} – {self.name}" if self.name else base


def _trailing_number(name):
    """The trailing integer in a name, e.g. 'Small Group 7' -> 7. None if the
    name has no trailing digits. Shared with envelopes.services.posting,
    which has its own identical copy to avoid a cross-app import for one
    small helper — keep the two in step if this ever changes."""
    import re
    m = re.search(r"(\d+)\s*$", (name or "").strip())
    return int(m.group(1)) if m else None


def numbered_subgroup_parent_map():
    """{child_department_id: parent_department} for every *numbered*
    sub-account (Department.parent set AND the child's own name ends in a
    number, e.g. "Small Group 7", "Development Group 12") — deliberately
    NOT every Department.parent child. Trust Fund's and LCB's established,
    individually-named children (Tithe, Camp Meeting, Sabbath School, ...)
    are NOT numbered and are excluded here on purpose: those have always
    been reported individually and church treasurers rely on seeing each by
    name — this rollup only targets the "many numbered subgroups" case (a
    fund set up with a numbered family of sub-accounts, e.g. Small Group
    1..30), where listing every one separately in a summary/export makes it
    unreadable. Used by the envelope Sabbath statement, monthly summary and
    Sabbath Excel export to consolidate numbered subgroups back under their
    parent fund for DISPLAY — actual ledger postings still go to the exact
    subgroup account (see envelopes.services.posting.subgroups_for), so
    accounting stays precise; only these summary views roll up.
    """
    out = {}
    for d in Department.objects.filter(parent__isnull=False, active=True
                                       ).select_related("parent"):
        if _trailing_number(d.name) is not None:
            out[d.id] = d.parent
    return out


def split_component_dept_ids():
    """IDs of departments that are halves of a split offering (collection-only)."""
    from giving.models import SplitComponent
    return set(SplitComponent.objects.values_list("department_id", flat=True))


def total_opening_cash_position():
    """The church's total starting cash position (all funds, trust and local,
    combined) — the sum of every fund's own opening_balance.

    Deliberately does NOT read SiteConfig.opening_bank_balance /
    opening_cash_on_hand / opening_unremitted_trust — those three fields are
    populated only by the legacy-spreadsheet import tool as a labelled
    reference snapshot of what a summary sheet once said, and are left at
    zero for every normal deployment. Used as a component figure elsewhere;
    for "the true cash position as of a date" use current_cash_position()
    below, which is exact by construction rather than approximated from this
    plus income and expense totals (a transfer between funds can otherwise
    throw a hand-built approximation off by a small amount)."""
    return Department.objects.aggregate(
        t=models.Sum("opening_balance"))["t"] or Decimal(0)


def current_cash_position(as_of=None):
    """The true, exact total cash & bank position across every fund as of a
    date — the same figure the Statement of Financial Position shows as
    "cash", computed the same way (the sum of every fund's own closing
    balance from reports.services.balances.department_summary), so this can
    never drift from the SOFP the way a hand-rebuilt "opening + income -
    expenses" approximation could (a fund transfer, for instance, is exactly
    accounted for here because department_summary already includes it)."""
    import datetime as _dt
    from decimal import Decimal as _Decimal
    from reports.services import balances
    as_of = as_of or _dt.date.today()
    rows = balances.department_summary(None, as_of)
    return sum((r["closing"] for r in rows), _Decimal(0))


def expense_departments():
    """Departments that may be charged operating expenses. Excludes split-offering
    halves, sub-accounts whose parent keeps expenses at the parent level, funds
    hidden from expenses, and TRUST funds — trust money is restricted: held as a
    liability and only remitted to the field, never spent on operations.
    (Remittance postings are created by the remittance flow, not this picker.)"""
    comp = split_component_dept_ids()
    out = []
    for d in Department.objects.filter(active=True, show_in_expenses=True).select_related("parent"):
        if d.id in comp or d.is_trust or d.collection_only:
            continue
        if d.parent_id and not (d.parent and d.parent.children_in_expenses):
            continue
        out.append(d)
    return out


def income_departments():
    """Departments that may receive giving directly: excludes split-offering
    halves (those come via the split fund as one component)."""
    comp = split_component_dept_ids()
    return [d for d in Department.objects.filter(active=True) if d.id not in comp]


class Budget(models.Model):
    """A fund's planned budget for a financial year (year-scoped, unlike the legacy
    Department.annual_budget field which it supersedes)."""
    year = models.PositiveIntegerField(db_index=True)
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="budgets")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.CharField(max_length=200, blank=True)
    history = HistoricalRecords()

    class Meta:
        unique_together = ("year", "department")
        ordering = ["-year", "department__name"]

    def __str__(self):
        return f"{self.department.name} {self.year}: {self.amount}"

    @property
    def lines_total(self):
        from django.db.models import Sum
        return self.lines.aggregate(t=Sum("amount"))["t"] or 0


class BudgetLine(models.Model):
    """An optional breakdown line within a fund's annual budget. If a category is
    set, the line's actual spend can be measured against it."""
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="lines")
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=14, blank=True,
                                help_text="Optional expense category, for line-level actuals.")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quarter = models.CharField(
        max_length=2, blank=True,
        choices=[("Q1", "Q1 (Jan–Mar)"), ("Q2", "Q2 (Apr–Jun)"),
                 ("Q3", "Q3 (Jul–Sep)"), ("Q4", "Q4 (Oct–Dec)")],
        help_text="Optional: the quarter the fund expects to spend this line, "
                  "for planning insight. Leave blank for spread across the year.")
    source_fund = models.ForeignKey("Department", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="funded_budget_lines",
        help_text="Where the money comes from. Leave blank to fund it from the "
                  "department's own funds; or choose the Local Church Budget or "
                  "another fund.")
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}: {self.amount}"

    @property
    def source_label(self):
        if self.source_fund_id is None:
            return self.budget.department.name + " (own funds)"
        return self.source_fund.name

    @property
    def quarter_label(self):
        return dict(self._meta.get_field("quarter").choices).get(self.quarter, "Whole year")

    @property
    def source_kind(self):
        """'OWN', 'LCB' or 'OTHER' — used to summarise funding on the board report."""
        if self.source_fund_id is None:
            return "OWN"
        lcb = lcb_fund()
        if lcb and self.source_fund_id == lcb.id:
            return "LCB"
        return "OTHER"


def lcb_fund():
    """The Local Church Budget fund (the account LCB money flows through), or None.

    Honours the LCB departments chosen in Settings first (the first configured one
    is treated as the main fund); otherwise falls back to matching by name."""
    try:
        from core.models import SiteConfig
        chosen = SiteConfig.get().lcb_departments.order_by("name").first()
        if chosen:
            return chosen
    except Exception:  # noqa: BLE001 — config/table may not be ready (e.g. migrations)
        pass
    qs = Department.objects.filter(fund_type=Department.FundType.LOCAL)
    return (qs.filter(name__istartswith="LCB ").first()
            or qs.filter(name__icontains="Local Church Budget").exclude(
                subgroups__isnull=False).first()
            or qs.filter(name__icontains="LCB").first())


def lcb_fund_ids():
    """Every fund in the LCB FAMILY: the funds a church has configured in
    Settings under "Local Church Budget (LCB) funds", **plus their subgroups**.

    The configured list is authoritative. Only where a church has configured
    nothing does this fall back to matching on the name — which is a guess, and
    was previously the ONLY thing some callers did, so a church that had
    carefully listed its LCB funds in Settings found that setting quietly
    ignored and its funds matched (or missed) by whether somebody had spelt
    "LCB" into the name.
    """
    try:
        from core.models import SiteConfig
        chosen = set(SiteConfig.get().lcb_departments.values_list("id", flat=True))
    except Exception:  # noqa: BLE001 — config/table may not be ready
        chosen = set()

    if chosen:
        # subgroups belong to their parent's family — LCB money that lands in an
        # LCB subgroup is still LCB money, and a treasurer who listed the parent
        # should not have to list every child
        subs = set(Department.objects
                   .filter(parent_id__in=chosen)
                   .values_list("id", flat=True))
        return chosen | subs

    # legacy fallback: no LCB funds configured, so guess from the name
    from django.db.models import Q
    named = Department.objects.filter(
        Q(name__icontains="LCB") | Q(name__icontains="Local Church Budget")
        | Q(parent__name__icontains="LCB")
        | Q(parent__name__icontains="Local Church Budget"))
    return set(named.values_list("id", flat=True))


def receiptable_fund_ids():
    """The funds a church normally turns into a formal receipt: every TRUST
    fund, plus the whole LCB family.

    This is the single definition of "Trust + LCB" — the same phrase the
    Sabbath-confirm scope setting uses, and now the same code behind it. It
    previously existed only as a private, name-matching helper inside the
    statement importer, so two different screens could (and did) disagree about
    which funds counted.
    """
    trust = set(Department.objects
                .filter(fund_type=Department.FundType.TRUST)
                .values_list("id", flat=True))
    return trust | lcb_fund_ids()


class DepartmentLeadership(models.Model):
    """Links a user (with the Leader role) to a department they lead. A leader
    may lead more than one department; each gets a row. Leaders get a read-only,
    scoped dashboard for exactly the departments listed here (plus those funds'
    own sub-accounts and development groups)."""
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE,
                             related_name="led_departments")
    department = models.ForeignKey(Department, on_delete=models.CASCADE,
                                   related_name="leaders")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "department"],
                                    name="uniq_user_department_lead"),
        ]
        verbose_name_plural = "Department leaderships"

    def __str__(self):
        return f"{self.user} leads {self.department}"


def departments_led_by(user):
    """The set of Department objects a leader may see: the departments they are
    assigned to, plus ALL their sub-accounts at any depth (children, grandchildren
    …). So assigning the parent fund (e.g. CAMP MEETING) automatically gives the
    leader its whole tree (CAMP_1 … CAMP_30). Office staff are not constrained
    here — this only scopes the Leader role. Returns a queryset.
    """
    led_ids = list(DepartmentLeadership.objects.filter(user=user)
                   .values_list("department_id", flat=True))
    if not led_ids:
        return Department.objects.none()
    # walk down the tree, level by level, until no new sub-accounts appear
    all_ids = set(led_ids)
    frontier = set(led_ids)
    while frontier:
        children = set(Department.objects.filter(parent_id__in=frontier)
                       .exclude(id__in=all_ids).values_list("id", flat=True))
        if not children:
            break
        all_ids |= children
        frontier = children
    return Department.objects.filter(id__in=all_ids).distinct()
