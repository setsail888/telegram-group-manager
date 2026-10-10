"""Opt-in Chinese coverage, real routing, conservative modes and source integrity."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from app.moderation import compile_words, normalize
from app.service import GroupBot
from app.store import Store
from app.variants import AliasIndex, VariantIndex, context_matches, join_short_gaps, simplify
from app.word_library import ROOT, catalog
from tests.test_service import FakeTelegramAPI


class VariantFixture(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def add(self, *words):
        for word in words:
            self.store.add_filter(-1001, "word", normalize(word), word)

    def modes(self, **changes):
        self.store.set_variant_modes(-1001, changes)

    def hit(self, text, **extra):
        settings = self.store.settings(-1001)
        return self.store.word_policy(-1001).hit({"text": text, **extra}, settings=settings,
                                                loose=settings["word_loose_match"])

    def group(self, text, mid=1, uid=2, edit=False, **extra):
        self.bot.handle_update({"edited_message" if edit else "message": {
            "message_id": mid, "text": text, "from": {"id": uid},
            "chat": {"id": -1001, "type": "supergroup"}, **extra}})

    def click(self, data, uid=1, mid=42):
        self.bot.handle_update({"callback_query": {"id": "q", "from": {"id": uid}, "data": data,
            "message": {"message_id": mid, "chat": {"id": uid, "type": "private"}}}})

    def private(self, text):
        self.bot.handle_update({"message": {"message_id": 20, "text": text, "from": {"id": 1},
            "chat": {"id": 1, "type": "private"}}})

    def confirm(self):
        d = dict(self.bot.admin_ui.drafts[1])
        self.click(f"ac:{d['cid']}:{d['nonce']}", mid=d["prompt_id"])
        return d

    def page(self):
        return [p for m, p in self.api.calls if m in ("sendMessage", "editMessageText")][-1]

    def deletes(self):
        return [p["message_id"] for m, p in self.api.calls if m == "deleteMessage"]


class ChineseCoverageTests(VariantFixture):
    def test_short_pack_default_off_and_literal_substrings_already_work(self):
        pack = catalog()["short_words"]
        self.assertEqual(len(pack.words), 73)
        self.assertTrue(all(len(w) == 2 for w in pack.words))
        self.assertFalse(pack.recommended)
        self.assertIsNone(self.hit("刷单"))
        self.add("刷单")
        self.assertEqual(self.hit("警惕刷单骗局")["word"], "刷单")
        self.assertIsNone(self.hit("刷⭐单"))
        self.store.set_pack_modes(-1001, {"short_words": "observe"})
        self.assertTrue(self.hit("赌博")["observe"])
        self.assertIsNone(self.hit("賭博"))

    def test_traditional_and_mixed_script_bidirectional_rules(self):
        self.add("刷单返佣", "联系微信", "赌博")
        self.modes(traditional="enforce")
        for text, word in (("刷單返佣", "刷单返佣"), ("聯繫微信", "联系微信"), ("賭博", "赌博")):
            with self.subTest(text=text):
                hit = self.hit(text)
                self.assertEqual(hit["word"], word)
                self.assertEqual(hit["match_kind"], "繁简字形")
        self.add("詐騙")
        self.assertEqual(self.hit("诈骗")["word"], "詐騙")

    def test_directional_homophone_pinyin_and_normalization(self):
        self.add("刷单返佣", "洗钱", "黑u")
        self.modes(homophone="enforce")
        for text, word in (("唰单返佣", "刷单返佣"), ("唰丹返拥", "刷单返佣"),
                           ("SHUADAN返佣", "刷单返佣"), ("shua dan返佣", "刷单返佣"),
                           ("刷ｄａｎ返佣", "刷单返佣"), ("洗💰", "洗钱"), ("出售黑优", "黑u")):
            with self.subTest(text=text):
                hit = self.hit(text)
                self.assertEqual(hit["word"], word)
                self.assertIn("同音", hit["match_kind"])

    def test_contextual_alias_positive_and_normal_chat_negative(self):
        self.add("博彩", "微信", "开盒", "洗钱", "代考", "接码")
        self.modes(homophone="enforce")
        for text, word in (("菠菜平台", "博彩"), ("平台波菜首充", "博彩"), ("联系wx123", "微信"),
                           ("查人开合", "开盒"), ("跑分洗前", "洗钱"), ("带考包过", "代考"), ("出售结码", "接码")):
            with self.subTest(text=text):
                self.assertEqual(self.hit(text)["word"], word)
        for text in ("菠菜很好吃", "博采众长", "老师很有威信", "门窗开合顺畅", "衣服洗前浸泡",
                     "带考生参观", "wxWidgets客服开发", "客服mywxvalue", "shuadanloader", "myshuadan", "今天比赛跑分数"):
            with self.subTest(text=text):
                self.assertIsNone(self.hit(text))

    def test_context_does_not_cross_clause_newline_or_24_character_window(self):
        self.add("博彩")
        self.modes(homophone="enforce")
        for text in ("菠菜\n平台", "菠菜。平台", "菠菜；平台", "菠菜！平台", "菠菜"+"好"*25+"平台"):
            self.assertIsNone(self.hit(text), text)
        self.assertEqual(self.hit("菠菜，平台")["word"], "博彩")
        self.assertIsNone(self.hit("菠菜", reply_markup={"inline_keyboard": [[{"text": "平台"}]]}))

    def test_short_split_limits_and_separate_button_parts(self):
        self.add("刷单")
        self.modes(split="enforce")
        for text in ("刷 单", "刷⭐单", "刷*_*单", "刷\u0301单"):
            self.assertEqual(self.hit(text)["word"], "刷单", text)
        for text in ("刷\n单", "刷\r单", "刷****单", "刷A单", "刷1单"):
            self.assertIsNone(self.hit(text), text)
        self.assertIsNone(self.hit("刷", reply_markup={"inline_keyboard": [[{"text": "单"}]]}))
        self.assertIsNone(self.hit("", reply_markup={"inline_keyboard": [[{"text": "刷"}, {"text": "单"}]]}))
        self.assertEqual(self.hit("说明", reply_markup={"inline_keyboard": [[{"text": "刷⭐单"}]]})["word"], "刷单")

    def test_mode_combinations_and_any_required_observe_are_conservative(self):
        self.add("刷单返佣")
        self.modes(traditional="enforce", homophone="enforce", split="observe")
        self.assertTrue(self.hit("shuadan⭐返佣")["observe"])
        hit = self.hit("唰⭐單返佣")
        self.assertTrue(hit["observe"])
        self.assertEqual(hit["match_kind"], "繁简字形／定向同音／拼音／短词拆字增强")
        self.assertFalse(self.hit("唰單返佣")["observe"])
        self.modes(split="enforce")
        self.assertFalse(self.hit("唰⭐單返佣")["observe"])
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.store.del_filter(-1001, "word", "刷单返佣")
        self.assertTrue(self.hit("唰⭐單返佣")["observe"])
        self.add("洗钱联系客服", "加微信领取资源")
        self.assertEqual(self.hit("洗💰联⭐系客服")["word"], "洗钱联系客服")
        for text in ("加薇信领取资源", "加徽信领取资源", "加v信领取资源"):
            self.assertEqual(self.hit(text)["word"], "加微信领取资源", text)

    def test_exact_and_word_rules_do_not_expand_or_change_custom_overrides(self):
        self.add("刷单返佣")
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.modes(traditional="enforce", homophone="enforce", split="enforce")
        for mode in ("word", "exact"):
            self.store.edit_word_rule(-1001, "刷单返佣", mode)
            for text in ("刷單返佣", "唰单返佣", "刷⭐单返佣"):
                self.assertIsNone(self.hit(text), (mode, text))
            self.assertIsNotNone(self.hit("刷单返佣"))
        self.store.edit_word_rule(-1001, "刷单返佣", "contains")
        self.assertIsNotNone(self.hit("唰单返佣"))

    def test_exclusions_original_context_waivers_other_rules_and_group_scope(self):
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.modes(homophone="enforce")
        self.store.exclude_word(-1001, "刷单返佣")
        self.assertIsNone(self.hit("唰单返佣"))
        self.store.exclude_word(-1001, "刷单返佣", exclude=False)
        self.bot._private_word_input(1, -1001, "except", "刷单返佣|警惕唰单返佣")
        self.assertIsNone(self.hit("警惕唰单返佣"))
        self.assertIsNotNone(self.hit("警惕唰单返佣，加我"))
        self.assertIsNotNone(self.hit("警惕刷单返佣"))
        self.add("警惕")
        self.assertEqual(self.hit("警惕唰单返佣")["word"], "警惕")
        self.assertIsNone(self.store.word_policy(-1002).hit({"text": "唰单返佣"}, settings=self.store.settings(-1001)))
        self.assertTrue(context_matches("刷单返佣", "唰单返佣", self.store.settings(-1001)))
        self.assertFalse(context_matches("刷单返佣", "普通聊天", self.store.settings(-1001)))

    def test_enforcing_variant_beats_observed_literal_or_variant_pack(self):
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.add("微信", "赌博")
        self.modes(traditional="observe", homophone="enforce")
        hit = self.hit("刷单返佣 薇信")
        self.assertEqual(hit["word"], "微信")
        self.assertFalse(hit["observe"])
        hit = self.hit("賭博 薇信")
        self.assertEqual(hit["word"], "微信")
        self.assertFalse(hit["observe"])

    def test_disabled_variants_do_not_load_indexes_and_warm_indexes_are_reused(self):
        self.add("赌博")
        policy = self.store.word_policy(-1001)
        with patch("app.variants.VariantIndex", side_effect=AssertionError("must remain lazy")):
            self.assertIsNone(self.hit("賭博"))
        self.modes(traditional="enforce")
        self.assertIsNotNone(self.hit("賭博"))
        engine = policy.variant_rules.engine(True)
        self.assertIs(self.store.word_policy(-1001), policy)
        self.assertIs(policy.variant_rules.engine(True), engine)

    def test_fifty_thousand_rules_scan_without_rebuilding(self):
        words = tuple(f"独立广告规则{i:06}完成" for i in range(50000))
        index = VariantIndex(compile_words(words).rules)
        engine = index.engine(True)
        self.assertEqual(index.find(simplify(words[-1]), traditional=True), words[-1])
        self.assertIsNone(index.find("正常聊天"*200, traditional=True))
        self.assertIs(index.engine(True), engine)


class VariantWorkflowTests(VariantFixture):
    def test_menu_buttons_preview_binding_and_single_use(self):
        self.click("v:-1001:words")
        self.assertTrue(any(b["callback_data"] == "v:-1001:wordvariants"
                            for row in self.page()["reply_markup"]["inline_keyboard"] for b in row))
        self.click("v:-1001:wordvariants")
        self.assertIn("中文变体识别", self.page()["text"])
        for row in self.page()["reply_markup"]["inline_keyboard"]:
            for b in row:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)
        self.click("ad:-1001:variantmode:homophone_enforce")
        d = dict(self.bot.admin_ui.drafts[1])
        self.assertEqual(self.store.variant_modes(-1001)["homophone"], "off")
        for uid, cid, mid in ((999, -1001, d["prompt_id"]), (1, -1002, d["prompt_id"]), (1, -1001, d["prompt_id"]+1)):
            self.click(f"ac:{cid}:{d['nonce']}", uid=uid, mid=mid)
            self.assertEqual(self.store.variant_modes(-1001)["homophone"], "off")
        self.confirm()
        self.assertEqual(self.store.variant_modes(-1001)["homophone"], "enforce")
        self.modes(homophone="off")
        self.click(f"ac:-1001:{d['nonce']}", mid=d["prompt_id"])
        self.assertEqual(self.store.variant_modes(-1001)["homophone"], "off")

    def test_stale_settings_stale_words_revoked_admin_and_permissions(self):
        for reason in ("settings", "words", "admin", "bot"):
            with self.subTest(reason=reason):
                self.modes(homophone="off")
                self.click("ad:-1001:variantmode:homophone_enforce")
                if reason == "settings":
                    self.modes(split="observe")
                if reason == "words":
                    self.add("新增"+reason)
                with patch.object(self.bot, "is_admin", return_value=reason != "admin"), patch.object(
                        self.bot, "bot_can", return_value=reason != "bot"):
                    self.confirm()
                self.assertEqual(self.store.variant_modes(-1001)["homophone"], "off")

    def test_preview_expiry_and_preset_only_observes(self):
        self.click("ad:-1001:variantmode:traditional_enforce")
        self.bot.admin_ui.drafts[1]["expires"] = time.monotonic()-1
        self.confirm()
        self.assertEqual(self.store.variant_modes(-1001)["traditional"], "off")
        self.click("ad:-1001:variantpreset:observe")
        self.confirm()
        self.assertEqual(set(self.store.variant_modes(-1001).values()), {"observe"})
        self.assertEqual(set(self.store.variant_modes(-1002).values()), {"off"})

    def test_test_button_commands_and_spam_test_explain_kind_and_global_state(self):
        self.add("刷单返佣")
        self.modes(homophone="observe")
        self.click("w:-1001:test")
        self.private("唰单返佣")
        self.assertIn("定向同音", self.page()["text"])
        self.assertIn("仅观察", self.page()["text"])
        self.group("/wordtest 唰单返佣", uid=1)
        self.assertIn("定向同音", self.page()["text"])
        self.click("ad:-1001:begin:spam_test")
        self.private("唰单返佣")
        self.assertIn("定向同音", self.page()["text"])
        self.store.set(-1001, "word_filter_enabled", False)
        self.click("w:-1001:test")
        self.private("唰单返佣")
        self.assertIn("总开关已关闭", self.page()["text"])
        self.assertFalse(self.deletes())

    def test_observe_does_not_mask_independent_link_or_flood_and_audit_displays_kind(self):
        self.add("刷单返佣")
        self.modes(homophone="observe")
        self.group("唰单返佣")
        event = self.store.audit_page(-1001)[0][0]
        self.assertEqual(event["outcome"], "observed")
        self.assertEqual(event["rule_key"], "刷单返佣")
        self.assertIn("定向同音", event["match_kind"])
        self.click("v:-1001:audit")
        self.assertIn("定向同音", self.page()["text"])
        self.group("唰单返佣 https://bad.example.com", mid=2)
        self.assertEqual(self.deletes(), [2])
        self.store.set(-1001, "repeat_limit", 2)
        self.group("唰单返佣", mid=3, uid=3)
        self.group("唰单返佣", mid=4, uid=3)
        self.assertEqual(self.deletes(), [2, 4])

    def test_global_observation_total_off_admin_and_edit_guards(self):
        self.add("赌博")
        self.modes(traditional="enforce")
        self.group("賭博", uid=1)
        self.assertFalse(self.deletes())
        self.group("賭博", edit=True)
        self.assertEqual(self.deletes(), [1])
        self.store.set(-1001, "observe_only", True)
        self.group("賭博", mid=2, uid=3)
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "observed")
        self.store.set(-1001, "word_filter_enabled", False)
        self.group("賭博", mid=3, uid=4)
        self.assertEqual(len(self.store.audit_page(-1001)[0]), 2)
        self.assertEqual(self.deletes(), [1])

    def test_precise_waiver_command_accepts_only_enabled_variants(self):
        self.add("刷单返佣")
        self.modes(homophone="enforce")
        self.group("/wordexcept 刷单返佣|警惕唰单返佣", uid=1)
        self.assertIsNone(self.hit("警惕唰单返佣"))
        self.modes(homophone="off")
        self.group("/wordexcept 刷单返佣|不要唰单返佣", uid=1)
        self.assertEqual(self.store.filter_count(-1001, "word_exception"), 1)

    def test_ocr_keeps_confidence_four_character_minimum_and_variant_metadata(self):
        self.add("刷单返佣", "赌博")
        self.modes(traditional="enforce", homophone="enforce", split="enforce")
        self.store.set(-1001, "ocr_mode", "delete")
        for mid, text, confidence, uid in ((1, "唰 單 返 佣", 95, 2), (2, "赌搏", 95, 2),
                (3, "唰单返佣", 50, 2), (4, "唰单返佣", float("nan"), 2),
                (5, "唰单返佣", float("inf"), 2), (6, "唰单返佣", 95, 1)):
            event = {"chat_id": -1001, "message_id": mid, "user_id": uid, "created_at": time.time()}
            with patch.object(self.bot.ocr, "results", return_value=[(event, text, confidence)]):
                self.bot.drain_ocr()
        self.assertEqual(self.deletes(), [1])
        event = self.store.audit_page(-1001)[0][0]
        self.assertEqual(event["rule_key"], "刷单返佣")
        self.assertIn("繁简字形", event["match_kind"])

    def test_ocr_observed_literal_does_not_mask_punitive_variant_and_waiver_is_precise(self):
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.add("联系微信")
        self.modes(homophone="enforce")
        self.store.set(-1001, "ocr_mode", "delete")
        def result(mid, text):
            event = {"chat_id": -1001, "message_id": mid, "user_id": 2, "created_at": time.time()}
            with patch.object(self.bot.ocr, "results", return_value=[(event, text, 95)]):
                self.bot.drain_ocr()
        result(1, "刷单返佣 联系薇信")
        self.assertEqual(self.deletes(), [1])
        self.store.set_pack_modes(-1001, {"task_scam": "off"})
        self.bot._private_word_input(1, -1001, "except", "联系微信|警惕联系薇信")
        result(2, "警 惕 联 系 薇 信")
        self.assertEqual(self.deletes(), [1])
        result(3, "警惕联系薇信额外广告")
        self.assertEqual(self.deletes(), [1, 3])
        self.bot._ocr_invalidated[(-1001, 4)] = time.time()
        result(4, "联系薇信")
        self.assertEqual(self.deletes(), [1, 3])


class VariantDataPersistenceTests(unittest.TestCase):
    def test_pinned_character_source_integrity_license_and_alias_shape(self):
        sources = json.loads((ROOT / "variant-sources.json").read_text(encoding="utf-8"))
        for source in sources:
            self.assertEqual(hashlib.sha256((ROOT / source["file"]).read_bytes()).hexdigest(), source["sha256"])
            self.assertEqual(source["license"], "Apache-2.0")
            self.assertIn("556ed22496d650bd0b13b6c163be9814637970ae", source["url"])
        self.assertIn("Apache License", (ROOT / "LICENSE-OpenCC").read_text(encoding="utf-8"))
        self.assertEqual(simplify("賭博刷單"), "赌博刷单")
        data = json.loads((ROOT / "aliases.json").read_text(encoding="utf-8"))
        self.assertTrue(all(len(entry["word"]) >= 2 and entry["aliases"] for entry in data))
        self.assertEqual(AliasIndex().rewrite("菠菜很好吃"), "菠菜很好吃")

    def test_settings_audit_additive_migration_persist_and_compare_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "db.sqlite")
            store = Store(path)
            store.ensure_chat(-1, "旧群")
            expected = store.variant_modes(-1)
            store.set_variant_modes(-1, {"traditional": "observe"}, expected=expected, revision=0)
            with self.assertRaises(ValueError):
                store.set_variant_modes(-1, {"traditional": "enforce"}, expected=expected)
            with self.assertRaises(ValueError):
                store.set_variant_modes(-1, {"traditional": "enforce", "bad": "off"})
            store.audit(-1, 1, 2, "违禁词", "deleted", rule_key="赌博", match_kind="繁简字形")
            store.audit(-1, 1, 2, "违禁词", "observed", rule_key="另一个", match_kind="其他")
            self.assertEqual(store.audit_page(-1)[0][0]["match_kind"], "繁简字形")
            store.close()
            # Simulate an older database lacking the additive audit column.
            import sqlite3
            with closing(sqlite3.connect(path)) as db:
                db.execute("ALTER TABLE moderation_audit DROP COLUMN match_kind")
                db.commit()
            store = Store(path)
            try:
                self.assertEqual(store.variant_modes(-1)["traditional"], "observe")
                row = store.audit_page(-1)[0][0]
                self.assertEqual((row["rule_key"], row["outcome"], row["match_kind"]), ("赌博", "deleted", ""))
                with self.assertRaises(ValueError):
                    store.set(-1, "word_homophone_mode", "invalid")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
