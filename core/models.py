from decimal import Decimal
"""Site-wide configuration (a single editable row) and the SMS log.

SiteConfig holds the feature toggles and the Advanta bulk-SMS credentials so a
treasurer can turn capabilities on/off from the UI without touching settings.py.
"""
from django.db import models
from simple_history.models import HistoricalRecords
from core.fields import EncryptedCharField


class SiteConfig(models.Model):
    """One editable row of configuration. Use SiteConfig.get()."""

    # --- Branding ---
    church_name = models.CharField(max_length=120, default="SDA Church")
    field_name = models.CharField(
        max_length=120, default="East Nairobi Field",
        help_text="Where trust funds are remitted.")

    # --- Feature toggles ---
    require_expense_approval = models.BooleanField(
        default=True, help_text="Expenses need treasurer approval before they hit balances.")
    show_mpesa_ref = models.BooleanField(
        default=True, help_text="Show the M-Pesa / channel reference on ledger rows.")
    enable_dev_groups = models.BooleanField(
        default=True, help_text="Track giving against development groups.")
    auto_create_members = models.BooleanField(
        default=True, help_text="Create a member automatically for unknown bank payers.")
    envelope_auto_receipt = models.BooleanField(
        default=True, help_text="Issue a receipt automatically when an envelope is recorded.")

    class ReceiptBankScope(models.TextChoices):
        TRUST_ONLY = "TRUST_ONLY", "Trust funds only"
        ALL = "ALL", "All giving (trust and local)"

    receipt_bank_scope = models.CharField(
        max_length=12, choices=ReceiptBankScope.choices, default=ReceiptBankScope.TRUST_ONLY,
        help_text="Which bank giving the 'Receipt bank giving' button turns into envelopes.")

    class SabbathConfirmScope(models.TextChoices):
        RECEIPTABLE = "RECEIPTABLE", "Only funds we receipt (Trust + LCB)"
        ALL = "ALL", "All bank giving"

    sabbath_confirm_scope = models.CharField(
        max_length=12, choices=SabbathConfirmScope.choices,
        default=SabbathConfirmScope.RECEIPTABLE,
        help_text="Which late-imported gifts appear in the Sabbath confirmations "
                  "queue. By default only the funds you normally receipt (Trust "
                  "funds and the Local Church Budget) — others just post by date.")

    # --- SMS (Advanta bulk SMS) ---
    sms_enabled = models.BooleanField(
        default=False, help_text="Send SMS receipts / notifications via Advanta.")
    sms_api_url = models.CharField(
        max_length=200, default="https://quicksms.advantasms.com",
        help_text="Advanta API base URL (see advantasms.com/bulksms-api).")
    sms_api_key = EncryptedCharField(max_length=500, blank=True)
    sms_partner_id = EncryptedCharField(max_length=500, blank=True)
    sms_shortcode = models.CharField(
        max_length=40, blank=True, help_text="Registered sender ID / shortcode.")
    sms_receipt_template = models.CharField(
        max_length=320, blank=True,
        default="Dear {name}, we received your offering of KES {amount} "
                "(receipt {receipt}) on {date}. God bless you. - {church}",
        help_text="Placeholders: {name} {amount} {receipt} {date} {church}")

    class SmsReceiptScope(models.TextChoices):
        OFF = "OFF", "Don't send receipts"
        ALL = "ALL", "All envelope entries (with a phone)"
        BANK = "BANK", "Bank receipts only (with a phone)"

    sms_receipt_scope = models.CharField(
        max_length=4, choices=SmsReceiptScope.choices, default=SmsReceiptScope.OFF,
        help_text="When a member has a phone number, text them a receipt for these entries.")

    # ---- Assistant / LLM (optional; off by default) ----
    class LlmProvider(models.TextChoices):
        ANTHROPIC = "ANTHROPIC", "Anthropic (Claude)"
        OPENAI = "OPENAI", "OpenAI (GPT)"
        GEMINI = "GEMINI", "Google (Gemini)"
        GROQ = "GROQ", "Groq"
        OPENROUTER = "OPENROUTER", "OpenRouter"
        CUSTOM = "CUSTOM", "Custom (OpenAI-compatible)"

    llm_enabled = models.BooleanField(
        default=False, help_text="Use an external LLM for the assistant. Off = the "
                                 "built-in offline assistant answers from your data.")
    llm_provider = models.CharField(
        max_length=12, choices=LlmProvider.choices, default=LlmProvider.ANTHROPIC)
    llm_api_key = EncryptedCharField(max_length=500, blank=True)
    llm_model = models.CharField(
        max_length=80, blank=True,
        help_text="e.g. claude-3-5-sonnet, gpt-4o-mini, gemini-1.5-flash.")
    llm_base_url = models.CharField(
        max_length=200, blank=True,
        help_text="Only for Custom / self-hosted OpenAI-compatible endpoints.")

    # ---- Telegram bot (remote treasurer access) ----
    telegram_enabled = models.BooleanField(default=False)
    telegram_bot_token = EncryptedCharField(
        max_length=255, blank=True, default="",
        help_text="Bot token from @BotFather on Telegram.")
    telegram_pin = models.CharField(
        max_length=12, blank=True, default="1234",
        help_text="PIN the user must enter at the start of a chat before any data "
                  "is shown or any expense is recorded.")
    telegram_run_in_app = models.BooleanField(
        default=False,
        help_text="Run the Telegram bot inside this app via background polling "
                  "(no separate service or public webhook needed). Turn on for "
                  "simple deployments; leave off if you use the webhook URL.")
    telegram_session_minutes = models.PositiveIntegerField(
        default=30, help_text="Minutes a chat stays unlocked after a correct PIN.")

    # ---- Report signatories ----
    sig_treasurer = models.CharField("Head treasurer", max_length=120, blank=True)
    sig_pastor = models.CharField("Pastor", max_length=120, blank=True)
    sig_elder = models.CharField("First church elder", max_length=120, blank=True)

    # ---- Fixed-asset depreciation defaults (used when no category rule applies) ----
    asset_depr_method = models.CharField(
        max_length=10, default="STRAIGHT",
        help_text="Default depreciation method for new assets.")
    sabbath_cutoff_enabled = models.BooleanField(default=True,
        help_text="When a Sabbath has been closed (counted & receipted), credit any "
                  "later gift for that Sabbath to the next OPEN Sabbath, so a closed "
                  "count is never reopened. Close a Sabbath from its cash-count page.")

    trust_remit_due_day = models.PositiveSmallIntegerField(
        default=15, help_text="Day of the following month by which trust funds "
                              "(tithe, offerings) should be remitted to the field.")
    asset_depr_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0"),
        help_text="Default annual depreciation rate (percent).")
    receipt_message = models.TextField(blank=True,
        help_text="Footer message printed at the bottom of contribution receipts. "
                  "Leave blank to use a default thank-you message.")
    petty_cash_float = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Petty cash imprest (float) — the amount the petty cash is "
                  "topped back up to. Set to 0 if you don't run a fixed float.")

    # ---- approvals & financial controls (configurable) ----
    dual_approval_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Expenses at or above this amount need a second treasurer's approval "
                  "before they can be marked paid. 0 disables dual approval.")
    enforce_fund_balance = models.BooleanField(
        default=True,
        help_text="Block expenses that would take a fund's available balance negative. "
                  "Treasurers may override with a logged note; assistants are always blocked.")
    enforce_petty_float = models.BooleanField(
        default=True,
        help_text="Block petty cash disbursements that exceed the float on hand.")
    require_dual_yearend = models.BooleanField(
        default=False,
        help_text="Require a second treasurer to confirm a year-end close before it is recorded.")
    require_import_confirmation = models.BooleanField(
        default=False,
        help_text="Hold auto-allocated statement imports for review; they only affect "
                  "balances once a treasurer confirms them.")

    # --- Notifications (in-app always on; email optional) ---
    notify_email_enabled = models.BooleanField(default=False,
        help_text="Also email treasurers when an expense needs approval, a remittance "
                  "is overdue, or a budget is exceeded (requires email to be configured).")

    # --- Allocation tuning ---
    dev_group_extra_prefixes = models.CharField(max_length=200, blank=True,
        help_text="Extra words (comma-separated) that mean a development group in "
                  "bank references, e.g. 'project, phase'. The common 'grp/devgroup' "
                  "spellings are always recognised.")
    numbered_fund_families = models.TextField(blank=True, default="",
        help_text="Route numbered narrations straight to numbered funds, one family "
                  "per line as 'prefixes = NAME_TEMPLATE'. {n} is the number found. "
                  "e.g. 'expense, exp, expe = CAMP_{n}' sends EXPENSE1/exp1/expe1 to "
                  "the fund named CAMP_1, EXPENSE30 to CAMP_30, and so on — no need "
                  "for a rule per group. Only applies when that fund exists.")

    # --- Outgoing email (SMTP) ---
    email_enabled = models.BooleanField(default=False,
        help_text="Enable sending email (reports, receipts, notifications) via SMTP.")
    email_host = models.CharField(max_length=120, blank=True, help_text="e.g. smtp.gmail.com")
    email_port = models.PositiveIntegerField(default=587)
    email_use_tls = models.BooleanField(default=True)
    email_host_user = models.CharField(max_length=160, blank=True)
    email_host_password = EncryptedCharField(max_length=255, blank=True, default="")
    email_from = models.CharField(max_length=160, blank=True,
        help_text="From address, e.g. treasurer@church.org")

    # --- WhatsApp receipt delivery (optional, for future use) ---
    whatsapp_enabled = models.BooleanField(default=False)
    whatsapp_provider = models.CharField(max_length=20, default="TWILIO", blank=True,
        help_text="TWILIO or AFRICASTALKING.")
    whatsapp_api_url = models.CharField(max_length=200, blank=True)
    whatsapp_api_key = EncryptedCharField(max_length=255, blank=True, default="")
    whatsapp_sender = models.CharField(max_length=40, blank=True,
        help_text="The WhatsApp Business sender number, e.g. +14155238886.")

    # --- M-Pesa Daraja (Paybill) direct pull (optional, for future use) ---
    daraja_enabled = models.BooleanField(default=False)
    daraja_shortcode = models.CharField(max_length=20, blank=True)
    daraja_consumer_key = EncryptedCharField(max_length=255, blank=True, default="")
    daraja_consumer_secret = EncryptedCharField(max_length=255, blank=True, default="")
    daraja_env = models.CharField(max_length=10, default="SANDBOX", blank=True,
        help_text="SANDBOX or PRODUCTION.")

    # --- Co-operative Bank CBS real-time transaction feed (inbound webhook) ----
    class BankFeedAuth(models.TextChoices):
        BASIC = "BASIC", "Basic authentication (username & password)"
        TOKEN = "TOKEN", "Token / bearer authentication"
        NONE = "NONE", "None (test environment only)"

    bank_feed_enabled = models.BooleanField(default=False,
        help_text="Accept real-time transaction notifications pushed by the bank's "
                  "Core Banking System to this app's webhook endpoint.")
    bank_feed_auth_mode = models.CharField(max_length=8, choices=BankFeedAuth.choices,
        default=BankFeedAuth.BASIC,
        help_text="How the bank authenticates when calling the webhook. Must match "
                  "what was agreed with the bank.")
    bank_feed_username = EncryptedCharField(max_length=255, blank=True, default="",
        help_text="Basic-auth username the bank will present.")
    bank_feed_password = EncryptedCharField(max_length=255, blank=True, default="",
        help_text="Basic-auth password the bank will present.")
    bank_feed_token = EncryptedCharField(max_length=500, blank=True, default="",
        help_text="Shared bearer token the bank will present (sent as "
                  "'Authorization: Bearer <token>' or the 'X-Auth-Token' header).")

    # --- Pledge matching parameters -----------------------------------------
    class PledgeMatchMode(models.TextChoices):
        OFF = "OFF", "Off — never match automatically"
        SUGGEST = "SUGGEST", "Suggest — flag a likely pledge for review"
        AUTO = "AUTO", "Auto — apply the match automatically"

    pledge_match_mode = models.CharField(max_length=8,
        choices=PledgeMatchMode.choices, default=PledgeMatchMode.SUGGEST,
        help_text="What happens when a new contribution arrives from a member who "
                  "has an active pledge: do nothing, flag it for a treasurer to "
                  "confirm, or apply the match automatically.")
    pledge_match_same_fund_only = models.BooleanField(default=True,
        help_text="Only auto-match a contribution to a pledge when the gift's fund "
                  "matches the campaign's target fund.")
    pledge_match_window_days = models.PositiveIntegerField(default=400,
        help_text="How many days after a pledge's end date a contribution may "
                  "still be matched to it.")
    pledge_public_form_enabled = models.BooleanField(default=False,
        help_text="Allow members to submit a pledge from a public link. Submissions "
                  "are held as unverified drafts for a treasurer to review and "
                  "approve — they never post anywhere until approved.")

    # --- Backups -------------------------------------------------------------
    backup_email = models.CharField(max_length=200, blank=True, default="",
        help_text="Email address(es) to send the nightly backup to, comma-separated. "
                  "Leave blank to keep backups on the server only.")

    # --- Security ------------------------------------------------------------
    require_2fa_for_treasurers = models.BooleanField(default=False,
        help_text="Require treasurers to set up two-factor authentication. They "
                  "will be prompted to enrol at next login and cannot skip it.")

    # --- Error alerts --------------------------------------------------------
    error_alerts_enabled = models.BooleanField(default=False,
        help_text="If on, the admin is alerted when an unexpected error occurs, "
                  "via whichever of email / SMS / WhatsApp are enabled.")
    error_alert_phone = models.CharField(max_length=32, blank=True, default="",
        help_text="Phone number to receive SMS / WhatsApp error alerts "
                  "(uses the SMS and WhatsApp settings already configured).")

    # --- Opening cash position at the start of the financial year ------------
    # Lets the Statement of Financial Position open with the real split of cash
    # rather than implying everything sits in the bank.
    opening_bank_balance = models.DecimalField(max_digits=14, decimal_places=2,
        default=0, help_text="Bank statement balance at the start of the year.")
    opening_cash_on_hand = models.DecimalField(max_digits=14, decimal_places=2,
        default=0, help_text="Cash not yet banked at the start of the year.")
    opening_unremitted_trust = models.DecimalField(max_digits=14, decimal_places=2,
        default=0, help_text="Trust funds collected but not yet remitted at year start.")

    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "site configuration"
        verbose_name_plural = "site configuration"

    def __str__(self):
        return "Site configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SmsLog(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        DISABLED = "DISABLED", "SMS disabled"

    to = models.CharField(max_length=20)
    message = models.TextField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED)
    response = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"SMS to {self.to} [{self.status}]"


