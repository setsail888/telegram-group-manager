"""Admin callbacks: one fresh security check, no accidental duplicate API calls.

All tests run offline. Live cloud/Telegram RTT is separately diagnostic.
"""
import os
import threading
import time
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
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"status": "administrator"}}

    class FakeSession:
        trust_env = True

        def __init__(self):
            self.calls = []
            self.closed = False

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return TelegramAPILatencyTests.FakeResponse()

        def close(self):
            self.closed = True

    def test_interactive_timeout_shorter_and_poll_timeout_unchanged(self):
        api = TelegramAPI("FAKE_PRIVATE_TOKEN")
        with patch("app.api.requests.Session", self.FakeSession):
            api.call("getChatMember", chat_id=-1001, user_id=1)
            self.assertEqual(api._session().calls[-1][1]["timeout"], (5, 15))
            api.call("getUpdates", timeout=25)
            self.assertEqual(api._session().calls[-1][1]["timeout"], (5, 35))
            api.call("getUpdates", timeout=40)
            self.assertEqual(api._session().calls[-1][1]["timeout"], (5, 50))
        api.close()

    def test_slow_api_log_only_mentions_method_and_time(self):
        api = TelegramAPI("DO_NOT_PRINT_THIS_TOKEN")
        with (patch("app.api.requests.Session", self.FakeSession),
              patch("app.api.time.monotonic", side_effect=[10.0, 14.2]),
              self.assertLogs("app.api", level="WARNING") as captured):
            api.call("getChatMember", chat_id=-1001, user_id=1)
        self.assertIn("getChatMember", captured.output[0])
        self.assertIn("4.20 秒", captured.output[0])
        self.assertNotIn("DO_NOT_PRINT_THIS_TOKEN", captured.output[0])
        self.assertNotIn("-1001", captured.output[0])
        api.close()

    def test_latency_debug_logs_subsecond_api_without_identifiers(self):
        with patch.dict(os.environ, {"LATENCY_DEBUG": "1"}):
            api = TelegramAPI("DO_NOT_PRINT_THIS_TOKEN")
        with (patch("app.api.requests.Session", self.FakeSession),
              patch("app.api.time.monotonic", side_effect=[10.0, 10.62]),
              self.assertLogs("app.api", level="INFO") as logged):
            api.call("getChatMember", chat_id=-1001, user_id=987654321)
        self.assertIn("getChatMember 耗时 0.62 秒", logged.output[0])
        for secret in ("DO_NOT_PRINT_THIS_TOKEN", "987654321", "-1001"):
            self.assertNotIn(secret, logged.output[0])
        api.close()

    def test_long_poll_is_not_counted_as_slow_api(self):
        api = TelegramAPI("FAKE_PRIVATE_TOKEN")
        with (patch("app.api.requests.Session", self.FakeSession),
              patch("app.api.time.monotonic", side_effect=[10.0, 40.0]),
              self.assertNoLogs("app.api", level="WARNING")):
            api.call("getUpdates", timeout=25)
        api.close()

    def test_https_connection_pool_reuses_main_thread_session(self):
        api = TelegramAPI("TOKEN_HIDDEN")
        with patch("app.api.requests.Session", side_effect=self.FakeSession) as factory:
            api.call("getChatMember", chat_id=-1001, user_id=1)
            session = api._session()
            api.call("answerCallbackQuery", callback_query_id="SAMPLE")
            api.call("editMessageText", chat_id=1, message_id=12, text="ok")
            self.assertEqual(len(session.calls), 3)
            factory.assert_called_once_with()
            self.assertTrue(session.trust_env)  # honors HTTPS proxy settings
            self.assertFalse(session.closed)
        api.close()
        self.assertTrue(session.closed)

    def test_api_failed_network_discards_broken_session_without_retries(self):
        import requests
        api = TelegramAPI("DO_NOT_LEAK_ME")

        class FailSession(self.FakeSession):
            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                raise requests.exceptions.ConnectionError("SECRET_URL")

        with patch("app.api.requests.Session", FailSession):
            with self.assertRaises(TelegramAPIError) as error:
                api.call("banChatMember", chat_id=-100, user_id=2)
            self.assertNotIn("SECRET_URL", str(error.exception))
            self.assertNotIn("DO_NOT_LEAK_ME", str(error.exception))
            # Sensitive mutation must NOT be automatically retried.
            self.assertEqual(api._sessions, set())
        api.close()

    def test_connection_pool_recovers_on_next_call_after_network_error(self):
        import requests
        api = TelegramAPI("HIDDEN_TOKEN")
        instances = []

        class RecoverSession(self.FakeSession):
            def __init__(self):
                super().__init__()
                instances.append(self)

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if len(instances) == 1:
                    raise requests.exceptions.ConnectionError("network down")
                return TelegramAPILatencyTests.FakeResponse()

        with patch("app.api.requests.Session", side_effect=RecoverSession) as factory:
            with self.assertRaises(TelegramAPIError):
                api.call("getChatMember", chat_id=-1001, user_id=1)
            self.assertTrue(instances[0].closed)
            self.assertEqual(len(instances[0].calls), 1)
            self.assertEqual(len(instances), 1)
            self.assertEqual(api.call("getChatMember", chat_id=-1001, user_id=1),
                             {"status": "administrator"})
            self.assertEqual(factory.call_count, 2)
        api.close()
        self.assertTrue(instances[1].closed)

    def test_proxy_aware_pooled_sessions_are_isolated_per_worker_thread(self):
        api = TelegramAPI("HIDDEN_TOKEN")
        with patch("app.api.requests.Session", side_effect=self.FakeSession) as factory:
            api.call("getChatMember", chat_id=-1001, user_id=1)
            main_session = api._session()
            self.assertTrue(api.answer_background("callback-id"))
            api.close()
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(main_session.trust_env)
            self.assertTrue(main_session.closed)

    def test_http_errors_keep_rate_limit_details_without_leaking_url(self):
        api = TelegramAPI("SECRET_HIDDEN_TOKEN")
        class RateLimited(self.FakeSession):
            def post(self, url, **kwargs):
                return type("Response", (), {
                    "status_code": 429,
                    "json": lambda self: {
                        "ok": False, "description": "Too Many Requests",
                        "error_code": 429, "parameters": {"retry_after": 17}}
                })()
        with patch("app.api.requests.Session", RateLimited):
            with self.assertRaises(TelegramAPIError) as caught:
                api.call("sendMessage", chat_id=-1001, text="test")
            self.assertEqual(caught.exception.retry_after, 17)
            self.assertEqual(caught.exception.error_code, 429)
            self.assertNotIn("SECRET_HIDDEN_TOKEN", str(caught.exception))
        api.close()

    def test_http_error_invalid_json_is_sanitized(self):
        api = TelegramAPI("SECRET_HIDDEN_TOKEN")
        class BadBody(self.FakeSession):
            def post(self, url, **kwargs):
                return type("Response", (), {
                    "status_code": 502,
                    "json": lambda self: (_ for _ in ()).throw(
                        ValueError("untrusted URL: SECRET_HIDDEN_TOKEN"))
                })()
        with patch("app.api.requests.Session", BadBody):
            with self.assertRaises(TelegramAPIError) as caught:
                api.call("getChatMember", chat_id=-1001, user_id=1)
            self.assertEqual(caught.exception.error_code, 502)
            self.assertNotIn("SECRET_HIDDEN_TOKEN", str(caught.exception))
        api.close()

    def test_api_refuses_new_requests_after_shutdown(self):
        api = TelegramAPI("TOKEN")
        api.close()
        with self.assertRaises(TelegramAPIError):
            api.call("getMe")
        self.assertFalse(api.answer_background("late-callback"))

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
            result = bot._edit_or_send(1, 5, "unchanged", [[
                {"text": "刷新", "callback_data": "g:-1001"}]])
            self.assertTrue(result)
            self.assertFalse(any(m == "sendMessage" for m, _ in sent))
            bot.ocr.shutdown()
        finally:
            store.close()
            api.close()


