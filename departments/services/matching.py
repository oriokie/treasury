"""Match free-text / narration hints to existing DevelopmentGroup rows.

Never creates groups — unknown or ambiguous hints return None so the gift
stays on the parent Development fund (unassigned) for a treasurer to tag.
"""
import re


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").strip().lower())


def match_existing_dev_group(hint):
    """Resolve ``hint`` to an active DevelopmentGroup, or None.

    Accepts a bare number (``12``, ``G12``, ``Group 12``, ``DEVGRP12``), a
    fragment of the group's name (contains), or a campaign sheet group string.
    Ambiguous name matches return None rather than guessing.
    """
    from departments.models import DevelopmentGroup

    raw = (hint if hint is not None else "")
    if isinstance(raw, int):
        return (DevelopmentGroup.objects
                .filter(active=True, number=int(raw)).first())
    raw = str(raw).strip()
    if not raw:
        return None

    groups = list(DevelopmentGroup.objects.filter(active=True))
    if not groups:
        return None

    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        try:
            n = int(digits)
        except ValueError:
            n = None
        if n is not None:
            by_num = [g for g in groups if g.number == n]
            if by_num:
                # Number wins when the hint is clearly about that group number
                # (exact digits, or a "group/grp/dev…" prefix around them).
                compact = _norm(raw)
                if (compact == str(n)
                        or compact.endswith(str(n))
                        or re.fullmatch(
                            rf"(?:dev(?:e?l?o?p?)?(?:gr(?:ou)?p?|gp|g)?|"
                            rf"gr(?:ou)?p|gp|g)?0*{n}", compact)):
                    return by_num[0]

    needle = _norm(raw)
    if len(needle) < 2:
        return None

    hits = []
    for g in groups:
        hay = _norm(f"{g.name or ''} group {g.number}")
        if needle in hay or str(g.number) == needle:
            hits.append(g)
    if len(hits) == 1:
        return hits[0]
    if digits:
        try:
            n = int(digits)
        except ValueError:
            n = None
        if n is not None:
            numbered = [g for g in hits if g.number == n]
            if len(numbered) == 1:
                return numbered[0]
    return None
