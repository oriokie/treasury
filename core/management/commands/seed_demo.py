"""Seed a realistic demo dataset so the app is explorable immediately.

    python manage.py seed_demo            # create everything (idempotent-ish)
    python manage.py seed_demo --flush    # wipe domain data first, then seed
"""
import datetime as dt
import random
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.core.management.base import BaseCommand
from django.db import transaction as db_tx

from core.models import SiteConfig, SmsLog
from core.roles import TREASURER, ASSISTANT, AUDITOR, ALL_ROLES
from core.utils import sabbath_week_of
from departments.models import Department, DevelopmentGroup
from members.models import Member, MemberAlias
from giving.models import AllocationRule, Transaction, SplitFund, SplitComponent
from giving.services.allocation import normalize_reference
from cashbook.models import Expense
from envelopes.models import Envelope, EnvelopeLine

T, L = Department.FundType.TRUST, Department.FundType.LOCAL
C = Department.Category

# Top-level (parent) funds.
FUNDS = [
    ("Trust Fund", T, C.TRUST, "0"),
    ("Local Church Budget (LCB)", L, C.OFFERING, "0"),   # the LCB umbrella
    ("Development", L, C.DEVELOPMENT, "0"),
]

# Sub-accounts: (name, parent_name, opening). They inherit the parent's fund type.
SUBGROUPS = [
    # Trust Fund account
    ("Tithe", "Trust Fund", "0"),
    ("Combined Offering (Trust 50%)", "Trust Fund", "0"),
    ("Camp Meeting", "Trust Fund", "0"),
    ("Evangelism – Field", "Trust Fund", "0"),
    ("Station Development Fund", "Trust Fund", "0"),
    ("Thanksgiving (Trust 50%)", "Trust Fund", "0"),
    # Local Church Budget account
    ("Sabbath School", "Local Church Budget (LCB)", "0"),
    ("Loose Offering", "Local Church Budget (LCB)", "0"),
    ("Combined Offering (Local 50%)", "Local Church Budget (LCB)", "0"),
    ("Thanksgiving (Local 50%)", "Local Church Budget (LCB)", "0"),
    ("LCB – Local Church Budget", "Local Church Budget (LCB)", "0"),
    ("Envelopes (SUS)", "Local Church Budget (LCB)", "0"),
    ("LCB Departments", "Local Church Budget (LCB)", "0"),
]

# 50/50 split offerings: a lump sum divides across these funds.
SPLIT_FUNDS = {
    "Combined Offering": [("Combined Offering (Trust 50%)", "50"),
                          ("Combined Offering (Local 50%)", "50")],
    "Thanksgiving Offering": [("Thanksgiving (Trust 50%)", "50"),
                              ("Thanksgiving (Local 50%)", "50")],
}

# Plain reference -> fund rules.
RULES = {
    "tithe": "Tithe",
    "camp": "Camp Meeting", "campmeeting": "Camp Meeting",
    "fieldevangelism": "Evangelism – Field", "evangelismfield": "Evangelism – Field",
    "stationdev": "Station Development Fund", "stationdevelopment": "Station Development Fund",
    "loose": "Loose Offering", "looseoffering": "Loose Offering",
    "lcb": "LCB – Local Church Budget", "localchurchbudget": "LCB – Local Church Budget",
    "churchbudget": "LCB – Local Church Budget",
    "ss": "Sabbath School", "sabbathschool": "Sabbath School",
    "envelopes": "Envelopes (SUS)", "sus": "Envelopes (SUS)",
}

# Reference -> split-fund rules (these divide 50/50).
SPLIT_RULES = {
    "combined": "Combined Offering", "combinedoffering": "Combined Offering",
    "offering": "Combined Offering",
    "thanksgiving": "Thanksgiving Offering", "thanks": "Thanksgiving Offering",
    "thanksgivingoffering": "Thanksgiving Offering",
}

DEV_GROUPS = [(1, "Faith", "300000"), (3, "Hope", "250000"), (5, "Grace", ""),
              (7, "Bethel", "200000"), (11, "Zion", ""), (12, "Eden", "300000")]

