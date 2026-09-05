"""The .docx writer's own guarantees.

The renderer tests prove reports come out; these prove the properties the
revamp exists for — a real package, real pages, tables that survive page
breaks — so a future edit to the XML strings cannot quietly lose one.
"""
import io
import xml.dom.minidom
import zipfile

from django.test import SimpleTestCase

from core.reporting.wordml import BLANK_MARK, PAGE_W, WordDoc, docx_text


def _doc(**kw):
    d = WordDoc(title=kw.pop("title", "Test Report"),
                church=kw.pop("church", "St Test & Co"),
                period=kw.pop("period", "01 Jan to 31 Jan"), **kw)
    return d


def _part(payload, name):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        return z.read(name).decode("utf-8")


class PackageTests(SimpleTestCase):
    def test_every_part_is_well_formed_xml(self):
        d = _doc()
        d.masthead(org="St Test & Co", meta="Prepared today")
        d.group_heading(1, "Overview", first=True)
        d.kpi_band([("Receipts", "1,000", "cash for the period"),
                    ("Expenditure", "400")])
        d.table([{"label": "Fund", "numeric": False},
                 {"label": "Closing", "numeric": True}],
                [{"cells": ["Building", "1,000.00"], "level": "",
                  "emphasis": False},
                 {"cells": ["Quiet fund", ""], "level": "",
                  "emphasis": False}],
                total=["TOTAL", "1,000.00"])
        d.keyvalue([("Assets", "", "heading"), ("Bank", "9.00", ""),
                    ("Nil line", "", ""),
                    ("Net assets", "9.00", "grand")])
        d.explanation("The books balance.")
        d.signatures(["Prepared by", "Approved by"])
        d.doc_footer(left="St Test · Report", right="Figures from the registry.")
        payload = d.to_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                if name.endswith((".xml", ".rels")):
                    xml.dom.minidom.parseString(z.read(name))

    def test_ampersands_in_church_names_are_escaped_everywhere(self):
        """St Test & Co appears in the header, the core properties and the
        masthead — one raw ampersand in any of them corrupts the file."""
        d = _doc()
        d.masthead(org="St Test & Co")
        payload = d.to_bytes()
        self.assertIn("St Test &amp; Co", _part(payload, "word/header1.xml"))
        self.assertIn("St Test &amp; Co", _part(payload, "docProps/core.xml"))
        self.assertIn("St Test & Co", docx_text(payload))


class PrintBehaviourTests(SimpleTestCase):
    """The three things the old HTML .doc could not promise."""

    def _table_doc(self):
        d = _doc()
        d.table([{"label": "Fund", "numeric": False},
                 {"label": "Closing", "numeric": True}],
                [{"cells": ["Building", "1.00"], "level": "",
                  "emphasis": False}])
        return d.to_bytes()

    def test_a4_with_the_pack_s_margins(self):
        docxml = _part(self._table_doc(), "word/document.xml")
        self.assertIn(f'w:pgSz w:w="{PAGE_W}"', docxml)
        self.assertIn('w:top="1134"', docxml)      # 20 mm, as the print CSS

    def test_table_headers_repeat_and_rows_never_split(self):
        docxml = _part(self._table_doc(), "word/document.xml")
        self.assertIn("<w:tblHeader/>", docxml)
        self.assertIn("<w:cantSplit/>", docxml)

    def test_the_footer_counts_pages_itself(self):
        payload = self._table_doc()
        ftr = _part(payload, "word/footer1.xml")
        self.assertIn('w:instr=" PAGE "', ftr)
        self.assertIn('w:instr=" NUMPAGES "', ftr)
        # and the document binds to it
        self.assertIn('w:footerReference', _part(payload, "word/document.xml"))

    def test_images_become_media_parts_with_relationships(self):
        png = (b"\\x89PNG\\r\\n\\x1a\\n" + b"\\x00" * 8
               + (600).to_bytes(4, "big") + (300).to_bytes(4, "big")
               + b"\\x00" * 8)
        d = _doc()
        d.image(png)
        payload = d.to_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            self.assertIn("word/media/image1.png", z.namelist())
        self.assertIn('Target="media/image1.png"',
                      _part(payload, "word/_rels/document.xml.rels"))
        self.assertIn("rIdImg1", _part(payload, "word/document.xml"))

    def test_a_grand_row_rules_top_and_double_bottom(self):
        d = _doc()
        d.keyvalue([("Net assets", "9.00", "grand")])
        docxml = _part(d.to_bytes(), "word/document.xml")
        self.assertIn('w:val="double"', docxml)


class BoardReadyPresentationTests(SimpleTestCase):
    """The polish that makes a board-pack Word download read like the printed
    pack: blank cells, KPI subs, commentary labels, page breaks."""

    def test_empty_numeric_cells_print_as_a_middot(self):
        d = _doc()
        d.table([{"label": "Account", "numeric": False},
                 {"label": "Debit", "numeric": True},
                 {"label": "Credit", "numeric": True}],
                [{"cells": ["Bank", "100.00", ""], "level": "",
                  "emphasis": False}])
        text = docx_text(d.to_bytes())
        self.assertIn(BLANK_MARK, text)
        self.assertIn("Bank", text)
        self.assertIn("100.00", text)

    def test_kpi_band_carries_an_optional_sub_line(self):
        d = _doc()
        d.kpi_band([("Total receipts", "50,000", "cash for the period")])
        text = docx_text(d.to_bytes())
        self.assertIn("Total receipts", text)
        self.assertIn("50,000", text)
        self.assertIn("cash for the period", text)

    def test_explanation_is_labelled_what_this_shows(self):
        d = _doc()
        d.explanation("Collections matched the cash book.")
        text = docx_text(d.to_bytes())
        self.assertIn("What this shows", text)
        self.assertIn("Collections matched the cash book.", text)

    def test_page_break_emits_a_real_word_break(self):
        d = _doc()
        d.group_heading(1, "Overview", first=True)
        d.page_break()
        d.group_heading(2, "Statements")
        payload = d.to_bytes()
        docxml = _part(payload, "word/document.xml")
        self.assertIn('w:br w:type="page"', docxml)
        text = docx_text(payload)
        self.assertIn("Overview", text)
        self.assertIn("Statements", text)
