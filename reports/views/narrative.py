"""Endpoints behind the editable commentary on an engine report.

Two JSON endpoints, both scoped to one report, one section and one period:
``save`` stores (or clears) the treasurer's wording, ``ai`` asks the configured
assistant to draft it. Neither touches a figure — commentary is words about the
statements, never a substitute for them.
"""
import datetime as dt

from django.http import JsonResponse
from django.views import View

from core.permissions import TreasurerRequiredMixin


def _date(raw):
    try:
        return dt.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


class NarrativeSaveView(TreasurerRequiredMixin, View):
    """Store the treasurer's own commentary for one section and period.
    Posting empty text clears the override and restores the generated text."""

    def post(self, request):
        from reports.services import narratives
        key = request.POST.get("report_key", "").strip()
        section = request.POST.get("section_key", "").strip()
        if not key or not section:
            return JsonResponse({"ok": False,
                                 "error": "Missing report or section."},
                                status=400)
        start, end = _date(request.POST.get("start")), _date(request.POST.get("end"))
        text = request.POST.get("text", "")
        obj = narratives.save(key, section, start, end, text, request.user)
        return JsonResponse({
            "ok": True,
            "edited": obj is not None,
            "text": obj.text if obj else "",
            "source": obj.source if obj else "AUTO",
        })


class NarrativeAiView(TreasurerRequiredMixin, View):
    """Draft one section's commentary with the configured AI assistant.

    The report is re-rendered for the requested period so the model sees the
    same rows the reader does; nothing beyond that section reaches it.
    """

    def post(self, request):
        import copy
        from core.reporting import registry
        from reports.services import narratives
        key = request.POST.get("report_key", "").strip()
        section_key = request.POST.get("section_key", "").strip()
        report = registry.get(key)
        if report is None:
            return JsonResponse({"ok": False, "error": "Unknown report."},
                                status=404)
        # The engine reads its period and filters from the query string, but
        # this arrives as a POST — so the period and filters the reader is
        # actually looking at are handed over as GET on a copy of the request.
        # Without this the assistant would quietly write about the current
        # month whatever period was on screen.
        proxy = copy.copy(request)
        proxy.GET = request.POST
        try:
            rendered = report.render(proxy)
        except Exception as exc:  # noqa: BLE001
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        section = next((s for s in rendered.sections if s.key == section_key),
                       None)
        if section is None:
            return JsonResponse({"ok": False, "error": "Unknown section."},
                                status=404)
        text, err = narratives.ai_explain(section, rendered.context)
        if err:
            return JsonResponse({"ok": False, "error": err})
        narratives.save(key, section_key, rendered.context.start,
                        rendered.context.end, text, request.user, source="AI")
        return JsonResponse({"ok": True, "text": text, "source": "AI"})
