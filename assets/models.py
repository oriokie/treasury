"""Fixed-asset register with configurable depreciation.

Depreciation rules live in DepreciationRule (per category) and can be managed
from settings; an individual asset may override the method/rate. Net book value
feeds the Statement of Financial Position.
"""
import datetime as dt
from decimal import Decimal
from django.core.validators import MinValueValidator

from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords


class DepreciationRule(models.Model):
    """A depreciation policy for a category of assets."""
    class Method(models.TextChoices):
        STRAIGHT = "STRAIGHT", "Straight-line"
        REDUCING = "REDUCING", "Reducing balance"
        NONE = "NONE", "Not depreciated"

    category = models.CharField(max_length=20, unique=True)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.STRAIGHT)
    rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"),
                               help_text="Annual depreciation rate, percent.")
    history = HistoricalRecords()

    class Meta:
        ordering = ["category"]

    def __str__(self):
        return f"{self.get_category_display()} — {self.get_method_display()} {self.rate}%"

    def get_category_display(self):
        return dict(FixedAsset.Category.choices).get(self.category, self.category)


class AssetClass(models.Model):
    """A configurable asset class carrying the depreciation policy and (optionally)
    the ledger control-account keys. Replaces the hardcoded Category enum as the
    authoritative classification, so a treasurer can add 'Library' or 'Heritage'
    without a code change. Seeded 1:1 from FixedAsset.Category on migration.

    ``acct_cost_key``/``acct_accdep_key`` are blank by default, meaning the asset
    posts to the single FIXED_ASSETS / ACCUM_DEPRECIATION control accounts; a
    class may override them for per-class ledger granularity in a later phase.
    """
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=80)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="children")
    depreciable = models.BooleanField(default=True)     # False for land, heritage
    is_cwip = models.BooleanField(default=False)        # construction-in-progress bucket
    default_method = models.CharField(max_length=10, choices=DepreciationRule.Method.choices,
                                      default=DepreciationRule.Method.STRAIGHT)
    default_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    default_useful_life_years = models.PositiveSmallIntegerField(null=True, blank=True)
    componentised = models.BooleanField(default=False)  # significant parts depreciate separately
    acct_cost_key = models.CharField(max_length=40, blank=True)
    acct_accdep_key = models.CharField(max_length=40, blank=True)
    active = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "asset classes"

    def __str__(self):
        return self.name


class Location(models.Model):
    """Hierarchical physical whereabouts (Campus → Building → Room), distinct
    from the fund that owns the asset."""
    name = models.CharField(max_length=120)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="children")
    church = models.ForeignKey("core.Organization", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="locations")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def full_path(self):
        parts, node, seen = [], self, set()
        while node and node.pk not in seen:
            seen.add(node.pk); parts.append(node.name); node = node.parent
        return " › ".join(reversed(parts))

    def __str__(self):
        return self.full_path()


class FixedAsset(models.Model):
    class Category(models.TextChoices):
        LAND = "LAND", "Land"
        CONSTRUCTION = "CONSTRUCTION", "Construction in progress"
        BUILDING = "BUILDING", "Buildings"
        FURNITURE = "FURNITURE", "Furniture & fittings"
        EQUIPMENT = "EQUIPMENT", "Equipment"
        VEHICLE = "VEHICLE", "Motor vehicles"
        IT = "IT", "Computers & IT"
        MUSICAL = "MUSICAL", "Musical instruments"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        PLANNED     = "PLANNED",     "Planned"
        ON_ORDER    = "ON_ORDER",    "On order"
        IN_CWIP     = "IN_CWIP",     "Under construction"
        IN_SERVICE  = "IN_SERVICE",  "In service"
        IDLE        = "IDLE",        "Idle / in store"
        MAINTENANCE = "MAINTENANCE", "Under maintenance"
        IMPAIRED    = "IMPAIRED",    "Impaired"
        HELD_SALE   = "HELD_SALE",   "Held for disposal"
        DISPOSED    = "DISPOSED",    "Disposed"
        ARCHIVED    = "ARCHIVED",    "Archived"

    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    # EAM classification (authoritative going forward; category retained for
    # backward compatibility and seeded 1:1 into asset_class on migration)
    asset_class = models.ForeignKey(AssetClass, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="assets")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_SERVICE,
                              db_index=True)
    tag = models.CharField(max_length=40, unique=True, null=True, blank=True, db_index=True,
                           help_text="Printed asset tag / QR payload.")
    serial_no = models.CharField(max_length=80, blank=True)
    in_service_on = models.DateField(null=True, blank=True,
        help_text="Commissioning date — when depreciation starts (may differ from acquisition).")
    location_fk = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="assets")
    custodian = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="assets_held")
    church = models.ForeignKey("core.Organization", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="assets")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="components")
    useful_life_years = models.PositiveSmallIntegerField(null=True, blank=True)
    revalued_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    is_heritage = models.BooleanField(default=False)
    is_donated = models.BooleanField(default=False)
    acquired_on = models.DateField()
    cost = models.DecimalField(max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))])
    salvage_value = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        validators=[MinValueValidator(Decimal("0"))])
    # optional per-asset overrides; blank => use the category rule / settings default
    method = models.CharField(max_length=10, choices=DepreciationRule.Method.choices, blank=True)
    rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    department = models.ForeignKey("departments.Department", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="assets")
    location = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=40, blank=True, help_text="Tag / serial no.")
    disposed = models.BooleanField(default=False)
    disposed_on = models.DateField(null=True, blank=True)
    disposal_proceeds = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    class DisposalMethod(models.TextChoices):
        SOLD = "SOLD", "Sold"
        SCRAPPED = "SCRAPPED", "Scrapped / written off"
        DONATED = "DONATED", "Donated / given away"
        LOST = "LOST", "Lost / stolen"
    disposal_method = models.CharField(max_length=10, choices=DisposalMethod.choices, blank=True)
    disposal_gain_loss = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Proceeds less net book value at disposal (gain positive, loss negative).")
    disposal_fund = models.ForeignKey("departments.Department", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="asset_disposals",
        help_text="Fund that received the disposal proceeds.")
    notes = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.name

    # ---- depreciation policy resolution ----
    def _policy(self, rules=None, cfg=None):
        from core.models import SiteConfig
        if self.method and self.rate is not None:
            return self.method, self.rate
        if rules is not None:            # cached path (bulk totals)
            rule = rules.get(self.category)
        else:
            rule = DepreciationRule.objects.filter(category=self.category).first()
        if rule:
            return (self.method or rule.method), (self.rate if self.rate is not None else rule.rate)
        cfg = cfg or SiteConfig.get()
        return (self.method or cfg.asset_depr_method), (self.rate if self.rate is not None else cfg.asset_depr_rate)

    def annual_depreciation(self):
        # retained for compatibility; straight-line annual = monthly * 12
        from assets.services import depreciation as _dep
        m = _dep.monthly_charge(self)
        return None if m is None else (m * 12)

    def accumulated_depreciation(self, as_of=None, rules=None, cfg=None):
        from assets.services import depreciation as _dep
        return _dep.accumulated_depreciation(self, as_of, rules, cfg)

    def net_book_value(self, as_of=None, rules=None, cfg=None):
        from assets.services import depreciation as _dep
        return _dep.net_book_value(self, as_of, rules, cfg)


