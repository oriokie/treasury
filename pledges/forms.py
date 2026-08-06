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
        self.fields["member"].queryset = Member.objects.filter(active=True)
        self.fields["installment_amount"].required = False
        self.fields["end_date"].required = False
        # Most pledges are paid monthly, and the ones that are not are the
        # exception a treasurer will notice and change. Optional as well as
        # defaulted: a promise of an amount is a pledge whether or not anyone
        # has decided how it will be paid, and refusing to record it until
        # somebody picks a schedule loses the pledge.
        self.fields["frequency"].required = False
        self.fields["frequency"].initial = Pledge.Frequency.MONTHLY

    def clean_frequency(self):
        return self.cleaned_data.get("frequency") or Pledge.Frequency.MONTHLY
