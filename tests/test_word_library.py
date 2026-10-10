"""Large word lists, observed policies, file workflows and delayed membership."""
import hashlib
import json
import os
import random
import re
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.api import TelegramAPI, TelegramAPIError
from app.moderation import compile_words, compact, match_words, normalize
from app.ocr import matched_words
from app.service import GroupBot
from app.store import Store
from app.word_library import ROOT, catalog, parse_import, IMPORT_BYTES
from tests.test_service import FakeTelegramAPI


class LibraryDataTests(unittest.TestCase):
    def test_pinned_sources_hashes_license_and_reproducible_original_lists(self):
        manifest = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
        for source in manifest:
            self.assertEqual(hashlib.sha256((ROOT / source["file"]).read_bytes()).hexdigest(), source["sha256"])
            self.assertEqual(source["license"], "Apache-2.0")
            self.assertIn(source["revision"], source["url"])
        self.assertIn("Apache License", (ROOT / "LICENSE-Apache-2.0").read_text(encoding="utf-8"))
        packs = catalog()
        self.assertEqual(len(packs), 13)
        self.assertGreater(len({w for p in packs.values() for w in p.words}), 6000)
        for pack in packs.values():
            self.assertTrue(all(2 <= len(w) <= 80 and normalize(w) == w for w in pack.words))
            self.assertEqual(len(pack.words), len(set(pack.words)))
            if not pack.id.startswith("public_"):
                self.assertEqual((ROOT / ("curated_"+pack.id+".txt")).read_text(encoding="utf-8").splitlines(), list(pack.words))

    def test_recommended_phrases_do_not_block_common_single_topic_words(self):
        corpus = ["今天学习投资风险管理", "我在研究股票和期货", "介绍USDT和比特币的区别", "管理员不会主动私聊",
                  "机器人不会索要验证码", "如何保护钱包私钥", "性教育与健康科普", "我们讨论网络安全和漏洞修复",
                  "下载Telegram手机客户端", "请不要参与刷单", "整理银行账户和电话号码", "今天去看足球比赛"]
        for pack in catalog().values():
            if pack.recommended:
                for text in corpus:
                    self.assertIsNone(match_words(text, pack.matcher), (pack.id, text))
        for text in ("周小川", "资金周转", "下载速度", "免费使用", "证监会", "视频来源"):
            self.assertIsNone(match_words(text, catalog()["public_ads"].matcher), text)

    def test_multiformat_import_normalizes_and_deduplicates(self):
        content = '\ufeff# 注释\nＡＤ\nword\tCONTACT\n{"word":"联系客服","mode":"exact"}\nAD\n'
        bundle = parse_import(content.encode("utf-8"))
        self.assertEqual(bundle["total"], 4)
        self.assertEqual(bundle["duplicates"], 1)
        self.assertEqual([r[2] for r in bundle["entries"]], ["contains", "word", "exact"])
        self.assertEqual(bundle["entries"][0][0], "ad")

    def test_invalid_imports_never_execute_patterns_or_partial_data(self):
        for text in ("", "# only comments", "ab\nword\tab\n", "ok\nregex\t.*", "ok\n{bad json}",
                     '{"word":"x"}', '{"word":10}', '{"word":"abc","extra":true}', 'bad\x00word', b'\xff\xfe'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_import(text)
        with self.assertRaises(ValueError):
            parse_import(b"a"*(IMPORT_BYTES+1))
        literal = parse_import("a+b\n.*\n")
        self.assertEqual(literal["entries"][0][0], "a+b")

    def test_deep_json_surrogates_and_invalid_labels_are_rejected(self):
        for content in ('{"word":'+'['*2000+'0'+']'*2000+'}',
                        '{"word":"abc","label":"\\ud800"}', '{"word":"\\udfffabc"}',
                        '{"word":"abc","label":"bad\\u0000label"}'):
            with self.subTest(content=content[:60]), self.assertRaises(ValueError):
                parse_import(content)

    def test_automaton_matches_reference_with_unicode_modes_loose_and_exceptions(self):
        rng = random.Random(42)
        words = ["a", "ab", "aba", "baba", "汉字", "广告联系", "a+b", "contact", "ab_1"]
        for _ in range(250):
            modes = {w: rng.choice(("contains", "word", "exact")) for w in words}
            text = "".join(rng.choice(("a", "b", "汉字", " 广告联系 ", "a+b", "contact", "_", "⭐", " ")) for _ in range(8))
            loose = rng.choice((True, False))
            waived_word = rng.choice(words)
            exceptions = [waived_word+"|"+text]
            expected = None
            for word in words:
                phrase, clean, mode = normalize(word), normalize(text), modes[word]
                minimum = 3 if any("\u3400" <= c <= "\u9fff" for c in compact(phrase)) else 4
                found = (clean == phrase if mode == "exact" else
                         bool(re.search(r"(?<!\w)"+re.escape(phrase)+r"(?!\w)", clean)) if mode == "word" else
                         phrase in clean or loose and len(compact(phrase)) >= minimum and compact(phrase) in compact(clean))
                if found and word != waived_word:
                    expected = word
                    break
            self.assertEqual(match_words(text, compile_words(words, modes), loose=loose, exceptions=exceptions), expected)

    def test_suffix_links_priority_and_fifty_thousand_rules(self):
        self.assertEqual(match_words("ushers", ["he", "she", "hers", "his"]), "he")
        self.assertEqual(match_words("baba", ["aba", "ba", "a"]), "aba")
        words = tuple(f"独立广告规则{i:06}完成" for i in range(50000))
        matcher = compile_words(words)
        self.assertIsNone(match_words("正常聊天"*200, matcher))
        self.assertEqual(match_words("前缀"+words[-1]+"后缀", matcher), words[-1])
        self.assertEqual(matched_words(" ".join(words[-1]), 95, words), [words[-1]])

    def test_all_categories_reuse_ocr_indexes_after_cache_churn(self):
        packs = tuple(catalog().values())
        compiled = [pack.ocr_rules for pack in packs]
        with patch("app.ocr.compile_ocr_words", side_effect=AssertionError("unexpected recompile")):
            for pack, rules in zip(packs, compiled):
                self.assertIs(pack.ocr_rules, rules)
                self.assertEqual(matched_words("普通交流", 95, (), compiled=pack.ocr_rules), [])


class LibraryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001, "测试群")
        self.store.ensure_chat(-1002, "另一群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api, self.store, 999, "MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def click(self, data, uid=1, mid=42):
        self.bot.handle_update({"callback_query": {"id": "q", "from": {"id": uid}, "data": data,
            "message": {"message_id": mid, "chat": {"id": uid, "type": "private"}}}})

    def private(self, text="", **extra):
        self.bot.handle_update({"message": {"message_id": 20, "text": text, "from": {"id": 1},
            "chat": {"id": 1, "type": "private"}, **extra}})

    def message(self, text, mid=1, uid=2, **extra):
        return {"message_id": mid, "text": text, "from": {"id": uid},
                "chat": {"id": -1001, "type": "supergroup"}, **extra}

    def group(self, text, **extra):
        self.bot.handle_update({"message": self.message(text, **extra)})

    def confirm(self):
        d = dict(self.bot.admin_ui.drafts[1])
        self.click(f"ac:{d['cid']}:{d['nonce']}", mid=d["prompt_id"])
        return d

    def page(self):
        return [p for m, p in self.api.calls if m in ("sendMessage", "editMessageText")][-1]

    def test_library_modes_preview_commit_binding_and_replay(self):
        self.click("v:-1001:wordpacks")
        self.assertIn("13类", self.page()["text"])
        self.click("ad:-1001:packmode:task_scam_observe")
        d = dict(self.bot.admin_ui.drafts[1])
        self.assertFalse(self.store.pack_modes(-1001))
        self.click(f"ac:-1001:{d['nonce']}", uid=999, mid=d["prompt_id"])
        self.assertFalse(self.store.pack_modes(-1001))
        self.confirm()
        self.assertEqual(self.store.pack_modes(-1001)["task_scam"], "observe")
        self.store.set_pack_modes(-1001, {"task_scam": "off"})
        self.click(f"ac:-1001:{d['nonce']}", mid=d["prompt_id"])
        self.assertEqual(self.store.pack_modes(-1001)["task_scam"], "off")

    def test_pack_card_pagination_short_callbacks_and_exclusion_confirmation(self):
        self.click("ad:-1001:packpage:task_scam_101")
        self.assertIn("第102/", self.page()["text"])
        for row in self.page()["reply_markup"]["inline_keyboard"]:
            for b in row:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)
        data = next(b["callback_data"] for row in self.page()["reply_markup"]["inline_keyboard"] for b in row
                    if b["callback_data"].startswith("ad:-1001:pexclude:"))
        self.click(data)
        word = self.bot.admin_ui.drafts[1]["values"]["text"]
        self.confirm()
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.assertIsNone(self.store.word_policy(-1001).hit({"text": word}))

    def test_stale_and_revoked_pack_changes_do_not_commit(self):
        self.click("ad:-1001:packmode:task_scam_enforce")
        self.store.add_filter(-1001, "word", "新增规则", "新增规则")
        self.confirm()
        self.assertFalse(self.store.pack_modes(-1001))
        self.click("ad:-1001:packmode:task_scam_enforce")
        with patch.object(self.bot, "is_admin", return_value=False):
            self.confirm()
        self.assertFalse(self.store.pack_modes(-1001))

    def test_preset_only_observes_and_permission_rechecked_before_enforcement(self):
        self.click("ad:-1001:packpreset:all")
        self.confirm()
        self.assertEqual(set(self.store.pack_modes(-1001).values()), {"observe"})
        self.click("ad:-1001:packmode:task_scam_enforce")
        with patch.object(self.bot, "bot_can", return_value=False):
            self.confirm()
        self.assertEqual(self.store.pack_modes(-1001)["task_scam"], "observe")

    def test_observe_allows_business_but_never_masks_external_link_violation(self):
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.store.set(-1001, "points_enabled", True)
        self.group("/checkin", reply_markup={"inline_keyboard": [[{"text": "刷单返佣", "callback_data": "ad"}]]})
        self.assertGreater(self.store.point_balance(-1001, 2)["balance"], 0)
        event = self.store.audit_page(-1001)[0][0]
        self.assertEqual((event["outcome"], event["pack_id"], event["rule_key"]), ("observed", "task_scam", "刷单返佣"))
        self.group("刷单返佣 https://bad.example.com", mid=2, uid=3)
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "deleted")
        self.assertEqual([p["message_id"] for m, p in self.api.calls if m == "deleteMessage"], [2])

    def test_enforce_blocks_commands_and_global_observe_and_total_off_take_priority(self):
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.store.set(-1001, "points_enabled", True)
        self.group("/checkin", reply_markup={"inline_keyboard": [[{"text": "刷单返佣", "callback_data": "ad"}]]})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 0)
        self.store.set(-1001, "observe_only", True)
        self.group("刷单返佣", mid=2, uid=3)
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "observed")
        self.store.set(-1001, "word_filter_enabled", False)
        self.group("刷单返佣", mid=3, uid=4)
        self.assertEqual(len(self.store.audit_page(-1001)[0]), 2)

    def test_custom_override_exclusions_context_waiver_and_group_scope(self):
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        self.assertIsNone(self.store.word_policy(-1002).hit({"text": "刷单返佣"}))
        self.store.add_filter(-1001, "word", "刷单返佣", "刷单返佣")
        self.store.edit_word_rule(-1001, "刷单返佣", "exact")
        self.assertIsNone(self.store.word_policy(-1001).hit({"text": "拒绝刷单返佣"}))
        self.assertEqual(self.store.word_policy(-1001).hit({"text": "刷单返佣"})["pack"], "")
        self.store.del_filter(-1001, "word", "刷单返佣")
        self.store.exclude_word(-1001, "刷单返佣")
        self.assertIsNone(self.store.word_policy(-1001).hit({"text": "刷单返佣"}))
        self.store.exclude_word(-1001, "刷单返佣", exclude=False)
        self.store.add_filter(-1001, "word_exception", "刷单返佣|拒绝刷单返佣", "刷单返佣|拒绝刷单返佣")
        self.assertIsNone(self.store.word_policy(-1001).hit({"text": "拒绝刷单返佣"}))
        self.assertIsNotNone(self.store.word_policy(-1001).hit({"text": "拒绝刷单返佣，联系客服"}))

    def test_enforcement_pack_beats_observed_pack(self):
        self.store.set_pack_modes(-1001, {"task_scam": "observe", "finance_scam": "enforce"})
        hit = self.store.word_policy(-1001).hit({"text": "刷单返佣 包赚不赔"})
        self.assertFalse(hit["observe"])
        self.assertEqual(hit["pack"], "finance_scam")

    def test_ocr_uses_pack_observation_and_metadata(self):
        self.store.set_pack_modes(-1001, {"task_scam": "observe"})
        self.store.set(-1001, "ocr_mode", "delete")
        event = {"chat_id": -1001, "user_id": 2, "message_id": 77, "created_at": int(time.time())}
        with patch.object(self.bot.ocr, "results", return_value=[(event, "刷 单 返 佣", 95)]):
            self.bot.drain_ocr()
        result = self.store.audit_page(-1001)[0][0]
        self.assertEqual((result["outcome"], result["pack_id"]), ("observed", "task_scam"))
        self.assertFalse(any(m == "deleteMessage" for m, _ in self.api.calls))
        self.store.set_pack_modes(-1001, {"task_scam": "enforce"})
        with patch.object(self.bot.ocr, "results", return_value=[(event, "刷 单 返 佣", 95)]):
            self.bot.drain_ocr()
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"], "deleted")

    def test_text_and_document_import_confirmation_then_roundtrip_export(self):
        self.store.add_filter(-1001, "word", "ad", "保留标签")
        self.store.edit_word_rule(-1001, "ad", "word")
        self.click("ad:-1001:begin:word_import")
        self.private("exact\tad\nword\tcontact\n联系客服")
        self.assertEqual(self.store.filter_count(-1001, "word"), 1)
        self.confirm()
        self.assertEqual(self.store.word_rule(-1001, "ad")["mode"], "word")
        self.click("ad:-1001:begin:word_import")
        with patch.object(self.api, "fetch_file", create=True, return_value=b"another word\n"):
            self.private(document={"file_id": "file1", "file_size": 13})
        self.confirm()
        self.assertEqual(self.store.filter_count(-1001, "word"), 4)
        self.click("ad:-1001:wordexport:0")
        file = [p["_files"]["document"] for m, p in self.api.calls if m == "sendDocument"][-1]
        bundle = parse_import(file[1])
        self.assertEqual(len(bundle["entries"]), 4)
        self.assertEqual(bundle["entries"][0], ("ad", "保留标签", "word"))

    def test_invalid_oversize_download_failure_and_stale_import_preserve_database(self):
        for payload in ("valid\nregex\t.*", "x"):
            self.click("ad:-1001:begin:word_import")
            self.private(payload)
            self.assertFalse(self.bot.admin_ui.drafts[1]["confirm"])
        self.private(document={"file_id": "file", "file_size": IMPORT_BYTES+1})
        self.assertFalse(self.bot.admin_ui.drafts[1]["confirm"])
        with patch.object(self.api, "fetch_file", create=True, side_effect=TelegramAPIError("getFile")):
            self.private(document={"file_id": "file"})
        self.assertFalse(self.bot.admin_ui.drafts[1]["confirm"])
        self.private("newword")
        self.store.add_filter(-1001, "word", "otherword", "otherword")
        self.confirm()
        self.assertIsNone(self.store.word_rule(-1001, "newword"))

    def test_unlimited_custom_import_and_pagination_beyond_old_hundred_pages(self):
        entries = parse_import("\n".join(f"广告词{i:05}" for i in range(2000)))["entries"]
        self.store.import_words(-1001, entries)
        self.assertEqual(self.store.filter_count(-1001, "word"), 2000)
        self.click("wl:-1001:150")
        self.assertIn("第 151/250 页", self.page()["text"])
        self.assertEqual(len(self.store.filter_page(-1001, "word", 150)), 8)

    def test_cache_survives_unrelated_statistics_and_invalidates_on_rule_sql(self):
        self.store.add_filter(-1001, "word", "badword", "badword")
        policy = self.store.word_policy(-1001)
        self.store.stat(-1001, "messages")
        self.assertIs(self.store.word_policy(-1001), policy)
        self.store.db.execute("UPDATE filters SET key='newword' WHERE chat_id=-1001 AND kind='word'")
        replacement = self.store.word_policy(-1001)
        self.assertIsNot(replacement, policy)
        self.assertIsNone(replacement.hit({"text": "badword"}))
        self.assertIsNotNone(replacement.hit({"text": "newword"}))

    def test_export_segments_have_no_skipped_or_duplicate_entries(self):
        self.store.import_words(-1001, parse_import("\n".join(f"word{i:03}" for i in range(30)))["entries"])
        after, exported = "", []
        while True:
            data, after, more, count = self.store.export_words(-1001, after, max_bytes=250)
            self.assertGreater(count, 0)
            exported.extend(r[0] for r in parse_import(data)["entries"])
            if not more:
                break
        self.assertEqual(exported, [f"word{i:03}" for i in range(30)])

    def test_export_cursor_checks_identity_group_purpose_and_revision(self):
        self.store.import_words(-1001, parse_import("\n".join(f"word{i:03}" for i in range(20)))["entries"])
        original = self.store.export_words
        with patch.object(self.store, "export_words", side_effect=lambda cid, after="": original(cid, after, max_bytes=250)):
            self.click("ad:-1001:wordexport:0")
            next_button = self.page()["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            self.click(next_button, uid=999)
            self.click(next_button.replace("-1001", "-1002"))
            search_token = self.bot.admin_ui._token(self.bot.admin_ui.searches, 1, -1001, query="word")
            self.click(f"ad:-1001:wordexport:{search_token}")
            self.assertEqual(sum(m == "sendDocument" for m, _ in self.api.calls), 1)
            self.click(next_button)
            documents = [p["_files"]["document"][1] for m, p in self.api.calls if m == "sendDocument"]
            self.assertEqual(len(documents), 2)
            first, second = ({r[0] for r in parse_import(data)["entries"]} for data in documents)
            self.assertFalse(first & second)
            self.store.add_filter(-1001, "word", "newword", "newword")
            self.click(next_button)
            self.assertEqual(sum(m == "sendDocument" for m, _ in self.api.calls), 2)

    def test_candidate_validation_skips_departed_and_never_redraws(self):
        self.bot.raffles.create(-1001, 1, "免费活动")
        self.bot.raffles.join(-1001, 2)
        self.bot.raffles.join(-1001, 3)
        original = self.api.call
        def membership(method, **kwargs):
            if method == "getChatMember" and kwargs.get("user_id") == 2:
                return {"status": "restricted", "is_member": False}
            return original(method, **kwargs)
        with patch.object(self.api, "call", membership), patch("app.service.secrets.randbelow", return_value=0):
            result = self.bot.draw_raffle(-1001, 1)
        self.assertEqual(result["winner_id"], 3)
        self.assertEqual(result["count"], 1)
        with self.assertRaises(ValueError):
            self.bot.draw_raffle(-1001, 1)

    def test_candidate_unknown_identity_keeps_activity_and_entries(self):
        self.bot.raffles.create(-1001, 1, "免费活动")
        self.bot.raffles.join(-1001, 2)
        for status in (None, {}, {"status": "unknown"}, {"status": "restricted"}):
            with patch.object(self.api, "call", return_value=status), self.assertRaises(ValueError):
                self.bot.draw_raffle(-1001, 1)
        self.assertIsNotNone(self.bot.raffles.active(-1001))
        self.assertEqual(self.bot.raffles.history(-1001)[0]["count"], 1)

    def test_raffle_checks_at_most_three_departed_candidates_and_stays_open(self):
        self.bot.raffles.create(-1001, 1, "免费活动")
        for uid in range(2, 7):
            self.bot.raffles.join(-1001, uid)
        with patch.object(self.api, "call", return_value={"status": "left"}) as lookup:
            with self.assertRaises(ValueError):
                self.bot.draw_raffle(-1001, 1)
        self.assertEqual(lookup.call_count, 3)
        self.assertIsNotNone(self.bot.raffles.active(-1001))
        self.assertEqual(self.bot.raffles.history(-1001)[0]["count"], 2)

    def test_activity_change_while_candidate_is_checked_does_not_draw_replacement(self):
        for status in ("member", "left"):
            with self.subTest(status=status):
                self.bot.raffles.cancel(-1001, 1)
                old = self.bot.raffles.create(-1001, 1, "旧活动")
                self.bot.raffles.join(-1001, 2)
                def change_activity(method, **params):
                    self.bot.raffles.cancel(-1001, 1)
                    self.bot.raffles.create(-1001, 1, "新活动")
                    self.bot.raffles.join(-1001, 2)
                    return {"status": status}
                with patch.object(self.api, "call", change_activity), self.assertRaises(ValueError):
                    self.bot.draw_raffle(-1001, 1, expected_id=old)
                self.assertEqual(self.bot.raffles.active(-1001)["title"], "新活动")
                self.assertEqual(self.bot.raffles.history(-1001)[0]["count"], 1)

    def test_transfer_rechecks_restricted_departed_and_pending_members(self):
        self.store.set(-1001, "points_enabled", True)
        self.store.change_points(-1001, 2, 50, 1)
        intent = self.bot.economy.new_transfer(-1001, 2, 3, 20, 33)
        self.bot.economy.bind_prompt(-1001, intent["id"], 77)
        query = {"id": "q", "from": {"id": 2}, "data": f"et:-1001:{intent['id']}:{intent['nonce']}",
                 "message": {"message_id": 77, "chat": {"id": -1001, "type": "supergroup"}}}
        original = self.api.call
        def membership(method, **params):
            if method == "getChatMember" and params.get("user_id") == 3:
                return {"status": "restricted", "is_member": False}
            return original(method, **params)
        with patch.object(self.api, "call", membership):
            self.bot.handle_update({"callback_query": query})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 50)
        self.store.put_pending(-1001, 3, "nonce", 0, int(time.time())+300)
        self.bot.handle_update({"callback_query": query})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 50)
        self.store.del_pending(-1001, 3)
        self.bot.handle_update({"callback_query": query})
        self.bot.handle_update({"callback_query": query})
        self.assertEqual(self.store.point_balance(-1001, 2)["balance"], 30)
        self.assertEqual(self.store.point_balance(-1001, 3)["balance"], 20)


