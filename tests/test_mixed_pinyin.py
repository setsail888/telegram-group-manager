"""Mixed-script keyword detection and its actual moderation / confirmation routes."""
import hashlib
import json
import time
from unittest.mock import patch

from app.moderation import compile_words
from app.variants import VariantIndex, context_matches
from app.word_library import ROOT
from tests.test_chinese_variants import VariantFixture


class MixedPinyinTests(VariantFixture):
    def test_default_off_then_all_partial_spellings(self):
        self.add("刷单返佣", "接码平台", "赌博")
        self.assertIsNone(self.hit("shua单返yong"))
        self.modes(pinyin="enforce")
        for text, word in (("shua单返佣", "刷单返佣"), ("刷dan返yong", "刷单返佣"),
                ("shuadan返佣", "刷单返佣"), ("警惕刷dan返佣骗局", "刷单返佣"),
                ("接ma平tai", "接码平台"), ("jie码平台", "接码平台"), ("du博", "赌博")):
            with self.subTest(text=text):
                hit = self.hit(text)
                self.assertEqual(hit["word"], word)
                self.assertEqual(hit["match_kind"], "拼音＋中文混写")
                self.assertFalse(hit["observe"])

    def test_case_width_accents_and_bounded_separators(self):
        self.add("刷单返佣")
        self.modes(pinyin="enforce")
        for text in ("ＳＨＵＡ单返yong", "shuǎ单返yōng", "shua dan返佣", "shua-dan返佣",
                     "shua'dan返佣", "shua   单返佣", "刷dān返佣", "刷dan返\u200byong"):
            self.assertIsNotNone(self.hit(text), text)
        for text in ("shua    单返佣", "shua\n单返佣", "shua。单返佣", "shua⭐单返佣", "shua1单返佣"):
            self.assertIsNone(self.hit(text), text)

    def test_never_generalizes_homophones_initials_or_pure_pinyin(self):
        self.add("刷单返佣", "赌博", "女友")
        self.modes(pinyin="enforce")
        for text in ("shuadanfanyong", "sdfy", "刷d返佣", "shua丹返佣", "刷dan返拥", "du搏",
                     "nu友", "shuai单返佣", "刷单返回佣"):
            self.assertIsNone(self.hit(text), text)
        self.assertIsNotNone(self.hit("nv友"))
        self.assertIsNotNone(self.hit("nǚ友"))

    def test_english_identifiers_and_syllable_boundaries(self):
        self.add("刷单", "安全")
        self.modes(pinyin="enforce")
        for text in ("myshua单", "shua单loader", "my_shua单", "shua单_value", "1shua单", "shua单2",
                     "方案quan", "方案全", "SHUADAN", "shua单元"):
            # A following Han character remains legal for a contains rule.
            if text == "shua单元":
                self.assertIsNotNone(self.hit(text), text)
            else:
                self.assertIsNone(self.hit(text), text)
        self.assertIsNotNone(self.hit("an全"))

    def test_caption_buttons_and_no_cross_part_joining(self):
        self.add("接码平台")
        self.modes(pinyin="enforce")
        self.assertIsNotNone(self.hit("", caption="接ma平tai"))
        self.assertIsNotNone(self.hit("说明", reply_markup={"inline_keyboard": [[{"text": "接ma平tai"}]]}))
        self.assertIsNone(self.hit("接ma", reply_markup={"inline_keyboard": [[{"text": "平tai"}]]}))
        self.assertIsNone(self.hit("", reply_markup={"inline_keyboard": [[{"text": "接ma"}, {"text": "平tai"}]]}))

    def test_exact_and_word_overrides_never_expand(self):
        self.add("刷单返佣")
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.modes(pinyin="enforce")
        for mode in ("word", "exact"):
            self.store.edit_word_rule(-1001, "刷单返佣", mode)
            self.assertIsNone(self.hit("shua单返yong"))
            self.assertIsNotNone(self.hit("刷单返佣"))

    def test_pack_exclusions_and_original_precise_exception(self):
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.modes(pinyin="enforce")
        self.assertIsNotNone(self.hit("shua单返yong"))
        self.store.exclude_word(-1001, "刷单返佣")
        self.assertIsNone(self.hit("shua单返yong"))
        self.store.exclude_word(-1001, "刷单返佣", exclude=False)
        self.bot._private_word_input(1, -1001, "except", "刷单返佣|警惕shua单返yong")
        self.assertIsNone(self.hit("警惕shua单返yong"))
        self.assertIsNotNone(self.hit("警惕shua单返yong，加我"))
        self.assertIsNotNone(self.hit("警惕刷dan返佣"))
        self.add("警惕")
        self.assertEqual(self.hit("警惕shua单返yong")["word"], "警惕")
        self.assertTrue(context_matches("刷单返佣", "shua单返yong", self.store.settings(-1001)))
        self.assertIsNone(self.store.word_policy(-1002).hit({"text": "shua单返yong"}, settings=self.store.settings(-1001)))

    def test_combinations_keep_required_observation(self):
        self.add("刷单返佣")
        self.modes(pinyin="enforce", traditional="observe", split="enforce")
        hit = self.hit("shua單⭐返yong")
        self.assertTrue(hit["observe"])
        self.assertIn("繁简", hit["match_kind"])
        self.assertIn("拆字", hit["match_kind"])
        self.assertIn("混写", hit["match_kind"])
        self.modes(traditional="enforce")
        self.assertFalse(self.hit("shua單⭐返yong")["observe"])
        self.store.del_filter(-1001, "word", "刷单返佣")
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.assertTrue(self.hit("shua單⭐返yong")["observe"])

    def test_observation_independent_rules_total_switch_and_admin_protection(self):
        self.add("刷单返佣")
        self.modes(pinyin="observe")
        self.group("shua单返yong")
        self.assertFalse(self.deletes())
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "observed")
        self.group("shua单返yong https://bad.example.com", mid=2)
        self.assertEqual(self.deletes(), [2])
        self.modes(pinyin="enforce")
        self.group("shua单返yong", mid=3, uid=1)
        self.store.set(-1001, "word_filter_enabled", False)
        self.group("shua单返yong", mid=4, uid=3)
        self.assertEqual(self.deletes(), [2])

    def test_ocr_keeps_four_han_rule_minimum_confidence_and_admin_guards(self):
        self.add("刷单返佣", "赌博")
        self.modes(pinyin="enforce")
        self.store.set(-1001, "ocr_mode", "delete")
        for mid, text, confidence, uid in ((1, "shua单返yong", 95, 2), (2, "du博", 95, 2),
                (3, "shua单返yong", 40, 2), (4, "shua单返yong", 95, 1)):
            event = {"chat_id": -1001, "message_id": mid, "user_id": uid, "created_at": time.time()}
            with patch.object(self.bot.ocr, "results", return_value=[(event, text, confidence)]):
                self.bot.drain_ocr()
        self.assertEqual(self.deletes(), [1])
        self.assertEqual(self.store.audit_page(-1001)[0][0]["match_kind"], "拼音＋中文混写")

    def test_visual_enable_preview_confirm_and_test(self):
        self.add("刷单返佣")
        self.click("v:-1001:wordvariants")
        self.assertIn("拼音＋中文混写", self.page()["text"])
        self.click("ad:-1001:variantmode:pinyin_enforce")
        draft = dict(self.bot.admin_ui.drafts[1])
        self.assertEqual(self.store.variant_modes(-1001)["pinyin"], "off")
        self.click(f"ac:-1002:{draft['nonce']}", mid=draft["prompt_id"])
        self.assertEqual(self.store.variant_modes(-1001)["pinyin"], "off")
        self.confirm()
        self.assertEqual(self.store.variant_modes(-1001)["pinyin"], "enforce")
        self.click("w:-1001:test")
        self.private("shua单返yong")
        self.assertIn("拼音＋中文混写", self.page()["text"])
        self.assertFalse(self.deletes())

    def test_disabled_mode_is_lazy_and_warm_index_is_reused(self):
        self.add("接码平台")
        with patch("app.mixed_pinyin.MixedPinyinIndex", side_effect=AssertionError("disabled")):
            self.assertIsNone(self.hit("接ma平tai"))
        self.modes(pinyin="enforce")
        self.assertIsNotNone(self.hit("接ma平tai"))
        policy = self.store.word_policy(-1001)
        engine = policy.variant_rules.mixed_engines[False]
        self.assertIsNotNone(self.hit("jie码平台"))
        self.assertIs(policy.variant_rules.mixed_engines[False], engine)

    def test_large_rule_index_and_source_integrity(self):
        words = tuple(f"独立广告规则{i:05}完成" for i in range(10000))
        index = VariantIndex(compile_words(words).rules)
        self.assertEqual(index.find("独立guang告规则09999完成", pinyin=True), words[-1])
        self.assertIsNone(index.find("正常聊天"*200, pinyin=True))
        source = json.loads((ROOT/"pinyin-source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["version"], "0.55.0")
        data = json.loads((ROOT/"pinyin-characters.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 41923)
        self.assertEqual(data["刷"], "shua")
        self.assertEqual(hashlib.sha256((ROOT/"pinyin-characters.json").read_bytes()).hexdigest(), source["generated_sha256"])
        self.assertEqual(hashlib.sha256((ROOT/"LICENSE-pypinyin-MIT.txt").read_bytes()).hexdigest(), source["license_sha256"])
        self.assertIn("MIT", (ROOT/"LICENSE-pypinyin-MIT.txt").read_text(encoding="utf-8"))

    def test_each_message_is_encoded_once_across_packs_without_persistent_text_cache(self):
        from app.mixed_pinyin import encoded
        self.store.set_pack_modes(-1001, {"task_scam": "enforce", "finance_scam": "enforce"})
        self.modes(pinyin="enforce", traditional="enforce")
        with patch("app.mixed_pinyin.encoded", wraps=encoded) as encode:
            self.assertIsNone(self.hit("Hello，普通群聊讨论软件开发"))
            self.assertEqual(encode.call_count, 1)
            self.assertIsNone(self.hit("Hello，普通群聊讨论软件开发"))
            self.assertEqual(encode.call_count, 2)
