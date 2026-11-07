# Live Development board: configurable Paybill + Account for presentation.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0069_queue_inquiry_sms'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalsiteconfig',
            name='dev_live_account',
            field=models.CharField(
                blank=True, default='441211#DEV',
                help_text='Account / reference shown on the live Development board '
                          '(e.g. 441211#DEV). Leave blank to hide.',
                max_length=80, verbose_name='Live board Account'),
        ),
        migrations.AddField(
            model_name='historicalsiteconfig',
            name='dev_live_paybill',
            field=models.CharField(
                blank=True, default='400222',
                help_text='M-Pesa Paybill shown on the live Development board '
                          '(e.g. 400222). Leave blank to hide.',
                max_length=40, verbose_name='Live board Paybill'),
        ),
        migrations.AddField(
            model_name='siteconfig',
            name='dev_live_account',
            field=models.CharField(
                blank=True, default='441211#DEV',
                help_text='Account / reference shown on the live Development board '
                          '(e.g. 441211#DEV). Leave blank to hide.',
                max_length=80, verbose_name='Live board Account'),
        ),
        migrations.AddField(
            model_name='siteconfig',
            name='dev_live_paybill',
            field=models.CharField(
                blank=True, default='400222',
                help_text='M-Pesa Paybill shown on the live Development board '
                          '(e.g. 400222). Leave blank to hide.',
                max_length=40, verbose_name='Live board Paybill'),
        ),
    ]