class PersistenceAndAPITests(unittest.TestCase):
    def test_other_connection_invalidates_snapshot_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "library.db")
            first, second = Store(path), Store(path)
            try:
                first.ensure_chat(-1, "旧群")
                previous = first.word_policy(-1)
                second.set_pack_modes(-1, {"task_scam": "observe"})
                self.assertIsNot(first.word_policy(-1), previous)
                self.assertTrue(first.word_policy(-1).hit({"text": "刷单返佣"})["observe"])
                second.exclude_word(-1, "刷单返佣")
                self.assertIsNone(first.word_policy(-1).hit({"text": "刷单返佣"}))
            finally:
                first.close()
                second.close()
            reopened = Store(path)
            try:
                self.assertEqual(reopened.pack_modes(-1)["task_scam"], "observe")
                self.assertIsNone(reopened.word_policy(-1).hit({"text": "刷单返佣"}))
                self.assertEqual(list(reopened.db.execute("PRAGMA foreign_key_check")), [])
            finally:
                reopened.close()

    def test_document_upload_uses_multipart_and_retains_json_for_normal_calls(self):
        api = TelegramAPI("not-a-real-token")
        session = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": 10}}
        session.post.return_value = response
        try:
            with patch.object(api, "_session", return_value=session):
                api.call("sendDocument", chat_id=1, _files={"document": ("words.jsonl", b"abc", "text/plain")})
                opts = session.post.call_args.kwargs
                self.assertIn("files", opts)
                self.assertNotIn("json", opts)
                self.assertFalse(opts["allow_redirects"])
                api.call("sendMessage", chat_id=1, text="hello")
                self.assertIn("json", session.post.call_args.kwargs)
        finally:
            api.close()


if __name__ == "__main__":
    unittest.main()
