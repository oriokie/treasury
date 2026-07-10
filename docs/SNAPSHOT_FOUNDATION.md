# Report Snapshot Foundation (Phase 7)

*The underlying architecture for immutable, versioned report snapshots. This
phase implements the foundation only — no scheduling, and no change to how
reports render today.*

---

## 1. Purpose

A snapshot is a permanent, immutable capture of what a report showed for an
accounting period at a moment in time. It is the substrate a later phase needs
for scheduled generation, board-pack archival, and version comparison. Creating
a snapshot is always explicit (via the service); nothing is captured
automatically yet.

## 2. Model — `reports.models.ReportSnapshot`

| Field | Purpose |
|---|---|
| `report_key`, `report_title` | which report |
| `period_start`, `period_end` | the accounting period |
| `generated_at`, `generated_by` | when and by whom |
| `report_version` | app VERSION at generation |
| `template_version` | engine/template version (`engine-1`) — bump when the rendered structure changes so snapshots are comparable within a version |
| `schema_version` | snapshot payload schema version |
| `payload` | the rendered report as structured JSON (sections → columns/rows/total) |
| `checksums` | `{"payload": …, "csv": …}` — integrity anchors |
| `filters`, `metrics_used`, `component_keys`, `render_meta` | provenance |
| `finalised` | once true, the record is immutable |

**Immutability** is enforced in `save()`: any attempt to re-save a finalised
snapshot raises `ValueError`. There is no edit path; a correction is a *new*
snapshot. The admin registration is read-only (no add/change; delete only for
superusers).

## 3. Service — `reports.services.snapshots`

* `create_snapshot(rendered, *, user, formats=("csv",), church, finalise=True)`
  — serialises the sections into the payload, records a **deterministic payload
  checksum** (the canonical integrity anchor), optionally records per-format
  export checksums, captures provenance from the dependency map, stamps the
  versions, and finalises.
* `verify_snapshot(snapshot, rendered)` — re-renders and compares checksums;
  returns `{"payload": bool, "csv": bool, …}`. The drift-detection primitive.

### Determinism note (important)

Only the **payload** checksum and the **CSV** export are byte-deterministic.
`xlsx`/`docx` embed a timestamp and `pdf` embeds metadata, so their bytes differ
between two identical renders — checksumming them would produce false "drift".
The service therefore:

* always records the payload checksum (structured figures, fully deterministic),
* defaults `formats=("csv",)` for the export checksum,
* documents that other formats are point-in-time copies, not equality anchors.

The payload checksum is the right integrity anchor anyway: it captures the
*figures and structure*, independent of presentation formatting.

## 4. What this enables later (not built now)

* **Scheduling** — a periodic task can call `create_snapshot` at period close.
* **Version comparison** — two snapshots of the same report/period across
  `template_version`s can be diffed via their payloads.
* **Integrity/audit** — `verify_snapshot` proves a live report still matches an
  archived board pack, or flags that the underlying data changed after sign-off.

## 5. Boundaries

* No scheduling, retention policy, or automatic capture in this phase.
* No change to any report's rendering or exports.
* One new model + migration (`reports.0001_initial`); no other schema change.
