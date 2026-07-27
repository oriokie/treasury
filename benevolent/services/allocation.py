"""Intelligent allocation.

A receipt arrives from the bank: an amount, a date, a phone number, a name as the
bank spelled it, and a narration a member typed on a phone keypad. Somewhere in
that is the answer to three questions:

    WHICH SCHEME is this money for?
    WHOSE money is it?
    WHAT KIND of money is it — dues, a levy, a registration fee?

This module answers them, and says how sure it is.

Three principles, and they are the same three that govern the policy engine —
because they are the right ones.

**It shows its working.** The allocator never returns a bare answer. It returns
every candidate it considered, every SIGNAL that fired for each, and the score
those signals produced. A treasurer resolving a queue item can see what the machine
thought and why; and when an automatic allocation turns out to be wrong, it can be
UNDERSTOOD rather than merely undone. A confidently-wrong allocation that nobody can
explain is the worst thing this module could produce.

**It is allowed to fail, and it must never lose the money.** A receipt whose owner
cannot be identified is still receipted, still in the scheme's fund, still in the
general ledger. Allocation failing costs a treasurer two minutes in a queue.
Allocation refusing to bank the money would cost the church a fund balance that
disagrees with the bank.

**One signal is never enough.** A name match alone must not attribute money, because
two brothers share a surname. A phone match alone is strong but not conclusive,
because families share handsets. The score is designed so that a single
weak-to-medium signal cannot clear the auto-allocation threshold on its own — it
takes corroboration, which is exactly how a careful treasurer works.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Optional

from django.db.models import Q

from benevolent.models import (BenevolentCase, BenevolentContribution, BenevolentScheme,
                               BenevolentSettings, ContributionRule, SchemeDependant,
                               SchemeMembership)
from members.models import normalize_phone


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
#
# The weights are the judgement in this module, so they are stated in one place
# rather than scattered through the code, and each one has a reason.

WEIGHTS = {
    # Conclusive: the member typed their own identifier. Nothing else identifies a
    # person this precisely, and no two members share one.
    "membership_number": 70,
    "case_number":       55,   # identifies the CASE conclusively; the member less so
    "household_id":      45,

    # Strong: the money came from a number we hold for this member. Not conclusive
    # — families share handsets, and a phone can be reassigned — but it is the best
    # everyday evidence there is.
    "member_phone":      55,
    # A second number carries the same weight as the first. "Primary" only
    # decides which number a receipt is sent to; both were put on the record by
    # a treasurer saying "this is also her line", and the member matcher has
    # always treated them as equally conclusive when identifying the person.
    # Scoring it lower meant a member paying from their M-Pesa line instead of
    # their airtime line landed in the queue every time even when the name
    # matched too (45 + 30 = 75, short of the 85 gate) — which is exactly the
    # case this signal exists for.
    "member_alt_phone":  55,

    # A spouse or grown child paying the member's dues from their OWN phone is
    # completely routine, and a system that cannot recognise it will send a
    # perfectly ordinary payment to an unmatched queue every single month.
    "spouse_phone":      45,
    "dependant_phone":   35,

    # Corroborating, never conclusive. A name alone must not attribute money: two
    # brothers share a surname, and Kenyan bank narrations abbreviate and reorder
    # names freely.
    "name_exact":        30,
    "name_fuzzy":        20,

    # The name on the bank record is a member of the household rather than the
    # member themselves. When a spouse pays, the narration carries THEIR name,
    # so matching only against members leaves the household's own evidence
    # unused: the phone said "this household" and the name agreed, and the
    # allocator could see only half of it. Weighted so that a spouse paying from
    # their own phone under their own name (45 + 40) reaches the same confidence
    # as a member paying from theirs under theirs (55 + 30) — which is the same
    # quality of evidence, arriving by a different route.
    "spouse_name":       40,
    "dependant_name":    30,

    # The narration says which scheme, and often what kind of money. It identifies
    # the SCHEME well and the MEMBER not at all.
    "rule_scheme":       25,
    "keyword_kind":      10,

    # The amount matching exactly what this member owes right now is real evidence
    # — but only ever supporting evidence, because a hundred members owe the same
    # 200 shillings.
    "amount_dues":       12,
    "amount_levy":       12,
    "amount_fee":        10,

    # A single member enrolled in exactly one scheme, receiving money on a fund
    # that only one scheme uses: weak, but it narrows things.
    "sole_scheme":        8,
}

CEILING = 100


@dataclass
class Signal:
    code: str
    label: str
    weight: int
    detail: str = ""

    def as_dict(self):
        return asdict(self)


# Signal codes that establish WHO the payer is. Everything else — chiefly the
# amount matching an obligation — CORROBORATES what the money is for, but says
# nothing about identity: a hundred members owe the same 500, so an amount match
# must never, on its own, turn "not sure who" into a confident auto-allocation.
# The auto-allocate threshold is checked against the identity score alone.
_IDENTITY_SIGNALS = {
    "membership_number", "case_number", "household_id",
    "spouse_name", "dependant_name",
    "member_phone", "member_alt_phone", "spouse_phone", "dependant_phone",
    "name_exact", "name_fuzzy",
}


@dataclass
class Candidate:
    """One possible answer, with everything that argued for it."""
    scheme_id: Optional[int] = None
    scheme_code: str = ""
    membership_id: Optional[int] = None
    membership_number: str = ""
    member_name: str = ""
    case_id: Optional[int] = None
    case_number: str = ""
    kind: str = ""
    signals: list = field(default_factory=list)

    @property
    def score(self):
        """Capped at 100. Signals ADD, so corroboration is what produces
        confidence — which is the intended shape: no single medium signal can
        reach the auto threshold alone."""
        return min(CEILING, sum(s.weight for s in self.signals))

    @property
    def identity_score(self):
        """The score from IDENTITY evidence only — who the payer is, ignoring
        obligation-amount corroboration. This is what the auto-allocate gate
        checks: an amount that matches what this member owes is real support for
        the money's PURPOSE, but a hundred members owe exactly 500, so it must
        not be what lifts a name-only guess over the threshold and posts money to
        the wrong person automatically."""
        return min(CEILING, sum(s.weight for s in self.signals
                                if s.code in _IDENTITY_SIGNALS))

    def as_dict(self):
        return {
            "scheme_id": self.scheme_id, "scheme_code": self.scheme_code,
            "membership_id": self.membership_id,
            "membership_number": self.membership_number,
            "member_name": self.member_name,
            "case_id": self.case_id, "case_number": self.case_number,
            "kind": self.kind, "score": self.score,
            "signals": [s.as_dict() for s in self.signals],
        }


@dataclass
class AllocationResult:
    candidates: list = field(default_factory=list)     # ranked, best first
    scheme: Optional[BenevolentScheme] = None
    kind: str = ""
    duplicate_of: Optional[BenevolentContribution] = None
    notes: list = field(default_factory=list)

    @property
    def best(self):
        return self.candidates[0] if self.candidates else None

    @property
    def confidence(self):
        return self.best.score if self.best else 0

    @property
    def identity_confidence(self):
        """The best candidate's IDENTITY score — what the auto-allocate gate
        should use, so an obligation-amount match cannot on its own push a
        name-only guess over the threshold (a hundred members owe the same 500)."""
        return self.best.identity_score if self.best else 0

    @property
    def runner_up(self):
        return self.candidates[1] if len(self.candidates) > 1 else None

    @property
    def is_ambiguous(self):
        """Two candidates within a whisker of each other is NOT confidence, however
        high the top score. Two brothers, one phone, one surname: the allocator
        must say "I cannot tell these apart" rather than pick the one that happened
        to sort first — that is precisely the situation where a wrong automatic
        answer is most likely and least likely to be noticed."""
        if self.payer_is_also_a_member:
            return True
        if not self.runner_up:
            return False
        return (self.best.score - self.runner_up.score) < 15

    @property
    def payer_is_also_a_member(self):
        """The payer is in someone's household AND enrolled in their own right.

        A wife listed as her husband's spouse may also hold her own membership.
        A payment in her name from her phone then has two perfectly good
        readings — her own dues, or her paying his — and the household evidence
        happens to score higher because it draws on two signals rather than one.

        Picking the husband would be a guess dressed as a decision, and the
        money would be credited to the wrong person's record while both
        memberships looked fine. The margin test does not catch it, because the
        two readings are not close: they are simply both true. So this is
        reported as ambiguous whatever the gap, and a treasurer decides.
        """
        if not self.best:
            return False
        # Compared on the normalised name key, not the raw string: a Member's
        # name is normalised when it is saved while a dependant's is stored as
        # it was typed, so "Abigael Omoche" and "ABIGAEL OMOCHE" are the same
        # person written twice and a plain comparison would miss it.
        household = {name_key(sig.detail.split(" (")[0])
                     for sig in self.best.signals
                     if sig.code in ("spouse_name", "dependant_name",
                                     "spouse_phone", "dependant_phone")}
        household.discard("")
        if not household:
            return False
        for other in self.candidates[1:]:
            if other.membership_id == self.best.membership_id:
                continue
            for sig in other.signals:
                if sig.code in ("name_exact", "member_phone", "member_alt_phone"):
                    if name_key(sig.detail.split(" (")[0]) in household:
                        return True
        return False

    def as_dict(self):
        return {"confidence": self.confidence, "ambiguous": self.is_ambiguous,
                "notes": list(self.notes),
                "candidates": [c.as_dict() for c in self.candidates[:6]]}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalise(text):
    """The SAME normalisation the main allocation engine uses, so a church that has
    learned how its members write things does not have to teach it twice."""
    return re.sub(r"\s+", "", (text or "").strip().lower())


def name_key(raw):
    """Order-insensitive: 'RUTH MOMANYI' and 'MOMANYI RUTH' are the same person.
    Reuses the members module's convention rather than inventing a second one."""
    from members.models import name_key as _nk
    return _nk(raw)


