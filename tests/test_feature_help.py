"""Read-only feature guides exercised through actual private callback routing."""
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from app.feature_help import ADMIN_PAGES, DOCS_URL, GUIDES, MEMBER_PAGES
from app.service import GroupBot, MENU_MODULES
from app.store import NUMBER_LIMITS, Store
from app.word_library import catalog
from tests.test_service import FakeTelegramAPI


class FeatureHelpTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def click(self, data, uid=1, chat_id=None, private=True, mid=42):
        self.api.calls.clear()
        self.bot.member_ui.last.clear()
        self.bot.handle_update({"callback_query": {
            "id": "q", "from": {"id": uid}, "data": data,
            "message": {"message_id": mid, "chat": {
                "type": "private" if private else "supergroup",
                "id": uid if chat_id is None else chat_id}},
        }})

    def pages(self):
        return [p for method, p in self.api.calls if method in ("sendMessage", "editMessageText")]

    def page(self):
        return self.pages()[-1]

    def buttons(self):
        return [b for row in self.page()["reply_markup"]["inline_keyboard"] for b in row]

    def help_button(self):
        buttons = [b for b in self.buttons() if b["text"] in ("📖 功能说明", "📖 Feature guide")]
        self.assertEqual(len(buttons), 1)
        return buttons[0]["callback_data"]

    def back(self):
        return next(b["callback_data"] for b in self.buttons()
                    if b["text"] in ("⬅️ 返回本功能", "⬅️ Back to feature"))

    def assert_single_auth(self, uid):
        self.assertEqual(len([p for method, p in self.api.calls
                              if method == "getChatMember" and p["user_id"] == uid]), 1)

    def assert_no_action(self):
        self.assertFalse([m for m, _ in self.api.calls if m in (
            "deleteMessage", "deleteMessages", "banChatMember", "unbanChatMember",
            "restrictChatMember", "sendDocument", "setMyCommands")])

    def test_all_admin_modules_offer_help_and_one_fresh_auth(self):
        for _, page in MENU_MODULES:
            if page == "groups":
                continue
            with self.subTest(page=page):
                self.click(f"v:-1001:{page}")
                self.assert_single_auth(1)
                route = self.help_button()
                self.click(route)
                self.assertIn("功能说明", self.page()["text"])
                self.assert_single_auth(1)
                self.assertTrue(any(b.get("url", "").startswith(DOCS_URL) for b in self.buttons()))
                self.assert_no_action()
                self.click(self.back())
                self.help_button()
                self.assert_single_auth(1)

    def test_all_admin_help_and_return_routes_are_read_only(self):
        before = list(self.store.db.iterdump())
        for page in ADMIN_PAGES:
            with self.subTest(page=page):
                self.click(f"fh:-1001:{page}")
                self.assertIn("功能说明", self.page()["text"])
                self.assert_no_action()
                self.click(self.back())
                self.assertTrue(self.pages())
                self.assert_no_action()
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_current_group_parameters_and_threshold_examples_refresh(self):
        self.store.set(-1001, "mention_limit", 9)
        self.store.set(-1002, "mention_limit", 18)
        self.click("fh:-1001:spam")
        self.assertIn("超过9次提及，从第10次触发", self.page()["text"])
        self.store.set(-1001, "mention_limit", 12)
        self.click("fh:-1001:spam")
        self.assertIn("超过12次提及，从第13次触发", self.page()["text"])
        self.assertNotIn("超过18次", self.page()["text"])
        self.store.set(-1001, "flood_limit", 11)
        self.click("fh:-1001:flood")
        self.assertIn("超过11条，从第12条触发", self.page()["text"])

    def test_help_is_private_and_fresh_admin_permission_is_required(self):
        for kwargs in ({"uid": 2}, {"private": False, "chat_id": -1001}, {"chat_id": 2}):
            with self.subTest(kwargs=kwargs):
                self.click("fh:-1001:spam", **kwargs)
                self.assertFalse(self.pages())
                self.assert_no_action()
        self.click("fh:-1001:spam")
        with patch.object(self.bot, "is_admin", return_value=False):
            self.click("fh:-1001:spam")
        self.assertFalse(self.pages())

    def test_malformed_and_unapproved_topics_do_not_render_or_mutate(self):
        before = list(self.store.db.iterdump())
        for route in ("fh:-1001", "fh:-1001:spam:extra", "fh:invalid:spam",
                      "fh:-9999:spam", "fh:-1001:__dict__", "fh:-1001:memberpoints",
                      "pc:-1001:help:spam", "pc:-1001:help:shop:extra",
                      "lang:help:invalid", "lang:help:2"):
            with self.subTest(route=route):
                self.click(route)
                self.assertFalse(self.pages())
                self.assert_no_action()
                self.assertEqual(len([m for m, _ in self.api.calls if m == "answerCallbackQuery"]), 1)
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_group_language_help_uses_personal_locale_without_changing_either(self):
        self.store.set_ui_language(1, "en")
        self.store.set_ui_language(-1001, "zh-CN")
        before = list(self.store.db.iterdump())
        self.click("lang:help:-1001")
        self.assertIn("Interface language", self.page()["text"])
        self.assert_single_auth(1)
        self.assertEqual(self.back(), "lang:group:-1001")
        self.assert_no_action()
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.click("lang:help:-1001", uid=2)
        self.assertFalse(self.pages())
        self.click("lang:help:1")
        self.assertEqual(self.back(), "lang:self")
        self.assert_no_action()

    def test_every_member_page_has_help_including_empty_raffle(self):
        self.store.set(-1001, "points_enabled", True)
        for op in MEMBER_PAGES:
            with self.subTest(op=op):
                self.click(f"pc:-1001:{op}:0", uid=2)
                self.assert_single_auth(2)
                self.click(self.help_button(), uid=2)
                self.assertIn("功能说明", self.page()["text"])
                self.assert_single_auth(2)
                self.assert_no_action()

    def test_member_help_and_back_cannot_checkin_buy_or_join(self):
        self.store.set(-1001, "points_enabled", True)
        self.bot.economy.add_item(-1001, 1, "虚拟奖励", "人工发放", 5, 3)
        self.bot.raffles.create(-1001, 1, "免费抽奖")
        before = list(self.store.db.iterdump())
        for op in MEMBER_PAGES:
            with self.subTest(op=op):
                self.click(f"pc:-1001:help:{op}", uid=2)
                self.assert_no_action()
                back = self.back()
                self.assertNotIn(back.split(":")[2], ("checkin", "buy", "join"))
                self.click(back, uid=2)
                self.assert_no_action()
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_member_home_help_can_read_individual_guides_before_any_action(self):
        before = list(self.store.db.iterdump())
        self.click("pc:-1001:help:home", uid=2)
        links = [b["callback_data"] for b in self.buttons()
                 if b.get("callback_data", "").startswith("pc:-1001:help:")]
        self.assertEqual(len(links), 5)
        for route in links:
            self.click(route, uid=2)
            self.assertIn("功能说明", self.page()["text"])
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_member_help_rechecks_membership_verification_and_own_private_chat(self):
        self.click("pc:-1001:help:home", uid=2, chat_id=1)
        self.assertFalse(self.pages())
        self.click("pc:-1001:help:home", uid=2, private=False, chat_id=-1001)
        self.assertFalse(self.pages())
        self.store.put_pending(-1001, 2, "nonce", 1, int(time.time())+300)
        self.click("pc:-1001:help:home", uid=2)
        self.assertFalse(self.pages())
        with patch.object(self.api, "safe", return_value={"status": "left"}):
            self.click("pc:-1001:help:home", uid=3)
        self.assertFalse(self.pages())

    def test_reading_shop_help_invalidates_previous_purchase_confirmation(self):
        self.store.set(-1001, "points_enabled", True)
        self.store.checkin(-1001, 2)
        item = self.bot.economy.add_item(-1001, 1, "虚拟奖励", "人工发放", 5, 3)
        self.click(f"pc:-1001:quote:{item}", uid=2)
        buy = next(b["callback_data"] for b in self.buttons() if ":buy:" in b.get("callback_data", ""))
        before = list(self.store.db.iterdump())
        self.click(self.help_button(), uid=2)
        self.assertNotIn((-1001, 2), self.bot.member_ui.quotes)
        self.click(self.back(), uid=2)
        self.click(buy, uid=2)
        self.assertFalse(self.pages())
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_help_navigation_cancels_unconfirmed_admin_draft(self):
        self.click("ad:-1001:begin:domain_add")
        self.assertIn(1, self.bot.admin_ui.drafts)
        before = list(self.store.db.iterdump())
        self.click("fh:-1001:domains")
        self.assertNotIn(1, self.bot.admin_ui.drafts)
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_paginated_lists_word_details_packs_exports_and_task_details_keep_help(self):
        for n in range(10):
            self.store.add_filter(-1001, "domain", f"site{n}.example.com", f"site{n}.example.com")
            self.store.add_filter(-1001, "word", f"word{n}", f"word{n}")
        self.click("ad:-1001:domains:1")
        self.help_button()
        self.click("wl:-1001:1")
        self.help_button()
        detail = next(b["callback_data"] for b in self.buttons() if ":word:" in b.get("callback_data", ""))
        self.click(detail)
        self.help_button()
        self.click(f"ad:-1001:pack:{next(iter(catalog()))}")
        self.help_button()
        self.click("ad:-1001:wordexport:0")
        self.help_button()
        task = self.store.add_schedule(-1001, 1, "公告", "测试正文", "daily", "09:00", int(time.time())+86400)
        self.click(f"s:-1001:{task}")
        self.help_button()

    def test_chinese_and_english_guides_fit_telegram_and_keep_short_callbacks(self):
        cid = -1001234567890123
        self.store.ensure_chat(cid, "大ID群")
        for key, (_, maximum) in NUMBER_LIMITS.items():
            self.store.set(cid, key, maximum)
        for locale in ("zh-CN", "en"):
            for uid in (1, 2):
                self.store.set_ui_language(uid, locale)
            routes = [(1, f"fh:{cid}:{page}") for page in ADMIN_PAGES]
            routes += [(2, f"pc:{cid}:help:{page}") for page in MEMBER_PAGES]
            routes += [(1, "lang:help:1")]
            for uid, route in routes:
                with self.subTest(locale=locale, route=route):
                    self.click(route, uid=uid)
                    text = self.page()["text"]
                    self.assertLess(len(text.encode("utf-16-le"))//2, 4000)
                    self.assertIn("Feature guide" if locale == "en" else "功能说明", text)
                    self.assertNotIn("{mention_limit}", text)
                    for b in self.buttons():
                        if "callback_data" in b:
                            self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_online_guide_destinations_exist_in_source(self):
        root = Path(__file__).resolve().parents[1]/"docs"
        for _, destination in GUIDES.values():
            path = destination.split("#")[0].removesuffix(".html")+".md"
            with self.subTest(path=path):
                self.assertTrue((root/path).is_file())


if __name__ == "__main__":
    unittest.main()
