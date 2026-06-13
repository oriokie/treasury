from django import forms
from .models import PledgeCampaign, Pledge


class CampaignForm(forms.ModelForm):
    class Meta:
        model = PledgeCampaign
        fields = ["name", "description", "target_department", "goal_amount",
                  "start_date", "end_date", "status"]
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
