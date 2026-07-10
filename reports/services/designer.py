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
components that need Python code the designer can't supply (``designer_safe``),
unknown narratives and malformed layouts — and it is written so that NO shape
of malformed JSON (wrong types, missing keys, extra keys, non-dict entries)
can ever escape as an uncaught exception. A definition is either accepted with
a clean report, or rejected with a specific, per-section, human-readable
reason. This is a hard requirement, not a nicety: a saved definition is
re-compiled on every server start (``register_all_enabled``) and on every
render, so an exception here is not a one-time editing mistake — it recurs on
every subsequent request until fixed. See ``docs/recommendations.md`` for the
production incident (an ``AttributeError`` on a section that was a bare string
rather than an object) that made this hardening necessary.
"""
from __future__ import annotations

import dataclasses

from core import roles
from core.reporting import Filter, Report, registry
from core.reporting.components import component_registry
from core.reporting.layout import LayoutMeta
from core.reporting.narrative import narrative_registry


# Components whose first positional-ish parameter names a sub-resource we must
# validate (so a definition can't reference a missing narrative/metric).
_NARRATIVE_COMPONENT = "narrative"

_LAYOUT_FIELDS = {f.name for f in dataclasses.fields(LayoutMeta)}


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


def _get_sections(definition):
    """Sections list off a ReportDefinition (or an equivalent dict), tolerant
    of the field being absent, None, or the wrong type entirely — callers
    always get a list back (empty if unusable) and never an exception."""
    raw = definition.sections if hasattr(definition, "sections") \
        else (definition.get("sections") if isinstance(definition, dict) else None)
    return raw if isinstance(raw, list) else []


def _get_filters_raw(definition):
    raw = definition.filters if hasattr(definition, "filters") \
        else (definition.get("filters") if isinstance(definition, dict) else None)
    return raw if isinstance(raw, list) else []


def _params_schema(comp):
    meta = component_registry._meta.get(comp) or {}
    return meta.get("params_schema") or []


def _coerce_number(value):
    """Best-effort str/JSON-number -> int|float. Returns (value, error);
    error is a short human-readable reason, or None on success. A blank
    string/None coerces to (None, None) — "not supplied", not an error, so an
    optional numeric param can be left empty."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "must be a number"
    if isinstance(value, (int, float)):
        return value, None
    if isinstance(value, str):
        if not value.strip():
            return None, None
        try:
            f = float(value)
        except ValueError:
            return None, "must be a number"
        return (int(f) if f.is_integer() else f), None
    return None, "must be a number"


def validate_definition(definition):
    """Validate a ReportDefinition (or an equivalent dict) without compiling.
    Returns a list of human-readable problems (empty = valid). Used by the
    designer to block saving invalid configurations.

    Defensive by construction: every ``.get``/indexing is guarded by an
    ``isinstance`` check first, so a malformed section (a bare string, a
    number, a list, a dict missing keys, an unknown component, a component
    that needs Python code) always turns into an entry in the returned list —
    never an exception. This function must never raise.
    """
    problems = []
    sections_field = definition.sections if hasattr(definition, "sections") \
        else (definition.get("sections") if isinstance(definition, dict) else None)
    if sections_field is not None and not isinstance(sections_field, list):
        problems.append("'sections' must be a list.")
    sections = _get_sections(definition)
    if not sections:
        problems.append("The report has no sections.")

    for i, sec in enumerate(sections, 1):
        if not isinstance(sec, dict):
            problems.append(
                f"Section {i}: expected a component block, got "
                f"{type(sec).__name__} ({sec!r:.60s}). Add it from the "
                f"component palette rather than typing its name alone.")
            continue

        comp = sec.get("component")
        if not comp or not isinstance(comp, str):
            problems.append(f"Section {i}: no component specified.")
            continue
        if not component_registry.has(comp):
            problems.append(f"Section {i}: unknown component '{comp}'.")
            continue
        if not component_registry.is_designer_safe(comp):
            problems.append(
                f"Section {i}: '{comp}' isn't available in the designer "
                f"(it requires Python code that can't come from saved data).")
            continue

        raw_params = sec.get("params")
        if raw_params is not None and not isinstance(raw_params, dict):
            problems.append(f"Section {i}: 'params' must be an object.")
            raw_params = {}
        params = raw_params or {}

        for field in _params_schema(comp):
            name, label = field["name"], field.get("label", field["name"])
            value = params.get(name)
            if field.get("required") and (value is None or value == ""):
                problems.append(f"Section {i}: '{label}' is required.")
                continue
            if value is not None and field.get("kind") == "number":
                _, err = _coerce_number(value)
                if err:
                    problems.append(f"Section {i}: '{label}' {err}.")

        if comp == _NARRATIVE_COMPONENT:
            nk = params.get("narrative_key")
            if nk and narrative_registry.get(nk) is None:
                problems.append(f"Section {i}: unknown narrative '{nk}'.")

        raw_layout = sec.get("layout")
        if raw_layout is not None and not isinstance(raw_layout, dict):
            problems.append(f"Section {i}: 'layout' must be an object.")
            raw_layout = {}
        layout = raw_layout or {}

        unknown = set(layout) - _LAYOUT_FIELDS
        if unknown:
            problems.append(f"Section {i}: layout has unknown field(s): "
                            f"{', '.join(sorted(unknown))}.")
        w = layout.get("width", 12)
        if isinstance(w, bool) or not isinstance(w, int) or not (1 <= w <= 12):
            problems.append(f"Section {i}: width must be a whole number "
                            f"1..12 (got {w!r}).")
        order = layout.get("order", 100)
        if isinstance(order, bool) or not isinstance(order, int):
            problems.append(f"Section {i}: order must be a whole number "
                            f"(got {order!r}).")

    filters_raw = definition.filters if hasattr(definition, "filters") \
        else (definition.get("filters") if isinstance(definition, dict) else None)
    if filters_raw is not None and not isinstance(filters_raw, list):
        problems.append("'filters' must be a list.")
    else:
        for i, f in enumerate(_get_filters_raw(definition), 1):
            if not isinstance(f, dict) or not f.get("name"):
                problems.append(f"Filter {i}: must be an object with a 'name'.")

    return problems


