"""Auto-bump and tag a patch release when main moves without a VERSION bump.

Cutting a release used to mean remembering ``python manage.py release --push``
after the merge. That step was skipped for 3.48.0: VERSION was on main, the
GitHub tag was not, and every hosted instance kept reporting "already on the
latest" against v3.47.0. This module is what the auto-release workflow calls so
that cannot happen again.

Policy, deliberately narrow:
- Only patch bumps (3.48.0 → 3.48.1). Minor/major still need a human note in
  ``WHATS_NEW`` and a deliberate VERSION edit.
- Only on ``main`` (the workflow enforces that). Feature branches are not
  releases.
- If VERSION was already raised above the newest tag, just tag it — do not bump
  again.
- Notes for auto patches live in ``core/auto_release_notes.json`` so a machine
  rewrite of ``core/version.py`` is never required. Curated ``WHATS_NEW`` text
  still wins when both exist.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.services import changelog as cl
from core.version import VERSION_FILE, get_version

ROOT = VERSION_FILE.parent
AUTO_NOTES_FILE = ROOT / "core" / "auto_release_notes.json"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"
COMMIT_MARK = "[auto-release]"


def _git(*args, check=True):
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout.strip()


def load_auto_notes():
    try:
        data = json.loads(AUTO_NOTES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_auto_notes(notes):
    AUTO_NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTO_NOTES_FILE.write_text(
        json.dumps(notes, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bump_patch(version):
    major, minor, patch = cl.parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


def latest_tag():
    tags = [t for t in _git("tag", "-l", "v*").splitlines() if t.strip()]
    return max(tags, key=cl.parse_version) if tags else ""


def commits_since(tag):
    rng = f"{tag}..HEAD" if tag else "HEAD"
    log = _git("log", "--no-merges", "--pretty=format:%s", rng, check=False)
    return [line for line in log.splitlines() if line.strip()]


def head_is_tagged(tag):
    if not tag:
        return False
    pointed = _git("rev-list", "-n1", tag, check=False)
    head = _git("rev-parse", "HEAD")
    return bool(pointed) and pointed == head


def note_from_commits(commits):
    """A short treasurer-facing sentence from the commit list."""
    if not commits:
        return "Automated patch release."
    # One line when there is one change; otherwise a short list.
    if len(commits) == 1:
        return f"Automated patch release: {commits[0].rstrip('.')}."
    bullets = "; ".join(c.rstrip(".") for c in commits[:8])
    extra = f" (+{len(commits) - 8} more)" if len(commits) > 8 else ""
    return f"Automated patch release covering: {bullets}{extra}."


def write_version(version):
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")
    get_version.cache_clear()


def ensure_auto_note(version, commits):
    """Record a note for an auto patch when no curated WHATS_NEW exists."""
    version = str(version).lstrip("v")
    if cl.notes_for(version):
        return False
    notes = load_auto_notes()
    notes[version] = note_from_commits(commits)
    save_auto_notes(notes)
    return True


def refresh_changelog():
    CHANGELOG_FILE.write_text(cl.render(), encoding="utf-8")


def prepare_release():
    """Decide what to do on this HEAD. Returns a plan dict for the workflow.

    ``action`` is one of:
    - ``noop`` — HEAD is already the tagged release for VERSION
    - ``tag`` — VERSION is ahead of the newest tag; tag HEAD as-is
    - ``bump`` — VERSION matches the newest tag; bump patch, commit, then tag
    """
    get_version.cache_clear()
    version = get_version()
    tag = f"v{version}"
    latest = latest_tag()
    commits = commits_since(latest)

    if head_is_tagged(tag):
        return {"action": "noop", "version": version, "tag": tag,
                "latest": latest, "commits": commits}

    if latest and cl.parse_version(version) < cl.parse_version(latest):
        raise RuntimeError(
            f"VERSION is {version} but {latest} is already released. "
            f"Bump VERSION before releasing.")

    if latest and cl.parse_version(version) == cl.parse_version(latest):
        if not commits:
            return {"action": "noop", "version": version, "tag": tag,
                    "latest": latest, "commits": commits}
        new = bump_patch(version)
        return {"action": "bump", "version": new, "tag": f"v{new}",
                "previous": version, "latest": latest, "commits": commits}

    # VERSION already ahead of the newest tag (human bumped it).
    return {"action": "tag", "version": version, "tag": tag,
            "latest": latest, "commits": commits}


def apply_bump(plan):
    """Write VERSION + auto note + changelog for a bump plan. Does not commit."""
    if plan["action"] != "bump":
        raise ValueError("apply_bump only accepts a bump plan")
    write_version(plan["version"])
    ensure_auto_note(plan["version"], plan["commits"])
    refresh_changelog()
    return plan


def apply_tag_note(plan):
    """Ensure a note exists when tagging a human-bumped VERSION."""
    if plan["action"] != "tag":
        raise ValueError("apply_tag_note only accepts a tag plan")
    ensure_auto_note(plan["version"], plan["commits"])
    refresh_changelog()
    return plan
