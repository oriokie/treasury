"""Server-side PNG of a fund's Group Contribution Goals and Budget vs Actual
tables, rendered with Pillow so they render identically wherever downloaded
— no browser/headless-Chrome step, no HTML-to-image conversion; Pillow draws
directly onto a bitmap.

High-DPI rendering (v2.43): everything (canvas size, every font, every line
width, every padding and radius) is defined at a LOGICAL size and then drawn
at ``SCALE``× that — the same "render at 3x/4x, let the viewer downscale"
technique behind Retina/HiDPI web assets and app icon exports. Previously
everything was drawn directly at logical (~96 DPI screen) pixel sizes: fine
on a phone screen, visibly soft once zoomed or printed at A4, because there
was no more pixel detail than a screen needed. A canvas that is
``LOGICAL_W * SCALE`` pixels wide, with a proportionally ``SCALE``× larger
font drawn onto it, contains real additional detail — Pillow's own font
rasteriser draws each glyph at the larger size, it is not a blurry upscale
of a small bitmap. The PNG is also tagged with 300 DPI metadata, the print
industry's ordinary "quality" figure, so print dialogs and PDF embedding
report and lay out the physical size correctly instead of guessing 96 DPI
(which would make a now-4×-larger image print roughly 4× too big).

PNG (not JPEG): these are sharp-edged tables of text and numbers, and JPEG's
lossy compression blurs text edges and introduces colour banding on flat
fills — PNG is lossless, and at typical table sizes the file-size cost of
even a 4×-scale render is small (flat colour and text compress well).
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

FOREST = (31, 95, 79)
FOREST_SOFT = (231, 240, 235)
FOREST_DEEP = (20, 58, 49)
BRASS = (176, 125, 44)
INK = (27, 36, 32)
MUTED = (102, 119, 112)
PAPER = (255, 255, 255)
ROW_ALT = (247, 250, 248)
LINE = (223, 230, 226)
LINE_STRONG = (180, 195, 188)
GREEN = (46, 125, 87)
GREEN_SOFT = (223, 240, 231)

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# Everything below is specified at this LOGICAL pixel size (what the old,
# screen-only version rendered at 1:1) and drawn at SCALE times that. 4x
# comfortably clears the 300 DPI print-quality bar for a page-width table
# and leaves headroom for zooming into the PNG on screen. Raise this if a
# future table is small enough that even more headroom is cheap; there is no
# correctness reason to keep it low — Pillow's draw cost for a few dozen
# rows of flat colour and text is trivial even at high SCALE.
SCALE = 4
PNG_DPI = (300, 300)


def _s(n):
    """Scale a logical dimension to actual pixels."""
    return round(n * SCALE)


# Fallback font family, used only if the system DejaVu fonts above are not
# installed. This is the actual bug behind a genuine production report:
# "the PNG isn't showing the figures." The system DejaVu path depends on an
# OS package (fonts-dejavu-core) that requirements.txt cannot express and a
# server deployment can easily be missing; when ImageFont.truetype() failed,
# _font() fell back to Pillow's ImageFont.load_default() — a FIXED-SIZE
# BITMAP font that ignores the requested size entirely. At this file's 4x
# print-quality SCALE, every position, padding and row height is computed
# for a font roughly four times the size load_default() actually draws, so
# the numbers were technically present in the file but rendered as
# near-invisible specks inside cells sized for much larger text — "the
# figures aren't loading" was an accurate description of what it looked
# like, even though nothing had actually failed to render.
#
# reportlab ships its own TTF fonts (Bitstream Vera) inside the wheel, so
# they are present in EVERY environment that can run this application at
# all — reportlab is already a hard, non-optional dependency (PDF export).
# Falling back to a real, scalable vector font here — instead of a bitmap
# one — means SCALE continues to work correctly even on a server with no
# system fonts installed, rather than merely failing more gracefully.
def _vera_font_dir():
    import reportlab
    return os.path.join(os.path.dirname(reportlab.__file__), "fonts")


_VERA_MAP = {
    "DejaVuSans-Bold.ttf": "VeraBd.ttf",
    "DejaVuSans.ttf": "Vera.ttf",
    # Vera has no monospace member; the regular face is still a real,
    # scalable font — a table column that is very slightly less evenly
    # aligned is a minor cosmetic difference, not the invisible-text
    # failure this fallback exists to avoid.
    "DejaVuSansMono.ttf": "Vera.ttf",
}


def _font(name, size):
    path = os.path.join(_FONT_DIR, name)
    try:
        return ImageFont.truetype(path, _s(size))
    except Exception:  # noqa: BLE001 — the system fonts may not be installed
        try:
            vera_path = os.path.join(_vera_font_dir(), _VERA_MAP.get(name, "Vera.ttf"))
            return ImageFont.truetype(vera_path, _s(size))
        except Exception:  # noqa: BLE001 — reportlab itself missing/changed;
            # true last resort. Rendering will look wrong at this file's 4x
            # SCALE, exactly as before this fix, but never crash the request.
            return ImageFont.load_default()


def _money(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def build_budget_items_png(*, dept_name, year, rows, tot_budget, tot_actual,
                           tot_variance, church_name=""):
    """rows: list of {"name", "category", "note", "budget", "actual",
    "variance", "pct"} — the same shape FundBudgetView already builds for the
    on-screen page. Returns PNG bytes. Rendered as a table with the same
    five columns as the "Budget vs actual by item" section (Budget item /
    Budget / Actual / Variance / Used), including the totals row — matching
    the on-screen table exactly, using the same server-side rendering
    approach as build_group_goals_png (Pillow, not a browser screenshot),
    so it looks identical wherever it's downloaded."""
    # --- sized for a PHONE, deliberately -----------------------------------
    #
    # This was 1180px wide with 14pt text. On a phone that image is scaled to fit
    # a ~380px viewport — about a third — so the "14pt" text actually rendered at
    # roughly 4.5pt. It was not that the fonts were small; it was that the image
    # was a desktop table being shrunk to a third of its size, and everything in
    # it with it.
    #
    # What matters is the RATIO of text size to image width, because that is what
    # survives the scaling. It was 14/1180 ≈ 1.2%. It is now ~2.8%, which is
    # legible on a phone held at arm's length — the test that actually counts.
    #
    # The "Used" progress bar is gone. A bar is a picture of a number, and a
    # picture of a number does not survive being scaled to a third of its size.
    # The percentage itself does, and says the same thing.
    W = 760
    pad = 22
    header_h = 84
    col_head_h = 40
    row_h = 46
    total_row_h = 50
    footer_h = 28
    cell_pad = 10
    n = len(rows)
    table_h = col_head_h + max(n, 1) * row_h + total_row_h
    H = header_h + table_h + footer_h

    img = Image.new("RGB", (_s(W), _s(H)), PAPER)
    d = ImageDraw.Draw(img)

    f_title = _font("DejaVuSans-Bold.ttf", 26)
    f_sub = _font("DejaVuSans.ttf", 15)
    f_colhead = _font("DejaVuSans-Bold.ttf", 17)
    f_cell = _font("DejaVuSansMono.ttf", 19)
    f_name = _font("DejaVuSans-Bold.ttf", 19)
    f_note = _font("DejaVuSans.ttf", 14)
    f_pct = _font("DejaVuSans-Bold.ttf", 18)
    f_foot = _font("DejaVuSans.ttf", 13)

    y = 14
    if church_name:
        d.text((_s(pad), _s(y)), church_name, font=f_sub, fill=MUTED)
        y += 20
    d.text((_s(pad), _s(y)), f"{dept_name} {year}", font=f_title, fill=FOREST_DEEP)
    y += 32
    d.text((_s(pad), _s(y)), "Budget vs actual, by item", font=f_sub, fill=MUTED)

    table_x0, table_x1 = pad, W - pad
    table_w = table_x1 - table_x0

    # column layout: Budget item (flexible) | Budget | Actual | Variance | %
    col_used_w = 68
    col_num_w = 132
    col_item_w = table_w - col_used_w - 3 * col_num_w
    col_x = [table_x0]
    for cw in (col_item_w, col_num_w, col_num_w, col_num_w, col_used_w):
        col_x.append(col_x[-1] + cw)

    def col_rect(i):
        return _s(col_x[i]), _s(col_x[i + 1])

    def right_text(i, cy, text, font, fill, pad_px=cell_pad):
        x0, x1 = col_rect(i)
        tw = d.textlength(text, font=font)
        d.text((x1 - _s(pad_px) - tw, cy), text, font=font, fill=fill)

    top = header_h
    d.rectangle([_s(table_x0), _s(top), _s(table_x1), _s(top + col_head_h)], fill=FOREST_SOFT)
    headers = ["Item", "Budget", "Actual", "Variance", "%"]
    hy = _s(top + (col_head_h - 18) / 2)
    d.text((_s(col_x[0] + cell_pad), hy), headers[0], font=f_colhead, fill=FOREST_DEEP)
    for i in range(1, 5):
        right_text(i, hy, headers[i], f_colhead, FOREST_DEEP)
    d.line([(_s(table_x0), _s(top + col_head_h)), (_s(table_x1), _s(top + col_head_h))],
          fill=LINE_STRONG, width=_s(2))

    ry = top + col_head_h
    if not rows:
        cy = _s(ry + (row_h - 16) / 2)
        d.text((_s(col_x[0] + cell_pad), cy), f"No budget items yet for {year}.",
               font=f_note, fill=MUTED)
        ry += row_h
        d.line([(_s(table_x0), _s(ry)), (_s(table_x1), _s(ry))], fill=LINE, width=_s(1))
    for idx, r in enumerate(rows):
        if idx % 2 == 1:
            d.rectangle([_s(table_x0), _s(ry), _s(table_x1), _s(ry + row_h)], fill=ROW_ALT)
        budget = float(r["budget"] or 0)
        actual = float(r["actual"] or 0)
        variance = float(r["variance"] or 0)
        pct = max(0, int(r["pct"] or 0))
        cy = _s(ry + (row_h - 16) / 2)

        name = r["name"]
        extra = " · ".join(x for x in (r.get("category") or "", r.get("note") or "") if x)
        # name on its own line, detail beneath it — a narrow image has no room to
        # run them side by side, and truncating the name would be worse
        if extra:
            d.text((_s(col_x[0] + cell_pad), _s(ry + 6)), name, font=f_name, fill=INK)
            d.text((_s(col_x[0] + cell_pad), _s(ry + 26)), extra[:34], font=f_note, fill=MUTED)
        else:
            d.text((_s(col_x[0] + cell_pad), cy), name, font=f_name, fill=INK)
        right_text(1, cy, _money(budget), f_cell, INK)
        right_text(2, cy, _money(actual), f_cell, INK)
        right_text(3, cy, _money(variance), f_cell, (196, 60, 51) if variance < 0 else INK)

        # The percentage, plainly. The progress bar that used to be here was a
        # picture of this same number — and a picture of a number does not
        # survive being scaled to a third of its size on a phone. The number
        # does, and says the same thing.
        right_text(4, cy, f"{pct}%", f_pct,
                   (196, 60, 51) if pct > 100 else MUTED)

        ry += row_h
        d.line([(_s(table_x0), _s(ry)), (_s(table_x1), _s(ry))], fill=LINE, width=_s(1))

    # ---- total row ----
    d.rectangle([_s(table_x0), _s(ry), _s(table_x1), _s(ry + total_row_h)], fill=FOREST_SOFT)
    ty = _s(ry + (total_row_h - 16) / 2)
    d.text((_s(col_x[0] + cell_pad), ty), "Total", font=f_name, fill=FOREST_DEEP)
    right_text(1, ty, _money(tot_budget), f_name, FOREST_DEEP)
    right_text(2, ty, _money(tot_actual), f_name, FOREST_DEEP)
    right_text(3, ty, _money(tot_variance), f_name,
              (196, 60, 51) if tot_variance < 0 else FOREST_DEEP)
    ry += total_row_h
    d.line([(_s(table_x0), _s(ry)), (_s(table_x1), _s(ry))], fill=LINE_STRONG, width=_s(2))

    d.rectangle([_s(table_x0), _s(top), _s(table_x1), _s(ry)], outline=LINE_STRONG, width=_s(1))
    for cx in col_x[1:-1]:
        d.line([(_s(cx), _s(top)), (_s(cx), _s(ry))], fill=LINE, width=_s(1))

    fy = _s(ry + 12)
    d.text((_s(table_x0), fy), f"Generated by the Treasury system · {year}",
           font=f_foot, fill=MUTED)

    out = io.BytesIO()
    img.save(out, format="PNG", dpi=PNG_DPI)
    out.seek(0)
    return out.getvalue()


