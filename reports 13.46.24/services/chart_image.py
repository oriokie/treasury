"""Small server-side chart images (PNG, base64-embeddable) for the Monthly
Treasurer's Report Word export, which — being rendered as HTML opened by
Word — can't run the JS charting library the on-screen report uses. Kept
deliberately simple: a handful of purpose-built chart builders rather than a
general charting engine."""
import base64
import io

from PIL import Image, ImageDraw, ImageFont

FOREST = (31, 95, 79)
FOREST_SOFT = (231, 240, 235)
BRASS = (176, 125, 44)
RED = (179, 38, 30)
INK = (27, 36, 32)
MUTED = (102, 119, 112)
PAPER = (255, 255, 255)


def _font(name, size):
    import os
    path = os.path.join("/usr/share/fonts/truetype/dejavu", name)
    try:
        return ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _money(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _to_data_uri(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def bar_chart(title, bars, *, width=760, bar_h=44, gap=14):
    """bars: list of (label, value, color). Horizontal bars, auto-scaled."""
    f_title = _font("DejaVuSans-Bold.ttf", 20)
    f_label = _font("DejaVuSans-Bold.ttf", 15)
    f_val = _font("DejaVuSansMono.ttf", 14)
    pad = 24
    header = 46
    h = header + len(bars) * (bar_h + gap) + pad
    img = Image.new("RGB", (width, h), PAPER)
    d = ImageDraw.Draw(img)
    d.text((pad, 14), title, font=f_title, fill=FOREST)
    max_v = max([abs(v) for _, v, _ in bars] + [1])
    track_x0 = pad
    track_x1 = width - pad
    track_w = track_x1 - track_x0
    y = header
    for label, value, color in bars:
        d.text((track_x0, y), label, font=f_label, fill=INK)
        vy = y + 20
        d.rounded_rectangle([track_x0, vy, track_x1, vy + 14], radius=7,
                            fill=FOREST_SOFT)
        w = int(track_w * min(abs(value) / max_v, 1.0)) if max_v else 0
        if w > 0:
            d.rounded_rectangle([track_x0, vy, track_x0 + max(w, 14), vy + 14],
                                radius=7, fill=color)
        label_txt = _money(value)
        lw = d.textlength(label_txt, font=f_val)
        d.text((track_x1 - lw, y), label_txt, font=f_val, fill=MUTED)
        y += bar_h + gap
    return _to_data_uri(img)


def donut_or_split(title, segments, *, width=560, height=260):
    """segments: list of (label, value, color). Simple side-by-side stacked
    bar (a donut needs more geometry than is worth it for a Word export)."""
    f_title = _font("DejaVuSans-Bold.ttf", 18)
    f_label = _font("DejaVuSans-Bold.ttf", 14)
    f_val = _font("DejaVuSansMono.ttf", 13)
    img = Image.new("RGB", (width, height), PAPER)
    d = ImageDraw.Draw(img)
    d.text((24, 14), title, font=f_title, fill=FOREST)
    total = sum(v for _, v, _ in segments) or 1
    bar_y0, bar_y1 = 60, 100
    x = 24
    bar_w = width - 48
    for label, value, color in segments:
        seg_w = int(bar_w * (value / total))
        d.rectangle([x, bar_y0, x + seg_w, bar_y1], fill=color)
        x += seg_w
    ly = 120
    for label, value, color in segments:
        d.rectangle([24, ly + 4, 40, ly + 18], fill=color)
        pct = f"{value / total * 100:.0f}%" if total else "0%"
        d.text((48, ly), f"{label}: {_money(value)} ({pct})", font=f_label, fill=INK)
        ly += 28
    return _to_data_uri(img)
