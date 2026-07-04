"""Server-side JPEG chart for a fund's Group Contribution Goals — a clean
per-group progress bar chart (not just a table screenshot), generated with
Pillow so it renders identically wherever it's downloaded from."""
import io
import os

from PIL import Image, ImageDraw, ImageFont

FOREST = (31, 95, 79)
FOREST_SOFT = (231, 240, 235)
BRASS = (176, 125, 44)
INK = (27, 36, 32)
MUTED = (102, 119, 112)
PAPER = (255, 255, 255)
LINE = (223, 230, 226)
GREEN = (46, 125, 87)

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


def build_group_goals_jpeg(*, dept_name, year, group_rows, contribution_goal,
                            church_name=""):
    """group_rows: list of {"name", "goal", "collected", "pct", "short"}.
    contribution_goal: {"goal", "collected", "short"} totals across all groups.
    Returns JPEG bytes."""
    W = 1180
    row_h = 62
    header_h = 132
    footer_h = 90
    pad = 40
    n = max(len(group_rows), 1)
    H = header_h + n * row_h + footer_h + pad

    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)

    f_title = _font("DejaVuSans-Bold.ttf", 26)
    f_sub = _font("DejaVuSans.ttf", 15)
    f_name = _font("DejaVuSans-Bold.ttf", 16)
    f_val = _font("DejaVuSansMono.ttf", 15)
    f_pct = _font("DejaVuSans-Bold.ttf", 14)
    f_foot = _font("DejaVuSans-Bold.ttf", 17)

    # header
    y = 28
    if church_name:
        d.text((pad, y), church_name, font=f_sub, fill=MUTED)
        y += 22
    d.text((pad, y), f"Group Contribution Goals — {dept_name} {year}",
           font=f_title, fill=FOREST)
    y += 38
    d.line([(pad, y), (W - pad, y)], fill=FOREST, width=3)
    y += 16
    d.text((pad, y), "Each development group's own collection toward its target",
           font=f_sub, fill=MUTED)

    bar_x0 = pad
    bar_x1 = W - pad
    bar_w = bar_x1 - bar_x0

    top = header_h
    for i, g in enumerate(group_rows):
        ry = top + i * row_h
        goal = float(g["goal"] or 0)
        collected = float(g["collected"] or 0)
        short = float(g["short"] or 0)
        pct = min(int(g["pct"] or 0), 100)
        over = collected > goal and goal > 0

        d.text((bar_x0, ry), g["name"], font=f_name, fill=INK)
        to_go_label = "met" if short <= 0 and goal > 0 else _money(short)
        label = (f"Target {_money(goal)}   Contributed {_money(collected)}   "
                f"To go {to_go_label}   ({pct}%)")
        lw = d.textlength(label, font=f_val)
        d.text((bar_x1 - lw, ry), label, font=f_val, fill=MUTED)

        track_y0 = ry + 26
        track_y1 = track_y0 + 16
        d.rounded_rectangle([bar_x0, track_y0, bar_x1, track_y1], radius=8,
                            fill=FOREST_SOFT)
        if goal > 0:
            fill_w = int(bar_w * min(collected / goal, 1.0))
        else:
            fill_w = 0
        if fill_w > 0:
            color = GREEN if over else FOREST
            d.rounded_rectangle([bar_x0, track_y0, bar_x0 + max(fill_w, 16), track_y1],
                                radius=8, fill=color)

    # footer: all-groups total
    fy = top + n * row_h + 20
    d.line([(pad, fy), (W - pad, fy)], fill=LINE, width=1)
    fy += 16
    d.text((pad, fy), "All groups", font=f_foot, fill=FOREST)
    total_short = float(contribution_goal.get("short", 0) or 0)
    total_label = (f"Target {_money(contribution_goal.get('goal', 0))}   "
                   f"Contributed {_money(contribution_goal.get('collected', 0))}   "
                   f"To go {_money(total_short)}")
    lw = d.textlength(total_label, font=f_foot)
    d.text((W - pad - lw, fy), total_label, font=f_foot, fill=BRASS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
