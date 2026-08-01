from decimal import Decimal
"""Site-wide configuration (a single editable row) and the SMS log.

SiteConfig holds the feature toggles and the Advanta bulk-SMS credentials so a
treasurer can turn capabilities on/off from the UI without touching settings.py.
"""
from django.db import models
from simple_history.models import HistoricalRecords
from core.fields import EncryptedCharField


import threading

#: request-scoped memo for SiteConfig.get() — see SiteConfig.get docstring and
#: core.middleware.SiteConfigCacheMiddleware (recommendation #2, Option A)
_siteconfig_local = threading.local()


class SiteConfig(models.Model):
    """One editable row of configuration. Use SiteConfig.get()."""

    # --- Branding ---
    church_name = models.CharField(max_length=120, default="SDA Church")
    field_name = models.CharField(
        max_length=120, default="East Nairobi Field",
        help_text="Where trust funds are remitted.")
    church_address = models.TextField(
        blank=True, help_text="Postal / physical address shown on report letterheads.")
    church_contact = models.CharField(
        max_length=200, blank=True,
        help_text="Phone and/or email shown on report letterheads.")
    currency_symbol = models.CharField(
        max_length=8, default="KSh",
        help_text="Currency symbol shown before amounts (e.g. KSh, USD, GHS).")
    report_footer_note = models.CharField(
        max_length=200, blank=True,
        help_text="Optional note printed at the foot of reports (e.g. a motto or verse).")
    receipt_strip_strings = models.TextField(
        blank=True,
        help_text="One phrase per line. These are removed from bank/M-Pesa receipt "
                  "messages when they are saved (e.g. the 'never share your PIN' "
                  "boilerplate), keeping stored receipts short and readable. Use * "
                  "as a wildcard for parts that change each time, e.g. an amount: "
                  "'New M-PESA balance is Ksh*.' strips that whole sentence "
                  "whatever the figure is.")

    # --- Church-wide goals (Camp Meeting) ---
    # --- Camp Meeting Offering goal (church-wide, Trust fund) ---
    # This is a single church-wide trust-fund target, so it's configured here
    # rather than on any individual fund. The Camp Meeting EXPENSE goal (a
    # Local fund) and every dev-group/sub-account goal stay on the fund itself
    # — only this one trust-fund figure moves to settings.
    camp_offering_fund = models.ForeignKey(
        "departments.Department", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", limit_choices_to={"fund_type": "TRUST"},
        help_text="The Trust fund collecting the Camp Meeting offering.")
    camp_offering_goal = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="This year's Camp Meeting Offering goal.")

    capitalisation_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Purchases below this amount are treated as running costs rather "
                  "than assets, to keep small items off the register. 0 = no threshold.")

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
        help_text="Which late-imported contributions appear in the Sabbath confirmations "
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
    site_base_url = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Public address of this site, e.g. https://kws.oriokie.com — used to "
                  "turn report links in Telegram replies and emails into clickable URLs.")

    # ---- Telegram envelope entry (assistants record offerings remotely) ----
    telegram_envelope_enabled = models.BooleanField(
        default=True,
        help_text="Allow recording envelope/offering giving from Telegram (/envelope).")
    telegram_allow_new_member = models.BooleanField(
        default=False,
        help_text="Let Telegram entry create a NEW member when no existing member "
                  "matches the typed name. Off = assistants can only record for "
                  "members already in the system (safer).")
    telegram_envelope_confirm = models.BooleanField(
        default=True,
        help_text="Show a summary and require a 'yes' before saving each envelope.")
    class TgEnvChannel(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK = "BANK", "Bank / already given"
    telegram_envelope_channel = models.CharField(
        max_length=4, choices=TgEnvChannel.choices, default=TgEnvChannel.CASH,
        help_text="Default channel for envelopes recorded via Telegram.")
    telegram_envelope_funds = models.ManyToManyField(
        "departments.Department", blank=True, related_name="telegram_envelope_configs",
        help_text="Funds offered when recording an envelope on Telegram. "
                  "Leave empty to use the active offering funds automatically.")

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
                  "later contribution for that Sabbath to the next OPEN Sabbath, so a closed "
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
    require_different_approver = models.BooleanField(
        default=False,
        help_text="Block a treasurer from approving an expense they recorded themselves, "
                  "for every expense (not just those above the dual-approval threshold). "
                  "Leave off if there is only one active treasurer.")
    auto_lock_on_reconciliation = models.BooleanField(
        default=False,
        help_text="Automatically lock the accounting month once a bank reconciliation for "
                  "it is marked reconciled, so a later edit can't silently invalidate a "
                  "completed reconciliation. Off by default since it also blocks routine "
                  "corrections in that month once locked; a warning is always shown when "
                  "editing an entry in an already-reconciled period regardless of this "
                  "setting.")
    leader_delete_window_days = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="A department leader may only delete their own already-posted advance "
                  "expense line within this many days of entering it (afterwards only a "
                  "treasurer can remove it). Leave blank for no time limit.")
    archive_replaced_ledger_entries = models.BooleanField(
        default=True,
        help_text="Keep a snapshot of a general-ledger entry's prior detail whenever a "
                  "correction causes it to be replaced (e.g. editing an expense's amount "
                  "after it was posted), so what the ledger said before the correction "
                  "can still be reviewed. Recommended on; adds one small archive row per "
                  "correction.")

    # --- Notifications (in-app always on; email optional) ---
    notify_email_enabled = models.BooleanField(default=False,
        help_text="Also email treasurers when an expense needs approval, a remittance "
                  "is overdue, or a budget is exceeded (requires email to be configured).")

    # --- Allocation tuning ---
    dev_group_builder_apply = models.BooleanField(default=False,
        help_text="Allow the development-group builder to actually create groups and "
                  "reassign members in the system. Off = the builder only produces a "
                  "downloadable Excel/CSV proposal and never changes any data.")
    numbered_fund_families = models.TextField(blank=True, default="",
        help_text="Route numbered narrations straight to numbered funds, one family "
                  "per line as 'prefixes = NAME_TEMPLATE'. {n} is the number found. "
                  "e.g. 'expense, exp, expe = CAMP_{n}' sends EXPENSE1/exp1/expe1 to "
                  "the fund named CAMP_1, EXPENSE30 to CAMP_30, and so on — no need "
                  "for a rule per group. A prefix wrapped in slashes is a regular "
                  "expression for misspellings/variations, e.g. '/expen[sc]es?/, exp "
                  "= CAMP_{n}' also catches EXPENCE7 and EXPENSES7. Patterns match "
                  "the lowercased reference with punctuation removed; an invalid "
                  "pattern is ignored. Only applies when that fund exists.")
    envelope_default_funds = models.TextField(blank=True, default="",
        help_text="Which fund columns a new Sabbath envelope sheet opens with, "
                  "one column key per line, in the order they should appear. "
                  "Blank means the built-in list. Set from the Envelope "
                  "columns page — a key naming a fund that no longer exists is "
                  "ignored rather than leaving a blank column.")
    allocation_priority = models.TextField(blank=True, default="",
        help_text="The order allocation sources are tried in, one key per line. "
                  "Blank means the built-in order. Set from the Allocation "
                  "priority page rather than by hand — the keys are defined in "
                  "core.services.allocation_priority, and an unknown one is "
                  "ignored rather than obeyed.")
    # ---- Cheque printing (onto the actual bank leaf) -----------------------
    #
    # Printing onto a real cheque means putting ink at exact millimetre positions
    # on a pre-printed leaf. Every bank's leaf differs — by a few millimetres, and
    # sometimes more — and there is no single layout that is right for all of
    # them. Guessing would waste numbered cheque leaves, which are neither free
    # nor replaceable.
    #
    # So nothing here is guessed. The defaults are a common Kenyan CTS size and a
    # sensible starting layout; a treasurer prints the CALIBRATION SHEET onto one
    # spoiled leaf, reads off where the marks actually landed, and adjusts. Once.
    cheque_width_mm = models.DecimalField(
        max_digits=6, decimal_places=1, default=Decimal("180.0"),
        help_text="Width of your bank's cheque leaf, in millimetres. Measure one.")
    cheque_height_mm = models.DecimalField(
        max_digits=6, decimal_places=1, default=Decimal("80.0"),
        help_text="Height of the leaf, in millimetres.")
    cheque_offset_x_mm = models.DecimalField(
        max_digits=6, decimal_places=1, default=Decimal("0.0"),
        help_text="Nudge EVERYTHING right (+) or left (−) by this many millimetres. "
                  "Use this to correct how your printer grips the paper, rather than "
                  "moving every field one at a time.")
    cheque_offset_y_mm = models.DecimalField(
        max_digits=6, decimal_places=1, default=Decimal("0.0"),
        help_text="Nudge everything down (+) or up (−).")
    cheque_date_x_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                           default=Decimal("120.0"))
    cheque_date_y_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                           default=Decimal("14.0"))
    cheque_payee_x_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                            default=Decimal("22.0"))
    cheque_payee_y_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                            default=Decimal("28.0"))
    cheque_words1_x_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                             default=Decimal("22.0"))
    cheque_words1_y_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                             default=Decimal("39.0"))
    cheque_words2_x_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                             default=Decimal("10.0"))
    cheque_words2_y_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                             default=Decimal("48.0"))
    cheque_amount_x_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                             default=Decimal("132.0"))
    cheque_amount_y_mm = models.DecimalField(max_digits=6, decimal_places=1,
                                             default=Decimal("48.0"))
    cheque_font_pt = models.PositiveSmallIntegerField(
        default=11, help_text="Point size of the printed text.")
    cheque_ac_payee_only = models.BooleanField(
        default=True,
        help_text="Also print “A/C PAYEE ONLY” between the crossing lines. Most "
                  "church cheques are crossed; untick if yours are pre-crossed.")

    lcb_departments = models.ManyToManyField("departments.Department", blank=True,
        related_name="+",
        help_text="The Local Church Budget fund and its sub-accounts. When set, "
                  "reports use exactly these (plus any of their sub-accounts) as the "
                  "LCB group, instead of guessing from the name. Leave empty to fall "
                  "back to matching funds named 'LCB' / 'Local Church Budget'.")

    # --- Outgoing email (SMTP) ---
    email_enabled = models.BooleanField(default=False,
        help_text="Enable sending email (reports, receipts, notifications) via SMTP.")
    email_host = models.CharField(max_length=120, blank=True, help_text="e.g. smtp.gmail.com")
    email_port = models.PositiveIntegerField(default=587)
    email_use_tls = models.BooleanField(default=True,
        help_text="STARTTLS — use for port 587. Turn OFF for port 465.")
    email_use_ssl = models.BooleanField(default=False,
        help_text="Implicit SSL — required for port 465. (Auto-enabled for port 465.)")
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
        help_text="Only auto-match a contribution to a pledge when the contribution's fund "
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
    offsite_backup_enabled = models.BooleanField(default=False,
        help_text="Also upload each backup to off-site storage over HTTPS "
                  "(e.g. Nextcloud/WebDAV or an object store that accepts an "
                  "authenticated upload).")
    offsite_backup_url = models.CharField(max_length=300, blank=True, default="",
        help_text="Destination folder/URL, e.g. https://cloud.example.com/remote.php/dav/"
                  "files/treasury/backups/ — the file name is appended.")
    offsite_backup_user = models.CharField(max_length=160, blank=True, default="")
    offsite_backup_password = EncryptedCharField(max_length=255, blank=True, default="")

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
        """The one settings row. Memoized PER REQUEST via a thread-local that
        SiteConfigCacheMiddleware opens and closes (recommendation #2, Option
        A): pages were observed issuing 7–11 identical queries for this row
        per request. Deliberately NOT cached across requests — several fields
        gate security and financial controls, and this deployment's default
        LocMemCache is per-process, so cross-request caching could serve stale
        control settings under a multi-worker server. Outside a request
        (shell, management commands, tests calling get() directly) the scope
        flag is off and every call reads the database exactly as before.
        ``save()`` drops the memo so a change made mid-request is seen by the
        rest of that same request."""
        cached = getattr(_siteconfig_local, "obj", None)
        if cached is not None:
            return cached
        obj, _ = cls.objects.get_or_create(pk=1)
        if getattr(_siteconfig_local, "scope_open", False):
            _siteconfig_local.obj = obj
        return obj

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # a settings change must be visible to the remainder of this request
        _siteconfig_local.obj = None

    # ---- Board report configuration (#4) ----
    BOARD_SECTIONS = [
        ("narrative",  "Narrative & insights"),
        ("income_exp", "Income and expenditure"),
        ("position",   "Statement of financial position"),
        ("funds",      "Fund balances"),
        ("trust",      "Trust funds and remittance"),
        ("goals",      "Goals and targets"),
        ("trend",      "Multi-year trend"),
        ("notes",      "Notes"),
        ("signatures", "Signature block"),
    ]

    board_config = models.JSONField(default=dict, blank=True,
        help_text="Board report layout: ordered section visibility and options.")

    def board_settings(self):
        """Merged board config: an ordered list of {key,label,visible} plus the
        notes text. Missing/legacy config falls back to all sections visible in
        the default order."""
        cfg = self.board_config or {}
        saved = {s["key"]: s for s in cfg.get("sections", []) if "key" in s}
        sections, seen = [], set()
        # keep any saved order first, then append any new sections at the end
        for s in cfg.get("sections", []):
            key = s.get("key")
            if key and key in dict(self.BOARD_SECTIONS) and key not in seen:
                sections.append({"key": key, "label": dict(self.BOARD_SECTIONS)[key],
                                 "visible": s.get("visible", True)})
                seen.add(key)
        for key, label in self.BOARD_SECTIONS:
            if key not in seen:
                sections.append({"key": key, "label": label, "visible": True})
        return {"sections": sections, "notes": cfg.get("notes", "")}


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
    figures: any contribution that arrives later for a closed Sabbath is credited to the
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


