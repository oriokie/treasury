"""Cut a release: check, write the changelog, tag, push.

The pieces already existed and were never joined up. `VERSION` holds the number,
`WHATS_NEW` holds the note, and `core.services.updates` asks GitHub for the
newest tag so a hosted instance can tell a treasurer an update is waiting — but
the repository had NO tags at all, so that check has never once found anything
and the "update available" banner has never fired. This is the missing step.

Deliberately not automatic on a push to main. A release says "this is fit to
run in a church's accounts", and that is a judgement someone makes, not a
consequence of merging. What is automated is everything after the judgement:
the checks that catch the release being malformed, the changelog, the tag, and
(through .github/workflows/release.yml) the GitHub Release itself.
"""
import subprocess

from django.core.management.base import BaseCommand, CommandError

from core.services import changelog as cl
from core.version import VERSION_FILE, get_version


def _git(*args, check=True):
    out = subprocess.run(["git", *args], capture_output=True, text=True,
                         cwd=str(VERSION_FILE.parent))
    if check and out.returncode != 0:
        raise CommandError(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout.strip()


def _git_ok(*args):
    """Whether the command succeeded, for the questions whose answer IS the
    exit code."""
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(VERSION_FILE.parent)).returncode == 0


def _remote_refs(pattern):
    """Ask origin what it has, WITHOUT writing anything locally.

    `git fetch` was the obvious way to compare against origin and the wrong one:
    it creates local refs as a side effect, so merely *checking* whether a
    release was possible could bring the very tag being checked for into the
    repository. `ls-remote` answers the same question and changes nothing —
    which is what `--check` promises.

    A network failure returns nothing rather than raising: not being able to
    reach GitHub should not stop someone tagging a release locally.
    """
    out = _git("ls-remote", "origin", pattern, check=False)
    refs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sha, ref = parts
            refs[ref.removesuffix("^{}")] = sha
    return refs


CHANGELOG_FILE = VERSION_FILE.parent / "CHANGELOG.md"


