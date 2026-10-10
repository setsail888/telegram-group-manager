"""Recipient languages, real button workflows, literal custom content and migration."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.command_menu import install_commands
from app.i18n import CURRENT_LANGUAGE, LocalizedAPI, Text, catalog, error_text, join_text, msg, render
from app.service import GroupBot, MENU_MODULES
from app.store import Store, DEFAULTS
from app.word_library import catalog as word_catalog
from tests.test_service import FakeTelegramAPI


class TranslationTests(unittest.TestCase):
    def test_interpolated_user_content_is_always_literal(self):
        raw = "✅ 语言已保存。"
        text = msg("命中 {p0}{p1}（{p2}）", p0=raw, p1="", p2=msg("仅观察"))
        self.assertEqual(str(text), "命中 "+raw+"（仅观察）")
        self.assertEqual(render(text, "en"), "Matched "+raw+" (Observe only)")
        self.assertEqual(render(raw, "en"), raw)
        self.assertEqual(json.loads(json.dumps({"value": text}))["value"], str(text))

    def test_join_concat_slice_replace_format_and_errors(self):
        text = "!" + join_text(" | ", (msg("关闭"), msg("仅观察"))) + "."
        self.assertEqual(render(text, "en"), "!Off | Observe only.")
        self.assertEqual(render(text[:6], "en"), "!Off |")
        welcome = msg("欢迎 {name} 加入本群！请先阅读 /rules。")
        self.assertIn("张三", render(welcome.replace("{name}", "张三"), "en"))
        self.assertIn("张三", render(welcome.format(name="张三"), "en"))
        self.assertEqual(render(error_text(ValueError(msg("未知界面语言。"))), "en"), "Unknown interface language.")

    def test_catalog_and_placeholders_cover_all_sources(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("prepare_i18n", root/"scripts/prepare-i18n.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        inventory = module.inventory()
        self.assertEqual(json.loads((root/"app/locales/sources.json").read_text(encoding="utf-8")), inventory)
        translated = catalog("en")
        for source in inventory:
            with self.subTest(source=source):
                self.assertIn(source, translated)
                self.assertEqual(module.placeholders(source), module.placeholders(translated[source]))
                self.assertTrue(translated[source])

    def test_trusted_pack_names_translate_but_keywords_do_not(self):
        pack = word_catalog()["task_scam"]
        self.assertIsInstance(pack.name, Text)
        self.assertEqual(render(pack.name, "en"), "Fake-order and task scams")
        self.assertNotEqual(render(pack.description, "en"), pack.description)
        self.assertIn("刷单返佣", pack.words)
        self.assertNotIsInstance(pack.words[0], Text)


class LanguageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def click(self, data, uid=1, mid=42, private=True, **sender):
        self.bot.member_ui.last.clear()
        self.bot.handle_update({"callback_query": {"id": "q", "from": {"id": uid, **sender}, "data": data,
            "message": {"message_id": mid, "chat": {"type": "private" if private else "supergroup",
                                                     "id": uid if private else -1001}}}})

    def private(self, text, uid=1, **sender):
        self.bot.handle_update({"message": {"message_id": 20, "text": text, "from": {"id": uid, **sender},
            "chat": {"id": uid, "type": "private"}}})

    def group(self, text, uid=2, mid=1, **extra):
        self.bot.handle_update({"message": {"message_id": mid, "text": text, "from": {"id": uid},
            "chat": {"id": -1001, "type": "supergroup", "title": "测试群"}, **extra}})

    def page(self):
        return [p for m, p in self.api.calls if m in ("sendMessage", "editMessageText")][-1]

    def test_auto_english_then_manual_selection_persists(self):
        self.private("/start", language_code="en-US")
        self.assertEqual(self.store.ui_language(1), "en")
        self.assertIn("Choose", self.page()["text"])
        self.click("lang:set:1:zh-CN", language_code="en-US")
        self.assertEqual(self.store.ui_language(1), "zh-CN")
        self.private("/start", language_code="en")
        self.assertIn("选择", self.page()["text"])
        self.assertEqual(CURRENT_LANGUAGE.get(), "zh-CN")

    def test_missing_or_unsupported_client_language_falls_back_to_chinese(self):
        for uid, language in ((2, None), (3, "fr")):
            self.private("/start", uid=uid, **({"language_code": language} if language else {}))
            self.assertEqual(self.store.ui_language(uid), "zh-CN")
            self.assertIn("暂未", self.page()["text"])
            self.assertTrue(any(b.get("callback_data") == "lang:self"
                                for row in self.page()["reply_markup"]["inline_keyboard"] for b in row))

    def test_language_command_deep_link_and_nonadmin_entry(self):
        self.private("/language", uid=2)
        self.assertIn("界面语言", self.page()["text"])
        self.private("/start language", uid=2)
        self.assertIn("lang:set:2:en", json.dumps(self.page()["reply_markup"]))
        self.click("lang:self", uid=2)
        self.assertTrue(any(m == "answerCallbackQuery" for m, _ in self.api.calls))
        self.group("/language")
        self.assertEqual(self.page()["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://t.me/MyBot?start=language")

    def test_personal_and_group_preferences_are_independent(self):
        self.click("lang:set:1:en")
        self.assertEqual(self.store.ui_language(-1001), "zh-CN")
        self.click("lang:set:-1001:en")
        self.assertEqual(self.store.ui_language(-1001), "en")
        self.assertEqual(self.store.ui_language(-1002), "zh-CN")
        self.assertEqual(self.store.ui_language(2), "zh-CN")
        self.private("/start", uid=2)
        self.assertIn("暂未", self.page()["text"])
        self.group("/rules")
        self.assertIn("Be respectful", self.page()["text"])
        self.click("lang:set:-1001:zh-CN")
        self.assertEqual(self.store.ui_language(1), "en")
        self.assertIn("Interface language", self.page()["text"])

    def test_group_messages_do_not_create_personal_preferences(self):
        self.bot.handle_update({"message": {"message_id": 30, "from": {"id": 3, "language_code": "en"},
            "chat": {"type": "supergroup", "id": -1001}, "text": "hello"}})
        self.assertFalse(self.store.has_ui_language(3))
        self.assertEqual(self.store.ui_language(-1001), "zh-CN")

    def test_language_change_requires_own_private_chat_and_fresh_group_admin(self):
        for data, uid, private in (("lang:set:-1001:en", 2, True), ("lang:set:1:en", 2, True),
                                   ("lang:set:-1001:en", 1, False), ("lang:set:-999:en", 1, True),
                                   ("lang:set:-1001:xx", 1, True), ("lang:set:0:en", 1, True)):
            self.click(data, uid=uid, private=private)
            self.assertEqual(self.store.ui_language(-1001), "zh-CN")
            self.assertEqual(self.store.ui_language(1), "zh-CN")
        self.click("v:-1001:language")
        self.assertIn("本群系统提示", self.page()["text"])
        with patch.object(self.bot, "is_admin", return_value=False):
            self.click("lang:set:-1001:en")
        self.assertEqual(self.store.ui_language(-1001), "zh-CN")

    def test_all_management_pages_keep_callbacks_and_show_english_labels(self):
        self.store.set_ui_language(1, "en")
        self.click("g:-1001")
        self.assertIn("测试群", self.page()["text"])  # group name is user content
        for label, module in MENU_MODULES:
            if module == "groups":
                continue
            with self.subTest(module=module):
                self.click(f"v:-1001:{module}")
                page = self.page()
                self.assertNotIn("暂不可用", page["text"])
                self.assertNotIn("unavailable", page["text"].lower())
                self.assertTrue(page["text"])
                for row in page["reply_markup"]["inline_keyboard"]:
                    for button in row:
                        if "callback_data" in button:
                            self.assertLessEqual(len(button["callback_data"].encode()), 64)
                        if module != "language":
                            self.assertFalse(any('\u3400' <= c <= '\u9fff' for c in button["text"]), button)

    def test_confirming_an_english_preview_preserves_canonical_modes(self):
        self.store.set_ui_language(1, "en")
        self.click("ad:-1001:variantmode:pinyin_enforce")
        draft = dict(self.bot.admin_ui.drafts[1])
        self.assertIn("preview", self.page()["text"].lower())
        self.click("lang:set:1:zh-CN")
        self.click(f"ac:-1001:{draft['nonce']}", mid=draft["prompt_id"])
        self.assertEqual(self.store.variant_modes(-1001)["pinyin"], "enforce")
        self.assertIn("已", self.page()["text"])
        self.click(f"ac:-1001:{draft['nonce']}", mid=draft["prompt_id"])
        self.assertEqual(self.store.variant_modes(-1001)["pinyin"], "enforce")

    def test_translated_long_preview_is_complete_and_only_last_part_can_confirm(self):
        self.store.set_ui_language(1, "en")
        summary = join_text(" | ", [msg("处罚失败：群类型、权限或目标身份不满足条件")]*80) + " FINAL_SENTINEL"
        draft = self.bot.admin_ui._draft(1, -1001, "rules_text", "welcome")
        self.bot.admin_ui._preview(1, draft, summary, 42)
        messages = [p for m, p in self.api.calls if m in ("sendMessage", "editMessageText")]
        self.assertGreater(len(messages), 1)
        complete = "".join(p["text"] for p in messages)
        self.assertIn(render(summary, "en"), complete)
        self.assertTrue(all(len(p["text"]) <= 3500 for p in messages))
        self.assertTrue(all("reply_markup" not in p for p in messages[:-1]))
        self.assertEqual(messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         f"ac:-1001:{draft['nonce']}")
    def test_custom_content_equal_to_a_translation_source_is_not_translated(self):
        self.store.set_ui_language(-1001, "en")
        self.store.set_ui_language(2, "en")
        for key in ("rules_text", "welcome_text"):
            self.store.set(-1001, key, str(DEFAULTS[key]))
        self.group("/rules")
        self.assertIn(str(DEFAULTS["rules_text"]), self.page()["text"])
        self.group("", mid=2, new_chat_members=[{"id": 3, "first_name": "✅ 语言已保存。"}])
        self.assertIn("欢迎 ✅ 语言已保存。", self.page()["text"])
        self.click("pc:-1001:rules:0", uid=2)
        self.assertIn(str(DEFAULTS["rules_text"]), self.page()["text"])

    def test_untouched_welcome_and_rules_translate_including_user_name(self):
        self.store.set_ui_language(-1001, "en")
        self.group("/rules")
        self.assertIn("Be respectful", self.page()["text"])
        self.group("", mid=2, new_chat_members=[{"id": 3, "first_name": "张三"}])
        self.assertIn("Welcome 张三!", self.page()["text"])
        self.store.set_ui_language(1, "en")
        self.click("v:-1001:welcome")
        self.assertIn("Welcome preview: Welcome {name}!", self.page()["text"])
        self.assertIn("Rules preview: Be respectful", self.page()["text"])

    def test_validation_errors_translate_and_do_not_save_invalid_input(self):
        self.store.set_ui_language(1, "en")
        self.click("ad:-1001:begin:rules_text")
        self.private("x"*1501)
        self.assertIn("1500", self.page()["text"])
        self.assertEqual(self.store.settings(-1001)["rules_text"], DEFAULTS["rules_text"])

    def test_stored_audit_metadata_translates_without_changing_keyword(self):
        self.store.set_ui_language(1, "en")
        self.store.audit(-1001, 11, 2, "跨账号重复刷屏", "deleted", rule_key="接码平台",
                         match_kind="繁简字形／拼音＋中文混写")
        self.store.audit_penalty(-1001, 11, 2, "warn", "已警告 1/3")
        self.click("v:-1001:audit")
        page = self.page()["text"]
        self.assertIn("Cross-account", page)
        self.assertIn("Mixed pinyin", page)
        self.assertIn("Warned 1/3", page)
        self.assertIn("接码平台", page)
        self.assertEqual(self.store.audit_page(-1001)[0][0]["reason"], "跨账号重复刷屏")

    def test_private_member_services_use_personal_language(self):
        self.store.set_ui_language(2, "en")
        self.store.set(-1001, "points_enabled", True)
        for operation, fragment in (("home", "Member"), ("balance", "Points"), ("checkin", "points"),
                                    ("rules", "Rules"), ("shop", "reward"), ("orders", "order")):
            self.click(f"pc:-1001:{operation}:0", uid=2)
            self.assertIn(fragment.lower(), self.page()["text"].lower())
        self.assertEqual(self.store.point_balance(-1001, 2)["checkins"], 1)

    def test_output_limits_and_transport_payloads_are_preserved(self):
        self.store.set_ui_language(1, "en")
        text = join_text("", [msg("关闭")]*3000)
        markup = {"inline_keyboard": [[{"text": msg("关闭"), "callback_data": "w:-1001:关闭"},
                                     {"text": "关闭", "url": "https://example.com/关闭"}]]}
        self.bot.api.safe("sendPhoto", chat_id=1, photo="photo_关闭", caption=text, reply_markup=markup)
        method, params = self.api.calls[-1]
        self.assertEqual(len(params["caption"]), 1024)
        self.assertEqual(params["photo"], "photo_关闭")
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][0]["text"], "Off")
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "w:-1001:关闭")
        self.assertEqual(params["reply_markup"]["inline_keyboard"][0][1], markup["inline_keyboard"][0][1])
        self.bot.send(1, text)
        self.assertEqual(len(self.page()["text"]), 4000)

    def test_callback_background_is_rendered_before_worker_submission(self):
        class BackgroundAPI(FakeTelegramAPI):
            def answer_background(self, **params):
                self.calls.append(("background", params))
                return True
        raw = BackgroundAPI()
        self.bot.api = LocalizedAPI(raw, self.store)
        self.store.set_ui_language(1, "en")
        self.click("lang:set:1:en")
        params = [p for m, p in raw.calls if m == "background"][-1]
        self.assertEqual(params["text"], "✅ Language saved.")
        self.assertIs(type(params["text"]), str)
        self.assertEqual(CURRENT_LANGUAGE.get(), "zh-CN")

    def test_native_command_list_has_same_identifiers_in_both_languages(self):
        install_commands(self.api, "zh-CN")
        zh = self.api.calls[-1][1]
        install_commands(self.api, "en", language_code="en")
        en = self.api.calls[-1][1]
        self.assertEqual([v["command"] for v in zh["commands"]], [v["command"] for v in en["commands"]])
        self.assertIn("language", [v["command"] for v in en["commands"]])
        self.assertTrue(all(1 <= len(v["description"]) <= 100 for v in en["commands"]))
        self.assertTrue(all(not any('\u3400' <= c <= '\u9fff' for c in v["description"]) for v in en["commands"]))
        self.click("lang:set:1:en")
        self.assertEqual([p for m, p in self.api.calls if m == "setMyCommands"][-1]["scope"],
                         {"type": "chat", "chat_id": 1})

    def test_language_and_mixed_mode_survive_reopen_and_legacy_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.db")
            store = Store(path)
            store.ensure_chat(-1001, "旧群")
            store.db.execute("UPDATE chats SET settings=?", (json.dumps({"rules_text": "旧的自定义群规"}),))
            self.assertEqual(store.ui_language(-1001), "zh-CN")
            self.assertEqual(store.variant_modes(-1001)["pinyin"], "off")
            self.assertEqual(render(store.content_text(-1001, "rules_text"), "en"), "旧的自定义群规")
            store.set_ui_language(1, "en")
            store.set_ui_language(-1001, "en")
            store.set_variant_modes(-1001, {"pinyin": "observe"})
            store.close()
            reopened = Store(path)
            try:
                self.assertEqual(reopened.ui_language(1), "en")
                self.assertEqual(reopened.ui_language(-1001), "en")
                self.assertEqual(reopened.variant_modes(-1001)["pinyin"], "observe")
                self.assertEqual(reopened.settings(-1001)["rules_text"], "旧的自定义群规")
            finally:
                reopened.close()