class ConcurrentAckTests(unittest.TestCase):
    class NetworkStub(TelegramAPI):
        def __init__(self):
            super().__init__("FAKE_TOKEN")
            self.events = []
            self.lock = threading.Lock()
            self.ack_enter = threading.Event()
            self.edit_enter = threading.Event()
            self.no_alert_acks = 0

        def call(self, method, **params):
            with self.lock:
                self.events.append((method, threading.current_thread().name))
            if method == "getChatMember":
                return {"status": "administrator", "can_delete_messages": True}
            if method == "answerCallbackQuery":
                if not params.get("show_alert"):
                    self.no_alert_acks += 1
                    self.ack_enter.set()
                    # If ACK were synchronous on main thread, this would
                    # have to wait until the edit never begins.
                    if not self.edit_enter.wait(1):
                        raise AssertionError("ACK blocked the UI edit")
                return True
            if method == "editMessageText":
                self.edit_enter.set()
                if not self.ack_enter.wait(1):
                    raise AssertionError("UI edit blocked ACK dispatch")
                return {"message_id": params["message_id"]}
            return True

    def test_acknowledge_and_page_edit_can_overlap(self):
        api = self.NetworkStub()
        store = Store(":memory:")
        try:
            store.ensure_chat(-1001, "群组")
            bot = GroupBot(api, store, 999, "MyBot")
            bot.handle_update({"callback_query": {
                "id": "ack1",
                "data": "v:-1001:flood",
                "from": {"id": 1},
                "message": {"chat": {"id": 1, "type": "private"},
                            "message_id": 99}}})
            api.close()  # waits for the outstanding callback ACK
            self.assertTrue(api.ack_enter.is_set())
            self.assertTrue(api.edit_enter.is_set())
            calls = [m for m, _thread in api.events]
            self.assertEqual(calls.count("getChatMember"), 1)
            self.assertIn("editMessageText", calls)
            self.assertIn("answerCallbackQuery", calls)
            self.assertTrue(any("callback-ack" in thread for m, thread
                                in api.events if m == "answerCallbackQuery"))
            bot.ocr.shutdown()
        finally:
            store.close()

    def test_unauthorized_callback_uses_synchronous_alert_not_async(self):
        api = self.NetworkStub()
        api.call = lambda method, **kwargs: (
            {"status": "member"} if method == "getChatMember" else
            api.events.append((method, threading.current_thread().name)) or True)
        store = Store(":memory:")
        try:
            store.ensure_chat(-1001, "群组")
            bot = GroupBot(api, store, 999, "MyBot")
            bot.handle_update({"callback_query": {
                "id": "q-forbidden", "data": "v:-1001:flood",
                "from": {"id": 2},
                "message": {"chat": {"id": 2, "type": "private"},
                            "message_id": 99}}})
            self.assertTrue(any(m == "answerCallbackQuery"
                                and thread == threading.current_thread().name
                                for m, thread in api.events))
            self.assertFalse(any(m == "editMessageText" for m, _ in api.events))
            bot.ocr.shutdown()
        finally:
            api.close()
            store.close()

    def test_callback_ack_queue_is_bounded_and_shutdown_drains(self):
        api = TelegramAPI("HIDDEN_TOKEN")
        block = threading.Event()
        recorded = []
        lock = threading.Lock()

        def slow_send(method, **kwargs):
            with lock:
                recorded.append((method, kwargs["callback_query_id"]))
            block.wait(2)
            return True

        api.safe = slow_send
        try:
            for index in range(12):
                self.assertTrue(api.answer_background(f"callback{index}"))
            self.assertFalse(api.answer_background("overflow"))
        finally:
            block.set()
            api.close()
        self.assertEqual(len(recorded), 12)
        self.assertFalse(api.answer_background("after-close"))
        self.assertNotIn("overflow", [qid for _method, qid in recorded])


if __name__ == "__main__":
    unittest.main()
