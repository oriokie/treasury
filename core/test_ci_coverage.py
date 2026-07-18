"""Guard: the CI test shards must cover every app that has tests.

The CI workflow (`.github/workflows/ci.yml`) splits the slow test suite across
parallel shards by naming apps in a matrix. That matrix is a hand-maintained
list — exactly the kind of frozen allowlist that silently rots (see the
engineering review's #74a finding). So this test reads the workflow back and
asserts two things:

  * every local app that actually contains tests is assigned to exactly one
    shard — so a new app can't be added and silently never run in CI;
  * no shard names an app twice or an app that doesn't exist.

If someone adds an app with tests and forgets the shard, THIS test fails — in
CI — naming the missing app. The list can't rot unnoticed.
"""
import pathlib
import re

from django.apps import apps as django_apps
from django.test import SimpleTestCase

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

# Apps that live in this repository (not third-party like django.contrib.*).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _is_local(app_path):
    """True only for apps that live in the project tree — not third-party
    packages. site-packages sits under .venv, which is itself under the repo
    root, so a plain relative_to() check wrongly counts django.contrib.* as
    local; excluding anything under a virtualenv / site-packages fixes that."""
    try:
        rel = app_path.relative_to(_REPO_ROOT)
    except ValueError:
        return False
    parts = set(rel.parts)
    return not (parts & {".venv", "venv", "site-packages", "env"})


def _local_apps_with_tests():
    """Local app labels that contain at least one test module."""
    out = set()
    for cfg in django_apps.get_app_configs():
        app_path = pathlib.Path(cfg.path)
        if not _is_local(app_path):
            continue
        has_tests = (app_path / "tests.py").exists() or any(
            app_path.glob("test_*.py")) or (app_path / "tests").is_dir()
        if has_tests:
            out.add(app_path.name)
    return out


def _sharded_apps():
    """Every app named across all shards in the CI workflow, as a list (so
    duplicates across shards are detectable)."""
    text = WORKFLOW.read_text()
    # matrix entries look like:  apps: giving envelopes core
    return [app
            for line in re.findall(r"^\s*apps:\s*(.+)$", text, re.MULTILINE)
            for app in line.split()]


class CiShardCoverageTests(SimpleTestCase):
    def test_workflow_exists(self):
        self.assertTrue(WORKFLOW.exists(),
                        f"CI workflow not found at {WORKFLOW}")

    def test_every_app_with_tests_is_in_a_shard(self):
        sharded = set(_sharded_apps())
        missing = _local_apps_with_tests() - sharded
        self.assertEqual(
            missing, set(),
            f"These apps have tests but are in no CI shard, so their tests would "
            f"never run in CI: {sorted(missing)}. Add each to a shard in "
            f".github/workflows/ci.yml.")

    def test_no_app_is_sharded_twice(self):
        sharded = _sharded_apps()
        dupes = {a for a in sharded if sharded.count(a) > 1}
        self.assertEqual(dupes, set(),
                         f"Apps named in more than one CI shard (their tests "
                         f"would run twice): {sorted(dupes)}.")

    def test_no_shard_names_a_nonexistent_app(self):
        local = {pathlib.Path(c.path).name
                 for c in django_apps.get_app_configs()}
        unknown = set(_sharded_apps()) - local
        self.assertEqual(unknown, set(),
                         f"CI shards name apps that don't exist: {sorted(unknown)}.")
