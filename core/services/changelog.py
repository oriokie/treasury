"""Turning the release notes the app already keeps into a changelog.

`core.version.WHATS_NEW` is already a curated, user-facing changelog: one plain
English paragraph per version, written for a treasurer rather than a developer,
and shown in the app under "What's new". It is the right source for a
CHANGELOG.md and for a GitHub release body too — writing those separately would
mean three descriptions of one release, drifting apart from the day they were
written.

Auto patch releases (see ``core.services.auto_release``) add notes in
``core/auto_release_notes.json`` instead of rewriting ``version.py``. Curated
``WHATS_NEW`` text still wins when both exist for the same version.

So nothing here composes prose. It reads those sources, sorts properly (by
version number, which a text sort gets wrong the moment there is a 3.10 and a
3.9), and renders it. The commit list is offered alongside, never instead: a
church treasurer reading a release note should not be handed forty commit
subjects, and a developer looking into a regression should not have to go and
find them.
"""
from core.version import WHATS_NEW, get_version

HEADER = """# Changelog

Every released version, newest first.

Generated from `core.version.WHATS_NEW` by `python manage.py release` — edit the
entry there, not this file, or the next release will overwrite your change.
Auto patch releases may also add notes via `core/auto_release_notes.json`.
"""


def parse_version(v):
    """A version as a tuple, so 3.10.0 sorts after 3.9.0 rather than before it."""
    parts = []
    for p in str(v).lstrip("v").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple((parts + [0, 0, 0])[:3])


def _auto_notes():
    from core.services.auto_release import load_auto_notes
    return load_auto_notes()


def released_versions(newest_first=True):
    """Every version with a written note, in version order."""
    keys = set(WHATS_NEW) | set(_auto_notes())
    return sorted(keys, key=parse_version, reverse=newest_first)


def notes_for(version):
    """The user-facing note for one version, or "" when none was written.

    Curated ``WHATS_NEW`` wins over an auto-generated patch note.
    """
    v = str(version).lstrip("v")
    return (WHATS_NEW.get(v) or _auto_notes().get(v) or "").strip()


def render(versions=None):
    """The whole CHANGELOG.md."""
    out = [HEADER]
    for v in (versions if versions is not None else released_versions()):
        out.append(f"\n## {v}\n\n{notes_for(v)}\n")
    return "".join(out)


def release_body(version, commits=()):
    """The GitHub release body: the same words the app shows, then the commits.

    The note comes first and the commits are folded away behind a summary — the
    people who read a release notification are not the people who read a commit
    log, and the ones who want both should not have to scroll past the wrong one
    to reach the right one.
    """
    version = str(version).lstrip("v")
    note = notes_for(version) or "_No release note was written for this version._"
    body = [note]
    if commits:
        body.append("\n\n<details>\n<summary>Commits in this release</summary>\n")
        body.extend(f"\n- {c}" for c in commits)
        body.append("\n\n</details>\n")
    return "".join(body)


def missing_note():
    """The current VERSION if it has no note written for it, else None.

    A release with no note is not a broken build, but it IS a release nobody can
    read — the app's "What's new" panel goes blank and the GitHub release says
    nothing. Cheap to check at the moment it can still be fixed.
    """
    v = get_version()
    return None if notes_for(v) else v
