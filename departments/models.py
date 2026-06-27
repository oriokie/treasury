from django.db import models
from django.utils.text import slugify
from simple_history.models import HistoricalRecords


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
        help_text="Annual goal for this fund (e.g. the Camp Meeting goal for the year).")
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
    consolidations, so every status change is recorded with who and when."""
    department = models.ForeignKey(Department, on_delete=models.CASCADE,
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


def split_component_dept_ids():
    """IDs of departments that are halves of a split offering (collection-only)."""
    from giving.models import SplitComponent
    return set(SplitComponent.objects.values_list("department_id", flat=True))


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
