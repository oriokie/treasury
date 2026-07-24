"""Suppliers — who the church buys from, and what it owes them.

Why this exists
---------------
A vendor was a string. ``Payable.vendor`` was free text, ``Expense.payee`` was
free text, and the cheque register had its own. So "Mwangi Hardware", "Mwangi
Hardware Ltd" and "mwangi hardware" were three suppliers, the question "what do
we owe Mwangi altogether" could not be asked, and nothing about a supplier —
their terms, their bank details, their KRA PIN, the contract — had anywhere to
live.

This module gives a supplier an identity, and everything that spends money can
point at it.

The design, and its one firm rule
---------------------------------
**The free text stays.** ``Payable.vendor`` and ``Expense.payee`` are not
deleted and not made mandatory. Two reasons, and the second is the important
one. First, a treasurer paying a boda rider once should not have to create a
supplier record. Second, the name written on a voucher is *what the voucher
said* — historical evidence — and re-pointing it at a tidied-up master record
would quietly rewrite what a document actually recorded. So the FK is added
alongside: where it is set, it is authoritative for grouping and reporting;
where it is not, the text still reads as it always did.

Nothing here computes money. What the church owes a supplier is the sum of its
obligations, and those are already computed by
``cashbook.services.treasury_position`` — the vendor profile asks that code, it
does not add its own arithmetic.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils.text import slugify

from simple_history.models import HistoricalRecords


def vendor_document_path(instance, filename):
    return f"vendors/{instance.vendor_id}/{filename}"


def name_key(raw):
    """A comparison key for supplier names.

    Deliberately the same idea as ``members.name_key``: upper-cased, punctuation
    stripped, company suffixes dropped, words sorted. "Mwangi Hardware Ltd" and
    "HARDWARE, MWANGI (LIMITED)" collapse to the same key, which is what lets
    the backfill recognise that a dozen spellings across ten years of payables
    are one supplier — and what stops a treasurer creating an eleventh.
    """
    if not raw:
        return ""
    text = re.sub(r"[^A-Z0-9 ]", " ", str(raw).upper())
    noise = {"LTD", "LIMITED", "CO", "COMPANY", "ENTERPRISES", "ENTERPRISE",
             "SUPPLIERS", "SUPPLIER", "SERVICES", "SERVICE", "AND", "THE",
             "INC", "PLC", "LLC", "GROUP", "TRADERS", "STORES", "GENERAL"}
    words = [w for w in text.split() if w and w not in noise]
    return " ".join(sorted(words))[:160]


class VendorCategory(models.Model):
    """What kind of supplier this is — hardware, printing, catering, utilities.

    A model rather than a fixed enum so a church can add its own without a code
    change, which is the same call `AssetClass` makes in the EAM design for the
    same reason.
    """
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    description = models.CharField(max_length=200, blank=True, default="")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "vendor categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:50]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class VendorTag(models.Model):
    """A free label — "preferred", "prepay only", "board approved".

    Separate from category because a supplier has exactly one kind and any
    number of labels, and conflating the two produces a category list that
    grows without limit.
    """
    name = models.CharField(max_length=40, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    colour = models.CharField(
        max_length=7, blank=True, default="",
        help_text="Optional hex colour for the pill, e.g. #b08d57.")

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:50]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Vendor(models.Model):
    """A supplier the church buys from."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ON_HOLD = "ON_HOLD", "On hold"
        ARCHIVED = "ARCHIVED", "Archived"

    class Terms(models.TextChoices):
        """Payment terms, as the days the church has to pay."""
        IMMEDIATE = "IMMEDIATE", "On delivery"
        NET7 = "NET7", "7 days"
        NET14 = "NET14", "14 days"
        NET30 = "NET30", "30 days"
        NET60 = "NET60", "60 days"
        PREPAY = "PREPAY", "Paid in advance"
        OTHER = "OTHER", "Other / by agreement"

    #: Days implied by each term, for working out a due date.
    TERM_DAYS = {"IMMEDIATE": 0, "NET7": 7, "NET14": 14, "NET30": 30,
                 "NET60": 60, "PREPAY": 0, "OTHER": 0}

    # --- identity ----------------------------------------------------------
    name = models.CharField(max_length=160)
    name_key = models.CharField(max_length=160, db_index=True, editable=False)
    code = models.CharField(
        max_length=20, blank=True, default="", db_index=True,
        help_text="Optional short code the office uses on vouchers.")
    category = models.ForeignKey(VendorCategory, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="vendors")
    tags = models.ManyToManyField(VendorTag, blank=True, related_name="vendors")
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.ACTIVE, db_index=True)

    # --- how we deal with them ---------------------------------------------
    payment_terms = models.CharField(max_length=10, choices=Terms.choices,
                                     default=Terms.IMMEDIATE)
    terms_note = models.CharField(max_length=200, blank=True, default="")
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="What the church is willing to owe this supplier at once. "
                  "Advisory — it warns, it does not block.")

    # --- tax ---------------------------------------------------------------
    tax_pin = models.CharField(
        max_length=20, blank=True, default="",
        help_text="KRA PIN or equivalent tax identifier.",
        validators=[RegexValidator(r"^[A-Za-z0-9\-/ ]*$",
                                   "Use letters, numbers, spaces, - and / only.")])
    tax_exempt = models.BooleanField(default=False)
    withholding_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Withholding tax percentage, where the church is required to "
                  "withhold on payments to this supplier.")

    # --- contact (the headline; more in VendorContact) ----------------------
    phone = models.CharField(max_length=32, blank=True, default="", db_index=True)
    email = models.EmailField(blank=True, default="")
    website = models.URLField(blank=True, default="")

    notes = models.TextField(blank=True, default="")
    archived_on = models.DateField(null=True, blank=True)
    archived_reason = models.CharField(max_length=200, blank=True, default="")

    created_by = models.ForeignKey("auth.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["status", "name"]),
                   models.Index(fields=["name_key"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = " ".join((self.name or "").split())
        self.name_key = name_key(self.name)
        super().save(*args, **kwargs)

    def clean(self):
        # Not a database unique constraint: two genuinely different suppliers
        # can normalise to the same key (two "Grace Stores" in different towns),
        # and a hard constraint would make them impossible to record. A warning
        # the treasurer can override is the right strength — enforced in the
        # form, which can offer the existing record instead.
        # Derived here rather than read off the field: `name_key` is set in
        # save(), which runs after clean(), so trusting the field would make
        # this check silently pass on every new record.
        key = name_key(self.name)
        if key and not self.pk:
            clash = Vendor.objects.filter(name_key=key).first()
            if clash:
                raise ValidationError(
                    {"name": f"“{clash.name}” looks like the same supplier. "
                             f"Use that record, or change this name to tell them apart."})

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def term_days(self):
        return self.TERM_DAYS.get(self.payment_terms, 0)

    def due_date_for(self, invoice_date):
        """When an invoice dated `invoice_date` falls due under these terms."""
        import datetime as _dt
        if not invoice_date:
            return None
        return invoice_date + _dt.timedelta(days=self.term_days)


class VendorContact(models.Model):
    """A person at the supplier. Several, because the person who takes the
    order is rarely the person who chases the payment."""

    class Kind(models.TextChoices):
        GENERAL = "GENERAL", "General"
        SALES = "SALES", "Sales / orders"
        ACCOUNTS = "ACCOUNTS", "Accounts / payments"
        SUPPORT = "SUPPORT", "Support"
        DIRECTOR = "DIRECTOR", "Owner / director"

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE,
                               related_name="contacts")
    name = models.CharField(max_length=120)
    role = models.CharField(max_length=80, blank=True, default="")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.GENERAL)
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    is_primary = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default="")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self):
        return f"{self.name} ({self.vendor.name})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # exactly one primary per vendor — enforced here rather than left to the
        # form, so an import or a shell edit cannot produce two
        if self.is_primary:
            (VendorContact.objects.filter(vendor=self.vendor, is_primary=True)
             .exclude(pk=self.pk).update(is_primary=False))


