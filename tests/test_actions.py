import tempfile
import unittest
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

import telegram_daily_checkin as app


class BotMessage:
    """Telegram boundary double: unmatched text returns None, like Telethon."""
    def __init__(self, texts):
        self.buttons = [[SimpleNamespace(text=text) for text in texts]] if texts else None
        self.clicked = []

    async def click(self, *, text):
        if self.buttons and any(button.text == text for row in self.buttons for button in row):
            self.clicked.append(text)
            return SimpleNamespace()
        return None


class TelegramBoundary:
    def __init__(self, messages=(), authorized=True, two_factor=False):
        self.messages = list(messages)
        self.authorized = authorized
        self.two_factor = two_factor
        self.sent = []
        self.code_phone = None
        self.connected = False
        session = StringSession()
        session.set_dc(2, "149.154.167.51", 443)
        session.auth_key = AuthKey(bytes([1]) * 256)
        self.saved_session = session.save()
        self.session = SimpleNamespace(save=lambda: self.saved_session)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def __aenter__(self):
        await self.connect()
        # Telethon's context manager calls start() and prompts before our login flow.
        if not self.authorized:
            input("Default Telethon phone prompt: ")
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.code_phone = phone

    async def sign_in(self, *, phone=None, code=None, password=None):
        if self.two_factor and password is None:
            raise SessionPasswordNeededError(request=None)
        self.authorized = True

    async def get_messages(self, entity, *, limit):
        return self.messages[:limit]

    async def get_entity(self, username):
        return username

    async def send_message(self, entity, text):
        self.sent.append((entity, text))


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_search_continues_past_a_keyboard_without_the_requested_text(self):
        newest, older = BotMessage(["Other"]), BotMessage(["Daily"])
        await app.click_latest_button_by_text(TelegramBoundary([newest, older]), "bot", "Daily")
        self.assertEqual(newest.clicked, [])
        self.assertEqual(older.clicked, ["Daily"])

    async def test_absent_button_is_failure_even_when_telethon_returns_none(self):
        with self.assertRaisesRegex(RuntimeError, "not found"):
            await app.click_latest_button_by_text(TelegramBoundary([BotMessage(["Other"])]), "bot", "Daily")

    async def test_actions_keep_order_and_missing_button_does_not_block_the_next_bot(self):
        client = TelegramBoundary([BotMessage(["Other"])])
        with self.assertLogs(level="ERROR"):
            failed = await app.send_checkin_to_bot(client, "primary", app.BotCheckin("@BotA", (
                app.BotAction("send_command", "/start"), app.BotAction("click_button", "Daily", 0))))
        succeeded = await app.send_checkin_to_bot(client, "primary", app.BotCheckin("@BotB", (
            app.BotAction("send_message", "Daily"),)))
        self.assertFalse(failed.ok)
        self.assertTrue(succeeded.ok)
        self.assertEqual(client.sent, [("@BotA", "/start"), ("@BotB", "Daily")])

    async def test_manual_ui_requirement_and_flood_wait_remain_failures(self):
        with self.assertLogs(level="ERROR"):
            manual = await app.send_checkin_to_bot(TelegramBoundary(), "primary", app.BotCheckin("@BotA", (
                app.BotAction("manual_ui_required", "Native click"),)))
        self.assertFalse(manual.ok)
        class LimitedClient(TelegramBoundary):
            async def get_entity(self, username):
                raise FloodWaitError(request=None, capture=30)
        with self.assertLogs(level="WARNING"):
            limited = await app.send_checkin_to_bot(LimitedClient(), "primary", app.BotCheckin("@BotA", (
                app.BotAction("send_command", "/daily"),)))
        self.assertFalse(limited.ok)
        self.assertEqual(limited.message, "flood_wait_30s")

    def account_settings(self, directory):
        account = app.AccountConfig("primary", "+123456789", 123, "test-hash", (
            app.BotCheckin("@BotA", (app.BotAction("send_command", "/daily"),)),))
        settings = app.Settings(Path("config.yaml"), Path(directory) / "sessions.sqlite3", Fernet.generate_key().decode(),
                                (account,), app.ProxyConfig(False, "127.0.0.1", 1080, None, None),
                                app.DelayConfig(0, 0, 0, 0, 0, 0), app.ReportConfig(False, None, None, "test"), None)
        return account, settings

    async def test_first_login_uses_configured_phone_and_supports_two_factor(self):
        with tempfile.TemporaryDirectory() as directory:
            account, settings = self.account_settings(directory)
            store = app.EncryptedSessionStore(settings.session_db_path, settings.encryption_key)
            client = TelegramBoundary(authorized=False, two_factor=True)
            def login_code(prompt):
                if "login code" not in prompt:
                    raise AssertionError("Unexpected phone prompt despite configured phone")
                return "12345"
            with patch.object(app, "TelegramClient", return_value=client), patch("builtins.input", side_effect=login_code), \
                    patch.object(app.getpass, "getpass", return_value="synthetic-password"):
                results = await app.run_account(account, settings, store)
            self.assertEqual(client.code_phone, "+123456789")
            self.assertTrue(results[0].ok)
            self.assertEqual(client.sent, [("@BotA", "/daily")])
            self.assertEqual(store.get_session("primary"), client.saved_session)
            self.assertFalse(client.connected)

    async def test_authorized_session_needs_no_prompt_and_disconnects_after_login_error(self):
        with tempfile.TemporaryDirectory() as directory:
            account, settings = self.account_settings(directory)
            store = app.EncryptedSessionStore(settings.session_db_path, settings.encryption_key)
            client = TelegramBoundary()
            with patch.object(app, "TelegramClient", return_value=client), patch("builtins.input", side_effect=AssertionError("Unexpected login")):
                self.assertTrue((await app.run_account(account, settings, store))[0].ok)
            self.assertFalse(client.connected)
            client = TelegramBoundary(authorized=False)
            class LoginError(Exception):
                pass
            with patch.object(app, "TelegramClient", return_value=client), patch("builtins.input", side_effect=LoginError):
                with self.assertRaises(LoginError):
                    await app.run_account(account, settings, store)
            self.assertFalse(client.connected)

    async def test_account_failure_does_not_block_next_account_and_history_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            account, settings = self.account_settings(directory)
            settings = replace(settings, accounts=(account, replace(account, account_id="secondary")))
            store = app.EncryptedSessionStore(settings.session_db_path, settings.encryption_key)
            store.save_session("primary", "synthetic-session")
            with closing(sqlite3.connect(settings.session_db_path)) as conn, conn:
                conn.execute("UPDATE account_sessions SET encrypted_session='corrupt' WHERE account_id='primary'")
            client = TelegramBoundary()
            with patch.object(app, "TelegramClient", return_value=client), self.assertLogs(level="ERROR"):
                exit_code = await app.run_checkins(settings)
            self.assertEqual(exit_code, 1)
            self.assertEqual(client.sent, [("@BotA", "/daily")])
            with closing(sqlite3.connect(settings.session_db_path)) as conn:
                results = json.loads(conn.execute("SELECT results_json FROM execution_runs").fetchone()[0])
            self.assertEqual([(r["account_id"], r["ok"]) for r in results], [("primary", False), ("secondary", True)])

    async def test_report_transport_failure_preserves_success_exit_code_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            _, settings = self.account_settings(directory)
            settings = replace(settings, report=app.ReportConfig(True, "test-token", "123", "test"))
            with patch.object(app, "TelegramClient", return_value=TelegramBoundary()), \
                    patch.object(app.urllib.request, "urlopen", side_effect=OSError("synthetic network failure")), \
                    self.assertLogs(level="ERROR"):
                exit_code = await app.run_checkins(settings)
            self.assertEqual(exit_code, 0)
            with closing(sqlite3.connect(settings.session_db_path)) as conn:
                results = json.loads(conn.execute("SELECT results_json FROM execution_runs").fetchone()[0])
            self.assertEqual([(r["account_id"], r["ok"]) for r in results], [("primary", True)])
