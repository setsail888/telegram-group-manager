"""Keyword panel controls, one-shot return navigation and opt-in enforcement."""
import os
import tempfile
import time
import unittest

from app.service import GroupBot
from app.store import Store
from tests.test_service import FakeTelegramAPI


class WordMenuTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一个群")
        self.bot = GroupBot(self.api, self.store, bot_id=999, username="MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def cb(self, data, user=1, message_id=56):
        return {"callback_query": {
            "id": "q5", "data": data, "from": {"id": user},
            "message": {"chat": {"id": user, "type": "private"},
                        "message_id": message_id}}}

    def private(self, text, uid=1, message_id=60):
        return {"message": {"chat": {"id": uid, "type": "private"},
                            "from": {"id": uid, "first_name": "管理员"},
                            "message_id": message_id, "text": text}}

    def group(self, text, uid=2, message_id=101, chat_id=-1001):
        return {"message": {
            "chat": {"id": chat_id, "type": "supergroup", "title": "测试群"},
            "from": {"id": uid, "first_name": "成员"},
            "message_id": message_id, "text": text}}

    def last_edited(self):
        return next(kwargs for method, kwargs in reversed(self.api.calls)
                    if method == "editMessageText")

    def last_sent(self):
        return next(kwargs for method, kwargs in reversed(self.api.calls)
                    if method == "sendMessage")

    def deletions(self):
        return [kwargs for method, kwargs in self.api.calls
                if method == "deleteMessage"]

    def buttons(self, kwargs):
        return [button for row in kwargs["reply_markup"]["inline_keyboard"]
                for button in row]

    def test_compact_word_panel_has_reference_layout_and_loose_switch(self):
        self.store.add_filter(-1001, "word", "加微信联系", "加微信联系")
        self.bot.handle_update(self.cb("v:-1001:words"))
        page = self.last_edited()
        self.assertIn("状态：✅ 启用", page["text"])
        self.assertIn("惩罚：仅删除消息", page["text"])
        self.assertIn("删除提醒：关闭（静默）", page["text"])
        self.assertIn("规则：1 条", page["text"])
        self.assertNotIn("加微信联系", page["text"])
        buttons = {b["callback_data"]: b["text"] for b in self.buttons(page)}
        for data in ("ws:-1001:on", "ws:-1001:off",
                     "w:-1001:add", "w:-1001:del",
                     "wl:-1001:0", "v:-1001:wordpenalty",
                     "v:-1001:wordnotice",
                     "t:-1001:word_loose_match:words",
                     "g:-1001"):
            self.assertIn(data, buttons)
            self.assertLessEqual(len(data.encode()), 64)
        self.assertIn("拆字违禁词",
                      buttons["t:-1001:word_loose_match:words"])

    def test_one_shot_success_messages_have_both_working_return_buttons(self):
        for operation, content, success in (
            ("add", "加微信联系,刷单返佣", "已导入"),
            ("test", "加微信联系", "测试结果"),
            ("except", "加微信联系 | 学校提醒加微信联系", "已更新"),
            ("unexcept", "加微信联系 | 学校提醒加微信联系", "已更新"),
            ("del", "刷单返佣", "已删除"),
        ):
            with self.subTest(operation=operation):
                self.bot.handle_update(self.cb(f"w:-1001:{operation}"))
                prompt = self.last_sent()
                self.assertEqual(prompt["chat_id"], 1)
                prompt_buttons = [b["callback_data"] for b in self.buttons(prompt)]
                self.assertEqual(prompt_buttons,
                                 ["v:-1001:words", "g:-1001"])
                self.bot.handle_update(self.private(content))
                reply = self.last_sent()
                self.assertIn(success, reply["text"])
                nav = [b["callback_data"] for b in self.buttons(reply)]
                self.assertEqual(nav, ["v:-1001:words", "g:-1001"])
                self.bot.handle_update(self.cb(nav[0]))
                self.assertIn("⛔ 违禁词", self.last_edited()["text"])
                self.bot.handle_update(self.cb(nav[1]))
                self.assertIn("设置「测试群」", self.last_edited()["text"])

    def test_one_shot_cancel_failure_and_timeout_keep_navigation(self):
        self.bot.handle_update(self.cb("w:-1001:add"))
        self.bot.handle_update(self.private("/cancel"))
        self.assertEqual(len(self.buttons(self.last_sent())), 2)
        self.bot.handle_update(self.cb("w:-1001:add"))
        self.bot.handle_update(self.private("x"))
        self.assertIn("⚠️", self.last_sent()["text"])
        self.assertEqual(len(self.buttons(self.last_sent())), 2)
        self.bot.handle_update(self.cb("w:-1001:add"))
        self.bot._word_wizards[1] = (-1001, "add", time.monotonic() - 1)
        self.bot.handle_update(self.private("应当过期的关键词"))
        self.assertIn("过期", self.last_sent()["text"])
        self.assertEqual(len(self.buttons(self.last_sent())), 2)
        self.assertEqual(self.store.filter_count(-1001, "word"), 0)

    def test_keyword_status_off_preserves_rules_and_other_link_detection(self):
        self.store.add_filter(-1001, "word", "违禁内容", "违禁内容")
        self.bot.handle_update(self.cb("ws:-1001:off"))
        self.assertFalse(self.store.settings(-1001)["word_filter_enabled"])
        self.bot.handle_update(self.group("违禁内容", message_id=100))
        self.assertEqual(self.deletions(), [])
        self.bot.handle_update(self.group(
            "https://untrusted.example.com", message_id=101))
        self.assertEqual([x["message_id"] for x in self.deletions()], [101])
        self.assertEqual(self.store.filter_count(-1001, "word"), 1)
        self.bot.handle_update(self.cb("ws:-1001:on"))
        self.bot.handle_update(self.group("违禁内容", message_id=102))
        self.assertEqual([x["message_id"] for x in self.deletions()], [101, 102])
        self.assertTrue(self.store.settings(-1001)["word_filter_enabled"])

    def test_word_status_controls_fail_closed_for_nonadmin_or_missing_permission(self):
        self.bot.handle_update(self.cb("ws:-1001:off", user=2))
        self.assertTrue(self.store.settings(-1001)["word_filter_enabled"])
        self.bot.handle_update(self.cb("ws:-1001:off"))
        original = self.api.call

        def missing_delete(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 999:
                return {"status": "administrator", "can_delete_messages": False,
                        "can_restrict_members": True}
            return original(method, **kwargs)

        self.api.call = missing_delete
        self.bot.handle_update(self.cb("ws:-1001:on"))
        self.assertFalse(self.store.settings(-1001)["word_filter_enabled"])
        self.assertTrue(any(
            p.get("show_alert") for m, p in self.api.calls
            if m == "answerCallbackQuery"))
        self.bot.handle_update(self.cb("ws:-1001:bad"))
        self.assertFalse(self.store.settings(-1001)["word_filter_enabled"])

    def test_list_is_separate_paginated_and_private(self):
        for i in range(20):
            self.store.add_filter(-1001, "word", f"词语{i:02d}", f"词语{i:02d}")
        self.bot.handle_update(self.cb("v:-1001:words"))
        self.assertNotIn("词语00", self.last_edited()["text"])
        self.bot.handle_update(self.cb("wl:-1001:0"))
        first = self.last_edited()
        self.assertIn("第 1/3 页", first["text"])
        self.assertIn("词语00", first["text"])
        self.assertNotIn("词语08", first["text"])
        self.assertIn("wl:-1001:1",
                      [b["callback_data"] for b in self.buttons(first)])
        self.bot.handle_update(self.cb("wl:-1001:1"))
        second = self.last_edited()
        self.assertIn("第 2/3 页", second["text"])
        self.assertIn("词语08", second["text"])
        self.assertNotIn("词语16", second["text"])
        self.bot.handle_update(self.cb("wl:-1001:100"))
        self.assertIn("第 3/3 页", self.last_edited()["text"])
        self.bot.handle_update(self.cb("wl:-1001:0", user=2))
        self.assertEqual(self.store.filter_count(-1001, "word"), 20)
        self.assertTrue(any(
            p.get("show_alert") for m, p in self.api.calls
            if m == "answerCallbackQuery"))
        self.assertEqual(self.store.filter_count(-1002, "word"), 0)

    def test_punishment_is_opt_in_and_can_be_switched_back_to_delete_only(self):
        self.store.add_filter(-1001, "word", "违禁广告", "违禁广告")
        self.bot.handle_update(self.group("违禁广告", message_id=110))
        self.assertFalse(any(m == "restrictChatMember" for m, _ in self.api.calls))
        self.bot.handle_update(self.cb("v:-1001:wordpenalty"))
        self.assertIn("禁言时长：10 分钟", self.last_edited()["text"])
        self.bot.handle_update(self.cb("wp:-1001:mute"))
        self.assertEqual(self.store.settings(-1001)["word_penalty"], "mute")
        self.bot.handle_update(self.cb("n:-1001:word_mute_minutes:+5:wordpenalty"))
        self.assertEqual(self.store.settings(-1001)["word_mute_minutes"], 15)
        before = int(time.time())
        self.bot.handle_update(self.group("违禁广告", message_id=111))
        restricts = [p for m, p in self.api.calls if m == "restrictChatMember"]
        self.assertEqual(len(restricts), 1)
        self.assertTrue(before + 14 * 60 < restricts[0]["until_date"]
                        < before + 16 * 60)
        self.bot.handle_update(self.cb("wp:-1001:delete"))
        self.bot.handle_update(self.group("违禁广告", message_id=112))
        self.assertEqual(len(
            [p for m, p in self.api.calls if m == "restrictChatMember"]), 1)

    def test_word_penalty_rejects_unauthorized_and_missing_rights(self):
        self.bot.handle_update(self.cb("wp:-1001:mute", user=2))
        self.assertEqual(self.store.settings(-1001)["word_penalty"], "delete")
        original = self.api.call

        def missing_restrict(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 999:
                return {"status": "administrator", "can_delete_messages": True,
                        "can_restrict_members": False}
            return original(method, **kwargs)

        self.api.call = missing_restrict
        self.bot.handle_update(self.cb("wp:-1001:mute"))
        self.assertEqual(self.store.settings(-1001)["word_penalty"], "delete")
        self.bot.handle_update(self.cb("wp:-1001:ban"))
        self.assertEqual(self.store.settings(-1001)["word_penalty"], "delete")

    def test_notice_is_silent_by_default_and_opt_in_auto_deletes(self):
        self.store.add_filter(-1001, "word", "违禁广告", "违禁广告")
        self.bot.handle_update(self.group("违禁广告", message_id=201))
        self.assertFalse(any(m == "sendMessage" and
                             p.get("chat_id") == -1001
                             for m, p in self.api.calls))
        self.bot.handle_update(self.cb("wn:-1001:10"))
        self.assertEqual(self.store.settings(-1001)["word_notice_seconds"], 10)
        self.bot.handle_update(self.group("违禁广告", message_id=202))
        notices = [p for m, p in self.api.calls
                   if m == "sendMessage" and p.get("chat_id") == -1001]
        self.assertEqual(len(notices), 1)
        self.assertIn("约 10 秒后撤回", notices[0]["text"])
        self.assertTrue(notices[0]["disable_notification"])
        self.assertTrue(self.store.has_pending_notice_deletions())
        # A burst must not generate a reminder for every single message.
        self.bot.handle_update(self.group("违禁广告", message_id=203))
        self.assertEqual(len([p for m, p in self.api.calls
                              if m == "sendMessage" and p.get("chat_id") == -1001]), 1)
        # Advance only stored due date; do not sleep ten seconds in a test.
        self.store.db.execute(
            "UPDATE pending_notice_deletions SET delete_at=?",
            (int(time.time()) - 1,))
        self.bot.cleanup_word_notices()
        self.assertFalse(self.store.has_pending_notice_deletions())
        self.assertIn(self.api.next_message_id,
                      [p["message_id"] for p in self.deletions()])

    def test_notice_state_is_stored_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "groupguard.sqlite3")
            first = Store(path)
            first.ensure_chat(-1001, "测试群")
            first.set(-1001, "word_filter_enabled", False)
            first.set(-1001, "word_penalty", "mute")
            first.set(-1001, "word_notice_seconds", 30)
            first.schedule_notice_deletion(-1001, 456,
                                           int(time.time()) - 1)
            first.close()
            second = Store(path)
            self.assertFalse(second.settings(-1001)["word_filter_enabled"])
            self.assertEqual(second.settings(-1001)["word_penalty"], "mute")
            self.assertEqual(second.settings(-1001)["word_notice_seconds"], 30)
            self.assertEqual(len(second.pending_notice_deletion_due()), 1)
            second.clear_notice_deletion(-1001, 456)
            self.assertFalse(second.has_pending_notice_deletions())
            second.close()

    def test_notice_rejects_injected_ttl_and_requires_delete_rights(self):
        for option in ("9999", "bogus", "15"):
            self.bot.handle_update(self.cb(f"wn:-1001:{option}"))
            self.assertEqual(self.store.settings(-1001)["word_notice_seconds"], 0)
        original = self.api.call

        def no_delete(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 999:
                return {"status": "administrator", "can_delete_messages": False}
            return original(method, **kwargs)

        self.api.call = no_delete
        self.bot.handle_update(self.cb("wn:-1001:10"))
        self.assertEqual(self.store.settings(-1001)["word_notice_seconds"], 0)

    def test_disable_filter_also_skips_ocr_word_submission(self):
        class OCRSpy:
            available = True

            def __init__(self):
                self.submitted = []

            def submit(self, file_id, context):
                self.submitted.append(file_id)
                return True

            def results(self):
                return iter(())

            def shutdown(self):
                pass

        self.bot.ocr.shutdown()
        spy = OCRSpy()
        self.bot.ocr = spy
        self.store.set(-1001, "ocr_mode", "delete")
        self.store.set(-1001, "word_filter_enabled", False)
        msg = self.group("", message_id=350)["message"]
        msg["photo"] = [{"file_id": "telegram-file", "file_unique_id": "uniq"}]
        self.bot._maybe_queue_ocr(msg)
        self.assertEqual(spy.submitted, [])
        self.store.set(-1001, "word_filter_enabled", True)
        self.bot._maybe_queue_ocr(msg)
        self.assertEqual(spy.submitted, ["telegram-file"])

    def test_edited_keyword_message_does_not_auto_mute(self):
        self.store.add_filter(-1001, "word", "违禁广告", "违禁广告")
        self.store.set(-1001, "word_penalty", "mute")
        message = self.group("违禁广告", message_id=370)["message"]
        self.bot.handle_update({"edited_message": message})
        self.assertEqual([p["message_id"] for p in self.deletions()], [370])
        self.assertFalse(any(m == "restrictChatMember" for m, _ in self.api.calls))

    def test_notice_bot_error_retries_then_drops_stale_records(self):
        self.store.schedule_notice_deletion(-1001, 888,
                                           int(time.time()) - 5)
        original = self.api.safe

        def refusal(method, **kw):
            if method == "deleteMessage":
                return None
            return original(method, **kw)

        self.api.safe = refusal
        self.bot.cleanup_word_notices()
        self.assertTrue(self.store.has_pending_notice_deletions())
        self.store.db.execute(
            "UPDATE pending_notice_deletions SET delete_at=?",
            (int(time.time()) - 3601,))
        self.bot.cleanup_word_notices()
        self.assertFalse(self.store.has_pending_notice_deletions())

    def test_polling_timeout_shortens_only_for_pending_reminder(self):
        import app.main as main
        from types import SimpleNamespace

        class StopAPI(FakeTelegramAPI):
            def call(self, method, **kwargs):
                if method == "getMe":
                    return {"id": 999, "username": "MyBot"}
                if method == "getUpdates":
                    self.calls.append((method, kwargs))
                    main._running = False
                    return []
                return super().call(method, **kwargs)

        saved = main._running
        config = SimpleNamespace(owner_ids=frozenset(), poll_timeout=25)
        try:
            for pending, expected in ((False, 25), (True, 3)):
                db = Store(":memory:")
                db.ensure_chat(-1001, "测试群")
                if pending:
                    db.schedule_notice_deletion(
                        -1001, 777, int(time.time()) + 30)
                api = StopAPI()
                main._running = True
                main.run(api, db, config)
                calls = [p for method, p in api.calls if method == "getUpdates"]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["timeout"], expected)
                db.close()
        finally:
            main._running = saved


if __name__ == "__main__":
    unittest.main()
