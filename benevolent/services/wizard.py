"""The Constitution & Policy Wizard.

A church has a constitution. It is a document, in words, written by a committee —
"members shall contribute two hundred shillings monthly", "a member shall not
claim within three months of joining", "the committee shall approve any sum above
fifty thousand". Nobody in that committee thinks in terms of
`waiting_period_days` or `ApprovalMode.TWO_STAGE`.

The wizard is the translator. It asks the questions a constitution actually
answers, in the language the constitution actually uses, and produces the policy
configuration. It is the difference between a system that CAN be configured and a
system that WILL be — because a treasurer will abandon a 54-field form and will
not abandon fifteen plain questions.

Two design commitments, both of which matter more than they look:

**It shows its reasoning.** Every setting the wizard derives is returned with the
answer that produced it, in words. A treasurer must be able to check the wizard's
work against the document on the desk in front of them — a black box that emits a
constitution is worse than no wizard at all, because it will be trusted.

**It produces a DRAFT, never a live policy.** The wizard's output is reviewed,
edited if need be, and published by a human. It advises; it does not govern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class Option:
    value: str
    label: str
    help: str = ""


@dataclass
class Question:
    key: str
    text: str                       # asked the way a constitution puts it
    kind: str = "choice"            # choice | money | number | text | multi | bool
    options: list = field(default_factory=list)
    help: str = ""
    default: str = ""
    section: str = "General"
    depends_on: Optional[tuple] = None   # (key, value) — only ask if answered so

    def visible(self, answers):
        if not self.depends_on:
            return True
        return answers.get(self.dep_key) in self.dep_values

    @property
    def dep_key(self):
        return self.depends_on[0] if self.depends_on else ""

    @property
    def dep_values(self):
        """Always a list, so the template and the JS never have to care whether a
        dependency was written as one value or several."""
        if not self.depends_on:
            return []
        want = self.depends_on[1]
        return list(want) if isinstance(want, (list, tuple, set)) else [want]

    @property
    def dep_values_csv(self):
        return ",".join(self.dep_values)


# ---------------------------------------------------------------------------
# The questions. Phrased as a constitution phrases them, not as the model does.
# ---------------------------------------------------------------------------

QUESTIONS = [
    # --- Section 1: what kind of scheme is this? --------------------------
    Question(
        "purpose", "What is this scheme for?", "choice", section="The scheme",
        options=[
            Option("BENEVOLENT", "Bereavement and funeral support",
                   "The commonest: the scheme helps a family when someone dies."),
            Option("MEDICAL", "Medical and hospital costs"),
            Option("EDUCATION", "School fees and education"),
            Option("EMERGENCY", "Emergency relief (fire, flood, loss)"),
            Option("OTHER", "Something else"),
        ],
        help="This sets the wording used around the scheme. Every actual rule comes "
             "from the answers below."),

    # --- Section 2: money in ---------------------------------------------
    Question(
        "funding", "How do members fund the scheme?", "choice", section="Contributions",
        options=[
            Option("FIXED_PERIODIC", "A set amount, every month or year",
                   "Dues build a reserve, so a family can be paid at once."),
            Option("PER_CASE_LEVY", "A levy collected each time a case arises",
                   "The harambee model. Nothing is held in reserve; the family gets "
                   "what is collected."),
            Option("HYBRID", "Both — modest dues, plus a levy after each case",
                   "Dues pay the family immediately; the levy replenishes the fund."),
            Option("VOLUNTARY", "Voluntary giving only"),
            Option("NONE", "The church funds it; members do not contribute"),
        ]),
    Question(
        "dues_amount", "How much are the dues?", "money", section="Contributions",
        depends_on=("funding", ("FIXED_PERIODIC", "HYBRID"))),
    Question(
        "dues_frequency", "How often do the dues fall due?", "choice",
        section="Contributions", depends_on=("funding", ("FIXED_PERIODIC", "HYBRID")),
        options=[Option("MONTHLY", "Monthly"), Option("QUARTERLY", "Quarterly"),
                 Option("ANNUAL", "Once a year")]),
    Question(
        "levy_amount", "How much is each member levied per case?", "money",
        section="Contributions", depends_on=("funding", ("PER_CASE_LEVY", "HYBRID"))),
    Question(
        "max_levies_per_year",
        "At most how many levies may one member be asked for in a year? "
        "(0 for no limit)",
        "number", section="Contributions", default="0",
        depends_on=("funding", ("PER_CASE_LEVY", "HYBRID")),
        help="The protection against a bad year. Once a member has been levied "
             "this many times in twelve months they are left off further levy "
             "rounds until a place frees up. Their own case never counts against "
             "them. Most schemes leave this at 0 and rely on goodwill; a scheme "
             "that has promised a limit in its constitution should set it here, "
             "because it is now enforced."),
    Question(
        "arrears", "What happens if a member is behind on their contributions "
                   "when they need to claim?",
        "choice", section="Contributions",
        options=[
            Option("DEDUCT", "Pay them, but take the arrears out of the benefit",
                   "What most constitutions actually say — and the kindest reading."),
            Option("BLOCK", "They cannot claim until they have paid up"),
            Option("IGNORE", "Arrears make no difference to a claim"),
        ]),

    # --- Section 3: joining ----------------------------------------------
    Question(
        "registration", "Must a member be formally registered before they are covered?",
        "choice", section="Joining",
        options=[
            Option("AUTO", "No — anyone enrolled is covered straight away"),
            Option("TREASURER", "Yes — the treasurer admits them"),
            Option("COMMITTEE", "Yes — the committee admits them"),
        ]),
    Question(
        "joining_fee", "Is there a one-off fee to join? (0 if not)", "money",
        section="Joining", default="0"),
    Question(
        "waiting_days", "How long after joining must a member wait before they can "
                        "claim? (days; 0 if they are covered at once)",
        "number", section="Joining", default="90",
        help="The single most important protection a scheme has: without it, a family "
             "can join the week a relative falls ill."),
    Question(
        "renewal", "Does membership have to be renewed?", "choice", section="Joining",
        options=[Option("NONE", "No — it runs until they leave"),
                 Option("ANNUAL", "Yes, every year"),
                 Option("BIENNIAL", "Yes, every two years")]),
    Question(
        "renewal_fee", "How much is the renewal?", "money", section="Joining",
        depends_on=("renewal", ("ANNUAL", "BIENNIAL"))),


    # --- Section: age limits ---------------------------------------------
    Question(
        "min_age", "What is the youngest age a person may join? (0 for no limit)",
        "number", section="Who is covered", default="18"),
    Question(
        "max_age", "Is there an age above which a person may not join? "
                   "(0 for no limit)",
        "number", section="Who is covered", default="0",
        help="A joining limit, not a leaving one — nobody is dropped for growing "
             "older."),
    Question(
        "exemption_age", "At what age does a member stop being asked to "
                         "contribute? (0 if never)",
        "number", section="Who is covered", default="0",
        help="Elderly members are often kept in full cover without further "
             "contributions. They stay on the register; they simply stop being "
             "levied."),
    Question(
        "max_household_size", "How many people may one household enrolment "
                              "cover in total? (0 for no limit)",
        "number", section="Who is covered", default="0",
        depends_on=("household", ("HOUSEHOLD",))),

    # --- Section: arrears and catching up ---------------------------------
    Question(
        "grace_period_days", "How many days after a contribution falls due "
                             "before it counts as late?",
        "number", section="Contributions", default="0",
        help="A grace period stops a member being marked in arrears for paying "
             "a few days after the Sabbath they meant to."),
    Question(
        "max_arrears_allowed", "How much may a member owe and still claim? "
                               "(0 for no limit)",
        "money", section="Contributions", default="0",
        depends_on=("arrears", ("BLOCK", "DEDUCT"))),
    Question(
        "max_arrears_periods", "Or how many periods may they be behind? "
                               "(0 for no limit)",
        "number", section="Contributions", default="0",
        depends_on=("arrears", ("BLOCK", "DEDUCT"))),
    Question(
        "missed_contributions_allowed",
        "How many contributions may a member miss before they lose cover? "
        "(0 for no limit)",
        "number", section="Contributions", default="0"),
    Question(
        "catch_up_restores_eligibility",
        "If a member pays off what they owe, are they covered again?", "choice",
        section="Contributions",
        options=[Option("YES", "Yes — paying up restores their cover"),
                 Option("NO", "No — they must wait out a fresh qualifying period")],
        help="Almost every scheme says yes. Saying no protects against somebody "
             "clearing their arrears the week a relative falls ill."),
    Question(
        "catch_up_requalify_days",
        "How long after catching up before they can claim? (days; 0 for at once)",
        "number", section="Contributions", default="0",
        depends_on=("catch_up_restores_eligibility", ("YES",))),

    # --- Section: what may be claimed -------------------------------------
    Question(
        "min_contributions", "How many contributions must a member have made "
                             "before they can claim? (0 for none)",
        "number", section="Claims", default="0"),
    Question(
        "min_paid_months", "Or how many months of paid-up membership? (0 for none)",
        "number", section="Claims", default="0"),
    Question(
        "max_claims_per_year", "How many claims may one membership make in a "
                               "year? (0 for no limit)",
        "number", section="Claims", default="0"),
    Question(
        "max_benefit_per_year", "And how much may they receive in a year in "
                                "total? (0 for no limit)",
        "money", section="Claims", default="0"),
    Question(
        "benefit_floor", "Is there a minimum a family receives, whatever the "
                         "calculation gives? (0 if not)",
        "money", section="Claims", default="0",
        help="A floor protects a family when a levy collects poorly."),
    Question(
        "claim_documents", "What must a claim be supported by?", "choice",
        section="Claims",
        options=[
            Option("NONE", "Nothing formal — the committee decides"),
            Option("DOCUMENTS", "Supporting documents (burial permit, invoice)"),
        ]),

    # --- Section: joining paperwork ---------------------------------------
    Question(
        "require_registration_form",
        "Must a member complete a registration form?", "choice",
        section="Joining",
        options=[Option("YES", "Yes"), Option("NO", "No")]),
    Question(
        "require_id_document", "Must they provide an identity document?",
        "choice", section="Joining",
        options=[Option("YES", "Yes"), Option("NO", "No")]),
    Question(
        "reinstatement_fee", "What does a lapsed member pay to rejoin? (0 if nothing)",
        "money", section="Leaving and lapsing", default="0"),
    Question(
        "renewal_month", "In which month does membership renew? "
                         "(1 = January, 0 if it runs from each member's own date)",
        "number", section="Joining", default="0",
        depends_on=("renewal", ("ANNUAL", "BIENNIAL"))),

    # --- Section: exceptions and governance -------------------------------
    Question(
        "allow_exemptions", "May the committee excuse a member from "
                            "contributing?", "choice",
        section="Governance",
        options=[Option("YES", "Yes — with a recorded reason"),
                 Option("NO", "No")],
        help="Widows, the very elderly and the destitute are commonly excused. "
             "Every exemption is recorded against the member either way."),
    Question(
        "allow_override", "May the committee approve a claim that the rules "
                          "would refuse?", "choice", section="Governance",
        options=[Option("YES", "Yes — recorded, with a reason"),
                 Option("NO", "No — the rules are the rules")],
        help="An override is always recorded and always attributed. A scheme "
             "that allows none cannot help a hard case; one that allows too many "
             "has no rules at all."),
    Question(
        "allow_transfers", "May a membership be transferred to somebody else?",
        "choice", section="Governance",
        options=[Option("YES", "Yes — on death, to a member of the household"),
                 Option("NO", "No")],
        help="Where a member dies, this lets their widow take over the "
             "membership and its joining date rather than starting again."),
    Question(
        "require_different_approver",
        "Must the person approving a claim be someone other than the one who "
        "recorded it?", "choice", section="Governance",
        options=[Option("YES", "Yes — two pairs of eyes"), Option("NO", "No")],
        help="The ordinary separation of duties. Saying no means one person can "
             "record and approve a payment to themselves."),
    Question(
        "committee_requires_chair",
        "Must the chair be among those approving?", "choice",
        section="Governance",
        options=[Option("YES", "Yes"), Option("NO", "No")],
        depends_on=("approval", ("COMMITTEE",))),

    # --- Section 4: who is covered ---------------------------------------
    Question(
        "household", "Does one membership cover just the member, or their household?",
        "choice", section="Who is covered",
        options=[
            Option("INDIVIDUAL", "The member alone",
                   "Only the person who enrolled is covered. Nobody else may be "
                   "registered against the membership — a spouse who is to be "
                   "covered enrols in their own right."),
            Option("HOUSEHOLD", "The member and their household",
                   "One enrolment covers the member, their spouse and the "
                   "dependants registered under it."),
        ],
        help="This decides who a claim can be made for. It is enforced: an "
             "individual scheme will not accept dependants."),
    Question(
        "max_dependants", "How many dependants may one member register? "
                          "(0 for no limit)",
        "number", section="Who is covered", default="0",
        depends_on=("household", ("HOUSEHOLD",))),
    Question(
        "child_age_limit", "Up to what age is a child covered? (0 for no limit)",
        "number", section="Who is covered", default="21"),

    # --- Section 5: what is paid -----------------------------------------
    Question(
        "benefit", "How is the benefit worked out?", "choice", section="The benefit",
        options=[
            Option("SCHEDULE", "A set amount for each kind of event",
                   "e.g. more for a spouse than for a parent. The commonest."),
            Option("FIXED", "The same amount, whatever the event"),
            Option("POOLED", "Whatever the levy for that case collects",
                   "Only possible if members are levied per case."),
            Option("PER_MEMBER_MULTIPLE", "The levy × the number of members",
                   "What the scheme PROMISES if everyone pays."),
            Option("PERCENTAGE", "A share of the cost the member incurred"),
            Option("DISCRETIONARY", "The committee decides each time, up to a limit"),
        ]),
    Question(
        "benefit_amount", "How much is the benefit?", "money", section="The benefit",
        depends_on=("benefit", "FIXED")),
    Question(
        "benefit_percent", "What percentage of the cost does the scheme meet?", "number",
        section="The benefit", depends_on=("benefit", "PERCENTAGE"), default="60"),
    Question(
        "benefit_cap", "What is the most the scheme will pay for one case? "
                       "(0 for no limit)",
        "money", section="The benefit", default="0"),
    Question(
        "claim_window", "How soon must a case be reported after the event? "
                        "(days; 0 for no limit)",
        "number", section="The benefit", default="90"),

    # --- Section 6: the member a case is about ----------------------------
    # Worded scheme-neutrally on purpose (Phase 10): the underlying field
    # names (bereaved_contribution_policy, etc.) stayed as they were named at
    # Phase 5 — renaming a data model field is a migration for no functional
    # gain — but the WIZARD is what a Medical Fund or Education Fund
    # administrator actually reads, and "the bereaved member" read oddly for
    # a hospital bill or a school fees claim. The question is the same one
    # regardless of scheme type: does the person a case is FOR contribute
    # towards their own claim?
    Question(
        "bereaved_levy", "When the scheme raises a levy for a member's own case, "
                         "does that member contribute to it too?",
        "choice", section="The member a case is about",
        depends_on=("funding", ("PER_CASE_LEVY", "HYBRID")),
        options=[
            Option("EXEMPT", "No — automatically exempt",
                   "What almost every constitution says."),
            Option("REDUCED", "Yes, but at a reduced amount"),
            Option("DEDUCT", "Yes in full, but taken out of what they receive rather "
                             "than collected up front"),
            Option("CONTRIBUTES", "Yes, they pay it like anyone else"),
            Option("COMMITTEE_DECIDES", "It is left to the committee, case by case"),
        ]),
    Question(
        "bereaved_reduction", "What percentage of the normal amount, when reduced?",
        "number", section="The member a case is about", default="50",
        depends_on=("bereaved_levy", "REDUCED")),
    Question(
        "dues_waiver", "How many months of dues are waived for a member after their own "
                       "case? (0 if none)",
        "number", section="The member a case is about", default="0",
        depends_on=("funding", ("FIXED_PERIODIC", "HYBRID"))),

    # --- Section 7: approval ---------------------------------------------
    Question(
        "approval", "Who approves a benefit?", "choice", section="Approval",
        options=[
            Option("TREASURER", "The treasurer"),
            Option("COMMITTEE", "The committee, by a quorum"),
            Option("TWO_STAGE", "The treasurer for small sums; the committee above a limit"),
        ]),
    Question(
        "committee_threshold", "Above what amount must the committee approve?", "money",
        section="Approval", depends_on=("approval", "TWO_STAGE")),
    Question(
        "committee_quorum", "How many committee members must agree?", "number",
        section="Approval", depends_on=("approval", ("COMMITTEE", "TWO_STAGE")),
        default="3"),

    # --- Section 8: falling away -----------------------------------------
    Question(
        "inactivity", "What happens to a member who stops contributing?", "choice",
        section="Leaving and lapsing",
        options=[
            Option("NONE", "Nothing — they stay covered"),
            Option("FLAG", "They are marked inactive, but stay covered"),
            Option("LAPSE", "Their membership lapses and they cannot claim"),
            Option("SUSPEND", "They are suspended until they return"),
        ]),
    Question(
        "inactivity_months", "After how many months without a contribution?", "number",
        section="Leaving and lapsing", default="12",
        depends_on=("inactivity", ("FLAG", "LAPSE", "SUSPEND"))),
    Question(
        "inactivity_missed_cases",
        "Or after how many case levies missed in a row? (0 to ignore)",
        "number", section="Leaving and lapsing", default="0",
        depends_on=("funding", ("PER_CASE_LEVY", "HYBRID")),
        help="A levy scheme has no monthly rhythm, so counting months can be the "
             "wrong measure — a member who has missed three collections running "
             "has stopped taking part whatever the calendar says. Counted only "
             "from the member's own cover date, and their own case is never a "
             "miss."),
    Question(
        "inactivity_missed_cases_window",
        "Must those misses be consecutive, or any within a year?", "choice",
        section="Leaving and lapsing",
        options=[Option("CONSECUTIVE", "In a row"),
                 Option("ROLLING_YEAR", "Any within a rolling year")],
        depends_on=("funding", ("PER_CASE_LEVY", "HYBRID"))),
    Question(
        "transfer_membership_on_death",
        "When a member dies, may their household take over the membership?",
        "choice", section="Governance",
        options=[Option("YES", "Yes — keeping the joining date already served"),
                 Option("NO", "No — a survivor enrols afresh")],
        depends_on=("allow_transfers", ("YES",))),
    Question(
        "refunds", "If a member leaves, is anything given back?", "choice",
        section="Leaving and lapsing",
        options=[
            Option("NONE", "No — contributions stay with the scheme",
                   "The commonest: what was given helped others at the time."),
            Option("PART", "Part of what they contributed"),
            Option("ALL", "Everything they contributed"),
        ],
        help="Money already spent on other families cannot be returned, so most "
             "schemes give nothing back. If yours does, say how much — it is "
             "enforced as a ceiling when a refund is raised."),
    Question(
        "refund_percent", "What share of their contributions? (%)", "number",
        section="Leaving and lapsing", default="50",
        depends_on=("refunds", ("PART",))),
    Question(
        "registration_fee_refundable",
        "Is the joining fee given back as well?", "choice",
        section="Leaving and lapsing",
        options=[Option("NO", "No — it paid for enrolment"),
                 Option("YES", "Yes")],
        depends_on=("refunds", ("PART", "ALL"))),
    Question(
        "rejoin_wait", "If a lapsed member rejoins, how long must they wait before "
                       "claiming again? (days)",
        "number", section="Leaving and lapsing", default="90",
        depends_on=("inactivity", ("LAPSE", "SUSPEND")),
        help="Without this, a member can lapse for years and rejoin the week a relative "
             "falls ill."),

    # --- Section 9: death of the member ----------------------------------
    Question(
        "inheritance", "When a member themselves dies, who is the benefit paid to?",
        "choice", section="On a member's death",
        options=[
            Option("NOMINEE", "The people they nominated, in the shares they set"),
            Option("NEXT_OF_KIN", "The next of kin named on the case"),
            Option("HOUSEHOLD", "The household, which takes over the membership"),
            Option("NONE", "Nothing is paid — the membership simply ends"),
        ]),
    Question(
        "transfer_membership", "May the surviving spouse take over the membership, "
                               "keeping the years already paid in?",
        "bool", section="On a member's death", default="yes",
        depends_on=("inheritance", ("NOMINEE", "NEXT_OF_KIN", "HOUSEHOLD"))),
]


SECTIONS = []
for _q in QUESTIONS:
    if _q.section not in SECTIONS:
        SECTIONS.append(_q.section)


#: Sections a treasurer may accept the defaults for without answering.
#:
#: Everything here has a sensible default and describes a limit, a safeguard or
#: a piece of paperwork rather than the shape of the scheme. What the scheme is
#: for, how it is funded, what it pays and who approves it cannot be defaulted —
#: a constitution that guessed at those would not be the church's.
#:
#: The point is not to discourage answering them. It is that a treasurer setting
#: up their first scheme should be able to get a working, honest policy in place
#: and come back to the fine print, instead of abandoning a seventy-question
#: form half way and having no scheme at all.
SKIPPABLE_SECTIONS = {
    "Joining", "Who is covered", "Claims", "Leaving and lapsing", "Governance",
}

#: The step from which "accept the defaults and review" is offered.
#:
#: Two sections in: the scheme's purpose and how it is funded. Those cannot be
#: guessed — a constitution that defaulted them would not be the church's. After
#: that everything has a defensible default, and the summary lists each one with
#: the reasoning that produced it, so nothing is adopted without being seen.
SKIP_ALLOWED_FROM = 2


def default_for(question):
    """The answer to use when a treasurer accepts the defaults.

    An explicit default wins. Otherwise a choice takes its first option, which
    every question in a skippable section is written to make the safe one —
    the conservative reading, not the permissive one.
    """
    if question.default:
        return question.default
    if question.kind == "choice" and question.options:
        return question.options[0].value
    if question.kind in ("money", "number"):
        return "0"
    return ""


def fill_defaults(answers, sections=None):
    """Answer everything still unanswered, leaving what the treasurer said alone.

    Only questions actually visible given the answers so far are filled: a
    question ruled out by an earlier answer has no business acquiring a value,
    and one whose controlling answer is itself being defaulted is picked up on
    the second pass.
    """
    filled = dict(answers)
    for _ in range(3):          # settle dependent questions
        changed = False
        for q in QUESTIONS:
            if sections is not None and q.section not in sections:
                continue
            if q.key in filled or not q.visible(filled):
                continue
            filled[q.key] = default_for(q)
            changed = True
        if not changed:
            break
    return filled


def questions_for(section, answers=None):
    """The questions to ASK in a section.

    A question is hidden only when what it depends on was answered in an EARLIER
    section and rules it out. A question that depends on another question in the
    SAME section is always shown, because otherwise it could never be answered:
    "how much are the dues?" depends on "how are you funded?", and both are asked
    on the contributions page — filtering it out at render time (and again at save
    time, since the controlling answer is not in the session yet) meant the amount
    was silently never captured, and every wizard-built policy came out with dues
    of zero. The template hides these live as the controlling question is answered.
    """
    answers = answers or {}
    here = {q.key for q in QUESTIONS if q.section == section}
    out = []
    for q in QUESTIONS:
        if q.section != section:
            continue
        if q.depends_on and q.depends_on[0] in here:
            out.append(q)          # same-section dependency: JS shows/hides it
        elif q.visible(answers):
            out.append(q)
    return out


def visible_questions(answers=None):
    answers = answers or {}
    return [q for q in QUESTIONS if q.visible(answers)]


# ---------------------------------------------------------------------------
# Translation: answers -> policy configuration
# ---------------------------------------------------------------------------

def _num(answers, key, default=0):
    try:
        return int(str(answers.get(key, "")).strip() or default)
    except ValueError:
        return default


def _money(answers, key, default="0"):
    raw = str(answers.get(key, "")).strip().replace(",", "") or default
    try:
        return str(Decimal(raw))
    except Exception:  # noqa: BLE001
        return default


def _bool(answers, key, default=False):
    v = answers.get(key)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


@dataclass
class Derivation:
    """One setting the wizard produced, and the answer that produced it. This is
    what makes the wizard checkable against the document on the treasurer's
    desk."""
    setting: str
    value: str
    because: str


def build_config(answers):
    """Translate constitution answers into a policy configuration.

    Returns (config, benefit_lines, derivations). `derivations` is the reasoning,
    in words — never omitted, because a wizard that cannot be checked is a wizard
    that should not be trusted.
    """
    cfg = {}
    why = []

    def set_(key, value, because):
        cfg[key] = value
        why.append(Derivation(key, str(value), because))

    # --- contributions ---------------------------------------------------
    funding = answers.get("funding", "FIXED_PERIODIC")
    set_("contribution_mode", funding,
         {"FIXED_PERIODIC": "Members pay set dues.",
          "PER_CASE_LEVY": "Members are levied when a case arises; nothing is held in reserve.",
          "HYBRID": "Members pay dues AND are levied per case.",
          "VOLUNTARY": "Members give voluntarily; nothing is owed.",
          "NONE": "Members do not contribute; the scheme is funded elsewhere."}[funding])

    if funding in ("FIXED_PERIODIC", "HYBRID"):
        set_("contribution_amount", _money(answers, "dues_amount"),
             f"The dues you gave: {_money(answers, 'dues_amount')}.")
        set_("contribution_frequency", answers.get("dues_frequency", "MONTHLY"),
             "How often you said the dues fall due.")
    if funding in ("PER_CASE_LEVY", "HYBRID"):
        cap = _num(answers, "max_levies_per_year", 0)
        set_("max_levies_per_year", cap,
             f"A member is asked for at most {cap} levies in a year."
             if cap else
             "No limit on how many levies a member may be asked for in a year.")
        set_("levy_amount", _money(answers, "levy_amount"),
             f"The per-case levy you gave: {_money(answers, 'levy_amount')}.")

    methods = {"FIXED_PERIODIC": ["DUES"], "PER_CASE_LEVY": ["LEVY"],
               "HYBRID": ["DUES", "LEVY"], "VOLUNTARY": ["DONATION"],
               "NONE": ["SUBSIDY", "DONATION"]}[funding]
    methods.append("DONATION")
    set_("funding_methods", sorted(set(methods)),
         "What the scheme may be funded by, following from how members contribute. "
         "This is a rule: it stops a member-funded scheme being quietly subsidised out "
         "of the church budget without the constitution being changed.")

    arrears = answers.get("arrears", "DEDUCT")
    set_("arrears_treatment", arrears,
         {"DEDUCT": "Arrears come out of the benefit rather than barring the claim.",
          "BLOCK": "A member in arrears cannot claim.",
          "IGNORE": "Arrears make no difference."}[arrears])
    set_("arrears_block", arrears == "BLOCK",
         "Kept in step with the arrears treatment above (the older boolean the engine "
         "still honours).")

    # --- joining ---------------------------------------------------------
    reg = answers.get("registration", "AUTO")
    set_("registration_required", reg != "AUTO",
         "Members are covered on enrolment." if reg == "AUTO"
         else "A member must be formally admitted before they are covered.")
    set_("registration_approval", reg,
         {"AUTO": "No admission step.",
          "TREASURER": "The treasurer admits a member.",
          "COMMITTEE": "The committee admits a member."}[reg])
    fee = _money(answers, "joining_fee")
    set_("registration_fee", fee,
         f"The joining fee you gave: {fee}." if Decimal(fee) else "No joining fee.")
    set_("waiting_period_days", _num(answers, "waiting_days", 90),
         f"A member must wait {_num(answers, 'waiting_days', 90)} day(s) after joining "
         f"before they can claim.")

    renewal = answers.get("renewal", "NONE")
    set_("renewal_required", renewal != "NONE",
         "Membership runs until the member leaves." if renewal == "NONE"
         else f"Membership is renewed {'every year' if renewal == 'ANNUAL' else 'every two years'}.")
    set_("renewal_period", renewal, "Follows from the renewal answer.")
    if renewal != "NONE":
        set_("renewal_fee", _money(answers, "renewal_fee"), "The renewal fee you gave.")
        set_("lapse_on_non_renewal", True,
             "A membership not renewed within the grace period lapses.")
        set_("renewal_grace_days", 30,
             "A 30-day grace period after renewal falls due — a sensible default you can "
             "change; nobody should lose cover for being a fortnight late.")

    # --- who is covered --------------------------------------------------
    hh = answers.get("household", "INDIVIDUAL")
    set_("household_mode", hh,
         "Each member enrols on their own account." if hh == "INDIVIDUAL"
         else "One enrolment covers the whole household.")
    set_("max_dependants", _num(answers, "max_dependants", 0),
         "No limit on registered dependants." if not _num(answers, "max_dependants", 0)
         else f"Up to {_num(answers, 'max_dependants', 0)} dependants per member.")
    set_("dependant_age_limit", _num(answers, "child_age_limit", 0),
         "No age limit on children." if not _num(answers, "child_age_limit", 0)
         else f"Children are covered to {_num(answers, 'child_age_limit', 0)}.")
    set_("spouse_auto_covered", True,
         "A registered spouse is covered without counting against the dependant limit — "
         "the usual reading, and easily changed.")

    # --- the benefit -----------------------------------------------------
    benefit = answers.get("benefit", "SCHEDULE")
    set_("benefit_mode", benefit,
         {"SCHEDULE": "A different amount for each kind of event.",
          "FIXED": "One amount, whatever the event.",
          "POOLED": "The family receives what the levy for their case collects.",
          "PER_MEMBER_MULTIPLE": "The benefit is the levy times the membership.",
          "PERCENTAGE": "The scheme meets a share of the cost.",
          "DISCRETIONARY": "The committee sets the amount each time, within a cap."}[benefit])
    if benefit == "FIXED":
        set_("benefit_amount", _money(answers, "benefit_amount"),
             "The benefit you gave.")
    if benefit == "PERCENTAGE":
        set_("benefit_percent", str(_num(answers, "benefit_percent", 60)),
             f"The scheme meets {_num(answers, 'benefit_percent', 60)}% of the cost.")
    cap = _money(answers, "benefit_cap")
    if Decimal(cap) > 0:
        set_("benefit_cap", cap, f"The most payable for one case: {cap}.")
    elif benefit == "DISCRETIONARY":
        set_("benefit_cap", "50000",
             "A discretionary benefit MUST have a cap — otherwise there is no limit on "
             "what one approval can authorise. 50,000 is a placeholder: set it properly.")
    if benefit in ("POOLED", "PER_MEMBER_MULTIPLE", "PERCENTAGE"):
        set_("benefit_rounding", "HUNDRED",
             "Rounded to the nearest 100 — these calculations produce awkward figures, "
             "and a family should not be handed 23,847.")
    set_("claim_window_days", _num(answers, "claim_window", 90),
         f"A case must be reported within {_num(answers, 'claim_window', 90)} day(s)."
         if _num(answers, "claim_window", 90) else "No reporting deadline.")

    # --- the member a case is about ---------------------------------------
    if funding in ("PER_CASE_LEVY", "HYBRID"):
        bl = answers.get("bereaved_levy", "EXEMPT")
        policy_map = {"EXEMPT": "EXEMPT", "REDUCED": "REDUCED", "DEDUCT": "CONTRIBUTES",
                      "CONTRIBUTES": "CONTRIBUTES", "COMMITTEE_DECIDES": "COMMITTEE_DECIDES"}
        reasons = {
            "EXEMPT": "The member a case is about is not levied towards their own benefit.",
            "REDUCED": f"The member a case is about contributes "
                      f"{_num(answers, 'bereaved_reduction', 50)}% of the normal amount.",
            "DEDUCT": "The member a case is about contributes in full, taken out of "
                     "their benefit.",
            "CONTRIBUTES": "The member a case is about is levied like anyone else.",
            "COMMITTEE_DECIDES": "The committee decides that member's own contribution "
                                 "for each case.",
        }
        set_("bereaved_contribution_policy", policy_map.get(bl, "EXEMPT"), reasons.get(bl, ""))
        if bl == "REDUCED":
            set_("bereaved_reduction_percent", str(_num(answers, "bereaved_reduction", 50)),
                 "The reduced percentage you gave.")
        set_("bereaved_deduct_own_levy", bl == "DEDUCT",
             "Their contribution is taken out of what they receive, rather than collected "
             "up front." if bl == "DEDUCT" else "Collected the ordinary way, not deducted.")
    if funding in ("FIXED_PERIODIC", "HYBRID"):
        w = _num(answers, "dues_waiver", 0)
        set_("bereaved_dues_waiver_months", w,
             f"{w} month(s) of dues waived after a member's own case." if w
             else "No dues waived after a member's own case.")

    # --- approval --------------------------------------------------------
    approval = answers.get("approval", "TREASURER")
    set_("approval_mode", approval,
         {"TREASURER": "The treasurer approves a benefit.",
          "COMMITTEE": "The committee approves, by a quorum.",
          "TWO_STAGE": "The treasurer approves small sums; the committee approves above "
                       "the threshold."}[approval])
    if approval in ("COMMITTEE", "TWO_STAGE"):
        set_("committee_quorum", _num(answers, "committee_quorum", 3),
             f"{_num(answers, 'committee_quorum', 3)} committee members must agree.")
    if approval == "TWO_STAGE":
        set_("committee_threshold", _money(answers, "committee_threshold"),
             f"Benefits at or above {_money(answers, 'committee_threshold')} go to the "
             f"committee.")

    # --- lapsing ---------------------------------------------------------
    inact = answers.get("inactivity", "NONE")
    set_("inactivity_action", inact,
         {"NONE": "A member who stops contributing keeps their cover.",
          "FLAG": "They are marked inactive but keep their cover.",
          "LAPSE": "Their membership lapses.",
          "SUSPEND": "They are suspended until they return."}[inact])
    if inact != "NONE":
        set_("inactivity_months", _num(answers, "inactivity_months", 12),
             f"After {_num(answers, 'inactivity_months', 12)} month(s) without a "
             f"contribution.")
    if answers.get("funding") in ("PER_CASE_LEVY", "HYBRID"):
        window = answers.get("inactivity_missed_cases_window", "CONSECUTIVE")
        set_("inactivity_missed_cases_window", window,
             "Misses must be in a row to count." if window == "CONSECUTIVE"
             else "Any misses within a rolling year count together.")
        missed = _num(answers, "inactivity_missed_cases", 0)
        if missed:
            set_("inactivity_missed_cases", missed,
                 f"A member who misses {missed} case levies in a row is treated "
                 f"as inactive — the measure that suits a scheme with no monthly "
                 f"rhythm.")
    if inact in ("LAPSE", "SUSPEND"):
        set_("reinstatement_waiting_days", _num(answers, "rejoin_wait", 90),
             f"A member who rejoins waits {_num(answers, 'rejoin_wait', 90)} day(s) again "
             f"before they can claim — without this, a member can lapse for years and "
             f"rejoin the week a relative falls ill.")

    # --- what a leaver gets back -----------------------------------------
    refunds = answers.get("refunds", "NONE")
    set_("refund_contributions_on_exit", refunds != "NONE",
         {"NONE": "Contributions are not refunded when a member leaves — what was "
                  "given had already helped somebody.",
          "PART": "Part of a leaver's contributions is refundable.",
          "ALL": "A leaver's contributions are refundable in full."}[refunds])
    if refunds == "PART":
        pct = _num(answers, "refund_percent", 50)
        set_("refund_percent", pct,
             f"At most {pct}% of what a member contributed may be returned. This "
             f"is a ceiling: a refund beyond it is refused.")
    elif refunds == "ALL":
        set_("refund_percent", 100, "The whole of what a member contributed may "
                                    "be returned.")
    if refunds != "NONE":
        set_("registration_fee_refundable",
             answers.get("registration_fee_refundable") == "YES",
             "The joining fee is returned as well."
             if answers.get("registration_fee_refundable") == "YES"
             else "The joining fee is not returned — it paid for enrolment "
                  "rather than being held on the member's behalf.")

    # --- age limits ------------------------------------------------------
    for key, label in (("min_age", "youngest age at joining"),
                       ("max_age", "oldest age at joining"),
                       ("exemption_age", "age at which contributions stop")):
        val = _num(answers, key, 18 if key == "min_age" else 0)
        set_(key, val,
             f"No {label} is set." if not val else f"The {label} is {val}.")
    if answers.get("household") == "HOUSEHOLD":
        size = _num(answers, "max_household_size", 0)
        set_("max_household_size", size,
             "No limit on how many people one household enrolment covers."
             if not size else f"One household enrolment covers up to {size} people.")

    # --- arrears and catching up -----------------------------------------
    grace = _num(answers, "grace_period_days", 0)
    set_("grace_period_days", grace,
         "A contribution is late the day after it falls due." if not grace
         else f"A contribution is not late until {grace} day(s) after it falls due.")
    for key, word in (("max_arrears_allowed", "may owe"),
                      ("max_arrears_periods", "may be behind by")):
        val = (_money(answers, key) if key.endswith("allowed")
               else _num(answers, key, 0))
        set_(key, val,
             f"No limit on what a member {word} and still claim." if not val
             else f"A member {word} at most {val} and still claim.")
    missed = _num(answers, "missed_contributions_allowed", 0)
    # The older boolean says the same thing as "0 misses allowed"; kept in step
    # so the two can never contradict each other on the same policy.
    set_("no_missed_contributions", False,
         "Members may miss a contribution without losing cover outright.")
    set_("missed_contributions_allowed", missed,
         "No limit on missed contributions." if not missed
         else f"A member may miss {missed} contribution(s) before losing cover.")
    catch = answers.get("catch_up_restores_eligibility", "YES") == "YES"
    set_("catch_up_restores_eligibility", catch,
         "Paying off arrears restores cover." if catch else
         "Paying off arrears does not restore cover on its own — a fresh "
         "qualifying period is served, so nobody clears their arrears the week "
         "a relative falls ill.")
    if catch:
        days = _num(answers, "catch_up_requalify_days", 0)
        set_("catch_up_requalify_days", days,
             "Cover resumes as soon as the arrears are paid." if not days
             else f"Cover resumes {days} day(s) after the arrears are paid.")

    # --- what may be claimed ---------------------------------------------
    for key, label in (("min_contributions", "contributions"),
                       ("min_paid_months", "months of paid-up membership")):
        val = _num(answers, key, 0)
        set_(key, val, f"No minimum {label} before a claim." if not val
                       else f"At least {val} {label} before a claim.")
    for key, label in (("max_claims_per_year", "claims in a year"),
                       ("max_benefit_per_year", "received in a year")):
        val = (_num(answers, key, 0) if key.startswith("max_claims")
               else _money(answers, key))
        set_(key, val, f"No limit on {label}." if not val
                       else f"At most {val} {label}.")
    floor = _money(answers, "benefit_floor")
    set_("benefit_floor", floor,
         "No minimum payment — a family receives what the rules produce."
         if not floor else
         f"A family receives at least {floor}, whatever the calculation gives.")
    docs = answers.get("claim_documents", "NONE") == "DOCUMENTS"
    set_("require_documents", docs,
         "A claim must be supported by documents." if docs
         else "No documents are required; the committee decides on what it is told.")

    # --- joining paperwork ------------------------------------------------
    for key, what in (("require_registration_form", "a registration form"),
                      ("require_id_document", "an identity document")):
        want = answers.get(key, "NO") == "YES"
        set_(key, want, f"Joining requires {what}." if want
                        else f"Joining does not require {what}.")
    rf = _money(answers, "reinstatement_fee")
    set_("reinstatement_fee", rf,
         "A lapsed member pays nothing to rejoin." if not rf
         else f"A lapsed member pays {rf} to rejoin.")
    if answers.get("renewal") in ("ANNUAL", "BIENNIAL"):
        month = _num(answers, "renewal_month", 0)
        set_("renewal_month", month,
             "Membership renews on each member's own anniversary." if not month
             else f"Membership renews in month {month} for everybody.")

    # --- exceptions and governance ---------------------------------------
    for key, yes, no in (
            ("allow_exemptions",
             "The committee may excuse a member from contributing; every "
             "exemption is recorded against them.",
             "No member may be excused from contributing."),
            ("allow_override",
             "The committee may approve a claim the rules would refuse, with the "
             "reason recorded and attributed.",
             "The rules decide every claim; the committee cannot override them."),
            ("allow_transfers",
             "A membership may be transferred — on a member's death their widow "
             "takes it over, keeping the joining date already served.",
             "A membership cannot be transferred to anybody else."),
            ("transfer_membership_on_death",
             "When a member dies their household may take over the membership, "
             "keeping the joining date already served.",
             "A membership ends with the member; a survivor enrols afresh."),
            ("require_different_approver",
             "A claim must be approved by somebody other than whoever recorded "
             "it.",
             "One person may both record and approve a claim — which means "
             "approving a payment to themselves."),
    ):
        want = answers.get(key, "YES") == "YES"
        set_(key, want, yes if want else no)
    if answers.get("approval") == "COMMITTEE":
        chair = answers.get("committee_requires_chair", "NO") == "YES"
        set_("committee_requires_chair", chair,
             "The chair must be among those approving." if chair
             else "Any quorum of the committee may approve.")

    # --- inheritance -----------------------------------------------------
    inh = answers.get("inheritance", "NONE")
    set_("inheritance_mode", inh,
         {"NOMINEE": "The benefit is paid to the member's nominees.",
          "NEXT_OF_KIN": "The benefit is paid to the next of kin on the case.",
          "HOUSEHOLD": "The household succeeds to the membership.",
          "NONE": "Nothing is paid on the member's own death."}[inh])
    if inh != "NONE":
        set_("transfer_membership_on_death", _bool(answers, "transfer_membership", True),
             "The successor keeps the original joining date, so the years already paid in "
             "are not lost."
             if _bool(answers, "transfer_membership", True)
             else "The membership ends; a successor would join afresh.")

    # --- things the constitution rarely mentions, defaulted sensibly -----
    set_("membership_required", funding != "NONE",
         "Claims are limited to enrolled members." if funding != "NONE"
         else "The scheme is open to anyone, since members do not contribute to it.")
    cfg.setdefault("require_documents", True)
    cfg.setdefault("allow_override", True)
    why.append(Derivation(
        "allow_override", "True",
        "An approver may pay a case that fails a rule, PROVIDED they record why. Welfare "
        "needs discretion; the recorded reason is what keeps it honest. Turn this off for "
        "a scheme whose constitution genuinely admits no exceptions."))
    why.append(Derivation(
        "require_documents", "True",
        "A supporting document (burial permit, medical report) is required. Assumed, "
        "because every constitution that has ever been tested says so."))

    lines = _benefit_lines(answers)
    return cfg, lines, why


def _benefit_lines(answers):
    """The events a scheme of this purpose covers, and the schedule shape it
    needs. Amounts are left at zero for the treasurer to fill in — the wizard
    will not invent what a life is worth to a church."""
    purpose = answers.get("purpose", "BENEVOLENT")
    benefit = answers.get("benefit", "SCHEDULE")
    catalogue = {
        "BENEVOLENT": [("Bereavement — member", "BER_MEMBER"),
                       ("Bereavement — spouse or child", "BER_SPOUSE"),
                       ("Bereavement — parent", "BER_PARENT")],
        "MEDICAL": [("Hospitalisation", "HOSPITAL"), ("Surgery", "SURGERY"),
                    ("Chronic illness", "CHRONIC")],
        "EDUCATION": [("School fees", "SCHOOL_FEES"), ("Examination fees", "EXAM_FEES")],
        "EMERGENCY": [("Fire or disaster", "FIRE"), ("Displacement", "DISPLACEMENT"),
                      ("Loss of livelihood", "LIVELIHOOD")],
        "OTHER": [("Assistance", "ASSISTANCE")],
    }
    events = catalogue.get(purpose, catalogue["OTHER"])
    # a scheduled benefit needs one line per event; the other modes compute the
    # amount and need the events only as a vocabulary
    return [{"event": name, "code": code, "amount": "0",
             "requires_document": True}
            for name, code in events]


def summarise(answers):
    """A plain-English paragraph of the constitution the answers describe. Shown
    back to the treasurer BEFORE anything is created, so they read their own
    constitution in their own words and catch a wrong answer before it becomes a
    policy."""
    funding = answers.get("funding", "FIXED_PERIODIC")
    bits = []
    if funding == "FIXED_PERIODIC":
        bits.append(f"Members pay {_money(answers, 'dues_amount')} "
                    f"{(answers.get('dues_frequency') or 'MONTHLY').lower()}.")
    elif funding == "PER_CASE_LEVY":
        bits.append(f"Members are levied {_money(answers, 'levy_amount')} each time a "
                    f"case arises.")
    elif funding == "HYBRID":
        bits.append(f"Members pay {_money(answers, 'dues_amount')} "
                    f"{(answers.get('dues_frequency') or 'MONTHLY').lower()} and are levied "
                    f"{_money(answers, 'levy_amount')} per case.")
    elif funding == "VOLUNTARY":
        bits.append("Members give voluntarily.")
    else:
        bits.append("The church funds the scheme; members do not contribute.")

    wait = _num(answers, "waiting_days", 0)
    if wait:
        bits.append(f"A new member waits {wait} days before they can claim.")
    benefit = answers.get("benefit", "SCHEDULE")
    bits.append({
        "SCHEDULE": "The benefit depends on the kind of event.",
        "FIXED": f"The benefit is {_money(answers, 'benefit_amount')}, whatever the event.",
        "POOLED": "The family receives whatever the levy collects.",
        "PER_MEMBER_MULTIPLE": "The benefit is the levy times the membership.",
        "PERCENTAGE": f"The scheme meets {_num(answers, 'benefit_percent', 60)}% of the cost.",
        "DISCRETIONARY": "The committee sets the benefit each time, within a cap.",
    }[benefit])
    approval = answers.get("approval", "TREASURER")
    bits.append({
        "TREASURER": "The treasurer approves a benefit.",
        "COMMITTEE": f"{_num(answers, 'committee_quorum', 3)} committee members must agree "
                     f"before a benefit is paid.",
        "TWO_STAGE": f"The treasurer approves up to "
                     f"{_money(answers, 'committee_threshold')}; above that, "
                     f"{_num(answers, 'committee_quorum', 3)} committee members must agree.",
    }[approval])
    inact = answers.get("inactivity", "NONE")
    if inact != "NONE":
        bits.append(f"A member who does not contribute for "
                    f"{_num(answers, 'inactivity_months', 12)} months "
                    f"{'lapses' if inact == 'LAPSE' else 'is suspended' if inact == 'SUSPEND' else 'is marked inactive'}.")
    return " ".join(bits)
