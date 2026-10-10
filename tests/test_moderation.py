import unittest

from app.moderation import (
    FloodGuard, PhotoRepeatGuard, allowed, evaluate, extract_domains,
    normalize, normalize_domain, compact, match_words)


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.defaults = {"block_links": True, "block_forward": False, "anti_flood": False}

    def test_zero_width_and_full_width_bypass(self):
        msg = {"text": "加\u200b微\u200b信，ＶＩＰ 服务"}
        self.assertEqual(normalize("ＶＩＰ"), "vip")
        self.assertEqual(evaluate(msg, self.defaults, ["加微信"], [], None), "违禁词")

    def test_links_and_hidden_links(self):
        self.assertEqual(extract_domains({"text": "欢迎 http://spam.example.com/a"}), ["spam.example.com"])
        self.assertEqual(extract_domains({"text": "点击这里",
                                          "entities": [{"type": "text_link", "url": "https://bad.com"}]}),
                         ["bad.com"])
        self.assertEqual(evaluate({"text": "看 t.me/abc"}, self.defaults, [], [], None),
                         "外部链接")

    def test_whitelisted_subdomains_only(self):
        self.assertTrue(allowed("cdn.example.com", ["example.com"]))
        self.assertFalse(allowed("badexample.com", ["example.com"]))
        self.assertEqual(normalize_domain("HTTPS://EXAMPLE.COM/path"), "example.com")
        self.assertIsNone(evaluate({"text": "https://cdn.example.com/a"}, self.defaults,
                                    [], ["example.com"]))

    def test_forward_switch(self):
        msg = {"text": "普通消息", "forward_origin": {"type": "user"}}
        self.assertIsNone(evaluate(msg, self.defaults, [], []))
        self.assertEqual(evaluate(msg, {**self.defaults, "block_forward": True}, [], []),
                         "禁止转发")

    def test_malformed_explicit_link_is_not_allowed(self):
        msg = {"text": "访问 https://bad..domain.example/abc"}
        self.assertEqual(evaluate(msg, self.defaults, [], ["example.com"]),
                         "外部链接")

    def test_burst_and_repetition(self):
        guard = FloodGuard(limit=5, window=10, repeat_limit=3, repeat_window=30)
        self.assertIsNone(guard.check(-100, 7, "内容", now=1))
        self.assertIsNone(guard.check(-100, 7, "内容", now=2))
        self.assertEqual(guard.check(-100, 7, "内容", now=3), "重复刷屏")
        self.assertIsNone(guard.check(-100, 8, "内容", now=3))
        self.assertIsNone(guard.check(-100, 7, "不同", now=100))

    def test_repeat_photo_by_sender_and_group_only(self):
        guard = PhotoRepeatGuard()
        item = {"photo": [{"file_unique_id": "abc"}],
                "from": {"id": 7}, "chat": {"id": -100}}
        self.assertIsNone(guard.check(item, limit=3, window=60, now=100))
        self.assertIsNone(guard.check(item, limit=3, window=60, now=110))
        other_user = {**item, "from": {"id": 8}}
        other_group = {**item, "chat": {"id": -101}}
        different_photo = {**item, "photo": [{"file_unique_id": "other"}]}
        self.assertIsNone(guard.check(other_user, now=120))
        self.assertIsNone(guard.check(other_group, now=120))
        self.assertIsNone(guard.check(different_photo, now=120))
        self.assertEqual(guard.check(item, limit=3, window=60, now=120),
                         "重复图片刷屏")
        self.assertIsNone(guard.check(item, limit=3, window=60, now=1000))

    def test_photo_without_metadata_is_ignored(self):
        guard = PhotoRepeatGuard()
        item = {"photo": [{"file_id": "not-unique"}],
                "from": {"id": 7}, "chat": {"id": -100}}
        for _ in range(5):
            self.assertIsNone(guard.check(item))

    def test_loosely_split_word_requires_explicit_opt_in(self):
        msg = {"text": "请加-微-信-联系"}
        self.assertIsNone(evaluate(msg, {**self.defaults, "block_links": False},
                                   ["加微信联系"], []))
        self.assertEqual(evaluate(
            msg, {**self.defaults, "block_links": False, "word_loose_match": True},
            ["加微信联系"], []), "违禁词")
        self.assertEqual(compact("Ｖ．Ｉ．Ｐ"), "vip")
        # Common two-character phrases are intentionally not loose-matched.
        self.assertIsNone(match_words("可-爱", ["可爱"], loose=True))

    def test_word_exception_only_waives_own_keyword_and_exact_message(self):
        self.assertIsNone(match_words(
            "学校广告设计", ["广告"], exceptions=["广告|学校广告设计"]))
        self.assertEqual(match_words(
            "学校广告设计 私聊加微信", ["广告"],
            exceptions=["广告|学校广告设计"]), "广告")
        self.assertEqual(match_words(
            "学校广告设计", ["广告", "学校"],
            exceptions=["广告|学校广告设计"]), "学校")

    def test_common_obfuscated_telegram_urls_opt_in(self):
        base = {**self.defaults, "spam_guard_enabled": True}
        self.assertEqual(evaluate({"text": "访问 t[.]me/abc"}, base, [], []),
                         "伪装链接")
        self.assertEqual(evaluate({"text": "访问 t . me/abc"}, base, [], []),
                         "伪装链接")
        self.assertIsNone(evaluate(
            {"text": "访问 t[.]me/abc"}, {**base, "spam_guard_enabled": False}, [], []))
        self.assertIsNone(evaluate(
            {"text": "访问 t[.]me/abc"}, {**base, "block_links": False}, [], []))

    def test_bulk_mentions_opt_in_and_configurable(self):
        content = " ".join(f"@person{i:02d}" for i in range(7))
        self.assertEqual(evaluate(
            {"text": content},
            {**self.defaults, "block_links": False, "spam_guard_enabled": True,
             "mention_limit": 5}, [], []), "批量提及刷屏")
        self.assertIsNone(evaluate(
            {"text": content},
            {**self.defaults, "block_links": False, "spam_guard_enabled": False},
            [], []))

    def test_media_storm_can_be_enabled_without_impacting_other_senders(self):
        guard = FloodGuard()
        conf = {**self.defaults, "block_links": False, "anti_flood": True,
                "flood_limit": 15, "flood_window": 30,
                "media_flood_enabled": True, "media_flood_limit": 2,
                "media_flood_window": 20}
        for i in range(2):
            item = {"chat": {"id": -100}, "from": {"id": 8},
                    "photo": [{"file_unique_id": str(i)}]}
            self.assertIsNone(evaluate(item, conf, [], [], guard))
        item = {"chat": {"id": -100}, "from": {"id": 8},
                "sticker": {"file_unique_id": "stamp"}}
        self.assertEqual(evaluate(item, conf, [], [], guard), "媒体刷屏")
        self.assertIsNone(evaluate(
            {**item, "from": {"id": 9}}, conf, [], [], guard))

    def test_edits_do_not_increase_flood_counters(self):
        guard = FloodGuard(limit=2, window=60, repeat_limit=5)
        msg = {"chat": {"id": -100}, "from": {"id": 8}, "text": "安全消息"}
        self.assertIsNone(evaluate(msg, {**self.defaults, "anti_flood": True,
                                          "flood_limit": 2, "flood_window": 60},
                                   [], [], guard))
        for _ in range(10):
            self.assertIsNone(evaluate(msg, {**self.defaults, "anti_flood": True},
                                       [], [], guard, is_edit=True))
        self.assertEqual(len(guard.records[(-100, 8)]), 1)


if __name__ == "__main__":
    unittest.main()
