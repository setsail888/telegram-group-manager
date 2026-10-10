"""Admin callbacks: one fresh security check, no accidental duplicate API calls.

All tests run offline. Live cloud/Telegram RTT is separately diagnostic.
"""
import io
import json
import unittest
from unittest.mock import patch

from app.api import TelegramAPI, TelegramAPIError
from app.service import GroupBot
from app.store import Store
from tests.test_service import FakeTelegramAPI


class CallbackRequestTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "群组")
        self.store.add_filter(-1001, "word", "违规词", "违规词")
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def callback(self, data, *, uid=1):
        self.api.calls.clear()
        self.bot.handle_update({"callback_query": {
            "id": "q-fast",
            "from": {"id": uid},
            "data": data,
            "message": {"chat": {"id": uid, "type": "private"},
                        "message_id": 42},
        }})
        return self.api.calls[:]

    @staticmethod
    def member_checks(calls, *, uid):
        return [(method, opts) for method, opts in calls
                if method == "getChatMember" and opts["user_id"] == uid]

    def test_simple_navigation_requires_only_one_fresh_admin_request(self):
        for data in ("g:-1001", "v:-1001:flood", "v:-1001:words",
                     "v:-1001:floodmedia", "v:-1001:stats",
                     "wl:-1001:0", "v:-1001:shop"):
            with self.subTest(data=data):
                calls = self.callback(data)
                self.assertEqual(len(self.member_checks(calls, uid=1)), 1)
                self.assertTrue(any(m == "editMessageText" for m, _ in calls))

    def test_settings_mutation_one_auth_request_plus_fresh_bot_permissions(self):
        for data in ("n:-1001:flood_limit:+1:floodcount",
                     "fs:-1001:off",
                     "ws:-1001:off",
                     "t:-1001:media_flood_enabled:floodmedia",
                     "fp:-1001:mute",
                     "wp:-1001:mute"):
            with self.subTest(data=data):
                calls = self.callback(data)
                self.assertEqual(len(self.member_checks(calls, uid=1)), 1)
                self.assertTrue(any(m == "answerCallbackQuery" for m, _ in calls))
                self.assertTrue(any(m == "editMessageText" for m, _ in calls))
        # Permission checks for sensitive actions are not removed.
        calls = self.callback("fp:-1001:delete")
        self.assertEqual(len(self.member_checks(calls, uid=1)), 1)

    def test_unauthorized_user_cannot_use_faster_callback(self):
        before = self.store.settings(-1001)["flood_limit"]
        for data in ("g:-1001", "v:-1001:flood", "wl:-1001:0",
                     "n:-1001:flood_limit:+1:floodcount",
                     "fs:-1001:off", "fp:-1001:mute"):
            with self.subTest(data=data):
                calls = self.callback(data, uid=2)
                self.assertEqual(len(self.member_checks(calls, uid=2)), 1)
                self.assertFalse(any(m == "editMessageText" for m, _ in calls))
                self.assertTrue(any(
                    m == "answerCallbackQuery" and p["show_alert"]
                    for m, p in calls))
        self.assertEqual(self.store.settings(-1001)["flood_limit"], before)
        self.assertTrue(self.store.settings(-1001)["anti_flood"])
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")

    def test_direct_page_render_still_performs_fresh_auth(self):
        self.api.calls.clear()
        self.bot._private_view(1, -1001, "flood", 42)
        self.assertEqual(len(self.member_checks(self.api.calls, uid=1)), 1)
        self.api.calls.clear()
        self.bot._private_panel(1, -1001, 42)
        self.assertEqual(len(self.member_checks(self.api.calls, uid=1)), 1)
        self.api.calls.clear()
        self.bot._private_word_list(1, -1001, 0, 42)
        self.assertEqual(len(self.member_checks(self.api.calls, uid=1)), 1)

    def test_direct_page_render_denies_nonadmin(self):
        for render in (
            lambda: self.bot._private_view(2, -1001, "words", 42),
            lambda: self.bot._private_panel(2, -1001, 42),
            lambda: self.bot._private_word_list(2, -1001, 0, 42),
        ):
            with self.subTest(render=render):
                self.api.calls.clear()
                render()
                edits = [p["text"] for m, p in self.api.calls
                         if m == "editMessageText"]
                self.assertTrue(edits and "权限" in edits[0])
                self.assertEqual(len(self.member_checks(self.api.calls, uid=2)), 1)

    def test_optional_audit_logging_does_not_block_page_display(self):
        self.store.set(-1001, "log_chat_id", -900)
        for data in (
            "n:-1001:flood_limit:+1:floodcount",
            "fs:-1001:off",
            "ws:-1001:off",
            "fp:-1001:mute",
            "wn:-1001:10",
        ):
            with self.subTest(data=data):
                calls = self.callback(data)
                rendered = [i for i, (m, _) in enumerate(calls)
                            if m == "editMessageText"]
                audit = [i for i, (m, p) in enumerate(calls)
                         if m == "sendMessage" and p["chat_id"] == -900]
                self.assertTrue(rendered, data)
                self.assertTrue(audit, data)
                self.assertLess(rendered[0], audit[0])
                self.assertEqual(len(self.member_checks(calls, uid=1)), 1)


