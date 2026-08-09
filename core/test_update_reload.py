"""What the update page is allowed to claim about restarting the app.

The in-app updater pulled the code, ran the migrations, touched config/wsgi.py
and told the treasurer "✓ Signalled the app to restart" followed by "Update
complete. The app will reload momentarily." None of that last part happened.
gunicorn only watches files when its `reload` option is on — `reload_extra_files`
merely extends it, and the watcher is built inside `if self.cfg.reload:` — and
`reload` is a development setting that was never enabled here. So the touch
changed an mtime nobody was reading, the workers carried on serving the version
from before the update (there is no max_requests either, so they are long
lived), and the only thing that would have told anyone said the opposite.

That is the worst shape a bug can take in this app: not a broken feature, but a
screen a treasurer reads to decide whether their finance system is now running
the new code, giving them the wrong answer.

Two invariants are pinned here.

  1. The log never claims a restart that this process cannot perform. Where the
     app can genuinely reload itself it says what it is doing; where it cannot
     it says so in plain words and gives the command.
  2. Where it CAN reload, it really signals — SIGHUP to the gunicorn master,
     the conventional graceful reload, and not a file touch.
  3. The outcome is machine-readable, so the banner cannot contradict the log.
     Fixing the log alone left the page still printing "Update complete. The
     app is reloading" over a run whose own log said "STILL SERVING THE OLD
     VERSION": the JSON the page polls carried ok=True and nothing else, and
     ok=True only ever meant the code reached the disk. The banner now branches
     on state["reload"] — the restart actually achieved — and that key is the
     one thing here a treasurer's decision hangs on, so it is asserted for
     every path out of a successful run.
"""
import copy
import json
import os
import signal
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from core.services import updates


