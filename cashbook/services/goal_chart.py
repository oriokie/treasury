"""Server-side JPEG of a fund's Group Contribution Goals, rendered as an
actual table (Group / Goal / Collected / To go / Progress) matching the
on-screen HTML table exactly — generated with Pillow so it renders
identically wherever it's downloaded from."""
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


def _font(name, size):
    path = os.path.join(_FONT_DIR, name)
    try:
        return ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001 — fonts may not be installed; never crash
        return ImageFont.load_default()


def _money(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def build_budget_items_jpeg(*, dept_name, year, rows, tot_budget, tot_actual,
                            tot_variance, church_name=""):
    """rows: list of {"name", "category", "note", "budget", "actual",
    "variance", "pct"} — the same shape FundBudgetView already builds for the
    on-screen page. Returns JPEG bytes. Rendered as a table with the same
    five columns as the "Budget vs actual by item" section (Budget item /
    Budget / Actual / Variance / Used), including the totals row — matching
    the on-screen table exactly, using the same server-side rendering
    approach as build_group_goals_jpeg (Pillow, not a browser screenshot),
    so it looks identical wherever it's downloaded."""
    W = 1180
    pad = 40
    header_h = 96
    col_head_h = 44
    row_h = 46
    total_row_h = 50
    footer_h = 34
    n = len(rows)
    table_h = col_head_h + max(n, 1) * row_h + total_row_h
    H = header_h + table_h + footer_h

    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    f_title = _font("DejaVuSans-Bold.ttf", 24)
    f_sub = _font("DejaVuSans.ttf", 14)
    f_colhead = _font("DejaVuSans-Bold.ttf", 13)
    f_cell = _font("DejaVuSansMono.ttf", 14)
    f_name = _font("DejaVuSans-Bold.ttf", 14)
    f_note = _font("DejaVuSans.ttf", 12)
    f_pct = _font("DejaVuSans-Bold.ttf", 12)
    f_foot = _font("DejaVuSans.ttf", 11)

    y = 26
    if church_name:
        d.text((pad, y), church_name, font=f_sub, fill=MUTED)
        y += 20
    d.text((pad, y), f"Budget vs Actual by Item — {dept_name} {year}",
           font=f_title, fill=FOREST_DEEP)
    y += 32
    d.text((pad, y), "Each budget item's spend so far this year, against its allotted budget",
           font=f_sub, fill=MUTED)

    table_x0, table_x1 = pad, W - pad
    table_w = table_x1 - table_x0

    # column layout: Budget item (flexible) | Budget | Actual | Variance | Used
    col_used_w = 190
    col_num_w = 170
    col_item_w = table_w - col_used_w - 3 * col_num_w
    col_x = [table_x0]
    for cw in (col_item_w, col_num_w, col_num_w, col_num_w, col_used_w):
        col_x.append(col_x[-1] + cw)

    def col_rect(i):
        return col_x[i], col_x[i + 1]

    def right_text(i, cy, text, font, fill, cell_pad=14):
        x0, x1 = col_rect(i)
        tw = d.textlength(text, font=font)
        d.text((x1 - cell_pad - tw, cy), text, font=font, fill=fill)

    top = header_h
    d.rectangle([table_x0, top, table_x1, top + col_head_h], fill=FOREST_SOFT)
    headers = ["Budget item", "Budget", "Actual", "Variance", "Used"]
    hy = top + (col_head_h - 14) / 2
    d.text((col_x[0] + 14, hy), headers[0], font=f_colhead, fill=FOREST_DEEP)
    for i in range(1, 4):
        right_text(i, hy, headers[i], f_colhead, FOREST_DEEP)
    d.text((col_x[4] + 14, hy), headers[4], font=f_colhead, fill=FOREST_DEEP)
    d.line([(table_x0, top + col_head_h), (table_x1, top + col_head_h)],
          fill=LINE_STRONG, width=2)

    ry = top + col_head_h
    if not rows:
        cy = ry + (row_h - 16) / 2
        d.text((col_x[0] + 14, cy), f"No budget items yet for {year}.",
               font=f_note, fill=MUTED)
        ry += row_h
        d.line([(table_x0, ry), (table_x1, ry)], fill=LINE, width=1)
    for idx, r in enumerate(rows):
        if idx % 2 == 1:
            d.rectangle([table_x0, ry, table_x1, ry + row_h], fill=ROW_ALT)
        budget = float(r["budget"] or 0)
        actual = float(r["actual"] or 0)
        variance = float(r["variance"] or 0)
        pct = max(0, int(r["pct"] or 0))
        cy = ry + (row_h - 16) / 2

        name = r["name"]
        extra = " · ".join(x for x in (r.get("category") or "", r.get("note") or "") if x)
        d.text((col_x[0] + 14, cy), name, font=f_name, fill=INK)
        if extra:
            name_w = d.textlength(name, font=f_name)
            d.text((col_x[0] + 20 + name_w, cy + 2), f"· {extra}", font=f_note, fill=MUTED)
        right_text(1, cy, _money(budget), f_cell, INK)
        right_text(2, cy, _money(actual), f_cell, INK)
        right_text(3, cy, _money(variance), f_cell, (196, 60, 51) if variance < 0 else INK)

        # "Used" column: progress bar + percentage, mirroring the on-screen
        # <div class="progress"> bar next to the "N%" text
        bar_x0 = col_x[4] + 14
        bar_x1 = col_x[5] - 55
        bar_y0 = ry + row_h / 2 - 6
        bar_y1 = bar_y0 + 12
        d.rounded_rectangle([bar_x0, bar_y0, bar_x1, bar_y1], radius=6, fill=FOREST_SOFT)
        clipped_pct = min(pct, 100)
        fill_w = int((bar_x1 - bar_x0) * clipped_pct / 100) if clipped_pct else 0
        if fill_w > 0:
            color = (196, 60, 51) if pct > 100 else FOREST
            d.rounded_rectangle([bar_x0, bar_y0, bar_x0 + max(fill_w, 10), bar_y1],
                                radius=6, fill=color)
        d.text((bar_x1 + 8, ry + row_h / 2 - 7), f"{pct}%", font=f_pct, fill=MUTED)

        ry += row_h
        d.line([(table_x0, ry), (table_x1, ry)], fill=LINE, width=1)

    # ---- total row ----
    d.rectangle([table_x0, ry, table_x1, ry + total_row_h], fill=FOREST_SOFT)
    ty = ry + (total_row_h - 16) / 2
    d.text((col_x[0] + 14, ty), "Total", font=f_name, fill=FOREST_DEEP)
    right_text(1, ty, _money(tot_budget), f_name, FOREST_DEEP)
    right_text(2, ty, _money(tot_actual), f_name, FOREST_DEEP)
    right_text(3, ty, _money(tot_variance), f_name,
              (196, 60, 51) if tot_variance < 0 else FOREST_DEEP)
    ry += total_row_h
    d.line([(table_x0, ry), (table_x1, ry)], fill=LINE_STRONG, width=2)

    d.rectangle([table_x0, top, table_x1, ry], outline=LINE_STRONG, width=1)
    for cx in col_x[1:-1]:
        d.line([(cx, top), (cx, ry)], fill=LINE, width=1)

    fy = ry + 12
    d.text((table_x0, fy), f"Generated by the Treasury system · {year}",
           font=f_foot, fill=MUTED)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    out.seek(0)
    return out.getvalue()


def build_group_goals_jpeg(*, dept_name, year, group_rows, contribution_goal,
                            church_name=""):
    """group_rows: list of {"name", "goal", "collected", "pct", "short"}.
    contribution_goal: {"goal", "collected", "short"} totals across all groups.
    Returns JPEG bytes. Rendered as a table with the same five columns as the
    on-screen page (Group / Goal / Collected <year> / To go / Progress), not
    a progress-bar chart — so the downloaded image reads exactly like the
    table a treasurer already sees on screen, for printing or sharing
    verbatim."""
    W = 1180
    pad = 40
    header_h = 96          # title + subtitle block
    col_head_h = 44         # table column-header row
    row_h = 46
    total_row_h = 50
    footer_h = 34
    n = max(len(group_rows), 1) if group_rows else 0
    table_h = col_head_h + n * row_h + total_row_h
    H = header_h + table_h + footer_h

    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    f_title = _font("DejaVuSans-Bold.ttf", 24)
    f_sub = _font("DejaVuSans.ttf", 14)
    f_colhead = _font("DejaVuSans-Bold.ttf", 13)
    f_cell = _font("DejaVuSansMono.ttf", 14)
    f_name = _font("DejaVuSans-Bold.ttf", 14)
    f_pill = _font("DejaVuSans-Bold.ttf", 11)
    f_pct = _font("DejaVuSans-Bold.ttf", 12)
    f_foot = _font("DejaVuSans.ttf", 11)

    # ---- header ----
    y = 26
    if church_name:
        d.text((pad, y), church_name, font=f_sub, fill=MUTED)
        y += 20
    d.text((pad, y), f"Group Contribution Goals — {dept_name} {year}",
           font=f_title, fill=FOREST_DEEP)
    y += 32
    d.text((pad, y), "Each development group's own collection toward its target",
           font=f_sub, fill=MUTED)

    table_x0, table_x1 = pad, W - pad
    table_w = table_x1 - table_x0

    # column layout: Group (flexible) | Goal | Collected | To go | Progress
    col_progress_w = 200
    col_num_w = 170
    col_group_w = table_w - col_progress_w - 3 * col_num_w
    col_x = [table_x0]
    for cw in (col_group_w, col_num_w, col_num_w, col_num_w, col_progress_w):
        col_x.append(col_x[-1] + cw)
    # col_x now has 6 boundaries for 5 columns

    def col_rect(i):
        return col_x[i], col_x[i + 1]

    def right_text(i, cy, text, font, fill, cell_pad=14):
        x0, x1 = col_rect(i)
        tw = d.textlength(text, font=font)
        d.text((x1 - cell_pad - tw, cy), text, font=font, fill=fill)

    top = header_h
    # ---- column header row ----
    d.rectangle([table_x0, top, table_x1, top + col_head_h], fill=FOREST_SOFT)
    headers = ["Group", "Goal", f"Collected {year}", "To go", "Progress"]
    hy = top + (col_head_h - 14) / 2
    d.text((col_x[0] + 14, hy), headers[0], font=f_colhead, fill=FOREST_DEEP)
    for i in range(1, 4):
        right_text(i, hy, headers[i], f_colhead, FOREST_DEEP)
    d.text((col_x[4] + 14, hy), headers[4], font=f_colhead, fill=FOREST_DEEP)
    d.line([(table_x0, top + col_head_h), (table_x1, top + col_head_h)],
          fill=LINE_STRONG, width=2)

    # ---- data rows ----
    ry = top + col_head_h
    for idx, g in enumerate(group_rows):
        if idx % 2 == 1:
            d.rectangle([table_x0, ry, table_x1, ry + row_h], fill=ROW_ALT)
        goal = float(g["goal"] or 0)
        collected = float(g["collected"] or 0)
        short = float(g["short"] or 0)
        pct = max(0, min(int(g["pct"] or 0), 100))
        cy = ry + (row_h - 16) / 2

        d.text((col_x[0] + 14, cy), g["name"], font=f_name, fill=INK)
        right_text(1, cy, _money(goal), f_cell, INK)
        right_text(2, cy, _money(collected), f_cell, INK)
        if short > 0:
            right_text(3, cy, _money(short), f_cell, INK)
        else:
            # a small "met" pill, right-aligned like the HTML page's badge
            label = "met"
            tw = d.textlength(label, font=f_pill)
            px1 = col_x[4] - 14
            px0 = px1 - tw - 16
            py0, py1 = ry + (row_h - 22) / 2, ry + (row_h + 22) / 2
            d.rounded_rectangle([px0, py0, px1, py1], radius=10, fill=GREEN_SOFT)
            d.text((px0 + 8, py0 + 4), label, font=f_pill, fill=GREEN)

        # progress column: small track + fill + percentage, mirroring the
        # on-screen <div class="progress"> bar and the "N%" text beside it
        bar_x0 = col_x[4] + 14
        bar_x1 = col_x[5] - 60
        bar_y0 = ry + row_h / 2 - 6
        bar_y1 = bar_y0 + 12
        d.rounded_rectangle([bar_x0, bar_y0, bar_x1, bar_y1], radius=6, fill=FOREST_SOFT)
        if goal > 0:
            fill_w = int((bar_x1 - bar_x0) * min(collected / goal, 1.0))
        else:
            fill_w = 0
        if fill_w > 0:
            color = GREEN if (collected > goal and goal > 0) else FOREST
            d.rounded_rectangle([bar_x0, bar_y0, bar_x0 + max(fill_w, 10), bar_y1],
                                radius=6, fill=color)
        d.text((bar_x1 + 8, ry + row_h / 2 - 7), f"{pct}%", font=f_pct, fill=MUTED)

        ry += row_h
        d.line([(table_x0, ry), (table_x1, ry)], fill=LINE, width=1)

    # ---- total row: "All groups" ----
    d.rectangle([table_x0, ry, table_x1, ry + total_row_h], fill=FOREST_SOFT)
    ty = ry + (total_row_h - 16) / 2
    d.text((col_x[0] + 14, ty), "All groups", font=f_name, fill=FOREST_DEEP)
    right_text(1, ty, _money(contribution_goal.get("goal", 0)), f_name, FOREST_DEEP)
    right_text(2, ty, _money(contribution_goal.get("collected", 0)), f_name, FOREST_DEEP)
    right_text(3, ty, _money(contribution_goal.get("short", 0)), f_name, BRASS)
    ry += total_row_h
    d.line([(table_x0, ry), (table_x1, ry)], fill=LINE_STRONG, width=2)

    # outer border
    d.rectangle([table_x0, top, table_x1, ry], outline=LINE_STRONG, width=1)
    for cx in col_x[1:-1]:
        d.line([(cx, top), (cx, ry)], fill=LINE, width=1)

    # ---- footer ----
    fy = ry + 12
    d.text((table_x0, fy), f"Generated by the Treasury system · {year}",
           font=f_foot, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
