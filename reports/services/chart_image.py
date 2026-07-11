"""Small server-side chart images (PNG, base64-embeddable) for the Monthly
Treasurer's Report Word export, which — being rendered as HTML opened by
Word — can't run the JS charting library the on-screen report uses. Kept
deliberately simple: a handful of purpose-built chart builders rather than a
general charting engine.

High-DPI rendering (v2.43): drawn at SCALE× the logical dimensions below —
the same technique used in cashbook/services/goal_chart.py (see that
module's docstring for the full rationale). These charts get embedded into
PDF and Word exports, both explicitly print-oriented deliverables, so the
same "not enough source pixels once printed at A4" problem applied here too
before this change.
"""
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

# See goal_chart.py's module docstring for why 4x and why it's cheap here.
SCALE = 4
PNG_DPI = (300, 300)


def _s(n):
    return round(n * SCALE)


def _font(name, size):
    import os
    path = os.path.join("/usr/share/fonts/truetype/dejavu", name)
    try:
        return ImageFont.truetype(path, _s(size))
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _money(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _to_data_uri(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=PNG_DPI)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def bar_chart(title, bars, *, width=760, bar_h=44, gap=14):
    """bars: list of (label, value, color). Horizontal bars, auto-scaled.
    width/bar_h/gap are LOGICAL (pre-scale) pixels, as before — callers don't
    need to know about SCALE."""
    f_title = _font("DejaVuSans-Bold.ttf", 20)
    f_label = _font("DejaVuSans-Bold.ttf", 15)
    f_val = _font("DejaVuSansMono.ttf", 14)
    pad = 24
    header = 46
    h = header + len(bars) * (bar_h + gap) + pad
    img = Image.new("RGB", (_s(width), _s(h)), PAPER)
    d = ImageDraw.Draw(img)
    d.text((_s(pad), _s(14)), title, font=f_title, fill=FOREST)
    max_v = max([abs(v) for _, v, _ in bars] + [1])
    track_x0 = _s(pad)
    track_x1 = _s(width - pad)
    track_w = track_x1 - track_x0
    y = _s(header)
    for label, value, color in bars:
        d.text((track_x0, y), label, font=f_label, fill=INK)
        vy = y + _s(20)
        d.rounded_rectangle([track_x0, vy, track_x1, vy + _s(14)], radius=_s(7),
                            fill=FOREST_SOFT)
        w = int(track_w * min(abs(value) / max_v, 1.0)) if max_v else 0
        if w > 0:
            d.rounded_rectangle([track_x0, vy, track_x0 + max(w, _s(14)), vy + _s(14)],
                                radius=_s(7), fill=color)
        label_txt = _money(value)
        lw = d.textlength(label_txt, font=f_val)
        d.text((track_x1 - lw, y), label_txt, font=f_val, fill=MUTED)
        y += _s(bar_h + gap)
    return _to_data_uri(img)


def donut_or_split(title, segments, *, width=560, height=260):
    """segments: list of (label, value, color). Simple side-by-side stacked
    bar (a donut needs more geometry than is worth it for a Word export)."""
    f_title = _font("DejaVuSans-Bold.ttf", 18)
    f_label = _font("DejaVuSans-Bold.ttf", 14)
    img = Image.new("RGB", (_s(width), _s(height)), PAPER)
    d = ImageDraw.Draw(img)
    d.text((_s(24), _s(14)), title, font=f_title, fill=FOREST)
    total = sum(v for _, v, _ in segments) or 1
    bar_y0, bar_y1 = _s(60), _s(100)
    x = _s(24)
    bar_w = _s(width) - _s(48)
    for label, value, color in segments:
        seg_w = int(bar_w * (value / total))
        d.rectangle([x, bar_y0, x + seg_w, bar_y1], fill=color)
        x += seg_w
    ly = _s(120)
    for label, value, color in segments:
        d.rectangle([_s(24), ly + _s(4), _s(40), ly + _s(18)], fill=color)
        pct = f"{value / total * 100:.0f}%" if total else "0%"
        d.text((_s(48), ly), f"{label}: {_money(value)} ({pct})", font=f_label, fill=INK)
        ly += _s(28)
    return _to_data_uri(img)


def _line_chart(title, labels, series, *, width=760, height=300):
    """series: list of (label, [values], color). Simple polyline plot with a
    light horizontal grid — enough for a trend in an exported document."""
    f_title = _font("DejaVuSans-Bold.ttf", 18)
    f_axis = _font("DejaVuSans.ttf", 12)
    pad_l, pad_r, pad_t, pad_b = 70, 20, 52, 34
    img = Image.new("RGB", (_s(width), _s(height)), PAPER)
    d = ImageDraw.Draw(img)
    d.text((_s(20), _s(14)), title, font=f_title, fill=FOREST)
    all_vals = [float(v or 0) for _, vals, _ in series for v in vals] or [0.0]
    lo, hi = min(all_vals + [0.0]), max(all_vals + [0.0])
    if hi == lo:
        hi = lo + 1
    plot_w = _s(width - pad_l - pad_r)
    plot_h = _s(height - pad_t - pad_b)

    def xy(i, v):
        n = max(len(labels) - 1, 1)
        x = _s(pad_l) + plot_w * (i / n)
        y = _s(pad_t) + plot_h * (1 - (float(v or 0) - lo) / (hi - lo))
        return x, y

    for g in range(5):                       # horizontal grid + axis values
        gy = _s(pad_t) + plot_h * g / 4
        d.line([(_s(pad_l), gy), (_s(width - pad_r), gy)], fill=(232, 236, 233))
        gv = hi - (hi - lo) * g / 4
        d.text((_s(8), gy - _s(7)), _money(gv), font=f_axis, fill=MUTED)
    step = max(len(labels) // 8, 1)          # sparse x labels
    for i, lab in enumerate(labels):
        if i % step == 0:
            x, _ = xy(i, lo)
            d.text((x - _s(12), _s(height - pad_b + 6)), str(lab)[:8], font=f_axis,
                   fill=MUTED)
    for lbl, vals, color in series:
        pts = [xy(i, v) for i, v in enumerate(vals)]
        if len(pts) >= 2:
            d.line(pts, fill=color, width=_s(3))
        for p in pts:
            r = _s(3)
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)
    ly = _s(34)                                  # legend
    lx = _s(pad_l)
    for lbl, _vals, color in series:
        d.rectangle([lx, ly, lx + _s(14), ly + _s(12)], fill=color)
        d.text((lx + _s(20), ly - _s(2)), str(lbl), font=f_axis, fill=INK)
        lx += _s(24) + int(d.textlength(str(lbl), font=f_axis)) + _s(16)
    return img


def _hex_rgb(value, fallback=FOREST):
    """'#1f5f4f' -> (31, 95, 79); tolerant of lists and junk."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if not isinstance(value, str) or not value.startswith("#") or \
            len(value) not in (4, 7):
        return fallback
    v = value[1:]
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    try:
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def render_chart_config(config, title=""):
    """Render a Chart.js config dict (the engine ChartSpec.to_config output,
    i.e. what a chart SectionData carries in extra['chart']) to a PNG for the
    PDF/Word exports, which cannot run Chart.js. Returns (data_uri, png_bytes)
    or (None, None) for an empty/unrenderable config — the exports simply skip
    the chart then, exactly as before this existed (recommendation #28).

    Deliberately a faithful-enough summary, not a Chart.js clone: bar-family
    charts render as horizontal bars (first dataset, or per-label totals when
    stacked), pie/doughnut as the proportional split bar, and line-family as a
    polyline plot. Figures come straight from the config's datasets — the same
    registry-sourced numbers the on-screen chart shows.
    """
    import io as _io
    try:
        ctype = (config or {}).get("type", "")
        data = (config or {}).get("data", {})
        labels = list(data.get("labels") or [])
        datasets = list(data.get("datasets") or [])
        if not labels or not datasets:
            return None, None

        if ctype in ("pie", "doughnut"):
            vals = [float(v or 0) for v in (datasets[0].get("data") or [])]
            colors = datasets[0].get("backgroundColor") or []
            segments = []
            for i, lab in enumerate(labels[: len(vals)]):
                c = _hex_rgb(colors[i] if i < len(colors) else None,
                             (FOREST, BRASS)[i % 2])
                segments.append((str(lab), vals[i], c))
            uri = donut_or_split(title, segments)
            img_b64 = uri.split(",", 1)[1]
            import base64 as _b64
            return uri, _b64.b64decode(img_b64)

        if ctype == "line":
            series = []
            for i, ds in enumerate(datasets):
                color = _hex_rgb(ds.get("borderColor")
                                 or ds.get("backgroundColor"),
                                 (FOREST, BRASS, (107, 143, 126))[i % 3])
                series.append((ds.get("label") or f"Series {i+1}",
                               [float(v or 0) for v in (ds.get("data") or [])],
                               color))
            img = _line_chart(title, labels, series)
            buf = _io.BytesIO()
            img.save(buf, format="PNG", dpi=PNG_DPI)
            return _to_data_uri(img), buf.getvalue()

        # bar family (bar / stacked / comparison / waterfall emulation):
        # one horizontal bar per label; stacked configs sum their datasets
        bars = []
        many = len(datasets) > 1
        for i, lab in enumerate(labels):
            if many:
                total = sum(float((ds.get("data") or [0] * len(labels))[i] or 0)
                            for ds in datasets
                            if i < len(ds.get("data") or []))
                color = FOREST
            else:
                ds = datasets[0]
                vals = ds.get("data") or []
                total = float(vals[i] or 0) if i < len(vals) else 0.0
                bg = ds.get("backgroundColor")
                color = _hex_rgb(bg[i] if isinstance(bg, list) and i < len(bg)
                                 else bg, FOREST)
            bars.append((str(lab), total, color))
        uri = bar_chart(title, bars)
        import base64 as _b64
        return uri, _b64.b64decode(uri.split(",", 1)[1])
    except Exception:  # noqa: BLE001 — a chart must never break an export
        from core.utils import log_exception
        log_exception("reports/services/chart_image.py")
        return None, None
