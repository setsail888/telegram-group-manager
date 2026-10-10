"""Control-panel language selection, complete locale bundles and scope isolation."""
from collections import Counter
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from app.command_menu import COMMAND_MENU, install_commands
from app.feature_help import GUIDES
from app.i18n import LOCALES, catalog, msg, normalize_locale, render
from app.service import GroupBot, MENU_MODULES
from app.store import DEFAULTS, Store
from tests.test_service import FakeTelegramAPI


class PanelLanguageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一个群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def click(self, data, uid=1, private=True, chat_id=None):
        self.api.calls.clear()
        self.bot.member_ui.last.clear()
        self.bot.handle_update({"callback_query": {
            "id": "q", "from": {"id": uid}, "data": data,
            "message": {"message_id": 42, "chat": {
                "type": "private" if private else "supergroup",
                "id": uid if chat_id is None else chat_id}},
        }})

    def pages(self):
        return [p for method, p in self.api.calls if method in ("sendMessage", "editMessageText")]

    def page(self):
        return self.pages()[-1]

    def buttons(self):
        return [b for row in self.page()["reply_markup"]["inline_keyboard"] for b in row]

    def auth_count(self):
        return len([m for m, p in self.api.calls if m == "getChatMember" and p["user_id"] == 1])

    def test_panel_language_button_opens_personal_two_column_picker(self):
        self.click("g:-1001")
        language = next(b for b in self.buttons() if b.get("callback_data") == "v:-1001:language")
        self.assertEqual(language["text"], "🌐 语言 / Language")
        before = list(self.store.db.iterdump())
        self.click(language["callback_data"])
        self.assertIn("选择控制面板语言", self.page()["text"])
        rows = self.page()["reply_markup"]["inline_keyboard"]
        self.assertEqual([len(row) for row in rows[:13]], [2]*13)
        routes = {b["callback_data"] for b in self.buttons()}
        self.assertTrue({f"lang:pick:-1001:{code}" for code in LOCALES} <= routes)
        self.assertIn("lang:group:-1001", routes)
        self.assertIn("g:-1001", routes)
        self.assertEqual(self.auth_count(), 1)
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_english_selection_immediately_refreshes_same_group_panel(self):
        before = dict(self.store.settings(-1001))
        self.click("lang:pick:-1001:en")
        self.assertEqual(self.store.ui_language(1), "en")
        self.assertIn("Settings", self.page()["text"])
        self.assertIn("测试群", self.page()["text"])
        self.assertEqual(self.page()["message_id"], 42)
        self.assertTrue(any(b.get("callback_data") == "v:-1001:words" for b in self.buttons()))
        self.assertEqual(self.auth_count(), 1)
        self.assertEqual(self.store.settings(-1001), before)
        for owner in (-1001, -1002, 2):
            self.assertEqual(self.store.ui_language(owner), "zh-CN")
        self.click("v:-1001:language")
        checked = [b for b in self.buttons() if b["text"].startswith("✅ ")]
        self.assertEqual([b["callback_data"] for b in checked], ["lang:pick:-1001:en"])
        self.click("lang:pick:-1001:zh-CN")
        self.assertIn("请选择要更改的项目", self.page()["text"])

    def test_every_language_switches_panel_subpages_and_member_service(self):
        for locale in LOCALES:
            with self.subTest(locale=locale):
                self.click(f"lang:pick:-1001:{locale}")
                self.assertEqual(self.store.ui_language(1), locale)
                self.assertEqual(self.auth_count(), 1)
                for label, module in MENU_MODULES:
                    if module != "groups":
                        button = next(b for b in self.buttons() if b.get("callback_data") == f"v:-1001:{module}")
                        self.assertEqual(button["text"], render(label, locale))
                self.click("v:-1001:words")
                self.assertIn(render(msg("📖 功能说明"), locale), [b["text"] for b in self.buttons()])
                self.click("pc:-1001:home:0")
                balance = next(b for b in self.buttons() if b.get("callback_data") == "pc:-1001:balance:0")
                self.assertEqual(balance["text"], render(msg("🪙 积分"), locale))

    def test_group_notices_have_a_separate_admin_entry(self):
        self.click("lang:pick:-1001:en")
        self.click("lang:panel:-1001")
        group = next(b["callback_data"] for b in self.buttons() if b.get("callback_data") == "lang:group:-1001")
        self.click(group)
        self.assertIn("Group notice language", self.page()["text"])
        self.assertEqual(self.auth_count(), 1)
        self.assertTrue(any(b.get("callback_data") == "lang:set:-1001:es" for b in self.buttons()))
        self.click("lang:set:-1001:es")
        self.assertEqual(self.store.ui_language(-1001), "es")
        self.assertEqual(self.store.ui_language(1), "en")
        self.assertEqual(self.store.ui_language(-1002), "zh-CN")
        self.click("lang:panel:-1001")
        self.assertIn("control panel", self.page()["text"])

    def test_language_help_returns_to_same_panel_picker(self):
        before = list(self.store.db.iterdump())
        self.click("lang:info:-1001")
        self.assertIn("共26种", self.page()["text"])
        self.assertTrue(any(b.get("callback_data") == "lang:panel:-1001" for b in self.buttons()))
        self.assertEqual(self.auth_count(), 1)
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_personal_picker_works_without_group_admin_permission(self):
        self.click("lang:self", uid=2)
        self.assertTrue(any(b.get("callback_data") == "lang:set:2:fr" for b in self.buttons()))
        self.click("lang:set:2:fr", uid=2)
        self.assertEqual(self.store.ui_language(2), "fr")
        self.assertEqual(self.store.ui_language(1), "zh-CN")
        self.assertEqual(self.store.ui_language(-1001), "zh-CN")

    def test_forged_and_stale_context_routes_cannot_change_preferences(self):
        before = list(self.store.db.iterdump())
        for route, kwargs in (
            ("lang:pick:-1001:en", {"uid": 2}),
            ("lang:pick:-1001:en", {"private": False, "chat_id": -1001}),
            ("lang:pick:-1001:en", {"chat_id": 2}),
            ("lang:pick:-9999:en", {}), ("lang:pick:1:en", {}),
            ("lang:pick:-1001:xx", {}), ("lang:pick:-1001:../../en", {}),
            ("lang:pick:-1001:en:extra", {}), ("lang:panel:0", {}),
            ("lang:info:-1001", {"uid": 2}),
            ("lang:panel:-999999999999999999999", {}),
        ):
            with self.subTest(route=route, kwargs=kwargs):
                self.click(route, **kwargs)
                self.assertFalse(self.pages())
                self.assertEqual(list(self.store.db.iterdump()), before)
        self.click("lang:panel:-1001")
        with patch.object(self.bot, "is_admin", return_value=False):
            self.click("lang:pick:-1001:en")
        self.assertFalse(self.pages())
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_callback_payloads_and_output_lengths_for_every_locale(self):
        cid = -1001234567890123
        self.store.ensure_chat(cid, "大ID群")
        for locale in LOCALES:
            self.store.set_ui_language(1, locale)
            self.click(f"lang:panel:{cid}")
            self.assertLess(len(self.page()["text"].encode("utf-16-le"))//2, 4000)
            for b in self.buttons():
                if "callback_data" in b:
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_new_locale_preferences_survive_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/"languages.db")
            saved = Store(path)
            for n, locale in enumerate(LOCALES, 1):
                saved.set_ui_language(n, locale)
            saved.close()
            reopened = Store(path)
            try:
                for n, locale in enumerate(LOCALES, 1):
                    self.assertEqual(reopened.ui_language(n), locale)
            finally:
                reopened.close()


