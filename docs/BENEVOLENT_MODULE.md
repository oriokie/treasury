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
