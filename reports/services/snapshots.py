"""Report snapshot service — turn a rendered engine report into an immutable
snapshot.

This is the *foundation* for a later scheduling/versioning phase: it can capture
any report registered on the Generic Report Engine, together with its payload,
per-format export checksums and provenance metadata, and finalise it as an
immutable record. It does not schedule anything and does not change how reports
render.
"""
from __future__ import annotations

from core.version import get_version


# Template/engine version — bump when the engine's rendered-payload structure
# changes in a way that makes older snapshots structurally incomparable.
ENGINE_TEMPLATE_VERSION = "engine-1"
SNAPSHOT_SCHEMA_VERSION = 1


def _serialise_sections(rendered):
    """Flatten a RenderedReport's sections into JSON-safe structured data —
    the immutable payload. Numbers become strings to preserve Decimal exactness
    across JSON."""
    out = []
    for s in rendered.sections:
        rows = []
        for r in s.rows:
            rows.append({k: (str(v) if _is_num(v) else v)
                         for k, v in r.cells.items()})
        total = None
        if s.total is not None:
            total = {k: (str(v) if _is_num(v) else v)
                     for k, v in s.total.cells.items()}
        out.append({
            "key": s.key, "title": s.title, "kind": s.kind,
            "columns": [{"key": c.key, "label": c.label, "numeric": c.numeric}
                        for c in s.columns],
            "rows": rows, "total": total, "note": s.note,
        })
    return out


def _is_num(v):
    from decimal import Decimal
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def create_snapshot(rendered, *, user=None, formats=("csv",),
                    church=None, finalise=True):
    """Create (and by default finalise) an immutable ReportSnapshot from a
    RenderedReport.

    * Serialises the sections as the payload, and records a checksum of that
      payload — the **canonical, deterministic** integrity anchor (structured
      figures, independent of export formatting).
    * Renders each requested export format and records its checksum. NOTE: only
      deterministic formats should be checksummed for drift detection — ``csv``
      is byte-stable, but ``xlsx``/``docx`` embed timestamps and ``pdf`` embeds
      metadata, so their bytes vary between identical renders. ``formats``
      therefore defaults to ``("csv",)``; pass others only for a point-in-time
      copy, not for equality checks.
    * Captures provenance: filters, metrics used (from the dependency map),
      component keys, and the report/template/app versions.
    """
    import json
    from reports.models import ReportSnapshot, compute_checksum
    from core.reporting import build_dependency_map, renderer_registry

    dep = build_dependency_map(rendered)
    ctx = rendered.context
    payload = {"sections": _serialise_sections(rendered)}

    snap = ReportSnapshot(
        report_key=rendered.report.key,
        report_title=rendered.report.title,
        period_start=ctx.start, period_end=ctx.end,
        generated_by=user if (user and getattr(user, "pk", None)) else None,
        report_version=get_version(),
        template_version=ENGINE_TEMPLATE_VERSION,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        payload=payload,
        filters={k: (v.isoformat() if hasattr(v, "isoformat") else v)
                 for k, v in (rendered.filters or {}).items()},
        metrics_used=dep.all_metrics(),
        component_keys=[s.key for s in rendered.sections],
        render_meta={"services": dep.all_services()},
    )
    # canonical payload checksum — deterministic, the real integrity anchor
    snap.checksums["payload"] = compute_checksum(
        json.dumps(payload, sort_keys=True, default=str))
    # optional per-format checksums (csv is deterministic; others are point-in-time)
    for fmt in formats:
        renderer = renderer_registry.get(fmt)
        if renderer is None:
            continue
        resp = renderer.render(rendered, church=church)
        content = getattr(resp, "content", None)
        if content is not None:
            snap.add_checksum(fmt, content)
    if finalise:
        snap.finalised = True
    snap.save()
    return snap


def verify_snapshot(snapshot, rendered, *, formats=None, church=None):
    """Re-render ``rendered`` and check stored checksums still match — the
    drift-detection primitive. The ``payload`` checksum is the authoritative
    check (deterministic); format checksums are only meaningful for
    deterministic formats (csv). Returns {name: bool}."""
    import json
    from reports.models import compute_checksum
    from core.reporting import renderer_registry
    result = {}
    if "payload" in snapshot.checksums:
        payload = {"sections": _serialise_sections(rendered)}
        result["payload"] = snapshot.matches(
            "payload", json.dumps(payload, sort_keys=True, default=str))
    fmts = formats if formats is not None else [
        f for f in snapshot.checksums if f != "payload"]
    for fmt in fmts:
        renderer = renderer_registry.get(fmt)
        if renderer is None:
            result[fmt] = False
            continue
        resp = renderer.render(rendered, church=church)
        content = getattr(resp, "content", b"")
        result[fmt] = snapshot.matches(fmt, content)
    return result
