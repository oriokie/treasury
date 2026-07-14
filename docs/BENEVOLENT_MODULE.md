# Benevolent Scheme Engine — Phase 1 (Foundation & Architecture)

*A configurable welfare-scheme platform, not a benevolent fund.*

---

## 1. The central design decision

The brief asked for a Benevolent Module that could later grow into a Medical Fund,
an Education Fund and an Emergency Relief Fund **through configuration rather than
new business logic**. Everything below follows from taking that literally.

A scheme is therefore made of four configurable things and no code:

| Model | What it configures |
|---|---|
| `BenevolentScheme` | What the scheme is called, and **which fund holds its money** |
| `BenevolentEventType` | The qualifying events the scheme recognises |
| `SchemePolicy` | **The rules** — versioned, immutable once used |
| `SchemeBenefitRule` | What each event is worth (the benefit schedule) |

Adding a Medical Fund means creating a scheme, a fund, some event types and a
policy. It does not mean writing a line of Python. There is exactly one
eligibility engine and exactly one case workflow, and both read policy fields.

`BenevolentScheme.kind` (Benevolent / Medical / Education / Emergency) is a
**label only**. It changes wording. It changes no rule. If a future requirement
ever tempts someone to branch on `kind`, that is the signal that a new *policy
field* is needed instead — which is the whole point of the engine.

---

## 2. Accounting: no new money machinery

This is the constraint the design is built around, and it follows the precedent
the loans module already set: **every shilling flows through the two existing
source-document types.** The module adds no balance maths of its own.

```
Contribution in    giving.Transaction  CREDIT on the scheme's fund, attributed
                   to the contributing member.
                   Posts DR Cash / CR Income — an ordinary receipt into a
                   designated local fund. (Unlike a loan receipt, which is
                   financing and excluded from income, a benevolent
                   contribution IS income, so nothing special happens.)

Benefit paid out   cashbook.Expense  category = BENEVOLENCE, charged to the
                   scheme's fund, raised in PENDING.
                   Posts DR Benevolence / CR Cash once approved.
```

Consequences, all of them free:

* The **general ledger, trial balance, fund balances, cash book, bank
  reconciliation, budget-vs-actual, I&E statement and Board Pack** all pick up
  benevolent activity with no benevolent-specific code, because they are looking
  at ordinary receipts and ordinary vouchers.
* `/ledger/rebuild/` regenerates benevolent postings with no special step.
* A scheme's cash balance **is its fund's balance**. It is never separately
  maintained, and is read from the Financial Metrics Registry
  (`metrics.fund_balance`). There is no second number that can drift.

### The index rows

`BenevolentContribution` and `BenevolentPayout` are the scheme-side *index* over
those documents. They carry only what the document cannot know — which enrolment
a receipt settles, which dues period it covers, which case a voucher pays.

**The documents remain authoritative.** `amount` and `date` are Python
*properties* that read the linked document; they are not stored columns. An index
row only counts while its document still counts (`effective`), which means:

* reversing a receipt removes the contribution — no correction needed here;
* rejecting a voucher un-pays the case — no correction needed here.

A treasurer rejecting a benevolent voucher in the ordinary expense screen, with
no idea a case exists behind it, gets the right answer anyway (see
`benevolent/signals.py`).

---

## 3. Internal controls

The module **never approves its own payments.**

```
approve_case()    records a DECISION. Moves no money. Posts nothing.
record_payout()   raises a cashbook.Expense in PENDING — like any other claim.
                  It then clears the ORDINARY expense workflow: treasurer
                  approval, the dual-approval threshold on high values, period
                  locks, the payment register, the ledger posting.
```

So a benevolent payout is no easier to get out of the bank than any other
payment. That is deliberate: welfare money is exactly the money most worth
protecting.

Other controls, all enforced in the service layer and tested:

* **Segregation of duties** — the person who raised a case cannot approve it.
* **Override discipline** — an ineligible case *can* be approved (welfare needs
  discretion), but only if the policy permits an override *and* the approver
  records a written reason, which becomes part of the permanent record and
  appears on the audit log. A policy can forbid overrides outright.
* **Cap discipline** — approving above the policy cap also requires a reason.
* **Authorisation is reserved, not just spent** — `available_to_voucher` nets off
  both paid *and* pending vouchers, so three pending vouchers cannot each be
  raised for the full approved amount.
* **Period locks** are honoured by contributions and payouts.
* **Nothing is deleted** — cases are rejected, cancelled or closed.

### Rights

Four new granular rights, layered on the existing profile system:

| Right | Default holders |
|---|---|
| `view_benevolent` | Treasurer, Assistant, Auditor |
| `manage_benevolent` (enrol, raise cases, record contributions/vouchers) | Treasurer, Assistant |
| `approve_benevolent` (**authorise a benefit**) | Treasurer only |
| `manage_benevolent_schemes` (**create schemes, publish policies**) | Treasurer only |

The split between *administering* a scheme and *making its rules* is intentional:
a welfare secretary can run the scheme day to day (a grantable profile right)
without ever being able to change what it pays or authorise a shilling.

---

## 4. Immutability & versioning

> *"Historical transactions and policies must remain immutable, and all future
> rule changes should be versioned without affecting historical data."*

Three mechanisms, belt and braces:

1. **A policy version locks the moment a case is decided under it.**
   `SchemePolicy.save()` refuses to change any rule field on a locked version;
   `delete()` refuses outright. The *only* way to change the rules is
   `new_version_from()` → edit → `publish_policy()`, which supersedes the old
   version and closes its effective window the day before the new one opens.

2. **Every case freezes what it was decided on.** `policy_snapshot` holds the full
   terms, and `eligibility_snapshot` holds every check that was run, whether it
   passed, and the figures compared. Even if a policy row were somehow tampered
   with, the case still carries the terms it was actually assessed against. The
   case screen renders the frozen snapshot, never a re-run.

3. **Resolution is by EVENT date, not today.** `scheme.policy_on(date)` returns
   the version in force on that date — including superseded versions, within
   their own windows. A claim reported late is judged by the rules that applied
   when the event happened.

Historical dues work the same way: arrears accrue from enrolment, each period
charged at the rate of the policy in force *during that period*. (Charging
everything at the current policy's rate from its effective date would have meant
that publishing a new version silently wiped every member's arrears — a treasurer
could have cleared the scheme's whole debt by republishing the same rules with a
new date.)

---

## 5. The policy engine

`benevolent/services/eligibility.py`. Given a scheme, a claimant and an event, it
answers two questions by reading policy fields and nothing else:

* **Is it eligible?** — every rule, run as an isolated `Check`.
* **What is it worth?** — the policy's benefit mode, with its workings shown.

It never returns a bare yes/no. It returns every check it ran, whether each
passed, and the actual figures compared — the same transparency principle as the
`HealthScore` in the intelligence platform. That structure *is* the
`eligibility_snapshot` frozen onto the case, so an auditor can reconstruct a
decision years later.

Rules currently modelled (all policy fields):

* membership required · waiting period (policy-wide or per event) · minimum
  contributions · arrears block, with a tolerance
* claim window · max claims per year (overall and per event type) · max benefit
  per year
* supporting documents required (policy-wide or per event)
* benefit mode: **fixed** · **schedule** (per event) · **percentage of cost** ·
  **discretionary within a cap** — plus per-event caps and a policy floor

Adding a rule is one policy field + one `_check_*` function. It is never a new
code path per scheme.

---

## 6. Financial Metrics Registry

Five metrics registered from `benevolent/apps.py::ready()`:

| Metric | Delegates to |
|---|---|
| `benevolent_scheme_summary` | `metrics.fund_summary` (+ scheme context) |
| `benevolent_contributions` | `core.metrics.income_credits` |
| `benevolent_payouts` | `metrics.expenses_by_department` |
| `benevolent_fund_balance` | `metrics.fund_balance` |
| `benevolent_commitments` | *(new — see below)* |

`benevolent/services/reporting.py` computes **no financial figure itself.** The
scheme summary's opening / contributions / payouts / closing columns are the
scheme fund's rows taken straight from `fund_summary` — literally the same call
the Board Pack's fund statement makes. What the module adds is the non-financial
context a fund cannot know about itself: members, open cases, commitments.

**`benevolent_commitments` is a memorandum figure, and is documented as one.**
Expenditure is recognised when a voucher is *approved*, at which point it is
already in the ledger and already reducing the fund. Commitments are what a case
decision has promised but not yet vouchered. It therefore deliberately does *not*
appear on the Statement of Financial Position, and does not contradict it.

---

## 7. Integration points

| Surface | How |
|---|---|
| General ledger | via `giving.Transaction` / `cashbook.Expense` — no new posting path |
| Fund balances / Board Pack / I&E / cash book | automatic, same reason |
| Financial Metrics Registry | 5 metrics, all delegating |
| Expense approval, dual approval, payment register | the payout voucher is an ordinary expense |
| Period locks | enforced in the services |
| Audit trail | `HistoricalRecords` on every model → the existing Audit Log report |
| Notifications | `core.services.notifications.notify` on submit / approve / payout |
| Members | `members.Member`, reused; never duplicated |
| Rights & profiles | 4 new rights in the existing catalogue |
| Navigation | a "Benevolent" nav group, gated on `view_benevolent` |
| Django admin | registered with `SimpleHistoryAdmin` |
| JSON API | read-only: schemes, scheme summary, **eligibility**, case |

### The API

`/benevolent/api/eligibility/` lets anything ask the policy engine a question
without raising a case, and get back the same transparent answer the treasurer
sees. It powers the case form's preview and is the integration surface for the
assistant and any external consumer.

The API is **read-only by design.** Nothing that moves money is exposed over it;
decisions stay behind the permissioned, audited workflow.

---

## 8. Files

```
benevolent/
  models.py                    scheme, policy, benefit rules, event types,
                               membership, dependants, contributions, cases,
                               payouts, attachments, year sequences
  services/eligibility.py      THE policy engine (transparent Check/Entitlement)
  services/cases.py            the case workflow + payout via cashbook.Expense
  services/contributions.py    money in via giving.Transaction; dues & arrears
  services/schemes.py          scheme lifecycle; policy publish/supersede/version
  services/reporting.py        summaries — every figure delegated to the registry
  metrics.py                   registry registration
  signals.py                   voucher → case status sync
  api.py, views.py, forms.py, urls.py, admin.py
  test_benevolent.py           43 tests
templates/benevolent/          12 templates
docs/BENEVOLENT_MODULE.md      this file
```

---

## 9. Deliberately not in Phase 1

Named honestly rather than half-built:

* **Per-case levies** — the model (`BenevolentContribution.case`) and the
  working list (`raise_case_levy`) exist and are tested at the service layer, but
  there is no collection *screen* yet. A levy is currently collected by recording
  ordinary contributions against the case.
* **Reports on the Report Engine.** Benevolent figures are registry metrics and so
  are *available* to the engine, but no `ComponentSection` has been written yet —
  the module has its own screens, not yet a board-pack section.
* **Bank-narration auto-intake** of dues (the loans module's `LoanNarrationPattern`
  equivalent). Contributions are recorded manually or by adopting an existing
  bank credit; there is no pattern engine routing `BEN` references automatically.
* **Arrears reminders** over SMS/WhatsApp. `refresh_arrears_status()` exists;
  nothing schedules it or messages anyone.
* **Dependant-aware benefit rules** (a different amount for a spouse vs a child
  *automatically*). Today this is expressed by having separate event types, which
  works but is a slightly blunt instrument.

See `docs/recommendations.md` for these as tracked items.

---
---

# Phase 2 — Constitution, Settings & Policy Engine

## 1. The line the whole phase rests on

Phase 2 makes every church-specific behaviour configurable. The danger in doing
that is obvious and fatal: if the rules become *settings*, then editing a setting
rewrites the basis of decisions already made, and the module's central promise —
that history is immutable — collapses.

So every configurable thing goes to one of two homes, and the test for which is a
single question:

> **Does it decide an outcome?**

| | YES → a **RULE** | NO → a **SETTING** |
|---|---|---|
| Lives on | `SchemePolicy` | `BenevolentSettings` |
| Versioned? | Yes | No |
| Frozen once used? | Yes — the model refuses to change | No — freely editable |
| Examples | registration, fees, renewals, contribution model, benefit calculation, committee approval, bereaved rules, inactivity, household cover, inheritance | accounting mappings, notification preferences, automation cadence, defaults for new schemes |

That is what lets the brief's two requirements hold *at the same time* rather than
trading off against each other: "all church-specific behaviour driven by
configuration" **and** "policy changes are version-controlled and do not modify
historical transactions".

`SchemePolicy.RULE_FIELDS` grew from 19 to 54 entries. Everything in that list is
under the version lock. A test asserts that every constitution dimension is
actually in it — because a rule that is *not* in `RULE_FIELDS` is a rule that
could be quietly changed after a case was decided on it.

### Why accounting mappings are a setting

This looks like the exception and is not. Every posted document — a
`giving.Transaction`, a `cashbook.Expense` — stores its own fund and category *at
the moment it is written*. Re-pointing a mapping therefore steers **future**
postings only, and is physically incapable of rewriting a historical one. The
ledger's history is safe by construction, not by policy. There is a test for
exactly this.

---

## 2. The settings area

`/benevolent/settings/` — its own page, under its own right
(`manage_benevolent_settings`), reached from its own nav.

It inherits the application's theme, layout, tab framework, form styling and
permission model wholesale; a treasurer cannot tell it was built separately. But
it is *separate*, deliberately: a welfare secretary can be given the module
without also being given the keys to the church's SMS gateway and bank feed.

Four tabs: **Accounting** (mappings), **Notifications** (which events tell whom,
over which channels), **Automation** (the standing rules, with a "run it now and
show me what it would do" button), **Defaults** (which profile a new scheme starts
from).

---

## 3. The constitution — what is now configurable