MEMBERS = [
    ("Cannon Odhiambo", "0796472241", Member.Group.YOUTH, Member.MemberType.MEMBER, 7, []),
    ("Roselyne Omanya", "0720323255", Member.Group.AWM, Member.MemberType.MEMBER, 11, ["Omanya Roselyne"]),
    ("John Motari", "0723249077", Member.Group.AMM, Member.MemberType.MEMBER, 14, []),
    ("Kelvin Mwathi", "0792249172", Member.Group.YOUTH, Member.MemberType.SS_MEMBER, None, []),
    ("Jane Nyamongo", "0711548871", Member.Group.AWM, Member.MemberType.MEMBER, None, []),
    ("Samuel Abaya", "0723492692", Member.Group.AMM, Member.MemberType.MEMBER, 5, []),
    ("Alan Otieno", "0716804186", Member.Group.AMBASSADORS, Member.MemberType.MEMBER, 6, []),
    ("Esther Muchoki", "0725768358", Member.Group.AWM, Member.MemberType.SS_MEMBER, None, ["Muchoki Esther"]),
    ("Duncan Njora", "0726447320", Member.Group.AMM, Member.MemberType.MEMBER, 3, []),
    ("Callen Makori", "0727653472", Member.Group.CHILDREN, Member.MemberType.SS_MEMBER, None, []),
]

EXPENSES = [
    ("Sabbath bulletins printing", "LCB – Local Church Budget", "2400", Expense.Category.STATIONERY, "Elder Joseph", Expense.Status.PAID),
    ("KPLC electricity token", "LCB – Local Church Budget", "1500", Expense.Category.UTILITIES, "", Expense.Status.PAID),
    ("PA system microphone", "YOUTH", "3500", Expense.Category.MAINTENANCE, "Alan Otieno", Expense.Status.APPROVED),
    ("Cement for foundation", "DEVELOPMENT", "48000", Expense.Category.CONSTRUCTION, "Building Committee", Expense.Status.APPROVED),
    ("Visiting choir transport", "CHURCH CHOIR", "6000", Expense.Category.TRANSPORT, "Jane Nyamongo", Expense.Status.PENDING),
    ("Communion supplies", "LCB – Local Church Budget", "1800", Expense.Category.MATERIALS, "", Expense.Status.PENDING),
]


