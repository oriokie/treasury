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
from django.utils import timezone

from core.models import SiteConfig, SmsLog
from core.roles import TREASURER, ASSISTANT, AUDITOR, LEADER, ALL_ROLES
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
            # pledges reference members (PROTECT) and transactions, so clear them
            # before those — pledges are informational and safe to wipe on reseed
            from pledges.models import (PledgePayment, Pledge, PledgeCampaign,
                                        PledgeReminderLog)
            PledgeReminderLog.objects.all().delete()
            PledgePayment.objects.all().delete()
            Pledge.objects.all().delete()
            PledgeCampaign.objects.all().delete()
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

        # a demo department leader: read-only, scoped to one development fund so
        # the dev-group view is exercised. Falls back to any fund if needed.
        from departments.models import DepartmentLeadership
        leader = self._user("leader", "Leah", "Leader", "leader123", LEADER)
        lead_dept = (Department.objects.filter(category=C.DEVELOPMENT,
                                               parent__isnull=True).first()
                     or Department.objects.filter(parent__isnull=True).first())
        if lead_dept:
            DepartmentLeadership.objects.get_or_create(user=leader, department=lead_dept)
            self.stdout.write(self.style.SUCCESS(
                f"Department leader: leader / leader123  (sees {lead_dept.name})"))

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
        self._seed_benevolent(treasurer, sab)
        self._print_login()

    # ---- Benevolent Scheme Engine -----------------------------------------
    def _seed_benevolent(self, treasurer, sab):
        """A working benevolent scheme, so the module is usable the moment the
        demo comes up: a fund, a published policy with a benefit schedule, a
        handful of enrolled members with their dues, and one case run all the way
        through to a paid benefit."""
        from benevolent.models import (BenevolentCase, BenevolentEventType,
                                       BenevolentScheme, SchemeBenefitRule,
                                       SchemePolicy)
        from benevolent.services import cases as case_svc
        from benevolent.services import contributions as contrib_svc
        from benevolent.services import notify as notify_svc
        from benevolent.services import schemes as scheme_svc

        if BenevolentScheme.objects.exists():
            return

        # Phase 7: installed FIRST, so every registration, renewal and case
        # decision seeded below (Phases 1-6) actually produces a notification
        # — the same order a real deployment would follow (templates exist
        # before the first member ever joins), and what makes the seeded
        # notification history worth looking at rather than empty.
        notify_svc.install_default_templates()

        fund, _ = Department.objects.get_or_create(
            name="BENEVOLENT", defaults=dict(
                slug="benevolent", fund_type=Department.FundType.LOCAL,
                category=Department.Category.MINISTRY, show_in_expenses=True,
                # a real welfare scheme runs on accumulated reserves; without an
                # opening balance the seeded benefit would drive the fund
                # negative, which is not what a healthy scheme looks like
                opening_balance=Decimal("60000")))

        scheme = BenevolentScheme.objects.create(
            name="Church Benevolent Scheme", code="BEN", fund=fund,
            kind=BenevolentScheme.Kind.BENEVOLENT, created_by=treasurer,
            description="Members contribute monthly dues; the scheme pays a set benefit "
                        "on a bereavement or a hospitalisation.")

        events = {}
        for name, code in [("Bereavement — member", "BER_MEMBER"),
                           ("Bereavement — spouse or child", "BER_SPOUSE"),
                           ("Bereavement — parent", "BER_PARENT"),
                           ("Hospitalisation", "HOSPITAL")]:
            events[code] = BenevolentEventType.objects.create(
                scheme=scheme, name=name, code=code, requires_document=False)

        policy = SchemePolicy.objects.create(
            scheme=scheme, effective_from=dt.date(sab.year, 1, 1),
            membership_required=True, waiting_period_days=60, min_contributions=1,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("200"),
            contribution_frequency=SchemePolicy.Frequency.MONTHLY,
            benefit_mode=SchemePolicy.BenefitMode.SCHEDULE,
            benefit_cap=Decimal("50000"), claim_window_days=90,
            max_claims_per_year=2, allow_override=True, created_by=treasurer,
            notes="Approved by the church board. Benefits differ by relationship.")
        for code, amt in [("BER_MEMBER", "50000"), ("BER_SPOUSE", "30000"),
                          ("BER_PARENT", "20000"), ("HOSPITAL", "10000")]:
            SchemeBenefitRule.objects.create(
                policy=policy, event_type=events[code], amount=Decimal(amt))
        scheme_svc.publish_policy(policy, user=treasurer)
        scheme_svc.activate_scheme(scheme, user=treasurer, on=dt.date(sab.year, 1, 1))

        joined = dt.date(sab.year, 1, 1)
        members = list(Member.objects.filter(active=True).order_by("id")[:8])
        memberships = [scheme_svc.enrol(scheme, m, joined_on=joined, user=treasurer)
                       for m in members]

        # dues up to the seeded Sabbath — two members are left a month behind, so
        # the arrears view has something real to show
        for i, ms in enumerate(memberships):
            month = dt.date(sab.year, 1, 1)
            while month <= sab.replace(day=1):
                if i >= 6 and month == sab.replace(day=1):
                    break
                contrib_svc.record_contribution(
                    scheme, date=month.replace(day=8), amount=Decimal("200"),
                    membership=ms, user=treasurer, channel=Transaction.Channel.CASH)
                month = (month.replace(day=28) + dt.timedelta(days=7)).replace(day=1)

        # one case, run end to end, so every screen has real content
        if memberships:
            claimant = memberships[0]
            case = BenevolentCase.objects.create(
                scheme=scheme, membership=claimant, event_type=events["BER_PARENT"],
                event_date=sab - dt.timedelta(days=14),
                reported_date=sab - dt.timedelta(days=12),
                beneficiary_name=f"Late parent of {claimant.member.name}",
                description="Bereavement reported to the elders; burial permit received.",
                raised_by=treasurer)
            case_svc.submit_case(case, user=treasurer)
            case_svc.assess_case(case, user=treasurer)
            case_svc.approve_case(case, amount=Decimal("20000"), user=treasurer,
                                  allow_self_approval=True)
            payout = case_svc.record_payout(
                case, amount=Decimal("20000"), date=sab - dt.timedelta(days=7),
                user=treasurer, method=Expense.Method.MPESA, voucher_no="BEN-001")
            # the treasurer clears the voucher through the ORDINARY expense
            # workflow; the case follows automatically, via the signal
            exp = payout.expense
            exp.status = Expense.Status.PAID
            exp.approved_by = treasurer
            exp.paid_date = sab - dt.timedelta(days=7)
            exp.save()

        self._seed_benevolent_phase2(treasurer, sab, scheme, memberships)

        self.stdout.write(self.style.SUCCESS(
            f"Benevolent: 1 scheme, policy v1 (4 benefits), "
            f"{len(memberships)} members, 1 paid case"))

    # ---- Phase 2: settings, profiles, a committee/levy scheme --------------
    def _seed_benevolent_phase2(self, treasurer, sab, main_scheme, memberships):
        """Enough Phase 2 in the demo to see it working: the built-in profile
        library, sensible module settings, and a SECOND scheme configured the
        other way round — a per-case levy paying out what it collects, approved by
        a committee — so the two shapes sit side by side and the engine's claim
        (same code, different configuration) is visible rather than asserted."""
        from benevolent.models import (BenevolentCase, BenevolentEventType,
                                       BenevolentScheme, BenevolentSettings,
                                       PolicyProfile, SchemeNominee, SchemePolicy)
        from benevolent.services import cases as case_svc
        from benevolent.services import contributions as contrib_svc
        from benevolent.services import profiles as profile_svc
        from benevolent.services import schemes as scheme_svc

        profile_svc.install_builtins()

        cfg = BenevolentSettings.get()
        cfg.automation_enabled = False          # off until a treasurer has seen a dry run
        cfg.default_profile = PolicyProfile.objects.filter(
            name="Monthly dues, fixed benefit").first()
        cfg.save()

        # a nominee on the main scheme, so the inheritance rule has something real
        if memberships:
            SchemeNominee.objects.get_or_create(
                membership=memberships[0], name="Next of kin",
                defaults={"relationship": "Spouse", "share_percent": Decimal("100"),
                          "is_successor": True})

        if BenevolentScheme.objects.filter(code="MED").exists():
            return

        med_fund, _ = Department.objects.get_or_create(
            name="MEDICAL", defaults=dict(
                slug="medical", fund_type=Department.FundType.LOCAL,
                category=Department.Category.MINISTRY, show_in_expenses=True,
                opening_balance=Decimal("15000")))
        med = BenevolentScheme.objects.create(
            name="Medical Harambee Scheme", code="MED", fund=med_fund,
            kind=BenevolentScheme.Kind.MEDICAL, created_by=treasurer,
            description="No standing dues. When a member is hospitalised, every member is "
                        "levied and the family receives what is collected. Approved by the "
                        "welfare committee, not by one person.")
        hosp = BenevolentEventType.objects.create(
            scheme=med, name="Hospitalisation", code="HOSPITAL")
        BenevolentEventType.objects.create(scheme=med, name="Surgery", code="SURGERY")

        pol = SchemePolicy.objects.create(
            scheme=med, effective_from=dt.date(sab.year, 1, 1),
            membership_required=True, waiting_period_days=30,
            registration_required=True,
            registration_approval=SchemePolicy.RegistrationApproval.AUTO,
            registration_fee=Decimal("200"),
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"), max_levies_per_year=12,
            funding_methods=["LEVY", "DONATION"],
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            benefit_mode=SchemePolicy.BenefitMode.POOLED,
            benefit_rounding=SchemePolicy.Rounding.HUNDRED,
            approval_mode=SchemePolicy.ApprovalMode.COMMITTEE,
            committee_quorum=3,
            bereaved_contribution_policy=SchemePolicy.BereavedContributionPolicy.EXEMPT,
            inheritance_mode=SchemePolicy.InheritanceMode.NEXT_OF_KIN,
            claim_window_days=60, max_claims_per_year=2, allow_override=True,
            created_by=treasurer,
            notes="Adopted by the church board. The scheme pays out what it collects, so "
                  "it can never become insolvent.")
        scheme_svc.publish_policy(pol, user=treasurer)
        scheme_svc.activate_scheme(med, user=treasurer, on=dt.date(sab.year, 1, 1))

        joined = dt.date(sab.year, 1, 15)
        med_members = []
        for m in Member.objects.filter(active=True).order_by("id")[:6]:
            ms = scheme_svc.enrol(med, m, joined_on=joined, user=treasurer)
            ms.registration_fee_paid = True
            ms.save(update_fields=["registration_fee_paid"])
            med_members.append(ms)

        # a live levy round: one member hospitalised, the others levied, some paid
        if med_members:
            patient = med_members[0]
            case = BenevolentCase.objects.create(
                scheme=med, membership=patient, event_type=hosp,
                event_date=sab - dt.timedelta(days=10),
                reported_date=sab - dt.timedelta(days=9),
                claimed_amount=Decimal("18000"),
                description="Admitted for five nights; discharge summary on file.",
                raised_by=treasurer)
            case_svc.submit_case(case, user=treasurer)
            case_svc.assess_case(case, user=treasurer)
            # three of the five levied members have paid so far — the case is still
            # collecting, which is exactly what a pooled scheme looks like mid-round
            for ms in med_members[1:4]:
                contrib_svc.record_contribution(
                    med, date=sab - dt.timedelta(days=5), amount=Decimal("500"),
                    membership=ms, case=case, user=treasurer,
                    channel=Transaction.Channel.CASH,
                    note=f"Levy for {case.number}")

        self._seed_benevolent_phase3(treasurer, sab, main_scheme, med, med_members)

        self.stdout.write(self.style.SUCCESS(
            "Benevolent (Phase 2): 4 policy profiles, settings, "
            "1 levy/committee scheme mid-collection"))

    # ---- Phase 3: registry, households, standing ---------------------------
    def _seed_benevolent_phase3(self, treasurer, sab, main_scheme, med, med_members):
        """The registry, showing what it is for: a household registration, a life
        member excused from dues, a member who has stopped contributing, and one who
        has died and passed the membership to his widow — so every standing on the
        register is a real one, not a demo constant."""
        from benevolent.models import (MembershipExemption, RegistrationType,
                                       SchemeDependant, SchemeMembership, Standing)
        from benevolent.services import registry as reg_svc
        from benevolent.services import standing as standing_svc

        memberships = list(main_scheme.memberships.select_related("member")
                           .order_by("id"))
        if not memberships or memberships[0].registration_type == \
                RegistrationType.HOUSEHOLD:
            return

        pool = list(Member.objects.filter(active=True).order_by("id"))

        # 1. a HOUSEHOLD registration — spouse LINKED to the church roll, not retyped
        if len(memberships) >= 1 and len(pool) > 9:
            hh = memberships[0]
            hh.registration_type = RegistrationType.HOUSEHOLD
            hh.household_name = f"The {hh.member.name.split()[-1].title()} household"
            hh.save(update_fields=["registration_type", "household_name"])
            spouse = next((p for p in pool if p.pk != hh.member_id), None)
            if spouse and not hh.dependants.filter(
                    relationship=SchemeDependant.Relationship.SPOUSE).exists():
                reg_svc.add_dependant(
                    hh, member=spouse,
                    relationship=SchemeDependant.Relationship.SPOUSE,
                    registered_on=hh.joined_on, user=treasurer)
            reg_svc.add_dependant(
                hh, name="Baby Achieng",
                relationship=SchemeDependant.Relationship.CHILD,
                date_of_birth=dt.date(sab.year - 4, 3, 12),
                registered_on=hh.joined_on, user=treasurer)

        # 2. a LIFE MEMBER, formally excused — proposed by one person, approved by
        #    another, with a reason on the permanent record
        if len(memberships) >= 3:
            ex = reg_svc.grant_exemption(
                memberships[2], kind=MembershipExemption.Kind.LIFE,
                reason="Founding member of the scheme; excused from dues by board "
                       "resolution 2019/3.",
                from_date=dt.date(sab.year, 1, 1),
                exempt_dues=True, exempt_levies=False, user=treasurer)
            ex.approved_by = treasurer      # demo only; the service requires two people
            ex.approved_at = timezone.now()
            ex.save(update_fields=["approved_by", "approved_at"])

        # 3. a member who has DIED, and whose membership passes to his widow —
        #    keeping the joining date, so the years already paid in are not lost
        if len(memberships) >= 5:
            deceased = memberships[4]
            surname = deceased.member.name.split()[-1].title()
            widow, _ = Member.objects.get_or_create(
                name=f"Widow {surname}",
                defaults={"phone": "254799000001", "active": True})
            reg_svc.add_dependant(
                deceased, member=widow,
                relationship=SchemeDependant.Relationship.SPOUSE,
                registered_on=deceased.joined_on, user=treasurer)
            reg_svc.record_death(
                deceased, died_on=sab - dt.timedelta(days=21), user=treasurer,
                reason="Reported to the elders; burial permit on file.")
            reg_svc.transfer(
                deceased, widow, on=sab - dt.timedelta(days=14), user=treasurer,
                reason="Surviving spouse succeeds to the membership under the "
                       "constitution. The years already paid in stay with the "
                       "household.")

        # 4. recompute every standing on both schemes, so the register is honest
        changed = 0
        for sch in (main_scheme, med):
            changed += len(standing_svc.refresh_scheme(sch, user=treasurer))

        self.stdout.write(self.style.SUCCESS(
            f"Benevolent (Phase 3): registry with a household, a life-member "
            f"exemption, a death and a transfer; {changed} standings computed"))

        self._seed_benevolent_phase4(treasurer, sab, main_scheme, med)

    # ---- Phase 4: the contribution engine ---------------------------------
    def _seed_benevolent_phase4(self, treasurer, sab, main_scheme, med):
        """The engine doing its job, on real bank receipts: one attached
        automatically, one queued because it is ambiguous, one unmatched, one that
        looks like a duplicate — plus a penalty and a waiver, neither of which posts
        anything, and a refund, which does."""
        from benevolent.models import (BenevolentContribution, ContributionRule,
                                       MemberAdjustment, SchemeDependant,
                                       SchemeMembership)
        from benevolent.services import engine as engine_svc
        from giving.models import Transaction as Txn

        if ContributionRule.objects.exists():
            return

        for scheme, pattern in ((main_scheme, "ben"), (med, "med")):
            ContributionRule.objects.create(
                pattern=pattern, match_type=ContributionRule.MatchType.CONTAINS,
                scheme=scheme, priority=1, seeded=True, source="SEEDED")

        live = list(main_scheme.memberships.filter(
            status=SchemeMembership.Status.ACTIVE).select_related("member")[:4])
        if not live:
            return

        # a spouse's number on file, so the allocator can recognise her paying his dues
        spouse = SchemeDependant.objects.filter(
            membership__scheme=main_scheme,
            relationship=SchemeDependant.Relationship.SPOUSE).first()
        if spouse and not spouse.phone:
            spouse.phone = "254733111222"
            spouse.save(update_fields=["phone"])

        def receipt(amount, reference, phone="", name="", days=0):
            return Txn.objects.create(
                date=sab - dt.timedelta(days=days), amount=Decimal(amount),
                department=main_scheme.fund,
                direction=Txn.Direction.CREDIT, channel=Txn.Channel.BANK,
                allocation_status=Txn.Status.AUTO, confirmed=True,
                reference=reference, payer_phone=phone, payer_name=name,
                raw_narration=f"{reference} {name} {phone}")

        # 1. an easy one: the member's own number, the exact dues, a clear narration
        m0 = live[0]
        engine_svc.intake(receipt("200", "ben dues", m0.member.phone or "",
                                  m0.member.name, days=6))

        # 2. the spouse paying his dues from HER phone — routine, and a system that
        #    could not see it would queue a perfectly ordinary payment every month
        if spouse:
            engine_svc.intake(receipt("200", "ben dues", "254733111222",
                                      spouse.display_name, days=5))

        # 3. nothing identifies the payer: an honest blank, not a bad guess. The money
        #    is banked and in the ledger all the same.
        engine_svc.intake(receipt("500", "ben", "254700000999", "UNKNOWN PAYER",
                                  days=4))

        # 4. the same member, the same amount, two days later — flagged, never blocked
        engine_svc.intake(receipt("200", "ben dues", m0.member.phone or "",
                                  m0.member.name, days=4))

        # 5. an obligation that is NOT money: a penalty charged, and one waived
        if len(live) > 1:
            p = engine_svc.charge(
                live[1], kind=MemberAdjustment.Kind.PENALTY, amount=Decimal("100"),
                reason="Dues more than three months in arrears (rule 7).",
                on=sab - dt.timedelta(days=10), user=treasurer)
            p.approved_by = treasurer
            p.approved_at = timezone.now()
            p.save(update_fields=["approved_by", "approved_at"])
        if len(live) > 2:
            w = engine_svc.waive(
                live[2], amount=Decimal("400"),
                reason="Hardship — out of work since March. Approved by the board.",
                on=sab - dt.timedelta(days=20), user=treasurer)
            w.approved_by = treasurer
            w.approved_at = timezone.now()
            w.save(update_fields=["approved_by", "approved_at"])

        from benevolent.models import ContributionIntake
        self.stdout.write(self.style.SUCCESS(
            f"Benevolent (Phase 4): 2 narration rules, "
            f"{ContributionIntake.objects.count()} receipts through the allocator "
            f"(auto / spouse / unmatched / duplicate), 1 penalty, 1 waiver"))

        self._seed_benevolent_phase5(treasurer, sab, main_scheme)

    # ---- Phase 5: bereavement case management ------------------------------
    def _seed_benevolent_phase5(self, treasurer, sab, main_scheme):
        """Three cases showing the phase's actual new ground: a funding target
        with real progress against it, a case-by-case committee decision on
        the bereaved member's own contribution, and a document checklist
        partly satisfied — plus the full history each one now leaves behind."""
        from benevolent.models import (BenevolentCase, SchemeMembership,
                                       SchemePolicy)
        from benevolent.services import cases as case_svc
        from benevolent.services import contributions as contrib_svc
        from benevolent.services import schemes as scheme_svc

        if BenevolentCase.objects.filter(
                scheme=main_scheme, funding_target__isnull=False).exists():
            self.stdout.write(self.style.SUCCESS(
                "Benevolent (Phase 5): cases already exist, skipping"))
            return

        bereavement = main_scheme.event_types.filter(code="BER").first() \
            or main_scheme.event_types.first()
        if bereavement is None:
            return

        live = list(main_scheme.memberships.filter(
            status=SchemeMembership.Status.ACTIVE).select_related("member")[:6])
        if len(live) < 3:
            return

        # 1. a funding target, part-collected — the fundraising-progress case
        target_case = case_svc.create_case(
            main_scheme, event_type=bereavement, membership=live[0],
            event_date=sab - dt.timedelta(days=6), reported_date=sab - dt.timedelta(days=5),
            description="Funeral expenses — father of the member.",
            funding_target=Decimal("30000"), user=treasurer)
        case_svc.submit_case(target_case, user=treasurer)
        case_svc.assess_case(target_case, user=treasurer)
        for i, payer in enumerate(live[1:4]):
            contrib_svc.record_contribution(
                main_scheme, date=sab - dt.timedelta(days=4 - i), amount=Decimal("500"),
                membership=payer, case=target_case, user=treasurer)

        # 2. a committee-decides bereaved policy, ruled on — the constitution
        #    keeps its default EXEMPT scheme-wide, so this case gets its own
        #    one-off policy version to demonstrate the pathway without
        #    disturbing every other case's assessment basis
        v2 = scheme_svc.new_version_from(
            main_scheme.current_policy, effective_from=sab - dt.timedelta(days=3),
            user=treasurer)
        v2.bereaved_contribution_policy = \
            SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES
        v2.save()
        scheme_svc.publish_policy(v2, user=treasurer)

        decided_case = case_svc.create_case(
            main_scheme, event_type=bereavement, membership=live[1],
            event_date=sab - dt.timedelta(days=2), reported_date=sab - dt.timedelta(days=1),
            description="Bereavement — spouse.", user=treasurer)
        case_svc.submit_case(decided_case, user=treasurer)
        case_svc.assess_case(decided_case, user=treasurer)
        case_svc.decide_bereaved_contribution(
            decided_case, waived=True,
            reason="Family circumstances discussed at the board meeting; waived by "
                  "unanimous vote.", user=treasurer)
        case_svc.approve_case(decided_case, user=treasurer, allow_self_approval=True)

        # 3. a document checklist, half-satisfied — the case sits at ASSESSED,
        #    correctly not yet approvable. Applied only now: cases 1 and 2
        #    above must not be affected by a requirement introduced for case 3.
        bereavement.required_documents = ["Burial permit", "Death certificate"]
        bereavement.save(update_fields=["required_documents"])
        doc_case = case_svc.create_case(
            main_scheme, event_type=bereavement, membership=live[2],
            event_date=sab - dt.timedelta(days=1), reported_date=sab, user=treasurer)
        case_svc.submit_case(doc_case, user=treasurer)
        from benevolent.models import CaseAttachment
        from django.core.files.base import ContentFile
        CaseAttachment.objects.create(
            case=doc_case, document_type="Burial permit", uploaded_by=treasurer,
            file=ContentFile(b"Demo placeholder - burial permit.", name="burial_permit.txt"))
        case_svc.assess_case(doc_case, user=treasurer)

        self.stdout.write(self.style.SUCCESS(
            "Benevolent (Phase 5): a funding-target case (part-collected), a "
            "committee-decided bereaved contribution, a document checklist "
            f"half-satisfied — {sum(c.events.count() for c in [target_case, decided_case, doc_case])} "
            "case-history events logged"))

        self._seed_benevolent_phase6(treasurer, main_scheme)

    # ---- Phase 6: committee management & policy evaluation -----------------
    def _seed_benevolent_phase6(self, treasurer, main_scheme):
        """A real committee roster with a Chair, an approval level that
        actually requires her vote, and a lapsed member reinstated with a
        fee charged automatically — the bug this phase fixed."""
        from benevolent.models import CommitteeMember, SchemeMembership, SchemePolicy
        from benevolent.services import committee as committee_svc
        from benevolent.services import registry as reg_svc
        from benevolent.services import schemes as scheme_svc

        if CommitteeMember.objects.filter(scheme=main_scheme).exists():
            self.stdout.write(self.style.SUCCESS(
                "Benevolent (Phase 6): committee already seeded, skipping"))
            return

        assistant = User.objects.filter(groups__name="Assistant").first() or treasurer
        auditor = User.objects.filter(groups__name="Auditor").first() or treasurer
        committee_svc.add_member(main_scheme, treasurer,
                                 role=CommitteeMember.Role.CHAIR, added_by=treasurer)
        if assistant != treasurer:
            committee_svc.add_member(main_scheme, assistant,
                                     role=CommitteeMember.Role.SECRETARY,
                                     added_by=treasurer)
        if auditor not in (treasurer, assistant):
            committee_svc.add_member(main_scheme, auditor,
                                     role=CommitteeMember.Role.MEMBER, added_by=treasurer)

        policy = main_scheme.current_policy
        if policy is not None and not policy.committee_requires_chair:
            v = scheme_svc.new_version_from(
                policy, effective_from=dt.date.today() - dt.timedelta(days=1),
                user=treasurer)
            v.committee_requires_chair = True
            v.reinstatement_fee = Decimal("300")
            v.save()
            scheme_svc.publish_policy(v, user=treasurer)

        # a lapsed member, reinstated — the reinstatement fee should now be
        # charged automatically, which it silently never was before this phase
        lapsed = main_scheme.memberships.filter(
            status=SchemeMembership.Status.SUSPENDED).first()
        if lapsed is None:
            candidate = main_scheme.memberships.filter(
                status=SchemeMembership.Status.ACTIVE).last()
            if candidate is not None:
                reg_svc.suspend(candidate, user=treasurer, reason="Demo: lapsed dues.")
                lapsed = candidate
        if lapsed is not None:
            reg_svc.reinstate(lapsed, user=treasurer, reason="Demo: caught up and returned.")

        self.stdout.write(self.style.SUCCESS(
            "Benevolent (Phase 6): committee seated (Chair + 2), an approval level "
            "requiring the Chair's vote, and a reinstatement fee charged automatically"))

        self._seed_benevolent_phase7(main_scheme)

    # ---- Phase 7: financial integration & communications -------------------
    def _seed_benevolent_phase7(self, main_scheme):
        """Templates were installed at the very start (see _seed_benevolent)
        so every registration, renewal and case decision seeded since has
        already produced real notification history — this just gives one
        member an email address (SchemeMembership.email, scoped to this
        module, since members.Member has none) so the demo shows the EMAIL
        channel working too, not only SMS."""
        from benevolent.models import BenevolentNotification

        m = main_scheme.memberships.filter(status="ACTIVE").first()
        if m is not None and not m.email:
            m.email = f"{m.member.name.split()[0].lower()}@example.com"
            m.save(update_fields=["email"])

        sent = BenevolentNotification.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Benevolent (Phase 7): {sent} notification(s) logged from the "
            f"registrations, renewals and case decisions seeded above"))

        self._seed_benevolent_phase9()

    # ---- Phase 9: roles, permissions & UX -----------------------------------
    def _seed_benevolent_phase9(self):
        """One demo user per seeded role profile, so the granular permission
        split (Registration/Case/Finance Officer, Approver, Committee
        Member, Administrator, Auditor) is something a reviewer can actually
        log in and see, not just a migration nobody visits."""
        from accounts.models import Profile

        role_users = {
            "ben_admin": "Benevolent Administrator (default)",
            "ben_approver": "Benevolent Approver (default)",
            "ben_committee": "Benevolent Committee Member (default)",
            "ben_registrar": "Benevolent Registration Officer (default)",
            "ben_case_officer": "Benevolent Case Officer (default)",
            "ben_finance": "Benevolent Finance Officer (default)",
            "ben_auditor": "Benevolent Auditor (default)",
        }
        if User.objects.filter(username="ben_admin").exists():
            self.stdout.write(self.style.SUCCESS(
                "Benevolent (Phase 9): role demo users already seeded, skipping"))
            return

        for username, profile_name in role_users.items():
            u = User.objects.create_user(username, password=f"{username}123")
            profile = Profile.objects.filter(name=profile_name).first()
            if profile is not None:
                profile.users.add(u)

        self.stdout.write(self.style.SUCCESS(
            f"Benevolent (Phase 9): {len(role_users)} role demo users seeded "
            f"(ben_admin, ben_approver, ben_committee, ben_registrar, "
            f"ben_case_officer, ben_finance, ben_auditor — each password "
            f"'<username>123')"))

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
