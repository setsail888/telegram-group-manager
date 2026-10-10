"""Rule precision, durable upgrades, and button-driven burst-join protection."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing

from app.api import TelegramAPIError
from app.moderation import evaluate, match_words
from app.ocr import matched_words
from app.raid import JoinGuard
from app.service import GroupBot
from app.store import Store
from tests.test_service import FakeTelegramAPI


class KeywordModesTests(unittest.TestCase):
    def test_legacy_contains_and_immediate_mode_change(self):
        self.assertEqual(match_words("bad", ["ad"]), "ad")
        self.assertIsNone(match_words("bad", ["ad"], modes={"ad": "word"}))
        self.assertEqual(match_words("ad", ["ad"], modes={"ad": "exact"}), "ad")
        self.assertIsNone(match_words("ad today", ["ad"], modes={"ad": "exact"}))
        self.assertEqual(match_words("bad", ["ad"]), "ad")

    def test_unicode_boundaries_and_literal_metacharacters(self):
        for text in ("bad", "advice", "ad_1", "1ad", "汉ad字"):
            self.assertIsNone(match_words(text, ["ad"], modes={"ad": "word"}), text)
        for text in ("(AD)", "ad!", "请发 ad 谢谢", "Ａ\u200bＤ"):
            self.assertEqual(match_words(text, ["ad"], modes={"ad": "word"}), "ad", text)
        self.assertEqual(match_words("[a+b]", ["a+b"], modes={"a+b": "word"}), "a+b")
        self.assertIsNone(match_words("aaab", ["a+b"], modes={"a+b": "word"}))

    def test_exact_normalization_but_no_extra_words_or_split_override(self):
        modes = {"contact": "exact"}
        self.assertEqual(match_words(" ＣＯＮ\u200bＴＡＣＴ ", ["contact"], modes=modes), "contact")
        for mode in ("word", "exact"):
            for text in ("c⭐o⭐n⭐t⭐a⭐c⭐t", "contacting"):
                self.assertIsNone(match_words(text, ["contact"], loose=True, modes={"contact": mode}))
        self.assertEqual(match_words("c⭐o⭐n⭐t⭐a⭐c⭐t", ["contact"], loose=True), "contact")

    def test_targeted_exception_does_not_waive_another_rule(self):
        self.assertIsNone(match_words("ad", ["ad"], exceptions=["ad|ad"], modes={"ad": "exact"}))
        self.assertEqual(match_words("ad spam", ["ad", "spam"], exceptions=["ad|ad spam"],
                                     modes={"ad": "word", "spam": "word"}), "spam")

    def test_caption_and_individual_button_exact_match(self):
        modes = {"contact": "exact"}
        button = {"inline_keyboard": [[{"text": "帮助", "url": "https://example.com"}]]}
        self.assertEqual(evaluate({"caption": "contact", "reply_markup": button},
                         {"block_links": False}, ["contact"], [], modes=modes), "违禁词")
        self.assertIsNone(evaluate({"caption": "contact", "reply_markup": button},
            {"block_links": False}, ["contact"], [], modes=modes, exceptions=["contact|contact\n帮助"]))
        self.assertEqual(evaluate({"caption": "contact", "reply_markup": button},
            {"block_links": False}, ["contact"], [], modes={"CONTACT": "exact"},
            exceptions=["contact|contact"]), "违禁词")
        button["inline_keyboard"][0][0]["text"] = "contact"
        self.assertEqual(evaluate({"text": "说明", "reply_markup": button},
                         {"block_links": False}, ["contact"], [], modes=modes), "违禁词")
        self.assertIsNone(evaluate({"text": "please contact me"}, {}, ["contact"], [], modes=modes))

    def test_ocr_modes_preserve_confidence_length_and_word_boundaries(self):
        self.assertEqual(matched_words("Contact", 90, ["contact"], modes={"contact": "word"}), ["contact"])
        self.assertFalse(matched_words("contacting", 90, ["contact"], modes={"contact": "word"}))
        self.assertEqual(matched_words("联 系 客 服", 90, ["联系客服"], modes={"联系客服": "exact"}), ["联系客服"])
        self.assertFalse(matched_words("请 联系客服", 90, ["联系客服"], modes={"联系客服": "exact"}))
        self.assertFalse(matched_words("联系客服", 20, ["联系客服"], modes={"联系客服": "exact"}))
        self.assertFalse(matched_words("ad", 90, ["ad"], modes={"ad": "exact"}))
        self.assertFalse(matched_words("联 系 客 服", 90, ["联系客服"],
            modes={"联系客服": "exact"}, exceptions=["联系客服|联系客服"]))


class KeywordStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1, "群1")
        self.store.ensure_chat(-2, "群2")

    def tearDown(self):
        self.store.close()

    def test_upserts_preserve_modes_and_deletion_cascades(self):
        self.store.add_filter(-1, "word", "ad", "ad")
        self.store.edit_word_rule(-1, "ad", "word")
        self.store.add_filter(-1, "word", "ad", "AD")
        self.store.add_filters_bulk(-1, "word", [("ad", "ad")])
        self.assertEqual(self.store.word_rule(-1, "ad")["mode"], "word")
        self.store.del_filter(-1, "word", "ad")
        self.assertFalse(self.store.word_modes(-1))
        self.store.add_filter(-1, "word", "ad", "ad")
        self.assertEqual(self.store.word_rule(-1, "ad")["mode"], "contains")

    def test_stale_preview_and_cross_group_are_rejected(self):
        self.store.add_filter(-1, "word", "ad", "ad")
        before = self.store.word_rule(-1, "ad")
        self.store.edit_word_rule(-1, "ad", "word", expected=before)
        with self.assertRaises(ValueError):
            self.store.edit_word_rule(-1, "ad", None, expected=before)
        with self.assertRaises(ValueError):
            self.store.edit_word_rule(-2, "ad", "exact")
        with self.assertRaises(ValueError):
            self.store.edit_word_rule(-1, "ad", "regex")
        self.assertEqual(self.store.word_rule(-1, "ad")["mode"], "word")

    def test_search_normalization_literal_wildcards_and_group_scope(self):
        for word in ("100%", "100x", "a_b", "axb", "x\\y", "xy", "ad"):
            self.store.add_filter(-1, "word", word, word)
        self.store.add_filter(-2, "word", "ad-other", "ad-other")
        for query, expected in (("%", "100%"), ("_", "a_b"), ("\\", "x\\y"), ("ＡＤ", "ad")):
            rows, count, _, _ = self.store.search_words(-1, query)
            self.assertEqual(count, 1, query)
            self.assertEqual(rows[0]["key"], expected)
        for invalid in ("", " "*5, "a"*81):
            with self.assertRaises(ValueError):
                self.store.search_words(-1, invalid)

    def test_search_pagination_and_bounds(self):
        self.store.add_filters_bulk(-1, "word", [(f"广告{i:02}", f"广告{i:02}") for i in range(19)])
        rows, count, page, pages = self.store.search_words(-1, "广告", 999)
        self.assertEqual((len(rows), count, page, pages), (3, 19, 2, 3))
        self.assertEqual(self.store.search_words(-1, "广告", -9)[2], 0)

    def test_legacy_database_upgrade_and_restart_retain_rules_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.sqlite")
            with closing(sqlite3.connect(path)) as db:
                db.executescript("CREATE TABLE chats(chat_id INTEGER PRIMARY KEY,title TEXT,settings TEXT,updated_at INTEGER);"
                                 "CREATE TABLE filters(chat_id INTEGER,kind TEXT,key TEXT,value TEXT,PRIMARY KEY(chat_id,kind,key));"
                                 "INSERT INTO chats VALUES(-1,'旧群','{\"block_links\":false}',0);"
                                 "INSERT INTO filters VALUES(-1,'word','ad','ad');")
            upgraded = Store(path)
            self.assertFalse(upgraded.settings(-1)["block_links"])
            self.assertFalse(upgraded.settings(-1)["raid_enabled"])
            self.assertEqual(upgraded.word_rule(-1, "ad")["mode"], "contains")
            upgraded.edit_word_rule(-1, "ad", "exact")
            until = int(time.time())+600
            upgraded.set_raid_until(-1, until)
            upgraded.close()
            reopened = Store(path)
            try:
                self.assertEqual(reopened.word_modes(-1), {"ad": "exact"})
                self.assertEqual(reopened.raid_until(-1), until)
                self.assertEqual(reopened.raid_until(-1, now=until), 0)
                self.assertEqual(list(reopened.db.execute("PRAGMA foreign_key_check")), [])
            finally:
                reopened.close()


class JoinGuardTests(unittest.TestCase):
    def test_distinct_members_window_and_group_isolation(self):
        guard = JoinGuard()
        self.assertFalse(guard.arrival(-1, 2, limit=3, window=10, now=0))
        self.assertFalse(guard.arrival(-1, 2, limit=3, window=10, now=1))
        self.assertFalse(guard.arrival(-1, 3, limit=3, window=10, now=2))
        self.assertFalse(guard.arrival(-2, 4, limit=3, window=10, now=3))
        self.assertTrue(guard.arrival(-1, 4, limit=3, window=10, now=4))
        self.assertFalse(guard.arrival(-1, 5, limit=3, window=10, now=15))

    def test_longest_window_memory_bounds_and_cleanup(self):
        guard = JoinGuard(max_groups=2)
        guard.arrival(-1, 2, limit=3, window=300, now=0)
        guard.arrival(-1, 3, limit=3, window=300, now=299)
        self.assertTrue(guard.arrival(-1, 4, limit=3, window=300, now=300))
        for cid in range(3):
            guard.arrival(cid, 2, now=301)
            guard.welcome(cid, now=301)
        self.assertEqual(len(guard.arrivals), 2)
        self.assertEqual(len(guard.welcomes), 2)
        guard.arrival(8, 2, now=650)
        self.assertEqual(list(guard.arrivals), [8])
        self.assertFalse(guard.welcomes)

    def test_welcome_limit_uses_group_window_and_recovers(self):
        guard = JoinGuard()
        self.assertTrue(guard.welcome(-1, limit=1, now=0))
        self.assertFalse(guard.welcome(-1, limit=1, now=59))
        self.assertTrue(guard.welcome(-2, limit=1, now=59))
        self.assertTrue(guard.welcome(-1, limit=1, now=61))


class KeywordRaidFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def click(self, data, uid=1, mid=42):
        self.bot.handle_update({"callback_query": {"id": "q", "from": {"id": uid}, "data": data,
            "message": {"message_id": mid, "chat": {"id": uid, "type": "private"}}}})

    def confirm(self):
        draft = dict(self.bot.admin_ui.drafts[1])
        self.click(f"ac:{draft['cid']}:{draft['nonce']}", mid=draft["prompt_id"])
        return draft

    def private(self, text):
        self.bot.handle_update({"message": {"message_id": 50, "text": text,
            "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}})

    def last_page(self):
        return [p for m, p in self.api.calls if m in ("sendMessage", "editMessageText")][-1]

    def button(self, prefix):
        return next(b["callback_data"] for row in self.last_page()["reply_markup"]["inline_keyboard"]
                    for b in row if b.get("callback_data", "").startswith(prefix))

    def join(self, *uids, cid=-1001, **attrs):
        self.bot.handle_update({"message": {"message_id": 20, "from": {"id": 1},
            "chat": {"id": cid, "type": "supergroup", "title": "测试群"},
            "new_chat_members": [{"id": uid, "first_name": "新人", **attrs} for uid in uids]}})

    def enable_raid(self):
        self.store.set(-1001, "raid_enabled", True)
        self.store.set(-1001, "raid_join_limit", 3)

    def test_word_mode_preview_confirm_replay_and_actual_moderation(self):
        self.store.add_filter(-1001, "word", "ad", "ad")
        self.click("wl:-1001:0")
        self.click(self.button("ad:-1001:word:"))
        self.click(self.button("ad:-1001:wm_word:"))
        self.assertEqual(self.store.word_rule(-1001, "ad")["mode"], "contains")
        draft = self.confirm()
        self.assertEqual(self.store.word_rule(-1001, "ad")["mode"], "word")
        self.store.edit_word_rule(-1001, "ad", "exact")
        self.click(f"ac:-1001:{draft['nonce']}", mid=draft["prompt_id"])
        self.assertEqual(self.store.word_rule(-1001, "ad")["mode"], "exact")
        self.api.calls.clear()
        for mid, text in ((1, "bad"), (2, "ad")):
            self.bot.handle_update({"message": {"message_id": mid, "text": text, "from": {"id": 2},
                "chat": {"id": -1001, "type": "supergroup"}}})
        self.assertEqual([p["message_id"] for m, p in self.api.calls if m == "deleteMessage"], [2])

    def test_word_delete_stale_preview_and_target_binding(self):
        self.store.add_filter(-1001, "word", "ad", "ad")
        self.click("wl:-1001:0")
        choice = self.button("ad:-1001:word:")
        self.click(choice, uid=999)
        self.assertNotIn(999, self.bot.admin_ui.drafts)
        self.click(choice.replace("-1001", "-1002"))
        self.assertNotIn(1, self.bot.admin_ui.drafts)
        self.click(choice)
        self.click(self.button("ad:-1001:word_delete:"))
        self.store.edit_word_rule(-1001, "ad", "word")
        self.confirm()
        self.assertIsNotNone(self.store.word_rule(-1001, "ad"))
        self.click(choice)
        self.click(self.button("ad:-1001:word_delete:"))
        self.confirm()
        self.assertIsNone(self.store.word_rule(-1001, "ad"))

    def test_search_form_pagination_scoped_tokens_and_expiry(self):
        self.store.add_filters_bulk(-1001, "word", [(f"广告{i:02}", f"广告{i:02}") for i in range(19)])
        self.click("ad:-1001:begin:word_search")
        self.private("广告")
        self.assertNotIn(1, self.bot.admin_ui.drafts)
        self.assertIn("共 19 条", self.last_page()["text"])
        next_page = self.button("ad:-1001:searchpage:")
        for row in self.last_page()["reply_markup"]["inline_keyboard"]:
            for b in row:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)
        self.click(next_page)
        self.assertIn("第 2/3 页", self.last_page()["text"])
        before = self.last_page()
        self.click(next_page, uid=999)
        self.assertEqual(self.last_page(), before)
        for entry in self.bot.admin_ui.searches.values():
            entry["expires"] = 0
        self.click(next_page)
        self.assertEqual(self.last_page(), before)

    def test_all_test_entry_points_use_selected_mode(self):
        self.store.add_filter(-1001, "word", "ad", "ad")
        self.store.edit_word_rule(-1001, "ad", "word")
        self.assertIn("没有命中", self.bot._private_word_input(1, -1001, "test", "bad"))
        self.click("ad:-1001:begin:spam_test")
        self.private("bad")
        self.assertIn("未命中", self.last_page()["text"])
        self.bot.handle_update({"message": {"message_id": 60, "text": "/wordtest bad", "from": {"id": 1},
            "chat": {"id": -1001, "type": "supergroup"}}})
        self.assertIn("未命中", self.last_page()["text"])

    def test_ocr_delivery_uses_current_rule_mode(self):
        self.store.add_filter(-1001, "word", "contact", "contact")
        self.store.set(-1001, "ocr_mode", "delete")
        self.store.edit_word_rule(-1001, "contact", "word")
        event = {"chat_id": -1001, "user_id": 2, "message_id": 61, "created_at": int(time.time())}
        with patch.object(self.bot.ocr, "results", return_value=[(event, "contacting", 95)]):
            self.bot.drain_ocr()
        self.assertFalse(any(m == "deleteMessage" for m, _ in self.api.calls))
        self.store.edit_word_rule(-1001, "contact", "contains")
        with patch.object(self.bot.ocr, "results", return_value=[(event, "contacting", 95)]):
            self.bot.drain_ocr()
        self.assertEqual([p["message_id"] for m, p in self.api.calls if m == "deleteMessage"], [61])

    def test_burst_threshold_verifies_trigger_and_later_joins_only(self):
        self.enable_raid()
        self.join(2, 3)
        self.assertFalse(self.store.raid_until(-1001))
        self.join(4, 5)
        self.assertTrue(self.store.raid_until(-1001))
        self.assertIsNone(self.store.pending(-1001, 2))
        self.assertIsNone(self.store.pending(-1001, 3))
        self.assertIsNotNone(self.store.pending(-1001, 4))
        self.assertIsNotNone(self.store.pending(-1001, 5))
        self.assertFalse(any(m == "banChatMember" for m, _ in self.api.calls))
        self.assertFalse(self.store.settings(-1001)["verify_enabled"])

    def test_defaults_duplicates_admins_bots_and_other_groups_do_not_trigger(self):
        self.join(2, 3, 4)
        self.assertFalse(self.store.raid_until(-1001))
        self.enable_raid()
        self.join(2, 2, 1, 999)
        self.join(3, is_bot=True)
        self.join(4, 5, cid=-1002)
        self.assertFalse(self.store.raid_until(-1001))
        self.join(3)
        self.assertFalse(self.store.raid_until(-1001))
        self.join(4)
        self.assertTrue(self.store.raid_until(-1001))

    def test_active_protection_replay_keeps_nonce_attempts_and_admin_exemption(self):
        self.store.set_raid_until(-1001, int(time.time())+600)
        self.join(2)
        self.store.verification_mistake(-1001, 2)
        pending = self.store.pending(-1001, 2)
        count = len([m for m, _ in self.api.calls if m == "restrictChatMember"])
        self.join(2, 1, 999)
        self.assertEqual(self.store.pending(-1001, 2), pending)
        self.assertEqual(len([m for m, _ in self.api.calls if m == "restrictChatMember"]), count)
        self.assertIsNone(self.store.pending(-1001, 1))
        self.assertFalse(self.store.settings(-1001)["raid_enabled"])
        original = self.api.call
        def restricted(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 7:
                return {"status": "restricted", "is_member": True, "can_send_messages": False}
            return original(method, **kwargs)
        with patch.object(self.api, "call", restricted):
            self.join(7)
            self.bot._start_verification(-1001, {"id": 7}, self.store.settings(-1001))
        self.assertIsNone(self.store.pending(-1001, 7))
        self.assertFalse(any(m == "restrictChatMember" and p["user_id"] == 7 for m, p in self.api.calls))

    def test_expiry_stops_extra_verification_without_unmuting_pending(self):
        self.store.set_raid_until(-1001, int(time.time())+600)
        self.join(2)
        pending = self.store.pending(-1001, 2)
        self.store.set_raid_until(-1001, int(time.time())-1)
        self.join(3)
        self.assertIsNone(self.store.pending(-1001, 3))
        self.assertEqual(self.store.pending(-1001, 2), pending)
        self.api.calls.clear()
        self.store.set(-1001, "name_guard_enabled", True)
        self.join(2, first_name="长"*100)
        self.assertFalse(any(m == "restrictChatMember" for m, _ in self.api.calls))
        self.assertEqual(self.store.pending(-1001, 2), pending)

    def test_unknown_membership_fails_open_without_counting_or_restriction(self):
        self.enable_raid()
        original = self.api.call
        def fail(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 2:
                raise TelegramAPIError("unavailable")
            return original(method, **kwargs)
        self.api.call = fail
        self.join(2, 3, 4)
        self.assertFalse(self.store.raid_until(-1001))
        self.assertFalse(any(m == "restrictChatMember" for m, _ in self.api.calls))

    def test_missing_permission_and_basic_group_never_start_protection(self):
        with patch.object(self.api, "call", return_value=None):
            self.assertFalse(self.bot.bot_can(-1001, "can_restrict_members"))
        for basic_group in (True, False):
            self.bot.join_guard.clear(-1001)
            self.enable_raid()
            original = self.api.call
            def limited(method, **kwargs):
                if basic_group and method == "getChat":
                    return {"type": "group"}
                if not basic_group and method == "getChatMember" and kwargs.get("user_id") == 999:
                    return {"status": "administrator", "can_restrict_members": False}
                return original(method, **kwargs)
            with patch.object(self.api, "call", limited):
                self.join(2, 3, 4)
            self.assertFalse(self.store.raid_until(-1001))
            self.assertFalse(any(m == "restrictChatMember" for m, _ in self.api.calls))

    def test_manual_start_stop_preview_and_stale_state(self):
        self.click("ad:-1001:raid:start")
        self.assertFalse(self.store.raid_until(-1001))
        self.confirm()
        until = self.store.raid_until(-1001)
        self.assertTrue(until)
        self.join(2)
        self.click("ad:-1001:raid:stop")
        self.store.set_raid_until(-1001, until+60)
        self.confirm()
        self.assertEqual(self.store.raid_until(-1001), until+60)
        self.click("ad:-1001:raid:stop")
        self.confirm()
        self.assertFalse(self.store.raid_until(-1001))
        self.assertIsNotNone(self.store.pending(-1001, 2))
        self.join(3)
        self.assertIsNone(self.store.pending(-1001, 3))

    def test_manual_start_rechecks_permission_and_auto_toggle_is_separate(self):
        self.click("ad:-1001:raid:start")
        with patch.object(self.bot, "bot_can", return_value=False):
            self.confirm()
        self.assertFalse(self.store.raid_until(-1001))
        self.click("t:-1001:raid_enabled:raid")
        self.assertTrue(self.store.settings(-1001)["raid_enabled"])
        self.click("ad:-1001:raid:start")
        self.confirm()
        until = self.store.raid_until(-1001)
        self.click("t:-1001:raid_enabled:raid")
        self.assertFalse(self.store.settings(-1001)["raid_enabled"])
        self.assertEqual(self.store.raid_until(-1001), until)

    def test_welcome_group_limit_does_not_throttle_verification_prompts(self):
        self.store.set(-1001, "welcome_limit", 1)
        self.join(2, 3, 4)
        self.assertEqual(len([m for m, p in self.api.calls if m == "sendMessage" and p["chat_id"] == -1001]), 1)
        self.api.calls.clear()
        self.store.set_raid_until(-1001, int(time.time())+600)
        self.join(5, 6)
        self.assertEqual(len([m for m, p in self.api.calls if m == "sendMessage" and p["chat_id"] == -1001]), 2)

    def test_new_bot_who_is_admin_is_exempt_from_removal(self):
        self.store.set(-1001, "block_new_bots", True)
        self.join(1, is_bot=True)
        self.assertFalse(any(m == "banChatMember" for m, _ in self.api.calls))
        self.join(2, is_bot=True)
        self.assertEqual([p["user_id"] for m, p in self.api.calls if m == "banChatMember"], [2])

    def test_raid_page_parameters_and_permissions_refresh(self):
        for key, old, new in (("raid_join_limit", 10, 11), ("raid_window", 60, 70),
                              ("raid_duration", 10, 11), ("welcome_limit", 10, 11)):
            self.assertEqual(self.store.settings(-1001)[key], old)
            page = "welcome" if key == "welcome_limit" else "raid"
            self.click(f"v:-1001:{page}")
            data = next(b["callback_data"] for row in self.last_page()["reply_markup"]["inline_keyboard"]
                        for b in row if b.get("callback_data", "").startswith("n:")
                        and key in b["callback_data"] and ":+" in b["callback_data"])
            self.click(data)
            self.assertEqual(self.store.settings(-1001)[key], new)
        self.api.calls.clear()
        self.click("v:-1001:permissions")
        self.assertIn("删除消息：✅", self.last_page()["text"])
        self.assertEqual(len([p for m, p in self.api.calls if m == "getChatMember" and p["user_id"] == 1]), 1)


if __name__ == "__main__":
    unittest.main()