def fuzzy(a, b):
    """0–100 similarity. Uses difflib, which is in the standard library — a fuzzy
    matcher is not worth a new dependency and a new supply-chain risk."""
    if not a or not b:
        return 0
    return int(100 * difflib.SequenceMatcher(None, a, b).ratio())


def _find_membership_number(text, scheme=None):
    """A membership number, if the member typed one. Conclusive when found."""
    if not text:
        return None
    qs = SchemeMembership.objects.select_related("member", "scheme")
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    up = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    for m in qs:
        if re.sub(r"[^A-Z0-9]", "", m.number.upper()) in up:
            return m
    return None


def _find_case_number(text, scheme=None):
    if not text:
        return None
    qs = BenevolentCase.objects.select_related("scheme", "membership__member")
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    up = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    for c in qs:
        if re.sub(r"[^A-Z0-9]", "", c.number.upper()) in up:
            return c
    return None


def _find_household(text, scheme=None):
    """A household identifier — the household's name, as the registration records
    it. A wife paying "OTIENO HOUSEHOLD" is telling us exactly which registration
    she means."""
    key = normalise(text)
    if not key or len(key) < 4:
        return None
    qs = SchemeMembership.objects.exclude(household_name="").select_related("member")
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    for m in qs:
        h = normalise(m.household_name)
        if h and len(h) >= 4 and h in key:
            return m
    return None


