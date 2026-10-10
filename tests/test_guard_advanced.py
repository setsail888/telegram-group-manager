"""Integration coverage of the new two-column panel and advanced moderation."""
import os
import tempfile
import time
import unittest

from app.service import GroupBot, MENU_MODULES, PAGE_SWITCHES, PAGE_NUMBERS
from app.store import Store, DEFAULTS, NUMBER_LIMITS
from tests.test_service import FakeTelegramAPI


class GuardStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "group.sqlite3")
        self.store = Store(self.path)
        self.store.ensure_chat(-100, "群甲")
        self.store.ensure_chat(-200, "群乙")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_new_flood_settings_default_to_conservative_opt_in(self):
        conf = self.store.settings(-100)
        for name in ("spam_guard_enabled", "word_loose_match",
                     "media_flood_enabled", "auto_mute_enabled",
                     "name_guard_enabled"):
            self.assertIs(conf[name], False)
        self.assertTrue(conf["anti_flood"])
        self.assertEqual(conf["flood_limit"], 7)
        self.store.set(-100, "repeat_limit", 4)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.settings(-100)["repeat_limit"], 4)
        self.assertEqual(self.store.settings(-200)["repeat_limit"], 3)

    def test_all_configured_number_ranges_and_boolean_types(self):
        for key, (low, high) in NUMBER_LIMITS.items():
            with self.subTest(name=key):
                self.store.set(-100, key, low)
                self.store.set(-100, key, high)
                for wrong in (low - 1, high + 1, True, "7"):
                    with self.assertRaises(ValueError):
                        self.store.set(-100, key, wrong)
        for key in ("spam_guard_enabled", "media_flood_enabled",
                    "auto_mute_enabled", "name_guard_enabled"):
            with self.assertRaises(ValueError):
                self.store.set(-100, key, 1)

    def test_bulk_words_import_atomic_and_scoped(self):
        self.assertEqual(self.store.add_filters_bulk(
            -100, "word", [("加微信", "加微信"), ("诈骗群", "诈骗群")]), 2)
        self.assertEqual(len(self.store.filters(-100, "word")), 2)
        self.assertEqual(self.store.filters(-200, "word"), [])
        with self.assertRaises(ValueError):
            self.store.add_filters_bulk(
                -100, "word", [(f"词{i}", f"词{i}") for i in range(80)]
                + [("另一个", "另一个")])
        self.assertEqual(len(self.store.filters(-100, "word")), 2)
        with self.assertRaises(ValueError):
            self.store.add_filters_bulk(-100, "word", [])
        self.assertEqual(len(self.store.filters(-100, "word")), 2)

    def test_existing_filter_at_capacity_can_be_replaced(self):
        self.store.add_filters_bulk(
            -100, "word", [(str(i) + "字", str(i) + "字") for i in range(80)])
        self.store.add_filters_bulk(
            -100, "word", [(str(i) + "词", str(i) + "词") for i in range(80)])
        self.store.add_filters_bulk(
            -100, "word", [(str(i) + "串", str(i) + "串") for i in range(80)])
        self.store.add_filters_bulk(
            -100, "word", [(str(i) + "句", str(i) + "句") for i in range(60)])
        self.assertEqual(len(self.store.filters(-100, "word")), 300)
        self.store.add_filter(-100, "word", "0字", "替换第一条")
        self.assertEqual(len(self.store.filters(-100, "word")), 300)
        with self.assertRaises(ValueError):
            self.store.add_filter(-100, "word", "新增规则", "新增规则")
        with self.assertRaises(ValueError):
            self.store.add_filters_bulk(
                -100, "word", [("新条目", "新条目"), ("1字", "更新旧条目")])
        self.assertEqual(len(self.store.filters(-100, "word")), 300)

    def test_durable_moderation_dedup_and_group_isolation(self):
        a = self.store.record_moderation(-100, 8, 10, "外链", now=1000)
        b = self.store.record_moderation(-100, 8, 10, "外链", now=1001)
        c = self.store.record_moderation(-100, 8, 11, "外链", now=1001)
        other = self.store.record_moderation(-200, 8, 10, "外链", now=1001)
        self.assertEqual((a, b, c, other), (
            {"created": True, "recent": 1},
            {"created": False, "recent": 1},
            {"created": True, "recent": 2},
            {"created": True, "recent": 1}))
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(
            self.store.record_moderation(-100, 8, 10, "外链", now=1002)["created"])


class GuardPanelTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store(":memory:")
        self.store.ensure_chat(-1001, "图二样式测试群")
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def cmd(self, uid, text, n=30):
        return {
            "chat": {"id": -1001, "type": "supergroup", "title": "图二样式测试群"},
            "from": {"id": uid, "first_name": "成员"},
            "text": text, "message_id": n,
        }

    def cb(self, uid, data, n=99):
        return {"callback_query": {
            "id": "testquery", "data": data, "from": {"id": uid},
            "message": {"chat": {"id": uid, "type": "private"}, "message_id": n}}}

    def edited(self):
        return [args for method, args in self.api.calls
                if method == "editMessageText"][-1]

    def deleted(self):
        return [args for method, args in self.api.calls
                if method == "deleteMessage"]

    def private_msg(self, uid, text):
        return {"message": {"chat": {"id": uid, "type": "private"},
                            "from": {"id": uid, "first_name": "管理员"},
                            "text": text, "message_id": 900}}

    def test_main_panel_is_two_column_and_every_module_has_destination(self):
        self.bot._private_panel(1, -1001, 100)
        page = self.edited()
        self.assertIn("设置「图二样式测试群」", page["text"])
        rows = page["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(rows), len(MENU_MODULES) // 2)
        self.assertTrue(all(len(row) == 2 for row in rows))
        self.assertTrue(all(
            len(btn["callback_data"].encode("utf-8")) <= 64
            for row in rows for btn in row))
        self.assertTrue(any("智能反垃圾" in b["text"] for row in rows for b in row))
        self.assertTrue(any("切换群组" in b["text"] for row in rows for b in row))

    def test_keyword_panel_never_expands_word_list_even_with_300_rules(self):
        for start in range(0, 300, 80):
            items = [(f"测试敏感词_{n:03d}", f"测试敏感词_{n:03d}")
                     for n in range(start, min(start + 80, 300))]
            self.store.add_filters_bulk(-1001, "word", items)
        self.store.add_filter(-1001, "word_exception",
                              "例外词|整句例外词", "例外词|整句例外词")
        self.assertEqual(self.store.filter_count(-1001, "word"), 300)
        self.assertEqual(self.store.filter_count(-1001, "word_exception"), 1)

        # Toggling loose matching must not make the message longer or expose
        # hundreds of stored sensitive phrases.
        bodies = []
        for loose in (False, True):
            self.store.set(-1001, "word_loose_match", loose)
            self.bot.handle_update(self.cb(1, "v:-1001:words"))
            page = self.edited()
            text = page["text"]
            bodies.append(text)
            self.assertIn("已添加：300 条", text)
            self.assertIn("精确例外：1 条", text)
            self.assertLess(len(text), 100)
            self.assertNotIn("测试敏感词_000", text)
            self.assertNotIn("测试敏感词_299", text)
            self.assertNotIn("例外词|整句例外词", text)

            buttons = [button for row in page["reply_markup"]["inline_keyboard"]
                       for button in row]
            add = [button for button in buttons
                   if button["callback_data"] == "w:-1001:add"]
            self.assertEqual(len(add), 1)
            self.assertEqual(add[0]["text"], "➕ 添加违禁词")
            self.assertFalse(any("私聊批量添加" in b["text"] for b in buttons))
            self.assertTrue(any(b["callback_data"] == "w:-1001:del"
                                for b in buttons))
            self.assertTrue(any(b["callback_data"] ==
                                "t:-1001:word_loose_match:words" for b in buttons))
        self.assertEqual(bodies[0], bodies[1])

    def test_keyword_panel_can_still_add_multiple_words_via_renamed_button(self):
        self.bot.handle_update(self.cb(1, "v:-1001:words"))
        buttons = [button for row in self.edited()["reply_markup"]["inline_keyboard"]
                   for button in row]
        add = next(b for b in buttons if b["text"] == "➕ 添加违禁词")
        self.bot.handle_update(self.cb(1, add["callback_data"]))
        self.bot.handle_update(self.private_msg(1, "推广广告,请加微信,刷单返佣"))
        self.assertEqual(self.store.filter_count(-1001, "word"), 3)
        self.bot.handle_update(self.cb(1, "v:-1001:words"))
        self.assertIn("已添加：3 条", self.edited()["text"])
        self.assertNotIn("推广广告", self.edited()["text"])

    def test_all_subpage_callback_data_fit_telegram_64_byte_limit(self):
        big_chat_id = -1001234567890123
        conf = DEFAULTS.copy()
        for page in set(PAGE_SWITCHES) | set(PAGE_NUMBERS):
            keyboard = self.bot._settings_controls(big_chat_id, page, conf)
            self.assertTrue(keyboard)
            for row in keyboard:
                for button in row:
                    self.assertLessEqual(
                        len(button["callback_data"].encode("utf-8")), 64)

    def test_subpage_switch_and_number_buttons_are_live(self):
        self.bot.handle_update(self.cb(1, "v:-1001:flood"))
        keys = self.edited()["reply_markup"]["inline_keyboard"]
        cmds = [b["callback_data"] for row in keys for b in row]
        self.assertIn("t:-1001:media_flood_enabled:flood", cmds)
        self.assertIn("n:-1001:flood_limit:+1:flood", cmds)
        self.bot.handle_update(self.cb(1, "t:-1001:media_flood_enabled:flood"))
        self.assertTrue(self.store.settings(-1001)["media_flood_enabled"])
        self.assertIn("媒体连发", self.edited()["text"])
        self.bot.handle_update(self.cb(1, "n:-1001:flood_limit:+1:flood"))
        self.assertEqual(self.store.settings(-1001)["flood_limit"], 8)
        self.bot.handle_update(self.cb(1, "n:-1001:flood_limit:-1:flood"))
        self.assertEqual(self.store.settings(-1001)["flood_limit"], 7)

    def test_tampered_numeric_button_or_group_member_is_denied(self):
        for data in (
            "n:-1001:flood_limit:+99:flood",
            "n:-1001:auto_mute_minutes:+1:flood",
            "n:-1001:flood_limit:bogus:flood",
        ):
            self.bot.handle_update(self.cb(1, data))
        self.bot.handle_update(self.cb(2, "n:-1001:flood_limit:+1:flood"))
        self.bot.handle_update(self.cb(2, "t:-1001:spam_guard_enabled:spam"))
        self.assertEqual(self.store.settings(-1001)["flood_limit"], 7)
        self.assertFalse(self.store.settings(-1001)["spam_guard_enabled"])
        self.assertTrue(any(
            p.get("show_alert") for m, p in self.api.calls if m == "answerCallbackQuery"))

    def test_spam_settings_respect_admin_permission_before_activation(self):
        original = self.api.call

        def no_delete(method, **kw):
            if method == "getChatMember" and kw.get("user_id") == 999:
                return {"status": "administrator", "can_delete_messages": False}
            return original(method, **kw)

        self.api.call = no_delete
        self.bot.handle_update(self.cb(1, "t:-1001:spam_guard_enabled:spam"))
        self.assertFalse(self.store.settings(-1001)["spam_guard_enabled"])

    def test_media_storm_silent_and_admin_exempt(self):
        self.store.set(-1001, "media_flood_enabled", True)
        self.store.set(-1001, "media_flood_limit", 2)
        self.store.set(-1001, "flood_limit", 15)
        for uid in (2, 1):
            for n in range(3):
                msg = self.cmd(uid, "", 200 + uid * 10 + n)
                msg["sticker"] = {"file_unique_id": f"{uid}-{n}"}
                self.bot.handle_update({"message": msg})
        self.assertEqual([p["message_id"] for p in self.deleted()], [222])

    def test_admin_batch_keyword_and_targeted_exception(self):
        self.bot.handle_update({"message": self.cmd(
            1, "/addwords 广告,加微信联系，虚假投资", 400)})
        self.assertEqual(len(self.store.filters(-1001, "word")), 3)
        self.bot.handle_update({"message": self.cmd(
            1, "/wordexcept 广告 | 学校广告设计", 401)})
        self.bot.handle_update({"message": self.cmd(2, "学校广告设计", 402)})
        self.assertEqual(self.deleted(), [])
        self.bot.handle_update({"message": self.cmd(
            2, "学校广告设计请私聊", 403)})
        self.assertEqual([p["message_id"] for p in self.deleted()], [403])
        self.bot.handle_update({"message": self.cmd(
            1, "/wordtest 你好请加-微-信-联系", 404)})
        # Loose mode default off; direct test should not mutate/group delete.
        self.assertEqual([p["message_id"] for p in self.deleted()], [403])
        self.bot.handle_update({"message": self.cmd(1, "/wordloose on", 405)})
        self.bot.handle_update({"message": self.cmd(
            2, "加-微-信-联系", 406)})
        self.assertEqual([p["message_id"] for p in self.deleted()], [403, 406])

    def test_private_wizard_bulk_add_delete_and_preview(self):
        self.bot.handle_update(self.cb(1, "w:-1001:add"))
        prompt = [p["text"] for method, p in self.api.calls
                  if method == "sendMessage"][-1]
        self.assertIn("最多80条", prompt)
        self.bot.handle_update(self.private_msg(1, "加微信\n虚假投资,刷屏广告"))
        self.assertEqual(len(self.store.filters(-1001, "word")), 3)
        self.bot.handle_update(self.cb(1, "w:-1001:test"))
        self.bot.handle_update(self.private_msg(1, "这里有虚假投资"))
        text = [p["text"] for method, p in self.api.calls
                if method == "sendMessage"][-1]
        self.assertIn("命中", text)
        self.bot.handle_update(self.cb(1, "w:-1001:del"))
        self.bot.handle_update(self.private_msg(1, "加微信，刷屏广告"))
        self.assertEqual([r["key"] for r in self.store.filters(-1001, "word")],
                         ["虚假投资"])
        self.assertFalse(any(p.get("chat_id") == -1001 for m, p in self.api.calls
                             if m == "sendMessage"))

    def test_private_wizard_permissions_timeout_and_cancel(self):
        self.bot.handle_update(self.cb(2, "w:-1001:add"))
        self.assertNotIn(2, self.bot._word_wizards)
        self.bot.handle_update(self.cb(1, "w:-1001:add"))
        self.bot.handle_update(self.private_msg(1, "/cancel"))
        self.assertNotIn(1, self.bot._word_wizards)
        self.bot.handle_update(self.cb(1, "w:-1001:add"))
        self.bot._word_wizards[1] = (-1001, "add", time.monotonic() - 1)
        self.bot.handle_update(self.private_msg(1, "非法加入列表"))
        self.assertEqual(self.store.filters(-1001, "word"), [])
        self.bot.handle_update(self.cb(1, "w:-1001:add"))
        self.bot.handle_update(self.private_msg(1, "/help"))
        self.assertNotIn(1, self.bot._word_wizards)
        self.bot.handle_update(self.cb(1, "w:-1001:add"))
        self.bot.handle_update(self.cb(1, "v:-1001:spam"))
        self.bot.handle_update(self.private_msg(1, "应不被添加的内容"))
        self.assertEqual(self.store.filters(-1001, "word"), [])

    def test_private_wizard_rechecks_admin_membership(self):
        self.bot.handle_update(self.cb(1, "w:-1001:add"))
        original = self.api.call

        def no_longer_admin(method, **args):
            if method == "getChatMember" and args.get("user_id") == 1:
                return {"status": "member"}
            return original(method, **args)

        self.api.call = no_longer_admin
        self.bot.handle_update(self.private_msg(1, "后门关键词"))
        self.assertEqual(self.store.filters(-1001, "word"), [])
        self.assertNotIn(1, self.bot._word_wizards)

    def test_auto_mute_is_off_by_default_and_opportunistic_when_enabled(self):
        self.assertFalse(self.store.settings(-1001)["auto_mute_enabled"])
        self.store.set(-1001, "auto_mute_enabled", True)
        self.store.set(-1001, "auto_mute_after", 3)
        for n in range(3):
            self.bot.handle_update({"message": self.cmd(
                2, f"来广告 https://spam{n}.example.com", 500 + n)})
        self.assertEqual(len(self.deleted()), 3)
        restrict = [p for method, p in self.api.calls if method == "restrictChatMember"]
        self.assertEqual(len(restrict), 1)
        self.assertEqual(restrict[0]["user_id"], 2)
        self.assertEqual(len([m for m, p in self.api.calls if m == "banChatMember"]), 0)
        # Receiving/editing an already moderated update is not a new offense.
        self.bot.handle_update({"edited_message": self.cmd(
            2, "https://spam2.example.com", 502)})
        restrict = [p for method, p in self.api.calls if method == "restrictChatMember"]
        self.assertEqual(len(restrict), 1)

    def test_editing_messages_does_not_count_as_flood(self):
        self.store.set(-1001, "flood_limit", 3)
        original = self.cmd(2, "这是一条正常文字", 600)
        self.bot.handle_update({"message": original})
        for i in range(8):
            self.bot.handle_update({"edited_message": {**original,
                                                        "text": f"编辑第{i}次"}})
        self.assertEqual(self.deleted(), [])
        self.bot.handle_update({"message": self.cmd(2, "第二条", 601)})
        self.bot.handle_update({"message": self.cmd(2, "第三条", 602)})
        self.assertEqual(self.deleted(), [])


    def test_opt_in_new_member_name_guard_restricts_not_bans(self):
        self.store.set(-1001, "name_guard_enabled", True)
        self.store.set(-1001, "name_length_limit", 20)
        member = {"id": 2, "first_name": "夸张名字" * 8, "last_name": "广告员"}
        msg = self.cmd(1, "", 700)
        msg["new_chat_members"] = [member]
        self.bot.handle_update({"message": msg})
        restrictions = [p for m, p in self.api.calls if m == "restrictChatMember"]
        self.assertEqual(len(restrictions), 1)
        self.assertEqual(restrictions[0]["user_id"], 2)
        self.assertTrue(restrictions[0]["until_date"] > int(time.time()))
        self.assertFalse(any(m == "banChatMember" for m, _ in self.api.calls))
        self.assertFalse(any(m == "sendMessage" and p["chat_id"] == -1001
                             for m, p in self.api.calls))

    def test_new_member_name_guard_admin_exempt_and_default_disabled(self):
        long_admin = {"id": 1, "first_name": "管理员超级长名字" * 5}
        new_user = {"id": 4, "first_name": "普通成员超长名称" * 6}
        msg = self.cmd(1, "", 701)
        msg["new_chat_members"] = [long_admin, new_user]
        self.bot.handle_update({"message": msg})
        self.assertFalse(any(m == "restrictChatMember" for m, _ in self.api.calls))
        self.store.set(-1001, "name_guard_enabled", True)
        msg["message_id"] = 702
        self.bot.handle_update({"message": msg})
        restrictions = [p for m, p in self.api.calls if m == "restrictChatMember"]
        self.assertEqual(len(restrictions), 1)
        self.assertEqual(restrictions[0]["user_id"], 4)

    def test_member_limit_page_and_numeric_control(self):
        self.bot.handle_update(self.cb(1, "v:-1001:members"))
        self.assertIn("新成员昵称限制", self.edited()["text"])
        self.bot.handle_update(self.cb(1, "n:-1001:name_length_limit:+5:members"))
        self.assertEqual(self.store.settings(-1001)["name_length_limit"], 45)
        self.bot.handle_update(self.cb(1, "t:-1001:name_guard_enabled:members"))
        self.assertTrue(self.store.settings(-1001)["name_guard_enabled"])

    def test_captcha_takes_priority_over_temporary_nickname_restriction(self):
        self.store.set(-1001, "name_guard_enabled", True)
        self.store.set(-1001, "name_length_limit", 20)
        self.store.set(-1001, "verify_enabled", True)
        msg = self.cmd(1, "", 703)
        msg["new_chat_members"] = [
            {"id": 4, "first_name": "很长很长的广告昵称" * 6}]
        self.bot.handle_update({"message": msg})
        restrictions = [p for m, p in self.api.calls if m == "restrictChatMember"]
        self.assertEqual(len(restrictions), 1)
        self.assertEqual(restrictions[0]["until_date"], 0)
        self.assertIsNotNone(self.store.pending(-1001, 4))


if __name__ == "__main__":
    unittest.main()
