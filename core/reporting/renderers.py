"""Rendering framework — renderer interfaces + a Renderer Registry.

Report components produce structured ``SectionData``; they never know how they
are rendered. A *renderer* takes a ``RenderedReport`` (the report's sections
plus context) and turns it into a specific medium: HTML context, CSV, Excel,
PDF, Word, or Print. New formats are added by registering a renderer, not by
changing components or the engine.

The renderers reuse the existing export helpers (``reports.exports``) so the new
framework produces output consistent with the current exports, and existing
exports keep working untouched alongside it.

Renderer contract
-----------------
A renderer implements ``render(rendered, *, church=None, request=None)`` and
declares its ``fmt`` (e.g. "csv") and ``content`` medium. Renderers honour each
component's ``LayoutMeta.visible_in(fmt)`` so per-medium visibility (e.g. an
info panel hidden from exports) is respected uniformly.
"""
from __future__ import annotations

from decimal import Decimal


def _visible_in(section, fmt):
    """Respect a component's layout export/print visibility. Sections without
    layout metadata (legacy) are always visible."""
    layout = section.extra.get("layout")
    if not layout:
        return True
    from core.reporting.layout import LayoutMeta
    return LayoutMeta.from_dict(layout).visible_in(fmt)


def resolve_branding(church=None):
    """Resolve the active ReportBranding into a plain dict the renderers stamp
    onto output (church name, header, footer, certification, watermark, page
    numbering, colours). Falls back to SiteConfig's church name when no branding
    is configured, so behaviour is unchanged until an admin sets branding up.

    This is the single place branding is read, so every renderer applies the
    same organisation identity consistently."""
    data = {"church_name": church or "", "header_text": "", "footer_text": "",
            "certification_statement": "", "watermark_text": "",
            "show_page_numbers": True, "primary_colour": "#1f5f4f",
            "logo_url": "", "conference": "", "region": ""}
    try:
        from reports.models import ReportBranding
        b = ReportBranding.active()
        if b:
            data.update({k: v for k, v in b.as_dict().items() if v not in (None,)})
            if not data.get("church_name"):
                data["church_name"] = church or b.church_name
    except Exception:  # noqa: BLE001 — branding is optional
        pass
    if church and not data.get("church_name"):
        data["church_name"] = church
    return data


def _flatten(section):
    """A table/keyvalue/kpi/signature section -> (title, header, rows) for
    tabular exports. Non-tabular kinds (chart/commentary/info) return None."""
    if section.kind in ("chart", "commentary", "info", "heading"):
        return None
    header = [c.label for c in section.columns] if section.columns else ["", ""]
    rows = []
    if section.columns:
        for r in section.rows:
            rows.append([r.cells.get(c.key, "") for c in section.columns])
        if section.total is not None:
            rows.append([section.total.cells.get(c.key, "")
                         for c in section.columns])
    else:
        # keyvalue-style without declared columns
        for r in section.rows:
            rows.append([r.cells.get("label", ""), r.cells.get("value", "")])
    return section.title, header, rows


def _section_group(section):
    """The section's layout group name (or '' if none)."""
    layout = section.extra.get("layout")
    if not layout:
        return ""
    return layout.get("group", "") or ""


def _section_breaks(section):
    layout = section.extra.get("layout")
    return bool(layout and layout.get("page_break_before"))


def _cover_health_line(rendered):
    """A one-line financial-health summary for a board-pack export cover, or ''
    when unavailable. Only computed for reports that opt into a custom template
    (the board pack), so ordinary engine exports pay nothing for it."""
    if not getattr(rendered.report, "html_template", None):
        return ""
    try:
        from core.intelligence import compute_health_score
        hs = compute_health_score(rendered.context)
        return f"Financial health score: {hs.overall:.0f}/100 ({hs.band})"
    except Exception:  # noqa: BLE001
        return ""


class Renderer:
    """Base renderer. Subclasses set ``fmt`` and implement ``render``."""
    fmt = ""
    label = ""

    def render(self, rendered, *, church=None, request=None):  # pragma: no cover
        raise NotImplementedError

    # shared helper: tabular blocks honouring per-medium visibility
    def blocks(self, rendered):
        out = []
        for s in rendered.sections:
            if not _visible_in(s, self.fmt):
                continue
            flat = _flatten(s)
            if flat:
                out.append(flat)
        return out


