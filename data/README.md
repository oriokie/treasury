# data/ — legacy church data + import tooling

This folder ships inside the app so the historical data and the importer travel
together.

Contents: the seven source workbooks, account_map.json, import_legacy.py (a copy
of the management command also installed at core/management/commands/), and
run_import.sh.

## Import everything (cleans DB, creates Treasurer treasurer/treasurer123):
    python manage.py import_legacy --phase all --clean
(--dir defaults to this folder; --map defaults to ./account_map.json.)

Run phase-by-phase for large data:
    python manage.py import_legacy --phase departments --clean
    python manage.py import_legacy --phase envelopes
    python manage.py import_legacy --phase bank
    python manage.py import_legacy --phase expenses
    python manage.py import_legacy --phase collection