Every item below is a policy field, decided by the engine, with no code branch per
scheme.

* **Registration** — required or not; admitted automatically, by a treasurer, or
  by the committee; a joining fee; a signed form and an ID document on file;
  minimum and maximum joining age (measured **at joining** — a scheme that caps
  entry at 70 does not throw a member out on their 71st birthday).
* **Renewals** — annual or biennial, a renewal month (every membership renews
  together, which is how a church actually runs a subscription year), a fee, a
  grace period, and whether non-renewal lapses the membership.
* **Contributions** — none, voluntary, fixed periodic dues, a per-case levy, or
  **hybrid** (dues *and* a levy). Levy caps per year.
* **Funding methods** — dues, levies, donations, church subsidy, fundraising,
  investment income. A *rule*, not a note: it stops a member-funded scheme being
  quietly subsidised out of the church budget without the constitution being
  changed to allow it.
* **Benefit calculation** — fixed, a per-event schedule, a percentage of cost,
  discretionary within a cap, and two new ones:
  * **POOLED** — the family receives whatever the levy for their case collects.
    The harambee model. Such a scheme can never become insolvent, because it never
    promises more than it raises.
  * **PER_MEMBER_MULTIPLE** — the levy × the membership. What the scheme
    *promises* if everybody pays. Deliberately distinct from POOLED: that is the
    reality, this is the pledge, and a scheme ought to know which it is making.
  * Plus rounding, so a pooled calculation does not hand a grieving family
    23,847.
* **Arrears** — IGNORE, BLOCK, or **DEDUCT** (pay the benefit, net off what is
  owed). DEDUCT is the default, because refusing a bereaved family over two months
  of dues is not what a welfare scheme is for, and it is what real constitutions
  actually say.
* **Committee approval** — treasurer, committee (by quorum), or two-stage
  (treasurer below a threshold, committee above it).
* **Bereaved-member rules** — the bereaved member is not levied towards their own
  benefit (almost every real constitution says this); or their levy is deducted
  from what they receive; and a waiver of some months of dues after their own
  case. Exemption and deduction are mutually exclusive, and the form enforces it.
* **Inactivity** — after N months without a contribution: nothing, flag, suspend,
  lapse or expel. Plus a **reinstatement waiting period**.
* **Household** — individual or household cover; a dependant cap; a child age
  limit; spouse automatically covered.
* **Inheritance** — nominees in recorded shares, next of kin, household
  succession, or nothing; and whether the successor inherits the membership
  *keeping its original joining date*, so the years the deceased paid in are not
  lost by the household.

### `cover_from` — the anti-gaming rule

One property, `SchemeMembership.cover_from`, is the single definition of what
every waiting period counts from. Three things move it, in precedence order:
**reinstatement** > **formal registration** > **joining date**.

The reinstatement case is the one that matters. Without it, a member could lapse
for years, rejoin the week a relative fell ill, and claim immediately on the
strength of a joining date from 2019 — the single most obvious way to game a
welfare scheme. Re-enrolling a former member *reinstates* their original
membership (so their history and number are never orphaned) and restarts their
waiting period.

---

## 4. Committee approval

Where a policy routes a benefit to the committee, the benefit is **not authorised
by an individual at all**. It is authorised when a quorum of recorded decisions is
reached. A treasurer cannot approve past a committee — that is the entire point of
having one, and there is a test that says so.

Each vote is its own `CaseApproval` row, with its author and timestamp, so the
minute of the decision is reconstructable.

Where committee members differ on the **amount**, the **lowest** approved figure
carries. Three people voting 10,000 / 8,000 / 10,000 have not agreed on 10,000 —
they have agreed on 8,000, which is the most all three of them sanctioned.

`benevolent_committee` is its own right. A committee whose seats are held
automatically by the treasurer is not a committee, so an elder or a welfare
secretary can be given a seat with no other treasury access at all.

---

## 5. Policy profiles

A named, reusable bundle of policy settings — a constitution template. Four ship
built in: **monthly dues with a fixed benefit**, **per-case levy (harambee)**,
**hybrid**, and **medical (percentage of cost)**.

A profile **governs nothing**. Applying one creates a **DRAFT** `SchemePolicy`,
which a human still reads and publishes. That is why profiles can be edited,
copied and deleted freely, with none of the immutability constraints that surround
a live policy version. Built-ins cannot be deleted (copy and adjust the copy), so
the library always has a starting point.

A working policy can be captured back as a profile — the route by which a church
that has got its constitution right contributes it to the library for the next
scheme, or the next church.

---

## 6. The Constitution & Policy Wizard

A church has a constitution. It is a document, in words, written by a committee:
*"members shall contribute two hundred shillings monthly"*, *"a member shall not
claim within three months of joining"*, *"the committee shall approve any sum above
fifty thousand"*. Nobody on that committee thinks in terms of
`waiting_period_days` or `ApprovalMode.TWO_STAGE`.

The wizard is the translator. It asks the questions a constitution actually
answers, in the language the constitution actually uses — about 28 of them, across
nine sections, with irrelevant ones hidden — and produces the policy configuration.

It is the difference between a system that *can* be configured and one that *will*
be, because a treasurer will abandon a 54-field form and will not abandon fifteen
plain questions.

Two commitments matter more than they look:

**It shows its reasoning.** Every setting is returned with the answer that produced
it, in words, and displayed on the review screen before anything is created. A
treasurer must be able to check the wizard's work against the document on their
desk. A black box that emits a constitution is *worse* than no wizard at all,
because it will be trusted. A test asserts that no setting is ever produced without
a reason.

**It produces a DRAFT, never a live policy.** The wizard advises; it does not
govern. And its output travels the *same code path* a hand-picked profile does
(it is expressed as a profile and applied) — one route into a policy, not two that
could drift apart.

---

## 7. Automation

`benevolent/services/schemes.py::run_automation`, and
`python manage.py benevolent_automation` (with `--dry-run`).

It applies the **policy's** rules to the membership register — arrears,
inactivity, renewals. It makes no rules of its own. Two principles make it safe to
point at a church's welfare register:

1. **It never overrides a human.** Only memberships in `AUTOMATABLE_STATUSES` are
   touched. One that someone deliberately SUSPENDED, WITHDREW or EXPELLED is left
   completely alone. An automated job quietly reversing a decision a treasurer made
   on purpose is the fastest way to make people stop trusting automation.
2. **It is reversible and it reports.** Every change is returned. Each rule
   reinstates as readily as it demotes: a member who clears their arrears goes back
   to ACTIVE on the next run with nobody intervening.

**Suspension and expulsion are never automated**, even when the policy names them
as the inactivity action. Removing someone from a welfare scheme is a decision a
person should make and answer for. The policy still bars their claims through the
eligibility engine; automation simply declines to be the one who throws them out.

---

## 8. Deliberately not in Phase 2

* **Household cover is modelled but only half-enforced.** `household_mode`,
  `household_name` and the dependant/age caps all work and are tested; what is
  *not* built is a true household object with its own members and a single
  subscription per household rather than per member. A HOUSEHOLD scheme today
  behaves as an individual one with generous dependant cover.
* **Inheritance stops at the nomination.** Nominees, shares and successor flags are
  recorded, and the engine reports a missing nominee rather than guessing. But
  *splitting a payout across nominees in their shares* is not automated — the
  treasurer raises the vouchers — and `transfer_membership_on_death` is recorded
  but not yet acted on by a "succeed to this membership" action.
* **Refunds on exit** (`refund_contributions_on_exit`, `refund_percent`) are
  policy fields the engine does not yet act on.
* **Reminders.** `arrears_reminder_days` and `renewal_reminder_days` are settings;
  nothing sends the reminder yet.
* **Levy caps.** `max_levies_per_year` is recorded and shown but not yet enforced
  against a member.