class Command(BaseCommand):
    help = "Seed a demo dataset (config, roles, users, funds, dev groups, members, giving, envelopes, expenses)."

    def add_arguments(self, parser):
        parser.add_argument("--flush", action="store_true",
                            help="Delete existing domain data before seeding.")

    @db_tx.atomic
    def handle(self, *args, **opts):
        rng = random.Random(42)

        if opts["flush"]:
            self.stdout.write("Flushing domain data…")
            EnvelopeLine.objects.all().delete()
            Envelope.objects.all().delete()
            from envelopes.models import CountSession
            CountSession.objects.all().delete()        # cascades denominations + witnesses
            from cashbook.models import StaffAdvance
            from statements.models import BankAccount
            from core.models import Notification, SabbathClose
            from statements.models import BankEvent
            BankEvent.objects.all().delete()
            Notification.objects.all().delete()
            SabbathClose.objects.all().delete()
            from ledger.models import JournalEntry, Account
            from cashbook.models import FundTransfer, RemittanceBatch
            from core.models import YearEndClose, FundCarryForward, PeriodLock
            from assets.models import FixedAsset, DepreciationRule
            from departments.models import Budget, BudgetLine
            JournalEntry.objects.all().delete()
            Account.objects.all().delete()
            FundTransfer.objects.all().delete()
            FundCarryForward.objects.all().delete()
            YearEndClose.objects.all().delete()
            PeriodLock.objects.all().delete()
            FixedAsset.objects.all().delete()
            DepreciationRule.objects.all().delete()
            BudgetLine.objects.all().delete()
            Budget.objects.all().delete()
            RemittanceBatch.objects.all().delete()
            from cashbook.models import RecurringExpense, Payable, Accrual, Prepayment, PettyCashTopUp
            Payable.objects.all().delete()
            Accrual.objects.all().delete()
            Prepayment.objects.all().delete()
            PettyCashTopUp.objects.all().delete()
            RecurringExpense.objects.all().delete()
            StaffAdvance.objects.all().delete()        # PROTECT -> dept; remove before depts
            Expense.objects.all().delete()
            Transaction.objects.all().delete()
            BankAccount.objects.all().delete()
            SplitComponent.objects.all().delete()
            SplitFund.objects.all().delete()
            AllocationRule.objects.all().delete()
            MemberAlias.objects.all().delete()
            Member.objects.all().delete()
            DevelopmentGroup.objects.all().delete()
            Department.objects.all().delete()
            SmsLog.objects.all().delete()

        cfg = SiteConfig.get()
        cfg.church_name = "SDA Church Kahawa"
        cfg.field_name = "East Nairobi Field"
        cfg.save()

        for name in ALL_ROLES:
            Group.objects.get_or_create(name=name)
        self.stdout.write(self.style.SUCCESS(f"Roles: {', '.join(ALL_ROLES)}"))

        treasurer = self._user("treasurer", "Tabitha", "Treasurer", "treasurer123", TREASURER, superuser=True)
        self._user("assistant", "Alan", "Assistant", "assistant123", ASSISTANT)
        self._user("auditor", "Aisha", "Auditor", "auditor123", AUDITOR)
        self.stdout.write(self.style.SUCCESS("Users: treasurer / assistant / auditor"))

        funds = {}
        for name, ftype, cat, opening in FUNDS:
            d, _ = Department.objects.get_or_create(
                name=name, defaults=dict(fund_type=ftype, category=cat,
                                         opening_balance=Decimal(opening)))
            funds[name] = d
        self.stdout.write(self.style.SUCCESS(
            f"Funds: {len(funds)} ({sum(1 for f in funds.values() if f.is_trust)} trust)"))

        for name, parent_name, opening in SUBGROUPS:
            sub, _ = Department.objects.get_or_create(
                name=name, defaults=dict(parent=funds[parent_name],
                                         category=funds[parent_name].category,
                                         opening_balance=Decimal(opening)))
            funds[name] = sub
        self.stdout.write(self.style.SUCCESS(f"Sub-accounts: {len(SUBGROUPS)}"))

        # ---- chart-of-accounts funds + opening balances (from the church's list) ----
        from ._seed_data import (PASTED_ACCOUNTS, MAP_TO_EXISTING,
                                 HOLDING_ACCOUNTS, DEVELOPMENT_ACCOUNTS, ACCOUNT_RULES)
        added = 0
        for acct_name, opening in PASTED_ACCOUNTS:
            bal = Decimal(str(opening or "0"))
            if acct_name in MAP_TO_EXISTING:
                target = funds.get(MAP_TO_EXISTING[acct_name])
                if target:
                    target.opening_balance = bal
                    target.save(update_fields=["opening_balance"])
                    funds[acct_name] = target
                continue
            if acct_name in HOLDING_ACCOUNTS:
                cat = C.HOLDING
            elif acct_name in DEVELOPMENT_ACCOUNTS:
                cat = C.DEVELOPMENT
            else:
                cat = C.MINISTRY
            d, _ = Department.objects.get_or_create(
                name=acct_name,
                defaults=dict(fund_type=L, category=cat, opening_balance=bal))
            funds[acct_name] = d
            added += 1
        self.stdout.write(self.style.SUCCESS(f"Chart-of-accounts funds: {added}"))

        # demo annual budgets for the current year (so Budget vs Actual has data)
        from departments.models import Budget, BudgetLine, lcb_fund
        import datetime as _d
        yr = _d.date.today().year
        demo_budgets = {"YOUTH": "120000", "CHURCH CHOIR": "90000",
                        "Development": "5000000", "AWM": "60000", "AMM": "60000"}
        bn = 0
        budget_objs = {}
        for fname, amt in demo_budgets.items():
            f = funds.get(fname)
            if f:
                b, _ = Budget.objects.update_or_create(year=yr, department=f,
                                                 defaults={"amount": Decimal(amt)})
                budget_objs[fname] = b
                bn += 1
        # itemised breakdown with a source of funds (some charged to LCB)
        lcb = lcb_fund()
        demo_lines = {
            "YOUTH": [("Youth camp", "EVANGELISM", "70000", None),
                      ("PA system hire", "MATERIALS", "30000", "LCB"),
                      ("Refreshments", "REFRESHMENTS", "20000", None)],
            "CHURCH CHOIR": [("Choir uniforms", "MATERIALS", "60000", "LCB"),
                             ("Music books", "STATIONERY", "30000", None)],
            "AWM": [("Women's retreat", "EVANGELISM", "40000", None),
                    ("Banners", "MATERIALS", "20000", "LCB")],
        }
        ln_n = 0
        for fname, rows in demo_lines.items():
            b = budget_objs.get(fname)
            if not b:
                continue
            b.lines.all().delete()
            for name, cat, amt, src in rows:
                src_fund = lcb if (src == "LCB" and lcb) else None
                BudgetLine.objects.create(budget=b, name=name, category=cat,
                                          amount=Decimal(amt), source_fund=src_fund)
                ln_n += 1
            b.amount = b.lines_total
            b.save(update_fields=["amount"])
        # a prior-year budget so trust/LCB items can be pegged on PY data
        for fname in ("YOUTH", "CHURCH CHOIR"):
            f = funds.get(fname)
            if f:
                pb, _ = Budget.objects.update_or_create(year=yr - 1, department=f,
                            defaults={"amount": Decimal(demo_budgets[fname])})
                if not pb.lines.exists():
                    BudgetLine.objects.create(budget=pb, name="Prior-year plan",
                        amount=Decimal(demo_budgets[fname]))
        self.stdout.write(self.style.SUCCESS(
            f"Annual budgets: {bn} ({ln_n} itemised lines, incl. LCB-sourced)"))

        # depreciation rules + a few demo fixed assets
        from assets.models import DepreciationRule, FixedAsset
        rules = {"BUILDING": ("STRAIGHT", "2"), "FURNITURE": ("STRAIGHT", "12.5"),
                 "EQUIPMENT": ("REDUCING", "20"), "IT": ("REDUCING", "30"),
                 "VEHICLE": ("REDUCING", "25"), "MUSICAL": ("STRAIGHT", "15"),
                 "LAND": ("NONE", "0"), "OTHER": ("STRAIGHT", "10")}
        for cat, (meth, rate) in rules.items():
            DepreciationRule.objects.update_or_create(
                category=cat, defaults={"method": meth, "rate": Decimal(rate)})
        demo_assets = [
            ("Church building", "BUILDING", _d.date(2015, 1, 1), "8000000"),
            ("PA / sound system", "EQUIPMENT", _d.date(2023, 6, 1), "420000"),
            ("Keyboard (Yamaha)", "MUSICAL", _d.date(2022, 3, 15), "180000"),
            ("Office laptops (x3)", "IT", _d.date(2024, 9, 1), "240000"),
            ("Plastic chairs (200)", "FURNITURE", _d.date(2021, 1, 20), "300000"),
        ]
        an = 0
        for name, cat, acq, cost in demo_assets:
            _, created = FixedAsset.objects.get_or_create(
                name=name, defaults={"category": cat, "acquired_on": acq,
                                     "cost": Decimal(cost)})
            an += 1 if created else 0
        self.stdout.write(self.style.SUCCESS(f"Fixed assets: {an}"))

        # a couple of capital expenditures (so the income statement shows a capital section)
        from cashbook.models import Expense as _Exp
        pa = FixedAsset.objects.filter(name__startswith="PA").first()
        dev = funds.get("Development") or Department.objects.filter(is_trust=False).first()
        cap = [
            ("Sound system upgrade", "CONSTRUCTION", "420000", pa),
            ("New plastic chairs", "MATERIALS", "120000", None),
        ]
        cn = 0
        for desc, cat, amt, asset in cap:
            obj, created = _Exp.objects.get_or_create(
                description=desc, defaults=dict(
                    date=_d.date(_d.date.today().year, max(_d.date.today().month-1,1), 12),
                    department=dev, amount=Decimal(amt), category=cat,
                    expenditure_type="CAPITAL", capitalized_asset=asset,
                    method="BANK", status="PAID", recorded_by=treasurer))
            cn += 1 if created else 0
        self.stdout.write(self.style.SUCCESS(f"Capital expenses: {cn}"))

        # a demo inter-fund transfer (local funds only)
        from cashbook.models import FundTransfer
        locals_ = list(Department.objects.filter(is_trust=False, parent__isnull=True)[:2])
        if len(locals_) >= 2 and not FundTransfer.objects.exists():
            FundTransfer.objects.create(
                date=_d.date(_d.date.today().year, max(_d.date.today().month-1,1), 20),
                source=locals_[0], destination=locals_[1], amount=Decimal("15000"),
                reason="Top-up for project", recorded_by=treasurer)
            self.stdout.write(self.style.SUCCESS("Fund transfers: 1"))

        # build the double-entry general ledger from everything seeded above
        from ledger.services import posting
        posting.rebuild()
        # recurring (scheduled) expenses — e.g. weekly stipend + monthly allowance
        from cashbook.models import RecurringExpense as _RE
        from cashbook.services import recurring as _recsvc
        lcb = funds.get("LCB – Local Church Budget") or Department.objects.filter(is_trust=False).first()
        if not _RE.objects.exists():
            _RE.objects.create(description="Sabbath cleaning stipend", department=lcb,
                category="ALLOWANCE", amount=Decimal("500"), frequency="SABBATH",
                start_date=_d.date(_d.date.today().year, 1, 1), created_by=treasurer)
            _RE.objects.create(description="Caretaker monthly allowance", department=lcb,
                category="ALLOWANCE", amount=Decimal("6000"), frequency="MONTHLY",
                day_of_month=1, start_date=_d.date(_d.date.today().year, 1, 1), created_by=treasurer)
            made = _recsvc.generate_due(user=treasurer)
            self.stdout.write(self.style.SUCCESS(f"Recurring expenses: 2 schedules, {made} entries generated"))
        # petty cash: set a float, top it up, and record disbursements charged to ministries
        from cashbook.models import PettyCashTopUp as _PTU
        cfg_pc = SiteConfig.get(); cfg_pc.petty_cash_float = Decimal("5000"); cfg_pc.save()
        lcb = funds.get("LCB \u2013 Local Church Budget") or Department.objects.filter(is_trust=False).first()
        if not _PTU.objects.exists():
            _PTU.objects.create(date=_d.date(_d.date.today().year, _d.date.today().month, 1),
                amount=Decimal("5000"), note="Initial float from bank", recorded_by=treasurer)
            for desc, amt, cat, dept in [("Sabbath tea & sugar", "650", "REFRESHMENTS", lcb),
                                          ("Photocopying bulletins", "300", "STATIONERY", lcb),
                                          ("Boda for guest speaker", "200", "TRANSPORT", lcb)]:
                Expense.objects.create(date=_d.date.today(), department=dept, description=desc,
                    amount=Decimal(amt), category=cat, method="CASH", paid_from_petty_cash=True,
                    status="PAID", paid_date=_d.date.today(), recorded_by=treasurer, approved_by=treasurer)
            self.stdout.write(self.style.SUCCESS("Petty cash: float 5000, 1 top-up, 3 disbursements"))

        # accrual overlay: a credit purchase, an accrual, and a prepayment
        from cashbook.models import Payable, Accrual, Prepayment
        if not Payable.objects.exists():
            Payable.objects.create(date=_d.date.today(), vendor="Mwangi Hardware",
                description="Cement for repairs (on account)", amount=Decimal("18000"),
                department=lcb, category="MAINTENANCE",
                due_date=_d.date.today() + _d.timedelta(days=30), recorded_by=treasurer)
            Accrual.objects.create(date=_d.date.today(), description="Estimated electricity (unbilled)",
                amount=Decimal("3500"), department=lcb, category="UTILITIES", recorded_by=treasurer)
            exp_pre = Expense.objects.create(date=_d.date.today(), department=lcb,
                description="Prepayment: Annual property insurance", amount=Decimal("24000"),
                category="OTHER", method="BANK", status="PAID", paid_date=_d.date.today(),
                recorded_by=treasurer, approved_by=treasurer)
            Prepayment.objects.create(date=_d.date.today(), description="Annual property insurance",
                amount=Decimal("24000"), department=lcb, category="OTHER", months=12,
                start_date=_d.date.today(), source_expense=exp_pre, recorded_by=treasurer)
            self.stdout.write(self.style.SUCCESS("Accruals: 1 payable, 1 accrual, 1 prepayment"))
        from ledger.services import posting as _post
        _post.rebuild()
        self.stdout.write(self.style.SUCCESS("General ledger posted"))

        split_funds = {}
        for sf_name, comps in SPLIT_FUNDS.items():
            sf, _ = SplitFund.objects.get_or_create(name=sf_name)
            for dept_name, pct in comps:
                SplitComponent.objects.get_or_create(
                    split_fund=sf, department=funds[dept_name],
                    defaults=dict(percent=Decimal(pct)))
                # the halves of a split are internal — never offered for direct
                # allocation; the treasurer picks the split fund concept instead
                half = funds[dept_name]
                if half.selectable:
                    half.selectable = False
                    half.save(update_fields=["selectable"])
            split_funds[sf_name] = sf
        self.stdout.write(self.style.SUCCESS(f"Split funds: {len(split_funds)}"))

        for num, gname, target in DEV_GROUPS:
            DevelopmentGroup.objects.get_or_create(
                number=num, defaults=dict(name=gname,
                                          target=Decimal(target) if target else None))
        dev_by_num = {g.number: g for g in DevelopmentGroup.objects.all()}
        self.stdout.write(self.style.SUCCESS(f"Development groups: {len(dev_by_num)}"))

        for ref, fund_name in RULES.items():
            AllocationRule.objects.get_or_create(
                reference=normalize_reference(ref),
                defaults=dict(department=funds[fund_name], source=AllocationRule.Source.SEED))
        for ref, sf_name in SPLIT_RULES.items():
            AllocationRule.objects.get_or_create(
                reference=normalize_reference(ref),
                defaults=dict(split_fund=split_funds[sf_name], source=AllocationRule.Source.SEED))
        # rules learned from the church's labelled narration dataset
        rule_n = 0
        for ref, fund_name in ACCOUNT_RULES.items():
            dept = funds.get(fund_name)
            if not dept:
                continue
            _, created = AllocationRule.objects.get_or_create(
                reference=normalize_reference(ref),
                defaults=dict(department=dept, source=AllocationRule.Source.SEED))
            rule_n += 1 if created else 0
        self.stdout.write(self.style.SUCCESS(
            f"Allocation rules: {len(RULES) + len(SPLIT_RULES) + rule_n}"))

        members = []
        for name, phone, group, mtype, devnum, aliases in MEMBERS:
            m, _ = Member.objects.get_or_create(
                name=name, defaults=dict(phone=phone, group=group, member_type=mtype,
                                         dev_group=dev_by_num.get(devnum) if devnum else None,
                                         source=Member.Source.MANUAL))
            for a in aliases:
                MemberAlias.objects.get_or_create(member=m, name=a)
            members.append(m)
        self.stdout.write(self.style.SUCCESS(f"Members: {len(members)}"))

        if Transaction.objects.exists() or Envelope.objects.exists():
            self.stdout.write(self.style.WARNING(
                "Giving/envelopes already present — leaving them untouched. "
                "Use `--flush` to wipe and reseed everything."))
            self._print_login()
            return

        today = dt.date.today()
        sabbaths = self._sabbaths(today.replace(day=1) - dt.timedelta(days=1)) + \
                   self._sabbaths(today.replace(day=1))

        trust_offerings = ["Tithe", "Camp Meeting", "Evangelism – Field"]
        local_offerings = ["LCB – Local Church Budget", "Sabbath School",
                           "Loose Offering", "Development"]
        n_txn = 0
        for sab in sabbaths:
            if sab > today:
                continue
            wk = sabbath_week_of(sab)
            for m in members:
                if rng.random() < 0.5:
                    continue
                picks = (["Tithe"] if rng.random() < 0.8 else []) + \
                        [rng.choice(trust_offerings[1:] + local_offerings)]
                for fund_name in picks:
                    amt = Decimal(rng.choice([50, 100, 200, 300, 500, 1000, 2000]))
                    Transaction.objects.create(
                        date=sab, sabbath_week=wk,
                        channel=rng.choice([Transaction.Channel.BANK, Transaction.Channel.CASH]),
                        direction=Transaction.Direction.CREDIT, amount=amt,
                        department=funds[fund_name], member=m,
                        reference=fund_name.lower().split()[0],
                        payer_name=m.name, payer_phone=m.phone or "",
                        mpesa_ref=f"UEU{rng.randint(10000,99999)}",
                        allocation_status=Transaction.Status.AUTO,
                        raw_narration=f"DEMO~{fund_name}~{m.phone}~{m.name}")
                    n_txn += 1
                # a Combined Offering, split 50/50 trust/local
                if rng.random() < 0.6:
                    total = Decimal(rng.choice([100, 200, 500, 1000]))
                    sf = split_funds["Combined Offering"]
                    for pdept, pamt in sf.split(total):
                        Transaction.objects.create(
                            date=sab, sabbath_week=wk,
                            channel=Transaction.Channel.BANK,
                            direction=Transaction.Direction.CREDIT, amount=pamt,
                            department=pdept, member=m, reference="combined",
                            payer_name=m.name, payer_phone=m.phone or "",
                            mpesa_ref=f"UEU{rng.randint(10000,99999)}",
                            allocation_status=Transaction.Status.AUTO,
                            raw_narration=f"DEMO~combined~{m.name}")
                        n_txn += 1
            for _ in range(2):
                grp = rng.choice(list(dev_by_num.values()))
                Transaction.objects.create(
                    date=sab, sabbath_week=wk, channel=Transaction.Channel.BANK,
                    direction=Transaction.Direction.CREDIT,
                    amount=Decimal(rng.choice([500, 1000, 2000, 5000])),
                    department=funds["Development"], dev_group=grp,
                    reference=f"devgr{grp.number}",
                    payer_name=rng.choice(members).name,
                    mpesa_ref=f"UEU{rng.randint(10000,99999)}",
                    allocation_status=Transaction.Status.AUTO,
                    raw_narration=f"DEMO~devgr{grp.number}")
                n_txn += 1
            # a little giving into real local department accounts
            for sub_name in ("YOUTH", "CHURCH CHOIR", "AWM"):
                if sub_name not in funds or rng.random() < 0.5:
                    continue
                Transaction.objects.create(
                    date=sab, sabbath_week=wk, channel=Transaction.Channel.CASH,
                    direction=Transaction.Direction.CREDIT,
                    amount=Decimal(rng.choice([100, 200, 300, 500])),
                    department=funds[sub_name], payer_name=rng.choice(members).name,
                    reference=sub_name.lower().replace(" ", ""),
                    allocation_status=Transaction.Status.MANUAL,
                    raw_narration=f"DEMO~{sub_name}")
                n_txn += 1

        for i in range(3):
            sab = min(rng.choice(sabbaths), today)
            Transaction.objects.get_or_create(
                core_ref=f"DEMOQ{i+1}",
                defaults=dict(date=sab, sabbath_week=sabbath_week_of(sab),
                              channel=Transaction.Channel.BANK,
                              direction=Transaction.Direction.CREDIT,
                              amount=Decimal(rng.choice([200, 350, 500])),
                              reference="", payer_name=f"Unknown Payer {i+1}",
                              mpesa_ref=f"UEU{rng.randint(10000,99999)}",
                              allocation_status=Transaction.Status.REVIEW,
                              raw_narration=f"UEU{i}~Other~254700000{i:03d}~Development200"))
        self.stdout.write(self.style.SUCCESS(f"Transactions: {n_txn} allocated + 3 in queue"))

        env_sab = max([s for s in sabbaths if s <= today], default=today)
        receipt = 106706
        for m in members[:6]:
            split = rng.sample([("Tithe", 0.6), ("Sabbath School", 0.2),
                                ("LCB – Local Church Budget", 0.2)], k=rng.choice([1, 2]))
            base = Decimal(rng.choice([100, 200, 500, 1000]))
            env = Envelope.objects.create(
                date=env_sab, sabbath_week=sabbath_week_of(env_sab),
                receipt_no=str(receipt), member=m, contributor_name=m.name,
                channel=Envelope.Channel.CASH, recorded_by=treasurer)
            receipt += 1
            for fund_name, frac in split:
                amt = (base * Decimal(str(frac))).quantize(Decimal("1"))
                if amt <= 0:
                    continue
                txn = Transaction.objects.create(
                    date=env_sab, sabbath_week=env.sabbath_week,
                    channel=Transaction.Channel.ENVELOPE,
                    direction=Transaction.Direction.CREDIT, amount=amt,
                    department=funds[fund_name], member=m, payer_name=m.name,
                    reference=f"envelope {env.receipt_no}",
                    allocation_status=Transaction.Status.MANUAL,
                    raw_narration=f"ENVELOPE {env.receipt_no}")
                EnvelopeLine.objects.create(envelope=env, department=funds[fund_name],
                                            amount=amt, transaction=txn)
            env.recompute_total(); env.save(update_fields=["total"])
        self.stdout.write(self.style.SUCCESS(f"Envelopes: 6 cash envelopes on {env_sab}"))

        for desc, fund_name, amt, cat, claimant, status in EXPENSES:
            d = env_sab
            exp = Expense.objects.create(
                date=d, sabbath_week=sabbath_week_of(d), department=funds[fund_name],
                description=desc, amount=Decimal(amt), category=cat, claimant=claimant,
                status=status, recorded_by=treasurer)
            if status in (Expense.Status.APPROVED, Expense.Status.PAID):
                exp.approved_by = treasurer
                if status == Expense.Status.PAID:
                    exp.paid_date = d
                exp.save()
        self.stdout.write(self.style.SUCCESS(f"Expenses: {len(EXPENSES)}"))

        # --- new-feature demo data: bank account, cash count, staff advance, notice ---
        from statements.models import BankAccount
        from envelopes.models import CountSession, CountDenomination, CountWitness
        from cashbook.models import StaffAdvance
        from core.models import Notification
        from core.utils import last_saturday, sabbath_of
        BankAccount.objects.get_or_create(name="Co-op Current", defaults=dict(
            bank_name="Cooperative Bank", account_number="01129XXXXXX00",
            kind=BankAccount.Kind.CURRENT, is_default=True))
        BankAccount.objects.get_or_create(name="Development Account", defaults=dict(
            bank_name="Cooperative Bank", kind=BankAccount.Kind.DEVELOPMENT))
        sab = sabbath_of(last_saturday())
        if not CountSession.objects.filter(date=sab).exists():
            from envelopes.views import CountSessionCreate
            expected = CountSessionCreate()._expected(sab)
            if expected <= 0:                      # ensure a tidy demo count
                expected = Decimal("6500")
            cs = CountSession.objects.create(date=sab, counted_total=expected,
                expected_total=expected, recorded_by=treasurer,
                note="Counted by the Sabbath counting team.")
            remaining = int(expected)
            for denom in (1000, 500, 200, 100, 50, 20, 10):
                qty = remaining // denom
                if qty > 0:
                    CountDenomination.objects.create(session=cs,
                        denomination=Decimal(denom), count=qty)
                    remaining -= denom * qty
            CountWitness.objects.create(session=cs, name="Head Deacon", role="Counter", signed=True)
            CountWitness.objects.create(session=cs, name="Assistant Treasurer", role="Counter", signed=True)
            from core.models import SabbathClose
            SabbathClose.objects.get_or_create(sabbath=sab,
                defaults={"closed_by": treasurer, "note": "Counted and receipted."})
        youth = Department.objects.filter(name="YOUTH").first() or Department.objects.filter(is_trust=False).first()
        if youth and not StaffAdvance.objects.exists():
            StaffAdvance.objects.create(staff_name="Pr. Mwangi", department=youth,
                amount=Decimal("15000"), date_issued=sab, purpose="Camp meeting travel advance",
                method=StaffAdvance.Method.MPESA, issued_by=treasurer)
        Notification.objects.get_or_create(
            kind=Notification.Kind.REMITTANCE,
            message="Tithe remittance is due — check the trust funds on the dashboard.",
            defaults=dict(link="/"))
        self.stdout.write(self.style.SUCCESS(
            "Channels: 2 bank accounts, 1 cash count, 1 staff advance, 1 notice"))
        # custom expense category + dev-group leader contacts (demo)
        from cashbook.models import ExpenseCategory
        ExpenseCategory.objects.get_or_create(code="MUSIC",
            defaults={"label": "Music ministry", "sort": 50})
        from departments.models import DevelopmentGroup as _DG
        for i, (nm, em) in enumerate([("Bro. Otieno", "leader1@example.com"),
                                      ("Sis. Achieng", "")]):
            g = _DG.objects.filter(number=i + 1).first()
            if g:
                g.leader_name = nm
                g.leader_email = em
                g.save(update_fields=["leader_name", "leader_email"])
        self._print_login()

    def _print_login(self):
        self.stdout.write(self.style.SUCCESS(
            "\nDemo ready. Sign in at /  with:\n"
            "  treasurer / treasurer123   (full access)\n"
            "  assistant / assistant123   (data entry)\n"
            "  auditor   / auditor123     (read-only)"))

    def _user(self, username, first, last, pw, role, superuser=False):
        u, created = User.objects.get_or_create(
            username=username,
            defaults=dict(first_name=first, last_name=last,
                          is_staff=superuser, is_superuser=superuser))
        if created:
            u.set_password(pw); u.save()
        if not superuser:
            u.groups.set([Group.objects.get(name=role)])
        return u

    def _sabbaths(self, any_day_in_month):
        d = any_day_in_month.replace(day=1)
        while d.weekday() != 5:
            d += dt.timedelta(days=1)
        out = []
        while d.month == any_day_in_month.month:
            out.append(d); d += dt.timedelta(days=7)
        return out
