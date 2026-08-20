from django import forms
from .models import PledgeCampaign, Pledge


class CampaignForm(forms.ModelForm):
    class Meta:
        model = PledgeCampaign
        fields = ["name", "description", "target_department", "goal_amount",
                  "start_date", "end_date", "status", "show_public_progress"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }


class PledgeForm(forms.ModelForm):
    """Office pledge form: member optional so a visitor can pledge; phone is
    always required (M-Pesa matching and reminders)."""

    phone = forms.CharField(
        max_length=20, required=True, label="Mobile number",
        help_text="M-PESA number this pledge will be paid from. "
                  "Filled automatically when you pick a member on the register.")
    visitor_name = forms.CharField(
        max_length=120, required=False, label="Visitor name",
        help_text="Required when the pledgor is not on the church register.")

    class Meta:
        model = Pledge
        fields = ["campaign", "member", "amount", "frequency",
                  "installment_amount", "start_date", "end_date", "note",
                  "reminders_opt_out"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "note": forms.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # only active campaigns and members are pickable
        self.fields["campaign"].queryset = PledgeCampaign.objects.exclude(
            status=PledgeCampaign.Status.CLOSED)
        from members.models import Member
        from django.db.models import Q
        member_qs = Member.objects.filter(active=True)
        if self.instance and self.instance.member_id:
            # Keep the current pledgor pickable even if they are a provisional
            # (inactive) visitor record created for an earlier pledge.
            member_qs = Member.objects.filter(
                Q(pk=self.instance.member_id) | Q(active=True)).distinct()
        self.fields["member"].queryset = member_qs
        self.fields["member"].required = False
        self.fields["member"].help_text = (
            "Leave blank for a visitor; enter their name below.")
        self.fields["installment_amount"].required = False
        self.fields["end_date"].required = False
        # Most pledges are paid monthly, and the ones that are not are the
        # exception a treasurer will notice and change. Optional as well as
        # defaulted: a promise of an amount is a pledge whether or not anyone
        # has decided how it will be paid, and refusing to record it until
        # somebody picks a schedule loses the pledge.
        self.fields["frequency"].required = False
        self.fields["frequency"].initial = Pledge.Frequency.MONTHLY
        if self.instance and self.instance.pk:
            self.fields["phone"].initial = (
                self.instance.pledged_phone
                or (self.instance.member.receipt_phone
                    if self.instance.member_id else "")
                or "")
            if self.instance.member_id and not self.instance.member.active:
                self.fields["visitor_name"].initial = self.instance.member.name

    def clean_frequency(self):
        return self.cleaned_data.get("frequency") or Pledge.Frequency.MONTHLY

    def clean_phone(self):
        from members.models import normalize_phone
        raw = (self.cleaned_data.get("phone") or "").strip()
        ph = normalize_phone(raw)
        if not ph:
            raise forms.ValidationError(
                "Enter a valid Kenyan mobile number (e.g. 07XXXXXXXX).")
        return ph

    def clean(self):
        cleaned = super().clean()
        member = cleaned.get("member")
        visitor = (cleaned.get("visitor_name") or "").strip()
        phone = cleaned.get("phone")
        if member:
            return cleaned
        if phone:
            from django.db.models import Q
            from members.models import Member
            # Phone is the trusted signal — reuse an existing record (active
            # or provisional) so we do not invent a duplicate for a visitor
            # who already pledged or gave from this line.
            hit = (Member.objects.filter(
                Q(phone=phone) | Q(phones__number=phone))
                .distinct().first())
            if hit:
                cleaned["member"] = hit
                return cleaned
        if not visitor:
            raise forms.ValidationError(
                "Select a member on the register, or enter the visitor's name.")
        cleaned["visitor_name"] = visitor
        return cleaned

    def save(self, commit=True):
        from members.models import Member
        p = super().save(commit=False)
        phone = self.cleaned_data["phone"]
        member = self.cleaned_data.get("member")
        visitor = (self.cleaned_data.get("visitor_name") or "").strip()
        if not member:
            member = Member.objects.create(
                name=visitor, phone=phone,
                source=Member.Source.AUTO_BANK, active=False)
        else:
            if not member.phone:
                member.phone = phone
                member.save(update_fields=["phone"])
            elif member.phone != phone:
                member.add_phone(phone)
        p.member = member
        display_name = member.name if member.active else (visitor or member.name)
        p.submitted_contact = f"{display_name} / {phone}"[:120]
        if commit:
            p.save()
            self.save_m2m()
        return p
