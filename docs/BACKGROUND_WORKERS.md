# Background workers — use cases and how they'd run

**Status: review document. No code has been written for any of this.**

Written to answer three questions: what work actually wants to run on its own,
whether it can run under gunicorn, and whether starting it can be automated.

---

## 1. What you already have

This is not a green field. Three of the pieces are in place.

**A worker pattern, already proven.** `benevolent_automation` is a management
command written to be run by cron: idempotent, safe to re-run after a failure,
and carrying a `--dry-run` that rolls back rather than taking a separate preview
path. `backup_db` goes further and prints its own cron line in its docstring.
Whatever gets built should look like these, because they already work.

**An in-process background thread, also already proven.** The Telegram poller
runs as a daemon thread started from `CoreConfig.ready()`
(`core/services/telegram_poller.py`, `core/apps.py`). It starts when gunicorn
starts, guards itself against being launched during `migrate`/`test`/`shell`,
and starts once per process. This is the direct precedent for "can it start with
the gunicorn command" — the answer is yes, and the app does it today.

**Most of the jobs themselves.** Nearly every candidate below is *already* a
pure service function with tests. What is missing is not the work — it is a
trigger. `reallocate_pending()` is the clearest case: it exists, it is tested,
and the only thing that ever calls it is a button on the review queue.

---

## 2. The use cases — for you to check

Ordered by how much they'd earn their keep. "Idempotent" means running it twice
in a row causes no harm, which decides how safely it can be automated.

| # | Job | What exists now | Trigger today | Wants | Idempotent | Notes |
|---|---|---|---|---|---|---|
| 1 | **Auto-match contributions** | `giving.services.allocation.reallocate_pending()` | A button on the review queue only | Every 10–15 min, or after each statement import | **Yes** — re-running finds nothing new to match | The one you named. Highest value: money sits unallocated only until the next run instead of until someone remembers to press the button |
| 1b | **Pledge auto-match** | `manage.py pledge_auto_match` → `pledges.services.matching` | Treasurer preview on `/pledges/auto-match/` | Every 30 min once enabled in Settings | **Yes** — re-running finds nothing new | Exact member/name matches only by default; `--fuzzy` for cash near-misses. Gated by **Settings → Pledges → Allow scheduled auto-match** |
| 2 | **Nightly encrypted backup** | `backup_db` command, complete | Nothing — must be run by hand | Nightly, off-peak | Yes (rotates copies) | Already written, already documented with a cron line. This is the highest-value *unstarted* job and needs no new code at all |
| 3 | **Benevolent standing / arrears** | `benevolent_automation` command | Nothing | Nightly | Yes — writes only a cache of a pure function | Also already written. Recommendation #56d wants the reminders that follow it |
| 4 | **Scheduled reports** | `reports.services.scheduling.run_due_schedules()` | Nothing calls it on a timer | Hourly | Yes, if it marks runs done | Recommendation #39. Snapshot retention/pruning belongs with it |
| 5 | **Bulk campaign SMS** | `giving.services.campaign_sms`, `CampaignMessage` with RUNNING/DONE/INTERRUPTED state | Synchronous, inside the request | Queue + worker | **No** — every send costs money and cannot be recalled | Recommendation #127c-ii. `CampaignMessage` was deliberately shaped to be resumable. **The one job that must never run twice** |
| 6 | **Pledge / arrears reminders** | `pledges.services.reminders` | Manual | Daily | **No** — sends messages | Same hazard as #5, smaller blast radius |
| 7 | **Large statement / envelope imports** | `statements.services.importer` | Synchronous, inside the request | Queue + worker | Depends on the importer's dedup | Recommendation #5. Only matters for multi-year backfills; a normal monthly statement is fine as it is |
| 8 | **Monthly depreciation** | `run_depreciation` command | Manual | Monthly | Yes (posting is idempotent) | Low urgency — it is a deliberate month-end act and arguably *should* stay manual |

**My read:** #1, #2 and #3 are the ones worth doing, and #2 and #3 need no code
whatsoever — only a cron line. #5 and #6 are worth doing but are the ones to be
careful with, because "ran twice" means "sent twice" and a member gets the same
SMS at 2am two nights running.

---

## 3. Can they run with gunicorn?

Gunicorn runs a web server. It answers HTTP requests and does not have a
scheduler, so nothing here happens "because gunicorn is running" unless
something explicitly starts it. Three ways to arrange that.

### Option A — cron calls a management command *(recommended)*

```
*/15 * * * *  cd /path/to/treasury && .venv/bin/python manage.py match_contributions
*/30 * * * *  cd /path/to/treasury && .venv/bin/python manage.py pledge_auto_match
30  2 * * *   cd /path/to/treasury && .venv/bin/python manage.py backup_db --keep 30
0   3 * * *   cd /path/to/treasury && .venv/bin/python manage.py benevolent_automation
```