def assets_live_at(as_of=None):
    """The assets on the register as at a date: acquired on or before it, and
    not yet disposed of.

    This is the ONE definition of which assets count at a date. Cost,
    accumulated depreciation, net book value and the register↔ledger
    reconciliation all read it, so they cannot drift apart — they did once, when
    net book value was computed over every asset while cost and accumulated
    depreciation were temporal, which quietly overstated net book value at any
    past date by the cost of anything acquired later.
    """
    import datetime as _dt
    from django.db.models import Q
    as_of = as_of or _dt.date.today()
    return FixedAsset.objects.filter(
        Q(acquired_on__isnull=True) | Q(acquired_on__lte=as_of)
    ).filter(
        Q(disposed=False) | Q(disposed_on__isnull=True) | Q(disposed_on__gt=as_of))


def nbv_total(as_of=None):
    """Total net book value of the assets held as at a date, computed with the
    depreciation rules and site config loaded ONCE (no per-asset queries)."""
    import datetime as _dt
    from core.models import SiteConfig
    rules = {r.category: r for r in DepreciationRule.objects.all()}
    cfg = SiteConfig.get()
    as_of = as_of or _dt.date.today()
    return sum((a.net_book_value(as_of, rules=rules, cfg=cfg)
                for a in assets_live_at(as_of)), Decimal(0))


class AssetAttachment(models.Model):
    """A supporting document for an asset — receipt, warranty, photo, etc."""
    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="assets/%Y/%m/")
    label = models.CharField(max_length=120, blank=True)
    uploaded_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.label or self.file.name


class DepreciationRun(models.Model):
    """One monthly depreciation charge across the register. Generated for a
    (year, month), reviewed, then posted to the ledger (Dr depreciation expense
    / Cr accumulated depreciation). Locked once the accounting period closes."""
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        LOCKED = "LOCKED", "Locked (period closed)"

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    run_date = models.DateField(help_text="Charge date (month end).")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT,
                              db_index=True)
    total_charge = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    journal = models.ForeignKey("ledger.JournalEntry", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="depreciation_runs")
    created_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [models.UniqueConstraint(fields=["year", "month"],
                                               name="one_depreciation_run_per_month")]

    def __str__(self):
        return f"Depreciation {self.year}-{self.month:02d} ({self.get_status_display()})"


class DepreciationLine(models.Model):
    run = models.ForeignKey(DepreciationRun, on_delete=models.CASCADE, related_name="lines")
    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="depreciation_lines")
    department = models.ForeignKey("departments.Department", null=True, blank=True,
                                   on_delete=models.SET_NULL)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    accumulated_after = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["asset__name"]


