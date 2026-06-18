"""Fixed-asset register with configurable depreciation.

Depreciation rules live in DepreciationRule (per category) and can be managed
from settings; an individual asset may override the method/rate. Net book value
feeds the Statement of Financial Position.
"""
import datetime as dt
from decimal import Decimal

from django.db import models
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

    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    acquired_on = models.DateField()
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    salvage_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
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
        # work in progress (and land) is not depreciated until it is in use /
        # reclassified, so it is carried at accumulated cost.
        if self.category in (self.Category.CONSTRUCTION, self.Category.LAND):
            return Decimal(0)
        method, rate = self._policy()
        rate = Decimal(rate or 0)
        if method == DepreciationRule.Method.NONE or rate <= 0:
            return Decimal(0)
        if method == DepreciationRule.Method.STRAIGHT:
            return (self.cost - (self.salvage_value or 0)) * rate / Decimal(100)
        return None  # reducing balance is computed period by period

    def accumulated_depreciation(self, as_of=None, rules=None, cfg=None):
        if self.category in (self.Category.CONSTRUCTION, self.Category.LAND):
            return Decimal(0)
        as_of = as_of or dt.date.today()
        if as_of < self.acquired_on:
            return Decimal(0)
        method, rate = self._policy(rules, cfg)
        rate = Decimal(rate or 0)
        cost = Decimal(self.cost)
        salvage = Decimal(self.salvage_value or 0)
        if method == DepreciationRule.Method.NONE or rate <= 0:
            return Decimal(0)
        # whole years elapsed (simple annual charge)
        years = as_of.year - self.acquired_on.year
        if (as_of.month, as_of.day) < (self.acquired_on.month, self.acquired_on.day):
            years -= 1
        years = max(years + 1, 0)  # charge in the year of acquisition
        if method == DepreciationRule.Method.STRAIGHT:
            annual = (cost - salvage) * rate / Decimal(100)
            acc = min(annual * years, cost - salvage)
            return acc.quantize(Decimal("0.01"))
        # reducing balance
        book = cost
        acc = Decimal(0)
        for _ in range(years):
            charge = book * rate / Decimal(100)
            if book - charge < salvage:
                charge = max(book - salvage, Decimal(0))
            acc += charge
            book -= charge
            if book <= salvage:
                break
        return acc.quantize(Decimal("0.01"))

    def net_book_value(self, as_of=None, rules=None, cfg=None):
        if self.disposed and self.disposed_on and (as_of or dt.date.today()) >= self.disposed_on:
            return Decimal(0)
        return (Decimal(self.cost)
                - self.accumulated_depreciation(as_of, rules, cfg)).quantize(Decimal("0.01"))


def nbv_total(as_of=None):
    """Total net book value of all assets as at a date, computed with the
    depreciation rules and site config loaded ONCE (no per-asset queries)."""
    from core.models import SiteConfig
    rules = {r.category: r for r in DepreciationRule.objects.all()}
    cfg = SiteConfig.get()
    return sum((a.net_book_value(as_of, rules=rules, cfg=cfg)
                for a in FixedAsset.objects.all()), Decimal(0))


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
