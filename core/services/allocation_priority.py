"""The order in which allocation sources get to claim a contribution.

Seven different things can decide which fund a bank credit belongs to — loan
narrations, benevolent scheme rules, development-group patterns, numbered fund
families, exact allocation rules, pattern allocation rules, and a campaign's
member table. Each was added by the module that needed it, each is configured on
its own page, and the order they run in was written into two files
(`giving.services.allocation.allocate` and `statements.services.importer`) where
no treasurer could see it.

That order is the whole game. "CAMP EXPENSE 3" is claimed by the numbered-fund
family rule or by a development-group pattern depending purely on which runs
first, and the money lands in a different fund either way. Until now the only
way to find out which was to read the source.

This module is the one description of that order. It does three things and
deliberately not a fourth:

  * names every source, in the order it actually runs, in language a treasurer
    can act on;
  * says which ones a church may reorder and which are pinned, with the reason
    each pin exists;
  * runs a reference through every source to show what each WOULD say — the
    tester, which answers "why did this money go there".

It does not itself allocate anything. `allocate()` remains the one place money
is resolved; this tells it what order to try, and asks it what it did.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    #: What this source recognises, for the person deciding where it belongs.
    what: str
    #: Empty when the stage may be reordered. When set, this is the reason it
    #: cannot be — always an accounting consequence, never a preference.
    pinned_because: str = ""
    #: Stages inside allocate() can be reordered. Those in the importer run
    #: around it and are shown for context, so a treasurer sees the whole
    #: sequence rather than the middle of it.
    movable: bool = True

    @property
    def pinned(self):
        return bool(self.pinned_because)


#: Everything that can claim a contribution, in the order it runs by default.
#: The order here IS the default order — `default_order()` reads it, so adding a
#: stage in the right place is all that is needed.
STAGES = [
    Stage(key="loan_narration",
          label="Loan narrations",
          what="A narration the loans module recognises as a lender's money — "
               "booked as a liability receipt, not as income.",
          pinned_because="A loan is money the church owes, not money it has "
                         "been given. \"LOAN DEV\" read as development income "
                         "overstates income and hides a debt, so this has to "
                         "be settled before anything treats it as giving.",
          movable=False),
    Stage(key="benevolent_scheme",
          label="Benevolent scheme rules",
          what="A narration a benevolent scheme claims, so the money reaches "
               "that scheme's fund and its intake queue.",
          pinned_because="Scheme money is members' welfare contributions held "
                         "for them, not church income. It is recognised before "
                         "ordinary allocation so it cannot be absorbed into a "
                         "general fund it would then have to be dug out of.",
          movable=False),

    # --- inside allocate(): the ambiguous middle, and what a church may order ---
    Stage(key="dev_group_numbered",
          label="Development group — numbered",
          what="A reference naming a development group with its number, like "
               "DEVGR7 or 'dev grp 14'."),
    Stage(key="numbered_families",
          label="Numbered fund families",
          what="A configured family such as EXPENSE7 → CAMP_7, set up under "
               "Settings. One line covers every group in the family."),
    Stage(key="exact_rules",
          label="Allocation rules — exact",
          what="A rule whose reference matches the whole narration exactly."),
    Stage(key="pattern_rules",
          label="Allocation rules — patterns",
          what="Starts-with, ends-with, contains and regex rules, most "
               "specific first."),
    Stage(key="dev_group_word",
          label="Development group — no number",
          what="Clearly a development gift, but the reference does not say "
               "which group. Held for review rather than guessed at."),

    # --- after allocate(): fallbacks ---
    Stage(key="campaign_members",
          label="Campaign member table",
          what="When nothing above resolved it, an uploaded campaign sheet can "
               "still identify the giver by name or phone and route the money "
               "to their group's fund.",
          pinned_because="This is the last resort, and it works by identifying "
                         "the PERSON rather than reading the narration. Running "
                         "it earlier would let a name match override a "
                         "narration that said plainly where the money was for.",
          movable=False),
]

STAGE_BY_KEY = {s.key: s for s in STAGES}

#: The stages `allocate()` runs, and the only ones a church may reorder. The
#: others bracket it and are shown for context.
MOVABLE_KEYS = [s.key for s in STAGES if s.movable]


def default_order():
    return [s.key for s in STAGES]


def parse_order(raw):
    """A stored order into a list of keys, tolerating anything.

    A stored order is configuration a person edited, and it can be stale: a
    stage may have been added since it was saved, or removed. Unknown keys are
    dropped and missing ones appended in their default position, so allocation
    never depends on the config being perfectly in step with the code.
    """
    stored = [k.strip() for k in (raw or "").replace(",", "\n").splitlines()
              if k.strip()]
    seen, out = set(), []
    for key in stored:
        if key in STAGE_BY_KEY and key not in seen:
            seen.add(key)
            out.append(key)
    for key in default_order():
        if key not in seen:
            out.append(key)
    return out


def movable_order(raw=None):
    """The configured order of just the stages `allocate()` runs."""
    order = parse_order(raw) if raw is not None else default_order()
    return [k for k in order if STAGE_BY_KEY[k].movable]


def current_order():
    """The order in force, read from the site configuration."""
    try:
        from core.models import SiteConfig
        return parse_order(SiteConfig.get().allocation_priority)
    except Exception:      # noqa: BLE001 — never let config break allocation
        return default_order()


def is_default(raw):
    return parse_order(raw) == default_order()


def _describe(value):
    """One source's answer, as a person would say it."""
    if value is None:
        return ""
    if isinstance(value, str):
        if value.startswith("DEV_GROUP_"):
            n = value.removeprefix("DEV_GROUP_")
            return ("Development group — which one is not in the reference"
                    if n == "NA" else f"Development group {n}")
        return value
    return getattr(value, "name", str(value))


