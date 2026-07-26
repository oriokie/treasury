# v3.22.0 — Approving a batch, counting the tin, and paying ahead

Three faults reported together. Two turned out to be the same shape: a figure or
an action that was right in one place and wrong in another, with nothing
comparing the two.

## Approving a remittance batch appeared to hang

`repost_to_ledger` took the expenses that had changed and ignored them. It called
`posting.rebuild()` instead — which deletes every non-manual journal entry in the
database and re-posts every transaction, expense, refund, transfer, asset
acquisition, disposal and depreciation run the church has ever recorded. To
approve one batch.

On the seeded demo, with 214 transactions, that was 3,349 queries and a second
and a half. On a real register with years behind it, it is minutes — and it runs
twice in the workflow, once on approve and again on remit. Worse, the cost grew
every year the church kept using the system, so a page that was merely slow at
first eventually stopped answering at all.

Only the batch's own expenses are reposted now. `post_expense` begins by
replacing that expense's entries, so it is idempotent and also withdraws them
when a status moves back out of approved or paid; reposting the affected rows is
therefore complete rather than a partial patch. Approving a three-line batch went
from 3,349 queries to 70, and a test compares the resulting ledger line by line
against what a full rebuild produces — same sources, accounts, debits, credits,
dates and funds — because a faster route to the books is only acceptable if it
arrives at the same books. Called with no argument the function still rebuilds
everything, so anything that genuinely wants that keeps it.

## The petty cash register disagreed with the float card

The closing balance and the "float on hand today" card are meant to be the same
number and were worked out two different ways. The card's helper counts cash
handed back into the tin when an expense is refunded. The register's list of
movements never showed those refunds at all — while its *opening* balance came
from that same helper. So a refund before the period was counted and a refund
inside it silently vanished, and the two figures differed by exactly the refunds
falling in the period.

Refunds returned to petty cash are now listed as what they are: money going back
into the box, on the day it went back, against the fund it came from. The two
figures agree by construction, and — the part that matters more — the register
adds up on its face. Somebody counting the tin can follow it. A refund that was
banked rather than returned to the box is still left out, because it never
touched the tin.

## A scheduled expense can be paid before it falls due

Churches pay ahead: the payee is travelling, the office closes over a holiday, a
quarter's rent goes out on one cheque. The schedule had no way to do it, so it
was done by hand — and generation recognised an existing entry by its *date*,
which a hand-written early payment does not match. The schedule then raised the
same charge again on the due date. A five thousand shilling stipend was recorded
twice, ten thousand against the fund, with nothing to flag it.

The idea that was missing is *which instalment a payment settles*, as distinct
from *when the money left*. Both are now recorded. The expense is dated the day
the cash goes out, because fund balances are kept on a cash basis and anything
else misstates the fund for the weeks in between; alongside it sits the
instalment being settled, which is what generation now checks. Pay January's
stipend in December and December is when the fund is charged — and January comes
round with nothing owing.

The schedules page offers the next three unsettled instalments with a **Pay
early** button. An instalment already settled drops off the list, so the same
period cannot be paid twice, and one that never was a due date, or falls after
the schedule ends, or lands in a closed period, is refused. Paying early does not
lower the bar for approval: an amount that needs a treasurer's signature on its
due date still needs it a fortnight sooner.

Existing generated expenses are backfilled to settle the date they were created
on, which is what they have always meant, so the new check is identical in effect
for everything already recorded.

## Files

* `reports/services/remittance.py` — repost the batch, not the whole ledger
* `cashbook/views.py` — refunds in the petty cash register; the pay-early action;
  upcoming instalments on the schedules page
* `cashbook/models.py` — `Expense.recurring_due_date`
* `cashbook/services/recurring.py` — `pay_early`, `upcoming_instalments`,
  generation keyed on the instalment
* `cashbook/migrations/0043_expense_recurring_due_date.py` — field and backfill
* `cashbook/urls.py`, `templates/cashbook/recurring_list.html`
* `cashbook/test_early_and_registers.py` — new, 19 tests

One migration, additive, with a backfill. No accounting figure changes for
anything already recorded: the ledger after a batch approval is proved identical
to a full rebuild, the petty cash float was already right on the card and is now
matched by the register, and existing recurring expenses keep the meaning they
had.

## Tests

cashbook 489 including the 19 new, reports 470 — all passing.

# v3.21.1 — The envelope list stops re-reading its own postings

A follow-on to the v3.21.0 audit, closing the one item on its own list that was
worth closing.

The weekly envelope list strikes a receipt through when every ledger entry
behind it has been reversed. Deciding that means reading each line's
transaction, and while the view prefetched each line's *department* it did not
prefetch its *transaction* — so drawing the list cost a query for every line
that had posted. Thirty queries down to twenty-one on the seeded register, and
the cost no longer follows the number of envelopes in the month.

The other three screens listed in recommendation #135 were re-probed and left
alone: each turns out to be a single bounded fetch rather than a per-row
pattern, so changing them would add risk without removing work. The
recommendation now records that, so the next person does not re-investigate
what has already been measured.

## Files

* `envelopes/views.py` — prefetch `lines__transaction`, select `bank_transaction`
* `docs/recommendations.md` — #135 updated

No migrations. No accounting change: this alters how the page fetches what it
already displayed, not what it displays.

## Tests

envelopes 171, benevolent 697 (the whole app, including the two modules
outstanding at v3.21.0), and the 16 query-growth guards — all passing.

# v3.21.0 — Pages stop paying a query for every fund

A performance audit, measured rather than guessed. Every one of the app's 951
URL patterns was swept, query counts and timings taken on the 189 pages that
answer a GET, and then the pages that looked expensive were measured a second
time against a larger register. That second step is the one that matters: a
count is only evidence of a fault if it *grows*. Three axes were tested — number
of funds, volume of transactions and expenses, and number of user accounts.

Nothing in the app scales with transaction volume. Adding three hundred
transactions and a hundred and fifty expenses moved no page by a single query,
which says the list views' pagination and `select_related` were already right.
What did scale was the fund register, and it scaled badly.

| Page | Before | After |
|---|---|---|
| General ledger reconciliation | 258 | 33 |
| Envelope template download | 108 | 12 |
| Envelope ledger | 69 | 19 |
| Settings | 66 | 23 |
| Expenses report | 61 | 19 |
| Allocation rules | 28 | 19 |

## The reconciliation report had a fix waiting for it

`/ledger/reconciliation/` called `fund_balance_from_ledger(d)` once per fund —
four queries each, 258 on a register of fifty-nine funds, and four more for
every fund a church adds. A bulk version, `fund_balances_from_ledger_bulk`,
already existed and does the identical computation in a small constant number of
grouped aggregates. Its own docstring names the reconciliation report as its
caller. Only the health check ever adopted it, which is why `/ledger/health/`
sat flat at 49 queries while the page next door, proving the same numbers, cost
five times as much. The helper was tested; nothing tested it *through the view*.

## A fund dropdown was costing a query per option

`Department.__str__` prints "Parent / Child" for a sub-account, so rendering a
department fetches its parent. Harmless in isolation and invisible in review —
until a form renders a fund selector, which calls `str()` once per option. Every
page in the app carrying a fund dropdown was paying a query per sub-account.

Fixed once, at the model's default manager, which now loads the parent with the
department. One small self-join on a table of a few dozen rows replaces the
per-option queries, and it corrects every form at once rather than leaving each
queryset to remember. No migration; `.values()`, `.update()` and aggregates
ignore `select_related`, so nothing else changes.

## Smaller ones

The envelope screens called `subgroups_for()` inside a loop over every fund. The
ordering rule has been lifted into `_shape_subgroups`, so the single-fund path
and the whole-register path share one definition and cannot disagree about what
a subgroup list looks like; the catalogue now fetches every fund's children in
one grouped query. The import screens were also building that catalogue twice in
the same request.

The settings page had three separate faults: a reverse one-to-one fetched per
user, the user's groups fetched per user, and the settings form built twice —
each rebuild running every fund selector's queryset again. The group fault
belonged in `core.roles.user_roles`, not in the page: it read groups with
`values_list`, which issues its own query and **ignores a prefetch cache**, so
no caller could have optimised it from the outside however carefully it built
its queryset. It now reads `user.groups.all()`, which uses the cache when the
caller has prefetched and costs the same single query when it has not.

The expenses report handed the template a queryset of outstanding vouchers with
no `select_related`, and the template printed each one's fund.

## Guards

`core.test_query_growth` measures a page, adds twenty funds with sub-accounts,
and requires the query count not to move. This is deliberately not a ceiling: a
ceiling set against a small fixture passes happily while a page queries per row,
which is exactly how the reconciliation report survived. Flatness is the property
that was actually violated, so flatness is what is asserted.

It also pins the root causes directly rather than only their symptoms — that
rendering the whole fund register costs one query, and that reading roles for
prefetched users costs none — because those faults lived in a model and a
helper, and a guard sitting on one page would not have found them.

Speed must not change the answer, so the same file checks that the bulk ledger
balance still agrees with the single-fund computation for every fund, that both
subgroup paths shape a fund identically, and that a sub-account still reads
"Parent / Child".

## Files

* `ledger/views.py` — reconciliation uses the bulk helper
* `departments/models.py` — `ParentAwareDepartmentManager`
* `core/roles.py` — `user_roles` honours a prefetch cache
* `core/views.py` — settings prefetches roles and profiles, builds its form once
* `envelopes/services/posting.py` — `_shape_subgroups`, bulk child fetch
* `envelopes/views.py` — catalogue built once per request
* `reports/views/overview.py` — outstanding vouchers select their fund
* `core/test_query_growth.py` — new, 16 tests
* `docs/recommendations.md` — #134, #135

No migrations. No accounting change: every figure, metric and ledger posting is
untouched, and the reconciliation report's numbers are checked against the
original computation fund by fund.

## Tests

Targeted regression, all passing: departments/ledger/envelopes 275, core 478 +
16 new, reports 425, giving 288, cashbook 470, accounts/members 199,
assets/vendors 164, loans/pledges 101, statements 176.

# v3.20.0 — The member portal gets the styling it was always asking for

The portal was built out of two classes, `.panel` and `.table`, that had never
been defined in the stylesheet. Twenty-seven templates asked for `.panel` and
eleven for `.table`, and every one of them was rendering as a bare `<div>` and an
unstyled HTML table: no surface, no border, no radius, no header treatment, no
zebra striping, no right-aligned figures. This was not a matter of taste. The
portal had been written against a component vocabulary that did not exist, and
because a missing CSS class is silent — the page loads, the markup is valid — it
had gone unnoticed through every render test the portal has.

Both are now defined once. `.panel` is a real component built from the same
tokens as `.card`, with `panel-forest`, `panel-brass`, `panel-amber` and
`panel-danger` accent rails replacing the inline `border-left` styles that had
been copied around. `.table` is unified with `table.ledger` by rewriting all
fifty-three ledger rules to `:is(.ledger,.table)` — one definition under two
names, so the two cannot drift apart the first time either is touched. The same
change lifts the supplier register and the benevolent screens, which were built
from the same vocabulary.

Two further classes were being used and never defined: `.compact`, written on
ninety-two tables across the app and therefore inert on all of them, and
`.field-xs`. Both are now defined, `.compact` matched to the figures the global
density preference already uses so a compact table and a compact site agree.

On top of that the portal pages get labelled filters, a three-figure summary on
the contributions page answering the question members actually ask first — did my
last payment land — and empty states that read as a normal condition rather than
a fault, because a member with no cases is a member nothing bad has happened to.
Every portal page also sets its own browser title; they had all shared one, so a
member with three tabs open could not tell them apart.

## The standing page returned 500 for anyone who could not yet claim

`/portal/standing/` rendered `{{ f.message|default:f.name }}` over an eligibility
check, whose fields are `code`, `label`, `passed`, `detail` and `blocking`.
Neither `message` nor `name` exists. Because `f.name` sits in a filter argument —
and a missing variable used as a filter argument raises in Django rather than
rendering blank — the page failed outright.

This is the same fault, in the loop immediately below the one that was fixed in
v3.19.x, and it survived for the reason recorded there: the line only runs for a
member who is *ineligible*, inside `{% for f in b.result.blocking_failures %}`.
Every fixture had members in good standing. Of the nine portal accounts in the
seeded demo, eight rendered perfectly and one — a reinstated member who had not
served the waiting period — crashed. The page now shows the check's own sentence
explaining what is in the way, which is what a member needs to read.

## A supplier can be chosen when a bill is entered

`PayableForm` has always defined `supplier` as a selector over the vendor
register. The payables page never rendered it, putting out only the free-text
"vendor" field. So every bill entered there was saved with no supplier — which is
exactly what the "N open bills are not linked to a supplier" warning at the top
of that same page counts. The page was generating the condition it complained
about, and no amount of clearing the backlog could have fixed it. The supplier's
payment terms, which set the due date, could not apply either.

The selector is now rendered first, on a new `.entry-grid` shared by the
payables, accruals and prepayments forms, with the invoice name back-filled from
the supplier and the due date taken from its terms. One-off purchases from
unregistered traders are still accepted; a bill owed to nobody is still refused.
The three summary figures on that page had been using `kpi-label` and `kpi-val`,
which are defined nowhere, so they rendered as plain body text inside otherwise
styled cards; they now use the classes `.stat` actually styles. The same defect
is fixed on the petty cash and budget board screens.

## Guards

Three suites, each verified by reintroducing the defect and confirming it fails.

`benevolent.test_portal_render_contract` renders every portal page for a member
who has a genuine blocking failure, and does it twice: once asserting a plain
200, once under a recording `string_if_invalid` sentinel asserting that no
template variable is left unresolved. Both halves are needed and neither is
sufficient. The 200 check is the only one that catches the raising kind, and it
needs a fixture that reaches the loop. The sentinel is the only one that catches
the *silent* kind — `{{ f.message }}` alone renders blank, which is how a wrong
attribute name survives review until the day it lands in a filter argument. And
the sentinel cannot replace the 200 check, because with a `%s` sentinel installed
Django returns early at the invalid variable and never resolves the filter's
arguments: a sentinel-only suite would have passed against this very bug.

`core.test_css_contract` compares the classes templates use against the classes
anything defines, following `{% extends %}` and `{% include %}` chains so
inherited styles are not counted as missing. It fails on any class used in three
or more templates with no definition — a single-use name is often a legitimate
JavaScript hook, but a name used across several templates with nothing behind it
is a shared vocabulary with a hole in it. Nine such classes already exist and are
listed in `KNOWN_UNDEFINED` as a ratchet: the list may shrink and must never
grow. They are written up as recommendation #132.

`cashbook.test_payables_supplier` tests the behaviour rather than the markup —
that a bill entered through the page arrives on the supplier's account with the
terms applied.

## Files

* `static/css/app.css` — `.panel` and its accent rails, `.compact`, `.field-xs`,
  `.entry-grid`; all fifty-three `.ledger` selectors rewritten to
  `:is(.ledger,.table)`
* `templates/benevolent/portal/` — `_base.html`, `standing.html`,
  `contributions.html`, `statement.html`, `household.html`, `cases.html`,
  `requests.html`, `documents.html`, `notifications.html`
* `benevolent/views_portal.py` — `portal_title` per view; most recent
  contribution date for the contributions summary
* `templates/cashbook/accruals.html` — supplier selector, entry grid, stat classes
* `templates/cashbook/petty_cash.html`, `templates/reports/budget_board.html` —
  stat classes
* `templates/vendors/`, `templates/giving/campaign_sms_confirm.html` — named
  accent classes in place of inline styles
* `benevolent/test_portal_render_contract.py`, `core/test_css_contract.py`,
  `cashbook/test_payables_supplier.py` — new
* `docs/recommendations.md` — #132, #133

No migrations. No accounting change: no figure, metric or ledger posting is
touched by this release.

## Tests

Targeted regression, all passing: cashbook 470, core 478, reports 425,
benevolent portal suites 58, vendors 38, and the 19 new tests above.

# v3.19.4 — The check for stray text now covers the pages it was missing

The check added in v3.19.3 read every page that can be opened without picking a
record first. It did not read the pages that show one record — an expense, an
asset, a case — nor any page in the member portal, which it cannot reach at all
because those require a member to be signed in.

Both are now covered. Nothing further was found, which is the point of looking.

---

# v3.19.3 — Removes stray text that was appearing on several pages

Explanatory notes left in five page templates were written in a form the
template system does not recognise when it runs across more than one line. The
result was that the notes themselves appeared on the page as plain text, in the
sidebar of every page and on the expense form, the petty cash register and two
member portal screens.

The notes are now written in the form that works, and a check has been added
that reads the finished pages rather than only asking whether they loaded — the
existing checks all passed while this was happening, because a page covered in
stray text still loads perfectly well.

The new supplier module has also been added to the automated test schedule; it
had tests but they were not being run there.

---

# v3.19.2 — An interrupted campaign message no longer disappears

Sending a message to a large group takes time, and a slow connection or a
server timeout could cut it off part-way. Until now the record of the send was
only written once it finished, so if it was cut off the messages that had
already gone left no trace: the treasurer saw a failed page, with no way to know
whether sending again would reach some people twice.

The record is now opened before the first message and kept up to date as the
send proceeds. If it is interrupted, the campaign page says so and shows how
many of the intended recipients were reached — which is the one case where
sending again is the right thing to do, and the case a plain "already sent"
note would have talked you out of.

---

# v3.19.1 — You can see what has already been sent to a group

Campaign messages now leave a record: which group was written to, what was said,
how many were reached, how many had no phone number, who pressed send and when.
Each group on the campaign page shows its recent messages, and if you compose
the same message to the same group again, the confirmation screen says so
before anything goes out.

It does not stop you — a reminder is sometimes meant to be repeated — but the
congregation would receive it twice and the church would pay twice, so it is not
a decision to make by accident.

---

# v3.19.0 — One charge for a batch; campaign groups and messages

**A batch of expenses can now share a single transaction charge.** If a stack of
receipts was settled with one transfer, the bank took one fee — so the fee is
entered once, for the batch, rather than on each line. It is recorded as a
single bank-charge expense on the fund. Charges on individual lines still work
and the two can be combined, for the case where most of a stack went in one
transfer and one item was paid separately.

**Campaigns now show the sheet that was uploaded.** Until now a campaign could
tell you how many members it had but not who they were, so there was no way to
check that an import had put people in the right groups. Each campaign has a
page listing its groups, who is in each, and how many of them have a phone
number the church can actually reach. Groups numbered 2 and 10 now sort in that
order rather than alphabetically.

**A custom message can be sent to one group at a time.** Write it once using
{name}, {group} and {campaign} where those should appear, and every member gets
their own. Nothing is sent until you have seen exactly who it goes to and what
they will receive — and anyone on the sheet without a usable phone number is
listed plainly rather than quietly left out. Sending is limited to a treasurer,
because it costs money each time and a text cannot be recalled.

---

# v3.18.0 — Enter a stack of receipts at once; the upload matches the form

**A batch entry screen for expenses.** When a treasurer settles several receipts
from the same person, on the same day, from the same fund, those four facts are
now entered once at the top and each receipt takes a single line — narration,
amount, and the transaction charge where there was one. A line can override the
category if one purchase was different. Either every line is saved or none is,
because a half-entered stack is worse than none.

**The expense spreadsheet now carries every field the form does.** Supplier,
payee, expenditure type and budget item were missing, so an upload quietly
produced the incomplete records the form no longer allows. The registered
suppliers are listed on the template as a dropdown, so whoever fills it in picks
a name that already exists rather than typing a fourth spelling of it. A name
that is not recognised is flagged on the review screen and the expense still
goes in — it is simply recorded without a supplier, never invented into the
register.

Budget items are matched within the fund on the same row, so the same item name
can be used in different funds without spend landing on the wrong one.

**One set of rules for recording an expense, everywhere.** The form, the
spreadsheet and the new batch screen now share a single implementation of what
state a new expense starts in and how a transaction charge is recorded. The
three copies had already drifted — one omitted the payee from the charge entry,
which is invisible until a bank reconciliation cannot match it.

---

# v3.17.0 — The expense form, rearranged

The form for recording an expense had grown a field at a time and read as one
long column of everything, in no particular order. It now has five numbered
sections — how much and when, what it was for, who was paid, how it was paid,
and anything else — and uses the width of the screen instead of a narrow strip
down the middle.

Alongside it sits a panel that stays in view as you scroll, showing the fund,
the amount, who is being paid and how, and — in plain words — what happens when
you press save: whether the expense goes in as pending for approval, or lands on
the fund's balance straight away. The save button lives there too, so it is
never scrolled away from the figure it commits.

**The supplier field has moved to where it belongs.** It was added in v3.14.0
and had been sitting at the bottom under "Other details" ever since, because it
had never been assigned to a section. It now sits with the payee, where someone
recording a payment would look for it.

On a phone the two columns become one, with the summary below the form.

---

# v3.16.0 — The member portal appears in the menu; petty cash reads newest first

**The office side of the member portal is now in the Benevolent menu.** It has
been built and working since v3.11.0, and linked from nowhere — the only way to
reach "Member requests" or "Portal accounts" was to know the address and type
it. Both are now where they should have been all along, with a count of requests
waiting.

**Petty cash starts with the most recent movement** and is paginated fifty at a
time. The closing float now sits at the top with the newest entry and the
opening float at the bottom, so the register still reads consistently — just
downwards through time. Each row keeps the balance as it stood on its own date.

**Payables show their supplier**, linked to that supplier's account, and the
page says how many open bills are not linked to one — since those appear on
nobody's account.

**A recurring expense now records everything an ordinary expense does** —
supplier, payee, voucher number, budget line, expenditure type and petty-cash
flag — and passes them all to each payment it generates. Previously a schedule
produced rows missing exactly the details a treasurer would have filled in by
hand, so each had to be opened and completed. A schedule that creates work is
not a schedule.

---

# v3.15.2 — Every page is now checked against a database with data in it

Three faults in recent releases were the same shape: a page that worked
perfectly on an empty system and failed on a real one. The tests passed each
time, because a test that creates only what its own assertion needs never
exercises the parts of a page that deal with actual records.

There is now a check that seeds the demonstration data and asks for every page
in the application — 275 of them — plus the detail pages for each kind of
record, and then asks for them all again with the optional details deliberately
left blank, since an unapproved expense or an unmatched payment is the ordinary
case rather than the exception.

No further faults were found, which is the reassuring part. Keeping it that way
is what the check is for.

---

# v3.15.1 — The member portal's "My standing" page works again

"My standing" failed to load for any member who had contributions due — which
is every member the page was written for. The page referred to information by
names the system does not use, and rather than leaving a blank space, that took
the whole page down. Members would have seen an error, not their record.

The household page had the same fault waiting for it, for any dependant
recorded by name only rather than linked to the church roll — which is most of
them, since a spouse or child is rarely on the roll in their own right.

Both are fixed. The portal has also now been walked end to end on real data —
sign-in, every page, submitting a request, the office review screens, and
suspension — rather than only tested against an empty record.

---

# v3.15.0 — Supplier payment details are a separate permission

Changing where a supplier is paid is now its own permission, held apart from
the right to maintain supplier records generally. The office can keep contacts,
addresses and notes up to date while only a treasurer adds or verifies bank and
M-Pesa details.

This is a deliberate control, not extra paperwork. A letter or email announcing
that a supplier's bank account has changed is the commonest way churches are
defrauded, and the person who receives that letter should not be the person who
can act on it alone. Every change to payment details stays on the record, so an
auditor can always see what the account used to be and who changed it.

**Assets can also record who they were bought from**, and appear on that
supplier's account alongside their bills and payments — so "what have we bought
from them, and were we happy with it" is answerable before buying again.

---

# v3.14.0 — Suppliers have a record of their own

A supplier used to be a name typed into a box. "Mwangi Hardware", "Mwangi
Hardware Ltd" and "mwangi hardware" were three different suppliers as far as the
system was concerned, the question "what do we owe Mwangi altogether" could not
be asked at all, and nothing about a supplier — their terms, their bank details,
their PIN, the contract — had anywhere to live.

**There is now a supplier register.** Each supplier has a page showing what is
owed, how overdue it is, everything ever bought from them, their contacts,
addresses, payment details, tax information, documents and notes, and a history
of changes to the record. Bills and payments are listed together in date order,
because that is how a treasurer asks the question.

**The names already on file were grouped automatically.** Existing payables were
read, spellings of the same business matched up, and a supplier created for each
— so the register is useful on the first day rather than after a week of typing.
What each invoice actually said is untouched: the register sits alongside it, and
where the grouping got something wrong the two records can be merged.

**Bills and payments can be recorded against a supplier as you enter them.**
Choosing a supplier fills in the name and works out the due date from their
agreed terms, and settling a bill puts the payment on their account
automatically. Typing a different name still works, and is kept as typed —
what the invoice said is what gets recorded.

**Payment details carry a warning and a verification mark.** A letter announcing
that a supplier's bank account has changed is the commonest fraud against
churches, so new details start unverified, must be confirmed by someone, and
every change is kept on the record with who made it.

A supplier is archived, never deleted — the bills that name them are evidence.

---

# v3.13.0 — Accruals can be paid in instalments too

What v3.12.0 did for payables now applies to accrued expenses. A utility bill
accrued at an estimate and then paid in two goes can be recorded as it actually
happened, and the balance sheet shows what is still owed on the day it is owed.

The behaviour is not a second copy of the payable version: both obligations now
share one implementation, so the rules about what counts as a payment and what
is still owed cannot drift apart between the two halves of the liability note.

---

# v3.12.0 — A bill can now be paid a bit at a time

Until now a payable could only be settled in one movement: one button, one
payment for the whole invoice. Vendors are rarely paid that way. A treasurer
paying a hardware bill in three instalments had two bad choices — mark the whole
thing paid when it was not, or record nothing and let the cash book disagree
with the bank.

**A payable is now paid in instalments, and the page shows what is left.** Each
payment is entered against the bill, the balance comes down, and the payable
stays on the list marked "part paid" until the last shilling. Leaving the amount
blank pays the balance, which is what the old button did, so nothing has to
change for a bill that is settled in one go.

**A half-paid bill is now a liability for the other half — from the day the
money left.** This is the substantive change. Before, a payable sat on the
balance sheet at its full value until the final instalment arrived, so the
church reported owing money it had already paid. Each payment now reduces what
is owed on the date it was made, and a statement dated between two instalments
shows exactly what was outstanding that day.

Every instalment is an ordinary expense in the bill's own fund, so it reaches
the cash book, the fund balance and the ledger by the same route as any other
payment. Paying more than is owed is refused rather than quietly written off —
that is either a typo or a credit the vendor now holds, and someone needs to say
which. A payment linked to the wrong bill can be detached by the treasurer; the
expense itself is kept, because the money did leave the account.

Bills settled before this release are unaffected and stay settled.

---

# v3.11.1 — Members invited to the portal can now actually get in

A fault in the invitation flow shipped in v3.11.0: a member who was invited, set
their password and signed in was told their access was "not yet activated" and
advised to set a password — which is what they had just done. There was no way
through it. The account is now made active by the member signing in with a
password they set themselves, which is the thing that proves the invitation was
taken up. Suspending or closing an account is unaffected: signing in cannot
revive access an officer has withdrawn.

Members also now land directly on their own portal after signing in, rather than
being bounced off an office page on the way.

---

# v3.11.0 — Members can now see their own record

Until now, everything this application knew about a member could only be seen by
somebody in the church office. A member who wanted to know what they had paid,
where they stood, or what had happened to a claim had to ask — and somebody had
to stop and look it up.

**There is now a member portal.** A member signs in and sees their own
contributions, their own standing and arrears, their own household, their own
cases, and everything the church has sent them. They can download a statement
and a receipt for any contribution. On a phone, the tables become cards; this is
read at a funeral as often as at a desk.

**A separate fault was found and fixed while testing this.** The public
application form — the one a church puts on a poster so somebody can apply to
join a scheme — has not worked since default-deny authorisation was introduced.
It sent every applicant to a login page they have no account for. It was missing
the one line that marks a page as public, and its own guard test could not see
the problem, because a redirect to the login page looks exactly like a page being
correctly protected. The form works again, and the guard now also checks that
every page the church intends to be public can actually be opened.

**They can ask for things, and follow what happens.** A request for assistance, a
death to report, a change to the household, a correction to a record, a change to
their own details on the roll. Each gets a reference the member can quote, a
status they can watch, and a conversation with the office if more is needed. A
request that is declined always carries a reason, and the member can read it.

**Nothing a member submits changes a record by itself.** A request is a claim, not
a change. Approving one calls the same service the office already uses — the same
registry that adds a dependant, the same case service that raises a case. A case
raised from a portal request is a DRAFT like any other and still has to be
assessed, pass eligibility and be approved on its own merits. The portal cannot
grant cover, approve money, or post to the ledger, because it never writes to
those records at all.

**A correction request deliberately corrects nothing on its own.** A member may
well be right that a contribution is missing, but putting that right is a ledger
adjustment under the treasurer's authority. Approving the request records that
the office accepted the point; the accounting change is still made where
accounting changes are made.

**The figures are the office's figures.** A member's arrears on their phone and
their arrears on the treasurer's screen are one calculation, not two — the portal
reads the same services and the same definition of a contribution that counts, so
a reversed payment does not reappear as evidence of having paid.

**Who can see what is one rule in one place.** A member's login reaches the portal
and nothing else, and every row it shows is scoped by a single function. Reads are
logged as well as writes: a member can see when their own record was opened, and
so can an auditor.

Giving a member access changes nothing about their membership or their cover, and
taking it away does not either. No password is ever issued or known by the office —
a member sets their own.

---

# v3.10.0 — The Income Statement and the Statement of Financial Position now agree

The last release noted that the two statements did not add to the same total, and
that closing the difference properly was still to do. It is done.

The difference was the church's prepayments, less what it owes suppliers and what
has been accrued. The Income Statement's reconciliation counted the fixed assets
but not those items.

**The Income Statement now bridges to net assets in full**, line by line: the money
held in the funds, then the fixed assets at written-down value, prepayments, less
amounts owed to suppliers, less expenses accrued, less loans still to repay —
arriving at exactly the net assets figure on the Statement of Financial Position.

Both statements now take those figures from a single shared definition, so they
cannot fall out of step. If they ever did, the Income Statement would show the
difference on its own line rather than quietly absorbing it.

---

# v3.09.0 — Depreciation and other non-cash items shown on every report

Until now, only the Income & Expenditure statement and the Statement of Changes
in Net Assets mentioned depreciation. The Board Report, the Treasurer's Report and
the Income Statement showed a surplus with no mention of the church's assets being
used up — so the board could be shown a surplus while the value of what the church
owns had fallen, with nothing on the page explaining why.

**The Board Report and the Treasurer's Report now show both results.** The cash
surplus as before, then the items that change what the church is worth without any
money moving — depreciation charged, assets donated in kind, and the gain or loss
on anything disposed of — and then the surplus after those. Both reports draw on
the same figures as the Income & Expenditure statement, so all three agree.

**A misleading note has been removed from the Income Statement.** It claimed its
net assets figure tied back to the Statement of Financial Position. It never did:
that statement reports the money held in the funds, while the financial position
also counts what the church owns. The statement now says which it is reporting,
and shows the value of the fixed assets held outside the funds — the main reason
the two differ.

The two statements still do not add to exactly the same total, and the report no
longer pretends otherwise. Closing that last difference properly is on the list.

---

# v3.08.2 — Negative figures on the statements, shown the usual way

A presentation correction. On the Income & Expenditure statement and the
Statement of Changes in Net Assets, the depreciation and disposal lines showed
their brackets in a way that bypassed the system's standard accounting format.
The figures were right; the formatting was written by hand rather than done the
way every other statement does it.

They now use the same accounting format as the rest of the system, so negatives
are shown consistently wherever they appear — and the check that enforces that
consistency across all statements passes again.

---

# v3.08.1 — Asset import: CSV files, a sample to start from, and clearer errors

Fixes and improvements to bringing in your asset register.

- **CSV files can now be imported**, not only .xlsx. Commas, semicolons or tabs
  all work, and a file saved from Excel as CSV is read correctly.

- **A sample file to start from.** "Download a sample file" on the import page
  gives you a CSV with the headings filled in and a few example assets. Replace
  the rows with your own and upload it.

- **Files from other programs are handled better.** Some spreadsheets describe
  their own size incorrectly, which stopped the file being read at all; those are
  now read a second way that copes. An older .xls file now says so and tells you
  to save it as .xlsx or CSV, instead of failing with a technical message.

- **Clearer errors.** If a file still cannot be read, the message now says what to
  try rather than showing an internal error.

---

# v3.08.0 — Asset reports, and loading your existing register from a spreadsheet

**Bring in the assets you already own.** A new Import option on the asset register
reads a spreadsheet and loads what the church owns. It matches your column
headings by name, so your own spreadsheet will usually work as it is — only a name
and a cost are essential, though an acquisition date is worth having, since
depreciation runs from when an asset came into service.

Nothing is saved when you first upload. The file is read and shown back to you:
what would be brought in, and what would be left out with the reason — no cost, a
date that could not be read, an asset already on the register, the same asset
twice in one file, or an amount below your capitalisation threshold. The assets
are added only after you confirm. Anything imported is recorded as already owned,
rather than as a purchase made through the system.

**Four asset reports, in the report library.** Alongside every other report, with
the same date filters, printing and exports to PDF, Word, Excel and CSV:

- **Fixed Asset Register** — what the church owns, at cost, with depreciation to
  date and net book value.
- **Fixed Asset Movement** — opening value, what was bought, what was donated,
  depreciation charged, what left through disposal, and the closing value. This is
  the note that supports the assets figure on the Statement of Financial Position.
- **Depreciation Schedule** — the charge for the period, asset by asset, including
  anything sold part-way through.
- **Asset Disposals** — what left, what it was worth, what was received for it,
  and the gain or loss.

Every figure in these comes from the same shared catalogue the statements use, so
they agree with each other.

---

# v3.07.0 — Assets in the financial statements, and pages that stay on the page

Two things: the statements now account for assets properly, and a layout fault
that pushed page content under the sidebar is fixed.

**Depreciation now appears in Income & Expenditure.** It was missing altogether,
so expenditure was understated and the surplus overstated by the whole
depreciation charge. It is shown under "Non-cash items" — with donated assets, and
a surplus after non-cash items — while the income, expenditure and surplus figures
above continue to show cash only, as before.

**The movement in fixed assets is now built from real figures.** On the Statement
of Changes in Net Assets, depreciation used to be worked out backwards from the
opening and closing values, which meant it quietly absorbed disposals and donated
assets too — a donated asset made it look as though the assets had gained value.
Each line is now its own figure: what was added, what was donated, what was
charged as depreciation, and what left through disposal. If those ever fail to
add up, the statement says so rather than hiding the difference.

**A correction worth knowing about.** Net book value was being calculated over
every asset on the register, including ones bought later, so the value shown for
any date in the past was too high by the cost of anything acquired after it.
Today's figures were right; older ones were not. This affected the Statement of
Financial Position. It is now calculated the same way as cost and depreciation,
and the three always agree.

Also in this release: depreciation is no longer projected forward to the end of an
unfinished period, the first month of a period is no longer left out of the
charge, and depreciation on an asset sold part-way through the year is now counted
up to the day it was sold.

**Pages spilling under the sidebar.** The asset pages, and the envelope import
page, were missing or had surplus closing tags, which ended the page layout early
and let everything after it slide under the sidebar when scrolled. Fixed, with
checks added so it cannot happen again unnoticed; wide tables now scroll within
their own panel instead of stretching the page.

---

# v3.06.0 — An asset's cost counts from the day you bought it

Until now, an asset bought part-way through the year was treated as though the
church had owned it since 1 January, because its cost came in with the opening
balances. That is now corrected.

- **Cost starts at the acquisition date.** An asset bought in June appears on the
  register from June. Ask what the church owned in January and you get January's
  answer, not today's. Anything bought since the opening date is carried into the
  books by the payment that bought it, or — for a gift — by the donation itself.

- **The check confirms it stays right.** The asset cost check added last release
  now serves as the standing explanation for any difference between the register
  and the books: if the two ever disagree, it names the assets responsible and
  what to do about each.

The register and the ledger still agree exactly, and no other part of the system
is affected — reports, cash book, giving, statements and expenses all behave
exactly as before.

Also fixed in this release: the "put on the asset register" page failed to open
because of a formatting fault (recording a payment as an asset worked, but the
page itself would not display).

---

# v3.05.0 — Asset cost check before recognising cost from the acquisition date

A new read-only check, ahead of a planned improvement.

At present an asset bought in June is treated as though the church had it from
1 January, because its cost is brought in by the opening balance. The plan is to
recognise each asset's cost from the date it was actually acquired. Before making
that change, this release adds a check that tells you whether your register is
ready for it — and if not, exactly what to put right first.

- **Where to find it.** "Asset cost — ledger backing check", from the asset
  register. It reads only; it changes nothing, so it is safe to run at any time.

- **What it looks for.** Two things. An asset added since the opening date with
  no payment or donation behind it — the books would come up short by its cost.
  And a payment dated after the opening date for an asset the church already
  owned at the opening date — that payment would end up counted twice.

- **What it tells you.** Each asset concerned, how much of its cost is accounted
  for, how much is not, and what to do about it in plain words — usually linking
  the payment that bought it, correcting the cost, or fixing a date.

Nothing about today's figures changes: the register and the ledger still agree
exactly, as they have since assets came onto the ledger.

---

# v3.04.0 — The life of an asset (EAM Phase 2c)

The register now follows an asset through its whole life, not just its value.

- **A lifecycle you can see.** A new Lifecycle board shows every asset in the
  column for the stage it is at — planned, on order, under construction, in
  service, idle, under maintenance, impaired, held for disposal — with a count
  and total value per column. Assets are moved along by buttons on each card, and
  only the moves that make sense are offered.

- **Who has it, and where.** An asset can be issued to a custodian and checked
  back in, with the date and its condition each way. The register always knows
  who is holding what, and an asset that is still in someone's hands cannot be
  put up for disposal until it is returned.

- **Moving an asset between locations or funds.** You can request a move; if it
  changes the fund that owns the asset, it needs a second person's approval,
  because the asset's value moves from one fund to another. Approval is what makes
  the move happen, and the person who requested it cannot approve it themselves.

- **A full profile for each asset.** The asset page now shows its status and who
  holds it, its custody and movement history, the accounting entries it has
  produced, its depreciation history, and a plain timeline of everything that has
  happened to it.

- **A disposal stays a disposal.** An asset can never be written off just by
  changing its status — a disposal must record the date, method, proceeds and
  fund, and post its entry to the ledger.

The register and the ledger continue to agree exactly.

---

# v3.03.0 — Donated assets as net assets, and non-cash contributions on the statement

Two corrections to how gifts in kind are treated.

- **A donated asset is no longer counted as income.** It is added to the church's
  net assets instead: to designated / restricted funds where the receiving fund is
  restricted, and to the capital (asset) fund otherwise. The fund is recorded
  either way, so the gift stays attributable to it. This matches the way a gift of
  property is properly accounted for — the church is better off by the value of
  the asset, but no money was received.

- **The Income & Expenditure statement now shows non-cash contributions.** A new
  "Non-cash contributions — donated assets" section lists each asset given during
  the period, with donor, fund and fair value, and a total. It sits outside the
  income and net surplus figures, which continue to reflect cash only — so gifts
  in kind are visible without distorting the cash result.

Also in this release: the gain/(loss) on disposals shown on that statement now
comes from the shared financial metrics catalogue rather than being calculated
separately, and a small layout fault in the statement's markup was corrected.

---

# v3.02.0 — Buying, building and being given assets (EAM Phase 2b)

Assets now record how the church came by them, and a capital payment can be put
straight onto the register.

- **Put a payment on the asset register.** On a capital payment there is now a
  "Put on the asset register" action. It creates the asset at the amount paid and
  links the payment to it, so the money is held as an asset instead of being
  counted as a running cost. You can also add the payment to an asset already on
  the register — use that for construction or an improvement, where several
  payments build up one asset's cost. Nothing is entered twice.

- **Donated assets are recognised properly.** An asset given to the church is
  brought onto the register at its fair value and recognised as donated-asset
  income against the fund, with the donor on record — so a gift in kind shows up
  as both an asset and support received, instead of appearing from nowhere.

- **Every asset says where it came from.** Purchased, donated, self-constructed,
  transferred in, or already owned before the system started — recorded once,
  when the asset is added, and kept with it.

- **Keep small items off the register.** You can set a capitalisation threshold in
  settings; purchases below it are treated as running costs. It starts switched
  off (set to 0), so nothing changes until you choose an amount.

- **Disposals must name a fund.** The fund receives any sale proceeds and carries
  the gain or loss, so every disposal belongs to a fund. This also closes a gap
  where proceeds recorded without a fund were left out of the ledger.

The asset register and the ledger continue to agree exactly.

Still to come: the asset lifecycle screens — status transitions, transfers and
custody, the full asset profile, and a board view of where each asset stands.

---

# v3.01.0 — Asset disposals on the ledger (EAM Phase 2a)

Selling, scrapping or writing off an asset is now recorded in the books as a
proper journal, not just a note on the asset register.

- **A disposal produces a real journal entry.** The asset's cost and its
  accumulated depreciation are taken out of the fixed-asset accounts, and the
  difference between what you received and what the asset was still worth is
  recognised as a gain or a loss on disposal. The Trial Balance, Income &
  Expenditure and Statement of Financial Position all pick this up.

- **The money you received is still the money you received.** Sale proceeds
  continue to be recorded as a receipt into the fund you nominate, so fund
  balances and the cash reports are unchanged. Only the genuine gain or loss
  reaches the income result — the proceeds themselves are not counted as income.

- **The register and the ledger still agree, before and after a disposal.**
  An asset is now treated as being on the register right up to the day it is
  disposed of, instead of disappearing from earlier periods. The reconciliation
  on the Depreciation runs page stays exact through a mid-year disposal.

- **The final month's depreciation is charged.** Depreciation runs now include
  assets disposed of during the month, so the charge is complete up to the
  disposal date.

Note: if you record proceeds without choosing a fund to receive them, the
proceeds are not reclassified in the ledger — choose the receiving fund when
disposing of an asset that was sold.

Still to come: entering new asset purchases through a proper acquisition
workflow (with a "convert an expense to an asset" helper), and the asset
lifecycle screens.

---

# v3.00.0 — Assets on the ledger (EAM Phase 1)

Church assets are now part of the general ledger, not a separate spreadsheet-style
register. Three things change:

- **Depreciation is charged monthly and posted to the books.** Each month a
  depreciation run books the charge (an expense) against accumulated depreciation,
  exactly like every other posting — so the Trial Balance, Income & Expenditure and
  Statement of Financial Position all reflect it. You can run it from the new
  **Depreciation runs** page (under the asset register) or on a schedule. Moving
  from a yearly to a monthly basis slightly changes the depreciation figures.

- **The register and the ledger now reconcile.** An opening balance brings the
  fixed-asset control accounts up to match the register, and the Depreciation runs
  page shows a live reconciliation so you can confirm at a glance that the books and
  the register agree — cost, accumulated depreciation and net book value.

- **Capital spending is handled correctly.** Money spent buying or building assets
  is held as an asset (capital work-in-progress), not counted as an ordinary expense,
  and is capitalised to the asset once it's linked to a register record.

Everything is backward compatible and the statements still balance. Disposals on the
ledger, and a workflow for entering new asset purchases (with a "convert an expense
to an asset" helper), come next.

---

# v2.99.0 — Asset management foundation (EAM Phase 0)

The first step of turning the basic Fixed-Asset Register into a full asset
management module. This release is groundwork only — nothing you see changes,
and every figure on the financial statements is identical.

What's now in place under the hood:

- **Asset classes** you can configure (with their own depreciation policy),
  replacing the fixed built-in category list — so a new class like "Library" or
  "Heritage" no longer needs a code change.
- **Locations** you can nest (campus → building → room), separate from the fund
  that owns an asset.
- **A lifecycle status on every asset** (planned, in service, under maintenance,
  held for disposal, disposed, and so on), plus asset tags, serial numbers, a
  commissioning date, a custodian, and the groundwork for multi-church use.
- **Every asset figure now flows through the central metrics catalogue** — the
  same place every other financial number comes from.

Every existing asset was automatically backfilled with a class, a tag and a
status, so the register is ready to use immediately. The next phase — putting
asset depreciation and disposals onto the general ledger — changes how the
financial statements are built, so it will wait for your sign-off on the
accounting treatment.

---

# v2.98.0 — Formal statements show negatives the accountant's way

The four formal statements — Income & Expenditure, the Income Statement, the
Statement of Financial Position and the Statement of Changes in Net Assets — and
the Trial Balance now show negative amounts in accounting parentheses, e.g.
"(1,234.50)" instead of "-1,234.50". This is the standard convention on formal
financial statements. The parentheses are real characters, so they carry through
to the Word and PDF exports as well as the screen.

Positive figures and all totals are completely unchanged; only the presentation
of genuine negatives differs. Everyday screens (dashboards, ledgers, giving,
expenses) keep the plain minus sign.

---

# v2.97.0 — One place for money formatting; consistent currency symbol

**Central money filter.** Every monetary figure in the app used to be formatted
with the same little incantation repeated in about 167 templates. That is now a
single `money` filter — the amount, its decimals, its thousands separators and
its handling of blanks all decided in one place. Nothing you see changes: the
filter was checked digit-for-digit against the old formatting across hundreds of
values before every page was switched over. A companion `money` variant renders
negatives in accounting parentheses "(1,234.50)" for the places that want the
accountant's convention.

**Consistent currency symbol.** The app had drifted into showing "KES" in some
places and "KSh" in others, ignoring the symbol set in Settings. Every figure's
symbol now comes from that one setting, so changing it in Settings changes it
everywhere at once.

No figures, models or workflows changed — this is formatting and consistency
only.

---

# v2.96.0 — More display preferences; leader pages join the statement design

**Appearance & Preferences** gains five new controls, each applying instantly
as you click, like the existing ones:

- **Headings** — the classic serif titles, or plain sans-serif.
- **Figures** — monospace numbers that align digit-for-digit in columns
  (default), or numbers set in the ordinary text face.
- **Row stripes** — the subtle alternating shading in tables, on or off.
- **Gridlines** — row lines only, or a full grid with column separators.
- **Sticky table headers** — keep column headings visible while scrolling
  long tables, or let them scroll away.

The landing-page choice also offers more destinations: Members, Envelopes,
Benevolent schemes and Budgeting.

**Leader pages redesigned to match the app.** Department leaders previously
saw a green banner design found nowhere else in the application. Every leader
page — departments, collections, expenses, advances, loans — now opens with
the same statement masthead as the rest of the app, and prints with a proper
document head. Nothing about what leaders can see or do changed.

---

# v2.95.0 — Reports code split into feature modules

Internal engineering, first of four sessions: the single largest source file
(reports/views.py, over 4,000 lines) is now thirteen focused modules — one per
report family (remittance, board pack, financial statements, development
groups, the treasurer report, and so on). Nothing changed for users: every
page, export and link works exactly as before, verified name-by-name against
the old file and by the full reports test suite.

Why it matters: smaller files are faster to navigate, safer to change, and a
new guard test stops any module from quietly growing back into a giant.

Also fixed along the way: two stale tests surfaced by running the full suite —
one asserting old dashboard wording, one flagging an improperly formatted
template comment.

---

# v2.94.0 — Statement design across the whole app

The statement design that arrived with the report engine, dashboards and the
transactions pages now runs through every page of the application.

**Every page, at once.** The two page-header styles used across ~246 screens
were upgraded in place — every page now carries the family's forest-over-brass
rule and spacing, with no behaviour changes anywhere.

**Printing fixed everywhere.** Until now, printing any ordinary page produced a
document with no title at all — the page header was simply left off the paper.
That bug had been reported and patched one page at a time; it is now fixed
globally. Every page prints with its title and description as a proper document
head, while the buttons and filters around it stay off the paper. A test pins
this so it cannot quietly break again.

**Key pages, full treatment.** Members, statement imports, bank
reconciliations, petty cash, transfers, funds & departments, the benevolent
dashboard, case list and registry, the intake queue and the budget board all
received the complete masthead — section label, title, description and the
double rule — with export links in the brass segmented style. The envelopes,
pledges, payments, advances, loans, assets, users and accounting screens keep
their layouts and gain the section label.

Presentation only: no figures, models or workflows changed.

---

# Changelog

## v2.93.0 - No setting can go missing

Fixed a whole class of bug where a newly added setting could quietly fail to show
up. Previously, several places kept a hand-written list of which fields to display
or save, and if someone added a field but forgot the list, that field simply never
appeared — with no error. This has happened more than a dozen times over the
project's life.

Now the settings form shows every setting automatically rather than from a list
that can fall out of date, and any setting that hasn't been given a home on a
specific tab appears under a new "Other" tab instead of disappearing. Turning this
on immediately surfaced a number of settings that had become unreachable — cheque
printing alignment, pledge matching, error alerts, off-site backup, and the
"require two-factor for treasurers" option among them — so they can be edited
again.

The same protection was added to the welfare-scheme policy rules: a test now fails
the build if a new policy field is ever added without being classified, so it can
never silently drop out of the rules that get versioned. None of this changes any
existing behaviour or figures; it only makes sure nothing goes missing.
# Changelog

## v2.92.0 - Locked by default

Every page in the application now requires you to be logged in unless it is one of
a small, deliberately chosen set of public pages (the login and password-reset
screens, the health check, the bank's data feed, and the optional public pledge
form). Previously each page had to remember to ask for a login individually; now
the default is that a page is protected, and the few public ones are marked as
such on purpose.

In practice nothing changes for anyone signed in — the same people can reach the
same pages as before. What changes is safety: if a new page is ever added and
someone forgets to protect it, it is now closed by default instead of open, and
an automatic test will flag it by name before it can ship.
# Changelog

## v2.91.0 - Faster benevolent reports

The welfare-scheme reports — contribution compliance, arrears ageing, and the
scheme overview — now open much faster, especially for large schemes. They used
to do a separate round of database work for every single member, so a scheme with
two hundred members was thousands of database queries; now the same figures are
gathered for the whole scheme in one short, fixed set of queries no matter how
many members there are. On the demo data the compliance report went from 139
queries to 32, and unlike before, that number no longer climbs as the membership
grows.

The numbers shown are exactly the same as before — this was purely about how they
are fetched, not how they are calculated, and each figure is still produced by
the one shared calculation the register and the eligibility decision both rely on,
so they can never disagree.

While making this change we also found and fixed a subtle correctness bug: in a
scheme where the rules had been re-published, a member's record could briefly be
judged against the old rules instead of the current ones. That is now guarded by a
test so it cannot come back.
# Changelog

## v2.90.0 - Continuous integration

The project now has an automated safety net. Every time a change is pushed, a
build runs on its own and checks three things: the application still starts, the
database models and their migrations still agree, and the test suite still passes.
The result shows as a single green or red mark, so the health of the code is
known at all times rather than only when someone remembers to run the tests by
hand.

Because the test suite is large, it is split into several groups that run at the
same time, so the whole thing still finishes quickly. A small built-in guard makes
sure every part of the app is included in one of those groups — if a new area is
ever added and left out, the build says so by name.

None of this changes how the application behaves; it is entirely about catching
mistakes before they reach the live site. A short guide, docs/CI_GUIDE.md,
explains how to read a failed build, how to run the same checks on your own
machine before pushing, and how to require a green build before anything is
merged.
# Changelog

## v2.89.0 - The ledger, expenses page and expense form join the statement design

The two pages a treasurer lives in — the ledger and the expenses list — now open
with the same masthead as every report: the page's place in small brass capitals,
the title, and the double rule. Because their old headers were hidden when
printing, both pages used to print with no title at all; they now print with a
proper document head. Everything that already worked well — the clickable status
totals, quick-filter tabs, bulk actions with their itemised confirmations, the
running balance — is untouched.

The bigger change is the **expense form**, reshaped around how you actually fill
it in:

- **Fund first**, with the live available-balance line as before.
- **Amount & date** together, with the amount set large in the app's figure
  typography — on a money form, the money leads.
- **What it was for** — description, category, capital or recurrent.
- **Paid to & how** — claimant, payee, method, voucher, petty cash.

Nothing about how expenses save, validate or get approved changed — the balance
check, the override, the pay-now option and every autocomplete work exactly as
before. The form is simply organised the way the questions come to mind.
# Changelog

## v2.88.0 - Report designer: write your own words, preview before saving

The report designer now treats your own text as a first-class part of a report,
not an afterthought:

- **Headings.** A new Heading component divides a designed report into named
  parts — "Part 1 — Income", "Notes for the board" — set in the same style as
  every section heading in the app, and carried into PDF and Word as proper
  document headings.
- **Text blocks that behave like text.** Blank lines now make real paragraphs —
  on screen, in PDF and in Word — instead of collapsing into one block.
- **Merge fields.** Write {period_start}, {period_end}, {church} or {today} in
  any heading, text block or note, and the report fills them in when it renders —
  so "For the period {period_start} to {period_end}" stays correct whatever dates
  the reader picks. Anything else in braces is left alone.
- **Find them easily.** Heading, Text block and Note now sit together under a
  "Text" group in the component palette, and every text box carries a short
  reminder of the paragraph rule and the merge fields.

And the change that makes authoring quick: **Preview without saving**. A new
button opens the report exactly as it currently stands in a new tab — nothing is
saved, nothing goes live. Adjust a sentence, preview, adjust again, and only save
when it reads right. If something in the draft isn't valid yet, the preview
lists the specific problems instead of failing.

The designer pages also joined the same masthead design as the rest of the
reporting surface.
# Changelog

## v2.87.0 - Telegram bot: working links and a command reference

Three fixes to the Telegram bot, and one addition:

- The help text for **/balance** had become garbled — a stray bit of code left it
  reading "closing balance of a fundlt;fund…". It now reads cleanly: every fund's
  closing balance, or one fund's detail with /balance <fund>.
- Replies that point at a report or record now always show where to find it. When
  the site's web address is set (Settings → Telegram), the reply carries a tappable
  link straight to the web app; when it isn't, it names the exact page to open
  instead of silently leaving the link out.
- **/case**, **/member** and **/benevolent** now each include an "open in the app"
  link, so you can go from a figure in the chat to the full record in one tap.
- The Settings → Telegram tab now lists everything the bot can do — every command
  and what it returns — with a reminder that anything touching money is saved as
  pending for approval in the web app.
# Changelog

## v2.86.0 - The dashboards join the statement design

The main dashboard and the executive dashboard now open the same way every
report does: a proper masthead — the page's place in small brass capitals, the
title, the period (or an "as at" timestamp on the executive view), and the double
rule beneath. Because the old dashboard headers were hidden when printing, both
pages used to print with no header at all; the masthead now prints as the
document head.

On the main dashboard, the date-range picker sits in the masthead with proper
labels, section headings carry the same brass-hairline treatment as the report
library, and "Needs attention" now appears once: the compact pill row only shows
where the fuller attention panel isn't on the page, instead of repeating the same
counts twice.

The executive dashboard — the page most often projected at a board meeting —
gains a "Board copy · Print" control in its masthead and a print layout that
drops the AI panel and lays the charts two-up. Its key-figure cards now use the
app's own figure typography (tabular monospace digits, the KES prefix set small),
with a coloured top edge marking anything that needs a second look. And its
charts now draw every colour from the app's palette — the one off-palette orange
that had crept in is gone.

Nothing moved and nothing was removed: every widget, chart, table, PNG export and
the AI briefing work exactly as before — they just dress consistently now.
# Changelog

## v2.85.0 - A statement-set report page and a browsable report library

Every engine report now opens with a proper report masthead, set like the head of
a printed financial statement: the report's category in small brass capitals, its
title, the reporting period in tabular figures, and a double rule beneath — forest
over a brass hairline. The same head carries straight through to print.

Around it, the page tidied up:

- The export buttons are one grouped control — Export: CSV · Excel · PDF · Word ·
  Print — instead of a row of separate buttons.
- Filters are labelled fields now, not bare boxes you had to hover to understand.
- Key-figure cards carry the app's accent rhythm and show the KES prefix small,
  the figure large — and can show percentages, day-counts and plain counts, not
  only money.
- Long tables keep their column headers in view as you scroll; negative figures
  show in red; blank cells show a quiet dash instead of nothing.
- An empty report now tells you what to do about it — widen the dates — rather
  than just stating there is no data.

The report library was rebuilt from a flat list into a browsable grid: each report
is a card with its category, a short description, a favourite star and its
snapshot history, grouped under category headings. Typing in the search box
narrows the grid instantly as you type.

One template serves every engine report, so all of this applies to the whole
catalogue at once — including the fourteen benevolent reports added in v2.84 —
with exports and permissions untouched.
# Changelog

## v2.84.0 - Fourteen new benevolent reports

Fourteen reports join the report library, each filterable by scheme and period,
and each — like every other report in the system — printable and exportable to
Excel, CSV, PDF and Word with nothing extra to set up.

**Where members stand:** Contribution Compliance shows what share of each member's
dues periods were paid, with the least-compliant first. Ageing Arrears takes the
arrears total and ages it into bands, with a chart. Fund Sustainability shows each
fund's balance once its commitments are set aside, and Contribution Forecast
projects each scheme forward at its recent run-rate to show whether — and roughly
when — it might run dry.

**Where cases stand:** Pending Approvals lists everything awaiting a decision or a
payment; Rejected Cases shows what was refused and why; Case Turnaround shows how
long cases take at each stage; Outstanding Documents flags open cases still missing
required paperwork before they can be approved.

**The bigger picture:** Benefit Utilisation compares what members put in against
what flowed back out as benefits; Scheme Surplus/Deficit shows each scheme's
operating result; Household Statistics and Dependant Demographics show who the
schemes actually cover, by household size, relationship and age; Committee
Performance shows each committee member's activity; and Fraud Red Flags brings the
red-flag scan into the report library for the board.

Several of these lean on visuals — compliance gauges, utilisation bars, forecast
lines, demographic doughnuts — in the app's own colours, so a report reads at a
glance and still exports cleanly to a document.

Every figure in every one of these reports comes from the same Financial Metrics
Registry the rest of the system uses; none of them computes a money total of its
own.
# Changelog

## v2.83.0 - Automation jobs and a review-task inbox

The nightly automation already recomputed where every member stood and sent due
reminders. It now does the rest of the routine watching a welfare scheme needs —
but with one firm rule: it never changes a member's status on its own. Suspending
a member, closing a membership, or ending a dependant's cover is a decision a
person makes and answers for. So where a job sees that such a decision is due, it
raises a task rather than acting on it.

**A new Review tasks inbox** (Benevolent -> Review tasks, with an open-count badge)
is where those land. Each task says what was found and what the policy would do,
links straight to the member or case, and waits for you to confirm or dismiss.
Marking a task done records that it was dealt with — it doesn't itself move
anything.

What the jobs watch for:

- **Overdue members** whose policy says they should be suspended or lapsed — raised
  as a task, the member left active until you confirm.
- **Long-suspended, long-idle memberships** worth closing off the register.
- **Child dependants who've passed the age limit** — flagged rather than dropped,
  because a church may keep a dependant in full-time education or with a disability.
- **Members who've just become eligible** after serving their waiting period.
- **Possible duplicate memberships** — the same name and phone enrolled twice (a
  shared family phone with different names is left alone).

And one job that runs on its own because it's pure housekeeping: **archiving cases**
that have been settled for more than six months, so the working case list shows
what's live. Archiving is just a display flag — nothing moves, and it can be undone.

None of this changes how anything is recorded; it's the nightly routine noticing
what needs a person's attention and making sure none of it sits unseen.
# Changelog

## v2.82.0 - Red flags: fraud detection for the welfare schemes

A new Red flags page (Benevolent -> Red flags) scans the schemes for the patterns
an auditor would look for by hand and lists anything worth a second look. It is
deliberately not a black box: every item says exactly what it found and why, links
straight to the case or member in question, and is ranked high / medium / low. It
never blocks anything and never accuses anyone — most flags have an innocent
explanation, and the whole point is that none of them goes unseen.

What it looks for:

**Control breaches.** A case both raised and approved by the same person; a payout
made to the very person who raised or approved it; a case approved over a failed
eligibility check with an override; a fund approving benefits it can't currently
afford.

**Membership abuse.** A member who claimed almost immediately after joining, having
paid little or nothing in; a member who joined, claimed, and left in quick
succession.

**Identity overlaps.** The same person named as beneficiary across several cases
under different members; one phone number registered against many members.

**Contribution manipulation.** A contribution reversed soon after a claim was paid —
money put in to look paid-up long enough to qualify, then pulled back out once the
benefit was secured; a burst of reversals by one person in a short window.

None of this required any new record-keeping — it's built entirely on the audit
trail the module already keeps (who raised, approved and recorded each thing, when
members joined and left, and what was reversed). It's a set of new questions asked
of information that was already there.
# Changelog

## v2.81.0 - Fund solvency: can the fund afford it, and is it sustainable?

The accounting was already right — a benefit is an ordinary expense in the scheme's
fund, in the ledger like any other payment. What was missing were the questions a
committee needs answered *before* it commits money, and this release adds them,
without changing a single thing about how the ledger records anything.

**Can the fund afford this payout?** When a benefit voucher is raised, the app now
checks it against the cash actually available after everything already approved. By
default it warns — a church may legitimately approve a payout it intends to fund
from a levy still being collected — and a new per-scheme setting can turn that into
a hard block for a fund that must never go negative.

**Where does the fund really stand?** A new Fund position page (Scheme → Fund
position) shows the balance with each claim taken off it in turn: what's approved
but not yet paid, what's on vouchers awaiting approval, and a prudent reserve for
open cases still working through the pipeline — ending with what's genuinely free to
commit to a new case. It flags a fund that's depleted (can't cover its approvals),
negative, or fully committed. These are memorandum figures: promises the fund must
honour, never restated as balance-sheet liabilities, because the balance already
reflects every voucher that's been approved.

**Is the fund sustainable?** The same page projects the fund forward month by month
at its recent run-rate — money in, money out, closing balance — and flags roughly
when it would run dry if nothing changes. It's a plain, follow-it-by-hand
projection, because a forecast a treasurer can't check is one they can't trust with
a welfare fund.

Every figure on these views comes from the same Financial Metrics Registry the board
pack and fund statements use, so nothing here can disagree with what the rest of the
app reports.
# Changelog

## v2.80.0 - Contribution exceptions and automatic reconciliation

The scheme's contribution handling now copes with the things that go wrong in
real life, not just the clean case where the right money arrives from the right
member on the right day.

**Money paid on someone's behalf, or anonymously.** A contribution can now record
WHO actually paid — the member themselves, their employer, a sponsor, another
third party, or an anonymous donor. An employer paying a member's dues is recorded
as exactly that: the member's dues, paid by the employer — so the member's
statement stays honest rather than pretending they paid it themselves.

**Reversing a payment.** A payment that bounced, was entered by mistake, or turns
out to be a duplicate can be reversed. Nothing is ever deleted — the original and
its reversal both stay on the record, with the reason, so the member's statement
and any auditor can see what happened. The money correctly leaves every total and
shows a contra entry on the bank reconciliation.

**Fixing a wrong attribution.** A contribution recorded against the wrong member,
the wrong scheme, or as the wrong kind of money can be corrected. This reverses the
wrong entry and books a correct one carrying the same money, rather than quietly
editing the original — so the correction itself is on the record.

**Catching mistakes as they're entered.** Recording a contribution now warns about
a future date, a date before the member's cover began, a long-backdated receipt, or
a possible duplicate — and blocks a closed accounting period or a non-positive
amount outright. A bulk upload is screened as a whole before any of it commits, so
a bad row is caught before the good ones post.

**Automatic reconciliation.** A new per-scheme reconciliation (Scheme → Reconcile)
checks that everything the scheme has recorded as contributions agrees with the bank
receipts that actually carry the money — flagging money banked but never attributed,
contributions whose receipt has gone, and any amount that disagrees with its receipt.
It's the benevolent-side counterpart to the bank reconciliation the rest of the app
already does.
# Changelog

## v2.79.0 - Eligibility rules real welfare schemes require

The policy engine now carries the standing rules church constitutions actually
use, alongside the ones it already had. Each is a per-scheme policy setting, so a
Medical fund and a Burial fund can require completely different things without any
code change — and each shows up, with its reasoning, on the case's eligibility
breakdown exactly like every existing check.

**Paid-up tenure.** A policy can require a member to have paid in for a set number
of months before a claim qualifies — the "you must have contributed for 3 / 6 / 12
months" rule most schemes have. This counts months actually paid in full, which is
deliberately different from the waiting period (calendar time whether or not anyone
paid) and from a bare contribution count (two payments in one month wouldn't satisfy
a two-month tenure).

**Unbroken contribution record.** For schemes that expect members to never lapse, a
policy can require an unbroken record: any period missed at the time it fell due
disqualifies, even if the member later back-paid it — with an optional tolerance for
the member who genuinely forgot a month or two.

**Partial arrears, counted in periods.** As well as the existing "how many shillings
may they still owe" tolerance, a policy can now say "up to N periods behind is fine" —
which is easier to reason about than an amount when the dues rate has changed over the
years.

**Catch-up re-qualification.** A policy chooses whether clearing arrears restores
cover immediately (the default, and what most schemes do) or only after the member has
stayed paid-up for a set window. The window only ever applies to a member who genuinely
just back-paid a late gap — someone who has always paid on time is never caught by it.

All of these sit on top of the grace period, waiting period, arrears treatment and the
dozen other checks already in the engine, and every one is frozen onto a case when it's
assessed, so an auditor can see years later exactly which rules applied and why.
# Changelog

## v2.78.0 - Sidebar scroll fix, and a rebuilt permission-profile editor

### The sidebar no longer jumps back to the top

Clicking a link deep in the sidebar used to scroll you back up to the top on the
next page. The cause was an ordering bug: the app restored your scroll position
first and *then* expanded the navigation group for the page you're on, which
changed the sidebar's height and threw the restored position away. Now the group
state is applied first and the scroll position is restored afterwards, once the
layout has settled — so you stay where you were. A click on a nav link also saves
your position immediately, so it can never be missed.

### The permission-profile editor is much easier to use

The page for editing a permission profile (a named bundle of rights you assign to
people) was a long, flat wall of checkboxes. It now has: a live count of how many
rights and people are selected, a search box to filter rights by name, a search box
to find a person, a "select all / clear" for the visible rights, an "all" toggle per
group with a running count, and a sticky save bar that follows you down the page and
summarises what you're about to save. The layout is a clean two-column split — rights
on the left, people on the right — and the save action names what it does ("Save
changes" vs "Create profile"). No change to how profiles or rights actually work.
# Changelog

## v2.77.0 - God-file refactor: accounting logic moved out of the two big view files (no behaviour change)

A structural, behaviour-preserving pass on the project's two largest files —
`reports/views.py` and `cashbook/views.py`. Pure calculation and query helpers
that had accumulated inside these view modules over many releases were moved
into properly-named, single-responsibility service modules, continuing the
pattern already established by `cashbook/services/treasury_position.py`.

Nothing about how the app behaves changes. Every function that moved is
re-exported from its original module under its exact original name, so every
existing import path — including the ones other apps rely on, and every
`views.ClassName` reference in the URL configuration — keeps working exactly as
before. This was verified three ways: the full cashbook test suite still passes
with the identical count (402), the full reports, statements and giving suites
plus the targeted core suites for every module that imports from these files all
pass, and a direct import-surface check confirms every externally-imported
symbol still resolves and both URL configs still load cleanly.

**Moved this release**

- `reports/services/goals.py` — the Camp Meeting goal records and the
  sentence-case fund-name helper.
- `reports/services/remittance.py` — days-outstanding, the post-bulk-update
  ledger repost, and the remittance dashboard rows.
- `reports/services/devgroups.py` — the balance-by-giving development-group
  partitioning algorithm.
- `cashbook/services/receipts.py` — the acceptable-receipt-file rule and the
  missing-receipts queue query.
- `cashbook/services/cheque_words.py` — the amount-in-words renderer for cheque
  printing.
- `cashbook/services/advances.py` — the advance running-balance statement
  builder and the account-against-an-advance expense recorder (both also used
  by the leaders' pages).

`reports/views.py` shrank from 4,180 to 4,034 lines and `cashbook/views.py`
from 3,508 to 3,358, with the extracted logic now living where it can be tested
and reused directly rather than through a view.

**Deliberately left for a dedicated pass**

The largest remaining clusters are view code, not pure helpers — the Monthly
Treasurer's Report and the board/position statements in reports, and the
expense/advance/petty-cash view clusters in cashbook. Splitting those (and
ultimately turning each views file into a package of topic sub-modules) touches
module-level import ordering across dozens of interdependent classes, so it is
recorded as its own future pass rather than rushed in alongside this one.
# Changelog

## v2.77.0 - Internal refactor: financial helpers moved out of the two big view files (no behaviour change)

A structural clean-up with no functional change. The two largest view files —
`reports/views.py` and `cashbook/views.py` — held a number of pure financial
helper functions that don't belong in a view layer at all (a query or a
number-to-words routine is not a view), several of which were imported from all
over the app. Those have been moved into properly-named service modules,
following the same pattern already established by the treasury-position service.

Every function was moved verbatim and re-exported from its original module under
its original name, so nothing that imported them had to change and no behaviour
shifts. Moved: the camp-goal records, the remittance-dashboard rows and their
ledger-repost/days-outstanding helpers, the development-group balancing
algorithm, the receipt-validation and missing-receipts-queue helpers (with their
size/type constants), and the cheque amount-in-words renderer. Three scattered
mid-file import blocks in the cashbook views were also consolidated to the top.

Verified against the full test suites of every app that touches these files —
cashbook, statements, leaders, core, giving and reports, 1,706 tests in all —
plus a check that all 889 URL patterns still resolve their views and every
previously-importable helper still imports. The remaining view classes were
deliberately left in place: splitting them further carries real risk for little
additional benefit, and the misplaced calculation logic — the part that actually
mattered — is now where it belongs.
# Changelog

## v2.76.0 - Ten production items: case pre-fill, fast-path approval, an SMS Center, and a real intake bug

### Raise-a-case pre-fills from what's already on file

The "Raise a case" links (a household dependant's death, and now a proper link
on the member's-own-death panel too) actually pass the pre-fill the case form
already supported but never received — beneficiary, relationship, the fixed
benefit amount, and now the event date too. The member's-own-death panel also
checks for an already-open case first, linking to it instead of risking a
duplicate.

### Bank gifts naming a scheme by fund reference alone now reach the intake queue

Root cause: recognising a scheme from a bank narration had two paths — a
configured rule, or "this fund belongs to only one scheme" — but the second
path was structurally unreachable, called before the fund itself was even
known. A church that set up an ordinary fund-allocation rule (got the fund
right) but never a benevolent-specific one got a silently empty intake queue.
Fixed: the fallback gets a real chance once the fund is actually resolved.

### The levy page, and attributing intake money, respect a case's real status

The levy page no longer works on a still-draft case (nothing has been decided
yet to collect against) — fixed at the page and the shared validation layer,
while the roster calculator itself stays usable for preview purposes
elsewhere, unchanged. The intake page's case picker is now scoped to open
cases in the transaction's own scheme, not any case in any scheme; posting a
closed or wrong-scheme case directly is refused server-side too.

### One-step create-and-approve, where the scheme already allows it

Reuses the scheme's own `require_different_approver` setting — already
configurable, already exactly this permission — rather than adding a
redundant one. A new checkbox on the case form, visible only where the policy
permits it and gated again on the Approve right, chains submit → assess →
approve automatically; if assessment finds the case ineligible, it stops
cleanly rather than overriding anything.

### Inactivity: consecutive misses, or any miss in the last year

A scheme's policy can now choose how a "missed case" streak is counted — an
unbroken consecutive run (the original, unchanged default), or any misses in
a rolling 12 months even with paid cases in between, for a policy that wants
to catch sporadic non-payers rather than only someone who has stopped paying
outright.

### An SMS Center

One page (Scheme → SMS Center) reaches all active members, defaulters, members
one step from being marked inactive, or a specific case's unpaid levy roster —
computed the same way the rest of the module already computes them, so an
audience here can never disagree with a member's own standing page. A
"✉ Notify members" link on an approved case jumps straight to notifying
everyone. Sends through the same SMS integration and log as everything else
in the app.

### Roster import: a column for whether the registration fee was already paid

Separate from the pre-existing "mark paid up" (dues arrears only) — the
registration fee is settled before anything else once the obligations engine
sees a payment, so leaving this blank on someone who paid years ago would
make their next payment wrongly re-clear the fee instead of their dues.

### Three failing tests, one of them a real bug

Two were outdated expectations from intentional changes earlier in the
project (the obligations engine correctly prioritising an unpaid registration
fee; the dedup-key format that now keeps a shared bank reference's distinct
payments apart). The third — `ben_admin` and six other role-specific demo
users missing — turned out to be a real, broader bug: two "already seeded,
skip" guards in the demo-data seeder returned early without continuing to the
next phase, silently skipping the committee roster and all seven role users
any time their own guard condition was already true — which is every run of
the standalone seed command, and would also affect a second run of the full
one. Fixed.
# Changelog

## v2.75.0 - Historical case import, and a module review

### Bring cases already decided years ago straight to their outcome

The recommendation tracker has flagged this since the very first review round:
a church adopting this system has real history behind it — Edwin's own
workbook has 7,442 contribution rows across roughly 50 cases. There was a way
to import a roster and a contribution history, but no way to bring the CASES
themselves in.

A historical case lands directly at its known outcome — never re-run through
submit → assess → approve, which would judge a decades-old decision against
today's eligibility rules, and would send "your case was approved"
notifications for something that happened years ago. A paid amount is recorded
as a marked historical payout, never a live expense voucher, so nothing here
can be double-paid through the ordinary approval workflow and the scheme's
real fund balance — computed purely from the ledger — is provably untouched.

Bulk CSV import (Schemes → a scheme → Import case history), with an old
paper/workbook reference kept for cross-checking — and the existing
contribution importer now finds a case by that same reference, so the same
column from Edwin's workbook works in both upload files without needing to
look up newly-issued case numbers in between.

### Two bugs found while building it

A historical case left open (still awaiting the rest of its funding) whose
event predated the scheme's oldest policy record could not have a new levy
raised against it — blocking the very collection it was left open to receive.
Fixed. And the case search never matched the old workbook reference just
added — the entire point of recording one — so it's now searchable, shown on
the case list and detail pages, and included in the case export (which also
gained a "Paid" column it never had).

### A module review turned up a live settings bug

Running the module's own regression suite surfaced a real, current bug: the
four settings added when the obligations engine shipped (v2.70.0) — whether a
payment applies to what a member owes, single-open-case auto-allocation,
overpayment review, multi-obligation review — were never wired into the
settings page. A treasurer has had no way to see or change any of that
behaviour since it shipped. Fixed; a regression test now guards against this
exact class of bug recurring, as it once did before (recommendation #74a).
# Changelog

## v2.74.0 - Pending receipt: one sort, one highlight, everywhere

The name-sorted, duplicate-highlighted view added for the pending-receipt page
now applies everywhere that list is read — the Excel download, the PDF
download, and the PDF the Telegram bot's /pending command sends. Previously
only the on-screen page had this; the downloads were still date-ordered with no
way to spot a repeated name.

### One function decides the order and the duplicates

`pending_receipt_rows()` now returns its rows sorted by name (order-insensitive
— "Ruth Momanyi" and "Momanyi Ruth" sort together), and a new
`duplicate_name_flags()` is the one place that decides which names repeat. The
on-page view, the Excel export, and the PDF export (web and Telegram) all call
these same two functions, so none of them can quietly show a different order or
a different idea of "duplicate" from the others.

- **Excel**: repeated-name rows get a light brass highlight, and the name cell
  is marked "⚠ repeats" so it survives being printed or read without color.
- **PDF** (web and Telegram): the same highlight and marker, plus a summary
  line up top counting how many names repeat.

### Pending receipt has one home

The ⤓ Excel and ⤓ PDF download links have been removed from the ledger page's
quick-filter bar — they now live only on the Pending receipt page itself
(Ledger → Pending receipt → the download buttons are right there), so there is
one place to find them rather than three. The download URLs and their
parameters are completely unchanged, so nothing that already points at them —
a bookmark, the Telegram bot's /pending route — is affected.
# Changelog

## v2.73.0 - A typed reference could look like a receipt; pending receipt gets its own page

### A payment reference could accidentally look like a bank receipt

A paybill narration's typed reference (e.g. "expenses12") is free text the PAYER
wrote to say what the money was for — never the bank's own transaction receipt.
But it can accidentally be 10 characters with both a letter and a digit, exactly
the shape a genuine M-Pesa receipt has. When the same reference text was used on
two different payments (the same giver, paying twice for the same thing), both
were assigned the same false "receipt" — and since a genuine receipt is treated
as globally unique, the second payment was silently dropped as a duplicate of the
first. Real example: two payments from Joseph Ngwato, 11 days apart, for
different amounts, both referencing "expenses12" — the second (250.00,
UGDGTBSPCN) never made it into the register.

Fixed at the root: the receipt search now excludes the typed reference text
before looking for a genuine receipt, so it can never mistake one for the other.
Verified against the real 878-row statement that triggered this: every row now
gets a unique key, and the previously-dropped payment imports correctly.

### Pending receipt is now a page, not just a download

"Pending receipt" was previously an Excel/PDF export only — download it, then
scroll to spot anything odd. It's now also a page on its own
(Ledger → Pending receipt), sorted by name by default so the same giver's
entries sit together, with any name that appears more than once highlighted —
the same person twice, or one name recorded two slightly different ways, both
jump out rather than requiring a careful read top to bottom. "Same name" is
judged the same way the system already judges it for member matching
(order-insensitive), not a raw text compare. The Excel and PDF downloads are
unchanged, at their same links, so nothing that depends on them — including the
Telegram bot's /pending route — is affected.
# Changelog

## v2.72.1 - Fixed: the dedup-key migration could look stuck on a real production table

The migration that normalises register keys (v2.72.0) was fine on a demo-sized
database and genuinely risky on a real one: it ran inside ONE uncommitted
transaction with no progress output and two full-table passes per account. On a
production table with years of history, that is indistinguishable from "stuck"
even when it is technically still working, and holding one huge transaction open
risks long lock waits against any concurrent write.

Rewritten to be safe at real scale:
  - no longer wrapped in one transaction — each small batch (500 rows) commits
    on its own, so a kill-and-restart loses at most one batch, not the whole run;
  - prints progress per account as it goes, so it is visible rather than silent;
  - fetches only the columns it needs, and commits in smaller batches, so no
    single UPDATE statement is enormous;
  - is idempotent and resumable: an already-correct row is skipped with no
    write, so re-running after an interruption only does the remaining work.

Tested against a synthetic 56,000-line register: killed deliberately partway
through (3,500 lines done), confirmed no data lost and nothing marked applied,
then re-run to completion — it resumed and finished with all keys correct and
unique.

If your migration is currently stuck: it has not committed anything (it never
reached the end of an all-or-nothing transaction), so it is safe to stop it and
deploy this version — it will start from scratch on the same table and this time
show its progress.
# Changelog

## v2.72.0 - Debits no longer duplicate on re-import; undo a same-day register upload

### Debits stopped duplicating on the second import

When the register's duplicate-detection key changed (v2.69, so a bank could share
one reference across several distinct movements), lines ALREADY in the register
still carried the old keys. On the next import the new formula produced a
different key for the same line, so it did not match and was added again — debits
especially, because they lean on the bank reference rather than a unique M-Pesa
receipt, so nearly every re-imported cheque or charge duplicated.

The register now recognises a line under either key form, so a re-import matches
what is already there regardless of which version stored it. A one-off migration
also rewrites existing register keys to the current form, so the register
self-heals on upgrade. Verified: a register full of old-format debit keys
re-imports and adds nothing.

### Undo a register upload made the same day

Upload the wrong file, or to the wrong account? Each row in "Recent imports" on
the bank-register import page now has an Undo button on the day it was made. It
removes only the lines that upload added (a line that was skipped as a duplicate
belongs to an earlier import and stays) and clears any exceptions that pointed at
the removed lines. It touches no ledger transaction, expense or envelope — the
register is the bank's own record, so undoing a mis-upload asserts nothing about
the money. After the day of upload the button is gone, because reconciliation
work may rely on the lines and re-importing brings back anything still on the
statement.
# Changelog

## v2.71.0 - Take bank-register exceptions to the books, safely

The bank-register exceptions page — where the bank's record and ours disagree —
now lets a treasurer take entries to the books, one at a time or in bulk. The
point was never a single "create transaction" button: a raw bank credit or debit
is not always new money, and posting it blindly would corrupt balances. So the
treasurer says WHAT KIND of thing each exception is, and each kind hits the books
differently.

### Four dispositions

**Genuine new movement** — a receipt or payment the books have simply never seen.
Creates a review-queue entry (credit or debit) for normal allocation. The only
disposition that adds money.

**Banking of cash already receipted** — the bank credit that is the other leg of
Sabbath cash counted and already in the fund, then deposited. Reconciles the bank
line and counts toward the bank position, but is NOT income and belongs to no
fund — recognising it again on deposit would double-count the offering.

**Already in our books elsewhere** — an expense already recorded, one withdrawal
that paid several expenses, a hand-entered receipt. Closes the exception with a
link and creates nothing.

**Bank charge** — stamp duty, ledger fees, cheque-book charges nobody recorded.
Posts an expense in the bank-charge category against a chosen fund.

### Bulk, with honest skipping

Select several exceptions and apply one disposition. Items the disposition does
not fit — a debit cannot be banking, a credit cannot be a bank charge, a
"missing in bank" exception is not taken to the books at all — are skipped and
listed with the reason, never silently dropped and never fatal to the batch.

### How deposits are treated

This answers a standing question: when already-receipted cash is banked, the bank
shows a credit, but the income and the fund balance were recognised when the cash
was counted. A new "banking" entry (is_banking) reconciles that credit on the
bank side without recognising income a second time or touching any fund — so the
bank position is right and the offering is counted exactly once.
# Changelog

## v2.70.0 - Obligations engine: one payment settles what a member owes, oldest first

A member owes the scheme things in a definite order — registration first, then a
levy for each open case, oldest case first — and a payment should walk down that
list settling each in turn. Until now the allocator attached a payment as one
flat contribution and a treasurer sorted out the rest by hand. This release makes
the obligations the system's own idea.

### What a member owes, in order (item 6)

A single service, benevolent.services.obligations, answers "what does this member
owe, and in what priority?" and everything reads it: the auto-allocator, the
review queue and the member's statement. Registration and renewal fees come
first, then one levy per open case ordered oldest-event-first (the family
bereaved three months ago has waited longest), then dues arrears.

### One payment, several obligations (item 7)

A payment that covers more than one obligation is split across them — clearing
two or three cases in arrears in a single receipt. The split divides the
TRANSACTION, never the contribution, so a levy is recorded as a levy and a fee as
a fee and nothing silently pays off a member's dues. Anything above what is owed
is recorded as a voluntary contribution. A treasurer can apply a queued payment
to a member's obligations from the intake screen, ticking specific ones or taking
them oldest-first.

### Auto-allocation where it is unambiguous (item 8)

When a scheme has exactly one open case and an identified member pays the levy
amount, it is attached to that case automatically — there is only one thing it
can be. If that member has already paid their levy, the payment goes to review
instead of being posted twice.

### The guard

Auto-allocation now gates on IDENTITY confidence alone. An amount that matches
what a member owes corroborates the money's purpose but says nothing about who
paid — a hundred members owe exactly 500 — so it can no longer lift a name-only
guess over the threshold and post to the wrong person.

All four behaviours are settings, off or on per the church's constitution:
apportion to obligations, auto-allocate a single open case, review overpayments,
review multi-obligation payments.

### Bank register: distinct charges under one reference (continued)

The duplicate-reference fix now also covers a bank journal that batches several
DISTINCT charges under one reference — stamp duty, excise and a cheque-book fee
all under Core Ref CB0170485260413 / Channel REF CB0170485_13042026, with no
M-Pesa receipt. These are told apart by amount and narration; keying on the bare
reference collapsed the three into one. Verified on the real 1,377-row statement:
all three charges import, and a re-import adds nothing.

# Changelog

## v2.69.0 - The bank stamped three different payments with one reference

A statement carried three real M-Pesa payments — 10, 11 and 9 shillings — that
the bank had all stamped with the SAME Core Ref (S90288428260130) and the same
Channel REF (SFI40DCBA1EA1F6DABA9). A mobile-banking sweep had batched them under
one bank reference; the only thing telling them apart was the unique 10-char
receipt inside each narration (UATKR5A7M8, UATKR5A7N9, UATKR5AIDQ).

Deduplication keyed on that shared reference first, so it collapsed the three
into one and dropped two — money the register then denied had ever arrived.

### The unique receipt now wins

A genuine 10-character M-Pesa receipt is unique per payment; a bank channel or
core reference is not. So the narration receipt is now the identifier of record.
Where a batch shares one Core Ref across distinct receipts, the shared value is
stored bare once and suffixed ("-S1", "-S2") for the rest — the same convention
split transactions already use, so the register still reconciles to the ledger.

Fixed on all three paths that take money in: the bank register, the statement
importer, and the live M-Pesa webhook. Re-importing the same file still adds
nothing — verified on the real 1,165-row statement: the first import brings in all
three payments, a second brings in none.
# Changelog

## v2.68.0 - Every benevolent table now downloads to Excel or CSV

The register, the memberships, the contributions and the cases had no export at
all — while the rest of the app has had spreadsheet downloads for a long time. So
a welfare secretary who wanted the membership list in Excel had to retype it.

Each list page now carries ⬇ Excel and ⬇ CSV buttons. The download respects
whatever filters the page has applied — a members list filtered to ACTIVE, a
contributions list for one period — so you get exactly what is on screen, not the
whole table. The format is the same styled workbook (church header, title, frozen
bold column headers) the reports already use, because it is the same helper.

No financial figure is recomputed for the export: a contribution's amount is its
transaction's amount, a case's figures are the case's own, straight off the rows
the page shows — so an export can never disagree with the screen it came from.

Round 9, item 5 of 9.

# Changelog

## v2.67.0 - A death opens the case; the case form stops asking what it already knows

A benevolent scheme exists FOR the death of a member or their family. So the
moment a death is recorded, the case that death entitles the family to is now
already there — a draft, pre-filled with everything the scheme knows — instead of
waiting for someone to remember to type it at the worst moment of a family's year.

### Recording a death auto-opens a draft case

Filled in from what the register already holds: the event type (the one marked as
the bereavement event), the beneficiary, the relationship, and the policy's fixed
benefit as both the claimed amount and the funding target. It is ALWAYS just a
draft — never auto-submitted, never auto-paid; a treasurer still reviews and
submits it. Two new settings, because how aggressively to do this is a church's
own call: auto_open_case_on_death (off / on-record / always) and
case_beneficiary_default (derive from who died / leave blank).

### The case form was backwards; now it is dependant-first

It used to ask for the member, then the dependant, then the relationship — three
things the database already holds or can derive. Now you pick the beneficiary
(a dependant, or the member) and the member fills in from their record, the
relationship fills in from the register, and the claimed amount is pre-filled and
LOCKED whenever the policy fixes it. Nothing correct has to be retyped, so nothing
correct can be mistyped.

### Two bugs fixed along the way

Editing a dependant silently did nothing (the edit form required a field it never
showed) and, had it worked, would have UNLINKED a linked dependant from their
member record. Both fixed, and every dependant edit row now carries a member-link
typeahead — so a dependant first captured as a plain name can be upgraded to a
linked church member.

Round 9, items 1 & 2 of 9. Migration 0024.

# Changelog

## v2.66.0 - The member list stopped at 50

"All members are not viewable" on /benevolent/members/. The view paginated
correctly and handed the template a `page_obj` — but `partials/pagination.html`
only showed its Prev/Next controls when `is_paginated` was set, and that flag is
set automatically only by Django's ListView.

Eleven modules across the app build their Paginator by hand. On all 26 pages that
include this partial, the controls rendered nothing — so a congregation larger
than one page left every member past number 50 stranded, with no way to reach
page 2.

The partial now decides for itself: it shows the controls whenever `page_obj`
reports more than one page. One template, 26 pages fixed, and the two ListViews
that do set the flag are unaffected.

Round 9, item 3 of 9.

# Changelog

## v2.65.0 - The member merge repointed 1 relation of 11

`ProtectedError at /members/duplicates/merge-all/` was the visible half of a
deeper bug. `merge_members` moved a member's Transactions onto the surviving
record and nothing else — but eleven relations point at Member.

### What actually happened at merge time

- **Two PROTECT relations** (SchemeMembership, Pledge) blocked the delete — that
  was your 500.
- **Five SET_NULL relations** (Envelope, EnvelopeBatchRow, SchemeDependant,
  BenevolentApplication, Lender) did NOT block anything. On the merges that did
  go through, they were silently cut loose — a benevolent dependant, a loan, a
  giving envelope, quietly detached from the person it belonged to.
- Three CASCADE relations (alias, phone, duplicate flag) are folded by hand.

### The fix walks the relation graph

Every FK/O2O pointing at Member is now repointed automatically, so a relation
added in a future release is carried without anyone remembering to update the
merge. Where repointing would breach a per-member uniqueness rule — both people
registered in the SAME scheme — the merge refuses BEFORE writing anything, with
a reason: "Both records have a scheme membership for the same scheme … withdraw
or transfer one first." Membership in different schemes repoints cleanly.

Bulk merge now skips a conflicted pair and finishes the rest instead of the
whole run dying on one bad pair. New `merge_conflicts()` pre-flight (read-only,
safe to show on a confirmation page) and a `MemberMergeConflict` carrying
treasurer-readable reasons.

Round 9, item 4 of 9 — shipped on its own so you can deploy the merge fix now.

# Changelog

## v2.64.0 - The debit bug: one root cause, three symptoms

The bank file you sent answered all three reports at once.

### Your bank exports NO DEBIT COLUMN

    Posting Date | Value Date | Core Ref | Channel REF | Narration |
    Credit Amount | Running Balance

That is the entire header. A cheque payment appears as **Credit Amount = 0.00**,
with the **running balance dropping** by the amount paid.

The parser had a guard — "nothing moved on this row" — that threw away any row
with no credit and no debit. Every debit on your statements hit it. On one month,
that silently discarded **eight cheques worth 3,061,850**.

That single bug caused all three symptoms:

1. **Debits never imported** — discarded before anything saw them.
2. **The balance never reconciled** — of course not; the rows that made it fall
   were missing. "Which usually means a row is missing" was exactly right.
3. **Cheques never cleared** — the auto-clearing machinery has been built, tested
   and wired into the debit queue all along. The queue was permanently empty.

### The fix

The balance column is the bank's own arithmetic. Where it disagrees with a zero in
the credit column, the balance is telling the truth. A movement is now derived from
the change in the running balance where a file states no debit — and only there. A
file with a proper debit column is untouched.

**Against your real statement: all 8 debits recovered, and it reconciles to the
bank's own closing balance to the penny.**

### Cheque auto-clearing

All 8 cheques on your statement now clear automatically, each linked to the debit
that cleared it. A cheque NUMBER match is exact — the bank issues each number once
and prints it in the narration — so it needs no confirmation.

Two deliberate refusals: a number matching with the **wrong amount** is not cleared
(that is a cheque altered or partly paid, and wants your eyes), and an
**amount-only** match is never auto-applied (two cheques for the same amount are
ordinary, and guessing would clear the wrong one).

**Tests:** 13 new, written against your file's exact shape. Affected suites clean:
statements, cashbook, giving — 795 tests.

## v2.63.0 - Reported issues, round 7

Built partly from the church's own files — a working benevolent scheme, and the
WhatsApp update a treasurer produces by hand after every case. That document IS
the specification for item 6.

### The register's DEBIT side never worked

M-Pesa gives every CREDIT a receipt code — which is why the credit side worked
from day one. But the debits a church actually makes are cheques, standing orders
and bank charges, and a bank identifies those by a cheque number in the narration,
or by nothing at all. So every debit fell through the "no reference, cannot say"
branch and was never checked.

The credits are gifts arriving, which are pleasant to get wrong. The debits are
money LEAVING, which is not. Debits are now matched by cheque number against the
payments register, and an unmatched debit is ALWAYS flagged — money leaving the
account with no record behind it is the single most important thing this check
exists to find.

### Reversals

A bank credits the church by mistake and takes it back. Nothing was really
received — but the importer was posting it as INCOME, so a church's books showed
a gift it never received. Transaction has carried the reversal machinery all
along, unused. Both halves are now skipped on import; the register keeps both
lines, because its whole contract is to say what the bank said.

A narration keyword is required to pair. A 5,000 gift on Monday and a 5,000
supplier payment on Tuesday are two real movements, and erasing both because they
cancel out would be far worse than missing a reversal.

### The case statement — for WhatsApp

Who contributed, who did not, who newly registered, and the money. Built to the
document the church already produces by hand. The bereaved member is never on the
defaulters list: publishing their name as somebody who failed to contribute to
their own bereavement would be grotesque. Also on Telegram (`/case`).

### The registry on Telegram

`/member NAME` — standing, arrears, dependants. The most common question anybody
asks a treasurer at church, and until now it needed a laptop. Plus `/benevolent`
and `/arrears`.

### The budget PNGs on a phone

The fonts were not small. The IMAGE was 1180px wide — a desktop table — and a
phone scales that to a third, taking every font with it. Now sized for a phone:
the same text lands at ~9.5pt instead of ~4.5pt. The progress bar is gone — a bar
is a picture of a number, and a picture of a number does not survive being scaled
to a third of its size.

### Also

The founding balance was still editable on the department form (the budget page
was locked; this was the other way in). `docs/FIRST_TIME_SETUP.md` is the setup
guide. And the beneficiary's RELATIONSHIP — "Father to Grace Nyaboke" — is now
captured; it is the line that tells a congregation whose loss this is.

**Tests:** 44 new. Full regression clean — 2,600+ tests.

## v2.62.0 - Reported issues, round 6

### The matching bug, at the root this time

"I can get the references under M-Pesa ref in the transactions. Yet being
detected as missing... affects transactions indicated as manual receipt, and the
amount may be zero. Check how split funds are matched."

Every clue was the same root cause, and it was mine. I was filtering the match
index by **channel**, **bank account** and **reversal status** — all of them
classifications WE make after the fact, any of which can hide a transaction that
plainly carries the bank's own reference. Marking a gift as a manual receipt
detaches it from its fund. A split part can be zero-valued, and importer-created
parts have no split link at all. An account tag may be missing or wrong.

For "did we ever record this bank line?", the only thing that can answer it is
whether the bank's reference is in our ledger. Nothing else. The account and
channel filters belong on the *other* direction — "which of our own bank entries
has the bank never mentioned?" — and now live only there.

Three rounds on one function, because I kept fixing the symptom in front of me
rather than asking what the question actually needs to know.

### The register's opening balance

A register starting mid-year summed forward from zero, so its closing balance was
out by whatever the account already held. It now derives the opening from the
bank's own balance column — the bank has already told us, and its figure beats
anything typed. It only asks where a statement carries no balance column at all.

### Pending receipt excludes cash — and reaches Telegram

Cash is receipted at the point of counting; it goes onto an envelope at the
table. Listing it asked a treasurer to chase a receipt for money that was never
going to have one. `/pending` on Telegram now returns the same PDF the web page
serves, from the same query.

### Petty cash, cheques, and the payee

A cheque cashed for petty cash is **two movements**: money leaves the bank, money
arrives in the tin. Record only one and the books stop adding up. Write it in the
payments register with source "Petty cash replenishment" and the float rises when
it is issued — and falls again if it is cancelled, because a cheque never cashed
never became notes in the tin.

The **payee** is now captured separately from the claimant: the member who
requested a purchase and the supplier the cheque is written to are often
different people.

The separate **"record a disbursement" form is retired**. It wrote exactly the
Expense the expense form writes with "paid from the petty cash float" ticked —
but could not attach a receipt, set an expenditure type or a budget line, and had
its own approval shortcut. One form, one approval trail, one place a voucher can
be found.

### Printing onto a real cheque

The old print was a facsimile on plain paper (still there — it is a useful
advice). Printing onto an actual leaf means ink at exact millimetre positions on
paper that already carries its own borders and labels.

**Cheque leaves differ between banks, and a spoiled numbered leaf is not free.**
So nothing is guessed: `?mode=leaf` prints only the values, the layout is
configurable, and `?mode=calibrate` prints a millimetre grid with a cross where
each field will land — onto one *spoiled* leaf, so the offsets can be measured
and corrected. Once.

**Tests:** 33 new. Full regression clean.

## v2.61.0 - Reported issues, round 5

### The register was crying wolf (the serious one)

"Many entries being detected as not in our books, but when searching I found
them." Two bugs, both mine:

- **The date window was used for MATCHING, not just reporting.** A bank
  reference is unique FOREVER — if any transaction carries it, the line IS in
  our books, whatever date it was recorded under. But the match index was built
  only from transactions inside the reporting window, so a payment the bank
  value-dated 1 July that the treasurer entered on 30 June (when the SMS
  arrived) fell outside it and was flagged as missing. Value date and entry date
  differing by a day or two is completely ordinary; a reconciliation that cannot
  survive that is worse than none, because every false positive teaches a
  treasurer to stop reading the report.
- **Transactions were not scoped to the account being checked**, so with two
  bank accounts every transaction of the second was flagged as missing from the
  first.

### Constraints that were silently absent in production

MariaDB does not create conditional unique constraints — it declines quietly
(W036) — so the register's duplicate guards were **not enforced at all** on the
production database. The conditions were never needed (NULLs are distinct in a
unique index on every supported backend) and are gone. The constraints now
actually exist where it matters.

### Pending receipt: renamed, and it now includes LCB

It was Trust-only, so LCB money a church receipts exactly as it receipts trust
money never appeared — which is why it was called "Trust pending receipt", a name
that described the bug rather than the intent. Worse, the receiptable check
matched LCB **by name**, ignoring the LCB funds configured in Settings entirely.
There is now one canonical definition, honouring the configured funds and their
subgroups, shared with the Sabbath-confirm scope. Old export URLs still work.

### Allocation & categories moved out; a duplicate retired

Now its own page, next to the rules and patterns it belongs with, reachable from
`/rules/`. The patterns page is out of the sidebar.

You asked whether the dev-group prefix setting duplicated the patterns page. **It
did** — it built precisely the regex a NUMBERED pattern builds, but could not be
labelled, ordered, disabled or audited. Retired, with existing values migrated
into real patterns rather than discarded.

### Also

The register downloads (CSV/Excel, with opening and closing balances). The
case-roster contribution import verified end to end — paid and unpaid — where
"did not contribute" is recorded by the ABSENCE of a payment, because writing a
zero-value contribution would put a receipt in the ledger for money nobody gave.
And a latent flaky test fixed: it captured TODAY at import and asserted against a
window ending "today", so a suite crossing midnight failed.

**Tests:** 26 new. Full regression clean — 2,500+ tests.

## v2.61.0 - Reported issues, round 5

### The serious one: the register's matching was crying wolf

*"Many entries being detected as not in our books, but when searching I found
them."* Two bugs, both mine:

- **The date window was used for MATCHING, not just reporting.** A bank
  reference is unique FOREVER — if any transaction carries it, the line IS in
  our books, whatever date it was recorded under. But the match index was built
  only from transactions inside the reporting window, so a payment the bank
  value-dated 1 July that the treasurer entered on 30 June (when the SMS
  arrived) fell outside it, and its statement line was flagged as missing.
- **Transactions were not scoped to the account being checked**, so a church
  with two accounts had every transaction of the second flagged as missing from
  the first.

A reconciliation that cries wolf is worse than none — every false positive
teaches a treasurer to stop reading it.

### The MariaDB warning was a real production hole

MariaDB does not create conditional unique constraints — it silently declines.
So on the production database they were **not enforced at all**, and duplicate
exceptions could be written. The conditions were never needed: NULLs are
distinct in a unique index on every backend, so an unconditional constraint says
exactly the same thing and actually exists.

### "Trust pending receipt" was named after its own bug

The list was Trust-only, so LCB money a church receipts exactly as it receipts
trust money never appeared. Renamed to **Pending receipt**, now covering Trust
**plus the LCB family** — the funds configured in Settings, **plus their
subgroups**. And `_is_receiptable_fund()` (which drives the Sabbath-confirm
scope) matched LCB **by name only**, so a church that had configured its LCB
funds found that setting silently ignored. One canonical definition now, shared
by both. Old export URLs still work.

### Allocation & categories moved — and a duplicate retired

Its own page, linked from the allocation rules, where it belongs. Dev-patterns
also linked from there and removed from the sidebar.

And yes, the duplicate was real: the "extra dev-group prefixes" setting built
exactly the regex a DevGroupPattern of kind NUMBERED builds, but with no label,
ordering, on/off switch or audit trail. Retired — with a migration that turns
anything a church had configured into real, visible patterns rather than
silently discarding it.

### Also

The register downloads to CSV and Excel, with opening and closing balances
included. Contribution import already handled a full case roster, paid and
unpaid — confirmed with tests rather than assumed. A latent date-boundary flake
was found and fixed (a test captured TODAY at import, so a suite crossing
midnight produced a None total and an AttributeError).

**Tests:** 26 new. Full regression clean — 2,547 tests.

## v2.60.0 - Bank Statement Register, and the public benevolent application form

Both are deliberately SEPARATE LAYERS. Neither can affect the ledger.

### The Bank Statement Register

A running record of what the BANK says happened — every line it ever sent, kept
forever, unjudged. It never posts, allocates, creates a transaction, or touches
a fund balance. That is the entire point: a register a treasurer could quietly
"correct" would be worthless as a check on their own books.

Not a reuse of the existing statement importer, because that one's job is the
opposite — it turns bank rows INTO transactions. The register asserts nothing,
and is therefore safe to re-import over any period, as often as you like.
Importing from January every month is sensible here; every line is deduplicated
on the bank's own reference.

**Exceptions** answer the two questions directly: money on the statement that is
not in our books, and bank movements in our books the bank never mentioned.
Matching is by BANK REFERENCE only (M-Pesa receipt / core banking ref).
Amount-and-date matching is deliberately not attempted — two members giving the
same amount on the same day is ordinary, and guessing there would manufacture
exactly the false reconciliation this exists to prevent.

Two corrections made during the build, both worth stating:

- **The check is bounded to the period the register covers.** The first version
  compared the whole ledger against whatever was imported — so with only July
  loaded, all of June was flagged as "missing from the bank." But the register
  has no June data; that is an absence of evidence, not a discrepancy, and
  reporting it as one buried the real exceptions under hundreds of false ones.
- **A bank transaction with no bank reference is "unverifiable", not an
  exception.** We cannot say the bank disagrees — only that we have no way to
  ask. Calling that a discrepancy would be an accusation the evidence does not
  support.

### A real bug this uncovered in the SHARED parser

`dayfirst=True` was scrambling ISO dates — **1 July was being read as 7
January**. Any bank exporting ISO dates was having its statement silently
misdated by up to eleven months, in the LEDGER importer as much as anywhere.
Fixed; the full statements and giving suites pass unchanged.

### The public benevolent application form

Off by default. An application is NOT a membership: nobody is covered, owes
dues, or can claim until a registration officer approves them — at which point
they are registered through exactly the same service as anyone enrolled at the
desk.

Write-only by design: it never reads or exposes member data (no autocomplete, no
lookup, no roll — a public form that could search the membership would leak it).
Honeypot, minimum fill time, per-session throttle. The applicant says whether
they are a registered member, a Sabbath School member, or a visitor — recorded
as their CLAIM, unverified; checking it is what the review is for.

Dependants are captured in the three sections a family is actually described in
— **spouse, children, parents** — rather than one undifferentiated list that
makes an applicant guess where their mother goes. A dependant's own phone is
asked for, because a spouse or grown child very often pays from their own line.

**Tests:** 36 new. Full regression clean — 2,436 tests.

## v2.59.0 - Reported issues, round 3

### The member-search widget had never worked

Not "worked badly" — it had **never displayed a single suggestion to anybody,
in any form, since the day it shipped**. `query()` resolved to the endpoint's
JSON envelope `{results: [...]}` and handed that whole object to
`renderResults()`, which tested `results.length` — `undefined` on an object —
and hid the box and returned. The endpoint was fine, the CSS was fine, the
request was even being made and answered. The answer was thrown away one line
before it could be rendered.

Nothing in the Django suite could see it — the failure lived entirely in the
browser. It is now guarded by a jsdom test, which was first run against the
pre-fix code to confirm it actually catches the bug. The file is also renamed
`benevolent-search.js` → `member-search.js`: it was never
benevolent-specific, and it now serves the pledge form and membership page too.

### Alternate phone numbers were invisible to every search screen

`MemberPhone` has always recorded a member's other numbers, and the
bank-statement matcher has always searched them. The search *screens* did not —
so a treasurer typing the very number in the narration in front of them was
told the member did not exist, and pushed into creating a duplicate.

### A levy recorded on the general form belonged to no case

`record_contribution()` correctly infers `kind = LEVY` from the presence of a
case — but the general contribution form could not name a case at all. So a
levy entered there was filed as VOLUNTARY, attached to nothing: the member
stayed "unpaid" on the case's levy roster, and under a POOLED policy — where
the benefit IS whatever the levy collected — the payout came out short. The
form now offers the case. Draft cases are deliberately included: a church
starts the harambee the moment a death is known.

### Founding balances could silently rewrite the entire history

`opening_balance` is the FOUNDING figure, not a yearly one — every later
year's opening is derived from it, and year-end close never writes it. But the
budget page let a treasurer edit it while calling it "opening balance for
<year>". Changing it in July did not set July's opening; it rewrote every fund
balance in every year, backwards. The page now shows each fund's DERIVED
opening for the year, labels the founding figure honestly, and freezes it once
any year has been closed — enforced server-side, not just hidden.

### Also

Registering someone already covered as another member's spouse is now refused
(one person, two memberships in one scheme = counted twice, levied twice, able
to claim twice). Pledge form gets the typeahead. Campaign detail gets search,
filters, newest-first ordering and inline edit/delete so a wrongly-allocated
import row is fixable where you'd look for it. Transfers page gets filters and
a current-month default. Member page gets a date filter — defaulting to the
current YEAR, not month, since unlike the unbounded list pages it is already
scoped to one person; its lifetime total never moves with the filter.

**Tests:** 23 new Django + 16 new jsdom. Full regression clean.

**Not done, and honestly logged** (`docs/recommendations.md` #75a/#75b): the
running bank statement with discrepancy checking, and the public benevolent
registration form. Both are substantial features that deserve a proper design
pass — the first around how a "discrepancy" is defined when the ledger and the
bank disagree, the second around the dependants question Edwin himself flagged
mid-sentence — rather than a rushed version tacked onto a bug-fix round.

## v2.58.0 - Full-module audit of the Benevolent module

A systematic sweep of every model field, view, permission, report, export
format, the wizard, the notification wiring, accounting integrity, and query
counts under load.

### Four real issues found and fixed

**Six enforced policy rules were unreachable from the UI.** `arrears_block`,
`grace_period_days`, `exemption_age`, `max_household_size`,
`allow_exemptions` and `allow_transfers` are all genuinely enforced — each
one verified to really block a transfer, refuse an exemption, cap a
household, produce GRACE standing, make an owing member ineligible, or
exempt an older member. But none appeared on the policy form, because
`grouped()` silently skipped any field absent from its GROUPS list. A
treasurer could not configure rules the system was enforcing against their
members. The mechanism is fixed as well as the instance: a stray field now
lands in a visible "Other settings" group instead of vanishing.

**A duplicate, inferior registration path.** Phase 1's enrolment view still
rendered its own form — no households, no dependants, no off-roll
registration — reachable by URL though nothing had linked to it since Phase
3. It now redirects to the real registration screen; the duplicate form is
deleted.

**The remaining N+1 (recommendation #70b) is closed.** `arrears_for()` cost
~22 queries per member, and it runs for every active member on the
dashboard, the arrears report and every standing recomputation. Now 6, flat.
Dashboard query growth fell 68%. Every number unchanged — the full
pre-existing suite passed untouched, which is exactly what should happen
when only the cost of an answer changes.

**Two dead functions, one lying about itself.** `periods_between()` claimed
in its docstring to be "the single definition of which periods have fallen
due" — a claim Phase 10's rewrite had quietly made false, since nothing
called it any more. A dead function asserting it is the source of truth for
a rule that has moved is how a future fix gets made in the wrong place.
Removed, along with a compatibility shim that had nothing to be compatible
with.

### Confirmed sound by being exercised, not re-read

Accounting integrity (ledger balances, metrics agree, no orphans);
historical accuracy (a decided case is unmoved by a later policy; arrears
across a mid-history dues change charge the right rate for each period);
edge cases (zero benefits, over-payouts, double approval, future events,
negative contributions, duplicate enrolments all correctly refused);
permissions (22 pages x 10 roles, no 500s, all correct); reports (10
reports x 5 formats = 50 combinations, all working); notifications (every
event maps to a real toggle; every placeholder is provided); the wizard;
and every template URL name.

**Tests:** 18 new. Full regression clean across the whole application —
2,316 tests.

## v2.57.0 - Production fixes and requested features, round 2

### Two "still broken" reports, fixed at their true root cause

- **Manual receipt still sweeping in unrelated entries.** The previous fix
  (a true fallback chain) was correct but not sufficient — even the
  strongest text-based match is still an inference. `Transaction` now has a
  real `split_of` foreign key, set by `split_into()` on every part it
  creates; `split_siblings()` checks this FIRST, falling back to text
  inference only for historical rows, which a migration backfills
  automatically from the `[Split of #N]` tag already written into every
  split's narration.
- **The envelope ledger popup still appearing outside the grid.** A
  different cause from the earlier CSS fix: `.content` (the page's main
  wrapper) runs a `transform`-animating entrance animation, which per the
  CSS spec traps any `position:fixed` descendant's coordinates against its
  own box instead of the true viewport. Fixed by moving the popup to
  `document.body` the first time it's shown, with cleanup on row removal
  and table rebuild.

### Confirmed already built, made discoverable

- **Segregation of duties on case approval** already worked correctly by
  default; added `require_different_approver` so a very small scheme can
  actually configure it, rather than it being a fixed rule.
- **Case-count-based inactivity** (`missed_case_levies()`) was fully built
  and correctly wired into the standing engine, exactly matching the
  scenario described — a levy scheme with no monthly dues, where time alone
  says nothing without recent cases. It was simply never exposed on the
  policy form.
- **Cash payment recording** was already fully supported.

### New

- The register form no longer requires an existing church-roll member — a
  benevolent scheme is its own thing. Also fixed in the same pass: a hidden
  `<select>` silently skipping native browser validation, a real cause of
  "the button doesn't do anything."
- Marking a dependant as deceased, distinct from generic removal, with a
  clear next-step prompt toward raising a case.
- Bulk import extended to contribution history, with both import screens
  now properly linked from the scheme's own page.
- A year selector on the (already paginated) contributions list.
- A member directory report — every member with their dependants, one
  place, filterable to active or inactive only.

**Tests:** 56 new. Full regression clean across the whole application —
2,282 tests total.

## v2.56.0 - Phase 11 (Guided Setup & Allocation Transparency) plus a wide production-fix pass

### Phase 11, as proposed

A plain-language guide on the policy profile library connecting the three
funding patterns Edwin described to the profiles that already implement
them; an explicit, logged "fund this case from the balance" decision for
levy-funded schemes (record_payout() never actually required a levy — this
makes skipping one a stated choice); and a "Matched via" column on the
contribution intake queue, surfacing allocator signal data that was already
being frozen onto each row but never displayed.

### Four real, confirmed bugs — found and fixed

- **The Admit button** on a pending membership's page produced "Unknown
  action" — wired to the wrong view.
- **`split_siblings()` (giving app)** could sweep unrelated people's
  payments into a manual-receipt mark when they merely shared a generic
  narration and date — an OR of three match conditions instead of a true
  fallback through them.
- **The autocomplete popup's CSS** existed only as drifting, copy-pasted
  fragments across three templates — consolidated into one definition, now
  also used for new member-search widgets on benevolent's forms.
- **A severe font-loading bug in the budget PNG export** — a missing system
  font package silently fell back to a fixed-size bitmap font incompatible
  with the file's 4x print-quality render scale, making the figures
  effectively invisible. Fixed to fall back to reportlab's own bundled
  fonts, present in every environment that can run this application.

### Two unbounded-by-default list views, fixed — plus a third, worse one found

`/transactions/` and `/expenses/` loaded every row ever recorded on a bare
visit; both now default to the current month, while any other filter with
no date bound still searches all time exactly as before (proven against
the existing test suite, which relies on that). A third, more severe
instance was found while checking "other pages": the envelope list scanned
every envelope ever recorded on every visit, discarding almost all of it in
Python — fixed to filter at the database level.

### Two items audited and proven already correct

Member phone-matching (`MemberPhone`, `merge_members()`,
`match_or_create_member()`) already recognises a contribution from any of
a member's known phones, including in the "not contributed to campaign" SMS
criterion — confirmed with new regression tests rather than left as a
re-read of the code.

### A new standalone seed command

`seed_benevolent_demo` — benevolent test data without running the full demo
first, reusing the existing seed chain rather than duplicating it.

**Tests:** 12 (Phase 11) + 24 (bugfixes) + 6 (phone audit) + 3 (envelope
list) + 5 (transaction date default) + 4 (expense date default) + 2 (PNG
font fallback) + 8 (seed command) = 64 new. Full regression clean across
benevolent (444 tests), giving, cashbook, envelopes, members, core, and
accounts.

## v2.55.0 - Production fixes and requested features

### Four real bugs, found and fixed

- **The "Admit" button produced "Unknown action."** It posted to the wrong
  view — admit has always lived on `MembershipAdminView`, not
  `MembershipLifecycleView`. Fixed the wiring; the shared reason/date fields
  the button sits alongside are now actually honoured by that action too.
- **Marking one bank transaction as a manual receipt could change several
  unrelated ones.** `split_siblings()` OR'd three match conditions instead
  of falling back through them, so a transaction with a solid, unique bank
  reference was still ALSO matched against anyone else's payment sharing its
  plain narration and date. Fixed to a true fallback: the strongest
  identifier available, and only that one.
- **The type-ahead name popup rendered incorrectly.** Its CSS was
  copy-pasted into three templates with drifting details; the shared
  stylesheet held only an orphaned fragment. Consolidated into one
  definition.
- **A dependant's own phone — documented since Phase 4 for allocation
  matching — was never actually settable.** Added to both the household-add
  form and its service function.

### Three requested features

- **Bulk roster import** for a church bringing an existing scheme into the
  system — every row an ordinary registration, dependants included, with
  "mark paid up" clearing arrears through a visible, auto-approved waiver
  record rather than a fabricated payment history, and no welcome
  notification for someone who has belonged for years.
- **Member/membership search** on the register, contribution, and case
  forms, replacing plain dropdowns, correctly scoped for Phase 9's
  scheme-specific roles.
- **A standing snapshot on a case's own page** for dues-funded schemes —
  the equivalent of the levy roster levy-funded schemes have always had for
  "who has and hasn't paid."

**Tests:** 27 new, all green. Full regression clean across the whole
application: benevolent (397), giving + members (288), accounts (127),
core (419).

## v2.55.0 - Bug fixes and features requested directly (post-Phase-10)

### Two real, production bugs

The **"Admit" button** on a pending membership's page produced "Unknown
action" — wired to a view with no admit handler; the correct one was
reachable only via a different URL. Fixed, and the shared reason/date
fields it sits alongside are now actually honoured rather than silently
ignored.

**Marking one bank transaction as a manual receipt could change six** —
`split_siblings()` OR'd three match conditions together instead of falling
back through them, so a transaction with a solid, unique bank reference was
*also* matched against anyone else's payment sharing its plain-text
narration and date. A sibling function had already correctly diagnosed and
avoided this exact risk in its own docstring; the reasoning had never been
applied here. Fixed to a true strongest-identifier-first fallback, with
tests proving both the fix and that it doesn't break the real split-payment
case it exists to serve.

### A site-wide styling bug

The type-ahead popup's CSS (`.ac-box`/`.ac-item`) was never actually defined
in the shared stylesheet — three existing screens each carried their own
copy-pasted, drifting local version. Consolidated into one definition.

### Two new features

**Member/membership search** on benevolent's register, contribution and case
forms — previously a plain, unusably long `<select>` for any church of
size. Its own endpoint, gated correctly for Phase 9's scheme-specific roles
(the general search is Treasurer/Assistant only). One shared, reusable JS
widget upgrades the existing dropdown in place.

**Bulk roster import** — bring an already-running scheme's existing
membership, dependants included, into the system at once via CSV, with
"mark paid up" clearing arrears through a visible, auto-approved,
explicitly-reasoned waiver rather than a fabricated payment history. A
related gap closed along the way: a dependant's phone (documented since
Phase 4 as existing for allocation matching) had no way to actually be set,
anywhere.

### Confirmed already built, not re-built

The per-case "who contributed, who did not" roster (Phase 4's per-case levy
screen) and the "members not in good standing" filter (the membership
registry's standing filter, live since Phase 3) already answer exactly what
was asked — pointed to directly rather than duplicated.

**Tests:** 26 new. Full regression clean: benevolent (423 across all ten
phases), giving (237), core + accounts (546), envelopes + cashbook (559).

## v2.54.0 - Benevolent Phase 10: Production Readiness & Final Review (module complete)

### A severe, previously-invisible performance bug — found and fixed

`_dues_rows()` — underneath `arrears_for()`, called by the eligibility
engine, the standing engine, every Phase 8 report, the Phase 7 reminder job
and the dashboard, for every active member — resolved "which policy was in
force" once per **day** of a member's history instead of once per call. A
member of a few years' standing cost 700-1000+ database queries to answer
"what do they owe." Found by writing the test this phase exists to write: a
measured query-count comparison between a small dataset and a larger one,
which showed a real dashboard's queries growing from 716 to over 5,000 for
seventeen extra members. Fixed by resolving a scheme's policy history once
per call instead of once per day — same rule, same answer, a fraction of the
cost. The full pre-existing suite — 1,331 tests — passed unchanged after the
fix, and several phases' worth of arrears-heavy tests ran roughly twice as
fast as a direct, measured result.

### The Scheme Engine claim, proved rather than asserted

`BenevolentScheme.Kind` (Medical/Education/Emergency alongside Benevolent),
the Constitution Wizard's very first question, and a working "Medical
assistance" policy profile with its own event types have all existed since
Phases 1-2 — genuine Scheme Engine infrastructure, not a rebrand. Nothing had
ever proved it by actually running a non-bereavement scheme end to end. This
phase does: a Medical Fund built from the existing profile runs a full
lifecycle — case raised with no prior membership, assessed against a
percentage-of-cost cap, committee-approved, paid, and reported through the
same Phase 8 engine every bereavement scheme uses — using zero new model
fields, zero new service functions, zero new views. A newly-added "Emergency
relief" built-in profile (fast, treasurer-only approval; no membership or
waiting period) proves it a second way.

### Wording that assumed bereavement, fixed

The wizard's "does the member a case is about contribute to their own case"
section was titled "The bereaved member" — correct for the common case,
mildly wrong for a hospital bill or a school fees claim. Retitled to "The
member a case is about" across the wizard, the policy form, and the case
screen — presentation only; field names were deliberately left alone.

### A dead setting, finally traced and fixed

`notify_committee_on_pending_vote`, flagged as confirmed-dead in Phase 9's
review, turned out to have a precise cause: its name didn't match the
`notify_on_<event>` convention the notifier's lookup depends on, so it could
never have fired for any deployment, ever. Renamed to
`notify_on_committee_pending` and correctly wired — a staff-facing
counterpart to Phase 7's member/committee-facing notice, not a duplicate.

### The module is complete

Ten phases, each audited before building, reused before duplicating, and —
where a real bug turned up, as one did in nearly every phase — named, fixed,
and tested. `docs/recommendations.md` remains the honest, permanent record
of what was deliberately left for later and why.

**Tests:** 14 new (397 across all ten phases, all green). Full regression
clean across the whole application: reports (388), core + accounts (546).

**Deferred, named** (`docs/recommendations.md` #70b): a smaller, related
inefficiency in the same function (`contributions_total()` called once per
dues period rather than once per membership) — real, but a different scale
of problem, and one that touches the arrears engine's core "how much has
been paid" calculation closely enough to deserve its own careful pass rather
than a rushed one at the end of a tenth phase.

## v2.53.0 - Benevolent Phase 9: Roles, Permissions & User Experience

### No separate permission system — extended the existing one

`core.rights` already had exactly the mechanism the brief asks for: named
rights bundled into assignable profiles, layered on the existing role groups.
This phase adds to it rather than building a second one alongside it.

### The real gap: one coarse right doing three jobs

`manage_benevolent` covered "enrol members, raise cases, record
contributions" as a single right. Split into three —
`benevolent_register_members`, `benevolent_manage_cases`,
`benevolent_manage_finance` — and eighteen views across four files re-pointed
to the specific mixin their work actually needs.

**Nothing that worked before stops working.** Every new check is the OLD
coarse check first, the new specific one second — a Treasurer, an Assistant,
or any existing profile keeps every capability it already had. Proven
directly: a test builds a pre-Phase-9-style profile and confirms it still
satisfies all three new checks.

### Seven seeded profiles, matching the brief's named roles

Administrator, Approver ("Treasurer"), Committee Member, Registration
Officer, Case Officer, Finance Officer, Auditor — seeded the exact way every
other default profile in this system already is. "Committee Chairperson" is
deliberately not an eighth profile: chairing is a SEAT on a scheme's
committee roster (Phase 6), not a different right — a new
`is_benevolent_committee_chair()` helper answers the one question the UI
needs without duplicating the roster as a second concept.

### A real bug found while working on the settings page

The settings template referenced three fields Phase 7 had retired (that
whole section silently rendered *empty*) and was missing roughly half the
model's actual fields — every Phase 7 notification toggle bar one, and every
Phase 4 allocation-tuning field — unreachable from the web UI since Phase 4.
Fixed comprehensively, with a new "Allocation" tab, and regression-guarded
with a test that walks every actual model field and confirms it appears on
the page.

### Dashboard: role-aware "your queues"

Each viewer now sees only the operational queues their own role covers —
computed only when they hold the matching right, so a Registration Officer's
page load never pays for a committee-vote query it will not show. A
members'-arrears KPI card was added alongside the existing balance/
contributions/payouts cards, and a Reports link now connects the module's own
nav to the nine reports Phase 8 built.

### Confirmed already solid

Search, progressive disclosure on the policy form (already collapsible,
Phase 2), and accessibility basics (`aria-required` applied centrally to
every form in the system) were audited and found already correct.

**Tests:** 32 new (377 across all nine phases, all green). Full regression
clean: accounts (127), core (419).

**Deferred, named** (`docs/recommendations.md` #69): payout-raising sits
under the Case Officer right rather than a separate Finance gate (a
defensible boundary, changeable if asked for); no inline explanation when a
control is hidden by permission (consistent with the rest of the app);
`notify_committee_on_pending_vote` confirmed as a fourth instance of this
project's recurring "declared but never wired" shape, found while fixing the
settings page rather than by deliberately auditing for it.

## v2.52.0 - Benevolent Phase 8: Reporting, Analytics & Dashboards

### The Report Engine already existed — this plugs into it

`core.reporting` is a mature, config-driven engine already running the Board
Pack, the Income & Expenditure Statement, and every other report in the
system: a component registry, a report registry, and CSV/XLSX/PDF/DOCX export
that comes free the moment a report is registered — no per-format code. This
phase was never about building a second reporting system for this module; it
was about plugging into the one that already runs everything else.

**Thirteen report components** — one per category the brief names
(operational dashboard, KPIs, contribution summary, membership, households,
committee, cases, fund balances, income & expenditure, arrears, benefit
payments, audit) — composed into **nine ready-to-use reports**, registered
from `BenevolentConfig.ready()` the same way `benevolent/metrics.py` already
registers this module's figures with the Financial Metrics Registry.

### No new financial calculation

Every money figure is read straight from the registry (`benevolent_scheme_
summary`, `_contributions`, `_payouts`, `_fund_balance`, `_commitments` — all
from Phase 1) or is a **breakdown** of one. Arrears analysis is not a second
way to compute what a member owes — it is `arrears_for()`, the same function
every membership screen already calls, listed member by member, with its own
sum registered as the new `benevolent_arrears` metric, so the KPI card and the
arrears report can never disagree. The audit report reuses the exact query
Phase 6's Overrides & Exceptions screen already built, rather than re-deriving
what counts as an override a second time.

### Historical accuracy, proven not assumed

The case report reads what was actually approved and paid — frozen at
decision time — never a live re-evaluation. A dedicated test changes a
scheme's benefit amount *after* a case was paid and confirms the report still
shows the original, historical figure.

### A real bug caught before it shipped

Early smoke-testing crashed XLSX export: a raw `User` object had been placed
directly into a report cell instead of its string form — openpyxl cannot
serialise a model instance. Fixed everywhere it occurred, and closed
generically with a test that renders every component and asserts every cell
holds an exportable primitive, so this specific mistake — or its equivalent in
a future component — can never silently reach production again.

**Tests:** 25 new (345 across the benevolent module, all eight phases, all
green). Full regression clean: reports (388), core (419).

**Deferred, named** (`docs/recommendations.md` #68): no live scheme-picker
dropdown (the shared engine's `Filter`/template do not yet render one, and
building it would mean querying the database at process startup — flagged as
a `core.reporting` enhancement, not a benevolent-specific workaround); the
membership/case/payment views cap at 2,000 rows on screen (CSV/XLSX export
carry no cap); no benevolent-specific AI narrative component (the wider
system's intelligence layer is general-purpose, and a scoped narrative is a
natural Designer composition rather than a fixed part of this phase).

## v2.51.0 - Benevolent Phase 7: Financial Integration & Communications

### Financial integration: confirmed, not rebuilt

Expense Voucher, Payment Register, General Ledger, Bank Reconciliation, Chart of
Accounts, Audit Log — every one of these was already correctly integrated since
Phase 1, reinforced by Phase 4's statement-import work. Re-verified with a full
contribution -> case -> payout cycle re-run under this phase's new code and checked
against the ledger's own accounting-equation check: still balanced. Nothing here
needed rebuilding, so nothing was.

### The real gap: nothing ever reached a member

Every notification before this phase went to STAFF. Worse than simply missing:
`registry.py` had a `_notify()` function whose own docstring said *"Tell the member,
where the settings say to"* — and then messaged treasurers regardless, gated by a
settings field nothing ever set. A confirmed bug — intent and implementation had
quietly diverged, for at least four phases.

Fixed by building the pathway that was actually missing: `services/notify.py` renders
a configurable template and delivers it to the member's own phone or email (or, for
committee notices, the seated member's own address), through the *existing* SMS and
email engines — never a parallel channel. The broken `registry._notify()` is gone.

### Configurable templates, notification history, delivery tracking, retries

`NotificationTemplate` — one editable row per event and channel, nine events named
directly from the brief (registrations, renewals, contribution reminders, case
notifications, committee approvals, benefit payments, membership status changes),
each on SMS and email, using the exact `{placeholder}` syntax already established
elsewhere in the system. `BenevolentNotification` is the permanent delivery record —
event, recipient, the rendered message frozen at send time, status, attempt count,
and a direct link to the SMS engine's own delivery log rather than a duplicate.
Retries are bounded and ride the existing nightly automation schedule; no second job
was added.

### Contribution reminders: closing a gap that survived three phases

`arrears_reminder_days` and `renewal_reminder_days` have existed since Phase 2,
editable, frozen into every policy snapshot, and acted on by nothing —
recommendation #62c named this explicitly, three phases running, and raised it to
HIGH priority. `send_due_reminders()` closes it: every member currently in arrears,
or due for renewal within the configured window, gets a reminder, throttled to at
most one every `reminder_min_gap_days` so a nightly job does not become a nightly
text message.

**Tests:** 42 new (320 across the benevolent module, all seven phases, all green).
Full regression clean: cashbook (396), core + members (470).

**Deferred, named** (`docs/recommendations.md` #67): `members.Member` still has no
email field — member email here is scoped to `SchemeMembership.email` specifically,
deliberately kept inside this module's boundary; no WhatsApp channel (SMS and email
cover everything the brief named); notification history has no export yet (a Report
Engine candidate, like the Overrides & Exceptions screen before it); and a payout
notification can under-fire on a multi-voucher case, sharing a pre-existing gap in
the CaseEvent logic it rides on rather than a new one.

## v2.50.0 - Benevolent Phase 6: Policy Evaluation & Committee Management

### This phase was mostly an audit, and that's the point

Almost everything the objective named — a policy evaluation engine, committee
structures, approval workflows, quorum, overrides, waivers, exemptions, renewals,
transfers, household inheritance, death processing, inactivity, reinstatements — was
already built across Phases 1-5. Rather than rebuild any of it, this phase went
looking for the specific places where the existing machinery fell short of what it
claimed to do, and fixed exactly those.

### A real gap: the committee had no roster

`can_vote_benevolent` already existed as its own right, correctly separate from the
treasurer role. What it could not do: say *whose* committee someone is on, in a church
running more than one scheme, or give a seat a name. **`CommitteeMember`** (per scheme,
with a role — Chair, Vice-chair, Secretary, Committee treasurer, Member) fills that in,
additively: a scheme that never seats anyone behaves exactly as before, and
`record_vote()` only starts enforcing roster membership once a roster actually exists.

### Approval levels: `committee_requires_chair`

A quorum is a headcount; an approval *level* is "and one of them must be this seat" —
which the old model genuinely could not express. When set, a quorum of ordinary members
does not carry a decision until the Chair specifically has voted, with a Chair-specific
error message rather than the generic "quorum not met." Ignored, not an error, on a
scheme with no Chair seated.

### A real bug: the reinstatement fee was never charged

`SchemePolicy.reinstatement_fee` has existed since Phase 2, editable on every policy
form, frozen into every snapshot — and read nowhere. Fixed by reusing Phase 4's
obligations ledger through a new auto-approved charge, mirroring the reasoning Phase 5
established for its bereavement exemption: a published, constitution-set fee is not a
new decision needing a second signature the moment it applies.

### The policy engine gets a second business rule

Case eligibility has always produced a transparent list of `Check` objects. Reinstatement
was decided by two hardcoded lines with no visibility into what the policy actually
says. `evaluate_reinstatement()` extends the same `Check` shape to a second rule — the
fee, the waiting-period consequence — logged onto the membership's history, advisory
only: nothing here blocks a reinstatement, which is an administrative act, not a
benefit decision.

### Every override now carries a policy reference and comments

`MembershipExemption` and `MemberAdjustment` gained `policy` (the version in force when
the decision was made) and `comments` (kept distinct from the required `reason`),
populated automatically, discretionary and automated paths alike.

### Auditability, consolidated

`/benevolent/overrides/` — overridden cases, committee votes, exemptions, charges and
waivers, all in one filterable, read-only screen, instead of four different places each
showing part of the picture.

### Confirmed solid, deliberately untouched

Renewals, transfers, household inheritance, member death processing, and inactivity
calculations were all audited and found to already correctly consult the policy fields
that govern them. Reused, not rebuilt.

**Tests:** 33 new (278 in the benevolent module, all green). Full regression clean:
accounts + core (546 tests).

**Deferred, named** (`docs/recommendations.md` #66): a committee seat does not itself
grant the right to vote — correct behaviour, but worth a roster-screen warning;
`committee_requires_chair` names only the Chair, not an arbitrary configurable seat; the
Overrides & Exceptions view has no PDF/Excel export yet (the rest of the module's
reports do, via the Report Engine).

## v2.49.0 - Benevolent Phase 5: Bereavement Case Management

### The case's own narrative

`django-simple-history` has always answered "what was this field on 3 March?" It has
never answered "what happened on this case, and why?" — the question a treasurer
re-opening a case six months later, a board reviewing a large payment, or a bereaved
family asking why their claim took so long, actually asks. **`CaseEvent`** is that
answer, mirroring Phase 3's `MembershipEvent` exactly: every workflow function — raise,
submit, assess, vote, approve, reject, cancel, raise a payout, a voucher clearing or
being reversed in the ordinary expense screen, close — writes one line, marked
`automated=True` where a rule did it rather than a person.

### Funding targets are a goal, not a rule

A case can now track an explicit fundraising goal (`funding_target`) with a progress bar
against `funding_collected` — deliberately **never consulted by the eligibility engine**.
The policy alone still decides what is owed; a target is something a committee sets to
work towards, tracked against the same collected figure the levy round itself reports,
so the two can never disagree. Reaching it notifies once, through its own dedicated
setting rather than a repurposed one.

### The bereaved member's own contribution — four options, one function, one bug fixed

Phase 2 modelled this as two overlapping booleans. Building this phase surfaced that they
**could not express "reduced" or "committee decides" at all, and had a live double-charge
bug**: a "deduct" bereaved member was left on the levy roster (asked to pay up front)
*and* had the same amount taken off their benefit — charged twice for one contribution.

Replaced with one explicit choice — **CONTRIBUTES / REDUCED / EXEMPT /
COMMITTEE_DECIDES** — resolved everywhere through a single shared weight function, so
the levy roster, the PER_MEMBER_MULTIPLE pledge calculation and the benefit deduction can
no longer disagree about how much, or whether, the bereaved member owes. The fix: a
deduct-collected member is now excluded from the roster, full stop.

### Automatic exemption is a real exemption, not silent arithmetic

Phase 2's dues waiver after a bereavement correctly zeroed what was owed — but did it
with **no record**: no exemption row, no membership event, nothing in the standing
register. Phase 3 already established, for every other exemption in the system, that
"an exemption without a recorded reason is indistinguishable from favouritism." A
silently-zeroed due was exactly that gap, just for the bereaved member specifically.

Fixed: approving a case under an EXEMPT bereaved policy now grants a real, auditable
`MembershipExemption` — auto-approved (a policy-computed waiver is not a new decision
needing a second signature; the church already wrote the rule down), but identical in
shape and just as visible as a hand-granted one. Standing correctly shows **EXEMPT**,
not a silent GOOD; both a membership event and a case event record it.

### Documents: a checklist, not a checkbox

Event types can now name the documents a case actually needs ("Burial permit", "Death
certificate"), each tracked individually rather than one yes/no. An event type with no
named list falls back to the old behaviour unchanged.

### Multiple concurrent cases

Nothing ever restricted a member to one open case at a time, and nothing added here
would either — confirmed with new tests proving two open cases never cross-contaminate
each other's levies, funding targets, or the annual claim-frequency cap (which already
only counted *decided* cases).

**Tests:** 45 new (245 in the module, all green). Full regression clean across
benevolent, cashbook (396), giving (234), core (419), and reports+members (439) —
1,733 tests total.

**Migration note:** `bereaved_exempt_own_levy` is retired via the same three-step
pattern Phase 3 used for its status/standing split — add the new field, translate every
existing value, then remove the old one — so existing data carries forward losslessly.

**Deferred, named** (`docs/recommendations.md` #65): the case *list* has no
funding-progress column yet (the detail screen carries it); `COMMITTEE_DECIDES` is a
binary ruling, not a per-case custom percentage — a church wanting that sets REDUCED at
the policy level instead. Checked, and found NOT to apply here: the silent-fund-drop
shape from recommendation #63 does not recur in the levy/deduction logic, since both
read live at call time rather than from a stale snapshot.

## v2.48.1 - Fix: envelope ledger could lose data for a fund outside the "preferred" defaults

**The bug, as reported:** opening an existing envelope batch and adding fund columns
partway through data entry could make previously-entered amounts vanish, with rows
failing to submit ("Total 200 doesn't match the fund amounts entered (0)"), across as
many rows as used a fund outside the ledger's five "preferred" quick-pick funds
(Tithe, Combined Offering, Camp Meeting, Development, LCB).

**Root cause — confirmed, not assumed.** Purely client-side, in the inline script on
`templates/envelopes/ledger.html`. The fund-column checkboxes start ticked only for the
five preferred funds. On page load, the script built its working column set entirely
from those checkbox states, with no regard for which funds the batch's OWN rows
actually used — so a row holding money against any other fund loaded with that
column hidden, its amount invisible, its on-screen total computed as 0 against
whatever "Total" was saved, and every such row flagged as mismatched. Worse: the grid
autosaves every 15 seconds with no further typing required, and autosave replaces a
batch's rows *wholesale* from whatever is currently on screen — so the very next
autosave silently erased that fund's amount from the database. "Adding the column
back" afterwards restored an empty box, not the original figure, because by then the
value was already gone.

Verified end-to-end with a jsdom harness that executes the real rendered page script:
reproduced the exact reported symptom (computed total 0 against a saved 200) against
the pre-fix template, across a 7-row batch shaped like the actual report (same row
numbers, same names, same "Total 200 / 1,000 / 200 vs (0)" pattern), and confirmed it
no longer reproduces post-fix.

**Fix (client-side only — no server bug, no migration):**
1. Any fund a batch's own rows already use is ticked automatically at boot, before the
   column set is computed — a used fund is never hidden by default.
2. A row's amounts are now read as a merge of everything it is *known* to carry with
   whatever is currently rendered, so hiding, reordering, or not-yet-showing a fund
   column can never again be how an amount is silently dropped.
3. The mismatch check and the autosave payload both go through that same merge, so
   they can never disagree with what the row actually holds.
4. A light, non-blocking notice appears if hiding a column would hide money that's
   already been entered against it.

**Tests:** a permanent regression test
(`envelopes/test_ledger_column_data_loss_v2481.py`) checks the server-side
prerequisites the fix depends on. Full `envelopes` suite (163 tests) green.

**Also found, not yet fixed** (`docs/recommendations.md` #63): `post_batch` resolves
funds as `active=True` only, so a fund deactivated between a batch being approved and
posted would have its line silently dropped at posting — the same *shape* of bug, in a
narrower, rarer window. Left open, tracked, and explicitly not folded into this fix.

## v2.48.0 - Benevolent Phase 4: Contribution Engine & Intelligent Allocation

### Money and obligations are different things

A welfare scheme deals in two currencies at once, and confusing them is the classic way
a member ledger goes quietly wrong. Booking a **waiver as an expense** shows a cash
outflow that never happened. Booking a **penalty as income** recognises revenue that may
never arrive. Booking a **refund as negative income** hides a real payment from the cash
book.

So money goes where money has always gone in this module — `giving.Transaction` in,
`cashbook.Expense` out, no new machinery — and **obligations get their own home**
(`MemberAdjustment`) which **posts nothing at all**. That is not an omission; it is the
design. A penalty charged is not income: nobody has paid it, and they may never. It
becomes income on the day it is actually paid, as an ordinary receipt. A waiver is the
church deciding to stop asking; no money left, so no entry.

What they change is one number — what `arrears_for()` says a member owes — and that
function is still the *one* place in the system that knows. It now has three inputs (the
policy's dues, the obligations ledger, the money received) and still gives one answer, to
the register, the eligibility engine, the arrears deduction on a benefit and the member's
statement alike.

### A refund is not a reversal

A receipt that **should never have existed** — wrong member, duplicate, bounced — is
**reversed**: the church never had that money. A receipt that was **correct**, where money
is genuinely handed back, is **refunded**: the church really received it and is really
paying it out, and **both facts belong in the cash book**. Reversing a correct receipt to
"cancel out" a refund would hide a real payment from the bank reconciliation and understate
income *and* expenditure. So a refund is an ordinary expense voucher, built exactly as a
benefit payout is, clearing the usual approval — the module still never approves its own
payments.

### Unallocated is not unrecorded

The single most important sentence in this phase. **Allocation is allowed to fail. It is
never allowed to lose the money.**

A receipt whose owner cannot be identified is still receipted, still in the scheme's fund,
still in the general ledger, still on the bank reconciliation, still in the board pack. It
sits in an intake queue until a human says whose it is — and the fund balance is right the
whole time. A system that refused to bank money it could not attribute would produce a fund
balance that disagreed with the bank, which is far worse than an unattributed receipt.

The importer therefore does two things in a deliberate order: it gets the **fund** right
(from a narration rule, with certainty) and banks the money; and only *then* asks whose it
is. The first must never wait on the second. Rejecting a queue item follows the same
principle — deciding a receipt is not benevolent money is a statement about *attribution*,
not about whether the church received it, so the transaction is left exactly where it was.
Conflating the two would let a treasurer make money vanish from the cash book by clicking a
button.

### The allocator

Every identifier the brief named, each a weighted **signal**: membership number (70,
conclusive) · case reference (55) · member's own phone (55) · household identifier (45) ·
the member's other numbers (45) · **the spouse's phone (45)** · a dependant's phone (35) ·
name, exact (30) and fuzzy (20) · narration rule → scheme (25) · the amount matching what
this member owes (10–12).

**Signals add**, so corroboration is what produces confidence — no single medium signal
reaches the auto-allocation threshold alone. A **name never carries an allocation by
itself**: two brothers share a surname. A **spouse paying her husband's dues from her own
phone** is completely routine, and a system that cannot see it would queue a perfectly
ordinary payment every single month.

**It shows its working** — every candidate, every signal, the score, frozen onto the queue
row, so a wrong automatic allocation can be *understood* rather than merely undone. There is
a screen where a treasurer can ask "what would you do with this?" and see the reasoning.

**It refuses when it should.** Two candidates within 15 points of each other is **not
confidence, however high the top score** — it is the allocator saying it cannot tell them
apart, which is exactly where a wrong automatic answer is most likely and least likely to be
noticed. Such a receipt goes to review even at 95%.

### The intake queue

AUTO / REVIEW / UNMATCHED / DUPLICATE / REJECTED. A suspected duplicate never
auto-allocates, whatever the confidence — and it is flagged, never *blocked*: some
duplicates are genuine, and silently refusing a real payment would be worse than accepting
one, because the member would have paid and the scheme would deny it.

**Learned rules are proposed, never switched on.** After a treasurer allocates the same
unrecognised narration by hand three times, the system writes the rule — inactive. A rule
that silently started routing money because of a pattern nobody agreed to would be a rule
nobody agreed to.

### Policy-driven validation

One function, asked by both the manual path and the intake path, so they cannot disagree
about what is legal. It refuses dues to a scheme with no dues, a levy with no case, a fee the
policy does not charge, an obligation from a member who owes nothing (their money is a
donation) — and the one a treasurer would not think of: **levying the bereaved member for
their own case**, which the policy already says is not done.

### Also

The full contribution taxonomy (dues / levy / registration / renewal / penalty / voluntary /
donation), with a data migration off the old coarser kinds; `SchemeDependant.phone`;
configurable thresholds; and the statement importer hooked alongside loans.

**Tests:** 50 new (200 in the module). Full regression green across statements, giving,
cashbook, ledger, reports, core and members.

**Deferred, named** (`docs/recommendations.md` #62): recurring contributions are
*recognised*, not *scheduled* — the engine handles dues arriving on any cadence but
initiates nothing, and I would rather say so than claim both; refund-on-exit is still not
automatic (though the mechanism now exists, so it is cheap to close); the allocator's weights
are hard-coded (the thresholds, which matter more, are not). And **reminders have now
survived three phases doing nothing — raised to HIGH priority**, because a setting that has
outlived three releases is a credibility problem, not a backlog item.

## v2.47.0 - Benevolent Phase 3: Member Registry, Households & Standing

### The refactor this phase turns on

Phases 1–2 kept a single `SchemeMembership.status`, and it was quietly doing two
incompatible jobs: carrying decisions a **human** makes (pending, active, suspended,
withdrawn) *and* facts a **job** derives (lapsed, expired, inactive). So automation
wrote into the same column a treasurer wrote into, kept safe only by an allowlist of
statuses it was permitted to touch — a rule someone had to remember, and would one day
forget. Worse, it made a derived fact look like a decision: a membership marked LAPSED
told you nothing about whether a person had chosen that or a nightly job had inferred
it.

Phase 3 splits them into two axes, and everything else follows:

* **`status` — the LIFECYCLE.** Pending, Active, Suspended, Withdrawn, Deceased,
  Closed. A human decides every one, records a reason, and is answerable for it.
* **`standing` — COMPUTED.** Good standing, Exempt, Grace period, Arrears, Inactive —
  plus the lifecycle states, which dominate. A pure function of the policy and the
  facts. Never hand-set.

`standing` is a **cache of a pure function**, so recomputing it can never lose
information. Automation now writes **only** to that column and is therefore
*structurally* incapable of overruling a treasurer — not because it is told not to,
but because `status` is a different column and the job does not write to it. A test
states it plainly: a suspended member who pays off every shilling stays suspended.

`LAPSED`, `EXPIRED`, `INACTIVE` and `EXPELLED` are gone from `status`; a data
migration moves existing rows across and records the original word in the event log so
nothing is lost.

### Standing reports; the policy decides

They must never disagree about a plain fact — so **they do not each compute it**.
`MembershipFacts` is computed once and consumed by both the register and the
eligibility engine. There is exactly one place in this system that knows how many
months a member is behind.

A test walks every combination of arrears treatment and inactivity action and asserts
that the register's view of cover and the engine's verdict never differ. An early
version of `covered` used a fixed list of "good" standings, and that test caught it
telling a treasurer an ARREARS member was "not covered" while the engine happily paid
them — because under DEDUCT, the commonest real rule, it does.

The same principle put exemptions inside `arrears_for()` rather than in the standing
engine. Had they lived anywhere else, an exempt member would have shown as clear on the
register **and still had money docked from their bereavement payout**. There is a test
for exactly that.

### Extending Members, not duplicating it

`members.Member` remains the only record of a person. A dependant on the church roll is
**linked** to their member record, not typed in a second time — so a spouse's name and
phone live in one place and cannot drift. A household is a *registration type*, not a
parallel person-database. A member's own page now shows their welfare standing, and the
households they are *covered by* as well as the ones they hold — a page that can only
exist because there is one registry.

### Death, transfer and inheritance

**Recording a death does not close the membership.** Their own death is very often the
last claim on the scheme — the thing they paid in for — and a system that closed the
membership there would discard a family's entitlement at the exact moment it fell due.
The eligibility engine explicitly does not bar a claim on a deceased member's own death.

**A transfer keeps the joining date.** A widow whose husband paid in for eleven years is
not a new member with a ninety-day wait. `transfer()` keeps `joined_on` and deliberately
does *not* set `reinstated_on` — that field exists to stop a lapsed member gaming the
scheme, and a grieving widow is not a lapsed member gaming the scheme. The household
travels with the membership; the trail is intact in both directions. Reinstatement still
restarts the waiting period, and that anti-gaming rule survives intact and tested.

### Exemptions

Without a first-class record, exemptions are handled by a treasurer quietly not chasing
certain people — which is **indistinguishable from favouritism**, cannot be handed over,
and disappears when they do. So an exemption must record why, is **approved by a second
person** (it relieves someone of an obligation everyone else is carrying), excuses nobody
until it is approved, and can cover dues, levies or both. A levy-exempt member comes off
the levy roster — leaving them on it would chase them for money the church has already
decided, in writing, that they do not owe.

### Inactivity: missed contributions *or* missed cases

A levy scheme has no monthly dues to miss, so "months since a contribution" sees nothing
— and the member who never stands with a bereaved family, then expects the family to
stand with them, walks straight through. `inactivity_missed_cases` catches them. Counted
**consecutively** backwards from the most recent case (an old lapse since made good is
not the problem this rule is for), and skipping cases raised for the member themselves —
they were never levied for their own bereavement, and counting it as a miss would punish
them for being bereaved.

### The membership event log

`simple-history` answers "what was this field on 3 March?". `MembershipEvent` answers
"what happened to this member, and why?" — which is what a treasurer, a board and a
bereaved family actually ask for. Every registration, admission, refusal, fee, renewal,
suspension, reinstatement, withdrawal, death, transfer, exemption and standing change is
one line, with who did it, why, and whether a person or a job did it.

### Also

Households (one spouse, size caps counting the principal member, removal that never
deletes — a dependant covered when an event happened stays covered for it); a register
with standing counts and filters; six new policy rules (`grace_period_days`,
`allow_exemptions`, `exemption_age`, `inactivity_missed_cases`, `allow_transfers`,
`max_household_size`).

**Tests:** 49 new (149 in the module). Seven Phase 2 tests were rewritten to the new
two-axis contract — legitimately, because the refactor changed it. Full regression green
across members, core, accounts, cashbook, giving, departments, ledger, reports, loans,
pledges, statements and envelopes.

**Deferred, named** (`docs/recommendations.md` #61): nominee payout splitting is still
manual; refunds on exit, arrears/renewal reminders and `max_levies_per_year` remain
fields nothing acts on (reminders have now survived two phases — noted as such); a
household cannot yet be charged per-adult; and a church that never schedules
`benevolent_automation` will have a quietly stale register.

## v2.46.0 - Benevolent Phase 2: Constitution, Settings & Policy Engine

Every church-specific behaviour is now configuration. The danger in doing that is
obvious and fatal — if the rules become *settings*, then editing a setting rewrites
the basis of decisions already made, and the module's central promise collapses. So
everything configurable goes to one of two homes, and the test for which is one
question: **does it decide an outcome?**

* **YES → it is a RULE.** It lives on the versioned `SchemePolicy`, which is frozen
  the instant a case is decided under it. Registration, fees, renewals, contribution
  models, benefit calculations, committee approvals, bereaved-member rules,
  inactivity, household cover and inheritance are all rules: change one and a claim
  that would have been paid might now be refused.
* **NO → it is a SETTING.** It lives on `BenevolentSettings`, freely editable.
  Accounting mappings, notification preferences, automation cadence and defaults
  steer how the module *operates*; none can change whether a past claim qualified.

That line is what lets "all behaviour driven by configuration" and "policy changes
do not modify historical transactions" both hold, instead of trading off.
`RULE_FIELDS` grew 19 → 54, and a test asserts every constitution dimension is
actually in it — a rule that is *not* under the version lock is one that could be
quietly changed after a case was decided on it. Accounting mappings sit on the
settings side because every posted document stores its own fund and category when
written: re-pointing a mapping steers future postings only and is physically
incapable of rewriting a historical one. There is a test for exactly that.

**The settings area** (`/benevolent/settings/`) is its own page under its own right,
inheriting the app's theme, layout, tab framework and permissions wholesale — but
separate, so a welfare secretary can run the module without also holding the keys to
the church's SMS gateway and bank feed. Four tabs: accounting, notifications,
automation (with a "run it now and show me what it would do" button), defaults.

**The constitution** now covers: registration (approval route, fee, forms, ID,
joining age — measured *at joining*, so a scheme capping entry at 70 does not throw a
member out on their 71st birthday); renewals (period, month, fee, grace, lapse);
contribution models including **hybrid** (dues *and* a per-case levy); funding methods
(a rule, not a note — it stops a member-funded scheme being quietly subsidised out of
the church budget without the constitution being changed); two new benefit modes —
**POOLED** (the family receives what the levy actually collects; such a scheme can
never become insolvent) and **PER_MEMBER_MULTIPLE** (the levy × the membership: what
the scheme *promises* if everyone pays, deliberately distinct from what was raised);
benefit rounding; arrears treatment with **DEDUCT** as the default, because refusing a
bereaved family over two months of dues is not what a welfare scheme is for;
bereaved-member rules (not levied towards their own benefit — what almost every real
constitution says — or deducted from it, never both); inactivity with a
**reinstatement waiting period**; household cover and dependant caps; and inheritance
by nominee, next of kin or household succession.

**`cover_from`** is the single definition of what every waiting period counts from:
reinstatement > registration > joining. The reinstatement case is the point — without
it a member could lapse for years, rejoin the week a relative fell ill, and claim on
the strength of a 2019 joining date.

**Committee approval** means a benefit routed to the committee is not authorised by an
individual *at all* — a treasurer cannot approve past a quorum, and there is a test
that says so. Where members differ on the amount, the **lowest** approved figure
carries: three people voting 10,000 / 8,000 / 10,000 have agreed on 8,000, not 10,000.
`benevolent_committee` is its own right, because a committee whose seats are held
automatically by the treasurer is not a committee.

**Policy profiles** — four built in (monthly dues, harambee levy, hybrid, medical
percentage). A profile governs nothing: applying one creates a **draft** policy a
human still publishes, which is why profiles can be edited freely. A working policy
can be captured back as a profile.

**The Constitution Wizard** asks the ~28 questions a constitution actually answers, in
the language a constitution actually uses, and writes the policy. It **shows its
reasoning** — every derived setting is listed with the answer that produced it, because
a black box that emits a constitution is worse than no wizard at all, since it will be
trusted. It produces a **draft**, and its output travels the same code path a
hand-picked profile does, so the two can never drift apart.

**Automation** (`manage.py benevolent_automation`, with `--dry-run`) applies the
policy's rules to the register. It **never overrides a human** — a membership someone
deliberately suspended or expelled is left alone — and it **never suspends or expels
anyone itself**, even when the policy names that as the inactivity action: removing
someone from a welfare scheme is a decision a person should make and answer for. The
policy still bars their claims; automation declines to be the one who throws them out.

**Two bugs the tests caught:**
1. A registration fee was counting as **dues paid**, silently wiping a member's
   arrears — a 500 fee cleared 300 of arrears. `exclude_levies` was too narrow: a fee
   is neither a levy nor a due. Contributions now carry an explicit `kind`
   (DUES / LEVY / FEE / DONATION), and only DUES settle dues.
2. A wizard question depending on another question **in the same section** could never
   be answered — it was hidden at render time *and* at save time (the controlling
   answer arrives in the same POST), so every wizard-built policy came out with dues
   of zero. Same-section dependencies are now shown and toggled live.

**Tests:** 56 new (99 in the module). Full targeted regression green across accounts,
core, cashbook, giving, departments, ledger, reports and loans.

**Deferred, named rather than glossed** (`docs/recommendations.md` #59): household
cover is modelled but only half-enforced (no true household object with one
subscription per household); inheritance stops at the nomination (shares are recorded,
splitting a payout across them is not automated); refunds on exit, arrears/renewal
reminders and `max_levies_per_year` are policy/settings fields nothing yet acts on.
Also noted (#60): `core/apps.py::ready()` queries the DB at startup — pre-existing,
unrelated, and left for its own change rather than smuggled into this release.

## v2.45.0 - Benevolent Scheme Engine (Phase 1: foundation & architecture)

A configurable **Benevolent Scheme Engine**, not a single benevolent fund. A
scheme is defined entirely by configuration — a fund, a set of covered events and
a versioned policy — so a Medical Fund, an Education Fund or an Emergency Relief
Fund is a data change, not a code change. There is one eligibility engine and one
case workflow, and both read policy fields. See `docs/BENEVOLENT_MODULE.md`.

**No new accounting machinery.** Following the loans module's precedent exactly,
every shilling flows through the two existing source-document types: a
contribution is an ordinary `giving.Transaction` credit on the scheme's fund (DR
Cash / CR Income), and a benefit is an ordinary `cashbook.Expense` with category
BENEVOLENCE (DR Benevolence / CR Cash). The ledger, fund balances, cash book,
bank reconciliation, budget and Board Pack therefore pick up benevolent activity
with no benevolent-specific code, and `/ledger/rebuild/` needs no new step. A
scheme's balance *is* its fund's balance, read from the Financial Metrics
Registry — there is no second number that can drift. `BenevolentContribution` and
`BenevolentPayout` index those documents and read `amount`/`date` back off them
as properties rather than storing copies.

**The module never approves its own payments.** Approving a *case* records a
decision and moves nothing. Paying it raises an expense voucher in PENDING, which
then clears the ordinary expense route — treasurer approval, the dual-approval
threshold, period locks, the payment register. A benevolent payout is no easier to
get out of the bank than any other payment. A treasurer rejecting that voucher in
the ordinary expense screen, knowing nothing about cases, correctly un-pays the
case with nobody having to remember anything (`benevolent/signals.py`).

**Immutability.** A policy version locks the instant a case is decided under it —
the model refuses to change a rule field or delete the row. The only way to change
the rules is to publish a new version, which supersedes the old one from its own
effective date forward. Every case additionally freezes the full policy terms and
the complete eligibility evaluation (every check, whether it passed, the figures
compared), so a decision is reproducible years later even if a policy row were
tampered with. Policies resolve by **event date, not today**: a claim reported late
is judged by the rules in force when the event happened.

**The policy engine** (`services/eligibility.py`) never returns a bare yes/no — it
returns every rule it ran and its workings, the same transparency principle as the
intelligence platform's HealthScore. Rules modelled today, all as policy fields:
membership required, waiting period (policy-wide or per event), minimum
contributions, arrears block with a tolerance, claim window, annual claim limits
(overall and per event type), annual benefit cap, documents required, and four
benefit modes (fixed / per-event schedule / percentage of cost / discretionary
within a cap) with per-event caps and a policy floor.

**Controls:** segregation of duties (the raiser cannot approve); an ineligible case
can be approved only where the policy permits an override *and* a written reason is
recorded; a policy can forbid overrides outright; approving above the cap also needs
a reason; `available_to_voucher` nets off pending vouchers as well as paid ones, so
several pending vouchers cannot each claim the full approved amount. Four new
granular rights, splitting *administering* a scheme from *making its rules*.

**Also:** 5 metrics registered (all delegating to existing registry
implementations; `benevolent_commitments` is documented as a memorandum figure that
deliberately does *not* touch the balance sheet), a read-only JSON API including a
live eligibility endpoint, a Benevolent nav group, Django admin with full history,
seed data (a scheme, a published policy with four benefits, 8 members with dues,
and one case run end to end to a paid benefit), and 43 tests.

**Three bugs the tests caught during development, all fixed:**
1. `policy_on()` filtered on `status=ACTIVE`, so a *superseded* version resolved
   for no date at all — publishing v2 would have meant every past date found "no
   policy in force", and a late-reported claim would have been refused instead of
   being decided by the rules that actually applied. It now resolves ACTIVE and
   SUPERSEDED versions within their own effective windows.
2. Arrears accrued only from the *current* policy's effective date, so simply
   republishing a policy silently wiped every member's arrears — a treasurer could
   have cleared the scheme's whole debt by republishing the same rules with a new
   date. Dues now accrue from enrolment, each period charged at the rate of the
   policy in force during it.
3. Payouts were guarded against `outstanding` (approved − paid), which ignored
   pending vouchers — so three PENDING vouchers could each be raised for the full
   approved amount and the case would overpay the moment they were all approved.

**Fixed outside the module:** `cashbook/test_group_goals_jpeg.py` was stale and had
been failing before this work — it targeted a `.jpg` chart route that no longer
exists (the app ships PNG). Repointed at the real route; all four tests pass.

**Deliberately deferred, not overclaimed** (tracked in `docs/recommendations.md`
#56): the per-case levy *collection screen* (the model and service exist; the UI
does not), benevolent sections on the Report Engine, bank-narration auto-intake of
dues, arrears reminders over SMS, and dependant-aware benefit rules. Also noted
(#57): a benevolent nav badge was **removed** rather than shipped, because it would
have been a seventh unconditional COUNT on every page render and tripped an
existing query-count guardrail — the right fix is to consolidate the six existing
badge queries first.

## v2.44.0 - Critical dev_group capture bug, budget page permissions/width, ledger UI cleanup, Cash & bank relabel
**1. CRITICAL: Development Group was never actually saved.**
`envelopes.services.batches.autosave_rows` looked up dev_group/member in a
dict keyed by integer model id using the raw client payload value — always
a string in JSON from a browser. `{4: obj}.get("4")` returns `None`: the
same string works fine in the ORM's own `pk__in` filter just above it
(Django coerces it there), so this was invisible at a query level. Silently
dropped `dev_group` on every single row regardless of the fund's category
or how many Development funds existed. Fixed with a single `_as_id()`
coercion helper applied at every lookup site (dev_group_id and member_id,
which had the identical bug). Verified end-to-end from batch autosave
through submit/approve/post to the posted Transaction/EnvelopeLine.

**2. Fund budget page: PNG downloads 403'd for non-Treasurers; table too
wide.** `GroupGoalsPngView`/`BudgetItemsPngView` required Treasurer-only
access — narrower than the budget page's own permission check (which also
covers Assistants and qualifying leaders), so the "Download PNG" links on
that very page 403'd for them with no obvious reason — likely what was
reported as "figures are not showing". Both views now match the page's own
permission model exactly. Separately, the page used `class="ledger
compact"` without ever defining the matching CSS rule (every other page in
the app that uses this class defines it locally) — "compact" was a no-op.
Added the missing rule, added the class to the one table missing it,
wrapped both tables in a scroll container, and tightened two column widths
— the tables now fit a portrait viewport properly.

**3. Envelope ledger UI, per spec.** "Manual Total" → "Total". Default/
pinned funds are now exactly Tithe, Combined Offering, Camp Meeting,
Development, LCB – Local Church Budget, in that order, and pin
automatically on first load. The "Allocated" running-sum column was
removed — the same Total-vs-fund-amounts check still runs in the
background, surfacing only via the row-errors panel on an actual mismatch.

**4. "Cash & bank (funds on hand)" reverted and relabelled "Bank (funds on
hand)".** The v2.42 Local/Trust split was reverted per explicit correction:
since petty cash and staff advances are already broken out onto their own
lines, what remains is genuinely bank-only. Applied to both the
board-pack summary and the legacy full Statement of Financial Position.
Every other "Cash & bank" occurrence in the app was individually checked
against this same test and left alone where the underlying figure isn't
actually reduced by petty/advances (the dashboard, the assistant, cash-flow
statements, the Monthly Treasurer's Report).

**Tests.** 9 + 9 + 9 = 27+ new tests across
`envelopes/test_dev_group_capture_v244.py`,
`cashbook/test_budget_page_v244.py`, plus updates to
`reports/test_financial_position_v242.py` and two older test files that
asserted pre-v2.42/pre-v2.44 labels. Full regression across envelopes,
reports, members, and the directly-affected cashbook/departments modules
all pass.

## v2.43.0 - Six fixes from live production review: loans, financial position, ledger UX, print-quality images, member merge
**1. Loan-conversion contra expense no longer queued as "awaiting a
receipt."** A Convert-to-donation/Write-off posts a same-day, same-amount
contra pair (income + a LOAN_REPAYMENT expense) that retires the liability
against income with no cash movement — there was never a physical document
for a receipt to attach. `missing_receipts_queryset` now excludes
specifically that contra expense (via the LoanTransaction link, kind
CONVERSION/WRITE_OFF); a genuine principal/interest repayment still
correctly requires proof of payment.

**2 & 3. Financial Position summary (Treasurer's Report board pack): Cash &
bank split, payables/accruals/prepayments wired in.** The legacy full
Statement of Financial Position already showed these correctly; the newer
engine-based summary didn't (by prior explicit design, for the board pack).
Three new Financial Metrics Registry entries (`payables_outstanding`/
`accruals_outstanding`/`prepayments_unexpired`, relocated from
`cashbook/views.py` to `cashbook/services/treasury_position.py`) wire the
same accrual-basis adjustments into the summary so the two statements can't
silently diverge. The lumped "Cash & bank (funds on hand)" line was replaced
with a Local/Trust (unrestricted/restricted) split rather than deleted —
Total Assets is built from it, so removing it outright would have broken
the statement's own reconciliation.

**4. Envelope ledger validation UX.** The mismatch message now only
evaluates once a row is finished (focusout on the row, not every keystroke
— the Allocated column still updates live) and renders in one panel below
the table instead of floating text inside a cell. A final validation pass
runs on Submit for a row that was never blurred. The Allocated column's
purpose is now explained in the page's help text.

**5. Server-generated report images — genuinely high-DPI, across the whole
pipeline.** Every Pillow image (the two budget-page PNGs, the three PDF/
Word-export chart builders) was drawn at ~96-DPI-equivalent screen pixel
sizes with no scale-up. Both files now render at 4× their previous logical
size with every PNG tagged at 300 DPI. The two client-side canvas exporters
already used the same technique at 2×; raised to 4× for consistency.

**6. Member merge phone numbers — verified already working, two gaps
closed.** `merge_members` already correctly preserved both members' phone
numbers (confirmed empirically). `match_or_create_member` — every future
import's matching step — only checked a member's PRIMARY number, so a
payment from an absorbed member's own preserved number would silently fail
to match; now checks both. The preserved secondary numbers were also never
shown anywhere in the UI; now shown on the member detail page.

**Tests.** 6 + 10 + 6 + 13 + 11 new tests across the six areas
(`cashbook/test_loan_contra_receipts.py`,
`reports/test_financial_position_v242.py`,
`envelopes/test_ledger_validation_ux_v242.py`,
`reports/test_high_dpi_images_v243.py`,
`members/test_phone_merge_v243.py`), plus two pre-existing tests updated to
match the intentional Cash & bank / image-dimension changes. 213+ tests
across the full regression pass.

## v2.41.0 - Five follow-up fixes from live review of the maker-checker/board-pack work
Fixes five issues raised after v2.39/v2.40 shipped: a genuine regression in
Development Groups (root-caused, not just patched), numbered subgroups
cluttering summary reports, chart sizing fixed at its systemic root, the
non-functional Ask AI affordances removed from the treasurer report, and a
broken multi-line Django comment that was rendering as visible page text.

**1. Development Groups — root cause found and fixed properly.** The ledger
picked "the" Development fund via an AMBIGUOUS, unordered `.filter(category=
"DEVELOPMENT", parent__isnull=True).first()` — fragile whenever more than one
department carries that category (a real case: several active building/
project funds). The v2.40 subgroup work happened to make this latent bug
visible. Fixed at the root: every column now carries its own `is_development`
flag via the SAME per-department check the cash-entry form and review queue
already use, so multiple Development funds each get an independent,
deterministic picker — and Development is now unconditionally immune to the
generic subgroup mechanism (`subgroups_for` always returns `[]` for it),
regardless of what `Department.parent` relations might otherwise exist.

**2. Numbered subgroups roll up to their parent in summaries — named
sub-accounts don't.** Real subgroup posting (v2.40) exploded the Sabbath
statement / monthly summary / Sabbath Excel export into one column per
subgroup for a fund with many. Rather than blanket-collapsing every
Department.parent child (which would have hidden Tithe/Camp Meeting under
"Trust Fund" — a real regression against reports treasurers already rely on),
the rollup targets specifically NUMBERED sub-accounts (a child whose name
ends in a number, e.g. "Small Group 7") via
`departments.models.numbered_subgroup_parent_map`. Ledger postings are
unaffected either way — display-only, in three places.

**3. Chart sizing fixed at its systemic root.** `ChartSpec.to_config()` — the
one place every engine chart's config is built — now defaults
`maintainAspectRatio:false`/`responsive:true` unless a caller overrides it,
so the fix covers every current and future engine chart, not just the
treasurer report's "Local vs trust funds" (a doughnut growing to match its
card's full width). Every canvas sits in a height-constrained box, including
the generic `engine_report.html` (so ordinary reports don't regress from
"too big" to "collapsed"). Fund balances are now sorted alphabetically within
each block, in both `FundSummaryComponent` and `FundBalancesStatementSection`.

**4. Ask AI removed from the treasurer report** (toolbar button, five
per-section links, the partial, related CSS) — scoped to that report as
asked; the same feature is untouched elsewhere.

**5. A broken multi-line Django `{# #}` comment removed** — Django comments
cannot span multiple lines; the board pack's header comment was rendering as
literal visible page text instead of being stripped. A repo-wide scan found
no other instance.

**Tests.** `envelopes/test_subgroup_followups_v241.py` (9) +
`reports/test_board_pack_fixes_v241.py` (13), plus two pre-existing tests
updated to match the intentional Ask AI removal. 462 tests across the
directly-touched apps and the broader regression all pass.

## v2.40.0 - Six envelope-ledger/dashboard/fund-report fixes from production review
Fixes six issues from live production review: a URL-routing crash, a missing
delete-draft UI, dashboard chart sizing (and the layout break it caused),
JPEG→PNG across every image export, live Channel/Group cascading, and a
generalised subgroup picker for any fund with real sub-account children.

**1. `/envelopes/ledger/<pk>/` crash fixed.** `EnvelopeLedgerCreate.get()`
now accepts the URL's `pk`; a stale/foreign batch id redirects to the Review
Queue with a message instead of crashing or showing the wrong sheet.

**2. Delete-draft UI added.** The backend endpoint from v2.39 had no button
anywhere; added to both the Review Queue list and the batch detail page,
behind a confirm dialog, own-drafts-only.

**3. Dashboard chart sizing fixed (and the "broken" card layout it caused).**
None of the four dashboard charts set `maintainAspectRatio:false`, and none
of their containers had a height — a doughnut chart could grow to match its
card's full width, stretching the card and breaking the three-column row
alongside it. Added a height-constrained `.chart-box` wrapper + explicit
`maintainAspectRatio:false` on every chart.

**4. JPEG → PNG, comprehensively renamed.** `static/js/table_jpeg.js` →
`table_png.js` (`tableToJpeg`→`tableToPng`), the dashboard's inline
`downloadLocalFundsJpeg()`→`downloadLocalFundsPng()`, and the two server-side
Pillow budget-page images (`cashbook/services/goal_chart.py`,
`build_*_jpeg`→`build_*_png`, `format="JPEG",quality=92`→`format="PNG"`) with
matching URL/view/template renames (`.jpg`→`.png` throughout). PNG is
strictly better for all of these — sharp table text and flat fills, not
photos — with no meaningful file-size cost.

**5. Channel/Development-Group now cascade live, not just at row creation.**
v2.39 only copied the row-above's value when a *new* row was added; editing
an *existing* row's Channel or Group did nothing further. Now mirrors the
receipt-number cascade exactly: a change propagates forward to every later
un-overridden row, and a later explicit change becomes the new anchor.

**6. Subgroup picker generalised beyond Development.** Any fund with real
`Department.parent` sub-account children (e.g. Trust Fund → Tithe, Camp
Meeting) now gets its own "which subgroup?" picker in the entry grid —
`column_catalog()` carries each fund's subgroups (id/label/trailing number).
Unlike Development's separate non-posting `DevelopmentGroup` tag (left
untouched — 15+ modules depend on it), choosing a subgroup here re-targets
the amount to post directly against that child fund's own account, since
these are independent real funds with their own balances; the grid's summary
still attributes the amount back to the parent's display bucket. The Excel
import's "Group Number" column now feeds the same mechanism — reusing the
identical trailing-number-matching idea a numbered fund family already uses
for bank-narration parsing, generalised to per-row subgroup reattribution
("the same row allocate" for numbered subgroups).

**Tests.** `envelopes/test_ledger_fixes_v240.py` (20 tests, items 1/2/6
backend); `cashbook/test_goal_table_png.py` / `test_budget_items_png.py`
(replacing the old JPEG test files). Items 5/6's client-side behaviour
(cascading, subgroup rekeying, totals bucketing) verified by running the
actual page JavaScript in a real DOM via Node + jsdom (no browser available
in this sandbox). All six touched pages re-validated with a stack-based HTML
structural check (zero errors). 150 tests across the directly-touched apps
plus 149 more across `leaders`/`departments`/`core` all pass.

## v2.39.0 - Report Designer visual builder + Envelope Ledger maker-checker redesign
Two substantial pieces: fixes the production Report Designer crash with deep,
general hardening and replaces its hand-typed-JSON editor with a real visual
builder; and rebuilds the Envelope Ledger into a production-grade Draft ->
Review -> Approve -> Post maker-checker workflow, preserving all existing
accounting logic.

**Report Designer — crash fixed, then rebuilt as a visual builder.** The
reported `AttributeError: 'str' object has no attribute 'get'` (a section
entry that was a bare component-name string instead of an object) is fixed
with deep hardening, not a one-line patch: `validate_definition`/
`_build_section`/`compile_definition`/`_build_filters` now guard every access
with an isinstance check first, so no malformed JSON shape can ever escape as
an uncaught exception — each becomes a specific, human-readable problem
instead, and `register_all_enabled` survives even a residual unexpected
failure per-definition so one broken saved report can never take the whole
platform down at startup. The editor itself was rebuilt: a click-to-add
component palette, drag-to-reorder section cards with real form fields
(title, a width preset, per-component parameters rendered from a new
`params_schema` on the component registry — e.g. narrative gets a dropdown of
titles instead of a key to remember), and section **order is now implied by
position in the list**, removing the manual order-number bookkeeping that was
likely the single biggest source of friction. `ComponentRegistry.register()`
gained `designer_safe` (default True); `chart`/`appendix`/`financial_statement`
— which need a raw Python callable/array the JSON wire format can't carry —
are marked unsafe: excluded from the palette and rejected by validation even
if referenced directly, so they can never reach `component_registry.create()`
with a missing callable and crash at render time. An "Advanced: raw JSON"
panel remains for power users, synced with the visual builder both ways.
29 new tests (`reports/test_designer_hardening.py`), including a direct
reproduction of the production incident.

**Envelope Ledger — Draft -> Review -> Approve -> Post.** New
`EnvelopeBatch`/`EnvelopeBatchRow` models are a pre-ledger staging area;
`envelopes/services/batches.py` owns validation, duplicate detection and the
whole transition set. Only `post_batch` ever writes to the ledger, and it does
so by calling `_save_envelope`/`_expand_lines` — relocated verbatim to the new
`envelopes/services/posting.py` (mirroring the `cashbook/services/
treasury_position.py` relocation pattern from v2.36) — so posted accounting is
byte-identical to before. Manual entry auto-saves into a Draft from the first
keystroke (debounced fetch + 15s heartbeat + a `sendBeacon` safety net on
tab-close, so nothing is lost to a crash); import is validated and lands
directly in Review, never posting directly, and a receipt clashing with an
existing envelope is now a reviewable row error instead of a silently dropped
line. Approve/Return/Reject/Post are Treasurer-only and honour
`require_different_approver`; Post re-validates fresh (including the
accounting-period lock) and locks the batch row so concurrent posting can't
double-post. `EnvelopeBatch` has full history and appears in the existing
Audit Log Report.

The entry grid itself: the Start-receipt-# field is gone (row 1's own value is
the start); editing any row's receipt continues the auto-increment sequence
from that point for every later un-edited row while preserving earlier manual
overrides (alphanumeric-aware); new rows inherit Channel and Development Group
from the row above. The calculated Total column was replaced by an editable
**Manual Total** column right after Receipt Number, compared automatically
against the allocation-column sum, with the whole row highlighted red and an
inline message on mismatch — blocking Submit both client-side and
server-side. Grid columns (fixed and dynamic fund columns alike) can be
dragged to reorder, shown/hidden, resized, and pinned, saved per user via a
new generic `table_state` endpoint (activating a previously-unused
`UserPreference.table_state` field) and restored automatically on future
logins.

**Tests.** `envelopes/test_batches.py` (54 tests) covers the full workflow,
duplicate detection, segregation of duties, the approve/post concurrency
race, autosave (both request shapes), permissions, and the import path. Five
pre-existing tests written against the old synchronous-post contract were
updated to the new workflow (same intent, new shape). The grid's highest-risk
client logic (receipt cascade, Manual Total validation, duplicate flagging,
inheritance, autosave debounce) was verified by running the actual extracted
page JavaScript in a real DOM via Node + jsdom, since a browser isn't
available in this sandbox — recommendation #50 notes a live-browser pass is
still worth doing before relying on drag/resize/pin in production.

## v2.38.0 - Option A (signed cash) + recommendations pass
Implements the agreed Option A for the manual-receipt double-count, then works
through docs/recommendations.md and closes four deferred items with clear,
bounded solutions.

**Option A — canonical signed-cash definition (rec #47, IMPLEMENTED).** One
definition on the Transaction model: `is_bank_memo` (a BANK row with
manual_receipt=True — the memo half of a manually-receipted pair, whose cash
lives on its envelope entry), `signed_cash_amount` (memo = 0, reversal/debit =
negative), `signed_cash_case()` (the SQL twin) and queryset
`signed_cash_total()`. Consumers: the transactions page running balance (both
the per-page loop and the prior-pages SQL aggregate — verified across a
pagination boundary), the CSV/XLSX export Amount column (now the row's true
cash effect; a memo row exports 0 with its Receipt-status column explaining
why), and the Cash Book — which was also summing unconfirmed and reversed
credits, fixed to confirmed, non-reversed, non-memo receipts. The memo row
stays fully visible, badged "MEMO — no cash effect" with a struck, muted
amount. Correction from deeper reading: processed_via_envelope rows are NOT
duplicates (that flow attaches an envelope to the bank row without a second
posting) and still count; bank-side reconciliation views were verified already
correct (book side = BANK-channel rows only, where memo rows rightly count —
the money is genuinely at the bank). The mislabelled export status for
loan/financing rows was also corrected.

**Rec #2 (IMPLEMENTED, Option A) — request-scoped SiteConfig memoization.**
SiteConfigCacheMiddleware opens a per-request memo for SiteConfig.get();
save() invalidates mid-request; the memo is unconditionally dropped at request
end; behaviour outside a request is unchanged. Measured: exactly one
SiteConfig select per request, down from 7–11. Cross-request caching remains
deliberately rejected (security/financial-control staleness under
multi-worker LocMemCache).

**Rec #28 + #44c (IMPLEMENTED) — charts in PDF/Word + per-section collapse.**
chart_image.render_chart_config() renders the engine's Chart.js configs to
PNG (bars, proportional split for pie/doughnut, new polyline renderer for
line charts; junk-safe). The engine PDF embeds via reportlab, Word via base64
data-URI; the Treasurer's Report charts are now export-visible — the board
pack PDF carries its three charts. Board-pack sections honouring
LayoutMeta.collapsible gained a click/keyboard collapse toggle (caret, Ask-AI
unaffected, print forces open).

**Rec #36 (IMPLEMENTED) — financial_statements_pack.** One report composing
the I&E, Financial Position, Cash Flow, Fund Balances and Trial Balance
sections under one shared ReportContext, so the statutory set is one click
and internally consistent by construction.

**Reviewed and deliberately deferred** (with reasons recorded in the doc):
#1 (legacy board-report aggregate sharing — superseded by the engine report's
shared context; retirement path is #32), #6 (advance-list bulk balances —
marginal at current scale), #8 (dropdown helper), #11 (scope=col sweep —
needs its own dedicated pass), #13 (BudgetLine rename — migration churn),
#20/#21 (policy decisions), #44b (designer template picker).

**Tests.** reports/test_recommendations_pass.py (14) + giving/test_signed_cash.py
(10): memoization query counts and scope isolation, chart PNG rendering and
junk-safety, PDF image embedding, Word data-URIs, collapse markup, bundle
registration/reconciliation/exports, signed-cash definition parity, running
balance across pagination, export column sums, cash-book basis. 416 tests
across the targeted regression waves (reporting core, giving, pages, accuracy,
accounts/middleware stack) all pass.

## v2.37.0 - Production review fixes: fund ledger financing, expense balance, regex fund families, petty cash on SOFP
Four fixes from live production review, plus one recorded advisory.

**1. Fund ledger now shows loan/financing receipts and expense refunds
(reports/views.py FundLedgerView).** The ledger excluded excluded_from_income
credits (loan receipts, asset-disposal proceeds), yet the fund's opening/closing
balances include that cash — so a loan received in the period was invisible AND
the ledger could not reconcile with its own closing balance. The fund ledger is a
CASH statement: those rows now appear, clearly labelled "loan / financing receipt
(not income)" with src=Financing, and remain excluded from every income report
(the correct split: cash yes, income no). Expense refunds — which the fund's
balances net against expenses — now also appear as contra credits (src=Refund),
so the ledger's closing ties to the canonical fund_balance exactly (asserted by
test).

**2. Expense form's available balance corrected (core/views.py
DepartmentBalanceView).** The endpoint duplicated the fund-balance calculation
inline and had drifted three ways: reversed/reversal credits still counted as
receipts (the production symptom — 838,375.87 shown vs the true 638,351.87),
REMITTANCE expenses were excluded from spend (canonical subtracts them: real
cash out), and refunds were ignored. The canonical calculation was refactored
into reports.services.balances.fund_balance_parts (fund_balance now sums it —
one implementation, two shapes) and the endpoint consumes it, so the form, the
overspend guard, the departments page and every report read the identical
figure by construction. The form's explanatory breakdown now shows the full
formula (refunds and transfers included when present).

**3. Numbered fund families accept /regex/ prefixes
(giving/services/allocation.py).** A prefix wrapped in slashes is a regular
expression for misspellings/variations — '/expen[sc]es?/, exp = CAMP_{n}' also
routes EXPENCE7 and EXPENSES7. Safety: patterns must compile (invalid ones are
skipped, never fatal), user capturing groups are converted to non-capturing,
and the family number is extracted by a named group so a pattern can never
shift it. Plain prefixes behave exactly as before. Settings help text updated
(migration 0050, help-text only).

**4. Petty cash float on its own Statement of Financial Position line.**
Mirroring the staff-advance treatment: the float is cash physically in the box,
inside the fund cash figure, so it is reclassified out of "Cash & bank" onto a
"Petty cash float" line — in the detailed SOFP (view, template, exports) and
the engine Financial Position summary in the Treasurer's Report (which now also
shows the staff-advance line). Totals and the balance-sheet tie are unchanged
(reclassification only, asserted by test).

**5. Manual-bank-receipt / envelope double-count — ADVISORY ONLY (as
requested).** Recorded as recommendation #47 with a recommended design: one
canonical signed-cash annotation where a processed_via_envelope bank credit
contributes zero (both rows stay visible, memo row badged), a reconciliation
rule that the book side is BANK-channel rows only, and a later hardening that
links each pair explicitly. Priority High; awaiting the Option A/B decision.

**Tests.** reports/test_production_fixes_v237.py (16 tests) covers all four
fixes, including a reproduction of the reversal double-count and ledger/
canonical-balance tie assertions. 493 tests across the targeted regression
waves (fund ledger, positions, departments, giving, statements, metrics, board
pack, accuracy, cashbook) all pass.

## v2.36.0 - Financial Metrics Registry expansion + registry hardening
Registers every distinct financial concept the Treasurer's Report (and the wider
application) displays, closes gaps in the registry implementation itself, and
relocates canonical calculations out of view god-files into services — with
byte-identical figures proven by parity tests.

**Ten new registry metrics.** petty_cash_balance, staff_advances_outstanding,
bank_position (system vs statement, with an opening_configured flag for rec #9),
cash_in_transit (from the reconciliation worksheet's IN_TRANSIT items),
pending_expense_claims ({count, total} of PENDING claims), total_payments (a
named composition: operating + capital + remittances, replacing per-section
re-summing), budget_vs_actual (the formal Budget records), dev_group_progress
(previously canonical but unregistered), and two canonical fund selectors —
negative_fund_balances and dormant_funds — defined over fund_summary so they can
never diverge from the balance table. 36 metrics total.

**Registry implementation hardening (core/metrics.py, core/reporting/context.py).**
The registry gained has()/get() safe lookups and validate_authoritative(), a
self-check that every metric's documented implementation path resolves (enforced
by test, so documentation can no longer drift from code as implementations move).
ReportContext.metric() now auto-applies the context's period end to as_of-keyed
metrics exactly as it already applied start/end to period metrics — sections no
longer each need to remember to pass ctx.end. pending_receipts_total was
recategorised from Trust to Balance (it is suspense cash, not a trust concept).

**Canonical implementations relocated (rec #7 progress).** The petty-cash float,
the three staff-advance outstanding totals and the unpresented-payment helpers
moved verbatim from cashbook/views.py into cashbook/services/treasury_position.py
(cashbook.views re-imports them under the old names, so the assistant, dashboards,
period close, statements reconciliation and all existing tests are untouched —
verified by identity assertions). The Bank Position calculation moved verbatim
from reports.views.BankPositionView into reports.services.balances.bank_position;
the view now only presents the service's result, and the metric points there.

**Included in the Treasurer's Report.** A new Treasury Position section (bank per
system vs statement with the difference, petty float, cash in transit, staff
advances, pending claims — "where, physically, is the money?"), a Funds Requiring
Attention section (overdrawn funds always listed in full; dormant funds capped at
the 12 largest with an explicit count note), three new executive-snapshot cards
(bank balance, petty cash, staff advances) with an unconfigured-opening caveat,
and Board Action follow-ups for pending claims and unaccounted advances. Both new
components are registered for the Report Designer.

**Deliberately not modelled:** pending journal entries (journals post immediately;
no draft state exists) and month-end checklist status (no checklist model) — see
recommendations #46 for the reasoning.

**Tests.** reports/test_treasury_metrics.py (22 tests): metric/service parity for
every new metric, relocation identity + figure assertions, registry validation,
context as_of auto-application, view/metric agreement for bank position, and
report inclusion + exports. 460 tests across the targeted regression waves
(metrics, reporting, cashbook advances/payments/liabilities, statements, pages,
board reports) all pass.

## v2.35.0 - Treasurer's Report redesign (executive board pack)
A complete presentation redesign of the Treasurer's Report (/reports/r/treasurer_report)
into a professional board / audit-committee financial pack, while preserving every
accounting figure and keeping all values sourced from the Financial Metrics Registry
through the Semantic Reporting Layer. No accounting calculation was added or duplicated;
the change is composition + presentation over the existing Generic Report Engine.

**Per-report presentation template (backward-compatible engine change).** `Report`
gained an optional `html_template`; when set, `EngineReportView` renders that template
(and the print path uses it too), otherwise the generic `engine_report.html` is used
exactly as before. Every other engine report and the Report Designer are unchanged.
The view also computes a generic grouped-sections context from `LayoutMeta.group`/
`order`/`page_break_before`, so any report can present grouped, navigable sections.

**Board-pack template (templates/reports/treasurer_board_pack.html + partials).**
An executive cover (organisation, title, period, financial-health band), a sticky
section navigator with active-section highlighting, professional grouped/separated
sections, executive KPI call-out cards with period-on-period movement, and a
print-optimised layout (page breaks between groups, running footer, page numbering).
Keeps every "Ask AI about this report"/per-section affordance and the live charts.

**Two new registered components (reports/board_pack_components.py).** An Executive
Snapshot band (Total receipts, Total payments, Net surplus/(deficit), Closing cash,
Trust to remit, Pending allocations, Active funds, Financial health — each with its
movement vs the prior equal-length period) and a Board Action Summary (decisions +
follow-ups from the Intelligence Engine and outstanding-item metrics). Both draw only
from the registry via ReportContext and are registered so the Designer can reuse them.

**Gap-filling composition.** The report now includes the full statutory set —
Statement of Income & Expenditure, Statement of Financial Position, Statement of Cash
Flows and Statement of Fund Balances (Financial Position and Cash Flows were missing
before) — organised into Executive summary, Financial statements, Income & expenditure,
Budget performance, Funds & cash, Trust & development, Treasury operations, Financial
intelligence and Board actions. Added a third chart (local vs trust funds).

**Consistent exports.** The engine PDF and Word renderers gained a matching cover,
group headings, per-group page breaks, a PDF footer with page numbering and the church
name, and section notes — so HTML, Print, PDF and Word read as one board pack from the
same SectionData (identical figures). Charts stay export-hidden (clean, no stubs).

**Tests.** reports/test_treasurer_board_pack.py (19 tests) covers the new components,
figure reconciliation across statements, grouping, the template, and every export;
the existing treasurer/report/engine suites continue to pass unchanged.

Addresses recommendations #43 (sticky TOC + grouping) and #44 (executive cover +
per-format layout); notes #28/#44b/#44c as small follow-ups. Runs alongside the
legacy monthly/board reports — nothing existing changed.

## v2.34.0 - Treasurer's Report + Report-Aware AI Assistant
Extends the EXISTING AI assistant (no second chatbot) to consume the Financial
Knowledge Service, making it report-context-aware, and rebuilds the Treasurer's
Report as the flagship, fully AI-integrated board report. Every figure still comes
from the Financial Metrics Registry via the Semantic Reporting Layer — a single
authoritative source of truth — and the assistant never recalculates a figure.

**Knowledge-aware assistant (core/services/assistant_knowledge.py).** New support
that answers grounded in the Knowledge Service (Phase 9): knowledge_context builds
a factual context block from full_briefing/knowledge_for (health score, headline
metrics, insights with their explanations, recommendations); structured_answer
returns deterministic grounded answers (health breakdown, risks, recommendations,
a concept's figures, the executive briefing) when the LLM is off; and
answer_with_context is the report-aware entry point — with the LLM on it gets the
grounded context and a system prompt forbidding invented/recalculated figures,
with it off it returns the structured knowledge answer. Provenance always attached.

**Report-aware persistent context.** /assistant/ask/ now accepts an optional
{report_key, period, element} payload and routes to knowledge-aware answering; the
/assistant/ page reads report_key/start/end/element/q from the URL, shows a context
banner, and includes that context in every question — so "why did income
decrease?", "explain this chart", "why is this score low?", "which transactions
make up this amount?" all work without the user restating context. A "use general
mode" control clears it. Classic keyword answering is unchanged when no context.

**Ask-AI throughout every engine report.** The shared engine template gained a top
"Ask AI about this report" button and a per-section "Ask AI" link (adds the section
as &element=), opening the assistant already aware of the report, period and
section — verified end-to-end. Because it lives in the shared template, every
engine report becomes report-aware, not just the Treasurer's Report.

**Treasurer's Report (reports/treasurer_report.py).** A comprehensive board report
composed ONLY from the Generic Report Engine, reusable components, the Metrics
Registry, ReportContext, the Narrative Engine and the Intelligence Platform — no
accounting calculation added or modified. Sections: AI executive briefing, financial
health score, KPI cards, income/fund charts, income & expense summaries + the
Income & Expenditure statement + income/expense narratives, Statement of Fund
Balances + cash position + fund-performance narrative, budget summary + variance,
trust-funds & development narratives, bank-reconciliation summary + outstanding
items, intelligence insights (explained) + board recommendations, a provenance
disclaimer and a signature block. Registered as treasurer_report, linked prominently
from the reports index, and rendering + exporting to CSV/Excel/PDF/Word (all verified).

**Intelligence report components (reports/intelligence_components.py).** Four
reusable components register with the component registry (category Intelligence) so
the report and the Report Designer can compose them: HealthScoreComponent,
InsightsComponent, RecommendationsComponent, AiBriefingComponent — each reading only
ReportContext / the intelligence layer. The AI executive briefing appears both in
the report and in the assistant (ask "brief the board" / "executive briefing").

**Validation.** Every reported figure originates from the Metrics Registry (the
report's declared metrics are all registered — tested); no duplicate calculations;
charts and narratives are metric-sourced; recommendations come from the Intelligence
Engine; and the assistant answers contextual questions without recalculating
(structured knowledge answers when the LLM is off; a no-invention system prompt when
on).

**Backward compatibility.** No existing report, view, template, export, URL,
permission, snapshot, definition or the classic assistant changed; the phase is
additive code + templates only. NO migration.

**Docs.** docs/TREASURER_REPORT.md (extended assistant, report-aware persistent
context, Ask-AI plumbing, report composition, intelligence components, output
formats, validation). Three deferred added (#43 sticky TOC + collapse, #44 cover
page + per-format tuning, #45 in-chat transaction-level drill-down).

Tests: 20 new (knowledge context + structured answers + grounded no-invention,
assistant view/endpoint context routing + classic fallback, the four intelligence
components incl. AI-briefing deterministic fallback, Treasurer's Report renders +
all-format exports + Ask-AI affordances + figures-from-registry + permission,
backward compatibility incl. classic assistant). Regression across intelligence,
admin platform, metrics, reporting layer/components, report accuracy and the
assistant-context batch — all green.

## v2.33.0 - Financial Intelligence Platform (Phase 9)
Transforms the reporting system into a Financial Intelligence Platform: it
continuously analyses accounting data and produces structured, explainable
insights, recommendations, trends/forecasts, a health score and a unified
knowledge service for a future AI Treasurer. Everything reads only the Financial
Metrics Registry via the Semantic Reporting Layer — no accounting calculation is
duplicated, and no chatbot is built (the reusable knowledge backend is).

**Financial Intelligence Engine (core/intelligence/).** An Insight is a
first-class structured object (title, description, severity, category, confidence,
priority, supporting metrics/transactions, affected funds/departments, period,
suggested actions, status) carrying an Explanation — the reason it fired, the
metrics read, the thresholds exceeded (limit + actual), the accounting services
used, and the contributing transactions (Part 9: no black boxes). The engine runs
registered modules against one shared ReportContext, backfills provenance, and
returns insights sorted by priority/severity. Deterministic: an insight is a pure
function of (figures, config); every threshold lives in IntelligenceConfig.

**15 insight modules across the seven categories** — financial health (operating
deficit, low reserves), income (declining/exceptional income, income
concentration, trust remittances), expense (budget overruns, spending spikes),
fund (negative balances, dormant funds, development progress), cash (cash
shortage, unpresented instruments), operational (pending receipts, outstanding
approvals), asset & liability (loan position). Each reads only registry metrics.

**Recommendation Engine.** Turns insights into prioritised, de-duplicated,
dismissible recommendations, each carrying the insight's rationale, supporting
metrics and fingerprint — always explainable back to the metric that triggered it.
Dismissal is persisted with an audit trail (InsightStatus + InsightStatusHistory).

**Treasurer Workspace (/workspace/).** Presents intelligence, not raw reports:
Financial Health Score + band, Risk Score, high-priority insights with their
explanations, prioritised recommendations, health-indicator drill-down, category
grouping, upcoming schedules and recent snapshots. Insights can be acknowledged/
resolved/dismissed (audit-trailed); dismissed insights drop out.

**Financial Knowledge Service (Part 5, no chatbot).** For any concept (income,
expenditure, funds, cash, trust, budget, loans, position), knowledge_for assembles
the metric values + definitions, narrative, insights, recommendations, linked
reports, supporting snapshots and the metric->service dependency graph — all from
the existing architecture. full_briefing returns the health score + all insights +
recommendations + provenance + disclaimer: the single call an AI Treasurer or
executive summary consumes.

**Trend & Forecast Engine.** Deterministic monthly series, trend (direction/growth/
rolling average), year-on-year, and a transparent least-squares linear forecast —
clearly labelled a projection (is_projection, "(proj.)" labels) that never replaces
accounting figures.

**Financial Health Score.** Overall 0-100 from nine transparently-weighted
indicators (liquidity, budget performance, income diversity, expense control, fund
health, cash management, reconciliation discipline, outstanding obligations,
operational completeness); each exposes its figures, score, weight and
explanation, and the overall is their weighted average. No black-box scoring.

**Analytics APIs (JSON).** /api/analytics/insights, /health, /trend, /knowledge —
for future mobile/AI consumers, all consuming the Semantic Reporting Layer.

**Accounting integrity.** The intelligence layer computes no accounting figure of
its own; tests assert every metric an insight names is a registered metric and
that an insight's headline value equals the corresponding metric. Only insight
status is persisted (by fingerprint), never a figure.

**Backward compatibility.** No existing report, view, template, export, URL,
permission, snapshot or report definition changed; the platform is additive. One
migration (two status models). Existing reporting/dashboard tests still pass.

**Docs.** docs/INTELLIGENCE_PLATFORM.md (engine, insight lifecycle, recommendation
lifecycle, health score methodology, workspace, knowledge service, trend/forecast,
analytics APIs, explainability, accounting integrity). Three deferred added (#40
conversational AI Treasurer on the Knowledge Service, #41 seasonality-aware
forecasting, #42 persisted insight snapshots).

Tests: 32 new (insight generation + determinism + detection, explainability +
threshold recording + registry-metric-only sourcing, recommendations, health
scoring + weighted average, trend/forecast + projection labelling, knowledge
service + all concepts, workspace + analytics APIs, insight status persistence +
audit trail, accounting correctness). Regression across intelligence, reporting
layer, components, metrics, narrative, admin platform, statement migration,
report accuracy, dashboard and executive — all green.

## v2.32.0 - Report Administration Platform (Phase 8)
A configuration-driven reporting platform: administrators design report layouts,
manage templates, schedule generation, brand output, browse a report library and
monitor reporting health — without modifying application code. Built entirely on
the existing engine; accounting still flows only through the Financial Metrics
Registry via ReportContext.

**Report Designer.** A ReportDefinition model persists reports as data (JSON
section list naming registered components + params + LayoutMeta, plus filters,
page settings, permission, category). reports/services/designer.py compiles a
definition into an engine Report rendered through the identical pipeline —
namespaced def__<key> so it never clashes with code reports. A definition can
only ARRANGE registered components, never introduce a calculation. Validation
refuses unknown components, unknown/missing narrative keys and out-of-range widths
before a definition can be saved live. Create/duplicate/edit/enable/delete at
/reports/designer/; designed reports render at /reports/r/def__<key>/ with every
export format. Lazy registration (first request) — no startup DB query. The
editor is a JSON section editor with the full component palette + narrative-key
list surfaced (a drag-and-drop canvas can layer on the same persistence later).

**Branding & themes.** A ReportBranding model (church name, conference, region,
contact, logo, colours, fonts, header/footer, watermark, certification statement,
page size/orientation/numbering); one active at a time. renderers.resolve_branding
is the single place branding is read (falling back to SiteConfig, so behaviour is
unchanged until configured); the PDF and Word renderers stamp church name,
conference/region, header, certification and footer, and Word applies the primary
colour — so branding applies consistently across outputs.

**Scheduling.** A ReportSchedule model (daily/weekly/monthly/quarterly/yearly/
manual, period policy, formats, recipients, next/last run + status) + ScheduleRun
history. reports/services/scheduling.py renders a report HEADLESS for the policy
period and creates an immutable snapshot (building on the Phase 7 Snapshot
Foundation), recording the run and never raising (failures captured for retry).
run_due_schedules is the cron/worker entry point; "run now" executes manually.
Period policies (prev_month/prev_quarter/ytd/prev_year/all) resolve to concrete
ranges. The background worker itself is an operational step, not app code.

**Report Library.** /reports/library/ — the central entry point: every report
(code-defined and designed) by category, with search, tags, favourites (per
user), recently used (per user) and frequently used (all). Backed by
ReportFavourite and ReportUsage; usage (with render time) is recorded by the
engine view on every render, guarded so it never breaks a report.

**Snapshot versioning.** Snapshot history at /reports/snapshots/ (filterable);
compare two snapshots section-by-section on their immutable structured payloads
at /reports/snapshots/compare/<a>/<b>/. Definitions carry a template_version
bumped on each edit. Snapshots remain immutable after publication.

**Feature Adoption Dashboard.** /reports/adoption/ — registered metrics, engine
reports, components, narratives; renderer formats; component reuse; snapshot
coverage; report view counts + average render time; most-used reports; active
schedules + failed runs; open recommendations (parsed from recommendations.md);
remaining legacy reports.

**Security.** Editing (designer, schedules) requires treasurer role; library,
adoption and snapshot history are read-only under report access. Designed reports
carry their own permission enforced by the engine. Invalid configurations are
refused before saving. Django admin exposes definitions/schedules/runs/branding/
usage/favourites (snapshots + runs read-only).

**Backward compatibility.** No existing report, view, template, export, URL,
permission or snapshot changed; the platform is additive. One migration
(reports/0002 — six admin models). Existing engine reports verified to still
render unchanged.

**Docs.** docs/REPORT_ADMIN.md (designer, branding, scheduling, distribution,
library, versioning, adoption dashboard, security, performance). Recommendations
#27 (Report Designer) and #34 (snapshot scheduling) marked ADDRESSED; three
deferred added (#37 drag-and-drop canvas, #38 actual email distribution, #39
retention policy + background scheduler).

Tests: 27 new (designer compile/validate/refuse-invalid, component config,
scheduling execution + next-run + period policies + failure capture + due-run,
branding applied to renderers + single-active, library + favourites + usage,
adoption dashboard, snapshot history + compare, permissions, backward
compatibility). Regression across reporting layer, components, metrics, narrative,
migration, report accuracy and report views — all green.

## v2.31.0 - Complete Statement Migration, Consistency Audit & Snapshot Foundation (Phase 7)
Continues consolidating the reporting platform: migrates the remaining core
financial statements onto the Generic Report Engine, adds a cross-report
consistency audit, and lays the immutable report-snapshot foundation.

**Financial statements migrated (parallel-run; legacy views untouched).**
cash_flow_v2 (Statement of Cash Flows — operating/investing/financing; reconciles
opening + net change == closing fund cash, classification mirrors the legacy
StatementOfCashFlowsView), fund_balances_v2 (Statement of Fund Balances — per
fund opening/movement/closing split local vs trust, total == total closing cash),
budget_vs_actual_v2 (complete budget vs actual with variance; totals equal the
canonical budget service the legacy view uses). These join the Phase 6 migrations
(Income & Expenditure, Trial Balance, Financial Position summary, Board Report) —
ten engine reports now registered. Each uses ReportContext exclusively, only
registry metrics, reusable components/layouts/renderers and the narrative engine,
and renders in HTML/CSV/Excel/PDF/Word/Print.

**New metrics (registry now 26).** financing_activity, loan_retirement_income,
remittances_total — canonical implementations in loans.services.reporting and
reports.services.balances, so the Cash Flow Statement reads every figure from the
registry.

**Reporting Consistency Audit.** reports/consistency_reports.py + the
consistency_audit report: for a period, checks the identities that must hold
across every statement — trial balance balances, accounting equation holds, I&E
surplus == income − operating − capital, cash flow reconciles, fund balances tie
to closing cash, and dashboard tithe/income equal the report metrics. All figures
come from one ReportContext, so a failure means a genuine inconsistency, not
definitional drift. All checks pass on seeded data.

**Report Snapshot Foundation.** reports.models.ReportSnapshot + a snapshot
service capture any engine report as an immutable, versioned record: period,
generation timestamp + user, app/template/schema versions, structured payload,
provenance (filters, metrics used, component keys, services), and checksums.
Immutability is enforced (saving a finalised snapshot raises); verify_snapshot
detects drift by re-rendering and comparing. The canonical integrity anchor is a
deterministic payload checksum (structured figures), since only the payload and
CSV export are byte-stable — xlsx/docx/pdf embed timestamps/metadata, documented
and handled so no false drift is reported. Read-only Django admin. No scheduling;
no change to report behaviour. One new model + migration (reports.0001_initial).

**Dashboard reconciliation confirmed.** The executive dashboard already draws
income through core.metrics.income_credits (the definition total_income wraps),
so its figures reconcile with the reports by construction — verified by test. The
main DashboardView reads through ReportContext (Phase 6).

**Fix.** xlsx export sheet titles were already sanitised in Phase 6; the snapshot
service now records a deterministic payload checksum rather than volatile export
bytes, avoiding false drift detection for spreadsheet/PDF/Word exports.

**Backward compatibility.** No existing report, view, template, export, URL or
permission changed; migrated reports run alongside the legacy ones. Legacy
retirement is staged behind human validation (nothing deleted this phase) — see
docs/REPORT_MIGRATION_STATUS.md. One database migration (the snapshot model).

**Docs.** docs/REPORT_MIGRATION_STATUS.md (migration status + legacy retirement
plan + remaining reports), docs/SNAPSHOT_FOUNDATION.md (snapshot architecture),
docs/METRICS_ADOPTION.md updated (26 metrics; Phase 7 status), and three deferred
recommendations (#34 snapshot scheduling/retention, #35 deterministic export
checksums, #36 combined statements bundle). Recommendations #26 and #30/#31
updated.

Tests: 19 new (cash-flow reconciliation & metric-only sourcing, fund-balances
total tie-out, budget-vs-actual equivalence with the canonical service,
consistency audit all-checks-pass + trial-balance-balances, snapshot
create/immutability/verify/drift-detection/deterministic-checksum, migrated-report
permissions & filters, dashboard reconciliation). Regression across reporting
layer, components, metrics, narrative, report accuracy, report views, position
reports, dashboard, executive and ledger — all green.

## v2.30.0 - Financial Narrative Engine & Report Migration (Phase 6)
A reusable Financial Narrative Engine plus migration of the core financial
statements and the Board Report onto the Generic Report Engine — every figure
from the Financial Metrics Registry, commentary generated from the same figures.

**Financial Narrative Engine (core/reporting/narrative.py + narrative_library.py).**
24 narratives that consume ONLY the Semantic Reporting Layer (ReportContext →
Metrics Registry): executive summary, financial highlights, income/expense
analysis, giving trends, budget performance/variance, fund performance,
restricted/trust funds, development projects, department performance, cash
position, bank reconciliation, outstanding items, asset/liability/loan position,
cash flow, financial risks, key changes, exceptions, warnings, recommendations.
No hardcoded values or accounting logic — a narrative asks the context for
registered metrics and renders words around them, so commentary can never
contradict the statements. Deterministic (a pure function of context figures +
config; verified byte-identical on re-run). Styles (executive/treasurer/auditor/
committee/detailed/concise) and tones (informational/analytical/formal/executive
summary) change phrasing, never numbers. Configurable Thresholds drive condition
detection (budget overruns, negative balances, cash shortages, inactive funds,
trust due, pending receipts, unpresented payments, large movements), surfaced as
structured Findings that power the warnings/exceptions/recommendations narratives
and are machine-readable for future AI. NarrativeEngine facade + narrative_registry.

**Narrative component.** NarrativeComponent folds any narrative into a report;
it draws from the same shared ReportContext as the tables, and its metrics flow
into the dependency map.

**New metrics (recommendation #23, addressed).** operating_expense,
capital_expenditure and a helper expense_by_category, with canonical
implementations in reports.services.balances proven equal to the legacy Income
Statement filters. Registry now has 23 metrics.

**Reports migrated (parallel-run; legacy views untouched).** income_statement_v2
(Statement of Financial Activity — recurrent/capital/operating/net-surplus proven
identical to the legacy IncomeStatementView), trial_balance_v2 (ledger trial
balance; balances by construction), financial_position_v2 (summary), and
board_report_v2 — the Board/Treasurer's Report rebuilt entirely from reusable
components + narratives (not copied from the legacy view). All use ReportContext
exclusively, only registry metrics, reusable components/layouts/renderers and the
narrative engine, and render in HTML/CSV/Excel/PDF/Word/Print.

**Dashboard migration begun (recommendation #24, partly addressed).** The main
DashboardView obtains its headline figures (fund summary, trust summary,
trust-to-remit, giving by group, income by channel, tithe) through a single
ReportContext. Figures are identical (services unchanged; metrics wrap them) and
now share the reports' memoized metrics — a dashboard figure equals the report
figure by construction (verified by reconciliation test).

**Accounting validation.** Income & Expenditure migrated figures equal legacy;
trial balance balances; dashboard tithe/trust-to-remit equal the registry
metrics; narrative figures equal the context's metric values. No migrated report
introduces a new accounting calculation.

**Fix.** xlsx export sheet titles are now sanitised (report titles containing
"/" such as "Board / Treasurer's Report" previously broke the Excel export).

**Backward compatibility.** No existing report, view, template, export, URL or
permission changed; migrated reports run alongside the legacy ones. No database
migrations.

**Docs.** docs/NARRATIVE_ENGINE.md (engine architecture, narrative lifecycle,
migration strategy, component reuse map, remaining legacy reports, accounting
validation), docs/METRICS_ADOPTION.md updated (23 metrics; narrative + migration
status), and four deferred recommendations (#30 remaining report migrations, #31
executive/leader dashboards, #32 retiring legacy views, #33 narrative
localisation) recorded in docs/recommendations.md.

Tests: 19 new (narrative determinism/provenance/style/detection/thresholds,
Income Statement figure-equivalence, trial-balance-balances, migrated-report
exports in every format, narrative-component integration and dependency
provenance). Regression across narrative, components, reporting layer, metrics,
dashboard, executive, report accuracy, report views, position reports and ledger
— all green.

## v2.29.0 - Report Component Library, Chart Engine & Rendering Framework
A reusable, component-based reporting layer built on the v2.28 Generic Report
Engine and the Semantic Reporting Layer. No existing report is redesigned or
migrated — this is the machinery the Board Report (next phase) and future reports
will compose from.

**Component Library (16 reusable components).** Each is a ComponentSection that
draws figures ONLY from a ReportContext (the Semantic Reporting Layer → Metrics
Registry), carries LayoutMeta, and records the metrics it consumed. Components:
KPI cards, executive summary (auto-generated), fund summary (drill-down to
ledgers), income summary, expenditure summary, budget vs actual (self-hiding when
no budgets), cash position, financial statement, bank reconciliation summary,
outstanding items, variance analysis (vs prior period), chart, commentary,
signature block, appendix, info panel. Registered in a ComponentRegistry so
reports compose by name and future modules add components by registration, never
by editing the engine.

**Chart Engine.** A chart is a metric-driven ChartSpec (never queries the DB);
to_config() yields a Chart.js config. Generic builders: line, bar/stacked,
doughnut/pie, waterfall (stacked-bar emulation), comparison, gauge. Metric-driven
builders: income_by_channel, fund_closing_balances, local_vs_trust. The
ChartComponent wraps any spec builder, exercising the engine's kind="chart"
sections (recommendation #25, addressed).

**Rendering Framework (6 formats).** Components produce format-agnostic
SectionData; renderers turn a RenderedReport into a medium — HTML, CSV, Excel,
PDF (ReportLab), Word (Word-compatible HTML, the app's existing approach, no new
dependency), and Print. Registered in a RendererRegistry; format is orthogonal to
components (a new format is a new renderer, components unchanged). Every renderer
honours each component's LayoutMeta export/print visibility uniformly. CSV/Excel
reuse the existing reports.exports helpers.

**Financial Dependency Map.** Derived from an actual render (never
hand-maintained): traces each component to the metrics it consumed and, via the
registry's authoritative metadata, to the accounting services behind them.
Exposes all_metrics/all_services, a reverse metric->components index for impact
analysis, and a JSON endpoint (?deps=json). Static impact_of_metric() answers
"which reports break if this metric changes?" without rendering.

**Layout metadata & the future Report Designer.** LayoutMeta is a complete,
serialisable placement model (width on a 12-col grid, order, priority, group,
collapse, responsive, print/export visibility, page-break). The renderers honour
it now; a future drag-and-drop Report Designer can read/write it (as_dict/
from_dict) so a report becomes data, not code. This phase builds the model, not
the UI.

**Demonstration + catalogue.** A board_pack_demo report composes 13 components
(with charts, KPIs, executive summary, signature block) end to end, rendering in
all six formats. A component catalogue at /reports/components/ lists the library,
the engine reports and the render formats. The v2.28 fund_overview demo still
renders unchanged through the extended template.

**Extension points.** New metric -> core/metrics.py; new component -> subclass +
register; new chart -> ChartEngine builder; new format -> Renderer + register;
new report -> compose components. Each is registration, not modification.

**Backward compatibility.** No existing report, view, template, export, URL or
permission changed. No database migrations (no models added). The generic
template gained new section kinds and a layout-aware grid; existing engine
reports render unchanged.

**Docs.** docs/COMPONENT_LIBRARY.md (architecture: component model, chart engine,
rendering framework, dependency map, layout/designer, extension points),
docs/METRICS_ADOPTION.md updated (0 new metrics needed — honest outcome; the 20
existing metrics covered the whole library), and three deferred enhancements
(#27 Report Designer UI, #28 server-side chart images for PDF/Word, #29 html
section kind) recorded in docs/recommendations.md.

Tests: 31 new (component registry + rendering, chart engine configs incl.
stacked/waterfall/gauge, all six renderers incl. export/print visibility, the
dependency map incl. reverse index and static impact analysis, and the
board_pack_demo end to end in every format). Regression across reporting layer,
metrics, report accuracy, report views and position reports — all green.

## v2.28.0 - Semantic Reporting Layer & Generic Report Engine
Two reusable foundations built on top of the Financial Metrics Registry (v2.27),
without redesigning any existing report. The Board Report will be the first
consumer, next phase.

**Semantic Reporting Layer (core/reporting/context.py).** A ReportContext is a
period- and scope-bound doorway to the Metrics Registry, memoized for the life
of one render. It is the sole interface new code should use to obtain financial
data: ctx.metric("tithe"), ctx.fund_summary(), etc. Every accessor resolves to a
registered metric (unknown names raise KeyError — no ad-hoc figures), period is
auto-applied to period-aware metrics, and results memoize per (metric, args) so
a report's sections share one computation. ctx.metrics_used() exposes provenance
for the adoption audit and future AI features.

**Generic Report Engine (core/reporting/engine.py).** Component-based reports:
Report (registered composition), Section (reusable unit that turns a
ReportContext into format-agnostic SectionData), Filter (declarative, typed),
ReportRegistry, and RenderedReport (exports via the existing reports.exports
helpers). Report.render() enforces the report's permission, resolves filters,
builds ONE shared ReportContext, and feeds it to every visible section — so
shared metrics compute once per report. Generic view (EngineReportView) +
template (engine_report.html) + route (/reports/r/<key>/) render any registered
report to HTML/CSV/Excel with drill-down, filters and section/report-level
permissions — no per-report view code.

**First engine report (reports/engine_reports.py).** A "Fund overview"
demonstration composed of three reusable sections (fund balances with drill-down
to fund ledgers, income by channel, trust still-to-remit), drawing every figure
from the registry. Proves the pipeline end to end without touching any existing
report.

**Recommendation #1 addressed (request-scoped memo).** core.perfcache now opens
a per-request memo (via RequestScopeMiddleware) so every aggregate flowing
through perfcache.cached() — department_summary, trust_summary, … — computes at
most once per request. This benefits the existing hand-written reports with no
change to their code: the Monthly Treasurer's Report drops from 133 to 120
queries per render (the eliminated calls were redundant same-period
department_summary recomputations; the remaining calls use genuinely different
periods and are correctly not deduped). A mid-request financial write clears the
memo (bump_data_version), so no stale figure is ever served within a request.

**Backward compatibility.** No existing URL, view, template, export or
permission changed. One new middleware (opens/closes a per-request dict). No
database migrations. The engine runs alongside the existing reporting system;
nothing is migrated this phase.

**Docs.** docs/REPORT_ENGINE.md (architecture, lifecycles, registry, rendering
pipeline, extension points, migration strategy, developer guide),
docs/METRICS_ADOPTION.md (adoption report), and four deferred enhancements
(#23–26) recorded in docs/recommendations.md.

Tests: 22 new (ReportContext memoization/period/provenance, request-scope memo
including write-then-read correctness and the department_summary dedup, engine
registration/render/permission/filters/section-visibility, and the fund_overview
report end to end with exports and drill-down). Regression across reports,
metrics, executive, dashboard, leaders and giving — all green.

## v2.27.0 - Calculation inventory & Financial Metrics Registry
A full inventory of every financial calculation in the system, and a single
authoritative home for them — the Semantic Reporting Layer. No business
behaviour changed: the only code edits are behaviour-preserving consolidations,
each proven equal to the idiom it replaced.

**Calculation inventory** (docs/CALCULATION_INVENTORY.md). Every financial
calculation across reports, dashboards, GL, cash book, reconciliation, giving,
envelopes, assets, expenses, budgets, statements, exports, charts, model
properties, managers, template tags and context processors was reviewed and
classified (unique / duplicate / intentional variant / cross-check /
report-specific). Key findings: the authoritative maths already lived in a clean
services layer; the real issues were a few named concepts recomputed inline in
dashboards and the assistant, and no single discoverable home for a definition.

**Financial Metrics Registry** (core/metrics.py + Reports → Financial metrics
registry). A self-documenting facade that re-exports the canonical
implementations under stable semantic names (metrics.tithe, metrics.fund_summary,
metrics.total_income, metrics.trust_to_remit, metrics.loans_outstanding, …). Each
metric carries its accounting definition and the dotted path of its
authoritative implementation, browsable at /reports/metrics/. It is a facade,
not a rewrite — every metric forwards to the existing service or is the single
shared implementation of a consolidated concept.

**Consolidations (behaviour-preserving):**
- Income-credit basis: dashboard._credits and assistant._credit_filter now
  delegate to core.metrics.income_credit_filter (they were identical).
- Tithe: the assistant reimplemented it twice inline — once WITHOUT
  excluded_from_income, a latent divergence — now both call metrics.tithe,
  which fixes the drift so the assistant matches every report.
- Trust still-to-remit: the repeated sum(r['to_remit'] …) idiom is now the
  single metric metrics.trust_to_remit.

**Intentional variants preserved (not merged):** receipts-by-fund (includes loan
cash) vs total-income (excludes it); balances._credit_filter (cash-position,
keeps loan cash) vs metrics.income_credit_filter (income-only); report
fund_balance vs ledger-derived fund_balance (kept as a cross-check); point
metrics vs monthly time-series. Each is documented with its reason.

**Compatibility:** all legacy service functions keep their signatures and
behaviour; existing reports, exports, APIs, dashboards and integrations continue
working unchanged. Migration is incremental — new code imports
`from core.metrics import metrics`.

Tests: 10 new (registry resolution, and equality tests proving each consolidated
metric returns exactly what the legacy idiom returned, plus the intentional
receipts-vs-income distinction). Regression across dashboard, assistant,
forecast, executive and report accuracy/views — all green. No migrations.

## v2.26.0 - Payment Register & payment-instrument lifecycle
The Payment Register becomes the single source of truth for payment
instruments, while expense vouchers remain the source documents that authorise
them. Complete instrument lifecycle, event-driven status, per-event dates, and
— critically — historical bank reconciliation judged on the CLEARED DATE, not
today's status.

**Lifecycle & audit.** A payment instrument now moves Draft → Approved →
Prepared → Issued → Presented → Cleared, with Cancelled / Rejected / Voided /
Reversed / Expired as terminal states. Every transition flows through one
audited service (apply_event): it moves the status, stamps that event's OWN
date field (dates never overwrite each other), and writes a PaymentEvent row
(user, business date, from/to status, reference, comment) — the timeline shown
inline on each register row. The current status is always the product of the
latest event.

**Cleared date (the critical fix).** Bank reconciliation now asks
`issued ≤ as-of AND not cleared/cancelled/voided/reversed by as-of`, judged on
the event dates via PaymentInstrument.outstanding_asof — never the current
status. A cheque issued 5 Jul that cleared 19 Jul correctly shows OUTSTANDING
on a 10 Jul reconciliation and CLEARED on a 31 Jul one, automatically, however
late the reconciliation is run. This now covers every bank-clearing method
(cheque, EFT, RTGS, M-Pesa), not just cheques. Legacy rows with no event dates
fall back to their status, so historical totals are preserved.

**Debit-queue integration.** Matching an imported bank debit to expense
voucher(s) now clears their outstanding instruments on the DEBIT'S date and
links the debit both ways — no duplicate records. The queue also suggests an
instrument per debit (number-in-narration, else unique exact amount) for
one-click clearing, and a new clear_instrument resolve action settles the
instrument and its source expense together. The same debit can never clear two
instruments.

**Register enhancements.** Search (instrument no, payee, expense no, bank ref,
amount), filters (status incl. outstanding-only, method, fund, bank, issue-date
range), whitelisted sorting, pagination, and CSV/Excel/print export. New
columns: source document (one-click to the voucher/batch), fund(s), cleared
date with a 🏦 marker when matched to an imported debit, expanded status pills,
and a per-row event history. Dashboard header: awaiting clearance (with
cheque/EFT/RTGS split), cleared today, average days to clear, oldest
outstanding, cancelled/voided count.

**Reports.** Outstanding report is now as-at-a-date (the reconciliation view of
the world) with days-outstanding. New Payment Analysis groups by fund / bank /
method / source over a period with cleared/outstanding/cancelled counts and
average & slowest days-to-clear. Both export.

**Source links & multi-instrument.** One EFT/RTGS can cover several vouchers
(comma-separated ids; the combined total must match); each instrument links
back to its expense, remittance batch, refund or transfer. Cancelled + re-issued
flow keeps the old instrument's full history and opens a replacement draft.

**Permissions (granular).** New rights view/manage/approve/clear/void_payments
(Treasurer full; Assistant create+clear; Auditor view-only). Department leaders
reaching the register are scoped to instruments on their own funds.

**Accounting integrity.** Instruments never post — the source voucher is the
accounting. Verified: the full lifecycle (issue → clear → reverse) creates zero
journal entries; trial balance, accounting equation, and every financial
statement are unchanged.

Tests: 27 new (full lifecycle & audit, the 5 Jul/19 Jul reconciliation case,
cancelled-date handling, EFT/M-Pesa as unpresented, legacy fallbacks, debit
auto-clear/one-click/no-duplicates, multi-expense EFT, loan repayment by
cheque, accounting invariance, granular permissions, leader scoping, search &
exports). Regression: cashbook, statements, giving, ledger, loans, reports,
rights/nav — all green.

Deploy: one migration (cashbook 0037: PaymentEvent, per-event dates,
bank_transaction link, extra_expenses, expanded statuses). No data backfill —
legacy rows use the status fallback.


## v2.25.0 - Liability transactions separated from the Expense Register
Operational expenses and balance-sheet liability settlements are now distinct
document classes with their own registers. Classification only - the posting
engine, every accounting entry, and all audit history are untouched.

**Document classification.** The voucher (Expense) model gains a high-level
`doc_class` (Receipt / Expense / Liability / Transfer / Journal / Adjustment -
the last four reserved for future document types). It is DERIVED from the
category on every save, so every creation path (forms, services, statement
imports, remittance batches, loan contras, the bulk recategorise tool) is
classified consistently with zero call-site changes. Built-in liability
categories: trust REMITTANCE and LOAN_REPAYMENT. Custom categories gain an
**is_liability** flag (Funds & setup -> categories, with one-click toggle that
refiles existing vouchers) - **new liability types (deposit refunds, advance
settlements, deferred income, supplier deposits...) need no code change.**

**Expense Register = operational only.** Loan repayments, loan conversions,
trust releases and other liability settlements no longer appear in the expense
list, its exports, or the pending-approval badge (they get their own badge).
Historical rows remain fully accessible - and approvable - through the
Liability Register, which links every row to its source voucher.

**Liability Transactions register** (Expenses -> Liability transactions,
/liabilities/): a unified, period-filtered register over the existing
documents - liability-class vouchers (trust releases, custom settlements),
every loan transaction (receipts remain on the bank/receipts ledger AND are
traceable here, per the borrowing trail), and trust fund receipts (via the
type filter, so routine tithe volume doesn't swamp the default view). Columns:
date, reference, transaction type, liability type, description, fund,
lender/beneficiary/trust, amount, effect (increase/settle), status, created
by. Search, filters (type/fund/status/period), pagination, CSV/Excel/print.
Dashboard header: outstanding loans, outstanding trust funds, advances
outstanding, this-month movement count and total, pending-approval alert.

**Query refactor.** The 25 scattered
`exclude(category__in=[REMITTANCE, LOAN_REPAYMENT])` sites across reports,
dashboards, forecasts and the assistant now filter on `doc_class` - one
concept instead of category lists, and future liability categories are
excluded from operating views automatically.

**Permissions & navigation.** New grantable rights `view_liabilities` and
`manage_liabilities` (Treasurer/Assistant by default; Auditor view-only);
approval reuses the existing expense-approval flow via the linked voucher.
Department leaders reaching the register are scoped to exactly their
allowed-department funds. The nav item appears only with the right.

**Migration.** Existing REMITTANCE and LOAN_REPAYMENT vouchers (and their
historical snapshots) reclassified via queryset update - no save() side
effects, no new history rows, no timestamps touched, no accounting change.

Verified end-to-end on live data: trial balance, accounting equation, balance
sheet, cash flow, fund/loan/trust balances all unchanged and reconciling;
loan balances tie to the Loans payable account. Tests: 19 new
(classification incl. custom categories and refiling, accounting invariance,
register content/filters/exports, permissions, leader scoping, badge split).
Regression: 623 tests across cashbook, loans, reports, ledger, leaders,
statements, giving, nav/rights/dashboard - all green.

Deploy: migrations 0035 (schema) + 0036 (reclassify). No manual steps.


## v2.24.0 - Loan reporting, financial-statement integration, petty cash & department visibility
Builds on the v2.23 Loan module. Ten report types, full financial-statement
integration, petty-cash receipt/repayment, and departmental-leader loan
visibility - all reusing the existing reporting, accounting, cashbook and
permission frameworks rather than duplicating them.

**Loan report catalogue** (all under Reports -> Loan reports, with the standard
date/fund/lender/status/type filters and CSV/Excel/print export, built on the
shared report mixins and export helper): Loan Liability Schedule, Outstanding
Loans, Loan Ageing, Maturity Schedule, Loans by Fund, Loans by Lender,
Repayment History, Interest Report, Converted/Written-off, and Cash Flow from
Financing Activities. Every figure is computed database-side from the same
LoanTransaction effectiveness the loan pages use, so the reports can never
disagree with the ledger. The Loan Liability Schedule total ties to the
LOANS_PAYABLE ledger account by construction.

**Financial-statement integration.**
- **Statement of Financial Position:** Loans payable now appears as a
  liability, split into current (<=12 months / on demand) and long-term
  (>12 months). Loan cash was already inside the cash asset figure, so
  recognising the matching liability is exactly what keeps the statement in
  balance - verified across all scenarios.
- **Statement of Cash Flows:** loan receipts and principal repayments are
  reclassified into Financing Activities (never operating); loan receipts are
  removed from operating cash receipts to avoid double-counting, and non-cash
  conversion/write-off income is removed too, so the statement reconciles.
- **Income & Expenditure:** unchanged behaviour confirmed - loan receipts
  never appear as income, principal repayments never as expenditure, only
  interest paid appears (as a finance cost).
- **Trial Balance / General Ledger / Chart of Accounts:** Loans payable (2300)
  posts and displays correctly; the trial balance and accounting equation
  balance after every scenario (receipt, partial/full repayment, conversion,
  write-off) - covered by tests.

**Fund & Development-group reporting.** Loan receipts on a Development-category
fund are now excluded from the unassigned dev-group queue, dev-group progress
and every dev-group figure (they carry excluded_from_income and are not member
contributions). Fund reporting continues to treat loan money as financing, not
income or contributions.

**Petty cash (sections 5 & 6).** Loan receipts can now be received into the
**petty cash float** (raises the float via the existing PettyCashTopUp
mechanism, linked to the loan transaction) as well as the bank; repayments can
be **paid from petty cash** (reduces the float via the existing
paid_from_petty_cash expense flag). The ledger posting is unchanged (bank and
petty share the single CASH account); only the cash-location control total
differs. Fund balances reconcile in both cases.

**Departmental-leader loan visibility (section 7).** A new read-only
**Leader -> Loans** area shows only loans on the funds a leader leads, scoped by
exactly the same allowed-department set as the rest of the leader area
(departments_led_by), with a read-only loan statement per loan. The Loans menu
item appears **only** when the leader actually has a loan on one of their funds
- no empty pages. Loans on other funds are never shown, and detail access is
guarded server-side.

Tests: 23 new (report catalogue & exports, liability-ledger reconciliation,
balance-sheet balancing incl. current/long-term split, cash-flow financing
classification & reconciliation incl. the non-cash conversion case,
dev-group exclusion, petty-cash receipt/repayment/fund reconciliation, leader
visibility & conditional menu). Full loans suite: 75 tests, all green.
Regression: reports 103, ledger+cashbook+leaders 181, statements+giving+nav
179 - all green. Section-9 end-to-end check (all seven scenarios at once):
trial balance, accounting equation, balance sheet all balance; cash flow
reconciles; loan balances tie to Loan Payable; dev-group reports unaffected;
health check and fund-variance drilldown clean.

Deploy: one migration (loans: petty_topup link on LoanTransaction). No data
backfill needed.


## v2.23.0 - Loan Management module
A loan is a liability, never income. The module is deliberately built on the two existing
source-document types so the general ledger, fund balances, bank reconciliation and every
report tie out with **no new balance math and no loan-specific rebuild step**:

- **Loan receipt** = a bank/cash credit on the financed fund with `excluded_from_income`
  (cash in the fund, never income) which the ledger now posts **DR Cash / CR Loans payable
  (2300)** - the exact shape trust receipts already have.
- **Principal repayment** = an Expense with new category `LOAN_REPAYMENT`, posting
  **DR Loans payable / CR Cash** and excluded from the I&E operating view - the exact
  treatment trust `REMITTANCE` expenses already receive (a liability settlement is not
  expenditure). Interest is an ordinary expense (`LOAN_INTEREST`, in I&E).
- **Conversion to donation / write-off** = a contra PAIR dated on the day (an income credit
  plus a LOAN_REPAYMENT voucher of the same amount) netting to **DR Loans payable /
  CR Income with zero cash movement**; conversions attribute the lender's linked member so
  the gift appears on their statement. `/ledger/rebuild/` regenerates all of it unchanged.
- A loan's outstanding balance is always **computed from its transactions**, and each loan
  transaction is only *effective* while its underlying document still counts - reversing a
  bank credit or rejecting a voucher flows straight through to the loan balance.

**Lenders** are their own register (member, visitor, institution, another church) - never
assumed to be, and never auto-created as, a church Member. Resolution reuses the member
matcher's conservative shape (national ID > phone > unambiguous name > create). A
**Lender matching** page links lenders to members (with phone/name suggestions), creates
pre-filled members, and merges duplicates (loans repointed, absorbed record retired with an
audit trail). Exact phone/ID duplicates are blocked at the form.

**Bank intake** extends the allocation pipeline rather than adding a parser: configurable,
database-driven **loan narration patterns** (seeded with the standard aliases; same
normalisation and match-type semantics as allocation rules, cached, editable at
/loans/patterns/) run on each credit *before* ordinary allocation. A receipt pattern with a
fund is fully automatic (lender resolved, open loan on that fund extended or a new one
opened, receipt recorded); without a fund the row goes to the review queue - never a
guess - where a new **"Loan receipt" action** completes it while keeping the row on the
bank ledger for reconciliation. The file importer and the live CBS webhook share one
intake path. Loan credits are skipped by pledge matching.

Also: reusable **Funding source** field on expenses (Contribution/Loan/Grant/Advance/
Transfer/Refund/Other); loans dashboard (outstanding, overdue, maturing, by fund, largest
lenders, recent activity); register/detail/statement with CSV+Excel exports; a **Loan
financing block on the fund budget page** (financing received, outstanding balance, per-loan
table); attachments; permanent LN-YYYY-NNNN numbering; three new grantable rights
(view/manage/convert - conversion and write-off are treasurer-level, like approvals);
validations (repayment can never exceed outstanding, retired loans are read-only, loans
with transactions cannot be deleted); full django-simple-history on every loan model.

Tests: 52 new across four files (balances & validations, journal shapes incl. the
conversion contra netting and trial-balance/accounting-equation checks, importer/webhook
intake & dedup, views & role permissions). Regression: ledger+statements 141, reports+
cashbook 167, giving+pledges 116, core nav/rights/render 61 - all green.

Deploy: migrations (cashbook: funding_source + 2 categories; loans: new app + seeded
patterns/chart). After deploy, point the receipt patterns you want automated at their
fund in Loans -> Narration patterns; fund-less patterns route to the review queue.


## v2.22.0 - comprehensive Transactions page review
A full review of the Transactions page against seven distinct concerns, each traced to a concrete root
cause and fixed or implemented in full.

**Fixed (critical):**
- **The Excel/CSV export summed a reversed transaction and its reversal as if both were ordinary income.**
  A reversal keeps the same direction and a positive amount as its original by design (the ledger nets it to
  zero by not posting either side, not by inverting the stored sign) - so the export's Amount column
  double-counted a reversed transaction instead of netting to zero, exactly the reported bug. Fixed to negate
  a reversal's amount in the export, added an explicit "Entry status" column, and hardened the underlying
  grouping helper so a reversed/reversal transaction can never be silently combined with anything else (a
  correction entry, not a split sibling), even a manually-entered cash transaction with no bank identifier
  that would otherwise fall through to a looser reference-based match.
- **Split remained available after a contribution had already been receipted**, risking an already-issued
  receipt being silently invalidated by a later split. Hidden once `manual_receipt` or `processed_via_envelope`
  is set, with a matching server-side guard on the view itself (GET and POST) - the template hiding a button
  alone is never sufficient on its own.

**Added:**
- **A dynamic, informative confirmation before Reverse Selected or Send to Review execute** - the exact
  count and total amount about to be affected (e.g. "57 transactions, Total: KES 3,540,230"), not a vague
  "are you sure?". Verified end-to-end with a real browser that the dialog reflects the actual current
  selection and that dismissing it correctly blocks submission.
- **Consolidated per-row actions into a single "⋮" dropdown menu** (Edit, Delete, Split, Receipt, Reverse,
  Send to review, Audit history), decluttering what had grown into up to seven separate inline controls per
  row. Added two actions that didn't exist as per-row options before: Reverse (previously bulk-only) and
  Audit History, a new view surfacing django-simple-history data that was already being tracked but never
  shown to users for a single transaction.
- **CR/DR accessibility badges** alongside the existing colour-coding on debit/reversal amounts, so the
  distinction doesn't rely on colour perception alone.
- **Eight new filters**, added as a collapsible "More filters" section to keep the primary filter bar
  uncluttered: Transaction Type, Amount Range, Member, Bank Account, Imported By, Reversed Only, Receipted
  Only, Manual Receipt Only. ("Entered By" was deliberately not implemented - no existing field tracks who
  recorded a manual cash entry, unlike Imported By; documented as a follow-up needing a new field and
  migration, not a same-scale addition as the rest of this set.)
- **A Type column and a running balance.** The running balance is computed chronologically and scoped to
  whatever filters are currently applied (filtering to one fund shows that fund's own running balance, not
  the whole church's), correct regardless of the page's own display sort order (a new Newest first / Oldest
  first toggle) and correct across pagination boundaries - verified directly that the second page's balance
  correctly continues from the first page's closing balance rather than restarting from zero. Only ever
  queries the current page's rows plus one aggregate for everything before it, never the full unbounded
  history, so this stays cheap regardless of total transaction count.

Tests: 49 new across six files, covering every fix and addition individually plus their interactions
(pagination boundaries, filter combinations, CSRF-safe confirmations). Full regression: giving — 224 tests,
all green.

Deploy: no migration.

## v2.21.1 - CRITICAL: a locked-out user was locking out everyone at the same location
**One account's failed sign-in attempts could lock out every other user sharing the same network** — e.g.
everyone in the same church office, on the same Wi-Fi/router, all sharing one public IP address.

**Root cause.** `AXES_LOCKOUT_PARAMETERS` was set to `["username", "ip_address"]` — a *flat* list, which
django-axes treats as two independent conditions: locked out if the username alone crosses the failure
limit, **or** if the IP address alone does, checked separately. Since many users can share one IP in a
small-office deployment, one person mistyping their password enough times tripped the *IP-based* check on
its own — locking out that IP for every username attempting to sign in from it, regardless of whether their
own account had ever failed at all. The error message shown was django-axes' own literal default text
("Account locked: too many login attempts...") on its own bare, unstyled response page, confirming this was
axes' built-in lockout — not anything this application added deliberately.

**Fixed** by changing to the *combination* form, `[["username", "ip_address"]]` — nested, not flat — which
locks out only the specific (username, IP) pair that actually failed repeatedly. Verified end-to-end with two
different accounts attempting from the exact same client: after one account is locked out, the other signs in
normally. The failing account itself remains correctly locked from that network for the cooloff period (15
minutes), and can still be reached from a different network if needed.

**Also fixed** the lockout response itself: it now redirects back to the application's own sign-in page with
a clear message ("Too many failed sign-in attempts...") instead of showing django-axes' separate, bare
default page.

Tests: 5 new, exercising the actual reported scenario directly (two users, one shared IP, one intentionally
locked out) with django-axes deliberately re-enabled for the test (it's normally disabled during the test
suite to avoid interfering with other tests' rapid login calls). Regression: accounts + core — 366 tests,
all green.

Deploy: no migration. This takes effect immediately on restart; no other action needed.

## v2.21.0 - receipt PDF masonry layout, strict split-grouping, reversal display
Three fixes plus a written architecture recommendation (see below, no code shipped for that item).

**Improved:**
- **Compact receipt PDF now uses a masonry-style, content-sized layout** instead of a fixed grid — each
  item's box height is computed from its own content (an image gets a height proportional to its real aspect
  ratio, capped to sane bounds; a text/e-receipt note gets a height proportional to how many wrapped lines it
  actually needs), and each item is placed into whichever column currently has the most room. Short notes no
  longer reserve a large, mostly-empty box; tall images get the room they need without being cramped into a
  fixed cell. Verified the computed heights are genuinely content-proportional (not just visually) and that
  every item still fits fully within the page.

**Fixed:**
- **Removed a duplicated date-range filter** on the Expense Receipts page — the standard period-selector
  partial had accidentally been included twice.
- **Bulk Send to review combined unrelated entries that happened to share a reference and date.** A common
  free-text reference like "tithe" is often used by many different people on the same day — the bulk action
  was treating any of them as "the same split" and wrongly merging separate people's gifts into one entry.
  Added `Transaction.strict_split_siblings()`, which only groups by a genuine bank-assigned identifier (the
  core_ref split-suffix pattern, or an exact M-Pesa reference) — used by Send to review specifically. The
  existing, looser `split_siblings()` is untouched, since cash-entry deletion genuinely needs its reference-
  based fallback (a cash entry has no bank identifier to match on at all). Also confirmed — with a real
  browser test, not just the backend — that selecting a single entry and using the bulk action works
  correctly.
- **Reversal (contra) entries now display their amount in parentheses**, like a debit, e.g. `(500.00)`. A
  reversal keeps the same direction and a positive amount as its original by design (the ledger nets it to
  zero by not posting either side at all, not by inverting the sign) — but the transaction list showed both
  sides as identical positive figures, with no visual cue that they cancel out.

**Recommendation (no code — advisory only, as requested):** a dedicated Bank Statement Register with
line-level reconciliation against imported transactions. See the project notes for the full write-up:
what exists today, why a register would add real audit value beyond it, and two integration paths (a
standalone feature alongside the current importer, or extending the importer to feed both).

Tests: 4 new for the masonry layout, 11 new for strict grouping + reversal display. Targeted regression
(giving, statements, cashbook): 596 tests, all green.

Deploy: no migration.

## v2.20.1 - CSRF fix + bulk send-to-review, receipt PDF note content, ingest.py allocation fix
Three fixes to what shipped in v2.20.0.

**Fixed:**
- **"CSRF verification failed" when using Send to review.** The form I added for this action was missing
  `{% csrf_token %}` entirely — a real bug that Django's test client didn't catch, since it doesn't enforce
  CSRF by default. Re-verified this fix with `Client(enforce_csrf_checks=True)`, and swept every other POST
  form added this session to confirm none had the same gap.
- **Moved Send to review to the top toolbar as a bulk action**, alongside Reverse selected — select one or
  more entries with the existing checkboxes, then act on all of them at once. Selecting both halves of the
  same wrongly-split contribution now correctly combines them into exactly one replacement entry, not two
  (grouped by split family before processing, same underlying logic as the single-entry version this
  replaces).
- **The compact receipt PDF's placeholder for a text/e-receipt-note attachment only ever showed a generic
  label** ("No file — text/e-receipt note") describing that a note existed, never the note's actual content
  — useless for exactly the attachments it was meant to cover. Now renders the actual text or link, wrapped
  and truncated to fit. Found and fixed a real off-by-one while building this: the truncation line-count
  estimate didn't account for the label's own line height, so the line carrying the "..." truncation marker
  could be silently dropped by the drawing loop's own space check before it was ever drawn — confirmed by
  extracting actual text back out of a generated PDF, not just checking it rendered without error.
- **The development-group campaign fallback (v2.20.0) still wasn't taking effect** — because that fix only
  reached the file-upload importer and `reallocate_pending()`. Live bank transactions (the far more common
  path in practice) arrive through a separate ingestion module for the real-time bank webhook, which had the
  exact same bug and was never touched. Applied the identical fix there.

Tests: 3 new for the CSRF/bulk fix (with CSRF enforcement deliberately turned on), 5 new for the PDF note
content fix, 3 new for the webhook ingestion fix. Targeted regression (giving, statements, cashbook): 581
tests, all green.

Deploy: no migration.

## v2.20.0 - development-group fallback fix, send-to-review action, receipt PDF default-period fix
Three issues traced to their real root cause and fixed.

**Fixed:**
- **The campaign member->group fallback allocation never got a chance to run for Development Groups.**
  `allocate()` detects a dev-group *word* (e.g. "dev", "grp") without a specific number and immediately
  resolves to "Development, group unknown" (status AUTO) — a non-null result. Since the importer and
  `reallocate_pending()` only tried the campaign fallback (the mechanism that already worked for sub-accounts
  like Camp Expense) when nothing had resolved at all, a Development-focused campaign's member table never
  got the chance to identify the *specific* group from the giver's name/phone, even when configured exactly
  the same way Camp Expense is. Fixed to also try the campaign fallback specifically in this case, only ever
  preferring its result when it actually recognises the payer — never downgrading an already-resolved "AUTO"
  outcome to "REVIEW" just because a trigger word matched without a member match.
- **Expense Receipts downloads (PDF and ZIP) looked broken on a fresh visit.** The page had no date-range
  picker at all and silently defaulted to "this month" — often empty, since receipts accumulate over time —
  so clicking either download button just bounced back to the same empty page with no obvious explanation.
  Now defaults to "this year so far" when no period is specified, and gained the same date-range picker
  (with This month / This quarter / This year presets) used elsewhere in the app. The download links also
  now always carry the resolved date range, rather than only when the URL happened to already have one.

**Added:**
- **"Send to review" on the Transactions page** — the direct answer to "this was wrongly auto-split across
  funds, how do I put it back as one fund?" Reverses the entry (a contra posting, same as the existing
  Reverse action) and, if it's part of a split contribution, every sibling too, then creates one new entry
  for the full combined original amount in the review queue, ready to be correctly allocated as a single
  fund. Nothing is ever deleted — the original split rows stay on the ledger, reversed, for the audit trail.

Tests: 6 new for the development-group fallback fix, 11 for send-to-review, 8 for the receipt default-period
fix. Targeted regression (giving, statements, cashbook — the affected apps): 573 tests, all green.

Deploy: no migration.

## v2.19.0 - self-service password reset, Elder nav simplification, font-inheritance fix
Three follow-ups from live use.

**Added:**
- **Self-service password reset.** A "Forgot your password?" link on the sign-in page starts a flow that
  uses whichever contact channel is on file and actually working: a 6-digit SMS code (if a phone number is
  on record and SMS sending is configured — reusing the existing Advanta SMS integration) or an emailed
  reset link (Django's own well-tested token mechanism, if an email is on record and real SMTP is
  configured — this app's email degrades to a harmless console/no-op backend otherwise, same as its other
  outbound email). The response is always the same regardless of whether the account exists or which
  channel it has — no way to tell from outside. SMS codes are single-use, expire in 10 minutes, stored
  hashed (never in plaintext), and requesting a new one invalidates any earlier pending one. Reset requests
  are rate-limited per account (max 3 within 15 minutes) to stop one account's phone being SMS-bombed.

**Fixed:**
- **A real bug found while building the above:** messages set via Django's messages framework before
  redirecting to the sign-in page were never displayed — the sign-in page's layout doesn't share the
  authenticated-area template section where messages normally render. Fixed by adding the same message
  rendering to the sign-in page directly; this fixes any future code that redirects to login with a message,
  not just this feature.
- **The Elder role's navigation was over-complicated.** Removed the standalone "Elder dashboard (preview)"
  link added under the treasurer-only Administration section in an earlier release — not needed. An elder's
  own primary navigation item is now simply labelled "Home", exactly matching what every other role sees for
  their own landing page (the underlying URL and its "redirect logic" are unchanged).
- **The user admin page's tabs (and, found alongside it, the same pattern on the Settings page) rendered in
  the browser's default UI font** (typically Arial) instead of the application's configured font, and didn't
  respond to the user's font-size/font-family appearance preference the way the rest of the page does.
  Root cause: browsers don't inherit font styling into `<button>` elements by default, and the tab buttons'
  CSS was missing the explicit `font: inherit` declaration every other interactive element in this app
  already includes. Confirmed with real browser screenshots and computed-style checks before and after —
  the tab font now matches the surrounding page exactly, in every font/size combination.

Tests: 18 new for the reset flow (`accounts.test_self_password_reset`), 3 for the font fix, plus updated
coverage for the simplified Elder navigation. Targeted regression, as requested (accounts + core only):
361 tests, all green.

Deploy: migrate (accounts 0007 — adds PasswordResetCode, no existing data affected). For SMS reset codes to
actually send, SMS must already be configured in Settings (as it is for receipts); for email reset links to
actually deliver, set `DJANGO_EMAIL_HOST` (and related `DJANGO_EMAIL_*` variables) in the environment —
without it, the app safely no-ops rather than erroring, exactly as it already does for its other email.

## v2.18.2 - user edit page layout fix + Elder dashboard nav discoverability
Two follow-ups reported directly from live use of v2.18.0/2.17.0.

**Fixed:**
- **The user admin page (`/users/<id>/edit/`) looked visibly cramped.** Every section had been given the
  `u-narrow` CSS class (max-width: 560px) — appropriate for the profile edit form, but wrong for the wide
  account-status stat grid, the administrative-actions grid, and the audit/activity tables, which all
  rendered squeezed into a narrow column regardless of the page's actual available width. Confirmed visually
  with real browser screenshots (Playwright + a live server) before and after — the fix removes `u-narrow`
  from every section except the two genuine forms (profile details, role & rights), matching the same
  convention this app's other tabbed page (`settings.html`) already uses: plain, full-width cards for
  form-grids, not narrow ones.
- **The Elder dashboard had no discoverable link for staff.** `ElderRequiredMixin` already allowed a
  treasurer (or any staff role) to open `/elder/` directly, explicitly for setup and troubleshooting — but
  nothing in the navigation let anyone find it without typing the URL from memory, since a treasurer's own
  nav correctly shows their own Home, not "Elder dashboard" (they aren't one). Added a clearly-labelled
  "Elder dashboard (preview)" link under the treasurer-only Administration nav group, alongside Users & roles
  and Profiles & rights. A real elder's own primary nav item is completely unchanged.

Tests: 9 new (4 in `accounts.test_user_management` for the layout fix, 5 in `core.test_elder_role` for the
nav fix). Targeted regression (the two affected apps only, per this request): accounts + core — 340 tests,
all green.

Deploy: no migration. Collectstatic recommended (template/CSS changes only) but not required for correctness.

## v2.18.1 - user list N+1 fix
Follow-up performance check after v2.18.0's User Management rework, prompted by the pattern of this
project's earlier performance reviews: verify a new list page doesn't quietly reintroduce a per-row query.

**Fixed:**
- The user list's role column called `user_roles(u)` per user, which calls `user.groups.values_list(...)` —
  a call that always issues a fresh query, completely bypassing `prefetch_related("groups")` (already present
  on the queryset) since `values_list()` returns a new queryset rather than reading the prefetched cache.
  Confirmed via query-count testing: the page was issuing one additional query per user shown (46 queries for
  a page of ~30 users), though bounded by pagination rather than growing unbounded. Fixed by building the
  roles column from the prefetched relation directly in the view, without changing the shared `user_roles()`
  utility (used throughout the rest of the application) at all. Verified byte-for-byte identical output
  against the original per-user computation across every account in the database before considering it safe.
  Query count: ~46 → ~21 for the same page.

Tests: 2 new, asserting both the query-count bound and that the displayed roles are unchanged. Full
regression: accounts 97 — all green.

Deploy: no migration.

## v2.18.0 - User Management module: profiles, account lifecycle, security dashboard, audit trail
A comprehensive rework of `/users/`, reviewed as a Treasurer, System Administrator, Security Administrator,
and Auditor would each use it.

**Added — User Profile Management:**
- A `UserProfile` extension (phone, gender, position, department/ministry, church assignment, internal
  notes) alongside Django's own name/email fields, all editable from one Profile tab.
- Account record: creation date, who created the account, and when the profile was last updated.

**Added — Account Management:**
- **Suspend / reinstate** (`UserProfile.locked`) — a short-term, easily-reversible block distinct from
  deactivation: ends any active session immediately (a new `AccountLockMiddleware`) and rejects both fresh
  and in-progress logins with a clear message, without touching the account's role or settings.
- **Admin password reset** — sets a new password directly (shown once, since this deployment has no
  outbound email — documented in `docs/recommendations.md`), optionally forcing the user to set their own
  password on next login (`ForcePasswordChangeMiddleware`, modelled on the existing 2FA enforcement
  middleware). Resetting a password also invalidates the user's other active sessions automatically — a
  free, welcome side effect of Django's own session-security design, not something this release had to build.
- **Disable two-factor authentication** for a user who's lost their device — a genuine admin-facing version
  of what previously only existed as a backend management command.
- **Clear a failed-login lockout** (django-axes) without resetting the password.
- **Force logout everywhere** — ends every active session for an account by decoding and clearing the
  matching `django.contrib.sessions` rows.
- **Clone an account** — role, led departments, and rights profiles copied into a brand new account;
  credentials are never copied, and the new account is forced to set its own password.
- Deliberately **not implemented**, with reasoning recorded in `docs/recommendations.md`: security questions
  (a deprecated pattern), password-reset emails (no email backend configured), and a separate "archive"
  concept (deactivation already preserves full history, with nothing further to add).

**Added — Roles & Permissions:**
- The Roles & Rights tab shows the account's role, any assigned rights profiles, and its full effective
  permission set in one place.
- **Self-permission-modification is now blocked entirely**: an administrator can no longer change their own
  role, active/suspended status, password, two-factor enrolment, or sessions from this module — every one of
  those actions now requires a *different* administrator. (One pre-existing test predated this rule and
  exercised exactly the scenario it now blocks; updated to reflect the stricter, intentional behaviour.)

**Added — Activity & Security Dashboard, and Audit Trail:**
- A per-user Security tab: account/lock status, 2FA status and method, password-last-changed, forced-
  change flag, active session count, last successful login and IP, failed-login count and last failed
  attempt (from django-axes), and lockout status.
- A dedicated `UserAdminLogEntry` audit trail — distinct from the generic `django-simple-history` field-change
  log — purpose-built to answer "who did what to whose account, and when": account creation, profile edits,
  role changes, activation/deactivation, lock/unlock, password resets, forced-change flags, 2FA disablement,
  lockout clears, session termination, and cloning. Shown in full on the Audit Log tab and summarised on
  Activity.

**Added — User Interface:**
- The user list gained search (username/name/email/phone), filtering (role, status), sorting, and
  pagination, plus 2FA and status columns at a glance.
- The user detail page is now a tabbed interface (Profile / Security / Roles & Rights / Activity / Audit
  Log), each tab's form independent of the others so saving one never touches another.

Tests: 48 new across three files (`test_user_management`, `test_user_admin_actions`, `test_user_list_search`),
covering profile edits, every account-lifecycle action, every self-permission-modification block, password
administration, 2FA administration, session termination, cloning, and list search/filter/sort — each
exercising both the successful path and its audit trail entry. One pre-existing test updated for the new
self-edit rule. Full regression: accounts 95, core+leaders 292 — all green.

Deploy: migrate (accounts 0006 — adds UserProfile and UserAdminLogEntry, no data affected).

## v2.17.0 - split export fix, compact receipt PDF, budget JPEG, leader budget access, Elder role
Five features/fixes, plus a serious access-control regression caught and fixed during review.

**Fixed:**
- **Trust Fund Pending Receipts export showed half a split gift's amount.** The export filtered to
  `fund_type=TRUST` *before* grouping a split contribution's siblings — so a "Combined Offering" gift split
  50% trust / 50% local only ever showed the trust-side partial amount (a 40 gift showed as 20). Fixed to
  group first, then include the whole group whenever *any* sibling is a trust credit, showing the full
  original amount — a giver's receipt should cover their whole gift, not the portion that happened to land
  in a trust account. A purely local split still correctly shows nothing (no trust concern); a group is only
  excluded once *every* sibling is already receipted.
- **Critical: department leaders could view every financial report church-wide.** While wiring up the new
  Elder role's assignable "view_reports" right, testing surfaced that `Department Leader`'s default rights
  already (silently, unused until now) included `view_reports` — dormant until the reports views started
  consulting it, at which point every leader could suddenly reach `/reports/`, `/reports/board/`, and every
  other report, not just their own department's leader-scoped views. Removed `view_reports` from a leader's
  default rights; nothing else depended on it.
- Fixed an inconsistency found alongside it: an Elder visiting `/leader/` got a bare 403 instead of the same
  friendly redirect every other blocked page gives.

**Added:**
- **Compact receipt PDF** for `/expenses/receipts/` — several receipt thumbnails per page in a grid (like a
  contact sheet), generated server-side with reportlab/Pillow rather than relying on the browser's
  print-to-PDF, whose page count and layout vary unpredictably by browser and OS. Text-only and e-receipt-link
  attachments get a labelled placeholder cell rather than being silently dropped. Caught and fixed a real
  pagination bug during development: placing the last item that exactly filled a page's grid eagerly started
  a new page, leaving a pointless blank trailing page — fixed to only start a new page lazily, right before
  the next item that actually needs it.
- **Budget vs Actual JPEG** on a fund's budget page (`/reports/fund/<id>/budget/`) — a downloadable table
  image (Budget item / Budget / Actual / Variance / Used, plus totals), matching the on-screen table exactly,
  using the same server-side Pillow table-rendering approach as the existing Group Contribution Goals JPEG
  (which was itself converted from a bar-chart to a proper table this release, for the same reason).
- **Leaders can be granted fund-budget access.** A new assignable `view_fund_budget` right lets a treasurer
  opt a specific leader into read-only access to a fund's budget page — only for a fund they actually lead,
  never bundled into the base Leader role by default. Editing a budget always stays treasurer/assistant only.
  The leader dashboard shows a "budget →" link only where this applies.
- **New Elder role.** A read-only, board-level role distinct from both office staff and departmental leaders.
  Elders get their own simple dashboard (a handful of headline figures plus a link to the executive overview)
  and the executive overview itself by default. Full reports access is a separately assignable right a
  treasurer can grant to a specific elder — not switched on for every elder automatically.

Tests: 43 new across cashbook/giving/core, verifying both the fixes (including the caught access-control
regression, reproduced and confirmed fixed) and the new features. Full regression run across the entire
application: core+leaders+accounts 338, reports 251, cashbook 318, giving+statements+envelopes 292,
assets+departments+members+pledges+ledger 188 — all green.

Deploy: migrate (accounts 0005 — seeds the "Elder (default)" profile). Collectstatic not required.

## v2.16.0 - advance/cash-count follow-up fixes, transactions export, ledger health, executive redesign
Follow-up to v2.15.0, tracing each report through to its real root cause before fixing.

**Fixed:**
- **Bank reconciliation "Staff advances from petty cash (not yet accounted)"** never actually decreased as
  advances were accounted for — `StaffAdvance.petty_outstanding_asof()` only ever subtracted returns to the
  float, never expenses recorded against the advance, so it permanently showed the full amount ever
  disbursed. Fixed to also subtract settled expenses, as of the reconciliation's own statement date.
- **Sabbath cash count double-counted staff advance settlements.** When someone accounts for a cash-tracked
  advance (an expense linked via `Expense.advance`), the real cash movement happened back when the advance
  was *issued* — possibly from an entirely different float (petty cash) — not when it's later accounted for.
  The cash count was counting the settlement as a brand-new disbursement from the Sabbath offering float.
  Fixed by excluding advance-settlement expenses from "Cash Disbursed" and instead adding back the advance's
  own issuance (only when cash, only when *not* from petty cash) at the point it actually happened.
  **Caught and fixed a regression from the first fix**, before shipping: the petty cash float's own running
  balance (`_petty_balance_asof`) depended on the same method, and would have incorrectly *increased* every
  time an advance was accounted for (settling an advance doesn't return cash to the float). Split into two
  clearly-named methods — `petty_cash_out_asof` (pure cash movement, for the float balance) and
  `petty_outstanding_asof` (accounted-for status, for reconciliation) — with a regression test locking in
  that settling an advance never changes the float's balance.
- **Transactions Excel/CSV export** now combines a contribution split across several funds back into one row
  with its full aggregate total, the way it looked before the split — matching the Trust Pending Receipts
  export's existing behaviour. Extracted the grouping logic into a shared helper used by both exports.
- **Ledger Health "Shared M-Pesa/bank receipt references"** flagged every legitimate split gift as "worth
  checking" — it compared `core_ref` values directly, but a split gives each sibling its *own* distinct
  core_ref (base + "-S1", "-S2", ...), never a shared one. Fixed to compare the base reference; the health
  page now separates the count into genuinely-unexplained references and legitimate splits (labelled and no
  longer counted as a concern).
- **Missing postings persisted after every rebuild.** A reversed transaction or a reversal's own contra-entry
  are never posted by design (`post_transaction()` and `rebuild()` both correctly decline), but the health
  check was flagging them as "missing" regardless — a false positive that no rebuild could ever clear. Fixed
  by excluding them from the check. Each remaining genuinely-missing item now links directly to its record.

**Redesigned:**
- **Executive overview** reorganised into clearly labelled sections (Performance this year / Giving
  breakdown / At a glance / Cash position & forecast / Trends) for easier scanning, with a consistent section
  heading style. Verified line-by-line against the original: every context variable, URL, chart canvas ID,
  and literal text label is preserved exactly — this is a presentation reorganisation, not a functional
  change. AI insights and all six charts work exactly as before.

Tests: 34 new across cashbook/envelopes/giving/ledger/core, covering each fix (including the caught-and-fixed
regression) with both the buggy and corrected scenarios reproduced directly. Targeted regression: cashbook
286, envelopes+giving 208, ledger+core+statements 340 — all green.

Deploy: no migration. Collectstatic recommended (executive.html CSS changed) but not required for correctness.

## v2.15.0 - ledger rebuild fix, cash count fix, transactions page improvements, export enhancements
Traced and resolved a batch of reported issues, each verified against a reproduction of the real scenario
(not assumed), plus one further defect found incidentally while running the regression suite.

**Fixed (Critical):**
- **`/ledger/rebuild/` server error** — `pymysql.err.IntegrityError: Duplicate entry '5105' for key 'code'`.
  Root cause: `ensure_chart()` assigned each expense category's account code from its *position* in
  `Expense.Category.choices` (`EXPENSE_BASE + enumerate index`). An earlier release inserted two new
  categories (Salaries/Wages, Lease Payment) into the middle of that list, shifting the positional index —
  and therefore the computed code — of every category listed after them. On any database that already had
  its chart of accounts built before that release, `UTILITIES` already held code 5105 on disk; the newly-
  computed code for `SALARIES` was *also* 5105 (its new position), so creating it collided. Reproduced the
  exact historical database state and confirmed the fix resolves it: codes are now assigned as "one past the
  highest code already on record", never recomputed from list position — immune to future reordering.

**Fixed:**
- **Sabbath Cash Count — Cash Disbursed** included expenses paid from the separate petty cash float
  (`Expense.paid_from_petty_cash`), money that never came out of the Sabbath offering cash box being counted.
  This silently understated "expected cash on hand" and made counts show discrepancies that weren't real.
  Now excluded.
- **Monthly Treasurer's Report — Collections insight** silently never generated (a `KeyError` on a wrong
  dict key, caught by an intentional "an optional narrative must never break the report" safeguard, so it
  never surfaced as an error — just a permanently missing paragraph). Found while running an unrelated
  regression suite. Fixed; the year-over-year collections commentary now appears correctly.

**Added:**
- **M-Pesa Reference column** on the Trust Fund Pending Receipts export (`/transactions/?export=trust-
  pending-receipt`).
- **Payment Method column** on the Expenses export (Cash / Bank / Petty Cash / Cheque / Mobile Money),
  using the actual recorded payment source (`Expense.method` plus the separate `paid_from_petty_cash` flag)
  — never inferred.
- **Quick-filter tabs** on the Transactions page (All / Needs review / Unallocated / Trust pending receipt),
  layered on top of the existing, already-tested filter mechanism without changing it — a safe, additive
  usability improvement rather than a full page rewrite, given how intricately the existing permission and
  status logic on this page is built. Caught and fixed a self-introduced duplicate-button/over-restrictive-
  permission bug while building this, by testing as an auditor as well as a treasurer before shipping.

**Reviewed, confirmed already correct (no change made):** the Staff Advances figure used during bank
reconciliation. Traced the full calculation chain (`_sync_managed_recon_items` ->
`outstanding_bank_advances_total`/`outstanding_petty_advances_total`) and confirmed it already computes
exactly "amount advanced minus expenses already settled against it, as of the reconciliation's own statement
date" — the Pending/Not-Accounted-For balance — cross-checked against the Statement of Financial Position
and dashboard, which use the same underlying concept. No competing or incorrect figure found anywhere.

Tests: 27 new across ledger/envelopes/giving/cashbook/reports/statements, 1 pre-existing test's header
assertion updated for the new export column. Targeted regression: ledger 55, envelopes 62, giving 136,
cashbook 278, reports 251, statements 77 — all green.

Deploy: no migration. Collectstatic recommended (new inline CSS for the quick-filter tabs).

## v2.14.0 - testing strategy review
Reviewed unit/integration test coverage, assertion quality, and long-term test reliability across the
application (135 test files, ~1,300 individual tests). No functional changes — this pass strengthens the
test suite itself.

**Added:**
- **A guardrail against test time-bombs.** An earlier review found and fixed a real bug class: `Pledge.
  start_date` defaults to `date.today()` (a moving target), and a test hardcoded an absolute contribution
  date that needed to fall within a window relative to it — the test passed when written and silently broke
  months later purely because real time had moved on, with no code change at all. This review added an
  automated scanner (`core.test_testing_review`) that inventories every `DateField`/`DateTimeField` across
  every app with this exact shape (a callable default, not `auto_now_add`) and fails if a new one appears
  without deliberate review. Verified it actually works: reintroduced a copy of the bug pattern in a
  throwaway model, confirmed the guardrail caught it with a clear, actionable failure message, then reverted.
- Confirmed, via the same scan, that `pledges/models.py` is the *only* file in the codebase with this
  pattern today, and that the only tests exercising the date-window-sensitive matching logic already pin
  their dates correctly (from the earlier fix) — no further live instances of the bug found.

**Assessed (no changes needed):** spot-checked the ~163 bare `status_code == 200` assertions across the test
suite for the "weak assertion" pattern flagged by this kind of review — found them to be legitimate in every
file sampled: either access-control tests where the status code itself *is* the meaningful assertion (a 200
vs. a 302 correctly proves who can and can't reach a page), or a "did it render" precondition followed by
real business-logic assertions on the same response. No systemic weak-assertion problem found.

**Recorded in `docs/recommendations.md`:** no CI/CD pipeline or code-coverage tooling exists (Medium-High —
arguably the highest-leverage testing investment available, converting every review's "run the tests" step
into an enforced gate rather than a manual habit); test files in `cashbook` (32 of them) mix feature-named and
version/session-named files, making coverage harder to locate by feature than necessary (Low); no genuine
concurrency/load testing exists, only careful code-review reasoning about concurrent-access risk (Low, given
current usage patterns).

Tests: 2 new (core.test_testing_review). Targeted regression: core 199, pledges+assets 45 — all green.

Deploy: no migration. Collectstatic not required.

## v2.13.0 - database integrity: transaction atomicity and referential protection
Reviewed schema design, foreign keys, cascade behaviour, and transaction handling across the whole
application. Two genuine risks found and fixed; further items recorded in `docs/recommendations.md`.

**Fixed:**
- **Splitting a contribution across several funds** (`Transaction.split_into()`) reduced the original entry's
  amount and then created the remaining sibling entries in separate, unwrapped database writes. A failure
  partway through — a constraint violation, a server restart — would leave the original entry permanently
  reduced with the remainder recorded nowhere, silently losing money from the books. Verified by forcing a
  real mid-split failure: before this fix, the original entry stayed wrongly reduced; after, it's fully
  restored to its original amount and fund.
- **Settling a trust remittance batch** (matching one bank payment to several trust funds' remittance lines)
  updated the batch, the expense lines, and the settling transaction in three separate, unwrapped writes.
  Now wrapped in one atomic block, so a failure partway through can't leave a batch marked "sent" with its
  expenses unpaid, or vice versa.
- **`DepartmentStatusLog`** (a fund's active/closed/archived history) and **`FundCarryForward`** (a fund's
  year-end closing-balance snapshot) both used `on_delete=CASCADE` on their department link, despite being
  audit-trail records by their own stated purpose. Changed to `PROTECT`, matching how `Transaction` and
  `Expense` already protect a fund from deletion — a department can't be deleted through any application view
  in the first place, so this only closes the narrow remaining gap (a fund with no financial activity left,
  but status or year-end history) rather than changing any current, working behaviour.

**Confirmed already correct (no changes needed):** every money-movement model's positivity validators (from
an earlier review), the dominant report-query indexes (from an earlier review), `DepartmentLeadership`'s
duplicate-prevention unique constraint, and `ATOMIC_REQUESTS` being intentionally off (checked to confirm no
other view silently relied on framework-level atomicity that isn't actually configured).

**Recorded in `docs/recommendations.md`:** two unrelated models are both named `BudgetLine` (one in
`departments`, one in `cashbook`) — confusing but not incorrect, and renaming either is an invasive,
multi-file migration better done as its own deliberate pass (Low priority).

Tests: 8 new (giving.test_atomicity, departments.test_referential_integrity) — including a test that forces
a real mid-operation failure and proves the database is left completely untouched, not partially written.
Targeted regression: departments 48, giving 127, core 197, cashbook 271 — all green.

Deploy: migrate (departments 0021, core 0048 — on_delete changes only, no data affected).

## v2.12.0 - accessibility: form labels and colour contrast
Reviewed navigation, forms, dashboards, and visual presentation against WCAG. The application already had a
strong accessibility foundation (skip link, ARIA landmarks, focus-visible styles with a high-contrast mode,
loading indicators, double-submit guards, `aria-live` flash/toast regions, correct viewport meta) — this
pass found and fixed two genuine, widespread gaps underneath that foundation.

**Fixed:**
- **Every form label in the application was missing its `for` attribute** — found first on the expense form,
  then confirmed across the entire app: the shared `partials/form_fields.html` template (used by most forms)
  and ten further custom form templates (expenses, obligations, petty cash, assets, members, pledges,
  campaigns, cash entries, settings, password change, the transactions filter bar, pledge import) all rendered
  a bare `<label>Field name</label>` with no programmatic link to its input. This meant a screen reader user
  had no way to know what a field was for beyond its position in the page, and clicking a label didn't focus
  its field for anyone. Fixed everywhere — including several hand-built (non-Django-field) inputs that needed
  a matching `id` added before they could be labelled correctly. Verified with zero remaining unassociated
  labels across every form template checked.
- **The amber "pending/warning" status colour failed WCAG AA contrast** — 3.99:1 in light mode and 3.28:1 in
  dark mode (both need 4.5:1), used throughout the app for pending/warning pills, flash messages, and toasts.
  Fixed to 4.76:1 (light) and 6.92:1 (dark, via a new theme-specific override) — both comfortably pass, and
  the colour still reads as the same warm amber/gold.

**Recorded in `docs/recommendations.md`:** two further items — data tables lack `scope="col"` on header cells
(Low-Medium, a broad mechanical cleanup better suited to its own pass than a partial sweep here), and no
dedicated mobile-breakpoint audit was performed this pass for the denser reporting pages (Low).

Tests: 12 new (core.test_label_accessibility), covering the shared partial directly and eight full-page
checks across the app finding zero unassociated labels. Targeted regression (every app with a touched
template): core 197, cashbook 271, giving 123, assets+pledges+accounts 91 — all green.

Deploy: no migration. Collectstatic recommended (CSS changed) but not required for correctness.

## v2.11.0 - critical: cash-position reporting and reconciliation fix
Reviewed every financial report, dashboard, chart, and export for calculation accuracy and reconciliation
with the General Ledger. Cross-checked collections, local funds statement, cash flow, and SOFP figures
against each other and against the ledger's own trial balance and accounting equation — all tied out exactly,
with one critical exception found and fixed.

**Fixed (Critical):**
- **Executive overview's "Cash & bank balance" KPI card, the Cash Flow Forecast, and the bank reconciliation
  book balance** were all computing "today's cash position" from `SiteConfig.opening_bank_balance` /
  `opening_cash_on_hand` / `opening_unremitted_trust` — fields populated only by the legacy-spreadsheet
  import tool as a one-time labelled snapshot, and left at zero for every normal deployment (including this
  one). The actual authoritative opening balance has always been `Department.opening_balance`, summed across
  every fund — the same source the ledger and the Statement of Financial Position correctly use. For this
  church's data, the bug understated "today's cash" by the full opening position (a multi-million-shilling
  discrepancy in testing), meaning: the Executive overview showed a false negative cash balance with no
  warning styling; the Cash Flow Forecast projected from a wrong starting point; and — most seriously — the
  bank reconciliation book balance could never tie to the actual bank statement, showing an unexplained gap
  on every single reconciliation. All three now use a new shared, correct helper
  (`departments.models.total_opening_cash_position()`) and were verified, after the fix, to tie out exactly
  to the Statement of Financial Position's own cash figure.
- The bank-reconciliation diagnostic view's "opening" breakdown (shown alongside its already-correct "book"
  figure, which came from a separate, unaffected calculation) is now internally consistent with that figure
  instead of contradicting it.

**Verified correct (no changes needed):** collections totals, local funds statement, trust fund summary,
cash flow opening/closing reconciliation, chart data vs. table data within the same report, and date-range
boundary handling (start/end dates correctly inclusive) — all cross-checked across multiple independent
calculation paths and found consistent.

**Recorded in `docs/recommendations.md`:** the Bank Position report depends on the same
`opening_bank_balance` field for a genuinely different figure (the bank account's own opening balance, which
isn't derivable from per-fund balances) and needs a treasurer to configure it explicitly — High priority,
operational rather than a code fix. Also recorded: the three legacy-import-only opening-balance fields remain
a duplicate, easily-misused source of truth that produced this exact mistake three times over several
releases — recommend renaming or documenting them more forcefully to prevent a fourth recurrence (Medium).

**Deploy note:** if bank reconciliation is in use, recompute any existing reconciliation worksheets (there's
a "recompute from ledger" action on each) so their stored book balance picks up the corrected figure — a
worksheet saved before this fix keeps its old, wrong stored value until recomputed.

Tests: 7 new (core.test_cash_position_fix), 1 existing performance test's query-count threshold adjusted
(+1 query — the correct opening-balance aggregate — a trivial cost for a critical correctness fix). Targeted
regression: core 185, statements 73, departments 44, reports 248, cashbook 271, giving 123 — all green.

Deploy: no migration. Collectstatic not required.

## v2.10.0 - trust pending-receipt export + architecture review
**New:** a "Trust pending receipt" download button on the Transactions page
(`/transactions/`) — an Excel export of trust-fund credits with no formal receipt yet
(Date, Phone, Member, Amount, Fund, Reference), independent of whatever filters are
currently applied to the list. A contribution split across several trust funds (e.g.
a Combined Offering split across two trust accounts) is recombined into one row —
summed amount, the split fund's name — rather than shown as separate partial lines,
since to the giver and the board it's one gift. The split fund's name is resolved via
the allocation rule that caused the split; falls back to a joined list of fund names
if no rule is found, so the export never breaks or shows a blank fund.

**Architecture & code-quality review** (Senior Architect / Django Expert lens):
codebase evaluated for maintainability, DRY compliance, dead code, and Django best
practices. Findings:
- Removed several confirmed-dead/fully-redundant imports (`reports/views.py`,
  `cashbook/views.py`, `giving/views.py`, `core/views.py`) — each verified to have
  zero usages, or to duplicate an import already present earlier in the same file,
  before removal. No behaviour changed.
- Reviewed all ~94 `except Exception` blocks in the codebase for silently-swallowed
  errors: the large majority are either explicitly marked `# noqa: BLE001` (a
  deliberate, previously-reviewed choice) or guard genuinely non-critical optional
  functionality (an update-availability check, an optional model import) where
  logging every transient failure would spam the log on every page load. No
  systemic error-handling issue found — a positive finding, not left for cleanup.
- Recorded two further items in `docs/recommendations.md`: `reports/views.py` (3,889
  lines) and `cashbook/views.py` (3,116 lines) have grown into "god files" that would
  benefit from splitting into logical sub-modules (Medium priority — a deliberate,
  dedicated refactor, not a quick fix); and the department-dropdown queryset pattern
  is repeated with minor variations across six form classes (Low priority).

Tests: 9 new (giving.test_trust_pending_receipt). Targeted regression (modules
touched): reports 248, cashbook 271, core 178, giving 123 — all green.

Deploy: no migration. Collectstatic not required.

## v2.9.0 - performance review, round two
Continued database/performance review. Two more N+1 patterns found and fixed, both more severe than
anything caught in the first pass; further items added to `docs/recommendations.md`.

**Fixed:**
- **Audit Log** (`/reports/audit/`) issued **~1,538 queries** for a church with a substantial history of
  allocation-rule edits (60 create+edit cycles in testing generated ~6,500 historical rows). Root cause:
  `h.instance` (a historical row reconstructed as a full model instance) carries no `select_related`, and
  `AllocationRule.__str__()` accesses `self.split_fund or self.department` — so calling `str(h.instance)` for
  every historical Allocation Rule row triggered a fresh FK lookup each time (1,426 Department queries + 90
  SplitFund queries observed). Fixed by building the display string directly from the historical row's own
  FK id columns against two name maps prefetched once, only for this one model (Transaction, Expense and
  Member history all use `h.instance` as before — their `__str__` methods don't touch relations, confirmed
  by testing). Verified identical display strings across 300 sampled historical rows. **~1,538 → ~24 queries.**
- **Every fund-picker dropdown built without `select_related("parent")`.** `Department.__str__()` shows
  "Parent / Name" for a sub-account — so rendering a `<select>` of departments calls `.parent` on each option,
  and eight form classes across cashbook, giving, assets, and core built their department queryset without
  prefetching it. The Payables & Accruals page alone (three such dropdowns) dropped from 44 to 23 queries;
  the fix applies equally to the expense, cash-entry, transaction, fund-transfer, payable, accrual,
  prepayment, and fixed-asset forms.

**Recorded in `docs/recommendations.md`:** one further item (`StaffAdvance.balance` computed per-row on the
advance list — Low priority, marginal at current scale), added to the five recorded in the first round.

Tests: 6 new (reports.test_performance2), asserting both correctness (display strings match the original
per-instance method) and query-count bounds. Targeted regression (the modules touched this round): reports
248, cashbook 271, giving 114, assets+core 197 — all green.

Deploy: no migration (queryset/view changes only). Collectstatic not required.

## v2.8.0 - performance & database review
Reviewed database access patterns, ORM usage, N+1 queries, indexing, and report generation. Two clear,
high-impact N+1 query patterns found and fixed; further architectural/infrastructure recommendations
recorded in `docs/recommendations.md` rather than implemented inline, per this review's scope.

**Fixed:**
- **General Ledger Health Check** (`/ledger/health/`) issued ~266 queries per load: `funds_out_of_balance()`
  called `fund_balance_from_ledger()` once per department (2 queries plus an implicit transaction each). New
  `fund_balances_from_ledger_bulk()` computes every fund's ledger balance with a small, constant number of
  grouped-aggregate queries instead. Verified byte-for-byte identical results against the original
  per-department function across every fund. Query count: ~266 → ~35.
- **Executive overview and budget-vs-actual reports** had the same pattern for budget figures:
  `budget_amount()` was called once per top-level fund (45 identical queries observed on the Executive
  overview alone). New `budget_amounts_bulk()` in `reports/services/budget.py` fixes both call sites
  (`ExpenseReportView`'s by-fund breakdown and `budget_vs_actual()`, which also feeds the Monthly Treasurer's
  Report). Verified identical results across every fund and every budget shape (legacy annual_budget, plain
  Budget.amount, Budget with breakdown lines, no budget at all). Executive overview: ~162 → ~114 queries;
  Monthly Treasurer's Report also dropped from ~175 to ~129 as a side effect of sharing the same fix.
- **New database indexes** for the two most common report-query shapes that weren't already covered:
  `Transaction(direction, confirmed, date)` — the shape behind `confirmed_credits()` filtered to a period,
  used by nearly every collections/income report — and `Expense(status, date)` — effective (approved/paid)
  expenses within a period, the dominant shape across expense reports.

**Caught during this review:** an earlier edit to `Transaction`'s model accidentally added a *second*
`class Meta` inside the same class body; Python silently keeps only the last one defined, which would have
discarded the pre-existing `ordering` and three existing indexes entirely. Caught before migrating by
checking `Transaction._meta.indexes` against the database rather than trusting the migration diff alone;
fixed by merging the new index into the correct, pre-existing `Meta` block.

**Recorded in `docs/recommendations.md` (not implemented this pass — see file for full detail):**
1. Monthly Treasurer's Report recomputes the same aggregates separately for several sections instead of once
   (Medium) — a report-internals refactor, not a quick fix.
2. `SiteConfig.get()` is uncached, costing 7-11 redundant identical queries per request (Medium) — the safe
   fix depends on an infrastructure decision (no shared cache backend is configured; the default per-process
   cache risks stale security/financial-control settings across workers).
3. No row-level locking on petty-cash-float balance checks — a narrow concurrency race (Low today, revisit
   if concurrent usage grows).
4. No systematic N+1/index audit against real production traffic, only synthetic profiling (Low).
5. Large file imports run synchronously with no background task queue (Low, given current usage patterns).

Tests: 9 new (ledger.test_performance, reports.test_performance), asserting both correctness (bulk functions
match the original per-item functions exactly) and query-count bounds. Targeted regression run (the four
modified apps only, per this review's instruction): ledger 49, reports 242, cashbook 271, giving 114 — all
green. Test suite run time also dropped noticeably (reports: ~150-170s -> ~78s) as a real-world side effect.

Deploy: migrate (cashbook 0033, giving 0022 — index-only, no data changes). Collectstatic not required.

## v2.7.0 - business logic & functional review
Comprehensive functional review of workflows, validations, calculations, and state transitions across every
module. Full findings, fixes, and regression results below; see the chat summary for the complete report.

**Fixed:**
- **Negative/zero amount validation (the main finding).** `Transaction.amount` already rejected non-positive
  values; `Expense.amount` and every other money-movement model did not — a real gap, not a deliberate
  design difference. A negative "expense" would post as an unreviewed credit to cash while still being
  categorised and reported as an expense, bypassing income recognition entirely. Now consistently enforced
  (`MinValueValidator(0.01)`) on: Expense, ExpenseRefund, FundTransfer, RecurringExpense, PettyCashTopUp,
  Payable, Accrual, Prepayment, StaffAdvance, AdvanceTopUp, ChequeRegister, PaymentInstrument, PledgePayment,
  PledgeMatchSuggestion, EnvelopeLine, and FixedAsset.cost (FixedAsset.salvage_value allows zero, the normal
  case, but not negative).
- **Future-date typo guard.** No form rejected a wildly future-dated entry — a year typo (2036 for 2026)
  would silently misfile a transaction with no error, since it just wouldn't appear in any report until that
  date arrived. Expense, cash entry, transaction-edit, and fund-transfer forms now reject a date more than a
  day ahead (a day of slack for timezone differences), with a clear message naming the likely mistake.
- **Pledge inline-matching test time-bomb.** Four tests failed with `paid` staying 0 / no match suggestions
  created. Root cause: a Pledge defaults `start_date` to *today*, but the tests hardcoded an absolute
  transaction date that was valid when written and has since drifted outside the matching window as real
  time passed — a test-fragility bug, not a defect in the matching logic itself (confirmed by testing the
  same code against the same scenario with an explicit, fixed `start_date`). Fixed by pinning `start_date`
  in the four affected tests.
- **Self-introduced regression, caught and fixed the same session:** an edit to `ExpenseForm` initially split
  `__init__` in two, leaving its accessibility styling call (`_style()`) and several field-setup steps
  (capitalized-asset queryset, petty-cash checkbox help text) as unreachable code with no visible error.
  Caught by the full regression run (`core.test_ux_a11y`), root-caused, and corrected; a regression test now
  guards against this exact class of mistake recurring silently.

**Documented for follow-up (not in scope for auto-fix this pass):**
- A deeper concurrency/race-condition review (e.g. simultaneous petty-cash top-ups both reading a stale
  float balance) was not performed in full — low real-world likelihood given typical single/few-treasurer
  usage, but worth a dedicated look if usage grows.
- A full N+1 query / performance audit across dashboards and reports was out of scope for this pass.

Tests: 33 new (cashbook.test_amount_validation, cashbook.test_future_date_validation, pledges/envelopes/
assets.test_amount_validation), 2 pre-existing pledge tests corrected. Full application regression, every
app: accounts 46, assets 19, cashbook 271, core 178, departments 44, envelopes 58, giving 114, ledger 44,
leaders 58, members 40, pledges 26, reports 238, statements 73 — 1,209 tests, all green.

Deploy: migrate (cashbook 0032, pledges 0004, envelopes 0004, assets 0004 — validator-only changes, no data
affected). Collectstatic not required.

## v2.6.0 - security & internal controls review
Reviewed against the OWASP Top 10, Django security best practices, least privilege, segregation of duties,
and internal-control principles for financial systems. Overall posture was already strong (Fernet-encrypted
credentials throughout, django-axes brute-force lockout on login, hardened production settings that fail
loudly on an insecure config, webhook endpoints authenticated with constant-time comparisons, no raw SQL
anywhere, every sensitive field explicitly listed rather than mass-assignable). Findings below.

**Fixed:**
- 2FA brute-force gap: the code-verification step (`/2fa/verify/`) had no rate limiting, unlike the password
  step (protected by django-axes) — a 6-digit TOTP could be guessed with unlimited attempts. Now locks the
  pending login out after 5 wrong codes and requires signing in again; the attempt counter is per-login and
  resets cleanly on a fresh sign-in.
- Last-active-Treasurer lockout protection: nothing stopped a Treasurer from demoting or deactivating the
  only active Treasurer account, which could leave the church with no one able to approve expenses, manage
  users, or unlock accounting periods. Now blocked with a clear message unless another active Treasurer
  exists; editing any other role is unaffected.
- Session lifetime: Django's default (2 weeks, no idle timeout) is longer than good practice for a financial
  system. Now a configurable 12-hour sliding session (renews on activity, so active users are never logged
  out mid-task) — override via `DJANGO_SESSION_COOKIE_AGE` if a different value is needed.
- Defence-in-depth: one chart-data JSON blob (leader dashboard) used a plain `json.dumps()` instead of the
  app's `safe_json()` (which escapes characters that could break out of a `<script>` tag) — switched for
  consistency, though the specific data involved was already free of user-controlled text. The manual journal
  entry form now handles a line with both a debit and a credit gracefully instead of raising, matching the
  new balanced-entry safeguard added in v2.4.0.

**Documented for review (policy decisions, not auto-fixed):**
- A department leader who leads any one development-fund can view the full contributor list (names, phones,
  amounts) for every development group church-wide, not just their own — confirmed as consistent, existing
  design throughout the leader area (not a new bug), but worth a policy decision on whether that shared
  visibility is acceptable for personally identifiable giving data.
- The bank-feed webhook's "None" authentication mode is already labelled "test environment only" and is not
  the default (Basic auth is) — low risk as configured, but worth confirming production never runs on it.

Tests: 11 new (accounts.test_security_audit, ledger.test_security_audit). Full application regression run:
accounts 46, cashbook 244, giving 114, leaders 58, statements 73, departments 44, members 40, core 178,
reports 238, envelopes 56, ledger 44, assets 15 — all green. Pledges: 35 of 39 pass; the remaining 4
(inline pledge-matching) were already failing before this review and are unrelated to it — flagged for
separate follow-up, not touched here.

Deploy: no migration. Collectstatic not required.

## v2.5.0 - general ledger health check, period-close checklist, permanent journal numbers
**1. General Ledger Health Check** (`/ledger/health/`, linked from the Accounting menu): a proactive integrity
dashboard showing trial balance status, unbalanced journals (should always be zero), orphan journals (a
posted entry whose source document no longer exists), missing postings (a source document that should have
been posted but wasn't), duplicate postings, shared M-Pesa/bank references worth a human look, and funds out
of balance between the fund-report engine and the general ledger. Every check is read-only; `ledger.services.health`
can also be used from a shell/management command for scripted monitoring.

**2. Accounting Period-Close Checklist**: the Treasury Controls page now shows a checklist for any open month
before it's locked — bank reconciliation complete, petty cash reconciled, advances cleared or explained, no
pending envelope allocations, no draft/pending journals, trial balance balances, fund balances reconcile, and
cash book equals bank plus cash on hand. Nothing here blocks locking outright (an advance spanning months is
often normal) — the lock button asks for confirmation if items are outstanding, so closing stays deliberate.

**3. Immutable journal sequence numbers**: every journal entry now gets a permanent reference (JV-2026-000001,
incrementing per year, assigned once and never reused). Since a correction to a source document replaces its
journal entry rather than editing it in place, the *original* entry's number is preserved in the Journal
Archive (added in v2.4.0) when it's superseded — the replacement gets its own new number. Existing entries
were backfilled in chronological order via a data migration. Shown on the Journal, General Ledger, Journal
Archive and Health Check pages, and included in the Journal's Excel/CSV export.

Tests: 16 new (ledger.test_health_and_numbering). Full regression green (cashbook 244, giving 114, core 178,
reports 238, statements 73, envelopes 56, ledger 42).

Deploy: migrate (ledger 0004-0005 — the 0005 migration backfills journal numbers for existing entries, safe
to run on a live database). Collectstatic not required.

## v2.4.0 - accounting-integrity review: customizable controls for every open recommendation
Implements the four Medium and two Low findings left open in the previous accounting-integrity review
(v2.3.0), each as a configurable setting where the recommendation involved a genuine trade-off, so every
existing deployment keeps its current behaviour until a treasurer deliberately opts in.

**New settings (Settings -> Approvals & financial controls):**
- **Require a different approver** (off by default) — blocks a treasurer from approving an expense they
  recorded themselves, for every expense, not just those above the dual-approval threshold. Applies to both
  the single-expense approval action and bulk approve.
- **Auto-lock on reconciliation** (off by default) — automatically locks the accounting month once a bank
  reconciliation for it balances. Independently of this setting, editing or adding an entry dated within an
  already-reconciled period now always shows a non-blocking warning.
- **Leader self-service delete window** (blank = unlimited, matching prior behaviour) — a department leader
  may only delete their own already-posted advance expense line within this many days of entering it;
  afterwards only a treasurer can remove it. Deleting now always requires a short reason (captured on the
  audit trail) and notifies all treasurers in-app.
- **Archive replaced ledger entries** (on by default) — snapshots a journal entry's detail whenever a
  correction (e.g. editing an expense's amount after posting) causes it to be replaced, so what the ledger
  said before the correction is never lost even though the live ledger only ever shows the current, correct
  posting. New Journal Archive page (`/ledger/journal/archive/`, linked from the Journal page) to review them.

**Always-on safety net:**
- Every journal entry is now validated to balance (debits == credits, no line with both a debit and a
  credit) at the single point all posting paths go through, raising `UnbalancedEntryError` if not. Every
  current posting path already balances by construction — this is defence-in-depth against a future mistake,
  not a behaviour change; full regression confirms it never triggers on any existing code path.

**Per-fund enhancement:**
- New optional `income_account` field on a fund: overrides the previous name-matching guess for which income
  account a local fund's receipts post to, for a fund whose name doesn't clearly say what it is (or after a
  rename). Blank (default) keeps the existing automatic guess.

Tests: 19 new regression tests (cashbook.test_audit_recommendations) covering every new setting in both its
default (off/unlimited) and opted-in state. 4 pre-existing tests updated to pass the now-required delete
reason. Full regression green (cashbook 244, giving 114, leaders 58, statements 73, departments 44, core 178,
reports 238, envelopes 56).

Deploy: migrate (core 0047, ledger 0003, departments 0020). Collectstatic not required.

## v2.3.0 - accounting-integrity & internal-controls review: two fixes, four documented for decision
A full review of the accounting core (general ledger, fund accounting, reconciliation, segregation of duties,
audit trail) against GAAP, IFRS for SMEs, and non-profit/church fund-accounting practice. See
`Treasury_Accounting_Controls_Review.docx` for the complete written findings register; summary below.

**Fixed this release:**
- General ledger date desync: correcting an envelope's Sabbath date bulk-updated its linked transactions'
  dates via a bulk `.update()`, which bypasses the signal that reposts entries to the ledger — the journal
  entry silently kept the old date. Now explicitly re-posted so the ledger follows the correction.
- Posted expenses were hard-deletable: any treasurer could permanently delete an APPROVED or PAID expense,
  removing it from the ledger with no trace, bypassing the app's own proper reversal mechanism (ExpenseRefund).
  Hard-delete is now restricted to PENDING expenses only; a posted expense must be reversed via a refund.

**Documented for review (not auto-fixed — policy or design decisions):**
- Leaders can hard-delete their own already-posted advance-accounting lines with no reason captured.
- No database/model-level safeguard that a journal entry's debits equal its credits (currently balanced by
  construction in every posting path, but nothing would catch a future mistake).
- The general ledger itself has no change history — only source documents do (a defensible, but
  under-documented, design choice).
- Bank reconciliation sign-off isn't linked to period locking, so a signed-off reconciliation can be silently
  invalidated by a later edit in its period.
- (Low priority) No system-level prevention of self-approval below the dual-approval threshold; income-account
  classification relies on fund-name text matching.

Tests: 5 new regression tests for the two fixes (cashbook.test_audit_findings). Full regression green
(cashbook 225, envelopes 56, giving 114). No migration; collectstatic not required.

## v2.2.1 - group contribution goals JPEG shows target/contributed/to go/progress
- The Group Contribution Goals JPEG chart (`/reports/fund/<id>/budget/group-goals.jpg`) now labels each
  group with Target, Contributed, To go, and percent progress — matching what's shown on the budget page —
  instead of just a collected/goal fraction. The all-groups footer total does the same.
- No tests changed this release (visual/label-only edit to the existing chart, already covered).

## v2.2.0 - receipt-strip wildcard fix, JPEG goal chart, member giving-count filter, board report case + charts
- Fix: the receipt strip-strings `*` wildcard was matching almost nothing. Two separate bugs: (1) whitespace
  in the configured phrase had to match the message exactly character-for-character, so a pattern typed with
  a double space against a message with a single space (a very easy copy-paste slip) silently failed to
  match at all; (2) the wildcard was non-greedy, so over an amount containing its own decimal point (e.g.
  "499,900.00") it stopped at the *first* period it found instead of the sentence's real full stop, leaving
  a fragment like "00." behind. Whitespace in a configured phrase now matches any run of whitespace in the
  message, and the wildcard is greedy so it correctly consumes the whole varying value.
- New server-side JPEG chart for a fund's Group Contribution Goals (`/reports/fund/<id>/budget/group-goals.jpg`):
  a proper per-group progress bar chart rendered with Pillow, not a table screenshot — same everywhere it's
  downloaded from, no client-side rendering needed.
- Members SMS page (`/members/sms/`) gained a "minimum contributions on record" filter, layered on top of
  any criterion (including the plain broadcast), to exclude one-time givers who may not be church members.
- Fix: `_camp_goal_records()` picked whichever CAMP_EXPENSE-flagged fund an unordered `.first()` happened to
  return, which is not guaranteed stable — if more than one fund was (mis)flagged, or the one returned had no
  `year_goal` set, the Camp Meeting Expense Goal would silently disappear from every report. Now deterministic:
  prefers the fund that actually has a goal set.
- Monthly Treasurer's Report (HTML and Word) and the classic board report now display fund/member names in
  Sentence case via a new `sentence_fund` filter, instead of the ALL CAPS many were originally entered in
  (short acronyms like AMM/LCB/PF are kept as-is).
- Word export gained three chart images (income vs expenditure, collections local/trust split, Camp Meeting
  Expense Goal progress when applicable), rendered server-side with Pillow and embedded as base64 — Word
  can't run the on-screen report's JS charts — each with a short AI-analysis caption (server-side, LLM-
  enriched with a rule-based fallback, same pattern as the per-section narratives).
- Tests: ~35 new tests across cashbook, reports, members. Full regression green (cashbook 220, reports 238,
  members 40, giving 114, core 178).
- Deploy: no migration; collectstatic not required.

## v2.1.0 - expense categories, member SMS, remittance-batch fix, camp goal bug fix, dynamic receipt stripping
- New expense categories: Salaries/Wages, Lease Payment (Stationery/Printing already existed).
- Bank/transaction charges (category BANK_CHARGE) no longer show a "no receipt" pill on the expense list, and
  the expense detail page shows "No receipt — none needed" instead of implying one is missing (the missing-
  receipts queue already excluded them; this closes the remaining inline UI gaps).
- New Members page SMS button (`/members/sms/`): send a message to members matching a criterion — not yet
  contributed to a given campaign (e.g. Camp Meeting), have an outstanding pledge, haven't given in the last
  N days, belong to a demographic group, or a plain broadcast to everyone with a phone. Recipients preview
  live before sending; message supports {name}/{church}/{campaign}/{amount} placeholders.
- Fix: the "settle a batch (multi-fund)" remittance option on the debits queue never populated any batches —
  its query ordered by `-created_at`, a field RemittanceBatch doesn't have. Django templates swallow that
  FieldError silently when iterating, so the dropdown just rendered empty with no visible error. Now orders
  by the model's actual `date`/`id` fields.
- Fix: the fund budget page (`/reports/fund/<id>/budget/`) has two independent forms — the fund's own
  expense goal, and per-group contribution goals — that both posted to the same `save_goals` flag; the
  shared handler unconditionally rewrote every field regardless of which form was submitted, so saving a
  per-group goal silently reset the fund's overall expense goal (and goal_type) to blank. Split into
  `save_expense_goal` / `save_group_goals`, each touching only its own fields.
- The Monthly Treasurer's Report (`/reports/board/`) already aggregates the Camp Meeting Expense Goal across
  the fund and all its sub-groups (never a per-group breakdown) — confirmed working now that the goal no
  longer gets wiped by the bug above.
- Receipt strip-strings (Settings → Branding) now support a `*` wildcard for parts that vary every message —
  an M-Pesa balance, a transaction cost, a promo link — so one configured line strips the whole sentence
  regardless of the actual figures.
- Tests: ~30 new tests across cashbook, members, reports; 4 pre-existing tests updated for the split budget
  forms. Full regression green (cashbook 212, giving 114, reports 225, members 36, core 178).
- Deploy: migrate (cashbook 0031, core 0046 — category/help-text changes only), collectstatic not required.

## v2.0.0 - reconciliation fix, board report rework, remittance batch matching, camp goal settings, and more
- **Bank reconciliation fix**: petty-cash-funded outstanding staff advances were never added to the
  reconciliation worksheet — the petty float already subtracted them (cash had left the box), but nothing
  added them back as their own item, so that money silently vanished from the reconciliation. New
  `outstanding_petty_advances_total()` adds a "Staff advances from petty cash (not yet accounted)" managed
  item; `outstanding_bank_advances_total()` also now respects top-up dates properly.
- **Monthly Treasurer's Report — RTF removed**, Excel rebuilt with full detail (every fund listed, not the
  on-screen top-10), native charts (pie/bar/line) on Collections, Trust Trend, Local Funds, Expenditure and
  Financial Position, and a KPI-card Executive Summary sheet. Word export rewritten to mirror the on-screen
  report's structure (Executive Summary, each management section, Board Decisions Required) and now carries a
  per-section analysis paragraph — computed server-side (`_ai_narratives`), LLM-enriched in one batched call
  when the assistant is enabled, always falling back to the same rule-based text already used on screen.
- **Trust remittance — batch matching**: a new "settle a batch (multi-fund)" option on the debits queue
  matches a bank payment to an open remittance batch, marking every trust fund's line PAID and charging each
  fund its own share, instead of forcing the whole payment onto one fund. Amount mismatches are rejected with
  a clear message.
- Fund Ledger sub-accounts now sort by closing balance (largest first) instead of receipts.
- **Camp Meeting Offering goal moved to Settings** (new Goals tab): a single church-wide Trust-fund figure,
  no longer set per fund. The Camp Meeting *Expense* goal and every fund's own budget/goals stay on that
  fund's own page. Data migration moves any existing configuration across; migrations core 0043-0045.
- New Settings field: strings to strip from saved bank/M-Pesa receipt messages (e.g. "never share your PIN"
  boilerplate), applied on every save, plus a "clean up already-saved messages" button to re-run it over
  everything already imported.
- Receipt/supporting-document uploads capped at 1MB (was 10MB) across every upload point.
- Supporting Documents PDF now only includes expenses with an actual file attachment (text/link-only
  attachments are covered by the Receipts view instead). Fixed a Django ORM gotcha along the way:
  `.exclude()` on a to-many relation excludes the *parent* if it has *any* matching related row, so an
  expense with one real file and one text-only attachment was being wrongly dropped entirely.
- Treasurer dashboard: the Latest Sabbath date now follows your font setting instead of a hardcoded font; the
  combined receipts-vs-expenses-by-month chart moved to the Executive overview showing the full year (was
  just the dashboard's selected period); its old spot is now a local-vs-trust pie chart for the selected
  month.
- Tests: ~55 new tests across statements, giving, reports, cashbook and core; 5 pre-existing tests updated
  for the redesign. Full regression green (statements 73, giving 114, cashbook 198, reports 225, core 178,
  leaders+members 85).
- Deploy: migrate (core 0043-0045), collectstatic (CSS/template changes).

## v1.99.1 - fix attachment popovers opening by default on the expense list
- Fix: `.clip-pop{display:flex}` was an unconditional CSS rule, so it overrode the browser's default
  `[hidden]{display:none}` styling on every popover (author styles beat user-agent styles at equal
  specificity). Every receipt/M-Pesa-text popover on the Expense Register rendered open immediately instead
  of staying hidden until its paperclip was clicked. Added `.clip-pop[hidden]{display:none}`.
- Improvement: opening a popover now closes any other one that's open, so only one shows at a time; clicking
  anywhere else on the page also closes it.
- Tests: cashbook/test_clip_popover (4). Full cashbook suite green (199 tests).
- No migration; deploy needs collectstatic (CSS/JS changed).

## v1.99.0 - advance top-up charges, debit "already accounted for", remittance crash fix
- Staff advance top-ups gain an optional Charge field (advance detail page): the bank/M-Pesa cost of sending
  the top-up, booked as a BANK_CHARGE expense against the fund (church's own cost) but NOT added to what the
  holder must account for. Reversing a top-up now also removes its linked charge expense. Migration
  cashbook 0030 (AdvanceTopUp.charge, charge_expense).
- Bank debits queue (/debits/): new "Already accounted for" resolution — for a payment already recorded
  another way (or that shouldn't hit the books at all). Resolves the queue item with a required reason, and
  creates no expense and touches no fund balance.
- Fix: resolving a debit as a Trust fund remittance crashed with "Field 'id' expected a number but got ''"
  when the fund dropdown was left blank — its display toggle had been left out of the kind-switch JS, so a
  hidden select posted an empty string straight into a raw Department.objects.filter(pk=...) query. Added a
  _dept_from_post() helper used across all department lookups in the debit-resolve flow, and fixed the JS to
  reveal the fund selector for the remittance option.
- Enhancement: resolving a debit as a remittance now links the new expense to the most recent open (DRAFT or
  APPROVED) RemittanceBatch, if one exists, and reports which batch it was linked to.
- Tests: cashbook/test_topup_charge (4), giving/test_debit_queue_fixes (9). cashbook 195 / giving 109 green.
- Deploy: migrate (cashbook 0030), collectstatic not required.

## v1.98.0 - Monthly Treasurer's Report redesigned for the Church Board
- Executive summary (page 1): 8 KPI cards (Total collections, Local fund receipts, Trust fund receipts,
  Total expenses, Monthly surplus/(deficit), Cash & bank balance, Net assets, Trust funds outstanding), a
  Key Highlights panel (4-6 rule-based/AI-enriched lines), and an Items Requiring Board Attention panel.
- New _board_focus() computation (reports/views.py): rule-based detection of an unbalanced/missing bank
  reconciliation, negative local-fund balances, trust funds collected but not yet receipted, and budget
  overruns for the month (via reports.services.budget.budget_vs_actual). Feeds both the attention panel and
  a new Board Decisions Required closing section (always includes financial-statement approval and, when
  applicable, trust-remittance approval and a resolve-item per high-severity issue).
- Management report reorganised and renamed in sentence case: Collections summary, Trust fund performance,
  Local fund performance, Expenditure summary, Budget & goal tracking, Statement of financial position,
  Five-year trend, Cash flow, Bank reconciliation.
- Two new trend charts: receipted-trust 3-month line chart and a top-local-funds bar chart, alongside the
  existing 5-year, income/expenditure and fund-composition charts.
- Long tables (collections detail, local funds statement) show the top 10 with a collapsible Appendix for
  the full listing; collections detail gains a % of total column; negative fund balances and budget-overrun
  rows are visually flagged.
- All existing calculations, figures and the Excel/Word/RTF exports are unchanged — this is a presentation
  and organisation redesign only, built on the same context data.
- Tests: reports/test_board_report_redesign (12); updated 6 pre-existing tests whose assertions named the
  old section titles. Full regression green (reports 210, core 171, cashbook 191).
- No migration; collectstatic not required (no static asset changes, only template/view Python).

## v1.97.1 - fund ledger: brought-forward opening balance, collection-only payments column
- Fix: FundLedgerView used each fund's raw `opening_balance` field directly, so a fund with real activity
  before the report period (but a zero founding balance) showed an opening of 0.00 instead of its true
  brought-forward balance. Added `balances.brought_forward()` / `brought_forward_map()` (founding balance +
  all net movement strictly before the period start) and wired it into both the main fund and every
  sub-account on the Fund Ledger, matching what the dashboard already computed correctly.
- Fix: the sub-accounts table's Payments column was always shown, even when every fund displayed was
  collection-only (which never takes expenses). It now hides — showing Opening / Receipts / Closing only —
  when this fund and all its sub-accounts are collection-only, and the Excel/CSV export matches.
- Tests: reports/test_fund_ledger_bf (8). Full regression green (reports 198, core 171, cashbook 191).
- No migration; deploy is a straight code update + collectstatic not required (no static changes).

## v1.97.0 - leader receipts + awaiting-receipts queue, statement debit fix, cheque auto-reconcile
- Debit-import fix: cheque/transfer rows previously used the narration's first word ("CHQ", a payee name) as
  the bank-receipt dedup key, so after the first cheque every later "CHQ No..." row was flagged a duplicate.
  Shape-C parsing now only uses a genuine M-Pesa-style receipt; cheques dedup on their unique Core Ref. A real
  receipt column ("Receipt No") is used as a fallback dedup key when the narration has none.
- Cheque auto-reconcile: the reconciler now extracts cheque numbers from the statement narration
  ("CHQ No.000411") and matches them to the expense voucher/cheque number (leading zeros ignored). An exact
  amount plus a cheque-number match auto-links the bank debit to the expense and marks it paid/cleared, even
  when the cheque clears a few days after it was written.
- Leaders: new Receipts page and Awaiting-receipts queue in the sidebar, scoped to the funds they lead.
- Receipts archive shows each expense id (e.g. #543) for matching a receipt to its expense.
- Awaiting-receipts queue (leaders + treasurers): expenses with no supporting document; attaching a file or an
  M-Pesa message removes the expense from the queue. Dashboard cards on both roles track the count and value.
- Clicking the paperclip on the expense list reveals the attached M-Pesa message (text) or opens the document.
- Supporting Documents PDF now includes only expenses that have an attachment.
- Leader development-groups section: download-all Excel button; leader collections / expenses / unassigned-
  offerings pages share the same hero + filter layout.
- Fixed a URL collision (expenses/<pk>/attach/) that had shadowed the existing receipt-upload endpoint; the
  upload view now also lets a department leader attach to their own funds and honours a next redirect.
- Tests: cashbook/test_batch_v197 (11), statements/test_cheque_debits (6). Full regression green
  (statements 69, leaders 58, cashbook 191, core 171, giving 100, reports 190).
- Deploy: collectstatic (CSS/template changes). No new migrations.

## v1.96.0 - leader/dashboard polish, allocation rights, collection-only columns, sign-out page, church settings
- Fix: campaign list member/transaction counts used two Counts in one annotate, multiplying into a cartesian
  product (e.g. 800 -> ~16000). Now Count(..., distinct=True).
- Rights: added allocate_transactions and classify_debits granular rights + AllocateRequiredMixin /
  DebitClassifyRequiredMixin; review-queue and debit-queue views honour them, so a profile can grant
  allocation without the full data-entry role. (Item 7 answer: yes, allocation rights were missing.)
- Leader dashboard: development groups drop Target/Progress (show Opening/Receipts/Closing); sub-accounts sorted
  by closing balance desc; collection-only funds hide the Expenses column (opening/receipts/closing only) and
  their KPI cards omit expenses/net.
- Treasurer dashboard: local funds sorted by closing balance desc; collection-only funds hide the Expenses
  column.
- Leader expenses page: Method/Status/Category removed from the display (retained in the Excel export); leaders
  can attach a supporting file or an M-Pesa reference to any expense on their department.
- New sign-out page (SignOutView) with a rotating public-domain KJV verse; redesigned sign-in already shipped.
- New church-customization settings: church_address, church_contact, currency_symbol, report_footer_note
  (exposed as CURRENCY / CHURCH_ADDRESS / CHURCH_CONTACT / REPORT_FOOTER_NOTE in templates). Migrations
  core 0041 (currency/footer) and 0042.
- Tests: leaders/test_batch_v196 (10). leaders+accounts 95 / giving 100 / core 171 / cashbook 180 / reports 190.
- Deploy: migrate (core 0041, 0042), collectstatic.

## v1.95.0 - leader advances polish, dev-group balances, receivable fix, modern sign-in
- Leader advances list: added a "how to account for an advance" help panel and an explicit Open button per row.
- Leader advance detail: Excel download of the full statement (?export=xlsx).
- Leaders can now delete a transaction charge line directly (not only whole expenses). Deleting an expense
  removes its linked charge; advance settling charges are now linked via charge_for so the cascade is reliable;
  deleting an advance still removes its issuance charge (AdvanceDelete) and settling charges (advance cascade).
- Leader dashboard development groups now show Opening / Receipts / Closing (and Target / Progress); export
  updated to match.
- Fix: outstanding_advances_total (Staff advances receivable on the Statement of Financial Position) now
  excludes top-ups dated after the as-of date, keeping both sides of amount-less-settled as of the same date.
- Sign-in page redesigned: modern split brand/form layout, password show/hide toggle, light theme + system font.
- Tests: leaders/test_batch_v195 (9). leaders 48 / cashbook 180 / reports 190 / core 171 green.
- No migration; run collectstatic on deploy.

## v1.94.0 - leader area overhaul, supporting-docs PDF, light+system defaults, advance Excel import
- Leader dashboard: removed the Top contributors card and the Recent collections/expenses preview cards.
  Sub-accounts table now shows Opening / Receipts / Expenses / Closing for every account.
- Leaders: Collections and Expenses are now dedicated sidebar menus (leader_primary_dept); both pages gain
  a search box and pagination. A date filter + status filter were added to expenses.
- Leader staff-advance page: date filter, search over the statement, paginated statement (25/page),
  mobile-friendly stacked entry form, and a delete button for the leader's own expense lines while the
  period is open and the advance is still pending (not settled/closed).
- Advance Excel import (advance_import): a sample .xlsx download with the key fields, available to the
  treasurer/assistant and the owning department leader. The combined total (amount + charge) may not exceed
  the advance's remaining balance - an over-budget file is rejected in full.
- Expense Register: new "Supporting documents" export (export=support-pdf) building a single PDF with a
  voucher summary page per expense followed by its attachments (PDFs merged, images drawn, unsupported/
  missing handled gracefully). Requires reportlab + pypdf on the server; degrades with a message if absent.
- Appearance: UserPreference theme default LIGHT and font_family default SYSTEM (migration core 0041). The
  anonymous/login page now explicitly renders light + system font.
- Info: petty-cash top-ups and staff-advance top-ups do NOT post journal entries; they are reflected in the
  derived cash/float balances, not the general ledger.
- Tests: leaders/test_batch_v194 (13), cashbook/test_supporting_pdf (6). leaders 39 / cashbook 180 /
  core 171 / reports 190 green.
- Deploy: pip install -r requirements.txt (adds reportlab, pypdf), then migrate (core 0041), collectstatic.

## v1.93.0 - fund opening column, payables tabs, settings/sidebar persistence, advance top-up double-entry
- Fund ledger sub-accounts table now has an Opening column (opening + receipts - payments = closing),
  with matching totals rows and the subgroups export updated to include Opening.
- Payables page reorganised into Payables / Accruals / Prepayments tabs; the active tab is kept in the URL
  hash so it survives a refresh.
- Settings page: the active tab is carried through the save round-trip via ?tab=, so saving no longer
  bounces you back to the first tab (also applied to the SMS/email/assistant test buttons). Sidebar scroll
  position is remembered across page loads via sessionStorage.
- Advance top-ups now record a true double entry against the source: a petty-funded top-up appears as a
  dated outflow in the petty-cash register and reduces the float (the base advance line now shows only the
  base amount so top-ups are not double counted). Added a treasurer-only reverse action
  (advance_topup_reverse) that removes the top-up, decrements the advance total and restores the source.
- Tests: cashbook/test_batch_v193 (10). cashbook 174 / reports 190 / core 171 green.
- No migration; run collectstatic on deploy.

## v1.92.0 - expense ID sort/export, edit-charge fix, net-asset rename, RTF export, debit pill, section insights
- Expenses list ordered by expense ID (was date); ID shown in the table and added to the Excel/CSV export.
- Editing an expense now syncs the linked transaction-charge entry: creates it if newly added, updates it
  in place if present (never duplicates), deletes it if cleared. The charge is prefilled on the edit form.
  (Previously the charge was ignored entirely on edit.)
- Renamed net-asset classes across report, exports and financial position: Unallocated -> General net
  assets; Allocated -> Designated development funds.
- New RTF export for the Monthly Treasurer's Report (report_board_rtf, "RTF / Pages" button). RTF opens
  natively in Apple Pages, Word, LibreOffice and Google Docs.
- Per-section trend insights on the Monthly Treasurer's Report (rule-based, LLM-enriched when enabled):
  a line or two under collections, trust trend, financial position and cash flow.
- Dashboard: split the single "to allocate" pill into a giving pill (-> review queue) and a bank-debits
  pill (-> debit queue). Bank-statement debits now have their own notification linking to the right queue.
- Tests: reports/test_batch_v192 (11); updated test_board_batch_v191 for renamed labels.
- No migration; run collectstatic on deploy.

## v1.91.0 - Monthly Treasurer's Report polish + font fixes
- Fixed the month filter: an <input type="month"> submits "YYYY-MM" but the view parsed only full ISO
  dates, so it silently fell back to the current month. Now accepts YYYY-MM and YYYY-MM-DD. This also
  resolves the "SoFP shows no items" impression (the fallback month often had little data).
- Camp goals table: removed the Type column (report, Excel and Word exports).
- Added a compact Statement of changes in net assets (opening + surplus/(deficit) = closing) to the
  report and both exports; a full standalone version already exists at /reports/changes-net-assets/.
- Narration labels no longer forced uppercase; report uses the selected --font-body/--font-display.
- Dashboard/report cards: replaced hardcoded "Fraunces" with var(--font-display) on card headers,
  report-card titles and empty-state text, so the Appearance font preference applies to them (item 4).
- Clarifying note on net-asset classification: unallocated = general local funds not earmarked for a
  project; allocated = board-designated Development funds (item 2 reconfirmed and documented in-report).
- Local funds statement already sorted by closing balance (total) descending; confirmed.
- Fixed a latent bug in BoardReportView (FixedAsset.objects.filter(active=True) -> disposed=False) that
  was silently zeroing property NBV there.
- Tests: reports/test_board_batch_v191 (9). reports 179 / cashbook 164 / core 171 green.
- No migration; run collectstatic on deploy.

## v1.90.0 - richer backup + assistant, reconciliation clean-up, remittance reminder verified
- Backup Excel (full_excel_export_response): added Payments, Staff Advances, Remittances, Fund Transfers,
  Payables, Accruals, Pledges, Fixed Assets and Petty Cash Top-ups sheets (now 19 sheets total). All
  guarded so a missing model/field never breaks the export.
- Assistant LLM context (_data_context): enriched with this-month/last-month collections, income by
  channel, tithe YTD, trust remittance compliance + unreceipted, latest bank reconciliation status,
  unpresented payments, pledges, active member count, and top expense categories YTD.
- Bank reconciliation: _sync_managed_recon_items now also runs at reconciliation creation (not only on
  detail view), and each managed amount is computed defensively so one failure can't block the others.
  Removed the redundant Petty cash / Staff advances / Unpresented cheques informational panels and the
  manual "add" button — all three are auto-populated into the reconciliation statement.
- Verified (no code change needed): trust-remittance to_remit counts only RECEIPTED trust money, so
  unreceipted trust never raises the reminder, and it clears once remitted (aggregate cache is busted on
  Expense/RemittanceBatch save). Locked in with regression tests.
- Tests: core/test_batch_v190 (6); updated cashbook/test_cheque_register recon-wiring for the auto
  aggregate. 564 green. No migration; run collectstatic on deploy.

## v1.89.0 - nav follow-ups (People/Funds split, remittance in Banking, setup consolidated) + pay-at-entry
- Split "People & funds" into "People" (Members, Pledges, Campaigns) and "Funds & setup" (Funds &
  departments, Fund transfers, Budgeting, Fixed assets, Allocation rules, Development-group patterns).
  dev_patterns was previously unreachable from any nav menu; it now has a home.
- Moved "Trust remittance" (remittance_dashboard) from Reports into Banking, next to Payment register,
  since it's an operational workflow rather than a report. Remittance calendar stays in Reports.
- Updated core/context_processors.py breadcrumb map (_BREADCRUMBS) for all moved items.
- New: issue a payment instrument directly from expense entry. ExpenseCreate gained an optional "Issue a
  payment now" section (method/reference/date/bank account); on save it creates a linked, ISSUED
  PaymentInstrument (posts no journal entries, same as the existing framework) and approves the expense.
  Available whenever the expense will be approved (auto-approve orgs, or any treasurer, who implicitly
  self-approves by issuing payment) — hidden with an explanatory note otherwise. Expense detail page now
  shows the linked payment or a prefilled "Issue a payment" link into the register.
- Tests: cashbook/test_expense_entry_payment (7), core/test_nav_reorg (5). 558 green.
- No migration required; run collectstatic on deploy (templates/CSS only, no schema change).

## v1.88.0 - navigation & UX audit
- Breadcrumbs: new core.context_processors.breadcrumb maps url_name -> (section, page); base.html renders a
  Home / Section / Page trail on every mapped page, styled in app.css.
- Renamed for clarity/consistency: Giving "Ledger" -> "Transactions"; "Ask the books" -> "Assistant";
  Accounting "Ledger check" -> "Ledger integrity"; Reports "Board report" -> "Monthly Treasurer's Report"
  (matches the page title). Basic report_monthly removed from the sidebar (kept in the reports index as
  "Fund movement summary"); report index card "Bank reconciliation" -> "Reconciliation summary".
- Bug fix: report_reconciliation (ReconciliationView) crashed with TypeError when book_balance was None.
- Duplicate removed: report_board was listed twice in the reports index (executive pane card removed).
- Current page highlights (active class) verified after renames; parent nav group auto-opens and its
  summary highlights via :has(a.active). Quick-add "+ New" and Ctrl+K palette retained near the top.
- Tests: core/test_nav_audit (7). Full nav crawl: 48/48 links OK. 323 green across core/reports.
- No migration required (nav/template/context-processor/CSS only); run collectstatic on deploy.

## v1.87.0 - board report exports/goals, budget goals, appearance, reconciliation, filters
- #1 Monthly Treasurer's Report: added Camp Meeting goal records (expense + offering, fund-level, never
  group), income-vs-expenditure and fund-composition charts, and Excel (openpyxl, multi-sheet) + Word
  (Word-compatible HTML, no server library) downloads. New _camp_goal_records helper; export views/urls.
- #2 Fund budget page: offering goal shown only for CAMP_EXPENSE funds (cleared on save for others); each
  development group has its own contribution_goal with per-group progress; aggregate total row.
- #3 Appearance: font preference now drives --font-display so headings, church name and logo use it;
  added sidebar_style preference (core 0040: Forest/Midnight/Brass/Charcoal) with live apply.
- #4 Bank reconciliation auto-includes petty-cash float, outstanding staff advances AND unpresented
  cheques via _sync_managed_recon_items; removed the manual add_petty_cash / add_advances /
  add_unpresented_cheques actions and buttons.
- #5 Pagination preserves the current query string (Django 5.2 {% querystring %}) across all lists, so
  filters persist between pages.
- Tests: reports/test_board_exports (5), cashbook/test_budget_goals_v2 (7), statements/test_auto_recon (4);
  recon + fund-budget tests updated. 539 green.

## v1.86.0 - unified legacy remittance onto the PaymentInstrument workflow
- RemitTrustView (/reports/remittance/remit/) reworked: instead of embedding a cheque number in each
  expense, it now creates a RemittanceBatch (status REMITTED), raises the per-fund remittance expenses
  against it, and settles the whole batch with one generic PaymentInstrument (method/reference/date/bank
  account). The instrument posts no journal entries; clearing only flips status. Single payment
  architecture for both batch and one-step remittances.
- Remittance report form replaced cheque_no/cheque_date inputs with method + reference + date + bank
  account; RemittanceView passes bank_accounts. Legacy cheque fields kept in step for the CHEQUE method.
- Data migration 0029: back-fills historical standalone remittance expenses (REMITTANCE category, cheque
  voucher_no, no batch) by grouping them per cheque into batches and creating matching PaymentInstruments,
  so all historical remittances share the unified settlement architecture.
- Tests: reports/test_legacy_remit_payment (6). 370 green across cashbook/reports/statements.

## v1.85.0 - Payment Register route + remittance settlement workflow
- Renamed /cheques/ -> /payments/ (names payment_register / payment_outstanding / payment_print);
  /cheques/ paths kept as permanent (301) redirects for backward compatibility. Templates renamed to
  payment_*.html; UI labelled "Payment register" consistently; added to the Banking nav group.
- RemittanceBatch.payment FK (cashbook 0027) -> PaymentInstrument: the generic settlement record (method,
  reference, date, bank account, status, cleared date). Legacy cheque_no/cheque_date retained but
  superseded; existing values migrated into PaymentInstruments and linked (cashbook 0028).
- Remittance workflow now: Draft -> Approve -> Issue payment instrument -> (linked) -> Mark sent -> await
  clearance -> Cleared. RemittanceBatchRemitView refuses to mark a batch sent until an issued payment is
  linked (batch.is_settled). New RemittanceBatchIssuePaymentView issues + links the instrument; it posts
  no journal entries (the batch expenses already account for the liability). Clearing only flips status.
- Batch detail page: settlement-payment card, issue-payment form (method/reference/date/bank account),
  5-step wizard (selected/approved/issued/sent/cleared), and clearance guidance.
- Tests: reports/test_remittance_payment (6) + route/label coverage; payment tests repointed to /payments/.
  364 green across cashbook/reports/statements.

## v1.84.0 - payment-instrument framework (cheque register rework)
- New PaymentInstrument model (cashbook 0025) + PaymentAttachment: generic payment framework supporting
  CHEQUE, EFT, RTGS, MPESA, CASH and OTHER methods. Existing ChequeRegister rows ported over (cashbook
  0026); the legacy model is retained read-only for history.
- Every payment references its source obligation (Expense / RemittanceBatch / ExpenseRefund / FundTransfer)
  via typed FKs; clean() enforces a source unless it is an explicitly manual/supplier payment, which the
  view gates on treasurer rights.
- Accounting integrity: a payment instrument posts NO journal entries — the source already accounts for it.
  Issuing a cheque against a trust remittance settles that obligation; clearing during reconciliation only
  changes status. Verified by tests that assert journal-entry counts are unchanged on issue and clear.
- Lifecycle: Draft -> Approved -> Issued -> Outstanding -> Cleared, plus Voided / Stopped. Cleared
  instruments are immutable (is_locked) — edit/delete blocked; void or reverse instead.
- Dual signatories, approval (approved_by/at), cheque printing with amount-in-words (/cheques/<id>/print/),
  outstanding-payments report with Excel/CSV (/cheques/outstanding/), and bank-reconciliation integration
  repointed to PaymentInstrument (unpresented_cheques_total + ReconciliationDetailView + add_unpresented).
- Tests: cashbook/test_payment_instrument (11) + rewritten test_cheque_register (4). 537 green across
  cashbook/statements/ledger/reports/core.

## v1.83.1 - stable goal-type identifier (no name matching)
- Department.goal_type (departments 0019): classifies a fund's annual goal as a general goal or the Camp
  Meeting Expense goal, replacing the previous fund-name match in the board report and budget page. Labels
  now follow goal_type and the configured offering_fund link, so renaming a fund no longer changes the
  report. Goal type is set on the fund budget page's Edit goals form.
- Tests updated to set goal_type; added rename-resilience coverage.

## v1.83.0 - Camp Meeting goals, board report sections & settings, chart of accounts
- #3 Fund budget page: Camp Meeting Expense goal (Local) now aggregates collections across the fund and
  all its sub-groups; renamed from "Camp Meeting Goal (Year)" to "Camp Meeting Expense Goal". A separate
  Camp Meeting Offering goal (Trust fund) is configured on the same page and tracked independently — the
  two totals are never merged. Group Contribution goal shows each group's own sub-account collection.
  New Department.offering_fund / offering_goal (departments 0018).
- #3 Board report: new "Goals and targets" section with target, collected, variance and completion %,
  covering expense, offering and contribution goals, kept separate.
- #4 BoardReportSettingsView (/reports/board-settings/): choose which sections appear, drag to reorder,
  and add report notes (SiteConfig.board_config, core 0039). Board report rewritten to render sections in
  configured order with sentence-case headings, clearer hierarchy/spacing and print-ready styling.
- #5 Chart of accounts expanded with standard church accounts: petty cash, mobile money, staff advances,
  prepayments, other receivables, accumulated depreciation, accruals, payables, statutory deductions,
  designated/restricted funds, opening-balance equity, and interest / fundraising / donations income.
- Tests: reports/test_board_goals (6); fund-budget tests updated for the renamed goal fields. 611 green.

## v1.82.0 - transfer editing, expense refunds, fonts, balancing, dev-patterns, rule lifecycle
- #1 TransferEdit (/transfers/<id>/edit/): editing re-syncs balances, journals (post_save signal) and
  history; is_locked guard blocks reversed/reversal/locked-period transfers; Edit button on the list.
- #2 ExpenseRefund (cashbook 0024): contra-entry preserving the original expense; net_amount /
  refundable_balance; netted into fund_balance + expenses_by_department (date-aware, effective-only);
  post_refund ledger posting + signals + rebuild; petty-float restore; refund UI on the expense detail.
- #6 UserPreference.font_family (core 0038): per-user body typeface (Public Sans / System / Serif /
  Atkinson Hyperlegible / Mono), applied live via data-fontfamily and CSS --font-body.
- #7 _balanced_partition rewritten: size-capped greedy seed + local-search swaps balance both capability
  and member count; documents the inherent skew limit; spread cut markedly.
- #8 DevGroupPattern (giving 0019/0020, seeded defaults): configurable dev-group regexes with a manager
  page (add/edit/enable/disable/delete), regex validation, capture-group check, live tester; allocate()
  uses cached configured patterns (signal-invalidated) with a built-in fallback.
- #9 AllocationRule lifecycle (giving 0021): archived/archived_at + is_expired; archived rules excluded
  from allocation; active/expired/archived views; bulk archive-expired; archive/restore/permanent-delete;
  archive_expired_rules management command with a grace period for nightly cron.
- Tests: cashbook/test_transfer_refund (8), giving/test_patterns_lifecycle (13). 633 tests green.

## v1.81.0 - dev-group builder: download-first, live apply opt-in
- DevGroupBuilderView now exports the balanced proposal to Excel/CSV (group, member, phone, capability)
  via ?export=xlsx|csv, including member phone numbers where present; with no group count it exports the
  member list by capability.
- New SiteConfig.dev_group_builder_apply (core 0037, default False): the live "create groups & reassign
  members" action is disabled by default and gated on this flag; POST is blocked with a message when off.
  Toggle added to Settings → Channels (allocation card). The preview hides the create form when off.
- Tests: core/test_rights_batch updated (apply requires the setting) + download-only test.

## v1.80.0 - delegated rights, balanced dev-group builder, assistant + settings
- #1 removed the duplicate "Allocation & categories" (allocation rules) card from settings; the dev-group
  prefixes / numbered-fund-families card is retained.
- #4 three new assignable rights in core.rights: allocate_dev_offering, manage_advances, build_dev_groups
  (granted to Treasurer always; allocate/advances also to Assistant by default). Helpers in core.roles;
  AdvanceAccessMixin gates the advance views on manage_advances.
- #3 DevGroupUnassignedView now requires the allocate_dev_offering right (treasurers included); a leader
  granted it sees a sidebar "Allocate dev offering" item.
- #7 DevGroupBuilderView (/reports/dev-groups/build/): generate N groups balanced by members' historical
  development giving (greedy longest-processing-time partition); preview + create; gated on build_dev_groups.
  Buttons added to the dev-groups page.
- #5 a petty-cash-funded advance's sending charge is now paid_from_petty_cash (reduces the float too).
- #6 assistant: new staff-advances and petty-cash intents, enriched data context, refreshed suggestions.
- Tests: core/test_rights_batch (9); settings test updated for the removed card.

## v1.79.0 - advance refinements, by-member fund view, recon export, global search
- #9 sending charge no longer reduces the advance (church cost; posts to the fund, not linked via the
  advance FK). #7 per-line transaction charges when recording an expense DO reduce the advance. #4 an
  expense+charge can't exceed the advance balance. #3 AdvanceTopUp (cashbook 0023) to add cash to an
  open advance; petty timing + statement updated. #6 leaders can edit only their own expense lines (not
  the advance); #10 leaders attach receipts/M-Pesa messages to lines, no delete. #8 advance deletable
  only with no expenses.
- #11 FundMembersView (/reports/fund/<id>/members/): giving rolled up across the fund + all sub-accounts
  grouped by member, with Excel/CSV; buttons added to the fund ledger (original page retained).
- #5 closing a parent fund cascades to its sub-accounts (zero-balance guard on each); sub-accounts get
  their own close/reopen; reopen keeps parent/sub consistent; the main department list shows ACTIVE only
  (closed/archived live on /departments/historical/).
- #1 reconciliation: Excel/CSV export, print-only statement with a print header, diagnostic + management
  panels hidden from print, column alignment fixes.
- #2 GlobalSearchView (/search/): command palette now also searches members, funds, staff advances,
  expenses and receipts, merged under grouped headers with sublabels.
- Tests: departments/test_close_cascade (+FundMembers/ReconExport/GlobalSearch); advance tests updated.

## v1.78.0 - charge reduces advance + auto-populated reconciliation
- Bank/M-Pesa charge on an advance now REDUCES the advance: _sync_advance_charge() links the BANK_CHARGE
  expense via the `advance` FK, so it counts toward settled_total and lowers the balance to account for
  (rationale: advance sent to a personal account, holder incurs charges while spending). Shows in the
  advance statement. Petty float unaffected (charge is bank-paid, not petty-flagged); SoFP still ties
  (fund balance and advance receivable both drop by the charge).
- Bank reconciliation auto-populates the petty-cash float and outstanding bank-funded staff advances:
  _sync_managed_recon_items() upserts both as ADD items on view (for data-entry users), updates their
  amount as values change, and removes them when zero. New ReconciliationItem.auto flag (statements 0009);
  auto items can't be hand-deleted (show an 'auto' marker). Manual add_petty_cash/add_advances actions
  retained but the panels now show 'added automatically'.
- AdvanceDelete fixed for the new charge link (detach charge_expense before cascading adv.expenses).
- Tests: cashbook/test_advance_charges_edit updated (charge reduces advance) + AutoReconAndChargeTests.

## v1.77.0 - advance charges + edit/delete, recon advances, leader UX
- #1 StaffAdvance.bank_charge + charge_expense (cashbook 0022); _sync_advance_charge() books/updates/
  removes a BANK_CHARGE expense (excluded from settled_total). AdvanceCreate captures it.
- #3 AdvanceEdit + AdvanceDelete + apply_advance_edit() (end-to-end: charge re-synced, petty float
  recomputed, settling/charge expenses cascade on delete). Leaders may correct an OPEN advance via the
  leader detail page; closed advances are treasurer-only to amend.
- #2 outstanding_bank_advances_total() + recon 'add_advances' action + panel: bank-funded advances are
  added back as a reconciling item (cash out of bank, not yet expensed). Petty advances already sit in
  the petty-cash float, so they're excluded here. (Answer: petty = already accounted; bank = now added.)
- #6 petty-cash register shows petty-funded advance issuance (out) and returns (in); model simplified so
  the box loses the full advance at issuance (settling expenses no longer petty-flagged) and the register
  reconciles to _petty_balance_asof.
- #4 leader sidebar: 'Staff advances' menu item; single-department leaders redirect straight to their
  department (?stay=1 keeps the overview); button removed; nav label singular when one dept.
- #5 leader department page: gradient hero header, one-line KPI values, advances summary in Explore.
- #7 .kpi-grid .stat .value stays on one line (no cent wrap).
- #8 executive overview: 'Staff advances outstanding' + 'Petty cash remaining' tiles.
- Tests: cashbook/test_advance_charges_edit (10); leader tests updated for the single-dept redirect.

## v1.76.0 - staff advances: petty-cash funding + leader self-service
- StaffAdvance gains from_petty_cash + returned_to_petty (cashbook 0021) and helpers settled_asof(),
  accounted_total, petty_outstanding_asof(), balance now nets returned cash.
- Issuing an advance from petty cash reduces the float: _petty_balance_asof() subtracts each petty
  advance's outstanding (issued - settled - returned); settling expenses against it are flagged
  paid_from_petty_cash, so the float stays exact (advance-out reclassifies to spent, net zero).
  AdvanceCreate validates the float can cover a petty advance.
- Settling expenses are now APPROVED+PAID (paid_date set). Shared helper _record_advance_expense().
- Leaders: new LeaderAdvancesView (/leader/advances/) + LeaderAdvanceDetailView with a statement and an
  add-expense form, strictly scoped to departments_led_by(); claimant = the leader's name. Link added on
  the leader dashboard. The leader area stays read-only everywhere else.
- Advance detail now shows a running statement (issued -> settling lines -> still-to-account) and the
  petty-cash source; AdvanceClose captures surplus returned to petty cash.
- Financial-statement impact verified: the SoFP reclassifies the advance within assets (cash_on_hand +
  receivable), so totals tie regardless of source; only settling expenses hit I&E/cash-flow; the petty
  float + bank reconciliation reflect a petty advance as cash physically out of the box.
- Tests: cashbook/test_advance_petty_leader (10).

## v1.75.0 - Appearance & Preferences (per-user workspace customisation)
- New core.UserPreference model (OneToOne with User, migration core 0036): theme, accent (+custom),
  sidebar mode, font size, layout width, card style, dashboard widget order/visibility, landing page,
  rows-per-page, table density, table_state, high_contrast, reduced_motion, large_targets,
  focus_indicators, toasts_enabled, toast_duration, desktop_notifications. Helpers: get_for(),
  accent_hex, merged_widgets(), visible_widget_keys(), reset_to_defaults().
- Exposed app-wide via core.context_processors (prefs) and applied on <html> as data-* attributes +
  --pref-accent; all rendering handled in CSS (dark theme, accent override via color-mix, sidebar
  modes, font/width/cards, density, high-contrast, reduced-motion, large-targets, focus toggle).
- PreferencesView (/preferences/) with tabbed UI + UserPreferenceForm; PreferenceUpdateView
  (/preferences/update/) JSON endpoint persists each change live; static/js/preferences.js applies
  changes to <html> instantly and auto-saves (segmented controls, accent swatches/custom picker,
  toggles, selects, number inputs, drag-and-drop widget reorder). Reset-to-defaults via POST.
- Landing page: PostLoginRedirectView (/after-login/) honours pref; LOGIN_REDIRECT_URL -> after_login.
- Dashboard widgets: DashboardView exposes widget_visible/widget_order; dashboard.html wraps sections
  (attention/kpis/sabbath/charts/funds/trend/recent) in .dash-widgets with show/hide guards + CSS order
  (DOM-safe; charts use IDs). 
- Toasts: configurable toast system in base.html (window.toast), flash messages render as toasts when
  enabled (honouring duration + reduced motion + optional desktop Notification).
- Tables: PrefPaginationMixin (core.utils) honours rows_per_page on Transaction/Expense/Member lists;
  density applied globally via data-density.
- Links from the user menu and the Settings page. Tests: core/test_preferences (13).

## v1.74.0 - app-wide UX/UI & accessibility polish
All changes live in the shared design system (base.html + app.css + form mixin), so they apply across
every page without touching individual templates or any business logic.
- A11y: skip-to-content link + focusable <main id="main">; sr-only utility; ARIA labels on the menu
  toggle, search box (with aria-keyshortcuts) and notification bell; flash messages now sit in an
  aria-live region with role=alert/status; form widgets emit aria-required. Darkened --muted (#7a8a83 ->
  #677770) for WCAG-AA contrast on secondary text.
- Feedback states: global HTMX top loading bar (htmx:beforeRequest/afterRequest); success/info flashes
  auto-dismiss after 6s and all flashes get a dismiss (x) button; busy spinner state for submit buttons.
- Responsiveness: any data table not already wrapped is auto-wrapped in a horizontal scroll container
  (.table-scroll) so wide tables no longer break mobile layouts.
- Integrity/UX: double-submit guard marks the triggering button busy and blocks repeat submissions
  (skips GET forms, htmx, and cancelled confirm() dialogs) - prevents accidental double-posting.
- Tests: core/test_ux_a11y.

## v1.73.0 - configurable LCB departments + dashboard tile overflow fix
- SiteConfig.lcb_departments M2M (core 0035) + picker in Settings -> Channels -> Allocation & categories
  (local funds only, checkboxes). reports/services/treasurer _lcb_dept_ids/_lcb_depts use the configured
  set expanded to include sub-accounts (children), falling back to name matching when unconfigured.
  departments.lcb_fund() also honours the config (first selected dept).
- Dashboard: .stat .value now uses clamp() font-size with overflow-wrap so long values (e.g. Total
  receipts) no longer spill over the tile. (collectstatic on deploy.)
- Tests: reports/test_lcb_config.

## v1.72.0 - receipt archive (#2) + monthly treasurer report rework (#6)
- #2: ExpenseAttachment.file upload_to is now a callable (expense_receipt_path) filing by the expense's
  INCURRED year/month (cashbook 0020). New ReceiptArchiveView (/expenses/receipts/) groups receipts by
  month for printing, with a ZIP download of a period (organised by year/month + index.txt). Link added
  to the expense list.
- #6: monthly treasurer report reworked. (c) trust + LCB trends now 3 months (current + previous two).
  (d) all LCB accounts listed via name match (_lcb_depts), new ones appear automatically. (e) five-year
  trend rendered as a vendored Chart.js bar chart (yearly_json). (f) LCB expenditure fixed - was matching
  the wrong 'LCB Departments' fund via lcb_fund(); now aggregates all LCB-named departments
  (_lcb_dept_ids). (g) removed 'Local funds (with activity)' and the income & expenditure statement.
  (h) new local_funds_statement (opening/receipts/expenses/closing). (i) full SoFP (trust receipted/
  unreceipted split, advances, prepaid, pending, unallocated/allocated), full cash-flow (operating/
  investing), and full reconciliation (bank/adjusted/book/difference) mirroring the main reports.
- Tests: cashbook/test_receipt_archive, reports/test_treasurer_rework (+ updated test_item_batch &
  test_report_fixes for the new section names).

## v1.71.0 - reconciliation polish + petty cash + feed tile (#1,#3,#4,#5)
- #1: reconciliation_detail redesigned - KPI summary strip (bank/adjusted/book/difference+status),
  reconciling items grouped into Add/Less sections.
- #5: ReconciliationDetailView add_petty_cash action adds the petty-cash float (via _petty_balance_asof)
  as a CASH_AT_HAND reconciling item (ADD), idempotent; suggestion panel explains it isn't double-counted.
- #3: PettyCashView gains ?export=csv/xlsx + ui/period_selector.html; download buttons added.
- #4: DashboardView exposes live_balance (latest_cleared_balance from the CBS feed); dashboard.html
  shows a 'Bank balance (live feed)' stat tile when a feed balance exists.
- Tests: statements/test_recon_pettycash.

## v1.70.0 - report fixes & polish (#1-#8)
- #1: fixed broken card structure on /ledger/reconciliation/ (orphaned divs); wrapped equation +
  fund-vs-GL tables in proper cards; added eq.net to accounting_equation().
- #2: Excel/CSV export on JournalView (?export=) and ReconciliationReportView (?export=), with buttons.
- #3: MonthlyTreasurerReportView is now report-form & detailed - masthead, per-fund collections detail,
  itemised income statement (revenue lines + expense categories), sign-off block.
- #4: financial position trust payable now split via trust_summary - receipted (to_remit) vs
  not-yet-receipted (closing remainder); pending bank receipts shown separately as suspense; balance
  sheet still ties.
- #5: Historical data reachable from Reports index (card) and an Annual summary header button.
- #6: income statement section headings changed from BLOCK CAPS to normal case.
- #7: historical page - each year expands to show its months with per-month delete and a 'delete all
  YEAR data' action (HistoricalMonth.month_label added; delete_year_all action).
- #8: shared ui/period_selector.html (start/end + month/quarter/year presets) added to the income
  statement and changes-in-net-assets reports; parse_period gained ?period= presets.
- Tests: reports/test_report_fixes (+ updated test_item_batch wording).

## v1.69.0 - monthly historical records (A) + SoFP clarity (B) + monthly treasurer's report (C)
- ITEM A: HistoricalYearManageView extended with per-month records, Excel import + sample download
  (?sample=1), and automatic yearly-total recomputation from months (_recompute_year). HistoricalMonth
  model already existed.
- ITEM B: financial_position splits 'Trust funds payable' into receipted (trust dept closings) vs
  not-yet-receipted (pending bank suspense), adds trust_total_payable to context, and adds plain-language
  explanations of unallocated (general) vs allocated (Board-designated) net assets.
- ITEM C: new reports/services/treasurer.py + MonthlyTreasurerReportView at /reports/board/ (old board
  report kept at /board-classic/). 10 compact sections: collections summary; trust receipted 4-month
  trend; LCB sub-account 4-month trend; 5-year YTD trend (live actuals blended with monthly history);
  LCB expenses by category; local funds (sorted); income statement; financial position; cash-flow
  statement; latest reconciliation. Each has a one-line note; an AI headline (via _llm_call) with a
  rule-based fallback. New compact/printable template.
- Tests: reports/test_item_batch.

## v1.68.0 - report accuracy: bank position, cash flows, duplicate detection (#11-#14)
- #11: StatementOfCashFlowsView operating bucket = total non-remittance - capital, so the three
  sections always sum to total expenses and the statement reconciles even with untyped expenses.
  Financial-position identity verified (assets == liabilities + net assets).
- #12: BankPositionView subtracts bank-method PAID expenses NOT linked to a bank_transaction (avoids
  double-counting linked ones); new statements.services.importer.latest_cleared_balance() surfaces the
  real-time CBS feed balance with a difference line.
- #13: _duplicate_offerings collapses split siblings (shared core_ref base / mpesa_ref+date / ref+date)
  into one gift, so split halves aren't flagged as duplicates of each other or the receipting envelope.
- #14: bank+envelope duplicate detection now requires the two entries to fall within window_days (7)
  of each other instead of merely the same month - removes coincidental same-amount false positives.
- Tests: core/test_duplicate_logic, reports/test_position_reports.

## v1.67.0 - envelope collapse (#7) + campaign delete (#8) + dev-group SMS (#9) + rule edit (#10)
- #7: each Sabbath's envelope table is a collapsed <details> (head/totals/actions stay visible;
  auto-opens when a Sabbath filter is active).
- #8: CampaignDeleteView (/pledges/campaigns/<pk>/delete/), treasurer-only, blocked if the campaign has
  pledges; delete button on campaign detail. (Transfers already reverse via FundTransfer.reverse.)
- #9: DevGroupSmsView (/dev-groups/sms/ and /dev-groups/<pk>/sms/) sends a templated SMS
  ({name}/{group}/{church}) to dev-group members (all or one); buttons on the funds list.
- #10: RuleEditView (/rules/<pk>/edit/) + Edit button on the rules list; an 'Allocation & categories'
  card added to Settings -> Channels linking to the rules manager.
- Tests: departments/test_batch_b.

## v1.66.0 - safety & audit hardening (#1-#6)
- #1: DebitResolveView.post calls block_if_locked(txn.date) up front — debits can no longer post
  expenses/transfers into a locked period.
- #2: ExpenseApprove reject sets new Expense.rejected_by (cashbook 0019) and no longer sets approved_by.
- #4: rejecting an expense sends a 'REJECTION' notification to the original submitter (optional note).
- #3: new core.utils.log_exception(); a 'treasury' logger added to LOGGING (-> console + error_file).
  Broad excepts across cashbook/giving/departments/members/pledges/assets/core/statements/reports views
  + allocation/matching services now log a full traceback before showing the generic message (32 sites).
- #5: htmx vendored to static/vendor/htmx.min.js (1.9.12) and served locally; removed the unpkg CDN
  <script>. Run collectstatic on deploy.
- #6: _block_if_locked deduplicated — single core.utils.block_if_locked, imported by giving & cashbook.
- Tests: cashbook/test_safety_fixes.

## v1.65.0 - collection accounts + lifecycle (#1,#2) + cheque register (#3) + pending-receipts fix (#4)
- #1: Department.collection_only — receives income but excluded from expense pickers (save() forces
  show_in_expenses off). ConsolidateView (/departments/<pk>/consolidate/) creates one FundTransfer per
  non-zero sub-account into the parent in a single atomic op; children zero, history preserved.
- #2: Department.status (ACTIVE/CLOSED/ARCHIVED; save() derives active). DepartmentStatusLog audit
  trail. CloseAccountView (guards zero balance via fund_balance), ArchiveAccountView, ReopenAccountView,
  HistoricalAccountsView. Closed/archived excluded from income/expense pickers (active=False) but stay
  in reports. Department migration 0017.
- #3: ChequeRegister model (cashbook 0018) + ChequeRegisterView (/cheques/): add/clear/bounce/cancel/
  reopen, sync from CHEQUE-method expenses & cheque remittances. unpresented_cheques_total() wired into
  the bank reconciliation (lists unpresented cheques as at the statement date + one-click 'add as items').
- #4: pending_receipts_total excludes bank credits receipted via envelope (processed_via_envelope /
  manual_receipt / excluded_from_income) so they no longer appear as 'Receipts Pending Allocation'.
- Tests: departments/test_collection_lifecycle, cashbook/test_cheque_register, reports/test_pending_receipts.

## v1.64.0 - period-correct SoFP settlement (#4) + thank-contributors SMS (#5)
- #4: open_payables_total/open_accruals_total are period-based when given an as-of date — an item is a
  liability if incurred on/before the date and either not settled or settled after it (settled_on > as_of).
  Fixes the SoFP showing an item as paid when it was settled a day after the statement date.
- #5: new FundThankSmsView (/reports/fund/<pk>/thank-sms/) + button on the fund report. Lumps each
  member's confirmed giving to the fund AND its sub-accounts over the selected period, skips members
  with no phone, and sends a customizable templated SMS ({name}/{amount}/{fund}/{period}/{church}) via
  the existing SMS service; treasurer-only to send, read-access preview. Tests:
  cashbook/test_period_settlement, reports/test_thank_sms.

## v1.63.0 - fund cards include sub-accounts (#1) + debit->petty cash (#2) + delete recurring (#3)
- #1: FundLedgerView computes combined_opening/combined_receipts/combined_closing (parent + sub-accounts
  + dev groups); the top cards show these with an 'incl. sub-accounts' note when sub-accounts exist.
- #2: DebitResolveView gains a 'petty_cash' kind that records a PettyCashTopUp from the bank debit
  (moves bank->cash on hand, not booked as an expense); option added to the debits form.
- #3: new RecurringDelete view/URL + Delete button on the recurring list; generated expenses are kept.
  Tests: reports/test_fund_combined, giving/test_debit_petty, cashbook/test_recurring_delete.

## v1.62.0 - off-site backup storage (#5)
- #5: new SiteConfig.offsite_backup_enabled/url/user/password (migration core 0034). New
  backup.upload_offsite() does a dependency-free authenticated HTTPS PUT (WebDAV/Nextcloud/object
  stores). backup_db command gains --offsite (and auto-uploads when enabled); a "Send a backup
  off-site now" button (OffsiteBackupNowView /backup/offsite-now/) uploads an encrypted copy on
  demand. Backup emails now use the configured SiteConfig SMTP connection (so the port-465 fix
  applies). Settings -> Backup gains the off-site fields. Tests: core/test_offsite_backup.

## v1.61.0 - cash flow forecasting (#6) + executive forecast & pledges KPIs (#7)
- #6: new core/services/forecast.py projects cash position over 30/91/365 days from a 6-month giving
  run-rate, scheduled recurring expenses (precise due dates) + a discretionary spend run-rate, and
  outstanding pledge installments. New CashFlowForecastView (/reports/forecast/) with a Chart.js line
  chart and a per-horizon breakdown; linked from the reports index.
- #7: the existing executive overview already covered giving-this-month, budget compliance, department
  performance, giving trends and pie/bar/trend charts; added a Cash flow forecast section (30d/quarter/
  year projected positions) and an Outstanding pledges figure. Tests: core/test_forecast.

## v1.60.0 - payables/accruals/prepayments CRUD + link-existing settle (#1) + pledge delete (#4)
- #1: edit/delete for Payable, Accrual, Prepayment via _ObligationEditView/_ObligationDeleteView;
  settled payables/accruals are read-only and undeletable. New SettleAgainstExpenseView
  (/payables/<kind>/<pk>/settle-existing/) links an already-entered, unlinked expense to a
  payable/accrual and marks it settled without creating a second expense. New templates
  obligation_edit.html + settle_existing.html; action buttons added to accruals.html.
- #4: new treasurer-only PledgeDeleteView (/pledges/<pk>/delete/); PledgePayment links cascade but
  the underlying contributions remain in the ledger. Delete button on pledge detail; is_treasurer/
  can_enter_data added to PledgeDetailView context. Tests: cashbook/test_obligation_crud,
  pledges/test_pledge_delete.

## v1.59.0 - quarterly/yearly recurring expenses (#2) + tag-aware update check (#3)
- #2: RecurringExpense.Frequency gains QUARTERLY and YEARLY (frequency max_length 8->10; migration
  cashbook 0017). due_dates() generalised to step by 1/3/12 months anchored to the start month.
- #3: updates.latest_release() now falls back to the GitHub tags API (newest by semver) when no
  published Release exists, fixing 'Latest release seen (none)' for a tag-only repo. New _fetch_json
  helper; diagnostics updated. Tests: cashbook/test_recurring_freq, core/test_update_check.

## v1.58.0 - itemised camp budgets (#1) + email SSL fix (#2)
- #1: BudgetLine reworked from category-keyed to named items (new `name`; `category` now optional/
  informational; unique per department/year/name; migration cashbook 0016). New Expense.budget_line FK
  (nullable) tags an expense to its budget item. Expense form shows a 'Budget item' picker populated
  per selected fund via new BudgetItemsJSONView (/expenses/budget-items/). FundBudgetView now reports
  budget-vs-actual per item (actuals from tagged expenses) and notes untagged spend. Categories remain
  for overall expense categorisation.
- #2: core/services/email._connection now selects implicit SSL for port 465 (use_ssl) and STARTTLS for
  587 (use_tls); they're mutually exclusive. New SiteConfig.email_use_ssl (auto-enabled for 465;
  migration core 0033), surfaced in Settings -> Email. Fixes SMTPServerDisconnected/timeout on 465.
  Tests: cashbook/test_fund_budget, core/test_email_ssl.

## v1.57.0 - settle via editable expense form (#5) + camp/fund budgets & goals (#7)
- #5: settling a payable/accrual links to the expense form pre-filled (department, description, amount,
  category) via ?settle=payable:N / accrual:N. ExpenseCreate gained _settle_target/get_initial/
  get_context_data and a form_valid hook that, after saving (including any charge), marks the
  obligation settled and links settled_expense. The payables page settle buttons are now GET links;
  the form shows a banner. Old POST settle endpoints remain (unused).
- #7: new cashbook.BudgetLine (department, year, category, amount, note; unique per dept/year/category;
  migration cashbook 0015). New Department.contribution_goal and Department.year_goal (editable;
  migration departments 0016). FundBudgetView at /reports/fund/<pk>/budget/ shows per-category
  budget-vs-actual for a year and two goal cards (contribution goal + yearly goal) tracked against
  collected receipts, with forms to edit goals and add/update budget lines. Linked from the fund
  ledger. Tests: cashbook/test_settle_form, cashbook/test_fund_budget.

## v1.56.0 - bank-feed balance card, audit log filters/download, faster executive
- #1: BankFeedLogView extracts the latest ClearedBalance from event payloads (case-insensitive,
  nested-safe) and shows it as a card; each row can expand its pretty-printed raw JSON payload.
- #2: AuditLogView rewritten with search (q), filters (model, change type +/~/-, user, date range),
  pagination (50/page) and CSV export; user/model lists drive the dropdowns.
- #4: ExecutiveDashboardView no longer runs health.anomalies() (slow); added fast dashboard.quick_facts()
  (top fund this month, top spend category, givers this month, largest single gift) shown in an
  'At a glance' card. Tests: statements/test_feed_log, reports/test_audit_log, core/test_executive_facts.

## v1.55.0 - profile rights on leader pages + faster, smarter Controls duplicates
- #3: leader views called mask_phone() directly, bypassing the rights system, so a profile granting
  view_member_phone_full had no effect. All leader phone/identity output now goes through
  display_phone()/new display_giver(). Leaders keep seeing giver names by default (added to LEADER
  group rights) but phones stay masked unless a profile grants the right; a profile can also withhold
  identity. Fixed a NameError by threading `user` through _collection_rows().
- #6: ControlsView no longer computes duplicates on load (~887 queries -> ~24); _duplicate_expenses
  and _duplicate_offerings now run on demand via ControlsDuplicatesView (HTMX "Run check" buttons,
  /controls/check/<kind>/). _duplicate_offerings rewritten: no longer flags a shared allocation
  reference (distinct bank gifts each have a unique receipt); flags same giver+amount counted on
  both bank and envelope within a month, or an envelope re-typed in one Sabbath. Tests:
  core/test_rights_leader, core/test_controls_duplicates.

## v1.54.0 - clickable report links in Telegram replies
- New SiteConfig.site_base_url (migration core 0032), editable under Settings -> Telegram.
- The Telegram assistant formatter turns a report's relative link into a full clickable URL using
  that base; it adds https:// if the scheme is omitted and trims a trailing slash. With no base set,
  replies remain text-only (graceful). Tests: core/test_telegram_links.

## v1.53.0 - recategorise type, simpler leader view, fund sub-account sort + JPEG
- #1: ExpenseRecategorizeView download gains "Current type"/"New type (capital/recurrent)" columns;
  the re-import now updates expenditure_type as well as category (each optional, keyed on the ID).
- #2: leader department detail removes the Chart.js insight charts; the "Recent expenses" card is
  hidden for funds that aren't expense-eligible; the sub-accounts table shows just name + total
  contribution when no subgroup carries expenses; and a JPEG download (with a date/time stamp) is
  offered for the subgroups. expenses_eligible / any_sub_expenses flags added to the context.
- #11: FundLedgerView sorts sub-accounts and dev rows by receipts (descending); the fund report's
  sub-accounts table gains a JPEG download.
- New static/js/table_jpeg.js: a dependency-free table->JPEG export (canvas, no html2canvas/Pillow)
  with title, subtitle and a "current as of" timestamp, used by both the leader and fund pages.
  (Run collectstatic on deploy.) Tests: cashbook/test_recategorize_type, leaders/test_dashboard_simplified,
  reports/test_fund_subaccount_sort.

## v1.52.0 - split-confirm fix, split funds in the queue, smarter Telegram
- #8: AutoAllocationReviewView (the 'require confirmation' screen) silently re-pointed split
  components to the dropdown's first option, because split halves aren't selectable and so weren't
  pre-selected. The picker now always includes each row's current fund (pre-selected), split
  components are shown locked, and the POST never re-points a split component.
- #9: the queue's manual Split rows can now target a split fund (the combo offers them); a split-
  fund part is expanded across its components server-side, so e.g. 600 to Combined Offering becomes
  300 ENF + 300 LCB within the wider split.
- Telegram /balance with no fund now lists every (parent) fund's closing balance with a grand total;
  /balance <fund> still gives the full breakdown. The free-text handler now renders the assistant's
  rows and report link properly.
- Telegram LLM report routing: when the assistant LLM is enabled, free-text questions are first
  classified by the LLM into a known report intent (+fund/period) and routed to that report via the
  existing rule engine; otherwise it falls back to a conversational answer. _llm_call gained an
  optional system-prompt override. Tests: statements/test_split_confirm, giving/test_queue_split_funds.

## v1.51.0 - cash-form dev group requirement + petty cash mirrors the expense form
- #7: CashEntryForm gains a dev_group field, shown on the cash form only when a DEVELOPMENT fund
  is picked (fund search now returns a `dev` flag) and required by clean() — a development gift
  can't be saved without its group.
- #10: petty cash disbursements mirror the expense form — method (cash/bank/M-Pesa/cheque),
  voucher, and an M-Pesa/bank transaction charge (for floats held on M-Pesa/bank). The charge is
  a linked bank-charge expense (charge_for) that is also paid_from_petty_cash, so it reduces the
  float; the float check includes it. A petty-cash disbursement is a normal Expense flagged
  paid_from_petty_cash=True (that flag is exactly what differentiates it and reduces the float).
  The regular expense form gains a "Paid from petty cash" checkbox, and the expense import gains
  a "Paid from petty cash" column (imported petty expenses are recorded as PAID). The manual/import
  charge inherits the parent's petty-cash flag. 7 tests (giving/test_cash_devgroup,
  cashbook/test_petty_charge).

## v1.50.0 - statement dedup, unassigned-page crash, notifications, ledger export column
- #5: reports/dev_unassigned (and the sabbath queue + pledge suggestions) crashed with
  VariableDoesNotExist when a row had no member, because `default:t.member.name` still evaluates
  member.name. Replaced with an explicit {% if %} guard.
- #6: the statement parser took the first '~' segment as the receipt, so mobile/bank-channel
  narrations ("NNNNNN:MBANKING~<REAL RECEIPT> ...") yielded non-unique keys and distinct payments
  could be dropped as duplicates. It now extracts the genuine 10-char M-Pesa receipt code
  (letters+digits) anywhere in the narration, falling back to the first segment only when absent.
- #3: the notifications page now lists only unread items (so they disappear once read) and each
  has a Dismiss action; "Mark all read" empties the list.
- #4: the transactions ledger export (xlsx/csv) gains a "Receipt status" column — Receipted
  (envelope) / Receipted (manual) / Memo (reconciled to envelope) / Not receipted.

## v1.49.0 - split-fund allocation fix + M-Pesa charge on expenses
- Allocation (#1): AllocationRule.reference is not unique, so a reference could have two rules
  (e.g. a stray learned 'remember this' to one account, plus the real split-fund rule). _pick
  now prefers, among rules covering the date: period rules, then an explicit split_fund over a
  bare department, then the newest rule. Pattern matching gets the same split-fund/newest
  tiebreak. So a configured split fund (Combined Offering) is never overridden by an older
  single-account rule (13th Sabbath). Hardening: the legacy importer's split-rule seeding now
  update_or_creates (so it can't be blocked by a pre-existing department rule), and a learned
  department rule clears any stale split_fund. 4 tests in giving/test_split_priority.py.
- Expenses (#2): new Expense.charge_for self-link (migration cashbook 0014). The manual form's
  M-Pesa/bank charge now links the generated bank-charge expense to its parent; the expense
  import template gains an 'M-Pesa charge' column that does the same on import. The expense
  detail page shows linked charge(s) and, on a charge, the expense it was for. 5 tests in
  cashbook/test_mpesa_charge.py.

## v1.48.0 - run allocation rules on the review queue on demand
- giving.services.allocation.reallocate_pending(): re-runs allocate() (+ dev-group token and
  campaign fallback, via the importer's _resolve) over the credits still in the review queue and
  updates each in place when it now resolves to a fund. Skips locked periods and split-fund
  matches; returns a {scanned, allocated, remaining, skipped_locked, skipped_split} summary.
- RunRulesOnQueueView (POST /queue/run-rules/, data-entry right) with a clear result message.
- "Run rules on pending" button added to the review-queue toolbar (shown when there are items).
  Use case: add rules after importing a statement, then clear the matching queued items without
  re-importing the file.
- 5 tests in giving/test_reallocate.py (matching allocated/others left, no-rule no-op, locked-
  period skip, the view, button visibility).

## v1.47.0 - Telegram envelope entry (configurable)
- Bot (#3): new guided /envelope flow in core/services/telegram_bot.py — Sabbath -> member
  (name match; ambiguity prompts; optional new-member creation) -> amount per configured fund
  (0/- to skip) -> optional confirmation -> save. Records via the same envelopes.views._save_envelope
  used by the web ledger, so it posts ENVELOPE-channel income and flows into reconciliation/reports.
  Respects locked periods (entry_blocked) and attributes the entry to the signed-in user (personal
  PIN), behind the existing PIN gate.
- Parameters on Settings -> Telegram (SiteConfig, migration core 0031):
  telegram_envelope_enabled, telegram_allow_new_member, telegram_envelope_confirm,
  telegram_envelope_channel (cash/bank) and telegram_envelope_funds (which funds are offered;
  empty = active top-level funds). Surfaced on the settings page; saved with the config form.
- 9 tests in core/test_telegram_envelope.py: full flow, skip-fund, new-member gating on/off,
  feature disabled, locked-period block, confirm-off immediate save, PIN-required, attribution.

## v1.46.0 - executive/controls speed-ups, aggregate caching, query-regression guards
- Controls (#2): _duplicate_expenses grouped expenses by service_sabbath_for(), which queried
  SiteConfig + closed-Sabbath rules per row (~8,000 queries on 4k expenses). It now groups by the
  pure natural Sabbath (sabbath_of, no DB) — correct for dedup and 1 query. Controls: ~887 q /
  4.8s -> 29 q / 77 ms.
- Executive (#2): health.anomalies() did a per-expense fund-average query and also invoked the
  expensive dedup; fund averages are now computed once and the dedup fix carries through.
  Executive: ~670 q / 5.1s -> ~239 q / 325 ms.
- Caching (#1): core.perfcache caches department_summary/trust_summary keyed by a global data
  version that is bumped on any Transaction/Expense/RemittanceBatch/FundTransfer write, with a
  TTL backstop. Off by default (DASHBOARD_CACHE_TTL=0); set DJANGO_DASH_CACHE_TTL=60 in prod.
- Regression guards (#1): core/test_performance.py asserts the hot pages stay under a query
  ceiling on a seeded dataset (catches N+1 regressions) plus cache hit/bust/off-by-default tests.

## v1.45.0 - performance at high volume
- Expenses list: eliminated an N+1 (a per-row `attachments.exists()` query). The receipt
  indicator is now an annotated Count in the main query — measured 66 -> 16 queries on a
  50-row page over 5,000 expenses.
- Member list: added a database index on `name` (migration members 0004) so the default
  name-ordered listing and search don't sort-scan at tens of thousands of members.
- Audited the hot paths on an 18,142-transaction / 5,042-expense / 4,010-member dataset:
  transactions (16 q), members (13 q), dashboard (52 bounded aggregate q, ~87 ms), review queue,
  audit log, fund ledger, trust, reports — all query-light with no N+1. The transactions page's
  one-off ~400 ms first hit was template/app warmup (38 ms warm); no code change needed.

## v1.44.2 - error monitoring, email config, log files
- Logging: server errors (django.request / django.security) now go to a rotating file
  (logs/treasury-errors.log, 5x5MB; dir configurable via DJANGO_LOG_DIR) and to an
  AdminEmailHandler that emails ADMINS on 500s when configured (no-op until set, so nothing
  breaks by default).
- Email: configurable via DJANGO_EMAIL_HOST/PORT/USER/PASSWORD/TLS, DJANGO_FROM_EMAIL,
  DJANGO_SERVER_EMAIL and DJANGO_ADMINS; defaults to the console backend when no SMTP is set so
  the app and the backup emailer degrade gracefully. Also wires DEFAULT_FROM_EMAIL/SERVER_EMAIL.
- Optional Sentry: set SENTRY_DSN (and optionally SENTRY_TRACES/SENTRY_ENV) to enable; guarded
  import means a missing sentry-sdk never breaks startup.
- (The encrypted, rotated, off-site backup_db cron command was already present — documented in
  its module docstring.)

## v1.44.1 - audit fixes & hardening
- Security: dashboard/report chart JSON is now emitted through a safe_json() helper that escapes
  <, >, & and line separators, so user-set fund/member names can't break out of the <script>
  block (low-severity stored-XSS hardening; dashboards are staff-only).
- Stability: the in-app Telegram poller no longer starts (or queries the DB) during `check`,
  `showmigrations`, `sqlmigrate` or `createsuperuser` — removes a DB-access-at-init warning.
- Cleanup: removed a redundant cumulative-receipts query in trust_summary (no behaviour change).
- Tests: pledge matching tests pin an explicit pledge start_date so they no longer depend on the
  current date.

## v1.44.0 - configurable profiles & rights (layered on roles)
- core/rights.py: a catalogue of granular rights (data entry, money controls, setup, reports,
  sensitive data) and resolution layered on the role groups — superuser = all; a user with
  assigned profiles is bound by the union of those profiles (can restrict); a user with none
  falls back to their role group's implied rights (full backward compatibility).
- accounts.Profile model (name, description, rights JSON, users M2M, is_system). Migration
  accounts 0003 + 0004 (four default profiles mirroring the role groups).
- Profiles management page (/profiles/) — create/edit/delete profiles, tick rights grouped by
  area, assign users. Gated by the manage_profiles right. Nav link beside Users & roles.
- Phone masking: member phone numbers are shown full only to viewers with view_member_phone_full
  (treasurer/assistant/auditor groups keep it by default); otherwise masked (e.g. *********678)
  in the member list, member detail, duplicates, the member-search typeahead and envelope ledger.
- RightRequiredMixin + has_right() + context `rights`/`can`/`phone_full` for further wiring.
- 16 new tests covering rights resolution, masking, profile CRUD/assignment and backward compat.

## v1.43.0 - asset cost from expenses: idempotent, itemised, reclass-aware
- Accumulate (#1): AssetAccumulateView now only picks up capital expenses not already linked to
  an asset (capitalized_asset is null), links them, and adds their sum to the cost — so clicking
  twice can't double-count. The asset detail page lists every expense included in the cost with a
  linked total.
- Reclassify/delete (#3): cashbook signals keep the cost honest — reclassifying a linked expense
  to recurrent (or unlinking, reducing its amount, or deleting it) reduces the linked asset's cost
  by the right amount. A recurrent expense can never stay attached to an asset.
- Legacy importer (#2): creates a single "Church building" construction-in-progress asset and
  capitalises every development/construction expense onto it (expenditure_type=CAPITAL,
  capitalized_asset set); the building's cost is set to the sum of those expenses.
- Backup workbook Trust Funds + Summary sheets now show outstanding-to-remit (receipted) and
  unreceipted (pending) separately, consistent with the on-screen trust reports.

## v1.42.0 - trust receipted/unreceipted split, construction asset, ledger autocomplete fix, budget quarter
- Trust (#1): trust_summary now splits cumulative trust receipts by whether a formal receipt was
  issued (envelope channel or manual_receipt). `to_remit` = opening + receipted − remitted (the
  firm liability due to the field); new `unreceipted` line = confirmed trust money with no receipt
  yet (still a liability, held off remittance); `total_liability` = to_remit + unreceipted.
  Surfaced on the Trust Fund Report, Remittance advice, Conference submission export, remittance
  dashboard and main dashboard. Remittance batches (which use to_remit) therefore only remit
  receipted money. Tests updated to the new policy.
- Assets (#2): new FixedAsset category "Construction in progress" that never depreciates (Land
  also corrected to not depreciate); NBV = accumulated cost. AssetAccumulateView totals CAPITAL
  expenses (approved/paid) on a chosen fund over any date range — including prior years — to set
  or add the asset's cost; manual cost editing remains. Migration assets 0003.
- Envelope ledger (#3): name autocomplete dropdown was being clipped by the scrolling table
  wrapper (overflow:auto). The suggestion box is now position:fixed, positioned from the input,
  and hidden on scroll/resize.
- Budget (#3a, from 1.41 work): BudgetLine.quarter (Q1–Q4) for planned spend timing. Migration
  departments 0015.

## v1.41.0 - budget timing by quarter
- BudgetLine gained an optional `quarter` (Q1–Q4) for the period a fund foresees spending the
  line; surfaced in the budget-lines page (column + add-form dropdown) and carried over by
  "copy prior year". Blank = spread across the year. Migration departments 0015.

## v1.40.0 - subgroup export, structure-import flag, charge traceability, print fit, audit creator
- Fund ledger (#1): sub-accounts table now exports to Excel and CSV (ID, Subgroup, Type,
  Receipts, Payments, Closing) via ?export=subgroups[/-csv]; download buttons on the page.
- Fund structure import (#2): new "Show in expenses (Yes/No)" column (template, parser, apply);
  defaults to Yes, "No" hides the fund from the expense picker.
- Charge traceability (#3): the auto-created transaction-charge expense now references its parent
  ("... [for <voucher / exp #id>]") and copies the parent voucher.
- Offering/Collection summary (#4): prints to a single A4 landscape page — a measured scale
  factor shrinks the sheet to fit when there are many funds/Sabbaths.
- Backup audit (#5): backup workbook adds a "Created by" column (from simple-history's create
  record) to Transactions, Expenses, Members, Departments and Reconciliations. Audit-only — not
  shown anywhere in the UI or on-screen reports. No schema change.

## v1.39.0 - Collections Detail report
- New /reports/collections-detail/ (CollectionsDetailView, PeriodMixin): collections for any
  chosen period broken down by fund, with Trust/Local subtotals and a grand total. Uses the same
  definition as the Collections Summary (confirmed credits, excluded_from_income=False; trust via
  is_trust), so totals reconcile exactly for matching dates. Headline strip shows Collections,
  Trust, Local, Expenditure and Net for the period.
- Excel (.xlsx) and CSV downloads. Linked from the reports index and the Collections Summary page.
- monthly.collections_detail() service added.

## v1.38.1 - campaign fallback splits to subgroups (fix)
- campaign_allocate now returns the matched member's subgroup fund, not the campaign's parent.
  Campaign.subgroup_department() gets-or-creates a child Department named after the member's
  group (e.g. CAMP_1), parented to the campaign's department so it inherits fund_type/is_trust
  and rolls up in trust/local reports. A member with no group still routes to the parent fund;
  a trigger match with no member still routes to the parent for review.
- Updated CampaignFallbackTests to assert subgroup routing + parent fallback.

## v1.38.0 - campaigns polish + smart bulk buttons
- Campaigns (#1): redesigned page (clean create form + campaigns table with per-row member
  upload). New "Sample upload file" download (Name, Mobile, Group). Import is now tolerant —
  numeric phone cells handled, bad/empty rows skipped and counted, no abort on a single bad row.
- Phone overflow fix: CampaignMember.save() stores only a normalised 12-digit phone (or blank),
  so the import can never raise DataError 1406 ("Data too long for column 'phone'").
- Expenses (#2) & Transactions (#3): bulk action buttons moved into the filter toolbar beside
  "Apply filters" (via the form= attribute), disabled by default and enabled only when the
  selected rows include items eligible for that action (Approve↔PENDING, Reject↔PENDING/APPROVED,
  Pay↔APPROVED, Delete↔any; Reverse↔any reversible row).

## v1.37.0 - transactions list bulk reverse
- TransactionBulkReverseView reverses several selected ledger entries at once (contra
  postings, never hard delete; linked envelope receipts removed and their siblings reversed).
  Locked-period and already-reversed/reversal rows are skipped and counted.
- Transactions list gains row checkboxes + select-all + a "Reverse selected" bar; the per-row
  Reverse button is removed (Edit / Split / Receipt / cash Delete stay per row).

## v1.36.0 - expenses bulk actions + ledger/backup IDs
- Expenses (#2): row checkboxes + select-all and one action bar (Approve / Reject / Mark paid /
  Delete) via ExpenseBulkActionView; per-row buttons removed, Edit kept. Each item is guarded
  the same as the single action (locked periods and dual-approval-needed items are skipped and
  counted, not errored).
- Fund-ledger export (#4): added ID and Type (Receipt / Expense / Transfer) columns so every
  line is traceable to its source row.
- Backup workbook (#5): Transactions, Expenses and Reconciliations sheets now lead with the
  database ID (Departments/Members already did); money-column indexes shifted accordingly.

## v1.35.0 - campaign fallback allocation
- New Campaign + CampaignMember models (giving). A Campaign has a fund (department), a set of
  comma/line-separated trigger words, and an active flag; members carry name/phone/group.
- giving.services.allocation.campaign_allocate runs ONLY after the normal allocate() misses:
  if the reference contains a campaign trigger word, the payer is matched by phone (or a
  unique name) to a campaign member and the credit is allocated to the campaign's fund and
  tagged with the member's group (AUTO); trigger-but-no-member routes to the fund as REVIEW.
- Wired into both the file importer and the live CBS feed (ingest_event); Transaction gains
  campaign (SET_NULL) + campaign_group so the group is reportable and survives campaign delete.
- UI at /campaigns/: create/update a campaign, upload its Name/Mobile/Group sheet (.xlsx/.csv),
  delete a finished campaign (members removed; past allocations keep their group tag). Nav link
  added. Regression tests cover trigger gating, phone/name matching, no-member review, inactive.
- Migration: giving 0018.

## v1.34.3 - CBS webhook token auth hardening
- CbsEventWebhookView TOKEN auth now accepts the shared token whether the bank sends it as a
  bare Authorization header, with a Bearer/Token scheme, or via X-Auth-Token / X-Api-Key /
  Api-Key / Token headers, and compares it in constant time (hmac.compare_digest).
- Confirmed the feed allocates incoming credits via the same allocate() rules as the
  statement importer (member match, split funds, dev-group tag, dedup, confirmation gating).

## v1.34.2 - mark-receipted now memos the bank credit (fixes inflated collections)
- Transaction.mark_manual_receipt now, for BANK credits, also sets excluded_from_income=True
  and nulls the department (the legacy "Processed via envelope" memo) when marking, and
  re-includes on un-mark. Previously it only set the manual_receipt flag, so under the new
  income-from-envelope model the credit stayed as income and double-counted the envelope
  it duplicated - inflating the dashboard and collections summary.
- This fixes all three callers at once: the bulk MarkProcessedImportView, the per-credit
  toggle, and receipt-one-bank's "mark only" paper-receipt path.
- The exclusion applies even when the credit was already flagged manual_receipt, so re-running
  the bulk mark-processed file settles credits marked before this fix.
- Full suite (458 tests) green; no migrations.

## v1.34.1 - cash count + report consistency for the legacy model
- Cash count (_breakdown): a BANK envelope now posts an ENVELOPE-channel transaction, but
  that is bank money, not physical cash. The count now excludes ENVELOPE transactions that
  belong to a bank-channel envelope (in both the cash total and the duplicate-matching
  heuristic), so the float still balances.
- Income reports that don't group by department now exclude the receipted bank-credit memos
  (excluded_from_income): income_by_channel, giving_by_group, offering_summary, tithe_total,
  dev_group_progress. Department-grouped reports already self-correct because a memo'd credit
  has department=None.
- Verified consistent (counted once) across: dashboard, collections summary, trust report,
  member statement, income-by-channel and the cash count. Full suite (479 tests) green.

## v1.34.0 - legacy accounting model: envelope is income, bank credit is a memo
- `_save_envelope` now posts an income transaction for BANK envelopes too (previously only
  cash), so the envelope is the income for all giving, matching the legacy import's
  phase_envelopes.
- Sabbath reconciliation INVERTED to match legacy: applying a match / marking a credit
  receipted now excludes the BANK CREDIT from income and nulls its department (the legacy
  "Processed via envelope" memo) - it no longer excludes the envelope's transaction. The
  envelope keeps its income, so the gift is counted once.
- reconcile_sabbath status is now "receipted" (excluded memo) vs "income" (still counted);
  a matched pair whose credit is still income is flagged as the double-count to clear, and
  `balanced` means no such double-count remains.
- _reverse_envelope re-includes a memo'd credit (clears excluded_from_income) on undo.
- New regression test locks the invariant: bank envelope + matching credit = double until
  receipted, then counted once (income AND fund balance). Full suite green.

## v1.33.0 - reconciliation status actions (mark receipted, cash->bank)
- ReconcileApplyView accepts two new pairing-free actions: `mark_receipted` (sets a bank
  credit and its split siblings to manual_receipt=True as a confirmation, no envelope link,
  no ledger change) and `to_bank` (reclassifies a cash envelope to bank and excludes its
  ENVELOPE-channel transaction from income to avoid overstating).
- reconcile_sabbath flags matched pairs as `miscat` when the bank credit is unreceipted but
  the envelope was entered as cash (the double-count case), and returns `miscat_count`.
- Unmatched bank table gains per-credit "mark receipted" checkboxes; the success message
  reports linked/receipted/moved counts separately.

## v1.32.1 - trust_reconcile accuracy for reconciled-and-excluded lines
- An envelope line whose transaction is excluded_from_income but whose envelope is linked
  to a bank credit (env.bank_transaction) is no longer reported as "offering but not
  collections" - the bank credit is the ledger entry and is already counted in collections.

## v1.32.0 - shared-name reconciliation match + receipt-only apply
- reconcile_sabbath suggestions now include a shared-name-token rule: within one amount,
  a name token (e.g. a first name) carried by exactly one remaining bank credit and one
  remaining envelope is suggested ("ADAM KEN" <-> "ADAM NYAN" when there is only one Adam
  of that amount). Suggestions are de-duplicated so no credit/envelope appears twice.
- ReconcileApplyView now marks the matched bank credit (and split siblings) as receipted
  (processed_via_envelope) WITHOUT changing the ledger: the credit stays as income and no
  envelope transaction is created. The existing duplicate-cash exclusion still applies only
  when a cash envelope is being reclassified as bank.

## v1.31.0 - smarter Sabbath reconciliation matching
- reconcile_sabbath auto-match is now conservative: it pairs a bank credit to an envelope
  only when the name+amount match is unambiguous (exactly one candidate on each side), so
  duplicates (two givers of the same amount, repeated names) are never mis-paired and are
  left for manual resolution.
- New unique-amount suggestions: any amount that appears exactly once among the remaining
  bank credits and exactly once among the remaining envelopes is surfaced as a suggested
  match (even when names differ), each confirmable with one tick. Returned as `suggestions`
  (list); the single-suggestion field is kept for compatibility.
- The reconciliation remains a detector/suggester only — it never posts a second ledger
  entry; hand-typed bank envelopes stay the offering record and the imported bank credit
  stays the income.

## v1.30.1 - trust_reconcile accuracy: respect env.bank_transaction
- The diagnostic previously counted any envelope line with no line-level transaction as
  "no ledger transaction", even when its envelope was linked to the imported bank credit
  (env.bank_transaction) — overstating the orphan figure. It now treats those as
  reconciled (the bank credit is the ledger entry) on both sides of the comparison.

## v1.30.0 - statement purge window extended to a week
- The statement-import Purge / Unlink-and-purge buttons now remain available for a
  week after upload instead of only the same day (StatementImport.can_purge, mirroring
  the bank-reconciliation delete window). All existing safety checks are unchanged:
  refuses inside a locked period or when expenses are linked (unless unlink is chosen).

## v1.29.0 - undo envelope entries (bulk reversal)
- New EnvelopeReversalView (/envelopes/reverse/, treasurer only): filter envelopes by
  Sabbath date and optional channel, preview the count/total, and reverse the batch
  with a confirm. Mirrors the bank statement import undo and respects locked periods.
- Reversal logic extracted into a shared _reverse_envelope helper used by both the
  single-envelope delete and the bulk reversal: it removes the ENVELOPE-channel ledger
  entries a cash envelope created, and for bank envelopes unlinks (keeps) the real bank
  deposit and clears its processed_via_envelope flag so it returns to the receipt queue.
- "Undo entries" link added to the envelope list for treasurers.

## v1.28.1 — revert bank-envelope ledger entry (keep diagnostic)
- Reverted v1.28.0: bank envelopes no longer create their own ledger transaction.
  Creating one risked counting the same gift twice once the bank statement (the real
  source of that money) is imported. _save_envelope is back to its prior behaviour and
  the backfill command is removed.
- Kept: the trust_reconcile management command.

## v1.28.0 — bank envelopes reach the ledger (trust/collections discrepancy)
- Root cause (found via trust_reconcile): manually-entered BANK envelopes created an
  envelope line with no ledger transaction, so the money appeared in the offering
  summary but never reached the cash book / collections / general ledger — the entire
  trust gap was these orphan lines.
- envelopes _save_envelope now creates one ENVELOPE-channel transaction per line for
  bank envelopes too (matching cash), so the money always reaches the ledger. To
  receipt money already imported from the bank statement, use the receipt-as-envelope
  action on that transaction (it links to the existing credit, so nothing doubles).
- New command backfill_envelope_transactions (report, or --fix) creates and links the
  missing transaction for existing orphan envelope lines. Run trust_reconcile first to
  confirm the orphan total, then rebuild the ledger after backfilling.

## v1.27.1 — trust reconciliation diagnostic
- New management command trust_reconcile <year> <month> reconciles the Offering
  Summary trust total (envelope lines, by Sabbath) against the Collections Summary
  trust total (transactions, by date) and itemises the difference: envelope lines
  with no ledger transaction, lines whose transaction is excluded or dates to
  another month, and trust collected with no envelope line or counted on another
  month's Sabbath. Both reports already use the same is_trust classification, so
  this isolates timing/data differences from genuine errors.

## v1.27.0 — reconciliation delete/recompute + split-fund allocation guard
- Bank reconciliations can be deleted within a week of creation (treasurer only,
  with a confirm). Older worksheets are protected. Reconciliations do not post to
  the ledger, so deletion is safe.
- Reconciliation detail: a one-click "Recompute from ledger" button refreshes a
  stale cash-book balance to the current figure as of the statement date, and the
  manual "Update book balance" now confirms when it saves.
- Allocation-rule form: the fund picker now lists only directly-allocatable funds,
  excluding the internal halves of a split offering, so a rule cannot send split
  giving entirely to one component. Rules should target the split fund itself.
  Also fixed unreachable validation in the rule form (the not-both-targets and
  date-range checks now run).

## v1.26.0 — trust classification single source of truth
Trust vs local was read from two places: the authoritative fund_type field (reports,
balance engine) and a cached is_trust flag (general ledger posting, envelope summary,
some pickers). If the two drifted — a bulk update or import that bypassed save() —
trust money could post to an income account instead of the trust liability, the
reports and the envelope summary disagreed, and the reconciliation couldn't balance.
- The general ledger now classifies trust strictly by fund_type (single _is_trust
  helper), so the ledger and the balance engine can never disagree. Once a fund's
  Fund Type is correct, every figure agrees and the reconciliation balances.
- New command, audit_funds, reports any fund whose Fund Type and envelope-summary
  classification disagree, and repairs in the direction you confirm:
    audit_funds                # report only
    audit_funds --from-cache   # trust the envelope summary: set Fund Type from it
    audit_funds --fix          # trust the Fund Type settings: set the cache from it
  No classification is changed automatically — you choose which source is correct.
- Regression test pins that a trust credit posts to the trust liability even if the
  cache is stale.
After repairing, rebuild the general ledger (Ledger check -> Rebuild) so existing
entries re-post under the corrected classification.

## v1.25.2 — backup authentication & ledger date filter
- Database backup/restore: the dump and restore tools now authenticate via a
  temporary [client] defaults file over TCP to the same host the application uses.
  Previously they passed -h localhost, which the command-line client treats as a
  Unix socket and can be denied even when the app connects fine — the cause of the
  'Access denied for user ... when trying to connect' error. They now also prefer
  the modern mariadb-dump/mariadb tools (clearing the deprecation notice) and drop
  options that need privileges shared-hosting users usually lack (--routines,
  tablespaces). Credentials are written to a 0600 temp file and deleted immediately.
- Ledger date filter: From/To dates are now parsed into real date objects before
  filtering (more reliable across database drivers) and malformed values are
  ignored instead of raising, so the filter always applies cleanly.

## v1.25.1 — one-click ledger rebuild from the Ledger check
When any fund does not tie to the general ledger, the Ledger check overview now
shows a clear explanation and a Rebuild button (treasurers only; others get a note
to ask a treasurer). This is the direct fix for an entry that is counted by a fund
but missing from the general ledger — it now both surfaces on the overview and is
fixable in one click, without drilling into each fund. Template-only change.

## v1.25.0 — summary reconciliation, amount search, accurate assistant
- Envelope/Offering summary: funds that received giving directly are now always
  listed, even if they also have sub-accounts (e.g. VBS). Previously such direct
  giving was silently dropped, so the summary total did not match the envelopes
  counted for the Sabbath. Both the per-Sabbath statement and the monthly summary
  are fixed; funds with no direct giving still do not appear.
- Ledger search: the search box now also matches by amount (type 1250 or 1,250.50)
  and by M-Pesa / bank receipt code, alongside name and reference.
- Assistant: all collection, tithe, giving, top-giver and development-group figures
  now use the recognised-income basis (confirmed credits, excluding reversed and
  double-counted envelope-twin rows) so they agree with the reports. Added a
  What is new answer that lists recent releases.

## v1.24.0 — wording: gift to contribution
Every user-facing use of the word gift or gifts now reads contribution or
contributions: dashboard, review queue, receipts, leader and department views,
reports, and spreadsheet/CSV export headers. The change is purely wording — no
totals, accounting rules, or behaviour were touched, and the underlying data keys
were left intact so all figures render exactly as before. Includes a no-op field
help-text migration.

## v1.23.0 — Latest Sabbath dashboard snapshot
The executive dashboard now leads with a Latest Sabbath card: the most recent
Sabbath's recognised collection, the change versus the previous Sabbath (up/down),
the number of gifts and envelopes recorded, and the top funds for that Sabbath. It
uses the same recognised-income basis as every other report (confirmed credits,
excluding the envelope-twin rows) so it never double-counts, and it is built from
grouped queries. Shown only when there is data for the latest Sabbath.

## v1.22.0 — keyboard-friendly entry & mobile receipting grid
Weekly envelope receipting grid:
- Spreadsheet-style keyboard navigation — Up/Down arrows move between rows in a
  column (Enter still moves down and adds a row at the bottom); the focused cell
  selects its contents so you can overtype immediately. Arrow keys are left alone
  inside dropdowns.
- Mobile/tablet: momentum scrolling, larger touch targets in cells, full-width
  toolbar fields and action buttons, and a two-column fund picker.
Cash and expense entry forms:
- The member, fund and claimant lookups were mouse-only; they are now fully
  keyboard-navigable (Up/Down to highlight, Enter to choose without submitting the
  form, Escape to dismiss), and the cash form lands focused in the first field.
No accounting or posting logic changed; 185 entry-related tests pass.

## v1.21.0 — professional print / PDF output for reports
- A comprehensive print stylesheet: printing any page (or saving to PDF) now hides
  all on-screen chrome — sidebar, top bar, filters, buttons, toolbars, action items
  and the on-screen page header — and lays the document out full width in black on
  white, ink-friendly (no shadows or solid fills; status pills print as outlines).
- Tables repeat their header row on every printed page and never split a row across
  a page break.
- Fix: the new sticky-header scroll caps were undone for printing, so long fund
  ledgers and journals print in full instead of being cut off at one screen.
- Reports now carry a print-only letterhead (church name, report title, period and
  the date/user it was generated) on 18 key reports, and a print-only signature
  block (prepared / checked / approved) on the monthly statement, remittance
  schedule, board report and financial position.
On-screen layout is unchanged — all of this applies only when printing.

## v1.20.0 — final design-system polish
Continued the rollout into the import wizards, executive summary, controls and the
remaining secondary tools. App-wide inline styles fell from ~370 to ~242; of those,
19 are JS-toggled visibility and 18 are dynamic templated values that must stay
inline, leaving ~205 genuine one-offs. (Since the modernization began the codebase
has gone from ~908 inline styles to ~242.) 117 pages verified rendering under
production settings with no failures; no behaviour or accounting logic changed.

## v1.19.0 — design-system rollout across secondary screens
Extended the component/utility adoption from the ten priority screens to the rest of
the app. Repetitive inline styling was replaced with shared utility classes
(merging into existing classes), cutting app-wide inline styles from ~908 at the
start of the sweep to ~370 — the remainder being data-driven values (e.g.
progress-bar widths) and a few genuine one-offs (bespoke backgrounds, JS-toggled
visibility, fixed pixel widths). Notable: settings 64->9, leader department detail
33->6, accruals 39->13, pledge detail 25->8. 117 pages verified rendering under
production settings; no behaviour or accounting logic changed.

## v1.18.0 — UI modernization & component-adoption sweep (part 2 of 2)
Completes the ten-screen sweep begun in 1.17.0.
- Fund Ledger — utilities + sticky running-ledger header.
- Journal Entries — modernized header and sticky headers.
- Bank Reconciliation — status summary rebuilt as stat tiles; the inline total-row
  style moved to the stylesheet; sticky comparison table.
- Contributions / Receipts (weekly receipting ledger + bank-gift receipting) —
  converted to utilities/components; the frozen member-name column behaviour is
  preserved exactly.
Across all ten priority screens, the only inline styles that remain are data-driven
values (e.g. progress-bar widths). Verified: ledger reconciliation, journal balance,
fund balances and the dual-approval gate are all unchanged (129 tests pass).

## v1.17.0 — UI modernization & component-adoption sweep (part 1 of 2)
Shared design-system components (reused across screens): toolbars, alerts/callouts,
filter bars, a responsive KPI grid, sticky table headers, and a set of spacing/layout
utility classes — plus reusable page-header, stat-card and empty-state partials.

Screens rebuilt on the component library (inline styles removed; only data-driven
values like progress-bar widths remain inline):
- Executive Dashboard — responsive KPI tiles, alert-style action items, cleaner charts.
- Transactions list — utilities + sticky headers; filters unchanged.
- Expenses list — utilities + sticky headers; approval/delete actions unchanged.
- Expense detail & approval — rebuilt with components; now shows inline Approve / Reject
  / 2nd-approve / Mark-paid actions that reuse the existing endpoint and enforce the
  same dual-approval threshold (no logic change).
- Pledges dashboard and Reports dashboard — converted to utilities/components.

All accounting behaviour, filters, and the dual-approval gate verified unchanged.
Remaining screens (Contributions/Receipts, Fund Ledger, Journal Entries, Bank
Reconciliation) follow in part 2.

## v1.16.0 — design-system foundation, security hardening & responsive polish
Security & stability
- Production now fails loudly if DJANGO_SECRET_KEY is unset (no more silently
  running on the shipped development key), and warns when ALLOWED_HOSTS is a
  wildcard or TREASURY_ENCRYPTION_KEY is missing (the latter is what previously
  risked locking users out of two-factor if SECRET_KEY rotated). Dev is unchanged.
UI consistency & code quality
- Added reusable template partials (ui/page_header, ui/stat, ui/empty) and a set of
  spacing/layout/text utility classes, so pages can drop ad-hoc inline styles for
  named, consistent ones. Adopted on representative pages to establish the pattern.
Branding & visual polish
- The 403, 404 and 500 pages now share one premium, centred-card design with the
  church brand mark, consistent with the sign-in screen. The 500 page keeps inline
  fallback styling so it still looks right even if the stylesheet cannot load.
Mobile & responsiveness
- Added a defensive rule so any wide data table scrolls horizontally on small
  screens instead of stretching the page (the main ledgers already scrolled).
Tests
- Added a production-mode (DEBUG=False) render guard for the sign-in, error, and
  dashboard pages.

## v1.15.0 — SMS / email one-time codes for two-factor
- Two-factor authentication now offers three delivery methods: authenticator app
  (TOTP, as before), text message (SMS, via the existing Advanta integration), or
  email (via the configured mail server). Each user picks their method when setting
  up two-factor.
- At sign-in, SMS/email users land on a 'code sent to ***' screen with a
  rate-limited resend button. Codes are 6 digits, stored only as a hash, expire
  after 5 minutes, and lock out after 5 wrong attempts. Recovery codes continue to
  work for every method.
- SMS and email options only appear when they are configured (SMS credentials in
  Settings; a mail server for email).

## v1.14.0 — leader sub-group access + performance
- Group leaders: assigning a parent fund now grants its entire sub-tree at any
  depth (CAMP MEETING -> CAMP_1..CAMP_30 and deeper), with drill-down links from
  the leader landing page and department dashboard into each subgroup. A leader can
  still be assigned a single subgroup directly.
- Fixed: a leader assigned only a subgroup no longer sees a blank dashboard (their
  subgroup now heads the list); siblings remain out of scope.
- Performance: removed per-group / per-sub-account query loops that scaled with the
  number of development groups and sub-accounts. Development-group progress, the
  leader dashboard, and the Fund Ledger report now use single grouped queries
  (e.g. 46 groups went from 47 queries to 2), so the dashboard and reports stay
  fast as the CAMP_1..CAMP_30 structure grows.

## v1.13.1 — production constraint fix
- Fixed a MariaDB warning (W036): the unique guard on PledgePayment
  (pledge, transaction) used a condition MariaDB can't create, so on production it
  was silently skipped and the same contribution could be matched to a pledge more
  than once. Replaced with a plain unique constraint that behaves identically on
  SQLite, MariaDB, and Postgres (all treat NULL as distinct), so it blocks
  duplicates while still allowing many manual no-transaction payments.

## v1.13.0 — numbered fund families (easy camp/expense-group routing)
- Added a 'numbered fund family' setting: one line such as
  'expense, exp, expe = CAMP_{n}' routes EXPENSE1 / exp1 / expe1 to the fund named
  CAMP_1, EXPENSE30 to CAMP_30, and so on for all groups — no rule per group.
  Handles narration variations, distinguishes EXPENSE1 from EXPENSE10, and only
  applies when the target fund exists (otherwise the gift goes to review).
- This resolves ahead of the generic development-group prefix matcher so a
  configured family is not intercepted and sent to a development group by mistake.
- The allocation-rules page now points to this instead of per-group regex rules.

## v1.12.0 — period-aware leader insights, fair trend, cash delete
- Leader dashboard: development-group collected figures now respect the selected
  period (previously all-time), in step with the other cards, and a per-period
  group summary can be downloaded as CSV or Excel.
- Multi-year trend now compares January-to-current-month of every year (prior
  years from monthly history, the current year from the live ledger, annual-only
  years pro-rated and flagged), so a part-year is not measured against full years.
- Cash entries page gains delete. A cash entry is the same record as its ledger
  row, so deleting it removes the single entry (split parts together); bank,
  reversed, and envelope-receipted rows are protected, and edits remain at the
  ledger.

## v1.11.0 — leader dashboard revamp
- The department-leader page is now an insights dashboard: headline KPIs (closing
  balance, collections, expenses, net), a monthly collections-vs-expenses chart,
  an income-by-channel breakdown, top contributors, budget-vs-actual and
  pledge-fulfilment cards, and development-group standings with drilldown.
- Added an "Explore" set of quick links and a dedicated, downloadable pledges page
  (CSV/Excel) to sit beside the existing collections and expenses pages.
- All leader views remain strictly read-only and scoped to the leader's own
  departments; contributor phone numbers are masked on detail pages and not shown
  on the overview at all.

## v1.10.2 — two-factor verify page renders in all states
- The 2FA code-entry page is now fully standalone (it no longer extends the main
  layout). It previously went blank when reached while already logged in but not
  yet verified (the middleware path), because the main layout only fills its body
  for verified users. It now renders for fresh logins and re-verification alike.

## v1.10.1 — two-factor sign-in fixes
- The 2FA code-entry page no longer renders blank. It is shown before the user is
  logged in, so it now uses the unauthenticated sign-in layout (the authenticated
  layout suppressed its body, which locked everyone out).
- The enrolment QR code now renders using a pure-Python SVG generator, so it shows
  even though the image library (Pillow) isn't installed on the server.
- A recovery code continues to work directly in the verification box.

## v1.10.0 — importers, regex rules, reconciliation, fixes
- Allocation rules: bulk Excel import (template + review), and a new REGEX match
  type so one rule covers many narration variations like EXPENSE_1 / exp1 / expe1
  for camp/expense groups (items 1, 2).
- Split funds are selectable in the bulk-allocate dropdown and split each gift
  into its parts (item 3).
- Sabbath reconciliation: split-fund bank parts are regrouped into one gift so the
  total matches the single envelope, matched/unmatched envelopes show their fund
  allocation (Tithe, Development, ...), and selected matches can be applied in one
  click to mark them as bank giving (items 1, 4 across releases).
- Expenses: bulk Excel import at /expenses/ with a template, review, and the
  approval setting honoured (item 5).
- Remittance dashboard: recent batches labelled as last 10; a note clarifies that
  Outstanding is the cumulative running balance. The underlying fix makes trust
  'to remit' a true running liability (opening + collected to date - remitted to
  date), so cross-month timing reconciles (items 6, 8).
- Envelope import: an unrecognised fund column is no longer dropped silently — you
  map it to a fund, create one, or ignore it before importing (item 7).
- Fixes: loose cash dated to a closed Sabbath now counts for that Sabbath (not the
  next one); a reset_2fa management command recovers users locked out by an
  encryption-key change (set a stable TREASURY_ENCRYPTION_KEY in .env).

## v1.9.0 — reconciliation apply, statement Sabbath, dashboard refresh, campaign pledge import
- Sabbath reconciliation: a one-click 'apply match' on selected pairs (and the
  singleton suggestion) marks the matched envelope as a bank item, links it to the
  bank gift, and neutralises the duplicate cash income so the money is counted once
  via the bank (item 1).
- Statement import: an optional Sabbath that every entry in the file counts under,
  for imports done later than the Saturday. It takes precedence over the by-date
  assignment and isn't held for confirmation; leave it blank for the current
  per-date behaviour (item 2).
- Dashboard: the local-funds table has a small button to download it as a JPEG
  image (item 3); the 'Giving by group' card is replaced by 'How giving arrives',
  showing the bank / M-Pesa vs cash vs envelope mix with gift counts and shares
  (item 4).
- Pledges: an Import button on a campaign page loads pledges straight into that
  campaign — no Campaign column needed — reusing the review-and-approve flow, with
  pledges landing as drafts (item 5).

## v1.8.0 — Sabbath reconciliation, leader pages, 2FA fix
- New per-Sabbath reconciliation (Envelopes -> Reconcile Sabbath): lists a
  Sabbath's bank giving (receipted + manual) and the envelopes counted for it,
  matches them by contributor and amount with fuzzy matching to catch misspelt
  manual-receipt names, suggests the last unmatched pair when only one remains on
  each side, excludes cash envelopes from the bank balance, and flags bank entries
  that aren't assigned to any Sabbath (item 1).
- Leaders get detailed pages: a full, downloadable collections list (contributor,
  masked phone, reference, channel, amount), a downloadable expenses list, and a
  development-group drill-down with each group's performance and a downloadable
  per-contributor list — all scoped to the leader's departments and read-only
  (item 2).
- Two-factor authentication: signing in no longer throws a server error when the
  stored authenticator secret can't be read (e.g. after an encryption-key change);
  a recovery code now works directly in the verification box as a second form of
  sign-in, and a broken secret is regenerated on re-enrol (item 3).
- 'Receipt bank giving' can optionally be limited to a single Sabbath; leave the
  date blank to keep the whole-month behaviour (item 4).

## v1.7.0 — queue tools, trust accuracy, cash-count control, error pages
- Review queue: select several gifts and allocate them to one fund at once
  (item 1); a button fetches unallocated gifts sitting in the ledger (no fund,
  not in the queue) back into the queue for allocation (item 5).
- Trust 'to remit' now keys off the authoritative fund type, so a stale flag can
  no longer pull a local fund into the remittance total; a migration re-syncs the
  flag on existing data (item 4).
- Expense form: an entry larger than the fund's available balance is no longer
  silently dropped — a clear notice keeps the entry intact and offers the
  override, so M-Pesa charges and other expenses don't 'disappear' (item 3).
- M-Pesa / bank charges are kept out of duplicate-expense detection even when
  recorded under another category (item 8).
- Possible duplicates are sorted by payer and now include fuzzy near-matches, to
  catch a manual receipt typed with a slightly misspelt name (item 9).
- The allocation rules list is paginated, shows the match type, and drops the
  source column (item 6).
- Friendly 404 / 403 / 500 pages with a way back to the app; the admin can be
  alerted on an unexpected error by email, SMS or WhatsApp (item 2).
- Sabbath cash count reflects physical cash only: a cash-envelope row that
  duplicates a bank gift for the same contributor that Sabbath is excluded from
  the expected total, so the count can balance (item 7).

## v1.6.0 — manual receipts vs system receipts
- Split the single processed-via-envelope flag into two clear states:
  - Manual receipt: the gift was receipted on paper (e.g. a hand-written
    envelope) with no link to the ledger. No system envelope is created, and the
    gift is kept out of BOTH the review queue and the receipt-bank-giving pull so
    it is never receipted again. Reversible — untick manual receipt on the entry
    to make it eligible for a system receipt later.
  - Processed via envelope: a system envelope record exists (it was receipted in
    the app).
- The bulk Mark tool, the per-gift mark-only action, and the entry edit page now
  set the manual-receipt state; all of them cascade across the parts of a split
  gift. The two states show with distinct labels on the ledger.
- A data migration splits existing flags: a previously-processed gift with no
  envelope record becomes a manual receipt; one with an envelope stays a system
  receipt. Income totals are unaffected (the bank entry remains the income).

## v1.5.1 — fix
- Receipt bank giving: the bulk pull now excludes any gift that already has an
  envelope record, not only those flagged processed-via-envelope. Previously, if
  a gift had been receipted but its processed flag was not set (older data, a
  manual envelope, or a partially-receipted split), the pull would receipt it
  again. The single-gift receipt action was hardened the same way, so receipting
  one part of a split can never re-add a part that is already receipted.

## v1.5.0 — fund import, sub-accounts, and queue clearing
- New dedicated fund/department structure importer (Funds and departments ->
  Import funds and sub-accounts). Download a template that lists your existing
  funds, add one row per fund, and set a Parent to make a row a sub-account.
  Parents are created before their sub-accounts so row order does not matter, and
  sub-accounts inherit their parent fund type. Existing funds are never modified.
- The budget import template now comes pre-filled with one row per existing fund
  (with the current year budget as a starting point where set), so you enter
  amounts against funds already in the system instead of typing names.
- Marking a bank entry processed via envelope (in the bulk tool or on the edit
  page) now also removes it from the review queue, and cascades to every part of
  a split gift so the whole gift leaves the queue together.

## v1.4.2 — split funds in bulk mark-processed
- The bulk "mark processed via envelope" tool now understands split offerings.
  A split gift (e.g. Combined Offering) is posted as several ledger rows that
  share the reference with the amount divided across funds. Uploading the
  reference with the TOTAL the member gave now confirms the whole group by its
  sum and marks every part processed together. A wrong total, or a reference that
  matches unrelated rows, is still reported rather than applied.

## v1.4.1 — fixes
- Settings: the SMS card was rendering on every tab (it had slipped outside its
  tab pane); it now shows only under the SMS tab.
- Discoverability: the bulk fund/department import is now linked on the Funds &
  departments page, not only on the budgeting page.
- New bulk tool (Ledger -> Mark processed): for gifts written on a physical
  envelope that also appear on the bank statement. Upload just a reference and an
  amount; the reference finds the bank entry and the amount confirms it is the
  right record. Matched entries are marked processed via envelope — kept out of
  receipting and the review queue so they are not entered twice — without
  creating a duplicate receipt. Amount mismatches and ambiguous or unknown
  references are reported, not applied. The processed status now shows as a badge
  on the ledger.

## v1.4.0 — Department leaders & configurable encryption
- New "Department leader" role: a read-only login scoped to the department(s) a
  leader is assigned. They get their own dashboard showing collections, expenses,
  sub-accounts, development-group progress (for a development leader) and any
  pledges toward their department. Scoping is enforced server-side — a leader
  cannot reach another department or any office screen.
- Privacy: contact phone numbers are masked (e.g. *********678) everywhere a
  leader sees member, payer or pledge data.
- Assign leaders from the user screen: set the role to "Leader" and pick the
  department(s); changing the role away clears the links so access never goes
  stale.
- Configurable encryption: the application-layer key now comes from
  TREASURY_ENCRYPTION_KEY (falling back to SECRET_KEY), encryption can be toggled
  with TREASURY_ENCRYPTION_ENABLED, and a new check_encryption command reports
  status and re-encrypts secrets after a key change (key rotation).
- Pledges and the books are unaffected: all 44 financial-accuracy invariants pass.

## v1.3.0 — Security & oversight
- Automated encrypted backups: a `backup_db` management command for a nightly
  cron job. Dumps the database, encrypts it with the application key, keeps the
  newest N copies (rotating older ones away), and can email the backup off-site.
  See deploy/AUTOMATED_BACKUPS.md. Set the off-site address in Settings.
- Two-factor authentication (TOTP): enrol from the user menu (Security & 2FA)
  with a QR code, then logins require a 6-digit code. One-time recovery codes are
  issued for lost-phone access. A setting can require all treasurers to enrol.
- Dashboard revamp: a single "Needs attention" panel replaces scattered alert
  banners, surfacing — with counts and one-tap links — transactions to allocate,
  expenses and pledges awaiting approval, overdue or soon-due trust remittances,
  overdue pledges, and possible duplicates. Only non-zero items appear.
- Pledges remain informational throughout: none of the above changes how money
  is recognised, and all 44 financial-accuracy invariants still pass.

## v1.2.1
- Treasurer-only bulk pledge import (Pledges -> Import): downloadable template
  with dropdowns; members matched by name or phone and campaigns by name, with a
  review screen to map or create anything unmatched; rows with no campaign can be
  assigned a default. Imported pledges are saved as DRAFTS for approval and, like
  all pledges, never post to the ledger or change a fund balance.

## v1.2.0 — Inline pledge matching + public pledge form
- Inline matching: when a new contribution is recorded (manual entry or statement
  import) from a member who has an active pledge, the system acts per a new
  setting (Settings to Pledges to Pledge matching mode):
    * OFF — do nothing;
    * SUGGEST (default) — flag a likely match for a treasurer to confirm;
    * AUTO — apply the match automatically, capped at the pledge's outstanding.
  Two more parameters: restrict matching to the campaign's target fund, and how
  many days after a pledge's end date a gift may still be matched.
- New match-suggestions review queue (Pledges to Review suggestions) where a
  treasurer confirms or dismisses each flagged match. Confirming links the
  existing contribution to the pledge; it never moves money.
- Optional public pledge link (/pledge/, off by default; enable in Settings to
  Pledges). Members submit a pledge themselves; submissions are held as
  UNVERIFIED DRAFTS for treasurer approval. The form is write-only — it never
  exposes member data, balances, or other pledges — and is guarded by a spam
  honeypot, a submit-rate limit, an amount ceiling, and mandatory manual approval.
- ACCOUNTING unchanged: pledges remain informational. All 44 financial-accuracy
  invariants continue to pass.

## v1.1.0 — Pledge Management
- New module for recording and tracking pledges, integrated with members,
  contributions, SMS/WhatsApp, reporting, security and the audit trail.
- Pledge campaigns (giving drives) with goals, target fund, and progress
  (pledged vs received vs outstanding).
- Member pledges with one-off or recurring (weekly / monthly / quarterly /
  annual) frequencies and an informational installment schedule.
- Approval workflow: an assistant's pledge is a draft a treasurer approves; a
  treasurer's pledge is active immediately. Cancel / reactivate supported.
- Fulfilment by matching real, confirmed contributions to a pledge — one click
  auto-match per pledge, a bulk auto-match sweep, manual match of a specific
  contribution (with split), or a directly-recorded payment. A contribution is
  never matched twice, and auto-match never over-applies past the outstanding
  balance.
- Reminders reuse the existing SMS / WhatsApp services, respect a per-pledge
  opt-out and missing phones, and are logged. Single or batch (per campaign).
- Reports: campaign progress and pledges-by-status, exportable to Excel; plus a
  printable year-end per-member pledge statement.
- ACCOUNTING: pledges are commitments, not income. Nothing in the module posts
  to the general ledger or changes a fund balance — only the matched real
  contribution does, exactly as before. All 44 financial-accuracy invariants
  continue to pass unchanged.

## v1.0.19
- Budgets: a Download template button produces a ready-to-fill spreadsheet with
  one row per planned line (Department, Line item, Category, Amount, Funded by),
  with dropdowns. Re-import it on the Bulk import screen and each department's
  budget becomes the sum of its lines; a line financed by another fund (or from
  the department's own funds) records that funding source.
- Controls: duplicate detection tightened — duplicate expenses are now flagged
  within the same Sabbath (not the whole month); M-Pesa / bank charges are
  excluded; duplicate offerings are only flagged within the SAME channel (so a
  giver who gave once by cash and once by M-Pesa is not flagged); and re-typed
  envelopes (same giver + amount on one Sabbath) are now detected.
- Remittance calendar: generated deadlines default to the 1st of the following
  month; and a period is automatically marked remitted when a completed
  remittance batch covers it.

## v1.0.18
- Names are now stored in a consistent UPPERCASE register everywhere — bank
  imports, manual entry, and envelope entry — via the member, transaction and
  envelope models, so matching and receipts read consistently.
- Expenses: the Type filter is replaced with a Search box (matches description,
  claimant and voucher number).
- Expenses: a new "Re-categorise" route lets you download all expenses, edit only
  the category column offline, and re-import — every other field is left
  untouched, keyed on the expense ID.
- Trust remittance dashboard: instead of "oldest outstanding", it now shows a
  COUNTDOWN to the reporting Sabbath (the Saturday whose count must be remitted),
  driven by the per-month remittance deadlines. Those deadline dates are set
  freely per month on the remittance calendar — they are not assumed to fall on a
  fixed day — and the reporting Sabbath updates automatically when a deadline is
  midweek.
- New Bulk fund & budget import (Budgets - Bulk import): upload a budget workbook
  with a DEPARTMENTS sheet, and the wizard matches each fund to an existing
  department (fuzzy + known synonyms). Anything that does not match is flagged so
  you can map it to a department, create a new fund or sub-group, or skip it.
  Applying writes the per-year budget and an optional Jan-Dec monthly breakdown
  (taken from the projected-expense columns so it ties to the headline).

## v1.0.17
- Ledger (transactions) made more compact: tighter rows, summary strip and
  toolbar, and — the real fix — wide tables now scroll horizontally instead of
  clipping, so the right-hand action buttons (Edit / Split / Reverse / Receipt)
  are always reachable. This overflow fix applies to the Envelopes and Expenses
  tables too.
- The Remittance calendar (trust-fund deadline dates and their reporting
  Sabbaths) is now linked directly in the left navigation under Reports, not only
  on the Reports index — it was already built but hard to find.
- Settings: the "Restore from backup" card no longer appears on every tab — it is
  now correctly scoped to the About tab. The settings tabs are laid out as a
  single tidy row with light separators between the General / Messaging / System
  groups (scrolling horizontally on small screens).

## v1.0.16
- Visual redesign of the three core data screens — Ledger (transactions),
  Envelopes and Expenses — around a single, consistent "workspace" layout so they
  read as one professional product:
  * a ruled page header with title and primary actions;
  * a calm summary strip of metric cards (the lead metric marked with a thin
    brass keyline), replacing the divergent per-page stat/chip styles;
  * a single contained command toolbar grouping all filters with Apply / Clear
    and export actions;
  * refined data tables with tighter rhythm, a subtle brass margin-cursor on
    hover, and clearer numeric treatment;
  * dignified empty states that tell the user what to do next.
  The warm forest-green / brass / parchment identity and the Fraunces + Public
  Sans + IBM Plex Mono type system are preserved throughout. All filters,
  exports, approval actions, bank-receipting and SMS workflows are unchanged.

## v1.0.15
- Extended the financial-accuracy suite (reports/test_accuracy.py) with a second
  layer of 15 edge-case / adversarial tests targeting the real-world conditions
  that cause reconciliation gaps:
  * period-window boundaries are inclusive and adjacent periods neither overlap
    nor leave a gap;
  * unconfirmed receipts and pending (unapproved) expenses never reach a balance;
  * excluded-from-income receipts stay in the fund balance but out of income;
  * split offerings divide to the exact cent with no money lost or created;
  * empty/zero state yields zero totals (never None or error) and still balances;
  * Decimal arithmetic shows no floating-point drift over awkward sums;
  * a mis-keyed far-future value date is excluded by a bounded period window;
  * bank debits correctly reduce the bank position.
  Validated by fault injection. 44 accuracy tests in total (416 across the app).

## v1.0.14
- New financial-accuracy test suite (reports/test_accuracy.py, 29 tests) that
  asserts the accounting invariants the figures depend on, each against a fully
  hand-totalled scenario:
  * departmental balance identity (closing = opening + receipts − expenses
    + transfers in − transfers out) for every fund;
  * carry-forward continuity (a period's opening equals the prior period's
    closing; a split year equals the full year);
  * reconciliation (the fund engine balance equals the general-ledger balance,
    with no variance, and rebuild is idempotent);
  * ledger integrity (every journal entry balances; the trial balance balances;
    Assets = Liabilities + Funds);
  * Statement of Financial Position balances (Total Assets = Total Liabilities
    + Net Assets) with trust-payable equal to unremitted tithe;
  * Statement of Cash Flows reconciles (opening + net change = closing; the
    three categories sum to the net change; capital is investing, not operating);
  * transfers are zero-sum; reversals net to zero; remittances are never income
    or operating expense; receipting a bank gift as an envelope never inflates
    income; and consolidated parents equal own-plus-children.
  The suite was validated by fault injection — deliberately breaking a formula
  makes the relevant tests fail, confirming they genuinely catch errors.

## v1.0.13
- New interactive deployment installer: deploy/install.sh. Collects all settings
  through validated dialog prompts (whiptail/dialog if available, plain prompts
  otherwise — never echoes secrets), then sets up the .env (600 perms), MySQL
  database (utf8mb4), Python venv + migrations + static + superuser, a systemd
  gunicorn service, the Apache proxy include under the domain-owning cPanel user,
  nginx pass-through and AutoSSL, and verifies /healthz/ at each layer. Safe to
  re-run; reuses the existing secret key and backs up the previous .env. See
  deploy/INSTALL.md.

## v1.0.12
- Transactions page redesigned: summary cards (count, receipts, payments, net,
  in-review), a cleaner filter bar with a Clear button, channel colour-coding,
  service-Sabbath hints and payer phone shown inline.
- Fixed a reporting bug where trust remittances were counted as expenses in the
  annual summary and the board-report multi-year trend, overstating expenses for
  prior years. (Operating expense totals now exclude REMITTANCE everywhere, as
  intended — trust funds are liabilities, not expenditure.)
- New Remittance calendar (Reports - Remittance calendar): per-year trust-fund
  remittance deadlines, each mapped to its reporting Sabbath (the most recent
  Saturday on/before the deadline). If a deadline falls midweek, the previous
  Sabbath is the reporting Sabbath. Overdue and due-soon remittances are alerted
  on the dashboard.
- Bank receipting: you can now mark a bank gift as receipted WITHOUT creating a
  new envelope (for when the envelope was already written/typed by hand).
- Bulk bank receipting now lets you optionally set a starting receipt number.
- Settings page reorganised into General / Messaging / System groups with a
  cleaner navigation.

## v1.0.11
- Redesigned the envelope ledger entry screen (Record envelopes) for faster,
  clearer entry: a cleaner toolbar, a live summary bar showing the running
  contributor count, grand total, and per-fund subtotals as you type, a sticky
  column-totals footer row, a clearer Save button showing the total, and an
  inline duplicate-name flag. All existing behaviour (name autocomplete,
  auto-incrementing receipts, keyboard navigation, fund picker, Excel template)
  is preserved.
- Confirmed SMS/WhatsApp receipt buttons on the envelopes list appear only when
  the matching channel is enabled in settings.

## v1.0.10
- Per-Sabbath Excel sheet cleanups:
  - Receipt numbers display without the internal month/sabbath prefix (e.g.
    "JUN1-0421" now shows as "0421").
  - Combined Offering and Thanksgiving Offering appear as a single block (the
    full amount given) in the per-contributor entries table, but are split into
    their trust and local halves in the summary table.
  - The summary table now has cell borders, matching the entries table.

## v1.0.9
- Statement imports now capture the statement's own opening/closing running
  balance and date span.
- New "Bank position check" report (Reports → Bank position check): compares the
  system's computed bank balance (opening + bank receipts − bank payments) against
  the most recent statement's closing balance. A non-zero difference means an
  entry is on the statement but not in the app (or vice versa) — the report lists
  the likely culprits (unconfirmed, in-review, or unallocated bank entries) so
  they can be chased. Directly addresses un-entered bank entries going undetected.

## v1.0.8
- New per-transaction "Receipt" action on the transactions list: receipt a single
  bank/M-Pesa gift as an envelope on demand (the per-entry counterpart to the bulk
  monthly pull). Supports a user-entered receipt number for hybrid manual
  receipting, so the system record matches a hand-written receipt/envelope; leave
  it blank to auto-assign. Split parts of one gift are receipted together, the
  bank transaction is linked, and it is marked accounted-for so income is never
  double-counted. (Items 7 + 8.)

## v1.0.7
- Reconciliation variance finder rewritten to explain real-world differences:
  it now compares each fund's engine contribution against what is actually
  posted in the ledger, catching transactions that were re-allocated to another
  fund, edited, excluded, reversed, or unconfirmed after posting — not just
  entries that were never posted. The flagged amounts now sum to the variance,
  and a one-click "Rebuild ledger" button on the page re-posts everything from
  current source records to clear it.

## v1.0.6
- Transactions Excel export now includes M-Pesa ref, core ref, bank receipt,
  member, phone, dev group, service Sabbath and confirmed status.
- SMS and WhatsApp send buttons on the envelopes page appear only when those
  channels are enabled in settings.
- The per-Sabbath Excel sheet now carries the church name, has cell borders,
  number formatting, and a print-ready landscape layout (fit-to-width, repeating
  headers, page footer).
- New reconciliation variance finder: when a fund's engine balance differs from
  the general ledger, click "investigate" to see the actual transactions and
  expenses causing the difference.
- M-Pesa webhook ingest now normalises dedup keys to uppercase (collation-safe),
  consistent with the statement importer.
- Mobile layout: tables scroll within their cards instead of forcing the page
  wide; tighter padding and wrapping on small screens.

## v1.0.5
- Fixed a 500 error (FieldError on 'children') on the budget breakdown edit page,
  triggered when the Local Church Budget fund was matched by its full name rather
  than an 'LCB ' prefix. The query now uses the correct 'subgroups' relation.

## v1.0.4
- Update checker now authenticates with an optional GITHUB_TOKEN, so it can read
  releases from a PRIVATE GitHub repository (the unauthenticated API returns 404
  for private repos).
- Fixed: the release check was cached permanently per process, so a new release
  was not noticed until the app restarted. It now re-checks at most every 10
  minutes, and the update page forces a fresh check.

## v1.0.3
- Import dedup now also matches on the M-Pesa receipt (mpesa_ref), catching a
  repeated payment even when one row has a core_ref and another does not.
- New 'dedupe_transactions' management command finds and removes existing
  duplicate transactions sharing an M-Pesa receipt (keeps the better record,
  repoints envelopes/expenses). Dry-run by default; --apply to perform.
- Statement purge gained an 'Unlink & purge' option: it clears the
  reconciliation links on any expenses tied to the statement's debits (keeping
  the expenses) instead of refusing outright.

## v1.0.2
- Statement dedup keys (core_ref / M-Pesa receipt) are normalised to uppercase,
  so duplicate detection is exact regardless of the database collation. Fixes
  false/inconsistent duplicate counts on MySQL databases created with a
  case-insensitive collation such as latin1_swedish_ci.

## v1.0.1
- Test release to validate the in-app update mechanism.
- Added a visible "What's new" note on the Settings → About tab so an applied
  update is easy to confirm.
- Database backup is now engine-aware (SQLite file / MySQL & Postgres dump).
- Importer creates a system user automatically on a fresh database, so the
  legacy import no longer fails on a brand-new deployment.
- `.env` is auto-loaded by the app (no fragile shell `export` needed).
- Production: WhiteNoise static serving, health check at /healthz/, gunicorn
  config, logging, and cPanel/WHM deployment runbook.

## v1.0.0
- Initial release: full SDA church treasury system — member giving, fund
  allocation, bank/M-Pesa reconciliation, trust remittances, expenses,
  departmental reporting, and audit logging.