class HtmlRenderer(Renderer):
    """Produces a template context; the actual HTML is the engine_report
    template. Returned as a dict so the view can render_to_response."""
    fmt = "html"
    label = "HTML"

    def render(self, rendered, *, church=None, request=None):
        return {
            "report": rendered.report,
            "rendered": rendered,
            "sections": rendered.sections,
            "filters": rendered.filters,
        }


class CsvRenderer(Renderer):
    fmt = "csv"
    label = "CSV"

    def render(self, rendered, *, church=None, request=None):
        from reports.exports import csv_response
        blocks = self.blocks(rendered)
        width = max((len(h) for _, h, _ in blocks), default=2)
        combined = []
        for title, hdr, rows in blocks:
            combined.append([title] + [""] * (width - 1))
            combined.append((hdr + [""] * width)[:width])
            for r in rows:
                combined.append((list(r) + [""] * width)[:width])
            combined.append([""] * width)
        return csv_response(f"{rendered.report.key}.csv", [""] * width, combined)


class XlsxRenderer(Renderer):
    fmt = "xlsx"
    label = "Excel"

    def render(self, rendered, *, church=None, request=None):
        from reports.exports import xlsx_response
        blocks = self.blocks(rendered)
        width = max((len(h) for _, h, _ in blocks), default=2)
        combined = []
        for title, hdr, rows in blocks:
            combined.append([title] + [""] * (width - 1))
            combined.append((hdr + [""] * width)[:width])
            for r in rows:
                combined.append((list(r) + [""] * width)[:width])
            combined.append([""] * width)
        return xlsx_response(f"{rendered.report.key}.xlsx", [""] * width,
                             combined, title=rendered.report.title, church=church)


class PrintRenderer(HtmlRenderer):
    """Print is HTML with print-visibility applied and a print flag the template
    uses to switch styling. Components with ``print_visible=False`` drop out."""
    fmt = "print"
    label = "Print"

    def render(self, rendered, *, church=None, request=None):
        ctx = super().render(rendered, church=church, request=request)
        ctx["sections"] = [s for s in rendered.sections if _visible_in(s, "print")]
        ctx["print_mode"] = True
        return ctx