def explain(reference, date=None, name="", phone="", order=None):
    """What EVERY source would say about one reference, and which one wins.

    The point is the losers. A treasurer looking at money in the wrong fund
    already knows where it went; what they cannot see is that two sources both
    claimed it and the order decided between them. Showing only the winner
    would reproduce exactly the blindness this page exists to remove.

    Reads the live rules and writes nothing.
    """
    from giving.services.allocation import (STEPS, detect_dev_group,
                                            normalize_reference)

    order = order or current_order()
    s = normalize_reference(reference)
    dev_hit = detect_dev_group(s) if s else None
    rows, winner = [], None

    for key in order:
        stage = STAGE_BY_KEY.get(key)
        if stage is None:
            continue
        claim, detail = "", ""
        try:
            if key == "loan_narration":
                from loans.services.narration import detect_loan
                lp = detect_loan(reference)
                if lp is not None and getattr(lp, "kind", "") == "RECEIPT":
                    claim = (getattr(getattr(lp, "fund", None), "name", "")
                             or "a loan, but the fund is unknown")
                    detail = "Booked as a liability receipt, not as income."
            elif key == "benevolent_scheme":
                from benevolent.services.allocation import detect_scheme
                scheme, _kind, _status = detect_scheme(reference)
                if scheme is not None:
                    claim = getattr(scheme, "name", str(scheme))
                    detail = "Goes to the scheme's fund and its intake queue."
            elif key == "campaign_members":
                from giving.services.allocation import campaign_allocate
                _c, _g, cdept, _cs = campaign_allocate(reference, name, phone)
                if cdept is not None:
                    claim = getattr(cdept, "name", str(cdept))
                    detail = "Matched the giver on a campaign sheet."
            elif key in STEPS and s:
                result = STEPS[key](s, date, dev_hit)
                if result is not None:
                    claim = _describe(result[0])
                    detail = f"Recorded as {result[1].lower()}."
        except Exception as exc:      # noqa: BLE001 — a tester never breaks
            detail = f"could not be checked ({type(exc).__name__})"

        if claim and winner is None:
            winner = key
        rows.append({"stage": stage, "claims": claim, "detail": detail,
                     "wins": claim and winner == key})

    return {
        "reference": reference,
        "normalised": s,
        "rows": rows,
        "winner": STAGE_BY_KEY[winner] if winner else None,
        # More than one source wanting the same money is the whole problem:
        # it is the case where the ORDER, and nothing else, decided the fund.
        "contested": [r for r in rows if r["claims"]][1:] if winner else [],
    }


def validate(keys):
    """Why a proposed order cannot be saved, or [] if it can.

    A pinned stage that has moved is refused rather than warned about: the
    reason each pin exists is an accounting one, and a page that let it be
    overridden with a shrug would be worse than a page that did not offer it.
    """
    problems = []
    keys = [k for k in keys if k in STAGE_BY_KEY]
    if len(set(keys)) != len(keys):
        problems.append("A source appears more than once.")
    missing = set(default_order()) - set(keys)
    if missing:
        problems.append(
            "Some sources are missing: "
            + ", ".join(STAGE_BY_KEY[k].label for k in sorted(missing)))
    default = default_order()
    for key in keys:
        stage = STAGE_BY_KEY[key]
        if stage.pinned and key in default and keys.index(key) != default.index(key):
            problems.append(
                f"{stage.label} cannot be moved. {stage.pinned_because}")
    return problems