class PeriodLock(models.Model):
    """A closed accounting period. Entries dated within a locked month cannot be
    created, edited, reversed or deleted except by an admin override. Locking a
    quarter or a year locks the underlying months."""
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()   # 1..12
    locked_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    locked_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("year", "month")
        ordering = ["-year", "-month"]

    def __str__(self):
        import calendar
        return f"{calendar.month_name[self.month]} {self.year}"


def period_locked(d):
    """Return the PeriodLock covering date d, or None."""
    if not d:
        return None
    return PeriodLock.objects.filter(year=d.year, month=d.month).first()


class TelegramSession(models.Model):
    """Per-chat state for the Telegram bot: PIN authentication window and any
    in-progress multi-step flow (e.g. recording an expense)."""
    chat_id = models.CharField(max_length=40, unique=True)
    user = models.ForeignKey("auth.User", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="telegram_sessions")
    authenticated_until = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=30, blank=True, default="")  # e.g. EXP_AMOUNT
    state_data = models.JSONField(default=dict, blank=True)
    last_seen = models.DateTimeField(auto_now=True)

    def is_authenticated(self):
        from django.utils import timezone
        return bool(self.authenticated_until and self.authenticated_until > timezone.now())

    def reset_flow(self):
        self.state = ""
        self.state_data = {}


