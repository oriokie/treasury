from django import forms
from accounts.forms import StyledFormMixin
from .models import SiteConfig


class SiteConfigForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = SiteConfig
        fields = [
            "church_name", "field_name",
            "require_expense_approval", "show_mpesa_ref", "enable_dev_groups",
            "auto_create_members", "envelope_auto_receipt", "receipt_bank_scope",
            "receipt_message",
            "sms_enabled", "sms_receipt_scope", "sms_api_url", "sms_api_key",
            "sms_partner_id", "sms_shortcode", "sms_receipt_template",
            "llm_enabled", "llm_provider", "llm_api_key", "llm_model", "llm_base_url",
            "sig_treasurer", "sig_pastor", "sig_elder",
            "asset_depr_method", "asset_depr_rate",
            "trust_remit_due_day", "petty_cash_float",
            "dual_approval_threshold", "enforce_fund_balance",
            "enforce_petty_float", "require_dual_yearend", "require_import_confirmation",
            "notify_email_enabled", "dev_group_extra_prefixes",
            "numbered_fund_families",
            "sabbath_cutoff_enabled",
            "email_enabled", "email_host", "email_port", "email_use_tls",
            "email_host_user", "email_host_password", "email_from",
            "whatsapp_enabled", "whatsapp_provider", "whatsapp_api_url",
            "whatsapp_api_key", "whatsapp_sender",
            "daraja_enabled", "daraja_shortcode", "daraja_consumer_key",
            "daraja_consumer_secret", "daraja_env",
            "bank_feed_enabled", "bank_feed_auth_mode", "bank_feed_username",
            "bank_feed_password", "bank_feed_token",
            "pledge_match_mode", "pledge_match_same_fund_only",
            "pledge_match_window_days", "pledge_public_form_enabled",
            "backup_email", "require_2fa_for_treasurers",
            "error_alerts_enabled", "error_alert_phone",
            "opening_bank_balance", "opening_cash_on_hand", "opening_unremitted_trust",
            "telegram_enabled", "telegram_bot_token", "telegram_pin",
            "telegram_session_minutes", "telegram_run_in_app",
            "sabbath_confirm_scope",
        ]
        widgets = {"sms_receipt_template": forms.Textarea(attrs={"rows": 3}),
                   "receipt_message": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
        self.fields["sms_api_key"].widget = forms.PasswordInput(
            render_value=True, attrs={"class": "field"})
        self.fields["llm_api_key"].widget = forms.PasswordInput(
            render_value=True, attrs={"class": "field"})
        self.fields["telegram_bot_token"].widget = forms.PasswordInput(
            render_value=True, attrs={"class": "field"})
        self.fields["asset_depr_method"] = forms.ChoiceField(
            label="Default depreciation method",
            choices=[("STRAIGHT", "Straight-line"), ("REDUCING", "Reducing balance"),
                     ("NONE", "Not depreciated")],
            initial=self.instance.asset_depr_method or "STRAIGHT", required=False)
        self.fields["asset_depr_method"].widget.attrs.update({"class": "field--select"})