class LocaleDataTests(unittest.TestCase):
    def test_client_language_normalization(self):
        for raw, expected in (("fr-FR", "fr"), ("pt-BR", "pt"), ("fa_IR", "fa"),
                              ("en-US", "en"), ("uz-Cyrl", "uz"), ("in-ID", "id"),
                              (None, "zh-CN"), ("ja", "zh-CN"), ("zh-TW", "zh-CN")):
            self.assertEqual(normalize_locale(raw), expected)

    def test_custom_content_is_literal_in_every_language(self):
        for locale in LOCALES:
            raw = "测试群 /words {p0} ✅ 语言已保存。"
            translated = render(msg("命中 {p0}{p1}（{p2}）", p0=raw, p1="", p2=msg("仅观察")), locale)
            self.assertIn(raw, translated)
            self.assertNotIn("__GG_", translated)

    def test_guide_formats_and_command_descriptions_for_every_locale(self):
        values = dict(DEFAULTS, mention_trigger=6, flood_trigger=8, media_trigger=6)
        api = FakeTelegramAPI()
        for locale in LOCALES:
            for topic, (body, _) in GUIDES.items():
                with self.subTest(locale=locale, topic=topic):
                    rendered = render(body.format(**values), locale)
                    self.assertTrue(rendered)
                    self.assertNotIn("__GG_", rendered)
                    self.assertLess(len(rendered.encode("utf-16-le"))//2, 4000)
            install_commands(api, locale, chat_id=1)
            commands = api.calls[-1][1]["commands"]
            self.assertEqual([c["command"] for c in commands], [key for key, _ in COMMAND_MENU])
            self.assertTrue(all(1 <= len(c["description"]) <= 256 for c in commands))

    def test_static_command_arguments_and_chinese_examples_are_preserved(self):
        english = catalog("en")
        commands = re.compile(r"/(?:setlog\s+off|ocr\s+(?:review|off)|schedule\s+(?:daily|weekly|once))\b")
        for locale in LOCALES:
            if locale in ("zh-CN", "en"):
                continue
            for source, text in catalog(locale).items():
                with self.subTest(locale=locale, source=source[:30]):
                    self.assertNotIn("__GG_", text)
                    slash_commands = r"(?<![A-Za-z0-9])/[a-z][a-z0-9_]*"
                    expected = Counter(re.findall(slash_commands, english[source]))
                    actual = Counter(re.findall(slash_commands, text))
                    self.assertFalse(expected-actual)
                    for literal in commands.findall(english[source]):
                        self.assertIn(literal, text)
                    for word in re.findall(r"[A-Za-z]*[\u3400-\u9fff][\u3400-\u9fffA-Za-z·⭐]*", english[source]):
                        self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