class SabbathClose(models.Model):
    """A counted-and-receipted Sabbath. Closing a Sabbath fixes its offering/count
    figures: any gift that arrives later for a closed Sabbath is credited to the
    next OPEN Sabbath instead, so a closed count is never reopened. This is an
    event (you close when you finish pooling/counting), not a fixed clock time."""
    sabbath = models.DateField(unique=True)        # the Saturday
    closed_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    closed_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-sabbath"]

    def __str__(self):
        return f"Sabbath closed {self.sabbath}"


def sabbath_is_closed(d):
    return bool(d) and SabbathClose.objects.filter(sabbath=d).exists()


def entry_blocked(d):
    """Reason string when entries dated d may not be created, edited or deleted:
    the month is locked, or d is a closed Sabbath. None when posting is open."""
    lk = period_locked(d)
    if lk:
        return f"{lk} is locked — unlock the period before changing entries in it."
    if sabbath_is_closed(d):
        return (f"Sabbath {d:%d %b %Y} is closed — reopen it from the Envelopes "
                "page before adding, editing or deleting its entries.")
    return None


def next_open_sabbath(natural):
    """The earliest Saturday on/after `natural` that has not been closed."""
    import datetime as _dt
    if not natural:
        return natural
    closed = set(SabbathClose.objects.filter(sabbath__gte=natural)
                 .values_list("sabbath", flat=True))
    s = natural
    # bounded walk (a year of Sabbaths) so a misconfiguration can't loop forever
    for _ in range(54):
        if s not in closed:
            return s
        s += _dt.timedelta(days=7)
    return s


