"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from core.utils import parse_period, safe_json


class PeriodMixin(ReportAccessMixin):
    def period(self):
        return parse_period(self.request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["start"], ctx["end"] = self.period()
        ctx["filters"] = self.request.GET
        return ctx
