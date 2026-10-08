import os
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "posix", "Linux Bash/flock scheduler")
class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "data" / "last-success-epoch"
        self.calls = self.root / "docker-calls"
        scripts = {
            "python3": ('#!/bin/sh\ncat >/dev/null\n'
                        'if [ "$TEST_TIME_FAIL_ALWAYS" = "1" ]; then exit 2; fi\n'
                        'if [ "$TEST_CONFIRM_FAIL" = "1" ]; then\n'
                        '  if [ -f "$TEST_TIME_CALLS" ]; then exit 2; fi\n'
                        '  touch "$TEST_TIME_CALLS"\nfi\n'
                        'printf "%s\\n" "$TEST_NOW"\n'),
            "docker": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\nexit "${TEST_DOCKER_EXIT:-0}"\n',
        }
        for name, text in scripts.items():
            path = self.bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "APP_DIR": str(self.root), "TEST_NOW": "1720000000", "TEST_CALLS": str(self.calls),
                    "DATA_DIR": str(self.root / "data"), "LOG_DIR": str(self.root / "logs"),
                    "STATE_FILE": str(self.state), "LOCK_FILE": str(self.root / "data" / "lock"),
                    "MIN_INTERVAL_SECONDS": "86400", "COMPOSE_SERVICE": "telegram-daily-checkin"}
        self.env["TEST_TIME_CALLS"] = str(self.root / "time-calls")

    def run_guard(self):
        return subprocess.run(["bash", str(Path("scripts/run-daily-if-due.sh").resolve())],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_recent_success_skips_without_starting_docker(self):
        self.state.parent.mkdir()
        self.state.write_text("1719999999\n")
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skip", result.stdout)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.state.read_text(), "1719999999\n")

    def test_due_success_runs_once_and_records_private_state(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["compose run --rm telegram-daily-checkin"])
        self.assertEqual(self.state.read_text(), "1720000000\n")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_failed_checkin_preserves_previous_success_timestamp(self):
        self.state.parent.mkdir()
        self.state.write_text("1719000000\n")
        self.env["TEST_DOCKER_EXIT"] = "1"
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state.read_text(), "1719000000\n")

    def test_success_is_recorded_when_confirmation_time_becomes_unavailable(self):
        self.env["TEST_CONFIRM_FAIL"] = "1"
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), "1720000000\n")
        self.assertEqual(self.calls.read_text().splitlines(), ["compose run --rm telegram-daily-checkin"])

    def test_future_success_timestamp_fails_closed_without_repeating_actions(self):
        self.state.parent.mkdir()
        self.state.write_text("1720000001\n")
        result = self.run_guard()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.state.read_text(), "1720000001\n")

    def test_missing_initial_internet_time_does_not_start_docker(self):
        self.env["TEST_TIME_FAIL_ALWAYS"] = "1"
        result = self.run_guard()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.state.exists())

    def test_exactly_elapsed_interval_is_due(self):
        self.state.parent.mkdir()
        self.state.write_text("1719913600\n")
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls.exists())

    def test_overlapping_run_skips_without_starting_docker(self):
        import fcntl
        lock = Path(self.env["LOCK_FILE"])
        lock.parent.mkdir()
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("another daily check-in run is active", result.stdout)
        self.assertFalse(self.calls.exists())
