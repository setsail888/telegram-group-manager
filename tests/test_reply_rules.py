import os
import sqlite3
import tempfile
import unittest

from app.reply_rules import media_from_reply, parse_buttons, parse_keywords
from app.store import Store


class ReplyParserTests(unittest.TestCase):
    def test_multiple_keywords_same_identity_regardless_of_order(self):
        left, label = parse_keywords("教程,  帮助、问题")
        right, _ = parse_keywords("问题,教程,帮助")
        self.assertEqual(left, right)
        self.assertEqual(label, "教程、帮助、问题")

    def test_reject_empty_repeated_and_excess_keywords(self):
        for value in ("", "帮助,帮助", "太", ",".join(f"关键词{i}" for i in range(9))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_keywords(value)

    def test_multiple_safe_url_buttons(self):
        links = parse_buttons(
            "官网 - https://example.com/path && 电报 - t.me/example")
        self.assertEqual(links, [
            {"text": "官网", "url": "https://example.com/path"},
            {"text": "电报", "url": "https://t.me/example"},
        ])

    def test_reject_unsafe_link_buttons(self):
        for value in ("无效", "网站 - javascript:alert(1)",
                      "网站 - http://example.com",
                      "网站 - https://user:pass@example.com",
                      "网站 - https://example.com:99999",
                      "网站 - https://example.com && 二 - https://example.com && "
                      "三 - https://example.com && 四 - https://example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_buttons(value)

    def test_reuse_existing_telegram_photo(self):
        media = {"reply_to_message": {
            "photo": [{"file_id": "small"}, {"file_id": "large"}]}}
        self.assertEqual(media_from_reply(media), ("photo", "large"))
        self.assertEqual(media_from_reply({"reply_to_message": {}}), (None, None))


class ExistingDatabaseMigrationTests(unittest.TestCase):
    def test_old_reply_schema_migrates_without_losing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "legacy.sqlite")
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE replies (
                  chat_id INTEGER NOT NULL,
                  key TEXT NOT NULL,
                  keyword TEXT NOT NULL,
                  reply TEXT NOT NULL,
                  PRIMARY KEY(chat_id,key)
                );
                INSERT INTO replies VALUES (-100,'帮助','帮助','旧版回复仍可用');
            """)
            db.close()
            store = Store(path)
            try:
                legacy = store.replies(-100)[0]
                self.assertEqual(legacy["reply"], "旧版回复仍可用")
                self.assertEqual(legacy["buttons"], "[]")
                self.assertIsNone(legacy["media_type"])
                store.add_reply(-100, "教程", "教程", "请看教程",
                                buttons=[{"text": "官网", "url": "https://example.com"}],
                                media_type="photo", media_id="telegram-test-file-id")
                rules = store.replies(-100)
                self.assertEqual(len(rules), 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
