import unittest

from app.moderation import (
    FloodGuard, PhotoRepeatGuard, allowed, evaluate, extract_domains,
    normalize, normalize_domain)


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


if __name__ == "__main__":
    unittest.main()