def _build_section(sec):
    """Instantiate one component from a section spec, applying params + layout.
    Disabled sections return None (skipped). Raises ``DefinitionError`` — never
    a raw exception — for anything malformed; ``validate_definition`` should
    already have caught it, but this is defense-in-depth for direct callers
    (e.g. a management command) that skip validation."""
    if not isinstance(sec, dict):
        raise DefinitionError(
            f"Malformed section: expected an object, got {type(sec).__name__}.")
    if sec.get("enabled") is False:
        return None

    comp = sec.get("component")
    if not comp or not component_registry.has(comp):
        raise DefinitionError(f"Unknown component '{comp}'.")
    if not component_registry.is_designer_safe(comp):
        raise DefinitionError(f"Component '{comp}' isn't available here.")

    raw_params = sec.get("params")
    if raw_params is not None and not isinstance(raw_params, dict):
        raise DefinitionError(f"'params' for '{comp}' must be an object.")
    schema_kinds = {f["name"]: f.get("kind") for f in _params_schema(comp)}
    params = {}
    for name, value in (raw_params or {}).items():
        if schema_kinds.get(name) == "number":
            coerced, err = _coerce_number(value)
            if err:
                raise DefinitionError(f"'{name}' for '{comp}' {err}.")
            if coerced is not None:
                params[name] = coerced
        else:
            params[name] = value

    if sec.get("title"):
        params["title"] = sec["title"]

    raw_layout = sec.get("layout")
    if raw_layout:
        if not isinstance(raw_layout, dict):
            raise DefinitionError(f"'layout' for '{comp}' must be an object.")
        try:
            params["layout"] = LayoutMeta.from_dict(raw_layout)
        except TypeError as e:
            raise DefinitionError(f"Invalid layout for '{comp}': {e}") from e

    try:
        return component_registry.create(comp, **params)
    except DefinitionError:
        raise
    except Exception as e:  # noqa: BLE001 — a bad definition must never 500
        raise DefinitionError(f"Could not build '{comp}': {e}") from e


def _build_filters(definition):
    filters = []
    for f in _get_filters_raw(definition):
        if not isinstance(f, dict) or not f.get("name"):
            continue          # validate_definition already reported this
        filters.append(Filter(
            name=f["name"], label=f.get("label", f["name"]),
            kind=f.get("kind", "text"), default=f.get("default"),
            choices=f.get("choices")))
    return filters


def compile_definition(definition):
    """Compile a ReportDefinition into an engine ``Report`` (not registered).
    Raises DefinitionError if invalid — every path, including anything
    unanticipated during section construction, funnels through
    DefinitionError so a bad saved definition can never turn into a raw
    traceback for the person viewing the report."""
    problems = validate_definition(definition)
    if problems:
        raise DefinitionError("; ".join(problems))

    sections = []
    for sec in _get_sections(definition):
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
    it is reachable at /reports/r/<engine_key>/. Idempotent: replaces any existing
    compiled report for this definition."""
    report = compile_definition(definition)
    # allow re-registration by removing a prior compiled version
    registry._reports.pop(report.key, None)
    registry.register(report)
    return report


def register_all_enabled():
    """Register every enabled ReportDefinition. Called at startup and after edits
    so designed reports are available alongside the code-defined ones. A single
    broken saved definition (however it got that way) must never prevent every
    OTHER report — designed or code-defined — from starting up."""
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
        except Exception:  # noqa: BLE001 — belt-and-suspenders: compile_definition
            # already converts anticipated failures to DefinitionError, but an
            # app start-up must survive even a truly unexpected one
            from core.utils import log_exception
            log_exception("reports/services/designer.py")
            continue
    return out
