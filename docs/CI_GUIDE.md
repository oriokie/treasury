# Continuous Integration — Guide

*What the automated checks are, why they exist, how to read a red build, and how
to keep the pipeline healthy. This addresses recommendation **P0-1** in
`docs/ENGINEERING_REVIEW.md`: the test suite is large, so its green status should
be known automatically on every change, not "I think it passed last time someone
ran it".*

---

## What CI does

Every push and every pull request triggers `.github/workflows/ci.yml`, which runs
three things:

1. **Checks** — a fast gate (under a minute) that:
   - runs `python manage.py check` (does the app boot? are settings/URLs sane?)
   - runs `python manage.py makemigrations --check --dry-run` (do the models and
     the migrations agree? — this catches the single most common
     "worked-on-my-machine, broke-on-deploy" mistake: changing a model without
     making a migration).

2. **Tests** — the full test suite, split into parallel **shards** (see below)
   so it finishes in reasonable wall-clock time. Every shard must pass.

3. **CI Passed** — a single summary status that is green only if *every* job
   above succeeded. Branch protection can require just this one check.

If all three are green, the change is safe to merge on the three things that
matter most: it boots, models and migrations are in sync, and nothing is broken
that the tests cover.

---

## Reading a red build

Open the **Actions** tab on GitHub, click the failed run, and look at which job
is red:

- **Checks → System check failed.** The app doesn't boot. Usually a broken
  import, a bad setting, or a URL/view error. Reproduce locally with
  `python manage.py check`.

- **Checks → Migration drift failed** (`makemigrations --check` reported
  changes). You changed a model but didn't create the migration. Fix:
  ```
  python manage.py makemigrations
  ```
  commit the new migration file, and push. (Never edit an already-applied
  migration; always add a new one.)

- **Tests → some shard failed.** Click the shard to see which test failed and
  its traceback. Reproduce *just that test* locally (much faster than the whole
  suite):
  ```
  python manage.py test benevolent.test_solvency.SolvencyTests.test_fund_depleted
  ```
  Shards run with `fail-fast: false`, so a failure in one shard does **not** hide
  failures in the others — every shard runs to completion so you see all the
  breakage in one go.

---

## Running the same checks locally (before you push)

You do not need to wait for CI. Run the fast gate in seconds:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
```

Run the tests for the area you touched (targeted, not the whole suite):

```bash
python manage.py test benevolent            # one app
python manage.py test benevolent.test_fraud # one module
python manage.py test giving reports --parallel auto   # a couple of apps, all cores
```

Run the whole suite the way CI does (slow — prefer targeted runs while working,
save this for a final check):

```bash
python manage.py test --parallel auto
```

**Tip:** `--parallel auto` uses every CPU core and is dramatically faster than a
serial run. `--noinput` (which CI uses) never stops to ask a question.

---

## How the shards work, and how to change them

The suite is large, so a single job would be slow. The `tests` job uses a
**matrix** of shards — each shard is one parallel CI job that runs a named subset
of apps:

| Shard | Apps |
|---|---|
| `core-giving-envelopes` | giving envelopes core |
| `cashbook` | cashbook |
| `reports-statements` | reports statements |
| `benevolent` | benevolent |
| `the-rest` | accounts assets departments leaders ledger loans members pledges |

They are balanced by rough test volume so they finish at about the same time. To
add or rebalance a shard, edit the `matrix.shard` list in
`.github/workflows/ci.yml` — it is a plain list of `{name, apps}` entries.

### The shards can't silently rot

There is a safety net. `core/test_ci_coverage.py` reads the workflow file back
and **fails the build** if:

- a local app has tests but is in **no** shard (so its tests would never run in
  CI), or
- an app is named in **two** shards (its tests would run twice), or
- a shard names an app that doesn't exist.

So if you add a new app with tests and forget to assign it to a shard, CI goes
red and tells you exactly which app is missing. This is the same principle the
engineering review recommends everywhere: **a hand-maintained list must have a
test that fails when it drifts** — never a bare list that rots unnoticed. (This
guard already earned its keep once: it caught a real gap while the workflow was
being written.)

---

## Environment

- **Python 3.12** — matches the development and production runtime.
- **SQLite** for tests — the default when no `POSTGRES_DB`/`MYSQL_DB` env var is
  set. Tests never touch the production database. (Production runs MariaDB; the
  test suite does not need it, and the app's SQL is ORM-only so it behaves the
  same on both.)
- `DJANGO_DEBUG=1` in CI so the production-only start-up guards (which *raise* if
  the real secret key is missing) don't fire against throwaway CI values.
- `DJANGO_SECRET_KEY` is a fixed CI-only string, never a real secret. **No
  production secret is ever needed by CI** — it neither deploys nor connects to
  any real service.

---

## What CI does *not* do (yet)

By design, this first pipeline is the safety gate only. It does **not**:

- deploy anything (deployment stays manual, as documented in `deploy/`);
- lint or format (no failures gate on style today — a future addition could add
  `ruff`/`flake8` as a separate non-blocking job);
- measure coverage (a natural next step — add `coverage run` around the test
  command and upload the report, especially useful before the god-file refactors
  in the review).

These are deliberate omissions to keep the first CI simple and trustworthy. Add
them incrementally once the gate itself is established and trusted.

---

## Turning it on

CI runs automatically once the workflow file is on the default branch on GitHub —
no extra setup. To make it *enforced* (so nothing merges red):

1. Push the repository (including `.github/workflows/ci.yml`) to GitHub.
2. In the repo: **Settings → Branches → Add branch protection rule** for `main`.
3. Enable **"Require status checks to pass before merging"** and select
   **"CI Passed"**.

From then on, a pull request cannot be merged until the build is green.