# ---------------------------------------------------------------------------
# The scheme, and the kind of money
# ---------------------------------------------------------------------------

def detect_scheme(reference, fund=None):
    """Which scheme is this money for? Answered by a configurable RULE, or — when
    the money has landed on a fund only one scheme uses — by the fund itself.

    Returns (scheme, kind, signal) where kind may be "" (unknown, and that is fine:
    the allocator has better evidence for the kind further down).
    """
    norm = normalise(reference)
    best = None
    for rule in ContributionRule.objects.filter(active=True).select_related("scheme"):
        if rule.matches(norm):
            if best is None or rule.priority > best.priority:
                best = rule
    if best is not None:
        return best.scheme, (best.kind or ""), Signal(
            "rule_scheme", "Narration matched a scheme rule", WEIGHTS["rule_scheme"],
            f"'{best.pattern}' → {best.scheme.code}")

    if fund is not None:
        schemes = list(BenevolentScheme.objects.filter(fund=fund).exclude(
            status=BenevolentScheme.Status.DRAFT))
        if len(schemes) == 1:
            return schemes[0], "", Signal(
                "sole_scheme", "The fund belongs to exactly one scheme",
                WEIGHTS["sole_scheme"], f"{fund.name} → {schemes[0].code}")
    return None, "", None


