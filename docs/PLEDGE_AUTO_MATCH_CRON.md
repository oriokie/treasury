# Pledge auto-match on a schedule

The treasurer preview at `/pledges/auto-match/` is still the way to review
matches by hand. For unattended runs, use the management command.

## 1. Turn it on in Settings

**Settings → Pledges → Allow scheduled auto-match** must be ticked. Until it is,
`pledge_auto_match` refuses to write (unless you pass `--force` for a one-off
test).

## 2. Dry-run first

From the project directory, with the same virtualenv the site uses:

```bash
cd /path/to/treasury
.venv/bin/python manage.py pledge_auto_match --dry-run
```

You should see each proposed link (member, campaign, amount, txn id). Nothing
is written.

## 3. Add a cron line

Every 30 minutes is a sensible start (idempotent — a second pass finds nothing
new once gifts are linked):

```
*/30 * * * *  cd /path/to/treasury && .venv/bin/python manage.py pledge_auto_match >> /path/to/treasury/logs/pledge_auto_match.log 2>&1
```

On cPanel: Cron Jobs → paste the same line (use the absolute path to your
Python and project).

Cron does **not** load your shell profile. Use the venv’s `python` and ensure
env vars the app needs (`TREASURY_ENCRYPTION_KEY`, DB settings, etc.) are
available the same way other cron jobs (`backup_db`, `benevolent_automation`)
are set up on this host.

## 4. Options

| Flag | Effect |
|------|--------|
| `--dry-run` | Print the plan; write nothing |
| `--force` | Run even if the Settings toggle is off |
| `--fuzzy` | Also apply near-miss cash/envelope names (threshold in Settings) |
| `--campaign N` | Limit to one campaign id |

By default the cron job applies **exact** matches only. Fuzzy suggestions stay
on the treasurer preview unless you add `--fuzzy`.

## 5. Related settings

- **Pledge matching mode** — what happens when a *new* contribution is entered
  (Off / Suggest / Auto). Independent of the cron sweep.
- **Fuzzy name threshold** — how close a cash/envelope payer name must be to
  suggest a match (default `0.84`). Set to `0` to disable fuzzy suggestions.
