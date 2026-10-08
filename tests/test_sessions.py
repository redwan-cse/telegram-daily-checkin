import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

import telegram_daily_checkin as app


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "sessions.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.store = app.EncryptedSessionStore(self.path, self.key)

    def test_account_isolation_updates_and_reopen_preserve_sessions(self):
        self.assertEqual(self.store.get_session("missing"), "")
        self.store.save_session("first", "synthetic-first-session")
        self.store.save_session("second", "synthetic-second-session")
        self.store.save_session("first", "replacement-session")
        reopened = app.EncryptedSessionStore(self.path, self.key)
        self.assertEqual(reopened.get_session("first"), "replacement-session")
        self.assertEqual(reopened.get_session("second"), "synthetic-second-session")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            ciphertext = conn.execute("SELECT encrypted_session FROM account_sessions WHERE account_id='first'").fetchone()[0]
        self.assertNotIn("replacement-session", ciphertext)
        self.assertEqual(Fernet(self.key.encode()).decrypt(ciphertext.encode()), b"replacement-session")

    def test_wrong_key_and_corrupt_tokens_raise_actionable_errors(self):
        self.store.save_session("first", "synthetic-session")
        wrong_key = app.EncryptedSessionStore(self.path, Fernet.generate_key().decode())
        with self.assertRaisesRegex(RuntimeError, "SESSION_ENCRYPTION_KEY"):
            wrong_key.get_session("first")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE account_sessions SET encrypted_session='corrupt'")
        with self.assertRaisesRegex(RuntimeError, "SESSION_ENCRYPTION_KEY"):
            self.store.get_session("first")

    def test_history_retains_success_and_failure_results(self):
        results = [app.BotResult("first", "@BotA", "send_command:/daily", True, "sent", "start", "finish"),
                   app.BotResult("second", "@BotB", "click_button:Daily", False, "missing", "start", "finish")]
        self.store.save_run("start", "finish", results)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            row = conn.execute("SELECT started_at, finished_at, results_json FROM execution_runs").fetchone()
        self.assertEqual(row[:2], ("start", "finish"))
        payload = json.loads(row[2])
        self.assertEqual([(r["account_id"], r["ok"]) for r in payload], [("first", True), ("second", False)])

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_database_is_readable_only_by_its_owner(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