# keywords a member actually types on a phone keypad. Order matters: the more
# specific ones must be tested first, or "reg" would swallow "regfee".
KIND_KEYWORDS = [
    (BenevolentContribution.Kind.LEVY, ["levy", "harambee", "contribution", "case"]),
    (BenevolentContribution.Kind.REGISTRATION, ["registration", "regfee", "joining",
                                                "join", "reg"]),
    (BenevolentContribution.Kind.RENEWAL, ["renewal", "renew", "subscription", "sub"]),
    (BenevolentContribution.Kind.PENALTY, ["penalty", "fine"]),
    (BenevolentContribution.Kind.DUES, ["dues", "monthly", "subs"]),
    (BenevolentContribution.Kind.DONATION, ["donation", "gift", "offering"]),
]


def detect_kind(reference):
    norm = normalise(reference)
    for kind, words in KIND_KEYWORDS:
        for w in words:
            if w in norm:
                return kind, Signal("keyword_kind", "Narration named the kind of money",
                                    WEIGHTS["keyword_kind"], f"'{w}' → {kind}")
    return "", None


# ---------------------------------------------------------------------------
# The allocator
# ---------------------------------------------------------------------------

def allocate(*, reference="", phone="", name="", amount=None, date=None,
             fund=None, scheme=None) -> AllocationResult:
    """Work out whose money this is, and how sure we are.

    Every piece of evidence is a signal with a weight; a candidate's score is the
    sum of its signals. Corroboration is what produces confidence — no single
    medium signal reaches the auto-allocation threshold on its own, which is
    deliberate.
    """
    cfg = BenevolentSettings.get()
    result = AllocationResult()

    # --- 1. which scheme? -------------------------------------------------
    scheme_signal = None
    kind_from_rule = ""
    if scheme is None:
        scheme, kind_from_rule, scheme_signal = detect_scheme(reference, fund)
    result.scheme = scheme
    if scheme is None:
        result.notes.append(
            "No rule or fund identifies this as scheme money. Add a narration rule "
            "if it is.")
        return result

    # --- 2. what kind of money? -------------------------------------------
    kind, kind_signal = detect_kind(reference)
    kind = kind or kind_from_rule
    result.kind = kind

    # --- 3. who? Gather candidates, each with its signals ------------------
    #
    # A dict keyed by membership id, so several signals pointing at the same person
    # ACCUMULATE into one confident candidate rather than competing as several
    # weak ones. That accumulation is the whole idea.
    by_membership = {}

    def candidate_for(m):
        if m.pk not in by_membership:
            by_membership[m.pk] = Candidate(
                scheme_id=scheme.pk, scheme_code=scheme.code,
                membership_id=m.pk, membership_number=m.number,
                member_name=m.member.name, kind=kind)
            if scheme_signal is not None:
                by_membership[m.pk].signals.append(scheme_signal)
            if kind_signal is not None:
                by_membership[m.pk].signals.append(kind_signal)
        return by_membership[m.pk]

    haystack = f"{reference} {name}"

    # --- membership number: conclusive ------------------------------------
    m = _find_membership_number(haystack, scheme)
    if m is not None:
        candidate_for(m).signals.append(Signal(
            "membership_number", "The membership number was in the narration",
            WEIGHTS["membership_number"], m.number))

    # --- case number: conclusive about the CASE ---------------------------
    case = _find_case_number(haystack, scheme)
    if case is not None:
        # money quoting a case number is a levy for that case — that is the only
        # thing it can sensibly be
        result.kind = kind = BenevolentContribution.Kind.LEVY
        for c in by_membership.values():
            c.case_id, c.case_number, c.kind = case.pk, case.number, kind

    # --- household identifier ---------------------------------------------
    hh = _find_household(haystack, scheme)
    if hh is not None:
        candidate_for(hh).signals.append(Signal(
            "household_id", "The household was named in the narration",
            WEIGHTS["household_id"], hh.household_name))

    # --- phone numbers ----------------------------------------------------
    ph = normalize_phone(phone) or (phone or "").strip()
    if ph:
        _phone_signals(scheme, ph, candidate_for)

    # --- names: corroborating, never conclusive ---------------------------
    if name:
        _name_signals(scheme, name, candidate_for, cfg)
        _dependant_name_signals(scheme, name, candidate_for, cfg)

    # --- the amount corroborates ------------------------------------------
    if amount is not None:
        for cand in list(by_membership.values()):
            _amount_signals(cand, scheme, Decimal(amount), case, date)

    # --- attach the case to every candidate, and rank ----------------------
    for c in by_membership.values():
        if case is not None:
            c.case_id, c.case_number = case.pk, case.number
        c.kind = c.kind or kind

    result.candidates = sorted(by_membership.values(), key=lambda c: -c.score)

    if not result.candidates:
        result.notes.append(
            "The scheme is clear, but nothing identifies the member — no known phone, "
            "no membership number, no recognisable name.")
    elif result.is_ambiguous:
        result.notes.append(
            f"Two candidates score within {result.best.score - result.runner_up.score} "
            f"of each other ({result.best.member_name} and "
            f"{result.runner_up.member_name}). This is not confidence — it is the "
            f"allocator saying it cannot tell them apart.")

    # --- duplicate detection ----------------------------------------------
    if result.best and amount is not None and date is not None:
        result.duplicate_of = find_duplicate(
            scheme, result.best.membership_id, Decimal(amount), date, cfg)
        if result.duplicate_of is not None:
            result.notes.append(
                f"{result.best.member_name} already paid {amount} to {scheme.code} on "
                f"{result.duplicate_of.date:%d %b %Y}. This may be the same money "
                f"counted twice.")
    return result