class VendorAddress(models.Model):
    class Kind(models.TextChoices):
        PHYSICAL = "PHYSICAL", "Physical"
        POSTAL = "POSTAL", "Postal"
        DELIVERY = "DELIVERY", "Delivery"
        BILLING = "BILLING", "Billing"

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE,
                               related_name="addresses")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PHYSICAL)
    line1 = models.CharField(max_length=160, blank=True, default="")
    line2 = models.CharField(max_length=160, blank=True, default="")
    town = models.CharField(max_length=80, blank=True, default="")
    county = models.CharField(max_length=80, blank=True, default="")
    postal_code = models.CharField(max_length=20, blank=True, default="")
    country = models.CharField(max_length=60, blank=True, default="Kenya")
    is_primary = models.BooleanField(default=False)
    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "kind"]
        verbose_name_plural = "vendor addresses"

    def __str__(self):
        parts = [self.line1, self.town, self.country]
        return ", ".join(p for p in parts if p) or self.get_kind_display()


class VendorBankAccount(models.Model):
    """Where the supplier is paid.

    Bank details are the highest-risk field on this record — the classic
    invoice-redirection fraud is a letter announcing "our bank has changed".
    So a change is `verified_by`/`verified_on` and the model keeps its history:
    a treasurer can see who confirmed the account and when, and an auditor can
    see what it was before.
    """

    class Kind(models.TextChoices):
        BANK = "BANK", "Bank account"
        MPESA = "MPESA", "M-Pesa"
        PAYBILL = "PAYBILL", "Paybill"
        TILL = "TILL", "Till number"
        CHEQUE = "CHEQUE", "Cheque only"

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE,
                               related_name="bank_accounts")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.BANK)
    account_name = models.CharField(max_length=160, blank=True, default="")
    bank_name = models.CharField(max_length=120, blank=True, default="")
    branch = models.CharField(max_length=120, blank=True, default="")
    account_number = models.CharField(max_length=40, blank=True, default="")
    paybill_or_till = models.CharField(max_length=20, blank=True, default="")
    swift_code = models.CharField(max_length=20, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    verified_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    verified_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "bank_name"]

    def __str__(self):
        if self.kind in (self.Kind.MPESA, self.Kind.PAYBILL, self.Kind.TILL):
            return f"{self.get_kind_display()} {self.paybill_or_till}"
        return f"{self.bank_name} {self.account_number}".strip()

    @property
    def is_verified(self):
        return self.verified_on is not None

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_primary:
            (VendorBankAccount.objects.filter(vendor=self.vendor, is_primary=True)
             .exclude(pk=self.pk).update(is_primary=False))