def reconciled_period_warning(d):
    """Non-blocking warning when d falls within a bank reconciliation that
    already balances: editing/adding an entry here won't be stopped (unless
    the period is also locked — see entry_blocked), but it may silently
    invalidate a reconciliation that was already signed off as correct.
    Returns a warning string, or None if there's nothing to flag."""
    if not d:
        return None
    from statements.models import BankReconciliation
    rec = (BankReconciliation.objects.filter(
               statement_date__year=d.year, statement_date__month=d.month)
           .order_by("-statement_date").first())
    if rec and rec.is_reconciled:
        return (f"Note: {rec.statement_date:%B %Y} already has a bank reconciliation "
                "that balances. This change may need it to be re-checked.")
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
    """The Sabbath a contribution dated `date` should be credited to: its natural Sabbath
    (the Saturday of its week), rolled forward past any closed Sabbaths.

    When `as_of` is given (the day the contribution is being entered/imported) and the
    natural Sabbath already fell on or before it, the contribution is rolled to the next
    open Sabbath — a contribution can never be credited to a Sabbath that has already
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
    carried forward as the opening balance of the next year).

    PROTECT, not CASCADE: this is a permanent audit record of what a fund's
    balance was at a specific year-end close. Deleting a department should
    never silently take a historical closing-balance record with it — that
    would happen, for instance, if a fund's transactions were reassigned
    elsewhere (transactions/expenses use PROTECT, so they'd no longer block
    deletion) while this snapshot of its past was left behind."""
    close = models.ForeignKey(YearEndClose, on_delete=models.CASCADE, related_name="lines")
    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT)
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

    @property
    def month_label(self):
        import calendar
        return calendar.month_abbr[self.month] if 1 <= self.month <= 12 else str(self.month)

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