def _phone_forms(ph):
    """Every way the same Kenyan number is written, so a stored one can match.

    "254722123456", "0722123456", "722123456" and "+254722123456" are one
    telephone. Records typed by hand or imported from a spreadsheet carry
    whichever form the church happened to use.
    """
    digits = "".join(c for c in (ph or "") if c.isdigit())
    if not digits:
        return []
    if digits.startswith("254") and len(digits) == 12:
        local = "0" + digits[3:]
    elif digits.startswith("0") and len(digits) == 10:
        local, digits = digits, "254" + digits[1:]
    else:
        return [ph, digits]
    return list({ph, digits, local, local[1:], "+" + digits})


def _phone_signals(scheme, ph, candidate_for):
    """The member's own number, their other numbers, and — routinely — a spouse's.

    A spouse or grown child paying the member's dues from their own handset is
    completely ordinary. A system that cannot recognise it drops a perfectly normal
    payment into an unmatched queue every single month, and a treasurer stops
    trusting the queue.
    """
    from members.models import Member

    # the member's own primary number
    for m in SchemeMembership.objects.filter(
            scheme=scheme, member__phone=ph).select_related("member"):
        candidate_for(m).signals.append(Signal(
            "member_phone", "Paid from the member's own number",
            WEIGHTS["member_phone"], ph))

    # other numbers recorded for the same member — a second line, an M-Pesa
    # number, or a number kept when two records for one person were merged.
    #
    # This queried `phones__phone`; the field is `number`, so it raised a
    # FieldError on every single payment. The `except Exception` around it had
    # been written to tolerate the table not existing, and instead swallowed a
    # plain misspelling — so a member paying from their second line was never
    # once recognised, silently, for as long as the feature has existed. The
    # handler is gone: if this breaks again it should be loud.
    for mem in Member.objects.filter(
            phones__number__in=_phone_forms(ph)).distinct():
        for m in SchemeMembership.objects.filter(
                scheme=scheme, member=mem).select_related("member"):
            cand = candidate_for(m)
            if not any(sig.code == "member_phone" for sig in cand.signals):
                cand.signals.append(Signal(
                    "member_alt_phone",
                    "Paid from another number held for the member",
                    WEIGHTS["member_alt_phone"], ph))

    # a spouse or dependant paying on the member's behalf
    #
    # Matched on every written form of the number, not just the normalised one.
    # A Member's phone is normalised when it is saved; a dependant's is stored
    # exactly as it was typed or imported, which for a Kenyan roster means
    # "0722..." while the bank sends "254722...". Comparing the two directly
    # never matched, so spouse recognition worked only for the handful of rows
    # that happened to have been entered in international form — which is why
    # a spouse's payment still went to the unmatched queue.
    for d in SchemeDependant.objects.filter(
            membership__scheme=scheme, active=True, phone__in=_phone_forms(ph)
    ).select_related("membership__member", "member"):
        spouse = d.relationship == SchemeDependant.Relationship.SPOUSE
        candidate_for(d.membership).signals.append(Signal(
            "spouse_phone" if spouse else "dependant_phone",
            f"Paid from the {d.get_relationship_display().lower()}'s number",
            WEIGHTS["spouse_phone" if spouse else "dependant_phone"],
            f"{d.display_name} ({ph})"))

    # a dependant who is themselves a church member, paying from their member number
    for d in SchemeDependant.objects.filter(
            membership__scheme=scheme, active=True, member__phone=ph
    ).select_related("membership__member", "member"):
        spouse = d.relationship == SchemeDependant.Relationship.SPOUSE
        cand = candidate_for(d.membership)
        if not any(s.code in ("spouse_phone", "dependant_phone") for s in cand.signals):
            cand.signals.append(Signal(
                "spouse_phone" if spouse else "dependant_phone",
                f"Paid from the {d.get_relationship_display().lower()}'s number",
                WEIGHTS["spouse_phone" if spouse else "dependant_phone"],
                f"{d.display_name} ({ph})"))


