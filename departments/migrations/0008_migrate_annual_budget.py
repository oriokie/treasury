from django.db import migrations
import datetime


def forward(apps, schema_editor):
    Department = apps.get_model("departments", "Department")
    Budget = apps.get_model("departments", "Budget")
    year = datetime.date.today().year
    for d in Department.objects.exclude(annual_budget__isnull=True).exclude(annual_budget=0):
        Budget.objects.get_or_create(year=year, department=d,
                                     defaults={"amount": d.annual_budget})


def backward(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("departments", "0007_alter_department_annual_budget")]
    operations = [migrations.RunPython(forward, backward)]