def _no_server_env():
    """An environment with no web server markers at all — the situation of a
    plain `manage.py` process, and the one the test runner is in."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SERVER_SOFTWARE", "RUN_MAIN")}
    return mock.patch.dict(os.environ, env, clear=True)


class _Isolated(SimpleTestCase):
    """Keeps the module's global run state and handoff file out of the way."""

    def setUp(self):
        before = copy.deepcopy(updates._update_state)
        self.addCleanup(lambda: updates._update_state.update(before))
        # reload is cleared here too, or a test that leaves "gunicorn" behind
        # lends its restart to the next one — and the tests below exist
        # precisely to catch a run being credited with a restart it never got.
        updates._update_state.update(running=False, finished=False, ok=None,
                                     reload=None, log=[], started_at=None,
                                     finished_at=None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_file = Path(tmp.name) / "last-update.json"
        patch = mock.patch.object(updates, "_STATE_FILE", self.state_file)
        patch.start()
        self.addCleanup(patch.stop)


class ReloadTargetTests(_Isolated):
    def test_nothing_identifiable_means_a_human_has_to_restart_it(self):
        with _no_server_env():
            self.assertEqual(updates._reload_target(), ("manual", None))

    def test_a_gunicorn_worker_targets_its_own_master(self):
        with _no_server_env(), \
                mock.patch.dict(os.environ, {"SERVER_SOFTWARE": "gunicorn/23.0.0"}), \
                mock.patch("os.getppid", return_value=4242), \
                mock.patch("pathlib.Path.read_bytes",
                           return_value=b"/opt/treasury/.venv/bin/gunicorn\0-c\0"):
            self.assertEqual(updates._reload_target(), ("gunicorn", 4242))

    def test_an_orphaned_worker_signals_nobody(self):
        """getppid() is 1 once the parent has gone, and the only thing worse
        than failing to reload is sending SIGHUP to init."""
        with _no_server_env(), \
                mock.patch.dict(os.environ, {"SERVER_SOFTWARE": "gunicorn/23.0.0"}), \
                mock.patch("os.getppid", return_value=1):
            self.assertEqual(updates._reload_target(), ("manual", None))

    def test_a_parent_that_is_not_gunicorn_is_left_alone(self):
        """Where /proc can be read, the inherited variable is not taken on
        trust — some other process may have exported it."""
        with _no_server_env(), \
                mock.patch.dict(os.environ, {"SERVER_SOFTWARE": "gunicorn/23.0.0"}), \
                mock.patch("os.getppid", return_value=4242), \
                mock.patch("pathlib.Path.read_bytes", return_value=b"/bin/bash\0"):
            self.assertEqual(updates._reload_target(), ("manual", None))

    def test_the_development_server_is_recognised(self):
        with _no_server_env(), mock.patch.dict(os.environ, {"RUN_MAIN": "true"}):
            self.assertEqual(updates._reload_target(), ("runserver", None))


class WhatTheTreasurerIsToldTests(_Isolated):
    def test_when_nothing_can_be_restarted_it_says_so_and_how(self):
        said = " ".join(updates._reload_notes("manual", None))
        self.assertIn("STILL SERVING THE OLD VERSION", said)
        self.assertIn("systemctl restart treasury", said)

    def test_it_never_promises_a_reload_it_cannot_perform(self):
        said = " ".join(updates._reload_notes("manual", None)).lower()
        for lie in ("signalled the app to restart",
                    "the app will reload momentarily",
                    "the app is restarting"):
            self.assertNotIn(lie, said)

    def test_the_gunicorn_note_names_the_mechanism_and_the_process(self):
        said = " ".join(updates._reload_notes("gunicorn", 4242))
        self.assertIn("SIGHUP", said)
        self.assertIn("4242", said)


class SignallingTests(_Isolated):
    def test_a_gunicorn_reload_is_a_real_signal_to_the_master(self):
        with mock.patch("os.kill") as killed:
            updates._apply_reload("gunicorn", 4242)
        killed.assert_called_once_with(4242, signal.SIGHUP)

    def test_the_manual_case_touches_nothing(self):
        """The old code touched config/wsgi.py here and called that a restart.
        Under gunicorn it was a no-op, so it must not be what we fall back to."""
        with mock.patch("os.kill") as killed:
            updates._apply_reload("manual", None)
        killed.assert_not_called()

    def test_a_signal_that_fails_corrects_the_log_rather_than_leaving_it(self):
        """We announce the reload before sending it, because the reload can end
        this thread mid-sentence. If the signal is refused we are still here,
        and the promise above has to be taken back."""
        updates._log("→ Asking the web server to reload …")
        with mock.patch("os.kill", side_effect=PermissionError("not permitted")):
            updates._apply_reload("gunicorn", 4242)
        said = " ".join(updates._update_state["log"])
        self.assertIn("still running the old version", said)
        self.assertIn("systemctl restart treasury", said)


class TheWholeRunTests(_Isolated):
    """Drive _do_update end to end with the shell steps stubbed out, which is
    the only level at which the old behaviour and the new one can be compared:
    both versions of this function produce a log, and it is the log the
    treasurer reads."""

    def _run(self):
        with _no_server_env(), \
                mock.patch.object(updates, "_backup_db"), \
                mock.patch.object(updates, "_run_step"):
            updates._do_update()
        return " ".join(updates._update_state["log"])

    def test_it_reports_that_the_app_is_still_on_the_old_code(self):
        said = self._run()
        self.assertTrue(updates._update_state["ok"])
        self.assertIn("STILL SERVING THE OLD VERSION", said)
        self.assertIn("systemctl restart treasury", said)

    def test_it_does_not_announce_a_restart_that_did_not_happen(self):
        said = self._run().lower()
        self.assertNotIn("signalled the app to restart", said)
        self.assertNotIn("will reload momentarily", said)

    def test_a_finished_run_is_closed_off_properly(self):
        self._run()
        self.assertFalse(updates._update_state["running"])
        self.assertTrue(updates._update_state["finished"])


class WhatTheStatusPayloadAdmitsTests(_Isolated):
    """The half of the fix the log cannot do.

    UpdateStatusView serialises update_status() straight to JSON and the page
    branches on it, so whatever this dict says is what the treasurer is told.
    "ok" answers "did the update run without errors"; it has never answered
    "is this server on the new code", which is the only question being asked
    by the person watching. That second answer is state["reload"].
    """

    def _run_with(self, target, kill=None):
        with _no_server_env(), \
                mock.patch.object(updates, "_backup_db"), \
                mock.patch.object(updates, "_run_step"), \
                mock.patch.object(updates, "_reload_target", return_value=target), \
                mock.patch("pathlib.Path.touch"), \
                mock.patch("os.kill", side_effect=kill):
            updates._do_update()

    def test_a_run_that_could_restart_nothing_admits_it(self):
        """The case that made this necessary: a good update on a host with no
        gunicorn master to signal. ok is true, and the app is still old."""
        self._run_with(("manual", None))
        self.assertTrue(updates._update_state["ok"])
        self.assertFalse(updates._update_state["reload"],
                         "a run nobody could restart must not look live")
        self.assertFalse(updates.update_status()["reload"])

    def test_a_signalled_run_says_which_restart_it_got(self):
        self._run_with(("gunicorn", 4242))
        self.assertEqual(updates._update_state["reload"], "gunicorn")
        self.assertEqual(updates.update_status()["reload"], "gunicorn")

    def test_the_development_server_counts_as_a_real_reload(self):
        """Touching config/wsgi.py under `runserver` genuinely does restart the
        process — that branch is not the pretend one."""
        self._run_with(("runserver", None))
        self.assertEqual(updates._update_state["reload"], "runserver")

    def test_a_refused_signal_is_taken_back_in_the_state_and_on_disk(self):
        """The reload is recorded before the kill, because a kill that lands
        may end this thread mid-line. When it is refused instead, correcting
        only the log leaves the banner still saying the app is restarting."""
        self._run_with(("gunicorn", 4242),
                       kill=PermissionError("not permitted"))
        self.assertTrue(updates._update_state["ok"])
        self.assertFalse(updates._update_state["reload"])
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertFalse(saved["reload"],
                         "the process that replaces us reads the file, not the "
                         "log, and would otherwise inherit the retracted claim")

    def test_a_failed_update_reloads_nothing(self):
        with _no_server_env(), \
                mock.patch.object(updates, "_backup_db"), \
                mock.patch.object(updates, "_run_step",
                                  side_effect=RuntimeError("git pull failed")):
            updates._do_update()
        self.assertFalse(updates._update_state["ok"])
        self.assertFalse(updates._update_state["reload"])

    def test_the_handoff_carries_the_reload_across_the_restart_it_caused(self):
        self._run_with(("gunicorn", 4242))
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["reload"], "gunicorn")

    def test_a_new_run_does_not_inherit_the_last_one_s_restart(self):
        """Two updates in the life of one worker: the second is started by a
        button press, and if it ends up unable to signal anything it must not
        be reported live on the strength of the first."""
        updates._update_state.update(finished=True, ok=True, reload="gunicorn")
        with mock.patch.object(updates.threading, "Thread") as thread:
            self.assertTrue(updates.start_update())
        thread.assert_called_once()
        self.assertIsNone(updates._update_state["reload"])