def build_group_goals_png(*, dept_name, year, group_rows, contribution_goal,
                          church_name=""):
    """group_rows: list of {"name", "goal", "collected", "pct", "short"}.
    contribution_goal: {"goal", "collected", "short"} totals across all groups.
    Returns PNG bytes. Rendered as a table with the same five columns as the
    on-screen page (Group / Goal / Collected <year> / To go / Progress), not
    a progress-bar chart — so the downloaded image reads exactly like the
    table a treasurer already sees on screen, for printing or sharing
    verbatim."""
    # Sized for a phone — see build_budget_items_png above for why the ratio of
    # text size to image width is the thing that matters, not the point size.
    W = 760
    pad = 22
    header_h = 84
    col_head_h = 40
    row_h = 46
    total_row_h = 50
    footer_h = 28
    cell_pad = 10
    n = max(len(group_rows), 1) if group_rows else 0
    table_h = col_head_h + n * row_h + total_row_h
    H = header_h + table_h + footer_h

    img = Image.new("RGB", (_s(W), _s(H)), PAPER)
    d = ImageDraw.Draw(img)

    f_title = _font("DejaVuSans-Bold.ttf", 26)
    f_sub = _font("DejaVuSans.ttf", 15)
    f_colhead = _font("DejaVuSans-Bold.ttf", 17)
    f_cell = _font("DejaVuSansMono.ttf", 19)
    f_name = _font("DejaVuSans-Bold.ttf", 19)
    f_pill = _font("DejaVuSans-Bold.ttf", 14)
    f_pct = _font("DejaVuSans-Bold.ttf", 18)
    f_foot = _font("DejaVuSans.ttf", 13)

    # ---- header ----
    y = 26
    if church_name:
        d.text((_s(pad), _s(y)), church_name, font=f_sub, fill=MUTED)
        y += 20
    d.text((_s(pad), _s(y)), f"Group Contribution Goals — {dept_name} {year}",
           font=f_title, fill=FOREST_DEEP)
    y += 32
    d.text((_s(pad), _s(y)), "Each development group's own collection toward its target",
           font=f_sub, fill=MUTED)

    table_x0, table_x1 = pad, W - pad
    table_w = table_x1 - table_x0

    # column layout: Group (flexible) | Goal | Collected | To go | %
    col_progress_w = 68
    col_num_w = 132
    col_group_w = table_w - col_progress_w - 3 * col_num_w
    col_x = [table_x0]
    for cw in (col_group_w, col_num_w, col_num_w, col_num_w, col_progress_w):
        col_x.append(col_x[-1] + cw)
    # col_x now has 6 boundaries for 5 columns

    def col_rect(i):
        return _s(col_x[i]), _s(col_x[i + 1])

    def right_text(i, cy, text, font, fill, pad_px=cell_pad):
        x0, x1 = col_rect(i)
        tw = d.textlength(text, font=font)
        d.text((x1 - _s(pad_px) - tw, cy), text, font=font, fill=fill)

    top = header_h
    # ---- column header row ----
    d.rectangle([_s(table_x0), _s(top), _s(table_x1), _s(top + col_head_h)], fill=FOREST_SOFT)
    headers = ["Group", "Goal", f"Collected {year}", "To go", "%"]
    hy = _s(top + (col_head_h - 14) / 2)
    d.text((_s(col_x[0] + cell_pad), hy), headers[0], font=f_colhead, fill=FOREST_DEEP)
    for i in range(1, 4):
        right_text(i, hy, headers[i], f_colhead, FOREST_DEEP)
    d.text((_s(col_x[4] + cell_pad), hy), headers[4], font=f_colhead, fill=FOREST_DEEP)
    d.line([(_s(table_x0), _s(top + col_head_h)), (_s(table_x1), _s(top + col_head_h))],
          fill=LINE_STRONG, width=_s(2))

    # ---- data rows ----
    ry = top + col_head_h
    for idx, g in enumerate(group_rows):
        if idx % 2 == 1:
            d.rectangle([_s(table_x0), _s(ry), _s(table_x1), _s(ry + row_h)], fill=ROW_ALT)
        goal = float(g["goal"] or 0)
        collected = float(g["collected"] or 0)
        short = float(g["short"] or 0)
        pct = max(0, min(int(g["pct"] or 0), 100))
        cy = _s(ry + (row_h - 16) / 2)

        d.text((_s(col_x[0] + cell_pad), cy), g["name"], font=f_name, fill=INK)
        right_text(1, cy, _money(goal), f_cell, INK)
        right_text(2, cy, _money(collected), f_cell, INK)
        if short > 0:
            right_text(3, cy, _money(short), f_cell, INK)
        else:
            # a small "met" pill, right-aligned like the HTML page's badge
            label = "met"
            tw = d.textlength(label, font=f_pill)
            px1 = _s(col_x[4]) - _s(14)
            px0 = px1 - tw - _s(16)
            py0, py1 = _s(ry + (row_h - 22) / 2), _s(ry + (row_h + 22) / 2)
            d.rounded_rectangle([px0, py0, px1, py1], radius=_s(10), fill=GREEN_SOFT)
            d.text((px0 + _s(8), py0 + _s(4)), label, font=f_pill, fill=GREEN)

        # The percentage, plainly — the progress bar that used to be here was a
        # picture of this same number, and a picture of a number does not survive
        # being scaled to a third of its size on a phone.
        right_text(4, cy, f"{pct}%", f_pct,
                   GREEN if (goal > 0 and collected >= goal) else MUTED)

        ry += row_h
        d.line([(_s(table_x0), _s(ry)), (_s(table_x1), _s(ry))], fill=LINE, width=_s(1))

    # ---- total row: "All groups" ----
    d.rectangle([_s(table_x0), _s(ry), _s(table_x1), _s(ry + total_row_h)], fill=FOREST_SOFT)
    ty = _s(ry + (total_row_h - 16) / 2)
    d.text((_s(col_x[0] + cell_pad), ty), "All groups", font=f_name, fill=FOREST_DEEP)
    right_text(1, ty, _money(contribution_goal.get("goal", 0)), f_name, FOREST_DEEP)
    right_text(2, ty, _money(contribution_goal.get("collected", 0)), f_name, FOREST_DEEP)
    right_text(3, ty, _money(contribution_goal.get("short", 0)), f_name, BRASS)
    ry += total_row_h
    d.line([(_s(table_x0), _s(ry)), (_s(table_x1), _s(ry))], fill=LINE_STRONG, width=_s(2))

    # outer border
    d.rectangle([_s(table_x0), _s(top), _s(table_x1), _s(ry)], outline=LINE_STRONG, width=_s(1))
    for cx in col_x[1:-1]:
        d.line([(_s(cx), _s(top)), (_s(cx), _s(ry))], fill=LINE, width=_s(1))

    # ---- footer ----
    fy = _s(ry + 12)
    d.text((_s(table_x0), fy), f"Generated by the Treasury system · {year}",
           font=f_foot, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=PNG_DPI)
    return buf.getvalue()
