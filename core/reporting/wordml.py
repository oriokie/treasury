"""A real .docx writer, dependency-free.

The Word export used to be an HTML file wearing a ``.doc`` extension. Word
tolerates that, but only just: the document opens in web view with no pages,
no margins, no headers — and Google Docs, phones and LibreOffice each mangle
it their own way. A treasurer mailing the board pack to twelve people cannot
know what any of them will see.

A ``.docx`` is a zip of small XML parts, and this project's deployment target
(cPanel, pure-Python wheels only) rules out python-docx's lxml dependency — so
the parts are written directly. That is less code than it sounds: the format's
surface is enormous, but a financial report needs a dozen constructs, and each
is a fixed pattern. What hand-writing buys, besides the dependency-free deploy:

* **Real pagination** — A4, the same 20/14/16 mm margins as the print CSS, a
  running header and a "Page X of Y" footer that Word computes itself.
* **Print behaviour matching the printed pack** — table header rows repeat
  across page breaks, no row splits mid-page, headings keep with what follows.
* **A document, not a webpage** — opens in print layout everywhere, edits like
  anything else the board secretary works with.

Sizing note: everything here is in twips (1/20 pt; 567 to the cm) except image
extents, which OOXML wants in EMUs (914,400 to the inch). The constants are
named so the arithmetic reads.
"""
from __future__ import annotations

import datetime as _dt
import struct
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape as _esc

# ---- page geometry (A4 portrait, matching the board pack's @page) ----------
PAGE_W, PAGE_H = 11906, 16838            # A4 in twips
MARGIN_TOP, MARGIN_BOTTOM = 1134, 907    # 20 mm / 16 mm
MARGIN_SIDE = 794                        # 14 mm
CONTENT_W = PAGE_W - 2 * MARGIN_SIDE     # 10318 twips of usable width
EMU_PER_PX = 9525                        # 96 dpi pixel, in EMUs
TWIP_EMU = 635                           # one twip, in EMUs

# ---- the pack's palette, hex without '#' ------------------------------------
INK = "1B2420"
INK_SOFT = "3F4F48"
MUTED = "677770"
HAIR = "E6E0D2"
RULE = "D3CBB6"
TINT = "F0F6F2"

BODY_FONT = "Calibri"
DISPLAY_FONT = "Georgia"
MONO_FONT = "Consolas"


def _t(text):
    """A text run's content, whitespace preserved (Word otherwise eats leading
    and trailing spaces, which matters for indented statement labels)."""
    return f'<w:t xml:space="preserve">{_esc(str(text))}</w:t>'


def _rpr(*, bold=False, italic=False, size=None, color=None, font=None,
         caps=False, spacing=None):
    """Run properties. ``size`` is in half-points, as the format demands."""
    bits = []
    if font:
        bits.append(f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}"/>')
    if bold:
        bits.append("<w:b/>")
    if italic:
        bits.append("<w:i/>")
    if caps:
        bits.append("<w:caps/>")
    if spacing:
        bits.append(f'<w:spacing w:val="{spacing}"/>')
    if color:
        bits.append(f'<w:color w:val="{color}"/>')
    if size:
        bits.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    return f"<w:rPr>{''.join(bits)}</w:rPr>" if bits else ""


def run(text, **kw):
    return f"<w:r>{_rpr(**kw)}{_t(text)}</w:r>"


def _border(edge, *, sz=4, color=HAIR, style="single"):
    return f'<w:{edge} w:val="{style}" w:sz="{sz}" w:space="0" w:color="{color}"/>'