class UserPreference(models.Model):
    """Per-user workspace preferences (appearance, dashboard, tables, a11y,
    notifications). One row per user; persists across devices."""

    class Theme(models.TextChoices):
        SYSTEM = "SYSTEM", "System"
        LIGHT = "LIGHT", "Light"
        DARK = "DARK", "Dark"

    class Sidebar(models.TextChoices):
        EXPANDED = "EXPANDED", "Expanded"
        COMPACT = "COMPACT", "Compact"
        ICON = "ICON", "Icon-only"

    class FontSize(models.TextChoices):
        SMALL = "SMALL", "Small"
        MEDIUM = "MEDIUM", "Medium"
        LARGE = "LARGE", "Large"
    class FontFamily(models.TextChoices):
        DEFAULT = "DEFAULT", "Public Sans (default)"
        SYSTEM = "SYSTEM", "System UI"
        SERIF = "SERIF", "Serif (Georgia)"
        LEGIBLE = "LEGIBLE", "Atkinson Hyperlegible"
        MONO = "MONO", "Monospace"

    class Width(models.TextChoices):
        BOXED = "BOXED", "Boxed"
        FULL = "FULL", "Full width"

    class Cards(models.TextChoices):
        ROUNDED = "ROUNDED", "Rounded"
        SQUARE = "SQUARE", "Square"

    class Density(models.TextChoices):
        COMFORTABLE = "COMFORTABLE", "Comfortable"
        COMPACT = "COMPACT", "Compact"

    class HeadingFont(models.TextChoices):
        SERIF = "SERIF", "Serif (Fraunces — default)"
        SANS = "SANS", "Sans-serif (match body)"

    class FigureFont(models.TextChoices):
        MONO = "MONO", "Tabular monospace (default)"
        BODY = "BODY", "Match body text"

    class TableGrid(models.TextChoices):
        ROWS = "ROWS", "Row lines only"
        GRID = "GRID", "Full grid"

    class Negatives(models.TextChoices):
        MINUS = "MINUS", "Minus sign  −1,234.50"
        PARENS = "PARENS", "Parentheses  (1,234.50)"

    # preset accent palette (key -> hex). CUSTOM uses accent_custom.
    ACCENT_PRESETS = {
        "forest": "#1f5f4f", "brass": "#b07d2c", "blue": "#2c5d86",
        "plum": "#6b3b6e", "teal": "#157f7b", "rust": "#a4502b",
        "indigo": "#3f4d9c", "slate": "#41565f",
    }

    user = models.OneToOneField("auth.User", on_delete=models.CASCADE,
                                related_name="preference")

    # --- Appearance ---
    theme = models.CharField(max_length=8, choices=Theme.choices, default=Theme.LIGHT)
    accent = models.CharField(max_length=16, default="forest",
        help_text="Preset key (e.g. 'forest') or 'custom'.")
    accent_custom = models.CharField(max_length=7, blank=True, default="",
        help_text="Custom accent hex when accent='custom'.")
    sidebar = models.CharField(max_length=10, choices=Sidebar.choices,
                               default=Sidebar.EXPANDED)
    sidebar_style = models.CharField(max_length=10, default="FOREST",
        choices=[("FOREST", "Forest (default)"), ("MIDNIGHT", "Midnight"),
                 ("BRASS", "Brass"), ("CHARCOAL", "Charcoal")],
        help_text="The colour treatment of the navigation sidebar.")
    font_size = models.CharField(max_length=6, choices=FontSize.choices,
                                 default=FontSize.MEDIUM)
    font_family = models.CharField(max_length=8, choices=FontFamily.choices,
                                   default=FontFamily.SYSTEM,
        help_text="The typeface used across the app's body text.")
    layout_width = models.CharField(max_length=5, choices=Width.choices,
                                    default=Width.BOXED)
    card_style = models.CharField(max_length=7, choices=Cards.choices,
                                  default=Cards.ROUNDED)
    heading_font = models.CharField(max_length=6, choices=HeadingFont.choices,
        default=HeadingFont.SERIF,
        help_text="Typeface for page titles and section headings.")
    negatives = models.CharField(max_length=6, choices=Negatives.choices,
        default=Negatives.MINUS,
        help_text="How negative figures are shown. Accounting convention puts "
                  "them in parentheses; the parentheses are real characters, so "
                  "they survive Word, Excel, PDF and print.")
    figure_font = models.CharField(max_length=5, choices=FigureFont.choices,
        default=FigureFont.MONO,
        help_text="How monetary figures are set. Tabular monospace keeps "
                  "columns of numbers perfectly aligned.")

    # --- Dashboard ---
    dashboard_widgets = models.JSONField(default=list, blank=True,
        help_text="Ordered list of {key, visible} for dashboard widgets.")
    landing_page = models.CharField(max_length=40, default="dashboard",
        help_text="URL name to open after login.")

    # --- Tables ---
    rows_per_page = models.PositiveSmallIntegerField(default=25)
    density = models.CharField(max_length=11, choices=Density.choices,
                               default=Density.COMFORTABLE)
    table_stripes = models.BooleanField(default=True,
        help_text="Subtle alternating row shading in tables.")
    table_grid = models.CharField(max_length=5, choices=TableGrid.choices,
        default=TableGrid.ROWS,
        help_text="Row lines only (default) or a full grid with column lines.")
    sticky_headers = models.BooleanField(default=True,
        help_text="Keep table headers visible while scrolling long tables.")
    table_state = models.JSONField(default=dict, blank=True,
        help_text="Per-table saved columns / sort / filters, keyed by table id.")

    # --- Accessibility ---
    high_contrast = models.BooleanField(default=False)
    reduced_motion = models.BooleanField(default=False)
    large_targets = models.BooleanField(default=False)
    focus_indicators = models.BooleanField(default=True)

    # --- Notifications ---
    toasts_enabled = models.BooleanField(default=True)
    toast_duration = models.PositiveSmallIntegerField(default=6,
        help_text="Seconds a toast stays on screen.")
    desktop_notifications = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    # landing pages a user may choose (url name -> label)
    LANDING_CHOICES = [
        ("dashboard", "Home dashboard"),
        ("executive", "Executive overview"),
        ("transaction_list", "Giving ledger"),
        ("envelope_list", "Envelopes"),
        ("member_list", "Members"),
        ("expense_list", "Expenses"),
        ("benevolent_dashboard", "Benevolent schemes"),
        ("budget", "Budgeting"),
        ("report_index", "Reports"),
        ("report_board", "Monthly Treasurer's Report"),
        ("reconciliation_list", "Bank reconciliation"),
    ]

    DEFAULT_WIDGETS = [
        {"key": "attention", "label": "Needs attention", "visible": True},
        {"key": "kpis", "label": "Key figures", "visible": True},
        {"key": "sabbath", "label": "Latest Sabbath snapshot", "visible": True},
        {"key": "charts", "label": "Income & expense charts", "visible": True},
        {"key": "funds", "label": "Fund balances", "visible": True},
        {"key": "trend", "label": "Multi-year trend", "visible": True},
        {"key": "recent", "label": "Recent imports", "visible": True},
    ]

    def __str__(self):
        return f"Preferences for {self.user}"

    @classmethod
    def get_for(cls, user):
        if not user or not getattr(user, "is_authenticated", False):
            return None
        pref, created = cls.objects.get_or_create(user=user)
        if created or not pref.dashboard_widgets:
            pref.dashboard_widgets = [dict(w) for w in cls.DEFAULT_WIDGETS]
            pref.save(update_fields=["dashboard_widgets"])
        return pref

    @property
    def accent_hex(self):
        if self.accent == "custom" and self.accent_custom:
            return self.accent_custom
        return self.ACCENT_PRESETS.get(self.accent, self.ACCENT_PRESETS["forest"])

    def merged_widgets(self):
        """Saved widget order/visibility, reconciled with the current default set
        (new widgets appended, removed ones dropped)."""
        saved = {w.get("key"): w for w in (self.dashboard_widgets or [])}
        out, seen = [], set()
        for w in (self.dashboard_widgets or []):
            k = w.get("key")
            if k and any(d["key"] == k for d in self.DEFAULT_WIDGETS) and k not in seen:
                label = next(d["label"] for d in self.DEFAULT_WIDGETS if d["key"] == k)
                out.append({"key": k, "label": label,
                            "visible": bool(w.get("visible", True))})
                seen.add(k)
        for d in self.DEFAULT_WIDGETS:
            if d["key"] not in seen:
                out.append(dict(d))
        return out

    def visible_widget_keys(self):
        return [w["key"] for w in self.merged_widgets() if w["visible"]]

    def reset_to_defaults(self):
        f = self._meta
        for name in ("theme", "accent", "accent_custom", "sidebar", "font_size",
                     "layout_width", "card_style", "landing_page", "rows_per_page",
                     "density", "high_contrast", "reduced_motion", "large_targets",
                     "focus_indicators", "toasts_enabled", "toast_duration",
                     "desktop_notifications"):
            setattr(self, name, f.get_field(name).get_default())
        self.dashboard_widgets = [dict(w) for w in self.DEFAULT_WIDGETS]
        self.table_state = {}
        self.save()


class Organization(models.Model):
    """A church / entity — the root of multi-church scoping.

    Scaffolding only in this phase: model FKs that reference an organization are
    nullable and default to the single implicit church returned by
    ``get_default()``, so a one-church install behaves exactly as before. Later
    phases activate per-entity scoping and upward consolidation (union /
    conference) without a painful migration, because the FK already exists.
    """
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    is_default = models.BooleanField(default=False,
        help_text="The implicit church used wherever an organization isn't set.")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def get_default(cls):
        """The default church, created on first use so single-church installs
        never have to think about organizations."""
        org = cls.objects.filter(is_default=True).order_by("id").first()
        if org is None:
            from core.models import SiteConfig
            name = getattr(SiteConfig.get(), "church_name", "") or "Our Church"
            org = cls.objects.create(name=name, slug="default", is_default=True)
        return org


# Financial Intelligence status persistence (insight dismissal audit trail).
from core.models_intelligence import (  # noqa: E402,F401
    InsightStatus, InsightStatusHistory)