class Command(BaseCommand):
    help = ("Tag the current VERSION as a release, refreshing CHANGELOG.md from "
            "core.version.WHATS_NEW. Run --check first; nothing is pushed "
            "without --push.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Report whether this version is ready to release and stop.")
        parser.add_argument(
            "--push", action="store_true",
            help="Push the tag (and the changelog commit) to origin. Without "
                 "this the release stays local and can be undone with "
                 "`git tag -d`.")
        parser.add_argument(
            "--allow-dirty", action="store_true",
            help="Tag even with uncommitted changes. Off by default: a tag "
                 "should name a commit that IS what was tested.")

    # -- the checks ---------------------------------------------------------

    def _problems(self, version, allow_dirty, remote=True):
        """Everything wrong with releasing right now, in one pass.

        All of them, not the first — someone about to tag wants the whole list
        so they can fix it in one go, rather than discovering the next problem
        each time they re-run.

        `remote=False` skips the questions that need origin, for tests and for
        working offline.
        """
        problems = []
        tag = f"v{version}"
        remote_tags = _remote_refs("refs/tags/*") if remote else {}

        if not cl.notes_for(version):
            problems.append(
                f"No entry in core.version.WHATS_NEW for {version}. That text is "
                f"what the app's \"What's new\" panel shows and what the GitHub "
                f"release will say — without it both are blank.")

        if _git("tag", "-l", tag):
            problems.append(
                f"{tag} already exists locally. Bump VERSION, or delete the tag "
                f"with `git tag -d {tag}` if it was never pushed.")
        elif f"refs/tags/{tag}" in remote_tags:
            # Worth its own message: the tag is not here, so `git tag -d` would
            # report nothing to delete and leave someone puzzled.
            problems.append(
                f"{tag} is already released on origin, though not in this "
                f"checkout. Bump VERSION — re-tagging would change what every "
                f"hosted instance believes that version is.")

        if not allow_dirty and _git("status", "--porcelain"):
            problems.append(
                "The working tree has uncommitted changes. A tag should name a "
                "commit that is exactly what was tested — commit or stash first, "
                "or pass --allow-dirty if you are certain.")

        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            problems.append(
                f"On branch {branch}, not main. Releases are cut from main so "
                f"the tag is reachable from what everyone else has.")

        # Behind origin is worth catching here rather than at push time, when
        # the tag already exists locally and has to be unpicked. Asked without
        # fetching: if origin's main is not an ancestor of HEAD — including the
        # case where we do not have that commit at all — we are behind it.
        if remote:
            remote_main = _remote_refs("refs/heads/main").get("refs/heads/main")
            if remote_main and not _git_ok("merge-base", "--is-ancestor",
                                           remote_main, "HEAD"):
                problems.append(
                    f"origin/main is at {remote_main[:8]}, which is not in this "
                    f"checkout. Pull first, or the tag will miss those commits.")

        latest = self._latest_tag(remote_tags)
        if latest and cl.parse_version(latest) >= cl.parse_version(version):
            problems.append(
                f"VERSION is {version} but {latest} is already released. "
                f"Bump VERSION before tagging.")
        return problems

    def _latest_tag(self, remote_tags=None):
        """The newest released version, counting origin's tags as well as ours.

        A fresh clone, or CI, may have no tags locally while origin has every
        release — trusting only the local list there would report the newest
        release as "the start" and happily re-tag a version already published.
        """
        tags = [t for t in _git("tag", "-l", "v*").splitlines() if t.strip()]
        tags += [r.removeprefix("refs/tags/")
                 for r in (remote_tags or {}) if r.startswith("refs/tags/v")]
        return max(tags, key=cl.parse_version) if tags else ""

    def _commits_since(self, previous_tag):
        rng = f"{previous_tag}..HEAD" if previous_tag else "HEAD"
        log = _git("log", "--no-merges", "--pretty=format:%s", rng, check=False)
        return [line for line in log.splitlines() if line.strip()]

    # -- doing it -----------------------------------------------------------

    def handle(self, *args, **opts):
        version = get_version()
        tag = f"v{version}"
        remote_tags = _remote_refs("refs/tags/*")
        problems = self._problems(version, opts["allow_dirty"])

        if opts["check"]:
            if problems:
                self.stdout.write(self.style.ERROR(
                    f"{tag} is not ready to release:"))
                for p in problems:
                    self.stdout.write(f"  - {p}")
                raise SystemExit(1)
            previous = self._latest_tag(remote_tags)
            n = len(self._commits_since(previous))
            self.stdout.write(self.style.SUCCESS(
                f"{tag} is ready: {n} commit(s) since {previous or 'the start'}."))
            return

        if problems:
            raise CommandError(
                f"{tag} is not ready to release:\n  - " + "\n  - ".join(problems))

        previous = self._latest_tag(remote_tags)
        commits = self._commits_since(previous)

        # The changelog is rendered from WHATS_NEW every time rather than
        # appended to, so a note edited after the fact is corrected here too
        # instead of leaving the file and the app disagreeing.
        wrote = self._write_changelog()
        if wrote:
            _git("add", str(CHANGELOG_FILE))
            _git("commit", "-m", f"Changelog for {tag}")
            self.stdout.write(f"CHANGELOG.md updated and committed.")

        # The tag message is the note alone. The commit list belongs in the
        # GitHub release, where it can be folded away; in `git show v3.39.0` it
        # would just repeat the log the reader is already standing in.
        _git("tag", "-a", tag, "-m", cl.notes_for(version))
        self.stdout.write(self.style.SUCCESS(
            f"Tagged {tag} ({len(commits)} commit(s) since {previous or 'the start'})."))

        if not opts["push"]:
            self.stdout.write(
                f"Not pushed. When you are ready:\n"
                f"  git push origin main && git push origin {tag}\n"
                f"Pushing the tag is what builds the GitHub Release.")
            return

        _git("push", "origin", "main")
        _git("push", "origin", tag)
        self.stdout.write(self.style.SUCCESS(
            f"Pushed {tag}. The release workflow will publish it on GitHub, and "
            f"hosted instances will see the update within ten minutes."))

    def _write_changelog(self):
        new = cl.render()
        try:
            if CHANGELOG_FILE.read_text(encoding="utf-8") == new:
                return False
        except OSError:
            pass
        CHANGELOG_FILE.write_text(new, encoding="utf-8")
        return True
