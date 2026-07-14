# Setting the church up for the first time

The order below matters. Each step depends on the one before it, and two of them
(**founding balances** and **the first year-end close**) are effectively one-way
doors — so do them deliberately, not in passing.

---

## 1. The church itself

**Settings → General.** Church name, currency, financial year start.

---

## 2. The funds

**Funds → New fund.** Every account money can sit in. For most SDA churches:

* **Trust funds** — money held for the conference: Tithe, Combined Offering, the
  various mission offerings. *This money is not the church's.* It is collected,
  held, and remitted, and every report keeps it separate for exactly that reason.
* **Local funds** — the church's own money: Local Church Budget (LCB) and its
  subgroups, development groups, welfare, building, and so on.

**Set the parent** for anything that belongs to a family (an LCB subgroup's
parent is the LCB fund). Subgroups inherit their parent's treatment, so you do
not have to configure each one.

---

## 3. Founding balances — the one-way door

**Funds → each fund → Founding balance**, or **Budget** for all of them at once.

This is what each fund held **on the day you start using this system**. It is a
**one-time figure, not a yearly one**.

Every later year's opening balance is *calculated* — the founding balance plus
every receipt, payment and transfer before that year. Nothing is carried forward
by hand at year end, because nothing needs to be. Which means:

> **Changing a founding balance later does not adjust "the opening". It silently
> rewrites that fund's balance in every year the church has ever recorded,
> backwards.**

So: get them right now, from your last hand-kept books. Once you close a year
(step 9), they lock automatically and cannot be edited again — because by then
they underpin an audited history.

If you have nothing to bring forward (a brand-new church, or you are starting the
system at a genuine zero), leave them at zero. That is a perfectly good answer.

---

## 4. Members

**Members → Import** (CSV) or add them one at a time.

Names and phone numbers. The phone number matters more than it looks: it is what
matches an M-Pesa payment to the person who sent it, automatically, for the rest
of the church's life. A member who pays from two lines can have both recorded
(**Member → Other phones**) and either will match.

---

## 5. The bank

**Settings → Channels → Bank accounts.** Add the account(s).

**Settings → Channels → Local Church Budget (LCB) funds.** Tell the system which
funds are LCB. Do not rely on the name — a fund called "Budget Main" is LCB if
you say it is, and the system will not guess.

---

## 6. Allocation rules

**Rules.** How a bank narration becomes a fund: `tithe` → Tithe, `lcb` → Local
Church Budget, `grp12` → Development Group 12.

You do not have to get this complete on day one. Anything the system cannot place
goes to a review queue, and you can teach it a rule from there in one click —
which is usually a faster way to build the rules than guessing at them up front.

**Rules → Development-group patterns** if your church numbers its dev groups in
some way the built-in patterns do not already recognise.

---

## 7. Opening the books

You are now ready to record. Two ways in:

* **Statements → Import** — a bank statement file. Allocates what it can,
  queues what it cannot.
* **Envelopes**, **Cash entry** — what came in on a Sabbath.

**Expenses → New** for money going out. **Payments** for cheques.

---

## 8. Optional, when you need them

* **Benevolent** — the welfare/bereavement scheme. Has its own setup wizard;
  start at *Benevolent → Schemes → Which pattern fits your church?*
* **Pledges** — campaign pledges and their fulfilment.
* **Bank register** — an independent record of what the bank says, checked
  against your books. Import from January; it is safe to re-import.
* **Telegram** — a bot for asking the books questions from a phone.

---

## 9. Year end — the other one-way door

**Reports → Year-end close.**

Closing a year says: *this history is final.* It:

* locks the period against further edits,
* **freezes every founding balance**, permanently,
* and carries nothing forward by hand, because every subsequent opening balance
  was already being calculated from the founding figures.

Do not close a year until you are satisfied the year is right. Reopening is
possible but is an event, not a routine.

---

## The two things worth being slow about

1. **Founding balances.** They are the foundation the whole history is computed
   from. Wrong here is wrong everywhere, silently.
2. **The first year-end close.** It makes the above permanent.

Everything else can be corrected as you go, and is designed to be.