class VendorDocument(models.Model):
    class Kind(models.TextChoices):
        CONTRACT = "CONTRACT", "Contract or agreement"
        QUOTE = "QUOTE", "Quotation"
        INVOICE = "INVOICE", "Invoice"
        TAX = "TAX", "Tax certificate"
        REGISTRATION = "REGISTRATION", "Business registration"
        BANK_LETTER = "BANK_LETTER", "Bank confirmation letter"
        CORRESPONDENCE = "CORRESPONDENCE", "Correspondence"
        OTHER = "OTHER", "Other"

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE,
                               related_name="documents")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.OTHER)
    label = models.CharField(max_length=140, blank=True, default="")
    file = models.FileField(upload_to=vendor_document_path)
    original_name = models.CharField(max_length=200, blank=True, default="")
    valid_from = models.DateField(null=True, blank=True)
    expires_on = models.DateField(
        null=True, blank=True,
        help_text="For a contract or a tax certificate — the profile flags it "
                  "as it approaches.")
    uploaded_by = models.ForeignKey("auth.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.label or self.original_name or self.get_kind_display()

    @property
    def is_expired(self):
        import datetime as _dt
        return bool(self.expires_on and self.expires_on < _dt.date.today())


class VendorNote(models.Model):
    """A dated note on the supplier — a price agreed, a complaint, a promise.

    Append-only by intention: notes are edited by adding another, not by
    changing what was written, because a supplier file that can be rewritten is
    no use in a dispute.
    """
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE,
                               related_name="note_entries")
    body = models.TextField()
    author = models.ForeignKey("auth.User", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    pinned = models.BooleanField(
        default=False, help_text="Keep at the top of the supplier's file.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pinned", "-created_at"]

    def __str__(self):
        return f"{self.vendor.name}: {self.body[:40]}"
