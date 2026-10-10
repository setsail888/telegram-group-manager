"""Adversarial positives and real-chat negatives through policy and bot routes."""
import hashlib
import json
import os
import tempfile
import time
from unittest.mock import patch

from app.i18n import render
from app.moderation import compile_words
from app.phonetic import PhoneticIndex, polyphones, preferred
from app.store import Store
from app.variants import VariantIndex, context_matches, kind_text
from app.word_library import ROOT
from tests.test_chinese_variants import VariantFixture


class PhoneticVariantTests(VariantFixture):
    def test_polyphones_in_every_mixed_slot_without_homophone_folding(self):
        self.add("地下银行", "重庆贷款", "长乐音乐", "音乐", "行业")
        self.modes(pinyin="enforce")
        for text, word in (("地下银hang", "地下银行"), ("地下yin行", "地下银行"),
                           ("chong庆贷款", "重庆贷款"), ("重qing贷kuan", "重庆贷款"),
                           ("音yue", "音乐"), ("hang业", "行业"), ("chang乐yin乐", "长乐音乐")):
            with self.subTest(text=text):
                self.assertEqual(self.hit(text)["word"], word)
        for text in ("地下银杏", "地下yin航", "chong情贷款", "重庆dai宽", "yin悦", "chang乐yin悦"):
            self.assertIsNone(self.hit(text), text)

    def test_new_features_default_off_and_independent(self):
        self.add("刷单返佣")
        for text in ("sdfy，日结加我", "唰单返佣，日结加我"):
            self.assertIsNone(self.hit(text))
        self.modes(pinyin="enforce")
        self.assertIsNone(self.hit("sdfy，日结加我"))
        self.assertIsNone(self.hit("唰单返佣，日结加我"))
        self.modes(initials="enforce")
        self.assertEqual(self.hit("sdfy，日结加我")["word"], "刷单返佣")
        self.assertIsNone(self.hit("唰单返佣，日结加我"))

    def test_initials_pure_partial_full_syllables_width_and_boundaries(self):
        self.add("刷单返佣", "接码平台")
        self.modes(initials="enforce")
        for text, word in (("sdfy，日结加我", "刷单返佣"), ("ＳＤＦＹ，日结加我", "刷单返佣"),
                           ("s-d-f-y，日结加我", "刷单返佣"), ("刷d返y，日结加我", "刷单返佣"),
                           ("sd返yong，日结加我", "刷单返佣"), ("jie码pt，下单联系我", "接码平台")):
            self.assertEqual(self.hit(text)["word"], word, text)
        for text in ("sdfy", "mysdfy，日结加我", "sdfy_value，日结加我", "sdfy2，日结加我",
                     "my_sdfy，日结加我", "sdfyloader，日结加我", "shuadanfanyong，日结加我"):
            self.assertIsNone(self.hit(text), text)

    def test_short_initials_need_anchor_and_specific_cue(self):
        self.add("刷单", "社工库", "微信")
        self.modes(initials="enforce")
        self.assertEqual(self.hit("刷d，日结加我")["word"], "刷单")
        self.assertEqual(self.hit("社g库，查人接单")["word"], "社工库")
        for text in ("sd，日结加我", "sgk，接单加我", "wx，加我", "刷d，联系我", "w信，招聘"):
            self.assertIsNone(self.hit(text), text)

    def test_homophones_use_preferred_phrase_sound_not_rare_readings(self):
        self.add("银行账户", "重新办理", "刷单返佣", "重量检验")
        self.modes(phonetic="enforce")
        for text, word in (("银航账户，联系我", "银行账户"), ("崇新办理，下单加我", "重新办理"),
                           ("刷单返拥，日结加我", "刷单返佣"), ("刷丹返佣，日结加我", "刷单返佣")):
            self.assertEqual(self.hit(text)["word"], word, text)
        for text in ("银杏账户，联系我", "从新办理，下单加我", "刷单返拥", "刷丹反佣，日结加我", "崇量检验，联系我"):
            self.assertIsNone(self.hit(text), text)
        self.assertEqual(preferred("银行重新"), ["yin", "hang", "chong", "xin"])

    def test_short_homophones_context_and_benign_collisions(self):
        self.add("赌博", "微信", "博彩", "老师的微信")
        self.modes(phonetic="enforce")
        self.assertEqual(self.hit("赌搏，充值加我")["word"], "赌博")
        self.assertEqual(self.hit("博财，首充加我")["word"], "博彩")
        for text in ("赌搏，招聘", "读博，充值加我", "威信，下单加我", "博采，首充加我",
                     "菠菜很好吃，招聘厨师", "老师的威信，招聘教师", "读博需要联系导师",
                     "老师很有威信", "博采众长", "门窗开合顺畅", "衣服洗前浸泡"):
            self.assertIsNone(self.hit(text), text)

    def test_longer_homophones_allow_two_but_keep_literal_anchors(self):
        self.add("刷单返佣日结到账")
        self.modes(phonetic="enforce")
        self.assertIsNotNone(self.hit("刷丹返拥日结到账，联系我"))
        self.assertIsNone(self.hit("唰丹反佣日结到账，联系我"))

    def test_technical_acronyms_and_ordinary_chat(self):
        self.add("安培仪", "查询接口", "超频优化", "赌博", "微信", "博彩", "刷单返佣")
        self.modes(initials="enforce", phonetic="enforce", pinyin="enforce")
        for text in ("API接口文档，招聘开发者", "CPU/GPU性能优化，联系我", "SQL查询，欢迎咨询",
                     "HTTP服务支持JSON，联系我", "wxWidgets开发讨论", "shuaidan返回佣金数据",
                     "sdfy变量是示例，招聘开发者", "春天菠菜很好吃", "这篇论文讨论读博生活",
                     "银行账户测试数据正常", "普通群聊软件开发、读书与生活经验"):
            self.assertIsNone(self.hit(text), text)
        # A four-Han rule whose initials are HTTP must still preserve the acronym.
        self.add("后台调配")
        self.assertIsNone(self.hit("HTTP，招聘开发者联系我"))

    def test_code_links_usernames_and_commands_protected_for_new_expansions(self):
        self.add("刷单返佣", "重庆贷款")
        self.modes(initials="enforce", phonetic="enforce", pinyin="enforce")
        for token in ("`sdfy`", "```刷单返拥```", "https://example.org/sdfy", "@sdfy", "/sdfy",
                      "https://example.org/刷单返拥", "`chong庆贷款`", "@chong庆贷款", "/chong庆贷款"):
            self.assertIsNone(self.hit(token+"，日结加我"), token)
        self.assertIsNotNone(self.hit("`sdfy` 另有刷单返拥，日结加我"))

    def test_cues_never_cross_clause_distance_buttons_or_match_span(self):
        self.add("刷单返佣")
        self.modes(initials="enforce", phonetic="enforce")
        for text in ("sdfy\n日结加我", "sdfy。日结加我", "sdfy；日结加我", "sdfy！日结加我",
                     "sdfy"+"好"*25+"日结加我", "刷单返拥", "刷单返拥\n加我",
                     "sd\nfy，日结加我", "s    dfy，日结加我", "sd⭐fy，日结加我"):
            self.assertIsNone(self.hit(text), text)
        self.assertIsNone(self.hit("日结加我", reply_markup={"inline_keyboard": [[{"text": "sdfy"}]]}))
        self.assertIsNone(self.hit("sd", reply_markup={"inline_keyboard": [[{"text": "fy，日结加我"}]]}))
        self.assertIsNotNone(self.hit("", caption="sdfy，日结加我"))
        self.assertIsNotNone(self.hit("说明", reply_markup={"inline_keyboard": [[{"text": "sdfy，日结加我"}]]}))

    def test_anti_scam_quotation_guard_is_limited_to_new_fuzzy_modes(self):
        self.add("刷单返佣")
        self.modes(initials="enforce", phonetic="enforce", pinyin="enforce")
        for text in ("警惕sdfy，日结加我", "谨防刷单返拥，日结加我", "不要参与sdfy，日结加我",
                     "sdfy是骗局，日结加我", "拒绝刷单返拥，日结加我"):
            self.assertIsNone(self.hit(text), text)
        self.assertIsNotNone(self.hit("警惕刷单返佣，日结加我"))
        self.assertIsNotNone(self.hit("警惕刷dan返佣，日结加我"))
        self.assertIsNotNone(self.hit("反诈提醒。sdfy，日结加我"))

    def test_observe_combinations_do_not_bypass_required_features(self):
        self.add("刷单返佣")
        self.modes(initials="enforce", phonetic="observe", traditional="observe", split="enforce")
        text = "刷d反⭐傭，日结加我"
        hit = self.hit(text)
        self.assertTrue(hit["observe"])
        for label in ("繁简", "拆字", "缩写", "同音字增强"):
            self.assertIn(label, hit["match_kind"])
        self.modes(phonetic="enforce")
        self.assertTrue(self.hit(text)["observe"])
        self.modes(traditional="enforce")
        self.assertFalse(self.hit(text)["observe"])
        # Initials include full-syllable/initial mixtures as a single feature.
        self.modes(pinyin="observe")
        self.assertFalse(self.hit("刷d返yong，日结加我")["observe"])

    def test_han_gaps_require_the_split_feature_for_homophones(self):
        self.add("刷单返佣")
        self.modes(phonetic="enforce")
        self.assertIsNone(self.hit("刷 单 返 拥，日结加我"))
        self.modes(split="observe")
        self.assertTrue(self.hit("刷 单 返 拥，日结加我")["observe"])

    def test_pinyin_homophone_combination_preserves_observation(self):
        self.add("刷单返佣")
        self.modes(pinyin="observe", phonetic="enforce")
        self.assertTrue(self.hit("shua丹返佣，日结加我")["observe"])
        self.modes(pinyin="enforce")
        self.assertFalse(self.hit("shua丹返佣，日结加我")["observe"])
        self.modes(pinyin="off")
        self.assertIsNone(self.hit("shua丹返佣，日结加我"))

    def test_exact_word_overrides_pack_exclusions_and_group_isolation(self):
        self.add("刷单返佣")
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.modes(initials="enforce", phonetic="enforce")
        for mode in ("word", "exact"):
            self.store.edit_word_rule(-1001, "刷单返佣", mode)
            self.assertIsNone(self.hit("sdfy，日结加我"))
            self.assertIsNone(self.hit("唰单返佣，日结加我"))
        self.store.del_filter(-1001, "word", "刷单返佣")
        self.assertIsNotNone(self.hit("sdfy，日结加我"))
        self.store.exclude_word(-1001, "刷单返佣")
        self.assertIsNone(self.hit("sdfy，日结加我"))
        self.assertIsNone(self.store.word_policy(-1002).hit({"text": "sdfy，日结加我"}, settings=self.store.settings(-1001)))

    def test_precise_exception_uses_original_context_and_other_rules_still_apply(self):
        self.add("刷单返佣")
        self.modes(initials="enforce", phonetic="enforce")
        text = "sdfy，日结加我"
        self.assertTrue(context_matches("刷单返佣", text, self.store.settings(-1001)))
        self.bot._private_word_input(1, -1001, "except", "刷单返佣|"+text)
        self.assertIsNone(self.hit(text))
        self.assertIsNotNone(self.hit(text+"，额外广告"))
        self.assertIsNotNone(self.hit("刷单返拥，日结加我"))
        self.add("日结")
        self.assertEqual(self.hit(text)["word"], "日结")

    def test_actual_moderation_observe_enforce_admin_and_master_switch(self):
        self.add("刷单返佣")
        self.modes(initials="observe", phonetic="enforce")
        self.group("sdfy，日结加我")
        self.assertFalse(self.deletes())
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "observed")
        self.group("sdfy，日结加我 https://bad.example.com", mid=2)
        self.assertEqual(self.deletes(), [2])
        self.modes(initials="enforce")
        self.group("sdfy，日结加我", mid=3)
        self.group("刷单返拥，日结加我", mid=4, uid=1)
        self.store.set(-1001, "observe_only", True)
        self.group("刷单返拥，日结加我", mid=5, uid=3)
        self.store.set(-1001, "observe_only", False)
        self.store.set(-1001, "word_filter_enabled", False)
        self.group("刷单返拥，日结加我", mid=6, uid=4)
        self.assertEqual(self.deletes(), [2, 3])

    def test_pack_observation_cannot_be_promoted_by_fuzzy_mode(self):
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.modes(initials="enforce", phonetic="enforce")
        for text in ("sdfy，日结加我", "唰单返佣，日结加我"):
            self.assertTrue(self.hit(text)["observe"])

    def test_new_fuzzy_modes_never_drive_ocr_deletion(self):
        self.add("刷单返佣", "重庆贷款")
        self.modes(initials="enforce", phonetic="enforce", pinyin="enforce")
        self.store.set(-1001, "ocr_mode", "delete")
        for mid, text in enumerate(("sdfy，日结加我", "刷单返拥，日结加我", "chong庆贷款"), 1):
            event = {"chat_id": -1001, "message_id": mid, "user_id": 2, "created_at": time.time()}
            with patch.object(self.bot.ocr, "results", return_value=[(event, text, 95)]):
                self.bot.drain_ocr()
        self.assertEqual(self.deletes(), [3])

    def test_visual_preview_confirm_replay_and_bilingual_audit_label(self):
        self.add("刷单返佣")
        self.click("v:-1001:wordvariants")
        page = self.page()
        for label in ("拼音缩写（上下文）", "同音字增强（上下文）", "六项"):
            self.assertIn(label, page["text"])
        self.click("ad:-1001:variantmode:initials_enforce")
        self.assertEqual(self.store.variant_modes(-1001)["initials"], "off")
        draft = self.confirm()
        self.assertEqual(self.store.variant_modes(-1001)["initials"], "enforce")
        self.modes(initials="off")
        self.click(f"ac:-1001:{draft['nonce']}", mid=draft["prompt_id"])
        self.assertEqual(self.store.variant_modes(-1001)["initials"], "off")
        self.click("ad:-1001:variantpreset:observe")
        self.confirm()
        self.assertEqual(set(self.store.variant_modes(-1001).values()), {"observe"})
        self.click("w:-1001:test")
        self.private("sdfy，日结加我")
        self.assertIn("拼音缩写", self.page()["text"])
        self.assertFalse(self.deletes())
        self.assertIn("Contextual pinyin initials", render(kind_text("拼音缩写（上下文）"), "en"))

    def test_shared_transient_preparation_and_lazy_indexes(self):
        self.store.set_pack_modes(-1001, {"task_scam": "enforce", "finance_scam": "enforce"})
        self.modes(initials="enforce", phonetic="enforce")
        with patch("app.phonetic.PhoneticIndex", side_effect=AssertionError("no cue")):
            self.assertIsNone(self.hit("Hello，普通聊天内容"))
        from app.phonetic import prepare
        with patch("app.phonetic.prepare", wraps=prepare) as prep:
            self.assertIsNone(self.hit("Hello，招聘开发工程师"))
            self.assertEqual(prep.call_count, 1)
            self.assertIsNone(self.hit("Hello，招聘开发工程师"))
            self.assertEqual(prep.call_count, 2)

    def test_large_rule_trie_and_ambiguous_search_budget(self):
        words = [f"独立广告规则{i:05}完成" for i in range(10000)]
        index = VariantIndex(compile_words(words).rules)
        self.assertEqual(index.find("独立g告规则09999完成，联系我", initials=True), words[-1])
        ambiguous = PhoneticIndex([(str(i), chr(0x4e00+i)+"单返佣") for i in range(2000)])
        with patch("app.phonetic.MAX_WORK", 100):
            self.assertIsNone(ambiguous.find("s"*1000+"，日结加我", initials=True))
        self.add("必须处理")
        self.modes(initials="enforce")
        self.assertEqual(self.hit("s"*1000+"必须处理，日结加我")["word"], "必须处理")

    def test_data_hashes_polyphone_coverage_and_additive_persistence(self):
        source = json.loads((ROOT/"pinyin-source.json").read_text(encoding="utf-8"))
        self.assertEqual(len(polyphones()), source["polyphones"])
        for name, key in (("pinyin-polyphones.json", "polyphones_sha256"), ("pinyin-phrases.json", "phrases_sha256")):
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(), source[key])
        self.assertIn("hang", polyphones()["行"])
        self.assertIn("yue", polyphones()["乐"])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "old.db")
            store = Store(path)
            store.ensure_chat(-1, "旧群")
            store.db.execute("UPDATE chats SET settings=? WHERE chat_id=-1", (json.dumps({"word_mixed_pinyin_mode": "enforce"}),))
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.variant_modes(-1)["pinyin"], "enforce")
                self.assertEqual(store.variant_modes(-1)["initials"], "off")
                self.assertEqual(store.variant_modes(-1)["phonetic"], "off")
                store.set_variant_modes(-1, {"initials": "observe", "phonetic": "enforce"})
            finally:
                store.close()
            store = Store(path)
            try:
                self.assertEqual(store.variant_modes(-1)["initials"], "observe")
                self.assertEqual(store.variant_modes(-1)["phonetic"], "enforce")
            finally:
                store.close()