def _name_signals(scheme, name, candidate_for, cfg):
    """Names corroborate. They never conclude.

    Two brothers share a surname, and a bank narration will happily render "RUTH
    ACHIENG OMONDI" as "OMONDI R". So a name match adds weight — enough to tip a
    phone match over the line — and is never, on its own, enough to attribute money
    to anybody.
    """
    key = name_key(name)
    if not key:
        return
    threshold = cfg.fuzzy_name_threshold or 82

    for m in SchemeMembership.objects.filter(
            scheme=scheme, status__in=SchemeMembership.LIVE_STATUSES
    ).select_related("member"):
        mk = name_key(m.member.name)
        if not mk:
            continue
        if mk == key:
            candidate_for(m).signals.append(Signal(
                "name_exact", "The name matches exactly", WEIGHTS["name_exact"],
                m.member.name))
            continue
        score = fuzzy(mk, key)
        if score >= threshold:
            candidate_for(m).signals.append(Signal(
                "name_fuzzy", "The name is a close match", WEIGHTS["name_fuzzy"],
                f"{m.member.name} ({score}% similar)"))


def _dependant_name_signals(scheme, name, candidate_for, cfg):
    """The payer's name is somebody in the member's household.

    A spouse or grown child paying on the member's behalf signs the transfer
    with their own name, so matching the narration only against members throws
    that away — and worse, can fuzzily match some unrelated member who happens
    to share a surname. Reading the household as well means the phone and the
    name corroborate each other, which is exactly the case a treasurer resolves
    by hand every month.

    Deceased and withdrawn dependants are not matched: they are not paying.
    """
    key = name_key(name)
    if not key:
        return
    threshold = cfg.fuzzy_name_threshold or 82

    for d in SchemeDependant.objects.filter(
            membership__scheme=scheme, active=True, died_on__isnull=True
    ).select_related("membership__member", "member"):
        dk = name_key(d.display_name)
        if not dk:
            continue
        spouse = d.relationship == SchemeDependant.Relationship.SPOUSE
        code = "spouse_name" if spouse else "dependant_name"
        cand = candidate_for(d.membership)
        if any(sig.code in ("spouse_name", "dependant_name") for sig in cand.signals):
            continue          # one household member is enough corroboration
        if dk == key:
            cand.signals.append(Signal(
                code, f"The name is the member's {d.get_relationship_display().lower()}",
                WEIGHTS[code], d.display_name))
            continue
        score = fuzzy(dk, key)
        if score >= threshold:
            # A fuzzy household match is weaker than an exact one; it corroborates
            # a phone match but must not carry a payment on its own.
            cand.signals.append(Signal(
                code, f"The name is close to the member's "
                      f"{d.get_relationship_display().lower()}",
                WEIGHTS["name_fuzzy"], f"{d.display_name} ({score}% similar)"))


