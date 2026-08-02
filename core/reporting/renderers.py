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

import datetime as _dt
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
            # The section's commentary — generated, AI-drafted or the
            # treasurer's own. A printed pack the board takes away must carry
            # the same explanation the screen showed, so it travels with every
            # export rather than living only in HTML. Commentary sections have
            # already printed their text above.
            if s.note:
                story.append(Paragraph(f"<i>{s.note}</i>", styles["Italic"]))
            explanation = (s.extra.get("explanation") or "").strip()
            if explanation and explanation != s.note \
                    and s.kind not in ("commentary", "info"):
                for para in explanation.split("\n\n"):
                    if para.strip():
                        story.append(Paragraph(
                            para.strip().replace("\n", "<br/>"), styles["Normal"]))
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
    """Word export as a real .docx, written by ``core.reporting.wordml``.

    This replaced an HTML file wearing a .doc extension. That opened in Word,
    but in web view — no pages, no margins, no header — and Google Docs,
    phones and LibreOffice each mangled it differently. The .docx is the same
    document everywhere: A4 with the pack's own margins, a running header, a
    "Page X of Y" footer Word computes itself, table headings that repeat
    across page breaks, and rows that never split over one. Charts embed as
    real pictures. No new dependency: the format is a zip of small XML parts,
    and the writer emits them directly.
    """
    fmt = "docx"
    label = "Word"

    def render(self, rendered, *, church=None, request=None):
        from django.http import HttpResponse
        from core.reporting.wordml import WordDoc

        brand = resolve_branding(church)
        try:
            from core.models import SiteConfig
            currency = SiteConfig.get().currency_symbol or "KES"
        except Exception:  # noqa: BLE001 — the export must not need the DB row
            currency = "KES"

        period = ""
        if rendered.context.start and rendered.context.end:
            period = (f"{rendered.context.start:%d %B %Y} to "
                      f"{rendered.context.end:%d %B %Y}")
        doc = WordDoc(title=rendered.report.title,
                      church=brand.get("church_name") or "",
                      period=period,
                      primary=brand.get("primary_colour") or "#1F5F4F")

        meta = [f"Prepared {_dt.date.today():%d %B %Y}"]
        if brand.get("conference") or brand.get("region"):
            meta.append(" · ".join(x for x in (brand.get("conference"),
                                               brand.get("region")) if x))
        health_line = _cover_health_line(rendered)
        if health_line:
            meta.append(health_line)
        basis = ""
        if getattr(rendered.context, "as_reported_at", None):
            basis = (f"Position as it stood on "
                     f"{rendered.context.as_reported_at:%d %B %Y} — entries "
                     "made or receipted after that date are excluded.")
        doc.masthead(org=brand.get("church_name") or "",
                     meta=" · ".join(meta), basis=basis,
                     header_text=brand.get("header_text") or "")

        def money(v, places=2):
            if v is None or v == "":
                return ""
            if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                return f"{v:,.{places}f}"
            return str(v)

        current_group = None
        group_no = 0
        first_group = True
        for s in rendered.sections:
            if not _visible_in(s, "docx"):
                continue
            grp = _section_group(s)
            if grp and grp != current_group:
                current_group = grp
                group_no += 1
                if not first_group and _section_breaks(s):
                    doc.page_break()
                first_group = False
                doc.group_heading(group_no, grp)

            if s.kind == "heading":
                doc.text(s.extra.get("text", ""), size=26, bold=True,
                         font="Georgia", keep_next=True,
                         space_before=200, space_after=100)
                continue

            if s.kind == "kpi":
                cards = []
                for r in s.rows:
                    disp = r.cells.get("display")
                    cards.append((str(r.cells.get("label", "")),
                                  disp if disp else money(r.cells.get("value"),
                                                          0)))
                doc.kpi_band(cards, currency=currency)
            elif s.kind in ("commentary", "info"):
                doc.section_title(s.title)
                doc.prose(s.extra.get("text", ""))
            elif s.kind == "signature":
                doc.section_title(s.title)
                doc.signatures([str(r.cells.get("role", "")) for r in s.rows])
            elif s.kind == "chart":
                doc.section_title(s.title)
                from reports.services.chart_image import render_chart_config
                _uri, png = render_chart_config(s.extra.get("chart"), s.title)
                if png:
                    doc.image(png)
                else:
                    doc.caption("[chart available on screen]")
            elif s.kind == "keyvalue":
                doc.section_title(s.title)
                pairs = []
                for r in s.rows:
                    level = (r.meta or {}).get("level") or (
                        "subtotal" if r.emphasis else "")
                    v = r.cells.get("value")
                    pairs.append((str(r.cells.get("label", "")),
                                  "" if level == "heading" else money(v),
                                  level))
                doc.keyvalue(pairs)
            elif getattr(s, "is_empty", False):
                doc.section_title(s.title)
                doc.caption("Nothing to report for this period.")
            else:
                doc.section_title(s.title)
                columns = [{"label": c.label, "numeric": c.numeric}
                           for c in s.columns]
                places = {c.label: getattr(c, "places", 2) for c in s.columns}
                rows = []
                for r in s.rows:
                    level = (r.meta or {}).get("level") or ""
                    cells = []
                    for c in s.columns:
                        v = r.cells.get(c.key)
                        cells.append(money(v, getattr(c, "places", 2))
                                     if c.numeric else
                                     ("" if v is None else str(v)))
                    rows.append({"cells": cells, "level": level,
                                 "emphasis": r.emphasis})
                total = None
                if s.total is not None:
                    total = [money(s.total.cells.get(c.key),
                                   getattr(c, "places", 2))
                             if c.numeric else
                             str(s.total.cells.get(c.key) or "")
                             for c in s.columns]
                doc.table(columns, rows, total=total)

            # the method caption and the commentary travel with the document,
            # exactly as on screen and in print
            if s.note and s.kind not in ("commentary", "info"):
                doc.caption(s.note)
            explanation = (s.extra.get("explanation") or "").strip()
            if explanation and explanation != s.note \
                    and s.kind not in ("commentary", "info"):
                doc.explanation(explanation)

        if brand.get("certification_statement"):
            doc.prose(brand["certification_statement"], small=True)
        if brand.get("footer_text"):
            doc.text(brand["footer_text"], size=15, color="677770",
                     space_before=200)

        resp = HttpResponse(
            doc.to_bytes(),
            content_type="application/vnd.openxmlformats-officedocument"
                         ".wordprocessingml.document")
        resp["Content-Disposition"] = \
            f'attachment; filename="{rendered.report.key}.docx"'
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