class Acquisition(models.Model):
    """How an asset came to exist.

    Separates the acquisition *event* from the asset record, and links it to the
    money that paid for it so nothing is double-counted against the cash books.
    A purchase or self-construction is paid through an Expense, which already
    carries the cash side of the entry; the acquisition documents it and links
    the asset. A donation involves no cash at all, so it is the one source that
    posts its own journal (fair value in, donated-asset income recognised).
    """

    class Source(models.TextChoices):
        PURCHASE = "PURCHASE", "Purchased"
        DONATION = "DONATION", "Donated (in kind)"
        CONSTRUCTION = "CONSTRUCTION", "Self-constructed"
        TRANSFER_IN = "TRANSFER_IN", "Transferred in"
        OPENING = "OPENING", "Opening balance (pre-system)"

    asset = models.OneToOneField(FixedAsset, on_delete=models.CASCADE,
                                 related_name="acquisition")
    source = models.CharField(max_length=12, choices=Source.choices,
                              default=Source.PURCHASE, db_index=True)
    date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0,
                                 validators=[MinValueValidator(Decimal("0"))],
                                 help_text="Purchase cost, or fair value for a donation.")
    expense = models.ForeignKey("cashbook.Expense", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="acquisitions",
                                help_text="The payment that bought or built this asset.")
    fund = models.ForeignKey("departments.Department", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="asset_acquisitions",
                             help_text="Fund that paid for it, or that a donation is credited to.")
    donor_name = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    notes = models.CharField(max_length=250, blank=True)
    recorded_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="asset_acquisitions")
    approved_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="asset_acquisitions_approved")
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["source", "date"])]

    def __str__(self):
        return f"{self.get_source_display()}: {self.asset.name} ({self.date})"

    @property
    def posts_own_journal(self):
        """Only a donation moves value without cash, so only a donation needs a
        journal of its own. Purchases/construction are posted by their Expense;
        opening balances by the asset opening entry."""
        return self.source == self.Source.DONATION


class AssetEvent(models.Model):
    """The asset's story in one place.

    Every lifecycle action writes a line here, so the profile can render a
    timeline without walking six history tables. It is a denormalised log for
    reading — `simple_history` remains the audit record of truth.
    """

    class Kind(models.TextChoices):
        CREATED = "CREATED", "Added to the register"
        ACQUIRED = "ACQUIRED", "Acquired"
        STATUS = "STATUS", "Status changed"
        ASSIGNED = "ASSIGNED", "Issued to a custodian"
        RETURNED = "RETURNED", "Returned"
        TRANSFERRED = "TRANSFERRED", "Transferred"
        DISPOSED = "DISPOSED", "Disposed"
        DOCUMENT = "DOCUMENT", "Document attached"
        NOTE = "NOTE", "Note"

    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.NOTE)
    summary = models.CharField(max_length=200)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["asset", "-at"])]

    def __str__(self):
        return f"{self.asset.name}: {self.summary}"


class AssetAssignment(models.Model):
    """Custody: who holds the asset, where, and over what period.

    An assignment is open until the asset is checked back in, so the register
    can always answer "who has it?" and an asset cannot be sent for disposal
    while it is still in someone's hands.
    """
    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="assignments")
    custodian = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="asset_assignments")
    holder_name = models.CharField(max_length=120, blank=True,
                                   help_text="If the holder is not a system user.")
    location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="assignments")
    from_date = models.DateField(default=dt.date.today)
    to_date = models.DateField(null=True, blank=True)
    condition_out = models.CharField(max_length=120, blank=True)
    condition_in = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=250, blank=True)
    issued_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="asset_assignments_issued")
    received_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="asset_assignments_received")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-from_date", "-id"]
        indexes = [models.Index(fields=["asset", "-from_date"])]

    @property
    def is_open(self):
        return self.to_date is None

    @property
    def holder(self):
        if self.custodian:
            return self.custodian.get_full_name() or self.custodian.username
        return self.holder_name or "—"

    def __str__(self):
        return f"{self.asset.name} → {self.holder}"


class AssetTransfer(models.Model):
    """Move an asset between locations, funds, or churches.

    A change of location is administrative. A change of FUND is accounting: the
    asset's carrying value moves from one fund's net assets to another, so an
    approved fund transfer posts an inter-fund equity move (see
    ledger.services.posting.post_asset_transfer).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    asset = models.ForeignKey(FixedAsset, on_delete=models.CASCADE, related_name="transfers")
    date = models.DateField(default=dt.date.today, db_index=True)
    from_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="transfers_out")
    to_location = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="transfers_in")
    from_fund = models.ForeignKey("departments.Department", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="asset_transfers_out")
    to_fund = models.ForeignKey("departments.Department", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="asset_transfers_in")
    reason = models.CharField(max_length=250, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING,
                              db_index=True)
    requested_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="asset_transfers_requested")
    approved_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="asset_transfers_approved")
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]

    @property
    def changes_fund(self):
        return self.from_fund_id != self.to_fund_id

    def __str__(self):
        return f"Transfer of {self.asset.name} ({self.date})"


# ---------------------------------------------------------------------------
# EAM evolution: `Asset` is the going-forward name for the asset record. The
# model keeps the FixedAsset class/table for backward compatibility (external
# code and migrations import FixedAsset; the DB table is unchanged), and `Asset`
# is a first-class alias so new code reads naturally.
Asset = FixedAsset