def _amount_signals(cand, scheme, amount, case, date):
    """The amount is supporting evidence, never more. A hundred members owe the
    same 200 shillings, so an amount alone identifies nobody — but an amount that
    exactly matches what THIS member owes, on top of a phone match, is what turns a
    probable answer into a confident one."""
    from benevolent.services.contributions import arrears_for

    m = SchemeMembership.objects.filter(pk=cand.membership_id).first()
    if m is None:
        return
    policy = scheme.policy_on(date)
    if policy is None:
        return

    if policy.contribution_amount and amount == Decimal(policy.contribution_amount):
        cand.signals.append(Signal(
            "amount_dues", "The amount is exactly one period's dues",
            WEIGHTS["amount_dues"], str(amount)))
    elif policy.levy_amount and amount == Decimal(policy.levy_amount):
        cand.signals.append(Signal(
            "amount_levy", "The amount is exactly the per-case levy",
            WEIGHTS["amount_levy"], str(amount)))
    elif policy.registration_fee and amount == Decimal(policy.registration_fee):
        cand.signals.append(Signal(
            "amount_fee", "The amount is exactly the registration fee",
            WEIGHTS["amount_fee"], str(amount)))
    else:
        owed = arrears_for(m, policy, as_of=date)
        if owed > 0 and amount == owed:
            cand.signals.append(Signal(
                "amount_dues", "The amount is exactly what this member owes",
                WEIGHTS["amount_dues"], str(owed)))


def find_duplicate(scheme, membership_id, amount, date, cfg=None):
    """The same member paying the same amount to the same scheme within a few days.

    Deliberately a SUSPICION, not a rejection. Some of these are genuine — a member
    paying two months' dues in two identical instalments, or two households sending
    the same levy on the same day. So it is flagged for a human, never blocked;
    silently refusing a real payment would be worse than accepting a duplicate,
    because the member would have paid and the scheme would deny it.
    """
    import datetime as _dt
    cfg = cfg or BenevolentSettings.get()
    window = cfg.duplicate_window_days or 0
    if not window or not membership_id:
        return None
    lo = date - _dt.timedelta(days=window)
    hi = date + _dt.timedelta(days=window)
    return (BenevolentContribution.objects
            .filter(scheme=scheme, membership_id=membership_id,
                    transaction__amount=amount,
                    transaction__date__gte=lo, transaction__date__lte=hi,
                    transaction__is_reversed=False)
            .select_related("transaction").first())