def service_sabbath_for(date, as_of=None):
    """The Sabbath a gift dated `date` should be credited to: its natural Sabbath
    (the Saturday of its week), rolled forward past any closed Sabbaths.

    When `as_of` is given (the day the gift is being entered/imported) and the
    natural Sabbath already fell on or before it, the gift is rolled to the next
    open Sabbath — a gift can never be credited to a Sabbath that has already
    happened at the time it is recorded. This is what makes a 6th-dated row
    imported on the 11th schedule to the 13th, exactly like a Sunday row does."""
    import datetime as _dt
    from core.utils import sabbath_of
    if not date:
        return None
    cfg = SiteConfig.get()
    natural = sabbath_of(date)
    if not cfg.sabbath_cutoff_enabled:
        return natural
    target = next_open_sabbath(natural)
    if as_of and target < as_of:
        # the natural Sabbath has passed relative to entry day: schedule to the
        # next Sabbath *after* the entry day (snap as_of to its Saturday first).
        target = next_open_sabbath(sabbath_of(as_of + _dt.timedelta(days=1)))
    return target


class YearEndClose(models.Model):
    """An auditable record that a financial year has been formally closed and its
    fund balances carried forward. Closing a year also locks its months."""
    year = models.PositiveIntegerField(unique=True)
    closed_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    closed_at = models.DateTimeField(auto_now_add=True)
    total_carried = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    note = models.CharField(max_length=200, blank=True)
    confirmed_by = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="yearend_confirmations",
        help_text="Second treasurer who confirmed the close (when dual confirmation is required).")
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"Year-end close {self.year}"

    @property
    def is_effective(self):
        """A close takes effect once balances have been carried forward (which only
        happens after second-treasurer confirmation when dual confirmation is on)."""
        return self.lines.exists()


