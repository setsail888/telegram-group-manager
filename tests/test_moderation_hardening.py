"""Adversarial moderation cases, with no live Telegram API or external services."""
import unittest
from unittest.mock import patch

from app.moderation import FloodGuard, PhotoRepeatGuard, evaluate, match_words, normalize_domain
from app.service import GroupBot
from app.store import Store
from tests.test_service import FakeTelegramAPI


def entity(text, token, kind="url", **extra):
    start = text.index(token)
    return {"type": kind, "offset": len(text[:start].encode("utf-16-le")) // 2,
            "length": len(token.encode("utf-16-le")) // 2, **extra}


class ContentHardeningTests(unittest.TestCase):
    conf = {"block_links": True, "spam_guard_enabled": True, "anti_flood": False}

    def test_emoji_and_combining_marks_in_split_words_are_opt_in(self):
        for content in ("加⭐微⭐信联系", "加\u0332微\u0332信联系", "加＋微＋信联系"):
            with self.subTest(content=content):
                self.assertIsNone(match_words(content, ["加微信联系"]))
                self.assertEqual(match_words(content, ["加微信联系"], loose=True), "加微信联系")
        self.assertIsNone(match_words("可⭐爱", ["可爱"], loose=True))

    def test_rule_changes_and_group_specific_exceptions_take_effect_immediately(self):
        text = "学校广告设计"
        self.assertIsNone(match_words(text, ["广告"], exceptions=["广告|" + text]))
        self.assertEqual(match_words(text, ["广告"]), "广告")
        self.assertEqual(match_words(text, ["广告", "设计"], exceptions=["广告|" + text]), "设计")
        self.assertEqual(match_words(text + "买产品", ["广告"], exceptions=["广告|" + text]), "广告")
        self.assertIsNone(match_words(text, []))

    def test_url_entity_supports_utf16_emoji_and_unlisted_tld(self):
        text = "😀 官网 spam.surf"
        msg = {"text": text, "entities": [entity(text, "spam.surf")]}
        self.assertEqual(evaluate(msg, self.conf, [], []), "外部链接")
        self.assertIsNone(evaluate(msg, self.conf, [], ["spam.surf"]))

    def test_url_caption_entity_and_fullwidth_text_use_original_offsets(self):
        text = "😀 ＡＢ 链接 spam.surf"
        msg = {"caption": text, "caption_entities": [entity(text, "spam.surf")]}
        self.assertEqual(evaluate(msg, self.conf, [], []), "外部链接")

    def test_malformed_entity_offsets_do_not_crash(self):
        for offset, length in ((-1, 3), (999, 1), (1, 1), ("0", 2), (0, 0)):
            with self.subTest(offset=offset, length=length):
                msg = {"text": "😀普通文字", "entities": [
                    {"type": "url", "offset": offset, "length": length}]}
                self.assertIsNone(evaluate(msg, self.conf, [], []))

    def test_malformed_hostname_does_not_abort_filtering(self):
        self.assertEqual(normalize_domain("https://[invalid/path"), "")
        self.assertEqual(evaluate({"text": "https://[invalid/path"}, self.conf, [], []), "外部链接")

    def test_www_whitelist_keeps_full_host(self):
        conf = {**self.conf, "spam_guard_enabled": False}
        self.assertIsNone(evaluate({"text": "www.example.com"}, conf, [], ["www.example.com"]))
        self.assertEqual(evaluate({"text": "example.com"}, conf, [], ["www.example.com"]), "外部链接")

    def test_whitelisted_links_next_to_sentence_punctuation(self):
        for text in ("网址 https://example.com。", "(https://example.com)", "https://example.com.",
                     "参考https://example.com，官方教程", "https://example.com（官方）"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate({"text": text}, self.conf, [], ["example.com"]))

    def test_entity_domain_is_not_misread_as_a_known_tld_prefix(self):
        text = "docs.com.surf"
        msg = {"text": text, "entities": [entity(text, text)]}
        self.assertIsNone(evaluate(msg, self.conf, [], ["docs.com.surf"]))
        self.assertEqual(evaluate(msg, self.conf, [], ["docs.com"]), "外部链接")

    def test_obfuscated_links_respect_same_domain_whitelist(self):
        for text in ("t[.]me/demo", "t ( . ) me/demo", "t . me/demo", "hxxps://t.me/demo"):
            with self.subTest(text=text):
                self.assertIn(evaluate({"text": text}, self.conf, [], []), ("外部链接", "伪装链接"))
                self.assertIsNone(evaluate({"text": text}, self.conf, [], ["t.me"]))

    def test_general_obfuscated_domains_and_suffix_attacks(self):
        for text in ("evil[.]com/path", "evil(dot)net", "hxxps://bad.surf", "evil . org"):
            with self.subTest(text=text):
                self.assertEqual(evaluate({"text": text}, self.conf, [], []), "伪装链接")
                self.assertIsNone(evaluate({"text": text}, {**self.conf, "spam_guard_enabled": False}, [], []))
        self.assertIn(evaluate({"text": "hxxps://t.me.evil.com/path"}, self.conf, [], ["t.me"]), ("外部链接", "伪装链接"))

    def test_hidden_mentions_count_alongside_visible_mentions(self):
        text = "😀 张三 李四 王五 @person01"
        entities = [entity(text, name, "text_mention", user={"id": index})
                    for index, name in enumerate(("张三", "李四", "王五"), 10)]
        entities.append(entity(text, "@person01", "mention"))
        msg = {"text": text, "entities": entities}
        conf = {**self.conf, "mention_limit": 3}
        self.assertEqual(evaluate(msg, conf, [], []), "批量提及刷屏")
        self.assertIsNone(evaluate(msg, {**conf, "mention_limit": 4}, [], []))
        self.assertIsNone(evaluate(msg, {**conf, "spam_guard_enabled": False}, [], []))

    def test_caption_hidden_mentions_and_no_visible_double_count(self):
        text = "@person01 @person02 @person03"
        entities = [entity(text, token, "text_mention", user={"id": i})
                    for i, token in enumerate(text.split(), 10)]
        msg = {"caption": text, "caption_entities": entities}
        self.assertIsNone(evaluate(msg, {**self.conf, "mention_limit": 3}, [], []))
        hidden = "张三 李四 王五 赵六"
        msg = {"caption": hidden, "caption_entities": [
            entity(hidden, token, "text_mention", user={"id": i})
            for i, token in enumerate(hidden.split(), 10)]}
        self.assertEqual(evaluate(msg, {**self.conf, "mention_limit": 3}, [], []), "批量提及刷屏")


class FloodHardeningTests(unittest.TestCase):
    def test_long_messages_with_same_prefix_are_not_repeats(self):
        guard = FloodGuard(repeat_limit=3)
        prefix = "项目说明" * 100
        for i in range(3):
            self.assertIsNone(guard.check(-100, 2, prefix + str(i), now=i))

    def test_long_identical_messages_are_still_repeats(self):
        guard = FloodGuard(repeat_limit=3)
        for i in range(2):
            self.assertIsNone(guard.check(-100, 2, "长文本" * 1000, now=i))
        self.assertEqual(guard.check(-100, 2, "长文本" * 1000, now=2), "重复刷屏")

    def test_duplicate_delivery_does_not_count_as_new_message(self):
        guard = FloodGuard(repeat_limit=3)
        for i in range(10):
            self.assertIsNone(guard.check(-100, 2, "内容", now=i, message_id=1))
        self.assertIsNone(guard.check(-100, 2, "内容", now=11, message_id=2))
        self.assertEqual(guard.check(-100, 2, "内容", now=12, message_id=3), "重复刷屏")

    def test_busy_group_keeps_five_minute_repeat_window(self):
        guard = FloodGuard(repeat_window=300)
        for uid in range(3, 15004):
            guard.check(-100, uid, "普通文字", now=100)
        guard.check(-100, 2, "重复消息", now=100)
        guard.check(-100, 2, "重复消息", now=101)
        guard.check(-100, 20000, "触发清理", now=170)
        self.assertEqual(guard.check(-100, 2, "重复消息", now=171), "重复刷屏")

    def test_history_has_hard_group_member_and_message_bounds(self):
        guard = FloodGuard()
        guard.max_records = 10
        for uid in range(20):
            guard.check(-100, uid, "内容", now=1)
        self.assertLessEqual(len(guard.records), 10)
        for i in range(1000):
            guard.check(-100, 19, "内容", now=2)
        self.assertLessEqual(len(guard.records[(-100, 19)]), 512)

    def test_inactive_history_is_pruned_after_longest_window(self):
        guard = FloodGuard()
        guard.check(-100, 2, "内容", now=1)
        guard.check(-100, 3, "内容", now=302)
        self.assertNotIn((-100, 2), guard.records)

    def test_photo_replay_and_per_photo_memory_are_bounded(self):
        guard = PhotoRepeatGuard()
        msg = {"chat": {"id": -100}, "from": {"id": 2},
               "photo": [{"file_unique_id": "same"}], "message_id": 1}
        for i in range(10):
            self.assertIsNone(guard.check(msg, now=i))
        self.assertIsNone(guard.check({**msg, "message_id": 2}, now=11))
        self.assertEqual(guard.check({**msg, "message_id": 3}, now=12), "重复图片刷屏")
        for i in range(1000):
            guard.check({**msg, "message_id": 100 + i}, now=13)
        self.assertLessEqual(len(guard.records[(-100, 2, "same")]), 512)


class CommandModerationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeTelegramAPI()
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def msg(self, text, mid=1, uid=2):
        return {"text": text, "message_id": mid, "from": {"id": uid},
                "chat": {"id": -1001, "type": "supergroup", "title": "测试群"}}

    def deleted(self):
        return [p["message_id"] for method, p in self.api.calls if method == "deleteMessage"]

    def test_buy_and_transfer_cannot_hide_ads_even_when_points_disabled(self):
        for i, command in enumerate(("/buy https://spam.com", "/transfer https://spam.com"), 1):
            self.bot.handle_update({"message": self.msg(command, i)})
        self.assertEqual(self.deleted(), [1, 2])
        self.assertFalse(any(method == "sendMessage" for method, _ in self.api.calls))

    def test_hidden_link_in_valid_checkin_is_moderated_before_awarding_points(self):
        self.store.set(-1001, "points_enabled", True)
        msg = self.msg("/checkin")
        msg["entities"] = [{"type": "text_link", "url": "https://spam.com"}]
        self.bot.handle_update({"message": msg})
        self.assertEqual(self.deleted(), [1])
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 0)

    def test_disabled_and_throttled_commands_still_enter_repeat_filter(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.store.set(-1001, "points_enabled", enabled)
                uid = 2 if enabled else 3
                for mid in range(1, 4):
                    self.bot.handle_update({"message": self.msg("/points", 10 * uid + mid, uid)})
                self.assertIn(10 * uid + 3, self.deleted())

    def test_rotating_public_commands_enter_total_flood_count(self):
        self.store.set(-1001, "flood_limit", 3)
        for i, command in enumerate(("/points", "/checkin", "/shop", "/raffle"), 1):
            self.bot.handle_update({"message": self.msg(command, i)})
        self.assertEqual(self.deleted(), [4])

    def test_admin_commands_remain_exempt_and_operational(self):
        self.store.add_filter(-1001, "word", "广告", "广告")
        self.bot.handle_update({"message": self.msg("/setrules 广告 https://spam.com", uid=1)})
        self.assertEqual(self.deleted(), [])
        self.assertIn("https://spam.com", self.store.settings(-1001)["rules_text"])

    def test_filtered_command_delete_failure_never_executes_business_action(self):
        self.store.set(-1001, "points_enabled", True)
        self.store.add_filter(-1001, "word", "checkin", "checkin")
        original = self.api.safe
        self.api.safe = lambda method, **params: False if method == "deleteMessage" else original(method, **params)
        self.bot.handle_update({"message": self.msg("/checkin")})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 0)
        self.assertFalse(self.store.stats(-1001).get("deleted", 0))

    def test_edited_command_is_filtered_without_running_or_counting_it(self):
        self.store.set(-1001, "points_enabled", True)
        self.bot.handle_update({"edited_message": self.msg("/checkin")})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 0)
        self.assertEqual(len(self.bot.flood.records), 0)

    def test_first_auto_reply_works_immediately_after_system_boot(self):
        self.store.add_reply(-1001, "帮助", "帮助", "请看群公告")
        with patch("app.service.time.monotonic", return_value=5):
            self.bot.handle_update({"message": self.msg("需要帮助", 1)})
            self.bot.handle_update({"message": self.msg("需要帮助", 2)})
        replies = [p for method, p in self.api.calls if method == "sendMessage"]
        self.assertEqual([p["text"] for p in replies], ["请看群公告"])


if __name__ == "__main__":
    unittest.main()
