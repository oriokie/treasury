#!/usr/bin/env bash
# One-shot legacy import. Copy import_legacy.py into your project at
# core/management/commands/import_legacy.py first, then run this from anywhere:
#     ./run_import.sh /path/to/treasury-project
# It cleans the database, creates a Treasurer (treasurer / treasurer123) and
# imports every phase. Account questions (if any) are asked interactively.
set -e
PROJECT="${1:-.}"
DATADIR="$(cd "$(dirname "$0")" && pwd)"
MAP="$DATADIR/account_map.json"
cd "$PROJECT"
[ -d .venv ] && source .venv/bin/activate
echo "Importing legacy data from: $DATADIR"
python manage.py import_legacy --phase departments --clean --dir "$DATADIR" --map "$MAP"
python manage.py import_legacy --phase envelopes  --dir "$DATADIR" --map "$MAP"
python manage.py import_legacy --phase bank       --dir "$DATADIR" --map "$MAP"
python manage.py import_legacy --phase expenses   --dir "$DATADIR" --map "$MAP"
python manage.py import_legacy --phase collection --dir "$DATADIR" --map "$MAP"
echo "Legacy import complete."
