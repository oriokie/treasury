"""Reclassify existing liability-related vouchers into the Liability class.

Uses queryset .update() deliberately: no save() side effects, no new
django-simple-history rows, no timestamps touched — the vouchers, their
accounting entries and their audit history are byte-for-byte what they were;
only the presentation class changes. Custom categories flagged is_liability
later are refiled automatically on their next save (or by re-running this
logic), since Expense.save() derives doc_class from the category.
"""
from django.db import migrations

LIABILITY_BUILTIN = ("REMITTANCE", "LOAN_REPAYMENT")


def reclassify(apps, schema_editor):
    Expense = apps.get_model("cashbook", "Expense")
    HistoricalExpense = apps.get_model("cashbook", "HistoricalExpense")
    Expense.objects.filter(category__in=LIABILITY_BUILTIN).update(
        doc_class="LIABILITY")
    # keep the historical snapshots consistent with the live rows so audit
    # views show the same classification (no new history rows are created)
    HistoricalExpense.objects.filter(category__in=LIABILITY_BUILTIN).update(
        doc_class="LIABILITY")


def declassify(apps, schema_editor):
    Expense = apps.get_model("cashbook", "Expense")
    HistoricalExpense = apps.get_model("cashbook", "HistoricalExpense")
    Expense.objects.filter(category__in=LIABILITY_BUILTIN).update(
        doc_class="EXPENSE")
    HistoricalExpense.objects.filter(category__in=LIABILITY_BUILTIN).update(
        doc_class="EXPENSE")


class Migration(migrations.Migration):
    dependencies = [
        ("cashbook", "0035_expense_doc_class_expensecategory_is_liability_and_more"),
    ]

    operations = [
        migrations.RunPython(reclassify, declassify),
    ]
