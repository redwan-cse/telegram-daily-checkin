import unittest
import urllib.parse
from unittest.mock import patch

import telegram_daily_checkin as app


class ReportTests(unittest.TestCase):
    def deliver(self, text):
        requests = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"ok":true}'
        def transport(request, timeout):
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "https://api.telegram.org/bottest-token/sendMessage")
            self.assertEqual(timeout, 30)
            requests.append(urllib.parse.parse_qs(request.data.decode("utf-8")))
            return Response()
        with patch.object(app.urllib.request, "urlopen", side_effect=transport):
            app.send_telegram_report(app.ReportConfig(True, "test-token", "123", "test"), text)
        return requests

    def test_short_report_preserves_the_existing_single_message(self):
        requests = self.deliver("Daily report\nSuccess: 2\nFailed: 0")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["text"], ["Daily report\nSuccess: 2\nFailed: 0"])
        self.assertEqual(requests[0]["chat_id"], ["123"])

    def test_long_unicode_report_is_complete_and_each_message_fits_telegram(self):
        text = "Daily report\n" + "✅🤖 result: daily action succeeded\n" * 400
        requests = self.deliver(text)
        chunks = [request["text"][0] for request in requests]
        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks))

    def test_disabled_report_never_contacts_the_transport(self):
        with patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network")):
            app.send_telegram_report(app.ReportConfig(False, None, None, "test"), "Daily report")

    def test_report_counts_failures_and_successes(self):
        report = app.format_execution_report("test", "start", "finish", [
            app.BotResult("one", "@BotA", "/daily", True, "sent", "start", "finish"),
            app.BotResult("two", "@BotB", "/daily", False, "missing button", "start", "finish"),
        ])
        self.assertIn("Success: 1\nFailed: 1", report)
        self.assertIn("missing button", report)