class FundCarryForward(models.Model):
    """Snapshot of a fund's closing balance at a year-end close (the balance
    carried forward as the opening balance of the next year)."""
    close = models.ForeignKey(YearEndClose, on_delete=models.CASCADE, related_name="lines")
    department = models.ForeignKey("departments.Department", on_delete=models.CASCADE)
    closing_balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["department__name"]


class Notification(models.Model):
    """A lightweight in-app alert (with optional email) for treasurers — e.g. an
    expense awaiting approval, an overdue remittance, or a budget overrun."""
    class Kind(models.TextChoices):
        APPROVAL = "APPROVAL", "Approval needed"
        REMITTANCE = "REMITTANCE", "Remittance"
        BUDGET = "BUDGET", "Budget"
        GENERAL = "GENERAL", "General"

    recipient = models.ForeignKey("auth.User", null=True, blank=True,
        on_delete=models.CASCADE, related_name="notifications",
        help_text="Null = visible to all treasurers.")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.GENERAL)
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=200, blank=True)
    read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.kind}] {self.message}"


class HistoricalYear(models.Model):
    """A prior year's headline totals (collection, trust fund, expenditure) kept
    for multi-year comparison. Reference data only — it never affects the live
    ledger or fund balances."""
    year = models.PositiveIntegerField(unique=True)
    collection = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    trust_fund = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    expenditure = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["year"]

    def __str__(self):
        return f"{self.year}: collection {self.collection}"

    @property
    def net(self):
        return self.collection - self.trust_fund - self.expenditure


class HistoricalMonth(models.Model):
    """A prior year's per-month totals, for seasonality analysis. Reference data
    only — never affects the live ledger."""
    year = models.PositiveIntegerField(db_index=True)
    month = models.PositiveSmallIntegerField()   # 1..12
    collection = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    trust_fund = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    expenditure = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        ordering = ["year", "month"]
        constraints = [models.UniqueConstraint(fields=["year", "month"],
                                               name="uniq_hist_year_month")]

    def __str__(self):
        return f"{self.year}-{self.month:02d}"


class TelegramProfile(models.Model):
    """Links a Telegram PIN to a specific app user, so an expense raised over
    Telegram is attributed to the real person who raised it. Each user sets
    their own PIN; the bot identifies the user by the PIN they enter."""
    user = models.OneToOneField("auth.User", on_delete=models.CASCADE,
                                related_name="telegram_profile")
    pin = EncryptedCharField(max_length=200, blank=True, default="",
                             help_text="Personal PIN for this user on the Telegram bot.")
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"Telegram PIN for {self.user.username}"

    @staticmethod
    def user_for_pin(pin):
        """Return the active user whose personal PIN matches, else None.
        Compared in constant time to avoid leaking which PINs exist."""
        import hmac
        pin = (pin or "").strip()
        if not pin:
            return None
        for prof in TelegramProfile.objects.filter(active=True).select_related("user"):
            if prof.user.is_active and hmac.compare_digest(str(prof.pin), pin):
                return prof.user
        return None
