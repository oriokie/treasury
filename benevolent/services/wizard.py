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

    # --- Section 4: who is covered ---------------------------------------
    Question(
        "household", "Does one membership cover just the member, or their household?",
        "choice", section="Who is covered",
        options=[Option("INDIVIDUAL", "The member alone (plus any dependants they register)"),
                 Option("HOUSEHOLD", "The whole household")]),
    Question(
        "max_dependants", "How many dependants may one member register? "
                          "(0 for no limit)",
        "number", section="Who is covered", default="0"),
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
    if inact in ("LAPSE", "SUSPEND"):
        set_("reinstatement_waiting_days", _num(answers, "rejoin_wait", 90),
             f"A member who rejoins waits {_num(answers, 'rejoin_wait', 90)} day(s) again "
             f"before they can claim — without this, a member can lapse for years and "
             f"rejoin the week a relative falls ill.")

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