These are tracked in `docs/recommendations.md` (#59).

---
---

# Phase 3 — Member Registry, Households & Standing

## 1. The refactor the phase turns on: two axes, not one

Phases 1–2 kept a single `SchemeMembership.status`, and it was quietly doing two
incompatible jobs at once:

* decisions a **human** makes — pending, active, suspended, withdrawn
* facts a **job** derives — lapsed, expired, inactive

So automation wrote into the same column a treasurer wrote into. It was kept safe
by an allowlist of statuses the job was permitted to touch. That worked — but it
was a rule somebody had to remember, and one day would not. Worse, it made a
derived fact *look* like a decision: a membership marked LAPSED told you nothing
about whether a person had chosen that or a nightly job had inferred it.

Phase 3 splits them, and everything else follows:

| `status` — the **LIFECYCLE** | `standing` — the **STANDING** |
|---|---|
| A human decides it. Never automated. | A pure function computes it. Never hand-set. |
| Pending · Active · Suspended · Withdrawn · Deceased · Closed | Good standing · Exempt · Grace period · Arrears · Inactive — plus the lifecycle states, which dominate |

`standing` is a **cache of a pure function** (`services/standing.assess`).
Recomputing it can never lose information, which is what makes it safe for a
nightly job to touch. Automation now writes **only** to the derived axis, and is
therefore *structurally* incapable of overriding a treasurer's decision — not
because it is told not to, but because `status` is a different column and the job
does not write to it.

A test states the guarantee directly: a suspended member who pays off every
shilling stays suspended, and the job simply reports their standing as SUSPENDED.

`LAPSED`, `EXPIRED`, `INACTIVE` and `EXPELLED` are gone from `status`. A data
migration (`0005_split_status_from_standing`) moves existing rows across, and
records the original word in the event log so nothing is lost.

### The order of the standing tests is the design

1. **The lifecycle dominates.** A deceased member is not "in arrears"; a withdrawn
   one is not "inactive". Whatever a human decided outranks any calculation.
2. **Exempt beats everything derived.** Someone excused from contributing cannot be
   behind on contributions — that is what the word means.
3. **Inactive beats arrears.** Owing three months' dues and having vanished for
   three years are different problems, and the second is the one worth saying out
   loud.
4. **Grace beats arrears.** A grace period exists so that being a fortnight late is
   not being in default.
5. Otherwise: arrears, or good standing.

---

## 2. Standing reports; the policy decides

The two must never disagree about a plain fact, but they answer different
questions:

```
standing     = where the member STANDS        (a fact about the member)
eligibility  = whether the claim is PAYABLE   (a decision about a case)
```

A member in ARREARS may still be paid — under DEDUCT, the commonest real rule, the
scheme pays them and nets off what they owe. So `StandingResult.covered` is *not* a
fixed list of "good" standings: it mirrors the eligibility engine's blocking rules.
An early version did use a fixed list, and a test caught it telling a treasurer that
an arrears member was "not covered" while the engine happily paid them.

**They cannot disagree, because they do not each compute.** `MembershipFacts` is
computed once, in `services/standing.py`, and both consume it. There is exactly one
place in this system that knows how many months a member is behind. A test walks
every combination of arrears treatment and inactivity action and asserts the
register's view of cover and the engine's verdict never differ.

The same principle put exemptions in `contributions._waived_periods` rather than in
the standing engine: `arrears_for()` is the one place that knows what a member owes,
and it is called by the register, the eligibility check *and* the arrears deduction
on a benefit. Had exemptions been applied anywhere else, an exempt member would have
shown as clear on the register and still had money docked from their bereavement
payout. There is a test for exactly that.

---

## 3. Extending Members, not duplicating it

`members.Member` remains the **only** record of a person.

* A `SchemeMembership` is an *enrolment*, not a person.
* A `SchemeDependant` now carries an optional FK to `members.Member`, so a spouse
  who is on the church roll is **linked**, not typed in a second time. Their name
  and phone live in one place and cannot drift. Where the dependant is not a church
  member (a young child, a parent in the village), `name` carries them and nothing
  is lost.
* A **household** is a *registration type* on the membership — not a parallel
  person-database with its own names to fall out of step with the roll.
* A member's own page (`/members/<id>/`) now shows their welfare standing, and the
  households they are *covered by* as well as the ones they hold. That page can only
  exist because there is one registry.

---

## 4. Households

One subscription, a principal member, a spouse, dependants. Not a different *kind*
of membership with its own code path — the same `SchemeMembership` with
`registration_type` set. Everything downstream (dues, standing, claims, benefits)
treats it identically, which is the property that stops household schemes becoming a
second, subtly-different system.

* One spouse per registration.
* `max_household_size` counts the principal member.
* Removing a dependant sets `removed_on`; it never deletes. **A dependant who was
  covered when an event happened is still covered for that event**, however the
  household has changed since. Deleting the row would quietly destroy a claim the
  family had already earned.

---

## 5. Death, transfer and inheritance

**Recording a death does not close the membership.** Their own death is very often
the *last claim on the scheme* — the thing they paid in for. A system that closed the
membership at that moment would discard a family's entitlement at the exact moment it
fell due. So the status becomes DECEASED, the claim can still be raised, assessed and
paid, and the eligibility engine explicitly does **not** bar a claim on a deceased
member's own death.

**A transfer keeps the joining date.** This is the whole point of the function. A
widow whose husband paid into the scheme for eleven years should not be told she is a
new member with a ninety-day wait before the scheme will help her. `transfer()` keeps
`joined_on`, and deliberately does **not** set `reinstated_on` — that field exists to
stop a lapsed member gaming the scheme, and a grieving widow is not a lapsed member
gaming the scheme.

The household travels with the membership (the dependants were the household's, not
the deceased's personally), the old membership points at the new one, and the trail is
intact in both directions.

**Reinstatement still restarts the waiting period.** The anti-gaming rule survives
Phase 3 intact, and is tested: a lapsed member coming back is not a widow inheriting.

---

## 6. Exemptions

Every scheme has them: the founding members, the very old, a family in genuine
hardship. Without a first-class record they are handled by a treasurer quietly not
chasing certain people — which is **indistinguishable from favouritism**, cannot be
handed over, and disappears when they do.

An exemption is a policy decision about a member, so:

* it must record **why** (the service refuses a blank reason);
* it is **approved by a second person** — it relieves someone of an obligation
  everyone else is carrying, which is a money decision;
* an **unapproved** exemption excuses nobody;
* `exemption_age` gives an automatic age exemption with no paperwork;
* an exemption can excuse dues, levies, or both — and a levy-exempt member is off the
  levy roster, because leaving them on it would chase them for money the church has
  already decided in writing that they do not owe;
* a policy may forbid exemptions altogether.

---

## 7. Inactivity: missed contributions *or* missed cases

A dues scheme measures inactivity in **months since a contribution**. A levy scheme
has no monthly dues to miss, so that measure sees nothing at all — and the member who
never stands with a bereaved family, and then expects the family to stand with them,
walks straight through.

`inactivity_missed_cases` is the measure that catches them. It counts **consecutively,
backwards from the most recent case**: someone who missed two levies two years ago and
has paid every one since is not the problem this rule is for. Cases raised for the
member *themselves* are skipped — they were never levied for their own bereavement, and
counting it as a miss would punish them for being bereaved.

---

## 8. The membership event log

`django-simple-history` already records every field change, and it is what an auditor
uses to prove a value. But nobody can *read* it: it answers "what was this field on 3
March?" and not "what happened to this member, and why?".

`MembershipEvent` is the second thing, and it is the one a treasurer, a board and a
bereaved family actually ask for. Every registration, admission, refusal, fee, renewal,
suspension, reinstatement, withdrawal, death, transfer, exemption, dependant change and
standing change is one line, with **who** did it, **why**, and whether a person or a job
did it (automated events are visibly marked — a member has a right to know whether a
human decided this).

Every lifecycle function in `services/registry.py` writes one. No view can move a
membership without it, because no view writes the lifecycle directly.

---

## 9. New policy rules (Phase 3)

All under the version lock, as every rule is:

`grace_period_days` · `allow_exemptions` · `exemption_age` ·
`inactivity_missed_cases` · `allow_transfers` · `max_household_size`

---

## 10. Deliberately not in Phase 3

* **Splitting a payout across nominees in their recorded shares** is still not
  automated (carried over from #59b). The shares are recorded and the successor flag
  drives the transfer prompt, but the treasurer raises the vouchers.
* **Refunds on exit** (`refund_contributions_on_exit`, `refund_percent`) remain policy
  fields the engine does not act on.
* **Reminders** (`arrears_reminder_days`, `renewal_reminder_days`) are still settings
  with nothing behind them.
* **`max_levies_per_year`** is still recorded and not enforced.
* **A household does not yet pay one subscription instead of several.** Dues are still
  charged per membership, which for a household registration is per household — so the
  common case is right — but a scheme wanting *per-adult* household dues has no way to
  say so.

Tracked in `docs/recommendations.md` (#61).

---
---

# Phase 4 — Contribution Engine & Intelligent Allocation

## 1. Money and obligations are different things

A welfare scheme deals in two currencies at once, and confusing them is the classic
way a member ledger goes quietly wrong.

| | |
|---|---|
| **MONEY** — actually moved | Receipted, in the bank, in the general ledger. |
| **OBLIGATION** — what a member *owes* | A penalty charged. A due waived. A debt written off. **Nothing has moved. Nobody has paid anything.** |

* Booking a **waiver as an expense** shows a cash outflow that never happened, and
  the cash book stops agreeing with the bank.
* Booking a **penalty as income** recognises revenue that may never arrive.
* Booking a **refund as negative income** hides a real payment from the cash book.

So:

```
money in     → giving.Transaction     (exactly as Phases 1–3)
money out    → cashbook.Expense       (exactly as a benefit is)
obligations  → MemberAdjustment       (NOTHING POSTS)
```

`MemberAdjustment` touches the general ledger nowhere, and that is not an omission
— it is the design. A penalty becomes income on the day it is **paid**, as an
ordinary receipt, like everything else. A waiver is the church deciding to stop
asking; no money left, so no entry.

What they change is one number: what `arrears_for()` says the member owes. And
that function — still the *one* place in the system that knows — now has three
inputs (the policy's dues, the obligations ledger, the money actually received) and
still gives one answer, to the register, the eligibility engine, the arrears
deduction on a benefit and the member's own statement alike.

## 2. A refund is not a reversal

The distinction, and why it matters:

* A receipt that **should never have existed** — wrong member, duplicate, bounced —
  is **reversed**. The church never had that money; the record should say so.
  `Transaction.is_reversed` already does this, and the contribution index row stops
  counting the moment it is set (Phase 1).
* A receipt that was **correct**, where the church now hands money back — a member
  leaving a scheme that refunds, an overpayment returned — is **refunded**. The
  money was really received and is really being paid out. **Both facts belong in the
  cash book.**

Reversing a correct receipt to "cancel out" a refund would hide a real payment from
the bank reconciliation and understate income *and* expenditure. So a refund is an
ordinary `cashbook.Expense` — built exactly as a benefit payout is — clearing the
usual approval, getting a voucher, appearing on the payment register and posting to
the ledger like any other payment. The module still never approves its own payments.

## 3. Unallocated is not unrecorded

**The single most important sentence in this phase.**

Intelligent allocation can fail, and it must be allowed to. What it must never do is
lose the money.

A receipt whose owner cannot be identified is still receipted, still in the scheme's
fund, still in the general ledger, still on the bank reconciliation, still in the
board pack. It sits in an intake queue until a human says whose it is — **and the
fund balance is right the whole time.** A system that refused to bank money it could
not attribute would produce a fund balance that disagreed with the bank, which is far
worse than an unattributed receipt.

The importer therefore does two separate things in a deliberate order: it gets the
**fund** right (from a narration rule, with certainty) and banks the money; and only
*then* asks who it belongs to. The first must never wait on the second.

The same principle governs *rejecting* a queue item: deciding a receipt is not
benevolent money is a statement about **attribution**, not about whether the church
received it. Rejecting leaves the transaction exactly where it was. Conflating the
two would let a treasurer make money vanish from the cash book by clicking a button.

## 4. The allocator

Given an amount, a date, a phone number, a name as the bank spelled it, and a
narration typed on a phone keypad, it answers three questions and says how sure it is:
*which scheme, whose money, what kind*.

**Identifiers, all of them from the brief:**

| Signal | Weight | Why |
|---|---|---|
| Membership number | 70 | Conclusive. The member typed their own identifier. |
| Case reference | 55 | Conclusive about the case; makes it a levy for that case. |
| Member's own phone | 55 | Best everyday evidence — but families share handsets. |
| Household identifier | 45 | The registration names itself. |
| Member's other numbers | 45 | Kept after a merge; still theirs. |
| **Spouse's phone** | 45 | A spouse paying his dues from *her* phone is completely routine. A system that cannot see it queues an ordinary payment every single month. |
| Dependant's phone | 35 | Same reasoning. |
| Name (exact) | 30 | Corroborating. **Never conclusive** — two brothers share a surname. |
| Name (fuzzy) | 20 | Kenyan narrations abbreviate and reorder names constantly. |
| Narration rule → scheme | 25 | Identifies the scheme well and the member not at all. |
| Amount matches dues/levy/fee | 10–12 | Supporting only — a hundred members owe the same 200. |

**Signals add.** Corroboration is what produces confidence, so no single medium
signal reaches the auto-allocation threshold on its own. That is deliberate, and it
is how a careful treasurer works.

**It shows its working.** Every candidate, every signal that fired for it, and the
score. Frozen onto the queue row, so a wrong automatic allocation can be *understood*
rather than merely undone. A confidently-wrong allocation that nobody can explain is
the worst thing this module could produce. There is a screen (`/benevolent/allocation-test/`)
where a treasurer can ask "what would you do with this?" and see every signal.

**It refuses when it should.** Two candidates within 15 points of each other is **not
confidence, however high the top score** — it is the allocator saying it cannot tell
them apart. Two brothers, one handset, one surname: that is precisely where a wrong
automatic answer is most likely and least likely to be noticed. Such a receipt goes
to review even at 95%.

## 5. The intake queue

| Outcome | When |
|---|---|
| **AUTO** | Confident, unambiguous, valid — attached without a human. |
| **REVIEW** | Plausible. Suggestions shown, with the reasoning. |
| **UNMATCHED** | Nothing identified the member. An honest blank, not a bad guess. |
| **DUPLICATE** | Same member, same amount, same scheme, within a few days. |
| **REJECTED** | Not scheme money. The receipt is untouched. |

A suspected duplicate **never** auto-allocates, whatever the confidence — the whole
point of the flag is that a human looks. And it is flagged, never *blocked*: some are
genuine (a member paying two months in two identical instalments), and silently
refusing a real payment would be worse than accepting a duplicate, because the member
would have paid and the scheme would deny it.

## 6. Learned rules are *proposed*, never switched on

After a treasurer has allocated the same unrecognised narration by hand three times,
the system writes the rule — **inactive**. A rule that silently started routing money
because of a pattern nobody agreed to would be a rule nobody agreed to.

## 7. Policy-driven validation

`engine.validate()` is one function, asked by both the manual path and the intake
path, so they cannot disagree about what is legal. It refuses: dues to a scheme with
no dues; a levy with no case; a fee the policy does not charge; an obligation from a
member who owes nothing (their money is a donation); a membership from the wrong
scheme; and — the one a treasurer would not think of — **levying the bereaved member
for their own case**, which the policy already says is not done.

## 8. New models

`ContributionRule` · `MemberAdjustment` · `ContributionRefund` ·
`ContributionIntake` · new settings (`auto_allocate`, thresholds,
`fuzzy_name_threshold`, `duplicate_window_days`, `learn_allocation_rules`) ·
`SchemeDependant.phone` · the full contribution taxonomy (DUES / LEVY / REGISTRATION
/ RENEWAL / PENALTY / VOLUNTARY / DONATION).

## 9. Deliberately not in Phase 4

* **Recurring contributions are recognised, not *scheduled*.** The engine handles
  dues arriving on any cadence, and knows what is owed for each period. What it does
  not do is *initiate* anything — no standing order, no scheduled M-Pesa pull. A
  church wanting a monthly auto-debit still has no way to say so.
* **`max_levies_per_year` is still not enforced** (open since Phase 2).
* **Reminders still do nothing** (open since Phase 2 — now flagged for the third
  time).
* **Refund on exit is not automatic.** `refund_percent` is still a policy field the
  engine does not act on; a treasurer raises the voucher and types the amount.
* **The allocator never creates a member.** Unlike the main statement importer, it
  will not invent a `Member` from a bank narration — a welfare scheme's register is
  a list of people who signed up, not of people who happened to send money.

Tracked in `docs/recommendations.md` (#62).

---
---

# Phase 5 — Bereavement Case Management

## 1. The case's own narrative

`django-simple-history` answers "what was this field on 3 March?" It has never answered
"what happened on this case, and why?" — the question a treasurer re-opening a case six
months later, a board reviewing a large payment, or a bereaved family asking why their
claim took so long, actually asks.

`CaseEvent` is that answer, mirroring `MembershipEvent` from Phase 3 exactly. Every
workflow function in `services/cases.py` — raise, submit, assess, vote, approve, reject,
cancel, raise a payout, a voucher clearing or being reversed in the ordinary expense
screen, close — writes one line. Automated entries (a voucher status sync, an automatic
exemption) are marked `automated=True`, so a reader can always tell whether a person
decided something or a rule did.

Case creation itself moved into the service layer (`case_svc.create_case`) for exactly
this reason — a `BenevolentCase.objects.create()` in a view would miss the very first
line of the case's own history.

## 2. Funding targets are a goal, not a rule

`BenevolentCase.funding_target` is deliberately **not** consulted by the eligibility
engine. The policy alone still decides what is owed. A target is a fundraising goal a
committee or treasurer sets — "we're aiming for 30,000" — tracked against
`funding_collected` (every levy contribution tagged to the case; the same figure
`levy_summary()` reports, so the two can never disagree) and shown as a progress bar.
Reaching it fires a notification exactly once, via a dedicated
`notify_on_funding_target_reached` toggle — not a repurposed one.

## 3. The bereaved member's own contribution — four options, one function

Phase 2 modelled this as two overlapping booleans (`bereaved_exempt_own_levy` /
`bereaved_deduct_own_levy`). Building Phase 5 surfaced that they could not express
"reduced" or "committee decides" at all, **and had a live double-charge bug**: a
"deduct" member was left on the levy roster (asked to pay up front) *and* had the same
amount taken off their benefit.

Replaced with one explicit choice, `SchemePolicy.BereavedContributionPolicy`:

| | |
|---|---|
| **CONTRIBUTES** | Levied like anyone else, on the roster in full. |
| **REDUCED** | On the roster at `bereaved_reduction_percent`% of the normal amount. |
| **EXEMPT** | Off the roster entirely. The default — "almost every real constitution says this." |
| **COMMITTEE_DECIDES** | Off the roster until `decide_bereaved_contribution()` records a ruling; undecided is treated as EXEMPT's weight, never CONTRIBUTES' — nobody is chased on the strength of a rule nobody has actually applied yet. |

`bereaved_deduct_own_levy` survives as an orthogonal modifier — where the member does
contribute (CONTRIBUTES or REDUCED), collect it by deduction from their benefit instead
of the ordinary roster. **This is the fix**: a deduct-collected member is now excluded
from the roster (`raise_case_levy`), and `_bereaved_weight()` — one function — is the
only place that decides how much they owe, consumed identically by the levy roster, the
PER_MEMBER_MULTIPLE pledge calculation, and the benefit deduction. All three can no
longer disagree, because there is only one answer to ask.

## 4. Automatic exemption is a real exemption, not silent arithmetic

Phase 2's `bereaved_dues_waiver_months` correctly zeroed a bereaved member's dues for N
months — but did it by adjusting `arrears_for()`'s arithmetic inline, with **no
record**: no exemption row, no membership event, no line in the standing register.
Phase 3 established, for every other exemption in the system, that "an exemption
without a recorded reason is indistinguishable from favouritism." A silently-zeroed due
was exactly that gap, just for the bereaved member specifically.

Fixed: approving a case under an EXEMPT bereaved policy with a nonzero waiver now calls
`registry.grant_policy_exemption()` — a new, deliberately distinct entry point from the
human-discretionary `grant_exemption()`/`approve_exemption()` pair. A policy-computed
waiver is not a new decision needing a second signature; the church already wrote the
rule down and published it. Auto-approved, but identical in shape and just as visible as
a hand-granted exemption: it shows in the member's exemptions panel, Standing correctly
reports `EXEMPT` (not a silent `GOOD`), and both a `MembershipEvent` and a `CaseEvent`
record it, each marked `automated=True`.

## 5. Documents: a checklist, not a checkbox

`BenevolentEventType.required_documents` (a JSON list, e.g. `["Burial permit", "Death
certificate"]`) turns the old single `requires_document` boolean into a real checklist.
`CaseAttachment.document_type` matches against it; `missing_required_documents()` says
exactly what's still needed, surfaced both on the case screen and in the `documents`
eligibility check's detail text. An event type with no named list falls back to the old
"at least one attachment" behaviour unchanged — nothing that used the plain toggle
before needs to change.

## 6. Multiple concurrent cases

Nothing in the case workflow ever restricted a member to one open case at a time, and
Phase 5 adds nothing that would: levies, funding targets and document checklists are all
scoped to the individual case, so two open cases for the same member never cross-
contaminate each other's money or paperwork. The annual claim-frequency cap already only
counted *decided* cases (Phase 1) — confirmed, not changed, and covered by new tests
proving an undecided second case never blocks eligibility for a first, and a decided one
correctly does count against a later claim.

## 7. New models & fields

`CaseEvent` (new model, `models_case.py`) · `BenevolentCase.funding_target` +
`funding_target_set_by/_at` · `BenevolentCase.bereaved_levy_waived` +
`_decision_reason/_decided_by/_decided_at` · `SchemePolicy.bereaved_contribution_policy`
+ `bereaved_reduction_percent` (replacing `bereaved_exempt_own_levy`, removed via a
three-step migration: add → translate → remove, the same pattern Phase 3 used for the
status/standing split) · `BenevolentEventType.required_documents` ·
`CaseAttachment.document_type` · `BenevolentSettings.notify_on_funding_target_reached`.

## 8. Deliberately not in Phase 5

* **The case list view has no funding-progress column.** The case detail screen — where
  a treasurer actually works a case — carries the full progress bar and history; adding
  it to the list too is a reasonable follow-up, not done here to keep the change
  surgical.
* **A funding target cannot be enforced** (e.g. "do not approve until reached") — it
  remains purely informational, deliberately, per the objective's own framing of it as a
  goal rather than a rule.
* **COMMITTEE_DECIDES supports only a binary ruling** (waived / contributes in full),
  not a committee-set custom reduced amount for one specific case. The brief asked for
  "committee approval" as one of four options, not an open-ended per-case override; a
  scheme wanting a genuinely custom figure still has REDUCED at the policy level.

Tracked in `docs/recommendations.md` (#65).

---
---

# Phase 6 — Policy Evaluation & Committee Management

## What this phase actually is

Almost everything the objective names — a policy evaluation engine, committee
structures, approval workflows, quorum, overrides, waivers, exemptions, renewals,
transfers, household inheritance, death processing, inactivity, reinstatements — was
already built across Phases 1–5. This phase is a deep audit of that existing ground,
not a rebuild: it looks for the places where "committee" meant "any authorised
individual," where "the policy says X" was never actually checked, and where an
override's paper trail was thinner than it should be — and fixes exactly those, using
the machinery already in place wherever it fits.

## 1. A real gap: the committee had no roster

`core.roles.can_vote_benevolent` already existed as its own right, deliberately
separate from the treasurer role — good design, already in place. What it could not
do: distinguish *whose* committee a person is on, in a church running more than one
scheme, or give any seat a named role. `CommitteeMember` (per scheme, with a Role —
Chair, Vice-chair, Secretary, Committee treasurer, Member) fills that in, and does it
additively: `record_vote()` only enforces roster membership once a scheme has actually
seated someone; a scheme that never configures one behaves exactly as before.

## 2. Approval levels: `committee_requires_chair`

A quorum is a headcount. An approval *level* is "and one of them must be this specific
seat" — a distinction the brief names explicitly ("configurable committee roles and
approval levels") and the old quorum-only model could not express at all. When set,
`committee_state()`'s `carried` is false until the Chair specifically has voted, however
many ordinary approvals are recorded, and `approve_case()` gives a Chair-specific error
rather than the generic quorum message. Ignored — not an error — where the scheme has no
Chair seated, so turning the flag on can never silently deadlock a committee that has
not been fully set up.

## 3. A real bug: the reinstatement fee was never charged

`SchemePolicy.reinstatement_fee` has existed since Phase 2, editable on every policy
form, frozen into every policy snapshot — and read nowhere. A church could configure a
fee and it would simply never be charged. Fixed by reusing Phase 4's obligations ledger
(`MemberAdjustment`) through a new auto-approved entry point,
`engine.charge_policy_fee()` — the same reasoning Phase 5 established for
`grant_policy_exemption()`: a published, constitution-set fee is not a new decision that
needs a second signature the moment it applies; it is the same decision, applied.
`registry.reinstate()` now raises it automatically, marked `automated=True` so it is
never mistaken for a treasurer's own discretionary penalty.

## 4. The policy engine gets a second business rule

Case eligibility has always produced a list of `Check` objects — transparent, one shape,
reusable. Reinstatement was decided by two hardcoded lines with no visibility into what
the policy actually says. `eligibility.evaluate_reinstatement()` extends the *same*
`Check` shape to a second rule (the fee, the waiting-period consequence), used to log a
structured, honest account of what reinstating someone will actually do — advisory only;
nothing here blocks the reinstatement itself, since bringing a person's record back to
ACTIVE is an administrative act, not a benefit decision.

## 5. Every override now carries a policy reference and a comments field

`MembershipExemption` and `MemberAdjustment` gained `policy` (the version in force when
the decision was made — so a later policy change can never retroactively make an old
exemption look like it was decided under rules that did not yet exist) and `comments`
(supplementary context, kept distinct from the required `reason`). Populated
automatically on every creation path, discretionary and automated alike.

## 6. Auditability, consolidated

`/benevolent/overrides/` — cases approved despite failing a check, committee votes,
exemptions granted, charges and waivers approved, all in one filterable, read-only
screen. Previously scattered across four different places, each showing part of the
picture; a board or an external auditor now has one.

## 7. Confirmed solid, deliberately untouched

Renewals (`record_fee`/`_advance_renewal`), transfers and household inheritance
(`registry.transfer`), member death processing (`registry.record_death`), and inactivity
calculations (`standing.py` + the automation job) were all audited during this phase and
found to already correctly consult the policy fields that govern them. Nothing was
changed there — reused, not rebuilt, exactly as instructed.

## 8. Deliberately not in Phase 6

* **A committee seat does not itself grant the general right.** Seating someone who
  does not separately hold `benevolent_committee` still leaves them unable to vote —
  intentional (a seat is *who*, the right is *may they at all*), but worth a treasurer
  knowing when a vote unexpectedly fails.
* **`committee_requires_chair` only names the Chair**, not an arbitrary configurable
  seat (e.g. "requires the Secretary"). The brief's "approval levels" is satisfied by
  the Chair case specifically; a fully generic "requires role X" is a natural, easy
  follow-up if a church needs it.

Tracked in `docs/recommendations.md` (#66).

---
---

# Phase 7 — Financial Integration & Communications

## 1. Financial integration: confirmed, not rebuilt

Every piece of financial infrastructure the objective names was already correctly
integrated, since Phase 1 and reinforced through Phase 4:

* **Expense Voucher / Payment Register** — `record_payout()` creates an ordinary
  `cashbook.Expense`, PENDING, through the normal approval workflow. Never approved
  by this module.
* **General Ledger** — every contribution, fee, refund and payout posts DR/CR through
  `ledger.services.posting`, exactly like any other transaction.
* **Bank Reconciliation** — the Phase 4 statement-import hook recognises scheme money
  by narration, banks it immediately, and only asks *whose* it is afterward; nothing
  about that changed.
* **Chart of Accounts** — the BENEVOLENCE expense category and each scheme's own
  fund are ordinary chart entries, not a parallel structure.
* **Audit Log** — `CaseEvent`, `MembershipEvent` and `django-simple-history` between
  them already cover every decision this module makes.

Confirmed with a full contribution → case → payout cycle re-run under this phase's
new code and checked against `ledger.services.posting.accounting_equation()` — still
balanced. Notifications are a side effect of these workflows now, never a precondition
for them: a failed SMS can never stop a case being approved or a voucher being paid.

## 2. The real gap: nothing ever reached a member

Every notification before this phase went to STAFF. `BenevolentSettings` already
carried `member_channel`/`staff_channel` fields (added in Phase 2, defaulting
`member_channel` to SMS) and `member_email()`/`member_sms()` helper methods —
declared, and never once called. Worse: `registry.py` had a `_notify()` function
whose own docstring said *"Tell the member, where the settings say to"* — and then
called the STAFF notification path regardless, gated by a settings field
(`notify_member_on_enrolment`) that was never true because nothing ever set it. A
confirmed bug: intent and implementation had quietly diverged.

Fixed by building the pathway that was missing rather than patching the broken one:
`services/notify.py` renders a configurable template and delivers it to the actual
member's phone (`member.receipt_phone`) or email (`SchemeMembership.email` — new
this phase; `members.Member` has no email field at all, so this is scoped to the
benevolent module specifically) or, for committee notices, the seated member's own
`User.email`. The old, broken `registry._notify()` is gone.

## 3. Configurable templates, reusing the existing engines

`NotificationTemplate` — one editable row per (event, channel) — nine events named
directly from the brief's own list (registrations, renewals ×2, contribution
reminders, case notifications ×2, committee approvals, benefit payments, membership
status changes), each on SMS and email. Placeholders use the exact `{name}` syntax
`core.services.sms` already established for the envelope receipt template — one
convention, not a second one invented here. Delivery goes through
`core.services.sms.send_sms` (already logging to `SmsLog`) and
`core.services.email.send_email` — the existing SMS Engine and Email Engine,
literally reused, never a parallel channel.

## 4. Notification history, delivery tracking, retries

`BenevolentNotification` is the record: event, channel, recipient, the rendered
message (frozen, so an edited template never rewrites history), status, attempt
count, and — for SMS — a direct link to the authoritative `SmsLog` row rather than a
duplicate status field. `retry_failed()` re-attempts FAILED rows, bounded per-row
(a configurable max) and per-call, riding the *existing* nightly
`benevolent_automation` schedule rather than a new one.

## 5. Contribution reminders — closing a gap that survived three phases

`arrears_reminder_days` and `renewal_reminder_days` have existed since Phase 2.
Recommendation #62c named them, three phases running, as fields that were stored,
displayed, and acted on by nothing — raised to HIGH priority in Phase 4's writeup.
`services.notify.send_due_reminders()` closes it: every active member currently in
arrears, or whose renewal falls due within the configured window, gets a reminder —
throttled to at most one every `reminder_min_gap_days` per member, so a nightly job
does not become a nightly text message. Wired into the same automation cycle that
already recomputes standing.

## 6. New models & fields

`NotificationTemplate`, `BenevolentNotification` (new module, `models_notify.py`) ·
`SchemeMembership.email` · nine `notify_member_*`/`notify_committee_*` toggles plus
`reminder_min_gap_days` on `BenevolentSettings` (replacing three narrower,
confirmed-dead `notify_member_on_*` fields via the same add → translate → remove
migration pattern established in Phase 3 and Phase 5).

## 7. Deliberately not in Phase 7

* **`members.Member` still has no email field.** Member-facing email is scoped to
  `SchemeMembership.email` specifically — a church wanting email addresses tracked
  centrally for every purpose, not just benevolent notices, needs that as a
  `members` app change, outside this module's phases.
* **No WhatsApp channel**, though `core.services.whatsapp` exists and is used for
  staff error alerts. SMS and email cover every event named in the brief; adding a
  third channel to nine templates each was judged a proportionate scope call, not an
  oversight — a natural follow-up if a church specifically asks.
* **The consolidated notification history has no CSV/PDF export** yet, matching
  recommendation #66c's note about the Overrides & Exceptions screen — both are
  reasonable candidates for the existing Report Engine rather than a bespoke export
  each.

Tracked in `docs/recommendations.md` (#67).

---
---

# Phase 8 — Reporting, Analytics & Dashboards

## The Report Engine already existed — this plugs into it

`core.reporting` is a mature, config-driven Report Engine already used by every
other report in the system: a `ComponentRegistry` of reusable sections, a
`ReportRegistry` of composed reports, and a `RendererRegistry` giving HTML,
CSV, XLSX, PDF and DOCX for free once a report is registered — no per-report,
per-format code. Phase 8's job was never to build a second reporting system for
this module; it was to plug into the one that already runs the Board Pack,
the Income & Expenditure Statement, and everything else in `/reports/`.

That is what `benevolent/report_components.py` does: thirteen
`ComponentSection` subclasses, one per named category in the brief
(operational dashboard, KPIs, contribution summary, membership, households,
committee, cases, fund balances, income & expenditure, arrears, benefit
payments, audit), composed into nine ready-to-use `Report`s registered from
`BenevolentConfig.ready()` — the exact same "register a plugin, the app
discovers it" pattern `benevolent/metrics.py` already established for the
Financial Metrics Registry in Phase 1.

## No new financial calculation

Every money figure is either read straight from the registry via
`ctx.metric(...)` — `benevolent_scheme_summary`, `benevolent_contributions`,
`benevolent_payouts`, `benevolent_fund_balance`, `benevolent_commitments`, all
from Phase 1 — or is a **breakdown** of one. `arrears_analysis()` is not a
second way to compute what a member owes; it is `arrears_for()` (the same
function `services/notify.py` and every membership screen already calls),
listed member by member, and its own sum is registered as the new
`benevolent_arrears` metric — so the KPI card and the arrears report can never
show two different totals for the same thing. `services/reporting.py`'s
`audit_summary()` deliberately reuses the exact query shape
`views_committee.OverridesExceptionsView` already built in Phase 6, rather than
re-deriving "what counts as an override" a second time.

## Historical accuracy

The case report reads `case.approved_amount` and `case.paid_total` — the
figures actually decided, frozen at approval time — never a live
re-evaluation. A policy published after a case was approved cannot retroactively
change what a report says was paid; a dedicated test proves exactly this
(changing a scheme's benefit amount after payment does not move the historical
figure).

## Filtering

Reports filter by scheme, typed as the scheme's own short code (e.g. `BEN`) —
the same code that already appears on every case number in this module — and,
where the underlying figures are period-based, by the engine's existing
`?start=`/`?end=` period parameters. A live scheme-picker dropdown was
deliberately not built: the engine's `Filter` dataclass has a `choices` kind,
but the shared HTML template does not yet render it, and populating one would
mean querying the database from `AppConfig.ready()` at process startup — a
fragile pattern this phase chose not to introduce. Recorded as recommendation
#68a rather than worked around silently.

## Performance

Every component uses grouped aggregate queries (`.values().annotate()`) or a
small, fixed number of queries per scheme — never one query per row. The
membership, case and benefit-payment tables cap at 2,000 rows in the HTML/PDF
view (each notes when it has truncated, and points at CSV/XLSX export, which
carry no such cap) so a very large register cannot make an on-screen report
unusable.

## New surfaces

Nine reports under Reports → **Benevolent** (also auto-listed at
`/reports/library/` — the engine's own catalogue, with search and favourites,
needed no code to pick these up) · thirteen components, reusable by the Report
Designer for any custom composition · one new metric,
`benevolent_arrears` · four new `services/reporting.py` functions
(`arrears_analysis`, `arrears_total`, `committee_report`, `household_report`,
`audit_summary`).

## Deliberately not in Phase 8

* **No live scheme-picker dropdown**, for the DB-at-startup reason above —
  the code-typed filter works, is fast, and is honest about the trade-off.
* **Scheduled reports use the engine's existing `ReportSchedule` mechanism**
  unmodified — a treasurer can already schedule any of these nine reports
  the same way they schedule any other; nothing benevolent-specific needed
  building for that part of the brief.
* **No AI-narrative component** for benevolent reports (the wider system's
  `AiBriefingComponent`/intelligence layer is a general-purpose narrative over
  the whole church's finances, not scoped per-module) — a natural future
  composition via the Designer rather than something this phase needed to add.

Tracked in `docs/recommendations.md` (#68).

---
---

# Phase 9 — Roles, Permissions & User Experience

## No separate permission system — extended the existing one

`core.rights` already had a working, general-purpose mechanism: named rights,
bundled into assignable **profiles** (`accounts.Profile`), layered on top of
the existing role groups. That is exactly the machinery the brief asks for —
"configurable... roles" — so Phase 9 adds to it rather than building a second
one alongside it.

## The real gap: one coarse right doing three jobs

`manage_benevolent` covered "enrol members, raise cases, record
contributions" as a single right — a Registration Officer, a Case Officer and
a Finance Officer were, until this phase, the same permission. Split into
three: `benevolent_register_members`, `benevolent_manage_cases`,
`benevolent_manage_finance`. Eighteen views across four files were re-pointed
from the old blanket `BenevolentManageMixin` to the specific mixin their work
actually needs.

**Nothing that worked before stops working.** Every new role-check function
is `can_manage_benevolent(user) or has_right(user, "<the specific right>")` —
the OLD coarse check, kept as a superset, first. A Treasurer, an Assistant, or
any profile still holding the old `manage_benevolent` right keeps every
capability it already had. The split only ever *narrows* who else can reach
these views; it never widens who already could. Proven directly: a test
constructs a profile with only the pre-Phase-9 `manage_benevolent` right and
confirms it still satisfies all three new checks.

## Seven seeded profiles, one per named role

Matching the brief's list precisely — Administrator, Approver ("Treasurer"),
Committee Member, Registration Officer, Case Officer, Finance Officer,
Auditor — seeded the same way every other default profile in this system is
seeded (`accounts/migrations/0008_benevolent_role_profiles.py`, following the
exact pattern of `0004_default_profiles.py` and `0005_elder_default_profile.py`).
Assignable to any user via the existing Users & Roles screen; no new UI was
needed because none was needed.

**"Committee Chairperson" is deliberately not an eighth profile.** Chairing is
a SEAT on a specific scheme's committee roster (`CommitteeMember.Role.CHAIR`,
Phase 6), not a different right — a chair holds the identical Committee
Member profile and is additionally seated as Chair. A new role-check helper,
`is_benevolent_committee_chair(user, scheme=None)`, answers the one question
the UI actually needs ("is this person the chair?") without duplicating the
committee roster as a second concept. A test proves the chair and an ordinary
member hold exactly the same right.

**"Optional Elder roles"** — the `benevolent_committee` right (and
`view_benevolent`) were already documented as assignable to an elder with no
other treasury access; this phase makes that concrete rather than merely
possible, since the "Benevolent Auditor" and "Benevolent Committee Member"
profiles are the natural, ready-to-assign choice for exactly that case.

## A real bug found while working on the settings page

The settings template hand-listed field names into tabbed sections — and
still referenced three fields Phase 7 had retired (so that whole section
silently rendered *empty*), while never having been updated for eight of
Phase 7's nine new fields or any of Phase 4's six allocation-tuning fields.
Roughly half the model's settings had been unreachable from the web UI since
Phase 4. Fixed comprehensively, added a new "Allocation" tab for the
previously-invisible intake-matching settings, and regression-guarded with a
test that walks every actual model field and confirms it appears on the page
— so a future field addition can never again go silently missing from the
form the same way.

## Dashboard: role-aware "your queues"

Each viewer's dashboard now shows only the operational queues their own role
actually covers — the intake queue count for a Finance Officer, cases
awaiting assessment for a Case Officer, cases awaiting approval for an
Approver, cases awaiting *their own* vote for a committee member, pending
admissions for a Registration Officer — computed only when the viewer holds
the matching right, so a Registration Officer's page load never pays for a
committee-vote query it will not show. A members'-arrears KPI card (reusing
Phase 8's `arrears_total`) was added alongside the existing balance/
contributions/payouts/committed cards.

## Navigation

A "Reports" link was added to the module's own nav dropdown, pointing at the
report library pre-filtered to Benevolent — Phase 8 built nine reports with no
way to reach them from inside the module itself; now there is one.

## Confirmed already solid

Search (member/case/registry list views already had it), progressive
disclosure on the policy form (already collapsible `<details>` sections, only
the first open by default), and accessibility basics (`aria-required` applied
centrally by `StyledFormMixin` to every form in the system, label/`for`
pairing consistent throughout) were all audited and found already correct —
confirmed, not rebuilt.

## New surfaces

Three new rights, three new role-check functions, three new permission
mixins, one committee-chair helper, seven seeded profiles, one new settings
tab, one new dashboard panel, one new nav link.

## Deliberately not in Phase 9

* **`CasePayoutView` (raising a payment voucher) stays under the Case
  Officer right**, not a separate finance gate — a defensible boundary
  (raising a voucher is the natural end of a case's own workflow; the actual
  money decision remains the ordinary, unrelated expense-approval gate) but a
  church that wants payout-raising restricted to Finance Officers specifically
  would need a further split.
* **No per-view UI hint explaining *why* a link is hidden** — a Registration
  Officer simply does not see the "raise a case" button, with no "ask a Case
  Officer" message. Consistent with how every other permission-gated control
  in this system already behaves (hide, don't explain), not a new
  inconsistency introduced here.

Tracked in `docs/recommendations.md` (#69).

---
---

# Phase 10 — Production Readiness & Final Review

## What this phase actually was

Nine phases had already built a complete, tested, documented benevolent
scheme engine. Phase 10 was not a tenth feature phase — it was the
self-review the brief asks for: read back through the whole module looking
for what a production deployment would actually surface, verify the claims
already made in this document against the real code, and fix exactly what
that review found. Four real things came out of it.

## 1. A severe, previously-invisible performance bug — found and fixed

`services/contributions._dues_rows()` — the function underneath
`arrears_for()`, called by the eligibility engine, the standing engine,
every Phase 8 report, the Phase 7 reminder job, and the dashboard, for every
active member — resolved "which policy was in force" once per **day** of a
member's history rather than once per call. A member of two or three years'
standing cost 700-1000+ database queries just to answer "what do they owe."
This had been true since the function was first written; nothing in Phases
1-9 was positioned to notice it, because every prior phase's tests checked
*correctness* (does arrears_for return the right number?) rather than
*query count* (how many round trips did it take to get there?).

Found by writing the test Phase 10 exists to write: build a scheme, measure
a screen's query count with a few members, add many more, measure again, and
assert the growth is bounded rather than assuming it. The very first run of
that test on the real dashboard showed queries growing from 716 to over
5,000 for seventeen extra members — unambiguous.

Fixed by resolving a scheme's policy history once per call (a single query,
cached in memory) instead of once per day — the exact same resolution rule
`BenevolentScheme.policy_on()` already uses, just run in Python against an
already-fetched list instead of against the database, repeatedly. The full
pre-existing test suite — 1,331 tests spanning every phase of this module
plus the reports, core and accounts apps — passed **unchanged** after the
fix, which is exactly what should happen: the answer never changed, only how
expensively it was computed. Several phases' worth of tests that exercise
arrears calculations ran roughly twice as fast as a direct, measurable
side-effect.

A smaller, related cost remains (`_dues_rows()` still calls
`contributions_total()` once per dues period rather than once per
membership) and is honestly logged as recommendation #70b rather than rushed
through at the end of a long build — a different scale of problem, and one
that touches the "how much has been paid" calculation at the center of the
arrears engine, which deserves its own careful pass.

## 2. The Scheme Engine claim, proved rather than asserted

`BenevolentScheme.Kind` has offered MEDICAL, EDUCATION and EMERGENCY
alongside BENEVOLENT since Phase 1, and the Constitution Wizard has asked
"what is this scheme for" — with the same five options — since Phase 2. A
"Medical assistance (percentage of cost)" policy profile, complete with its
own event types (Hospitalisation, Surgery, Chronic illness), has existed
since Phase 2 too. These were real, working pieces of a genuine Scheme
Engine — not a rebrand of a bereavement fund — but nothing had ever *proved*
it by actually running a non-bereavement scheme through the full lifecycle.

Phase 10 does exactly that: `test_phase10.py` builds a Medical Fund from the
existing built-in profile and runs it — case raised with no prior membership
(the profile doesn't require one), assessed against a percentage-of-cost cap,
routed to committee, voted on, approved, paid, and reported through the exact
same Phase 8 report engine every bereavement scheme uses — using zero new
model fields, zero new service functions, zero new views. A second test does
the same for a newly-added "Emergency relief (fast, fixed amounts)" built-in
profile (treasurer-only approval, for speed; no membership or waiting
period), proving the claim a second, differently-shaped way. A third confirms
a bare-bones Education Fund correctly produces the same registration
notification every bereavement scheme's member already gets.

## 3. Wording that assumed bereavement, fixed

The wizard's "does the member a case is about contribute to their own case"
section was titled "The bereaved member" — correct for the common case,
mildly wrong-sounding for a hospital bill or a school fees claim, despite the
underlying mechanism applying identically to either. Retitled to "The member
a case is about" in the wizard, the policy form's group label, and the case
detail screen — presentation strings only; the underlying field names
(`bereaved_contribution_policy` etc.) were deliberately left alone, since
renaming a data model field for wording reasons alone is a migration for no
functional gain.

## 4. A dead setting, finally traced to its actual cause and fixed

`notify_committee_on_pending_vote` — flagged in Phase 9's review
(recommendation #69c) as confirmed-dead but not yet investigated — turned
out to have a precise, findable cause: its name didn't match the
`notify_on_<event>` convention `_notify()`'s lookup depends on, so no amount
of toggling it could ever have had an effect, for any deployment, ever.
Renamed to `notify_on_committee_pending` (the same add -> translate ->
remove migration pattern used throughout this project) and wired to fire
exactly once, the first time a case reaches committee — a staff-facing
counterpart to Phase 7's member/committee-facing `notify_committee_vote_
needed`, not a duplicate of it.

## The module is complete

Ten phases: the policy evaluation engine and case workflow (1); committee
approval, constitution wizard and policy versioning (2); the member registry
and standing engine (3); the contribution engine and intelligent allocation
(4); bereavement case management with funding targets and document
checklists (5); committee roles, approval levels and a real reinstatement-fee
bug fixed (6); financial integration confirmed and member/committee
notifications actually wired (7); reporting plugged into the system's
existing Report Engine (8); role-based permissions extending the system's
existing rights framework (9); and this phase, closing the loop on
production readiness. Every phase audited before building, reused before
duplicating, and — where a real bug turned up along the way, as one did in
nearly every phase — named it, fixed it, and tested that it stayed fixed.

`docs/recommendations.md` remains the honest, permanent record of what was
deliberately left for later and why. Nothing in it is an oversight; it is
the module telling the truth about its own edges.

---
---

# Production Fixes & Requested Features (post-Phase 10)

Four real bugs reported from production use, and three features requested
directly, addressed in one pass rather than deferred to a future phase.

## Bugs

**The "Admit" button on a pending membership's own page produced "Unknown
action."** Root cause: the button posted to `MembershipLifecycleView`, which
handles suspend/reinstate/withdraw/deceased/close/refuse/transfer but never
admit — that action has always lived on a separate view,
`MembershipAdminView` (registration/renewal/fee actions). A template wiring
bug, not a missing feature: the button pointed at the wrong URL. Fixed, and
`MembershipAdminView`'s admit/reinstate actions now also read the reason/date
fields from the shared lifecycle form they sit inside, which they previously
silently ignored.

**Marking one bank transaction as a manual receipt could change several
unrelated ones.** Root cause: `Transaction.split_siblings()` (giving app)
OR'd together three different ways of finding "the same split contribution's
other rows" — a bank-assigned core_ref, an M-Pesa reference, and a plain
reference-plus-date. The last of these is payer-entered free text ("BEN
DUES", "TITHE"), which different, unrelated people routinely enter
identically on the same day — and because the match was a union rather than
a fallback, even a transaction WITH a solid, unique core_ref was still ALSO
matched against everyone else's payment sharing its narration. Fixed to a
genuine fallback chain: the strongest identifier available, and only that
one. The codebase already had a stricter sibling of this function
(`strict_split_siblings`, used by "send back to review") with exactly this
reasoning already written down for why the loose match is dangerous — it
had just never been applied to this one.

**The type-ahead name popup used across the app (cash entry, envelope
ledger, cashbook, and now benevolent forms) rendered incorrectly.** Root
cause: its CSS (`.ac-box`/`.ac-item`) was copy-pasted into three separate
templates with drifting details, and the shared stylesheet carried only an
orphaned partial rule. Consolidated into one definition in `app.css`; the
one template that genuinely needs different positioning (the envelope
ledger's table-embedded search) now overrides only that.

## Features

**Bulk roster import**, for a church bringing a scheme it already runs on
paper into the system rather than registering members one at a time —
`/benevolent/schemes/<id>/import/`. Every row becomes an ordinary
`registry.register()` call, dependants included; "mark paid up" clears
whatever arrears the import would otherwise show through a visible,
auto-approved waiver record dated today ("migrated from prior records"),
never a fabricated payment history. No welcome notification fires for an
import — a member of several years' standing does not need to be told they
have just joined.

**Member/membership search for benevolent forms**, replacing plain
`<select>` dropdowns (unusable once a church roll runs into the hundreds) on
the register, contribution, and case forms — reusing the same proven
type-ahead pattern as the rest of the app, through two new endpoints scoped
correctly for Phase 9's role-specific users (a Registration Officer holds no
Treasurer/Assistant group membership, so the general, treasury-gated search
endpoint would have refused them).

**A dependant's own phone**, for allocation matching — the field
(`SchemeDependant.phone`) has existed since Phase 4, documented for exactly
this purpose, but neither the household-add screen nor its service function
ever accepted one. Fixed on both.

**A standing snapshot on a case's own page**, for schemes funded by ongoing
dues rather than a per-case levy. A levy-funded scheme has always had this —
`raise_case_levy()`'s roster shows exactly who has and hasn't paid towards a
specific case. A dues-funded scheme has no equivalent "per case" question,
but "who currently stands where" is the same underlying need, answered by
grouping the standing engine's own already-computed field rather than
introducing a new calculation.

## Bearing on the "Scheme Engine" design question

Edwin separately asked whether the module, as built, can express three
specific registration/funding patterns (monthly dues with a bereavement
lump sum; optional annual renewal; a registration-fee-only pattern where
ongoing giving happens per case, not periodically) and a set of edge cases
around them (spouse/dependant contribution linking, member death and
beneficiary handling, deregistration, inactivity, concurrent cases, a
committee paying from the fund balance without requiring member
contributions, and phone-based allocation matching). That audit, and the
phased plan for what it found still genuinely missing, is written up
separately as the proposed next phase — see the plan delivered alongside
this update rather than duplicated here.

---
---

# Phase 11 — Guided Scheme Setup & Allocation Transparency

Implemented as proposed: a plain-language guide connecting Edwin's three
described funding patterns to the profiles that already implement them; an
explicit, logged "fund this case from the balance" decision for levy-funded
schemes (record_payout() never required a levy — this makes skipping one a
stated choice, not an unstated one); and a "Matched via" column on the
intake queue surfacing signal data the allocator already froze onto each
row but never displayed.

## Also in this pass: production fixes and two audited-and-confirmed items

**The Admit button, fixed.** Wired to a view with no admit handler —
`MembershipAdminView` has always had one, `MembershipLifecycleView` never
did. A template wiring bug, not a missing feature.

**A real bug in `split_siblings()` (giving app), fixed.** Marking one bank
transaction as a manual receipt could sweep in unrelated people's payments
that merely shared a generic reference and date — the function OR'd three
match conditions instead of falling back through them. Fixed to a true
fallback: the strongest identifier available, and only that one.

**The autocomplete popup, fixed.** Its CSS was copy-pasted into three
templates with drifting details; the shared stylesheet held only an
orphaned fragment. Consolidated into one definition, and used for new
type-ahead widgets on benevolent's register/contribution/case forms via two
new endpoints — scoped correctly for Phase 9's scheme-specific roles, which
the general, Treasurer/Assistant-only search would have silently refused.

**A severe font-loading bug in the budget PNG export, found and fixed.**
`goal_chart.py`'s font loader depended on a system package
(`fonts-dejavu-core`) not guaranteed on a server, and its fallback —
Pillow's fixed-size bitmap default — was catastrophically incompatible with
the file's 4x print-quality render scale, producing PNGs where the figures
were technically present but rendered as near-invisible specks. Fixed to
fall back to reportlab's own bundled TTF fonts, present in every
environment that can run this application at all.

**Two unbounded-by-default list views, fixed.** `/transactions/` and
`/expenses/` loaded every row ever recorded on a bare visit; now default to
the current month, while any other filter (search, amount, member — with
no date bound) still searches all time exactly as before. A third, more
severe instance was found while checking "other pages": the envelope list
was scanning every envelope ever recorded on every visit regardless of the
month selected, discarding almost all of it in Python — fixed to filter at
the database level.

**Two items audited and confirmed already correct, proven with new
regression tests rather than merely re-read:** the Member model's
additional-phone infrastructure (`MemberPhone`) is fully populated during a
duplicate merge, and contribution matching already recognises a gift from
any of a member's known phones, not just their primary one — including the
"not contributed to campaign" SMS criterion, which inherits this correctly
because it reads the same `Transaction.member` attribution
`match_or_create_member()` already gets right.

**A new standalone seed command**, `seed_benevolent_demo`, for benevolent
test data without running the full demo first — reuses the existing
seed_demo seed chain rather than duplicating it, filling in the one
prerequisite (a pool of church members) that chain has always assumed was
already there.

---
---

# Production Fixes & Requested Features, Round 2

Eleven items reported directly from production/local testing. Four were real
bugs (two of them recurrences or deeper instances of issues thought already
fixed); two were confirmed as already fully built, just not discoverable or
exposed; the rest were genuine, well-scoped feature gaps.

## The two "still broken" reports — found at their true root cause this time

**"Marking one item as manual receipt still marks the rest."** The previous
fix (a true fallback chain instead of an OR) was correct as far as it went,
but not sufficient: `split_into()` never created a real, queryable
relationship between the parts of a split — `split_siblings()` was always
*inferring* the relationship from shared reference text, core_ref, or
mpesa_ref, and even the strongest of those is still an inference, not a
certainty. Two unrelated CASH gifts (no core_ref, no mpesa_ref) sharing a
generic reference and date on the same day would still, correctly by the
old design, be treated as siblings. Fixed properly this time: `Transaction`
now has a real `split_of` foreign key, set by `split_into()` on every part
it creates. `split_siblings()` and `strict_split_siblings()` check this
first and only fall back to text inference for historical rows that predate
it — which a migration backfills automatically, by parsing the
`[Split of #N]` tag `split_into()` has always written into `raw_narration`.
Proven with a test that reconstructs the exact remaining failure mode: a
genuine split plus an unrelated gift sharing its reference and date, and
only the genuine split's sibling is found.

**"The envelope ledger popup still appears outside the grid."** A genuinely
different cause from the CSS-consolidation fix in the previous round: the
popup used `position:fixed`, positioned correctly in JS via
`getBoundingClientRect()`, but was left nested inside `.content` — the
page's main wrapper, which runs a `transform`-animating entrance animation
on every load. Per the CSS spec, an element animating `transform`
establishes a new containing block for any `position:fixed` descendant, so
the popup's "fixed" coordinates were being resolved against `.content`'s own
box, not the true viewport. Fixed by moving the popup to a direct child of
`document.body` the first time it's shown — the standard "portal" pattern
for exactly this — with cleanup on row removal and table rebuild so it
cannot accumulate orphaned DOM nodes over a long editing session.

## Confirmed already built, made discoverable

**Segregation of duties for case approval** ("a benefit must be approved by
someone other than the person who raised it") already existed, correctly
enforced by default — but was hardcoded, with no way for a very small scheme
(one treasurer, no assistant) to switch it off. Added
`SchemePolicy.require_different_approver` (defaulting to the existing,
safer behaviour) so it's now a real setting, not a fixed rule.

**Case-count-based inactivity** ("deactivate a member who hasn't
contributed to the last N cases") was fully built and correctly wired into
the standing engine — `missed_case_levies()` already existed, already
excluded a member's own bereavement from counting against them, already
handled the exact scenario Edwin named (a levy scheme with no monthly dues,
where time alone says nothing if there simply haven't been recent cases).
The gap was narrower than "missing feature": `inactivity_missed_cases` was
never exposed on the policy form, so nobody could actually turn it on.

**Recording a cash payment** is already fully supported —
`ContributionForm`/`record_contribution()` have always accepted
`channel=CASH` alongside bank/M-Pesa.

## New

**The register form no longer requires an existing church-roll member.** A
benevolent scheme is its own thing, per Edwin's own framing — the form now
offers "…or type a name" (mirroring the pattern already used for a spouse),
matching or creating a `members.Member` on the fly. Also fixed, in the same
pass: a hidden `<select>` silently skips the browser's native "please fill
this in" validation, which was a real, separate cause of "the button
doesn't seem to do anything" — the required-field cue now lives on the
visible search box instead.

**Marking a dependant as deceased** — a dedicated action distinct from
generic removal (which could mean anything from moving away to a
correction), logging why, and prompting toward raising a case. The
household view now keeps a deceased dependant visible with that status
shown, rather than letting them silently vanish the way a dependant removed
for any other reason correctly does.

**Bulk import extended to contribution history**, alongside the existing
roster import, with proper discoverability this time — both are now linked
directly from the scheme's own page, not only reachable by already being on
the registration form.

**A year selector on the contributions list** (already paginated), and **a
member directory report** — every member's own information alongside their
dependants in one place, filterable to active or inactive only, through the
existing Report Engine.

**Tests:** 56 new in this round. Full regression clean across the whole
application — 2,282 tests total across benevolent, giving, cashbook,
envelopes, members, reports, core and accounts.

---
---

# Full-Module Audit

A systematic sweep of the whole benevolent module — every model field, every
view, every permission, every report, every export format, the wizard, the
notification wiring, accounting integrity, and query counts under load.

## Four real issues found and fixed

### 1. Six enforced policy rules were unreachable from the UI (the serious one)

`arrears_block`, `grace_period_days`, `exemption_age`, `max_household_size`,
`allow_exemptions` and `allow_transfers` are all **genuinely enforced** — an
audit probe confirmed each one really does block a transfer, refuse an
exemption, cap a household, produce GRACE standing, make an owing member
ineligible, or exempt an older member. But none of them appeared in
`PolicyForm.GROUPS`, and `grouped()` silently skipped any field not listed
there, so the template rendered nothing for them.

A treasurer could not configure rules the system was nonetheless enforcing
against their members. This is the same shape as the settings-page bug found
in Phase 9 — which is why the fix is not merely "add the six fields" but
"make `grouped()` structurally incapable of dropping a field again": any
field not in a group now lands in a visible "Other settings" group. A missing
field is a five-second fix someone notices; an invisible one is not.

### 2. A duplicate, inferior registration path

`MembershipCreateView` (Phase 1) still rendered its own enrolment form — no
households, no dependants, no off-roll registration — reachable by URL though
nothing has linked to it since Phase 3. Two divergent code paths for one job,
one of them strictly worse, quietly waiting for a stale bookmark to find it.
Now redirects to the real registration screen; the duplicate form and template
are deleted.

### 3. The remaining N+1 (recommendation #70b), closed

`arrears_for()` cost ~22 database queries **per member**, and it runs for
every active member on the dashboard, the arrears report, the overview report
and every standing recomputation. Three causes, all fixed: `contributions_
total()` called once per dues PERIOD (now one grouped query for all periods);
the scheme's policy-window boundary re-queried after `_dues_rows()` had
already computed it; and `_waived_periods()` re-fetching the same policy
versions `_dues_rows()` had just loaded.

Now **6 queries per member, flat** — each one a distinct per-member table, not
a repeated lookup. Dashboard query growth fell from 22.5 to 7.3 per member, a
68% reduction. Every number is unchanged: the full pre-existing suite passes
untouched, which is exactly what should happen when only the cost of an answer
changes, not the answer.

### 4. Two dead functions, one with a false docstring

`periods_between()` claimed to be *"the single definition of 'which periods
have fallen due', shared by the arrears calculation and the dues schedule"* —
a claim Phase 10's N+1 rewrite had quietly made false, since `_dues_rows()`
stopped calling it and nothing else ever did. A dead function asserting it is
the source of truth for a rule that has since moved is worse than no function:
it is precisely how a future fix gets made in the wrong place. Removed, with
the reason recorded where it stood. `refresh_arrears_status()` — a
"backwards-compatible" shim with no callers anywhere — went with it.

## Confirmed sound under probing

Everything below was tested by actually exercising it, not by re-reading the
code that claims it:

* **Accounting integrity** — ledger balances; every registry metric agrees
  with its reporting-service equivalent; no orphaned contributions, payouts,
  policy-less approved cases, unnumbered memberships or unapproved-but-effective
  adjustments.
* **Historical accuracy** — a case assessed under one policy is unmoved by a
  later, more generous one published before approval. Arrears across a
  mid-history dues change correctly charge the old rate for old periods and
  the new rate for new ones.
* **Edge cases** — zero benefits, over-payouts, two payouts summing past the
  approved amount, double approval, future-dated events, negative
  contributions and duplicate enrolments are each correctly refused.
* **Permissions** — all 22 benevolent pages × 10 roles: no 500s, and every
  role sees exactly what Phase 9 designed.
* **Reports** — all 10 reports × 5 formats (HTML/CSV/XLSX/PDF/DOCX) = 50
  combinations, all working, scheme-filtered and unfiltered.
* **Notifications** — every event maps to a real settings toggle; every
  placeholder used in every default template is one that event's context
  actually provides.
* **The wizard** — every `depends_on` refers to a real question; every field
  `build_config()` sets is a real policy field; its own default answers
  produce a valid policy.
* **Templates** — every `{% url %}` name in every benevolent template resolves.

**Tests:** 18 new. Full regression clean across the whole application — 2,316
tests.

---
---

# Round 3 — reported issues

## The headline: the member-search widget had never worked

Not "worked badly" — **never displayed a single suggestion to anybody, in any
form, since the day it shipped.**

`query()` resolved to the endpoint's JSON envelope `{results: [...]}` and
handed that whole object to `renderResults()`, which immediately tested
`results.length` — `undefined` on an object — and hid the box and returned.
The endpoint was fine. The CSS was fine. The request was even being made and
answered. The answer was thrown away one line before it could be rendered, on
every keystroke, in every form that used it.

Nothing in the Django suite could see it: the failure lived entirely in the
browser. It is now guarded by a jsdom test
(`tests/js/member_search.test.js`), which was first run against the
pre-fix code to confirm it actually catches the bug.

The file was also renamed `benevolent-search.js` → `member-search.js`. It was
never benevolent-specific, and a misleading name is how a second, duplicate
copy gets written by someone who did not think to look inside a module they
were not working on. It now serves benevolent, the membership page, and the
pledge form.

## Alternate phone numbers were invisible to every search screen

`MemberPhone` has always recorded a member's other numbers, and the
bank-statement matcher has always searched them. The search *screens* did not.
So a treasurer typing the very number that appears in the narration in front of
them was told the member did not exist — and pushed into creating a duplicate
for someone the system already knew. Fixed on the members list, the shared
typeahead, and the benevolent typeahead.

## Registering someone already covered as a spouse

The search now warns ("already registered as spouse of X"), and — the part
that actually matters — the server **refuses** it. One person must not end up
with two memberships in one scheme: counted twice on the roll, levied twice,
able to claim twice.

## How does a contribution know which case it belongs to?

Three ways, and one of them was broken:

1. **The case's own levy screen** — sets it explicitly. Always worked.
2. **The bank-statement allocator** — reads the case number out of the
   narration (weight 55). Always worked.
3. **The general contribution form** — could not name a case *at all*.

That third gap was doing real damage. `record_contribution()` correctly infers
`kind = LEVY` from the presence of a case — so with no case, a levy recorded on
that form was filed as `VOLUNTARY`, attached to nothing. The member stayed
"unpaid" on the case's levy roster. And under a **POOLED** policy — where the
benefit *is* whatever the levy collected — the payout itself came out short.

The form now offers the case. Cases that are closed, rejected or cancelled are
excluded; **draft cases are deliberately included**, because a church starts the
harambee the moment a death is known, long before the paperwork catches up, and
refusing to attribute that money would be the system telling a treasurer their
own practice is invalid.

## Founding balances could silently rewrite the whole history

`Department.opening_balance` is the **founding** brought-forward figure — what a
fund held on the day the church started using this system. It is *not*
year-scoped: every later year's opening is **derived** (founding + all movement
before that year), and year-end close never writes it. The architecture was
already right.

But the budget page let a treasurer edit it while labelling it "opening balance
for <year>". Changing it in July did not set July's opening — it silently
rewrote every fund balance in every year the church had ever recorded,
backwards. Now: the page shows each fund's **derived** opening for the year
being budgeted (the number a treasurer actually came for), the founding figure
is labelled honestly as one-time, and it is **frozen once any year has been
closed** — a close is the church declaring that history final, and editing its
foundation afterwards would rewrite an audited past. Enforced server-side, not
just hidden in the template.

## Also

Pledge form gets the member typeahead. Campaign detail gets search, status
filter, newest-first ordering (so a fresh import is at the top) and inline
edit/delete, so a wrongly-allocated import row is correctable from where a
treasurer actually looks for it. The transfers page gets filters (date, fund
either side, text) and the current-month default. The member page gets a date
filter — defaulting to the current **year**, not month, because unlike the
unbounded list pages this one is already scoped to one person, and a
one-month window would hide most of what someone opened the page to see. Its
lifetime total never moves with the filter.

**Tests:** 23 new Django + 16 new jsdom. Full regression clean.

---
---

# Round 4 — the Bank Statement Register, and the public application form

Both are deliberately **separate layers**. Neither can affect the ledger.

## The Bank Statement Register

A running record of what the **bank** says happened — every line it ever sent,
kept forever, unjudged. It never posts, allocates, creates a transaction, or
touches a fund balance. That is not a limitation; it is the entire point. A
register a treasurer could quietly "correct" would be worthless as a check on
their own books.

**Why not just reuse the existing statement importer?** Because that one's job
is the opposite: it turns bank rows *into* ledger transactions — it allocates,
matches members, and posts. A row it cannot allocate goes to a queue; a row it
skips as a duplicate leaves no trace. The register keeps everything, asserts
nothing, and is therefore **safe to re-import over any period, as often as you
like**. Importing from January every month is a sensible thing to do here; every
line is deduplicated on the bank's own reference.

**Exceptions** are the two questions asked directly:

* *On the statement, not in our books* — real money the bank says moved, that we
  have not recorded.
* *In our books, not on the statement* — we assert a bank movement the bank does
  not. Rarer and more serious.

Matching is **by bank reference only** — the M-Pesa receipt or the core banking
ref. Amount-and-date matching is deliberately not attempted: two members giving
the same amount on the same day is completely ordinary, and guessing there would
manufacture exactly the false reconciliation this exists to prevent.

Two design decisions worth stating, because both were corrections made *during*
the build after the first version got them wrong:

1. **The check is bounded to the period the register actually covers.** The
   first version compared our whole ledger against whatever the register held —
   so with only July imported, all of June was flagged as "missing from the
   bank". But the register has no June data; it cannot assert anything about
   June. That is an absence of evidence, not a discrepancy, and reporting it as
   one buried the handful of real exceptions under hundreds of false ones. That
   is exactly how a discrepancy report gets ignored.

2. **A bank transaction carrying no bank reference is "unverifiable", not an
   exception.** We cannot say the bank disagrees — only that we have no way to
   ask. Calling it a discrepancy would be an accusation the evidence does not
   support. It is surfaced separately, where it is actionable.

The bank's own running balance is kept alongside ours. Where they diverge, the
register is **missing lines the bank included** — the clearest possible signal
that a statement period was never imported.

### A real bug this uncovered in the shared parser

`dayfirst=True` was scrambling ISO dates. dateutil, told day-comes-first,
applies that to the `07-01` portion of `2026-07-01` even though a leading
four-digit year makes the order unambiguous — so **1 July was being read as 7
January**. Any bank exporting ISO dates was having its statement silently
misdated by up to eleven months, in the **ledger importer** as much as in the
register. Fixed in the shared parser; the full statements and giving suites pass
unchanged.

## The public application form

Off by default. An application is **not a membership**: nobody who submits one
is covered, owes dues, or can claim, until a registration officer approves them
— at which point they are registered through exactly the same
`registry.register()` as anyone enrolled at the desk.

Security follows the public pledge form's model, which was designed for this
problem:

* **Write-only.** It never reads or exposes member data — no autocomplete, no
  lookup, no roll. A public form that could search the membership would leak it.
  The applicant types their own details; a reviewer links them to the real church
  record afterwards, and is shown phone-matched candidates so one person ends up
  with one record rather than a duplicate.
* Honeypot, minimum fill time, per-session throttle.
* A submission touches no ledger, no fund, no balance, and creates no cover.

The applicant says what they are — **registered member / Sabbath School member /
visitor** — and that is recorded as their *claim*, unverified. Checking it is
what the review is for.

Dependants are captured in the three sections a family is actually described in
— **spouse, children, parents** — rather than one undifferentiated list that
makes an applicant guess where their mother goes. A dependant's own phone is
asked for, because a spouse or grown child very often pays from their own line,
and a number recorded here lets that payment be matched to the family
automatically instead of landing in an unmatched queue.

**Tests:** 36 new. Full regression clean — 2,436 tests.

---
---

# Round 5 — reported issues

## The serious one: the register's matching was crying wolf (item 4)

Reported: *"Many entries being detected as not in our books, but when searching
I found them."*

Two bugs, both mine:

1. **The date window was being used for MATCHING, not just for reporting.** A
   bank reference is unique *forever* — if any transaction carries it, the line
   IS in our books, whatever date it happens to be recorded under. But the match
   index was built only from transactions inside the reporting window, so a
   payment the bank value-dated 1 July that the treasurer entered on 30 June
   (when the SMS arrived) fell outside it, and its statement line was flagged as
   missing. Value date and entry date differing by a day or two is completely
   ordinary.

2. **Transactions were not scoped to the account being checked**, so a church
   with two bank accounts had every transaction of the second flagged as missing
   from the first — where it was never supposed to be.

The date window's job is to decide **what we report on**, never **what
matches**. A reconciliation that cries wolf is worse than none, because every
false positive teaches a treasurer to stop reading it.

## The MariaDB constraint warning was a real production hole (item 1)

MariaDB does not create conditional unique constraints — it silently declines
(W036). So on the production database they were **not enforced at all**, and a
duplicate exception could be written. The conditions were never needed: SQLite,
PostgreSQL and MariaDB all treat NULLs as *distinct* in a unique index, so an
unconditional constraint permits any number of `line=NULL` rows while still
enforcing one row per `(account, kind, line)` where `line` is set — which is
what the condition was trying to say, and now actually exists on every backend
rather than only on the one nobody runs in production.

## "Trust pending receipt" was named after its own bug (item 5)

The list was Trust-only, so LCB money a church receipts exactly as it receipts
trust money simply never appeared — which is why it was called *Trust* pending
receipt. Renamed to **Pending receipt**, and it now covers the whole receiptable
set: every Trust fund **plus the LCB family** (the funds configured in Settings,
**plus their subgroups**).

Worse, `_is_receiptable_fund()` — which drives the Sabbath-confirm scope —
matched LCB **by name only**, so a church that had carefully configured its LCB
funds in Settings found that setting silently ignored, and two screens could
disagree about which funds counted. There is now one canonical definition,
`departments.models.receiptable_fund_ids()`, and both use it. Old export URLs
still work: renaming a URL a user has bookmarked is not a rename, it is a
breakage.

## Allocation & categories moved, and a duplicate retired (item 6)

Now its own page, reachable from the allocation rules — where it belongs, rather
than sitting in Settings → Channels among bank accounts and opening balances
that have nothing to do with allocation. Both it and the development-group
patterns page are linked from `/rules/`; patterns is gone from the sidebar.

**And yes — the duplicate was real.** The "extra dev-group prefixes" setting
built exactly the regex a `DevGroupPattern` of kind NUMBERED builds, but could
not be labelled, ordered, disabled or audited. Two places to configure one
behaviour, neither able to see the other. Retired — but not by silently
discarding what a church had configured: migration `giving.0025` turns any
existing prefixes into real, visible, editable patterns on the page built for
the job.

## Also

The register downloads to CSV and Excel, with the opening and closing balances
included — a register exported without them cannot be checked by anyone (item
3). Contribution import already handled a full case roster, paid and unpaid
(item 2); confirmed with tests rather than assumed, since a blank amount
correctly records *no* contribution: "did not contribute" is the absence of a
payment, and writing a zero-value receipt to say so would put money in the
ledger nobody gave.

A latent **date-boundary flake** was also found and fixed: a test captured
`TODAY` at module import, so a long suite crossing midnight dated its payouts
into a day the report window excluded, producing a `None` total and an
`AttributeError`. It only ever surfaced because these runs are long enough to
cross midnight.

**Tests:** 26 new. Full regression clean — 2,547 tests.

---
---

# Round 5 — reported issues

## The serious one: the register was crying wolf (item 4)

*"Many entries being detected as not in our books, but when searching I found
them."* Two bugs, both mine, and the first one bad:

**1. The date window was being used for MATCHING, not just for reporting.** A
bank reference is unique *forever* — if any transaction carries it, the line
**is** in our books, whatever date it happens to be recorded under. But the
match index was built only from transactions inside the reporting window. So a
payment the bank value-dated 1 July, which the treasurer entered on 30 June when
the SMS arrived, fell outside it — and its statement line was flagged as
"not in our books" even though it plainly was.

Value date and entry date differing by a day or two is completely ordinary. A
reconciliation that cannot survive that is worse than none, because **every
false positive teaches a treasurer to stop reading the report.** The date window
now decides what we *report on*; it never decides what *matches*.

**2. Transactions were not scoped to the account being checked.** With two bank
accounts, every transaction of the second was flagged as missing from the first
— where it was never supposed to be.

## Constraints that were silently absent in production (item 1)

MariaDB does not create conditional unique constraints. It declines, quietly
(Django warns W036) — so on the production database the register's duplicate
guards **were not enforced at all**, and a duplicate exception could be written.

The conditions were never needed: SQLite, PostgreSQL and MariaDB all treat NULLs
as *distinct* in a unique index, so an unconditional constraint permits any
number of rows with `line=NULL` while still enforcing one row per
`(account, kind, line)` where line is set. That is exactly what the condition was
trying to express — and it now actually exists on every backend, rather than only
on the one nobody runs in production.

## Pending receipt: renamed, and it now includes LCB (item 5)

It was Trust-only, so LCB money a church receipts exactly as it receipts trust
money simply never appeared. It was called "Trust pending receipt" — a name that
described the bug rather than the intent.

Worse: `_is_receiptable_fund()` matched LCB **by name**, ignoring the LCB funds a
church had configured in Settings entirely. A church that had carefully listed
them found that setting silently disregarded, and its funds matched (or missed)
by whether somebody had spelt "LCB" into a name.

There is now one canonical definition — `departments.models.receiptable_fund_ids()`
— which honours the configured funds **and their subgroups**, and which the
Sabbath-confirm scope and the pending-receipt list now literally share. Old
export URLs still work: renaming a URL a user has bookmarked is not a rename, it
is a breakage.

## Allocation & categories, and a duplicate retired (item 6)

Moved to its own page, next to the allocation rules and development-group
patterns it belongs with — rather than sitting in Settings → Channels among bank
accounts and opening balances that have nothing to do with allocation. Buttons on
`/rules/` reach both; the patterns page is out of the sidebar.

**Edwin asked whether the dev-group prefix setting duplicated the patterns page.
It did.** It built precisely the regex a `DevGroupPattern` of kind NUMBERED
builds — but could not be labelled, ordered, disabled or audited. Two places to
configure one behaviour, neither able to see the other. It is retired; a
migration turned whatever any church had configured into real, visible patterns
rather than silently discarding it.

## Also

The register downloads (CSV and Excel, with opening and closing balances — a
register exported without them cannot be checked by anyone). The case-roster
contribution import was verified end to end: a treasurer can upload the whole
roster for a case, paid **and** unpaid, and "did not contribute" is recorded by
the *absence* of a payment — writing a zero-value contribution to say so would
put a receipt in the ledger for money nobody gave.

**A latent test bug fixed too:** one Phase 8 test captured `TODAY` at module
import and asserted against a window ending "today" — so a long suite crossing
midnight dated its payouts into a tomorrow the window excluded, leaving zero rows
and an AttributeError. It was flaky, not wrong, but a flaky test is a test nobody
trusts.

**Tests:** 26 new. Full regression clean — 2,500+ tests.

---
---

# Round 6 — reported issues

## The matching bug, third time, and finally at the root (item 3)

*"I can get the references under M-Pesa ref in the transactions. Yet being
detected as missing in the reference UAVAM5CG31. Realized it affects
transactions which have indicated as manual receipt, and the amount may be zero.
Check how split funds are also matched."*

Every one of those clues was the same root cause, and it was mine. I was building
the match index with filters on **channel**, **bank account** and **reversal
status** — all of which are classifications *we* make after the fact, and any of
which can hide a transaction that plainly carries the bank's own reference:

* **Manual receipt** sets `excluded_from_income` and detaches the row from its
  fund. It is still the same bank line.
* **A split part can be zero-valued**, and importer-created split parts have no
  `split_of` link at all — they share the parent's `mpesa_ref` and carry
  `REF-S1` core_refs.
* **A transaction may carry no bank account**, or one tagged later.
* **A human may have reclassified the channel.**

For the question *"did we ever record this bank line?"*, the only thing that can
answer it is whether the bank's reference appears in our ledger. Nothing else.
The account and channel filters still belong on the *other* direction — "which of
our own bank entries has the bank never mentioned?" — which is where they now
live, and only there.

That is three rounds on one function. Each fix was right and each was
insufficient, because I kept correcting the symptom in front of me rather than
asking what the question actually needs to know. It needs to know one thing.

## The register's opening balance (item 2)

A register that starts mid-year summed forward from zero, so its closing balance
was out by whatever the account already held. Now it derives the opening from the
bank's own balance column on the very first line — the bank has already told us,
and its figure beats anything typed. Only where a statement carries no balance
column at all does the page ask, and then it asks once.

## Pending receipt excludes cash (item 1)

Cash is receipted at the point of counting — it goes onto an envelope at the
table. It does not arrive silently and wait to be chased. Listing it asked a
treasurer to chase a receipt for money that was never going to have one.

The list also has a Telegram route now: `/pending` returns the same PDF the
transactions page serves, from the same single query — so the bot and the web
page can never give a treasurer two different answers to one question.

## Petty cash and cheques (items 4, 5, 6)

**A cheque cashed for petty cash is two movements**, not one: money leaves the
bank, and money arrives in the tin. Record only the cheque and the float is
understated; record only the top-up and the bank is overstated. They are now one
action: write the cheque in the payments register with source *"Petty cash
replenishment"*, and the float rises automatically when it is issued — and falls
again if it is cancelled, because a cheque that was never cashed never became
notes in the tin.

**The payee is now distinct from the claimant.** The member who requested a
purchase and the supplier the cheque is written to are often different people.
`PaymentInstrument` always had a payee; the expense giving rise to it never
captured one, so the cheque had to be filled in from scratch.

**The separate "record a disbursement" form is retired.** It wrote exactly the
`Expense` the expense form writes with *"paid from the petty cash float"* ticked
— but could not attach a receipt, set an expenditure type or a budget line, and
had its own approval shortcut. A treasurer who used it ended up with a voucher
that looked different from every other voucher in the book. One form, one
approval trail, one place a voucher can be found.

## Printing onto a real cheque (item 7)

The existing print was a *facsimile* on plain paper — a payment advice, which is
still there and still useful. Printing onto an actual bank leaf is a different
problem: ink must land at exact millimetre positions on paper that already has
its own borders and labels, and printing ours on top of them would ruin it.

**Cheque leaves differ between banks, and a numbered leaf spoiled by a bad guess
is not free.** So nothing is guessed. `?mode=leaf` prints only the values,
absolutely positioned; the layout is configurable; and `?mode=calibrate` prints a
millimetre grid with a red cross where each field will land, onto one *spoiled*
leaf, so a treasurer can measure the offsets and correct them. Once.

**Tests:** 33 new. Full regression clean.

---
---

# Round 7 — reported issues, and the church's own workbook

Edwin sent the real thing: a working benevolent scheme
(`BENEVOLENT_2023_Case_50.xlsx`) and the WhatsApp update a treasurer produces by
hand after every case (`CASE_68.docx`). Item 6 is built to that document exactly,
because **that document is the specification** — it is what the congregation
already expects to read.

## The debit side of the bank register never worked (item 3)

M-Pesa gives every **credit** a receipt code — which is why the credit side
worked from the first day. But the **debits** a church actually makes are
cheques, standing orders and bank charges, and a bank identifies those by a
cheque number in the narration, or by nothing at all.

So every debit was falling through the "no reference, cannot say" branch and
never being checked. The credits are gifts arriving, which are pleasant to get
wrong. The debits are money *leaving*, which is not.

Debits are now matched by **cheque number against the payments register** — a
cheque leaving the bank should correspond to a cheque we wrote — and an unmatched
debit is **always** flagged, reference or no reference. Money leaving the account
with no record behind it is the single most important thing this check exists to
find; staying silent because the bank did not print a reference hid exactly that.

## Reversals (item 4)

A bank credits the church by mistake and takes it back. Nothing was really
received — but **the importer was posting it as income**, and posting the
reversing debit separately, so a church's books showed a gift it never received
and its income was overstated by the amount of the bank's own mistake.

`Transaction` has carried `is_reversed` / `is_reversal` all along, and every
report already excludes both. Nothing was setting them from a statement. The
importer now pairs the rows up front and skips them, and the **register keeps
both lines** — its whole contract is to say what the bank said, and they net out
in the running balance exactly as they do on the real statement.

A narration keyword is **required** to pair. A church that receives 5,000 on
Monday and pays a 5,000 supplier on Tuesday has two perfectly real movements, and
silently erasing both because they cancel out would be far worse than leaving a
genuine reversal unrecognised.

One further bug this exposed: a bank reversing its own mistake issues the debit
under the **same reference** as the credit it is undoing — so the register was
deduplicating it away as a "duplicate", losing the line entirely and showing money
the bank had already taken back.

## The case statement (item 6)

Built to `CASE_68.docx`, line for line: the summary, then who newly registered,
who contributed, and who did not.

That last list is the point of the whole exercise. A benevolent scheme runs on
the plain fact that everybody can see who stood with the bereaved family. The
treasurer was assembling it by hand, from a spreadsheet, after every single case.
The system already held every fact in it.

Membership is counted **as the case saw it** — everyone on the roll on the day it
happened. Somebody who joined afterwards was never asked to contribute and is not
a defaulter. **The bereaved member is never on the defaulters list**: publishing
their name as somebody who failed to contribute to their own bereavement would be
grotesque.

Plain text, no markdown, no emoji — WhatsApp mangles all of it, and a treasurer
pasting a broken table into a congregation group at 10pm is not a problem worth
creating.

## The registry, on Telegram (item 5)

`/member NAME` — standing, arrears, dependants. The single most common thing a
treasurer or an elder is asked at church, and until now it needed a laptop.
`/case [NUMBER]` — the WhatsApp statement, from the same function the web page
uses, so the bot and the page can never tell a treasurer two different stories.
Over ~3,500 characters it arrives as a file rather than a truncated message,
because truncating would cut names off the list — the one thing on the statement
nobody may quietly drop. `/benevolent` and `/arrears` complete the set.

## The budget PNGs on a phone (item 2)

The fonts were not small. The **image** was 1180px wide — a desktop table — and a
phone scales that to fit a ~380px viewport, about a third. So "14pt" text actually
rendered at roughly 4.5pt, along with everything else in it.

What matters is the **ratio** of text size to image width, because that is what
survives the scaling. It was 1.2%. It is now ~2.5%, and the same text lands at
~9.5pt on a phone instead of ~4.5pt.

The progress bar is gone. A bar is a picture of a number, and a picture of a
number does not survive being scaled to a third of its size. The number does, and
says the same thing.

## Founding balances, and first-time setup (item 1)

The budget page was locked against editing a founding balance after a year-end
close. **The department edit form was the other way in**, and had the same hole.
Same lock, same reasoning.

`docs/FIRST_TIME_SETUP.md` is the setup guide, and is blunt about the two things
worth being slow over: the founding balances, and the first year-end close. Both
are one-way doors.

## What the workbook showed was missing (item 7)

The model covers the church's real workbook almost completely — member numbers,
spouses, deregistration, contacts, per-case collections and expenses and
balances, registration fees, family details. One real gap:

**The beneficiary's *relationship*.** The church's own report records *"Mzee Harun
Kanyi — Father to Grace Nyaboke"*. That line tells the congregation *whose* loss
this is, which is the whole reason anybody is being asked to contribute. We
captured it only when the beneficiary happened to be a registered dependant, and
dropped it otherwise. Now a field, and on the statement.

**Tests:** 44 new. Full regression clean — 2,600+ tests.

---
---

# Round 8 — one bug, three symptoms

Edwin sent the actual bank file. It answered all three reports at once.

## The bank exports no debit column

    Posting Date | Value Date | Core Ref | Channel REF | Narration |
    Credit Amount | Running Balance

That is the whole header. **There is no debit column.** A cheque payment appears
as **Credit Amount = 0.00**, with the **running balance dropping** by the amount
paid:

    CB0408755260701 | SYBINSE... | HENRY CHQ No.000412 | 0.00 | 4,227,950.03
                                              (the balance fell 15,500)

The parser had a guard — *"nothing moved on this row"* — that discarded any row
with no credit and no debit. Every debit on this bank's statements hit it. On a
single month, that silently threw away **eight cheques worth 3,061,850**.

And that one bug produced all three of the reported symptoms:

1. **Debits never imported** — the rows were discarded before anything saw them.
2. **The register's balance never reconciled** — of course it did not; the rows
   that made it fall were missing. *"which usually means a row is missing"* was
   exactly right.
3. **Cheques never cleared** — `clear_for_bank_debit` and
   `suggest_instrument_for_debit` have been built, tested and wired into the debit
   review queue all along. The queue was permanently empty, so the machinery had
   nothing whatever to act on.

## The fix

The balance column is the bank's own arithmetic. Where it disagrees with a zero in
the credit column, **it is the balance that is telling the truth**. So where a file
has no debit column, a movement is derived from the change in the running balance.

Deliberately conservative: it only fills in a movement the file does not otherwise
state. A row whose credit column carries a real figure is taken exactly as given,
and a file that has a proper debit column is not touched at all.

Against the real statement: **all 8 debits recovered, and the file reconciles to
the bank's own closing balance to the penny** — 2,035,728.03, difference 0.00.

## Cheque auto-clearing

Now that debits arrive, the cheques clear themselves. A cheque **number** match is
exact — the bank issues each number once, prints it in the narration, and it is
the same number on the stub — so it needs no human confirmation. All 8 cheques on
the real statement cleared automatically and are linked to the debit that cleared
them; that link *is* the reconciliation trail.

Two deliberate refusals:

* **A number that matches with the wrong amount is not cleared.** That is a cheque
  altered, partly paid, or misread, and it wants somebody's eyes on it rather than
  a silent tick.
* **An amount-only match is never auto-applied.** Two cheques for the same amount
  are perfectly ordinary, and guessing between them would clear the wrong one. It
  stays a suggestion in the debit queue, exactly as it always was.

**Tests:** 13 new, written against the real file's exact shape. Affected suites
run clean: statements, cashbook, giving (795 tests).