Entirely independent of gunicorn: the web app can restart, reload or crash and
the schedule is unaffected. Each run is a fresh process, so a job that hangs or
leaks cannot degrade the site. It is also what the two existing commands were
written for, and cPanel exposes cron in its own UI.

The cost: a separate thing to set up, and jobs finer-grained than one minute
aren't possible.

### Option B — a scheduler thread inside gunicorn

Start a loop from `CoreConfig.ready()` exactly as the Telegram poller does. It
then genuinely "starts with the gunicorn command" — nothing else to configure.

Three caveats, and they are not small:

1. **It multiplies with workers.** `gunicorn.conf.py` sets `workers = 1` today,
   so one process means one scheduler. But that same file says to raise
   `workers` when moving to PostgreSQL — and on that day every job starts
   running *N times concurrently*. For auto-match that means two processes
   racing to allocate the same transaction; for campaign SMS it means every
   member gets the message twice. Guarding it needs a database lock
   (`select_for_update` on a job-lock row), which is a real piece of work and
   exactly the TOCTOU shape recommendation #3 already records as unaudited.
2. **SIGHUP kills it mid-job.** The in-app updater reloads by sending SIGHUP
   (see the long comment in `gunicorn.conf.py`). Workers are retired as they
   finish their *requests* — a background thread is not a request, so a job
   halfway through a batch of SMS is simply gone. #5's `INTERRUPTED` state
   exists because this has already been thought about.
3. **A long job competes with page loads.** With `threads = 4`, one thread on a
   ten-minute import is a quarter of the site's capacity.

### Option C — a separate worker process

```
web:    gunicorn -c gunicorn.conf.py config.wsgi
worker: .venv/bin/python manage.py run_jobs
```

The clean answer at scale: isolated, restartable, no interference with request
handling. But the `Procfile` needs something that reads it (Honcho, systemd, a
process manager), and on shared cPanel hosting there is often no supervisor to
keep a second long-running process alive. This is where Celery/RQ would
normally go, and recommendation #5 records that as a deliberate architectural
decision not yet taken.

### Recommendation

**Option A for everything, at least to begin with**, with one refinement: a
single `run_jobs` command taking `--only` so cron has one entry point rather
than eight, and every job records when it last ran so the Settings page can show
"auto-match: last ran 11 minutes ago" — the thing that is genuinely hard to
diagnose with cron is silence.

Use Option B *only* for auto-match if you want near-real-time allocation and
accept the `workers = 1` constraint being written down as a hard requirement
rather than a default.

Do not put #5 or #6 in-process under any option until there is a job lock.

---

## 4. Can the setup be automated?

Partly, and it is worth being precise about which part.

**Yes:** installing the cron entries. A `manage.py install_cron` command could
write the crontab lines (via `crontab -l`/`crontab -` or a file in
`/etc/cron.d/`), with `--dry-run` printing what it would install. On cPanel the
same lines can be pasted into the Cron Jobs UI. A one-time installer that is
idempotent and reversible is a small, self-contained piece of work.

**Yes:** the app can *verify* its own schedule. A settings panel reading each
job's last-run timestamp and flagging "expected every 15 minutes, last ran 6
hours ago" turns a silent cron failure into something visible. Without this, a
broken schedule looks exactly like a quiet week.

**No:** the app cannot reliably install a system service (systemd unit,
supervisor config) for itself — that needs root, which the app does not and
should not have. On cPanel it is moot anyway; there is usually no systemd
available to the account.

**Caution:** whatever installs cron must know the absolute path to
`.venv/bin/python` and the project directory, and must set the environment the
app needs (the database credentials, `TREASURY_ENCRYPTION_KEY`). Cron does not
inherit the shell profile, and the single most common failure here is a job that
runs, fails to read its environment, and logs nothing.

---

## 5. What I'd suggest doing first

1. **Add the two cron lines that need no code** — `backup_db` and
   `benevolent_automation`. Both are written and tested; they are simply not
   scheduled. The backup one in particular is protecting nothing at present.
2. **Write `match_contributions`** wrapping `reallocate_pending()`, with
   `--dry-run`, and schedule it every 15 minutes. Small, idempotent, immediately
   useful.
3. **Pledge auto-match is ready:** enable **Allow scheduled auto-match** under
   Settings → Pledges, then add:

   ```
   */30 * * * *  cd /path/to/treasury && .venv/bin/python manage.py pledge_auto_match
   ```

   Try `python manage.py pledge_auto_match --dry-run` first. Use `--force` to
   bypass the settings toggle while testing. Exact matches only unless you pass
   `--fuzzy`.
4. **Add last-run tracking and a Settings panel**, before adding any further
   jobs, so the schedule is observable.
5. **Then** decide about #5/#6, which need a job lock and a queue, and are the
   ones where getting it wrong costs money.

Steps 1–3 are hours, not days. Step 5 is the one that deserves its own
design.