class TelegramAPILatencyTests(unittest.TestCase):
    @staticmethod
    def urlopen_stub(*_args, **_kwargs):
        return io.BytesIO(json.dumps({
            "ok": True, "result": {"status": "administrator"}
        }).encode("utf-8"))

    def test_interactive_timeout_shorter_and_poll_timeout_unchanged(self):
        api = TelegramAPI("FAKE_PRIVATE_TOKEN")
        with patch("app.api.urllib.request.urlopen", side_effect=self.urlopen_stub) as opened:
            api.call("getChatMember", chat_id=-1001, user_id=1)
            self.assertEqual(opened.call_args.kwargs["timeout"], 15)
            api.call("getUpdates", timeout=25)
            self.assertEqual(opened.call_args.kwargs["timeout"], 35)
            api.call("getUpdates", timeout=40)
            self.assertEqual(opened.call_args.kwargs["timeout"], 50)

    def test_slow_api_log_only_mentions_method_and_time(self):
        api = TelegramAPI("DO_NOT_PRINT_THIS_TOKEN")
        with (patch("app.api.urllib.request.urlopen", side_effect=self.urlopen_stub),
              patch("app.api.time.monotonic", side_effect=[10.0, 14.2]),
              self.assertLogs("app.api", level="WARNING") as captured):
            api.call("getChatMember", chat_id=-1001, user_id=1)
        self.assertIn("getChatMember", captured.output[0])
        self.assertIn("4.20 秒", captured.output[0])
        self.assertNotIn("DO_NOT_PRINT_THIS_TOKEN", captured.output[0])
        self.assertNotIn("-1001", captured.output[0])

    def test_long_poll_is_not_counted_as_slow_api(self):
        api = TelegramAPI("FAKE_PRIVATE_TOKEN")
        with (patch("app.api.urllib.request.urlopen", side_effect=self.urlopen_stub),
              patch("app.api.time.monotonic", side_effect=[10.0, 40.0]),
              self.assertNoLogs("app.api", level="WARNING")):
            api.call("getUpdates", timeout=25)

    def test_not_modified_edit_is_success_no_duplicate_message(self):
        api = TelegramAPI("FAKE_PRIVATE_TOKEN")
        sent = []

        def fake_call(method, **kwargs):
            sent.append((method, kwargs))
            if method == "editMessageText":
                raise TelegramAPIError(
                    method, "Bad Request: message is not modified")
            return {"message_id": 99}

        api.call = fake_call
        self.assertTrue(api.safe(
            "editMessageText", chat_id=1, message_id=5, text="unchanged"))
        store = Store(":memory:")
        try:
            bot = GroupBot(api, store, 999, "MyBot")
            result = bot._edit_or_send(1, 5, "unchanged", [
                {"text": "刷新", "callback_data": "g:-1001"}])
            self.assertTrue(result)
            self.assertFalse(any(m == "sendMessage" for m, _ in sent))
            bot.ocr.shutdown()
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
