"""Report Designer compiler — turn a persisted ``ReportDefinition`` into a live
Generic Report Engine ``Report``.

This is the bridge that makes the platform configuration-driven: an
administrator arranges *registered* components as data (a JSON section list with
per-section params + LayoutMeta), and this service compiles that into exactly the
same kind of ``Report`` object the code-defined reports produce — so it renders
through the identical engine, renderers, narrative engine and snapshot pipeline,
with the same permissions and exports.

Crucially, a definition can only *arrange registered components*; it cannot
introduce a calculation. Accounting still flows solely through the Financial
Metrics Registry via ReportContext. Validation refuses unknown components,
unknown narratives and malformed layouts, so an administrator cannot save a
configuration that would render incorrectly.
"""
from __future__ import annotations

from core import roles
from core.reporting import Filter, Report, registry
from core.reporting.components import component_registry
from core.reporting.layout import LayoutMeta
from core.reporting.narrative import narrative_registry


# Components whose first positional-ish parameter names a sub-resource we must
# validate (so a definition can't reference a missing narrative/metric).
_NARRATIVE_COMPONENT = "narrative"


class DefinitionError(ValueError):
    """Raised when a report definition is invalid (unknown component, bad
    layout, missing narrative, …). Surfaced to the designer UI as a clear
    message so invalid configurations cannot be saved or rendered."""


def _permission_fn(permission):
    """Map a definition's permission choice to a callable, mirroring the
    existing report access rules."""
    def _reports(user):
        from core.rights import has_right
        return roles.is_staff_role(user) or has_right(user, "view_reports")

    def _treasurer(user):
        return roles.is_treasurer(user) if hasattr(roles, "is_treasurer") \
            else roles.is_staff_role(user)

    def _admin(user):
        return bool(getattr(user, "is_staff", False) or
                    getattr(user, "is_superuser", False))

    return {"reports": _reports, "treasurer": _treasurer,
            "admin": _admin}.get(permission, _reports)


def validate_definition(definition):
    """Validate a ReportDefinition (or an equivalent dict) without compiling.
    Returns a list of human-readable problems (empty = valid). Used by the
    designer to block saving invalid configurations."""
    problems = []
    sections = definition.sections if hasattr(definition, "sections") \
        else definition.get("sections", [])
    if not sections:
        problems.append("The report has no sections.")
    for i, sec in enumerate(sections, 1):
        comp = sec.get("component")
        if not comp:
            problems.append(f"Section {i}: no component specified.")
            continue
        if not component_registry.has(comp):
            problems.append(f"Section {i}: unknown component '{comp}'.")
            continue
        params = sec.get("params") or {}
        if comp == _NARRATIVE_COMPONENT:
            nk = params.get("narrative_key")
            if not nk:
                problems.append(f"Section {i}: narrative needs a narrative_key.")
            elif narrative_registry.get(nk) is None:
                problems.append(f"Section {i}: unknown narrative '{nk}'.")
        layout = sec.get("layout") or {}
        w = layout.get("width", 12)
        if not isinstance(w, int) or not (1 <= w <= 12):
            problems.append(f"Section {i}: width must be 1..12 (got {w!r}).")
    return problems


def _build_section(sec):
    """Instantiate one component from a section spec, applying params + layout.
    Disabled sections return None (skipped)."""
    if sec.get("enabled") is False:
        return None
    comp = sec["component"]
    params = dict(sec.get("params") or {})
    if sec.get("title"):
        params["title"] = sec["title"]
    layout = sec.get("layout")
    if layout:
        params["layout"] = LayoutMeta.from_dict(layout)
    return component_registry.create(comp, **params)


def _build_filters(definition):
    filters = []
    raw = definition.filters if hasattr(definition, "filters") \
        else definition.get("filters", [])
    for f in raw or []:
        filters.append(Filter(
            name=f["name"], label=f.get("label", f["name"]),
            kind=f.get("kind", "text"), default=f.get("default"),
            choices=f.get("choices")))
    return filters


def compile_definition(definition):
    """Compile a ReportDefinition into an engine ``Report`` (not registered).
    Raises DefinitionError if invalid."""
    problems = validate_definition(definition)
    if problems:
        raise DefinitionError("; ".join(problems))

    sections = []
    for sec in (definition.sections if hasattr(definition, "sections")
                else definition["sections"]):
        built = _build_section(sec)
        if built is not None:
            sections.append(built)

    key = definition.engine_key if hasattr(definition, "engine_key") \
        else f"def__{definition['key']}"
    perm = _permission_fn(getattr(definition, "permission", "reports"))
    return Report(
        key=key,
        title=getattr(definition, "title", None) or definition.get("title", key),
        description=getattr(definition, "description", "") or "",
        category=getattr(definition, "category", "Custom") or "Custom",
        permission=perm,
        filters=_build_filters(definition),
        sections=sections,
    )


def register_definition(definition):
    """Compile and (re)register a definition's report in the engine registry so
    it is reachable at /reports/d/<key>/. Idempotent: replaces any existing
    compiled report for this definition."""
    report = compile_definition(definition)
    # allow re-registration by removing a prior compiled version
    registry._reports.pop(report.key, None)
    registry.register(report)
    return report


def register_all_enabled():
    """Register every enabled ReportDefinition. Called at startup and after edits
    so designed reports are available alongside the code-defined ones."""
    from reports.models import ReportDefinition
    out = []
    try:
        defs = list(ReportDefinition.objects.filter(enabled=True))
    except Exception:  # noqa: BLE001 — DB may not be ready at import time
        return out
    for d in defs:
        try:
            out.append(register_definition(d))
        except DefinitionError:
            # a broken saved definition must never break startup; skip it
            continue
    return out