class PdfRenderer(Renderer):
    """PDF via ReportLab, honouring export visibility. Renders each tabular
    section as a heading + table; non-tabular commentary/info as paragraphs.
    Kept intentionally simple — a faithful, dependency-light export, not a
    pixel-perfect redesign."""
    fmt = "pdf"
    label = "PDF"

    def render(self, rendered, *, church=None, request=None):
        import io
        from django.http import HttpResponse
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer, HRFlowable,
                                        PageBreak)
        buf = io.BytesIO()
        brand = resolve_branding(church)
        primary = colors.HexColor(brand.get("primary_colour") or "#1f5f4f")

        def _footer(canvas, doc):
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.setFillColor(colors.HexColor("#666666"))
            title = rendered.report.title
            canvas.drawString(18 * mm, 12 * mm, title)
            canvas.drawRightString(
                A4[0] - 18 * mm, 12 * mm, f"Page {doc.page}")
            if brand.get("church_name"):
                canvas.drawCentredString(A4[0] / 2, 12 * mm,
                                         brand["church_name"])
            canvas.restoreState()

        doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm,
                                bottomMargin=20 * mm)
        styles = getSampleStyleSheet()
        h_group = ParagraphStyle(
            "GroupHead", parent=styles["Heading1"], textColor=primary,
            fontSize=15, spaceBefore=4, spaceAfter=6)
        story = []
        # ---- cover ----
        if brand.get("header_text"):
            story.append(Paragraph(brand["header_text"], styles["Normal"]))
        if brand.get("church_name"):
            story.append(Paragraph(f"<b>{brand['church_name']}</b>",
                                   styles["Title"]))
        if brand.get("conference") or brand.get("region"):
            story.append(Paragraph(
                " · ".join(x for x in (brand.get("conference"),
                                       brand.get("region")) if x),
                styles["Normal"]))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(rendered.report.title, styles["Heading1"]))
        if rendered.context.start and rendered.context.end:
            story.append(Paragraph(
                f"For the period {rendered.context.start:%d %B %Y} – "
                f"{rendered.context.end:%d %B %Y}", styles["Normal"]))
        health_line = _cover_health_line(rendered)
        if health_line:
            story.append(Spacer(1, 1 * mm))
            story.append(Paragraph(f"<b>{health_line}</b>", styles["Normal"]))
        story.append(Spacer(1, 2 * mm))
        story.append(HRFlowable(width="100%", thickness=1.2, color=primary,
                                spaceAfter=6))
        # ---- grouped sections ----
        current_group = None
        for s in rendered.sections:
            if not _visible_in(s, "pdf"):
                continue
            grp = _section_group(s)
            if grp and grp != current_group:
                if current_group is not None and _section_breaks(s):
                    story.append(PageBreak())
                current_group = grp
                story.append(Paragraph(grp, h_group))
                story.append(HRFlowable(width="100%", thickness=0.6,
                                        color=colors.HexColor("#c79241"),
                                        spaceAfter=6))
            if s.kind == "heading":
                story.append(Paragraph(s.extra.get("text", ""), styles["Heading1"]))
                continue
            story.append(Paragraph(s.title, styles["Heading2"]))
            if s.kind in ("commentary", "info"):
                for para in (s.extra.get("text", "") or "").split("\n\n"):
                    if para.strip():
                        story.append(Paragraph(para.strip().replace("\n", "<br/>"),
                                               styles["Normal"]))
            elif s.kind == "chart":
                # server-side PNG of the same registry-sourced figures the
                # on-screen Chart.js draws (recommendation #28)
                from reports.services.chart_image import render_chart_config
                _, png = render_chart_config(s.extra.get("chart"), s.title)
                if png:
                    import io as _io
                    from reportlab.platypus import Image as RLImage
                    from PIL import Image as PILImage
                    w_px, h_px = PILImage.open(_io.BytesIO(png)).size
                    disp_w = min(170 * mm, A4[0] - 36 * mm)
                    story.append(RLImage(_io.BytesIO(png), width=disp_w,
                                         height=disp_w * h_px / w_px))
                else:
                    story.append(Paragraph("<i>[chart available on screen]</i>",
                                           styles["Italic"]))
            else:
                flat = _flatten(s)
                if flat:
                    _, header, rows = flat
                    data = [header] + [[_fmt_cell(c) for c in r] for r in rows]
                    t = Table(data, hAlign="LEFT")
                    t.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, 0), primary),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                         [colors.white, colors.HexColor("#f4f1ea")]),
                    ]))
                    story.append(t)
            if s.note:
                story.append(Paragraph(f"<i>{s.note}</i>", styles["Italic"]))
            story.append(Spacer(1, 5 * mm))
        if brand.get("certification_statement"):
            story.append(Spacer(1, 6 * mm))
            story.append(Paragraph(f"<i>{brand['certification_statement']}</i>",
                                   styles["Normal"]))
        if brand.get("footer_text"):
            story.append(Spacer(1, 4 * mm))
            story.append(Paragraph(brand["footer_text"], styles["Normal"]))
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
        resp["Content-Disposition"] = \
            f'attachment; filename="{rendered.report.key}.pdf"'
        return resp