class WordDoc:
    """Collects block-level XML and zips a complete, valid package.

    High-level methods mirror the sections a rendered report is made of, so
    the renderer walks its sections and calls one method per kind — the same
    shape as the HTML template, which is what keeps the two media in step.
    """

    def __init__(self, *, title, church="", period="", primary="1F5F4F",
                 brass="B07D2C"):
        self.title = title
        self.church = church
        self.period = period
        self.primary = primary.lstrip("#").upper()
        self.brass = brass.lstrip("#").upper()
        self._body: list[str] = []
        self._images: list[bytes] = []      # PNG payloads, rId offset by index

    # ------------------------------------------------------------------ text
    def para(self, runs_xml, *, align=None, space_before=0, space_after=120,
             keep_next=False, borders=None, shading=None, indent=None):
        """One paragraph. ``runs_xml`` is a string of <w:r> elements (or "" for
        an empty line); ``borders`` maps edge -> border XML from _border()."""
        ppr = []
        if keep_next:
            ppr.append("<w:keepNext/>")
        if borders:
            ppr.append("<w:pBdr>" + "".join(borders) + "</w:pBdr>")
        if shading:
            ppr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shading}"/>')
        ppr.append(f'<w:spacing w:before="{space_before}" w:after="{space_after}"/>')
        if indent:
            ppr.append(f'<w:ind w:left="{indent}"/>')
        if align:
            ppr.append(f'<w:jc w:val="{align}"/>')
        self._body.append(f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{runs_xml}</w:p>")

    def text(self, text, **kw):
        """Convenience: a paragraph of one plain run."""
        para_kw = {k: kw.pop(k) for k in ("align", "space_before", "space_after",
                                          "keep_next", "borders", "shading",
                                          "indent") if k in kw}
        self.para(run(text, **kw), **para_kw)

    def page_break(self):
        self._body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    # ------------------------------------------------------------- masthead
    def masthead(self, *, org, meta="", basis="", header_text=""):
        if header_text:
            self.text(header_text, size=16, color=MUTED, space_after=60)
        if org:
            self.text(org, size=15, bold=True, caps=True, color=MUTED,
                      spacing=30, space_after=40)
        self.text(self.title, size=44, font=DISPLAY_FONT, color=INK,
                  space_after=40, keep_next=True)
        if self.period:
            self.text(f"For the period {self.period}", size=21,
                      color=INK_SOFT, space_after=40)
        if meta:
            self.text(meta, size=15, color=MUTED, space_after=80)
        if basis:
            # the as-reported banner: printed, bordered, unmissable — two packs
            # for one date with different figures must read as two questions
            self.text(basis, size=16, color=INK_SOFT, space_after=80,
                      shading="F5EAD2",
                      borders=[_border("left", sz=16, color=self.brass)],
                      indent=113)
        self.para("", borders=[_border("bottom", sz=12, color=self.primary)],
                  space_after=200)

    def group_heading(self, number, name):
        self.para(
            run(f"{number}  ", size=24, font=DISPLAY_FONT, color=self.brass,
                bold=True)
            + run(name, size=26, font=DISPLAY_FONT, color=INK, bold=True),
            keep_next=True, space_before=260, space_after=120,
            borders=[_border("bottom", sz=6, color=RULE)])

    def section_title(self, text):
        self.text(text, size=17, bold=True, caps=True, color=INK_SOFT,
                  spacing=16, keep_next=True, space_before=140, space_after=60)

    def prose(self, text, *, small=False):
        for chunk in str(text).split("\n\n"):
            if chunk.strip():
                self.text(chunk.strip(), size=16 if small else 19,
                          color=INK_SOFT, space_after=100)

    def caption(self, text):
        """The method note under a table — how the figures were arrived at."""
        self.text(text, size=15, italic=True, color=MUTED,
                  space_before=40, space_after=60)

    def explanation(self, text):
        """The treasurer's (or generated) commentary, set off by a rule as on
        screen and on paper."""
        for chunk in str(text).split("\n\n"):
            if chunk.strip():
                self.text(chunk.strip(), size=17, color=INK_SOFT,
                          borders=[_border("left", sz=8, color=RULE)],
                          indent=170, space_after=80)

    # ----------------------------------------------------------------- KPIs
    def kpi_band(self, cards, currency=""):
        """The headline figures as a borderless row of cells, hairline-ruled
        top and bottom like the screen's band. ``cards`` is [(label, value)]."""
        if not cards:
            return
        w = CONTENT_W // len(cards)
        tcs = []
        for label, value in cards:
            cell_ps = (
                f"<w:p><w:pPr><w:spacing w:before=\"60\" w:after=\"20\"/></w:pPr>"
                f"{run(label, size=13, bold=True, caps=True, color=MUTED, spacing=16)}</w:p>"
                f"<w:p><w:pPr><w:spacing w:before=\"0\" w:after=\"60\"/></w:pPr>"
                f"{run((currency + ' ') if currency else '', size=15, color=MUTED)}"
                f"{run(value, size=28, bold=True, font=DISPLAY_FONT, color=INK)}</w:p>")
            tcs.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>'
                f"<w:tcBorders>{_border('top', sz=4, color=HAIR)}"
                f"{_border('bottom', sz=4, color=HAIR)}</w:tcBorders>"
                f"</w:tcPr>{cell_ps}</w:tc>")
        self._body.append(
            f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_W}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/></w:tblPr>'
            "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for _ in cards)
            + f"</w:tblGrid><w:tr><w:trPr><w:cantSplit/></w:trPr>{''.join(tcs)}</w:tr></w:tbl>")
        self.para("", space_after=60)

    # --------------------------------------------------------------- tables
    def _cell(self, xml_runs, width, *, align=None, borders="", shading=None,
              space=(30, 30)):
        shd = (f'<w:shd w:val="clear" w:color="auto" w:fill="{shading}"/>'
               if shading else "")
        jc = f'<w:jc w:val="{align}"/>' if align else ""
        return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>'
                f"{('<w:tcBorders>' + borders + '</w:tcBorders>') if borders else ''}{shd}"
                f"</w:tcPr><w:p><w:pPr>{jc}"
                f'<w:spacing w:before="{space[0]}" w:after="{space[1]}"/></w:pPr>'
                f"{xml_runs}</w:p></w:tc>")

    def _grid(self, columns):
        """Column widths: numeric columns take a fixed money width, the label
        column takes the rest — the same proportioning the screen reaches by
        letting text shrink-wrap."""
        n_num = sum(1 for c in columns if c["numeric"])
        n_lbl = max(len(columns) - n_num, 1)
        money = 1500 if len(columns) <= 6 else 1150
        label = max((CONTENT_W - money * n_num) // n_lbl, 1400)
        return [money if c["numeric"] else label for c in columns]

    def table(self, columns, rows, total=None):
        """A report table.

        ``columns``: [{label, numeric}]; ``rows``: [{cells: [str], level,
        emphasis, numeric_blank}] where level is '', 'heading', 'subtotal' or
        'grand'. The first row repeats as a header on every printed page and no
        row splits across one — the two things that make a forty-row fund
        statement readable on paper, and the two things the old HTML export
        could not promise.
        """
        widths = self._grid(columns)
        head = "".join(
            self._cell(run(c["label"], size=13, bold=True, caps=True,
                           color=MUTED, spacing=12),
                       widths[i], align="right" if c["numeric"] else None,
                       borders=_border("bottom", sz=8, color=INK))
            for i, c in enumerate(columns))
        body_rows = []
        for r in rows:
            level = r.get("level") or ""
            emphasis = bool(r.get("emphasis")) or level in ("subtotal", "grand")
            shading = TINT if level == "grand" else None
            cells = []
            for i, c in enumerate(columns):
                v = r["cells"][i]
                is_num = c["numeric"]
                if level == "heading":
                    cells.append(self._cell(
                        run(v if not is_num else "", size=13, bold=True,
                            caps=True, color=INK_SOFT, spacing=12),
                        widths[i], borders=_border("bottom", sz=0,
                                                   style="nil"),
                        space=(90, 20)))
                    continue
                borders = _border("bottom", sz=2, color=HAIR)
                if level == "subtotal":
                    borders = (_border("top", sz=4, color=INK)
                               + _border("bottom", sz=0, style="nil"))
                elif level == "grand":
                    borders = (_border("top", sz=8, color=self.primary)
                               + _border("bottom", sz=8, color=self.primary,
                                         style="double"))
                neg = is_num and str(v).strip().startswith(("-", "("))
                cells.append(self._cell(
                    run(v, size=16 if is_num else 17,
                        font=MONO_FONT if is_num else None,
                        bold=emphasis,
                        color="A23B32" if neg else (INK if emphasis else INK_SOFT)),
                    widths[i], align="right" if is_num else None,
                    borders=borders, shading=shading))
            body_rows.append("<w:tr><w:trPr><w:cantSplit/></w:trPr>"
                             + "".join(cells) + "</w:tr>")
        foot = ""
        if total:
            cells = []
            for i, c in enumerate(columns):
                v = total[i]
                cells.append(self._cell(
                    run(v, size=16 if c["numeric"] else 17, bold=True,
                        font=MONO_FONT if c["numeric"] else None, color=INK),
                    widths[i], align="right" if c["numeric"] else None,
                    borders=_border("top", sz=8, color=self.primary)
                    + _border("bottom", sz=8, color=self.primary,
                              style="double"),
                    shading=TINT, space=(50, 50)))
            foot = ("<w:tr><w:trPr><w:cantSplit/></w:trPr>"
                    + "".join(cells) + "</w:tr>")
        self._body.append(
            f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/></w:tblPr>'
            "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
            + "</w:tblGrid>"
            + f'<w:tr><w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>{head}</w:tr>'
            + "".join(body_rows) + foot + "</w:tbl>")
        self.para("", space_after=60)

    def keyvalue(self, pairs):
        """A label/figure statement. Rendered as a table capped at roughly the
        screen's narrow width, for the same reason the screen caps it: a label
        a hand-span from its figure is a row the eye loses."""
        width = min(CONTENT_W, 6200)
        cols = [{"label": "", "numeric": False}, {"label": "", "numeric": True}]
        widths = [width - 1800, 1800]
        body_rows = []
        for label, value, level in pairs:
            emphasis = level in ("subtotal", "grand")
            if level == "heading":
                body_rows.append(
                    "<w:tr><w:trPr><w:cantSplit/></w:trPr>"
                    + self._cell(run(label, size=13, bold=True, caps=True,
                                     color=INK_SOFT, spacing=12), widths[0],
                                 borders=_border("bottom", sz=0, style="nil"),
                                 space=(90, 20))
                    + self._cell("", widths[1],
                                 borders=_border("bottom", sz=0, style="nil"))
                    + "</w:tr>")
                continue
            borders = _border("bottom", sz=2, color=HAIR)
            shading = None
            if level == "subtotal":
                borders = (_border("top", sz=4, color=INK)
                           + _border("bottom", sz=0, style="nil"))
            elif level == "grand":
                borders = (_border("top", sz=8, color=self.primary)
                           + _border("bottom", sz=8, color=self.primary,
                                     style="double"))
                shading = TINT
            neg = str(value).strip().startswith(("-", "("))
            body_rows.append(
                "<w:tr><w:trPr><w:cantSplit/></w:trPr>"
                + self._cell(run(label, size=17, bold=emphasis,
                                 color=INK if emphasis else INK_SOFT),
                             widths[0], borders=borders, shading=shading)
                + self._cell(run(value, size=16, bold=emphasis, font=MONO_FONT,
                                 color="A23B32" if neg else INK),
                             widths[1], align="right", borders=borders,
                             shading=shading)
                + "</w:tr>")
        self._body.append(
            f'<w:tbl><w:tblPr><w:tblW w:w="{width}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/></w:tblPr>'
            "<w:tblGrid>"
            + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
            + "</w:tblGrid>" + "".join(body_rows) + "</w:tbl>")
        self.para("", space_after=60)

    def signatures(self, roles):
        """Adoption lines: a rule to sign on, the role, and a prompt — one cell
        per signatory, side by side as on the printed pack."""
        if not roles:
            return
        w = CONTENT_W // len(roles)
        tcs = []
        for role in roles:
            cell = (
                f"<w:p><w:pPr><w:spacing w:before=\"500\" w:after=\"40\"/>"
                f"<w:pBdr>{_border('bottom', sz=6, color=INK)}</w:pBdr>"
                f'<w:ind w:right="600"/></w:pPr></w:p>'
                f"<w:p><w:pPr><w:spacing w:before=\"40\" w:after=\"0\"/></w:pPr>"
                f"{run(role, size=16, bold=True, color=INK)}</w:p>"
                f"<w:p><w:pPr><w:spacing w:before=\"0\" w:after=\"120\"/></w:pPr>"
                f"{run('Name, signature & date', size=14, color=MUTED)}</w:p>")
            tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/></w:tcPr>'
                       f"{cell}</w:tc>")
        self._body.append(
            f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_W}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/></w:tblPr>'
            "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for _ in roles)
            + f"</w:tblGrid><w:tr><w:trPr><w:cantSplit/></w:trPr>{''.join(tcs)}</w:tr></w:tbl>")

    # --------------------------------------------------------------- images
    @staticmethod
    def _png_size(data):
        """Width/height straight from the PNG's IHDR — Pillow is not a
        dependency this writer is allowed to assume."""
        if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w or 1, h or 1
        return 600, 300

    def image(self, png_bytes):
        """An inline picture (charts), scaled to the content width."""
        self._images.append(png_bytes)
        rid = f"rIdImg{len(self._images)}"
        px_w, px_h = self._png_size(png_bytes)
        max_w = CONTENT_W * TWIP_EMU
        cx = min(px_w * EMU_PER_PX, max_w)
        cy = int(px_h * EMU_PER_PX * (cx / (px_w * EMU_PER_PX)))
        n = len(self._images)
        self._body.append(
            '<w:p><w:pPr><w:spacing w:before="60" w:after="120"/></w:pPr>'
            '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{n}" name="Chart {n}"/>'
            '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:nvPicPr><pic:cNvPr id="{n}" name="Chart {n}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
            "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>")

    # ------------------------------------------------------------- assembly
    def _header_xml(self):
        half = CONTENT_W // 2
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            "<w:hdr " + _NS + ">"
            f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_W}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/></w:tblPr>'
            f'<w:tblGrid><w:gridCol w:w="{half}"/><w:gridCol w:w="{half}"/></w:tblGrid>'
            "<w:tr>"
            + self._cell(run(self.church, size=15, color=MUTED), half,
                         borders=_border("bottom", sz=4, color=RULE))
            + self._cell(run(self.title, size=15, color=MUTED), half,
                         align="right",
                         borders=_border("bottom", sz=4, color=RULE))
            + "</w:tr></w:tbl><w:p><w:pPr><w:spacing w:after=\"60\"/></w:pPr></w:p></w:hdr>")

    def _footer_xml(self):
        half = CONTENT_W // 2
        page_of = (
            f"{_rpr(size=15, color=MUTED)}".join(["<w:r>", "</w:r>"]) )
        field = (
            f"<w:r>{_rpr(size=15, color=MUTED)}{_t('Page ')}</w:r>"
            f'<w:fldSimple w:instr=" PAGE "><w:r>{_rpr(size=15, color=MUTED)}'
            f"{_t('1')}</w:r></w:fldSimple>"
            f"<w:r>{_rpr(size=15, color=MUTED)}{_t(' of ')}</w:r>"
            f'<w:fldSimple w:instr=" NUMPAGES "><w:r>{_rpr(size=15, color=MUTED)}'
            f"{_t('1')}</w:r></w:fldSimple>")
        left = run(self.period or "", size=15, color=MUTED)
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            "<w:ftr " + _NS + ">"
            f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_W}" w:type="dxa"/>'
            '<w:tblLayout w:type="fixed"/></w:tblPr>'
            f'<w:tblGrid><w:gridCol w:w="{half}"/><w:gridCol w:w="{half}"/></w:tblGrid>'
            "<w:tr>"
            + self._cell(left, half, borders=_border("top", sz=4, color=RULE))
            + f'<w:tc><w:tcPr><w:tcW w:w="{half}" w:type="dxa"/>'
              f"<w:tcBorders>{_border('top', sz=4, color=RULE)}</w:tcBorders></w:tcPr>"
              f'<w:p><w:pPr><w:jc w:val="right"/>'
              f'<w:spacing w:before="30" w:after="30"/></w:pPr>{field}</w:p></w:tc>'
            "</w:tr></w:tbl></w:ftr>")

    def _document_xml(self):
        sect = (
            "<w:sectPr>"
            '<w:headerReference w:type="default" r:id="rIdHdr"/>'
            '<w:footerReference w:type="default" r:id="rIdFtr"/>'
            f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
            f'<w:pgMar w:top="{MARGIN_TOP}" w:right="{MARGIN_SIDE}" '
            f'w:bottom="{MARGIN_BOTTOM}" w:left="{MARGIN_SIDE}" '
            'w:header="450" w:footer="450" w:gutter="0"/>'
            '<w:cols w:space="708"/>'
            "</w:sectPr>")
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                "<w:document " + _NS + "><w:body>"
                + "".join(self._body) + sect + "</w:body></w:document>")

    def _styles_xml(self):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            "<w:styles " + _NS + "><w:docDefaults><w:rPrDefault><w:rPr>"
            f'<w:rFonts w:ascii="{BODY_FONT}" w:hAnsi="{BODY_FONT}"/>'
            f'<w:color w:val="{INK}"/><w:sz w:val="20"/><w:szCs w:val="20"/>'
            "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
            '<w:spacing w:after="0" w:line="276" w:lineRule="auto"/>'
            "</w:pPr></w:pPrDefault></w:docDefaults></w:styles>")

    def _core_xml(self):
        now = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{_esc(self.title)}</dc:title>"
            f"<dc:creator>{_esc(self.church or 'Treasury')}</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            "</cp:coreProperties>")

    def to_bytes(self):
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
            '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>")
        root_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            "</Relationships>")
        doc_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rIdHdr" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
            '<Relationship Id="rIdFtr" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
            + "".join(
                f'<Relationship Id="rIdImg{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{i + 1}.png"/>'
                for i in range(len(self._images)))
            + "</Relationships>")
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("docProps/core.xml", self._core_xml())
            z.writestr("word/document.xml", self._document_xml())
            z.writestr("word/styles.xml", self._styles_xml())
            z.writestr("word/header1.xml", self._header_xml())
            z.writestr("word/footer1.xml", self._footer_xml())
            z.writestr("word/_rels/document.xml.rels", doc_rels)
            for i, png in enumerate(self._images):
                z.writestr(f"word/media/image{i + 1}.png", png)
        return buf.getvalue()


_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
)


def docx_text(payload):
    """Every piece of visible text in a .docx, as a reader would see it — the
    counterpart of decoding the old HTML export, for tests and debugging."""
    import re
    from xml.sax.saxutils import unescape
    with zipfile.ZipFile(BytesIO(payload)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return unescape(" ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)))
