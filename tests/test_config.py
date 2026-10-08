import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from cryptography.fernet import Fernet

import telegram_daily_checkin as app


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "accounts": [{"id": "primary", "api_id": 123, "api_hash": "test-hash", "bots": ["BotA"]}],
            "report": {"enabled": False},
            "proxy": {"enabled": False, "host": "127.0.0.1", "port": 1080},
        }

    def load(self, config=None, **env):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(config or self.config), encoding="utf-8")
            variables = {"CHECKIN_CONFIG_PATH": str(path), "SESSION_ENCRYPTION_KEY": Fernet.generate_key().decode(), **env}
            with patch.dict(os.environ, variables, clear=True), patch.object(app, "_read_env_file"):
                return app.load_settings()

    def test_report_can_be_enabled_from_environment_with_example_yaml_defaults(self):
        settings = self.load(REPORT_ENABLED="true", REPORT_BOT_TOKEN="test-token", REPORT_CHAT_ID="123")
        self.assertTrue(settings.report.enabled)
        self.assertEqual(settings.report.bot_token, "test-token")

    def test_yaml_enabled_report_remains_enabled_with_compose_false_default(self):
        self.config["report"] = {"enabled": True, "bot_token": "test-token", "chat_id": "123"}
        self.assertTrue(self.load(REPORT_ENABLED="false").report.enabled)

    def test_missing_report_environment_placeholders_are_not_credentials(self):
        self.config["report"] = {"enabled": True, "bot_token": "${REPORT_BOT_TOKEN}", "chat_id": "${REPORT_CHAT_ID}"}
        with self.assertRaisesRegex(ValueError, "Report delivery enabled"):
            self.load()

    def test_quoted_false_does_not_enable_report_or_proxy(self):
        self.config["report"]["enabled"] = "false"
        self.config["proxy"]["enabled"] = "false"
        settings = self.load()
        self.assertFalse(settings.report.enabled)
        self.assertFalse(settings.proxy.enabled)

    def test_proxy_environment_settings_work_with_example_yaml_defaults(self):
        settings = self.load(PROXY_ENABLED="true", PROXY_HOST="proxy.example.com", PROXY_PORT="9000")
        self.assertTrue(settings.proxy.enabled)
        self.assertEqual((settings.proxy.host, settings.proxy.port), ("proxy.example.com", 9000))

    def test_yaml_proxy_remains_enabled_with_false_environment_flag(self):
        self.config["proxy"]["enabled"] = True
        self.assertTrue(self.load(PROXY_ENABLED="false").proxy.enabled)

    def test_example_environment_proxy_defaults_do_not_redirect_a_yaml_proxy(self):
        self.config["proxy"] = {"enabled": True, "host": "remote.proxy.example", "port": 9001}
        settings = self.load(PROXY_ENABLED="false", PROXY_HOST="127.0.0.1", PROXY_PORT="1080")
        self.assertEqual((settings.proxy.host, settings.proxy.port), ("remote.proxy.example", 9001))

    def test_duplicate_ids_cannot_share_an_encrypted_account_session(self):
        self.config["accounts"].append(copy.deepcopy(self.config["accounts"][0]))
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate account"):
            self.load()

    def test_blank_bot_and_bare_at_sign_are_rejected_before_network_calls(self):
        for bot in ["", "   ", "@"]:
            with self.subTest(bot=bot):
                self.config["accounts"][0]["bots"] = [bot]
                with self.assertRaises(ValueError):
                    self.load()

    def test_negative_button_wait_is_rejected(self):
        self.config["accounts"][0]["bots"] = [{"username": "BotA", "actions": [{"type": "click_button", "value": "Daily", "wait_seconds": -1}]}]
        with self.assertRaisesRegex(ValueError, "non-negative"):
            self.load()

    def test_all_existing_bot_shorthands_and_action_aliases_remain_supported(self):
        self.config["accounts"][0]["bots"] = [
            "BotA", {"username": "@BotB", "command": "/daily"},
            {"username": "BotC", "command": "Daily"},
            {"username": "BotD", "actions": [{"type": "click_button", "button_text": "Daily", "wait_seconds": 0}]},
        ]
        bots = self.load().accounts[0].bots
        self.assertEqual([bot.username for bot in bots], ["@BotA", "@BotB", "@BotC", "@BotD"])
        self.assertEqual([(bot.actions[0].type, bot.actions[0].value) for bot in bots],
                         [("send_command", "/checkin"), ("send_command", "/daily"), ("send_message", "Daily"), ("click_button", "Daily")])

    def test_invalid_delay_window_is_rejected(self):
        self.config["delays"] = {"initial_min_seconds": 10, "initial_max_seconds": 1}
        with self.assertRaisesRegex(ValueError, "minimum"):
            self.load()

    def test_checked_in_example_loads_with_synthetic_credentials(self):
        config = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        settings = self.load(config, TELEGRAM_API_ID="123", TELEGRAM_API_HASH="test-hash")
        self.assertEqual(len(settings.accounts), 1)
        self.assertEqual([action.type for bot in settings.accounts[0].bots for action in bot.actions],
                         ["send_command", "send_message", "send_command", "click_button", "manual_ui_required"])
