"""Loan narration detection — a companion to the allocation engine, not
another parser. It runs on the SAME normalize_reference() output the
allocation engine uses, with the same match-type semantics, from
database-driven, configurable patterns (LoanNarrationPattern).

The importer / webhook consult detect_loan() on each credit BEFORE ordinary
allocation, so 'LOAN DEV' becomes a loan receipt instead of development
income; anything ambiguous falls through to the normal path and, ultimately,
the review queue — never a silent guess.
"""
from django.core.cache import cache

from giving.services.allocation import normalize_reference

CACHE_KEY = "loan_narration_patterns_v1"


def clear_pattern_cache():
    cache.delete(CACHE_KEY)


def _patterns():
    """Active patterns, cached (invalidated on save/delete via signals)."""
    pats = cache.get(CACHE_KEY)
    if pats is None:
        from loans.models import LoanNarrationPattern
        pats = list(LoanNarrationPattern.objects.filter(active=True)
                    .select_related("fund"))
        cache.set(CACHE_KEY, pats, 300)
    return pats


# Longest/most-specific pattern first, and money-out / retirement kinds
# before RECEIPT — 'loanrepayment' contains 'loan', so the plain receipt
# alias must never shadow the more specific intent.
_KIND_ORDER = {"CONVERSION": 0, "INTEREST": 1, "REPAYMENT": 2, "RECEIPT": 3}


def detect_loan(reference):
    """Return the best-matching LoanNarrationPattern for a reference, or None."""
    s = normalize_reference(reference)
    if not s:
        return None
    hits = [p for p in _patterns() if p.matches(s)]
    if not hits:
        return None
    hits.sort(key=lambda p: (_KIND_ORDER.get(p.kind, 9),
                             -len(p.pattern or ""), p.pk or 0))
    return hits[0]


# The aliases suggested in the requirement, normalised. Installed by the data
# migration; all editable/deactivatable on the patterns page afterwards.
SEED_PATTERNS = [
    # money in
    ("loan",            "RECEIPT"),
    ("memberloan",      "RECEIPT"),
    ("churchloan",      "RECEIPT"),
    ("developmentloan", "RECEIPT"),
    ("devloan",         "RECEIPT"),
    ("loandev",         "RECEIPT"),
    ("loandevelopment", "RECEIPT"),
    ("lending",         "RECEIPT"),
    # money out
    ("loanrepayment",   "REPAYMENT"),
    ("repayloan",       "REPAYMENT"),
    ("loanrefund",      "REPAYMENT"),
    ("loanreturn",      "REPAYMENT"),
    ("loaninterest",    "INTEREST"),
    ("loanint",         "INTEREST"),
    # retirement
    ("loandonation",    "CONVERSION"),
    ("convertloan",     "CONVERSION"),
    ("loangift",        "CONVERSION"),
    ("loanforgive",     "CONVERSION"),
]


def seed_default_patterns():
    from loans.models import LoanNarrationPattern
    created = 0
    for pattern, kind in SEED_PATTERNS:
        _, was_created = LoanNarrationPattern.objects.get_or_create(
            pattern=pattern, kind=kind,
            defaults={"match_type": LoanNarrationPattern.MatchType.CONTAINS,
                      "seeded": True})
        created += 1 if was_created else 0
    clear_pattern_cache()
    return created
