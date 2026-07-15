"""Policy profiles — reusable constitutions.

Churches do not invent welfare schemes from nothing. They run one of a small
number of well-known shapes, and the differences between two churches' rules are
usually amounts, not architecture. A profile captures one of those shapes once,
so configuring a scheme becomes *choose and adjust* rather than *answer forty
questions from a blank page*.

A profile is not a policy and governs nothing. Applying one CREATES A DRAFT
`SchemePolicy`, which still has to be published before it decides anything — so
profiles can be edited, copied and deleted freely, with none of the immutability
constraints that (rightly) surround a live policy version.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx

from benevolent.models import (BenevolentEventType, PolicyProfile, SchemeBenefitRule,
                               SchemePolicy)


def _coerce(policy, fieldname, value):
    """Turn a JSON value into what the model field actually wants. A profile is
    stored as JSON, so decimals arrive as strings and dates as ISO text; putting
    those on the model unconverted is how you get a policy that compares a string
    to a Decimal at assessment time and silently decides the wrong way."""
    field = SchemePolicy._meta.get_field(fieldname)
    internal = field.get_internal_type()
    if value is None:
        return None
    if internal == "DecimalField":
        return Decimal(str(value))
    if internal == "DateField":
        return (value if isinstance(value, _dt.date)
                else _dt.date.fromisoformat(str(value)))
    if internal == "BooleanField":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
    if internal in ("IntegerField", "PositiveIntegerField",
                    "PositiveSmallIntegerField", "SmallIntegerField"):
        return int(value)
    return value


def config_from_policy(policy) -> dict:
    """Every rule on a policy, as JSON-safe config. The exact inverse of
    `apply_config`, so a policy → profile → policy round-trip is lossless."""
    out = {}
    for f in SchemePolicy.RULE_FIELDS:
        if f == "effective_from":
            continue          # a profile is undated: the date is chosen on use
        v = getattr(policy, f)
        if isinstance(v, Decimal):
            v = str(v)
        elif isinstance(v, _dt.date):
            v = v.isoformat()
        out[f] = v
    return out


def lines_from_policy(policy) -> list:
    return [{"event": r.event_type.name, "code": r.event_type.code,
             "amount": str(r.amount), "percent": str(r.percent),
             "cap": (str(r.cap) if r.cap is not None else None),
             "max_per_year": r.max_per_year}
            for r in policy.benefit_rules.select_related("event_type").filter(active=True)]


def apply_config(policy, config):
    """Write a config dict onto a (draft) policy. Unknown keys are ignored rather
    than raising — a profile written for a later version of the module must not
    make the whole library unusable on an older one."""
    known = set(SchemePolicy.RULE_FIELDS)
    for key, value in (config or {}).items():
        if key not in known or key == "effective_from":
            continue
        try:
            setattr(policy, key, _coerce(policy, key, value))
        except (ValueError, TypeError, ArithmeticError):
            continue          # a malformed value falls back to the field default
    return policy


@db_tx.atomic
def apply_profile(profile, scheme, *, effective_from=None, user=None):
    """Create a DRAFT policy for a scheme from a profile.

    Draft, not live — deliberately. Applying a profile is a starting point that a
    human then reviews and publishes; it is never itself an act of government. A
    profile can therefore be a suggestion without being a risk.

    Event types named in the profile's benefit schedule are created on the scheme
    if it does not have them yet, so a profile brings its whole vocabulary with it
    and the church is not left with a benefit schedule referring to events that do
    not exist.
    """
    effective_from = effective_from or _dt.date.today()
    draft = SchemePolicy(scheme=scheme, effective_from=effective_from,
                         status=SchemePolicy.Status.DRAFT, created_by=user)
    apply_config(draft, profile.config)
    draft.notes = (f"Created from the '{profile.name}' profile."
                   + (f"\n\n{profile.description}" if profile.description else ""))
    draft.save()

    death_marked = scheme.event_types.filter(triggers_on_death=True).exists()
    for line in (profile.benefit_lines or []):
        code = (line.get("code") or line.get("event") or "").strip().upper().replace("-", "_")
        name = line.get("event") or code.replace("_", " ").title()
        if not code:
            continue
        # Mark the first bereavement/funeral event as the one deaths are claimed
        # under, so a scheme built from a profile can auto-open death cases with
        # no extra setup. Only the FIRST — a schedule may carry several
        # bereavement tiers (member, spouse, child), and exactly one should be
        # the trigger; a treasurer can move the mark afterwards.
        looks_like_death = any(
            w in code.lower() or w in name.lower()
            for w in ("death", "bereav", "funeral", "burial"))
        mark_this = looks_like_death and not death_marked
        event, created = BenevolentEventType.objects.get_or_create(
            scheme=scheme, code=code[:20],
            defaults={"name": name[:80],
                      "requires_document": bool(line.get("requires_document")),
                      "triggers_on_death": mark_this})
        if mark_this:
            death_marked = True
            if not created and not event.triggers_on_death:
                event.triggers_on_death = True
                event.save(update_fields=["triggers_on_death"])
        SchemeBenefitRule.objects.get_or_create(
            policy=draft, event_type=event,
            defaults={
                "amount": Decimal(str(line.get("amount") or 0)),
                "percent": Decimal(str(line.get("percent") or 0)),
                "cap": (Decimal(str(line["cap"])) if line.get("cap") else None),
                "max_per_year": int(line.get("max_per_year") or 0),
            })
    return draft


@db_tx.atomic
def save_as_profile(policy, *, name, description="", user=None):
    """Capture a working policy as a reusable profile — the route by which a
    church that has got its constitution right contributes it back to the library
    for the next scheme (or the next church)."""
    if PolicyProfile.objects.filter(name=name).exists():
        raise ValidationError(f"A profile called '{name}' already exists.")
    return PolicyProfile.objects.create(
        name=name, description=description, kind=policy.scheme.kind,
        config=config_from_policy(policy), benefit_lines=lines_from_policy(policy),
        created_by=user)


@db_tx.atomic
def duplicate(profile, *, name=None, user=None):
    name = name or f"{profile.name} (copy)"
    n, i = name, 2
    while PolicyProfile.objects.filter(name=n).exists():
        n = f"{name} {i}"
        i += 1
    return PolicyProfile.objects.create(
        name=n, description=profile.description, kind=profile.kind,
        config=dict(profile.config or {}),
        benefit_lines=list(profile.benefit_lines or []),
        created_by=user)


# ---------------------------------------------------------------------------
# The built-in library
# ---------------------------------------------------------------------------
#
# Five shapes, which between them cover the great majority of church welfare
# schemes actually in operation — bereavement, medical and emergency relief
# among them, deliberately, as the concrete proof that this is a Scheme
# Engine and not a bereavement-fund with other labels bolted on. They are
# starting points, not prescriptions: a church copies the nearest one and
# adjusts the amounts.

BUILTINS = [
    {
        "name": "Monthly dues, fixed benefit",
        "kind": "BENEVOLENT",
        "description": (
            "The commonest shape. Members pay a set amount every month, and the scheme "
            "pays a set benefit on a bereavement — the amount differing by how close the "
            "relative was. A reserve builds up, so the scheme can pay immediately without "
            "waiting to collect."),
        "config": {
            "membership_required": True, "waiting_period_days": 90,
            "min_contributions": 3,
            "arrears_treatment": "DEDUCT", "max_arrears_allowed": "0",
            "registration_required": True, "registration_approval": "TREASURER",
            "registration_fee": "500", "require_registration_form": True,
            "renewal_required": False, "renewal_period": "NONE",
            "contribution_mode": "FIXED_PERIODIC", "contribution_amount": "200",
            "contribution_frequency": "MONTHLY",
            "funding_methods": ["DUES", "DONATION"],
            "benefit_mode": "SCHEDULE", "benefit_cap": "100000",
            "approval_mode": "TWO_STAGE", "committee_threshold": "50000",
            "committee_quorum": 3,
            "bereaved_contribution_policy": "EXEMPT", "bereaved_dues_waiver_months": 3,
            "inactivity_months": 12, "inactivity_action": "LAPSE",
            "reinstatement_waiting_days": 90,
            "household_mode": "INDIVIDUAL", "max_dependants": 0,
            "dependant_age_limit": 21, "spouse_auto_covered": True,
            "inheritance_mode": "NOMINEE", "transfer_membership_on_death": True,
            "claim_window_days": 90, "max_claims_per_year": 2,
            "require_documents": True, "allow_override": True,
        },
        "benefit_lines": [
            {"event": "Bereavement — member", "code": "BER_MEMBER", "amount": "50000"},
            {"event": "Bereavement — spouse or child", "code": "BER_SPOUSE", "amount": "30000"},
            {"event": "Bereavement — parent", "code": "BER_PARENT", "amount": "20000"},
            {"event": "Hospitalisation", "code": "HOSPITAL", "amount": "10000",
             "max_per_year": 1},
        ],
    },
    {
        "name": "Per-case levy (harambee)",
        "kind": "BENEVOLENT",
        "description": (
            "No standing dues. When a bereavement happens, every member is levied a set "
            "amount and the family receives what is collected. The scheme cannot become "
            "insolvent, because it never promises more than it raises — but the family's "
            "benefit depends on how many members actually pay."),
        "config": {
            "membership_required": True, "waiting_period_days": 30,
            "min_contributions": 0, "arrears_treatment": "IGNORE",
            "registration_required": True, "registration_approval": "AUTO",
            "registration_fee": "200",
            "contribution_mode": "PER_CASE_LEVY", "levy_amount": "500",
            "max_levies_per_year": 12,
            "funding_methods": ["LEVY", "DONATION"],
            "benefit_mode": "POOLED",
            "benefit_rounding": "HUNDRED",
            "approval_mode": "COMMITTEE", "committee_quorum": 3,
            "bereaved_contribution_policy": "EXEMPT",
            "inactivity_months": 0, "inactivity_action": "NONE",
            "household_mode": "INDIVIDUAL", "spouse_auto_covered": True,
            "inheritance_mode": "NEXT_OF_KIN",
            "claim_window_days": 60, "max_claims_per_year": 2,
            "require_documents": True, "allow_override": True,
        },
        "benefit_lines": [
            {"event": "Bereavement — member", "code": "BER_MEMBER"},
            {"event": "Bereavement — spouse or child", "code": "BER_SPOUSE"},
            {"event": "Bereavement — parent", "code": "BER_PARENT"},
        ],
    },
    {
        "name": "Hybrid — monthly dues plus a levy",
        "kind": "BENEVOLENT",
        "description": (
            "Modest monthly dues keep a working reserve, so the family is paid at once; a "
            "levy then replenishes the fund after each case. This is what many schemes "
            "drift towards after a bad year has emptied a pure-dues fund, or after a "
            "pure-levy scheme has been embarrassed by a poor collection."),
        "config": {
            "membership_required": True, "waiting_period_days": 60,
            "min_contributions": 2, "arrears_treatment": "DEDUCT",
            "registration_required": True, "registration_approval": "TREASURER",
            "registration_fee": "300",
            "contribution_mode": "HYBRID", "contribution_amount": "100",
            "contribution_frequency": "MONTHLY", "levy_amount": "300",
            "max_levies_per_year": 6,
            "funding_methods": ["DUES", "LEVY", "DONATION"],
            "benefit_mode": "SCHEDULE", "benefit_cap": "80000",
            "approval_mode": "TWO_STAGE", "committee_threshold": "40000",
            "committee_quorum": 3,
            "bereaved_contribution_policy": "EXEMPT", "bereaved_dues_waiver_months": 2,
            "inactivity_months": 12, "inactivity_action": "FLAG",
            "household_mode": "HOUSEHOLD", "max_dependants": 6,
            "dependant_age_limit": 21, "spouse_auto_covered": True,
            "inheritance_mode": "HOUSEHOLD", "transfer_membership_on_death": True,
            "claim_window_days": 90, "max_claims_per_year": 2,
            "require_documents": True, "allow_override": True,
        },
        "benefit_lines": [
            {"event": "Bereavement — member", "code": "BER_MEMBER", "amount": "40000"},
            {"event": "Bereavement — spouse or child", "code": "BER_SPOUSE", "amount": "25000"},
            {"event": "Bereavement — parent", "code": "BER_PARENT", "amount": "15000"},
        ],
    },
    {
        "name": "Medical assistance (percentage of cost)",
        "kind": "MEDICAL",
        "description": (
            "Open to the whole congregation, funded by giving rather than dues. The scheme "
            "meets a share of a member's medical bill, up to a cap — so it helps with a "
            "large cost without promising to meet it in full."),
        "config": {
            "membership_required": False, "waiting_period_days": 0,
            "arrears_treatment": "IGNORE",
            "registration_required": False, "registration_approval": "AUTO",
            "contribution_mode": "VOLUNTARY",
            "funding_methods": ["DONATION", "SUBSIDY", "FUNDRAISING"],
            "benefit_mode": "PERCENTAGE", "benefit_percent": "60",
            "benefit_cap": "50000", "benefit_rounding": "HUNDRED",
            "approval_mode": "COMMITTEE", "committee_quorum": 3,
            "inactivity_months": 0, "inactivity_action": "NONE",
            "inheritance_mode": "NONE",
            "claim_window_days": 60, "max_claims_per_year": 2,
            "max_benefit_per_year": "80000",
            "require_documents": True, "allow_override": True,
        },
        "benefit_lines": [
            {"event": "Hospitalisation", "code": "HOSPITAL", "amount": "0"},
            {"event": "Surgery", "code": "SURGERY", "amount": "0"},
            {"event": "Chronic illness", "code": "CHRONIC", "amount": "0", "max_per_year": 2},
        ],
    },
    {
        "name": "Emergency relief (fast, fixed amounts)",
        "kind": "EMERGENCY",
        "description": (
            "For a fire, flood, theft or sudden displacement — help that needs to move "
            "fast, not a claims process. No membership or waiting period (anyone in the "
            "congregation qualifies the moment disaster strikes), a fixed amount per kind "
            "of emergency, and treasurer-level approval so a family is not left waiting "
            "on a committee to convene while their roof is gone."),
        "config": {
            "membership_required": False, "waiting_period_days": 0,
            "arrears_treatment": "IGNORE",
            "registration_required": False, "registration_approval": "AUTO",
            "contribution_mode": "VOLUNTARY",
            "funding_methods": ["DONATION", "FUNDRAISING", "SUBSIDY"],
            "benefit_mode": "SCHEDULE", "benefit_cap": "30000",
            "approval_mode": "TREASURER",
            "inactivity_months": 0, "inactivity_action": "NONE",
            "inheritance_mode": "NONE",
            "claim_window_days": 30, "max_claims_per_year": 1,
            "require_documents": False, "allow_override": True,
        },
        "benefit_lines": [
            {"event": "Fire", "code": "FIRE", "amount": "20000"},
            {"event": "Flood", "code": "FLOOD", "amount": "15000"},
            {"event": "Theft or burglary", "code": "THEFT", "amount": "10000"},
            {"event": "Sudden displacement", "code": "DISPLACED", "amount": "15000"},
        ],
    },
]


def install_builtins():
    """Idempotent. Creates any built-in profile that is missing, and leaves alone
    any a church has since edited — a profile the treasurer has tuned is theirs,
    and must not be quietly reset by an upgrade."""
    made = 0
    for spec in BUILTINS:
        if PolicyProfile.objects.filter(name=spec["name"]).exists():
            continue
        PolicyProfile.objects.create(
            name=spec["name"], description=spec["description"], kind=spec["kind"],
            config=spec["config"], benefit_lines=spec["benefit_lines"], builtin=True)
        made += 1
    return made
