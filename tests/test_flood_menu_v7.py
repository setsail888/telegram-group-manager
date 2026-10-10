"""Nested anti-flood controls and safe opt-in punishment/temporary notices."""
import os
import tempfile
import time
import unittest

from app.service import GroupBot
from app.store import DEFAULTS, Store
from tests.test_service import FakeTelegramAPI


class FloodMenuTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "防刷屏测试群")
        self.store.ensure_chat(-1002, "群乙")
        self.bot = GroupBot(self.api, self.store, bot_id=999, username="MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def cb(self, data, uid=1, mid=88):
        return {"callback_query": {
            "id": "callback", "data": data, "from": {"id": uid},
            "message": {"chat": {"id": uid, "type": "private"}, "message_id": mid}}}

    def msg(self, text, uid=2, mid=101, kind=None):
        obj = {"chat": {"id": -1001, "type": "supergroup",
                         "title": "防刷屏测试群"},
               "from": {"id": uid, "first_name": "测试用户"},
               "text": text, "message_id": mid}
        if kind:
            obj[kind] = {"file_unique_id": f"media{mid}"}
        return {"message": obj}

    def page(self):
        return next(p for method, p in reversed(self.api.calls)
                    if method == "editMessageText")

    def buttons(self):
        return [b for row in self.page()["reply_markup"]["inline_keyboard"]
                for b in row]

    def button_values(self):
        return {b["callback_data"]: b["text"] for b in self.buttons()}

    def deletions(self):
        return [p for m, p in self.api.calls if m == "deleteMessage"]

    def restrictions(self):
        return [p for m, p in self.api.calls if m == "restrictChatMember"]

    def flood(self, uid=2, start=200):
        """Threshold 3: send four distinct messages to trigger one burst."""
        for j in range(4):
            self.bot.handle_update(self.msg(f"正常交流内容{start+j}", uid, start+j))

    def test_main_panel_short_and_nested_like_reference(self):
        self.bot.handle_update(self.cb("v:-1001:flood"))
        view = self.page()
        self.assertIn("💬 反刷屏", view["text"])
        self.assertIn("状态：✅ 启用", view["text"])
        self.assertIn("当前：10 秒 / 超过 7 条", view["text"])
        self.assertIn("惩罚：单次仅删除触发消息", view["text"])
        self.assertIn("删除提醒：关闭（静默）", view["text"])
        self.assertLess(len(view["text"]), 250)
        rows = view["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(rows), 7)  # status, 3 module rows, cross-account, help, back
        self.assertEqual(list(map(len, rows)), [2, 2, 2, 2, 1, 1, 1])
        actual = self.button_values()
        for callback in (
            "fs:-1001:on", "fs:-1001:off",
            "v:-1001:floodcount", "v:-1001:floodinterval",
            "v:-1001:floodrepeat", "v:-1001:floodmedia",
            "v:-1001:floodpenalty", "v:-1001:floodnotice",
            "fh:-1001:flood",
            "g:-1001",
        ):
            self.assertIn(callback, actual)
            self.assertLessEqual(len(callback.encode()), 64)
        self.assertFalse(any(value.startswith("n:-1001:") for value in actual))

    def test_nested_parameter_editing_with_return_and_isolated_group(self):
        cases = (
            ("floodcount", "flood_limit", 1, 7),
            ("floodinterval", "flood_window", 5, 10),
            ("floodrepeat", "repeat_limit", 1, 3),
            ("floodrepeat", "repeat_window", 5, 45),
            ("floodmedia", "media_flood_limit", 1, 5),
            ("floodmedia", "media_flood_window", 5, 20),
        )
        for i, (page, key, delta, original) in enumerate(cases):
            with self.subTest(page=page):
                self.bot.handle_update(self.cb(f"v:-1001:{page}"))
                self.assertIn("⬅️ 返回防刷屏",
                              [b["text"] for b in self.buttons()])
                self.assertIn(
                    f"n:-1001:{key}:+{delta}:{page}", self.button_values())
                self.bot.handle_update(
                    self.cb(f"n:-1001:{key}:+{delta}:{page}"))
                self.assertEqual(self.store.settings(-1001)[key], original + delta)
                self.bot.handle_update(
                    self.cb(f"n:-1001:{key}:-{delta}:{page}"))
                self.assertEqual(self.store.settings(-1001)[key], original)
                self.bot.handle_update(self.cb("v:-1001:flood"))
                self.assertIn("💬 反刷屏", self.page()["text"])
        self.assertEqual(self.store.settings(-1002)["flood_limit"], 7)

    def test_media_page_has_its_own_real_switch_and_numbers(self):
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        self.assertIn("t:-1001:media_flood_enabled:floodmedia",
                      self.button_values())
        self.assertFalse(self.store.settings(-1001)["media_flood_enabled"])
        self.bot.handle_update(
            self.cb("t:-1001:media_flood_enabled:floodmedia"))
        self.assertTrue(self.store.settings(-1001)["media_flood_enabled"])
        self.bot.handle_update(
            self.cb("n:-1001:media_flood_window:+5:floodmedia"))
        self.assertEqual(self.store.settings(-1001)["media_flood_window"], 25)
        self.bot.handle_update(self.cb("v:-1001:flood"))
        self.assertIn("媒体连发：✅ 开启", self.page()["text"])

    def test_media_explanation_describes_actual_threshold_scope_and_consequences(self):
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        initial = self.page()
        text = initial["text"]
        for phrase in (
            "状态：⭕ 未生效（本项关，主防刷屏开）",
            "同一普通成员在 20 秒内发送超过 5 条媒体",
            "从第 6 条起触发清理",
            "照片、视频、动图、贴纸、文件、语音、音频、视频留言",
            "不同类型合计，内容不必相同",
            "纯文字及编辑消息；管理员豁免",
            "中间穿插文字也不会清空该时间窗口内的媒体计数",
            "再发第 6 条贴纸时会删除该贴纸",
            "之前的媒体不会追溯删除",
            "处罚：删除触发消息",
            "提醒：静默清理，不在群内发提示",
            "主防刷屏和本项两个开关都要开启",
            "机器人还需有删除消息权限",
            "纯文字仍可能触发普通刷屏规则",
        ):
            self.assertIn(phrase, text)
        self.assertLess(len(text), 520)
        self.assertIn("t:-1001:media_flood_enabled:floodmedia",
                      self.button_values())
        self.assertIn("n:-1001:media_flood_limit:+1:floodmedia",
                      self.button_values())
        self.assertIn("⬅️ 返回防刷屏",
                      [button["text"] for button in self.buttons()])

    def test_media_explanation_follows_custom_settings_and_parent_master_switch(self):
        self.store.set(-1001, "media_flood_limit", 8)
        self.store.set(-1001, "media_flood_window", 35)
        self.store.set(-1001, "media_flood_enabled", True)
        self.store.set(-1001, "anti_flood", False)
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        text = self.page()["text"]
        self.assertIn("状态：⭕ 未生效（本项开，主防刷屏关）", text)
        self.assertIn("35 秒内发送超过 8 条媒体", text)
        self.assertIn("第 9 条", text)
        self.assertNotIn("20 秒", text)
        self.bot.handle_update(self.cb("fs:-1001:on"))
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        self.assertIn("状态：✅ 生效中（本项开，主防刷屏开）",
                      self.page()["text"])
        self.store.set(-1001, "flood_penalty", "mute")
        self.store.set(-1001, "flood_mute_minutes", 25)
        self.store.set(-1001, "flood_notice_seconds", 10)
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        text = self.page()["text"]
        self.assertIn("处罚：删除并禁言 25 分钟", text)
        self.assertIn("提醒：发送短提示，约 10 秒后撤回", text)

    def test_media_explanation_shows_global_cumulative_mute_warning(self):
        self.store.set(-1001, "auto_mute_enabled", True)
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        self.assertIn("全局累计违规禁言", self.page()["text"])
        self.store.set(-1001, "flood_penalty", "mute")
        self.bot.handle_update(self.cb("v:-1001:floodmedia"))
        self.assertNotIn("全局累计违规禁言", self.page()["text"])

    def test_mixed_media_counts_with_interspersed_text_and_only_deletes_sixth_media(self):
        self.store.set(-1001, "media_flood_enabled", True)
        self.store.set(-1001, "media_flood_limit", 5)
        self.store.set(-1001, "media_flood_window", 20)
        # Disable other flood triggers to isolate the media-specific rule.
        self.store.set(-1001, "flood_limit", 30)
        self.store.set(-1001, "repeat_limit", 10)
        kinds = ["photo", "video", "document", "voice", "animation", "sticker"]
        for index, kind in enumerate(kinds):
            if index in (1, 3):
                self.bot.handle_update(self.msg(
                    f"普通文字穿插 {index}", mid=1500 + index))
            self.bot.handle_update(self.msg(
                f"媒体正文 {index}", mid=1600 + index, kind=kind))
            expected_deleted = [] if index < 5 else [1605]
            self.assertEqual(
                [d["message_id"] for d in self.deletions()], expected_deleted)
        self.assertEqual(self.restrictions(), [])
        # Editing an existing media message does not add to the flood count.
        edited = self.msg("编辑字幕内容", mid=1604, kind="video")["message"]
        self.bot.handle_update({"edited_message": edited})
        self.assertEqual([d["message_id"] for d in self.deletions()], [1605])

    def test_media_limit_needs_both_switches_no_auto_deletion_when_off(self):
        self.store.set(-1001, "flood_limit", 30)
        self.store.set(-1001, "repeat_limit", 10)
        for enabled_media, enabled_parent in ((False, True), (True, False)):
            self.bot.flood.records.clear()
            self.store.set(-1001, "media_flood_enabled", enabled_media)
            self.store.set(-1001, "anti_flood", enabled_parent)
            for index in range(7):
                self.bot.handle_update(self.msg(
                    f"不同媒体 {enabled_media}-{enabled_parent}-{index}",
                    mid=1700 + index, kind="photo"))
            self.assertEqual(self.deletions(), [])

    def test_flood_on_off_works_without_deleting_settings(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "repeat_limit", 10)
        self.bot.handle_update(self.cb("fs:-1001:off"))
        self.assertFalse(self.store.settings(-1001)["anti_flood"])
        self.flood(start=300)
        self.assertEqual(self.deletions(), [])
        self.bot.handle_update(self.cb("fs:-1001:on"))
        self.assertTrue(self.store.settings(-1001)["anti_flood"])
        self.assertEqual(self.store.settings(-1001)["flood_limit"], 3)
        self.flood(start=400)
        self.assertEqual([p["message_id"] for p in self.deletions()], [403])

    def test_default_delete_only_and_no_notice(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "repeat_limit", 10)
        self.flood(start=450)
        self.assertEqual([p["message_id"] for p in self.deletions()], [453])
        self.assertEqual(self.restrictions(), [])
        self.assertFalse(any(m == "sendMessage" and p.get("chat_id") == -1001
                             for m, p in self.api.calls))
        self.assertFalse(self.store.has_pending_notice_deletions())

    def test_flood_penalty_and_notification_are_opt_in(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "repeat_limit", 10)
        self.bot.handle_update(self.cb("v:-1001:floodpenalty"))
        self.assertIn("flood_mute_minutes", " ".join(self.button_values()))
        self.bot.handle_update(self.cb("fp:-1001:mute"))
        self.bot.handle_update(self.cb(
            "n:-1001:flood_mute_minutes:+5:floodpenalty"))
        self.assertEqual(self.store.settings(-1001)["flood_mute_minutes"], 15)
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "mute")
        self.bot.handle_update(self.cb("fn:-1001:10"))
        self.assertEqual(self.store.settings(-1001)["flood_notice_seconds"], 10)
        self.flood(start=500)
        self.assertEqual(len(self.restrictions()), 1)
        until = self.restrictions()[0]["until_date"]
        self.assertTrue(int(time.time())+13*60 < until <
                        int(time.time())+16*60)
        notices = [p for m, p in self.api.calls
                   if m == "sendMessage" and p.get("chat_id") == -1001]
        self.assertEqual(len(notices), 1)
        self.assertIn("防刷屏违规消息", notices[0]["text"])
        self.assertTrue(self.store.has_pending_notice_deletions())
        self.store.db.execute(
            "UPDATE pending_notice_deletions SET delete_at=?",
            (int(time.time()) - 1,))
        self.bot.cleanup_word_notices()
        self.assertFalse(self.store.has_pending_notice_deletions())

    def test_mute_uses_successful_deletions_and_skips_global_double_mute(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "repeat_limit", 10)
        self.store.set(-1001, "flood_penalty", "mute")
        self.store.set(-1001, "auto_mute_enabled", True)
        self.store.set(-1001, "auto_mute_after", 2)
        self.store.record_moderation(-1001, 2, 50, "其他违规")
        self.flood(start=600)
        self.assertEqual(len(self.restrictions()), 1)
        self.assertFalse(any("10分钟内违规" in p.get("text", "")
                             for m, p in self.api.calls if m == "sendMessage"))

        original = self.api.safe
        def fail_delete(method, **kwargs):
            if method == "deleteMessage":
                return None
            return original(method, **kwargs)
        self.api.safe = fail_delete
        self.flood(uid=3, start=700)
        self.assertEqual(len(self.restrictions()), 1)

    def test_invalid_callbacks_nonadmin_or_permission_failure_cannot_mutate(self):
        for cb in ("fs:-1001:bad", "fp:-1001:ban", "fn:-1001:999",
                   "n:-1001:flood_mute_minutes:+99:floodpenalty"):
            self.bot.handle_update(self.cb(cb))
        self.bot.handle_update(self.cb("fp:-1001:mute", uid=2))
        self.bot.handle_update(self.cb("fn:-1001:10", uid=2))
        self.bot.handle_update(self.cb("fs:-1001:off", uid=2))
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")
        self.assertEqual(self.store.settings(-1001)["flood_notice_seconds"], 0)
        self.assertTrue(self.store.settings(-1001)["anti_flood"])
        original = self.api.call
        def no_restrict(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 999:
                return {"status":"administrator","can_delete_messages":True,
                        "can_restrict_members":False}
            return original(method, **kwargs)
        self.api.call = no_restrict
        self.bot.handle_update(self.cb("fp:-1001:mute"))
        self.assertEqual(self.store.settings(-1001)["flood_penalty"], "delete")

    def test_bad_permissions_fail_closed_for_enabling_and_reminders(self):
        self.bot.handle_update(self.cb("fs:-1001:off"))
        original = self.api.call
        def no_delete(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 999:
                return {"status":"administrator","can_delete_messages":False,
                        "can_restrict_members":True}
            return original(method, **kwargs)
        self.api.call = no_delete
        self.bot.handle_update(self.cb("fs:-1001:on"))
        self.bot.handle_update(self.cb("fn:-1001:10"))
        self.assertFalse(self.store.settings(-1001)["anti_flood"])
        self.assertEqual(self.store.settings(-1001)["flood_notice_seconds"], 0)
        alerts = [p for m, p in self.api.calls if m == "answerCallbackQuery"
                  and p.get("show_alert")]
        self.assertGreaterEqual(len(alerts), 2)

    def test_global_cumulative_mute_warning_is_explicit_in_panel(self):
        self.store.set(-1001, "auto_mute_enabled", True)
        self.bot.handle_update(self.cb("v:-1001:flood"))
        self.assertIn("全局「累计违规禁言」已启用", self.page()["text"])
        self.bot.handle_update(self.cb("v:-1001:floodpenalty"))
        self.assertIn("全局累计违规禁言已开启", self.page()["text"])
        self.bot.handle_update(self.cb("fp:-1001:mute"))
        self.bot.handle_update(self.cb("v:-1001:flood"))
        self.assertNotIn("全局「累计违规禁言」已启用", self.page()["text"])

    def test_existing_docker_sqlite_data_retained_after_settings_added(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "store.sqlite3")
            old = Store(path)
            old.ensure_chat(-1001, "长期使用的群")
            old.add_filter(-1001, "word", "已有敏感词", "已有敏感词")
            old.set(-1001, "flood_limit", 6)
            old.close()
            new = Store(path)
            self.assertEqual(new.settings(-1001)["flood_limit"], 6)
            self.assertEqual(new.settings(-1001)["flood_penalty"], "delete")
            self.assertEqual(new.settings(-1001)["flood_notice_seconds"], 0)
            self.assertEqual(new.filter_count(-1001, "word"), 1)
            new.set(-1001, "flood_penalty", "mute")
            new.set(-1001, "flood_notice_seconds", 30)
            new.close()
            verify = Store(path)
            self.assertEqual(verify.settings(-1001)["flood_penalty"], "mute")
            self.assertEqual(verify.settings(-1001)["flood_notice_seconds"], 30)
            verify.close()

    def test_group_admin_exemption_and_no_edit_flood_count(self):
        self.store.set(-1001, "flood_limit", 3)
        self.store.set(-1001, "repeat_limit", 10)
        self.store.set(-1001, "flood_penalty", "mute")
        for j in range(5):
            self.bot.handle_update(self.msg("管理员发言", uid=1, mid=800+j))
        self.assertEqual(self.deletions(), [])
        original = self.msg("用户消息", uid=2, mid=900)["message"]
        self.bot.handle_update({"message": original})
        for j in range(15):
            self.bot.handle_update({"edited_message": {
                **original, "text": f"更新原消息{j}"}})
        self.assertEqual(self.deletions(), [])
        self.assertEqual(self.restrictions(), [])


if __name__ == "__main__":
    unittest.main()
