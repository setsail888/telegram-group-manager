"""Offline end-to-end button flows and moderation boundary regressions."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from app.admin_ui import FORMS
from app.economy import Economy
from app.i18n import LOCALES, render
from app.moderation import evaluate, FloodGuard
from app.ocr import matched_words, OCR_MODES
from app.service import GroupBot, MENU_MODULES
from app.store import Store, DEFAULTS
from tests.test_service import FakeTelegramAPI


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.ensure_chat(-1001,"测试群")
        self.store.ensure_chat(-1002,"另一群")
        self.api = FakeTelegramAPI()
        self.bot = GroupBot(self.api,self.store,999,"MyBot")

    def tearDown(self):
        self.bot.ocr.shutdown()
        self.store.close()

    def click(self,data,uid=1,mid=42,private=True):
        self.bot.member_ui.last.clear()  # Each helper call represents a separate human click.
        self.bot.handle_update({"callback_query":{"id":"q", "from":{"id":uid},"data":data,
            "message":{"message_id":mid,"chat":{"type":"private" if private else "supergroup","id":uid if private else -1001}}}})

    def input(self,text=None,uid=1,**extra):
        message = {"message_id":20,"from":{"id":uid},"chat":{"type":"private","id":uid},**extra}
        if text is not None:
            message["text"] = text
        self.bot.handle_update({"message":message})

    def confirm(self,uid=1):
        draft = self.bot.admin_ui.drafts[uid]
        self.click(f"ac:{draft['cid']}:{draft['nonce']}",uid,draft["prompt_id"])

    def form(self,op,*values):
        self.click(f"ad:-1001:begin:{op}")
        for value in values:
            self.input(value)

    def message(self,text,mid=1,uid=2,**extra):
        return {"message_id":mid,"text":text,"from":{"id":uid},
                "chat":{"id":-1001,"type":"supergroup","title":"测试群"},**extra}

    def edits(self):
        return [p for method,p in self.api.calls if method in ("sendMessage","editMessageText")]

    def test_all_modules_have_real_pages_and_short_callback_data(self):
        for _label,page in MENU_MODULES:
            if page=="groups":
                continue
            self.api.calls.clear()
            self.click(f"v:-1001:{page}")
            result = self.edits()[-1]
            self.assertNotIn("暂不可用",result["text"],page)
            for row in result["reply_markup"]["inline_keyboard"]:
                for button in row:
                    if "callback_data" in button:
                        self.assertLessEqual(len(button["callback_data"].encode()),64)
            auth = [p for m,p in self.api.calls if m=="getChatMember" and p["user_id"]==1]
            self.assertEqual(len(auth),1,page)

    def test_domains_batch_preview_commit_then_remove(self):
        self.form("domain_add","Example.com\nhttps://docs.python.org/start")
        self.assertFalse(self.store.filters(-1001,"domain"))
        self.confirm()
        self.assertEqual(len(self.store.filters(-1001,"domain")),2)
        self.click("ad:-1001:domain:0")
        self.confirm()
        self.assertEqual(len(self.store.filters(-1001,"domain")),1)

    def test_invalid_domain_batch_is_atomic_and_retryable(self):
        self.form("domain_add","good.com\nnot-a-domain")
        self.assertFalse(self.store.filters(-1001,"domain"))
        self.assertFalse(self.bot.admin_ui.drafts[1]["confirm"])
        self.input("good.com")
        self.confirm()
        self.assertEqual(len(self.store.filters(-1001,"domain")),1)

    def test_bulk_domain_capacity_does_not_partially_apply(self):
        for i in range(299):
            self.store.add_filter(-1001,"domain",f"s{i}.com",f"s{i}.com")
        self.form("domain_add","new.com\nextra.com")
        self.confirm()
        self.assertEqual(self.store.filter_count(-1001,"domain"),299)

    def test_reply_text_media_and_button_forms(self):
        self.form("reply_add","你好,hello","欢迎加入","官网 - https://example.com")
        self.confirm()
        self.assertEqual(self.store.replies(-1001)[0]["reply"],"欢迎加入")
        self.click("ad:-1001:begin:reply_add")
        self.input("帮助")
        self.input(photo=[{"file_id":"photo123"}],caption="使用指南")
        self.input("-")
        self.confirm()
        media = next(r for r in self.store.replies(-1001) if r["media_type"])
        self.assertEqual(media["media_id"],"photo123")
        self.assertEqual(media["reply"],"使用指南")

    def test_reply_overwrite_detects_concurrent_admin_change(self):
        self.store.add_reply(-1001,"帮助","帮助","旧内容")
        self.form("reply_add","帮助","新内容","-")
        self.store.add_reply(-1001,"帮助","帮助","另一个管理员编辑")
        self.confirm()
        self.assertEqual(self.store.replies(-1001)[0]["reply"],"另一个管理员编辑")

    def test_forms_for_rules_welcome_points_and_schedule(self):
        for op,text in (("rules_text","请文明交流"),("welcome_text","你好 {name}")):
            self.form(op,text)
            self.confirm()
            self.assertEqual(self.store.settings(-1001)[op],text)
        self.form("point_adjust","2","10")
        self.confirm()
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],10)
        for mode,rule in (("daily","09:00"),("weekly","一 09:00"),("once","2099-01-01 09:00")):
            # One-off fixture must be future and inside the one-year limit.
            if mode=="once":
                from datetime import datetime,timedelta
                rule = (datetime.now(self.store.tz)+timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
            self.form("schedule_"+mode,rule,"活动提醒","今晚有活动")
            self.confirm()
        self.assertEqual(len(self.store.schedules(-1001)),3)

    def test_cancel_navigation_expiry_and_binding_invalidate_confirmation(self):
        for kind in ("cancel","navigate","expire","wrong_message","wrong_group","wrong_user"):
            with self.subTest(kind=kind):
                original = self.store.settings(-1001)["rules_text"]
                self.form("rules_text","新群规")
                d = dict(self.bot.admin_ui.drafts[1])
                if kind=="cancel":
                    self.input("/cancel")
                elif kind=="navigate":
                    self.click("g:-1001")
                elif kind=="expire":
                    self.bot.admin_ui.drafts[1]["expires"] = 0
                self.click(f"ac:{-1002 if kind=='wrong_group' else -1001}:{d['nonce']}",
                           2 if kind=="wrong_user" else 1,
                           99 if kind=="wrong_message" else d["prompt_id"])
                self.assertEqual(self.store.settings(-1001)["rules_text"],original)

    def test_revoked_permission_at_input_and_commit(self):
        self.form("rules_text","新群规")
        with patch.object(self.bot,"is_admin",return_value=False):
            self.confirm()
        self.assertEqual(self.store.settings(-1001)["rules_text"],DEFAULTS["rules_text"])
        self.click("ad:-1001:begin:rules_text")
        with patch.object(self.bot,"is_admin",return_value=False):
            self.input("新的内容")
        self.assertNotIn(1,self.bot.admin_ui.drafts)

    def test_confirm_replay_executes_only_once(self):
        self.form("point_adjust","2","10")
        d = dict(self.bot.admin_ui.drafts[1])
        self.confirm()
        self.click(f"ac:-1001:{d['nonce']}",mid=d["prompt_id"])
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],10)

    def test_shop_order_fulfill_refund_and_cross_group(self):
        self.store.set(-1001,"points_enabled",True)
        self.store.change_points(-1001,2,100,1)
        self.form("item_add","小奖励","10","3","管理员人工核销")
        self.confirm()
        item = self.bot.economy.items(-1001)[0]
        order = self.bot.economy.purchase(-1001,2,item["id"],5)
        self.click(f"ad:-1002:refund:{order['id']}")
        self.assertNotIn(1,self.bot.admin_ui.drafts)
        self.click(f"ad:-1001:refund:{order['id']}")
        self.confirm()
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],100)
        order = self.bot.economy.purchase(-1001,2,item["id"],6)
        self.click(f"ad:-1001:fulfill:{order['id']}")
        self.confirm()
        self.assertEqual(self.bot.economy.orders(-1001)[0]["status"],"fulfilled")
        self.click(f"ad:-1001:item:{item['id']}")
        self.confirm()
        self.assertFalse(self.bot.economy.items(-1001))

    def test_raffle_create_draw_and_stale_confirmation(self):
        self.form("raffle_add","免费小活动")
        self.confirm()
        activity = self.bot.raffles.active(-1001)
        self.bot.raffles.join(-1001,2)
        self.click(f"ad:-1001:draw:{activity['id']}")
        self.confirm()
        self.assertEqual(self.bot.raffles.history(-1001)[0]["winner_id"],2)
        self.assertTrue(any("获胜成员ID" in p["text"] for p in self.edits()))
        old = self.bot.raffles.create(-1001,1,"活动二")
        self.click(f"ad:-1001:raffle_cancel:{old}")
        self.bot.raffles.cancel(-1001,1)
        current = self.bot.raffles.create(-1001,1,"活动三")
        self.confirm()
        self.assertEqual(self.bot.raffles.active(-1001)["id"],current)

    def test_member_management_protects_admin_and_handles_api_failure(self):
        self.form("member_ban","1")
        self.confirm()
        self.assertFalse(any(m=="banChatMember" for m,p in self.api.calls))
        self.form("member_mute","2","20")
        self.confirm()
        self.assertTrue(any(m=="restrictChatMember" and p["user_id"]==2 for m,p in self.api.calls))
        self.form("member_kick","2")
        original = self.api.safe
        with patch.object(self.api,"safe",side_effect=lambda m,**k: False if m=="banChatMember" else original(m,**k)):
            self.confirm()
        self.assertTrue(any(p.get("show_alert") for m,p in self.api.calls if m=="answerCallbackQuery"))

    def test_ocr_modes_need_engine_and_delete_permission(self):
        self.click("ad:-1001:ocr:delete")
        self.assertNotIn(1, self.bot.admin_ui.drafts)
        self.assertIn("gg ocr", self.edits()[-1]["text"])
        self.assertEqual(self.store.settings(-1001)["ocr_mode"],"off")
        self.bot.ocr.available = True
        self.click("ad:-1001:ocr:review")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["ocr_mode"],"review")
        self.click("ad:-1001:ocr:delete")
        with patch.object(self.bot,"bot_can",return_value=False):
            self.confirm()
        self.assertEqual(self.store.settings(-1001)["ocr_mode"],"review")

    def test_ocr_current_off_is_explicit_and_does_not_offer_a_noop_confirmation(self):
        self.click("v:-1001:ocr")
        page = self.edits()[-1]
        buttons = [b for row in page["reply_markup"]["inline_keyboard"] for b in row]
        self.assertIn("OCR 模式：关闭（off）", page["text"])
        self.assertEqual([b["text"] for b in buttons if b.get("callback_data") == "ad:-1001:ocr:off"], ["✅ 关闭（off）"])
        self.click("ad:-1001:ocr:off")
        self.assertNotIn(1, self.bot.admin_ui.drafts)
        self.assertEqual(self.store.settings(-1001)["ocr_mode"], "off")
        self.assertTrue(any("无需重复保存" in p.get("text", "") and p.get("show_alert")
                            for method, p in self.api.calls if method == "answerCallbackQuery"))

    def test_ocr_enable_without_engine_shows_setup_and_does_not_create_drafts(self):
        for mode in ("review", "delete"):
            self.api.calls.clear()
            self.click(f"ad:-1001:ocr:{mode}")
            self.assertNotIn(1, self.bot.admin_ui.drafts)
            self.assertEqual(self.store.settings(-1001)["ocr_mode"], "off")
            self.assertIn("gg ocr", self.edits()[-1]["text"])
            self.assertIn("选择1", self.edits()[-1]["text"])
            self.assertNotIn("确认后生效", self.edits()[-1]["text"])
            self.assertTrue(any("未更改群模式" in p.get("text", "") and p.get("show_alert")
                                for method, p in self.api.calls if method == "answerCallbackQuery"))

    def test_ocr_confirmation_names_both_modes_and_refreshes_saved_selection(self):
        self.bot.ocr.available = True
        for mode, previous, target in (("review", "关闭（off）", "仅审核（review）"),
                                        ("delete", "仅审核（review）", "自动删除（delete）"),
                                        ("off", "自动删除（delete）", "关闭（off）")):
            self.click(f"ad:-1001:ocr:{mode}")
            self.assertIn(f"{previous} → {target}", self.edits()[-1]["text"])
            draft = dict(self.bot.admin_ui.drafts[1])
            self.confirm()
            self.assertEqual(self.store.settings(-1001)["ocr_mode"], mode)
            page = self.edits()[-1]
            self.assertEqual(page["message_id"], draft["prompt_id"])
            self.assertIn("OCR 模式："+target, page["text"])
            buttons = [b for row in page["reply_markup"]["inline_keyboard"] for b in row]
            self.assertEqual([b["text"] for b in buttons if b.get("callback_data") == f"ad:-1001:ocr:{mode}"], ["✅ "+target])

    def test_ocr_confirmation_rechecks_engine_and_concurrent_mode_changes(self):
        self.bot.ocr.available = True
        self.click("ad:-1001:ocr:review")
        self.bot.ocr.available = False
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["ocr_mode"], "off")
        self.bot.ocr.available = True
        self.click("ad:-1001:ocr:review")
        self.store.set(-1001, "ocr_mode", "delete")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["ocr_mode"], "delete")
        self.assertTrue(any("OCR模式已变化" in p.get("text", "")
                            for method, p in self.api.calls if method == "answerCallbackQuery"))

    def test_ocr_can_be_disabled_after_server_engine_is_disabled(self):
        self.store.set(-1001, "ocr_mode", "review")
        self.click("ad:-1001:ocr:off")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["ocr_mode"], "off")
        self.assertIn("关闭（off）", self.edits()[-1]["text"])
        self.assertIn("gg ocr", self.edits()[-1]["text"])

    def test_ocr_saved_mode_and_setup_command_in_every_interface_language(self):
        for locale in LOCALES:
            with self.subTest(locale=locale):
                self.store.set_ui_language(1, locale)
                self.store.set(-1001, "ocr_mode", "off")
                self.bot.ocr.available = False
                self.click("v:-1001:ocr")
                self.assertIn("gg ocr", self.edits()[-1]["text"])
                self.bot.ocr.available = True
                self.click("ad:-1001:ocr:review")
                self.confirm()
                page = self.edits()[-1]
                label = render(OCR_MODES["review"], locale)
                self.assertIn(label, page["text"])
                buttons = [b for row in page["reply_markup"]["inline_keyboard"] for b in row]
                self.assertEqual([b["text"] for b in buttons if b.get("callback_data") == "ad:-1001:ocr:review"], ["✅ "+label])
                self.assertEqual(self.store.settings(-1001)["ocr_mode"], "review")
                self.assertLess(len(page["text"].encode("utf-16-le"))//2, 4000)

    def test_logs_require_both_admins_and_post_permission(self):
        self.form("log_chat_id","-1001")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["log_chat_id"],0)
        self.form("log_chat_id","-1002")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["log_chat_id"],-1002)
        self.form("log_chat_id","0")
        self.confirm()
        self.assertEqual(self.store.settings(-1001)["log_chat_id"],0)

    def test_all_numeric_pages_work_and_reject_injected_deltas(self):
        for key,step,page in (("verify_timeout",60,"verify"),("verify_attempt_limit",1,"verify"),
            ("repeat_photo_limit",1,"photo"),("repeat_photo_window",30,"photo"),
            ("checkin_award",1,"points"),("ocr_confidence",5,"ocr"),("reply_group_limit",1,"replies"),("warn_limit",1,"manage")):
            old = self.store.settings(-1001)[key]
            self.click(f"n:-1001:{key}:+{step}:{page}")
            self.assertEqual(self.store.settings(-1001)[key],old+step)
            self.click(f"n:-1001:{key}:+{step*2}:{page}")
            self.assertEqual(self.store.settings(-1001)[key],old+step)

    def test_defense_simulation_does_not_punish(self):
        self.form("spam_test","https://evil.com")
        self.assertTrue(any("外部链接" in p["text"] for p in self.edits()))
        self.assertFalse(any(m=="deleteMessage" for m,p in self.api.calls))

    def test_member_menu_checkin_shop_confirm_orders_and_raffle(self):
        self.store.set(-1001,"points_enabled",True)
        self.input("/start member_1001",uid=2)
        self.click("pc:-1001:checkin:0",uid=2)
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],5)
        self.click("pc:-1001:checkin:0",uid=2)
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],5)
        item = self.bot.economy.add_item(-1001,1,"奖励","说明",2,3)
        self.click(f"pc:-1001:quote:{item}",uid=2)
        token = self.bot.member_ui.quotes[(-1001,2)]["token"]
        self.click(f"pc:-1001:buy:{token}",uid=3)
        self.assertEqual(self.bot.economy.item(-1001,item)["stock"],3)
        self.click(f"pc:-1001:buy:{token}",uid=2)
        self.click(f"pc:-1001:buy:{token}",uid=2)
        self.assertEqual(self.bot.economy.item(-1001,item)["stock"],2)
        self.click("pc:-1001:orders:0",uid=2)
        self.assertIn("待核销",self.edits()[-1]["text"])
        activity = self.bot.raffles.create(-1001,1,"免费抽奖")
        self.click(f"pc:-1001:join:{activity}",uid=2)
        self.click(f"pc:-1001:join:{activity}",uid=2)
        self.assertEqual(self.bot.raffles.history(-1001)[0]["count"],1)

    def test_member_quotes_cancel_expire_or_change_stock(self):
        self.store.set(-1001,"points_enabled",True)
        self.store.change_points(-1001,2,100,1)
        item = self.bot.economy.add_item(-1001,1,"奖励","说明",10,3)
        for kind in ("cancel","expire","wrong_message","changed_stock"):
            self.click(f"pc:-1001:quote:{item}",uid=2)
            quote = self.bot.member_ui.quotes[(-1001,2)]
            token = quote["token"]
            if kind=="cancel":
                self.click("pc:-1001:cancel:0",uid=2)
            elif kind=="expire":
                quote["expires"] = 0
            elif kind=="changed_stock":
                self.bot.economy.purchase(-1001,2,item,30)
            before = self.store.point_balance(-1001,2)["balance"]
            self.click(f"pc:-1001:buy:{token}",uid=2,mid=99 if kind=="wrong_message" else 42)
            self.assertEqual(self.store.point_balance(-1001,2)["balance"],before)

    def test_member_left_or_pending_verification_cannot_use_portal(self):
        self.store.set(-1001,"points_enabled",True)
        self.store.put_pending(-1001,2,"nonce",0,int(time.time())+300)
        self.click("pc:-1001:checkin:0",uid=2)
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],0)
        self.store.del_pending(-1001,2)
        original = self.api.safe
        with patch.object(self.api,"safe",side_effect=lambda m,**p: {"status":"left"} if m=="getChatMember" else original(m,**p)):
            self.click("pc:-1001:checkin:0",uid=2)
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],0)

    def test_member_callbacks_private_only_and_one_fresh_check(self):
        for op in ("balance","top","orders","shop","rules","id","home"):
            self.api.calls.clear()
            self.click(f"pc:-1001:{op}:0",uid=2)
            self.assertEqual(sum(m=="getChatMember" and p["user_id"]==2 for m,p in self.api.calls),1)
        self.api.calls.clear()
        self.click("pc:-1001:checkin:0",uid=2,private=False)
        self.assertFalse(any(m=="getChatMember" for m,p in self.api.calls))

    def test_private_photo_without_draft_and_id_never_crash(self):
        self.input(photo=[{"file_id":"photo"}])
        self.input("/id",uid=2)
        self.assertIn("2",self.edits()[-1]["text"])

    def test_long_announcement_preview_includes_the_end(self):
        body = "正文"*1700+"必须看到的末尾"
        self.form("schedule_daily","09:00","长公告",body)
        self.assertTrue(any("必须看到的末尾" in p["text"] for p in self.edits()))
        self.confirm()
        self.assertEqual(self.store.schedules(-1001)[0]["body"],body)

    def test_invalid_schedule_time_stays_at_first_field(self):
        self.form("schedule_once","2000-01-01 09:00")
        self.assertEqual(self.bot.admin_ui.drafts[1]["step"],0)
        self.assertFalse(self.store.schedules(-1001))

    def test_preview_send_failure_cannot_leave_valid_confirmation(self):
        self.click("ad:-1001:begin:rules_text")
        original = self.api.safe
        with patch.object(self.api,"safe",side_effect=lambda m,**k: False if m=="sendMessage" else original(m,**k)):
            self.input("新群规")
        self.assertNotIn(1,self.bot.admin_ui.drafts)
        self.assertEqual(self.store.settings(-1001)["rules_text"],DEFAULTS["rules_text"])

    def test_member_quote_edit_failure_binds_replacement_message(self):
        self.store.set(-1001,"points_enabled",True)
        self.store.change_points(-1001,2,100,1)
        item = self.bot.economy.add_item(-1001,1,"奖励","说明",10,3)
        original = self.api.safe
        with patch.object(self.api,"safe",side_effect=lambda m,**k: False if m=="editMessageText" else original(m,**k)):
            self.click(f"pc:-1001:quote:{item}",uid=2)
        quote = self.bot.member_ui.quotes[(-1001,2)]
        self.assertNotEqual(quote["mid"],42)
        self.click(f"pc:-1001:buy:{quote['token']}",uid=2,mid=quote["mid"])
        self.assertEqual(self.store.point_balance(-1001,2)["balance"],90)

    def test_ocr_review_observation_and_late_admin_promotion(self):
        self.store.add_filter(-1001,"word","广告推广","广告推广")
        event = {"chat_id":-1001,"user_id":2,"message_id":30,"created_at":int(time.time())}
        self.store.set(-1001,"ocr_mode","delete")
        self.store.set(-1001,"observe_only",True)
        with patch.object(self.bot.ocr,"results",return_value=iter([(event,"广告推广",90)])):
            self.bot.drain_ocr()
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"],"observed")
        self.assertFalse(any(m=="deleteMessage" for m,p in self.api.calls))
        self.store.set(-1001,"ocr_mode","review")
        with patch.object(self.bot.ocr,"results",return_value=iter([({**event,"user_id":1,"message_id":31},"广告推广",90)])):
            self.bot.drain_ocr()
        self.assertEqual(len(self.store.audit_page(-1001)[0]),1)

    def test_group_menu_and_reply_id(self):
        self.bot.handle_update({"message":self.message("/menu")})
        self.assertIn("member_1001",self.edits()[-1]["reply_markup"]["inline_keyboard"][0][0]["url"])
        self.bot.handle_update({"message":self.message("/id",uid=1,reply_to_message={"from":{"id":123}})})
        self.assertIn("123",self.edits()[-1]["text"])

    def test_observe_records_without_deletion_or_punitive_strikes(self):
        self.store.set(-1001,"observe_only",True)
        self.store.set(-1001,"auto_mute_enabled",True)
        for i in range(4):
            self.bot.handle_update({"message":self.message("https://bad.com",mid=i+1)})
        self.assertEqual(len(self.store.audit_page(-1001)[0]),4)
        self.assertFalse(any(m in ("deleteMessage","restrictChatMember") for m,p in self.api.calls))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM moderation_events").fetchone()[0],0)
        self.assertEqual(self.store.stats(-1001).get("deleted",0),0)

    def test_audit_deleted_failed_review_and_group_scoping(self):
        self.bot.handle_update({"message":self.message("https://bad.com")})
        event = self.store.audit_page(-1001)[0][0]
        self.assertEqual(event["outcome"],"deleted")
        self.click(f"ad:-1002:review:{event['id']}")
        self.assertIsNone(self.store.audit_page(-1001)[0][0]["reviewed_by"])
        self.click(f"ad:-1001:review:{event['id']}")
        self.assertEqual(self.store.audit_page(-1001)[0][0]["reviewed_by"],1)
        with patch.object(self.bot,"bot_can",return_value=False):
            self.bot.handle_update({"message":self.message("https://bad.com",mid=2)})
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"],"failed")

    def test_channel_anonymous_and_automatic_forward_boundaries(self):
        self.store.set(-1001,"block_external_channels",True)
        for i,extra in enumerate(({"sender_chat":{"id":-1001}},
                                 {"sender_chat":{"id":-200},"is_automatic_forward":True})):
            self.bot.handle_update({"message":self.message("https://bad.com",mid=i+1,**extra)})
        self.assertFalse(any(m=="deleteMessage" for m,p in self.api.calls))
        self.bot.handle_update({"message":self.message("频道广告",mid=3,uid=1087968824,
            sender_chat={"id":-200,"type":"channel"})})
        event = self.store.audit_page(-1001)[0][0]
        self.assertEqual(event["target_id"],-200)
        self.assertEqual(event["source"],"channel")
        self.assertFalse(any(m in ("restrictChatMember","banChatMember") for m,p in self.api.calls))

    def test_channel_filter_opt_in_and_observation(self):
        message = self.message("频道广告",sender_chat={"id":-200})
        self.bot.handle_update({"message":message})
        self.assertFalse(self.store.audit_page(-1001)[0])
        self.store.set(-1001,"block_external_channels",True)
        self.store.set(-1001,"observe_only",True)
        self.bot.handle_update({"message":message})
        self.assertEqual(self.store.audit_page(-1001)[0][0]["outcome"],"observed")

    def test_group_reply_rate_limit_across_users(self):
        self.store.set(-1001,"reply_group_limit",2)
        self.store.add_reply(-1001,"帮助","帮助","请阅读说明")
        for uid in range(2,7):
            self.bot.handle_update({"message":self.message("帮助",uid=uid,mid=uid)})
        self.assertEqual(sum(p["text"]=="请阅读说明" for p in self.edits()),2)

    def test_captcha_wrong_answers_lock_and_survive_restart(self):
        self.store.put_pending(-1001,2,"n",0,int(time.time())+300,77)
        def choose(value):
            self.bot.handle_update({"callback_query":{"id":"q","from":{"id":2},
                "data":f"verify:-1001:2:n:{value}","message":{"message_id":77,"chat":{"id":-1001,"type":"supergroup"}}}})
        for _ in range(3):
            choose(1)
        choose(0)
        self.assertIsNotNone(self.store.pending(-1001,2))
        self.assertEqual(self.store.pending(-1001,2)["attempts"],3)
        self.assertFalse(any(m=="restrictChatMember" for m,p in self.api.calls))


class ProtectionAndMigrationTests(unittest.TestCase):
    def test_album_counts_one_event_but_all_captions_inspected(self):
        conf = {**DEFAULTS,"flood_limit":3,"repeat_limit":10,"album_single_enabled":True}
        guard = FloodGuard()
        for i in range(8):
            msg = {"chat":{"id":-1001},"from":{"id":2},"photo":[{}],"media_group_id":"album",
                   "message_id":i+1,"caption":f"图片{i}"}
            self.assertIsNone(evaluate(msg,conf,[],[],guard))
        self.assertEqual(evaluate({**msg,"caption":"违规内容"},conf,["违规内容"],[],guard),"违禁词")
        for i in range(3):
            reason = guard.check(-1001,2,f"正文{i}",message_id=20+i,limit=3,repeat_limit=10)
        self.assertEqual(reason,"短时刷屏")

    def test_album_default_unchanged_and_keyboard_links_filtered(self):
        guard = FloodGuard(limit=3)
        for i in range(4):
            reason = guard.check(-1001,2,str(i),message_id=i)
        self.assertEqual(reason,"短时刷屏")
        message = {"text":"普通正文","reply_markup":{"inline_keyboard":[[{"text":"官网","url":"https://bad.com"}]]}}
        self.assertEqual(evaluate(message,DEFAULTS,[],[]),"外部链接")
        self.assertIsNone(evaluate(message,DEFAULTS,[],["bad.com"]))
        message["reply_markup"]["inline_keyboard"][0][0]["text"] = "违规内容"
        self.assertEqual(evaluate(message,DEFAULTS,["违规内容"],["bad.com"]),"违禁词")

    def test_ocr_exceptions_are_exact_and_only_waive_their_own_word(self):
        exceptions = ["广告推广|广告推广设计课程"]
        self.assertFalse(matched_words("广 告 推 广 设 计 课 程",90,["广告推广"],exceptions=exceptions))
        self.assertTrue(matched_words("广告推广设计课程附带链接",90,["广告推广"],exceptions=exceptions))
        self.assertEqual(matched_words("广告推广设计课程",90,["广告推广","设计课程"],exceptions=exceptions),["设计课程"])

    def test_audit_bounded_deduplicated_and_private(self):
        store = Store()
        self.addCleanup(store.close)
        store.ensure_chat(-1001,"群")
        now = int(time.time())
        store.audit(-1001,1,2,"旧记录","observed",now=now-31*86400)
        for i in range(1005):
            store.audit(-1001,i+2,2,"违禁词","observed",now=now)
        self.assertEqual(store.db.execute("SELECT count(*) FROM moderation_audit").fetchone()[0],1000)
        store.audit(-1001,1006,2,"违禁词","deleted",now=now)
        store.review_audit(-1001,store.audit_page(-1001)[0][0]["id"],1)
        store.audit(-1001,1006,2,"重投删除失败","failed",now=now)
        newest = store.audit_page(-1001)[0][0]
        self.assertEqual(newest["outcome"],"deleted")
        self.assertEqual(newest["reviewed_by"],1)
        rows,page,pages = store.audit_page(-1001,9999)
        self.assertEqual(page,pages-1)
        self.assertEqual(len(rows),8)
        columns = {r[1] for r in store.db.execute("PRAGMA table_info(moderation_audit)")}
        self.assertNotIn("text",columns)
        self.assertNotIn("image",columns)

    def test_migrates_legacy_pending_and_retains_points_and_settings(self):
        with tempfile.TemporaryDirectory() as path:
            dbpath = os.path.join(path,"old.db")
            db = sqlite3.connect(dbpath)
            db.execute("CREATE TABLE pending(chat_id INTEGER,user_id INTEGER,nonce TEXT,answer INTEGER,expires_at INTEGER,message_id INTEGER,PRIMARY KEY(chat_id,user_id))")
            db.execute("INSERT INTO pending VALUES(-1001,2,'n',0,9999999999,77)")
            db.commit()
            db.close()
            store = Store(dbpath)
            store.ensure_chat(-1001,"原群")
            store.set(-1001,"block_links",False)
            store.change_points(-1001,2,100,1)
            store.verification_mistake(-1001,2)
            store.close()
            reopened = Store(dbpath)
            try:
                self.assertEqual(reopened.pending(-1001,2)["attempts"],1)
                self.assertFalse(reopened.settings(-1001)["block_links"])
                self.assertEqual(reopened.point_balance(-1001,2)["balance"],100)
            finally:
                reopened.close()

    def test_ui_purchase_keys_disjoint_from_commands_and_durable(self):
        store = Store()
        self.addCleanup(store.close)
        store.ensure_chat(-1001,"群")
        store.set(-1001,"points_enabled",True)
        store.change_points(-1001,2,100,1)
        store.change_points(-1001,3,100,1)
        economy = Economy(store)
        item = economy.add_item(-1001,1,"奖励","说明",10,10)
        command = economy.purchase(-1001,2,item,1)
        ui = economy.purchase(-1001,2,item,1,request_key="a"*24)
        other = economy.purchase(-1001,3,item,1,request_key="a"*24)
        self.assertEqual(len({command["id"],ui["id"],other["id"]}),3)
        replay = Economy(store).purchase(-1001,2,item,99,request_key="a"*24)
        self.assertFalse(replay["created"])
        self.assertEqual(store.point_balance(-1001,2)["balance"],80)
        self.assertEqual(economy.item(-1001,item)["stock"],7)


if __name__=="__main__":
    unittest.main()