class DocxRenderer(Renderer):
    """Word export as a Word-compatible HTML document (opens natively in
    Microsoft Word), matching the app's existing Word-export approach so no
    extra server dependency is needed. Honours export visibility. Charts are
    noted but not embedded (a future enhancement could render them server-side
    with Pillow, as the Monthly report does)."""
    fmt = "docx"
    label = "Word"

    def render(self, rendered, *, church=None, request=None):
        from django.http import HttpResponse
        from django.utils.html import escape
        brand = resolve_branding(church)
        _pc = escape(brand.get("primary_colour") or "#1f5f4f")
        parts = ["<html xmlns:o='urn:schemas-microsoft-com:office:office' "
                 "xmlns:w='urn:schemas-microsoft-com:office:word'>",
                 "<head><meta charset='utf-8'>"
                 "<style>body{font-family:Calibri,Arial,sans-serif;}"
                 "table{border-collapse:collapse;width:100%;margin:6px 0 14px;}"
                 "th,td{border:1px solid #999;padding:4px 8px;font-size:10pt;}"
                 f"th{{background:{_pc};color:#fff;text-align:left;}}"
                 f"td.num{{text-align:right;}} h1{{color:{_pc};}} h2{{color:{_pc};}}"
                 ".tot td{font-weight:bold;background:#f4f1ea;}</style></head><body>"]
        if brand.get("header_text"):
            parts.append(f"<p style='font-size:9pt'>{escape(brand['header_text'])}</p>")
        if brand.get("church_name"):
            parts.append(f"<p style='font-weight:bold;font-size:14pt'>"
                         f"{escape(brand['church_name'])}</p>")
        if brand.get("conference") or brand.get("region"):
            parts.append("<p style='font-size:10pt'>" + escape(
                " · ".join(x for x in (brand.get("conference"),
                                       brand.get("region")) if x)) + "</p>")
        parts.append(f"<h1>{escape(rendered.report.title)}</h1>")
        if rendered.context.start and rendered.context.end:
            parts.append(f"<p>For the period {rendered.context.start:%d %B %Y} – "
                         f"{rendered.context.end:%d %B %Y}</p>")
        health_line = _cover_health_line(rendered)
        if health_line:
            parts.append(f"<p style='font-weight:bold'>{escape(health_line)}</p>")
        parts.append(f"<hr style='border:0;border-top:2px solid {_pc}'>")
        current_group = None
        for s in rendered.sections:
            if not _visible_in(s, "docx"):
                continue
            grp = _section_group(s)
            if grp and grp != current_group:
                current_group = grp
                parts.append("<br style='page-break-before:always'>"
                             if _section_breaks(s) else "")
                parts.append(f"<h1 style='border-bottom:1px solid {_pc};"
                             f"padding-bottom:3px'>{escape(grp)}</h1>")
            if s.kind == "heading":
                parts.append(f"<h1>{escape(s.extra.get('text', ''))}</h1>")
                continue
            parts.append(f"<h2>{escape(s.title)}</h2>")
            if s.kind in ("commentary", "info"):
                for para in (s.extra.get("text", "") or "").split("\n\n"):
                    if para.strip():
                        parts.append(
                            "<p>" + escape(para.strip()).replace("\n", "<br>") + "</p>")
            elif s.kind == "chart":
                # server-side PNG (recommendation #28) — Word can't run
                # Chart.js; the Monthly report already embeds images this way
                from reports.services.chart_image import render_chart_config
                uri, _png = render_chart_config(s.extra.get("chart"), s.title)
                if uri:
                    parts.append(f"<p><img src='{uri}' width='620'></p>")
                else:
                    parts.append("<p><i>[chart available on screen]</i></p>")
            else:
                flat = _flatten(s)
                if flat:
                    _, header, rows = flat
                    parts.append("<table><tr>"
                                 + "".join(f"<th>{escape(str(h))}</th>" for h in header)
                                 + "</tr>")
                    for r in rows:
                        parts.append("<tr>" + "".join(
                            f"<td class='num'>{_fmt_cell(c)}</td>"
                            if isinstance(c, (int, float, Decimal)) and not isinstance(c, bool)
                            else f"<td>{escape(_fmt_cell(c))}</td>" for c in r)
                            + "</tr>")
                    parts.append("</table>")
            if s.note:
                parts.append(f"<p style='font-size:9pt;color:#555;font-style:italic'>"
                             f"{escape(s.note)}</p>")
        if brand.get("certification_statement"):
            parts.append(f"<p style='margin-top:14pt'><i>"
                         f"{escape(brand['certification_statement'])}</i></p>")
        if brand.get("footer_text"):
            parts.append(f"<p style='font-size:9pt;color:#555'>"
                         f"{escape(brand['footer_text'])}</p>")
        parts.append("</body></html>")
        resp = HttpResponse("".join(parts), content_type="application/msword")
        resp["Content-Disposition"] = \
            f'attachment; filename="{rendered.report.key}.doc"'
        return resp


def _fmt_cell(v):
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return f"{v:,.2f}"
    return str(v) if v is not None else ""


# ===========================================================================
# Renderer Registry
# ===========================================================================

class RendererRegistry:
    def __init__(self):
        self._renderers: dict[str, Renderer] = {}

    def register(self, renderer: Renderer):
        if renderer.fmt in self._renderers:
            raise ValueError(f"Renderer '{renderer.fmt}' already registered.")
        self._renderers[renderer.fmt] = renderer
        return renderer

    def get(self, fmt):
        return self._renderers.get(fmt)

    def formats(self):
        return sorted(self._renderers)

    def all(self):
        return [self._renderers[f] for f in self.formats()]


renderer_registry = RendererRegistry()
renderer_registry.register(HtmlRenderer())
renderer_registry.register(CsvRenderer())
renderer_registry.register(XlsxRenderer())
renderer_registry.register(PrintRenderer())
renderer_registry.register(PdfRenderer())
renderer_registry.register(DocxRenderer())