class WhatTheProgressPageShowsTests(SimpleTestCase):
    """The banner itself. The log has been honest since the last fix; this is
    the sentence in the coloured box that people actually read."""

    def _source(self):
        from django.template.loader import get_template
        return get_template("update_run.html").template.source

    def test_the_success_banner_branches_on_the_recorded_reload(self):
        src = self._source()
        self.assertIn("s.reload", src,
                      "the success banner must ask whether a restart happened, "
                      "not assume it from s.ok")

    def test_the_page_names_the_command_when_the_app_is_not_live(self):
        src = self._source()
        self.assertIn("NOT live", src)
        self.assertIn("systemctl restart treasury", src)
        self.assertIn("touch tmp/restart.txt", src)


class SurvivingOurOwnRestartTests(_Isolated):
    """A successful update ends by restarting the workers, which throws away
    the in-memory log the progress page is polling. Without a handoff the
    treasurer watches the log they are reading empty itself and the page go
    quiet, with nothing saying whether the upgrade worked."""

    def _write(self, finished_at, log=("done",)):
        self.state_file.write_text(json.dumps({
            "running": False, "finished": True, "ok": True, "log": list(log),
            "started_at": None, "finished_at": finished_at.isoformat()}),
            encoding="utf-8")

    def test_a_fresh_process_reports_the_run_that_restarted_it(self):
        import datetime as dt
        self._write(dt.datetime.now(), ["Update complete."])
        status = updates.update_status()
        self.assertTrue(status["finished"])
        self.assertEqual(status["log"], ["Update complete."])

    def test_a_stale_result_does_not_haunt_the_page(self):
        """Otherwise the update screen shows last month's log for ever instead
        of the button that starts the next update."""
        import datetime as dt
        self._write(dt.datetime.now() - dt.timedelta(seconds=updates._STATE_FILE_TTL + 60))
        self.assertFalse(updates.update_status()["finished"])

    def test_a_run_in_this_process_wins_over_the_file(self):
        import datetime as dt
        self._write(dt.datetime.now(), ["from the previous process"])
        updates._update_state.update(running=True,
                                     started_at=dt.datetime.now().isoformat())
        updates._log("in progress here")
        status = updates.update_status()
        self.assertTrue(status["running"])
        self.assertNotIn("from the previous process", status["log"])

    def test_a_finished_run_leaves_its_outcome_behind(self):
        updates._log("Update complete.")
        updates._finish(ok=True)
        saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(saved["ok"])
        self.assertIn("Update complete.", " ".join(saved["log"]))

    def test_the_outcome_is_on_disk_before_the_signal_that_ends_us(self):
        """Ordering, not just presence — and it is the whole handoff.

        SIGHUP to the master retires this worker, so the moment the signal goes
        out this thread may not run again. If the outcome were written after it,
        the write would be a race the treasurer loses: the log they are watching
        vanishes with the worker and the fresh one has nothing to show them. So
        the assertion here is made from inside the kill itself.
        """
        seen = {}

        def record(pid, sig):
            seen["file_existed"] = self.state_file.exists()
            seen["log"] = json.loads(
                self.state_file.read_text(encoding="utf-8"))["log"]

        with _no_server_env(), \
                mock.patch.object(updates, "_backup_db"), \
                mock.patch.object(updates, "_run_step"), \
                mock.patch.object(updates, "_reload_target",
                                  return_value=("gunicorn", 4242)), \
                mock.patch("os.kill", side_effect=record) as killed:
            updates._do_update()

        killed.assert_called_once_with(4242, signal.SIGHUP)
        self.assertTrue(seen["file_existed"],
                        "the worker was signalled before its outcome was saved")
        self.assertIn("SIGHUP", " ".join(seen["log"]),
                      "the saved log must already carry the reload note, since "
                      "this thread may never get to append anything after it")
